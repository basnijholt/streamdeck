"""The daemon: turns device events into actions and keeps the deck showing the state.

Everything runs on one asyncio loop. Store subscribers only mark "render needed";
the render loop does the drawing, at most `1 / RENDER_INTERVAL` times a second.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import replace
from pathlib import Path
from typing import Any

from PIL import Image

from deck.config import Action, Config, ConfigError, KeySpec, Page, load_config
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

Slot = tuple[str, int]  # ("key" | "segment", index)
ServicesFactory = Callable[[Config, StateStore], Services]


class Daemon:
    """Raises `ConfigError` if the config is invalid at startup; later reloads keep the old one."""

    def __init__(
        self,
        config_path: Path,
        device: Device,
        services_factory: ServicesFactory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = Path(config_path)
        self._device = device
        self._services_factory = services_factory
        self._clock = clock
        self.store = StateStore()
        self.store.subscribe(self._on_store_change)
        self._dirty = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        # Pressed keys: index -> (press time, the key's spec when it went down).
        self._down: dict[int, tuple[float, KeySpec]] = {}
        self._flash: dict[int, float] = {}  # key index -> when its error flash ends
        self._pushed: dict[Slot, tuple[Any, bytes]] = {}  # last view and image bytes pushed
        self._config_error = False
        self._signature = self._config_signature()
        config = load_config(self._path)
        self.current_page = config.start_page
        self._install(config)

    # --- config ---------------------------------------------------------------

    def reload(self) -> bool:
        """Load the config file again. On failure keep the old config and mark key 0 with ❌."""
        self._signature = self._config_signature()
        try:
            self._install(load_config(self._path))
        except ConfigError as exc:
            log.error(
                "invalid config %s, keeping the previous one:\n%s",
                self._path,
                "\n".join(exc.errors),
            )
        except Exception:
            log.exception("cannot apply config %s, keeping the previous one", self._path)
        else:
            self._config_error = False
            self._dirty.set()
            log.info("loaded config %s", self._path)
            return True
        self._config_error = True
        self._dirty.set()
        return False

    def _install(self, config: Config) -> None:
        services = self._services_factory(config, self.store)
        services.actions.register("page", self._page_action)
        page = self.current_page if self.current_page in config.pages else config.start_page
        dials = self._build_dials(config.pages[page], services)
        self.config, self.services, self.current_page, self._dials = config, services, page, dials

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
        """Handle device events until the device's event stream ends or this task is cancelled."""
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

    def _key_down(self, index: int) -> None:
        keys = self._page().keys
        spec = keys[index] if 0 <= index < len(keys) else None
        if spec is None or (self._config_error and index == 0):
            return  # nothing to run, or the key is showing the config error
        self._down[index] = (self._clock(), spec)

    def _key_up(self, index: int) -> None:
        pressed = self._down.pop(index, None)
        if pressed is None:
            return
        since, spec = pressed
        held_long = self._clock() - since >= LONG_PRESS_SECONDS
        action = (spec.long_press if held_long else None) or spec.press
        if action is not None:
            self._spawn(self._run_action(index, action))

    async def _run_action(self, index: int, action: Action) -> None:
        try:
            await self.services.actions.run(action)
        except Exception:
            log.exception("action %s failed", action.type)
            self._flash[index] = self._clock() + FLASH_SECONDS
            self._dirty.set()

    async def _dial_turn(self, index: int, ticks: int) -> None:
        # Awaited in event order, so ticks are never reordered; handlers coalesce slow work.
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
