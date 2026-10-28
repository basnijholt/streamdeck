# Agentic Stream Deck Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development — 15 interface-heavy tasks across two machines; a per-task reviewer catches name/type drift before later tasks build on it.

**Goal:** A YAML-configured daemon on the NixOS PC that drives a Stream Deck + XL, executes Mac actions over one SSH channel, and shows/steers T3 Code threads, Mac audio, Home Assistant lights, and display brightness.

**Architecture:** One Python package `deck`. `deck run` (PC) owns the device, a state store, providers, and an action runner; `deck remote` (Mac, SSH forced command) executes an allowlist of Mac actions and pushes Mac state over JSON Lines. Views are pure functions of config + state; rendering is pure Pillow.

**Tech Stack:** Python 3.13, uv, `streamdeck>=0.10.0`, `pillow`, `pydantic` v2, `pyyaml`, `websockets`; dev: `pytest`, `pytest-asyncio`.

**Spec:** `docs/baspowers/specs/2026-10-28-streamdeck-design.md`

## Global Constraints

- Runtime dependencies are exactly: `streamdeck>=0.10.0`, `pillow`, `pydantic>=2`, `pyyaml`, `websockets`. HTTP via stdlib `urllib` in `asyncio.to_thread`.
- Package/import name `deck`; console script `deck`; source in `src/deck/`, tests in `tests/`.
- Device geometry: 36 keys (9 cols × 4 rows), key image 112×112; 6 dials; touch strip 1200×100 split into six 200×100 segments; strip images are JPEG.
- Long press = key held ≥ 0.5 s, fired on release.
- Mac transport: `ssh -T -o BatchMode=yes -o ServerAliveInterval=10 -i <key> <host> deck remote`; JSON Lines; request timeout 15 s; reconnect backoff 1 s doubling to 30 s; while offline `mac.*` presses are dropped, not queued.
- Mac state poll every 1 s; send only changes; full snapshot on connect.
- Bluetooth connect wait ≤ 10 s, polling every 0.5 s.
- DDC: `ddcutil --bus <N> --skip-ddc-checks --sleep-multiplier 0.3 getvcp|setvcp 10 …`; bus found once via `ddcutil detect --brief` matching `ddc.model`; brightness re-read every 30 s; presets default `[35, 80]`.
- `mac.url` and `mac.tab.url` accept only `http`/`https`. `mac.script` names match `^[A-Za-z0-9._-]+$` and resolve inside `~/.config/deck/scripts/`. No generic shell kind on the Mac.
- AppleScript arguments are passed via `on run argv`, never interpolated into script text.
- Secrets only from environment variables named in config (`token_env`); never from YAML.
- Control socket `$XDG_RUNTIME_DIR/deck.sock`, mode 0600; it never exposes Mac execution.
- State changes visible on keys within ~1 s; renders throttled to ≤ 20 Hz; only changed images are pushed to the device.
- Colors (from the approved mockup): working bg `#0f2a1a` border `#22c55e`; needs-you bg `#3a2a06` border `#f59e0b`; done bg `#1a1a1f` border `#3f3f46`; error/live bg `#3a0f0f` border `#ef4444`; on/lit bg `#3a3208` border `#facc15`; default key bg `#1c1c21` border `#2a2a31`; selected border `#ffffff`.

## Review Focus

1. **Mac link drops mid-request** — pending requests must fail at once with `MacOffline` (not hang 15 s), and the next connect's snapshot must replace stale Mac state. Test: Task 10 `test_disconnect_fails_pending_and_snapshot_replaces_state`.
2. **Fast dial spins** — brightness, Mac volume, and HA brightness must coalesce to the latest value with one request in flight, never queue one request per tick. Tests: Task 7 `test_set_brightness_coalesces`, Task 10 `test_volume_dial_coalesces`, Task 11 `test_ha_brightness_coalesces`.
3. **Config saved mid-edit** (editor writes a partial/invalid file) — the daemon keeps the previous config and shows ❌ on key 0; a later valid save recovers. Test: Task 5 `test_invalid_reload_keeps_previous_config`.
4. **You switch threads between turning and pressing a dial** — model/effort must never land on a thread you are no longer looking at; the press re-reads Brave's front tab and refuses on mismatch. Test: Task 13 `test_thread_changed_between_turn_and_press`.
5. **Quotes and AppleScript metacharacters in config strings** (tab match, macro names) — passed as argv, so `"` or `& do shell script` cannot change the script. Test: Task 9 `test_tab_argv_does_not_interpolate`.

---

### Task 1: Project scaffold and config models

**Files:**
- Create: `pyproject.toml`, `src/deck/__init__.py`, `src/deck/config.py`, `src/deck/cli.py`, `tests/test_config.py`, `tests/fixtures/valid.yaml`, `examples/config.yaml`, `.gitignore`

**Interfaces:**
- Produces (`deck.config`):
  - `class Action(BaseModel)`: `type: str`, `args: Any`. A `mode="before"` validator accepts the YAML form `{"mac.app": "Ghostty"}` (exactly one key) and validates `args` against `ACTION_ARGS[type]`.
  - `ACTION_ARGS: dict[str, Any]` with keys and arg types: `page: str`; `shell: list[str]`; `ddc: DdcArgs(code:int, value:int)`; `ha: HaArgs(service:str, entity_id:str, data:dict={})`; `t3.focus: int` (slot 0–5); `t3.new: None`; `mac.app: str`; `mac.url: str` (http/https only); `mac.tab: TabArgs(match:str, url:str, navigate:bool=False)` (url http/https only); `mac.audio.output: OutputArgs(name:str, disconnect:bool=False)`; `mac.audio.input: str`; `mac.mic.mute: None`; `mac.volume: VolumeArgs(mute: Literal["toggle"])`; `mac.screenshot: None`; `mac.km: str`; `mac.script: str` (regex `^[A-Za-z0-9._-]+$`); `mac.lock: None`.
  - `class KeySpec(BaseModel)`: `icon: str|None`, `label: str|None`, `state: HaStateRef|None` (`{ha: entity_id}`), `press: Action|None`, `long_press: Action|None`, `t3_slot: int|None` (YAML key `t3.slot`, 0–5). A key with `t3_slot` and no `press` gets `press = Action(type="t3.focus", args=t3_slot)` in a validator.
  - `class DialSpec(BaseModel)`: `kind: Literal["volume","input","brightness","t3.effort","t3.model","ha.brightness"]`, `entity_id: str|None`, `presets: list[int] = [35, 80]`.
  - `class Page(BaseModel)`: `keys: list[KeySpec|None]` (≤ 36), `dials: list[DialSpec|None]` (≤ 6).
  - `MacConfig(host:str, key:Path, command:list[str]|None=None)`; `HAConfig(url:str, token_env:str)`; `T3Server(name:str, url:str, web_url:str, environment_id:str, token_env:str)`; `T3Config(servers:list[T3Server], models:dict[str,list[str]], efforts:list[str]=["low","medium","high","xhigh","max"])`; `DDCConfig(model:str)`; `AudioOutput(name:str, match:str, bt:str|None=None)`; `AudioConfig(outputs:list[AudioOutput], inputs:list[str], pin_input:bool=False)`.
  - `class Config(BaseModel)`: `mac, home_assistant, t3, ddc, audio` (all optional), `pages: dict[str, Page]`, `start_page: str = "home"`. A `mode="after"` validator enforces: any `mac.*` action needs `mac`; `ha` actions/state/`ha.brightness` dials need `home_assistant`; `t3.*` needs `t3`; `brightness` dial needs `ddc`; `page` targets and `start_page` exist; `mac.audio.output` names exist in `audio.outputs`; `ha.brightness` dials have `entity_id`.
  - `class ConfigError(Exception)`: `errors: list[str]`, each `"<dotted.path>: <message>"`.
  - `load_config(path: Path) -> Config` — raises `ConfigError` for YAML and validation errors.
