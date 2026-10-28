# Agentic Stream Deck — design

Date: 2026-10-28
Status: design approved in chat, ready for an implementation plan

## 1. Intent

A general-purpose, YAML-configured Stream Deck daemon that turns an Elgato
**Stream Deck + XL** into a control surface for an agentic-coding desk:
launching apps, switching audio, controlling lights and display brightness,
and — the part no existing tool does — showing and steering **T3 Code
threads** (status, model, effort).

The predecessor, `basnijholt/home-assistant-streamdeck-yaml`, is Home
Assistant–centric. This project is general: Home Assistant is one action
type among many. Rendering, dial, and touch-strip ideas are ported from it;
its code is not forked.

### What the user said

- Open apps, toggle the guest-room light, jump to a browser tab, switch the
  model/effort of coding agents.
- Switch Mac audio output (external speakers ⇄ AirPods ⇄ internal), make
  sure the right input is selected, mute the microphone.
- One dial for display brightness (today done with MonitorControl on the
  Mac).
- Agent integration targets **T3 Code threads** first.
- Physical setup: the deck stays plugged into the NixOS PC. The Dell
  UltraSharp U4025QW is connected to the PC over DisplayPort and to the Mac
  over Thunderbolt; 99.9% of the time the screen shows the Mac.

### Assumptions (correct these if wrong)

- The desk Mac is `macbook-m5` (ssh alias); the Mac host is a config value,
  not hard-coded.
- Brave is the browser on the Mac.
- Home Assistant runs at `http://192.168.1.178:8123` (ssh alias `ha`) and is
  reached with a long-lived access token.
- The iLoud speakers connect to the Mac over Bluetooth.

### Success criteria

1. Every key and dial action in the v1 layout (section 6) works from the deck
   while the screen shows the Mac.
2. Keys reflect live state within ~1 s: active audio device, mic mute,
   light on/off, brightness, T3 thread status, the in-front thread's model and
   effort.
3. The daemon survives the Mac being asleep, away, or rebooting: Mac keys go
   grey and recover automatically; PC-local and network keys keep working.
4. Config is plain YAML in dotfiles; an agent can edit it, validate it
   (`deck check`), and preview it (`deck render --png`) without the hardware.

### Non-goals (v1)

- Windows support, a GUI config editor, Elgato marketplace plugins.
- Driving Claude Code / Codex running in zellij (phase 2).
- The hardware approval gate, `deck ask`, frontmost-app pages, CI keys
  (phase 2, section 11).

## 2. Facts established during design

| Fact | Evidence |
|---|---|
| Device is Stream Deck + XL, USB `0fd9:00c6`: 36 keys (9×4, 112×112 px), 6 dials, 1200×100 touch strip (JPEG) | `lsusb`; `StreamDeckPlusXL.py` |
| `python-elgato-streamdeck` supports it from **0.10.0** (2026-08-30); nixpkgs ships 0.9.8 (no support) | PyPI, `nix eval` |
| `/dev/hidraw*` is `root 0600` on the PC — needs a udev rule | `ls -la /dev/hidraw*` |
| PC reaches the Dell over DDC **while the Mac is the active input** | `ddcutil getvcp 10` → `current value = 100` |
| DDC read costs 7.3 s with bus auto-detect, **0.16 s** with `--bus 4 --skip-ddc-checks --sleep-multiplier 0.3` | timed |
| PC→Mac SSH handshake ≈ 0.2 s; the 1Password/keychain agent refuses non-interactive signing (`agent refused operation`) — the daemon needs a dedicated key | `ssh macbook-m5.local` |
| Mac already has `blueutil` (homebrew.nix); `switchaudio-osx` is missing | `configs/nix-darwin/homebrew.nix` |
| T3 Code server on the PC: `t3 serve` on `0.0.0.0:3773`, v0.1.3 | `configs/nixos/common/t3code.nix`, `server-runtime.json` |
| `t3 thread list --json --project <id\|path>` returns `threadId, title, status, model, providerInstanceId, settled, updatedAt, …`; `t3 thread show --json` adds `pendingRequestCount`, `projectId` | ran both |
| Thread statuses: `idle, preparing, queued, starting, running, waiting, completed, interrupted, failed, cancelled, rolled_back` | `t3_thread_list` schema |
| Model/effort live in `modelSelection = {instanceId, model, options:[{id:"effort",value:"xhigh"}, …]}`; the server command is `thread.model-selection.set` (event `thread.model-selection-updated`) | `t3_thread_configure` read; strings in the T3 binary |
| `t3 auth session issue --scope orchestration:read --scope orchestration:operate --label streamdeck --token-only` mints a scoped bearer token | `t3 auth session issue --help` |
| The server exposes `orchestration.subscribeShell` (push updates) and `dispatchCommand` over its WebSocket RPC | strings in the T3 binary; source is `pingdotgg/t3code`, `apps/server` |

