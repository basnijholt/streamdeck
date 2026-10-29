import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from deck.cli import _load_state, main
from deck.config import Config, KeySpec, load_config
from deck.render import KeyView, SegmentView
from deck.state import StateStore
from deck.t3.model import Pending, Thread
from deck.views import key_view, segment_view

EXAMPLE = Path(__file__).parent.parent / "examples" / "config.yaml"

WORKING = ("#0f2a1a", "#22c55e")
NEEDS_YOU = ("#3a2a06", "#f59e0b")
DONE = ("#1a1a1f", "#3f3f46")
ERROR = ("#3a0f0f", "#ef4444")
ON = ("#3a3208", "#facc15")
DEFAULT = ("#1c1c21", "#2a2a31")


@pytest.fixture
def cfg() -> Config:
    return load_config(EXAMPLE)


@pytest.fixture
def store() -> StateStore:
    s = StateStore()
    s.update(
        "",
        {
            "mac.online": True,
            "ha.online": True,
            "mac.audio.output": "iLoud Micro Monitor",
            "mac.audio.input": "Q9 USB",
            "mac.audio.inputs": ["AirPods Pro", "Q9 USB", "MacBook Pro Microphone"],
            "mac.mic.muted": False,
            "t3.online.pc": True,
        },
    )
    return s


def key(**fields) -> KeySpec:
    return KeySpec.model_validate(fields)


def colors(view: KeyView) -> tuple[str | None, str | None]:
    return (view.bg, view.border)


def thread(**overrides) -> Thread:
    fields = dict(
        server="pc",
        thread_id="t1",
        project_id="p1",
        title="Fix the thing",
        status="running",
        pending_requests=0,
        model="claude-opus-5-5",
        instance_id="claudeAgent",
        options=(),
        settled=False,
        archived=False,
        parent_thread_id=None,
        updated_at=datetime(2026, 10, 28, 12, 0, tzinfo=UTC) + timedelta(minutes=1),
    )
    return Thread(**{**fields, **overrides})


# --- StateStore -------------------------------------------------------------


def test_store_get_set_snapshot():
    s = StateStore()
    assert s.get("a") is None and s.get("a", 5) == 5
    s.set("a", 1)
    assert s.get("a") == 1
    snap = s.snapshot()
    snap["a"] = 99
    assert s.get("a") == 1  # snapshot is a copy


def test_store_set_notifies_only_on_change():
    s, calls = StateStore(), []
    s.subscribe(calls.append)
    s.set("a", 1)
    s.set("a", 1)
    s.set("a", 2)
    assert calls == [{"a"}, {"a"}]


def test_store_set_distinguishes_bool_from_int():
    s, calls = StateStore(), []
    s.set("a", 1)
    s.subscribe(calls.append)
    s.set("a", True)
    assert calls == [{"a"}] and s.get("a") is True


def test_store_set_none_on_missing_key_is_a_change():
    s, calls = StateStore(), []
    s.subscribe(calls.append)
    s.set("ddc.error", None)
    assert calls == [{"ddc.error"}]
    assert "ddc.error" in s.snapshot()


def test_store_update_reports_all_changed_keys_once():
    s, calls = StateStore(), []
    s.set("mac.volume", 10)
    s.subscribe(calls.append)
    s.update("mac.", {"volume": 10, "online": True, "audio.output": "x"})
    assert calls == [{"mac.online", "mac.audio.output"}]
    s.update("mac.", {"volume": 10})
    assert len(calls) == 1


def test_store_update_replace_drops_stale_keys():
    s, calls = StateStore(), []
    s.update("mac.", {"volume": 10, "bt.connected": ["x"]})
    s.set("ha.online", True)
    s.subscribe(calls.append)
    s.update("mac.", {"volume": 10}, replace=True)
    assert s.get("mac.bt.connected") is None
    assert "mac.bt.connected" not in s.snapshot()
    assert s.get("mac.volume") == 10 and s.get("ha.online") is True
    assert calls == [{"mac.bt.connected"}]


def test_store_update_without_replace_keeps_other_keys():
    s = StateStore()
    s.update("mac.", {"volume": 10, "bt.connected": ["x"]})
    s.update("mac.", {"volume": 20})
    assert s.get("mac.bt.connected") == ["x"] and s.get("mac.volume") == 20


def test_store_subscribers_get_independent_sets():
    s, seen = StateStore(), []
    s.subscribe(lambda keys: (seen.append(set(keys)), keys.clear()))
    s.subscribe(seen.append)
    s.set("a", 1)
    assert seen == [{"a"}, {"a"}]


# --- key_view: defaults and Mac keys -----------------------------------------


