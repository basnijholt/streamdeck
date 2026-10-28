import asyncio
import contextlib
import json
import socket
import sys

import pytest

from deck.remote.server import ALLOWED_KINDS, MAX_LINE_BYTES, RemoteError, serve

BAD_REQUEST = {"ok": False, "error": "bad request"}


class StubExecutor:
    def __init__(self, handler=None):
        self.handler = handler
        self.hellos = []
        self.calls = []

    def configure(self, hello):
        self.hellos.append(hello)

    async def run(self, kind, args):
        self.calls.append((kind, args))
        if self.handler is None:
            return None
        return await self.handler(kind, args)


class StubPoller:
    """Answers each `snapshot()` with the next item (an exception is raised); the last repeats."""

    def __init__(self, snapshots=({},)):
        self.snapshots = list(snapshots)
        self.calls = 0

    async def snapshot(self):
        item = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


class Link:
    """The PC's end of a socket pair whose other end is served by `serve`."""

    def __init__(self, reader, writer, serve_task):
        self.reader = reader
        self.writer = writer
        self.serve_task = serve_task

    async def send_raw(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def send(self, message) -> None:
        await self.send_raw(json.dumps(message).encode() + b"\n")

    async def recv(self):
        return json.loads(await asyncio.wait_for(self.reader.readline(), 2))

    async def start(self, hello=None):
        """Say hello and take the first snapshot off the line."""
        await self.send({"hello": hello or {}})
        return await self.recv()

    async def assert_silent(self, seconds: float = 0.1) -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(self.reader.readline(), seconds)


@contextlib.asynccontextmanager
async def running(executor=None, poller=None, interval=0.01):
    executor = executor or StubExecutor()
    poller = poller or StubPoller()
    server_sock, pc_sock = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=server_sock)
    pc_reader, pc_writer = await asyncio.open_connection(sock=pc_sock, limit=8 << 20)
    task = asyncio.create_task(serve(reader, writer, executor, poller, interval))
    try:
        yield Link(pc_reader, pc_writer, task)
    finally:
        pc_writer.close()
        await asyncio.wait_for(task, 2)  # serve returns once the PC hangs up
        writer.close()


def test_allowed_kinds_are_the_mac_kinds():
    assert ALLOWED_KINDS == {
        "app", "url", "tab", "audio.output", "audio.input", "mic.mute",
        "volume", "screenshot", "km", "script", "lock", "active_url",
    }  # fmt: skip


# --- state ------------------------------------------------------------------


async def test_snapshot_then_diffs():
    poller = StubPoller([{"a": 1, "b": 2}, {"a": 1, "b": 2}, {"a": 1, "b": 3}])
    async with running(poller=poller) as link:
        await link.send({"hello": {}})
        assert await link.recv() == {"state": {"a": 1, "b": 2}}
        assert await link.recv() == {"state": {"b": 3}}
        await link.assert_silent()  # nothing changed since


async def test_new_key_is_sent_and_empty_first_snapshot_still_answers():
    async with running(poller=StubPoller([{}, {}, {"c": [1]}])) as link:
        await link.send({"hello": {}})
        assert await link.recv() == {"state": {}}
        assert await link.recv() == {"state": {"c": [1]}}


async def test_no_state_before_hello_then_configure_and_snapshot():
    poller, executor = StubPoller([{"a": 1}]), StubExecutor()
    hello = {"audio": {"outputs": [], "inputs": ["Q9"], "pin_input": True}}
    async with running(executor, poller) as link:
        await link.assert_silent()
        assert poller.calls == 0
        await link.send({"hello": hello})
        assert await link.recv() == {"state": {"a": 1}}
        assert executor.hellos == [hello]


async def test_request_before_hello_configures_with_an_empty_hello_and_is_answered():
    executor = StubExecutor(handler=lambda kind, args: asyncio.sleep(0, args))
    async with running(executor, StubPoller([{"a": 1}])) as link:
        await link.send({"id": 1, "kind": "app", "args": "Ghostty"})
        lines = [await link.recv(), await link.recv()]
        assert {"id": 1, "ok": True, "result": "Ghostty"} in lines
        assert {"state": {"a": 1}} in lines
        assert executor.hellos == [{}]
        assert executor.calls == [("app", "Ghostty")]


async def test_malformed_lines_before_hello_do_not_start_the_state_loop():
    poller = StubPoller()
    async with running(poller=poller) as link:
        await link.send_raw(b"nope\n")
        assert await link.recv() == BAD_REQUEST
        await link.assert_silent()
        assert poller.calls == 0


async def test_second_hello_reconfigures_without_a_new_snapshot():
    executor = StubExecutor()
    async with running(executor, StubPoller([{"a": 1}])) as link:
        await link.start({"n": 1})
        await link.send({"hello": {"n": 2}})
        await link.assert_silent()
        assert executor.hellos == [{"n": 1}, {"n": 2}]


