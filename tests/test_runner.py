import asyncio
import os
import sys
import time

import pytest

from deck.runner import Result, Runner


async def test_captures_output_and_exit_code():
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    assert await Runner().run([sys.executable, "-c", code]) == Result(3, "out\n", "err\n")


async def test_timeout_kills_the_process_and_returns_minus_one():
    started = time.monotonic()
    result = await Runner().run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
    assert result.rc == -1
    assert "timed out" in result.err
    assert time.monotonic() - started < 5


async def test_missing_program_returns_minus_one():
    result = await Runner().run(["deck-no-such-program"])
    assert result.rc == -1
    assert "deck-no-such-program" in result.err


async def test_cancellation_kills_the_process(tmp_path):
    pid_file = tmp_path / "pid"
    code = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    task = asyncio.create_task(Runner().run([sys.executable, "-c", code]))
    while not pid_file.exists() or not pid_file.read_text():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_does_not_use_a_shell():
    result = await Runner().run(["echo", "$HOME;", "echo", "x"])
    assert result.out == "$HOME; echo x\n"