- Produces (`deck.cli`): `main(argv: list[str]|None = None) -> int` with subcommands `check <path>` (prints `ok` or one error per line, exit 1) and `schema` (prints `Config.model_json_schema()` as JSON). Later tasks add subcommands here.

- [ ] **Step 1: Write `pyproject.toml`** (hatchling build, `requires-python = ">=3.13"`, the five runtime deps, dev group `pytest`, `pytest-asyncio` with `asyncio_mode = "auto"`, script `deck = "deck.cli:main"`). Copy the spec's config sketch into `examples/config.yaml` and `tests/fixtures/valid.yaml` (fill BT addresses with `00-11-22-33-44-55`).

- [ ] **Step 2: Write the failing tests**

```python
def test_valid_fixture_loads():
    cfg = load_config(FIXTURES / "valid.yaml")
    assert cfg.pages["home"].keys[0].press == Action(type="mac.app", args="Ghostty")

def test_unknown_action_type_reports_path(tmp_path):
    p = write(tmp_path, {"pages": {"home": {"keys": [None, None, None, {"press": {"mac.nope": 1}}]}}})
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert any(err.startswith("pages.home.keys.3.press") for err in e.value.errors)

@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "ssh://x"])
def test_mac_url_rejects_non_http(tmp_path, url): ...  # ConfigError

def test_too_many_keys_rejected(tmp_path): ...          # 37 keys -> ConfigError
def test_mac_action_without_mac_section(tmp_path): ...  # ConfigError mentions "mac"
def test_page_target_must_exist(tmp_path): ...
def test_output_name_must_exist_in_audio(tmp_path): ...
def test_script_name_rejects_path(tmp_path): ...        # "../x" -> ConfigError
def test_cli_check_ok_and_error(tmp_path, capsys): ...  # main(["check", p]) == 0 / 1
```

- [ ] **Step 3: Run** `uv run pytest tests/test_config.py -v` — Expected: FAIL (`ModuleNotFoundError: deck`).
- [ ] **Step 4: Implement** `deck.config` and `deck.cli` per Interfaces. Convert pydantic `ValidationError` locations to dotted paths for `ConfigError.errors`.
- [ ] **Step 5: Run** `uv run pytest tests/test_config.py -v` — Expected: all PASS. Also `uv run deck check examples/config.yaml` prints `ok`.
- [ ] **Step 6: Commit** `git add -A && git commit -m "feat: project scaffold and validated YAML config"`

---

### Task 2: Rendering and `deck render --png`

**Files:**
- Create: `src/deck/render.py`, `tests/test_render.py`
- Modify: `src/deck/cli.py`

**Interfaces:**
- Consumes: nothing from other tasks except `KeyView`/`SegmentView` defined here.
- Produces (`deck.render`):
  - `@dataclass(frozen=True) KeyView`: `icon: str|None`, `label: str|None`, `sublabel: str|None`, `bg: str = "#1c1c21"`, `border: str|None = "#2a2a31"`, `badge: str|None = None`, `dim: bool = False`.
  - `@dataclass(frozen=True) SegmentView`: `title: str`, `value: str`, `bar: float|None = None` (0–1), `color: str = "#3b82f6"`.
  - `draw_key(view: KeyView) -> Image.Image` — 112×112 RGB; 4 px rounded border when `border`; icon centered upper half; label and sublabel below; `dim` multiplies all pixels by 0.4; badge top-right.
  - `draw_segment(view: SegmentView) -> Image.Image` — 200×100 RGB.
  - `compose_deck(keys: list[Image.Image|None], segments: list[Image.Image|None]) -> Image.Image` — preview sheet: 9×4 grid with 8 px gaps above a 1200×100 strip.
  - `find_font(kind: Literal["text","emoji"]) -> str|None` — first existing of: env `DECK_FONT_TEXT`/`DECK_FONT_EMOJI`, `fc-match -f %{file}` for `DejaVu Sans` / `Noto Color Emoji`, macOS `/System/Library/Fonts/Apple Color Emoji.ttc`. Missing emoji font → icon drawn as text with the text font.
- CLI: `deck render <config> --png <out> [--page home] [--state state.json]` — builds views from config + optional JSON state (via Task 3's `key_view`/`segment_view`; until Task 3 lands, render with empty views is acceptable within this task's test).

- [ ] **Step 1: Write the failing tests**

```python
def test_key_size_and_background():
    img = draw_key(KeyView(icon=None, label="Ghostty", sublabel=None, bg="#3a3208", border=None))
    assert img.size == (112, 112)
    assert img.getpixel((56, 2)) == (0x3a, 0x32, 0x08)

def test_border_color_at_edge():
    img = draw_key(KeyView(icon=None, label=None, sublabel=None, bg="#1c1c21", border="#22c55e"))
    assert img.getpixel((1, 56)) == (0x22, 0xc5, 0x5e)

def test_label_region_not_empty():
    img = draw_key(KeyView(icon=None, label="Guest", sublabel=None))
    region = img.crop((0, 70, 112, 100))
    assert len(set(region.getdata())) > 1

def test_dim_darkens():
    a = draw_key(KeyView(icon=None, label=None, sublabel=None, bg="#3a3208", border=None))
    b = draw_key(KeyView(icon=None, label=None, sublabel=None, bg="#3a3208", border=None, dim=True))
    assert sum(b.getpixel((56, 2))) < sum(a.getpixel((56, 2)))

def test_segment_and_compose_sizes():
    seg = draw_segment(SegmentView(title="☀️ Brightness", value="80%", bar=0.8))
    assert seg.size == (200, 100)
    sheet = compose_deck([None] * 36, [seg] + [None] * 5)
    assert sheet.width >= 1200
```

- [ ] **Step 2: Run** `uv run pytest tests/test_render.py -v` — Expected: FAIL (import error).
- [ ] **Step 3: Implement** `deck.render` per Interfaces; emoji via `ImageFont.truetype(path, 109)` with `embedded_color=True`, scaled down to 44 px.
- [ ] **Step 4: Run** `uv run pytest tests/test_render.py -v` — Expected: PASS. `uv run deck render examples/config.yaml --png /tmp/deck.png` writes a file.
- [ ] **Step 5: Commit** `git commit -am "feat: key/segment rendering and deck render --png"` (add new files first).

