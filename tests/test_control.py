import asyncio
import contextlib
import json
import os
import socket
import stat
import time
from pathlib import Path

import pytest
from fake_deck import FakeDeck

from deck import control
from deck.actions import ActionRunner
from deck.cli import main
from deck.config import Action
from deck.control import bind_private_socket, call, default_control_path, serve_control
from deck.daemon import Daemon
from deck.dials import Services
from deck.events import Connected, KeyDown, KeyUp
from deck.state import StateStore

CONFIG = """\
mac: {}
pages:
  home:
    keys:
      - {label: Ghostty, press: {mac.app: Ghostty}, long_press: {mac.app: Terminal}}
      - {label: Next, press: {page: second}}
      - {label: Safari, press: {mac.app: Safari}}
      - {label: Idle}
      -
      - {label: Fails, press: {mac.lock: null}}
  second:
    keys:
      - {label: Back, press: {page: home}}
"""


async def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.005)


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "deck.yaml"
    path.write_text(CONFIG)
    return path


@pytest.fixture
def sock_path(tmp_path: Path) -> Path:
    return tmp_path / "deck.sock"


@pytest.fixture
def deck() -> FakeDeck:
    return FakeDeck()


@pytest.fixture
def calls() -> list[Action]:
    return []


@pytest.fixture
def make_daemon(config_path, deck, calls):
    def build(**kwargs) -> Daemon:
        async def record(action: Action) -> None:
            calls.append(action)

        async def fail(action: Action) -> None:
            raise RuntimeError("mac is on fire")

        async def services_factory(config, store: StateStore, stack) -> Services:
            actions = ActionRunner()
            actions.register("mac.", record)
            actions.register("mac.lock", fail)
            return Services(store, config, actions)

        return Daemon(config_path, deck, services_factory, **kwargs)

    return build


@contextlib.asynccontextmanager
async def running(daemon: Daemon, deck: FakeDeck, path: Path):
    """Run the daemon (control socket included) against the fake deck."""
    task = asyncio.create_task(daemon.run())
    await deck.inject(Connected())
    await wait_until(lambda: path.exists() or task.done())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
async def daemon(make_daemon, deck, sock_path):
    """A running daemon listening on `sock_path`."""
    daemon = make_daemon(control_path=sock_path)
    async with running(daemon, deck, sock_path):
        await wait_until(lambda: hasattr(daemon, "services"))
        yield daemon


# --- socket -------------------------------------------------------------------------


async def test_socket_is_created_with_mode_0600_whatever_the_umask(make_daemon, sock_path):
    daemon = make_daemon()
    old = os.umask(0)
    try:
        server = await serve_control(sock_path, daemon)
    finally:
        os.umask(old)
    try:
        assert stat.S_ISSOCK(sock_path.lstat().st_mode)
        assert stat.S_IMODE(sock_path.lstat().st_mode) == 0o600
    finally:
        await control.close_control(server, sock_path)


async def test_umask_is_restored_after_creating_the_socket(make_daemon, sock_path):
    old = os.umask(0o022)
    try:
        server = await serve_control(sock_path, make_daemon())
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(old)
    await control.close_control(server, sock_path)


def test_bind_private_socket_replaces_a_stale_socket(sock_path):
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(sock_path))
    stale.close()  # the file stays behind, nobody listens
    assert sock_path.exists()
    sock = bind_private_socket(sock_path)
    try:
        sock.listen()
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(sock_path))
        assert stat.S_IMODE(sock_path.lstat().st_mode) == 0o600
    finally:
        sock.close()


def test_bind_private_socket_refuses_a_regular_file(sock_path):
    sock_path.write_text("precious")
    with pytest.raises(OSError):
        bind_private_socket(sock_path)
    assert sock_path.read_text() == "precious"


def test_default_path_follows_xdg_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert default_control_path() == tmp_path / "deck.sock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert default_control_path() == Path(f"/tmp/deck-{os.getuid()}.sock")


# --- daemon lifecycle -----------------------------------------------------------------


