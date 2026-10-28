"""T3 Code: thread status and model selection over the server's WebSocket RPC.

The protocol, and why it is this one and not `/mcp`, is in `docs/t3-protocol.md`. In short: a
WebSocket to `/ws?orchestrationProtocol=2` with the bearer token in the upgrade request carries
Effect RPC messages as JSON. `orchestration.subscribeShell` streams one snapshot of every thread
and then a `thread.updated` / `thread.removed` item per change; `orchestration.getThreadFamily`
reads one thread; `orchestration.dispatchCommand` with `thread.model-selection.set` changes it.

`T3Client.watch` keeps one subscription open and mirrors the server's threads into
`t3.threads.<server>`, with `t3.online.<server>` saying whether it is connected. On a lost
connection the last thread list stays and the client reconnects with exponential backoff.
"""

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException

from deck.config import T3Server
from deck.state import StateStore
from deck.t3.model import Thread

log = logging.getLogger(__name__)

ORCHESTRATION_PROTOCOL = 2  # the server answers 426 to any other version
CONNECT_TIMEOUT = 10.0  # seconds to open the WebSocket
REQUEST_TIMEOUT = 15.0  # seconds for one request, from connecting to the answer
SNAPSHOT_TIMEOUT = 60.0  # seconds for the first snapshot; a cold server took over 10 s
LIVENESS_TIMEOUT = 15.0  # seconds without any message after which the connection counts as dead
MAX_MESSAGE_SIZE = 64 << 20  # the full shell snapshot is 2 MB for 800 threads
BACKOFF_START = 1.0  # seconds before the first reconnect
BACKOFF_MAX = 30.0

# Leave out subagents that are idle and threads settled long ago: neither ever gets a key, and
# the snapshot shrinks from 2 MB to about 100 kB.
SHELL_OPTIONS = {"omitIdleSubagents": True, "omitOlderSettled": True}
REQUEST_ID = "1"  # every connection carries one request
PING = json.dumps({"_tag": "Ping"})
# Acknowledges a stream chunk: the server sends the next one only after this.
ACK = json.dumps({"_tag": "Ack", "requestId": REQUEST_ID})
_ACTIVE_RUN = {"preparing", "starting", "running"}
_THREAD_ID = re.compile(r"[A-Za-z0-9-]+")  # top-level thread ids are UUIDs


class T3Error(Exception):
    """The T3 Code server cannot be reached, or rejected or did not understand a request."""


class T3AuthError(T3Error):
    """The T3 Code server rejected the token."""


@dataclass(frozen=True)
class ModelSelection:
    """A thread's provider instance, model and model options (such as `("effort", "high")`)."""

    instance_id: str
    model: str
    options: tuple[tuple[str, Any], ...]

    @classmethod
    def from_wire(cls, raw: Any) -> "ModelSelection":
        """Read T3's `{instanceId, model, options: [{id, value}]}`; `options` may be absent."""
        try:
            options = tuple((o["id"], o["value"]) for o in raw.get("options") or [])
            selection = cls(raw["instanceId"], raw["model"], options)
        except (AttributeError, KeyError, TypeError) as exc:
            raise T3Error(f"malformed model selection {raw!r}") from exc
        if not isinstance(selection.instance_id, str) or not isinstance(selection.model, str):
            raise T3Error(f"malformed model selection {raw!r}")
        return selection

    def to_wire(self) -> dict[str, Any]:
        return {
            "instanceId": self.instance_id,
            "model": self.model,
            "options": [{"id": key, "value": value} for key, value in self.options],
        }


class T3Source(Protocol):
    """What the deck needs from one T3 Code server."""

    async def list_threads(self) -> list[Thread]: ...

    async def get_model_selection(self, thread_id: str) -> ModelSelection: ...

    async def set_model_selection(self, thread_id: str, selection: ModelSelection) -> None: ...

    def thread_url(self, thread: Thread) -> str: ...

    def thread_id_from_url(self, url: str) -> str | None: ...


