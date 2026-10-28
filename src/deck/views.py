"""Pure mapping from config and state to what each key and strip segment shows."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from deck.audio import matches, preferred_input
from deck.config import Config, DialSpec, KeySpec
from deck.render import SHEET_BG, KeyView, SegmentView
from deck.state import StateStore
from deck.t3.model import Pending, Thread, thread_state

# (background, border) pairs from the approved mockup.
DEFAULT = ("#1c1c21", "#2a2a31")
WORKING = ("#0f2a1a", "#22c55e")
NEEDS_YOU = ("#3a2a06", "#f59e0b")
DONE = ("#1a1a1f", "#3f3f46")
ERROR = ("#3a0f0f", "#ef4444")  # also the live-mic look
ON = ("#3a3208", "#facc15")

_THREAD_LOOK = {
    "working": (WORKING, "working"),
    "needs_you": (NEEDS_YOU, "needs you"),
    "done": (DONE, "done"),
    "error": (ERROR, "error"),
}
_NO_VALUE = "—"


def _paint(view: KeyView, palette: tuple[str, str]) -> KeyView:
    return replace(view, bg=palette[0], border=palette[1])


# --- keys -------------------------------------------------------------------


def key_view(
    spec: KeySpec | None,
    store: StateStore,
    cfg: Config,
    slot_threads: list[Thread],
    running_for: Callable[[Thread], float] | None = None,
) -> KeyView:
    """What key `spec` shows. `running_for` gives the seconds a working thread has been working;
    without it a working slot says only `working`."""
    if spec is None:
        return KeyView(None, None, None, bg=SHEET_BG, border=None)
    view = _paint(KeyView(spec.icon, spec.label, None), DEFAULT)

    press = spec.press.type if spec.press else ""
    if press.startswith("mac.") and not store.get("mac.online"):
        view = replace(view, dim=True, badge="🚫")
    if press == "mac.audio.output":
        view = _output_key(view, spec, store, cfg)
    elif press == "mac.mic.mute":
        view = _mic_key(view, store, cfg)
    if spec.state is not None:
        view = _ha_key(view, spec.state.ha, store)
    if spec.t3_slot is not None:
        view = _slot_key(view, spec.t3_slot, store, slot_threads, running_for)
    return view


def _output_key(view: KeyView, spec: KeySpec, store: StateStore, cfg: Config) -> KeyView:
    current = store.get("mac.audio.output")
    outputs = cfg.audio.outputs if cfg.audio else []
    output = next((o for o in outputs if o.name == spec.press.args.name), None)
    if current and output and matches(current, output.match):
        return _paint(view, ON)
    return view


def _mic_key(view: KeyView, store: StateStore, cfg: Config) -> KeyView:
    current = store.get("mac.audio.input")
    muted = store.get("mac.mic.muted")
    if muted is not None:
        view = _paint(view, DONE if muted else ERROR)
        view = replace(view, sublabel="muted" if muted else f"LIVE {current or ''}".strip())
    if current and cfg.audio and cfg.audio.inputs:
        preferred = preferred_input(store.get("mac.audio.inputs", []), cfg.audio.inputs)
        if current != preferred:
            view = replace(view, border=NEEDS_YOU[1])
    return view


def _ha_key(view: KeyView, entity_id: str, store: StateStore) -> KeyView:
    entity = store.get(f"ha.{entity_id}")
    state = entity.get("state") if isinstance(entity, dict) else None
    if state == "on":
        view = _paint(view, ON)
    if state in (None, "unavailable") or not store.get("ha.online"):
        view = replace(view, dim=True)
    return view


def _slot_key(
    view: KeyView,
    slot: int,
    store: StateStore,
    threads: list[Thread],
    running_for: Callable[[Thread], float] | None,
) -> KeyView:
    if slot >= len(threads):
        return replace(view, label=_NO_VALUE, sublabel=None, dim=True)
    thread = threads[slot]
    state = thread_state(thread)
    palette, sublabel = _THREAD_LOOK[state]
    if state == "working" and running_for is not None:
        sublabel = f"working {int(running_for(thread) // 60)}m"
    return replace(
        _paint(view, palette),
        label=thread.title,
        sublabel=sublabel,
        dim=not store.get(f"t3.online.{thread.server}"),
    )


# --- strip segments ---------------------------------------------------------


def segment_view(spec: DialSpec | None, store: StateStore, cfg: Config) -> SegmentView:
    if spec is None:
        return SegmentView("", "")
    match spec.kind:
        case "volume":
            title = f"🔊 {store.get('mac.audio.output') or 'Volume'}"
            return _percent(title, store.get("mac.volume"))
        case "input":
            return _input_segment(store)
        case "brightness":
            if store.get("ddc.error"):
                return SegmentView("☀️ Brightness", "❌")
            return _percent("☀️ Brightness", store.get("ddc.brightness"))
        case "t3.effort" | "t3.model":
            return _t3_segment(spec.kind.removeprefix("t3."), store)
        case "ha.brightness":
            return _ha_brightness_segment(spec, store)
    raise AssertionError(f"unhandled dial kind {spec.kind!r}")  # pragma: no cover


def _t3_segment(kind: str, store: StateStore) -> SegmentView:
    """The effort or model dial: a pending change (the thread, and what it would go from and
    to), else a message, else the prompt to turn."""
    icon, name = ("🧠", "Effort") if kind == "effort" else ("🤖", "Model")
    shown = store.get(f"t3.pending.{kind}")
    if isinstance(shown, Pending):
        return SegmentView(
            f"{icon} {shown.title or name}", shown.pending, detail=f"from {shown.current}"
        )
    return SegmentView(f"{icon} {name}", shown or "turn to read")


def _percent(title: str, value: int | None) -> SegmentView:
    if value is None:
        return SegmentView(title, _NO_VALUE)
    return SegmentView(title, f"{value}%", bar=value / 100)


def _input_segment(store: StateStore) -> SegmentView:
    muted = store.get("mac.mic.muted")
    if muted is None:
        return SegmentView("🎙️ Input", _NO_VALUE)
    if muted:
        return SegmentView("⚫ muted", "")
    return SegmentView(f"🔴 LIVE {store.get('mac.audio.input') or ''}".strip(), "")


def _ha_brightness_segment(spec: DialSpec, store: StateStore) -> SegmentView:
    title = f"💡 {spec.entity_id}"
    entity: Any = store.get(f"ha.{spec.entity_id}")
    state = entity.get("state") if isinstance(entity, dict) else None
    if state == "off":
        return SegmentView(title, "off")
    if state != "on":
        return SegmentView(title, _NO_VALUE)
    level = entity.get("attributes", {}).get("brightness")
    if level is None:
        return SegmentView(title, "on")
    return _percent(title, round(level / 255 * 100))
