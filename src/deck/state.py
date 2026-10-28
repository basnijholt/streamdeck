"""Observable key/value store holding everything the deck displays.

Providers write, views read, the render loop subscribes. Callbacks run
synchronously inside `set`/`update`; the store knows nothing about asyncio.
Values are treated as immutable: replace a value instead of mutating it.
"""

from collections.abc import Callable
from typing import Any

# Module-level aliases: inside the class body, `set` is the method below.
ChangedKeys = set[str]
Subscriber = Callable[[ChangedKeys], None]
_MISSING = object()


class StateStore:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._subscribers: list[Subscriber] = []

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Store `value`; a no-op (and no notification) if it is already stored."""
        if self._store(key, value):
            self._notify({key})

    def update(self, prefix: str, values: dict[str, Any], replace: bool = False) -> None:
        """Store every `values` item under `prefix + name`, notifying once.

        With `replace`, existing keys starting with `prefix` that are not in `values`
        are dropped first, so stale state cannot outlive the snapshot replacing it.
        """
        changed: set[str] = set()
        if replace:
            wanted = {prefix + name for name in values}
            for key in [k for k in self._data if k.startswith(prefix) and k not in wanted]:
                del self._data[key]
                changed.add(key)
        for name, value in values.items():
            if self._store(prefix + name, value):
                changed.add(prefix + name)
        if changed:
            self._notify(changed)

    def subscribe(self, callback: Subscriber) -> None:
        """Call `callback` with the set of changed keys after every change."""
        self._subscribers.append(callback)

    def snapshot(self) -> dict[str, Any]:
        return dict(self._data)

    def _store(self, key: str, value: Any) -> bool:
        old = self._data.get(key, _MISSING)
        # The type check keeps 1 / True / 1.0 (equal in Python) distinguishable.
        if old is not _MISSING and type(old) is type(value) and old == value:
            return False
        self._data[key] = value
        return True

    def _notify(self, changed: ChangedKeys) -> None:
        for callback in tuple(self._subscribers):
            callback(set(changed))