---

### Task 3: State store, views, thread model, audio helpers

**Files:**
- Create: `src/deck/state.py`, `src/deck/views.py`, `src/deck/t3/__init__.py`, `src/deck/t3/model.py`, `src/deck/audio.py`, `tests/test_views.py`, `tests/test_t3_model.py`
- Modify: `src/deck/cli.py` (render uses views)

**Interfaces:**
- Produces (`deck.state`): `class StateStore`: `get(key: str, default=None)`, `set(key: str, value) -> None` (no-op if equal), `update(prefix: str, values: dict, replace: bool = False)` (replace drops existing keys under `prefix`), `subscribe(cb: Callable[[set[str]], None]) -> None` (called with changed keys), `snapshot() -> dict`.
- State keys used across tasks: `mac.online: bool`, `mac.audio.output: str`, `mac.audio.input: str`, `mac.audio.outputs: list[str]`, `mac.audio.inputs: list[str]`, `mac.mic.muted: bool`, `mac.volume: int`, `mac.bt.connected: list[str]`, `ha.online: bool`, `ha.<entity_id>: {"state": str, "attributes": dict}`, `ddc.brightness: int`, `t3.threads: list[Thread]`, `t3.online.<server>: bool`, `t3.pending.<kind>: str|None`, `ddc.error: str|None`.
- Produces (`deck.t3.model`):
  - `@dataclass(frozen=True) Thread`: `server: str`, `thread_id: str`, `project_id: str`, `title: str`, `status: str`, `pending_requests: int`, `model: str`, `instance_id: str`, `options: tuple[tuple[str, Any], ...]`, `settled: bool`, `archived: bool`, `parent_thread_id: str|None`, `updated_at: datetime`.
  - `thread_state(t: Thread) -> Literal["working","needs_you","done","error"]` — spec §9 table; `waiting` or `pending_requests > 0` wins over everything.
  - `visible_threads(threads: list[Thread]) -> list[Thread]` — not settled, not archived, `parent_thread_id is None`, sorted by `updated_at` desc, at most 6.
- Produces (`deck.audio`): `matches(device: str, needle: str) -> bool` (case-insensitive substring); `preferred_input(available: list[str], prefs: list[str]) -> str|None` (first pref that matches any available device; returns the device name).
- Produces (`deck.views`):
  - `key_view(spec: KeySpec|None, store: StateStore, cfg: Config, slot_threads: list[Thread]) -> KeyView` — rules: default colors; `mac.*` press and `mac.online` falsy → `dim=True, badge="🚫"`; `mac.audio.output` press lit (on colors) when `matches(store mac.audio.output, output.match)`; `mac.mic.mute` press → muted: bg done, sublabel `muted`; live: error colors, sublabel `LIVE <input>`; border needs-you color when `mac.audio.input` ≠ `preferred_input(...)`; `state: {ha: e}` → on colors when state `on`, `dim` when missing/`unavailable` or `ha.online` falsy; `t3_slot` → thread at that index colored by `thread_state`, label = title truncated to 12 chars, sublabel `working 4m` / `needs you` / `done` / `error`; empty slot → `dim=True`, label `—`; slot whose server has `t3.online.<server>` falsy → `dim=True`.
  - `segment_view(spec: DialSpec|None, store: StateStore, cfg: Config) -> SegmentView` — volume `🔊 <output>` / `<n>%`; input `🔴 LIVE <input>` or `⚫ muted`; brightness `☀️ Brightness` / `<n>%` (value `❌` when `ddc.error` is set); `t3.effort`/`t3.model` show the `t3.pending.<kind>` text when set, else `turn to read`; `ha.brightness` `💡 <entity>` / `<pct>%` or `off`.

- [ ] **Step 1: Write the failing tests**

```python
def test_waiting_beats_running():
    assert thread_state(thread(status="running", pending_requests=1)) == "needs_you"

@pytest.mark.parametrize("status,expected", [("queued","working"),("completed","done"),("idle","done"),("rolled_back","error")])
def test_status_mapping(status, expected): ...

def test_visible_threads_filters_and_sorts():
    ts = [thread(settled=True), thread(archived=True), thread(parent_thread_id="p"), *[thread(updated_at=dt(i)) for i in range(8)]]
    out = visible_threads(ts)
    assert len(out) == 6 and out[0].updated_at == dt(7)

def test_preferred_input_skips_airpods():
    assert preferred_input(["AirPods Pro", "Q9 USB", "MacBook Pro Microphone"], ["q9", "brio", "macbook"]) == "Q9 USB"

def test_output_key_lit_when_active(): ...       # mac.audio.output "iLoud Micro" -> bg "#3a3208"
def test_mac_key_dimmed_when_offline(): ...      # mac.online False -> dim True, badge "🚫"
def test_mic_key_amber_when_not_preferred(): ... # input "AirPods Pro" -> border "#f59e0b"
def test_ha_key_on_and_unavailable(): ...
def test_selected_slot_has_white_border(): ...
def test_store_update_replace_drops_stale_keys(): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/test_views.py tests/test_t3_model.py -v` — Expected: FAIL (import error).
- [ ] **Step 3: Implement** the four modules per Interfaces; wire `deck render --state` to load JSON into a `StateStore` and use `key_view`/`segment_view`.
- [ ] **Step 4: Run** the same command — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: state store, views, thread model, audio helpers"` (add files).

---

### Task 4: Device layer (real + fake)

**Files:**
- Create: `src/deck/events.py`, `src/deck/device.py`, `tests/fake_deck.py`, `tests/test_device.py`

**Interfaces:**
- Produces (`deck.events`): frozen dataclasses `KeyDown(index:int)`, `KeyUp(index:int)`, `DialTurn(index:int, ticks:int)`, `DialDown(index:int)`, `DialUp(index:int)`, `TouchTap(x:int, y:int)`, `TouchSwipe(dx:int)`, `Connected()`, `Disconnected()`; `Event` = their union.
- Produces (`deck.device`):
  - `class Device(Protocol)`: `key_count: int`, `dial_count: int`, `set_key(index:int, image:Image) -> None`, `set_segment(index:int, image:Image) -> None`, `set_brightness(pct:int) -> None`, `events() -> AsyncIterator[Event]`.
  - `class StreamDeckDevice(Device)`: `__init__(serial: str|None = None)`; `events()` opens the first `Stream Deck + XL` (or matching serial), emits `Connected()`, forwards library callbacks (library thread → `loop.call_soon_threadsafe` into an `asyncio.Queue`), emits `Disconnected()` on transport error and reopens with backoff 1 s → 30 s. `set_key` uses `PILHelper.to_native_key_format`; `set_segment(i, img)` calls `set_touchscreen_image(jpeg, x_pos=i*200, y_pos=0, width=200, height=100)`. Calls while disconnected are ignored.
