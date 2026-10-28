import asyncio
import contextlib
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fake_deck import FakeDeck
from PIL import Image

from deck import cli
from deck import daemon as daemon_module
from deck.actions import ActionRunner
from deck.cli import main
from deck.config import Action, ConfigError, load_config
from deck.daemon import Daemon
from deck.dials import Coalescer, Services
from deck.events import (
    Connected,
    DialDown,
    DialTurn,
    DialUp,
    Disconnected,
    KeyDown,
    KeyUp,
    TouchSwipe,
    TouchTap,
)
from deck.render import KeyView, SegmentView, draw_key, draw_segment
from deck.state import StateStore

VALID = """\
mac:
  host: macbook
  key: /tmp/key
home_assistant:
  url: http://ha.local:8123
  token_env: HA_TOKEN
audio:
  outputs:
    - {name: iLoud, match: iloud}
  inputs: [q9]
pages:
  home:
    keys:
      - {icon: "👻", label: Ghostty, press: {mac.app: Ghostty}, long_press: {mac.app: Terminal}}
      - {icon: "📄", label: Next, press: {page: second}}
      - icon: "💡"
        label: Guest
        state: {ha: light.guest_room}
        press: {ha: {service: light.toggle, entity_id: light.guest_room}}
      - {icon: "🧭", label: Safari, press: {mac.app: Safari}}
      - {icon: "🔇", label: Idle}
    dials:
      - {kind: volume}
      - {kind: input}
  second:
    keys:
      - {icon: "🏠", label: Back, press: {page: home}}
    dials:
      - {kind: volume}
"""

KEYS_AND_SEGMENTS = 36 + 6
ERROR_COLORS = {"bg": "#3a0f0f", "border": "#ef4444"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def settle() -> None:
    """Let ready tasks run to their next real suspension point."""
    for _ in range(10):
        await asyncio.sleep(0)


async def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.005)


@contextlib.asynccontextmanager
async def running(daemon: Daemon, deck: FakeDeck):
    """Run the daemon against the fake deck, with the deck already connected."""
    task = asyncio.create_task(daemon.run())
    await deck.inject(Connected())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def image_bytes(view: KeyView) -> bytes:
    return draw_key(view).tobytes()


def home_key(label: str, icon: str, **fields: Any) -> KeyView:
    return KeyView(icon, label, None, **fields)


GHOSTTY = home_key("Ghostty", "👻")
GUEST_ON = home_key("Guest", "💡", bg="#3a3208", border="#facc15")


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "deck.yaml"
    path.write_text(VALID)
    return path


@pytest.fixture
def deck() -> FakeDeck:
    return FakeDeck()


@pytest.fixture
def calls() -> list[Action]:
    return []


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def make_daemon(config_path, deck, calls):
    """Build daemons whose "mac." actions are recorded in `calls` (or sent to `handler`).

    By default the daemon is started, so `handle` and `render` work. Tests that call `run`
    pass `started=False`: `run` starts the services itself.
    """
    async with contextlib.AsyncExitStack() as stack:

        async def build(clock=time.monotonic, handler=None, started=True) -> Daemon:
            async def record(action: Action) -> None:
                calls.append(action)

            async def services_factory(config, store: StateStore, stack) -> Services:
                actions = ActionRunner()
                actions.register("mac.", handler or record)
                return Services(store, config, actions)

            daemon = Daemon(config_path, deck, services_factory, clock=clock)
            daemon.store.update("", {"mac.online": True, "ha.online": True})
            if started:
                await daemon.start(stack)
            return daemon

        yield build


@pytest.fixture
async def daemon(make_daemon, clock) -> Daemon:
    return await make_daemon(clock)


@pytest.fixture
async def unstarted(make_daemon, clock) -> Daemon:
    """For tests that call `run()`, which starts the services."""
    return await make_daemon(clock, started=False)


@pytest.fixture
def store(daemon) -> StateStore:
    return daemon.store