def test_plain_key_uses_default_colors(cfg, store):
    view = key_view(key(icon="👻", label="Ghostty", press={"mac.app": "Ghostty"}), store, cfg, [])
    assert view == KeyView("👻", "Ghostty", None, *DEFAULT)


def test_empty_key_is_blank(cfg, store):
    view = key_view(None, store, cfg, [])
    assert (view.icon, view.label, view.sublabel, view.border) == (None, None, None, None)


def test_non_mac_key_not_dimmed_when_mac_offline(cfg, store):
    store.set("mac.online", False)
    view = key_view(key(label="Page", press={"page": "home"}), store, cfg, [])
    assert not view.dim and view.badge is None


def test_mac_key_dimmed_when_offline(cfg, store):
    store.set("mac.online", False)
    view = key_view(key(label="Ghostty", press={"mac.app": "Ghostty"}), store, cfg, [])
    assert view.dim is True and view.badge == "🚫"


def test_mac_key_dimmed_when_never_connected(cfg):
    view = key_view(key(label="Ghostty", press={"mac.app": "Ghostty"}), StateStore(), cfg, [])
    assert view.dim is True and view.badge == "🚫"


def test_output_key_lit_when_active(cfg, store):
    view = key_view(
        key(label="iLoud", press={"mac.audio.output": {"name": "iLoud"}}), store, cfg, []
    )
    assert colors(view) == ON and not view.dim


def test_output_key_not_lit_for_other_output(cfg, store):
    view = key_view(
        key(label="MacBook", press={"mac.audio.output": {"name": "MacBook"}}), store, cfg, []
    )
    assert colors(view) == DEFAULT


def test_output_key_not_lit_when_output_unknown(cfg):
    s = StateStore()
    view = key_view(key(label="iLoud", press={"mac.audio.output": {"name": "iLoud"}}), s, cfg, [])
    assert colors(view) == DEFAULT


MIC = {"label": "Mic", "press": {"mac.mic.mute": None}}


def test_mic_key_live(cfg, store):
    view = key_view(key(**MIC), store, cfg, [])
    assert view.bg == ERROR[0] and view.border == ERROR[1]
    assert view.sublabel == "LIVE Q9 USB"


def test_mic_key_muted(cfg, store):
    store.set("mac.mic.muted", True)
    view = key_view(key(**MIC), store, cfg, [])
    assert colors(view) == DONE and view.sublabel == "muted"


def test_mic_key_unknown_state_is_plain(cfg):
    view = key_view(key(**MIC), StateStore(), cfg, [])
    assert colors(view) == DEFAULT and view.sublabel is None


def test_mic_key_amber_when_not_preferred(cfg, store):
    store.set("mac.audio.input", "AirPods Pro")
    live = key_view(key(**MIC), store, cfg, [])
    assert live.border == "#f59e0b" and live.bg == ERROR[0]
    store.set("mac.mic.muted", True)
    muted = key_view(key(**MIC), store, cfg, [])
    assert muted.border == "#f59e0b" and muted.bg == DONE[0]


def test_mic_key_not_amber_when_on_preferred_input(cfg, store):
    assert key_view(key(**MIC), store, cfg, []).border == ERROR[1]


# --- key_view: Home Assistant -----------------------------------------------

LIGHT = {"icon": "💡", "label": "Guest", "state": {"ha": "light.guest_room"}}


def test_ha_key_on_and_unavailable(cfg, store):
    store.set("ha.light.guest_room", {"state": "on", "attributes": {}})
    on = key_view(key(**LIGHT), store, cfg, [])
    assert colors(on) == ON and not on.dim

    store.set("ha.light.guest_room", {"state": "off", "attributes": {}})
    off = key_view(key(**LIGHT), store, cfg, [])
    assert colors(off) == DEFAULT and not off.dim

    store.set("ha.light.guest_room", {"state": "unavailable", "attributes": {}})
    assert key_view(key(**LIGHT), store, cfg, []).dim is True


def test_ha_key_dim_when_entity_missing_or_ha_offline(cfg, store):
    assert key_view(key(**LIGHT), store, cfg, []).dim is True  # no entity yet
    store.set("ha.light.guest_room", {"state": "on", "attributes": {}})
    store.set("ha.online", False)
    assert key_view(key(**LIGHT), store, cfg, []).dim is True


# --- key_view: T3 slots -----------------------------------------------------

SLOT = {"t3.slot": 0}


@pytest.mark.parametrize(
    "overrides,palette,sublabel",
    [
        ({"status": "running"}, WORKING, "working"),
        ({"status": "queued"}, WORKING, "working"),
        ({"status": "waiting"}, NEEDS_YOU, "needs you"),
        ({"status": "running", "pending_requests": 1}, NEEDS_YOU, "needs you"),
        ({"status": "completed"}, DONE, "done"),
        ({"status": "failed"}, ERROR, "error"),
    ],
)
def test_slot_colored_by_thread_state(cfg, store, overrides, palette, sublabel):
    view = key_view(key(**SLOT), store, cfg, [thread(**overrides)])
    assert colors(view) == palette
    assert view.sublabel == sublabel and view.label == "Fix the thing" and not view.dim


