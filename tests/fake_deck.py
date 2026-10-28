"""In-memory `Device` for tests: records pushed images, replays injected events."""

import asyncio
from collections.abc import AsyncIterator

from PIL import Image

from deck.device import DIAL_COUNT, KEY_COUNT, Device
from deck.events import Event


class FakeDeck(Device):
    key_count = KEY_COUNT
    dial_count = DIAL_COUNT

    def __init__(self) -> None:
        self.keys: dict[int, Image.Image] = {}
        self.segments: dict[int, Image.Image] = {}
        self.brightness: int | None = None
        self.push_count = 0
        self._queue: asyncio.Queue[Event] = asyncio.Queue()

    def set_key(self, index: int, image: Image.Image) -> None:
        self.keys[index] = image
        self.push_count += 1

    def set_segment(self, index: int, image: Image.Image) -> None:
        self.segments[index] = image
        self.push_count += 1

    def set_brightness(self, pct: int) -> None:
        self.brightness = pct

    async def inject(self, *events: Event) -> None:
        for event in events:
            await self._queue.put(event)

    async def events(self) -> AsyncIterator[Event]:
        while True:
            yield await self._queue.get()