async def press(daemon: Daemon, index: int) -> None:
    await daemon.handle(KeyDown(index))
    await daemon.handle(KeyUp(index))


# --- actions ------------------------------------------------------------------


async def test_action_runner_uses_longest_prefix():
    seen = []

    async def generic(action: Action) -> None:
        seen.append(("generic", action.type))

    async def audio(action: Action) -> None:
        seen.append(("audio", action.type))

    runner = ActionRunner()
    runner.register("mac.", generic)
    runner.register("mac.audio.", audio)
    await runner.run(Action(type="mac.app", args="Ghostty"))
    await runner.run(Action(type="mac.audio.output", args={"name": "iLoud"}))
    assert seen == [("generic", "mac.app"), ("audio", "mac.audio.output")]


async def test_action_runner_exact_name_and_unregistered_type():
    seen = []

    async def page(action: Action) -> None:
        seen.append(action.args)

    runner = ActionRunner()
    runner.register("page", page)
    await runner.run(Action(type="page", args="second"))
    assert seen == ["second"]
    with pytest.raises(KeyError):
        await runner.run(Action(type="mac.lock"))


# --- key presses ----------------------------------------------------------------


async def test_press_runs_action(unstarted, deck, calls):
    await deck.inject(KeyDown(0), KeyUp(0))
    task = asyncio.create_task(unstarted.run())
    await wait_until(lambda: calls)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert calls == [Action(type="mac.app", args="Ghostty")]


async def test_long_press_runs_long_action(daemon, calls, clock):
    await daemon.handle(KeyDown(0))
    clock.advance(0.6)
    await daemon.handle(KeyUp(0))
    await settle()
    assert calls == [Action(type="mac.app", args="Terminal")]


async def test_long_press_threshold_is_half_a_second(daemon, calls, clock):
    await daemon.handle(KeyDown(0))
    clock.advance(0.49)
    await daemon.handle(KeyUp(0))
    await daemon.handle(KeyDown(0))
    clock.advance(0.5)
    await daemon.handle(KeyUp(0))
    await settle()
    assert calls == [
        Action(type="mac.app", args="Ghostty"),
        Action(type="mac.app", args="Terminal"),
    ]


async def test_long_hold_without_long_press_runs_press(daemon, calls, clock):
    await daemon.handle(KeyDown(3))
    clock.advance(2.0)
    await daemon.handle(KeyUp(3))
    await settle()
    assert calls == [Action(type="mac.app", args="Safari")]


async def test_long_press_fires_on_release_not_while_held(make_daemon, deck, calls, monkeypatch):
    monkeypatch.setattr(daemon_module, "LONG_PRESS_SECONDS", 0.05)
    daemon = await make_daemon(started=False)  # real clock
    async with running(daemon, deck):
        await deck.inject(KeyDown(0))
        await asyncio.sleep(0.2)  # well past the long-press time
        assert calls == []
        await deck.inject(KeyUp(0))
        await wait_until(lambda: calls)
    assert calls == [Action(type="mac.app", args="Terminal")]


async def test_keys_without_action_are_ignored(daemon, calls, clock):
    await press(daemon, 4)  # label only
    await press(daemon, 35)  # beyond the page's keys
    await daemon.handle(KeyUp(0))  # release without a press
    await settle()
    assert calls == []


async def test_disconnect_forgets_pressed_keys(daemon, calls, clock):
    await daemon.handle(KeyDown(0))
    await daemon.handle(Disconnected())
    clock.advance(1.0)
    await daemon.handle(KeyUp(0))  # a stale release must not fire anything
    await settle()
    assert calls == []


async def test_slow_action_does_not_block_other_keys(make_daemon, clock):
    gate = asyncio.Event()
    started = []

    async def slow(action: Action) -> None:
        started.append(action.args)
        await gate.wait()

    daemon = await make_daemon(clock, handler=slow)
    await press(daemon, 0)
    await press(daemon, 3)
    assert started == ["Ghostty", "Safari"]
    gate.set()
    await settle()