async def test_run_serves_the_socket_and_removes_it_on_exit(make_daemon, deck, sock_path):
    daemon = make_daemon(control_path=sock_path)
    async with running(daemon, deck, sock_path):
        assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600
        assert (await call(sock_path, {"cmd": "state"}))["ok"] is True
    assert not sock_path.exists()


async def test_default_control_path_is_used_when_none_is_given(make_daemon, deck, runtime_dir):
    daemon = make_daemon()
    async with running(daemon, deck, runtime_dir / "deck.sock"):
        assert (await call(runtime_dir / "deck.sock", {"cmd": "state"}))["ok"] is True


async def test_shutdown_does_not_wait_for_connected_clients(make_daemon, deck, sock_path):
    daemon = make_daemon(control_path=sock_path)
    async with running(daemon, deck, sock_path) as task:
        _, writer = await asyncio.open_unix_connection(sock_path)
        await settle()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        writer.close()
    assert not sock_path.exists()


async def test_daemon_runs_without_control_socket_if_it_cannot_be_created(
    make_daemon, deck, tmp_path, caplog
):
    daemon = make_daemon(control_path=tmp_path / "missing-dir" / "deck.sock")
    task = asyncio.create_task(daemon.run())
    try:
        await deck.inject(Connected())
        await wait_until(lambda: deck.push_count > 0)
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert "control socket" in caplog.text


# --- commands -------------------------------------------------------------------------


async def test_state_returns_the_store(daemon, sock_path):
    daemon.store.update("", {"mac.online": True, "mac.volume": 40})
    response = await call(sock_path, {"cmd": "state"})
    assert response["ok"] is True
    assert response["state"] == daemon.store.snapshot()
    assert response["state"]["mac.volume"] == 40


async def test_state_stringifies_values_json_cannot_hold(daemon, sock_path):
    daemon.store.set("t3.threads", [Path("/x")])
    response = await call(sock_path, {"cmd": "state"})
    assert response["state"]["t3.threads"] == ["/x"]


async def test_state_larger_than_the_default_line_limit_round_trips(daemon, sock_path):
    big = {f"t3.key{i}": f"value {i} " * 4 for i in range(4000)}
    daemon.store.update("", big)
    assert len(json.dumps(daemon.store.snapshot())) > 2 * 65536
    response = await call(sock_path, {"cmd": "state"})
    assert response["ok"] is True
    assert response["state"] == daemon.store.snapshot()


async def test_state_that_cannot_be_serialized_is_answered_not_dropped(daemon, sock_path):
    daemon.store.set("broken", {(1, 2): "tuple keys are not JSON"})
    reader, writer = await asyncio.open_unix_connection(sock_path)
    try:
        writer.write(b'{"cmd": "state"}\n{"cmd": "frobnicate"}\n')
        await writer.drain()
        replies = [json.loads(await asyncio.wait_for(reader.readline(), 2)) for _ in range(2)]
    finally:
        writer.close()
    assert replies == [
        {"ok": False, "error": "internal error"},
        {"ok": False, "error": "unknown command"},
    ]


async def test_press_runs_the_keys_action(daemon, sock_path, calls):
    response = await call(sock_path, {"cmd": "press", "page": "home", "key": 0})
    assert response == {"ok": True}
    await settle()
    assert calls == [Action(type="mac.app", args="Ghostty")]


async def test_long_press_runs_long_press_or_falls_back_to_press(daemon, sock_path, calls):
    await call(sock_path, {"cmd": "press", "page": "home", "key": 0, "long": True})
    await call(sock_path, {"cmd": "press", "page": "home", "key": 2, "long": True})
    await settle()
    assert calls == [
        Action(type="mac.app", args="Terminal"),
        Action(type="mac.app", args="Safari"),
    ]


async def test_press_does_not_switch_the_visible_page(daemon, sock_path, deck, calls):
    await deck.inject(KeyDown(1), KeyUp(1))  # physical press on "Next"
    await wait_until(lambda: daemon.current_page == "second")
    await call(sock_path, {"cmd": "press", "page": "home", "key": 0})
    await settle()
    assert calls == [Action(type="mac.app", args="Ghostty")]
    assert daemon.current_page == "second"


async def test_press_on_a_page_action_switches_like_a_physical_press(daemon, sock_path):
    await call(sock_path, {"cmd": "press", "page": "home", "key": 1})
    assert daemon.current_page == "second"


