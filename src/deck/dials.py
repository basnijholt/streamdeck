"""What the daemon hands to dial handlers, and the helpers they share."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from deck.actions import ActionRunner
from deck.config import Config, DialSpec
from deck.state import StateStore

log = logging.getLogger(__name__)


@dataclass
class Services:
    """Everything the daemon and the dial handlers talk to.

    `ddc`, `mac`, `ha` and `t3` stay None until the task that provides them sets them.
    """

    store: StateStore
    config: Config
    actions: ActionRunner
    ddc: Any = None
    mac: Any = None
    ha: Any = None
    t3: Any = None


class DialHandler(Protocol):
    async def on_turn(self, ticks: int) -> None: ...

    async def on_press(self) -> None: ...


# Filled by the modules that implement each dial kind; a kind without a factory
# gets no handler, so its events are ignored.
DIAL_FACTORIES: dict[str, Callable[[DialSpec, Services], DialHandler]] = {}

_EMPTY: Any = object()


class Coalescer[T]:
    """Sends values one at a time, dropping all but the newest while a send is in flight.

    The first value of a burst is sent at once (up to the send's first suspension), so a
    single submit never waits for the next event-loop iteration. `send` errors are logged.
    """

    def __init__(self, send: Callable[[T], Awaitable[None]]) -> None:
        self._send = send
        self._pending: T = _EMPTY
        self._busy = False
        self._task: asyncio.Task[None] | None = None

    def submit(self, value: T) -> None:
        self._pending = value
        if not self._busy:
            self._busy = True
            loop = asyncio.get_running_loop()
            self._task = asyncio.Task(self._drain(), loop=loop, eager_start=True)

    async def _drain(self) -> None:
        try:
            while self._pending is not _EMPTY:
                value, self._pending = self._pending, _EMPTY
                try:
                    await self._send(value)
                except Exception:
                    log.exception("send failed")
        finally:
            self._busy = False
