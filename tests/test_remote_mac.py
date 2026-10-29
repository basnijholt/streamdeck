import asyncio
import json
import logging
import os
from pathlib import Path

import pytest
from fake_runner import FakeRunner

from deck import cli
from deck.remote.connect import ssh_argv
from deck.remote.mac import (
    ACTIVE_URL_SCRIPT,
    BRAVE_TAB_SCRIPT,
    IDLE_ARGV,
    INPUT_VOLUME_SCRIPT,
    KM_SCRIPT,
    VOLUME_MUTE_SCRIPT,
    VOLUME_SCRIPT,
    MacExecutor,
    MacPoller,
    active_url_argv,
    app_argv,
    input_volume_argv,
    km_argv,
    lock_argv,
    parse_idle,
    parse_volume_settings,
    screenshot_argv,
    tab_argv,
    url_argv,
    volume_argv,
    volume_mute_argv,
)
from deck.remote.server import ALLOWED_KINDS, RemoteError
from deck.runner import Result

BT = "00-11-22-33-44-55"
LIST_OUTPUTS = ["SwitchAudioSource", "-a", "-t", "output", "-f", "json"]
LIST_INPUTS = ["SwitchAudioSource", "-a", "-t", "input", "-f", "json"]
CURRENT_OUTPUT = ["SwitchAudioSource", "-c", "-t", "output", "-f", "json"]
CURRENT_INPUT = ["SwitchAudioSource", "-c", "-t", "input", "-f", "json"]
BT_CONNECTED = ["blueutil", "--connected", "--format", "json"]
GET_INPUT_VOLUME = ["osascript", "-e", "input volume of (get volume settings)"]
GET_VOLUME_SETTINGS = ["osascript", "-e", "get volume settings"]
ILOUD = {"name": "iLoud", "match": "iloud", "bt": BT}
SETTINGS = "output volume:42, input volume:75, alert volume:100, output muted:false\n"
IDLE_OUT = '+-o IOHIDSystem  <class IOHIDSystem>\n    "HIDIdleTime" = 4500000000\n'


def device(name: str, kind: str = "output") -> str:
    return json.dumps({"name": name, "type": kind, "id": "91", "uid": f"{name}:uid"})


