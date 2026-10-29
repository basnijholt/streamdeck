"""The PC's side of the Mac protocol: JSON Lines with `deck.remote.server.serve` on each Mac.

`MacHub` serves the Macs that dial in. Each Mac keeps an SSH connection open to the PC, and
sshd runs `deck attach <name>` for it, which relays the connection to the hub's Unix socket
after an attach line naming the Mac. The hub runs one `MacSession` per name: the hello, the
Mac's state pushes and the replies to requests. The Mac with the latest keyboard or mouse input
is the active one: `mac.*` in the store mirrors it and `request` goes to it.

`MacClient` is the older transport: one persistent SSH process from the PC to the Mac. Its
`run` keeps the process alive: it sends the hello, turns the Mac's state pushes into `mac.*`
keys in the store and answers requests when the Mac replies. When the connection ends it fails
every pending request, marks the Mac offline and reconnects with exponential backoff.

Either way, while no Mac is online `request` raises `MacOffline` at once: presses are dropped,
never queued.
"""

import asyncio
import contextlib
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from deck.config import Action, AudioConfig, MacConfig
from deck.control import bind_private_socket
from deck.mac.attach import valid_mac_name
from deck.proc import forward_stderr, spawn, stop
from deck.state import StateStore

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15  # seconds; a Bluetooth connect is the slow path
BACKOFF_START = 1.0  # seconds before the first reconnect
BACKOFF_MAX = 30.0
STDOUT_LIMIT = 16 << 20  # state lines can be big
ATTACH_TIMEOUT = 5.0  # seconds for a new connection to name its Mac
MAX_ATTACH_LINE = 1024  # bytes, newline included
STALE_AFTER = 5.0  # seconds of silence after which a Mac with a heartbeat counts as gone
STALE_CHECK = 1.0  # seconds between the hub's checks for silent Macs

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


class MacSession:
    """One Mac's connection to the hub: the protocol half of `MacClient`, without the store.

    `state` is the Mac's snapshot with every later diff merged in. `last_input` is when the Mac
    last had keyboard or mouse input, on the PC's clock: the connect time until a state message
    carries `idle_ms`. `idle_ms` changes on every poll, so a Mac that sends it sends a diff every
    second; from its first `idle_ms` on, `heartbeat` is true and a long silence means the
    connection is dead even if TCP has not noticed.
    """

    def __init__(
        self,
        name: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        clock: Callable[[], float],
    ) -> None:
        self.name = name
        self.state: dict[str, Any] = {}
        self.online = False  # from the first state message until the connection ends or is closed
        self.heartbeat = False
        self.last_input = self.last_message = clock()
        self._reader = reader
        self._writer = writer
        self._clock = clock
        self._closed = False
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}

    async def run(
        self, hello: bytes, on_state: Callable[["MacSession", dict[str, Any]], None]
    ) -> None:
        """Send `hello`, then handle the Mac's lines until it hangs up or `close` is called.

        `on_state` gets the values of each state message once they are merged into `state`.
        On the way out every pending request fails with `MacOffline`.
        """
        try:
            self._writer.write(hello)
            await self._writer.drain()
            await self._read(on_state)
        except ConnectionError:
            pass  # the Mac went away abruptly; the same as EOF
        finally:
            self.online = False
            self._closed = True
            pending, self._pending = self._pending, {}
            for future in pending.values():
                if not future.done():
                    future.set_exception(
                        MacOffline(f"the connection to Mac {self.name!r} was lost")
                    )

    async def request(self, kind: str, args: Any, timeout: float = REQUEST_TIMEOUT) -> Any:
        """Send one request and return its `result`.

        Raises `MacOffline` at once when not online (or when the connection ends first) and
        `MacError` for an error reply or when no reply comes within `timeout` seconds.
        """
        if not self.online:
            raise MacOffline(f"Mac {self.name!r} is offline")
        request_id = next(self._ids)
        data = _encode({"id": request_id, "kind": kind, "args": args})
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(timeout):
                self._writer.write(data)
                await self._writer.drain()
                return await future
        except TimeoutError:
            raise MacError(f"{kind} timed out after {timeout:g}s") from None
        except OSError as exc:  # the connection broke
            raise MacOffline(f"the connection to Mac {self.name!r} broke: {exc}") from exc
        finally:
            self._pending.pop(request_id, None)

    def close(self) -> None:
        """Drop the connection now; `run` returns soon after and fails what is pending."""
        self._closed = True
        self.online = False
        self._writer.transport.abort()  # unlike close(), does not wait for a stalled peer

    async def _read(self, on_state: Callable[["MacSession", dict[str, Any]], None]) -> None:
        while line := await self._reader.readline():
            if self._closed:
                return  # what was still buffered when the hub dropped the Mac is not wanted
            self.last_message = self._clock()
            try:
                message = json.loads(line)
            except ValueError:
                log.warning("ignoring a line from Mac %r that is not JSON: %.200r", self.name, line)
                continue
            if not isinstance(message, dict):
                log.warning(
                    "ignoring a message from Mac %r that is not an object: %.200r", self.name, line
                )
            elif isinstance(message.get("state"), dict):
                self._on_state(message["state"], on_state)
            elif isinstance(message.get("id"), int):
                self._on_reply(message)
            else:
                log.warning("unexpected message from Mac %r: %.200r", self.name, line)

    def _on_state(
        self, values: dict[str, Any], on_state: Callable[["MacSession", dict[str, Any]], None]
    ) -> None:
        self.state.update(values)
        idle_ms = values.get("idle_ms")
        if isinstance(idle_ms, int) and not isinstance(idle_ms, bool):
            self.last_input = self._clock() - idle_ms / 1000
            self.heartbeat = True
        if not self.online:
            log.info("Mac %r is online", self.name)
            self.online = True
        on_state(self, values)

    def _on_reply(self, message: dict[str, Any]) -> None:
        future = self._pending.pop(message["id"], None)
        if future is None or future.done():  # it timed out or was cancelled meanwhile
            log.debug("reply to a request nobody waits for: %.200r", message)
        elif message.get("ok") is True:
            future.set_result(message.get("result"))
        else:
            future.set_exception(MacError(str(message.get("error", "unknown error"))))


