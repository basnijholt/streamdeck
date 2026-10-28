"""Actions and dials that work on this machine: shell commands and monitor brightness."""

from deck.config import Action, DialSpec
from deck.dials import DIAL_FACTORIES, Services
from deck.runner import Runner

BRIGHTNESS_STEP = 2  # percent per dial tick


def register_local_actions(services: Services, runner: Runner) -> None:
    """Register the `shell` and `ddc` actions on `services.actions`."""

    async def shell(action: Action) -> None:
        argv: list[str] = action.args
        if not argv:
            raise ValueError("empty shell command")
        result = await runner.run(argv)
        if result.rc != 0:
            detail = result.err.strip() or result.out.strip()
            raise RuntimeError(f"{argv[0]} exited with {result.rc}: {detail}".rstrip(": "))

    async def ddc(action: Action) -> None:
        if services.ddc is None:
            raise RuntimeError("ddc action needs a 'ddc' section in the config (and a restart)")
        await services.ddc.setvcp(action.args.code, action.args.value)

    services.actions.register("shell", shell)
    services.actions.register("ddc", ddc)


class BrightnessDial:
    """Turn: brightness up or down. Press: jump to the next preset."""

    def __init__(self, spec: DialSpec, services: Services) -> None:
        if services.ddc is None:
            raise RuntimeError(
                "brightness dial needs the ddc service: restart deck after adding a 'ddc' section"
            )
        self._ddc = services.ddc
        self._store = services.store
        self._presets = spec.presets

    async def on_turn(self, ticks: int) -> None:
        current = self._store.get("ddc.brightness")
        if current is not None:  # not read yet: nothing to step from
            self._ddc.set_brightness(current + BRIGHTNESS_STEP * ticks)

    async def on_press(self) -> None:
        if not self._presets:
            return
        current = self._store.get("ddc.brightness")
        if current is None:
            self._ddc.set_brightness(self._presets[0])
            return
        nearest = min(range(len(self._presets)), key=lambda i: abs(self._presets[i] - current))
        self._ddc.set_brightness(self._presets[(nearest + 1) % len(self._presets)])


DIAL_FACTORIES["brightness"] = BrightnessDial
