import asyncio
import contextlib
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from deck import cli
from deck import ha as ha_module
from deck.actions import ActionRunner
from deck.config import Action, Config, DialSpec, HAConfig
from deck.dials import DIAL_FACTORIES, Services
from deck.ha import HaBrightnessDial, HomeAssistant, entities_in
from deck.state import StateStore

GUEST = "light.guest_room"
HA_CONFIG = {"url": "http://ha.local:8123", "token_env": "HA_TOKEN"}


# --- stubs ------------------------------------------------------------------


class HttpStub:
    """Home Assistant's REST API in a thread: records POSTs, answers with `status`.

    Clear `gate` to make requests wait (up to 10 s) after they have been recorded.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, Any, str | None]] = []
        self.status = 200
        self.body = b"[]"
        self.headers: dict[str, str] = {}
        self.gate = threading.Event()
        self.gate.set()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"null")
                stub.requests.append((self.path, body, self.headers.get("Authorization")))
                stub.gate.wait(10)
                self.send_response(stub.status)
                for name, value in stub.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(stub.body)))
                self.end_headers()
                self.wfile.write(stub.body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.gate.set()
        self.server.shutdown()
        self.server.server_close()


class WsStub:
    """Home Assistant's WebSocket API: the auth handshake, `get_states` and `subscribe_events`."""

    def __init__(self, token: str = "T") -> None:
        self.token = token
        self.states: list[dict[str, Any]] = []
        self.fail_get_states = False
        self.received: list[dict[str, Any]] = []
        self.connections = 0
        self.clients: set[Any] = set()
        self.url = ""
        self._server: Any = None

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, ws: Any) -> None:
        self.connections += 1
        self.clients.add(ws)
        try:
            await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.1.0"}))
            auth = json.loads(await ws.recv())
            self.received.append(auth)
            if auth.get("access_token") != self.token:
                await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access"}))
                return
            await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.1.0"}))
            async for raw in ws:
                message = json.loads(raw)
                self.received.append(message)
                failed = message["type"] == "get_states" and self.fail_get_states
                reply: dict[str, Any] = {
                    "id": message["id"],
                    "type": "result",
                    "success": not failed,
                }
                if failed:
                    reply["error"] = {"code": "unknown_error", "message": "nope"}
                else:
                    reply["result"] = self.states if message["type"] == "get_states" else None
                await ws.send(json.dumps(reply))
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)

    async def push(self, entity_id: str, new_state: dict[str, Any] | None) -> None:
        event = {
            "event_type": "state_changed",
            "data": {"entity_id": entity_id, "new_state": new_state},
        }
        await self.push_raw(json.dumps({"id": 2, "type": "event", "event": event}))

    async def push_raw(self, text: str) -> None:
        for ws in list(self.clients):
            await ws.send(text)

    async def drop(self) -> None:
        await asyncio.gather(*(ws.close(1011) for ws in list(self.clients)))


def light(entity_id: str, state: str, **attributes: Any) -> dict[str, Any]:
    """An entity as Home Assistant sends it."""
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attributes,
        "last_changed": "2026-10-28T12:00:00+00:00",
        "context": {"id": "01HX", "parent_id": None, "user_id": None},
    }


def stored(state: str, **attributes: Any) -> dict[str, Any]:
    """What the store holds for an entity."""
    return {"state": state, "attributes": attributes}


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


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def running(ha: HomeAssistant):
    task = asyncio.create_task(ha.run())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def make_ha(url: str, store: StateStore, entities, sleep=None, token: str = "T") -> HomeAssistant:
    cfg = HAConfig(url=url, token_env="HA_TOKEN")
    if sleep is None:
        return HomeAssistant(cfg, token, store, entities)
    return HomeAssistant(cfg, token, store, entities, sleep=sleep)


def ha_keys(store: StateStore) -> set[str]:
    return {key for key in store.snapshot() if key.startswith("ha.")}


@pytest.fixture
def store() -> StateStore:
    return StateStore()


@pytest.fixture
def http_stub():
    stub = HttpStub()
    yield stub
    stub.close()