# --- pages ----------------------------------------------------------------------


async def test_page_switch_rerenders(daemon, deck):
    daemon.render()
    assert daemon.current_page == "home"
    assert deck.keys[0].tobytes() == image_bytes(GHOSTTY)

    await press(daemon, 1)
    assert daemon.current_page == "second"
    daemon.render()
    assert deck.keys[0].tobytes() == image_bytes(home_key("Back", "🏠"))
    assert deck.keys[1].tobytes() == image_bytes(
        KeyView(None, None, None, bg="#08080a", border=None)
    )

    await press(daemon, 0)  # Back
    assert daemon.current_page == "home"
    daemon.render()
    assert deck.keys[0].tobytes() == image_bytes(GHOSTTY)


async def test_page_switch_marks_render_needed_for_the_loop(unstarted, deck):
    daemon = unstarted
    async with running(daemon, deck):
        await wait_until(lambda: deck.push_count >= KEYS_AND_SEGMENTS)
        await deck.inject(KeyDown(1), KeyUp(1))
        await wait_until(lambda: deck.keys[0].tobytes() == image_bytes(home_key("Back", "🏠")))


async def test_strip_segments_follow_the_page_dials(daemon, deck, store):
    daemon.render()
    volume = daemon.config.pages["home"].dials[0]
    expected = draw_segment(SegmentView("🔊 Volume", "—"))
    assert volume.kind == "volume"
    assert deck.segments[0].tobytes() == expected.tobytes()
    blank = draw_segment(SegmentView("", ""))
    assert deck.segments[5].tobytes() == blank.tobytes()
    store.set("mac.volume", 40)
    daemon.render()
    loud = draw_segment(SegmentView("🔊 Volume", "40%", bar=0.4))
    assert deck.segments[0].tobytes() == loud.tobytes()


# --- rendering ------------------------------------------------------------------


async def test_connected_pushes_every_key_and_segment(daemon, deck):
    await daemon.handle(Connected())
    daemon.render()
    assert deck.push_count == KEYS_AND_SEGMENTS
    assert sorted(deck.keys) == list(range(36))
    assert sorted(deck.segments) == list(range(6))


async def test_reconnect_repushes_unchanged_images(daemon, deck):
    daemon.render()
    daemon.render()
    assert deck.push_count == KEYS_AND_SEGMENTS
    await daemon.handle(Disconnected())
    await daemon.handle(Connected())
    daemon.render()
    assert deck.push_count == 2 * KEYS_AND_SEGMENTS


async def test_only_changed_images_pushed(daemon, deck, store):
    daemon.render()
    before = deck.push_count
    store.set("ha.light.guest_room", {"state": "on", "attributes": {}})
    daemon.render()
    assert deck.push_count - before == 1
    assert deck.keys[2].tobytes() == image_bytes(GUEST_ON)
    daemon.render()
    assert deck.push_count - before == 1


async def test_changed_view_with_identical_pixels_is_not_pushed(daemon, deck, store, monkeypatch):
    drawn = []

    def draw_flat(view: KeyView) -> Image.Image:
        drawn.append(view)
        return Image.new("RGB", (112, 112), "#123456")

    monkeypatch.setattr(daemon_module, "draw_key", draw_flat)
    daemon.render()
    before = deck.push_count
    drawn.clear()
    store.set("mac.online", False)  # dims and badges the mac keys
    daemon.render()
    assert drawn  # the views changed, so they were drawn again
    assert deck.push_count == before  # but the pixels did not


async def test_store_subscriber_only_marks_render_needed(daemon, deck, store):
    daemon.render()
    before = deck.push_count
    store.set("ha.light.guest_room", {"state": "on", "attributes": {}})
    assert deck.push_count == before  # nothing is drawn inside the subscriber


