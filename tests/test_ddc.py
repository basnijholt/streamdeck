import asyncio
import logging

import pytest
from fake_runner import FakeRunner

from deck.ddc import Ddc, DdcError
from deck.runner import Result
from deck.state import StateStore

DETECT = """Display 1
   I2C bus:          /dev/i2c-4
   DRM connector:    card1-DP-1
   Monitor:          DEL:DELL U4025QW:4350B34

Invalid display
   I2C bus:          /dev/i2c-5
   Monitor:          IDV:AOC28E850.HDR:
"""
MODEL = "DELL U4025QW"
BRIGHTNESS = "VCP code 0x10 (Brightness                    ): current value =   100, max value =   100"
BUS4 = ["ddcutil", "--bus", "4", "--skip-ddc-checks", "--sleep-multiplier", "0.3"]
BUS6 = ["ddcutil", "--bus", "6", "--skip-ddc-checks", "--sleep-multiplier", "0.3"]


@pytest.fixture
def runner() -> FakeRunner:
    runner = FakeRunner()
    runner.script(["ddcutil", "detect"], *[Result(0, DETECT, "")] * 20)
    return runner


@pytest.fixture
def store() -> StateStore:
    return StateStore()


@pytest.fixture
def ddc(runner, store) -> Ddc:
    return Ddc(runner, MODEL, store)


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def sets(runner: FakeRunner) -> list[str]:
    return [c[-1] for c in runner.calls if "setvcp" in c]


def detects(runner: FakeRunner) -> int:
    return sum(c[:2] == ["ddcutil", "detect"] for c in runner.calls)


# --- detect_bus -------------------------------------------------------------


async def test_detect_bus_matches_model(runner):
    assert await Ddc(runner, "DELL U4025QW", StateStore()).detect_bus() == 4
    assert runner.calls == [["ddcutil", "detect", "--brief"]]


async def test_detect_bus_picks_the_block_of_the_model(runner):
    assert await Ddc(runner, "aoc28e850", StateStore()).detect_bus() == 5


async def test_detect_bus_without_a_match_raises(runner):
    with pytest.raises(DdcError, match="LG"):
        await Ddc(runner, "LG", StateStore()).detect_bus()


async def test_detect_bus_never_matches_an_empty_model(runner):
    with pytest.raises(DdcError):
        await Ddc(runner, "", StateStore()).detect_bus()


async def test_detect_bus_failure_raises_with_the_reason():
    runner = FakeRunner()
    runner.script(["ddcutil", "detect"], Result(1, "", "ddcutil: no i2c devices\n"))
    with pytest.raises(DdcError, match="no i2c devices"):
        await Ddc(runner, MODEL, StateStore()).detect_bus()


# --- get_brightness ---------------------------------------------------------


async def test_get_brightness_argv_and_parse(runner, ddc):
    runner.script([*BUS4, "getvcp"], Result(0, BRIGHTNESS + "\n", ""))
    assert await ddc.get_brightness() == 100
    assert runner.calls[-1] == [*BUS4, "getvcp", "10"]


async def test_bus_is_detected_once(runner, ddc):
    runner.script([*BUS4, "getvcp"], Result(0, BRIGHTNESS, ""), Result(0, BRIGHTNESS, ""))
    await ddc.get_brightness()
    await ddc.get_brightness()
    assert detects(runner) == 1


async def test_get_brightness_unparsable_output_raises(runner, ddc):
    runner.script([*BUS4, "getvcp"], Result(0, "VCP code 0x10: no idea\n", ""))
    with pytest.raises(DdcError, match="no idea"):
        await ddc.get_brightness()


async def test_get_brightness_failure_redetects_once_then_raises(runner, ddc):
    runner.script([*BUS4, "getvcp"], Result(1, "", "Display not found\n"), Result(1, "", "Display not found\n"))
    with pytest.raises(DdcError, match="Display not found"):
        await ddc.get_brightness()
    assert detects(runner) == 2


# --- set_brightness ---------------------------------------------------------


async def test_set_brightness_updates_the_store_at_once(runner, store, ddc):
    runner.block = asyncio.Event()
    ddc.set_brightness(42)
    assert store.get("ddc.brightness") == 42
    runner.block.set()
    await settle()
    assert runner.calls[-1] == [*BUS4, "setvcp", "10", "42"]


async def test_set_brightness_clamps(runner, store, ddc):
    ddc.set_brightness(150)
    await settle()
    assert store.get("ddc.brightness") == 100
    ddc.set_brightness(-7)
    await settle()
    assert store.get("ddc.brightness") == 0
    assert sets(runner) == ["100", "0"]


async def test_set_brightness_coalesces(runner, ddc):
    runner.block = asyncio.Event()
    for v in range(10, 60):
        ddc.set_brightness(v)
    runner.block.set()
    await settle()
    assert sets(runner) == ["10", "59"]


async def test_failed_set_redetects_once_and_retries(store):
    runner = FakeRunner()
    runner.script(["ddcutil", "detect"], Result(0, DETECT, ""), Result(0, DETECT.replace("i2c-4", "i2c-6"), ""))
    runner.script([*BUS4, "setvcp"], Result(1, "", "Display not found\n"))
    ddc = Ddc(runner, MODEL, store)
    ddc.set_brightness(50)
    await settle()
    assert detects(runner) == 2
    assert [c[:3] for c in runner.calls if "setvcp" in c] == [BUS4[:3], BUS6[:3]]
    assert runner.calls[-1] == [*BUS6, "setvcp", "10", "50"]
    assert store.get("ddc.error") is None


