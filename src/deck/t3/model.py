"""T3 Code thread model and the rules that turn threads into key states."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

MAX_SLOTS = 6

ThreadState = Literal["working", "needs_you", "done", "error"]

_WORKING = {"preparing", "queued", "starting", "running"}
_ERROR = {"failed", "interrupted", "cancelled", "rolled_back"}


@dataclass(frozen=True)
class Thread:
    server: str
    thread_id: str
    project_id: str
    title: str
    status: str
    pending_requests: int
    model: str
    instance_id: str
    options: tuple[tuple[str, Any], ...]
    settled: bool
    archived: bool
    parent_thread_id: str | None
    updated_at: datetime  # timezone-aware


@dataclass(frozen=True)
class Pending:
    """A change the effort or model dial is about to apply, for the thread titled `title`."""

    title: str
    current: str
    pending: str


def thread_state(t: Thread) -> ThreadState:
    """Key state for a thread. Waiting on you beats every other status.

    An unrecognised status counts as done: a newer server must not paint keys red.
    """
    if t.status == "waiting" or t.pending_requests > 0:
        return "needs_you"
    if t.status in _WORKING:
        return "working"
    if t.status in _ERROR:
        return "error"
    return "done"


def visible_threads(threads: list[Thread]) -> list[Thread]:
    """Threads that get a slot: open, top-level, most recently updated first, at most six."""
    shown = [t for t in threads if not t.settled and not t.archived and t.parent_thread_id is None]
    shown.sort(key=lambda t: t.updated_at, reverse=True)
    return shown[:MAX_SLOTS]
