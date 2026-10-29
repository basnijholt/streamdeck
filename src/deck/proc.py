"""Child processes that talk over pipes, started and stopped so that none is ever left behind.

Used by the Mac's `deck connect` (an ssh out to the PC): `spawn` starts the child with all three
streams piped, `forward_stderr` logs what it writes there and `stop` ends it, however the caller
got cancelled.
"""

import asyncio
import contextlib
import logging
from asyncio.subprocess import PIPE, Process

log = logging.getLogger(__name__)

TERMINATE_GRACE = 2.0  # seconds a terminated child has to exit before it is killed
STDERR_GRACE = 1.0  # seconds to let the last of the child's stderr through
SPAWN_GRACE = 1.0  # seconds a spawn cancelled halfway has to finish before it is cancelled again
MAX_LOG_LINE = 4096  # characters of a stderr line that are logged


async def spawn(argv: list[str], limit: int) -> Process:
    """Start the child, without letting a cancellation hit asyncio's spawn halfway.

    A spawn cancelled before its pipes are connected waits forever for them to close, which
    would hang the shutdown. So the spawn runs as its own task; when this one is cancelled
    it is given a moment to finish, its child is stopped, and only a spawn that still hangs
    is cancelled (that second cancel is what unblocks it).
    """
    starting = asyncio.ensure_future(
        asyncio.create_subprocess_exec(*argv, stdin=PIPE, stdout=PIPE, stderr=PIPE, limit=limit)
    )
    try:
        return await asyncio.shield(starting)
    except asyncio.CancelledError:
        # Whatever happens from here, even a second cancel, a child that appears must die.
        starting.add_done_callback(_kill_spawned)
        _, unfinished = await asyncio.wait({starting}, timeout=SPAWN_GRACE)
        for task in unfinished:
            task.cancel()
        (outcome,) = await asyncio.gather(starting, return_exceptions=True)
        if isinstance(outcome, Process):
            await stop(outcome, None)
        raise


async def forward_stderr(stream: asyncio.StreamReader, prefix: str) -> None:
    """Log the child's stderr line by line, so the pipe never fills up and blocks it.

    `deck attach` reports its errors there, and ssh reports its own failures there (a refused
    connection, a rejected key). Each line is logged as a warning, after `prefix`.
    """
    pending = b""
    while chunk := await stream.read(1 << 16):
        *lines, pending = (pending + chunk).split(b"\n")
        pending = pending[-MAX_LOG_LINE:]  # a line without a newline stays bounded
        for line in lines:
            _log_stderr(line, prefix)
    _log_stderr(pending, prefix)


def _log_stderr(line: bytes, prefix: str) -> None:
    text = line.decode(errors="replace").strip()
    if text:
        log.warning("%s: %s", prefix, text[:MAX_LOG_LINE])


def _kill_spawned(starting: asyncio.Future[Process]) -> None:
    """Done-callback of a spawn nobody is waiting for any more: its child must not live on."""
    if starting.cancelled() or starting.exception() is not None:
        return
    proc = starting.result()
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    if proc.stdin is not None:
        proc.stdin.close()


async def stop(proc: Process, stderr_task: asyncio.Task[None] | None) -> None:
    """Close the child's stdin, terminate it, kill it if it lingers: no process is left behind."""
    assert proc.stdin
    proc.stdin.close()
    try:
        await _end(proc)
    finally:  # also when cancelled again while waiting
        if stderr_task is not None:
            try:
                await asyncio.wait({stderr_task}, timeout=STDERR_GRACE)
            finally:
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)


async def _end(proc: Process) -> None:
    try:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), TERMINATE_GRACE)
    finally:  # also when cancelled again while waiting
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
