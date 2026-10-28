"""The Stream Deck + XL as seen by the daemon: push images, receive events."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from PIL import Image
from StreamDeck.DeviceManager import DeviceManager, ProbeError
from StreamDeck.Devices.StreamDeck import (
    DialEventType,
    StreamDeck,
    TouchscreenEventType,
)
from StreamDeck.ImageHelpers import PILHelper
from StreamDeck.Transport.Transport import TransportError

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

log = logging.getLogger(__name__)

DECK_TYPE = "Stream Deck + XL"
KEY_COUNT = 36
DIAL_COUNT = 6
SEGMENT_WIDTH = 200
SEGMENT_HEIGHT = 100
RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0
# Opening fails with TransportError (no permission, device busy) or ProbeError
# (libhidapi missing); a stale handle can also surface as OSError.
_OPEN_ERRORS = (TransportError, ProbeError, OSError)


@runtime_checkable
class Device(Protocol):
    key_count: int
    dial_count: int

    def set_key(self, index: int, image: Image.Image) -> None: ...

    def set_segment(self, index: int, image: Image.Image) -> None: ...

    def set_brightness(self, pct: int) -> None: ...

    def events(self) -> AsyncIterator[Event]: ...


def key_event(index: int, pressed: bool) -> Event:
    return KeyDown(index) if pressed else KeyUp(index)


def dial_event(index: int, kind: DialEventType, value: int | bool) -> Event:
    if kind is DialEventType.TURN:
        return DialTurn(index, int(value))
    return DialDown(index) if value else DialUp(index)


def touch_event(kind: TouchscreenEventType, value: dict[str, int]) -> Event | None:
    """Short touches are taps, drags are swipes; long touches are not used."""
    if kind is TouchscreenEventType.SHORT:
        return TouchTap(value["x"], value["y"])
    if kind is TouchscreenEventType.DRAG:
        return TouchSwipe(value["x_out"] - value["x"])
    return None


def open_first_deck(serial: str | None = None) -> StreamDeck:
    """Open the first attached Stream Deck + XL (with this serial, if given)."""
    for deck in DeviceManager().enumerate():
        if deck.deck_type() != DECK_TYPE:
            continue
        deck.open()
        if serial is None or deck.get_serial_number() == serial:
            return deck
        deck.close()
    raise TransportError(
        f"no {DECK_TYPE} found" + (f" with serial {serial}" if serial else "")
    )


class StreamDeckDevice(Device):
    key_count = KEY_COUNT
    dial_count = DIAL_COUNT

    def __init__(
        self,
        serial: str | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval: float = 0.5,
        opener: Callable[[], Any] | None = None,
    ) -> None:
        self._open = opener or (lambda: open_first_deck(serial))
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._deck: Any | None = None  # the open library deck, None while disconnected

    def set_key(self, index: int, image: Image.Image) -> None:
        deck = self._deck
        if deck is not None:
            self._write(
                deck,
                deck.set_key_image,
                index,
                PILHelper.to_native_key_format(deck, image),
            )

    def set_segment(self, index: int, image: Image.Image) -> None:
        deck = self._deck
        if deck is not None:
            jpeg = PILHelper.to_native_touchscreen_format(deck, image)
            self._write(
                deck,
                deck.set_touchscreen_image,
                jpeg,
                x_pos=index * SEGMENT_WIDTH,
                y_pos=0,
                width=SEGMENT_WIDTH,
                height=SEGMENT_HEIGHT,
            )

    def set_brightness(self, pct: int) -> None:
        deck = self._deck
        if deck is not None:
            self._write(deck, deck.set_brightness, int(pct))

    def _write(
        self, deck: Any, write: Callable[..., None], *args: Any, **kwargs: Any
    ) -> None:
        try:
            write(*args, **kwargs)
        except TransportError as exc:
            log.warning("write to deck failed, treating it as disconnected: %s", exc)
            self._drop(deck)

    def _drop(self, deck: Any) -> None:
        """Stop using `deck`: later calls are ignored and `events()` reports the loss."""
        if self._deck is deck:
            self._deck = None
        try:
            deck.close()
        except TransportError:
            pass

    async def events(self) -> AsyncIterator[Event]:
        loop = asyncio.get_running_loop()
        delay = RECONNECT_MIN
        while True:
            try:
                deck = await asyncio.to_thread(self._open)
            except _OPEN_ERRORS as exc:
                log.warning("cannot open deck, retrying in %gs: %s", delay, exc)
            else:
                log.info("deck connected")
                delay = RECONNECT_MIN
                queue: asyncio.Queue[Event] = asyncio.Queue()
                self._attach(deck, loop, queue)
                self._deck = deck
                try:
                    yield Connected()
                    # The library's reader thread swallows a TransportError by
                    # closing the deck and fires no callback, so watch is_open().
                    while self._deck is deck and deck.is_open():
                        try:
                            event = await asyncio.wait_for(
                                queue.get(), self._poll_interval
                            )
                        except TimeoutError:
                            continue
                        yield event
                finally:
                    self._detach(deck)
                # Events still queued are discarded, so consumers must reset
                # their pressed-key state when they see Disconnected().
                log.warning("deck disconnected")
                yield Disconnected()
            await self._sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    @staticmethod
    def _attach(
        deck: Any, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Event]
    ) -> None:
        """Route library callbacks (reader thread) into `queue` (event loop)."""

        def emit(event: Event | None) -> None:
            if event is None:
                return
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:  # the loop closed while the reader thread ran on
                pass

        deck.set_key_callback(lambda _deck, key, pressed: emit(key_event(key, pressed)))
        deck.set_dial_callback(
            lambda _deck, dial, kind, value: emit(dial_event(dial, kind, value))
        )
        deck.set_touchscreen_callback(
            lambda _deck, kind, value: emit(touch_event(kind, value))
        )

    def _detach(self, deck: Any) -> None:
        deck.set_key_callback(None)
        deck.set_dial_callback(None)
        deck.set_touchscreen_callback(None)
        self._drop(deck)