def lines(*objects: str) -> Result:
    return Result(0, "".join(o + "\n" for o in objects), "")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeSleep:
    """Records each sleep and moves the clock forward by it."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        self.clock.now += delay


class TimeoutRunner(FakeRunner):
    """A FakeRunner that also remembers the timeout of each call (None when not given)."""

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float | None] = []

    async def run(self, argv, timeout=None):
        self.timeouts.append(timeout)
        return await super().run(argv, timeout)

    def timeout_of(self, prefix: list[str]) -> float | None:
        (timeout,) = [t for c, t in zip(self.calls, self.timeouts) if c[: len(prefix)] == prefix]
        return timeout


class SlowRunner(FakeRunner):
    """A FakeRunner whose `blueutil --connect` takes `cost` seconds of fake time."""

    def __init__(self, clock: FakeClock, cost: float) -> None:
        super().__init__()
        self.clock = clock
        self.cost = cost

    async def run(self, argv, timeout=10):
        if argv[:2] == ["blueutil", "--connect"]:
            self.clock.now += self.cost
        return await super().run(argv, timeout)


class CancelWatchRunner(FakeRunner):
    """Counts the calls that were cancelled while running."""

    cancelled = 0

    async def run(self, argv, timeout=10):
        try:
            return await super().run(argv, timeout)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class MicRunner(FakeRunner):
    """A mic with an input volume: every call yields to the loop, so presses could interleave."""

    def __init__(self, volume: int) -> None:
        super().__init__()
        self.volume = volume

    async def run(self, argv, timeout=10):
        await asyncio.sleep(0)
        if argv == GET_INPUT_VOLUME:
            await asyncio.sleep(0)
            return Result(0, f"{self.volume}\n", "")
        if argv[:3] == ["osascript", "-e", INPUT_VOLUME_SCRIPT]:
            await asyncio.sleep(0)
            self.volume = int(argv[-1])
        return await super().run(argv, timeout)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_sleep(clock) -> FakeSleep:
    return FakeSleep(clock)


@pytest.fixture
def scripts_dir(tmp_path):
    path = tmp_path / "scripts"
    path.mkdir()
    return path


@pytest.fixture
def executor(runner, scripts_dir, fake_sleep, clock) -> MacExecutor:
    return MacExecutor(runner, scripts_dir, sleep=fake_sleep, clock=clock)


@pytest.fixture
def poller(runner, executor) -> MacPoller:
    return MacPoller(runner, executor)


# --- argv builders and scripts ----------------------------------------------


def test_tab_argv_does_not_interpolate():
    argv = tab_argv('x" & do shell script "touch /tmp/pwn', "https://a.b")
    assert argv[:2] == ["osascript", "-e"] and argv[2] == BRAVE_TAB_SCRIPT
    assert argv[3] == 'x" & do shell script "touch /tmp/pwn'
    assert "touch" not in argv[2]


def test_active_url_script_never_launches_brave():
    assert 'application "Brave Browser" is running' in ACTIVE_URL_SCRIPT
    # no `tell` of Brave can come before the running check
    assert ACTIVE_URL_SCRIPT.index("is running") < ACTIVE_URL_SCRIPT.index("tell application")


def test_tab_argv_navigate_flag():
    assert tab_argv("t3.lab.nijho.lt", "https://t3.lab.nijho.lt/x", navigate=True)[-1] == "true"
    assert tab_argv("t3.lab.nijho.lt", "https://t3.lab.nijho.lt/x")[-1] == "false"
    assert tab_argv("m", "https://u")[3:5] == ["m", "https://u"]


def test_scripts_read_their_arguments_from_argv():
    assert "item 1 of argv" in BRAVE_TAB_SCRIPT and "item 3 of argv" in BRAVE_TAB_SCRIPT
    assert "item 1 of argv" in KM_SCRIPT
    assert "item 1 of argv" in VOLUME_SCRIPT


def test_simple_argv_builders():
    assert app_argv("Ghostty") == ["open", "-a", "Ghostty"]
    assert url_argv("https://a.b") == ["open", "https://a.b"]
    assert active_url_argv() == ["osascript", "-e", ACTIVE_URL_SCRIPT]
    assert km_argv("Toggle Mic") == ["osascript", "-e", KM_SCRIPT, "Toggle Mic"]
    assert volume_argv(42) == ["osascript", "-e", VOLUME_SCRIPT, "42"]
    assert volume_mute_argv() == ["osascript", "-e", VOLUME_MUTE_SCRIPT]
    assert "output muted" in VOLUME_MUTE_SCRIPT
    assert input_volume_argv(0)[-1] == "0"
    assert screenshot_argv() == ["screencapture", "-ic"]
    assert lock_argv() == ["pmset", "displaysleepnow"]


def test_parse_volume_settings():
    assert parse_volume_settings(SETTINGS) == {
        "output volume": "42",
        "input volume": "75",
        "alert volume": "100",
        "output muted": "false",
    }
    with pytest.raises(ValueError):
        parse_volume_settings("no colons here")


def test_parse_idle():
    assert parse_idle(IDLE_OUT) == 4500  # whole milliseconds
    with pytest.raises(ValueError):
        parse_idle("+-o IOHIDSystem\n")


# --- executor: simple kinds -------------------------------------------------


async def test_every_allowed_kind_has_a_handler(executor):
    assert set(executor._handlers) == ALLOWED_KINDS


async def test_unknown_kind_is_refused(executor, runner):
    with pytest.raises(RemoteError, match="kind not allowed: shell"):
        await executor.run("shell", ["ls"])
    assert runner.calls == []


@pytest.mark.parametrize(
    ("kind", "args", "argv"),
    [
        ("app", "Ghostty", ["open", "-a", "Ghostty"]),
        ("url", "https://example.com/x", ["open", "https://example.com/x"]),
        (
            "tab",
            {"match": "t3.lab", "url": "https://t3.lab/x"},
            tab_argv("t3.lab", "https://t3.lab/x", False),
        ),
        (
            "tab",
            {"match": "t3.lab", "url": "https://t3.lab/x", "navigate": True},
            tab_argv("t3.lab", "https://t3.lab/x", True),
        ),
        ("km", "Toggle Mic", km_argv("Toggle Mic")),
        ("volume", 0, volume_argv(0)),
        ("volume", 100, volume_argv(100)),
        ("volume", {"mute": "toggle"}, volume_mute_argv()),
        ("audio.input", "Q9 USB", ["SwitchAudioSource", "-t", "input", "-s", "Q9 USB"]),
        ("lock", None, lock_argv()),
    ],
)
async def test_kind_runs_its_command(executor, runner, kind, args, argv):
    assert await executor.run(kind, args) is None
    assert runner.calls == [argv]


@pytest.mark.parametrize(
    ("kind", "args"),
    [
        ("app", ""),
        ("app", "-x"),
        ("app", 5),
        ("app", None),
        ("url", 5),
        ("url", "file:///etc/passwd"),
        ("url", "-x"),
        ("tab", "https://a.b"),
        ("tab", {"match": "m"}),
        ("tab", {"url": "https://a.b"}),
        ("tab", {"match": "", "url": "https://a.b"}),
        ("tab", {"match": "-l", "url": "https://a.b"}),
        ("tab", {"match": 1, "url": "https://a.b"}),
        ("tab", {"match": "m", "url": "javascript:alert(1)"}),
        ("tab", {"match": "m", "url": "https://a.b", "navigate": "yes"}),
        ("km", ""),
        ("km", "-l"),
        ("km", ["x"]),
        ("volume", -1),
        ("volume", 101),
        ("volume", True),
        ("volume", 42.5),
        ("volume", "42"),
        ("volume", {"mute": "on"}),
        ("volume", {"mute": "toggle", "x": 1}),
        ("audio.output", "iLoud"),
        ("audio.output", {"name": "iLoud"}),
        ("audio.output", {"match": "iloud"}),
        ("audio.output", {"name": 1, "match": "iloud"}),
        ("audio.output", {"name": "", "match": "iloud"}),
        ("audio.output", {"name": "iLoud", "match": ""}),
        ("audio.output", {"name": "iLoud", "match": 1}),
        ("audio.output", {"name": "iLoud", "match": "iloud", "bt": 5}),
        ("audio.output", {"name": "iLoud", "match": "iloud", "bt": "-x"}),
        ("audio.output", {"name": "iLoud", "match": "iloud", "disconnect": "yes"}),
        ("audio.output", {"name": "iLoud", "match": "iloud", "disconnect": True}),
        ("audio.input", ""),
        ("audio.input", 5),
        ("audio.input", {"name": "Q9"}),
        ("script", ""),
        ("script", "a/b.sh"),
        ("script", "../x"),
        ("script", ".."),
        ("script", "."),
        ("script", "a b"),
        ("script", "go.sh\n"),
        ("script", 5),
    ],
)
async def test_bad_args_are_refused_without_running_anything(executor, runner, kind, args):
    with pytest.raises(RemoteError) as exc:
        await executor.run(kind, args)
    assert str(exc.value) == f"bad args for {kind}"
    assert runner.calls == []


async def test_failed_command_raises_with_first_line_of_stderr(executor, runner):
    runner.script(["open"], Result(1, "", "The file does not exist.\nsecond line\n"))
    with pytest.raises(RemoteError) as exc:
        await executor.run("app", "Nope")
    assert str(exc.value) == "The file does not exist."


async def test_failed_command_without_stderr_still_explains(executor, runner):
    runner.script(["pmset"], Result(1, "", ""))
    with pytest.raises(RemoteError, match=r"pmset failed \(rc 1\)"):
        await executor.run("lock", None)


async def test_timed_out_command_raises(executor, runner):
    runner.script(["pmset"], Result(-1, "", "pmset: timed out after 10s"))
    with pytest.raises(RemoteError, match="timed out"):
        await executor.run("lock", None)


async def test_every_command_has_an_explicit_timeout(scripts_dir, fake_sleep, clock):
    """At most 10 s each; only the screenshot's selection UI may run longer (60 s)."""
    runner = TimeoutRunner()
    executor = MacExecutor(runner, scripts_dir, sleep=fake_sleep, clock=clock)
    (scripts_dir / "go.sh").write_text("#!/bin/sh\n")
    (scripts_dir / "go.sh").chmod(0o755)
    runner.script(BT_CONNECTED, Result(0, "[]", ""))
    runner.script(LIST_OUTPUTS, lines(device("iLoud Micro Monitor")))
    runner.script(GET_INPUT_VOLUME, Result(0, "75\n", ""))
    for kind, args in [
        ("app", "Ghostty"),
        ("url", "https://a.b"),
        ("tab", {"match": "m", "url": "https://a.b"}),
        ("audio.output", ILOUD | {"name": "iLoud"}),
        ("audio.input", "Q9"),
        ("mic.mute", None),
        ("volume", 5),
        ("screenshot", None),
        ("km", "m"),
        ("script", "go.sh"),
        ("lock", None),
        ("active_url", None),
    ]:
        await executor.run(kind, args)
    await asyncio.sleep(0)  # let the background screenshot start
    await executor.aclose()
    assert len(runner.timeouts) == len(runner.calls) > 12
    assert runner.timeout_of(screenshot_argv()) == 60
    assert runner.timeout_of(["blueutil", "--connect"]) == 4
    others = [t for c, t in zip(runner.calls, runner.timeouts) if c != screenshot_argv()]
    assert all(t is not None and 0 < t <= 10 for t in others)