- Produces (`tests/fake_deck.py`): `class FakeDeck(Device)`: `keys: dict[int, Image]`, `segments: dict[int, Image]`, `brightness: int|None`, `async inject(*events)`, `push_count: int` (number of `set_key`/`set_segment` calls).

- [ ] **Step 1: Write the failing tests** — `FakeDeck` satisfies `Device` (`isinstance` with `runtime_checkable`); `StreamDeckDevice` callback translation unit-tested with a stub library deck object: key callback `(deck, 3, True)` → `KeyDown(3)`; dial turn callback `(deck, DialEventType.TURN, 2, -1)` → `DialTurn(2, -1)`; touch callback `(deck, TouchscreenEventType.SHORT, {"x": 450, "y": 50})` → `TouchTap(450, 50)`; drag → `TouchSwipe(dx)`.
- [ ] **Step 2: Run** `uv run pytest tests/test_device.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run** — Expected: PASS. Manual (only if the deck is attached and the udev rule is active): `uv run python -c "import asyncio; from deck.device import StreamDeckDevice; ..."` prints `Connected()` and key events.
- [ ] **Step 5: Commit** `git commit -m "feat: device layer with fake deck"` (add files).

---

### Task 5: Daemon core — dispatch, pages, rendering loop, hot reload

**Files:**
- Create: `src/deck/actions.py`, `src/deck/dials.py`, `src/deck/daemon.py`, `tests/test_daemon.py`
- Modify: `src/deck/cli.py` (`deck run <config>`)

**Interfaces:**
- Consumes: `Config`, `load_config`, `ConfigError` (Task 1); `draw_key`, `draw_segment` (Task 2); `StateStore`, `key_view`, `segment_view`, `visible_threads` (Task 3); `Device`, events (Task 4).
- Produces (`deck.actions`): `class ActionRunner`: `register(prefix: str, handler: Callable[[Action], Awaitable[None]])` (longest-prefix match on `action.type`, e.g. `"mac."`), `async run(action: Action) -> None` (raises `KeyError` for unregistered types).
- Produces (`deck.dials`): `class DialHandler(Protocol)`: `async on_turn(ticks: int) -> None`, `async on_press() -> None`. `DIAL_FACTORIES: dict[str, Callable[[DialSpec, Services], DialHandler]]` (filled by later tasks); `@dataclass Services`: `store: StateStore`, `config: Config`, `actions: ActionRunner`, plus optional `ddc`, `mac`, `ha`, `t3` set by later tasks. `class Coalescer`: `__init__(send: Callable[[T], Awaitable[None]])`, `submit(value: T) -> None` — at most one `send` in flight; while busy only the latest submitted value is kept and sent next. (Used by Tasks 7, 10, 11, 13.)
- Produces (`deck.daemon`): `class Daemon`: `__init__(config_path: Path, device: Device, services_factory: Callable[[Config, StateStore], Services], clock=time.monotonic)`; `async run()`; `async handle(event: Event)`; `current_page: str`; `reload() -> bool` (False and ❌ on key 0 when invalid); registers the `page` action itself. Behavior: render on `Connected()` and on store changes (≤ 20 Hz), push only images whose bytes changed since last push; long press per Global Constraints; action exception → key shows error colors for 1 s then re-renders; file watcher polls config mtime every 1 s and reloads after the mtime is stable for 0.3 s.

- [ ] **Step 1: Write the failing tests** (all with `FakeDeck` and a recording handler registered for `"mac."`)

```python
async def test_press_runs_action(daemon, deck, calls):
    await deck.inject(KeyDown(0), KeyUp(0))
    assert calls == [Action(type="mac.app", args="Ghostty")]

async def test_long_press_runs_long_action(daemon, deck, calls, clock): ...  # hold 0.6 s
async def test_page_switch_rerenders(daemon, deck): ...
async def test_only_changed_images_pushed(daemon, deck, store):
    before = deck.push_count
    store.set("ha.light.guest_room", {"state": "on", "attributes": {}})
    await settle()
    assert deck.push_count - before == 1

async def test_action_error_flashes_key(daemon, deck): ...  # failing handler -> key 0 bg "#3a0f0f", restored after 1 s

async def test_invalid_reload_keeps_previous_config(daemon, deck, config_path):
    config_path.write_text("pages: [")          # partial save
    assert daemon.reload() is False
    await deck.inject(KeyDown(1), KeyUp(1))      # old config still active
    ...
    config_path.write_text(VALID)
    assert daemon.reload() is True

async def test_coalescer_keeps_latest(): ...   # submit 1..50 while send blocked -> sent == [1, 50]
```

- [ ] **Step 2: Run** `uv run pytest tests/test_daemon.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces. `deck run` builds `StreamDeckDevice` and a `services_factory` that later tasks extend.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: daemon core with pages, long press, hot reload"` (add files).

---

### Task 6: Control socket and CLI commands

**Files:**
- Create: `src/deck/control.py`, `tests/test_control.py`
- Modify: `src/deck/daemon.py`, `src/deck/cli.py`

**Interfaces:**
- Consumes: `Daemon` (Task 5), `StateStore.snapshot()` (Task 3).
- Produces (`deck.control`): `async serve_control(path: Path, daemon: Daemon) -> asyncio.Server` (creates socket with mode 0600, removes a stale file first); JSON Lines commands `{"cmd":"reload"}` → `{"ok":bool,"errors":[...]}`, `{"cmd":"state"}` → `{"ok":true,"state":{...}}` (non-JSON values via `str`), `{"cmd":"press","page":str,"key":int,"long":bool}` → runs that key's action; unknown cmd → `{"ok":false,"error":"unknown command"}`. `async call(path: Path, msg: dict) -> dict`.
- CLI: `deck reload`, `deck state`, `deck press <page> <key> [--long]` talk to `$XDG_RUNTIME_DIR/deck.sock`.

- [ ] **Step 1: Write the failing tests** — socket file mode is `0o600`; `state` returns the store; `press` invokes the recorded handler; an unknown cmd returns the error; there is no command that accepts an arbitrary action (`{"cmd":"run","action":{...}}` → unknown command).
- [ ] **Step 2: Run** `uv run pytest tests/test_control.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces; `Daemon.run()` starts the control server.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: control socket with reload/state/press"` (add files).

---

### Task 7: Command runner, local actions, DDC brightness

**Files:**
- Create: `src/deck/runner.py`, `src/deck/ddc.py`, `src/deck/local.py`, `tests/fake_runner.py`, `tests/test_ddc.py`, `tests/test_local.py`
- Modify: `src/deck/cli.py` (services factory registers `shell`, `ddc`, `brightness` dial, DDC provider)

