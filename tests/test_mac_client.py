import asyncio
import contextlib
import gc
import json
import logging
import os
import sys
import threading
from pathlib import Path

import pytest

from deck import cli
from deck import proc as proc_module
from deck.actions import ActionRunner
from deck.config import (
    Action,
    AudioConfig,
    AudioOutput,
    Config,
    DialSpec,
    MacConfig,
    OutputArgs,
)
from deck.dials import DIAL_FACTORIES, Services
from deck.mac.client import MacClient, MacError, MacOffline, handle_mac_action
from deck.mac.dials import InputDial, VolumeDial
from deck.state import StateStore

STUB = str(Path(__file__).with_name("stub_remote.py"))
AUDIO = AudioConfig(
    outputs=[
        AudioOutput(name="iLoud", match="iLoud", bt="aa-bb-cc"),
        AudioOutput(name="Mac", match="MacBook Pro Speakers"),
    ],
    inputs=["Q9", "MV7"],
    pin_input=True,
)


def stub_command(state_file: Path, *flags: str) -> list[str]:
    return [sys.executable, STUB, str(state_file), *flags]


def mac_config(command: list[str]) -> MacConfig:
    return MacConfig(host="macbook", key=Path("/nonexistent/key"), command=command)


class FakeSleep:
    """Records the backoff delays and returns at once, or when `gate` is set."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(0)


async def eventually(check, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def running(client: MacClient):
    """Run `client.run()` for the duration of the block, then cancel it."""
    task = asyncio.create_task(client.run())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def received(client: MacClient, kind: str | None = None) -> list[dict]:
    """What the stub got so far: the hello and the requests, optionally of one kind."""
    log = await client.request("log", None)
    return [m for m in log if kind is None or m.get("kind") == kind]


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


@pytest.fixture
def store() -> StateStore:
    return StateStore()


@pytest.fixture
def fake_sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def state_file(tmp_path) -> Path:
    """The snapshot the stub sends on connect; tests rewrite it between connections."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"audio.output": "iLoud", "volume": 42}))
    return path


@pytest.fixture
async def client(state_file, store, fake_sleep):
    client = MacClient(mac_config(stub_command(state_file)), AUDIO, store, sleep=fake_sleep)
    async with running(client):
        await eventually(lambda: store.get("mac.online") is True)
        yield client


# --- argv -------------------------------------------------------------------


def test_argv_is_the_ssh_command(monkeypatch, store):
    monkeypatch.setenv("HOME", "/home/someone")
    cfg = MacConfig(host="macbook-m5", key=Path("~/.config/deck/mac_ed25519"))
    assert MacClient(cfg, None, store).argv() == [
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
        "/home/someone/.config/deck/mac_ed25519",
        "macbook-m5",
        "deck",
        "remote",
    ]


def test_argv_prefers_the_configured_command(store):
    cfg = MacConfig(host="macbook", key=Path("/k"), command=["my", "cmd"])
    assert MacClient(cfg, None, store).argv() == ["my", "cmd"]


# --- requests and state -----------------------------------------------------


async def test_request_roundtrip_and_state(client, store):
    assert store.get("mac.audio.output") == "iLoud"
    assert store.get("mac.volume") == 42
    assert await client.request("echo", {"a": [1, 2]}) == {"a": [1, 2]}
    assert await client.request("app", "Ghostty") == "Ghostty"
    assert [m["kind"] for m in await received(client, "app")] == ["app"]


async def test_hello_carries_the_audio_config(client):
    (hello,) = [m for m in await received(client) if "hello" in m]
    assert hello == {"hello": {"audio": AUDIO.model_dump()}}


async def test_hello_without_audio_config(state_file, store, fake_sleep):
    client = MacClient(mac_config(stub_command(state_file)), None, store, sleep=fake_sleep)
    async with running(client):
        await eventually(lambda: client.online)
        assert (await received(client))[0] == {"hello": {}}