async def test_store_change_is_rendered_by_the_loop(unstarted, deck):
    daemon, store = unstarted, unstarted.store
    async with running(daemon, deck):
        await wait_until(lambda: deck.push_count >= KEYS_AND_SEGMENTS)
        before = deck.push_count
        store.set("ha.light.guest_room", {"state": "on", "attributes": {}})
        await wait_until(lambda: deck.push_count > before)
        await asyncio.sleep(0.15)
        assert deck.push_count - before == 1
        assert deck.keys[2].tobytes() == image_bytes(GUEST_ON)


async def test_renders_are_throttled_to_20_hz(unstarted, deck, monkeypatch):
    daemon, store = unstarted, unstarted.store
    renders = 0
    real_render = daemon.render

    def counting_render() -> None:
        nonlocal renders
        renders += 1
        real_render()

    monkeypatch.setattr(daemon, "render", counting_render)
    async with running(daemon, deck):
        await wait_until(lambda: deck.push_count >= KEYS_AND_SEGMENTS)
        renders = 0
        pushed = deck.push_count
        started = time.monotonic()
        for i in range(60):
            store.set("t3.tick", i)  # a change no key shows
            await asyncio.sleep(0.005)
        elapsed = time.monotonic() - started
        assert 2 <= renders <= elapsed / daemon_module.RENDER_INTERVAL + 2
        assert deck.push_count == pushed


async def test_render_failure_does_not_stop_the_loop(unstarted, deck, monkeypatch, caplog):
    daemon, store = unstarted, unstarted.store
    real_render = daemon.render
    failures = []

    def flaky_render() -> None:
        if not failures:
            failures.append(1)
            raise RuntimeError("draw exploded")
        real_render()

    monkeypatch.setattr(daemon, "render", flaky_render)
    with caplog.at_level(logging.ERROR, logger="deck.daemon"):
        async with running(daemon, deck):
            await wait_until(lambda: failures)
            store.set("ha.light.guest_room", {"state": "on", "attributes": {}})
            await wait_until(lambda: deck.push_count >= KEYS_AND_SEGMENTS)
    assert "draw exploded" in caplog.text


# --- action errors --------------------------------------------------------------


async def test_action_error_flashes_key(make_daemon, deck, clock, caplog):
    async def failing(action: Action) -> None:
        raise RuntimeError("mac is on fire")

    daemon = await make_daemon(clock, handler=failing)
    daemon.render()
    with caplog.at_level(logging.ERROR, logger="deck.daemon"):
        await press(daemon, 0)
        await settle()
    daemon.render()
    assert deck.keys[0].tobytes() == image_bytes(replace(GHOSTTY, **ERROR_COLORS))
    assert "mac is on fire" in caplog.text
    assert "Ghostty" in caplog.text  # the failed action is logged with its arguments

    clock.advance(0.99)
    daemon.render()
    assert deck.keys[0].tobytes() == image_bytes(replace(GHOSTTY, **ERROR_COLORS))
    clock.advance(0.01)
    daemon.render()
    assert deck.keys[0].tobytes() == image_bytes(GHOSTTY)


async def test_action_without_registered_handler_flashes_key(daemon, deck):
    await press(daemon, 2)  # an `ha` action; no handler registered for it
    await settle()
    daemon.render()
    guest = home_key("Guest", "💡", dim=True, **ERROR_COLORS)  # no ha state yet, so dimmed
    assert deck.keys[2].tobytes() == image_bytes(guest)


async def test_flash_ends_by_itself_in_the_running_daemon(make_daemon, deck, monkeypatch):
    async def failing(action: Action) -> None:
        raise RuntimeError("nope")

    monkeypatch.setattr(daemon_module, "FLASH_SECONDS", 0.1)
    daemon = await make_daemon(handler=failing, started=False)  # real clock
    async with running(daemon, deck):
        await wait_until(lambda: deck.push_count >= KEYS_AND_SEGMENTS)
        await deck.inject(KeyDown(0), KeyUp(0))
        await wait_until(
            lambda: deck.keys[0].tobytes() == image_bytes(replace(GHOSTTY, **ERROR_COLORS))
        )
        await wait_until(lambda: deck.keys[0].tobytes() == image_bytes(GHOSTTY))


