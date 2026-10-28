"""The volume and input dials: they drive the Mac through `services.mac`.

Turning is shown at once (the store is written before the Mac answers) and the requests go
through a `Coalescer`, so a fast spin sends the first and the last value, not every tick.
While requests are in flight or waiting, each turn steps from the dial's own target, not from
the store: a state push from the Mac then still reports the value before the spin, and would
make the dial lose ticks. A dial without a connected Mac client does nothing.
"""

import logging

from deck.audio import matches
from deck.config import DialSpec
from deck.dials import DIAL_FACTORIES, Coalescer, Services
from deck.mac.client import MacError, MacOffline

log = logging.getLogger(__name__)

VOLUME_STEP = 2  # percent per dial tick


class VolumeDial:
    """Turn: Mac output volume up or down. Press: toggle mute."""

    def __init__(self, spec: DialSpec, services: Services) -> None:
        self._services = services
        self._sender = Coalescer[tuple[int, int]](self._send)
        self._submitted = 0  # number of the newest request handed to the sender
        self._target: int | None = None  # its level, until the sender has dealt with it

    async def on_turn(self, ticks: int) -> None:
        store = self._services.store
        mac = self._services.mac
        current = self._target if self._target is not None else store.get("mac.volume")
        if mac is None or not mac.online or current is None:  # nothing to step from
            return
        level = max(0, min(100, current + VOLUME_STEP * ticks))
        store.set("mac.volume", level)  # also puts back a level a stale push has overwritten
        if level != current:
            self._submitted += 1
            self._target = level
            self._sender.submit((self._submitted, level))

    async def on_press(self) -> None:
        if self._services.mac is not None:
            await self._services.mac.request("volume", {"mute": "toggle"})

    async def _send(self, request: tuple[int, int]) -> None:
        number, level = request
        try:
            if self._services.mac is not None:
                await self._services.mac.request("volume", level)
        except (MacOffline, MacError) as exc:
            log.warning("setting the volume to %d failed: %s", level, exc)
        finally:
            if number == self._submitted:  # no newer request is waiting
                self._target = None


class InputDial:
    """Turn: next or previous available input among the preferred ones. Press: mute the mic."""

    def __init__(self, spec: DialSpec, services: Services) -> None:
        self._services = services
        self._sender = Coalescer[tuple[int, str]](self._send)
        self._submitted = 0  # number of the newest request handed to the sender
        self._target: str | None = None  # its device, until the sender has dealt with it

    async def on_turn(self, ticks: int) -> None:
        store = self._services.store
        mac = self._services.mac
        audio = self._services.config.audio
        if mac is None or not mac.online or audio is None:
            return
        choices = _preferred_inputs(store.get("mac.audio.inputs", []), audio.inputs)
        if not choices:
            return
        current = self._target if self._target is not None else store.get("mac.audio.input")
        if current in choices:
            target = choices[(choices.index(current) + ticks) % len(choices)]
        else:  # such as AirPods: enter the list from the end the turn points away from
            target = choices[0 if ticks > 0 else -1]
        store.set("mac.audio.input", target)  # also puts back one a stale push has overwritten
        if target != current:
            self._submitted += 1
            self._target = target
            self._sender.submit((self._submitted, target))

    async def on_press(self) -> None:
        if self._services.mac is not None:
            await self._services.mac.request("mic.mute", None)

    async def _send(self, request: tuple[int, str]) -> None:
        number, device = request
        try:
            if self._services.mac is not None:
                await self._services.mac.request("audio.input", device)
        except (MacOffline, MacError) as exc:
            log.warning("switching the input to %s failed: %s", device, exc)
        finally:
            if number == self._submitted:  # no newer request is waiting
                self._target = None


def _preferred_inputs(available: list[str], prefs: list[str]) -> list[str]:
    """The available devices that match a preference, in the order of the preferences."""
    choices: list[str] = []
    for pref in prefs:
        for device in available:
            if matches(device, pref) and device not in choices:
                choices.append(device)
    return choices


DIAL_FACTORIES["volume"] = VolumeDial
DIAL_FACTORIES["input"] = InputDial