async def test_hello_uses_the_audio_config_of_each_connect(state_file, store, fake_sleep):
    audio = [AudioConfig(outputs=[], inputs=["Q9"])]
    client = MacClient(
        mac_config(stub_command(state_file)), lambda: audio[0], store, sleep=fake_sleep
    )
    async with running(client):
        await eventually(lambda: client.online)
        assert (await received(client))[0]["hello"]["audio"]["inputs"] == ["Q9"]
        audio[0] = AudioConfig(outputs=[], inputs=["MV7"])
        with pytest.raises(MacOffline):
            await client.request("die", None)
        await eventually(lambda: client.online)
        assert (await received(client))[0]["hello"]["audio"]["inputs"] == ["MV7"]


async def test_error_reply_raises_mac_error(client):
    with pytest.raises(MacError, match="boom"):
        await client.request("fail", None)
    assert await client.request("echo", 1) == 1  # the connection is fine


async def test_request_times_out_with_mac_error(client):
    with pytest.raises(MacError, match="timed out after 0.1s"):
        await client.request("hang", None, timeout=0.1)
    assert client.online
    assert await client.request("echo", 1) == 1


async def test_replies_are_matched_by_id(client):
    await client.request("hold", ["slow"])
    slow = asyncio.create_task(client.request("slow", "first"))
    assert await client.request("fast", "second") == "second"
    assert not slow.done()
    await client.request("release", None)
    assert await slow == "first"


async def test_state_changes_merge_into_the_store(client, store):
    await client.request("push", {"volume": 50, "mic.muted": True})
    await eventually(lambda: store.get("mac.volume") == 50)
    assert store.get("mac.mic.muted") is True
    assert store.get("mac.audio.output") == "iLoud"  # only the changed keys came


async def test_big_lines_are_read(client):
    result = await client.request("big", 300_000)
    assert len(result) == 300_000


async def test_stderr_is_drained_and_logged(client, caplog):
    with caplog.at_level(logging.WARNING, logger="deck.mac.client"):
        # 20k lines are far more than a pipe holds: the stub blocks unless stderr is read.
        await client.request("noise", 20_000, timeout=10)
        await eventually(lambda: "stub noise 19999" in caplog.text)
    assert "stub noise 0" in caplog.text


# --- offline ----------------------------------------------------------------


async def test_offline_request_raises_immediately(store):
    client = MacClient(mac_config(["unused"]), None, store)
    assert not client.online
    with pytest.raises(MacOffline):
        await client.request("echo", 1)


async def test_request_before_the_first_snapshot_raises_offline(store, fake_sleep):
    silent = [sys.executable, "-c", "import time; time.sleep(60)"]
    client = MacClient(mac_config(silent), None, store, sleep=fake_sleep)
    async with running(client):
        await eventually(lambda: store.get("mac.online") is False)
        await asyncio.sleep(0.1)
        with pytest.raises(MacOffline):
            await client.request("echo", 1)
        assert store.get("mac.online") is False


async def test_disconnect_fails_pending_and_snapshot_replaces_state(state_file, store, fake_sleep):
    state_file.write_text(json.dumps({"volume": 10, "bt.connected": ["aa-bb"]}))
    client = MacClient(mac_config(stub_command(state_file)), AUDIO, store, sleep=fake_sleep)
    async with running(client):
        await eventually(lambda: store.get("mac.online") is True)
        assert store.get("mac.bt.connected") == ["aa-bb"]

        state_file.write_text(json.dumps({"volume": 20}))
        fake_sleep.gate = asyncio.Event()  # hold the reconnect back to look at the offline state
        async with asyncio.timeout(1):
            with pytest.raises(MacOffline):
                await client.request("die", None)
        assert store.get("mac.online") is False
        assert not client.online
        with pytest.raises(MacOffline):
            await client.request("echo", 1)
        await eventually(lambda: fake_sleep.delays == [1])  # backing off, not yet reconnected

        fake_sleep.gate.set()
        await eventually(lambda: store.get("mac.online") is True)
        assert store.get("mac.volume") == 20
        assert store.get("mac.bt.connected") is None
        assert "mac.bt.connected" not in store.snapshot()
        assert await client.request("echo", 1) == 1