class MacHub:
    """Serves the Macs that dial in on a Unix socket, and follows the one used last.

    `audio` may be a callable: each connection's hello is built from its value at connect time,
    so audio changes after a config reload reach a Mac when it next connects.

    A connection counts from its first state message: then it takes over its name, closing an
    older session under that name. A session that ends leaves `sessions` only if it is still the
    one registered under its name, so an old connection's teardown never touches a newer one.
    A session that has sent `idle_ms` and then nothing for `STALE_AFTER` seconds is closed, as
    if its Mac had dropped: it slept or lost the network without closing the connection.
    """

    def __init__(
        self,
        path: Path,
        audio: AudioSource,
        store: StateStore,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._audio = audio
        self._store = store
        self._clock = clock
        self.sessions: dict[str, MacSession] = {}  # the online session of each name
        self._active: MacSession | None = None  # the session `mac.*` mirrors
        self._handlers: set[asyncio.Task[Any]] = set()  # one per connection

    @property
    def online(self) -> bool:
        """True while some Mac is online."""
        return self._active is not None

    @property
    def active(self) -> str | None:
        """The name of the Mac that `mac.*` mirrors and requests go to."""
        return None if self._active is None else self._active.name

    async def run(self) -> None:
        """Listen until cancelled, then close every session and remove the socket."""
        self._publish()
        sock = bind_private_socket(self._path)
        try:
            # A reader's limit holds for all its lines: the protocol follows the attach line.
            server = await asyncio.start_unix_server(self._handle, sock=sock, limit=STDOUT_LIMIT)
        except BaseException:
            sock.close()
            raise
        try:
            await self._watch()
        finally:
            server.close()
            server.abort_clients()  # every connection ends, so every handler returns
            await asyncio.gather(*self._handlers, return_exceptions=True)
            await server.wait_closed()
            with contextlib.suppress(FileNotFoundError):
                self._path.unlink()

    async def request(self, kind: str, args: Any, timeout: float = REQUEST_TIMEOUT) -> Any:
        """Send one request to the active Mac and return its `result`.

        Raises `MacOffline` at once when no Mac is online, and when the Mac it was sent to drops
        before answering, even if another Mac is online by then. Raises `MacError` for an error
        reply or when no reply comes within `timeout` seconds.
        """
        session = self._active
        if session is None:
            raise MacOffline("no Mac is online")
        return await session.request(kind, args, timeout)

    # --- connections ----------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            name = await self._read_attach(reader)
            if name is not None:
                await self._serve(MacSession(name, reader, writer, self._clock))
        except Exception:
            log.exception("a Mac connection failed")
        finally:
            self._handlers.discard(task)
            writer.close()

    async def _read_attach(self, reader: asyncio.StreamReader) -> str | None:
        """The Mac's name from the attach line, or None (logged) when it is missing or bad."""
        try:
            async with asyncio.timeout(ATTACH_TIMEOUT):
                line = await reader.readline()
        except TimeoutError:
            log.warning("no attach line within %gs; closing the connection", ATTACH_TIMEOUT)
            return None
        except (ValueError, ConnectionError) as exc:  # ValueError: beyond the reader's limit
            log.warning("cannot read the attach line (%s); closing the connection", exc)
            return None
        if not line.endswith(b"\n"):
            log.warning("the connection ended before its attach line: %.200r", line)
            return None
        if len(line) > MAX_ATTACH_LINE:
            log.warning("attach line of %d bytes is too long; closing the connection", len(line))
            return None
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):  # ValueError includes bad UTF-8
            message = None
        name = message.get("attach") if isinstance(message, dict) else None
        if not isinstance(name, str) or not valid_mac_name(name):
            log.warning("bad attach line %.200r; closing the connection", line)
            return None
        return name

    async def _serve(self, session: MacSession) -> None:
        try:
            await session.run(_hello(self._audio), self._on_state)
        finally:
            if self.sessions.get(session.name) is session:
                del self.sessions[session.name]
            log.info("Mac %r disconnected", session.name)
            self._publish()

    def _on_state(self, session: MacSession, values: dict[str, Any]) -> None:
        older = self.sessions.get(session.name)
        if older is not session:  # its first state message: it takes over the name
            self.sessions[session.name] = session
            if older is not None:
                log.info("Mac %r connected again; closing its older connection", session.name)
                older.close()
        self._publish(session, values)

    async def _watch(self) -> None:
        """Close each session with a heartbeat that has been silent for `STALE_AFTER`."""
        while True:
            await asyncio.sleep(STALE_CHECK)
            now = self._clock()
            stale = [
                s
                for s in self.sessions.values()
                if s.online and s.heartbeat and now - s.last_message > STALE_AFTER
            ]
            for session in stale:
                log.warning(
                    "no word from Mac %r for %.1fs; dropping it",
                    session.name,
                    now - session.last_message,
                )
                session.close()
            if stale:
                self._publish()

    def _publish(
        self, source: MacSession | None = None, values: dict[str, Any] | None = None
    ) -> None:
        """Pick the active Mac and mirror it into `mac.*`; `values` is what `source` just sent."""
        online = [s for s in self.sessions.values() if s.online]
        connected = sorted(s.name for s in online)
        active = max(online, key=lambda s: s.last_input, default=None)
        previous, self._active = self._active, active
        if active is None:
            self._store.update("mac.", {"online": False, "connected": []}, replace=True)
        elif active is not previous:  # the new Mac's whole state replaces the old one's
            log.info("the active Mac is now %r", active.name)
            mirror = {**active.state, "online": True, "active": active.name, "connected": connected}
            self._store.update("mac.", mirror, replace=True)
        elif source is active and values:
            # Only the keys that came: a dial may show a target the Mac has not reached yet.
            self._store.update("mac.", values)
        else:
            self._store.set("mac.connected", connected)


def _hello(audio: AudioSource) -> bytes:
    value = audio() if callable(audio) else audio
    return _encode({"hello": {} if value is None else {"audio": value.model_dump(mode="json")}})


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