# --- screenshot -------------------------------------------------------------


async def test_screenshot_returns_while_the_selection_is_still_up(executor, runner):
    runner.block = asyncio.Event()  # `screencapture -ic` runs until the user is done
    assert await asyncio.wait_for(executor.run("screenshot", None), 2) is None
    await asyncio.sleep(0)
    assert runner.calls == [screenshot_argv()]
    runner.block.set()
    await executor.aclose()


async def test_new_screenshot_stops_the_previous_one(scripts_dir, fake_sleep):
    runner = CancelWatchRunner()
    runner.block = asyncio.Event()
    executor = MacExecutor(runner, scripts_dir, sleep=fake_sleep)
    await executor.run("screenshot", None)
    await asyncio.sleep(0)
    await executor.run("screenshot", None)
    await asyncio.sleep(0)
    assert runner.calls == [screenshot_argv()] * 2
    assert runner.cancelled == 1
    await executor.aclose()
    assert runner.cancelled == 2


async def test_aclose_without_a_screenshot_is_fine(executor):
    await executor.aclose()
    await executor.aclose()


async def test_screenshot_cancelled_by_escape_is_only_logged_at_debug(executor, runner, caplog):
    runner.script(["screencapture"], Result(1, "", "screencapture: cancelled\n"))
    with caplog.at_level(logging.DEBUG, logger="deck.remote.mac"):
        await executor.run("screenshot", None)
        await asyncio.sleep(0)
    records = [r for r in caplog.records if r.name == "deck.remote.mac"]
    assert records and all(r.levelno == logging.DEBUG for r in records)
    assert "cancelled" in records[0].getMessage()


# --- active_url -------------------------------------------------------------


async def test_active_url_returns_the_url(executor, runner):
    runner.script(["osascript"], Result(0, "https://t3.lab.nijho.lt/x\n", ""))
    assert await executor.run("active_url", None) == "https://t3.lab.nijho.lt/x"
    assert runner.calls == [active_url_argv()]


async def test_active_url_is_none_for_empty_output(executor, runner):
    runner.script(["osascript"], Result(0, "\n", ""))
    assert await executor.run("active_url", None) is None


# --- audio.output -----------------------------------------------------------


async def test_output_connects_bt_then_switches(executor, runner, fake_sleep):
    runner.script(BT_CONNECTED, Result(0, "[]\n", ""))
    runner.script(
        LIST_OUTPUTS,
        lines(device("MacBook Speakers")),
        lines(device("MacBook Speakers"), device("iLoud Micro Monitor")),
    )
    await executor.run("audio.output", ILOUD | {"name": "iLoud"})
    assert runner.calls == [
        BT_CONNECTED,
        ["blueutil", "--connect", BT],
        LIST_OUTPUTS,
        LIST_OUTPUTS,
        ["SwitchAudioSource", "-t", "output", "-s", "iLoud Micro Monitor"],
    ]
    assert fake_sleep.calls == [0.5]