async def test_backoff_doubles_to_30(store, fake_sleep):
    client = MacClient(mac_config(["/nonexistent/deck-stub"]), None, store, sleep=fake_sleep)
    async with running(client):
        await eventually(lambda: len(fake_sleep.delays) >= 7)
    assert fake_sleep.delays[:7] == [1, 2, 4, 8, 16, 30, 30]
    assert store.get("mac.online") is False


async def test_backoff_restarts_after_a_connection_was_made(client, fake_sleep, store):
    for expected in (1, 2):
        with pytest.raises(MacOffline):
            await client.request("die", None)
        await eventually(lambda: store.get("mac.online") is True)
        assert len(fake_sleep.delays) == expected
    assert fake_sleep.delays == [1, 1]


# --- cleanup ----------------------------------------------------------------


async def test_cancelling_run_leaves_no_child_process(state_file, store, fake_sleep):
    client = MacClient(mac_config(stub_command(state_file)), None, store, sleep=fake_sleep)
    async with running(client) as task:
        await eventually(lambda: client.online)
        pid = await client.request("pid", None)
        assert process_exists(pid)
    assert task.cancelled()
    assert not process_exists(pid)
    assert store.get("mac.online") is False
    with pytest.raises(MacOffline):
        await client.request("echo", 1)


async def test_cancelling_run_fails_pending_requests(state_file, store, fake_sleep):
    client = MacClient(mac_config(stub_command(state_file)), None, store, sleep=fake_sleep)
    async with running(client):
        await eventually(lambda: client.online)
        pending = asyncio.create_task(client.request("hang", None))
        await asyncio.sleep(0.05)
        assert not pending.done()
    with pytest.raises(MacOffline):
        await asyncio.wait_for(pending, 1)


async def test_cancelling_while_the_child_starts_leaves_no_child(state_file, store, fake_sleep):
    client = MacClient(mac_config(stub_command(state_file)), None, store, sleep=fake_sleep)
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0)  # the child is being started
    task.cancel()
    async with asyncio.timeout(5):
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert processes_running(str(state_file)) == []


@pytest.mark.filterwarnings("ignore::ResourceWarning")
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_asyncio_run_ending_while_the_child_starts_does_not_hang(state_file, store):
    async def main() -> None:
        client = MacClient(mac_config(stub_command(state_file)), None, store)
        asyncio.create_task(client.run())
        await asyncio.sleep(0)  # the child is being started; asyncio.run now cancels the task

    thread = threading.Thread(target=asyncio.run, args=(main(),), daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    assert processes_running(str(state_file)) == []
    gc.collect()  # asyncio leaves the pipes of a spawn cancelled this early unclosed


async def test_a_second_cancel_while_the_child_starts_still_leaves_no_child(
    state_file, store, fake_sleep
):
    client = MacClient(mac_config(stub_command(state_file)), None, store, sleep=fake_sleep)
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0)  # the child is being started
    task.cancel()
    await asyncio.sleep(0)  # the cancelled run waits for the spawn to finish
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await eventually(lambda: processes_running(str(state_file)) == [])  # the spawn's child dies


async def test_a_child_that_ignores_sigterm_is_killed(monkeypatch, state_file, store, fake_sleep):
    monkeypatch.setattr(proc_module, "TERMINATE_GRACE", 0.2)
    cfg = mac_config(stub_command(state_file, "--stubborn"))
    client = MacClient(cfg, None, store, sleep=fake_sleep)
    async with running(client):
        await eventually(lambda: client.online)
        pid = await client.request("pid", None)
    assert not process_exists(pid)


async def test_a_failed_spawn_is_survived(store, fake_sleep, caplog):
    client = MacClient(mac_config(["/nonexistent/deck-stub"]), None, store, sleep=fake_sleep)
    with caplog.at_level(logging.WARNING, logger="deck.mac.client"):
        async with running(client) as task:
            await eventually(lambda: len(fake_sleep.delays) >= 2)
            assert not task.done()
    assert "deck-stub" in caplog.text


# --- mac actions ------------------------------------------------------------