@pytest.fixture
async def ws_stub():
    stub = WsStub()
    await stub.start()
    yield stub
    await stub.stop()


@pytest.fixture
def ha(http_stub, store) -> HomeAssistant:
    return make_ha(http_stub.url, store, {GUEST})


# --- REST: call_service -----------------------------------------------------


async def test_call_service_posts(ha, http_stub):
    await ha.call_service("light.toggle", GUEST)
    assert http_stub.requests == [("/api/services/light/toggle", {"entity_id": GUEST}, "Bearer T")]


async def test_call_service_sends_the_data_next_to_the_entity(ha, http_stub):
    await ha.call_service("light.turn_on", GUEST, {"brightness_pct": 40, "transition": 2})
    assert http_stub.requests == [
        (
            "/api/services/light/turn_on",
            {"entity_id": GUEST, "brightness_pct": 40, "transition": 2},
            "Bearer T",
        )
    ]


async def test_call_service_keeps_the_base_path_of_the_url(http_stub, store):
    ha = make_ha(http_stub.url + "/ha/", store, set())
    await ha.call_service("switch.turn_off", "switch.fan")
    assert http_stub.requests[0][0] == "/ha/api/services/switch/turn_off"


@pytest.mark.parametrize("status", [401, 404, 500])
async def test_call_service_non_2xx_raises(ha, http_stub, status):
    http_stub.status = status
    http_stub.body = b'{"message": "Service not found."}'
    with pytest.raises(RuntimeError, match=rf"{status}.*Service not found") as excinfo:
        await ha.call_service("light.toggle", GUEST)
    assert "Bearer" not in str(excinfo.value)


async def test_call_service_does_not_follow_redirects(ha, http_stub):
    http_stub.status = 302
    http_stub.headers = {"Location": "/elsewhere"}
    with pytest.raises(RuntimeError, match="302"):
        await ha.call_service("light.toggle", GUEST)
    assert len(http_stub.requests) == 1  # the bearer token was not sent anywhere else


async def test_call_service_unreachable_raises(http_stub, store):
    ha = make_ha(http_stub.url, store, set())
    http_stub.close()
    with pytest.raises(RuntimeError, match="unreachable"):
        await ha.call_service("light.toggle", GUEST)


async def test_call_service_times_out(ha, http_stub, monkeypatch):
    monkeypatch.setattr(ha_module, "REQUEST_TIMEOUT", 0.2)
    http_stub.gate.clear()
    with pytest.raises(RuntimeError, match="unreachable"):
        await ha.call_service("light.toggle", GUEST)


@pytest.mark.parametrize("service", ["toggle", "light.", ".toggle", ""])
async def test_call_service_rejects_a_service_without_domain_and_name(ha, http_stub, service):
    with pytest.raises(ValueError, match="domain.name"):
        await ha.call_service(service, GUEST)
    assert http_stub.requests == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://ha.local", "ha.local:8123", ""])
def test_the_url_must_be_http_or_https(store, url):
    with pytest.raises(ValueError, match="http"):
        make_ha(url, store, set())


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://ha.local:8123", "ws://ha.local:8123/api/websocket"),
        ("https://ha.example.com/", "wss://ha.example.com/api/websocket"),
        ("HTTPS://ha.example.com/ha/", "wss://ha.example.com/ha/api/websocket"),
    ],
)
def test_the_websocket_url_follows_the_http_url(url, expected):
    assert ha_module.websocket_url(url) == expected


# --- WebSocket: state and events --------------------------------------------


async def test_states_and_events_update_store(ws_stub, store):
    ws_stub.states = [
        light(GUEST, "on", brightness=128, friendly_name="Guest room"),
        light("light.other", "off"),
        light("sensor.temp", "21.5"),
    ]
    ha = make_ha(ws_stub.url, store, {GUEST})
    async with running(ha):
        await eventually(lambda: store.get("ha.online") is True)
        assert store.get(f"ha.{GUEST}") == stored("on", brightness=128, friendly_name="Guest room")
        assert ha_keys(store) == {"ha.online", f"ha.{GUEST}"}  # only configured entities

        await ws_stub.push(GUEST, light(GUEST, "off"))
        await eventually(lambda: store.get(f"ha.{GUEST}") == stored("off"))
        await ws_stub.push("light.other", light("light.other", "on"))
        await ws_stub.push(GUEST, light(GUEST, "on", brightness=51))
        await eventually(lambda: store.get(f"ha.{GUEST}") == stored("on", brightness=51))
        assert ha_keys(store) == {"ha.online", f"ha.{GUEST}"}