async def test_late_failure_flashes_the_key_it_was_pressed_on(make_daemon, deck, clock):
    gate = asyncio.Event()

    async def fails_late(action: Action) -> None:
        await gate.wait()
        raise RuntimeError("late")

    daemon = await make_daemon(clock, handler=fails_late)
    await press(daemon, 0)
    gate.set()
    await settle()
    daemon.render()
    assert deck.keys[0].tobytes() == image_bytes(replace(GHOSTTY, **ERROR_COLORS))


async def test_late_failure_after_page_switch_does_not_flash_the_new_page(make_daemon, deck, clock):
    gate = asyncio.Event()

    async def fails_late(action: Action) -> None:
        await gate.wait()
        raise RuntimeError("late")

    daemon = await make_daemon(clock, handler=fails_late)
    await press(daemon, 0)  # slow action on page "home"
    await press(daemon, 1)  # now on page "second", whose key 0 is another key
    gate.set()
    await settle()
    daemon.render()
    assert deck.keys[0].tobytes() == image_bytes(home_key("Back", "🏠"))


async def test_page_switch_drops_pending_flash(make_daemon, deck, clock):
    async def failing(action: Action) -> None:
        raise RuntimeError("nope")

    daemon = await make_daemon(clock, handler=failing)
    await press(daemon, 0)
    await press(daemon, 1)
    daemon.render()
    assert deck.keys[0].tobytes() == image_bytes(home_key("Back", "🏠"))


# --- config reload ----------------------------------------------------------------


CONFIG_ERROR_KEY = KeyView("❌", "config", None, bg="#3a0f0f", border="#ef4444")


async def test_invalid_reload_keeps_previous_config(daemon, deck, calls, config_path, caplog):
    config_path.write_text("pages: [")  # partial save
    with caplog.at_level(logging.ERROR, logger="deck.daemon"):
        assert daemon.reload() is False
    assert "yaml:" in caplog.text

    daemon.render()
    assert deck.keys[0].tobytes() == image_bytes(CONFIG_ERROR_KEY)
    assert deck.keys[1].tobytes() == image_bytes(home_key("Next", "📄"))

    await press(daemon, 1)  # old config still active
    assert daemon.current_page == "second"
    await press(daemon, 0)  # "Back", but key 0 shows the error
    await settle()
    assert daemon.current_page == "second"
    assert calls == []

    config_path.write_text(VALID)
    assert daemon.reload() is True
    daemon.render()
    assert deck.keys[0].tobytes() == image_bytes(home_key("Back", "🏠"))  # still on page two


async def test_error_key_ignores_presses(daemon, calls, config_path):
    config_path.write_text("pages: [")
    daemon.reload()
    await press(daemon, 0)
    await settle()
    assert calls == []


async def test_valid_reload_applies_new_config(daemon, deck, config_path):
    daemon.render()
    config_path.write_text(VALID.replace("label: Ghostty", "label: Kitty"))
    assert daemon.reload() is True
    daemon.render()
    assert deck.keys[0].tobytes() == image_bytes(home_key("Kitty", "👻"))


async def test_reload_runs_actions_of_the_new_config(daemon, calls, config_path):
    config_path.write_text(VALID.replace("press: {mac.app: Safari}", "press: {mac.app: Firefox}"))
    assert daemon.reload() is True
    await press(daemon, 3)
    await settle()
    assert calls == [Action(type="mac.app", args="Firefox")]