async def test_hello_that_is_not_an_object_is_a_bad_request():
    executor = StubExecutor()
    async with running(executor) as link:
        await link.send({"hello": 3})
        assert await link.recv() == BAD_REQUEST
        assert executor.hellos == []


async def test_failing_configure_is_reported_and_the_server_keeps_going():
    class Executor(StubExecutor):
        def configure(self, hello):
            raise RuntimeError("bad hello")

    async with running(Executor(), StubPoller([{"a": 1}])) as link:
        await link.send({"hello": {}})
        lines = [await link.recv(), await link.recv()]
        assert {"ok": False, "error": "internal error"} in lines
        assert {"state": {"a": 1}} in lines


async def test_poller_failure_is_logged_and_retried(caplog):
    poller = StubPoller([RuntimeError("blueutil is gone"), {"a": 1}])
    async with running(poller=poller) as link:
        await link.send({"hello": {}})
        assert await link.recv() == {"state": {"a": 1}}
    assert any(r.exc_info and "blueutil is gone" in str(r.exc_info[1]) for r in caplog.records)


async def test_unserializable_snapshot_is_logged_and_retried():
    poller = StubPoller([{"a": object()}, {"a": 1}])
    async with running(poller=poller) as link:
        await link.send({"hello": {}})
        assert await link.recv() == {"state": {"a": 1}}


async def test_a_finished_request_makes_the_next_push_the_full_state():
    # The PC may show an optimistic value the Mac never took: the unchanged state is sent again.
    async with running(poller=StubPoller([{"a": 1, "b": 2}])) as link:
        assert await link.start() == {"state": {"a": 1, "b": 2}}
        await link.assert_silent()
        await link.send({"id": 1, "kind": "app", "args": "Ghostty"})
        assert await link.recv() == {"id": 1, "ok": True, "result": None}
        assert await link.recv() == {"state": {"a": 1, "b": 2}}
        await link.assert_silent()  # and only once


async def test_a_failed_request_makes_the_next_push_the_full_state():
    async def handler(kind, args):
        raise RemoteError("boom")

    async with running(StubExecutor(handler), StubPoller([{"a": 1}])) as link:
        await link.start()
        await link.send({"id": 1, "kind": "app", "args": "Ghostty"})
        assert await link.recv() == {"id": 1, "ok": False, "error": "boom"}
        assert await link.recv() == {"state": {"a": 1}}


async def test_a_poll_that_began_before_a_request_finished_is_not_pushed():
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowPoller:
        """The second poll starts before the request and answers (stale) only after it."""

        def __init__(self):
            self.calls = 0

        async def snapshot(self):
            self.calls += 1
            if self.calls == 2:
                entered.set()
                await release.wait()
                return {"v": 1}
            return {"v": 1} if self.calls == 1 else {"v": 2}

    async with running(poller=SlowPoller()) as link:
        assert await link.start() == {"state": {"v": 1}}
        await asyncio.wait_for(entered.wait(), 2)
        await link.send({"id": 1, "kind": "app", "args": "Ghostty"})
        assert await link.recv() == {"id": 1, "ok": True, "result": None}
        release.set()
        assert await link.recv() == {"state": {"v": 2}}  # not the stale {"v": 1}


# --- requests ---------------------------------------------------------------


async def test_request_ok_returns_result_and_null():
    async def handler(kind, args):
        return {"volume": 42} if kind == "volume" else None

    async with running(StubExecutor(handler)) as link:
        await link.start()
        await link.send({"id": 1, "kind": "volume", "args": 42})
        assert await link.recv() == {"id": 1, "ok": True, "result": {"volume": 42}}
        await link.send({"id": 2, "kind": "lock"})
        assert await link.recv() == {"id": 2, "ok": True, "result": None}


async def test_remote_error_is_reported_and_other_errors_are_not_leaked(caplog, capsys):
    async def handler(kind, args):
        if kind == "app":
            raise RemoteError("boom")
        raise ValueError("secret detail")

    async with running(StubExecutor(handler)) as link:
        await link.start()
        await link.send({"id": 2, "kind": "app", "args": "X"})
        assert await link.recv() == {"id": 2, "ok": False, "error": "boom"}
        await link.send({"id": 3, "kind": "lock"})
        assert await link.recv() == {"id": 3, "ok": False, "error": "internal error"}
    assert any(r.exc_info and "secret detail" in str(r.exc_info[1]) for r in caplog.records)
    assert capsys.readouterr().out == ""  # stdout is the protocol channel