async def test_the_protocol_sequence(ws_stub, store):
    ha = make_ha(ws_stub.url, store, {GUEST})
    async with running(ha):
        await eventually(lambda: store.get("ha.online") is True)
    assert ws_stub.received == [
        {"type": "auth", "access_token": "T"},
        {"id": 1, "type": "get_states"},
        {"id": 2, "type": "subscribe_events", "event_type": "state_changed"},
    ]


async def test_a_removed_entity_is_dropped_from_the_store(ws_stub, store):
    ws_stub.states = [light(GUEST, "on")]
    async with running(make_ha(ws_stub.url, store, {GUEST})):
        await eventually(lambda: store.get(f"ha.{GUEST}") is not None)
        await ws_stub.push(GUEST, None)
        await eventually(lambda: store.get(f"ha.{GUEST}") is None)
        assert store.get("ha.online") is True


async def test_ha_is_offline_until_connected_and_again_after_cancel(ws_stub, store):
    ws_stub.states = [light(GUEST, "on")]
    ha = make_ha(ws_stub.url, store, {GUEST})
    async with running(ha) as task:
        await settle()
        assert store.get("ha.online") is not None  # written as soon as the client runs
        await eventually(lambda: store.get("ha.online") is True)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert store.get("ha.online") is False
    assert store.get(f"ha.{GUEST}") == stored("on")  # still shown, dimmed by the views


async def test_a_large_states_message_is_accepted(ws_stub, store):
    ws_stub.states = [light(GUEST, "on"), light("sensor.big", "1", blob="x" * 3_000_000)]
    async with running(make_ha(ws_stub.url, store, {GUEST})):
        await eventually(lambda: store.get("ha.online") is True)
        assert store.get(f"ha.{GUEST}") == stored("on")


async def test_garbage_messages_are_ignored(ws_stub, store, caplog):
    ws_stub.states = [light(GUEST, "on")]
    with caplog.at_level(logging.WARNING):
        async with running(make_ha(ws_stub.url, store, {GUEST})):
            await eventually(lambda: store.get("ha.online") is True)
            await ws_stub.push_raw("not json")
            await ws_stub.push_raw("[1, 2]")
            await ws_stub.push_raw(json.dumps({"id": 2, "type": "event", "event": {"data": []}}))
            await ws_stub.push_raw(json.dumps({"id": 2, "type": "event", "event": None}))
            await ws_stub.push_raw(
                json.dumps({"id": 2, "type": "event", "event": {"data": {"new_state": {}}}})
            )
            await ws_stub.push(GUEST, light(GUEST, "off"))
            await eventually(lambda: store.get(f"ha.{GUEST}") == stored("off"))
    assert ws_stub.connections == 1
    assert all(r.exc_info is None for r in caplog.records)


# --- WebSocket: connection loss, backoff, authentication --------------------


async def test_reconnect_sets_offline(ws_stub, store):
    ws_stub.states = [light(GUEST, "on")]
    sleep = FakeSleep(hold_after=1)  # the first backoff never ends: the store shows the gap
    async with running(make_ha(ws_stub.url, store, {GUEST}, sleep=sleep)):
        await eventually(lambda: store.get("ha.online") is True)
        await ws_stub.drop()
        await eventually(lambda: store.get("ha.online") is False)
        assert store.get(f"ha.{GUEST}") == stored("on")
        assert sleep.delays == [1.0]
        assert ws_stub.connections == 1


