# Mac dial-out — design

Supersedes the transport of the main design (§5 "Mac remote", the SSH key
direction in §10, the Mac half of §12). Everything else in
`2026-10-28-streamdeck-design.md` stands.

## 1. Intent

**What the user said**

- The deck must also drive a MacBook that nothing can SSH into.
- When both Macs are connected, keys act on the Mac with the most recent
  keyboard or mouse input (option A of three: last input, monitor input,
  manual toggle).

**Assumptions**

- Both Macs are on the home LAN (192.168.1.0/24) and use the same Dell.
- Every Mac can open an outgoing SSH connection to the PC
  (192.168.1.5:22).
- Installing `deck` and `switchaudio-osx` on every Mac, and granting it
  Automation permission for Brave and Keyboard Maestro, is allowed.

**Success criteria**

- No connection into any Mac: every Mac dials out to the PC.
- The deck acts on the Mac that had input last, within 2 s of that input.
- One Mac going away never affects the other.
- The PC holds no key that can make a Mac do anything.
- The JSON Lines protocol, the Mac executor's allowlist, the views, dials and
  actions stay as they are.

## 2. Approaches

1. **Chosen: each Mac SSHes out to the PC.** The PC's `authorized_keys`
   pins each Mac's key to the forced command `deck attach <name>`, which
   relays to a Unix socket of the daemon. No new network listener, SSH does
   the authentication and encryption, and the Mac's name is bound to its key.
2. A WebSocket listener in the daemon with a token per Mac. Rejected: a new
   LAN-facing port plus token and TLS handling, for nothing SSH lacks.
3. Keep PC→Mac SSH for Macs that allow it and add dial-out for the rest.
   Rejected: two transports to build, test and debug.

## 3. Architecture

```
  Mac (each)                                   PC
┌──────────────────────────────┐         ┌──────────────────────────────────────────┐
│ LaunchAgent:                 │  SSH    │ sshd ─ forced command                    │
│ deck connect basnijholt@pc ──┼────────▶│        deck attach mini ──┐              │
│   serve(): executor, poller  │ JSONL   │                           ▼              │
└──────────────────────────────┘         │ $XDG_RUNTIME_DIR/deck-mac.sock (0600)    │
                                         │   └─ deck run: MacHub ─ sessions by name │
                                         │        active = latest input  ─▶ mac.*   │
                                         └──────────────────────────────────────────┘
```

The first line on the socket comes from `deck attach`:
`{"attach": "mini"}`. From then on the stream carries today's protocol
unchanged: the PC sends `hello`, the Mac answers with its snapshot, then
state diffs, requests and replies.

## 4. Components

### 4.1 Mac: `deck connect DESTINATION [--key PATH]`

- Loops: spawn
  `ssh -T -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=3 -o ServerAliveCountMax=2 [-i PATH] DESTINATION`
  (no remote command: the PC's forced command decides), then run
  `serve(proc.stdout, proc.stdin, MacExecutor, MacPoller)` until ssh exits.
  Reconnect with backoff 1 s → 30 s, reset after a session in which the
  `hello` arrived. ssh's stderr goes to the log.
- Calls `_extend_path()` first: launchd starts it with a bare `PATH`.
- The spawn and stop helpers that `deck.mac.client` uses today for ssh move
  to a shared module and are reused here unchanged.
- Runs as a LaunchAgent in the user's GUI session (`RunAtLoad`,
  `KeepAlive`), so AppleScript, `screencapture` and Bluetooth run as the
  logged-in user. Automation prompts name the deck's Python; accept once.
- `deck remote` and `serve_stdio` are removed: nothing runs them any more.

### 4.2 Mac poller: `idle`

`MacPoller.snapshot()` gains `idle`: seconds since the last keyboard or
mouse input, an integer, read from
`ioreg -c IOHIDSystem -d 4 -r -k HIDIdleTime` (nanoseconds). It changes on
every poll, so each Mac sends one small diff per second. A failed or
unparseable read leaves `idle` out.

### 4.3 PC: `deck attach NAME`

- `NAME` must match `^[A-Za-z0-9_-]{1,32}$`.
- Connects to `$XDG_RUNTIME_DIR/deck-mac.sock`, writes the attach line, then
  copies stdin to the socket and the socket to stdout until either side
  closes. If the daemon is not running it prints why on stderr and exits 1;
  the Mac's ssh ends and `deck connect` retries.

### 4.4 PC: `MacHub` (replaces `MacClient`)

- Listens on `$XDG_RUNTIME_DIR/deck-mac.sock`, mode 0600, replacing a stale
  socket file.