**Interfaces:**
- Produces (`deck.runner`): `@dataclass Result(rc:int, out:str, err:str)`; `class Runner`: `async run(argv: list[str], timeout: float = 10) -> Result` (asyncio subprocess, no shell; kills on timeout and returns `rc=-1`).
- Produces (`tests/fake_runner.py`): `class FakeRunner(Runner)`: `calls: list[list[str]]`, `script(prefix: list[str], *results: Result)` (results consumed in order for argv starting with prefix; default `Result(0,"","")`), optional `block: asyncio.Event` to hold calls.
- Produces (`deck.ddc`): `class Ddc`: `__init__(runner: Runner, model: str, store: StateStore)`; `async detect_bus() -> int` (parse `ddcutil detect --brief`: the `I2C bus:  /dev/i2c-N` of the block whose `Monitor:` line contains `model`); `async get_brightness() -> int` (parse `current value = <n>`); `set_brightness(value: int) -> None` (clamps 0–100, updates `ddc.brightness` immediately, sends through a `Coalescer`; on `rc != 0` re-detects the bus once and retries; a second failure sets `ddc.error`, a later success clears it); `async poll_forever(interval: float = 30)`.
- Produces (`deck.local`): handlers for `shell` (runs `args` argv) and `ddc` (`setvcp <code> <value>` on the detected bus); `BrightnessDial(DialHandler)`: turn → `set_brightness(current + 2*ticks)`; press → next preset after the one closest to current.

- [ ] **Step 1: Write the failing tests**

```python
DETECT = """Display 1
   I2C bus:          /dev/i2c-4
   DRM connector:    card1-DP-1
   Monitor:          DEL:DELL U4025QW:4350B34

Invalid display
   I2C bus:          /dev/i2c-5
   Monitor:          IDV:AOC28E850.HDR:
"""

async def test_detect_bus_matches_model(runner):
    runner.script(["ddcutil", "detect"], Result(0, DETECT, ""))
    assert await Ddc(runner, "DELL U4025QW", StateStore()).detect_bus() == 4

async def test_get_brightness_argv_and_parse(runner):
    ...
    assert runner.calls[-1] == ["ddcutil", "--bus", "4", "--skip-ddc-checks", "--sleep-multiplier", "0.3", "getvcp", "10"]
    assert value == 100   # from "VCP code 0x10 (Brightness ): current value =   100, max value =   100"

async def test_set_brightness_coalesces(runner):
    runner.block = asyncio.Event()
    for v in range(10, 60): ddc.set_brightness(v)
    runner.block.set(); await settle()
    assert [c[-1] for c in runner.calls if "setvcp" in c] == ["10", "59"]

async def test_failed_set_redetects_once(runner): ...
async def test_brightness_preset_press(store): ...   # current 78 -> 35 ; current 36 -> 80
```

- [ ] **Step 2: Run** `uv run pytest tests/test_ddc.py tests/test_local.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run** — Expected: PASS. Manual read-only check on the PC: `uv run python -c` calling `Ddc(Runner(), "DELL U4025QW", StateStore()).get_brightness()` prints the current value.
- [ ] **Step 5: Commit** `git commit -m "feat: runner, local actions, DDC brightness dial"` (add files).

---

### Task 8: `deck remote` protocol server

**Files:**
- Create: `src/deck/remote/__init__.py`, `src/deck/remote/server.py`, `tests/test_remote_server.py`
- Modify: `src/deck/cli.py` (`deck remote` → `serve_stdio()` with the Task 9 executor/poller)

**Interfaces:**
- Produces (`deck.remote.server`):
  - `ALLOWED_KINDS = frozenset({"app","url","tab","audio.output","audio.input","mic.mute","volume","screenshot","km","script","lock","active_url"})`.
  - `class RemoteError(Exception)`.
  - `class Executor(Protocol)`: `async run(kind: str, args: Any) -> Any` (return value becomes `result`); `configure(hello: dict) -> None`.
  - `class Poller(Protocol)`: `async snapshot() -> dict`.
  - `async serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, executor: Executor, poller: Poller, interval: float = 1.0) -> None`.
- Wire format (both directions, one JSON object per line):
  - PC → Mac first line: `{"hello": {"audio": {"outputs": [...], "inputs": [...], "pin_input": bool}}}` → `executor.configure(hello)`.
  - PC → Mac: `{"id": int, "kind": str, "args": any}`.
  - Mac → PC: `{"id": int, "ok": true, "result": any}` (`result` is the executor's return value, `null` for most kinds) | `{"id": int, "ok": false, "error": str}`; `{"state": {...}}` — full snapshot once after hello, then only changed keys each `interval`.
  - Malformed line → `{"ok": false, "error": "bad request"}`; kind not in `ALLOWED_KINDS` → `{"id": n, "ok": false, "error": "kind not allowed: <kind>"}`; `url`/`tab` with non-http(s) → `ok:false` (second check besides config). Requests run concurrently; responses may arrive out of order.

- [ ] **Step 1: Write the failing tests** (stub executor/poller, in-memory streams)

```python
async def test_snapshot_then_diffs(): ...       # poller returns {"a":1,"b":2} then {"a":1,"b":3} -> lines {"state":{"a":1,"b":2}}, {"state":{"b":3}}
async def test_request_ok_and_error(): ...      # executor raises RemoteError("boom") -> {"id":2,"ok":false,"error":"boom"}
async def test_disallowed_kind(): ...           # {"id":3,"kind":"shell","args":"rm -rf /"} -> "kind not allowed: shell", executor not called
async def test_bad_json_keeps_serving(): ...
async def test_url_scheme_rechecked(): ...      # {"kind":"url","args":"file:///etc"} -> ok false
```

- [ ] **Step 2: Run** `uv run pytest tests/test_remote_server.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: deck remote JSONL protocol with allowlist"` (add files).

---

### Task 9: Mac executor, audio flows, state poller

**Files:**
- Create: `src/deck/remote/mac.py`, `tests/test_remote_mac.py`

