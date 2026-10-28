"""Home Assistant: service calls over REST, entity state over the WebSocket API, a brightness dial.

`HomeAssistant.call_service` POSTs to the REST API. `HomeAssistant.run` keeps a WebSocket
open: it authenticates, fetches every entity state, subscribes to `state_changed` events,
and on a lost connection marks Home Assistant offline and reconnects with exponential backoff.

Every entity state is kept in a cache, but the store only gets the entities the current config
shows (`entities_in`): `ha.<entity_id>` holds `{"state": str, "attributes": dict}` and
`ha.online` says whether the WebSocket is up. The config can change at a reload without the
client being rebuilt, so the store is brought back in line with the cache within
`WATCH_INTERVAL` seconds of the set of shown entities changing.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import SplitResult, quote, urlsplit

from websockets import connect
from websockets.exceptions import WebSocketException

from deck.config import Config, DialSpec, HAConfig
from deck.dials import DIAL_FACTORIES, Coalescer, Services
from deck.state import StateStore

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5.0  # seconds for one REST call
CONNECT_TIMEOUT = 10.0  # seconds to open the WebSocket
HANDSHAKE_TIMEOUT = 60.0  # seconds from the WebSocket opening to the subscription being live
MAX_MESSAGE_SIZE = 64 << 20  # `get_states` is several MB on a big installation
BACKOFF_START = 1.0  # seconds before the first reconnect
BACKOFF_MAX = 30.0
WATCH_INTERVAL = 1.0  # seconds between looks at which entities the config shows
MAX_ERROR_DETAIL = 200  # characters of an error body that go into a message

BRIGHTNESS_STEP = 5  # percent per dial tick
SUBSCRIBE_ID = 1  # Home Assistant wants the ids of one connection to increase
GET_STATES_ID = 2

EntitySource = set[str] | Callable[[], set[str]]


class HaProtocolError(Exception):
    """Home Assistant said something the client cannot work with."""


class HaAuthError(HaProtocolError):
    """Home Assistant rejected the access token."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would send the bearer token wherever it points."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _opener() -> urllib.request.OpenerDirector:
    """Home Assistant is on the LAN: no proxy from the environment, and no redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def websocket_url(url: str) -> str:
    """The WebSocket endpoint of the Home Assistant at `url`: `http` becomes `ws`, `https` `wss`."""
    parts = _http_parts(url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}/api/websocket"


def _http_parts(url: str) -> SplitResult:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"home_assistant.url must be an http or https URL, not {url!r}")
    return parts


def entities_in(cfg: Config) -> set[str]:
    """The entities something on any page of `cfg` shows: key states and brightness dials."""
    entities: set[str] = set()
    for page in cfg.pages.values():
        for key in page.keys:
            if key is not None and key.state is not None:
                entities.add(key.state.ha)
        for dial in page.dials:
            if dial is not None and dial.kind == "ha.brightness" and dial.entity_id:
                entities.add(dial.entity_id)
    return entities


def _entity(raw: dict[str, Any]) -> dict[str, Any]:
    """What is kept of an entity as Home Assistant sends it."""
    return {"state": raw.get("state"), "attributes": raw.get("attributes") or {}}


class HomeAssistant:
    """Calls services and mirrors the state of the entities that `entities` names.

    `entities` is a set, or a callable returning one that is looked at every `WATCH_INTERVAL`
    seconds, so it can follow a config reload. `sleep` waits out the reconnect backoff.
    """

    def __init__(
        self,
        cfg: HAConfig,
        token: str,
        store: StateStore,
        entities: EntitySource,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        parts = _http_parts(cfg.url)
        self._base = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
        self._ws_url = websocket_url(cfg.url)
        self._token = token
        self._store = store
        self._entities = entities
        self._sleep = sleep
        self._cache: dict[str, dict[str, Any]] = {}  # every entity Home Assistant has told us of
        self._tracked = self._wanted()  # the entities mirrored into the store
        self._online = False
        self._loading = False  # True while a new connection fetches the states
        self._delay = BACKOFF_START

    # --- services ---------------------------------------------------------------

    async def call_service(
        self, service: str, entity_id: str, data: dict[str, Any] | None = None
    ) -> None:
        """Call `service` (`domain.name`) on `entity_id`, with `data` as extra service data.

        Raises `ValueError` for a service that is not `domain.name`, `RuntimeError` when Home
        Assistant cannot be reached or does not answer with a 2xx status.
        """
        domain, _, name = service.partition(".")
        if not domain or not name:
            raise ValueError(f"service {service!r} must be written domain.name")
        url = f"{self._base}/api/services/{quote(domain, safe='')}/{quote(name, safe='')}"
        body = json.dumps({"entity_id": entity_id, **(data or {})}).encode()
        await asyncio.to_thread(self._post, service, url, body)

    def _post(self, service: str, url: str, body: bytes) -> None:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with _opener().open(request, timeout=REQUEST_TIMEOUT) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                detail = exc.read(MAX_ERROR_DETAIL).decode(errors="replace").strip()
            message = f"Home Assistant answered {exc.code} {exc.reason} to {service}"
            raise RuntimeError(f"{message}: {detail}" if detail else message) from None
        except OSError as exc:  # URLError, a refused connection, a timeout
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            raise RuntimeError(f"Home Assistant unreachable calling {service}: {reason}") from None

    # --- state ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect, mirror state and reconnect until cancelled."""
        self.resync()  # ha.online = False until the first connection is up
        watcher = asyncio.create_task(self._watch_entities())
        try:
            while True:
                try:
                    await self._session()
                except HaAuthError as exc:
                    log.error("Home Assistant rejected the access token: %s", exc)
                    self._delay = BACKOFF_MAX  # the same token will not work on a retry
                except (OSError, WebSocketException, HaProtocolError) as exc:  # TimeoutError is one
                    log.warning("Home Assistant connection failed: %s", exc or type(exc).__name__)
                except Exception:
                    log.exception("Home Assistant connection failed")
                finally:
                    self._set_online(False)
                delay, self._delay = self._delay, min(self._delay * 2, BACKOFF_MAX)
                log.warning("Home Assistant connection down, retrying in %gs", delay)
                await self._sleep(delay)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            self._set_online(False)

    async def _session(self) -> None:
        """One connection: returns when it is closed, raises when it fails."""
        async with connect(
            self._ws_url, max_size=MAX_MESSAGE_SIZE, open_timeout=CONNECT_TIMEOUT, proxy=None
        ) as ws:
            self._loading = True
            try:
                async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                    await self._authenticate(ws)
                    # Subscribe first: a change between the snapshot and a later subscription
                    # would be lost. Events that arrive before the snapshot are older than it.
                    self._cache = {}
                    subscribe = {"id": SUBSCRIBE_ID, "type": "subscribe_events"}
                    await self._call(ws, {**subscribe, "event_type": "state_changed"})
                    states = await self._call(ws, {"id": GET_STATES_ID, "type": "get_states"})
                    self._cache = {
                        raw["entity_id"]: _entity(raw)
                        for raw in states or []
                        if isinstance(raw, dict) and isinstance(raw.get("entity_id"), str)
                    }
            finally:
                self._loading = False
            self._delay = BACKOFF_START
            self._online = True
            self.resync()
            log.info("connected to Home Assistant")
            async for raw in ws:
                self._handle(_parse(raw))

    async def _authenticate(self, ws: Any) -> None:
        hello = _parse(await ws.recv())
        if hello.get("type") != "auth_required":
            raise HaProtocolError(f"expected auth_required, got {hello.get('type')!r}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        reply = _parse(await ws.recv())
        if reply.get("type") == "auth_invalid":
            raise HaAuthError(str(reply.get("message", "invalid access token")))
        if reply.get("type") != "auth_ok":
            raise HaProtocolError(f"expected auth_ok, got {reply.get('type')!r}")

    async def _call(self, ws: Any, command: dict[str, Any]) -> Any:
        """Send `command` and return its result; events that arrive meanwhile are handled."""
        await ws.send(json.dumps(command))
        while True:
            reply = _parse(await ws.recv())
            if reply.get("type") == "result" and reply.get("id") == command["id"]:
                if reply.get("success") is not True:
                    raise HaProtocolError(f"{command['type']} failed: {reply.get('error')}")
                return reply.get("result")
            self._handle(reply)

    def _handle(self, message: dict[str, Any]) -> None:
        """Apply a `state_changed` event to the cache, and to the store if it shows the entity."""
        if message.get("type") != "event":
            return
        event = message.get("event")
        data = event.get("data") if isinstance(event, dict) else None
        entity_id = data.get("entity_id") if isinstance(data, dict) else None
        if not isinstance(entity_id, str):
            return
        new_state = data.get("new_state")
        if isinstance(new_state, dict):
            self._cache[entity_id] = _entity(new_state)
        elif new_state is None:  # the entity was removed
            self._cache.pop(entity_id, None)
        else:
            return
        if entity_id in self._tracked:
            self.resync()

    async def _watch_entities(self) -> None:
        while True:
            await asyncio.sleep(WATCH_INTERVAL)
            try:
                wanted = self._wanted()
                if wanted != self._tracked:
                    self._tracked = wanted
                    self.resync()
            except Exception:  # tracking must not die silently
                log.exception("cannot follow the entities of the config")

    def _wanted(self) -> set[str]:
        return set(self._entities() if callable(self._entities) else self._entities)

    def _set_online(self, online: bool) -> None:
        if online != self._online:
            self._online = online
            self.resync()

    def resync(self) -> None:
        """Make the store's `ha.*` keys the connection state and the cached tracked entities.

        What Home Assistant last said wins: this also takes back a value that was written
        ahead of it, such as the level a dial showed for a request that then failed.
        """
        if self._loading:  # the cache is being rebuilt: the store keeps the last full state
            return
        values: dict[str, Any] = {"online": self._online}
        values.update((e, self._cache[e]) for e in self._tracked if e in self._cache)
        self._store.update("ha.", values, replace=True)


def _parse(raw: str | bytes) -> dict[str, Any]:
    """The JSON object in `raw`; anything else is an empty message, which is ignored."""
    try:
        message = json.loads(raw)
    except ValueError:
        log.warning("ignoring a message from Home Assistant that is not JSON: %.200r", raw)
        return {}
    if not isinstance(message, dict):
        log.warning("ignoring a message from Home Assistant that is not an object: %.200r", raw)
        return {}
    return message


# --- the brightness dial ----------------------------------------------------


def _brightness_pct(entity: Any) -> int | None:
    """The brightness of a light entity in percent: 0 if it is off, None if it has none."""
    if not isinstance(entity, dict):
        return None
    state = entity.get("state")
    if state == "off":
        return 0
    level = entity.get("attributes", {}).get("brightness")  # 0-255
    if state != "on" or isinstance(level, bool) or not isinstance(level, int | float):
        return None
    return round(level * 100 / 255)


def _with_brightness(entity: Any, level: int) -> dict[str, Any]:
    """`entity` as Home Assistant will report it once its brightness is `level` percent."""
    attributes = dict(entity.get("attributes") or {}) if isinstance(entity, dict) else {}
    if level == 0:  # a brightness of 0 % turns the light off, and off lights have none
        return {"state": "off", "attributes": {**attributes, "brightness": None}}
    return {"state": "on", "attributes": {**attributes, "brightness": round(level * 255 / 100)}}


class HaBrightnessDial:
    """Turn: a light's brightness up or down by 5 % a tick. Press: toggle the light.

    Turning is shown at once (the store is written before Home Assistant answers) and the
    requests go through a `Coalescer`, so a fast spin sends the first and the last level, not
    every tick. Home Assistant's own state events then confirm or correct the display, and a
    request that fails puts back what Home Assistant last reported. While requests are in
    flight or waiting, each turn steps from the dial's own target, not from the store: an event
    that still reports the level before the spin would make the dial lose ticks.
    """

    def __init__(self, spec: DialSpec, services: Services) -> None:
        assert spec.entity_id is not None  # the config requires it for this kind
        self._entity_id = spec.entity_id
        self._services = services
        self._sender = Coalescer[tuple[int, int]](self._send)
        self._submitted = 0  # number of the newest request handed to the sender
        self._target: int | None = None  # its level, until the sender has dealt with it

    async def on_turn(self, ticks: int) -> None:
        if self._services.ha is None:
            return
        store = self._services.store
        key = f"ha.{self._entity_id}"
        current = self._target
        if current is None:
            current = _brightness_pct(store.get(key))
        if current is None:  # unavailable, or a light without brightness: nothing to step from
            return
        level = max(0, min(100, current + BRIGHTNESS_STEP * ticks))
        if level != current or self._target is not None:
            # Also puts back a level that a stale event has overwritten mid-spin.
            store.set(key, _with_brightness(store.get(key), level))
        if level != current:
            self._submitted += 1
            self._target = level
            self._sender.submit((self._submitted, level))

    async def on_press(self) -> None:
        if self._services.ha is not None:
            await self._services.ha.call_service("light.toggle", self._entity_id)

    async def _send(self, request: tuple[int, int]) -> None:
        number, level = request
        ha = self._services.ha
        failed = True
        try:
            if ha is not None:
                await ha.call_service("light.turn_on", self._entity_id, {"brightness_pct": level})
            failed = False
        except RuntimeError as exc:
            log.warning("setting %s to %d%% failed: %s", self._entity_id, level, exc)
        finally:
            if number == self._submitted:  # no newer request is waiting
                self._target = None
                if failed and ha is not None:
                    ha.resync()  # the display goes back to what Home Assistant says


DIAL_FACTORIES["ha.brightness"] = HaBrightnessDial