- Per connection: read the attach line (5 s timeout, max 1 KiB; otherwise
  log and close), then run a `MacSession` for that name. A second connection
  with the same name closes the older session.
- `MacSession` is the protocol half of today's `MacClient`, unchanged in
  behavior: hello built from the audio config at connect time, read loop,
  request ids and futures, 15 s request timeout, every pending request fails
  with `MacOffline` when the session ends. It also keeps the Mac's full state
  (snapshot merged with diffs) and `last_input`.
- `last_input`: the PC's monotonic clock minus `idle`, updated by each state
  message that carries `idle`; the connect time until the first one.
- **Active Mac:** the online session with the greatest `last_input`,
  recomputed on every state message, connect and disconnect.
- **Store:** `mac.*` mirrors the active session with today's keys, so views,
  dials and actions keep working unchanged, plus `mac.active` (its name) and
  `mac.connected` (sorted names of online sessions). When the active Mac
  changes, `mac.*` is replaced by the new Mac's state. With no Mac online,
  `mac.online` is false.
- `request(kind, args)` goes to the active session; with none it raises
  `MacOffline`. A request belongs to the session it was sent on: if that Mac
  drops, it fails with `MacOffline` even if another Mac is online.
- Public surface stays `run()`, `request()`, `online`, so
  `handle_mac_action`, the volume and input dials and `T3Controller` do not
  change.

### 4.5 Config

`mac:` loses `host`, `key` and `command` and has no fields: `mac: {}` turns
the hub on. Old fields fail validation; no deployed config uses them yet.

## 5. Security

🔒 Each Mac has its own key, `~/.config/deck/pc_ed25519`. The PC's
`~/.ssh/authorized_keys` gets one line per Mac:

```
command="/home/basnijholt/.local/bin/deck attach mini",restrict,from="192.168.1.0/24" ssh-ed25519 AAAA… deck@mini
```

- The key can only attach under its own name: no shell, PTY or forwarding.
- The PC no longer holds a key that drives a Mac. The `mac_ed25519` key
  generated for the old design is deleted.
- Whoever holds a Mac's key can pose as that Mac: claim constant input to
  become active, then see the requests the deck sends (which apps, URLs and
  tabs are pressed) and feed it false state. They cannot run anything on the
  PC or on the other Mac. Accepted for a home LAN.
- The socket is 0600: only processes of the same user on the PC can attach,
  the same trust level as the control socket.

## 6. Error handling

| Failure | Behavior |
|---|---|
| PC unreachable, daemon down | `deck connect` retries with backoff 1 s → 30 s |
| A Mac drops | its session ends, its pending requests fail with `MacOffline`, the next most recent Mac becomes active, or `mac.online` goes false |
| Bad or missing attach line | connection closed, warning logged |
| Same name connects twice | the newer session wins, the older is closed |
| `ioreg` fails | `idle` left out; the Mac keeps its last `last_input` |
| Mac sleeps or the network drops without closing the connection | a session that has sent nothing for 5 s is closed (with `idle`, every Mac sends a diff each second), as if the Mac dropped |

## 7. Testing

- `MacSession` and `MacHub` against in-process fake Macs: `serve()` with fake
  executor and poller, connected to a real hub socket in a temp dir.
- Active selection: two fake Macs; `idle` updates move the active Mac;
  disconnecting the active one falls back; no Mac means offline; a request in
  flight fails when its Mac drops while another stays online.
- `deck attach` end to end: hub socket, `deck attach` as a subprocess, the
  existing `tests/stub_remote.py` on its stdio.
- `deck connect` with the ssh argv swapped for a local command that runs
  `deck attach`, so the whole chain runs without ssh.
- `idle` parsing from `ioreg` output, including failure.
- The existing Mac client tests move to the hub where they still apply.

## 8. Setup

- **PC:** one `authorized_keys` line per Mac (section 5). Nothing else new.
- **Each Mac:** `uv tool install` the package, `brew install
  switchaudio-osx`, `ssh-keygen -t ed25519 -f ~/.config/deck/pc_ed25519`,
  send the public key to the PC, install the LaunchAgent. A Mac managed
  by nix-darwin gets it from `launchd.user.agents.deck`, any other from a
  plist in `~/Library/LaunchAgents/`.
- **Preview without a Mac:** cross-wire `tests/stub_remote.py` and
  `deck attach preview` on the PC (each one's stdout into the other's stdin,
  e.g. with a bash `coproc`).

## 9. Not in this change

- Showing which Mac is active on the deck (`mac.active` is in `deck state`).
- Per-Mac audio config: both Macs share `audio`; an output a Mac lacks
  fails with a red flash.
