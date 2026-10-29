"""`MacHub` and `MacSession` against fake Macs: `serve()` on a real hub socket in a temp dir."""

import asyncio
import contextlib
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from deck.config import AudioConfig, AudioOutput
from deck.mac import client as mac_client
from deck.mac.attach import default_mac_socket, valid_mac_name
from deck.mac.client import MacError, MacHub, MacOffline
from deck.remote.server import RemoteError, serve
from deck.state import StateStore

AUDIO = AudioConfig(
    outputs=[AudioOutput(name="iLoud", match="iLoud", bt="aa-bb-cc")],
    inputs=["Q9", "MV7"],
    pin_input=True,
)
HELLO = {"hello": {"audio": AUDIO.model_dump(mode="json")}}


class Clock:
    """A monotonic clock that only moves when the test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Executor:
    """Records hellos and requests; answers with the args, or with what `handler` returns."""

    def __init__(self, handler=None) -> None:
        self.handler = handler
        self.hellos: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []

    def configure(self, hello: dict[str, Any]) -> None:
        self.hellos.append(hello)

    async def run(self, kind: str, args: Any) -> Any:
        self.calls.append((kind, args))
        if self.handler is None:
            return args
        return await self.handler(kind, args)


class Poller:
    """Answers each poll with a copy of `state`, which the test changes between polls."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = dict(state)

    async def snapshot(self) -> dict[str, Any]:
        return dict(self.state)


class FakeMac:
    def __init__(self, writer: asyncio.StreamWriter, task: asyncio.Task, executor: Executor):
        self.writer = writer
        self.task = task  # `serve`, which returns once either side hangs up
        self.executor = executor

    async def hang_up(self) -> None:
        self.writer.close()
        await asyncio.wait_for(self.task, 2)


def attach_line(name: str) -> bytes:
    return json.dumps({"attach": name}).encode() + b"\n"


async def hold(kind: str, args: Any) -> Any:
    await asyncio.Event().wait()  # never answers


