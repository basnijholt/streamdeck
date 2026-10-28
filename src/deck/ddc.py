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
    line = detail[-1] if detail else f"exit {result.rc}"
    return line if line.startswith("ddcutil:") else f"ddcutil: {line}"


class Ddc:
    def __init__(self, runner: Runner, model: str, store: StateStore) -> None:
        self._runner = runner
        self._model = model
        self._store = store
        self._bus: int | None = None
        self._lock = asyncio.Lock()  # one ddcutil call at a time on the I2C bus
        # Numbers the brightness writes handed to the sender and the newest it has dealt with;
        # while they differ a write is in flight or waiting, so a read may be stale.
        self._submitted = 0
        self._finished = 0
        self._sender = Coalescer[tuple[int, int]](self._send)

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
        self._store.set("ddc.brightness", value)
        self._submitted += 1
        self._sender.submit((self._submitted, value))

    async def setvcp(self, code: int, value: int) -> None:
        """Set any VCP feature; raises `DdcError` on failure."""
        await self._run("setvcp", f"{code:x}", str(value))
        if code == BRIGHTNESS_CODE and self._sender_idle():  # else the dial's target wins
            self._store.set("ddc.brightness", value)

    async def refresh(self) -> None:
        """Read the brightness into the store; raises `DdcError` if the monitor fails."""
        value = await self.get_brightness()
        if self._sender_idle():  # else a write is on its way and this read may be stale
            self._store.set("ddc.brightness", value)
            self._store.set("ddc.error", None)

    async def poll_forever(self, interval: float = 30) -> None:
        """Re-read the brightness every `interval` seconds.

        A failure sets `ddc.error`; the loop goes on.
        """
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                self._fail(exc)
            await asyncio.sleep(interval)

    def _sender_idle(self) -> bool:
        return self._finished == self._submitted

    def _fail(self, exc: Exception) -> None:
        """Show `exc` as `ddc.error`, logging it only when it differs from the one shown."""
        message = str(exc) or type(exc).__name__
        if self._store.get("ddc.error") != message:
            log.error("%s", message, exc_info=None if isinstance(exc, DdcError) else exc)
        self._store.set("ddc.error", message)

    async def _send(self, write: tuple[int, int]) -> None:
        number, value = write
        try:
            await self._run("setvcp", f"{BRIGHTNESS_CODE:x}", str(value))
        except Exception as exc:
            self._fail(exc)
        else:
            self._store.set("ddc.error", None)
        finally:
            self._finished = number

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
