# Mac Dial-Out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development — four tasks with shared interfaces and concurrency-sensitive session code; fresh contexts and per-task review are worth the cost.

**Goal:** Every Mac connects out to the PC over SSH, and the daemon drives whichever Mac had keyboard or mouse input last.

**Architecture:** On each Mac, `deck connect` keeps an ssh child open to the PC and serves today's protocol over its stdio. The PC's sshd forces `deck attach <name>`, which relays to the daemon's Unix socket `deck-mac.sock`. There `MacHub` runs one `MacSession` per name, picks the active Mac by latest input, and mirrors it into `mac.*`.

**Tech Stack:** Python 3.13, asyncio, pydantic, pytest + pytest-asyncio, uv.

**Spec:** `docs/baspowers/specs/2026-10-28-mac-dial-out-design.md`

## Global Constraints

- Dependencies stay exactly `streamdeck>=0.10.0`, `pillow>=10.1`, `pydantic>=2`, `pyyaml`, `websockets>=15`.
- ssh argv: `ssh -T -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=3 -o ServerAliveCountMax=2 [-i PATH] DESTINATION`, no remote command.
- Reconnect backoff 1 s doubling to 30 s, reset after a session in which the `hello` arrived.
- Hub socket `$XDG_RUNTIME_DIR/deck-mac.sock` (else `/tmp/deck-mac-<uid>.sock`), mode 0600, stale socket file replaced.
- Attach line `{"attach": "<name>"}`, name `^[A-Za-z0-9_-]{1,32}$`, read within 5 s, at most 1 KiB.
- Request timeout 15 s. A session that has sent `idle_ms` at least once and then nothing for 5 s is closed.
- `idle_ms` = integer milliseconds from `ioreg -c IOHIDSystem -d 4 -r -k HIDIdleTime` (nanoseconds). (Ruling: was `idle` in seconds.)
- Store: `mac.*` mirrors the active Mac with today's keys, plus `mac.active` (name) and `mac.connected` (sorted names); no Mac online means `mac.online` false.
- The JSON Lines protocol, `ALLOWED_KINDS`, views, dials and actions do not change.
- Commit messages in normal English, conventional prefix (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`).

## Review Focus

1. A Mac that sleeps or loses Wi-Fi without closing TCP: the PC must stop treating it as online within ~5 s and hand the deck to the other Mac (Task 3 test `test_silent_session_is_closed`).
2. The same name reconnecting before the old connection's EOF: the old session's teardown must not mark the new one offline or wipe `mac.*` (Task 3 test `test_old_session_teardown_leaves_new_one_alone`).
3. The attach line and the first protocol line arriving in one write, or the attach line split across two writes (Task 3 test `test_attach_line_framing`).
4. Daemon restart: a Mac whose session had a hello retries after 1 s, not 30 s (Task 1 test `test_backoff_restarts_after_a_hello`).
5. `deck attach` while the daemon is down: one clear stderr line and exit 1, no traceback (Task 4 test `test_attach_without_daemon`).

---

### Task 1: `deck connect` loop (Mac side)

**Files:**
- Create: `src/deck/proc.py`, `src/deck/remote/connect.py`, `tests/fake_pc.py`, `tests/test_connect.py`
- Modify: `src/deck/mac/client.py` (import the moved helpers instead of defining them)
- Test: `tests/test_connect.py`, existing `tests/test_mac_client.py` must still pass

**Interfaces:**
- Produces, in `deck.proc` (moved from `deck.mac.client`, behavior unchanged):
  - `async def spawn(argv: list[str], limit: int) -> asyncio.subprocess.Process` (was `MacClient._spawn`; stdin/stdout/stderr are PIPEs)
  - `async def stop(proc: Process, stderr_task: asyncio.Task[None] | None) -> None`
  - `async def forward_stderr(stream: asyncio.StreamReader, prefix: str) -> None` (logs each line as warning `"%s: %s"`, prefix first)
  - constants `TERMINATE_GRACE = 2.0`, `STDERR_GRACE = 1.0`, `SPAWN_GRACE = 1.0`, `MAX_LOG_LINE = 4096`
- Produces, in `deck.remote.connect`:
  - `def ssh_argv(destination: str, key: Path | None) -> list[str]`
  - `async def connect_forever(argv: list[str], executor: Executor, poller: Poller, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None`
  - constants `BACKOFF_START = 1.0`, `BACKOFF_MAX = 30.0`, `STDOUT_LIMIT = 16 << 20`
- Consumes: `deck.remote.server.serve(reader, writer, executor, poller)`, `Executor`, `Poller`.

- [ ] **Step 1: Write `tests/fake_pc.py`**, a child that plays the PC: writes `{"hello": {}}\n` to stdout, then appends every line it reads from stdin to the file named by `argv[1]` until EOF. With `--stubborn` it ignores SIGTERM and keeps running after stdin closes (as `tests/stub_remote.py` does).

- [ ] **Step 2: Write the failing tests in `tests/test_connect.py`**

```python
def test_ssh_argv():
    assert ssh_argv("basnijholt@192.168.1.5", Path("/k")) == [
        "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=2",
        "-i", "/k", "basnijholt@192.168.1.5",
    ]
    assert "-i" not in ssh_argv("pc", None)

async def test_serves_the_child_until_it_exits(tmp_path): ...
    # fake_pc child; fake executor/poller; the log file gains a {"state": {...}} line
async def test_backoff_doubles_to_30(fake_sleep): ...
    # argv of a command that exits at once without a hello: sleeps 1, 2, 4, 8, 16, 30, 30
async def test_backoff_restarts_after_a_hello(fake_sleep): ...
    # a fake_pc session that sent its hello, then exits: the next sleep is 1.0
async def test_ssh_stderr_is_logged(caplog): ...
    # child writes "Permission denied (publickey)." to stderr: a warning "ssh: Permission denied (publickey)."
async def test_cancelling_leaves_no_child_process(tmp_path): ...
async def test_a_child_that_ignores_sigterm_is_killed(tmp_path): ...
async def test_a_failed_spawn_is_survived(fake_sleep, caplog): ...
    # argv ["/nonexistent"]: logs a warning, sleeps, retries
```

Port the cancellation and SIGTERM checks from `tests/test_mac_client.py` (`test_cancelling_run_leaves_no_child_process`, `test_a_child_that_ignores_sigterm_is_killed`, `test_a_failed_spawn_is_survived`), with `fake_pc.py` as the child.

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_connect.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'deck.remote.connect'`

- [ ] **Step 4: Move the helpers to `src/deck/proc.py`** and make `deck.mac.client` import them (`MacClient` passes prefix `"Mac"`).

- [ ] **Step 5: Implement `src/deck/remote/connect.py`.** One session: `spawn`, start `forward_stderr(proc.stderr, "ssh")`, `await serve(proc.stdout, proc.stdin, watched, poller)`, then `stop`. Detect the hello by wrapping the executor so its `configure` sets a flag and delegates. Log info `connected to the PC` on the hello and warning `PC connection down, retrying in %gs` before each sleep. An `OSError` from `spawn` is logged and retried like a dropped session.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_connect.py tests/test_mac_client.py -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add src/deck/proc.py src/deck/remote/connect.py src/deck/mac/client.py tests/fake_pc.py tests/test_connect.py
git commit -m "feat: add the Mac's dial-out loop, deck connect"
```

### Task 2: `idle` in the Mac poller

**Files:**
- Modify: `src/deck/remote/mac.py` (`MacPoller.snapshot`, new argv and parser)
- Test: `tests/test_remote_mac.py`

**Interfaces:**
- Produces: `IDLE_ARGV = ["ioreg", "-c", "IOHIDSystem", "-d", "4", "-r", "-k", "HIDIdleTime"]`; `def parse_idle(out: str) -> int` (whole seconds; `ValueError` when no `"HIDIdleTime" = <digits>` line); snapshot key `"idle"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_idle():
    out = '+-o IOHIDSystem  <class IOHIDSystem>\n    "HIDIdleTime" = 4500000000\n'
    assert parse_idle(out) == 4
    with pytest.raises(ValueError):
        parse_idle("+-o IOHIDSystem\n")

async def test_snapshot_reports_idle(): ...      # FakeRunner answers IDLE_ARGV: state["idle"] == 4
async def test_snapshot_without_idle_when_ioreg_fails(): ...  # rc 1: "idle" not in state
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_remote_mac.py -k idle -v`
Expected: FAIL, `ImportError: cannot import name 'parse_idle'`

- [ ] **Step 3: Implement.** Add the query to the `asyncio.gather` in `snapshot()` through `self._query(IDLE_ARGV, parse_idle)`; set `state["idle"]` when not None. Integer division of nanoseconds by 1 000 000 000.

- [ ] **Step 4: Run the Mac remote tests**

Run: `uv run pytest tests/test_remote_mac.py -v`
Expected: all PASS (update any test that asserts the exact set of snapshot keys or the exact argv list)

- [ ] **Step 5: Commit**

```bash
git add src/deck/remote/mac.py tests/test_remote_mac.py
git commit -m "feat: report seconds since the last keyboard or mouse input from the Mac"
```

### Task 3: `MacHub` and `MacSession` (PC side)

**Files:**
- Create: `src/deck/mac/attach.py` (name check and socket path only; the relay comes in Task 4), `tests/test_mac_hub.py`
- Modify: `src/deck/mac/client.py` (add `MacSession`, `MacHub`; `MacClient` stays until Task 4), `src/deck/control.py` (extract the socket binding)
- Test: `tests/test_mac_hub.py`, `tests/test_control.py`

**Interfaces:**
- Produces, in `deck.control`: `def bind_private_socket(path: Path) -> socket.socket`, the bind-with-umask-0o177, chmod 0600 and stale-socket replacement now inline in `serve_control`, which calls it.
- Produces, in `deck.mac.attach`: `def valid_mac_name(name: str) -> bool`; `def default_mac_socket() -> Path`.
- Produces, in `deck.mac.client`:
  - `class MacSession`: `__init__(self, name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, clock: Callable[[], float])`; `async def run(self, hello: bytes, on_state: Callable[["MacSession"], None]) -> None` (sends `hello`, reads until EOF or close, fails pending requests with `MacOffline` on the way out); `async def request(self, kind: str, args: Any, timeout: float = REQUEST_TIMEOUT) -> Any`; `def close(self) -> None`; attributes `name`, `state: dict[str, Any]` (snapshot merged with diffs), `online: bool` (true from the first snapshot), `last_input: float`, `last_message: float`.
  - `class MacHub`: `__init__(self, path: Path, audio: AudioSource, store: StateStore, clock: Callable[[], float] = time.monotonic)`; `async def run(self) -> None` (listens until cancelled, then closes every session and unlinks the socket); `async def request(self, kind: str, args: Any, timeout: float = REQUEST_TIMEOUT) -> Any`; properties `online: bool`, `active: str | None`.
  - constants `ATTACH_TIMEOUT = 5.0`, `MAX_ATTACH_LINE = 1024`, `STALE_AFTER = 5.0`
- Consumes: `deck.remote.server.serve` (fake Macs in tests), `MacOffline`, `MacError`, `REQUEST_TIMEOUT`, `AudioSource`.

- [ ] **Step 1: Write the failing tests in `tests/test_mac_hub.py`.** A fixture runs `MacHub` on `tmp_path / "mac.sock"`. A helper `fake_mac(path, name, poller, executor=None)` opens the socket, writes the attach line, and runs `serve()` with a fake executor and a poller whose returned dict the test changes.

```python
async def test_request_reaches_the_mac_and_state_is_mirrored(hub, store): ...
    # store["mac.online"] is True, store["mac.active"] == "mini", store["mac.connected"] == ["mini"]
async def test_hello_carries_the_audio_config(hub): ...
async def test_active_is_the_mac_with_the_latest_input(hub, store): ...
    # mini idle 0, home idle 50: active "mini"; home idle drops to 0 a poll later: active "home",
    # store["mac.audio.output"] is home's
async def test_active_mac_dropping_falls_back(hub, store): ...
async def test_no_mac_is_offline(hub, store): ...
    # store["mac.online"] is False; hub.request raises MacOffline at once
async def test_request_fails_when_its_mac_drops(hub): ...
    # request to mini held by its executor; mini disconnects while home stays: MacOffline
async def test_same_name_replaces_the_older_session(hub): ...
async def test_old_session_teardown_leaves_new_one_alone(hub, store): ...
    # second "mini" connects; first "mini" closes afterwards: mac.online stays True, state is the new one's
async def test_attach_line_framing(hub): ...
    # attach line + first protocol line in one write; attach line split over two writes
@pytest.mark.parametrize("line", [b"nope\n", b'{"attach": "bad name"}\n', b'{"x": 1}\n', b"a" * 2000])
async def test_bad_attach_line_is_closed(hub, caplog, line): ...
async def test_silent_attach_is_closed_after_5s(hub): ...    # fake clock / short constant via monkeypatch
async def test_silent_session_is_closed(hub, store): ...
    # a Mac that sends its snapshot then nothing for STALE_AFTER: dropped, mac.online False
async def test_error_reply_raises_mac_error(hub): ...
async def test_request_times_out_with_mac_error(hub): ...
async def test_big_lines_are_read(hub): ...
async def test_socket_is_private_and_removed_on_exit(tmp_path, store): ...  # mode 0o600; gone after cancel
```

In `tests/test_control.py` add `test_bind_private_socket_replaces_a_stale_socket` and `test_bind_private_socket_refuses_a_regular_file` (moved from the existing `serve_control` cases if they already cover it).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_mac_hub.py -v`
Expected: FAIL, `ImportError: cannot import name 'MacHub'`

- [ ] **Step 3: Implement `bind_private_socket` and `deck.mac.attach`'s two functions.** `default_mac_socket()` mirrors `default_control_path()`: `$XDG_RUNTIME_DIR/deck-mac.sock`, else `/tmp/deck-mac-<uid>.sock`.

- [ ] **Step 4: Implement `MacSession` and `MacHub`.**
  - Build `MacSession` from the protocol half of `MacClient`: `_read`, `_on_reply` and request bookkeeping. Leave `MacClient` untouched; Task 4 deletes it, so the duplication lasts one task.
  - Every message sets `last_message`. A state message carrying `idle` sets `last_input = clock() - idle`. `last_input` starts at the connect time.
  - The hub's attach handshake reads with `reader.readline()` under `asyncio.timeout(ATTACH_TIMEOUT)`, with the StreamReader limit at `MAX_ATTACH_LINE` for that line.
  - The hub keeps `sessions: dict[str, MacSession]`. Its `on_state` callback and session exits call one `_publish()`:
    - Choose active = the online session with the greatest `last_input`.
    - If active changed, `store.update("mac.", {**session.state, "online": True, "active": name, "connected": names}, replace=True)`.
    - Otherwise, if the update came from the active session, `store.update("mac.", changed_values)`.
    - Otherwise refresh only `mac.connected` when it changed.
    - With no online session, `store.update("mac.", {"online": False, "connected": []}, replace=True)`.
  - A session leaves `sessions` only if it is still the one registered under its name, which fixes Review Focus 2.
  - A watchdog task closes any session with `clock() - last_message > STALE_AFTER`. It checks once a second.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_mac_hub.py tests/test_control.py tests/test_mac_client.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/deck/mac/attach.py src/deck/mac/client.py src/deck/control.py tests/test_mac_hub.py tests/test_control.py
git commit -m "feat: add MacHub, which serves Macs that dial in and follows the last-used one"
```

### Task 4: Wire it up: `deck attach`, `deck connect`, config, removals, docs

**Files:**
- Modify: `src/deck/mac/attach.py` (relay), `src/deck/cli.py`, `src/deck/config.py` (`MacConfig`), `src/deck/mac/client.py` (delete `MacClient` and its subprocess code), `src/deck/remote/server.py` (delete `serve_stdio`), `src/deck/t3/controller.py` (type `MacHub`), `examples/config.yaml`, `examples/full.yaml`, `tests/fixtures/valid.yaml`, `README.md`, `docs/baspowers/specs/2026-10-28-streamdeck-design.md` (one line under §5: `Superseded by 2026-10-28-mac-dial-out-design.md.`)
- Delete: the `MacClient` tests in `tests/test_mac_client.py`. Keep the file's `handle_mac_action` tests (the `FakeMac` section) and point them at `MacHub` where they need a type. Delete the `serve_stdio` test in `tests/test_remote_server.py`.
- Create: `tests/test_mac_chain.py`
- Test: `tests/test_mac_chain.py`, `tests/test_config.py`, whole suite

**Interfaces:**
- Produces: `async def attach(name: str, path: Path) -> int` in `deck.mac.attach`; CLI `deck attach NAME`, `deck connect DESTINATION [--key PATH]`; `MacConfig` with no fields (`mac: {}`).
- Consumes: Task 1 `ssh_argv`, `connect_forever`; Task 3 `MacHub`, `valid_mac_name`, `default_mac_socket`; existing `_extend_path`, `MacExecutor`, `MacPoller`, `Runner`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mac_chain.py
async def test_connect_attach_hub_chain(tmp_path, monkeypatch, store):
    # XDG_RUNTIME_DIR=tmp_path; MacHub on default_mac_socket();
    # connect_forever([sys.executable, "-c", "from deck.cli import main; raise SystemExit(main(['attach', 'mini']))"],
    #                 FakeExecutor(), FakePoller({"volume": 42, "idle": 0}))
    # eventually: store["mac.active"] == "mini", store["mac.volume"] == 42;
    # await hub.request("app", "Ghostty") returns and FakeExecutor saw ("app", "Ghostty")

def test_attach_without_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert main(["attach", "mini"]) == 1
    assert "deck is not running" in capsys.readouterr().err

def test_attach_rejects_a_bad_name(capsys):
    assert main(["attach", "bad name"]) == 1
    assert "invalid name" in capsys.readouterr().err

# tests/test_config.py
def test_mac_section_has_no_fields(tmp_path): ...   # mac: {} loads; mac: {host: x} reports "mac.host: Extra inputs are not permitted"
```

Set `MAC = {}` in `tests/test_config.py`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_mac_chain.py tests/test_config.py -v`
Expected: FAIL (`invalid choice: 'attach'`, and `mac.host` still accepted)

- [ ] **Step 3: Implement `attach`.** Invalid name: stderr `deck attach: invalid name {name!r}`, return 1. `FileNotFoundError`/`ConnectionRefusedError` on connect: stderr `deck is not running (nothing is listening on {path})`, return 1. Otherwise send the attach line, then pump stdin to the socket and the socket to stdout. Both are asyncio stream pairs, the same pipe setup the deleted `serve_stdio` used. Return 0 when either side ends.

- [ ] **Step 4: Wire the CLI and config.**
  - Add `attach` and `connect`; `connect` calls `_extend_path()` and runs `connect_forever(ssh_argv(...), MacExecutor(Runner()), MacPoller(Runner(), executor))` with `logging.basicConfig` to stderr.
  - Remove the `remote` subcommand and `_remote`.
  - In `_services`, build `MacHub(default_mac_socket(), lambda: services.config.audio, store)` and start it with `_start_background`.
  - Empty `MacConfig`.
  - Delete `MacClient` and the spawn imports it alone used, and delete `serve_stdio`. Update the type in `t3/controller.py`.
  - Set `mac: {}` in the two examples and the fixture.

- [ ] **Step 5: Update `README.md`**
  - Architecture diagram: the arrow runs from Mac to PC, labeled `deck connect` (Mac) and `deck attach` (PC).
  - Mac setup:
    - `uv tool install`, `brew install switchaudio-osx`
    - `ssh-keygen -t ed25519 -f ~/.config/deck/pc_ed25519 -N ""`
    - the PC `authorized_keys` line from spec §5
    - a LaunchAgent plist `~/Library/LaunchAgents/nl.nijho.deck.plist` with `ProgramArguments` `[/Users/<you>/.local/bin/deck, connect, basnijholt@192.168.1.5, --key, /Users/<you>/.config/deck/pc_ed25519]`, `RunAtLoad`, `KeepAlive`, `StandardErrorPath` `/tmp/deck.log`, loaded with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/nl.nijho.deck.plist`
  - Security: rewrite from spec §5.
  - CLI table: add `attach` and `connect`, drop `remote`.
  - Config table: `mac` is `{}` and turns on the hub.
  - Note that two `deck connect`s under one name keep replacing each other.

- [ ] **Step 6: Run the whole suite and the example checks**

Run: `uv run pytest -q && uv run deck check examples/config.yaml && uv run deck check examples/full.yaml`
Expected: all tests pass, `ok` twice

- [ ] **Step 7: Commit**

```bash
git add -A src tests examples README.md docs/baspowers/specs/2026-10-28-streamdeck-design.md
git commit -m "feat: Macs dial in: deck connect on the Mac, deck attach on the PC"
```
