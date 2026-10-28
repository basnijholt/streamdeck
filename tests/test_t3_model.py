from datetime import UTC, datetime, timedelta

import pytest

from deck.audio import matches, preferred_input
from deck.t3.model import Thread, thread_state, visible_threads

T0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def dt(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def thread(**overrides) -> Thread:
    fields = dict(
        server="pc",
        thread_id="t1",
        project_id="p1",
        title="Fix the thing",
        status="idle",
        pending_requests=0,
        model="claude-opus-5-5",
        instance_id="claudeAgent",
        options=(("effort", "xhigh"),),
        settled=False,
        archived=False,
        parent_thread_id=None,
        updated_at=dt(0),
    )
    return Thread(**{**fields, **overrides})


# --- thread_state -----------------------------------------------------------


def test_waiting_beats_running():
    assert thread_state(thread(status="running", pending_requests=1)) == "needs_you"


@pytest.mark.parametrize(
    "status,expected",
    [
        ("preparing", "working"),
        ("queued", "working"),
        ("starting", "working"),
        ("running", "working"),
        ("waiting", "needs_you"),
        ("completed", "done"),
        ("idle", "done"),
        ("failed", "error"),
        ("interrupted", "error"),
        ("cancelled", "error"),
        ("rolled_back", "error"),
    ],
)
def test_status_mapping(status, expected):
    assert thread_state(thread(status=status)) == expected


@pytest.mark.parametrize("status", ["idle", "completed", "failed", "running"])
def test_pending_requests_win_over_every_status(status):
    assert thread_state(thread(status=status, pending_requests=2)) == "needs_you"


def test_unknown_status_is_not_alarming():
    assert thread_state(thread(status="something_new")) == "done"


# --- visible_threads --------------------------------------------------------


def test_visible_threads_filters_and_sorts():
    ts = [
        thread(settled=True),
        thread(archived=True),
        thread(parent_thread_id="p"),
        *[thread(updated_at=dt(i)) for i in range(8)],
    ]
    out = visible_threads(ts)
    assert len(out) == 6 and out[0].updated_at == dt(7)
    assert [t.updated_at for t in out] == [dt(i) for i in range(7, 1, -1)]


def test_visible_threads_empty_and_short():
    assert visible_threads([]) == []
    assert len(visible_threads([thread(thread_id="a"), thread(thread_id="b")])) == 2


def test_visible_threads_does_not_mutate_input():
    ts = [thread(updated_at=dt(1)), thread(updated_at=dt(2))]
    visible_threads(ts)
    assert [t.updated_at for t in ts] == [dt(1), dt(2)]


# --- audio helpers ----------------------------------------------------------


def test_matches_is_case_insensitive_substring():
    assert matches("iLoud Micro Monitor", "iloud")
    assert matches("AirPods Pro", "AIRPODS")
    assert not matches("MacBook Pro Speakers", "iloud")


def test_empty_needle_matches_nothing():
    assert not matches("iLoud Micro Monitor", "")


def test_preferred_input_skips_airpods():
    available = ["AirPods Pro", "Q9 USB", "MacBook Pro Microphone"]
    assert preferred_input(available, ["q9", "brio", "macbook"]) == "Q9 USB"


def test_preferred_input_follows_preference_order_not_device_order():
    available = ["MacBook Pro Microphone", "Brio 4K"]
    assert preferred_input(available, ["q9", "brio", "macbook"]) == "Brio 4K"


def test_preferred_input_none_when_nothing_matches():
    assert preferred_input(["AirPods Pro"], ["q9", "brio"]) is None
    assert preferred_input([], ["q9"]) is None