async def test_reconnect_fetches_the_states_again(ws_stub, store):
    ws_stub.states = [light(GUEST, "on")]
    async with running(make_ha(ws_stub.url, store, {GUEST}, sleep=FakeSleep())):
        await eventually(lambda: store.get("ha.online") is True)
        ws_stub.states = [light(GUEST, "off")]  # changed while the connection was down
        await ws_stub.drop()
        await eventually(lambda: store.get(f"ha.{GUEST}") == stored("off"))
        await eventually(lambda: store.get("ha.online") is True)
        assert ws_stub.connections == 2


async def test_backoff_doubles_to_the_maximum_and_resets_after_a_connection(ws_stub, store):
    sleep = FakeSleep(hold_after=8)
    ha = make_ha(ws_stub.url, store, {GUEST}, sleep=sleep)
    await ws_stub.stop()  # nothing listens: every attempt fails
    try:
        async with running(ha):
            await eventually(lambda: len(sleep.delays) == 8)
    finally:
        await ws_stub.start()
    assert sleep.delays == [1, 2, 4, 8, 16, 30, 30, 30]


async def test_backoff_restarts_after_a_good_connection(ws_stub, store):
    sleep = FakeSleep(hold_after=2)
    async with running(make_ha(ws_stub.url, store, {GUEST}, sleep=sleep)):
        await eventually(lambda: store.get("ha.online") is True)
        await ws_stub.drop()
        await eventually(lambda: ws_stub.connections == 2 and store.get("ha.online") is True)
        await ws_stub.drop()
        await eventually(lambda: len(sleep.delays) == 2)
    assert sleep.delays == [1, 1]


async def test_auth_invalid_logs_an_error_and_backs_off_the_most(ws_stub, store, caplog):
    sleep = FakeSleep(hold_after=1)
    with caplog.at_level(logging.WARNING):
        async with running(make_ha(ws_stub.url, store, {GUEST}, sleep=sleep, token="wrong")):
            await eventually(lambda: len(sleep.delays) == 1)
    assert sleep.delays == [30.0]  # retrying with the same token cannot help: do not hammer
    assert store.get("ha.online") is False
    assert ws_stub.connections == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "token" in errors[0].message and errors[0].exc_info is None
    assert "wrong" not in caplog.text


async def test_a_failed_get_states_goes_offline_and_retries(ws_stub, store, caplog):
    ws_stub.fail_get_states = True
    sleep = FakeSleep(hold_after=2)
    with caplog.at_level(logging.WARNING):
        async with running(make_ha(ws_stub.url, store, {GUEST}, sleep=sleep)):
            await eventually(lambda: len(sleep.delays) == 2)
    assert store.get("ha.online") is False
    assert sleep.delays == [1, 2]
    assert "get_states" in caplog.text and all(r.exc_info is None for r in caplog.records)


async def test_a_server_that_is_not_home_assistant_is_retried_quietly(http_stub, store, caplog):
    sleep = FakeSleep(hold_after=1)
    with caplog.at_level(logging.WARNING):
        async with running(make_ha(http_stub.url, store, {GUEST}, sleep=sleep)):
            await eventually(lambda: len(sleep.delays) == 1)
    assert store.get("ha.online") is False
    assert [r.exc_info for r in caplog.records] == [None, None]


# --- WebSocket: entities follow the config ----------------------------------


async def test_config_reload_resyncs_the_store_from_the_cache(ws_stub, store, monkeypatch):
    monkeypatch.setattr(ha_module, "WATCH_INTERVAL", 0.02)
    ws_stub.states = [light(GUEST, "on"), light("light.desk", "off"), light("light.hall", "off")]
    wanted = {GUEST}
    ha = make_ha(ws_stub.url, store, lambda: set(wanted))
    async with running(ha):
        await eventually(lambda: store.get("ha.online") is True)
        await ws_stub.push("light.hall", light("light.hall", "on"))  # not referenced yet
        await settle()
        assert ha_keys(store) == {"ha.online", f"ha.{GUEST}"}

        wanted.clear()
        wanted.update({"light.desk", "light.hall"})  # a reload swapped the config
        await eventually(lambda: store.get("ha.light.hall") == stored("on"))
        assert store.get("ha.light.desk") == stored("off")
        assert ha_keys(store) == {"ha.online", "ha.light.desk", "ha.light.hall"}  # guest dropped

        await ws_stub.push("light.desk", light("light.desk", "on"))  # now tracked live
        await eventually(lambda: store.get("ha.light.desk") == stored("on"))
    assert [m["type"] for m in ws_stub.received].count("get_states") == 1  # no refetch needed