@pytest.mark.parametrize(
    "seconds,sublabel",
    [
        (0, "working 0m"),
        (59.9, "working 0m"),
        (60, "working 1m"),
        (185, "working 3m"),
        (3725, "working 62m"),
    ],
)
def test_working_slot_shows_elapsed_minutes(cfg, store, seconds, sublabel):
    seen = []

    def running_for(t: Thread) -> float:
        seen.append(t)
        return seconds

    working = thread(status="running")
    view = key_view(key(**SLOT), store, cfg, [working], running_for)
    assert view.sublabel == sublabel and colors(view) == WORKING
    assert seen == [working]


@pytest.mark.parametrize(
    "overrides,sublabel",
    [
        ({"status": "waiting"}, "needs you"),
        ({"status": "completed"}, "done"),
        ({"status": "failed"}, "error"),
    ],
)
def test_elapsed_time_is_for_working_slots_only(cfg, store, overrides, sublabel):
    def running_for(t: Thread) -> float:
        raise AssertionError("asked for the elapsed time of a thread that is not working")

    view = key_view(key(**SLOT), store, cfg, [thread(**overrides)], running_for)
    assert view.sublabel == sublabel


def test_slot_label_is_the_whole_title(cfg, store):
    view = key_view(key(**SLOT), store, cfg, [thread(title="Refactor the authentication layer")])
    assert view.label == "Refactor the authentication layer"  # draw_key wraps it


def test_slot_picks_thread_by_index(cfg, store):
    threads = [thread(title="first"), thread(title="second", status="waiting")]
    view = key_view(key(**{"t3.slot": 1}), store, cfg, threads)
    assert view.label == "second" and colors(view) == NEEDS_YOU


def test_empty_slot_is_dim_dash(cfg, store):
    view = key_view(key(**{"t3.slot": 3}), store, cfg, [thread()])
    assert view.dim is True and view.label == "—" and view.sublabel is None
    assert key_view(key(**SLOT), store, cfg, []).label == "—"


def test_slot_dim_when_server_offline(cfg, store):
    store.set("t3.online.pc", False)
    view = key_view(key(**SLOT), store, cfg, [thread()])
    assert view.dim is True and colors(view) == WORKING


# --- segment_view -----------------------------------------------------------


def dial(**fields):
    return (
        Config.model_validate(
            {
                "mac": {},
                "home_assistant": {"url": "http://ha", "token_env": "T"},
                "t3": {"servers": [], "models": {}},
                "ddc": {"model": "x"},
                "audio": {"outputs": [], "inputs": []},
                "pages": {"home": {"dials": [fields]}},
            }
        )
        .pages["home"]
        .dials[0]
    )


def test_segment_empty_dial(cfg, store):
    assert segment_view(None, store, cfg) == SegmentView("", "")


def test_segment_volume(cfg, store):
    store.set("mac.volume", 42)
    view = segment_view(dial(kind="volume"), store, cfg)
    assert (view.title, view.value, view.bar) == ("🔊 iLoud Micro Monitor", "42%", 0.42)


def test_segment_volume_unknown(cfg):
    view = segment_view(dial(kind="volume"), StateStore(), cfg)
    assert view.value == "—" and view.bar is None


def test_segment_input_live_and_muted(cfg, store):
    assert segment_view(dial(kind="input"), store, cfg).title == "🔴 LIVE Q9 USB"
    store.set("mac.mic.muted", True)
    assert segment_view(dial(kind="input"), store, cfg).title == "⚫ muted"


def test_segment_brightness(cfg, store):
    store.set("ddc.brightness", 80)
    view = segment_view(dial(kind="brightness"), store, cfg)
    assert (view.title, view.value, view.bar) == ("☀️ Brightness", "80%", 0.8)


def test_segment_brightness_error(cfg, store):
    store.set("ddc.brightness", 80)
    store.set("ddc.error", "bus lost")
    view = segment_view(dial(kind="brightness"), store, cfg)
    assert view.value == "❌" and view.bar is None
    store.set("ddc.error", None)
    assert segment_view(dial(kind="brightness"), store, cfg).value == "80%"


@pytest.mark.parametrize("kind,title", [("effort", "🧠 Effort"), ("model", "🤖 Model")])
def test_segment_t3_dials_idle_and_messages(cfg, store, kind, title):
    spec = dial(kind=f"t3.{kind}")
    assert segment_view(spec, store, cfg) == SegmentView(title, "turn to read")
    store.set(f"t3.pending.{kind}", "thread changed — not applied")
    assert segment_view(spec, store, cfg) == SegmentView(title, "thread changed — not applied")
    store.set(f"t3.pending.{kind}", None)
    assert segment_view(spec, store, cfg) == SegmentView(title, "turn to read")


