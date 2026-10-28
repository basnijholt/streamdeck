"""The PC's client of `deck remote`: one persistent SSH process to the Mac, JSON Lines over it.

`MacClient.run` keeps the process alive: it sends the hello, turns the Mac's state pushes into
`mac.*` keys in the store and answers requests when the Mac replies. When the connection ends
it fails every pending request, marks the Mac offline and reconnects with exponential backoff.
While offline, `request` raises `MacOffline` at once: presses are dropped, never queued.
"""

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from deck.config import Action, AudioConfig, MacConfig
from deck.proc import forward_stderr, spawn, stop
from deck.state import StateStore

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15  # seconds; a Bluetooth connect is the slow path
BACKOFF_START = 1.0  # seconds before the first reconnect
BACKOFF_MAX = 30.0
STDOUT_LIMIT = 16 << 20  # state lines can be big

AudioSource = AudioConfig | None | Callable[[], AudioConfig | None]


class MacOffline(Exception):
    """The Mac is not connected, or the connection ended before it answered."""


class MacError(Exception):
    """The Mac answered with an error, or did not answer in time."""


class MacClient:
    """Talks to `deck remote`. `audio` may be a callable: the hello is built from its value at
    each (re)connect, so audio changes after a config reload reach the Mac on the next one."""

    def __init__(
        self,
        cfg: MacConfig,
        audio: AudioSource,
        store: StateStore,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cfg = cfg
        self._audio = audio
        self._store = store
        self._sleep = sleep
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._writer: asyncio.StreamWriter | None = None  # set from the first snapshot on
        self._delay = BACKOFF_START

    @property
    def online(self) -> bool:
        """True from the first state snapshot of a connection until the connection ends."""
        return self._writer is not None

    def argv(self) -> list[str]:
        if self._cfg.command:
            return list(self._cfg.command)
        return [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=3",
            "-o",
            "ServerAliveCountMax=2",
            "-i",
            str(self._cfg.key.expanduser()),
            self._cfg.host,
            "deck",
            "remote",
        ]

    async def run(self) -> None:
        """Connect, serve and reconnect until cancelled; the child process never outlives it."""
        self._store.set("mac.online", False)
        while True:
            try:
                await self._session()
            except OSError as exc:
                log.warning("Mac connection failed: %s", exc)
            except Exception:
                log.exception("Mac connection failed")
            delay, self._delay = self._delay, min(self._delay * 2, BACKOFF_MAX)
            log.warning("Mac connection down, retrying in %gs", delay)
            await self._sleep(delay)

    async def request(self, kind: str, args: Any, timeout: float = REQUEST_TIMEOUT) -> Any:
        """Send one request and return its `result`.

        Raises `MacOffline` at once when not connected (or when the connection ends first) and
        `MacError` for an error reply or when no reply comes within `timeout` seconds.
        """
        writer = self._writer
        if writer is None:
            raise MacOffline("the Mac is offline")
        request_id = next(self._ids)
        data = _encode({"id": request_id, "kind": kind, "args": args})
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(timeout):
                writer.write(data)
                await writer.drain()
                return await future
        except TimeoutError:
            raise MacError(f"{kind} timed out after {timeout:g}s") from None
        except OSError as exc:  # the pipe broke: the connection is gone
            raise MacOffline(f"the Mac connection broke: {exc}") from exc
        finally:
            self._pending.pop(request_id, None)

    # --- one connection -------------------------------------------------------

    async def _session(self) -> None:
        proc = await spawn(self.argv(), STDOUT_LIMIT)
        assert proc.stdin and proc.stdout and proc.stderr  # all three are PIPEs
        stderr_task = asyncio.create_task(forward_stderr(proc.stderr, "Mac"))
        try:
            proc.stdin.write(self._hello())
            await proc.stdin.drain()
            await self._read(proc.stdin, proc.stdout)
        finally:
            self._go_offline()
            await stop(proc, stderr_task)

    def _hello(self) -> bytes:
        audio = self._audio() if callable(self._audio) else self._audio
        hello = {} if audio is None else {"audio": audio.model_dump(mode="json")}
        return _encode({"hello": hello})

    async def _read(self, writer: asyncio.StreamWriter, reader: asyncio.StreamReader) -> None:
        """Handle the Mac's lines until it hangs up."""
        while line := await reader.readline():
            try:
                message = json.loads(line)
            except ValueError:
                log.warning("ignoring a line from the Mac that is not JSON: %.200r", line)
                continue
            if not isinstance(message, dict):
                log.warning("ignoring a message from the Mac that is not an object: %.200r", line)
            elif isinstance(message.get("state"), dict):
                self._on_state(message["state"], writer)
            elif isinstance(message.get("id"), int):
                self._on_reply(message)
            else:
                log.warning("unexpected message from the Mac: %.200r", line)

    def _on_state(self, values: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        if self._writer is None:  # the first message of a connection is the full snapshot
            self._store.update("mac.", {**values, "online": True}, replace=True)
            self._writer = writer
            self._delay = BACKOFF_START
            log.info("connected to the Mac")
        else:
            self._store.update("mac.", values)

    def _on_reply(self, message: dict[str, Any]) -> None:
        future = self._pending.pop(message["id"], None)
        if future is None or future.done():  # it timed out or was cancelled meanwhile
            log.debug("reply to a request nobody waits for: %.200r", message)
        elif message.get("ok") is True:
            future.set_result(message.get("result"))
        else:
            future.set_exception(MacError(str(message.get("error", "unknown error"))))

    def _go_offline(self) -> None:
        self._writer = None
        self._store.set("mac.online", False)
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(MacOffline("the Mac connection was lost"))


def _encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, allow_nan=False).encode() + b"\n"


# --- actions ----------------------------------------------------------------


async def handle_mac_action(client: MacClient, audio: AudioConfig | None, action: Action) -> None:
    """Run a `mac.*` action: the kind is the type without its `mac.` prefix.

    An `audio.output` action is sent with its output's `match` and `bt` from `audio`, because
    the Mac only knows what the hello told it. Raises whatever `client.request` raises.
    """
    kind = action.type.removeprefix("mac.")
    args = action.args
    if kind == "audio.output":
        output = next((o for o in audio.outputs if o.name == args.name), None) if audio else None
        if output is None:
            raise ValueError(f"unknown audio output {args.name!r}")
        args = {
            "name": output.name,
            "match": output.match,
            "bt": output.bt,
            "disconnect": args.disconnect,
        }
    elif isinstance(args, BaseModel):
        args = args.model_dump(mode="json")
    await client.request(kind, args)
