import asyncio
import contextlib

import pytest
from fake_runner import FakeRunner
from test_ddc import BRIGHTNESS, BUS4, DETECT, MODEL, settle

from deck import cli
from deck.actions import ActionRunner
from deck.config import Action, Config, DialSpec
from deck.ddc import Ddc, DdcError
from deck.dials import DIAL_FACTORIES, Services
from deck.local import BrightnessDial, register_local_actions
from deck.runner import Result
from deck.state import StateStore

PRESETS = [35, 80]


@pytest.fixture
def runner() -> FakeRunner:
    runner = FakeRunner()
    runner.script(["ddcutil", "detect"], *[Result(0, DETECT, "")] * 20)
    return runner


@pytest.fixture
def services(runner) -> Services:
    store = StateStore()
    services = Services(store, Config(pages={"home": {}}), actions=ActionRunner())
    services.ddc = Ddc(runner, MODEL, store)
    register_local_actions(services, runner)
    return services


def dial(services: Services, presets: list[int] = PRESETS) -> BrightnessDial:
    return BrightnessDial(DialSpec(kind="brightness", presets=presets), services)


# --- shell ------------------------------------------------------------------


async def test_shell_runs_the_argv(services, runner):
    await services.actions.run(Action(type="shell", args=["notify-send", "hi there", "$HOME"]))
    assert runner.calls == [["notify-send", "hi there", "$HOME"]]


async def test_shell_failure_raises_with_the_reason(services, runner):
    runner.script(["false"], Result(1, "", "nope\n"))
    with pytest.raises(RuntimeError, match=r"false.*1.*nope"):
        await services.actions.run(Action(type="shell", args=["false"]))


async def test_shell_empty_command_raises(services, runner):
    with pytest.raises(ValueError, match="empty"):
        await services.actions.run(Action(type="shell", args=[]))
    assert runner.calls == []


# --- ddc action -------------------------------------------------------------


async def test_ddc_action_sets_the_vcp_on_the_detected_bus(services, runner):
    await services.actions.run(Action(type="ddc", args={"code": 16, "value": 35}))
    assert runner.calls == [["ddcutil", "detect", "--brief"], [*BUS4, "setvcp", "10", "35"]]


async def test_ddc_action_failure_raises(services, runner):
    runner.script(["ddcutil", "--bus"], Result(1, "", "bad\n"), Result(1, "", "bad\n"))
    with pytest.raises(DdcError, match="bad"):
        await services.actions.run(Action(type="ddc", args={"code": 16, "value": 35}))


async def test_ddc_action_without_ddc_section_raises(runner):
    services = Services(StateStore(), Config(pages={"home": {}}), actions=ActionRunner())
    register_local_actions(services, runner)
    with pytest.raises(RuntimeError, match="ddc"):
        await services.actions.run(Action(type="ddc", args={"code": 16, "value": 35}))


# --- brightness dial --------------------------------------------------------


def test_brightness_factory_is_registered():
    assert DIAL_FACTORIES["brightness"] is BrightnessDial


def test_brightness_dial_needs_the_ddc_service():
    services = Services(StateStore(), Config(pages={"home": {}}), actions=ActionRunner())
    with pytest.raises(RuntimeError, match="restart"):
        dial(services)


async def test_brightness_turn_steps_by_two_per_tick(services, runner):
    services.store.set("ddc.brightness", 50)
    await dial(services).on_turn(3)
    assert services.store.get("ddc.brightness") == 56
    await dial(services).on_turn(-1)
    assert services.store.get("ddc.brightness") == 54
    await settle()
    assert runner.calls[-1] == [*BUS4, "setvcp", "10", "54"]


async def test_brightness_turns_accumulate_before_the_monitor_answers(services, runner):
    services.store.set("ddc.brightness", 50)
    runner.block = asyncio.Event()
    handler = dial(services)
    for _ in range(5):
        await handler.on_turn(1)
    assert services.store.get("ddc.brightness") == 60
    runner.block.set()
    await settle()
    assert [c[-1] for c in runner.calls if "setvcp" in c] == ["52", "60"]


async def test_brightness_turn_clamps(services):
    services.store.set("ddc.brightness", 99)
    await dial(services).on_turn(5)
    assert services.store.get("ddc.brightness") == 100
    await dial(services).on_turn(-100)
    assert services.store.get("ddc.brightness") == 0


async def test_brightness_turn_before_the_first_read_is_ignored(services, runner):
    await dial(services).on_turn(1)
    await settle()
    assert runner.calls == []
    assert services.store.get("ddc.brightness") is None


@pytest.mark.parametrize(("current", "expected"), [(78, 35), (36, 80), (35, 80), (80, 35), (0, 80), (100, 35)])
async def test_brightness_press_goes_to_the_next_preset(services, current, expected):
    services.store.set("ddc.brightness", current)
    await dial(services).on_press()
    assert services.store.get("ddc.brightness") == expected


async def test_brightness_press_cycles_custom_presets_in_list_order(services):
    handler = dial(services, [10, 50, 90])
    seen = []
    services.store.set("ddc.brightness", 12)
    for _ in range(4):
        await handler.on_press()
        seen.append(services.store.get("ddc.brightness"))
    assert seen == [50, 90, 10, 50]


async def test_brightness_press_before_the_first_read_picks_the_first_preset(services):
    await dial(services).on_press()
    assert services.store.get("ddc.brightness") == 35


async def test_brightness_press_without_presets_does_nothing(services, runner):
    services.store.set("ddc.brightness", 50)
    await dial(services, []).on_press()
    await settle()
    assert services.store.get("ddc.brightness") == 50
    assert runner.calls == []


# --- the daemon's services --------------------------------------------------


def _config(**sections) -> Config:
    return Config.model_validate({"pages": {"home": {}}, **sections})


async def test_services_without_ddc_register_shell_only(monkeypatch, runner):
    monkeypatch.setattr(cli, "Runner", lambda: runner)
    async with contextlib.AsyncExitStack() as stack:
        services = await cli._services(_config(), StateStore(), stack)
    assert services.ddc is None
    await services.actions.run(Action(type="shell", args=["true"]))
    assert runner.calls == [["true"]]


async def test_services_with_ddc_poll_and_clean_up(monkeypatch, runner):
    runner.script([*BUS4, "getvcp"], Result(0, BRIGHTNESS, ""))
    monkeypatch.setattr(cli, "Runner", lambda: runner)
    store = StateStore()
    stack = contextlib.AsyncExitStack()
    services = await cli._services(_config(ddc={"model": MODEL}), store, stack)
    assert isinstance(services.ddc, Ddc)
    for _ in range(100):
        if store.get("ddc.brightness") == 100:
            break
        await asyncio.sleep(0.01)
    assert store.get("ddc.brightness") == 100
    poll = next(t for t in asyncio.all_tasks() if "poll_forever" in repr(t.get_coro()))
    await stack.aclose()
    assert poll.cancelled()
