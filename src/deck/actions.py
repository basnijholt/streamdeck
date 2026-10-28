"""Dispatch of configured actions to whoever registered for their type."""

from collections.abc import Awaitable, Callable

from deck.config import Action

Handler = Callable[[Action], Awaitable[None]]


class ActionRunner:
    """Routes an action to the handler registered for the longest matching prefix.

    A handler registered for ``"mac."`` runs ``mac.app`` and ``mac.url``; one registered
    for ``"mac.audio."`` takes ``mac.audio.output`` instead. Handlers raise to report failure.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, prefix: str, handler: Handler) -> None:
        self._handlers[prefix] = handler

    async def run(self, action: Action) -> None:
        """Run `action`; raise `KeyError` if no handler is registered for its type."""
        prefixes = [p for p in self._handlers if action.type.startswith(p)]
        if not prefixes:
            raise KeyError(action.type)
        await self._handlers[max(prefixes, key=len)](action)
