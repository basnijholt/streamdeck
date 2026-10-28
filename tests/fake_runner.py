"""A `Runner` that records argv instead of starting processes."""

import asyncio
from collections import deque

from deck.runner import Result, Runner


class FakeRunner(Runner):
    """Records every call; answers from scripted results, else `Result(0, "", "")`.

    `block`, when set, holds every call (after recording it) until the event is set.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.block: asyncio.Event | None = None
        self._scripts: dict[tuple[str, ...], deque[Result]] = {}

    def script(self, prefix: list[str], *results: Result) -> None:
        """Answer calls whose argv starts with `prefix` with `results`, in order."""
        self._scripts.setdefault(tuple(prefix), deque()).extend(results)

    async def run(self, argv: list[str], timeout: float = 10) -> Result:
        self.calls.append(list(argv))
        if self.block is not None:
            await self.block.wait()
        matching = [p for p in self._scripts if tuple(argv[: len(p)]) == p]
        if matching:
            queue = self._scripts[max(matching, key=len)]
            if queue:
                return queue.popleft()
        return Result(0, "", "")
