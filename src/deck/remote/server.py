"""`deck remote` protocol server: JSON Lines between the PC-side daemon and the Mac.

One JSON object per line in both directions. The PC sends a `hello` first, then requests
`{"id", "kind", "args"}`; the Mac answers each request with `{"id", "ok", "result"|"error"}`
and pushes `{"state": {...}}`: the full snapshot once, then only the keys that changed.

Only the kinds in `ALLOWED_KINDS` are ever handed to the executor. There is no generic shell
kind, and `url`/`tab` URLs are checked again here, whatever the PC's config said.

stdout is the protocol channel: nothing in here may print to it. Diagnostics go through
`logging`, which reaches stderr.
"""

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from typing import Any, Protocol
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

ALLOWED_KINDS = frozenset(
    {
        "app",
        "url",
        "tab",
        "audio.output",
        "audio.input",
        "mic.mute",
        "volume",
        "screenshot",
        "km",
        "script",
        "lock",
        "active_url",
    }
)

BAD_REQUEST = "bad request"
INTERNAL_ERROR = "internal error"
MAX_LINE_BYTES = 1 << 20
_READ_CHUNK = 64 << 10


class RemoteError(Exception):
    """An executor failure whose message is safe and useful to show the PC."""


class Executor(Protocol):
    def configure(self, hello: dict[str, Any]) -> None: ...

    async def run(self, kind: str, args: Any) -> Any:
        """Run one allowlisted request; the return value becomes the response's `result`."""
        ...


class Poller(Protocol):
    async def snapshot(self) -> dict[str, Any]: ...


async def serve(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    executor: Executor,
    poller: Poller,
    interval: float = 1.0,
) -> None:
    """Serve one PC connection until it hangs up; the caller owns (and closes) `writer`.

    State pushes start once the PC's `hello` arrives (a request that comes first counts as an
    empty hello). A request runs as its own task, so a slow one never holds up the next line,
    the other requests or the state pushes. On EOF everything still running is cancelled.
    """
    await _Session(writer, executor, poller, interval).run(reader)