## 3. Architecture

```
            PC (NixOS, always on)                               Mac (macbook-m5)
┌───────────────────────────────────────────────┐        ┌──────────────────────────────┐
│ deck run  (systemd user service)              │        │ deck remote (forced command) │
│                                               │  SSH   │                              │
│  device ── render ── pages/config (YAML)      │◀══════▶│  executor: open, osascript,  │
│    │                    │                     │ JSONL  │  SwitchAudioSource, blueutil,│
│  events ──▶ dispatcher ─┴─▶ actions           │        │  screencapture, scripts/     │
│                             ├─ local (ddcutil)│        │  state poller (audio, BT)    │
│  state store ◀── providers  ├─ ha (REST)      │        └──────────────────────────────┘
│                  ├─ t3 (WS) ├─ t3 (WS)        │
│                  ├─ ha (WS) └─ mac (remote) ──┼──▶
│                  ├─ mac (remote state)        │
│                  └─ ddc (poll)                │
│  control socket ◀── `deck` CLI                │
└───────────────────────────────────────────────┘
```

The daemon (the "brain") runs on the PC because the USB device is there and
the PC is always on. Rendering stays local; only small action and state
messages cross the network. Rejected alternative: logic on the Mac with the
PC as a thin HID bridge — key images and dial feedback would cross the
network and lights/agent keys would die whenever the Mac is away.

One Python package, `deck`, provides both sides: `deck run` on the PC and
`deck remote` on the Mac. Python 3.13, `uv`, asyncio throughout.

Dependencies: `streamdeck>=0.10.0` (HID protocol — the hard part, widely
used), `pillow`, `pydantic`, `pyyaml`, `websockets`. Nothing else.

## 4. Components

Each unit has one job and is testable alone.

### 4.1 `device`
Wraps `StreamDeck.DeviceManager`. Opens the first + XL (or a serial from
config), exposes `set_key(index, image)`, `set_strip(segment, image)`,
`set_brightness(pct)`, and an async event stream: `KeyDown/KeyUp(index)`,
`DialTurn(index, ticks)`, `DialDown/DialUp(index)`, `TouchTap(x, y)`,
`TouchSwipe(dx)`. Detects unplug and reopens with backoff. A `FakeDeck` with
the same interface records images and accepts injected events for tests.

### 4.2 `render`
Pure functions: `(KeySpec, State) -> PIL.Image` (112×112) and
`(DialSpec, State) -> PIL.Image` (200×100 strip segment). Icon (emoji via a
bundled emoji font, or an image path), label, sub-label, background color
from state. No I/O. `deck render --png out.png` composes the whole deck
(keys + strip) into one image for agents and for snapshot tests.

### 4.3 `config`
YAML → pydantic models; `deck check` validates and prints errors with paths;
`deck schema` emits JSON Schema. The daemon watches the file and hot-reloads;
an invalid file keeps the previous config and shows ❌ on key 0 until fixed.

### 4.4 `actions`
A press/turn resolves to an action object; each type is a small class with
`async run(ctx)`:

| Type | Runs on | Effect |
|---|---|---|
| `page` | PC | switch page |
| `shell` | PC | run argv (no shell string) |
| `ddc` | PC | `ddcutil --bus N setvcp <code> <value>` |
| `ha` | network | `POST /api/services/<domain>/<service>` with `entity_id` and data |
| `t3.focus` | PC + Mac | open a slot's thread in the T3 tab on the Mac |
| `t3.model` / `t3.effort` | PC + Mac | `thread.model-selection.set` on the thread open in Brave's front tab (section 9) |
| `mac.app` | Mac | `open -a <name>` (launch or raise) |
| `mac.url` | Mac | `open <http(s) url>` |
| `mac.tab` | Mac | Brave AppleScript: activate first tab whose URL contains `match` (with `navigate: true`, also point it at `url`), else open `url` in a new tab |
| `mac.audio.output` | Mac | connect Bluetooth if `bt` address given, wait ≤10 s for the device, `SwitchAudioSource -t output -s <name>` |
| `mac.audio.input` | Mac | `SwitchAudioSource -t input -s <name>` |
| `mac.mic.mute` | Mac | toggle input mute (section 7) |
| `mac.volume` | Mac | `set volume output volume <n>` / mute |
| `mac.screenshot` | Mac | `screencapture -ic` |
| `mac.km` | Mac | `osascript -e 'tell application "Keyboard Maestro Engine" to do script "<macro>"'` |
| `mac.script` | Mac | run `~/.config/deck/scripts/<name>` (executables managed by dotfiles on the Mac) |
| `mac.lock` | Mac | lock the screen |

### 4.5 `providers` and the state store
Providers write into one in-memory key→value store; any change re-renders
only the keys/segments that reference that key.

- **`t3`** — per configured server, keeps the thread list fresh (≤ 1 s).
  Transport is decided by a spike in the plan: the server's `/api/mcp`
  JSON-RPC tools (`t3_project_list`, `t3_thread_list`,
  `t3_thread_configure`) polled every 1 s, or the WebSocket RPC
  (`orchestration.subscribeShell`, `dispatchCommand`) — whichever accepts the
  scoped bearer token and exposes status, `pendingRequestCount`, and
  `modelSelection`. The decision and wire shapes are recorded in
  `docs/t3-protocol.md`.
- **`ha`** — WebSocket `get_states` once, then `subscribe_events
  state_changed`, filtered to entities used in the config.
- **`mac`** — state lines pushed by `deck remote` (section 5).
- **`ddc`** — reads brightness on start and every 30 s.

### 4.6 `control`
Unix socket at `$XDG_RUNTIME_DIR/deck.sock` (mode 0600) and the `deck` CLI:
`deck reload`, `deck check`, `deck render --png`, `deck state` (dump the
store), `deck press <page>/<key>` (inject a press for testing). The socket
never exposes Mac execution or arbitrary actions — only these commands.

## 5. Mac remote

- **Transport:** the daemon keeps one `ssh -T -i ~/.config/deck/mac_ed25519
  macbook-m5 deck remote` process open and speaks JSON Lines over its
  stdin/stdout. No per-press handshake. On EOF it reconnects with
  exponential backoff (1 s → 30 s); while disconnected, every `mac.*` key
  renders greyed with a 🚫 badge and presses are dropped (not queued).
- **Requests:** `{"id": 7, "kind": "audio.output", "args": {...}}` →
  `{"id": 7, "ok": true, "result": <any|null>}` or `{"id": 7, "ok": false, "error": "..."}`.
  The read-only kind `active_url` returns, as `result`, the URL of the active
  tab of Brave's front window, or `null` when Brave is not running or has no
  window. It checks `application "Brave Browser" is running` first and must
  never launch Brave.
  Timeout 15 s (Bluetooth connect is the slow path).
- **State push:** `{"state": {"audio.output": "iLoud", "audio.input": "Q9",
  "mic.muted": false, "volume": 42, "bt.connected": ["..."]}}`. The remote
  polls once per second and sends only changes; it sends a full snapshot on
  connect.
- **Allowlist:** `deck remote` only accepts the kinds in section 4.4's `mac.*`
  rows plus `active_url`, with typed arguments. `mac.url` accepts only `http`/`https`.
  `mac.script` runs only files already present in `~/.config/deck/scripts/`
  on the Mac. There is no generic shell kind.
