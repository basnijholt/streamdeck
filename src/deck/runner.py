"""Running external commands, behind a seam tests can replace."""

import asyncio
import contextlib
from asyncio.subprocess import DEVNULL, PIPE, Process
from dataclasses import dataclass


@dataclass
class Result:
    rc: int
    out: str
    err: str


class Runner:
    """Runs a program without a shell and collects its output."""

    async def run(self, argv: list[str], timeout: float = 10) -> Result:
        """Run `argv` and wait up to `timeout` seconds.

        A program that cannot be started or does not finish in time (it is killed) gives
        `rc == -1` with the reason in `err`. Cancelling the call kills the program too.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=DEVNULL, stdout=PIPE, stderr=PIPE
            )
        except OSError as exc:
            return Result(-1, "", f"{argv[0]}: {exc.strerror or exc}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            await _kill(proc)
            return Result(-1, "", f"{argv[0]}: timed out after {timeout:g}s")
        except BaseException:
            await _kill(proc)
            raise
        assert proc.returncode is not None
        return Result(
            proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
        )


async def _kill(proc: Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()