**Interfaces:**
- Consumes: `Runner`/`FakeRunner` (Task 7), `Executor`/`Poller`/`RemoteError` (Task 8), `preferred_input`, `matches` (Task 3).
- Produces (`deck.remote.mac`):
  - Argv builders (pure): `app_argv(name) -> ["open","-a",name]`; `url_argv(url) -> ["open",url]`; `tab_argv(match, url, navigate=False)` → `["osascript","-e",BRAVE_TAB_SCRIPT,match,url,"true"|"false"]`; `active_url_argv()` → `["osascript","-e",ACTIVE_URL_SCRIPT]`; `km_argv(macro)` → `["osascript","-e",KM_SCRIPT,macro]`; `volume_argv(n)` (`set volume output volume` via `on run argv`); `volume_mute_argv()` (toggle `output muted`); the `volume` kind takes an int (set) or `{"mute": "toggle"}`; `screenshot_argv() -> ["screencapture","-ic"]`; `lock_argv() -> ["pmset","displaysleepnow"]`.
  - `BRAVE_TAB_SCRIPT`: `on run argv` — iterate windows/tabs of `application "Brave Browser"`; first tab whose URL contains `item 1 of argv` → if `item 3 of argv` is `"true"` and the tab's URL ≠ `item 2 of argv`, set the tab's `URL` to it; set `active tab index`, set window `index` to 1, `activate`; none → make a new tab with `URL` `item 2 of argv` in the front window (new window if none) and `activate`.
  - `ACTIVE_URL_SCRIPT`: `if application "Brave Browser" is running` → `URL of active tab of front window` (empty string when no window); not running → empty string. Must never launch Brave.
  - `KM_SCRIPT`: `on run argv` / `tell application "Keyboard Maestro Engine" to do script (item 1 of argv)`.
  - `class MacExecutor(Executor)`: `__init__(runner: Runner, scripts_dir: Path = ~/.config/deck/scripts, sleep=asyncio.sleep)`. `audio.output` args `{"name","match","bt","disconnect"}`: disconnect → `blueutil --disconnect <bt>`; else if `bt` and not in `blueutil --connected --format json` → `blueutil --connect <bt>`, poll `SwitchAudioSource -a -t output -f json` every 0.5 s up to 10 s for a device matching `match` (timeout → `RemoteError("<name> did not appear")`), then `SwitchAudioSource -t output -s <device name>`. `audio.input` → `SwitchAudioSource -t input -s <name>`. `mic.mute` per spec §7: read `input volume of (get volume settings)`; number → toggle between 0 and the saved level (default 75 if none saved); `missing value` → `SwitchAudioSource -m toggle -t input` and flip an in-memory flag. `script` → resolved path must stay inside `scripts_dir`, exist, and be executable. `active_url` → runs `active_url_argv()` and returns the URL, or `None` for empty output.
  - `class MacPoller(Poller)`: `__init__(runner: Runner, executor: MacExecutor)`; `snapshot()` returns `{"audio.output", "audio.input", "audio.outputs", "audio.inputs", "mic.muted", "volume", "bt.connected"}` from `SwitchAudioSource -c -t output|input -f json`, `-a -t output|input -f json` (one JSON object per line), `osascript -e "get volume settings"` (parse `output volume:42, input volume:75, alert volume:…, output muted:false`), `blueutil --connected --format json`. When hello's `pin_input` is true and current input ≠ `preferred_input(...)`, switch input first.

- [ ] **Step 1: Write the failing tests**

```python
def test_tab_argv_does_not_interpolate():
    argv = tab_argv('x" & do shell script "touch /tmp/pwn', "https://a.b")
    assert argv[:2] == ["osascript", "-e"] and argv[2] == BRAVE_TAB_SCRIPT
    assert argv[3] == 'x" & do shell script "touch /tmp/pwn'
    assert "touch" not in argv[2]

def test_active_url_script_never_launches_brave():
    assert 'application "Brave Browser" is running' in ACTIVE_URL_SCRIPT

def test_tab_argv_navigate_flag():
    assert tab_argv("t3.lab.nijho.lt", "https://t3.lab.nijho.lt/x", navigate=True)[-1] == "true"

async def test_output_connects_bt_then_switches(runner): ...   # not connected -> blueutil --connect, device appears on 2nd poll, then -s "iLoud Micro Monitor"
async def test_output_bt_timeout(runner, fake_sleep): ...       # never appears -> RemoteError after 20 polls
async def test_mute_via_input_volume(runner): ...               # "75" -> set 0; next call restores 75
async def test_mute_falls_back_to_coreaudio(runner): ...        # "missing value" -> ["SwitchAudioSource","-m","toggle","-t","input"]
async def test_script_rejects_escape(tmp_path): ...             # symlink pointing outside scripts_dir -> RemoteError
async def test_poller_parses_snapshot(runner): ...              # fixtures -> {"audio.output":"iLoud Micro Monitor","volume":42,"mic.muted":False,...}
async def test_pin_input_switches_back(runner): ...             # input AirPods, prefs [q9] -> SwitchAudioSource -t input -s "Q9 USB"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_remote_mac.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run** — Expected: PASS. Manual on the Mac (after Task 14 installs tools): `printf '{"hello":{"audio":{"outputs":[],"inputs":["q9"],"pin_input":false}}}\n' | deck remote` prints a `{"state": ...}` line with the real output device.
- [ ] **Step 5: Commit** `git commit -m "feat: Mac executor, audio flows, state poller"` (add files).

---

### Task 10: PC-side Mac client, Mac actions, volume and input dials

**Files:**
- Create: `src/deck/mac/__init__.py`, `src/deck/mac/client.py`, `src/deck/mac/dials.py`, `tests/test_mac_client.py`
- Modify: `src/deck/cli.py` (services factory: start `MacClient`, register `"mac."` handler and `volume`/`input` dials)

**Interfaces:**
- Consumes: `MacConfig`, `AudioConfig` (Task 1); `StateStore` (Task 3); `ActionRunner`, `Coalescer`, `DialHandler` (Task 5); `deck remote` (Tasks 8–9).
- Produces (`deck.mac.client`): `class MacOffline(Exception)`; `class MacError(Exception)`; `class MacClient`: `__init__(cfg: MacConfig, audio: AudioConfig|None, store: StateStore, sleep=asyncio.sleep)`; `argv() -> list[str]` (`cfg.command` or the Global Constraints ssh argv); `async run()` (spawn, send hello, read loop, on EOF: fail pending with `MacOffline`, `mac.online=False`, backoff, respawn); `async request(kind: str, args: Any, timeout: float = 15) -> Any` (returns `result`; raises `MacOffline` immediately when offline); state messages → `store.update("mac.", values, replace=<first message after connect>)`, `mac.online=True` after the first snapshot.
- Produces (`deck.mac.client`): `async handle_mac_action(client: MacClient, audio: AudioConfig|None, action: Action)` — strips `mac.` prefix; `audio.output` args expanded with that output's `match` and `bt`.
- Produces (`deck.mac.dials`): `VolumeDial` (turn → `volume` request with `current + 2*ticks` through a `Coalescer`, store updated immediately; press → `{"mute":"toggle"}`); `InputDial` (turn → next/previous available input among `audio.inputs` prefs, sent through a `Coalescer`; press → `mic.mute`).

- [ ] **Step 1: Write the failing tests** — use `cfg.command = [sys.executable, "-m", "tests.stub_remote"]` (a small script in `tests/` speaking the protocol with scripted behavior) so no SSH is involved.

```python
async def test_request_roundtrip_and_state(client, store): ...    # state {"audio.output":"iLoud"} -> store "mac.audio.output"
async def test_offline_request_raises_immediately(client): ...
async def test_disconnect_fails_pending_and_snapshot_replaces_state(client, store):
    # stub drops the connection while a request is pending -> MacOffline raised in < 1 s
    # after reconnect the snapshot lacks "mac.bt.connected" -> key removed from store
async def test_backoff_doubles_to_30(client, fake_sleep): ...      # sleeps 1,2,4,8,16,30,30
async def test_volume_dial_coalesces(client, stub): ...            # 30 ticks while stub blocks -> 2 volume requests
async def test_output_action_expands_bt(client, stub): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/test_mac_client.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: Mac client over SSH with volume and input dials"` (add files).

---

### Task 11: Home Assistant actions, state, brightness dial

**Files:**
- Create: `src/deck/ha.py`, `tests/test_ha.py`
- Modify: `src/deck/cli.py` (services factory)