- **Mac permissions:** `open`, `screencapture`, `pbcopy` work over SSH as is.
  AppleScript control of Brave and Keyboard Maestro Engine triggers a
  one-time Automation prompt for `sshd-keygen-wrapper`; accept it once.
  Anything that needs synthetic keystrokes goes through a Keyboard Maestro
  macro (KM already holds Accessibility permission).

## 6. v1 layout

Rows (9 keys each) and dials. All values are config, not code.

| Row | Keys |
|---|---|
| 1 Apps | Ghostty · Brave · T3 Code (`mac.tab` on `t3.lab.nijho.lt`) · Obsidian · Slack · 1Password · Spotify · Files · 📸 Screenshot → clipboard |
| 2 Agents | 6 T3 thread slots · ➕ New thread (v1: opens the T3 web UI on the Mac; phase 2: starts a thread with the clipboard as prompt) · 🎙️ Dictate (`mac.km`) · reserved (phase 2: Approve) |
| 3 Tabs | Gmail · My PRs · GH notifications · HA · MindRoom · Calendar · YouTube · (free) · 📄 Page 2 |
| 4 Home/system | 💡 Guest · 💡 Office · 🌙 Good night · 🔈 iLoud · 🎧 AirPods · 💻 MacBook · 🎤 Mic · 🖥️ Input ⇄ · 🔒 Lock |

| Dial | Turn | Press | Strip shows |
|---|---|---|---|
| 1 🔊 Volume | Mac output volume | mute output | output device + % |
| 2 🎙️ Input | cycle preferred inputs | toggle mic mute | `🔴 LIVE Q9` / `⚫ muted` |
| 3 ☀️ Brightness | DDC `0x10` on the PC | toggle presets 35 / 80 | `☀️ 80%` |
| 4 🧠 Effort | cycle effort values of the thread open in Brave's front tab | apply (re-checks it is still that thread) | `<title> · current → pending` |
| 5 🤖 Model | cycle configured models for the in-front thread's provider instance | apply (same re-check) | `<title> · current → pending` |
| 6 💡 Guest | guest light brightness | toggle | `60%` / `off` |

Page 2 holds: Living scene, Sunshine stream mode, Deck off, Dell output.

### Config sketch

```yaml
mac:
  host: macbook-m5
  key: ~/.config/deck/mac_ed25519
home_assistant:
  url: http://192.168.1.178:8123
  token_env: HA_TOKEN
t3:
  servers:
    - name: pc
      url: http://127.0.0.1:3773        # used by the daemon on the PC
      web_url: https://t3.lab.nijho.lt  # the Brave tab on the Mac (proxied to :3773)
      environment_id: 6cd1637d-2c2f-4fa6-8cd2-fa04748043e2  # ~/.t3/userdata/environment-id
      token_env: T3_TOKEN
  models:
    claudeAgent: [claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5]
  efforts: [low, medium, high, xhigh, max]
  effort_options: {codex: reasoningEffort}  # effort option id per instance; default "effort"
ddc:
  model: DELL U4025QW
audio:
  outputs:
    - {name: iLoud, match: iloud, bt: "xx-xx-xx-xx-xx-xx"}
    - {name: AirPods, match: airpods, bt: "xx-xx-xx-xx-xx-xx"}
    - {name: MacBook, match: macbook}
  inputs: [q9, brio, macbook]     # preferred order; AirPods never auto-picked
  pin_input: false
pages:
  home:
    keys:
      - {icon: "👻", label: Ghostty, press: {mac.app: Ghostty}}
      - {icon: "📬", label: Gmail, press: {mac.tab: {match: mail.google.com, url: "https://mail.google.com"}}}
      - icon: "💡"
        label: Guest
        state: {ha: light.guest_room}
        press: {ha: {service: light.toggle, entity_id: light.guest_room}}
      - {t3.slot: 0}
    dials:
      - {kind: brightness}
      - {kind: t3.effort}
```

Bluetooth addresses and entity ids are filled in during setup.

## 7. Audio details

