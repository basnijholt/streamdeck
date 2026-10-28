import asyncio
import dataclasses
import io
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fake_deck import FakeDeck
from PIL import Image
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from StreamDeck.Transport.Transport import TransportError

from deck import device as device_module
from deck.device import (
    Device,
    StreamDeckDevice,
    dial_event,
    key_event,
    open_first_deck,
    touch_event,
)
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


class StubDeck:
    """Stands in for a library `StreamDeck`: same callback slots, formats and setters."""

    def __init__(self) -> None:
        self.open = True
        self.key_callback: Callable[..., None] | None = None
        self.dial_callback: Callable[..., None] | None = None
        self.touchscreen_callback: Callable[..., None] | None = None
        self.key_images: list[tuple[int, bytes]] = []
        self.touch_images: list[tuple[bytes, dict[str, int]]] = []
        self.brightness: list[int] = []
        self.fail_writes = False

    def key_image_format(self) -> dict[str, Any]:
        return {
            "size": (112, 112),
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 90,
        }

    def touchscreen_image_format(self) -> dict[str, Any]:
        return {
            "size": (1200, 100),
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 90,
        }

    def set_key_callback(self, callback: Callable[..., None] | None) -> None:
        self.key_callback = callback

    def set_dial_callback(self, callback: Callable[..., None] | None) -> None:
        self.dial_callback = callback

    def set_touchscreen_callback(self, callback: Callable[..., None] | None) -> None:
        self.touchscreen_callback = callback

    def is_open(self) -> bool:
        return self.open

    def close(self) -> None:
        self.open = False

    def _write(self) -> None:
        if self.fail_writes:
            raise TransportError("Failed to write out report (-1)")

    def set_key_image(self, key: int, image: bytes) -> None:
        self._write()
        self.key_images.append((key, image))

    def set_touchscreen_image(
        self,
        image: bytes,
        x_pos: int = 0,
        y_pos: int = 0,
        width: int = 0,
        height: int = 0,
    ) -> None:
        self._write()
        self.touch_images.append(
            (image, {"x_pos": x_pos, "y_pos": y_pos, "width": width, "height": height})
        )

    def set_brightness(self, percent: int) -> None:
        self._write()
        self.brightness.append(percent)


