"""The daemon: turns device events into actions and keeps the deck showing the state.

Everything runs on one asyncio loop. Store subscribers only mark "render needed";
the render loop does the drawing, at most `1 / RENDER_INTERVAL` times a second.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import replace
from pathlib import Path
from typing import Any

from PIL import Image

from deck.config import Action, Config, ConfigError, KeySpec, Page, load_config
from deck.control import close_control, default_control_path, serve_control
from deck.device import SEGMENT_WIDTH, Device
from deck.dials import DIAL_FACTORIES, DialHandler, Services
from deck.events import (
    Connected,
    DialDown,
    DialTurn,
    DialUp,
    Disconnected,
    Event,
    KeyDown,
    KeyUp,
    TouchSwipe,
    TouchTap,
)
from deck.render import KeyView, draw_key, draw_segment
from deck.state import StateStore
from deck.t3.model import visible_threads
from deck.views import ERROR, key_view, segment_view

log = logging.getLogger(__name__)

LONG_PRESS_SECONDS = 0.5
FLASH_SECONDS = 1.0
RENDER_INTERVAL = 0.05  # 20 Hz
WATCH_INTERVAL = 1.0
WATCH_STABLE = 0.3

CONFIG_ERROR_VIEW = KeyView("❌", "config", None, bg=ERROR[0], border=ERROR[1])

# Sections describing connections that services open once; a reload cannot change them.
CONNECTION_SECTIONS = ("mac", "home_assistant", "t3", "ddc")

Slot = tuple[str, int]  # ("key" | "segment", index)
# Awaited once, on the event loop, when the daemon starts. It starts the long-lived
# clients and registers their cleanup on the exit stack, which `run` closes on the way out.
ServicesFactory = Callable[[Config, StateStore, contextlib.AsyncExitStack], Awaitable[Services]]


class Daemon:
    """Drives one deck from one config file.

    `ConfigError` is raised by the constructor if the config is invalid at startup; later
    reloads keep the old config. Services are built once by `start` (which `run` calls) and
    follow reloads only through `services.config`: connections need a restart.

    `run` also serves the control socket at `control_path` (default: `default_control_path()`).
    """

    services: Services  # set by start()
    config_errors: list[str]  # why the last `reload` failed; empty if it succeeded

    def __init__(
        self,
        config_path: Path,
        device: Device,
        services_factory: ServicesFactory,
        clock: Callable[[], float] = time.monotonic,
        control_path: Path | None = None,
    ) -> None:
        self._path = Path(config_path)
        self._control_path = control_path if control_path is not None else default_control_path()
        self.config_errors = []
        self._device = device
        self._services_factory = services_factory
        self._clock = clock
        self.store = StateStore()
        self.store.subscribe(self._on_store_change)
        self._dirty = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        # Pressed keys: index -> (press time, the key's spec and page when it went down).
        self._down: dict[int, tuple[float, KeySpec, str]] = {}
        self._flash: dict[int, float] = {}  # key index -> when its error flash ends
        self._pushed: dict[Slot, tuple[Any, bytes]] = {}  # last view and image bytes pushed
        self._config_error = False
        self._signature = self._config_signature()
        self.config = load_config(self._path)
        self.current_page = self.config.start_page
        self._built_with = self.config
        self._dials: dict[int, DialHandler] = {}

    # --- config ---------------------------------------------------------------

    async def start(self, stack: contextlib.AsyncExitStack) -> None:
        """Build the services, once. `run` calls this; tests driving `handle` call it too.

        A factory error propagates: it is a startup failure, not a config problem.
        """
        self.services = await self._services_factory(self.config, self.store, stack)
        self.services.actions.register("page", self._page_action)
        self._built_with = self.config
        self._dials = self._build_dials(self._page(), self.services)

    def reload(self) -> bool:
        """Load the config file again. On failure keep the old config and mark key 0 with ❌."""
        self._signature = self._config_signature()
        try:
            config = load_config(self._path)
        except ConfigError as exc:
            self.config_errors = list(exc.errors)
            log.error(
                "invalid config %s, keeping the previous one:\n%s",
                self._path,
                "\n".join(exc.errors),
            )
        except Exception as exc:
            self.config_errors = [f"cannot read {self._path}: {exc}"]
            log.exception("cannot read config %s, keeping the previous one", self._path)
        else:
            self._apply(config)
            self.config_errors = []
            self._config_error = False
            self._dirty.set()
            log.info("loaded config %s", self._path)
            return True
        self._config_error = True
        self._dirty.set()
        return False

    def _apply(self, config: Config) -> None:
        self.config = self.services.config = config
        if self.current_page not in config.pages:
            self.current_page = config.start_page
            self._flash.clear()
        self._dials = self._build_dials(self._page(), self.services)
        stale = [
            name
            for name in CONNECTION_SECTIONS
            if getattr(config, name) != getattr(self._built_with, name)
        ]
        if stale:
            log.warning("restart deck to apply changes to: %s", ", ".join(stale))

    def _config_signature(self) -> tuple[int, int] | None:
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    async def _watch_config(self) -> None:
        """Reload when the file changes, once it has stopped changing (editors save in steps)."""
        while True:
            await asyncio.sleep(WATCH_INTERVAL)
            changed = self._config_signature()
            if changed == self._signature:
                continue
            while True:
                await asyncio.sleep(WATCH_STABLE)
                latest = self._config_signature()
                if latest == changed:
                    break
                changed = latest
            self.reload()

    # --- pages and dials --------------------------------------------------------

    async def _page_action(self, action: Action) -> None:
        self._switch_page(action.args)

    def _switch_page(self, name: str) -> None:
        page = self.config.pages[name]
        self.current_page = name
        self._dials = self._build_dials(page, self.services)
        self._flash.clear()
        self._dirty.set()

    @staticmethod
    def _build_dials(page: Page, services: Services) -> dict[int, DialHandler]:
        handlers: dict[int, DialHandler] = {}
        for index, spec in enumerate(page.dials):
            factory = DIAL_FACTORIES.get(spec.kind) if spec else None
            if factory is None:
                continue
            try:
                handlers[index] = factory(spec, services)
            except Exception:
                log.exception("cannot build %s dial %d", spec.kind, index)
        return handlers

    # --- events -----------------------------------------------------------------

    async def run(self) -> None:
        """Handle device events until the device's event stream ends or this task is cancelled.

        The services' exit stack is closed last, after every task of the daemon has stopped.
        """
        async with contextlib.AsyncExitStack() as stack:
            await self.start(stack)
            await self._start_control(stack)
            background = [
                asyncio.create_task(self._render_loop()),
                asyncio.create_task(self._watch_config()),
            ]
            try:
                async for event in self._device.events():
                    try:
                        await self.handle(event)
                    except Exception:
                        log.exception("error handling %r", event)
            finally:
                tasks = [*background, *self._tasks]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _start_control(self, stack: contextlib.AsyncExitStack) -> None:
        """Serve the control socket until `stack` closes. Without it the deck still works."""
        try:
            server = await serve_control(self._control_path, self)
        except OSError:
            log.exception(
                "cannot serve the control socket %s, `deck` commands will fail", self._control_path
            )
            return
        stack.push_async_callback(close_control, server, self._control_path)
        log.info("control socket %s", self._control_path)

    async def handle(self, event: Event) -> None:
        match event:
            case KeyDown(index):
                self._key_down(index)
            case KeyUp(index):
                self._key_up(index)
            case DialTurn(index, ticks):
                await self._dial_turn(index, ticks)
            case DialDown(index):
                self._dial_press(index)
            case TouchTap(x):
                self._dial_press(x // SEGMENT_WIDTH)
            case Connected():
                self._pushed.clear()
                self._dirty.set()
            case Disconnected():
                # Events queued at that moment were discarded: no KeyUp will follow.
                self._down.clear()
                self._pushed.clear()
            case DialUp() | TouchSwipe():
                pass

    def _page(self) -> Page:
        return self.config.pages[self.current_page]

    @staticmethod
    def _key_spec(page: Page, index: int) -> KeySpec | None:
        return page.keys[index] if 0 <= index < len(page.keys) else None

    @staticmethod
    def _key_action(spec: KeySpec, long: bool) -> Action | None:
        return (spec.long_press if long else None) or spec.press

    def _key_down(self, index: int) -> None:
        spec = self._key_spec(self._page(), index)
        if spec is None or (self._config_error and index == 0):
            return  # nothing to run, or the key is showing the config error
        self._down[index] = (self._clock(), spec, self.current_page)

    def _key_up(self, index: int) -> None:
        pressed = self._down.pop(index, None)
        if pressed is None:
            return
        since, spec, page = pressed
        action = self._key_action(spec, self._clock() - since >= LONG_PRESS_SECONDS)
        if action is not None:
            self._spawn(self._run_action(index, page, action))

    def press_key(self, page: str, index: int, long: bool = False) -> None:
        """Press key `index` of `page` as the control socket asks, whichever page is showing.

        The action runs like a physical press (a `long` one uses `long_press`, falling back
        to `press`; a failure flashes the key if its page is showing) and the page on show
        does not change unless the action is a `page` action. This returns once the action
        has started, without waiting for it. Raises `LookupError` saying why, if there is no
        such page, no key there or no action on it.
        """
        if page not in self.config.pages:
            raise LookupError(f"unknown page {page!r}")
        spec = self._key_spec(self.config.pages[page], index)
        if spec is None:
            raise LookupError(f"no key {index} on page {page!r}")
        action = self._key_action(spec, long)
        if action is None:
            raise LookupError(f"key {index} on page {page!r} has no action")
        self._spawn(self._run_action(index, page, action))

    async def _run_action(self, index: int, page: str, action: Action) -> None:
        try:
            await self.services.actions.run(action)
        except Exception:
            log.exception("action %r failed", action)
            if self.current_page == page:  # else the key is no longer on screen
                self._flash[index] = self._clock() + FLASH_SECONDS
                self._dirty.set()

    async def _dial_turn(self, index: int, ticks: int) -> None:
        # Awaited in event order, so ticks are never reordered (see `DialHandler`).
        handler = self._dials.get(index)
        if handler is None:
            return
        try:
            await handler.on_turn(ticks)
        except Exception:
            log.exception("dial %d turn failed", index)

    def _dial_press(self, index: int) -> None:
        handler = self._dials.get(index)
        if handler is not None:
            self._spawn(self._run_dial_press(index, handler))

    @staticmethod
    async def _run_dial_press(index: int, handler: DialHandler) -> None:
        try:
            await handler.on_press()
        except Exception:
            log.exception("dial %d press failed", index)

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """Run `coroutine` concurrently so a slow action never blocks the event stream.

        It starts at once, so a quick action (such as a page switch) has taken effect
        before the next event is handled.
        """
        task = asyncio.Task(coroutine, loop=asyncio.get_running_loop(), eager_start=True)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- rendering --------------------------------------------------------------

    def _on_store_change(self, changed: set[str]) -> None:
        self._dirty.set()  # never draws or raises: a store subscriber failure would hit the writer

    async def _render_loop(self) -> None:
        while True:
            timeout = max(0.0, min(self._flash.values()) - self._clock()) if self._flash else None
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout)
            except TimeoutError:
                pass  # a key's error flash is over
            self._dirty.clear()
            try:
                self.render()
            except Exception:
                log.exception("render failed")
            await asyncio.sleep(RENDER_INTERVAL)

    def render(self) -> None:
        """Draw every key and segment and push those whose image changed."""
        now = self._clock()
        self._flash = {index: end for index, end in self._flash.items() if end > now}
        page = self._page()
        slot_threads = visible_threads(self.store.get("t3.threads", []))
        for index in range(self._device.key_count):
            spec = page.keys[index] if index < len(page.keys) else None
            view = key_view(spec, self.store, self.config, slot_threads)
            if index in self._flash:
                view = replace(view, bg=ERROR[0], border=ERROR[1])
            if self._config_error and index == 0:
                view = CONFIG_ERROR_VIEW
            self._push(("key", index), view, draw_key, self._device.set_key)
        for index in range(self._device.dial_count):
            spec = page.dials[index] if index < len(page.dials) else None
            view = segment_view(spec, self.store, self.config)
            self._push(("segment", index), view, draw_segment, self._device.set_segment)

    def _push(
        self,
        slot: Slot,
        view: Any,
        draw: Callable[[Any], Image.Image],
        send: Callable[[int, Image.Image], None],
    ) -> None:
        last = self._pushed.get(slot)
        if last is not None and last[0] == view:
            return
        image = draw(view)
        data = image.tobytes()
        if last is None or last[1] != data:
            send(slot[1], image)
        self._pushed[slot] = (view, data)