class FakeMac:
    """Records requests instead of sending them; `block` holds each one until set."""

    def __init__(self, online: bool = True) -> None:
        self.online = online
        self.requests: list[tuple[str, object]] = []
        self.block: asyncio.Event | None = None
        self.error: Exception | None = None

    async def request(self, kind: str, args: object, timeout: float = 15) -> None:
        self.requests.append((kind, args))
        if self.block is not None:
            await self.block.wait()
        if self.error is not None:
            raise self.error


def action(kind: str, args) -> Action:
    return Action.model_validate({kind: args})


@pytest.mark.parametrize(
    ("act", "expected"),
    [
        (action("mac.app", "Ghostty"), ("app", "Ghostty")),
        (action("mac.url", "https://example.com"), ("url", "https://example.com")),
        (
            action("mac.tab", {"match": "mail", "url": "https://mail.example.com"}),
            ("tab", {"match": "mail", "url": "https://mail.example.com", "navigate": False}),
        ),
        (action("mac.audio.input", "Q9"), ("audio.input", "Q9")),
        (action("mac.mic.mute", None), ("mic.mute", None)),
        (action("mac.volume", {"mute": "toggle"}), ("volume", {"mute": "toggle"})),
        (action("mac.screenshot", None), ("screenshot", None)),
        (action("mac.km", "Macro"), ("km", "Macro")),
        (action("mac.script", "do-it.sh"), ("script", "do-it.sh")),
        (action("mac.lock", None), ("lock", None)),
    ],
)
async def test_actions_map_to_kinds_and_args(act, expected):
    mac = FakeMac()
    await handle_mac_action(mac, AUDIO, act)
    assert mac.requests == [expected]


async def test_output_action_expands_bt(client):
    await handle_mac_action(client, AUDIO, action("mac.audio.output", {"name": "iLoud"}))
    await handle_mac_action(
        client, AUDIO, action("mac.audio.output", {"name": "iLoud", "disconnect": True})
    )
    await handle_mac_action(client, AUDIO, action("mac.audio.output", {"name": "Mac"}))
    sent = [m["args"] for m in await received(client, "audio.output")]
    assert sent == [
        {"name": "iLoud", "match": "iLoud", "bt": "aa-bb-cc", "disconnect": False},
        {"name": "iLoud", "match": "iLoud", "bt": "aa-bb-cc", "disconnect": True},
        {"name": "Mac", "match": "MacBook Pro Speakers", "bt": None, "disconnect": False},
    ]


async def test_output_action_with_an_undefined_output_raises():
    mac = FakeMac()
    out = Action(type="mac.audio.output", args=OutputArgs(name="Nope"))
    with pytest.raises(ValueError, match="Nope"):
        await handle_mac_action(mac, AUDIO, out)
    with pytest.raises(ValueError, match="Nope"):
        await handle_mac_action(mac, None, out)
    assert mac.requests == []


async def test_action_failures_propagate():
    mac = FakeMac()
    mac.error = MacError("no such app")
    with pytest.raises(MacError, match="no such app"):
        await handle_mac_action(mac, AUDIO, action("mac.app", "Nope"))
    mac.error = MacOffline("offline")
    with pytest.raises(MacOffline):
        await handle_mac_action(mac, AUDIO, action("mac.lock", None))


# --- dials ------------------------------------------------------------------


def make_services(store: StateStore, mac, audio: AudioConfig | None = AUDIO) -> Services:
    config = Config(pages={"home": {}}, audio=audio)
    services = Services(store, config, actions=ActionRunner())
    services.mac = mac
    return services


def volume_dial(services: Services) -> VolumeDial:
    return VolumeDial(DialSpec(kind="volume"), services)


def input_dial(services: Services) -> InputDial:
    return InputDial(DialSpec(kind="input"), services)


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def test_dial_factories_are_registered():
    assert DIAL_FACTORIES["volume"] is VolumeDial
    assert DIAL_FACTORIES["input"] is InputDial


