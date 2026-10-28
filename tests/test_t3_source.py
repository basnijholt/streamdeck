import asyncio
import contextlib
import copy
import json
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from deck.config import T3Server
from deck.state import StateStore
from deck.t3 import source as source_module
from deck.t3.model import Thread, thread_state
from deck.t3.source import ModelSelection, T3AuthError, T3Client, T3Error, parse_thread

FIXTURES = Path(__file__).parent / "fixtures" / "t3"
FIXTURE_ID = "1cca8a80-c3a4-48a5-bfe0-87c810cb2f91"  # completed, settled, claudeAgent at xhigh
RUNNING_ID = "13e54142-7a54-4090-ac59-017010bc35ec"
ENV_ID = "6cd1637d-2c2f-4fa6-8cd2-fa04748043e2"
WEB_URL = "https://t3.lab.nijho.lt"
TOKEN = "T"
ALL_STATUSES = {
    "idle",
    "preparing",
    "queued",
    "starting",
    "running",
    "waiting",
    "completed",
    "interrupted",
    "failed",
    "cancelled",
    "rolled_back",
}
ANY = "<any>"  # a fixture value the client generates: any non-empty string matches


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def snapshot_threads() -> list[dict[str, Any]]:
    """The thread shells of the recorded authoritative snapshot."""
    return fixture("shell_snapshot.json")["values"][0]["snapshot"]["threads"]


def shell(thread_id: str = FIXTURE_ID, **changes: Any) -> dict[str, Any]:
    """A recorded thread shell, with `changes` applied."""
    raw = next(t for t in snapshot_threads() if t["id"] == thread_id)
    return {**copy.deepcopy(raw), **changes}


def thread_updated(raw: dict[str, Any], sequence: int = 10**7) -> dict[str, Any]:
    """A `thread.updated` chunk shaped like the recorded one."""
    frame = fixture("shell_thread_updated.json")
    frame["values"] = [{**frame["values"][0], "sequence": sequence, "thread": raw}]
    return frame


def _matches(expected: Any, actual: Any) -> bool:
    if expected == ANY:
        return isinstance(actual, str) and actual != ""
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and expected.keys() == actual.keys()
            and all(_matches(expected[k], actual[k]) for k in expected)
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(_matches(e, a) for e, a in zip(expected, actual, strict=True))
        )
    return type(expected) is type(actual) and expected == actual