async def eventually(check, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def fake_mac(path: Path, name: str, poller: Poller, executor: Executor | None = None):
    """A Mac dialed in as `name`: the attach line, then `serve()` on the hub connection."""
    executor = executor or Executor()
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(attach_line(name))
    await writer.drain()
    task = asyncio.create_task(serve(reader, writer, executor, poller, interval=0.01))
    try:
        yield FakeMac(writer, task, executor)
    finally:
        writer.close()
        await asyncio.wait_for(task, 2)


@contextlib.asynccontextmanager
async def running(hub: MacHub, path: Path):
    """Run `hub.run()` for the duration of the block, then cancel it."""
    task = asyncio.create_task(hub.run())
    await eventually(lambda: path.exists() or task.done())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def store() -> StateStore:
    return StateStore()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def path(tmp_path) -> Path:
    return tmp_path / "mac.sock"


@pytest.fixture
async def hub(path, store, clock, monkeypatch):
    monkeypatch.setattr(mac_client, "STALE_CHECK", 0.01)
    hub = MacHub(path, AUDIO, store, clock=clock)
    async with running(hub, path):
        yield hub


# --- names and paths --------------------------------------------------------


@pytest.mark.parametrize(
    "name, ok",
    [
        ("mini", True),
        ("home-2", True),
        ("my_mac", True),
        ("A" * 32, True),
        ("", False),
        ("A" * 33, False),
        ("bad name", False),
        ("mini\n", False),
        ("../mini", False),
        ("wörk", False),
    ],
)
def test_valid_mac_name(name, ok):
    assert valid_mac_name(name) is ok


def test_default_mac_socket_follows_xdg_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert default_mac_socket() == tmp_path / "deck-mac.sock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert default_mac_socket() == Path(f"/tmp/deck-mac-{os.getuid()}.sock")


# --- one Mac ----------------------------------------------------------------


async def test_request_reaches_the_mac_and_state_is_mirrored(hub, path, store):
    poller = Poller({"volume": 42, "audio.output": "iLoud", "idle_ms": 0})
    async with fake_mac(path, "mini", poller) as mac:
        await eventually(lambda: store.get("mac.online") is True)
        assert store.get("mac.active") == "mini"
        assert store.get("mac.connected") == ["mini"]
        assert store.get("mac.volume") == 42
        assert hub.online and hub.active == "mini"

        assert await hub.request("app", "Ghostty") == "Ghostty"
        assert mac.executor.calls == [("app", "Ghostty")]

        poller.state["volume"] = 50
        await eventually(lambda: store.get("mac.volume") == 50)
        assert store.get("mac.audio.output") == "iLoud"


async def test_a_diff_only_writes_the_keys_it_carries(hub, path, store):
    poller = Poller({"volume": 42, "idle_ms": 0})
    async with fake_mac(path, "mini", poller):
        await eventually(lambda: store.get("mac.online") is True)
        store.set("mac.volume", 44)  # a dial shows its target before the Mac answers
        poller.state["idle_ms"] = 7
        await eventually(lambda: store.get("mac.idle_ms") == 7)
        assert store.get("mac.volume") == 44


async def test_hello_carries_the_audio_config(hub, path):
    async with fake_mac(path, "mini", Poller({})) as mac:
        await eventually(lambda: hub.online)
    assert mac.executor.hellos == [HELLO["hello"]]


async def test_hello_is_built_at_each_connect(path, store, clock):
    audio: list[AudioConfig | None] = [None]
    hub = MacHub(path, lambda: audio[0], store, clock=clock)
    async with running(hub, path):
        async with fake_mac(path, "mini", Poller({})) as first:
            await eventually(lambda: first.executor.hellos)
        audio[0] = AUDIO
        async with fake_mac(path, "mini", Poller({})) as second:
            await eventually(lambda: second.executor.hellos)
    assert first.executor.hellos == [{}]
    assert second.executor.hellos == [HELLO["hello"]]


async def test_no_mac_is_offline(hub, store):
    assert store.get("mac.online") is False
    assert store.get("mac.connected") == []
    assert not hub.online
    assert hub.active is None
    with pytest.raises(MacOffline):
        await asyncio.wait_for(hub.request("app", "Ghostty"), 0.5)


async def test_mac_counts_from_its_first_snapshot(hub, path, store):
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(attach_line("mini"))
        assert json.loads(await asyncio.wait_for(reader.readline(), 2)) == HELLO
        assert not hub.online
        assert store.get("mac.connected") == []
        with pytest.raises(MacOffline):
            await hub.request("app", "Ghostty")

        writer.write(b'{"state": {"volume": 42}}\n')
        await eventually(lambda: hub.online)
        assert store.get("mac.volume") == 42
    finally:
        writer.close()


async def test_error_reply_raises_mac_error(hub, path):
    async def fail(kind: str, args: Any) -> Any:
        raise RemoteError("boom")

    async with fake_mac(path, "mini", Poller({}), Executor(fail)):
        await eventually(lambda: hub.online)
        with pytest.raises(MacError, match="boom"):
            await hub.request("km", "macro")
        assert hub.online


async def test_request_times_out_with_mac_error(hub, path):
    async with fake_mac(path, "mini", Poller({}), Executor(hold)):
        await eventually(lambda: hub.online)
        with pytest.raises(MacError, match="timed out after 0.1s"):
            await hub.request("app", "Ghostty", timeout=0.1)
        assert hub.online


async def test_big_lines_are_read(hub, path, store):
    async def big(kind: str, args: Any) -> Any:
        return "x" * args

    async with fake_mac(path, "mini", Poller({"big": "y" * 300_000}), Executor(big)):
        await eventually(lambda: hub.online)
        assert len(store.get("mac.big")) == 300_000
        assert len(await hub.request("km", 300_000)) == 300_000


# --- several Macs -----------------------------------------------------------


async def test_active_is_the_mac_with_the_latest_input(hub, path, store, clock):
    mini = Poller({"volume": 10, "audio.output": "iLoud", "mic.muted": True, "idle_ms": 0})
    home = Poller({"volume": 20, "audio.output": "Mac", "idle_ms": 50_000})
    async with fake_mac(path, "mini", mini) as mini_mac, fake_mac(path, "home", home) as home_mac:
        await eventually(lambda: store.get("mac.connected") == ["home", "mini"])
        assert store.get("mac.active") == "mini"
        assert store.get("mac.volume") == 10

        clock.now += 1
        home.state["idle_ms"] = 0  # someone just used the home Mac
        await eventually(lambda: store.get("mac.active") == "home")
        assert hub.active == "home"
        assert store.get("mac.audio.output") == "Mac"
        assert store.get("mac.volume") == 20
        assert "mac.mic.muted" not in store.snapshot()  # the mini Mac's state is gone
        assert store.get("mac.connected") == ["home", "mini"]

        assert await hub.request("app", "Safari") == "Safari"
        assert home_mac.executor.calls == [("app", "Safari")]
        assert mini_mac.executor.calls == []


async def test_active_mac_dropping_falls_back(hub, path, store):
    async with fake_mac(path, "home", Poller({"volume": 20, "idle_ms": 50_000})) as home:
        async with fake_mac(path, "mini", Poller({"volume": 10, "idle_ms": 0})) as mini:
            await eventually(lambda: store.get("mac.active") == "mini")
            await mini.hang_up()
            await eventually(lambda: store.get("mac.active") == "home")
            assert store.get("mac.volume") == 20
            assert store.get("mac.connected") == ["home"]
        await home.hang_up()
        await eventually(lambda: store.get("mac.online") is False)
    assert store.get("mac.connected") == []
    assert "mac.volume" not in store.snapshot()
    assert not hub.online


async def test_request_fails_when_its_mac_drops(hub, path):
    mini_executor = Executor(hold)
    async with (
        fake_mac(path, "home", Poller({"idle_ms": 50_000})),
        fake_mac(path, "mini", Poller({"idle_ms": 0}), mini_executor) as mini,
    ):
        await eventually(lambda: hub.active == "mini")
        pending = asyncio.create_task(hub.request("app", "Ghostty"))
        await eventually(lambda: mini_executor.calls)
        await mini.hang_up()
        with pytest.raises(MacOffline):
            await asyncio.wait_for(pending, 2)
        assert hub.active == "home"


async def test_same_name_replaces_the_older_session(hub, path, store):
    async with fake_mac(path, "mini", Poller({"volume": 10})) as old:
        await eventually(lambda: store.get("mac.volume") == 10)
        async with fake_mac(path, "mini", Poller({"volume": 20})) as new:
            await asyncio.wait_for(old.task, 2)  # the hub hung up on the older one
            await eventually(lambda: store.get("mac.volume") == 20)
            assert store.get("mac.connected") == ["mini"]
            assert await hub.request("app", "Ghostty") == "Ghostty"
            assert new.executor.calls == [("app", "Ghostty")]
            assert old.executor.calls == []


async def test_old_session_teardown_leaves_new_one_alone(hub, path, store):
    online: list[Any] = []

    def record(keys: set[str]) -> None:
        if "mac.online" in keys:
            online.append(store.get("mac.online"))

    store.subscribe(record)
    async with fake_mac(path, "mini", Poller({"volume": 10, "bt.connected": ["aa"]})) as old:
        await eventually(lambda: store.get("mac.online") is True)
        online.clear()
        async with fake_mac(path, "mini", Poller({"volume": 20})) as new:
            await asyncio.wait_for(old.task, 2)
            await eventually(lambda: store.get("mac.volume") == 20)
            await asyncio.sleep(0.05)  # let the hub finish tearing the older session down
            assert online == []  # mac.online never left True
            assert hub.online
            assert "mac.bt.connected" not in store.snapshot()
            assert await hub.request("app", "Ghostty") == "Ghostty"
            assert new.executor.calls == [("app", "Ghostty")]


# --- attach line ------------------------------------------------------------


async def test_attach_line_framing(hub, path, store):
    # The attach line and the first protocol line in one write.
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(attach_line("mini") + b'{"state": {"volume": 42}}\n')
    await eventually(lambda: store.get("mac.volume") == 42)
    assert json.loads(await asyncio.wait_for(reader.readline(), 2)) == HELLO
    assert store.get("mac.active") == "mini"
    writer.close()
    await eventually(lambda: not hub.online)

    # The attach line split over two writes.
    reader, writer = await asyncio.open_unix_connection(path)
    line = attach_line("home")
    writer.write(line[:7])
    await writer.drain()
    await asyncio.sleep(0.05)
    writer.write(line[7:] + b'{"state": {"volume": 7}}\n')
    await eventually(lambda: store.get("mac.active") == "home")
    assert store.get("mac.volume") == 7
    writer.close()


@pytest.mark.parametrize(
    "line",
    [
        b"nope\n",
        b'{"attach": "bad name"}\n',
        b'{"attach": 5}\n',
        b'{"x": 1}\n',
        b'["mini"]\n',
        b"[" * 1000 + b"\n",
        b"a" * 2000 + b"\n",
        json.dumps({"attach": "mini", "pad": "x" * 2000}).encode() + b"\n",
    ],
)
async def test_bad_attach_line_is_closed(hub, path, store, caplog, line):
    with caplog.at_level(logging.WARNING, logger="deck.mac.client"):
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(line)
        assert await asyncio.wait_for(reader.read(), 2) == b""  # closed without a hello
        writer.close()
    assert "attach" in caplog.text
    assert store.get("mac.connected") == []


async def test_silent_attach_is_closed_after_5s(hub, path, caplog, monkeypatch):
    assert mac_client.ATTACH_TIMEOUT == 5.0
    monkeypatch.setattr(mac_client, "ATTACH_TIMEOUT", 0.1)
    with caplog.at_level(logging.WARNING, logger="deck.mac.client"):
        reader, writer = await asyncio.open_unix_connection(path)
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
    assert "attach" in caplog.text


# --- silence ----------------------------------------------------------------


async def test_silent_session_is_closed(hub, path, store, clock):
    assert mac_client.STALE_AFTER == 5.0
    clock.now = 900.0
    async with fake_mac(path, "home", Poller({"volume": 20})):  # no idle_ms: no heartbeat
        await eventually(lambda: hub.active == "home")
        clock.now = 1000.0
        async with fake_mac(path, "mini", Poller({"volume": 10, "idle_ms": 0})) as mini:
            await eventually(lambda: hub.active == "mini")
            clock.now += 4.9
            await asyncio.sleep(0.1)  # ten watchdog checks
            assert hub.active == "mini"

            clock.now += 0.2  # 5.1 s without a word from the mini Mac
            await asyncio.wait_for(mini.task, 2)  # the hub hung up on it
            await eventually(lambda: hub.active == "home")
            assert store.get("mac.volume") == 20
            assert store.get("mac.connected") == ["home"]


async def test_mac_without_idle_ms_is_never_closed_for_silence(hub, path, clock):
    async with fake_mac(path, "mini", Poller({"volume": 10})) as mac:
        await eventually(lambda: hub.online)
        clock.now += 60
        await asyncio.sleep(0.1)  # ten watchdog checks
        assert hub.online
        assert not mac.task.done()
        assert await hub.request("app", "Ghostty") == "Ghostty"


# --- lifecycle --------------------------------------------------------------


async def test_socket_is_private_and_removed_on_exit(path, store, clock):
    hub = MacHub(path, None, store, clock=clock)
    old = os.umask(0)
    try:
        task = asyncio.create_task(hub.run())
        await eventually(lambda: path.exists() or task.done())
    finally:
        os.umask(old)
    assert stat.S_ISSOCK(path.lstat().st_mode)
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert not path.exists()


async def test_cancelling_run_closes_every_session(path, store, clock):
    hub = MacHub(path, None, store, clock=clock)
    task = asyncio.create_task(hub.run())
    await eventually(lambda: path.exists() or task.done())
    async with fake_mac(path, "mini", Poller({"volume": 10}), Executor(hold)) as mac:
        await eventually(lambda: hub.online)
        pending = asyncio.create_task(hub.request("app", "Ghostty"))
        await eventually(lambda: mac.executor.calls)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        with pytest.raises(MacOffline):
            await asyncio.wait_for(pending, 1)
        await asyncio.wait_for(mac.task, 2)  # the hub hung up on the Mac
        assert not hub.online
        assert store.get("mac.online") is False