async def test_volume_turn_updates_the_store_and_sends(store):
    mac = FakeMac()
    store.set("mac.volume", 40)
    await volume_dial(make_services(store, mac)).on_turn(3)
    assert store.get("mac.volume") == 46
    await settle()
    assert mac.requests == [("volume", 46)]


async def test_volume_turn_down_and_clamp(store):
    mac = FakeMac()
    dial = volume_dial(make_services(store, mac))
    store.set("mac.volume", 3)
    await dial.on_turn(-5)
    assert store.get("mac.volume") == 0
    await settle()
    store.set("mac.volume", 99)
    await dial.on_turn(4)
    assert store.get("mac.volume") == 100
    await settle()
    assert mac.requests == [("volume", 0), ("volume", 100)]


async def test_volume_turn_at_the_limit_sends_nothing(store):
    mac = FakeMac()
    store.set("mac.volume", 100)
    await volume_dial(make_services(store, mac)).on_turn(1)
    await settle()
    assert mac.requests == []


async def test_volume_turn_without_a_known_volume_does_nothing(store):
    mac = FakeMac()
    await volume_dial(make_services(store, mac)).on_turn(1)
    await settle()
    assert mac.requests == []
    assert store.get("mac.volume") is None


async def test_volume_turn_while_offline_does_nothing(store):
    mac = FakeMac(online=False)
    store.set("mac.volume", 40)
    await volume_dial(make_services(store, mac)).on_turn(1)
    await settle()
    assert mac.requests == []
    assert store.get("mac.volume") == 40


async def test_volume_press_toggles_mute(store):
    mac = FakeMac()
    await volume_dial(make_services(store, mac)).on_press()
    assert mac.requests == [("volume", {"mute": "toggle"})]


async def test_volume_turn_in_flight_steps_from_the_target_not_a_stale_push(store):
    mac = FakeMac()
    mac.block = asyncio.Event()
    store.set("mac.volume", 40)
    dial = volume_dial(make_services(store, mac))
    await dial.on_turn(1)  # 42, now in flight
    store.set("mac.volume", 40)  # a push still carrying the level from before the spin
    await dial.on_turn(1)
    assert store.get("mac.volume") == 44  # not 42: no tick is lost
    mac.block.set()
    await settle()
    assert mac.requests == [("volume", 42), ("volume", 44)]


async def test_volume_turn_after_the_sends_steps_from_the_store_again(store):
    mac = FakeMac()
    store.set("mac.volume", 40)
    dial = volume_dial(make_services(store, mac))
    await dial.on_turn(1)
    await settle()
    store.set("mac.volume", 10)  # the Mac's real level, however it got there
    await dial.on_turn(1)
    assert store.get("mac.volume") == 12


async def test_volume_send_failure_is_logged_without_a_traceback_and_ends_the_target(store, caplog):
    mac = FakeMac()
    mac.error = MacError("osascript: nope")
    store.set("mac.volume", 40)
    dial = volume_dial(make_services(store, mac))
    with caplog.at_level(logging.WARNING):
        await dial.on_turn(1)
        await settle()
    (record,) = caplog.records
    assert record.name == "deck.mac.dials" and record.exc_info is None
    assert "volume to 42" in record.message and "osascript: nope" in record.message
    store.set("mac.volume", 40)  # the Mac's real level comes back
    mac.error = None
    await dial.on_turn(1)
    assert store.get("mac.volume") == 42  # stepped from the store, not the failed 42 target


async def test_volume_send_while_the_connection_drops_is_logged_quietly(store, caplog):
    mac = FakeMac()
    mac.error = MacOffline("the Mac connection was lost")
    store.set("mac.volume", 40)
    with caplog.at_level(logging.WARNING):
        await volume_dial(make_services(store, mac)).on_turn(1)
        await settle()
    assert [r.exc_info for r in caplog.records] == [None]