class Sleeps:
    """Injectable `sleep` that records requested delays and returns immediately."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


def make_device(
    *decks: StubDeck | Exception, sleeps: Sleeps | None = None
) -> StreamDeckDevice:
    """A device whose opener hands out the given decks in order (raising exceptions)."""
    queue = list(decks)

    def opener() -> StubDeck:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return StreamDeckDevice(opener=opener, sleep=sleeps or Sleeps(), poll_interval=0.01)


async def next_event(stream: AsyncIterator[Event]) -> Event:
    return await asyncio.wait_for(anext(stream), timeout=2)


def red(size: tuple[int, int]) -> Image.Image:
    return Image.new("RGB", size, "#ff0000")


# --- events ---------------------------------------------------------------


def test_events_are_frozen_values():
    assert KeyDown(3) == KeyDown(3)
    assert KeyDown(3) != KeyUp(3)
    assert Connected() == Connected()
    with pytest.raises(dataclasses.FrozenInstanceError):
        KeyDown(3).index = 4  # type: ignore[misc]


# --- FakeDeck -------------------------------------------------------------


@pytest.mark.parametrize("cls", [FakeDeck, StreamDeckDevice])
def test_implementations_override_every_device_member(cls):
    # Both classes subclass the Protocol, so isinstance alone would pass even
    # with a member missing (the inherited `...` body); check they define each one.
    for name in ("set_key", "set_segment", "set_brightness", "events"):
        assert name in vars(cls), f"{cls.__name__} does not implement {name}"
    assert cls.key_count == 36
    assert cls.dial_count == 6
    assert isinstance(cls.__new__(cls), Device)


def test_device_protocol_rejects_objects_without_the_members():
    assert not isinstance(object(), Device)


async def test_fake_deck_records_pushes_and_replays_injected_events():
    fake = FakeDeck()
    key, segment = red((112, 112)), red((200, 100))
    fake.set_key(4, key)
    fake.set_segment(2, segment)
    fake.set_brightness(40)
    assert fake.keys == {4: key}
    assert fake.segments == {2: segment}
    assert fake.brightness == 40
    assert fake.push_count == 2

    await fake.inject(KeyDown(1), KeyUp(1))
    stream = fake.events()
    assert await next_event(stream) == KeyDown(1)
    assert await next_event(stream) == KeyUp(1)
    await stream.aclose()


# --- open_first_deck --------------------------------------------------------


class ListedDeck:
    """A library deck as returned by `DeviceManager.enumerate()`."""

    def __init__(self, deck_type: str = "Stream Deck + XL", serial: str = "A1") -> None:
        self._type = deck_type
        self.serial = serial
        self.opened = False
        self.closed = False

    def deck_type(self) -> str:
        return self._type

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def get_serial_number(self) -> str:
        assert self.opened, "serial can only be read from an open deck"
        return self.serial


@pytest.fixture
def attach(monkeypatch):
    """Make `open_first_deck` see exactly these decks (no libhidapi involved)."""

    def attach(*decks: ListedDeck) -> None:
        class StubManager:
            def enumerate(self) -> list[ListedDeck]:
                return list(decks)

        monkeypatch.setattr(device_module, "DeviceManager", StubManager)

    return attach


def test_open_first_deck_without_serial_opens_the_first_xl(attach):
    first, second = ListedDeck(serial="A1"), ListedDeck(serial="B2")
    attach(first, second)
    assert open_first_deck() is first
    assert first.opened
    assert not second.opened


def test_open_first_deck_skips_other_deck_models(attach):
    other, xl = ListedDeck("Stream Deck +"), ListedDeck()
    attach(other, xl)
    assert open_first_deck() is xl
    assert not other.opened


def test_open_first_deck_with_serial_closes_the_others_and_returns_the_match(attach):
    wrong, right = ListedDeck(serial="A1"), ListedDeck(serial="B2")
    attach(wrong, right)
    assert open_first_deck("B2") is right
    assert wrong.closed
    assert not right.closed


def test_open_first_deck_raises_when_nothing_matches(attach):
    attach(ListedDeck("Stream Deck +"))
    with pytest.raises(TransportError, match=r"^no Stream Deck \+ XL found$"):
        open_first_deck()


def test_open_first_deck_raises_when_no_serial_matches(attach):
    deck = ListedDeck(serial="A1")
    attach(deck)
    with pytest.raises(
        TransportError, match=r"^no Stream Deck \+ XL found with serial Z9$"
    ):
        open_first_deck("Z9")
    assert deck.closed


# --- callback translation -------------------------------------------------


@pytest.mark.parametrize(
    ("index", "pressed", "expected"),
    [(3, True, KeyDown(3)), (3, False, KeyUp(3))],
)
def test_key_event(index, pressed, expected):
    assert key_event(index, pressed) == expected


@pytest.mark.parametrize(
    ("index", "kind", "value", "expected"),
    [
        (2, DialEventType.TURN, -1, DialTurn(2, -1)),
        (0, DialEventType.TURN, 3, DialTurn(0, 3)),
        (5, DialEventType.PUSH, True, DialDown(5)),
        (5, DialEventType.PUSH, False, DialUp(5)),
    ],
)
def test_dial_event(index, kind, value, expected):
    assert dial_event(index, kind, value) == expected


def test_touch_event_short_is_a_tap():
    assert touch_event(TouchscreenEventType.SHORT, {"x": 450, "y": 50}) == TouchTap(
        450, 50
    )


def test_touch_event_drag_is_a_swipe_by_x_travel():
    drag = {"x": 900, "y": 40, "x_out": 700, "y_out": 45}
    assert touch_event(TouchscreenEventType.DRAG, drag) == TouchSwipe(-200)
    drag = {"x": 100, "y": 40, "x_out": 340, "y_out": 40}
    assert touch_event(TouchscreenEventType.DRAG, drag) == TouchSwipe(240)


def test_touch_event_long_press_is_ignored():
    assert touch_event(TouchscreenEventType.LONG, {"x": 450, "y": 50}) is None


# --- StreamDeckDevice: connecting and forwarding --------------------------


async def test_events_emits_connected_then_forwards_library_callbacks():
    deck = StubDeck()
    stream = make_device(deck).events()
    assert await next_event(stream) == Connected()

    deck.key_callback(deck, 3, True)
    deck.dial_callback(deck, 2, DialEventType.TURN, -1)
    deck.dial_callback(deck, 2, DialEventType.PUSH, True)
    deck.touchscreen_callback(deck, TouchscreenEventType.SHORT, {"x": 450, "y": 50})
    deck.touchscreen_callback(
        deck, TouchscreenEventType.DRAG, {"x": 900, "y": 40, "x_out": 700, "y_out": 40}
    )

    assert [await next_event(stream) for _ in range(5)] == [
        KeyDown(3),
        DialTurn(2, -1),
        DialDown(2),
        TouchTap(450, 50),
        TouchSwipe(-200),
    ]
    await stream.aclose()


async def test_callbacks_fired_from_the_library_thread_reach_the_loop():
    deck = StubDeck()
    stream = make_device(deck).events()
    assert await next_event(stream) == Connected()

    worker = threading.Thread(target=lambda: deck.key_callback(deck, 7, True))
    worker.start()
    assert await next_event(stream) == KeyDown(7)
    worker.join()
    await stream.aclose()


async def test_closing_the_stream_detaches_callbacks_and_closes_the_deck():
    deck = StubDeck()
    stream = make_device(deck).events()
    await next_event(stream)
    await stream.aclose()
    assert deck.key_callback is None
    assert deck.dial_callback is None
    assert deck.touchscreen_callback is None
    assert not deck.is_open()


# --- StreamDeckDevice: pushing images -------------------------------------


async def test_set_key_pushes_native_key_jpeg():
    deck = StubDeck()
    device = make_device(deck)
    stream = device.events()
    await next_event(stream)

    device.set_key(5, red((112, 112)))

    [(key, data)] = deck.key_images
    assert key == 5
    native = Image.open(io.BytesIO(data))
    assert native.format == "JPEG"
    assert native.size == (112, 112)
    await stream.aclose()


async def test_set_segment_draws_a_200x100_jpeg_at_its_slot():
    deck = StubDeck()
    device = make_device(deck)
    stream = device.events()
    await next_event(stream)

    device.set_segment(3, red((200, 100)))

    [(data, geometry)] = deck.touch_images
    assert geometry == {"x_pos": 600, "y_pos": 0, "width": 200, "height": 100}
    native = Image.open(io.BytesIO(data))
    assert native.format == "JPEG"
    assert native.size == (100, 200)  # rotated 90 degrees for the panel
    await stream.aclose()


async def test_set_brightness_passes_percent():
    deck = StubDeck()
    device = make_device(deck)
    stream = device.events()
    await next_event(stream)
    device.set_brightness(35)
    assert deck.brightness == [35]
    await stream.aclose()


def test_calls_before_connect_are_ignored():
    device = StreamDeckDevice(opener=lambda: pytest.fail("must not open"))
    device.set_key(0, red((112, 112)))
    device.set_segment(0, red((200, 100)))
    device.set_brightness(50)


async def test_calls_after_disconnect_are_ignored():
    deck = StubDeck()
    device = make_device(deck, TransportError("gone"))
    stream = device.events()
    await next_event(stream)
    deck.open = False  # the library reader thread closed it
    assert await next_event(stream) == Disconnected()

    device.set_key(0, red((112, 112)))
    device.set_segment(0, red((200, 100)))
    device.set_brightness(50)
    assert deck.key_images == []
    assert deck.touch_images == []
    assert deck.brightness == []
    await stream.aclose()


# --- StreamDeckDevice: disconnect and reconnect ---------------------------


async def test_reader_thread_closing_the_deck_emits_disconnected_then_reopens():
    first, second = StubDeck(), StubDeck()
    sleeps = Sleeps()
    stream = make_device(first, second, sleeps=sleeps).events()
    assert await next_event(stream) == Connected()

    first.open = False  # what the library's reader thread does on a TransportError
    assert await next_event(stream) == Disconnected()
    assert await next_event(stream) == Connected()
    assert sleeps.delays == [1]

    second.key_callback(second, 0, True)
    assert await next_event(stream) == KeyDown(0)
    await stream.aclose()


async def test_write_transport_error_emits_disconnected_and_closes_the_deck():
    first, second = StubDeck(), StubDeck()
    device = make_device(first, second)
    stream = device.events()
    await next_event(stream)

    first.fail_writes = True
    device.set_key(0, red((112, 112)))  # must not raise
    assert await next_event(stream) == Disconnected()
    assert not first.is_open()
    assert await next_event(stream) == Connected()

    device.set_key(1, red((112, 112)))  # goes to the new deck
    assert [k for k, _ in second.key_images] == [1]
    await stream.aclose()


async def test_backoff_doubles_to_thirty_seconds_and_resets_after_connecting():
    first, second = StubDeck(), StubDeck()
    failures = [TransportError("Could not open HID device.")] * 7
    sleeps = Sleeps()
    stream = make_device(*failures, first, second, sleeps=sleeps).events()

    assert await next_event(stream) == Connected()
    assert sleeps.delays == [1, 2, 4, 8, 16, 30, 30]

    sleeps.delays.clear()
    first.open = False
    assert await next_event(stream) == Disconnected()
    assert await next_event(stream) == Connected()
    assert sleeps.delays == [1]
    await stream.aclose()


async def test_open_failures_other_than_transport_errors_are_retried_too():
    deck = StubDeck()
    sleeps = Sleeps()
    stream = make_device(OSError("denied"), deck, sleeps=sleeps).events()
    assert await next_event(stream) == Connected()
    assert sleeps.delays == [1]
    await stream.aclose()
