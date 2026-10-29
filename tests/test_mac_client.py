"""`handle_mac_action`, the volume and input dials and the daemon's services, against a fake
Mac (`FakeMac` records requests) and, where the wiring matters, against a `MacHub` that a fake
Mac has dialed in to."""

import asyncio
import contextlib
import logging
from typing import Any

import pytest

from deck import cli
from deck.actions import ActionRunner
from deck.config import (
    Action,
    AudioConfig,
    AudioOutput,
    Config,
    DialSpec,
    OutputArgs,
)
from deck.dials import DIAL_FACTORIES, Services
from deck.mac.attach import default_mac_socket
from deck.mac.client import MacError, MacHub, MacOffline, handle_mac_action
from deck.mac.dials import InputDial, VolumeDial
from deck.remote.server import RemoteError, serve
from deck.state import StateStore

AUDIO = AudioConfig(
    outputs=[
        AudioOutput(name="iLoud", match="iLoud", bt="aa-bb-cc"),
        AudioOutput(name="Mac", match="MacBook Pro Speakers"),
    ],
    inputs=["Q9", "MV7"],
    pin_input=True,
)


async def eventually(check, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


class FakeExecutor:
    """Records the requests it runs. Kinds in `hold` wait for `released`, kinds in `reject` fail."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.hold: set[str] = set()
        self.reject: set[str] = set()
        self.released = asyncio.Event()

    def configure(self, hello: dict[str, Any]) -> None:
        pass

    async def run(self, kind: str, args: Any) -> None:
        self.calls.append((kind, args))
        if kind in self.reject:
            raise RemoteError("boom")
        if kind in self.hold:
            await self.released.wait()

    def sent(self, kind: str) -> list[Any]:
        return [args for k, args in self.calls if k == kind]


class FakePoller:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    async def snapshot(self) -> dict[str, Any]:
        return dict(self.state)


@contextlib.asynccontextmanager
async def dialed_in(store: StateStore, executor: FakeExecutor, poller: FakePoller):
    """A fake Mac named `mini` connected to the hub on the default socket, online in `store`."""
    path = default_mac_socket()
    await eventually(path.exists)
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(b'{"attach": "mini"}\n')
    task = asyncio.create_task(serve(reader, writer, executor, poller, interval=0.01))
    try:
        await eventually(lambda: store.get("mac.online") is True)
        yield
    finally:
        writer.close()
        await asyncio.wait_for(task, 5)


@pytest.fixture
def store() -> StateStore:
    return StateStore()


@pytest.fixture
def executor() -> FakeExecutor:
    return FakeExecutor()


@pytest.fixture
def poller() -> FakePoller:
    return FakePoller({"audio.output": "iLoud", "volume": 42})


@pytest.fixture
async def hub(store, executor, poller):
    """A running hub with the fake Mac dialed in."""
    hub = MacHub(default_mac_socket(), AUDIO, store)
    task = asyncio.create_task(hub.run())
    try:
        async with dialed_in(store, executor, poller):
            yield hub
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- mac actions ------------------------------------------------------------


class FakeMac:
    """Records requests instead of sending them; `block` holds each one until set."""

    def __init__(self, online: bool = True) -> None:
        self.online = online
        self.active = "mini"  # the name of the Mac that `mac.*` mirrors
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


async def test_output_action_expands_bt(hub, executor):
    await handle_mac_action(hub, AUDIO, action("mac.audio.output", {"name": "iLoud"}))
    await handle_mac_action(
        hub, AUDIO, action("mac.audio.output", {"name": "iLoud", "disconnect": True})
    )
    await handle_mac_action(hub, AUDIO, action("mac.audio.output", {"name": "Mac"}))
    assert executor.sent("audio.output") == [
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


async def test_volume_turn_after_the_active_mac_changed_steps_from_the_store(store):
    mac = FakeMac()
    mac.block = asyncio.Event()
    store.set("mac.volume", 40)
    dial = volume_dial(make_services(store, mac))
    await dial.on_turn(1)  # 42 for the mini Mac, now in flight
    mac.active = "home"
    store.set("mac.volume", 10)  # the home Mac's state replaces the mini Mac's
    await dial.on_turn(1)
    assert store.get("mac.volume") == 12  # not 44: the target was the mini Mac's level
    mac.block.set()
    await settle()
    assert mac.requests == [("volume", 42), ("volume", 12)]


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


async def test_a_rejected_optimistic_write_is_corrected_by_the_next_push(
    hub, executor, store, caplog
):
    executor.reject = {"volume"}
    dial = volume_dial(make_services(store, hub))
    with caplog.at_level(logging.WARNING, logger="deck.mac.dials"):
        await dial.on_turn(1)
        assert store.get("mac.volume") == 44  # shown at once
        await eventually(lambda: "boom" in caplog.text)  # the Mac refused it
    # `serve` sends the whole state again after each request, so the Mac's real level comes
    await eventually(lambda: store.get("mac.volume") == 42)


async def test_volume_dial_without_a_mac_client_does_nothing(store):
    store.set("mac.volume", 40)
    dial = volume_dial(make_services(store, None))
    await dial.on_turn(1)
    await dial.on_press()
    await settle()
    assert store.get("mac.volume") == 40


async def test_volume_dial_coalesces(hub, executor, store):
    executor.hold = {"volume"}
    dial = volume_dial(make_services(store, hub))
    assert store.get("mac.volume") == 42
    for _ in range(30):
        await dial.on_turn(1)
    assert store.get("mac.volume") == 100  # shown at once, before the Mac answered
    await eventually(lambda: executor.sent("volume"))
    assert executor.sent("volume") == [44]  # the first tick went out; the rest wait for its answer
    executor.released.set()
    await eventually(lambda: len(executor.sent("volume")) == 2)
    assert executor.sent("volume") == [44, 100]


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


async def test_input_turn_after_the_active_mac_changed_steps_from_the_store(store):
    mac = FakeMac()
    mac.block = asyncio.Event()
    store.update("mac.audio.", {"inputs": ["Q9 Mic", "MV7 USB", "Quiet MV7"], "input": "Q9 Mic"})
    dial = input_dial(make_services(store, mac))
    await dial.on_turn(1)  # MV7 USB for the mini Mac, now in flight
    mac.active = "home"
    store.set("mac.audio.input", "Q9 Mic")  # the home Mac's state replaces the mini Mac's
    await dial.on_turn(1)
    assert store.get("mac.audio.input") == "MV7 USB"  # not Quiet MV7: one step from Q9 Mic
    mac.block.set()
    await settle()
    assert mac.requests == [("audio.input", "MV7 USB"), ("audio.input", "MV7 USB")]


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


async def test_services_with_a_mac_section_serve_act_and_clean_up(store, executor, poller):
    stack = contextlib.AsyncExitStack()
    config = _config(
        mac={},
        audio={"outputs": [{"name": "iLoud", "match": "iLoud", "bt": "aa-bb"}], "inputs": ["Q9"]},
    )
    services = await cli._services(config, store, stack)
    assert isinstance(services.mac, MacHub)
    await eventually(lambda: store.get("mac.online") is False)  # the hub is up, no Mac is
    async with dialed_in(store, executor, poller):
        assert store.get("mac.active") == "mini"
        assert store.get("mac.audio.output") == "iLoud"

        await services.actions.run(action("mac.audio.output", {"name": "iLoud"}))
        assert executor.sent("audio.output") == [
            {"name": "iLoud", "match": "iLoud", "bt": "aa-bb", "disconnect": False}
        ]

        # a reload swaps services.config; the next action and connect use the new audio section
        services.config = config.model_copy(update={"audio": None})
        with pytest.raises(ValueError, match="iLoud"):
            await services.actions.run(action("mac.audio.output", {"name": "iLoud"}))

        await stack.aclose()
        assert not default_mac_socket().exists()
        assert store.get("mac.online") is False
        with pytest.raises(MacOffline):
            await services.mac.request("app", "Ghostty")