# --- entities_in ------------------------------------------------------------


def test_entities_in_collects_the_displayed_entities_of_every_page():
    config = Config.model_validate(
        {
            "home_assistant": HA_CONFIG,
            "pages": {
                "home": {
                    "keys": [
                        {
                            "label": "Guest",
                            "state": {"ha": GUEST},
                            "press": {"ha": {"service": "light.toggle", "entity_id": "light.x"}},
                        },
                        None,
                        {"label": "No state"},
                    ],
                    "dials": [{"kind": "ha.brightness", "entity_id": "light.desk"}, None],
                },
                "second": {
                    "keys": [{"state": {"ha": "switch.fan"}}],
                    "dials": [{"kind": "ha.brightness", "entity_id": GUEST}],
                },
            },
        }
    )
    assert entities_in(config) == {GUEST, "light.desk", "switch.fan"}


def test_entities_in_a_config_without_home_assistant_is_empty():
    assert (
        entities_in(Config.model_validate({"pages": {"home": {"keys": [{"label": "x"}]}}})) == set()
    )


# --- the brightness dial ----------------------------------------------------


class FakeHa:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.block: asyncio.Event | None = None
        self.error: Exception | None = None

    async def call_service(self, service: str, entity_id: str, data: dict[str, Any] | None = None):
        self.calls.append((service, entity_id, data))
        if self.block is not None:
            await self.block.wait()
        if self.error is not None:
            raise self.error


def make_services(store: StateStore, ha) -> Services:
    services = Services(store, Config(pages={"home": {}}), actions=ActionRunner())
    services.ha = ha
    return services


def brightness_dial(services: Services, entity_id: str = GUEST) -> HaBrightnessDial:
    return HaBrightnessDial(DialSpec(kind="ha.brightness", entity_id=entity_id), services)


def turn_on(pct: int) -> tuple[str, str, dict[str, Any]]:
    return ("light.turn_on", GUEST, {"brightness_pct": pct})


def test_the_dial_factory_is_registered():
    assert DIAL_FACTORIES["ha.brightness"] is HaBrightnessDial


async def test_turn_steps_five_percent_per_tick(store):
    ha = FakeHa()
    store.set(f"ha.{GUEST}", stored("on", brightness=128))  # 50 %
    await brightness_dial(make_services(store, ha)).on_turn(2)
    await settle()
    assert ha.calls == [turn_on(60)]


async def test_turn_down_steps_down(store):
    ha = FakeHa()
    store.set(f"ha.{GUEST}", stored("on", brightness=255))
    await brightness_dial(make_services(store, ha)).on_turn(-3)
    await settle()
    assert ha.calls == [turn_on(85)]


@pytest.mark.parametrize(
    ("brightness", "ticks", "expected"),
    [(250, 3, 100), (7, -1, 0), (255, 1, None), (0, -1, None), (1, -1, None)],
)
async def test_turn_is_clamped_to_0_and_100(store, brightness, ticks, expected):
    ha = FakeHa()
    store.set(f"ha.{GUEST}", stored("on", brightness=brightness))
    await brightness_dial(make_services(store, ha)).on_turn(ticks)
    await settle()
    assert ha.calls == ([] if expected is None else [turn_on(expected)])


async def test_an_off_light_is_at_zero(store):
    ha = FakeHa()
    store.set(f"ha.{GUEST}", stored("off"))
    dial = brightness_dial(make_services(store, ha))
    await dial.on_turn(-1)  # nothing below off
    await settle()
    assert ha.calls == []
    await dial.on_turn(1)
    await settle()
    assert ha.calls == [turn_on(5)]