@pytest.mark.parametrize("result", [object(), float("nan")])
async def test_result_json_cannot_hold_is_an_internal_error_and_serving_continues(result):
    async def handler(kind, args):
        return result if kind == "app" else "fine"

    async with running(StubExecutor(handler)) as link:
        await link.start()
        await link.send({"id": 1, "kind": "app", "args": "X"})
        assert await link.recv() == {"id": 1, "ok": False, "error": "internal error"}
        await link.send({"id": 2, "kind": "lock"})
        assert await link.recv() == {"id": 2, "ok": True, "result": "fine"}


async def test_disallowed_kind_is_refused_without_calling_the_executor():
    executor = StubExecutor()
    async with running(executor) as link:
        await link.start()
        await link.send({"id": 3, "kind": "shell", "args": "rm -rf /"})
        assert await link.recv() == {"id": 3, "ok": False, "error": "kind not allowed: shell"}
        assert executor.calls == []


@pytest.mark.parametrize("kind", ["audio", "mac.app", "App", "", " app", "__import__"])
async def test_allowlist_is_exact(kind):
    executor = StubExecutor()
    async with running(executor) as link:
        await link.start()
        await link.send({"id": 1, "kind": kind})
        assert await link.recv() == {"id": 1, "ok": False, "error": f"kind not allowed: {kind}"}
        assert executor.calls == []


@pytest.mark.parametrize("kind", sorted(ALLOWED_KINDS - {"url", "tab"}))
async def test_every_other_allowed_kind_reaches_the_executor(kind):
    executor = StubExecutor()
    async with running(executor) as link:
        await link.start()
        await link.send({"id": 1, "kind": kind, "args": {"x": 1}})
        assert await link.recv() == {"id": 1, "ok": True, "result": None}
        assert executor.calls == [(kind, {"x": 1})]


async def test_bad_json_keeps_serving():
    async with running() as link:
        await link.start()
        for garbage in (
            b"{nope\n",
            b"[1, 2]\n",
            b'"text"\n',
            b"\xff\xfe\n",
            b"[" * 200_000 + b"\n",
        ):
            await link.send_raw(garbage)
            assert await link.recv() == BAD_REQUEST
        await link.send_raw(b"\n  \n")  # blank lines are ignored
        await link.send({"id": 9, "kind": "lock"})
        assert await link.recv() == {"id": 9, "ok": True, "result": None}


@pytest.mark.parametrize(
    "message, expected",
    [
        ({"kind": "lock"}, BAD_REQUEST),  # nothing to answer to
        ({"id": "7", "kind": "lock"}, BAD_REQUEST),
        ({"id": True, "kind": "lock"}, BAD_REQUEST),
        ({"id": 7}, {"id": 7, "ok": False, "error": "bad request"}),
        ({"id": 7, "kind": ["app"]}, {"id": 7, "ok": False, "error": "bad request"}),
        ({"foo": 1}, BAD_REQUEST),
    ],
)
async def test_malformed_request_is_a_bad_request(message, expected):
    executor = StubExecutor()
    async with running(executor) as link:
        await link.start()
        await link.send(message)
        assert await link.recv() == expected
        assert executor.calls == []


@pytest.mark.parametrize(
    "args",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ssh://host",
        "http:",
        "http://",
        "-a Terminal",
        "https://a.b/ x",
        "https://a.b/\n",
        "http://[::1",
        "",
        None,
        42,
        ["https://a.b"],
        {"url": "https://a.b"},
    ],
)
async def test_url_scheme_rechecked(args):
    executor = StubExecutor()
    async with running(executor) as link:
        await link.start()
        await link.send({"id": 4, "kind": "url", "args": args})
        reply = await link.recv()
        assert reply["id"] == 4 and reply["ok"] is False
        assert "http" in reply["error"]
        assert executor.calls == []


@pytest.mark.parametrize(
    "args",
    [
        {"match": "x", "url": "file:///etc/passwd"},
        {"match": "x", "url": "javascript:alert(1)"},
        {"match": "x"},
        {"match": "x", "url": None},
        "https://a.b",
        None,
    ],
)
async def test_tab_url_rechecked(args):
    executor = StubExecutor()
    async with running(executor) as link:
        await link.start()
        await link.send({"id": 5, "kind": "tab", "args": args})
        reply = await link.recv()
        assert reply["id"] == 5 and reply["ok"] is False
        assert "http" in reply["error"]
        assert executor.calls == []


async def test_http_and_https_urls_pass_through():
    executor = StubExecutor()
    tab = {"match": "mail.google.com", "url": "https://mail.google.com", "navigate": False}
    async with running(executor) as link:
        await link.start()
        await link.send({"id": 1, "kind": "url", "args": "http://a.b/c?d=e"})
        await link.send({"id": 2, "kind": "url", "args": "HTTPS://a.b"})
        await link.send({"id": 3, "kind": "tab", "args": tab})
        replies = sorted([await link.recv() for _ in range(3)], key=lambda m: m["id"])
        assert [m["ok"] for m in replies] == [True, True, True]
        assert sorted(executor.calls, key=str) == sorted(
            [("url", "http://a.b/c?d=e"), ("url", "HTTPS://a.b"), ("tab", tab)], key=str
        )


