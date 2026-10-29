"""`deck connect`: the Mac dials out to the PC and serves the PC's requests over it.

The child is an ssh to the PC. The PC's `authorized_keys` forces a command on that key (`deck
attach <name>`), so the child's stdout carries the PC's lines and its stdin takes the Mac's
replies and state pushes: `serve` runs over those two pipes exactly as it does over a local
stdio. When the connection ends, for whatever reason, the loop reconnects with exponential
backoff. A session that got the PC's hello before it ended starts the backoff over, so a PC
that restarts is back within a second.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from deck.proc import forward_stderr, spawn, stop
from deck.remote.server import Executor, Poller, serve

log = logging.getLogger(__name__)

BACKOFF_START = 1.0  # seconds before the first reconnect
BACKOFF_MAX = 30.0
STDOUT_LIMIT = 16 << 20  # StreamReader buffer limit for ssh's stdout; serve caps lines itself


def ssh_argv(destination: str, key: Path | None) -> list[str]:
    """The ssh command that reaches the PC. No remote command: the PC's key forces one.

    `ControlPath=none` keeps it off a multiplexed connection the user's own ssh may share, and
    `IdentitiesOnly` makes it offer only `key`: an agent key that the PC accepts for a shell
    would otherwise log in without the forced `deck attach`.
    """
    argv = [
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
        "-o",
        "ControlPath=none",
    ]
    if key is not None:
        argv += ["-o", "IdentitiesOnly=yes", "-i", str(key)]
    return [*argv, destination]


async def connect_forever(
    argv: list[str],
    executor: Executor,
    poller: Poller,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Connect, serve and reconnect until cancelled; the child process never outlives it."""
    delay = BACKOFF_START
    while True:
        watched = _Watched(executor)
        try:
            await _session(argv, watched, poller)
        except OSError as exc:
            log.warning("PC connection failed: %s", exc)
        except Exception:
            log.exception("PC connection failed")
        if watched.hello_seen:
            delay = BACKOFF_START
        log.warning("PC connection down, retrying in %gs", delay)
        await sleep(delay)
        delay = min(delay * 2, BACKOFF_MAX)


async def _session(argv: list[str], executor: Executor, poller: Poller) -> None:
    proc = await spawn(argv, STDOUT_LIMIT)
    assert proc.stdin and proc.stdout and proc.stderr  # all three are PIPEs
    stderr_task = asyncio.create_task(forward_stderr(proc.stderr, "ssh"))
    try:
        await serve(proc.stdout, proc.stdin, executor, poller)
    finally:
        await stop(proc, stderr_task)


class _Watched:
    """The executor, noting that the PC's hello arrived: that is when a session counts as made."""

    def __init__(self, executor: Executor) -> None:
        self._executor = executor
        self.hello_seen = False

    def configure(self, hello: dict[str, Any]) -> None:
        if not self.hello_seen:
            self.hello_seen = True
            log.info("connected to the PC")
        self._executor.configure(hello)

    async def run(self, kind: str, args: Any) -> Any:
        return await self._executor.run(kind, args)