def parse_thread(server: str, raw: dict[str, Any]) -> Thread:
    """The `Thread` for a thread shell as `subscribeShell` and `getThreadFamily` send it.

    The status is the shell's `activityRunStatus` when it has one, as `t3 thread list` shows it.
    The shell names only the oldest pending request, so `pending_requests` is 0 or 1.
    """
    try:
        selection = ModelSelection.from_wire(raw["modelSelection"])
        status = raw.get("activityRunStatus") or raw["status"]
        updated_at = datetime.fromisoformat(raw["updatedAt"])
        thread = Thread(
            server=server,
            thread_id=raw["id"],
            project_id=raw["projectId"],
            title=raw["title"],
            status=status,
            pending_requests=1 if _needs_you(raw.get("pendingRuntimeRequest"), status) else 0,
            model=selection.model,
            instance_id=selection.instance_id,
            options=selection.options,
            settled=raw.get("settledOverride") == "settled",
            archived=raw.get("archivedAt") is not None,
            parent_thread_id=(raw.get("lineage") or {}).get("parentThreadId"),
            updated_at=updated_at,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        thread_id = raw.get("id") if isinstance(raw, dict) else None
        raise T3Error(f"malformed thread {thread_id!r}: {exc!r}") from exc
    if not isinstance(thread.thread_id, str) or updated_at.tzinfo is None:
        raise T3Error(f"malformed thread {thread.thread_id!r}")
    return thread


def _needs_you(request: Any, status: str) -> bool:
    """Whether a pending runtime request waits on the user. The rule is T3's own (agentAwareness):
    a question the run keeps working past does not count while the run is active, and an
    `auth_refresh` is the server's business."""
    if not isinstance(request, dict):
        return False
    kind = request.get("kind")
    if kind == "user_input":
        return request.get("blocking") is not False or status not in _ACTIVE_RUN
    return kind != "auth_refresh"


def _messages(raw: str | bytes) -> list[dict[str, Any]]:
    """The RPC messages in one WebSocket frame: a JSON object or an array of them."""
    try:
        decoded = json.loads(raw)
    except ValueError as exc:
        raise T3Error(f"the server sent something that is not JSON: {raw[:200]!r}") from exc
    messages = decoded if isinstance(decoded, list) else [decoded]
    return [m for m in messages if isinstance(m, dict)]


def _dict(value: Any) -> dict[str, Any]:
    """`value` if it is a JSON object, else an empty one: for fields a server might send odd."""
    return value if isinstance(value, dict) else {}


def _exit_value(message: dict[str, Any]) -> Any:
    """The result of an RPC `Exit` message; a failure raises `T3Error` with the server's words."""
    exit_ = _dict(message.get("exit"))
    if exit_.get("_tag") == "Success":
        return exit_.get("value")
    causes = exit_.get("cause")
    for cause in map(_dict, causes if isinstance(causes, list) else []):
        if cause.get("_tag") == "Fail":
            error = cause.get("error")
            if isinstance(error, dict):
                error = error.get("message") or error.get("_tag") or error
            raise T3Error(str(error))
        if cause.get("_tag") == "Die":
            raise T3Error(f"the server failed: {cause.get('defect')}")
    raise T3Error(f"the request failed: {str(message.get('exit'))[:200]}")


def _raise_if_fatal(message: dict[str, Any]) -> None:
    """`Defect` and `ClientProtocolError` end every request on the connection."""
    if message.get("_tag") == "Defect":
        raise T3Error(f"the server failed: {message.get('defect')}")
    if message.get("_tag") == "ClientProtocolError":
        raise T3Error(f"the server did not understand the request: {message.get('error')}")


def _is_authoritative_snapshot(item: dict[str, Any]) -> bool:
    """A full snapshot. Snapshots that name `resolvedRepositoryIdentityRoots` only refresh projects
    and carry no threads."""
    return item.get("kind") == "snapshot" and "resolvedRepositoryIdentityRoots" not in item


def _request(tag: str, payload: dict[str, Any]) -> str:
    return json.dumps(
        {"_tag": "Request", "id": REQUEST_ID, "tag": tag, "payload": payload, "headers": []}
    )


class T3Client(T3Source):
    """One T3 Code server. `sleep` waits out the reconnect backoff of `watch`."""

    def __init__(
        self,
        server: T3Server,
        token: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        parts = urlsplit(server.url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(
                f"t3 server {server.name}: url must be http or https, not {server.url!r}"
            )
        scheme = "wss" if parts.scheme == "https" else "ws"
        base = f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}"
        self._ws_url = f"{base}/ws?orchestrationProtocol={ORCHESTRATION_PROTOCOL}"
        self._server = server
        self._token = token
        self._sleep = sleep
        self._web_url = server.web_url.rstrip("/")
        web = urlsplit(self._web_url)
        self._web_origin = (web.scheme, web.hostname, web.port)
        self._web_path = web.path
        self._delay = BACKOFF_START

    @property
    def name(self) -> str:
        return self._server.name

    # --- one-shot requests ------------------------------------------------------

    async def list_threads(self) -> list[Thread]:
        """Every thread of the server that can get a key (see `SHELL_OPTIONS`)."""
        with self._as_t3_errors():
            async with asyncio.timeout(SNAPSHOT_TIMEOUT), self._connect() as ws:
                await ws.send(_request("orchestration.subscribeShell", SHELL_OPTIONS))
                while True:
                    for message in _messages(await ws.recv()):
                        snapshot = self._snapshot_in(message)
                        if snapshot is not None:
                            return list(snapshot.values())

    async def get_model_selection(self, thread_id: str) -> ModelSelection:
        family = await self._call("orchestration.getThreadFamily", {"threadId": thread_id})
        threads = family.get("threads") if isinstance(family, dict) else None
        for raw in threads or []:
            if isinstance(raw, dict) and raw.get("id") == thread_id:
                return ModelSelection.from_wire(raw.get("modelSelection"))
        raise T3Error(f"thread {thread_id} not found on t3 server {self.name}")

    async def set_model_selection(self, thread_id: str, selection: ModelSelection) -> None:
        """Set the thread's model selection; send every option, the server replaces the lot."""
        command = {
            "type": "thread.model-selection.set",
            "commandId": f"deck:{uuid.uuid4()}",
            "threadId": thread_id,
            "modelSelection": selection.to_wire(),
        }
        await self._call("orchestration.dispatchCommand", command)

    async def _call(self, tag: str, payload: dict[str, Any]) -> Any:
        """Send one request on a connection of its own and return its result."""
        with self._as_t3_errors():
            async with asyncio.timeout(REQUEST_TIMEOUT), self._connect() as ws:
                await ws.send(_request(tag, payload))
                while True:
                    for message in _messages(await ws.recv()):
                        _raise_if_fatal(message)
                        if message.get("_tag") == "Exit" and message.get("requestId") == REQUEST_ID:
                            return _exit_value(message)

    @contextlib.contextmanager
    def _as_t3_errors(self) -> Iterator[None]:
        """Turn a timeout or a network failure into `T3Error`, the one error callers catch."""
        try:
            yield
        except TimeoutError:
            raise T3Error(f"t3 server {self.name} timed out") from None
        except (OSError, WebSocketException) as exc:
            raise T3Error(f"t3 server {self.name} unreachable: {exc}") from None

    @contextlib.asynccontextmanager
    async def _connect(self) -> AsyncIterator[ClientConnection]:
        try:
            async with connect(
                self._ws_url,
                additional_headers={"Authorization": f"Bearer {self._token}"},
                open_timeout=CONNECT_TIMEOUT,
                max_size=MAX_MESSAGE_SIZE,
                proxy=None,
            ) as ws:
                yield ws
        except InvalidStatus as exc:
            status = exc.response.status_code
            body = exc.response.body.decode(errors="replace")[:200] if exc.response.body else ""
            if status in {401, 403}:
                raise T3AuthError(f"t3 server {self.name} rejected the token: {body}") from None
            raise T3Error(
                f"t3 server {self.name} refused the connection ({status}): {body}"
            ) from None

    # --- the live thread list ---------------------------------------------------

    async def watch(self, store: StateStore, interval: float = 1.0) -> None:
        """Mirror the server's threads into the store until cancelled; never raises.

        Every `interval` seconds without a message the connection is pinged; one that stays
        silent for `LIVENESS_TIMEOUT` seconds is dropped and reconnected.
        """
        online_key = f"t3.online.{self.name}"
        store.set(online_key, False)
        try:
            while True:
                try:
                    await self._follow(store, interval)
                except T3AuthError as exc:
                    log.error("%s", exc)
                    self._delay = BACKOFF_MAX  # the same token will not work on a retry
                except (OSError, WebSocketException, T3Error) as exc:  # TimeoutError is one
                    log.warning("t3 server %s: %s", self.name, exc or type(exc).__name__)
                except Exception:
                    log.exception("t3 server %s: connection failed", self.name)
                finally:
                    store.set(online_key, False)
                delay, self._delay = self._delay, min(self._delay * 2, BACKOFF_MAX)
                log.warning("t3 server %s down, retrying in %gs", self.name, delay)
                await self._sleep(delay)
        finally:
            store.set(online_key, False)

    async def _follow(self, store: StateStore, interval: float) -> None:
        """One connection: subscribe, then apply every change. Returns or raises when it ends."""
        loop = asyncio.get_running_loop()
        threads: dict[str, Thread] | None = None  # None until the first snapshot
        async with self._connect() as ws:
            await ws.send(_request("orchestration.subscribeShell", SHELL_OPTIONS))
            started = heard = loop.time()
            while True:
                try:
                    async with asyncio.timeout(interval):
                        raw = await ws.recv()
                except TimeoutError:
                    now = loop.time()
                    if threads is None and now - started > SNAPSHOT_TIMEOUT:
                        raise T3Error(f"no thread list within {SNAPSHOT_TIMEOUT:g}s") from None
                    if threads is not None and now - heard > LIVENESS_TIMEOUT:
                        raise T3Error(
                            f"no word from the server for {LIVENESS_TIMEOUT:g}s"
                        ) from None
                    await ws.send(PING)
                    continue
                heard = loop.time()
                changed = False
                for message in _messages(raw):
                    snapshot = self._snapshot_in(message)
                    if snapshot is not None:
                        threads, changed = snapshot, True
                        self._delay = BACKOFF_START
                        log.info("t3 server %s: following %d threads", self.name, len(threads))
                    if message.get("_tag") != "Chunk" or message.get("requestId") != REQUEST_ID:
                        continue
                    if snapshot is None and threads is not None:
                        changed |= self._apply(threads, message.get("values") or [])
                    await ws.send(ACK)
                if changed and threads is not None:
                    store.update(
                        "t3.",
                        {
                            f"threads.{self.name}": list(threads.values()),
                            f"online.{self.name}": True,
                        },
                    )

    def _snapshot_in(self, message: dict[str, Any]) -> dict[str, Thread] | None:
        """The threads of the shell subscription's full snapshot in `message`, if it has one.

        Raises `T3Error` when `message` ends the subscription.
        """
        _raise_if_fatal(message)
        if message.get("requestId") != REQUEST_ID:
            return None
        if message.get("_tag") == "Exit":
            _exit_value(message)
            raise T3Error("the server ended the thread subscription")
        if message.get("_tag") != "Chunk":
            return None
        snapshot = None
        for item in message.get("values") or []:
            if isinstance(item, dict) and _is_authoritative_snapshot(item):
                body = item.get("snapshot")
                if not isinstance(body, dict):
                    raise T3Error(f"malformed thread snapshot: {str(body)[:200]}")
                snapshot = {}
                for raw in body.get("threads") or []:
                    self._put(snapshot, raw)
            elif snapshot is not None and isinstance(item, dict):
                self._apply(snapshot, [item])
        return snapshot

    def _apply(self, threads: dict[str, Thread], items: list[Any]) -> bool:
        """Apply shell stream items to `threads`; True if anything changed."""
        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind == "thread.updated" and item.get("location") == "active":
                changed |= self._put(threads, item.get("thread"))
            elif kind in {"thread.updated", "thread.removed"}:  # removed, or moved to the archive
                thread_id = item.get("threadId") or _dict(item.get("thread")).get("id")
                changed |= threads.pop(thread_id, None) is not None
        return changed

    def _put(self, threads: dict[str, Thread], raw: Any) -> bool:
        try:
            thread = parse_thread(self.name, raw)
        except T3Error as exc:
            log.warning("t3 server %s: skipping a thread: %s", self.name, exc)
            return False
        if threads.get(thread.thread_id) == thread:
            return False
        threads[thread.thread_id] = thread
        return True

    # --- URLs -------------------------------------------------------------------

    def thread_url(self, thread: Thread) -> str:
        return f"{self._web_url}/{self._server.environment_id}/{thread.thread_id}"

    def thread_id_from_url(self, url: str) -> str | None:
        """The thread a `thread_url` of this server points at; None for any other URL.

        Scheme, host and port must be `web_url`'s, with no user info, and the path exactly
        `/<environment_id>/<threadId>` where the id is letters, digits and dashes. A query or
        fragment is ignored.
        """
        try:
            parts = urlsplit(url)
            origin = (parts.scheme, parts.hostname, parts.port)
        except ValueError:
            return None
        if parts.hostname is None or "@" in parts.netloc or origin != self._web_origin:
            return None
        prefix = f"{self._web_path}/{self._server.environment_id}/"
        thread_id = parts.path.removeprefix(prefix)
        if not parts.path.startswith(prefix) or not _THREAD_ID.fullmatch(thread_id):
            return None
        return thread_id