# --- concurrency and teardown -----------------------------------------------


async def test_requests_run_concurrently_and_a_slow_one_blocks_nothing():
    gate = asyncio.Event()

    async def handler(kind, args):
        if kind == "app":
            await gate.wait()
        return args

    poller = StubPoller([{"n": 0}, {"n": 1}])
    async with running(StubExecutor(handler), poller) as link:
        assert await link.start() == {"state": {"n": 0}}
        await link.send({"id": 1, "kind": "app", "args": "slow"})
        await link.send({"id": 2, "kind": "volume", "args": 5})
        lines = [await link.recv(), await link.recv()]  # the fast answer and the state push
        assert {"id": 2, "ok": True, "result": 5} in lines
        assert {"state": {"n": 1}} in lines
        gate.set()
        assert await link.recv() == {"id": 1, "ok": True, "result": "slow"}  # out of order


async def test_concurrent_responses_never_interleave_within_a_line():
    big = "x" * 200_000

    async def handler(kind, args):
        await asyncio.sleep(0)
        return big + str(args)

    async with running(StubExecutor(handler)) as link:
        await link.start()
        for i in range(25):
            await link.send({"id": i, "kind": "app", "args": i})
        replies = [await link.recv() for _ in range(25)]
        assert {m["id"]: m["result"] for m in replies} == {i: big + str(i) for i in range(25)}


async def test_hangup_cancels_requests_and_stops_polling():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(kind, args):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    poller = StubPoller()
    async with running(StubExecutor(handler), poller) as link:
        await link.start()
        await link.send({"id": 1, "kind": "app", "args": "hangs"})
        await asyncio.wait_for(started.wait(), 2)
        link.writer.close()
        await asyncio.wait_for(link.serve_task, 2)
    assert cancelled.is_set()
    polled = poller.calls
    await asyncio.sleep(0.05)
    assert poller.calls == polled


async def test_hangup_with_no_hello_returns():
    async with running():
        pass


# --- line length ------------------------------------------------------------


async def test_oversized_line_is_rejected_at_once_and_serving_continues():
    async with running() as link:
        await link.start()
        await link.send_raw(b"x" * (MAX_LINE_BYTES + 10))  # no newline yet
        assert await link.recv() == BAD_REQUEST
        await link.send_raw(b"y" * 5000 + b"\n")  # the tail of that line is dropped
        await link.send({"id": 7, "kind": "lock"})
        assert await link.recv() == {"id": 7, "ok": True, "result": None}
        await link.assert_silent()  # one rejection, not one per chunk


async def test_oversized_line_arriving_whole_is_rejected_once():
    async with running() as link:
        await link.start()
        await link.send_raw(b"x" * (MAX_LINE_BYTES + 10) + b"\n")
        await link.send({"id": 7, "kind": "lock"})
        assert await link.recv() == BAD_REQUEST
        assert await link.recv() == {"id": 7, "ok": True, "result": None}


async def test_long_but_legal_line_is_served():
    seen = []

    async def handler(kind, args):
        seen.append(len(args))

    args = "x" * (MAX_LINE_BYTES // 2)
    async with running(StubExecutor(handler)) as link:
        await link.start()
        await link.send({"id": 1, "kind": "app", "args": args})
        assert await link.recv() == {"id": 1, "ok": True, "result": None}
        assert seen == [len(args)]


# --- stdio ------------------------------------------------------------------

STDIO_SERVER = """
import asyncio
from deck.remote.server import serve_stdio

class Executor:
    def configure(self, hello):
        self.hello = hello
    async def run(self, kind, args):
        return [kind, args, self.hello]

class Poller:
    async def snapshot(self):
        return {"up": True}

asyncio.run(serve_stdio(Executor(), Poller()))
"""


async def test_serve_stdio_speaks_the_protocol_on_stdin_and_stdout():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", STDIO_SERVER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        proc.stdin.write(b'{"hello": {"h": 1}}\n{"id": 1, "kind": "app", "args": "Ghostty"}\n')
        await proc.stdin.drain()
        lines = [json.loads(await asyncio.wait_for(proc.stdout.readline(), 10)) for _ in range(2)]
        assert {"state": {"up": True}} in lines
        assert {"id": 1, "ok": True, "result": ["app", "Ghostty", {"h": 1}]} in lines
        proc.stdin.close()  # EOF: the server returns
        assert await asyncio.wait_for(proc.wait(), 10) == 0
        assert await proc.stdout.read() == b""
        assert await proc.stderr.read() == b""
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
