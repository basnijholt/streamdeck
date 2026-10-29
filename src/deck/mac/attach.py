"""`deck attach NAME`: the PC end of a Mac's dial-out SSH connection.

sshd runs it as the forced command of that Mac's key, so the name comes from the key, not
from the Mac. It relays the connection to the daemon's `MacHub` socket after an attach line
naming the Mac, then copies bytes both ways without looking at them. This module also holds
what the hub and `attach` agree on: which names are valid and where the socket lives.
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")


def valid_mac_name(name: str) -> bool:
    """Whether `name` is 1 to 32 ASCII letters, digits, `_` or `-`."""
    return _NAME.fullmatch(name) is not None


def default_mac_socket() -> Path:
    """`$XDG_RUNTIME_DIR/deck-mac.sock`, or `/tmp/deck-mac-<uid>.sock` when that is not set."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "deck-mac.sock"
    return Path(f"/tmp/deck-mac-{os.getuid()}.sock")


CHUNK = 1 << 16  # bytes moved per read


async def attach(name: str, path: Path) -> int:
    """Relay stdin and stdout to the daemon's socket at `path` as Mac `name`; the exit status.

    Returns 0 once either side hangs up, and 1 with a message on stderr when `name` is invalid
    or nothing listens on `path`. The messages reach the Mac through ssh, which logs them.
    """
    if not valid_mac_name(name):
        print(f"deck attach: invalid name {name!r}", file=sys.stderr)
        return 1
    try:
        socket_reader, socket_writer = await asyncio.open_unix_connection(path)
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"deck is not running (nothing is listening on {path})", file=sys.stderr)
        return 1
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    stdout = asyncio.StreamWriter(transport, protocol, stdin, loop)
    socket_writer.write(json.dumps({"attach": name}).encode() + b"\n")
    pumps = [
        asyncio.create_task(_pump(stdin, socket_writer)),
        asyncio.create_task(_pump(socket_reader, stdout)),
    ]
    try:
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pump in pumps:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        socket_writer.close()
        stdout.close()
    return 0


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy `reader` to `writer` until `reader` ends or `writer`'s peer is gone."""
    try:
        while chunk := await reader.read(CHUNK):
            writer.write(chunk)
            await writer.drain()
    except ConnectionError:  # the peer went away abruptly; the same as EOF
        pass