@pytest.mark.parametrize(
    "entity",
    [
        None,
        stored("unavailable"),
        stored("unknown"),
        stored("on"),  # on/off only: no brightness to step
        stored("on", brightness=None),
        stored("on", brightness="bright"),
        "garbage",
    ],
)
async def test_turn_without_a_level_to_step_from_does_nothing(store, entity):
    ha = FakeHa()
    if entity is not None:
        store.set(f"ha.{GUEST}", entity)
    await brightness_dial(make_services(store, ha)).on_turn(1)
    await settle()
    assert ha.calls == []


async def test_turn_does_not_write_the_store(store):
    store.set(f"ha.{GUEST}", stored("on", brightness=128))
    seen: list[set[str]] = []
    store.subscribe(seen.append)
    await brightness_dial(make_services(store, FakeHa())).on_turn(1)
    await settle()
    assert seen == []  # Home Assistant's state_changed event is what moves the display


async def test_dial_without_a_home_assistant_client_does_nothing(store):
    store.set(f"ha.{GUEST}", stored("on", brightness=128))
    dial = brightness_dial(make_services(store, None))
    await dial.on_turn(1)
    await dial.on_press()
    await settle()


async def test_turn_in_flight_steps_from_the_target_not_a_stale_state(store):
    ha = FakeHa()
    ha.block = asyncio.Event()
    store.set(f"ha.{GUEST}", stored("on", brightness=128))  # 50 %
    dial = brightness_dial(make_services(store, ha))
    await dial.on_turn(1)  # 55, in flight
    store.set(f"ha.{GUEST}", stored("on", brightness=128))  # a push still carrying 50 %
    await dial.on_turn(1)
    await dial.on_turn(1)
    ha.block.set()
    await settle()
    assert ha.calls == [turn_on(55), turn_on(65)]  # not 60: no tick is lost


async def test_turn_after_the_sends_steps_from_the_state_again(store):
    ha = FakeHa()
    store.set(f"ha.{GUEST}", stored("on", brightness=128))
    dial = brightness_dial(make_services(store, ha))
    await dial.on_turn(1)
    await settle()
    store.set(f"ha.{GUEST}", stored("on", brightness=26))  # 10 %, however it got there
    await dial.on_turn(1)
    await settle()
    assert ha.calls == [turn_on(55), turn_on(15)]


async def test_send_failure_is_logged_without_a_traceback_and_ends_the_target(store, caplog):
    ha = FakeHa()
    ha.error = RuntimeError("Home Assistant unreachable: refused")
    store.set(f"ha.{GUEST}", stored("on", brightness=128))
    dial = brightness_dial(make_services(store, ha))
    with caplog.at_level(logging.WARNING):
        await dial.on_turn(1)
        await settle()
    (record,) = caplog.records
    assert record.name == "deck.ha" and record.exc_info is None
    assert GUEST in record.message and "55" in record.message and "refused" in record.message
    ha.error = None
    await dial.on_turn(1)
    await settle()
    assert ha.calls == [turn_on(55), turn_on(55)]  # stepped from the state, not the failed target


async def test_press_toggles_the_light(store):
    ha = FakeHa()
    await brightness_dial(make_services(store, ha)).on_press()
    assert ha.calls == [("light.toggle", GUEST, None)]


async def test_ha_brightness_coalesces(ha, http_stub, store):
    store.set(f"ha.{GUEST}", stored("off"))
    services = make_services(store, ha)
    dial = brightness_dial(services)
    http_stub.gate.clear()  # Home Assistant is slow
    for _ in range(20):
        await dial.on_turn(1)
    await eventually(lambda: len(http_stub.requests) == 1)
    await asyncio.sleep(0.1)
    assert [body for _, body, _ in http_stub.requests] == [
        {"entity_id": GUEST, "brightness_pct": 5}
    ]
    http_stub.gate.set()
    await eventually(lambda: len(http_stub.requests) == 2)
    await asyncio.sleep(0.1)
    levels = [body["brightness_pct"] for _, body, _ in http_stub.requests]
    assert levels == [5, 100]  # 2 POSTs, not 20