**Interfaces:**
- Consumes: `HAConfig` (Task 1), `StateStore` (Task 3), `ActionRunner`, `Coalescer`, `DialHandler` (Task 5).
- Produces (`deck.ha`): `class HomeAssistant`: `__init__(cfg: HAConfig, token: str, store: StateStore, entities: set[str], sleep=asyncio.sleep)`; `async call_service(service: str, entity_id: str, data: dict | None = None) -> None` (`POST {url}/api/services/{domain}/{name}`, JSON body `{"entity_id": ..., **data}`, header `Authorization: Bearer <token>`, timeout 5 s, non-2xx → `RuntimeError`); `async run()` (WebSocket `{url→ws}/api/websocket`: `auth` → `get_states` (store only `entities`) → `subscribe_events` `state_changed`; on drop `ha.online=False`, backoff 1 → 30 s). `entities_in(cfg: Config) -> set[str]`. `HaBrightnessDial`: turn → `light.turn_on` `{"brightness_pct": clamp(current + 5*ticks)}` via `Coalescer`; press → `light.toggle`.

- [ ] **Step 1: Write the failing tests** — local `websockets.serve` stub for HA WS and `http.server` in a thread for REST.

```python
async def test_call_service_posts(ha, http_stub):
    await ha.call_service("light.toggle", "light.guest_room")
    assert http_stub.requests == [("/api/services/light/toggle", {"entity_id": "light.guest_room"}, "Bearer T")]

async def test_states_and_events_update_store(ha, ws_stub, store): ...  # only configured entities stored
async def test_reconnect_sets_offline(ha, ws_stub, store): ...
async def test_ha_brightness_coalesces(ha, http_stub): ...              # 20 ticks while blocked -> 2 POSTs
```

- [ ] **Step 2: Run** `uv run pytest tests/test_ha.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces; `HA_TOKEN` (name from `token_env`) missing at startup → log an error, mark `ha.online=False`, keep the daemon running.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: Home Assistant actions, live state, brightness dial"` (add files).

---

### Task 12: T3 Code source (protocol spike + client)

**Files:**
- Create: `docs/t3-protocol.md`, `src/deck/t3/source.py`, `tests/fixtures/t3/`, `tests/test_t3_source.py`

**Interfaces:**
- Consumes: `Thread` (Task 3), `T3Server` (Task 1).
- Produces (`deck.t3.source`): `@dataclass(frozen=True) ModelSelection(instance_id: str, model: str, options: tuple[tuple[str, Any], ...])`; `class T3Source(Protocol)`: `async list_threads() -> list[Thread]`, `async get_model_selection(thread_id: str) -> ModelSelection`, `async set_model_selection(thread_id: str, selection: ModelSelection) -> None`, `thread_url(thread: Thread) -> str` (`f"{web_url}/{environment_id}/{thread_id}"`); `thread_id_from_url(url: str) -> str|None` (inverse: host must equal the `web_url` host and the path must be exactly `/<environment_id>/<threadId>`; anything else → `None`); `class T3Client(T3Source)`: `__init__(server: T3Server, token: str)`; `async watch(store: StateStore, interval: float = 1.0)` — refreshes `t3.threads` (merged across servers by the caller) and `t3.online.<server>`.

- [ ] **Step 1: Spike — decide the transport.** Mint a token: `t3 auth session issue --scope orchestration:read --scope orchestration:operate --label streamdeck-dev --ttl 1d --token-only`. Read `pingdotgg/t3code` `apps/server` source (WebSocket RPC envelope, `/api/auth/websocket-ticket`, `orchestration.subscribeShell`, `dispatchCommand` payload for `thread.model-selection.set`) and `/api/mcp` (JSON-RPC `tools/call` for `t3_project_list`, `t3_thread_list`, `t3_thread_configure`). Pick the first option that, with the bearer token, can (a) list threads across all projects including `pendingRequestCount`, `projectId`, `modelSelection`, and (b) set `modelSelection`. Preference: `/api/mcp` (stable tool names, plain JSON-RPC) unless it rejects bearer tokens. Record the decision and exact request/response shapes in `docs/t3-protocol.md`, and save real responses (secrets and message text removed) under `tests/fixtures/t3/`. Revoke the dev token afterward (`t3 auth session revoke`).
- [ ] **Step 2: Write the failing tests** against a local stub server replaying the fixtures

```python
async def test_list_threads_parses_fixture(stub):
    threads = await client.list_threads()
    t = next(t for t in threads if t.thread_id == FIXTURE_ID)
    assert t.status in ALL_STATUSES and t.instance_id == "claudeAgent" and ("effort", "xhigh") in t.options

async def test_set_model_selection_sends_full_options(stub):
    await client.set_model_selection(FIXTURE_ID, ModelSelection("claudeAgent", "claude-opus-5-5", (("effort", "high"), ("fastMode", False))))
    assert stub.last_request_matches("set_model_selection.json")

async def test_server_down_marks_offline(store): ...
def test_thread_url_uses_web_url(): ...  # startswith("https://t3.lab.nijho.lt")
def test_thread_id_from_url_roundtrip(): ...  # roundtrip; other host, other env id, extra path segment, query-only URL -> None
```

- [ ] **Step 3: Run** `uv run pytest tests/test_t3_source.py -v` — Expected: FAIL.
- [ ] **Step 4: Implement** `T3Client` with the transport chosen in Step 1.
- [ ] **Step 5: Run** — Expected: PASS. Manual: a script using `T3Client` against the live PC server prints this project's threads.
- [ ] **Step 6: Commit** `git commit -m "feat: T3 Code client with recorded protocol fixtures"` (add files).

---

### Task 13: T3 deck behavior — slots, focus, effort/model dials on the thread in front

**Files:**
- Create: `src/deck/t3/controller.py`, `tests/test_t3_controller.py`
- Modify: `src/deck/cli.py` (services factory: `T3Controller`, `t3.` handler, `t3.effort`/`t3.model` dials), `src/deck/views.py` (elapsed time from controller)

**Interfaces:**
- Consumes: `Thread`, `thread_state`, `visible_threads` (Task 3); `T3Source`, `ModelSelection`, `thread_id_from_url` (Task 12); `MacClient.request`, `MacOffline` (Task 10); `DialHandler` (Task 5).
- Produces (`deck.t3.controller`): `class T3Controller`: `__init__(store: StateStore, sources: dict[str, T3Source], cfg: T3Config, mac: MacClient|None, clock=time.monotonic)`.
  - `async focus(slot: int)` — `mac.request("tab", {"match": <web_url host>, "url": source.thread_url(thread), "navigate": True})` for the thread in that visible slot; empty slot → no-op.
  - `async new_thread()` — same request with `url` = the first server's `web_url`.
  - `async thread_in_front() -> Thread|None` — `await mac.request("active_url", None)`, then the first source whose `thread_id_from_url(url)` is not None, then that thread from `t3.threads` (or `None` if unknown). Raises `MacOffline` through.
  - `running_for(thread: Thread) -> float` — seconds since the deck first saw the current working status; reset on status change.
  - `class EffortDial` / `class ModelDial(DialHandler)`: on a turn with no live capture (none yet, or > 10 s since the last turn), call `thread_in_front()` and capture `(server, thread_id)`; `None` → `t3.pending.<kind> = "no T3 thread in front"`, stop. Each turn steps the pending value over `cfg.efforts` / `cfg.models[thread.instance_id]` from the thread's current value and sets `t3.pending.<kind> = "<title> · <current> → <pending>"`. Press: call `thread_in_front()` again; a different thread or `None` → `"thread changed — not applied"`; `MacOffline` → `"Mac offline — not applied"`; same thread → `set_model_selection(thread_id, ...)` replacing only `effort` or `model`, keeping `instance_id` and other options, then clear pending. Server error → `"error: <msg>"`. Messages clear after 2 s; an unapplied pending clears after 10 s idle.

