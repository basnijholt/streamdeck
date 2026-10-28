"""Monitor brightness over DDC/CI, through `ddcutil`.

`ddcutil` is slow (a read takes ~0.2 s), so the bus is found once and kept, writes go
through a `Coalescer`, and every call is serialized. State written to the store:
`ddc.brightness` (0-100) and `ddc.error` (a message while the monitor cannot be reached).
"""

import asyncio
import logging
import re

from deck.dials import Coalescer
from deck.runner import Result, Runner
from deck.state import StateStore

log = logging.getLogger(__name__)

BRIGHTNESS_CODE = 0x10


class DdcError(RuntimeError):
    """`ddcutil` could not find the monitor or failed to talk to it."""


def _failure(result: Result) -> str:
    detail = (result.err.strip() or result.out.strip()).splitlines()
    return f"ddcutil: {detail[-1] if detail else f'exit {result.rc}'}"


class Ddc:
    def __init__(self, runner: Runner, model: str, store: StateStore) -> None:
        self._runner = runner
        self._model = model
        self._store = store
        self._bus: int | None = None
        self._lock = asyncio.Lock()  # one ddcutil call at a time on the I2C bus
        self._writes = 0  # counts brightness writes, so `refresh` can tell its read is stale
        self._sender = Coalescer(self._send)

    async def detect_bus(self) -> int:
        """Find the I2C bus of the monitor whose name contains the model, and remember it."""
        result = await self._runner.run(["ddcutil", "detect", "--brief"])
        if result.rc != 0:
            raise DdcError(_failure(result))
        needle = self._model.casefold()
        for block in re.split(r"\n\s*\n", result.out):
            bus = re.search(r"I2C bus:\s*/dev/i2c-(\d+)", block)
            monitor = re.search(r"Monitor:(.*)", block)
            if bus and monitor and needle and needle in monitor[1].casefold():
                self._bus = int(bus[1])
                return self._bus
        raise DdcError(f"ddcutil: no display matching {self._model!r}")

    async def get_brightness(self) -> int:
        result = await self._run("getvcp", f"{BRIGHTNESS_CODE:x}")
        match = re.search(r"current value =\s*(\d+)", result.out)
        if match is None:
            raise DdcError(f"ddcutil: unexpected getvcp output {result.out.strip()[:80]!r}")
        return int(match[1])

    def set_brightness(self, value: int) -> None:
        """Show `value` (clamped to 0-100) at once and send it, newest value only."""
        value = max(0, min(100, value))
        self._wrote(value)
        self._sender.submit(value)

    async def setvcp(self, code: int, value: int) -> None:
        """Set any VCP feature; raises `DdcError` on failure."""
        await self._run("setvcp", f"{code:x}", str(value))
        if code == BRIGHTNESS_CODE:
            self._wrote(value)

    async def refresh(self) -> None:
        """Read the brightness into the store; raises `DdcError` if the monitor fails."""
        writes = self._writes
        value = await self.get_brightness()
        if writes == self._writes:  # else a newer write is on its way and this read is stale
            self._store.set("ddc.brightness", value)
            self._store.set("ddc.error", None)

    async def poll_forever(self, interval: float = 30) -> None:
        """Re-read the brightness every `interval` seconds. A failure sets `ddc.error`; the loop goes on."""
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                if self._store.get("ddc.error") != message:
                    if isinstance(exc, DdcError):
                        log.warning("%s", message)
                    else:
                        log.exception("brightness read failed")
                self._store.set("ddc.error", message)
            await asyncio.sleep(interval)

    def _wrote(self, brightness: int) -> None:
        self._writes += 1
        self._store.set("ddc.brightness", brightness)

    async def _send(self, value: int) -> None:
        try:
            await self._run("setvcp", f"{BRIGHTNESS_CODE:x}", str(value))
        except DdcError as exc:
            log.warning("%s", exc)
            self._store.set("ddc.error", str(exc))
        else:
            self._store.set("ddc.error", None)

    async def _run(self, verb: str, *args: str) -> Result:
        """Run `ddcutil <verb> <args>` on the monitor's bus.

        The bus is detected on first use. A failure detects it again once and retries, in
        case the monitor moved to another bus; a second failure raises `DdcError`.
        """
        async with self._lock:
            if self._bus is None:
                await self.detect_bus()
            result = await self._runner.run(self._argv(verb, args))
            if result.rc != 0:
                await self.detect_bus()
                result = await self._runner.run(self._argv(verb, args))
            if result.rc != 0:
                raise DdcError(_failure(result))
            return result

    def _argv(self, verb: str, args: tuple[str, ...]) -> list[str]:
        return [
            "ddcutil", "--bus", str(self._bus), "--skip-ddc-checks",
            "--sleep-multiplier", "0.3", verb, *args,
        ]  # fmt: skip