- **Output keys** are lit when `audio.output` matches. Press: if `bt` is set
  and the device is not connected, `blueutil --connect <addr>`, poll
  `SwitchAudioSource -a -t output` until the device appears (≤10 s, key
  shows ⏳, then ❌ on timeout), then switch. Long press: `blueutil
  --disconnect <addr>`.
- **Input guard:** when AirPods connect, macOS switches input to the AirPods
  mic, which puts Bluetooth in HFP and degrades output to call quality. The
  🎤 key turns amber when `audio.input` is not the first available entry of
  `audio.inputs`; one press fixes it. With `pin_input: true`, `deck remote`
  re-applies the preferred input after any device change.
- **Mic mute:** primary path is input volume, because its state can be read
  back: save `input volume of (get volume settings)`, set it to 0, restore
  the saved level on unmute; `mic.muted` is `input volume == 0`. If the
  device reports `missing value` (no software input volume), fall back to
  `SwitchAudioSource -m toggle -t input` (CoreAudio mute) and track the muted
  flag in `deck remote`'s memory.

## 8. Brightness details

- On start, run `ddcutil detect --brief` once, pick the bus whose monitor
  matches `ddc.model`, cache it. Re-detect only when a command fails.
- Dial turns update the strip immediately; one `setvcp 10 <n>` is in flight
  at a time and always carries the latest value.
- ⚠️ MonitorControl keeps its own brightness value, so its slider drifts
  after a PC-side change. Mitigation order: (1) enable MonitorControl's DDC
  read-back setting if it exists; (2) otherwise route the dial through a
  `mac.km` macro that presses the brightness keys (coarse 1/16 steps, keeps
  MonitorControl in sync). v1 ships the PC path.

## 9. T3 Code integration

- **Auth:** one bearer token per server, minted with
  `t3 auth session issue --scope orchestration:read --scope orchestration:operate --label streamdeck --ttl 365d --token-only`,
  stored in the secrets env file.
- **Slots:** the 6 agent keys show unsettled, unarchived, top-level threads
  (`parentThreadId == null`), most recently updated first.
- **Status mapping:**

  | T3 status / field | Key |
  |---|---|
  | `preparing, queued, starting, running` | 🟢 working + elapsed time |
  | `waiting` or `pendingRequestCount > 0` | 🟡 needs you |
  | `completed, idle` | ⚪ done |
  | `failed, interrupted, cancelled, rolled_back` | 🔴 |

- **Thread URLs** are `<web_url>/<environmentId>/<threadId>` (route
  `/$environmentId/$threadId` in the T3 web client; this server's
  environment id is in `~/.t3/userdata/environment-id`).
- **T3 lives in a Brave tab** on the Mac, not a desktop app. Pressing a
  slot runs `mac.tab` with `match` = the `web_url` host, `url` = the
  thread's URL, and `navigate: true`: the existing T3 tab is brought to
  front and pointed at the thread (a full page load, ~1 s), so presses never
  pile up tabs. ➕ New thread does the same with `url` = `web_url`.
- **Dials act only on the thread you are looking at**: the one open in the
  active tab of Brave's front window. There is no fallback to any other
  thread.
  - First turn (or first turn after 10 s idle): the deck asks the Mac for
    `active_url` right then, parses `<environmentId>/<threadId>`, and shows
    `<title> · xhigh → high` on the strip. If the tab is not a thread of a
    configured server, the strip shows `no T3 thread in front` and turning
    does nothing.
  - Press: the deck reads `active_url` again and applies only if it is still
    the same thread; otherwise the strip shows `thread changed — not
    applied`. Mac offline: `Mac offline — not applied`.
  - The URL is never polled; each decision uses a fresh read, so there is no
    lag window in which a stale URL could pick the wrong thread.
- **Model/effort:** dials 4–5 cycle a pending value shown on the strip;
  pressing applies it via `thread.model-selection.set`, keeping the thread's
  `instanceId` and other options. Model lists are per provider instance from
  config, so the dial never offers a model the thread's provider can't run.
  If the server rejects the change, the strip flashes the error.

## 10. Security