async def test_output_already_connected_skips_connect(executor, runner):
    # same address, other case and separators
    runner.script(
        BT_CONNECTED,
        Result(0, json.dumps([{"address": "00:11:22:33:44:55", "name": "iLoud"}]), ""),
    )
    runner.script(LIST_OUTPUTS, lines(device("iLoud Micro Monitor")))
    await executor.run("audio.output", {"name": "iLoud", "match": "ILOUD", "bt": BT})
    assert ["blueutil", "--connect", BT] not in runner.calls
    assert runner.calls[-1] == ["SwitchAudioSource", "-t", "output", "-s", "iLoud Micro Monitor"]


async def test_output_without_bt_switches_directly(executor, runner):
    runner.script(LIST_OUTPUTS, lines(device("MacBook Speakers"), device("DELL U4025QW")))
    await executor.run("audio.output", {"name": "Monitor", "match": "dell"})
    assert runner.calls == [
        LIST_OUTPUTS,
        ["SwitchAudioSource", "-t", "output", "-s", "DELL U4025QW"],
    ]


async def test_output_without_bt_missing_device_fails_at_once(executor, runner, fake_sleep):
    runner.script(LIST_OUTPUTS, lines(device("MacBook Speakers")))
    with pytest.raises(RemoteError, match="Monitor did not appear"):
        await executor.run("audio.output", {"name": "Monitor", "match": "dell"})
    assert runner.calls == [LIST_OUTPUTS]
    assert fake_sleep.calls == []


async def test_output_bt_timeout(executor, runner, fake_sleep, clock):
    started = clock.now
    runner.script(BT_CONNECTED, Result(0, "[]", ""))
    runner.script(LIST_OUTPUTS, *[lines(device("MacBook Speakers"))] * 30)
    with pytest.raises(RemoteError, match="iLoud did not appear"):
        await executor.run("audio.output", ILOUD | {"name": "iLoud"})
    # polls at 0, 0.5 ... 10 s; the last one is at the deadline and nothing sleeps after it
    assert runner.calls.count(LIST_OUTPUTS) == 21
    assert fake_sleep.calls == [0.5] * 20
    assert clock.now - started == 10
    assert not any(c[:3] == ["SwitchAudioSource", "-t", "output"] for c in runner.calls)


async def test_output_connect_time_counts_toward_the_wait(scripts_dir, fake_sleep, clock):
    runner = SlowRunner(clock, cost=4)
    executor = MacExecutor(runner, scripts_dir, sleep=fake_sleep, clock=clock)
    started = clock.now
    runner.script(BT_CONNECTED, Result(0, "[]", ""))
    runner.script(LIST_OUTPUTS, *[lines(device("MacBook Speakers"))] * 30)
    with pytest.raises(RemoteError, match="iLoud did not appear"):
        await executor.run("audio.output", ILOUD | {"name": "iLoud"})
    assert runner.calls.count(LIST_OUTPUTS) == 13  # polls at 4, 4.5 ... 10 s
    assert sum(fake_sleep.calls) == 6
    assert clock.now - started == 10


async def test_output_last_sleep_stops_at_the_deadline(scripts_dir, fake_sleep, clock):
    runner = SlowRunner(clock, cost=4.25)
    executor = MacExecutor(runner, scripts_dir, sleep=fake_sleep, clock=clock)
    started = clock.now
    runner.script(BT_CONNECTED, Result(0, "[]", ""))
    with pytest.raises(RemoteError, match="did not appear"):
        await executor.run("audio.output", ILOUD | {"name": "iLoud"})
    assert fake_sleep.calls == [0.5] * 11 + [0.25]
    assert clock.now - started == 10


async def test_output_polls_once_when_connecting_used_up_the_wait(scripts_dir, fake_sleep, clock):
    runner = SlowRunner(clock, cost=11)
    executor = MacExecutor(runner, scripts_dir, sleep=fake_sleep, clock=clock)
    runner.script(BT_CONNECTED, Result(0, "[]", ""))
    with pytest.raises(RemoteError, match="did not appear"):
        await executor.run("audio.output", ILOUD | {"name": "iLoud"})
    assert runner.calls.count(LIST_OUTPUTS) == 1
    assert fake_sleep.calls == []


async def test_output_connect_failure_raises(executor, runner):
    runner.script(BT_CONNECTED, Result(0, "[]", ""))
    runner.script(["blueutil", "--connect"], Result(1, "", "Failed to connect\n"))
    with pytest.raises(RemoteError, match="Failed to connect"):
        await executor.run("audio.output", ILOUD | {"name": "iLoud"})
    assert LIST_OUTPUTS not in runner.calls


async def test_output_garbled_bt_list_raises(executor, runner):
    runner.script(BT_CONNECTED, Result(0, "not json", ""))
    with pytest.raises(RemoteError, match="unexpected blueutil output"):
        await executor.run("audio.output", ILOUD | {"name": "iLoud"})


async def test_output_disconnect(executor, runner):
    await executor.run("audio.output", ILOUD | {"name": "iLoud", "disconnect": True})
    assert runner.calls == [["blueutil", "--disconnect", BT]]


# --- mic.mute ---------------------------------------------------------------


async def test_mute_via_input_volume(executor, runner):
    runner.script(GET_INPUT_VOLUME, Result(0, "75\n", ""), Result(0, "0\n", ""))
    await executor.run("mic.mute", None)
    await executor.run("mic.mute", None)
    assert runner.calls == [
        GET_INPUT_VOLUME,
        input_volume_argv(0),
        GET_INPUT_VOLUME,
        input_volume_argv(75),
    ]


async def test_mute_restores_the_level_it_saved(executor, runner):
    runner.script(GET_INPUT_VOLUME, Result(0, "40\n", ""), Result(0, "0\n", ""))
    await executor.run("mic.mute", None)
    await executor.run("mic.mute", None)
    assert runner.calls[-1] == input_volume_argv(40)