async def test_a_rejected_optimistic_write_is_corrected_by_the_next_push(client, store, caplog):
    await client.request("reject", ["volume"])
    dial = volume_dial(make_services(store, client))
    with caplog.at_level(logging.WARNING, logger="deck.mac.dials"):
        await dial.on_turn(1)
        assert store.get("mac.volume") == 44  # shown at once
        await eventually(lambda: "boom" in caplog.text)  # the Mac refused it
    assert store.get("mac.volume") == 44  # and nothing has corrected the display yet
    # `deck remote` sends the whole state again after each request, so the Mac's real level comes
    await client.request("push", {"volume": 42})
    await eventually(lambda: store.get("mac.volume") == 42)


async def test_volume_dial_without_a_mac_client_does_nothing(store):
    store.set("mac.volume", 40)
    dial = volume_dial(make_services(store, None))
    await dial.on_turn(1)
    await dial.on_press()
    await settle()
    assert store.get("mac.volume") == 40


async def test_volume_dial_coalesces(client, store):
    await client.request("hold", ["volume"])
    dial = volume_dial(make_services(store, client))
    assert store.get("mac.volume") == 42
    for _ in range(30):
        await dial.on_turn(1)
    assert store.get("mac.volume") == 100  # shown at once, before the Mac answered

    async def volumes() -> list[int]:
        return [m["args"] for m in await received(client, "volume")]

    assert await volumes() == [44]  # the first tick went out; the rest wait for its answer
    await client.request("release", None)
    async with asyncio.timeout(5):
        while len(await volumes()) < 2:
            await asyncio.sleep(0.01)
    assert await volumes() == [44, 100]
    assert store.get("mac.volume") == 100


async def test_input_turn_cycles_the_preferred_inputs_that_are_available(store):
    mac = FakeMac()
    store.update("mac.audio.", {"inputs": ["MV7 USB", "AirPods", "Q9 Mic"], "input": "Q9 Mic"})
    dial = input_dial(make_services(store, mac))
    await dial.on_turn(1)  # prefs are [Q9, MV7]: Q9 Mic -> MV7 USB
    assert store.get("mac.audio.input") == "MV7 USB"
    await settle()
    await dial.on_turn(1)  # wraps around
    assert store.get("mac.audio.input") == "Q9 Mic"
    await settle()
    await dial.on_turn(-1)  # and back
    assert store.get("mac.audio.input") == "MV7 USB"
    await settle()
    assert mac.requests == [
        ("audio.input", "MV7 USB"),
        ("audio.input", "Q9 Mic"),
        ("audio.input", "MV7 USB"),
    ]


async def test_input_turn_from_a_device_outside_the_prefs(store):
    mac = FakeMac()
    store.update("mac.audio.", {"inputs": ["MV7 USB", "AirPods", "Q9 Mic"], "input": "AirPods"})
    dial = input_dial(make_services(store, mac))
    await dial.on_turn(1)
    assert store.get("mac.audio.input") == "Q9 Mic"  # the first preferred one
    store.set("mac.audio.input", "AirPods")
    await settle()
    await dial.on_turn(-1)
    assert store.get("mac.audio.input") == "MV7 USB"  # the last preferred one


async def test_input_turn_with_one_candidate_that_is_current_sends_nothing(store):
    mac = FakeMac()
    store.update("mac.audio.", {"inputs": ["Q9 Mic", "AirPods"], "input": "Q9 Mic"})
    await input_dial(make_services(store, mac)).on_turn(1)
    await settle()
    assert mac.requests == []


async def test_input_turn_coalesces_to_the_latest_target(store):
    mac = FakeMac()
    mac.block = asyncio.Event()
    store.update("mac.audio.", {"inputs": ["Q9 Mic", "MV7 USB", "Quiet MV7"], "input": "Q9 Mic"})
    dial = input_dial(make_services(store, mac))
    for _ in range(5):  # MV7 USB, Quiet MV7, Q9 Mic, MV7 USB, Quiet MV7
        await dial.on_turn(1)
    assert store.get("mac.audio.input") == "Quiet MV7"
    mac.block.set()
    await settle()
    assert mac.requests == [("audio.input", "MV7 USB"), ("audio.input", "Quiet MV7")]


