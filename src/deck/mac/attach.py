"""`deck attach NAME`: the PC end of a Mac's dial-out SSH connection.

sshd runs it as the forced command of that Mac's key, so the name comes from the key, not
from the Mac. It relays the connection to the daemon's `MacHub` socket after an attach line
naming the Mac. This module holds what both sides agree on: which names are valid and where
the socket lives.
"""

import os
import re
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
