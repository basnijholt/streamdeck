"""The volume and input dials: they drive the Mac through `services.mac`.

Turning is shown at once (the store is written before the Mac answers) and the requests go
through a `Coalescer`, so a fast spin sends the first and the last value, not every tick.
A dial without a connected Mac client does nothing.
"""

from deck.audio import matches
from deck.config import DialSpec
from deck.dials import DIAL_FACTORIES, Coalescer, Services

VOLUME_STEP = 2  # percent per dial tick


class VolumeDial:
    """Turn: Mac output volume up or down. Press: toggle mute."""

    def __init__(self, spec: DialSpec, services: Services) -> None:
        self._services = services
        self._sender = Coalescer[int](self._send)

    async def on_turn(self, ticks: int) -> None:
        store = self._services.store
        mac = self._services.mac
        current = store.get("mac.volume")
        if mac is None or not mac.online or current is None:  # nothing to step from
            return
        level = max(0, min(100, current + VOLUME_STEP * ticks))
        if level != current:
            store.set("mac.volume", level)
            self._sender.submit(level)

    async def on_press(self) -> None:
        if self._services.mac is not None:
            await self._services.mac.request("volume", {"mute": "toggle"})

    async def _send(self, level: int) -> None:
        if self._services.mac is not None:
            await self._services.mac.request("volume", level)


class InputDial:
    """Turn: next or previous available input among the preferred ones. Press: mute the mic."""

    def __init__(self, spec: DialSpec, services: Services) -> None:
        self._services = services
        self._sender = Coalescer[str](self._send)

    async def on_turn(self, ticks: int) -> None:
        store = self._services.store
        mac = self._services.mac
        audio = self._services.config.audio
        if mac is None or not mac.online or audio is None:
            return
        choices = _preferred_inputs(store.get("mac.audio.inputs", []), audio.inputs)
        if not choices:
            return
        current = store.get("mac.audio.input")
        if current in choices:
            target = choices[(choices.index(current) + ticks) % len(choices)]
        else:  # such as AirPods: enter the list from the end the turn points away from
            target = choices[0 if ticks > 0 else -1]
        if target != current:
            store.set("mac.audio.input", target)
            self._sender.submit(target)

    async def on_press(self) -> None:
        if self._services.mac is not None:
            await self._services.mac.request("mic.mute", None)

    async def _send(self, device: str) -> None:
        if self._services.mac is not None:
            await self._services.mac.request("audio.input", device)


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