def test_segment_t3_effort_pending_shows_thread_value_and_origin(cfg, store):
    store.set("t3.pending.effort", Pending("Fix the thing", "xhigh", "high"))
    view = segment_view(dial(kind="t3.effort"), store, cfg)
    assert view == SegmentView("🧠 Fix the thing", "high", detail="from xhigh")


def test_segment_t3_model_pending_shows_thread_value_and_origin(cfg, store):
    store.set("t3.pending.model", Pending("Fix the thing", "claude-opus-5-5", "claude-sonnet-5-5"))
    view = segment_view(dial(kind="t3.model"), store, cfg)
    assert view == SegmentView(
        "🤖 Fix the thing", "claude-sonnet-5-5", detail="from claude-opus-5-5"
    )


def test_segment_t3_message_is_never_parsed(cfg, store):
    # a message may contain the separator a pending change used to be written with
    store.set("t3.pending.effort", "error: a · b → c")
    assert segment_view(dial(kind="t3.effort"), store, cfg) == SegmentView(
        "🧠 Effort", "error: a · b → c"
    )


def test_segment_t3_pending_without_a_title_falls_back_to_the_dial_name(cfg, store):
    store.set("t3.pending.model", Pending("", "a", "b"))
    assert segment_view(dial(kind="t3.model"), store, cfg).title == "🤖 Model"


def test_segment_t3_pending_kinds_are_separate(cfg, store):
    store.set("t3.pending.model", Pending("t", "a", "b"))
    assert segment_view(dial(kind="t3.effort"), store, cfg).value == "turn to read"


def test_segment_ha_brightness(cfg, store):
    spec = dial(kind="ha.brightness", entity_id="light.guest_room")
    store.set("ha.light.guest_room", {"state": "on", "attributes": {"brightness": 153}})
    view = segment_view(spec, store, cfg)
    assert (view.title, view.value, view.bar) == ("💡 light.guest_room", "60%", 0.6)
    store.set("ha.light.guest_room", {"state": "off", "attributes": {}})
    view = segment_view(spec, store, cfg)
    assert view.value == "off" and view.bar is None
    store.set("ha.light.guest_room", {"state": "unavailable", "attributes": {}})
    assert segment_view(spec, store, cfg).value == "—"


# --- deck render --state ----------------------------------------------------


def key_origin(sheet: Image.Image, index: int) -> tuple[int, int]:
    grid_width = 9 * 112 + 8 * 8
    return ((sheet.width - grid_width) // 2 + index * (112 + 8), 8)


def test_cli_render_state_colors_slot(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "t3.online.pc": True,
                "t3.threads": [
                    {
                        "server": "pc",
                        "title": "Fix",
                        "status": "running",
                        "updated_at": "2026-10-28T12:00:00+00:00",
                    },
                    {
                        "server": "pc",
                        "title": "Old",
                        "status": "waiting",
                        "updated_at": "2026-10-28T11:00:00",
                    },
                ],
            }
        )
    )
    out = tmp_path / "deck.png"
    assert main(["render", str(EXAMPLE), "--png", str(out), "--state", str(state)]) == 0
    with Image.open(out) as sheet:
        x, y = key_origin(sheet, 3)  # example page: key 3 is {t3.slot: 0}
        assert sheet.convert("RGB").getpixel((x + 8, y + 20)) == (0x0F, 0x2A, 0x1A)


def test_cli_render_state_pending_dial(tmp_path):
    pending = {"title": "Fix login", "current": "xhigh", "pending": "high"}
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"t3.pending.effort": pending}))
    assert _load_state(state).get("t3.pending.effort") == Pending(**pending)
    state.write_text(json.dumps({"t3.pending.effort": {"title": "Fix login"}}))
    with pytest.raises(ValueError, match="t3.pending.effort"):
        _load_state(state)


@pytest.mark.parametrize(
    "content,message",
    [
        ("not json", "state:"),
        ("[1, 2]", "state: expected a JSON object"),
        ('{"t3.threads": [{"bogus": 1}]}', "state: t3.threads"),
        ('{"t3.threads": [{"updated_at": "yesterday"}]}', "state: t3.threads"),
    ],
)
def test_cli_render_bad_state(tmp_path, capsys, content, message):
    state = tmp_path / "state.json"
    state.write_text(content)
    out = tmp_path / "deck.png"
    assert main(["render", str(EXAMPLE), "--png", str(out), "--state", str(state)]) == 1
    assert message in capsys.readouterr().out
    assert not out.exists()