class StubT3:
    """T3 Code's WebSocket RPC endpoint (`/ws`), replaying recorded frames.

    `silent` makes it accept connections and then say nothing at all, not even Pong.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.acks = 0
        self.connections = 0
        self.reject_dispatch = False
        self.silent = False
        self.subscribers: dict[Any, str] = {}  # connection -> request id of its subscribeShell
        self.url = ""
        self.port = 0
        self._server: Any = None

    async def start(self) -> None:
        self._server = await serve(
            self._handle, "127.0.0.1", self.port, process_request=self._check_upgrade
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()
        self.subscribers.clear()

    def _check_upgrade(self, connection: Any, request: Any) -> Any:
        parts = urlsplit(request.path)
        if parts.path != "/ws" or parse_qs(parts.query).get("orchestrationProtocol") != ["2"]:
            recorded = fixture("upgrade_no_protocol.json")
            return connection.respond(HTTPStatus(recorded["status"]), recorded["body"])
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            recorded = fixture("upgrade_bad_token.json")
            return connection.respond(HTTPStatus(recorded["status"]), recorded["body"])
        return None

    async def _handle(self, ws: Any) -> None:
        self.connections += 1
        try:
            async for raw in ws:
                if self.silent:
                    continue
                message = json.loads(raw)
                if message["_tag"] == "Ping":
                    await ws.send(json.dumps({"_tag": "Pong"}))
                elif message["_tag"] == "Ack":
                    self.acks += 1
                elif message["_tag"] == "Request":
                    self.requests.append(message)
                    await self._answer(ws, message)
        except ConnectionClosed:
            pass
        finally:
            self.subscribers.pop(ws, None)

    async def _answer(self, ws: Any, request: dict[str, Any]) -> None:
        request_id = request["id"]
        match request["tag"]:
            case "orchestration.subscribeShell":
                self.subscribers[ws] = request_id
                await ws.send(
                    json.dumps({**fixture("shell_snapshot.json"), "requestId": request_id})
                )
            case "orchestration.getThreadFamily":
                family = fixture("thread_family.json")
                ids = {t["id"] for t in family["exit"]["value"]["threads"]}
                if request["payload"]["threadId"] not in ids:
                    family = fixture("thread_family_unknown.json")
                await ws.send(json.dumps({**family, "requestId": request_id}))
            case "orchestration.dispatchCommand":
                name = (
                    "dispatch_command_failure"
                    if self.reject_dispatch
                    else "dispatch_command_success"
                )
                await ws.send(json.dumps({**fixture(f"{name}.json"), "requestId": request_id}))

    async def push(self, frame: dict[str, Any]) -> None:
        """Send a shell stream chunk to every subscriber."""
        for ws, request_id in list(self.subscribers.items()):
            await ws.send(json.dumps({**frame, "requestId": request_id}))

    def last_request_matches(self, name: str) -> bool:
        return _matches(fixture(name), self.requests[-1])


class FakeSleep:
    """Records the backoff delays and returns at once; after `hold_after` calls it never returns."""

    def __init__(self, hold_after: int | None = None) -> None:
        self.delays: list[float] = []
        self.hold_after = hold_after

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self.hold_after is not None and len(self.delays) >= self.hold_after:
            await asyncio.Event().wait()
        await asyncio.sleep(0)


async def eventually(check, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


def server_config(url: str, web_url: str = WEB_URL) -> T3Server:
    return T3Server(
        name="pc", url=url, web_url=web_url, environment_id=ENV_ID, token_env="T3_TOKEN"
    )


@contextlib.asynccontextmanager
async def watching(client: T3Client, store: StateStore, interval: float = 1.0):
    """Run `client.watch(store)` for the duration of the block, then cancel it."""
    task = asyncio.create_task(client.watch(store, interval=interval))
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
async def stub():
    stub = StubT3()
    await stub.start()
    yield stub
    await stub.stop()


@pytest.fixture
def client(stub) -> T3Client:
    return T3Client(server_config(stub.url), TOKEN)


@pytest.fixture
def store() -> StateStore:
    return StateStore()


def threads_of(store: StateStore) -> dict[str, Thread]:
    return {t.thread_id: t for t in store.get("t3.threads.pc") or []}


# --- list_threads -------------------------------------------------------------


async def test_list_threads_parses_fixture(stub, client):
    threads = await client.list_threads()
    t = next(t for t in threads if t.thread_id == FIXTURE_ID)
    assert (
        t.status in ALL_STATUSES
        and t.instance_id == "claudeAgent"
        and ("effort", "xhigh") in t.options
    )


async def test_list_threads_maps_every_field(stub, client):
    raw = shell()
    threads = await client.list_threads()
    t = next(t for t in threads if t.thread_id == FIXTURE_ID)
    assert t == Thread(
        server="pc",
        thread_id=FIXTURE_ID,
        project_id=raw["projectId"],
        title=raw["title"],
        status="completed",
        pending_requests=0,
        model="claude-opus-5-5",
        instance_id="claudeAgent",
        options=(("effort", "xhigh"), ("fastMode", False)),
        settled=True,
        archived=False,
        parent_thread_id=None,
        updated_at=datetime.fromisoformat(raw["updatedAt"]),
    )
    assert t.updated_at.tzinfo is not None
    running = next(t for t in threads if t.thread_id == RUNNING_ID)
    assert (running.status, running.settled, thread_state(running)) == ("running", False, "working")


async def test_list_threads_takes_only_the_authoritative_snapshot(stub, client):
    # The recorded chunk holds the snapshot and then a project-only refresh with no threads.
    threads = await client.list_threads()
    assert len(threads) == len(snapshot_threads()) > 0


async def test_list_threads_asks_for_the_compact_shell(stub, client):
    await client.list_threads()
    expected = fixture("subscribe_shell.json")
    request = stub.requests[0]
    assert (request["tag"], request["payload"]) == (expected["tag"], expected["payload"])


async def test_list_threads_rejected_token_raises_auth_error(stub):
    client = T3Client(server_config(stub.url), "wrong")
    with pytest.raises(T3AuthError, match="invalid_credential"):
        await client.list_threads()


async def test_list_threads_server_down_raises(stub):
    await stub.stop()
    with pytest.raises(T3Error, match="pc"):
        await T3Client(server_config(stub.url), TOKEN).list_threads()


# --- parse_thread ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_", "status", "pending"),
    [
        (None, "idle", 0),
        ({"kind": "user_input", "blocking": True}, "running", 1),
        ({"kind": "user_input"}, "running", 1),  # servers before `blocking` existed
        ({"kind": "user_input", "blocking": False}, "running", 0),  # the run keeps going
        ({"kind": "user_input", "blocking": False}, "completed", 1),  # ... unless it stopped
        ({"kind": "command"}, "running", 1),  # an approval
        ({"kind": "auth_refresh"}, "running", 0),
    ],
)
def test_parse_thread_counts_requests_that_need_you(request_, status, pending):
    summary = None
    if request_ is not None:
        summary = {"id": "r1", "createdAt": "2026-10-28T19:00:00.000Z", **request_}
    t = parse_thread("pc", shell(status=status, pendingRuntimeRequest=summary))
    assert t.pending_requests == pending


def test_parse_thread_prefers_the_activity_status():
    t = parse_thread("pc", shell(status="idle", activityRunStatus="waiting"))
    assert t.status == "waiting"


def test_parse_thread_reads_lineage_and_archive():
    t = parse_thread(
        "pc",
        shell(
            lineage={
                "parentThreadId": RUNNING_ID,
                "relationshipToParent": "subagent",
                "rootThreadId": RUNNING_ID,
            },
            archivedAt="2026-10-28T19:00:00.000Z",
            settledOverride="active",
        ),
    )
    assert (t.parent_thread_id, t.archived, t.settled) == (RUNNING_ID, True, False)


def test_parse_thread_without_options():
    selection = {"instanceId": "codex", "model": "gpt-5.5"}
    t = parse_thread("pc", shell(modelSelection=selection))
    assert (t.instance_id, t.model, t.options) == ("codex", "gpt-5.5", ())


def test_parse_thread_rejects_a_malformed_shell():
    raw = shell()
    del raw["modelSelection"]
    with pytest.raises(T3Error):
        parse_thread("pc", raw)


# --- model selection ------------------------------------------------------------


async def test_get_model_selection_reads_the_thread(stub, client):
    selection = await client.get_model_selection(FIXTURE_ID)
    assert selection == ModelSelection(
        "claudeAgent", "claude-opus-5-5", (("effort", "xhigh"), ("fastMode", False))
    )
    assert stub.requests[-1]["tag"] == "orchestration.getThreadFamily"
    assert stub.requests[-1]["payload"] == {"threadId": FIXTURE_ID}


async def test_get_model_selection_unknown_thread_raises(stub, client):
    with pytest.raises(T3Error, match="not found"):
        await client.get_model_selection("00000000-0000-4000-8000-000000000000")


async def test_set_model_selection_sends_full_options(stub, client):
    await client.set_model_selection(
        FIXTURE_ID,
        ModelSelection("claudeAgent", "claude-opus-5-5", (("effort", "high"), ("fastMode", False))),
    )
    assert stub.last_request_matches("set_model_selection.json")


async def test_set_model_selection_uses_a_new_command_id_each_time(stub, client):
    selection = ModelSelection("claudeAgent", "claude-opus-5-5", (("effort", "high"),))
    await client.set_model_selection(FIXTURE_ID, selection)
    await client.set_model_selection(FIXTURE_ID, selection)
    first, second = (r["payload"]["commandId"] for r in stub.requests[-2:])
    assert first != second


async def test_set_model_selection_rejected_raises_the_server_message(stub, client):
    stub.reject_dispatch = True
    with pytest.raises(T3Error, match="No orchestration projection exists"):
        await client.set_model_selection(
            "00000000-0000-4000-8000-000000000000",
            ModelSelection("claudeAgent", "claude-opus-5-5", (("effort", "high"),)),
        )


# --- watch ----------------------------------------------------------------------


async def test_watch_mirrors_the_threads_of_this_server(stub, client, store):
    async with watching(client, store):
        await eventually(lambda: store.get("t3.online.pc") is True)
        assert set(store.snapshot()) == {"t3.online.pc", "t3.threads.pc"}
        assert len(threads_of(store)) == len(snapshot_threads())
        assert threads_of(store)[FIXTURE_ID].options == (("effort", "xhigh"), ("fastMode", False))
    assert stub.acks >= 1  # every chunk is acknowledged, or the server stops sending


async def test_watch_applies_thread_updates(stub, client, store):
    async with watching(client, store):
        await eventually(lambda: store.get("t3.online.pc") is True)
        await stub.push(fixture("shell_thread_updated.json"))  # recorded: the no-op set
        selection = {
            "instanceId": "claudeAgent",
            "model": "claude-opus-5-5",
            "options": [{"id": "effort", "value": "high"}],
        }
        await stub.push(
            thread_updated(shell(modelSelection=selection, status="running", settledOverride=None))
        )
        await eventually(lambda: threads_of(store)[FIXTURE_ID].status == "running")
        t = threads_of(store)[FIXTURE_ID]
        assert (t.options, t.settled) == ((("effort", "high"),), False)


async def test_watch_shows_a_thread_that_needs_you(stub, client, store):
    async with watching(client, store):
        await eventually(lambda: store.get("t3.online.pc") is True)
        summary = {"id": "r1", "kind": "permission", "createdAt": "2026-10-28T19:00:00.000Z"}
        await stub.push(thread_updated(shell(RUNNING_ID, pendingRuntimeRequest=summary)))
        await eventually(lambda: thread_state(threads_of(store)[RUNNING_ID]) == "needs_you")


async def test_watch_drops_removed_and_archived_threads(stub, client, store):
    async with watching(client, store):
        await eventually(lambda: store.get("t3.online.pc") is True)
        removed = {
            "kind": "thread.removed",
            "sequence": 10**7,
            "location": "active",
            "threadId": FIXTURE_ID,
        }
        archived = {**thread_updated(shell(RUNNING_ID))["values"][0], "location": "archive"}
        await stub.push({"_tag": "Chunk", "requestId": "", "values": [removed, archived]})
        await eventually(lambda: FIXTURE_ID not in threads_of(store))
        assert RUNNING_ID not in threads_of(store)


async def test_watch_reconnects_after_a_defect(stub, store):
    client = T3Client(server_config(stub.url), TOKEN, sleep=FakeSleep())
    async with watching(client, store):
        await eventually(lambda: store.get("t3.online.pc") is True)
        await stub.push({"_tag": "Defect", "defect": "boom"})  # ends every request
        await eventually(lambda: stub.connections == 2 and store.get("t3.online.pc") is True)


async def test_set_model_selection_fails_on_a_protocol_error(stub, client, monkeypatch):
    async def protocol_error(ws, request):
        await ws.send(json.dumps({"_tag": "ClientProtocolError", "error": {"_tag": "x"}}))

    monkeypatch.setattr(stub, "_answer", protocol_error)
    with pytest.raises(T3Error, match="did not understand"):
        await client.set_model_selection(FIXTURE_ID, ModelSelection("claudeAgent", "m", ()))


async def test_server_down_marks_offline(stub, store):
    sleep = FakeSleep(hold_after=1)
    client = T3Client(server_config(stub.url), TOKEN, sleep=sleep)
    async with watching(client, store):
        await eventually(lambda: store.get("t3.online.pc") is True)
        before = store.get("t3.threads.pc")
        await stub.stop()
        await eventually(lambda: store.get("t3.online.pc") is False)
        assert store.get("t3.threads.pc") == before  # the last list stays, dimmed by the view
        await eventually(lambda: sleep.delays == [1.0])


async def test_watch_backs_off_and_never_dies(stub, store):
    await stub.stop()
    sleep = FakeSleep(hold_after=7)
    client = T3Client(server_config(stub.url), TOKEN, sleep=sleep)
    async with watching(client, store) as task:
        await eventually(lambda: len(sleep.delays) == 7)
        assert sleep.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
        assert store.get("t3.online.pc") is False
        assert not task.done()


async def test_watch_reconnects_and_resets_the_backoff(stub, store):
    sleep = FakeSleep()
    client = T3Client(server_config(stub.url), TOKEN, sleep=sleep)
    async with watching(client, store):
        await eventually(lambda: store.get("t3.online.pc") is True)
        await stub.stop()
        await eventually(lambda: len(sleep.delays) >= 2)
        await stub.start()  # same port
        await eventually(lambda: store.get("t3.online.pc") is True)
        delays = len(sleep.delays)
        await stub.stop()
        await eventually(lambda: len(sleep.delays) > delays)
        assert sleep.delays[delays] == 1.0


async def test_watch_rejected_token_waits_the_longest(stub, store):
    sleep = FakeSleep(hold_after=1)
    client = T3Client(server_config(stub.url), "wrong", sleep=sleep)
    async with watching(client, store):
        await eventually(lambda: sleep.delays == [30.0])
        assert store.get("t3.online.pc") is False


async def test_watch_gives_up_on_a_silent_server(stub, store, monkeypatch):
    monkeypatch.setattr(source_module, "SNAPSHOT_TIMEOUT", 0.3)
    monkeypatch.setattr(source_module, "LIVENESS_TIMEOUT", 0.3)
    stub.silent = True
    sleep = FakeSleep(hold_after=1)
    client = T3Client(server_config(stub.url), TOKEN, sleep=sleep)
    async with watching(client, store, interval=0.05):
        await eventually(lambda: sleep.delays == [1.0])
        assert store.get("t3.online.pc") is False


async def test_watch_stays_online_while_pongs_arrive(stub, store, monkeypatch):
    monkeypatch.setattr(source_module, "LIVENESS_TIMEOUT", 0.3)
    sleep = FakeSleep()
    client = T3Client(server_config(stub.url), TOKEN, sleep=sleep)
    async with watching(client, store, interval=0.05):
        await eventually(lambda: store.get("t3.online.pc") is True)
        await asyncio.sleep(0.6)  # twice the liveness limit with no shell events at all
        assert store.get("t3.online.pc") is True
        assert sleep.delays == []


# --- URLs -----------------------------------------------------------------------


def make_thread(thread_id: str = FIXTURE_ID) -> Thread:
    return parse_thread("pc", shell(id=thread_id))


def test_thread_url_uses_web_url():
    client = T3Client(server_config("http://127.0.0.1:3773"), TOKEN)
    url = client.thread_url(make_thread())
    assert url.startswith("https://t3.lab.nijho.lt")
    assert url == f"{WEB_URL}/{ENV_ID}/{FIXTURE_ID}"


def test_thread_url_ignores_a_trailing_slash():
    client = T3Client(server_config("http://127.0.0.1:3773", web_url=WEB_URL + "/"), TOKEN)
    assert client.thread_url(make_thread()) == f"{WEB_URL}/{ENV_ID}/{FIXTURE_ID}"


def test_thread_id_from_url_roundtrip():
    client = T3Client(server_config("http://127.0.0.1:3773"), TOKEN)
    assert client.thread_id_from_url(client.thread_url(make_thread())) == FIXTURE_ID
    for url in [
        f"https://example.com/{ENV_ID}/{FIXTURE_ID}",  # other host
        f"{WEB_URL}/00000000-0000-4000-8000-000000000000/{FIXTURE_ID}",  # other environment
        f"{WEB_URL}/{ENV_ID}/{FIXTURE_ID}/diff",  # extra path segment
        f"{WEB_URL}/{ENV_ID}/{FIXTURE_ID}/",
        f"{WEB_URL}/{ENV_ID}",
        f"{WEB_URL}/{ENV_ID}/",
        f"{WEB_URL}/?thread={FIXTURE_ID}",  # query only
        f"{WEB_URL}?{ENV_ID}/{FIXTURE_ID}",
        f"{WEB_URL}/#/{ENV_ID}/{FIXTURE_ID}",
        "about:blank",
        "",
    ]:
        assert client.thread_id_from_url(url) is None, url


def test_thread_id_from_url_ignores_query_and_fragment_and_host_case():
    client = T3Client(server_config("http://127.0.0.1:3773"), TOKEN)
    assert (
        client.thread_id_from_url(f"https://T3.Lab.Nijho.lt/{ENV_ID}/{FIXTURE_ID}?tab=diff#end")
        == FIXTURE_ID
    )
