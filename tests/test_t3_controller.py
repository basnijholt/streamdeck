import asyncio
import contextlib
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from deck import cli
from deck.config import Action, Config, DialSpec, T3Config, T3Server
from deck.dials import DIAL_FACTORIES, Services
from deck.mac.client import MacError, MacOffline
from deck.state import StateStore
from deck.t3.controller import EffortDial, ModelDial, T3Controller
from deck.t3.model import Pending, Thread
from deck.t3.source import ModelSelection, T3Client, T3Error

ENV_ID = "6cd1637d-2c2f-4fa6-8cd2-fa04748043e2"
HOST = "t3.lab.nijho.lt"
T0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

SERVER = T3Server(
    name="pc",
    url="http://127.0.0.1:3773",
    web_url=f"https://{HOST}",
    environment_id=ENV_ID,
    token_env="T3_TOKEN",
)
LAPTOP = T3Server(
    name="laptop",
    url="http://127.0.0.1:3774",
    web_url="https://t3.laptop.example",
    environment_id="0a0a0a0a-1111-2222-3333-444444444444",
    token_env="T3_LAPTOP_TOKEN",
)
CLAUDE = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
CODEX = ["gpt-5", "gpt-5-mini"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
T3_CONFIG = T3Config(
    servers=[SERVER], models={"claudeAgent": CLAUDE, "codex": CODEX}, efforts=EFFORTS
)

NO_THREAD = "no T3 thread in front"
CHANGED = "thread changed — not applied"
OFFLINE = "Mac offline — not applied"


def thread(thread_id="aaaaaaaa-1", **overrides) -> Thread:
    fields = dict(
        server="pc",
        thread_id=thread_id,
        project_id="p1",
        title="Fix the thing",
        status="idle",
        pending_requests=0,
        model="claude-opus-5-5",
        instance_id="claudeAgent",
        options=(("effort", "xhigh"), ("fastMode", False)),
        settled=False,
        archived=False,
        parent_thread_id=None,
        updated_at=T0,
    )
    return Thread(**{**fields, **overrides})


T1 = thread("aaaaaaaa-1", title="First", updated_at=T0 + timedelta(minutes=2))
T2 = thread("bbbbbbbb-2", title="Second", updated_at=T0 + timedelta(minutes=1))
CODEX_THREAD = thread(
    "cccccccc-3",
    title="Codex one",
    model="gpt-5",
    instance_id="codex",
    options=(("reasoningEffort", "high"),),
    updated_at=T0,
)


def url_of(t: Thread, server: T3Server = SERVER) -> str:
    return f"{server.web_url}/{server.environment_id}/{t.thread_id}"


def selection(t: Thread, **changes) -> ModelSelection:
    """`t`'s selection with `changes` applied: `model=...` or an option such as `effort=...`."""
    model = changes.pop("model", t.model)
    options = tuple((key, changes.get(key, value)) for key, value in t.options)
    return ModelSelection(t.instance_id, model, options)


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


class FakeTime:
    """A clock and a `sleep` that only wakes when the test advances the clock."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + delay, future))
        await future

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        due = [s for s in self._sleepers if s[0] <= self.now]
        self._sleepers = [s for s in self._sleepers if s[0] > self.now]
        for _, future in due:
            if not future.done():
                future.set_result(None)
        await settle()


class FakeMac:
    """Answers `active_url` from `active_urls`, one entry (URL, None or exception) per read."""

    def __init__(self) -> None:
        self.active_urls: list[str | None | Exception] = []
        self.requests: list[tuple[str, object]] = []
        self.gate: asyncio.Event | None = None  # holds every `active_url` read while it is unset
        self.error: Exception | None = None  # raised by every other request

    @property
    def reads(self) -> int:
        return sum(1 for kind, _ in self.requests if kind == "active_url")

    async def request(self, kind, args, timeout=15):
        self.requests.append((kind, args))
        if kind != "active_url":
            if self.error is not None:
                raise self.error
            return None
        if self.gate is not None:
            await self.gate.wait()
        result = self.active_urls.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeSource(T3Client):
    """The real URL rules, with the two model-selection calls recorded and scripted."""

    def __init__(self, server: T3Server, threads: list[Thread]) -> None:
        super().__init__(server, "token")
        self.calls: list[tuple[str, ModelSelection]] = []
        self.reads: list[str] = []
        self.selections = {t.thread_id: selection(t) for t in threads}
        self.error: Exception | None = None

    async def get_model_selection(self, thread_id):
        self.reads.append(thread_id)
        current = self.selections[thread_id]
        await asyncio.sleep(0)  # a round trip: other presses get their turn meanwhile
        return current

    async def set_model_selection(self, thread_id, selection):
        if self.error is not None:
            raise self.error
        await asyncio.sleep(0)
        self.calls.append((thread_id, selection))
        self.selections[thread_id] = selection  # the server replaces the whole selection


@pytest.fixture
def store() -> StateStore:
    return StateStore()


@pytest.fixture
def time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def mac() -> FakeMac:
    return FakeMac()


@pytest.fixture
def source() -> FakeSource:
    return FakeSource(SERVER, [T1, T2, CODEX_THREAD])


@pytest.fixture
async def ctrl(store, source, mac, time):
    controller = T3Controller(
        store, {"pc": source}, T3_CONFIG, mac, clock=time.clock, sleep=time.sleep
    )
    store.set("t3.threads.pc", [T1, T2, CODEX_THREAD])
    yield controller
    await controller.aclose()


@pytest.fixture
def effort(ctrl) -> EffortDial:
    return EffortDial(ctrl)


@pytest.fixture
def model(ctrl) -> ModelDial:
    return ModelDial(ctrl)


def pending(store, kind="effort"):
    return store.get(f"t3.pending.{kind}")


# --- slots: merging the servers' thread lists ---------------------------------


async def test_every_servers_threads_are_merged_into_one_list(store, mac):
    other = thread("dddddddd-4", server="laptop", updated_at=T0 + timedelta(minutes=5))
    cfg = T3Config(servers=[SERVER, LAPTOP], models={}, efforts=EFFORTS)
    controller = T3Controller(store, {}, cfg, mac)
    store.set("t3.threads.pc", [T1, T2])
    assert store.get("t3.threads") == [T1, T2]
    store.set("t3.threads.laptop", [other])
    assert store.get("t3.threads") == [T1, T2, other]
    store.set("t3.threads.pc", [T2])  # a server's new list replaces its old one
    assert store.get("t3.threads") == [T2, other]
    await controller.aclose()


async def test_threads_already_in_the_store_are_merged_at_once(store, mac):
    store.set("t3.threads.pc", [T1])
    controller = T3Controller(store, {}, T3_CONFIG, mac)
    assert store.get("t3.threads") == [T1]
    await controller.aclose()


async def test_other_store_changes_do_not_touch_the_merged_list(ctrl, store):
    merged = store.get("t3.threads")
    store.set("t3.online.pc", True)
    assert store.get("t3.threads") is merged


# --- focus and new thread ---------------------------------------------------


async def test_focus_navigates_existing_tab(ctrl, mac):
    await ctrl.focus(0)  # slots go by most recent update: T1 is newer than T2
    assert mac.requests == [("tab", {"match": HOST, "url": url_of(T1), "navigate": True})]
    await ctrl.focus(1)
    assert mac.requests[-1] == ("tab", {"match": HOST, "url": url_of(T2), "navigate": True})


async def test_focus_url_has_environment_and_thread_id(ctrl, mac):
    await ctrl.focus(0)
    assert mac.requests[0][1]["url"] == f"https://{HOST}/{ENV_ID}/aaaaaaaa-1"


async def test_focus_empty_slot_does_nothing(ctrl, mac):
    await ctrl.focus(3)
    await ctrl.focus(5)
    assert mac.requests == []


async def test_focus_skips_threads_that_get_no_slot(ctrl, mac, store):
    store.set("t3.threads.pc", [replace(T1, settled=True), T2])
    await ctrl.focus(0)
    assert mac.requests[0][1]["url"] == url_of(T2)


async def test_focus_goes_to_the_threads_own_server(store, mac):
    other = thread("dddddddd-4", server="laptop", updated_at=T0 + timedelta(minutes=9))
    cfg = T3Config(servers=[SERVER, LAPTOP], models={}, efforts=EFFORTS)
    sources = {"pc": FakeSource(SERVER, []), "laptop": FakeSource(LAPTOP, [])}
    controller = T3Controller(store, sources, cfg, mac)
    store.set("t3.threads.pc", [T1])
    store.set("t3.threads.laptop", [other])
    await controller.focus(0)
    assert mac.requests == [
        ("tab", {"match": "t3.laptop.example", "url": url_of(other, LAPTOP), "navigate": True})
    ]
    await controller.aclose()


async def test_focus_lets_mac_offline_reach_the_action_runner(ctrl, mac):
    mac.error = MacOffline("the Mac is offline")
    with pytest.raises(MacOffline):  # the daemon flashes the key
        await ctrl.focus(0)


async def test_new_thread_opens_the_first_servers_web_url(ctrl, mac):
    await ctrl.new_thread()
    assert mac.requests == [("tab", {"match": HOST, "url": f"https://{HOST}", "navigate": True})]


async def test_new_thread_without_servers_does_nothing(store, mac):
    controller = T3Controller(store, {}, T3Config(servers=[], models={}), mac)
    await controller.new_thread()
    assert mac.requests == []
    await controller.aclose()


async def test_focus_and_new_without_a_mac_do_nothing(store, source, caplog):
    controller = T3Controller(store, {"pc": source}, T3_CONFIG, None)
    store.set("t3.threads.pc", [T1])
    with caplog.at_level(logging.WARNING):
        await controller.focus(0)
        await controller.new_thread()
    assert caplog.text.count("no Mac") == 2
    await controller.aclose()


# --- the thread in front ----------------------------------------------------


async def test_thread_in_front_is_looked_up_by_url(ctrl, mac):
    mac.active_urls = [url_of(T2)]
    assert await ctrl.thread_in_front() == T2


async def test_thread_in_front_ignores_a_query_and_fragment(ctrl, mac):
    mac.active_urls = [url_of(T1) + "?x=1#top"]
    assert await ctrl.thread_in_front() == T1


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "https://mail.google.com/",
        f"https://{HOST}/",  # T3's home, no thread open
        f"https://{HOST}/{ENV_ID}/does-not-exist",  # not a thread the deck knows
        f"http://{HOST}/{ENV_ID}/aaaaaaaa-1",  # the wrong scheme
    ],
)
async def test_thread_in_front_is_none_for_anything_else(ctrl, mac, url):
    mac.active_urls = [url]
    assert await ctrl.thread_in_front() is None


async def test_foreign_environment_id_is_not_a_thread(ctrl, mac):
    mac.active_urls = [f"https://{HOST}/ffffffff-0000-0000-0000-000000000000/aaaaaaaa-1"]
    assert await ctrl.thread_in_front() is None


async def test_thread_in_front_finds_the_thread_on_the_matching_server(store, mac):
    same_id = thread("aaaaaaaa-1", server="laptop", title="Elsewhere")
    cfg = T3Config(servers=[SERVER, LAPTOP], models={}, efforts=EFFORTS)
    sources = {"pc": FakeSource(SERVER, []), "laptop": FakeSource(LAPTOP, [])}
    controller = T3Controller(store, sources, cfg, mac)
    store.set("t3.threads.pc", [T1])
    store.set("t3.threads.laptop", [same_id])
    mac.active_urls = [url_of(same_id, LAPTOP), url_of(T1)]
    assert await controller.thread_in_front() == same_id
    assert await controller.thread_in_front() == T1
    await controller.aclose()


async def test_thread_in_front_lets_mac_errors_through(ctrl, mac):
    mac.active_urls = [MacOffline("the Mac is offline")]
    with pytest.raises(MacOffline):
        await ctrl.thread_in_front()


# --- effort dial ------------------------------------------------------------


async def test_effort_applies_to_thread_in_front(ctrl, mac, source, effort):
    mac.active_urls = [url_of(T1), url_of(T1)]  # turn read, press read
    await effort.on_turn(-1)
    await effort.on_press()
    assert source.calls == [(T1.thread_id, selection(T1, effort="high"))]


async def test_effort_keeps_model_and_other_options(ctrl, mac, source, store, effort):
    source.selections[T1.thread_id] = ModelSelection(
        "claudeAgent", "claude-sonnet-5-5", (("fastMode", False), ("effort", "xhigh"), ("x", 1))
    )
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(+1)
    await effort.on_press()
    assert source.calls == [
        (
            T1.thread_id,
            ModelSelection(
                "claudeAgent",
                "claude-sonnet-5-5",
                (("fastMode", False), ("effort", "max"), ("x", 1)),
            ),
        )
    ]


async def test_press_applies_the_current_selection_not_the_cached_one(ctrl, mac, source, effort):
    # the thread list is a moment old: the server's own options are what a set must keep
    source.selections[T1.thread_id] = ModelSelection(
        "claudeAgent", "claude-haiku-5-5", (("effort", "low"), ("fastMode", True))
    )
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(+1)
    await effort.on_press()
    assert source.reads == [T1.thread_id]
    assert source.calls == [
        (
            T1.thread_id,
            ModelSelection(
                "claudeAgent", "claude-haiku-5-5", (("effort", "max"), ("fastMode", True))
            ),
        )
    ]


async def test_pending_shows_title_current_and_pending(ctrl, mac, store, effort):
    mac.active_urls = [url_of(T1)]
    await effort.on_turn(-1)
    assert pending(store) == Pending("First", "xhigh", "high")
    await effort.on_turn(-1)  # later turns step from the pending value
    assert pending(store) == Pending("First", "xhigh", "medium")
    await effort.on_turn(+1)
    assert pending(store) == Pending("First", "xhigh", "high")
    assert mac.reads == 1


async def test_effort_wraps_around(ctrl, mac, store, effort):
    mac.active_urls = [url_of(T1)]
    await effort.on_turn(+1)  # xhigh → max
    await effort.on_turn(+1)
    assert pending(store) == Pending("First", "xhigh", "low")
    await effort.on_turn(-1)
    assert pending(store) == Pending("First", "xhigh", "max")


@pytest.mark.parametrize("ticks,expected", [(+1, "low"), (-1, "max"), (+3, "high")])
async def test_effort_unknown_current_value_is_entered_from_the_end_turned_away_from(
    ctrl, mac, store, effort, ticks, expected
):
    odd = replace(T1, options=(("effort", "ultra"),))
    store.set("t3.threads.pc", [odd])
    mac.active_urls = [url_of(odd)]
    await effort.on_turn(ticks)
    assert pending(store) == Pending("First", "ultra", expected)


async def test_effort_not_offered_by_the_provider_shows_n_a(ctrl, mac, source, store, effort):
    no_effort = replace(CODEX_THREAD, options=())
    store.set("t3.threads.pc", [no_effort])
    mac.active_urls = [url_of(no_effort), url_of(no_effort)]
    await effort.on_turn(+1)
    assert pending(store) == "effort n/a for codex"
    await effort.on_press()  # nothing was captured
    assert source.calls == [] and mac.reads == 1


async def test_effort_dial_reads_and_sets_codex_reasoning_effort(ctrl, mac, source, store, effort):
    source.selections[CODEX_THREAD.thread_id] = ModelSelection(
        "codex", "gpt-5", (("reasoningEffort", "high"), ("x", 1))
    )
    mac.active_urls = [url_of(CODEX_THREAD), url_of(CODEX_THREAD)]
    await effort.on_turn(+1)
    assert pending(store) == Pending("Codex one", "high", "xhigh")
    await effort.on_press()
    assert source.calls == [
        (
            CODEX_THREAD.thread_id,
            ModelSelection("codex", "gpt-5", (("reasoningEffort", "xhigh"), ("x", 1))),
        )
    ]


async def test_plain_effort_option_is_not_the_codex_one(ctrl, mac, source, store, effort):
    # codex threads carry `reasoningEffort`; an `effort` option there is another setting
    odd = replace(CODEX_THREAD, options=(("effort", "high"),))
    store.set("t3.threads.pc", [odd])
    mac.active_urls = [url_of(odd)]
    await effort.on_turn(+1)
    assert pending(store) == "effort n/a for codex"


async def test_claude_thread_with_only_reasoning_effort_shows_n_a(ctrl, mac, store, effort):
    odd = replace(T1, options=(("reasoningEffort", "high"),))
    store.set("t3.threads.pc", [odd])
    mac.active_urls = [url_of(odd)]
    await effort.on_turn(+1)
    assert pending(store) == "effort n/a for claudeAgent"


async def test_effort_option_id_follows_the_configured_instance_map(store, source, mac, time):
    cfg = T3Config(
        servers=[SERVER],
        models={},
        efforts=EFFORTS,
        effort_options={"claudeAgent": "thinking"},
    )
    custom = replace(T1, options=(("thinking", "low"), ("fastMode", False)))
    source.selections[T1.thread_id] = selection(custom)
    store.set("t3.threads.pc", [custom])
    controller = T3Controller(store, {"pc": source}, cfg, mac, clock=time.clock, sleep=time.sleep)
    try:
        dial = EffortDial(controller)
        mac.active_urls = [url_of(custom), url_of(custom)]
        await dial.on_turn(+1)
        assert pending(store) == Pending("First", "low", "medium")
        await dial.on_press()
        assert source.calls == [(T1.thread_id, selection(custom, thinking="medium"))]
    finally:
        await controller.aclose()


async def test_effort_n_a_when_the_server_selection_lost_its_effort_option(
    ctrl, mac, source, store, effort
):
    source.selections[T1.thread_id] = ModelSelection("claudeAgent", "claude-opus-5-5", ())
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(+1)
    await effort.on_press()
    assert source.calls == []
    assert pending(store) == "effort n/a for claudeAgent"


# --- model dial -------------------------------------------------------------


async def test_model_applies_and_keeps_instance_and_options(ctrl, mac, source, model):
    mac.active_urls = [url_of(T1), url_of(T1)]
    await model.on_turn(+1)
    await model.on_press()
    assert source.calls == [(T1.thread_id, selection(T1, model="claude-sonnet-5-5"))]
    assert source.calls[0][1].options == T1.options


async def test_model_dial_only_offers_instance_models(ctrl, mac, source, store, model):
    mac.active_urls = [url_of(CODEX_THREAD), url_of(CODEX_THREAD)]
    await model.on_turn(+1)
    assert pending(store, "model") == Pending("Codex one", "gpt-5", "gpt-5-mini")
    await model.on_turn(+1)  # wraps inside the codex list; no claude model ever shows up
    assert pending(store, "model") == Pending("Codex one", "gpt-5", "gpt-5")
    await model.on_turn(-1)
    await model.on_press()
    assert source.calls == [(CODEX_THREAD.thread_id, selection(CODEX_THREAD, model="gpt-5-mini"))]


async def test_model_dial_without_a_list_for_the_instance_does_nothing(
    ctrl, mac, source, store, model
):
    odd = replace(T1, instance_id="mystery")
    store.set("t3.threads.pc", [odd])
    mac.active_urls = [url_of(odd)]
    await model.on_turn(+1)
    await model.on_press()
    assert pending(store, "model") == "no models for mystery"
    assert source.calls == []


async def test_model_press_refuses_when_the_provider_instance_changed(
    ctrl, mac, source, store, model
):
    # the model chosen came from claudeAgent's list; the thread now runs on another instance
    source.selections[T1.thread_id] = ModelSelection("codex", "gpt-5", ())
    mac.active_urls = [url_of(T1), url_of(T1)]
    await model.on_turn(+1)
    await model.on_press()
    assert source.calls == []
    assert pending(store, "model") == CHANGED


async def test_effort_and_model_dials_are_independent(ctrl, mac, store, effort, model):
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(-1)
    await model.on_turn(+1)
    assert pending(store, "effort") == Pending("First", "xhigh", "high")
    assert pending(store, "model") == Pending("First", "claude-opus-5-5", "claude-sonnet-5-5")


# --- only ever the thread in front ------------------------------------------


async def test_no_thread_in_front_does_nothing(ctrl, mac, source, store, effort):
    mac.active_urls = ["https://mail.google.com/"]
    await effort.on_turn(+1)
    await effort.on_press()
    assert source.calls == [] and pending(store) == NO_THREAD
    assert mac.reads == 1  # the press had nothing to apply, so it did not even look


async def test_thread_changed_between_turn_and_press(ctrl, mac, source, store, effort):
    mac.active_urls = [url_of(T1), url_of(T2)]
    await effort.on_turn(+1)
    await effort.on_press()
    assert source.calls == [] and source.reads == []
    assert pending(store) == CHANGED


async def test_press_after_leaving_t3_does_nothing(ctrl, mac, source, store, effort):
    mac.active_urls = [url_of(T1), "https://mail.google.com/"]
    await effort.on_turn(+1)
    await effort.on_press()
    assert source.calls == [] and pending(store) == CHANGED


async def test_press_with_unreadable_tab_does_nothing(ctrl, mac, source, store, effort):
    mac.active_urls = [url_of(T1), None]
    await effort.on_turn(+1)
    await effort.on_press()
    assert source.calls == [] and pending(store) == CHANGED


async def test_mac_offline_on_press(ctrl, mac, source, store, effort):
    mac.active_urls = [url_of(T1), MacOffline("the Mac is offline")]
    await effort.on_turn(+1)
    await effort.on_press()
    assert source.calls == [] and pending(store) == OFFLINE


async def test_mac_offline_on_turn(ctrl, mac, source, store, effort):
    mac.active_urls = [MacOffline("the Mac is offline")]
    await effort.on_turn(+1)
    await effort.on_press()
    assert source.calls == [] and pending(store) == "Mac offline"


async def test_mac_error_on_press_shows_the_message(ctrl, mac, source, store, effort):
    mac.active_urls = [url_of(T1), MacError("active_url timed out after 15s")]
    await effort.on_turn(+1)
    await effort.on_press()
    assert source.calls == []
    assert pending(store) == "error: active_url timed out after 15s"


async def test_dials_without_a_mac_do_nothing(store, source, time):
    controller = T3Controller(
        store, {"pc": source}, T3_CONFIG, None, clock=time.clock, sleep=time.sleep
    )
    store.set("t3.threads.pc", [T1])
    dial = EffortDial(controller)
    await dial.on_turn(+1)
    await dial.on_press()
    assert pending(store) == "no Mac configured"
    assert source.calls == []
    await controller.aclose()


async def test_press_without_a_turn_does_nothing(ctrl, mac, source, store, effort):
    mac.active_urls = [url_of(T1)]
    await effort.on_press()
    assert source.calls == [] and mac.requests == [] and pending(store) is None


async def test_apply_error_shows_message(ctrl, mac, source, store, effort, time):
    source.error = T3Error("No orchestration projection exists for thread x")
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(+1)
    await effort.on_press()
    assert pending(store) == "error: No orchestration projection exists for thread x"
    await time.advance(2.1)
    assert pending(store) is None


async def test_unexpected_failure_is_shown_and_logged_not_raised(
    ctrl, mac, source, store, effort, caplog
):
    source.error = RuntimeError("boom")
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(+1)
    with caplog.at_level(logging.ERROR):
        await effort.on_press()
    assert pending(store) == "error: boom"
    assert "boom" in caplog.text


async def test_successful_apply_clears_the_pending_and_the_next_turn_reads_again(
    ctrl, mac, source, store, effort
):
    mac.active_urls = [url_of(T1), url_of(T1), url_of(T2)]
    await effort.on_turn(-1)
    await effort.on_press()
    assert pending(store) is None
    await effort.on_press()  # nothing is pending any more
    assert len(source.calls) == 1
    await effort.on_turn(-1)
    assert mac.reads == 3 and pending(store) == Pending("Second", "xhigh", "high")


async def test_press_that_changes_nothing_sets_nothing(ctrl, mac, source, store, effort):
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(+1)
    await effort.on_turn(-1)  # back on the thread's own value
    await effort.on_press()
    assert source.calls == []  # a set would bump `updatedAt` and reorder the slots
    assert mac.reads == 2 and source.reads == [T1.thread_id]
    assert pending(store) is None
    await effort.on_press()  # nothing is pending any more
    assert source.calls == []


async def test_press_whose_value_the_thread_already_has_sets_nothing(
    ctrl, mac, source, store, model
):
    # another client moved the thread to the pending model meanwhile
    source.selections[T1.thread_id] = selection(T1, model="claude-sonnet-5-5")
    mac.active_urls = [url_of(T1), url_of(T1)]
    await model.on_turn(+1)
    assert pending(store, "model") == Pending("First", "claude-opus-5-5", "claude-sonnet-5-5")
    await model.on_press()
    assert source.calls == []
    assert pending(store, "model") is None


async def test_a_second_press_while_applying_is_ignored(ctrl, mac, source, store, effort):
    gate = asyncio.Event()
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(+1)
    mac.gate = gate
    first = asyncio.create_task(effort.on_press())
    await settle()
    await effort.on_press()  # the first press is still reading the front tab
    assert mac.reads == 2
    gate.set()
    await first
    assert len(source.calls) == 1


async def test_concurrent_effort_and_model_presses_both_land(
    ctrl, mac, source, store, effort, model
):
    mac.active_urls = [url_of(T1)] * 4
    await effort.on_turn(+1)
    await model.on_turn(+1)
    await asyncio.gather(effort.on_press(), model.on_press())
    # each press reads and sets the whole selection: without taking turns, the second would
    # set the options it read before the first landed, and undo it
    assert source.selections[T1.thread_id] == ModelSelection(
        "claudeAgent", "claude-sonnet-5-5", (("effort", "max"), ("fastMode", False))
    )
    assert len(source.calls) == 2
    assert pending(store, "effort") is None and pending(store, "model") is None


# --- timers -----------------------------------------------------------------


async def test_messages_clear_after_two_seconds(ctrl, mac, store, effort, time):
    mac.active_urls = ["https://mail.google.com/"]
    await effort.on_turn(+1)
    assert pending(store) == NO_THREAD
    await time.advance(1.9)
    assert pending(store) == NO_THREAD
    await time.advance(0.2)
    assert pending(store) is None


async def test_thread_changed_message_clears_after_two_seconds(ctrl, mac, store, effort, time):
    mac.active_urls = [url_of(T1), url_of(T2)]
    await effort.on_turn(+1)
    await effort.on_press()
    assert pending(store) == CHANGED
    await time.advance(2.1)
    assert pending(store) is None


async def test_pending_clears_after_idle(ctrl, mac, source, store, effort, time):
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(-1)
    await time.advance(9.9)
    assert pending(store) == Pending("First", "xhigh", "high")
    await time.advance(0.2)
    assert pending(store) is None
    await effort.on_press()  # the capture is gone with the text: nothing to apply
    assert source.calls == []


async def test_each_turn_restarts_the_idle_time(ctrl, mac, store, effort, time):
    mac.active_urls = [url_of(T1)]
    await effort.on_turn(-1)
    for _ in range(3):
        await time.advance(8)
        await effort.on_turn(-1)
    assert pending(store) == Pending("First", "xhigh", "max")  # four steps down from xhigh, wrapped
    assert mac.reads == 1


async def test_first_turn_after_idle_reads_the_tab_again(ctrl, mac, store, effort, time):
    mac.active_urls = [url_of(T1), url_of(T2)]
    await effort.on_turn(-1)
    await time.advance(10.5)
    await effort.on_turn(-1)
    assert mac.reads == 2
    assert pending(store) == Pending("Second", "xhigh", "high")


async def test_stale_capture_is_read_again_even_if_its_timer_did_not_fire(
    ctrl, mac, store, effort, time
):
    mac.active_urls = [url_of(T1), url_of(T2)]
    await effort.on_turn(-1)
    time.now += 11  # the clock moved on, the timer has not run yet
    await effort.on_turn(-1)
    assert mac.reads == 2
    assert pending(store) == Pending("Second", "xhigh", "high")


async def test_turning_does_nothing_while_a_message_is_shown(ctrl, mac, store, effort, time):
    mac.active_urls = ["https://mail.google.com/", url_of(T1)]
    await effort.on_turn(+1)
    assert pending(store) == NO_THREAD
    await time.advance(1)
    await effort.on_turn(+1)  # inside the two seconds: no read, the message stays as it was
    await effort.on_turn(-1)
    assert mac.reads == 1
    assert pending(store) == NO_THREAD
    await time.advance(1.1)  # the first turn's two seconds, not extended by the others
    assert pending(store) is None
    await effort.on_turn(+1)  # the message is gone: this one reads the tab again
    assert mac.reads == 2
    assert pending(store) == Pending("First", "xhigh", "max")


async def test_turning_does_nothing_while_a_press_message_is_shown(
    ctrl, mac, source, store, effort, time
):
    mac.active_urls = [url_of(T1), url_of(T2), url_of(T1)]
    await effort.on_turn(+1)
    await effort.on_press()
    assert pending(store) == CHANGED
    await effort.on_turn(+1)
    assert mac.reads == 2 and pending(store) == CHANGED
    await time.advance(2.1)
    await effort.on_turn(+1)
    assert mac.reads == 3
    assert pending(store) == Pending("First", "xhigh", "max")


async def test_a_message_timer_does_not_wipe_a_later_pending(ctrl, mac, store, effort, time):
    mac.active_urls = ["https://mail.google.com/", url_of(T1)]
    await effort.on_turn(+1)
    await time.advance(2.1)
    await effort.on_turn(+1)
    await time.advance(1)
    assert pending(store) == Pending("First", "xhigh", "max")


async def test_press_does_not_expire_while_it_is_applying(ctrl, mac, source, store, effort, time):
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(+1)
    mac.gate = asyncio.Event()
    press = asyncio.create_task(effort.on_press())
    await settle()
    await time.advance(11)  # the apply is slow; the idle timer must not drop its capture
    assert pending(store) == Pending("First", "xhigh", "max")
    mac.gate.set()
    await press
    assert len(source.calls) == 1


# --- turning must not stall the daemon --------------------------------------


async def test_turn_returns_while_the_tab_is_being_read(ctrl, mac, store, effort):
    mac.gate = asyncio.Event()
    mac.active_urls = [url_of(T1)]
    await asyncio.wait_for(effort.on_turn(+1), 1)  # the daemon awaits it inline
    assert pending(store) == "reading…"


async def test_turns_during_the_read_are_not_lost(ctrl, mac, store, effort):
    mac.gate = asyncio.Event()
    mac.active_urls = [url_of(T1)]
    await effort.on_turn(-1)
    await effort.on_turn(-1)
    await effort.on_turn(+1)
    await effort.on_turn(-1)
    assert pending(store) == "reading…"
    mac.gate.set()
    await settle()
    assert pending(store) == Pending("First", "xhigh", "medium")  # two net steps down from xhigh
    assert mac.reads == 1


async def test_press_while_reading_is_ignored(ctrl, mac, source, store, effort):
    mac.gate = asyncio.Event()
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(-1)
    await effort.on_press()  # nothing has been shown yet: nothing to apply
    mac.gate.set()
    await settle()
    assert source.calls == []
    assert pending(store) == Pending("First", "xhigh", "high")
    assert mac.reads == 1


async def test_turn_while_applying_is_ignored(ctrl, mac, source, store, effort):
    mac.active_urls = [url_of(T1), url_of(T1)]
    await effort.on_turn(-1)
    mac.gate = asyncio.Event()
    press = asyncio.create_task(effort.on_press())
    await settle()
    await effort.on_turn(-1)
    mac.gate.set()
    await press
    assert source.calls == [(T1.thread_id, selection(T1, effort="high"))]


# --- elapsed time -----------------------------------------------------------


async def test_running_for_counts_from_the_first_sight_of_the_status(ctrl, store, time):
    running = replace(T1, status="running")
    store.set("t3.threads.pc", [running, T2])
    await time.advance(125)
    assert ctrl.running_for(running) == 125
    store.set("t3.threads.pc", [replace(running, title="renamed"), T2])  # same status
    await time.advance(5)
    assert ctrl.running_for(running) == 130


async def test_running_for_starts_again_when_the_status_changes(ctrl, store, time):
    store.set("t3.threads.pc", [replace(T1, status="running")])
    await time.advance(60)
    waiting = replace(T1, status="waiting")
    store.set("t3.threads.pc", [waiting])
    await time.advance(3)
    assert ctrl.running_for(waiting) == 3
    running = replace(T1, status="running")
    store.set("t3.threads.pc", [running])
    assert ctrl.running_for(running) == 0


async def test_running_for_keeps_counting_across_working_statuses(ctrl, store, time):
    queued = replace(T1, status="queued")
    store.set("t3.threads.pc", [queued])
    await time.advance(40)
    starting = replace(T1, status="starting")
    store.set("t3.threads.pc", [starting])
    await time.advance(40)
    running = replace(T1, status="running")
    store.set("t3.threads.pc", [running])
    await time.advance(40)
    assert ctrl.running_for(running) == 120  # all three keys read `working`


async def test_running_for_starts_again_when_a_request_makes_it_needs_you(ctrl, store, time):
    running = replace(T1, status="running")
    store.set("t3.threads.pc", [running])
    await time.advance(60)
    asking = replace(running, pending_requests=1)  # same status, but the key turns amber
    store.set("t3.threads.pc", [asking])
    assert ctrl.running_for(asking) == 0
    store.set("t3.threads.pc", [running])
    await time.advance(5)
    assert ctrl.running_for(running) == 5


async def test_running_for_an_unlisted_thread_counts_from_now(ctrl, time):
    ghost = replace(T1, thread_id="ffffffff-9", status="running")
    assert ctrl.running_for(ghost) == 0
    await time.advance(30)
    assert ctrl.running_for(ghost) == 30


async def test_elapsed_ticker_touches_the_store_only_while_something_works(ctrl, store, time):
    ticker = asyncio.create_task(ctrl.tick_forever())
    await settle()
    changes = []
    store.subscribe(changes.append)
    await time.advance(16)  # nothing is working
    assert changes == []
    store.set("t3.threads.pc", [replace(T1, status="running")])
    changes.clear()
    await time.advance(16)
    assert changes == [{"t3.elapsed"}]
    ticker.cancel()
    await asyncio.gather(ticker, return_exceptions=True)


# --- dial factories and the daemon's services -------------------------------


def _services(controller) -> Services:
    config = Config.model_validate({"t3": T3_CONFIG.model_dump(), "pages": {"home": {}}})
    return Services(StateStore(), config, actions=None, t3=controller)


async def test_dial_factories_hand_out_the_controllers_dials(ctrl):
    services = _services(ctrl)
    effort = DIAL_FACTORIES["t3.effort"](DialSpec(kind="t3.effort"), services)
    model = DIAL_FACTORIES["t3.model"](DialSpec(kind="t3.model"), services)
    assert isinstance(effort, EffortDial) and isinstance(model, ModelDial)
    # a page switch rebuilds the dials: the pending value must outlive the handler object
    assert DIAL_FACTORIES["t3.effort"](DialSpec(kind="t3.effort"), services) is effort


async def test_dial_factory_without_t3_services_refuses():
    services = _services(None)
    with pytest.raises(RuntimeError, match="t3"):
        DIAL_FACTORIES["t3.effort"](DialSpec(kind="t3.effort"), services)


class StubClient:
    """Stands in for `T3Client` in `cli._services`."""

    instances: list["StubClient"] = []

    def __init__(self, server: T3Server, token: str) -> None:
        self.server, self.token = server, token
        self.watch_task: asyncio.Task | None = None
        StubClient.instances.append(self)

    async def watch(self, store: StateStore) -> None:
        self.watch_task = asyncio.current_task()
        await asyncio.Event().wait()

    def thread_url(self, thread: Thread) -> str:
        return url_of(thread, self.server)

    def thread_id_from_url(self, url: str) -> str | None:
        return None


class StubMacHub(FakeMac):
    def __init__(self, path, audio, store) -> None:
        super().__init__()

    async def run(self) -> None:
        await asyncio.Event().wait()


def _cli_config(**sections) -> Config:
    t3 = T3Config(servers=[SERVER, LAPTOP], models={"claudeAgent": CLAUDE}, efforts=EFFORTS)
    return Config.model_validate(
        {"pages": {"home": {}}, "t3": t3.model_dump(), "mac": {}}
        | sections
    )


@pytest.fixture
def stubs(monkeypatch):
    StubClient.instances = []
    monkeypatch.setattr(cli, "T3Client", StubClient)
    monkeypatch.setattr(cli, "MacHub", StubMacHub)
    monkeypatch.delenv("T3_TOKEN", raising=False)
    monkeypatch.delenv("T3_LAPTOP_TOKEN", raising=False)


async def test_services_start_a_watch_per_server_with_a_token(stubs, monkeypatch, caplog):
    monkeypatch.setenv("T3_TOKEN", " secret \n")
    store = StateStore()
    stack = contextlib.AsyncExitStack()
    with caplog.at_level(logging.ERROR):
        services = await cli._services(_cli_config(), store, stack)
    await settle()
    assert isinstance(services.t3, T3Controller)
    (client,) = StubClient.instances
    assert (client.server.name, client.token) == ("pc", "secret")
    assert client.watch_task is not None  # the watch is running
    assert store.get("t3.online.laptop") is False  # no token: logged, offline, not watched
    assert "T3_LAPTOP_TOKEN" in caplog.text
    await stack.aclose()
    assert client.watch_task.cancelled()


async def test_services_without_tokens_keep_running(stubs, caplog):
    store = StateStore()
    stack = contextlib.AsyncExitStack()
    with caplog.at_level(logging.ERROR):
        services = await cli._services(_cli_config(), store, stack)
    assert StubClient.instances == []
    assert (store.get("t3.online.pc"), store.get("t3.online.laptop")) == (False, False)
    assert "T3_TOKEN" in caplog.text
    mac = services.mac
    await services.t3.new_thread()  # focus and new need no token
    assert mac.requests[0][0] == "tab"
    await stack.aclose()


async def test_services_register_the_t3_actions(stubs, monkeypatch):
    monkeypatch.setenv("T3_TOKEN", "secret")
    store = StateStore()
    stack = contextlib.AsyncExitStack()
    services = await cli._services(_cli_config(), store, stack)
    store.set("t3.threads.pc", [T1])
    await services.actions.run(Action(type="t3.focus", args=0))
    await services.actions.run(Action(type="t3.new"))
    assert services.mac.requests == [
        ("tab", {"match": HOST, "url": url_of(T1), "navigate": True}),
        ("tab", {"match": HOST, "url": f"https://{HOST}", "navigate": True}),
    ]
    await stack.aclose()


async def test_services_without_a_t3_section_have_no_t3(stubs):
    stack = contextlib.AsyncExitStack()
    services = await cli._services(
        Config.model_validate({"pages": {"home": {}}}), StateStore(), stack
    )
    assert services.t3 is None
    with pytest.raises(KeyError):
        await services.actions.run(Action(type="t3.new"))
    await stack.aclose()


async def test_services_without_a_mac_section_still_build_t3(stubs, monkeypatch):
    monkeypatch.setenv("T3_TOKEN", "secret")
    stack = contextlib.AsyncExitStack()
    config = Config.model_validate(
        {"pages": {"home": {}}, "t3": {"servers": [SERVER.model_dump()], "models": {}}}
    )
    services = await cli._services(config, StateStore(), stack)
    assert services.mac is None and services.t3.mac is None
    await services.actions.run(Action(type="t3.new"))  # logged, not raised
    await stack.aclose()