async def test_unmute_without_a_saved_level_uses_75(executor, runner):
    runner.script(GET_INPUT_VOLUME, Result(0, "0\n", ""))
    await executor.run("mic.mute", None)
    assert runner.calls[-1] == input_volume_argv(75)


async def test_mute_falls_back_to_coreaudio(executor, runner):
    runner.script(GET_INPUT_VOLUME, *[Result(0, "missing value\n", "")] * 2)
    assert executor.coreaudio_muted is False
    await executor.run("mic.mute", None)
    assert runner.calls == [GET_INPUT_VOLUME, ["SwitchAudioSource", "-m", "toggle", "-t", "input"]]
    assert executor.coreaudio_muted is True
    await executor.run("mic.mute", None)
    assert executor.coreaudio_muted is False


async def test_mute_does_not_flip_the_flag_when_the_toggle_fails(executor, runner):
    runner.script(GET_INPUT_VOLUME, Result(0, "missing value\n", ""))
    runner.script(["SwitchAudioSource", "-m"], Result(1, "", "no device\n"))
    with pytest.raises(RemoteError, match="no device"):
        await executor.run("mic.mute", None)
    assert executor.coreaudio_muted is False


@pytest.mark.parametrize("start", [75, 0])
async def test_concurrent_mic_presses_each_toggle_once(scripts_dir, fake_sleep, start):
    runner = MicRunner(start)
    executor = MacExecutor(runner, scripts_dir, sleep=fake_sleep)
    await asyncio.gather(executor.run("mic.mute", None), executor.run("mic.mute", None))
    assert runner.volume == start  # two toggles end where they began


async def test_mute_garbled_volume_raises(executor, runner):
    runner.script(GET_INPUT_VOLUME, Result(0, "huh\n", ""))
    with pytest.raises(RemoteError, match="unexpected input volume"):
        await executor.run("mic.mute", None)
    assert runner.calls == [GET_INPUT_VOLUME]


# --- script -----------------------------------------------------------------


def make_script(directory, name="go.sh", mode=0o755):
    path = directory / name
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return path


async def test_script_runs_the_file(executor, runner, scripts_dir):
    path = make_script(scripts_dir)
    assert await executor.run("script", "go.sh") is None
    assert runner.calls == [[str(path.resolve())]]


async def test_script_follows_a_symlink_that_stays_inside(executor, runner, scripts_dir):
    target = make_script(scripts_dir, "real.sh")
    (scripts_dir / "alias.sh").symlink_to(target)
    await executor.run("script", "alias.sh")
    assert runner.calls == [[str(target.resolve())]]


async def test_script_rejects_escape(executor, runner, scripts_dir, tmp_path):
    outside = make_script(tmp_path, "outside.sh")
    (scripts_dir / "evil.sh").symlink_to(outside)
    with pytest.raises(RemoteError, match="bad args for script"):
        await executor.run("script", "evil.sh")
    assert runner.calls == []


async def test_script_must_exist(executor, runner):
    with pytest.raises(RemoteError, match="script not found: nope.sh"):
        await executor.run("script", "nope.sh")
    assert runner.calls == []


async def test_script_must_be_executable(executor, runner, scripts_dir):
    make_script(scripts_dir, "plain.txt", mode=0o644)
    with pytest.raises(RemoteError, match="script not executable: plain.txt"):
        await executor.run("script", "plain.txt")
    assert runner.calls == []


async def test_script_directory_is_not_a_script(executor, runner, scripts_dir):
    (scripts_dir / "sub").mkdir()
    with pytest.raises(RemoteError, match="script not found"):
        await executor.run("script", "sub")
    assert runner.calls == []