async def test_input_turn_in_flight_steps_from_the_target_not_a_stale_push(store):
    mac = FakeMac()
    mac.block = asyncio.Event()
    store.update("mac.audio.", {"inputs": ["Q9 Mic", "MV7 USB", "Quiet MV7"], "input": "Q9 Mic"})
    dial = input_dial(make_services(store, mac))
    await dial.on_turn(1)  # MV7 USB, now in flight
    store.set("mac.audio.input", "Q9 Mic")  # a push still carrying the input from before
    await dial.on_turn(1)
    assert store.get("mac.audio.input") == "Quiet MV7"  # not MV7 USB again
    mac.block.set()
    await settle()
    assert mac.requests == [("audio.input", "MV7 USB"), ("audio.input", "Quiet MV7")]
    store.set("mac.audio.input", "Q9 Mic")  # after the sends the store is the base again
    await dial.on_turn(1)
    assert store.get("mac.audio.input") == "MV7 USB"


async def test_input_send_failure_is_logged_without_a_traceback(store, caplog):
    mac = FakeMac()
    mac.error = MacError("SwitchAudioSource: nope")
    store.update("mac.audio.", {"inputs": ["Q9 Mic", "MV7 USB"], "input": "Q9 Mic"})
    with caplog.at_level(logging.WARNING):
        await input_dial(make_services(store, mac)).on_turn(1)
        await settle()
    (record,) = caplog.records
    assert record.name == "deck.mac.dials" and record.exc_info is None
    assert "MV7 USB" in record.message and "SwitchAudioSource: nope" in record.message


async def test_input_turn_without_audio_config_or_inputs_does_nothing(store):
    mac = FakeMac()
    store.update("mac.audio.", {"inputs": ["Q9 Mic"], "input": "Q9 Mic"})
    await input_dial(make_services(store, mac, audio=None)).on_turn(1)
    store.update("mac.audio.", {"inputs": [], "input": "Q9 Mic"})
    await input_dial(make_services(store, mac)).on_turn(1)
    await input_dial(make_services(store, None)).on_turn(1)
    await input_dial(make_services(store, FakeMac(online=False))).on_turn(1)
    await settle()
    assert mac.requests == []


async def test_input_press_toggles_the_mic(store):
    mac = FakeMac()
    await input_dial(make_services(store, mac)).on_press()
    assert mac.requests == [("mic.mute", None)]


async def test_input_dial_without_a_mac_client_does_nothing(store):
    await input_dial(make_services(store, None)).on_press()


# --- the daemon's services --------------------------------------------------


def _config(**sections) -> Config:
    return Config.model_validate({"pages": {"home": {}}, **sections})


async def test_services_without_a_mac_section_have_no_mac():
    async with contextlib.AsyncExitStack() as stack:
        services = await cli._services(_config(), StateStore(), stack)
    assert services.mac is None
    with pytest.raises(KeyError):
        await services.actions.run(action("mac.lock", None))


async def test_services_with_a_mac_section_connect_act_and_clean_up(state_file):
    store = StateStore()
    stack = contextlib.AsyncExitStack()
    config = _config(
        mac={"host": "macbook", "key": "/nonexistent/key", "command": stub_command(state_file)},
        audio={"outputs": [{"name": "iLoud", "match": "iLoud", "bt": "aa-bb"}], "inputs": ["Q9"]},
    )
    services = await cli._services(config, store, stack)
    assert isinstance(services.mac, MacClient)
    await eventually(lambda: store.get("mac.online") is True)
    assert store.get("mac.audio.output") == "iLoud"

    await services.actions.run(action("mac.audio.output", {"name": "iLoud"}))
    (output,) = await received(services.mac, "audio.output")
    assert output["args"] == {"name": "iLoud", "match": "iLoud", "bt": "aa-bb", "disconnect": False}

    # a reload swaps services.config; the next action and reconnect use the new audio section
    services.config = config.model_copy(update={"audio": None})
    with pytest.raises(ValueError, match="iLoud"):
        await services.actions.run(action("mac.audio.output", {"name": "iLoud"}))

    pid = await services.mac.request("pid", None)
    await stack.aclose()
    assert not process_exists(pid)
    assert store.get("mac.online") is False
