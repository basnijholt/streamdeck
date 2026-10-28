"""Local control socket: JSON Lines over a Unix socket, for `deck reload|state|press`.

One request per line, one response line per request. The commands only touch what the
daemon already has: reload its config file, read its state, press a configured key. There
is no command that takes an action, and nothing here reaches the Mac.
"""

import asyncio
import contextlib
import json
import logging
import os
import socket
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deck.daemon import Daemon

log = logging.getLogger(__name__)

BAD_REQUEST = "bad request"


def default_control_path() -> Path:
    """`$XDG_RUNTIME_DIR/deck.sock`, or `/tmp/deck-<uid>.sock` when that is not set."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "deck.sock"
    return Path(f"/tmp/deck-{os.getuid()}.sock")


async def serve_control(path: Path, daemon: "Daemon") -> asyncio.Server:
    """Listen on a Unix socket at `path`, readable and writable by this user only.

    A socket file left over from a previous run is replaced; any other file is left alone
    and the call raises `OSError`.
    """
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _serve_client(reader, writer, daemon)

    # Bind by hand so the umask only changes while the file is created, with nothing else
    # running in between; the chmod covers a platform that ignores the umask for sockets.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        old_umask = os.umask(0o177)
        try:
            sock.bind(str(path))
        finally:
            os.umask(old_umask)
        path.chmod(0o600)
        return await asyncio.start_unix_server(handle, sock=sock)
    except BaseException:
        sock.close()
        raise


async def close_control(server: asyncio.Server, path: Path) -> None:
    """Stop listening, drop connected clients (they would hold `wait_closed` open) and unlink."""
    server.close()
    server.close_clients()
    await server.wait_closed()
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


async def call(path: Path, message: dict[str, Any]) -> dict[str, Any]:
    """Send one request to the daemon listening at `path` and return its response.

    Raises `FileNotFoundError` or `ConnectionRefusedError` if no daemon is listening.
    """
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(json.dumps(message).encode() + b"\n")
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()
    if not line:
        raise ConnectionError("the daemon closed the connection without answering")
    return json.loads(line)


async def _serve_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, daemon: "Daemon"
) -> None:
    try:
        while True:
            try:
                line = await reader.readline()
            except ValueError:  # longer than the reader's limit; the rest cannot be trusted
                await _send(writer, {"ok": False, "error": BAD_REQUEST})
                return
            if not line.endswith(b"\n"):
                return  # the client went away, possibly mid-request
            if line.strip():
                await _send(writer, _respond(line, daemon))
    except ConnectionError:
        pass
    finally:
        writer.close()


async def _send(writer: asyncio.StreamWriter, response: dict[str, Any]) -> None:
    writer.write(json.dumps(response, default=str).encode() + b"\n")
    await writer.drain()


def _respond(line: bytes, daemon: "Daemon") -> dict[str, Any]:
    try:
        message = json.loads(line)
    except ValueError:  # includes bad UTF-8
        return {"ok": False, "error": BAD_REQUEST}
    if not isinstance(message, dict):
        return {"ok": False, "error": BAD_REQUEST}
    try:
        return _dispatch(message, daemon)
    except Exception:
        log.exception("control request %r failed", message)
        return {"ok": False, "error": "internal error"}


def _dispatch(message: dict[str, Any], daemon: "Daemon") -> dict[str, Any]:
    match message.get("cmd"):
        case "reload":
            ok = daemon.reload()
            return {"ok": ok, "errors": list(daemon.config_errors)}
        case "state":
            return {"ok": True, "state": daemon.store.snapshot()}
        case "press":
            return _press(message, daemon)
        case _:
            return {"ok": False, "error": "unknown command"}


def _press(message: dict[str, Any], daemon: "Daemon") -> dict[str, Any]:
    page, key, long = message.get("page"), message.get("key"), message.get("long", False)
    if (
        not isinstance(page, str)
        or not isinstance(key, int)
        or isinstance(key, bool)
        or not isinstance(long, bool)
    ):
        return {
            "ok": False,
            "error": f"{BAD_REQUEST}: press needs page (string), key (integer), long (boolean)",
        }
    try:
        daemon.press_key(page, key, long)
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}