async def serve_stdio(executor: Executor, poller: Poller) -> None:
    """`serve` over this process's stdin and stdout."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    try:
        await serve(reader, writer, executor, poller)
    finally:
        writer.close()  # every write was already drained


class _Session:
    def __init__(
        self, writer: asyncio.StreamWriter, executor: Executor, poller: Poller, interval: float
    ) -> None:
        self._writer = writer
        self._executor = executor
        self._poller = poller
        self._interval = interval
        self._write_lock = asyncio.Lock()
        self._requests: set[asyncio.Task[None]] = set()
        self._state_task: asyncio.Task[None] | None = None  # set once configured

    async def run(self, reader: asyncio.StreamReader) -> None:
        try:
            async for line in _read_lines(reader):
                await self._handle_line(line)
        except ConnectionError:  # the PC went away abruptly; same as EOF
            pass
        finally:
            await self._stop()

    async def _stop(self) -> None:
        tasks = list(self._requests)
        if self._state_task is not None:
            tasks.append(self._state_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_line(self, line: bytes | None) -> None:
        """Act on one line; every failure is answered, none is raised."""
        if line is None:  # longer than MAX_LINE_BYTES
            await self._reject(None, BAD_REQUEST)
            return
        if not line.strip():
            return
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):  # ValueError includes bad UTF-8
            await self._reject(None, BAD_REQUEST)
            return
        if not isinstance(message, dict):
            await self._reject(None, BAD_REQUEST)
            return

        if "hello" in message:
            hello = message["hello"]
            if isinstance(hello, dict):
                await self._configure(hello)
            else:
                await self._reject(None, BAD_REQUEST)
            return

        request_id, kind, args = message.get("id"), message.get("kind"), message.get("args")
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            await self._reject(None, BAD_REQUEST)  # nothing to correlate an answer with
        elif not isinstance(kind, str):
            await self._reject(request_id, BAD_REQUEST)
        elif kind not in ALLOWED_KINDS:
            await self._reject(request_id, f"kind not allowed: {kind}")
        elif (error := _args_error(kind, args)) is not None:
            await self._reject(request_id, error)
        else:
            if self._state_task is None:  # a request came before any hello
                await self._configure({})
            task = asyncio.create_task(self._execute(request_id, kind, args))
            self._requests.add(task)
            task.add_done_callback(self._requests.discard)

    async def _configure(self, hello: dict[str, Any]) -> None:
        try:
            self._executor.configure(hello)
        except Exception:
            log.exception("configuring the executor failed")
            await self._reject(None, INTERNAL_ERROR)
        if self._state_task is None:
            self._state_task = asyncio.create_task(self._push_state())

    async def _execute(self, request_id: int, kind: str, args: Any) -> None:
        try:
            result = await self._executor.run(kind, args)
            data = _encode({"id": request_id, "ok": True, "result": result})
        except RemoteError as exc:
            data = _encode({"id": request_id, "ok": False, "error": str(exc)})
        except Exception:  # includes a result JSON cannot hold
            log.exception("request %s (%s) failed", request_id, kind)
            data = _encode({"id": request_id, "ok": False, "error": INTERNAL_ERROR})
        await self._write(data)

    async def _push_state(self) -> None:
        sent: dict[str, Any] = {}  # what the PC knows
        first = True
        while True:
            try:
                snapshot = await self._poller.snapshot()
                changed = {k: v for k, v in snapshot.items() if k not in sent or sent[k] != v}
                if first or changed:
                    await self._write(_encode({"state": changed}))
                    sent.update(changed)
                    first = False
            except Exception:
                log.exception("polling the state failed; retrying")
            await asyncio.sleep(self._interval)

    async def _reject(self, request_id: int | None, error: str) -> None:
        response: dict[str, Any] = {"ok": False, "error": error}
        if request_id is not None:
            response = {"id": request_id, **response}
        await self._write(_encode(response))

    async def _write(self, data: bytes) -> None:
        """Write one whole line; the lock keeps concurrent writers from overlapping."""
        async with self._write_lock:
            try:
                self._writer.write(data)
                await self._writer.drain()
            except ConnectionError:
                pass  # the PC is gone; the read loop sees EOF next


async def _read_lines(reader: asyncio.StreamReader) -> AsyncIterator[bytes | None]:
    """Yield each line without its newline; `None` stands for one longer than MAX_LINE_BYTES.

    Reads in chunks rather than with `readline`, so an overlong line is reported as soon as it
    is known to be too long, its rest is dropped up to the next newline, and memory stays
    bounded. A trailing partial line at EOF is dropped: the PC went away mid-request.
    """
    buffer = bytearray()
    discarding = False  # inside the rest of a line already reported as too long
    while chunk := await reader.read(_READ_CHUNK):
        buffer += chunk
        while (end := buffer.find(b"\n")) >= 0:
            line = bytes(buffer[:end])
            del buffer[: end + 1]
            if discarding:
                discarding = False
            elif len(line) > MAX_LINE_BYTES:
                yield None
            else:
                yield line
        if discarding:
            buffer.clear()
        elif len(buffer) > MAX_LINE_BYTES:
            buffer.clear()
            discarding = True
            yield None


def _encode(message: dict[str, Any]) -> bytes:
    """One line of strict JSON; raises `ValueError`/`TypeError` for a value JSON cannot hold."""
    return json.dumps(message, allow_nan=False).encode() + b"\n"


def _args_error(kind: str, args: Any) -> str | None:
    """Why `args` is refused for `kind`, or None. Only the URL kinds are checked here."""
    if kind == "url":
        url = args
    elif kind == "tab":
        url = args.get("url") if isinstance(args, dict) else None
    else:
        return None
    return None if _is_http_url(url) else "url must be an http or https URL"


def _is_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    # urlsplit silently drops some whitespace and control characters, so it would judge a
    # different string than the one `open` receives.
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)
