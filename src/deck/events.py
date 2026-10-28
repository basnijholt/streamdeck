"""Input events produced by a `Device`."""

from dataclasses import dataclass


@dataclass(frozen=True)
class KeyDown:
    index: int


@dataclass(frozen=True)
class KeyUp:
    index: int


@dataclass(frozen=True)
class DialTurn:
    index: int
    ticks: int


@dataclass(frozen=True)
class DialDown:
    index: int


@dataclass(frozen=True)
class DialUp:
    index: int


@dataclass(frozen=True)
class TouchTap:
    x: int
    y: int


@dataclass(frozen=True)
class TouchSwipe:
    dx: int


@dataclass(frozen=True)
class Connected:
    pass


@dataclass(frozen=True)
class Disconnected:
    pass


Event = (
    KeyDown
    | KeyUp
    | DialTurn
    | DialDown
    | DialUp
    | TouchTap
    | TouchSwipe
    | Connected
    | Disconnected
)