async def test_persistent_failure_sets_error_and_success_clears_it(runner, store, ddc):
    runner.script(["ddcutil", "--bus"], Result(1, "", "Display not found\n"), Result(1, "", "Display not found\n"))
    ddc.set_brightness(50)
    await settle()
    assert detects(runner) == 2  # the first detect and the single re-detect
    assert sets(runner) == ["50", "50"]
    assert "Display not found" in store.get("ddc.error")
    assert store.get("ddc.brightness") == 50  # the dial keeps showing the target

    ddc.set_brightness(60)
    await settle()
    assert store.get("ddc.error") is None


async def test_unreachable_monitor_sets_error_instead_of_raising(store):
    runner = FakeRunner()
    runner.script(["ddcutil", "detect"], Result(0, "", ""))
    ddc = Ddc(runner, MODEL, store)
    ddc.set_brightness(50)
    await settle()
    assert "DELL U4025QW" in store.get("ddc.error")


# --- setvcp -----------------------------------------------------------------


async def test_setvcp_sends_the_code_in_hex(runner, ddc):
    await ddc.setvcp(214, 5)
    assert runner.calls[-1] == [*BUS4, "setvcp", "d6", "5"]


async def test_setvcp_failure_raises(runner, ddc):
    runner.script(["ddcutil", "--bus"], Result(1, "", "bad\n"), Result(1, "", "bad\n"))
    with pytest.raises(DdcError, match="bad"):
        await ddc.setvcp(16, 35)
    assert detects(runner) == 2


async def test_setvcp_of_brightness_updates_the_store(runner, store, ddc):
    await ddc.setvcp(16, 35)
    assert store.get("ddc.brightness") == 35
    await ddc.setvcp(0xD6, 1)
    assert store.get("ddc.brightness") == 35


# --- refresh and poll_forever ------------------------------------------------


async def test_refresh_reads_brightness_into_the_store(runner, store, ddc):
    runner.script([*BUS4, "getvcp"], Result(0, BRIGHTNESS, ""))
    store.set("ddc.error", "stale")
    await ddc.refresh()
    assert store.get("ddc.brightness") == 100
    assert store.get("ddc.error") is None


async def test_refresh_drops_a_read_overtaken_by_a_set(runner, store, ddc):
    runner.script([*BUS4, "getvcp"], Result(0, BRIGHTNESS, ""))
    runner.block = asyncio.Event()
    read = asyncio.create_task(ddc.refresh())
    await settle()
    ddc.set_brightness(50)
    runner.block.set()
    await read
    await settle()
    assert store.get("ddc.brightness") == 50


async def test_poll_forever_reads_every_interval(runner, store, ddc):
    runner.script([*BUS4, "getvcp"], Result(0, BRIGHTNESS, ""), Result(0, BRIGHTNESS.replace("100,", "30,"), ""))
    task = asyncio.create_task(ddc.poll_forever(interval=0.02))
    try:
        await _until(lambda: store.get("ddc.brightness") == 30)
    finally:
        task.cancel()
    assert detects(runner) == 1


async def test_poll_forever_survives_failures(runner, store, ddc, caplog):
    bad = Result(1, "", "Display not found\n")
    runner.script([*BUS4, "getvcp"], bad, bad, Result(0, BRIGHTNESS, ""))
    with caplog.at_level(logging.WARNING, logger="deck.ddc"):
        task = asyncio.create_task(ddc.poll_forever(interval=0.02))
        try:
            await _until(lambda: "Display not found" in (store.get("ddc.error") or ""))
            await _until(lambda: store.get("ddc.brightness") == 100)
        finally:
            task.cancel()
    assert store.get("ddc.error") is None
    assert not task.done() or task.cancelled()


async def test_poll_forever_survives_unexpected_errors(store, caplog):
    class Flaky(FakeRunner):
        async def run(self, argv, timeout=10):
            if not self.calls:
                self.calls.append(argv)
                raise RuntimeError("boom")
            return await super().run(argv, timeout)

    flaky = Flaky()
    flaky.script(["ddcutil", "detect"], Result(0, DETECT, ""))
    flaky.script([*BUS4, "getvcp"], Result(0, BRIGHTNESS, ""))
    ddc = Ddc(flaky, MODEL, store)
    with caplog.at_level(logging.WARNING, logger="deck.ddc"):
        task = asyncio.create_task(ddc.poll_forever(interval=0.02))
        try:
            await _until(lambda: store.get("ddc.brightness") == 100)
        finally:
            task.cancel()
    assert "boom" in caplog.text


async def test_poll_forever_logs_an_unchanged_error_once(runner, store, ddc, caplog):
    bad = Result(1, "", "Display not found\n")
    runner.script(["ddcutil", "--bus"], *[bad] * 40)
    with caplog.at_level(logging.WARNING, logger="deck.ddc"):
        task = asyncio.create_task(ddc.poll_forever(interval=0.01))
        await asyncio.sleep(0.15)
        task.cancel()
    assert sum("Display not found" in r.getMessage() for r in caplog.records) == 1


async def _until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.005)