async def test_script_dir_may_be_a_symlink(runner, fake_sleep, scripts_dir, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(scripts_dir)
    path = make_script(scripts_dir)
    executor = MacExecutor(runner, link, sleep=fake_sleep)
    await executor.run("script", "go.sh")
    assert runner.calls == [[str(path.resolve())]]


# --- configure --------------------------------------------------------------


def test_configure_reads_pin_and_input_prefs(executor):
    executor.configure(
        {"audio": {"outputs": [ILOUD], "inputs": ["q9", 5, "usb"], "pin_input": True}}
    )
    assert executor.input_prefs == ["q9", "usb"]
    assert executor.pin_input is True


@pytest.mark.parametrize(
    "hello",
    [{}, {"audio": None}, {"audio": []}, {"audio": {"inputs": "q9", "pin_input": "yes"}}],
)
def test_configure_tolerates_missing_or_odd_audio(executor, hello):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    executor.configure(hello)
    assert executor.input_prefs == []
    assert executor.pin_input is False


# --- poller -----------------------------------------------------------------


def script_snapshot(
    runner,
    replace=None,
    *,
    current_input="Q9 USB",
    inputs=("Q9 USB", "MacBook Microphone"),
    times=1,
):
    """Script every poller query; `replace` maps a query's argv (as a tuple) to another result."""
    results = {
        tuple(CURRENT_OUTPUT): lines(device("iLoud Micro Monitor")),
        tuple(CURRENT_INPUT): lines(device(current_input, "input")),
        tuple(LIST_OUTPUTS): lines(device("MacBook Speakers"), device("iLoud Micro Monitor")),
        tuple(LIST_INPUTS): lines(*[device(i, "input") for i in inputs]),
        tuple(GET_VOLUME_SETTINGS): Result(0, SETTINGS, ""),
        tuple(BT_CONNECTED): Result(0, json.dumps([{"address": BT, "name": "iLoud"}]), ""),
        tuple(IDLE_ARGV): Result(0, IDLE_OUT, ""),
        **(replace or {}),
    }
    for argv, result in results.items():
        runner.script(list(argv), *[result] * times)


def settings(text):
    return {tuple(GET_VOLUME_SETTINGS): Result(0, text, "")}


async def test_poller_parses_snapshot(poller, runner):
    script_snapshot(runner)
    assert await poller.snapshot() == {
        "audio.output": "iLoud Micro Monitor",
        "audio.input": "Q9 USB",
        "audio.outputs": ["MacBook Speakers", "iLoud Micro Monitor"],
        "audio.inputs": ["Q9 USB", "MacBook Microphone"],
        "mic.muted": False,
        "volume": 42,
        "bt.connected": [BT],
        "idle_ms": 4500,
    }


async def test_poller_builds_fresh_values_each_call(poller, runner):
    script_snapshot(runner, times=2)
    first = await poller.snapshot()
    second = await poller.snapshot()
    assert first == second
    for key in ("audio.outputs", "audio.inputs", "bt.connected"):
        assert first[key] is not second[key]


async def test_poller_reports_muted_when_input_volume_is_zero(poller, runner):
    script_snapshot(
        runner, settings("output volume:42, input volume:0, alert volume:100, output muted:false")
    )
    assert (await poller.snapshot())["mic.muted"] is True


async def test_poller_mic_muted_uses_the_flag_without_input_volume(poller, runner, executor):
    missing = settings("output volume:42, input volume:missing value, output muted:false")
    script_snapshot(runner, missing, times=2)
    assert (await poller.snapshot())["mic.muted"] is False
    executor.coreaudio_muted = True
    assert (await poller.snapshot())["mic.muted"] is True


async def test_poller_leaves_out_keys_whose_query_failed(poller, runner):
    script_snapshot(
        runner,
        {
            tuple(CURRENT_OUTPUT): Result(1, "", "boom"),
            tuple(BT_CONNECTED): Result(-1, "", "blueutil: No such file or directory"),
        },
    )
    snapshot = await poller.snapshot()
    assert "audio.output" not in snapshot and "bt.connected" not in snapshot
    assert snapshot["audio.input"] == "Q9 USB"
    assert snapshot["volume"] == 42


@pytest.mark.parametrize(
    ("argv", "garbage", "gone"),
    [
        (CURRENT_OUTPUT, "not json", {"audio.output"}),
        (LIST_INPUTS, '["a"]', {"audio.inputs"}),
        (LIST_OUTPUTS, '{"nope": 1}', {"audio.outputs"}),
        (GET_VOLUME_SETTINGS, "garbage", {"volume", "mic.muted"}),
        (BT_CONNECTED, '{"address": "x"}', {"bt.connected"}),
        (IDLE_ARGV, "+-o IOHIDSystem\n", {"idle_ms"}),
    ],
)
async def test_poller_tolerates_unparseable_output(poller, runner, argv, garbage, gone):
    script_snapshot(runner, {tuple(argv): Result(0, garbage, "")})
    snapshot = await poller.snapshot()
    assert not gone & snapshot.keys()
    assert len(snapshot) == 8 - len(gone)


async def test_poller_with_everything_failing_is_empty(poller, runner):
    for argv in (
        CURRENT_OUTPUT,
        CURRENT_INPUT,
        LIST_OUTPUTS,
        LIST_INPUTS,
        GET_VOLUME_SETTINGS,
        BT_CONNECTED,
        IDLE_ARGV,
    ):
        runner.script(argv, Result(-1, "", f"{argv[0]}: No such file or directory"))
    assert await poller.snapshot() == {}


async def test_poller_volume_is_left_out_without_software_volume(poller, runner):
    script_snapshot(
        runner, settings("output volume:missing value, input volume:75, output muted:false")
    )
    snapshot = await poller.snapshot()
    assert "volume" not in snapshot and snapshot["mic.muted"] is False


@pytest.mark.parametrize(
    ("nanoseconds", "expected"),
    [(4500000000, 4500), (999999, 0), (1999999, 1), (0, 0)],  # whole ms, rounded down
)
async def test_poller_reports_idle_in_whole_milliseconds(poller, runner, nanoseconds, expected):
    out = f'    "HIDIdleTime" = {nanoseconds}\n'
    script_snapshot(runner, {tuple(IDLE_ARGV): Result(0, out, "")})
    assert (await poller.snapshot())["idle_ms"] == expected


async def test_poller_without_idle_ms_when_ioreg_fails(poller, runner):
    script_snapshot(runner, {tuple(IDLE_ARGV): Result(1, "", "ioreg: boom")})
    snapshot = await poller.snapshot()
    assert "idle_ms" not in snapshot
    assert snapshot["volume"] == 42


async def test_poller_polls_with_timeouts(executor):
    timed = TimeoutRunner()
    await MacPoller(timed, executor).snapshot()
    assert len(timed.timeouts) == 7 and all(0 < t <= 10 for t in timed.timeouts)


# --- pin_input --------------------------------------------------------------

AIRPODS_INPUTS = ("AirPods", "Q9 USB")
SWITCH_INPUT = ["SwitchAudioSource", "-t", "input"]


def switched_input(runner) -> list[list[str]]:
    return [c for c in runner.calls if c[:3] == SWITCH_INPUT]


async def baseline(poller, runner, current_input="Q9 USB", inputs=("Q9 USB",)):
    """The first poll, which only records which input devices there are."""
    script_snapshot(runner, current_input=current_input, inputs=inputs)
    await poller.snapshot()


async def test_pin_input_switches_back_when_a_device_appears(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    script_snapshot(runner, current_input="AirPods", inputs=AIRPODS_INPUTS)  # AirPods connect
    snapshot = await poller.snapshot()
    assert runner.calls[-1] == ["SwitchAudioSource", "-t", "input", "-s", "Q9 USB"]
    assert snapshot["audio.input"] == "Q9 USB"


async def test_pin_input_switches_back_when_a_device_goes_away(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9", "macbook"], "pin_input": True}})
    await baseline(poller, runner, "Q9 USB", ("Q9 USB", "MacBook Microphone"))
    script_snapshot(runner, current_input="MacBook Microphone", inputs=("MacBook Microphone",))
    assert (await poller.snapshot())["audio.input"] == "MacBook Microphone"  # nothing to pin to
    assert switched_input(runner) == []
    script_snapshot(
        runner, current_input="MacBook Microphone", inputs=("Q9 USB", "MacBook Microphone")
    )
    assert (await poller.snapshot())["audio.input"] == "Q9 USB"  # the mic is back
    assert switched_input(runner) == [[*SWITCH_INPUT, "-s", "Q9 USB"]]


async def test_pin_input_leaves_a_changed_input_alone_while_the_devices_stay(
    poller, runner, executor
):
    executor.configure({"audio": {"inputs": ["q9", "airpods"], "pin_input": True}})
    await baseline(poller, runner, "Q9 USB", AIRPODS_INPUTS)
    for _ in range(3):  # a deliberate choice (the input dial) is not undone by later polls
        script_snapshot(runner, current_input="AirPods", inputs=AIRPODS_INPUTS)
        assert (await poller.snapshot())["audio.input"] == "AirPods"
    assert switched_input(runner) == []


PIN_TO_Q9 = [*SWITCH_INPUT, "-s", "Q9 USB"]


async def poll_input(runner, poller, current_input, inputs=AIRPODS_INPUTS) -> str:
    script_snapshot(runner, current_input=current_input, inputs=inputs)
    return (await poller.snapshot())["audio.input"]


async def test_pin_input_reverts_a_switch_one_poll_after_the_device_appears(
    poller, runner, executor
):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    assert await poll_input(runner, poller, "Q9 USB") == "Q9 USB"  # AirPods listed, not chosen yet
    assert switched_input(runner) == []
    assert await poll_input(runner, poller, "AirPods") == "Q9 USB"  # macOS switched a poll later
    assert switched_input(runner) == [PIN_TO_Q9]


async def test_pin_input_keeps_watching_for_three_polls_after_the_change(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    for _ in range(3):  # the change poll and two more
        assert await poll_input(runner, poller, "Q9 USB") == "Q9 USB"
    assert await poll_input(runner, poller, "AirPods") == "Q9 USB"  # the third poll after it
    assert switched_input(runner) == [PIN_TO_Q9]


async def test_pin_input_stops_watching_after_the_window(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    for _ in range(4):  # the change poll and three more
        assert await poll_input(runner, poller, "Q9 USB") == "Q9 USB"
    assert await poll_input(runner, poller, "AirPods") == "AirPods"  # a deliberate choice now
    assert switched_input(runner) == []


async def test_pin_input_retries_a_failed_pin_within_the_window(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    runner.script(SWITCH_INPUT, Result(1, "", "nope\n"))  # the first attempt fails
    assert await poll_input(runner, poller, "AirPods") == "AirPods"
    assert await poll_input(runner, poller, "AirPods") == "Q9 USB"  # retried a poll later
    assert switched_input(runner) == [PIN_TO_Q9, PIN_TO_Q9]


async def test_pin_input_gives_up_a_failing_pin_after_the_window(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    runner.script(SWITCH_INPUT, *[Result(1, "", "nope\n")] * 10)
    for _ in range(4):
        assert await poll_input(runner, poller, "AirPods") == "AirPods"
    assert await poll_input(runner, poller, "AirPods") == "AirPods"
    assert len(switched_input(runner)) == 4  # the change poll and the three after it


async def test_pin_input_stops_watching_once_pinned(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    assert await poll_input(runner, poller, "AirPods") == "Q9 USB"
    # the user (the input dial) then picks AirPods on purpose
    assert await poll_input(runner, poller, "AirPods") == "AirPods"
    assert switched_input(runner) == [PIN_TO_Q9]


async def test_pin_input_another_device_change_restarts_the_window(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    for _ in range(3):
        await poll_input(runner, poller, "Q9 USB")
    more = (*AIRPODS_INPUTS, "Brio")
    assert await poll_input(runner, poller, "Q9 USB", more) == "Q9 USB"  # a new device appears
    for _ in range(2):
        assert await poll_input(runner, poller, "Q9 USB", more) == "Q9 USB"
    assert await poll_input(runner, poller, "AirPods", more) == "Q9 USB"
    assert switched_input(runner) == [PIN_TO_Q9]


async def test_pin_input_turned_off_mid_window_stops_watching(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    await poll_input(runner, poller, "Q9 USB")
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": False}})
    assert await poll_input(runner, poller, "AirPods") == "AirPods"
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    assert await poll_input(runner, poller, "AirPods") == "AirPods"  # the window is gone
    assert switched_input(runner) == []


async def test_pin_input_does_not_pin_on_the_first_poll(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    script_snapshot(runner, current_input="AirPods", inputs=AIRPODS_INPUTS)
    assert (await poller.snapshot())["audio.input"] == "AirPods"
    assert switched_input(runner) == []


async def test_pin_input_off_leaves_the_input_alone(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": False}})
    await baseline(poller, runner)
    script_snapshot(runner, current_input="AirPods", inputs=AIRPODS_INPUTS)
    snapshot = await poller.snapshot()
    assert switched_input(runner) == []
    assert snapshot["audio.input"] == "AirPods"


async def test_pin_input_does_nothing_when_already_preferred(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    script_snapshot(runner, current_input="Q9 USB", inputs=AIRPODS_INPUTS)
    await poller.snapshot()
    assert switched_input(runner) == []


async def test_pin_input_does_nothing_when_no_preference_is_available(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner, "MacBook Microphone", ("MacBook Microphone",))
    script_snapshot(runner, current_input="AirPods", inputs=("AirPods", "MacBook Microphone"))
    snapshot = await poller.snapshot()
    assert switched_input(runner) == []
    assert snapshot["audio.input"] == "AirPods"


async def test_pin_input_failure_keeps_the_current_input(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    script_snapshot(runner, current_input="AirPods", inputs=AIRPODS_INPUTS)
    runner.script(["SwitchAudioSource", "-t", "input", "-s"], Result(1, "", "nope\n"))
    assert (await poller.snapshot())["audio.input"] == "AirPods"


async def test_pin_input_ignores_a_failed_device_listing(poller, runner, executor):
    executor.configure({"audio": {"inputs": ["q9"], "pin_input": True}})
    await baseline(poller, runner)
    script_snapshot(
        runner,
        {tuple(LIST_INPUTS): Result(1, "", "boom")},
        current_input="AirPods",
        inputs=AIRPODS_INPUTS,
    )
    snapshot = await poller.snapshot()
    assert "audio.inputs" not in snapshot and snapshot["audio.input"] == "AirPods"
    assert switched_input(runner) == []
    script_snapshot(runner, current_input="AirPods", inputs=AIRPODS_INPUTS)
    assert (await poller.snapshot())["audio.input"] == "Q9 USB"  # compared with the last good list


# --- deck connect -----------------------------------------------------------


def test_main_connect_runs_connect_forever_over_ssh_with_the_mac_executor_and_poller(
    monkeypatch, capsys
):
    seen = {}

    async def fake_connect_forever(argv, executor, poller):
        seen.update(argv=argv, executor=executor, poller=poller)

    monkeypatch.setattr(cli, "connect_forever", fake_connect_forever)
    assert cli.main(["connect", "me@pc", "--key", "/keys/pc_ed25519"]) == 0
    assert seen["argv"] == ssh_argv("me@pc", Path("/keys/pc_ed25519"))
    assert isinstance(seen["executor"], MacExecutor)
    assert isinstance(seen["poller"], MacPoller)
    assert seen["poller"]._executor is seen["executor"]
    assert capsys.readouterr().out == ""


def test_main_connect_without_a_key_leaves_it_to_ssh(monkeypatch):
    seen = {}

    async def fake_connect_forever(argv, executor, poller):
        seen["argv"] = argv

    monkeypatch.setattr(cli, "connect_forever", fake_connect_forever)
    assert cli.main(["connect", "me@pc"]) == 0
    assert seen["argv"] == ssh_argv("me@pc", None)


def test_main_connect_needs_a_destination(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["connect"])
    assert exit_info.value.code == 2
    assert "destination" in capsys.readouterr().err


def test_extend_path_prepends_missing_tool_directories_that_exist(monkeypatch, tmp_path):
    brew, local, absent, present = (tmp_path / n for n in ("brew", "local", "absent", "present"))
    for directory in (brew, local, present):
        directory.mkdir()
    monkeypatch.setattr(cli, "TOOL_DIRS", (str(brew), str(present), str(absent), str(local)))
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", f"{present}/", "/bin"]))
    cli._extend_path()
    assert os.environ["PATH"].split(os.pathsep) == [
        str(brew),
        str(local),
        "/usr/bin",
        f"{present}/",  # already there: left where it is
        "/bin",
    ]


def test_extend_path_expands_the_home_directory(monkeypatch, tmp_path):
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setattr(cli, "TOOL_DIRS", ("~/.local/bin",))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    cli._extend_path()
    assert os.environ["PATH"] == f"{tmp_path}/.local/bin{os.pathsep}/usr/bin"


@pytest.mark.parametrize("path", [None, ""])
def test_extend_path_without_a_path(monkeypatch, tmp_path, path):
    monkeypatch.setattr(cli, "TOOL_DIRS", (str(tmp_path),))
    if path is None:
        monkeypatch.delenv("PATH", raising=False)
    else:
        monkeypatch.setenv("PATH", path)
    cli._extend_path()
    assert os.environ["PATH"] == str(tmp_path)  # no empty entry, which would mean "here"


def test_extend_path_leaves_path_alone_when_nothing_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "TOOL_DIRS", (str(tmp_path), str(tmp_path / "absent")))
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}{tmp_path}")
    cli._extend_path()
    assert os.environ["PATH"] == f"/usr/bin{os.pathsep}{tmp_path}"


def test_main_connect_extends_path_before_building_the_executor(monkeypatch, tmp_path, capsys):
    seen = {}

    def spy(runner):
        seen["path"] = os.environ["PATH"]
        return MacExecutor(runner)

    async def fake_connect_forever(argv, executor, poller):
        pass

    monkeypatch.setattr(cli, "TOOL_DIRS", (str(tmp_path),))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(cli, "MacExecutor", spy)
    monkeypatch.setattr(cli, "connect_forever", fake_connect_forever)
    assert cli.main(["connect", "me@pc"]) == 0
    assert seen["path"] == f"{tmp_path}{os.pathsep}/usr/bin"
    assert capsys.readouterr().out == ""
