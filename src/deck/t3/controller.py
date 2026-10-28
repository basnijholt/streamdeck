"""T3 Code on the deck: thread keys, focusing a thread, and the effort and model dials.

`T3Controller` merges the servers' thread lists into `t3.threads` (which the slot keys show),
opens threads in the Mac's Brave, and hands out the two dials.

The dials change only the thread that is open in the active tab of Brave's front window, and
never trust a URL that is not fresh. The first turn (or the first after 10 s without one) asks
the Mac for the tab's URL and remembers which thread it names; every later turn only steps the
pending value. A press asks the Mac again and applies the change only if the very same thread
is still in front. Whenever the deck is unsure (no Mac, another tab, another thread, an error)
it shows a message on the strip and changes nothing.

The first read runs as a task and the strip says `reading…` meanwhile. The daemon awaits
`on_turn` inline, so awaiting a slow Mac would hold up every other event, for up to the Mac
request timeout of 15 s. Turns that arrive during the read are added up, not lost. A press
while reading is ignored: the strip has shown nothing yet that it could apply.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from deck.config import DialSpec, T3Config, T3Server
from deck.dials import DIAL_FACTORIES, Services
from deck.mac.client import MacClient, MacError, MacOffline
from deck.state import StateStore
from deck.t3.model import Thread, thread_state, visible_threads
from deck.t3.source import ModelSelection, T3Error, T3Source

log = logging.getLogger(__name__)

IDLE_SECONDS = 10.0  # without a turn this long, the next turn reads the front tab again
MESSAGE_SECONDS = 2.0  # how long a message stays on the strip
TICK_SECONDS = 15.0  # how often the elapsed time of working threads is drawn again

NO_THREAD = "no T3 thread in front"
CHANGED = "thread changed — not applied"
OFFLINE_APPLY = "Mac offline — not applied"
OFFLINE_TURN = "Mac offline"
NO_MAC = "no Mac configured"
READING = "reading…"


class T3Controller:
    """The deck's side of T3 Code. `sleep` waits out the timers that clear the strip."""

    def __init__(
        self,
        store: StateStore,
        sources: dict[str, T3Source],
        cfg: T3Config,
        mac: MacClient | None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.store = store
        self.sources = sources
        self.cfg = cfg
        self.mac = mac
        self.clock = clock
        self.sleep = sleep
        self._tasks: set[asyncio.Task[None]] = set()
        self._dials: dict[str, _ThreadDial] = {}
        self._status_since: dict[tuple[str, str], tuple[str, float]] = {}
        self._elapsed_ticks = 0
        store.subscribe(self._on_store_change)
        self._merge()

    # --- tasks ------------------------------------------------------------------

    def spawn(self, coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run `coroutine` as a task that `aclose` cancels. It starts at once."""
        task = asyncio.Task(coroutine, loop=asyncio.get_running_loop(), eager_start=True)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def aclose(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- the thread list --------------------------------------------------------

    def _on_store_change(self, changed: set[str]) -> None:
        if any(key.startswith("t3.threads.") for key in changed):
            self._merge()

    def _merge(self) -> None:
        """Put every server's thread list into `t3.threads`, and note when statuses began."""
        threads: list[Thread] = []
        for server in self.cfg.servers:
            threads += self.store.get(f"t3.threads.{server.name}", [])
        now = self.clock()
        since: dict[tuple[str, str], tuple[str, float]] = {}
        for thread in threads:
            key = (thread.server, thread.thread_id)
            known = self._status_since.get(key)
            if known is None or known[0] != thread.status:
                known = (thread.status, now)
            since[key] = known
        self._status_since = since
        self.store.set("t3.threads", threads)

    def running_for(self, thread: Thread) -> float:
        """Seconds since the deck first saw `thread` with its current status."""
        key = (thread.server, thread.thread_id)
        known = self._status_since.get(key)
        if known is None or known[0] != thread.status:
            known = self._status_since[key] = (thread.status, self.clock())
        return self.clock() - known[1]

    async def tick_forever(self) -> None:
        """Touch the store now and then while a thread works, so the keys draw its elapsed time:
        nothing else changes while a run is quiet."""
        while True:
            await self.sleep(TICK_SECONDS)
            threads = self.store.get("t3.threads", [])
            if any(thread_state(thread) == "working" for thread in threads):
                self._elapsed_ticks += 1
                self.store.set("t3.elapsed", self._elapsed_ticks)

    # --- opening threads --------------------------------------------------------

    async def focus(self, slot: int) -> None:
        """Bring the thread in `slot` to the front in Brave. An empty slot does nothing."""
        threads = visible_threads(self.store.get("t3.threads", []))
        if slot >= len(threads):
            return
        thread = threads[slot]
        source = self.sources.get(thread.server)
        server = next((s for s in self.cfg.servers if s.name == thread.server), None)
        if source is None or server is None:
            log.warning("no t3 server %s to open thread %s on", thread.server, thread.thread_id)
            return
        await self._open(server, source.thread_url(thread))

    async def new_thread(self) -> None:
        """Bring the first server's T3 to the front in Brave, ready for a new thread."""
        if not self.cfg.servers:
            log.warning("no t3 server to open a new thread on")
            return
        server = self.cfg.servers[0]
        await self._open(server, server.web_url)

    async def _open(self, server: T3Server, url: str) -> None:
        """Navigate the Brave tab of `server` to `url`; raises what `MacClient.request` raises."""
        if self.mac is None:
            log.warning("no Mac configured: cannot open %s", url)
            return
        match = urlsplit(server.web_url).netloc
        await self.mac.request("tab", {"match": match, "url": url, "navigate": True})

    # --- the thread in front ----------------------------------------------------

    async def thread_in_front(self) -> Thread | None:
        """The thread open in the active tab of Brave's front window, read from the Mac now.

        None when that tab is not a thread of a configured server, or of one the deck does not
        know. Raises `MacOffline` and `MacError` as `MacClient.request` does.
        """
        if self.mac is None:
            raise MacOffline(NO_MAC)
        url = await self.mac.request("active_url", None)
        if not isinstance(url, str):
            return None
        for name, source in self.sources.items():
            thread_id = source.thread_id_from_url(url)
            if thread_id is not None:
                threads = self.store.get("t3.threads", [])
                return next(
                    (t for t in threads if t.server == name and t.thread_id == thread_id), None
                )
        return None

    # --- dials ------------------------------------------------------------------

    def dial(self, kind: str) -> "_ThreadDial":
        """The dial for `kind` (`effort` or `model`): always the same object, so a rebuilt page
        keeps the pending value."""
        if kind not in self._dials:
            self._dials[kind] = EffortDial(self) if kind == "effort" else ModelDial(self)
        return self._dials[kind]


@dataclass
class _Capture:
    """The thread a dial's turns are about, and the value a press would apply."""

    server: str
    thread_id: str
    title: str
    instance_id: str
    current: str
    choices: tuple[str, ...]
    value: str


class _ThreadDial:
    """Turn: pick the next effort or model for the thread in front. Press: apply it.

    Subclasses say what is being picked; this class holds the rules of the turn and the press.
    """

    kind: str  # `effort` or `model`: the dial's `t3.pending.<kind>` key

    def __init__(self, controller: T3Controller) -> None:
        self._ctl = controller
        self._capture: _Capture | None = None
        self._ticks = 0  # turns waiting for the first read
        self._last_turn = 0.0
        self._read: asyncio.Task[None] | None = None
        self._applying = False
        self._timer: asyncio.Task[None] | None = None

    # What a kind picks.

    def _current(self, thread: Thread) -> str | None:
        """The thread's value, or None if its provider has none to pick."""
        raise NotImplementedError

    def _choices(self, thread: Thread) -> list[str]:
        raise NotImplementedError

    def _unavailable(self, instance_id: str) -> str:
        """The message for a thread whose provider cannot take this kind of change."""
        raise NotImplementedError

    def _with_value(self, selection: ModelSelection, value: str) -> ModelSelection | None:
        """`selection` with only this kind's value replaced; None if it has none to replace."""
        raise NotImplementedError

    # Turning.

    async def on_turn(self, ticks: int) -> None:
        ctl = self._ctl
        if ctl.mac is None:
            self._message(NO_MAC)
            return
        if self._applying:
            return  # the outcome of the press is about to replace the pending value
        now = ctl.clock()
        if self._reading:
            self._ticks += ticks
        elif self._capture is None or now - self._last_turn > IDLE_SECONDS:
            self._begin(ticks)
        else:
            self._step(self._capture, ticks)
            self._show(self._capture)
        self._last_turn = now

    def _begin(self, ticks: int) -> None:
        """Start reading which thread is in front; the turns so far are added up meanwhile."""
        self._cancel_timer()
        self._capture = None
        self._ticks = ticks
        self._publish(READING)
        self._read = self._ctl.spawn(self._read_front())

    @property
    def _reading(self) -> bool:
        return self._read is not None and not self._read.done()

    async def _read_front(self) -> None:
        try:
            thread = await self._ctl.thread_in_front()
            if thread is None:
                self._message(NO_THREAD)
                return
            current = self._current(thread)
            choices = self._choices(thread)
            if current is None or not choices:
                self._message(self._unavailable(thread.instance_id))
                return
        except MacOffline:
            self._message(OFFLINE_TURN)
        except (MacError, T3Error) as exc:
            self._message(f"error: {exc}")
        except Exception as exc:
            log.exception("reading the thread in front failed")
            self._message(f"error: {exc}")
        else:
            capture = _Capture(
                thread.server,
                thread.thread_id,
                thread.title,
                thread.instance_id,
                current,
                tuple(choices),
                current,
            )
            self._step(capture, self._ticks)
            self._capture = capture
            self._show(capture)

    @staticmethod
    def _step(capture: _Capture, ticks: int) -> None:
        """Move the pending value `ticks` places along the choices, wrapping around.

        A value that is not among the choices is entered from the end the turn points away
        from, as the Mac's input dial does.
        """
        if ticks == 0:
            return
        choices = capture.choices
        if capture.value in choices:
            position = choices.index(capture.value)
        else:
            position = -1 if ticks > 0 else len(choices)
        capture.value = choices[(position + ticks) % len(choices)]

    def _show(self, capture: _Capture) -> None:
        self._publish(f"{capture.title} · {capture.current} → {capture.value}")
        self._arm(IDLE_SECONDS)

    # Pressing.

    async def on_press(self) -> None:
        capture = self._capture
        if capture is None or self._reading or self._applying:
            return  # nothing shown to apply, or a press is already on its way
        self._applying = True
        self._cancel_timer()  # a slow apply must not lose the capture it is working from
        try:
            await self._apply(capture)
        except MacOffline:
            self._message(OFFLINE_APPLY)
        except (MacError, T3Error) as exc:
            self._message(f"error: {exc}")
        except Exception as exc:
            log.exception("applying the %s failed", self.kind)
            self._message(f"error: {exc}")
        finally:
            self._applying = False

    async def _apply(self, capture: _Capture) -> None:
        """Set the pending value if the thread the turns were about is still the one in front."""
        front = await self._ctl.thread_in_front()
        if front is None or (front.server, front.thread_id) != (capture.server, capture.thread_id):
            self._message(CHANGED)
            return
        source = self._ctl.sources[capture.server]
        # What the thread has now, not what the list showed a moment ago: a set replaces it all.
        current = await source.get_model_selection(capture.thread_id)
        if current.instance_id != capture.instance_id:
            self._message(CHANGED)  # the choices were for another provider
            return
        selection = self._with_value(current, capture.value)
        if selection is None:
            self._message(self._unavailable(current.instance_id))
            return
        await source.set_model_selection(capture.thread_id, selection)
        self._cancel_timer()
        self._reset()

    # The strip.

    def _publish(self, text: str | None) -> None:
        self._ctl.store.set(f"t3.pending.{self.kind}", text)

    def _message(self, text: str) -> None:
        """Show `text` for a moment, then clear it. The next turn reads the front tab again."""
        self._capture = None
        self._ticks = 0
        self._publish(text)
        self._arm(MESSAGE_SECONDS)

    def _reset(self) -> None:
        self._capture = None
        self._ticks = 0
        self._publish(None)

    def _arm(self, delay: float) -> None:
        """Clear the strip and the capture in `delay` seconds, unless something re-arms first."""
        self._cancel_timer()
        self._timer = self._ctl.spawn(self._expire(delay))

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def _expire(self, delay: float) -> None:
        await self._ctl.sleep(delay)
        self._timer = None
        self._reset()


class EffortDial(_ThreadDial):
    """Turn: the next reasoning effort for the thread in front. Press: apply it."""

    kind = "effort"

    def _current(self, thread: Thread) -> str | None:
        value = dict(thread.options).get("effort")
        return None if value is None else str(value)

    def _choices(self, thread: Thread) -> list[str]:
        return self._ctl.cfg.efforts

    def _unavailable(self, instance_id: str) -> str:
        return f"effort n/a for {instance_id}"

    def _with_value(self, selection: ModelSelection, value: str) -> ModelSelection | None:
        if not any(key == "effort" for key, _ in selection.options):
            return None
        options = tuple((key, value if key == "effort" else old) for key, old in selection.options)
        return ModelSelection(selection.instance_id, selection.model, options)


class ModelDial(_ThreadDial):
    """Turn: the next model of the thread's own provider instance. Press: apply it."""

    kind = "model"

    def _current(self, thread: Thread) -> str | None:
        return thread.model

    def _choices(self, thread: Thread) -> list[str]:
        return self._ctl.cfg.models.get(thread.instance_id, [])

    def _unavailable(self, instance_id: str) -> str:
        return f"no models for {instance_id}"

    def _with_value(self, selection: ModelSelection, value: str) -> ModelSelection | None:
        return ModelSelection(selection.instance_id, value, selection.options)


def _dial_factory(kind: str) -> Callable[[DialSpec, Services], _ThreadDial]:
    def make(spec: DialSpec, services: Services) -> _ThreadDial:
        if services.t3 is None:
            raise RuntimeError("t3 is not running: restart deck after adding a t3 section")
        return services.t3.dial(kind)

    return make


DIAL_FACTORIES["t3.effort"] = _dial_factory("effort")
DIAL_FACTORIES["t3.model"] = _dial_factory("model")