🔒 The dedicated SSH key on the PC lets the deck run actions on the Mac. The
agents on the PC run unsandboxed as the same user and can read that key.
Controls:

- Mac `authorized_keys` entry:
  `command="deck remote",restrict,from="192.168.1.5" ssh-ed25519 …` — the key
  can only start `deck remote`, never a shell, port forward, or PTY.
- `deck remote` accepts only the allowlisted kinds (section 5).
- The control socket exposes no Mac execution.
- Secrets (`HA_TOKEN`, `T3_TOKEN`) come from an `EnvironmentFile` sourced
  from dotfiles-secrets, never from the YAML.

Accepted risk for v1: a deliberately hostile agent running as the same user
can still use the key within the allowlist. A hard boundary (daemon as a
separate `streamdeck` user) is deferred until the phase-2 approval gate,
where it matters.

## 11. Phase 2 (not in v1)

- **Hardware approval gate:** the git-guard hook asks the daemon for approval
  of the exact command; the Approve key lights with the command text; a
  physical press allows it, a long press denies, a timeout denies. The
  existing `EXPLICITLY_USER_APPROVED_HOOK_OVERRIDE` path stays unchanged when
  the daemon is unreachable.
- `deck ask "<question>" --yes --no` for agents.
- Frontmost-app context pages pushed from `deck remote`.
- CI / PR status keys via `gh`.
- New thread with the clipboard as prompt.
- Claude Code / Codex in zellij: hook-driven status and `zellij action
  write-chars` for model switching.
- macOS host for the daemon (deck plugged into the Mac).

## 12. Setup

**PC — dotfiles `configs/nixos/optional/streamdeck.nix`, imported by `hosts/pc`:**

- udev: `KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", TAG+="uaccess"` and
  `SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", TAG+="uaccess"`.
- `users.users.basnijholt.linger = true` (service runs without a desktop
  session).
- `systemd.user.services.deck`: `ExecStart=%h/.local/bin/deck run`,
  `Environment=LD_LIBRARY_PATH=${pkgs.hidapi}/lib`,
  `EnvironmentFile=` the secrets file, `Restart=always`,
  `WantedBy=default.target`, `ddcutil` on `PATH`. The `deck` binary is
  installed with `uv tool install` outside Nix, matching how `t3code.nix`
  treats its binary.
- Config `configs/streamdeck/config.yaml`, linked by dotbot to
  `~/.config/deck/config.yaml`.

**Mac — nix-darwin:**

- `homebrew.brews += [ "switchaudio-osx" ]` (`blueutil` already present).
- `uv tool install` the same package (provides `deck remote`).
- The restricted `authorized_keys` entry from section 10.
- Accept the Automation prompts for Brave and Keyboard Maestro Engine once.

## 13. Error handling

| Failure | Behavior |
|---|---|
| Deck unplugged | keep running, reopen with backoff, re-render on reconnect |
| Config invalid on reload | keep previous config, ❌ on key 0, error in `deck check` and the journal |
| Mac unreachable | `mac.*` keys grey + 🚫, presses dropped, reconnect with backoff |
| HA / T3 unreachable | affected keys grey, providers reconnect with backoff |
| Action fails | key flashes red for 1 s; error logged with the action and argv |
| DDC command fails | re-detect the bus once, then show ❌ on the strip segment |

## 14. Testing

- `FakeDeck` + `render` tests assert image properties (size, background and
  border pixel colors per state, non-empty label region) rather than golden
  PNGs, which break across font installs; `deck render --png` output is
  reviewed by eye.
- Config: valid and invalid fixtures; `deck check` error paths.
- Actions: a command-runner stub records argv; assert exact commands for
  `ddc`, `mac.*` (on the remote side), `ha` (HTTP stubbed).
- Remote protocol: run `deck remote` as a subprocess with a stub executor;
  round-trip requests, state pushes, malformed lines, disallowed kinds.
- T3 client: against recorded WebSocket frames from the real server, plus
  status-mapping unit tests.
- Manual on hardware: each v1 key and dial once, then unplug/replug the
  deck, sleep the Mac, restart the T3 server.