# --- the daemon's services --------------------------------------------------


def _config(**sections: Any) -> Config:
    return Config.model_validate({"pages": {"home": {}}, **sections})


def _action(kind: str, args: Any) -> Action:
    return Action.model_validate({kind: args})


async def test_services_without_a_home_assistant_section_have_no_ha():
    async with contextlib.AsyncExitStack() as stack:
        services = await cli._services(_config(), StateStore(), stack)
    assert services.ha is None
    with pytest.raises(KeyError):
        await services.actions.run(_action("ha", {"service": "light.toggle", "entity_id": GUEST}))


async def test_services_without_a_token_log_an_error_and_keep_going(monkeypatch, caplog, store):
    monkeypatch.delenv("HA_TOKEN", raising=False)
    config = _config(home_assistant=HA_CONFIG)
    with caplog.at_level(logging.ERROR):
        async with contextlib.AsyncExitStack() as stack:
            services = await cli._services(config, store, stack)
            assert services.ha is None
            assert store.get("ha.online") is False
            with pytest.raises(RuntimeError, match="Home Assistant"):
                await services.actions.run(
                    _action("ha", {"service": "light.toggle", "entity_id": GUEST})
                )
    assert "HA_TOKEN" in caplog.text


async def test_services_with_an_invalid_url_log_an_error_and_keep_going(monkeypatch, caplog, store):
    monkeypatch.setenv("HA_TOKEN", "T")
    config = _config(home_assistant={"url": "ha.local:8123", "token_env": "HA_TOKEN"})
    with caplog.at_level(logging.ERROR):
        async with contextlib.AsyncExitStack() as stack:
            services = await cli._services(config, store, stack)
    assert services.ha is None and store.get("ha.online") is False
    assert "http" in caplog.text


async def test_services_with_a_token_run_actions_over_rest_and_clean_up(
    monkeypatch, http_stub, store
):
    monkeypatch.setenv("HA_TOKEN", " T\n")  # surrounding whitespace is not part of the token
    config = _config(home_assistant={"url": http_stub.url, "token_env": "HA_TOKEN"})
    stack = contextlib.AsyncExitStack()
    services = await cli._services(config, store, stack)
    assert isinstance(services.ha, HomeAssistant)
    assert store.get("ha.online") is False

    action = _action(
        "ha", {"service": "light.turn_on", "entity_id": GUEST, "data": {"brightness_pct": 30}}
    )
    await services.actions.run(action)
    assert http_stub.requests == [
        ("/api/services/light/turn_on", {"entity_id": GUEST, "brightness_pct": 30}, "Bearer T")
    ]
    http_stub.status = 500
    with pytest.raises(RuntimeError, match="500"):
        await services.actions.run(action)
    await stack.aclose()  # cancels the client; nothing is left running
    assert store.get("ha.online") is False


async def test_services_track_the_entities_of_the_current_config(monkeypatch, ws_stub, store):
    monkeypatch.setenv("HA_TOKEN", "T")
    monkeypatch.setattr(ha_module, "WATCH_INTERVAL", 0.02)
    ws_stub.states = [light(GUEST, "on"), light("light.desk", "off")]
    config = _config(
        home_assistant={"url": ws_stub.url, "token_env": "HA_TOKEN"},
        pages={"home": {"keys": [{"state": {"ha": GUEST}}]}},
    )
    async with contextlib.AsyncExitStack() as stack:
        services = await cli._services(config, store, stack)
        await eventually(lambda: store.get("ha.online") is True)
        assert ha_keys(store) == {"ha.online", f"ha.{GUEST}"}

        # a reload swaps services.config without rebuilding the services
        services.config = Config.model_validate(
            {
                "home_assistant": {"url": ws_stub.url, "token_env": "HA_TOKEN"},
                "pages": {"home": {"keys": [{"state": {"ha": "light.desk"}}]}},
            }
        )
        await eventually(lambda: store.get("ha.light.desk") == stored("off"))
        assert ha_keys(store) == {"ha.online", "ha.light.desk"}