async def test_reload_keeps_current_page_or_falls_back_to_start_page(daemon, config_path):
    await press(daemon, 1)
    assert daemon.current_page == "second"
    config_path.write_text(VALID.replace("label: Next", "label: Forward"))
    assert daemon.reload() is True
    assert daemon.current_page == "second"

    removed = VALID.split("  second:")[0].replace("press: {page: second}", "press: {mac.app: X}")
    config_path.write_text(removed)
    assert daemon.reload() is True
    assert daemon.current_page == "home"


async def plain_services(config, store, stack) -> Services:
    return Services(store, config, ActionRunner())


def test_unreadable_config_at_startup_raises(tmp_path, deck):
    with pytest.raises(ConfigError):
        Daemon(tmp_path / "missing.yaml", deck, plain_services)


async def test_services_are_built_once_and_closed_when_run_ends(config_path, deck):
    built = []
    closed = []

    async def factory(config, store, stack) -> Services:
        built.append(config)
        stack.callback(closed.append, "sync cleanup")

        async def close() -> None:
            closed.append("async cleanup")

        stack.push_async_callback(close)
        return Services(store, config, ActionRunner())

    daemon = Daemon(config_path, deck, factory)
    assert built == []  # nothing is built before the loop runs
    async with running(daemon, deck):
        await wait_until(lambda: built)
        for i in range(3):
            config_path.write_text(VALID.replace("label: Ghostty", f"label: Kitty{i}"))
            assert daemon.reload() is True
        assert len(built) == 1
        assert closed == []
    assert closed == ["async cleanup", "sync cleanup"]


async def test_reload_hands_the_new_config_to_the_services(daemon, config_path):
    services = daemon.services
    config_path.write_text(VALID.replace("label: Ghostty", "label: Kitty"))
    assert daemon.reload() is True
    assert daemon.services is services
    assert services.config is daemon.config
    assert services.config.pages["home"].keys[0].label == "Kitty"


async def test_factory_failure_stops_run_and_unwinds_the_stack(config_path, deck):
    closed = []

    async def factory(config, store, stack) -> Services:
        stack.callback(closed.append, "cleanup")
        raise RuntimeError("cannot reach the mac")

    daemon = Daemon(config_path, deck, factory)
    with pytest.raises(RuntimeError, match="cannot reach the mac"):
        await daemon.run()
    assert closed == ["cleanup"]


async def test_changed_connection_sections_log_a_restart_warning(daemon, config_path, caplog):
    def warnings() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    with caplog.at_level(logging.WARNING, logger="deck.daemon"):
        config_path.write_text(VALID.replace("label: Ghostty", "label: Kitty"))
        assert daemon.reload() is True
        assert warnings() == []  # pages are applied live

        config_path.write_text(VALID.replace("host: macbook", "host: other-mac"))
        assert daemon.reload() is True
        assert warnings() == ["restart deck to apply changes to: mac"]

        caplog.clear()
        both = VALID.replace("host: macbook", "host: other-mac")
        config_path.write_text(both.replace("ha.local", "ha.example"))
        assert daemon.reload() is True
        assert warnings() == ["restart deck to apply changes to: mac, home_assistant"]

        caplog.clear()
        config_path.write_text(VALID)  # back to what the services were built with
        assert daemon.reload() is True
        assert warnings() == []


async def test_watcher_reloads_changed_config(make_daemon, deck, config_path, monkeypatch):
    monkeypatch.setattr(daemon_module, "WATCH_INTERVAL", 0.02)
    monkeypatch.setattr(daemon_module, "WATCH_STABLE", 0.05)
    daemon = await make_daemon(started=False)
    async with running(daemon, deck):
        await wait_until(lambda: deck.push_count >= KEYS_AND_SEGMENTS)
        config_path.write_text(VALID.replace("label: Ghostty", "label: Kitty"))
        await wait_until(lambda: deck.keys[0].tobytes() == image_bytes(home_key("Kitty", "👻")))
        config_path.write_text("pages: [")
        await wait_until(lambda: deck.keys[0].tobytes() == image_bytes(CONFIG_ERROR_KEY))
        config_path.write_text(VALID)
        await wait_until(lambda: deck.keys[0].tobytes() == image_bytes(GHOSTTY))


