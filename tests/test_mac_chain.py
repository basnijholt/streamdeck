"""`deck connect` to `deck attach` to `MacHub`, without ssh: the whole chain on this machine."""

import asyncio
import contextlib
import json
import sys
from typing import Any

import pytest

from deck.cli import main
from deck.mac.attach import default_mac_socket
from deck.mac.client import MacHub
from deck.remote.connect import connect_forever
from deck.state import StateStore

ATTACH = [
    sys.executable,
    "-c",
    "from deck.cli import main; raise SystemExit(main(['attach', 'mini']))",
]


class FakeExecutor:
    """Records the requests it runs and answers each with `done`."""

    def __init__(self) -> None:
        self.hello: dict[str, Any] | None = None
        self.calls: list[tuple[str, Any]] = []

    def configure(self, hello: dict[str, Any]) -> None:
        self.hello = hello

    async def run(self, kind: str, args: Any) -> Any:
        self.calls.append((kind, args))
        return "done"


class FakePoller:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    async def snapshot(self) -> dict[str, Any]:
        return dict(self.state)


async def eventually(check, timeout: float = 15.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@pytest.fixture
def store() -> StateStore:
    return StateStore()


async def test_connect_attach_hub_chain(store):
    path = default_mac_socket()
    hub = MacHub(path, None, store)
    executor = FakeExecutor()
    hub_task = connect_task = None
    try:
        hub_task = asyncio.create_task(hub.run())
        await eventually(lambda: path.exists() or hub_task.done())
        connect_task = asyncio.create_task(
            connect_forever(ATTACH, executor, FakePoller({"volume": 42, "idle_ms": 0}))
        )
        await eventually(lambda: store.get("mac.active") == "mini")
        await eventually(lambda: store.get("mac.volume") == 42)
        assert store.get("mac.online") is True
        assert await hub.request("app", "Ghostty") == "done"
        assert executor.calls == [("app", "Ghostty")]
        assert executor.hello == {}  # the hub had no audio config to send
    finally:
        for task in (connect_task, hub_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert not path.exists()


def test_attach_without_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert main(["attach", "mini"]) == 1
    assert "deck is not running" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["bad name", "", "a" * 33, "../x"])
def test_attach_rejects_a_bad_name(name, capsys):
    assert main(["attach", name]) == 1
    assert "invalid name" in capsys.readouterr().err


async def test_attach_relays_both_ways_and_ends_when_the_daemon_hangs_up():
    received = bytearray()
    first_line = asyncio.Event()
    ended = asyncio.Event()

    async def daemon(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.extend(await reader.readline())  # the attach line
        first_line.set()
        writer.write(b'{"hello": {}}\n')
        await writer.drain()
        received.extend(await reader.readline())  # what the Mac sent on
        await ended.wait()
        writer.close()

    path = default_mac_socket()
    server = await asyncio.start_unix_server(daemon, path=path)
    proc = await asyncio.create_subprocess_exec(
        *ATTACH,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(20):
            await first_line.wait()
            assert json.loads(received) == {"attach": "mini"}
            assert await proc.stdout.readline() == b'{"hello": {}}\n'
            proc.stdin.write(b'{"state": {"volume": 1}}\n')
            await proc.stdin.drain()
            while received.count(b"\n") < 2:
                await asyncio.sleep(0.01)
            assert received.endswith(b'{"state": {"volume": 1}}\n')
            ended.set()  # the daemon hangs up: attach returns, and so does ssh on the Mac
            assert await proc.wait() == 0
            assert await proc.stdout.read() == b""
            assert await proc.stderr.read() == b""
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        server.close()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


async def test_attach_ends_when_stdin_closes():
    attached = asyncio.Event()

    async def daemon(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()  # the attach line
        attached.set()
        await reader.read()  # until attach hangs up
        writer.close()

    path = default_mac_socket()
    server = await asyncio.start_unix_server(daemon, path=path)
    proc = await asyncio.create_subprocess_exec(
        *ATTACH,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(20):
            await attached.wait()
            proc.stdin.close()  # ssh ended: the Mac is gone
            assert await proc.wait() == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        server.close()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