- [ ] **Step 1: Write the failing tests** (fake `T3Source` recording calls; fake `mac.request` scripted per call)

```python
async def test_focus_navigates_existing_tab(ctrl, mac): ...      # ("tab", {"match": "t3.lab.nijho.lt", "url": ".../6cd1637d-.../<id>", "navigate": True})

async def test_effort_applies_to_thread_in_front(ctrl, mac, source):
    mac.active_urls = [url_of(t1), url_of(t1)]          # turn read, press read
    await effort.on_turn(-1); await effort.on_press()
    assert source.calls == [(t1.thread_id, sel(t1, effort="high"))]

async def test_no_thread_in_front_does_nothing(ctrl, mac, source, store):
    mac.active_urls = ["https://mail.google.com/"]
    await effort.on_turn(+1); await effort.on_press()
    assert source.calls == [] and store.get("t3.pending.effort") == "no T3 thread in front"

async def test_thread_changed_between_turn_and_press(ctrl, mac, source, store):
    mac.active_urls = [url_of(t1), url_of(t2)]
    await effort.on_turn(+1); await effort.on_press()
    assert source.calls == [] and store.get("t3.pending.effort") == "thread changed — not applied"

async def test_mac_offline_on_press(ctrl, mac, source, store): ...       # "Mac offline — not applied"
async def test_foreign_environment_id_is_not_a_thread(ctrl, mac): ...    # same host, other env id -> None
async def test_effort_keeps_model_and_other_options(ctrl, source): ...   # ("fastMode", False) preserved
async def test_model_dial_only_offers_instance_models(ctrl): ...         # codex instance -> codex list only
async def test_pending_clears_after_idle(ctrl, clock): ...
async def test_apply_error_shows_message(ctrl, source, store): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/test_t3_controller.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run** — Expected: PASS. Full suite: `uv run pytest -q` — Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: T3 thread slots, focus, effort and model dials"` (add files).

---

### Task 14: Machine setup (dotfiles, NixOS, nix-darwin) and README

**Files:**
- Create (dotfiles repo `~/dotfiles`): `configs/nixos/optional/streamdeck.nix`, `configs/streamdeck/config.yaml`
- Modify (dotfiles): `configs/nixos/hosts/pc/default.nix` (import), `install.conf.yaml` (link `~/.config/deck/config.yaml`), `configs/nix-darwin/homebrew.nix` (`"switchaudio-osx"` brew), nix-darwin user config (restricted `authorized_keys` entry)
- Modify (this repo): `README.md` (what it is, install with `uv tool install`, `deck check/render/run/remote`, setup steps)

**Interfaces:**
- Consumes: the `deck` CLI from Tasks 1–13; spec §10 and §12 values verbatim.

- [ ] **Step 1: Write `streamdeck.nix`**: udev rules `KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", TAG+="uaccess"` and `SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", TAG+="uaccess"` via `services.udev.extraRules`; `users.users.basnijholt.linger = true`; `systemd.user.services.deck` with `ExecStart=%h/.local/bin/deck run %h/.config/deck/config.yaml`, `Environment=LD_LIBRARY_PATH=${pkgs.hidapi}/lib`, `EnvironmentFile=` the secrets env file, `path = [ pkgs.ddcutil pkgs.openssh ]`, `Restart=always`, `wantedBy = [ "default.target" ]`. Follow the style of `configs/nixos/common/t3code.nix`.
- [ ] **Step 2: Verify the NixOS config evaluates**: `nix build ~/dotfiles/configs/nixos#nixosConfigurations.pc.config.system.build.toplevel --dry-run` — Expected: no evaluation errors.
- [ ] **Step 3: nix-darwin**: add `switchaudio-osx`; add `users.users.basnijholt.openssh.authorizedKeys.keys = [ ''command="deck remote",restrict,from="192.168.1.5" ssh-ed25519 <pubkey> deck@pc'' ]` (if the nix-darwin version lacks that option, write the entry to `~/.ssh/authorized_keys` through an activation script and say so in a comment). Verify with `darwin-rebuild build --flake ~/dotfiles/configs/nix-darwin` run on the Mac.
- [ ] **Step 4: Real config** `configs/streamdeck/config.yaml` from `examples/config.yaml` with the v1 layout of spec §6; `deck check` passes.
- [ ] **Step 5: README** in this repo.
- [ ] **Step 6: Stop for approval.** ⚠️ The following change live machines and need the user's explicit go-ahead before they run: generating `~/.config/deck/mac_ed25519`, `sudo nixos-rebuild switch` on the PC, `darwin-rebuild switch` on the Mac, `uv tool install` on both, minting the long-lived T3 token, creating the HA token, writing the secrets env file.
- [ ] **Step 7: Commit** in both repos (`feat: Stream Deck daemon setup` in dotfiles; `docs: README` here).

---

### Task 15: Hardware acceptance

**Files:** none (results recorded in the final report)

- [ ] **Step 1:** `systemctl --user status deck` — Expected: `active (running)`; journal shows `Connected()` and a Mac snapshot.
- [ ] **Step 2:** Press every v1 key and turn/press every dial once (spec §6); record ✅/❌ per control.
- [ ] **Step 3:** Change audio output from the macOS menu bar — Expected: the lit output key follows within ~1 s.
- [ ] **Step 4:** Unplug and replug the deck — Expected: re-render without a restart.
- [ ] **Step 5:** Make the Mac unreachable (disable Wi-Fi briefly) — Expected: `mac.*` keys grey with 🚫 within ~10 s; recover after reconnect; light and T3 keys keep working.
- [ ] **Step 6:** `systemctl --user restart t3code` — Expected: T3 slots grey, then recover.
- [ ] **Step 7:** Type a draft in a T3 thread, press another thread's slot, come back — record whether the draft survived the page load (slot presses reload the tab).
- [ ] **Step 8:** Turn the effort dial with Gmail in front (strip: `no T3 thread in front`), then with a T3 thread in front; switch threads before pressing — Expected: `thread changed — not applied`, no change on either thread.
- [ ] **Step 9:** Check MonitorControl after a brightness dial change; record whether its read-back option exists (spec §8 mitigation).