async def test_press_failure_flashes_the_key_like_a_physical_press(daemon, sock_path, deck):
    daemon.render()
    calm = deck.keys[5].tobytes()
    response = await call(sock_path, {"cmd": "press", "page": "home", "key": 5})
    assert response == {"ok": True}  # started; the action's own failure is not the caller's
    await settle()
    daemon.render()
    assert deck.keys[5].tobytes() != calm


async def test_press_failure_on_a_hidden_page_does_not_flash(daemon, sock_path, deck):
    await deck.inject(KeyDown(1), KeyUp(1))
    await wait_until(lambda: daemon.current_page == "second")
    daemon.render()
    before = {index: image.tobytes() for index, image in deck.keys.items()}
    await call(sock_path, {"cmd": "press", "page": "home", "key": 5})
    await settle()
    daemon.render()
    assert {index: image.tobytes() for index, image in deck.keys.items()} == before


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ({"cmd": "press", "page": "nope", "key": 0}, "unknown page 'nope'"),
        ({"cmd": "press", "page": "home", "key": 36}, "no key 36 on page 'home'"),
        ({"cmd": "press", "page": "home", "key": -1}, "no key -1 on page 'home'"),
        ({"cmd": "press", "page": "home", "key": 4}, "no key 4 on page 'home'"),
        ({"cmd": "press", "page": "home", "key": 3}, "key 3 on page 'home' has no action"),
    ],
)
async def test_press_reports_what_cannot_be_pressed(daemon, sock_path, calls, message, error):
    assert await call(sock_path, message) == {"ok": False, "error": error}
    await settle()
    assert calls == []


@pytest.mark.parametrize(
    "message",
    [
        {"cmd": "press"},
        {"cmd": "press", "page": "home"},
        {"cmd": "press", "page": 1, "key": 0},
        {"cmd": "press", "page": "home", "key": "0"},
        {"cmd": "press", "page": "home", "key": True},
        {"cmd": "press", "page": "home", "key": 1.5},
        {"cmd": "press", "page": "home", "key": 0, "long": "yes"},
    ],
)
async def test_press_rejects_malformed_arguments(daemon, sock_path, calls, message):
    response = await call(sock_path, message)
    assert response["ok"] is False
    assert response["error"].startswith("bad request")
    await settle()
    assert calls == []


async def test_reload_applies_the_file(daemon, sock_path, config_path):
    config_path.write_text(CONFIG.replace("label: Ghostty", "label: Kitty"))
    assert await call(sock_path, {"cmd": "reload"}) == {"ok": True, "errors": []}
    assert daemon.config.pages["home"].keys[0].label == "Kitty"


async def test_reload_reports_config_errors_and_keeps_the_old_config(
    daemon, sock_path, config_path
):
    config_path.write_text("pages: [")
    response = await call(sock_path, {"cmd": "reload"})
    assert response["ok"] is False
    assert response["errors"]
    assert response["errors"][0].startswith("yaml:")
    assert daemon.config.pages["home"].keys[0].label == "Ghostty"

    config_path.write_text(CONFIG)
    assert await call(sock_path, {"cmd": "reload"}) == {"ok": True, "errors": []}


async def test_unknown_command(daemon, sock_path):
    assert await call(sock_path, {"cmd": "frobnicate"}) == {
        "ok": False,
        "error": "unknown command",
    }
    assert await call(sock_path, {}) == {"ok": False, "error": "unknown command"}
    assert await call(sock_path, {"cmd": ["state"]}) == {"ok": False, "error": "unknown command"}


async def test_no_command_runs_an_arbitrary_action(daemon, sock_path, calls):
    for cmd in ("run", "action", "exec"):
        response = await call(sock_path, {"cmd": cmd, "action": {"mac.app": "Ghostty"}})
        assert response == {"ok": False, "error": "unknown command"}
    # An action smuggled into `press` is ignored: only the configured key's action runs.
    await call(sock_path, {"cmd": "press", "page": "home", "key": 2, "action": {"mac.app": "Evil"}})
    await settle()
    assert calls == [Action(type="mac.app", args="Safari")]