async def test_watcher_waits_for_the_file_to_stop_changing(
    make_daemon, deck, config_path, monkeypatch
):
    monkeypatch.setattr(daemon_module, "WATCH_INTERVAL", 0.02)
    monkeypatch.setattr(daemon_module, "WATCH_STABLE", 0.15)
    daemon = await make_daemon(started=False)
    reloads = []
    real_reload = daemon.reload

    def spy() -> bool:
        reloads.append(time.monotonic())
        return real_reload()

    monkeypatch.setattr(daemon, "reload", spy)
    async with running(daemon, deck):
        await wait_until(lambda: deck.push_count >= KEYS_AND_SEGMENTS)
        text = VALID
        for _ in range(10):  # ~0.3 s of writes, each well inside the stable window
            text += "# edit\n"
            config_path.write_text(text)
            await asyncio.sleep(0.03)
        last_write = time.monotonic()
        assert reloads == []
        await wait_until(lambda: reloads)
        assert reloads[0] - last_write >= 0.1
        await asyncio.sleep(0.1)
        assert len(reloads) == 1


async def test_watcher_ignores_untouched_config(make_daemon, deck, monkeypatch):
    monkeypatch.setattr(daemon_module, "WATCH_INTERVAL", 0.01)
    monkeypatch.setattr(daemon_module, "WATCH_STABLE", 0.02)
    daemon = await make_daemon(started=False)
    reloads = []
    monkeypatch.setattr(daemon, "reload", lambda: reloads.append(1))
    async with running(daemon, deck):
        await asyncio.sleep(0.15)
    assert reloads == []


# --- dials ----------------------------------------------------------------------


class RecordingDial:
    def __init__(self, spec, services, log):
        self.spec = spec
        self.services = services
        self.log = log

    async def on_turn(self, ticks: int) -> None:
        self.log.append(("turn", self.spec.kind, ticks))

    async def on_press(self) -> None:
        self.log.append(("press", self.spec.kind))


class DialLog(list):
    """What the recording dials were asked to do, plus the dials built so far."""

    def __init__(self) -> None:
        super().__init__()
        self.built: list[RecordingDial] = []


@pytest.fixture
def dial_log(monkeypatch) -> DialLog:
    log = DialLog()

    def factory(spec, services):
        dial = RecordingDial(spec, services, log)
        log.built.append(dial)
        return dial

    monkeypatch.setattr(daemon_module, "DIAL_FACTORIES", {"volume": factory})
    return log


async def test_dial_events_reach_the_handler(make_daemon, dial_log):
    daemon = await make_daemon()
    await daemon.handle(DialTurn(0, 3))
    await daemon.handle(DialTurn(0, -1))
    await daemon.handle(DialDown(0))
    await daemon.handle(DialUp(0))
    await settle()
    assert dial_log == [("turn", "volume", 3), ("turn", "volume", -1), ("press", "volume")]


async def test_touch_tap_presses_the_dial_under_it(make_daemon, dial_log):
    daemon = await make_daemon()
    await daemon.handle(TouchTap(199, 50))
    await daemon.handle(TouchTap(200, 50))  # dial 1 has no handler
    await daemon.handle(TouchTap(1199, 50))  # dial 5 has no dial at all
    await daemon.handle(TouchSwipe(120))
    await settle()
    assert dial_log == [("press", "volume")]


async def test_dials_without_handler_are_ignored(make_daemon, dial_log):
    daemon = await make_daemon()
    await daemon.handle(DialTurn(1, 1))  # `input` has no factory
    await daemon.handle(DialDown(1))
    await daemon.handle(DialTurn(4, 1))  # no dial configured
    await settle()
    assert dial_log == []


