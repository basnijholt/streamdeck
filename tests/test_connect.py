import asyncio
import contextlib
import gc
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from deck import proc as proc_module
from deck.remote.connect import connect_forever, ssh_argv

FAKE_PC = str(Path(__file__).with_name("fake_pc.py"))


def pc_command(log_file: Path, *flags: str) -> list[str]:
    return [sys.executable, FAKE_PC, str(log_file), *flags]


class FakeExecutor:
    def __init__(self) -> None:
        self.hellos: list[dict[str, Any]] = []

    def configure(self, hello: dict[str, Any]) -> None:
        self.hellos.append(hello)

    async def run(self, kind: str, args: Any) -> Any:
        return args


class FakePoller:
    async def snapshot(self) -> dict[str, Any]:
        return {"volume": 42}


class FakeSleep:
    """Records the backoff delays and returns at once."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


async def eventually(check, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def connected(argv: list[str], sleep, executor=None):
    """Run `connect_forever` for the duration of the block, then cancel it."""
    task = asyncio.create_task(
        connect_forever(argv, executor or FakeExecutor(), FakePoller(), sleep=sleep)
    )
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def processes_running(marker: str) -> list[int]:
    """Ids of the processes whose command line contains `marker`."""
    found = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (entry / "cmdline").read_bytes().decode(errors="replace")
        except OSError:  # it exited meanwhile
            continue
        if marker in cmdline and int(entry.name) != os.getpid():
            found.append(int(entry.name))
    return found


def sent(log_file: Path) -> list[dict]:
    """What the fake PC has received from the Mac so far."""
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text().splitlines()]


@pytest.fixture
def fake_sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def log_file(tmp_path) -> Path:
    return tmp_path / "from-the-mac.jsonl"


# --- argv -------------------------------------------------------------------


def test_ssh_argv():
    assert ssh_argv("basnijholt@192.168.1.5", Path("/k")) == [
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
        "/k",
        "basnijholt@192.168.1.5",
    ]
    assert "-i" not in ssh_argv("pc", None)


# --- serving ----------------------------------------------------------------


async def test_serves_the_child_until_it_exits(log_file, fake_sleep, caplog):
    executor = FakeExecutor()
    with caplog.at_level(logging.INFO, logger="deck.remote.connect"):
        # the fake PC hangs up after the Mac's first line, which is the state snapshot
        async with connected(pc_command(log_file, "--lines", "1"), fake_sleep, executor):
            await eventually(lambda: len(fake_sleep.delays) >= 1)
    assert executor.hellos[0] == {}
    assert sent(log_file)[0] == {"state": {"volume": 42}}
    assert "connected to the PC" in caplog.text


# --- backoff ----------------------------------------------------------------


async def test_backoff_doubles_to_30(fake_sleep):
    async with connected([sys.executable, "-c", "pass"], fake_sleep):
        await eventually(lambda: len(fake_sleep.delays) >= 7)
    assert fake_sleep.delays[:7] == [1, 2, 4, 8, 16, 30, 30]


async def test_backoff_restarts_after_a_hello(log_file, fake_sleep):
    # every session gets the PC's hello and is then dropped: the delay never grows
    async with connected(pc_command(log_file, "--lines", "1"), fake_sleep):
        await eventually(lambda: len(fake_sleep.delays) >= 3)
    assert fake_sleep.delays[:3] == [1.0, 1.0, 1.0]


# --- stderr -----------------------------------------------------------------


async def test_ssh_stderr_is_logged(fake_sleep, caplog):
    script = "import sys; print('Permission denied (publickey).', file=sys.stderr)"
    with caplog.at_level(logging.WARNING):
        async with connected([sys.executable, "-c", script], fake_sleep):
            await eventually(lambda: "Permission denied" in caplog.text)
    assert ("deck.proc", logging.WARNING, "ssh: Permission denied (publickey).") in (
        caplog.record_tuples
    )


# --- cleanup ----------------------------------------------------------------


async def test_cancelling_leaves_no_child_process(log_file, fake_sleep):
    async with connected(pc_command(log_file), fake_sleep) as task:
        await eventually(lambda: sent(log_file))
        (pid,) = processes_running(str(log_file))
        assert process_exists(pid)
    assert task.cancelled()
    assert not process_exists(pid)


async def test_cancelling_while_the_child_starts_leaves_no_child(log_file, fake_sleep):
    task = asyncio.create_task(
        connect_forever(pc_command(log_file), FakeExecutor(), FakePoller(), sleep=fake_sleep)
    )
    await asyncio.sleep(0)  # the child is being started
    task.cancel()
    async with asyncio.timeout(5):
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert processes_running(str(log_file)) == []


@pytest.mark.filterwarnings("ignore::ResourceWarning")
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_asyncio_run_ending_while_the_child_starts_does_not_hang(log_file):
    async def main() -> None:
        asyncio.create_task(connect_forever(pc_command(log_file), FakeExecutor(), FakePoller()))
        await asyncio.sleep(0)  # the child is being started; asyncio.run now cancels the task

    thread = threading.Thread(target=asyncio.run, args=(main(),), daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    assert processes_running(str(log_file)) == []
    gc.collect()  # asyncio leaves the pipes of a spawn cancelled this early unclosed


async def test_a_second_cancel_while_the_child_starts_still_leaves_no_child(log_file, fake_sleep):
    task = asyncio.create_task(
        connect_forever(pc_command(log_file), FakeExecutor(), FakePoller(), sleep=fake_sleep)
    )
    await asyncio.sleep(0)  # the child is being started
    task.cancel()
    await asyncio.sleep(0)  # the cancelled loop waits for the spawn to finish
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await eventually(lambda: processes_running(str(log_file)) == [])  # the spawn's child dies


async def test_a_child_that_ignores_sigterm_is_killed(monkeypatch, log_file, fake_sleep):
    monkeypatch.setattr(proc_module, "TERMINATE_GRACE", 0.2)
    async with connected(pc_command(log_file, "--stubborn"), fake_sleep):
        await eventually(lambda: sent(log_file))
        (pid,) = processes_running(str(log_file))
    assert not process_exists(pid)


async def test_a_failed_spawn_is_survived(fake_sleep, caplog):
    with caplog.at_level(logging.WARNING):
        async with connected(["/nonexistent/deck-pc"], fake_sleep) as task:
            await eventually(lambda: len(fake_sleep.delays) >= 2)
            assert not task.done()
    assert "deck-pc" in caplog.text
    assert "PC connection down, retrying in 1s" in caplog.text