# --- protocol -------------------------------------------------------------------------


async def test_one_response_line_per_request_and_bad_input_keeps_serving(daemon, sock_path):
    reader, writer = await asyncio.open_unix_connection(sock_path)
    try:
        writer.write(b"this is not json\n")
        writer.write(b'["a", "list"]\n')
        writer.write(b'{"cmd": "state"}\n')
        writer.write(b'{"cmd": "frobnicate"}\n')
        await writer.drain()
        replies = [json.loads(await asyncio.wait_for(reader.readline(), 2)) for _ in range(4)]
    finally:
        writer.close()
    assert replies[0] == {"ok": False, "error": "bad request"}
    assert replies[1] == {"ok": False, "error": "bad request"}
    assert replies[2]["ok"] is True
    assert replies[3] == {"ok": False, "error": "unknown command"}


async def test_blank_lines_are_ignored(daemon, sock_path):
    reader, writer = await asyncio.open_unix_connection(sock_path)
    try:
        writer.write(b'\n  \n{"cmd": "state"}\n')
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert reply["ok"] is True
    finally:
        writer.close()


async def test_oversized_request_is_rejected_and_the_daemon_keeps_serving(daemon, sock_path):
    reader, writer = await asyncio.open_unix_connection(sock_path)
    try:
        writer.write(b"x" * 70_000 + b"\n")  # beyond the 64 KiB line limit
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert reply == {"ok": False, "error": "bad request"}
    finally:
        writer.close()
    assert (await call(sock_path, {"cmd": "state"}))["ok"] is True


async def test_client_that_disconnects_mid_request_does_not_hurt(daemon, sock_path):
    _, writer = await asyncio.open_unix_connection(sock_path)
    writer.write(b'{"cmd": "sta')  # half a request
    writer.close()
    await settle()
    assert (await call(sock_path, {"cmd": "state"}))["ok"] is True


async def test_call_without_a_listener_raises(sock_path):
    with pytest.raises(FileNotFoundError):
        await call(sock_path, {"cmd": "state"})


# --- CLI ----------------------------------------------------------------------------------


def test_cli_reports_a_deck_that_is_not_running(capsys):
    for argv in (["reload"], ["state"], ["press", "home", "0"]):
        assert main(argv) == 1
        err = capsys.readouterr().err
        assert "deck is not running" in err


def test_cli_treats_a_stale_socket_as_not_running(capsys, runtime_dir):
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(runtime_dir / "deck.sock"))
    stale.close()
    assert main(["state"]) == 1
    assert "deck is not running" in capsys.readouterr().err


async def test_cli_state_prints_pretty_json(make_daemon, deck, runtime_dir, capsys):
    daemon = make_daemon()
    async with running(daemon, deck, runtime_dir / "deck.sock"):
        daemon.store.set("mac.volume", 40)
        assert await asyncio.to_thread(main, ["state"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["state"]["mac.volume"] == 40
    assert out.startswith("{\n  ")  # indented


async def test_cli_press_and_reload(make_daemon, deck, runtime_dir, calls, config_path, capsys):
    daemon = make_daemon()
    async with running(daemon, deck, runtime_dir / "deck.sock"):
        assert await asyncio.to_thread(main, ["press", "home", "0", "--long"]) == 0
        assert json.loads(capsys.readouterr().out) == {"ok": True}
        assert calls == [Action(type="mac.app", args="Terminal")]

        assert await asyncio.to_thread(main, ["press", "home", "0"]) == 0
        capsys.readouterr()
        assert calls[-1] == Action(type="mac.app", args="Ghostty")

        assert await asyncio.to_thread(main, ["press", "nope", "0"]) == 1
        assert json.loads(capsys.readouterr().out) == {
            "ok": False,
            "error": "unknown page 'nope'",
        }

        assert await asyncio.to_thread(main, ["reload"]) == 0
        assert json.loads(capsys.readouterr().out) == {"ok": True, "errors": []}

        config_path.write_text("pages: [")
        assert await asyncio.to_thread(main, ["reload"]) == 1
        printed = json.loads(capsys.readouterr().out)
        assert printed["ok"] is False
        assert printed["errors"]