async def test_dial_handlers_are_built_per_page_with_services(make_daemon, dial_log):
    daemon = await make_daemon()
    assert len(dial_log.built) == 1
    assert dial_log.built[0].services is daemon.services
    assert dial_log.built[0].spec.kind == "volume"
    await press(daemon, 1)  # second page has its own volume dial
    assert len(dial_log.built) == 2
    await daemon.handle(DialTurn(0, 2))
    assert dial_log == [("turn", "volume", 2)]


async def test_dial_handler_failure_is_logged_not_raised(make_daemon, monkeypatch, caplog):
    class Broken:
        async def on_turn(self, ticks: int) -> None:
            raise RuntimeError("dial broke")

        async def on_press(self) -> None:
            raise RuntimeError("press broke")

    monkeypatch.setattr(
        daemon_module, "DIAL_FACTORIES", {"volume": lambda spec, services: Broken()}
    )
    daemon = await make_daemon()
    with caplog.at_level(logging.ERROR, logger="deck.daemon"):
        await daemon.handle(DialTurn(0, 1))
        await daemon.handle(DialDown(0))
        await settle()
    assert "dial broke" in caplog.text
    assert "press broke" in caplog.text


# --- coalescer ------------------------------------------------------------------


async def test_coalescer_keeps_latest():
    sent = []
    gate = asyncio.Event()

    async def send(value: int) -> None:
        sent.append(value)
        await gate.wait()

    coalescer = Coalescer(send)
    for i in range(1, 51):
        coalescer.submit(i)
    await settle()
    assert sent == [1]
    gate.set()
    await settle()
    assert sent == [1, 50]


async def test_coalescer_never_overlaps_sends_and_restarts_when_idle():
    sent = []
    active = 0
    peak = 0

    async def send(value: int) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        sent.append(value)
        active -= 1

    coalescer = Coalescer(send)
    for i in range(5):
        coalescer.submit(i)
        await asyncio.sleep(0)
    await settle()
    assert peak == 1
    assert sent[-1] == 4
    coalescer.submit(99)
    await settle()
    assert sent[-1] == 99


async def test_coalescer_survives_a_failing_send(caplog):
    sent = []

    async def send(value: int) -> None:
        if value == 1:
            raise RuntimeError("send failed")
        sent.append(value)

    coalescer = Coalescer(send)
    with caplog.at_level(logging.ERROR, logger="deck.dials"):
        coalescer.submit(1)
        await settle()
        coalescer.submit(2)
        await settle()
    assert sent == [2]
    assert "send failed" in caplog.text


# --- deck run ---------------------------------------------------------------------


def test_run_reports_invalid_config(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("pages: [")
    assert main(["run", str(bad)]) == 1
    assert "yaml:" in capsys.readouterr().out


def test_run_builds_daemon_with_deck_and_services(monkeypatch, config_path):
    created = {}

    class StubDaemon:
        def __init__(self, path, device, services_factory):
            created.update(path=path, device=device, services_factory=services_factory)

        async def run(self) -> None:
            created["ran"] = True

    class StubMacClient:
        def __init__(self, cfg, audio, store) -> None:
            pass

        async def run(self) -> None:  # the real one would start ssh
            await asyncio.Event().wait()

    monkeypatch.setattr(cli, "Daemon", StubDaemon)
    monkeypatch.setattr(cli, "StreamDeckDevice", FakeDeck)
    monkeypatch.setattr(cli, "MacClient", StubMacClient)
    assert main(["run", str(config_path)]) == 0
    assert created["ran"] is True
    assert created["path"] == config_path
    assert isinstance(created["device"], FakeDeck)

    store = StateStore()
    config = load_config(config_path)
    services = asyncio.run(created["services_factory"](config, store, contextlib.AsyncExitStack()))
    assert isinstance(services, Services)
    assert (services.store, services.config) == (store, config)
    assert isinstance(services.actions, ActionRunner)
    assert isinstance(services.mac, StubMacClient)  # the config has a mac section
    assert (services.ddc, services.ha, services.t3) == (None, None, None)
