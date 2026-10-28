"""The Mac side of `deck remote`: runs the allowlisted kinds and polls the state to push.

Every command goes through a `Runner` as an argv list: no shell, and AppleScript gets its
arguments through `on run argv`, never spliced into script text. `MacExecutor` checks the
typed arguments of each kind itself (the server only re-checks URLs); `MacPoller` reads the
audio, volume and Bluetooth state, and tolerates any single query failing.

macOS tools used: `open`, `osascript`, `screencapture`, `pmset`, `SwitchAudioSource`
(brew switchaudio-osx >= 1.2, for `-f json`) and `blueutil`. stdout belongs to the protocol,
so nothing here prints.
"""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from deck.audio import matches, preferred_input
from deck.remote.server import Executor, Poller, RemoteError, is_http_url
from deck.runner import Result, Runner

log = logging.getLogger(__name__)

DEFAULT_SCRIPTS_DIR = Path("~/.config/deck/scripts")

ACTION_TIMEOUT = 10  # seconds; every command but the screenshot stays under the PC's 15 s limit
QUERY_TIMEOUT = 5  # seconds; the poller must not stall on a hung tool
SCREENSHOT_TIMEOUT = 60  # seconds the selection UI may stay up; it is not awaited by the request
BT_CONNECT_TIMEOUT = 4  # seconds for `blueutil --connect`; the rest of the budget goes to polling
BT_WAIT = 10  # seconds, from the start of the request, for a Bluetooth device to appear
BT_POLL_INTERVAL = 0.5  # seconds
DEFAULT_INPUT_VOLUME = 75  # restored on unmute when no level was saved
MAX_ERROR_LENGTH = 200

_SCRIPT_NAME = re.compile(r"[A-Za-z0-9._-]+")  # used with fullmatch: `$` accepts "\n"

# --- AppleScript ------------------------------------------------------------

# argv: 1 = text the tab's URL must contain, 2 = URL to open, 3 = "true" to navigate a found
# tab to that URL. Brave is launched by this script when it is not running: only a deliberate
# tab press runs it.
BRAVE_TAB_SCRIPT = """\
on run argv
    set needle to item 1 of argv
    set targetURL to item 2 of argv
    set shouldNavigate to ((item 3 of argv) is "true")
    tell application "Brave Browser"
        repeat with w in windows
            set i to 0
            repeat with t in tabs of w
                set i to i + 1
                if (URL of t) contains needle then
                    if shouldNavigate and ((URL of t) is not targetURL) then
                        set URL of t to targetURL
                    end if
                    set active tab index of w to i
                    set index of w to 1
                    activate
                    return
                end if
            end repeat
        end repeat
        if (count of windows) is 0 then
            make new window
            set URL of active tab of front window to targetURL
        else
            make new tab at end of tabs of front window with properties {URL:targetURL}
        end if
        activate
    end tell
end run
"""

# Must never launch Brave, hence the `is running` check before any `tell`.
ACTIVE_URL_SCRIPT = """\
if application "Brave Browser" is running then
    tell application "Brave Browser"
        if (count of windows) is 0 then return ""
        return URL of active tab of front window
    end tell
end if
return ""
"""

KM_SCRIPT = """\
on run argv
    tell application "Keyboard Maestro Engine" to do script (item 1 of argv)
end run
"""

VOLUME_SCRIPT = """\
on run argv
    set volume output volume ((item 1 of argv) as integer)
end run
"""

VOLUME_MUTE_SCRIPT = """\
set volume output muted (not (output muted of (get volume settings)))
"""

INPUT_VOLUME_SCRIPT = """\
on run argv
    set volume input volume ((item 1 of argv) as integer)
end run
"""

GET_VOLUME_SETTINGS = ["osascript", "-e", "get volume settings"]
GET_INPUT_VOLUME = ["osascript", "-e", "input volume of (get volume settings)"]

# --- argv builders ----------------------------------------------------------


def app_argv(name: str) -> list[str]:
    return ["open", "-a", name]


def url_argv(url: str) -> list[str]:
    return ["open", url]


def tab_argv(match: str, url: str, navigate: bool = False) -> list[str]:
    return ["osascript", "-e", BRAVE_TAB_SCRIPT, match, url, "true" if navigate else "false"]


def active_url_argv() -> list[str]:
    return ["osascript", "-e", ACTIVE_URL_SCRIPT]


def km_argv(macro: str) -> list[str]:
    return ["osascript", "-e", KM_SCRIPT, macro]


def volume_argv(level: int) -> list[str]:
    return ["osascript", "-e", VOLUME_SCRIPT, str(level)]


def volume_mute_argv() -> list[str]:
    return ["osascript", "-e", VOLUME_MUTE_SCRIPT]


def input_volume_argv(level: int) -> list[str]:
    return ["osascript", "-e", INPUT_VOLUME_SCRIPT, str(level)]


def screenshot_argv() -> list[str]:
    return ["screencapture", "-ic"]


def lock_argv() -> list[str]:
    return ["pmset", "displaysleepnow"]


def switch_argv(kind: str, name: str) -> list[str]:
    """Select the `kind` ("output" or "input") audio device called `name`."""
    return ["SwitchAudioSource", "-t", kind, "-s", name]


def list_devices_argv(kind: str) -> list[str]:
    return ["SwitchAudioSource", "-a", "-t", kind, "-f", "json"]


def current_device_argv(kind: str) -> list[str]:
    return ["SwitchAudioSource", "-c", "-t", kind, "-f", "json"]


BT_CONNECTED = ["blueutil", "--connected", "--format", "json"]

# --- parsing ----------------------------------------------------------------


def parse_json_objects(out: str) -> list[dict[str, Any]]:
    """`SwitchAudioSource -f json` output: one JSON object per line."""
    objects = []
    for line in out.splitlines():
        if line.strip():
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("expected a JSON object per line")
            objects.append(obj)
    return objects


def parse_device_names(out: str) -> list[str]:
    names = [obj["name"] for obj in parse_json_objects(out)]
    if not all(isinstance(name, str) for name in names):
        raise ValueError("device name is not a string")
    return names


def parse_current_device(out: str) -> str:
    names = parse_device_names(out)
    if not names:
        raise ValueError("no current device")
    return names[0]


def parse_bt_devices(out: str) -> list[dict[str, Any]]:
    """`blueutil --connected --format json`: a JSON array of device objects."""
    devices = json.loads(out)
    if not isinstance(devices, list) or not all(isinstance(d, dict) for d in devices):
        raise ValueError("expected a JSON array of objects")
    return devices


def parse_bt_addresses(out: str) -> list[str]:
    return [d["address"] for d in parse_bt_devices(out) if isinstance(d.get("address"), str)]


def parse_volume_settings(out: str) -> dict[str, str]:
    """`output volume:42, input volume:75, alert volume:100, output muted:false` as a dict."""
    settings = {}
    for part in out.strip().split(","):
        key, sep, value = part.partition(":")
        if not sep:
            raise ValueError(f"unexpected volume settings: {out.strip()!r}")
        settings[key.strip()] = value.strip()
    return settings


def _percent(value: str | None) -> int | None:
    """A volume level, or None when absent or `missing value`."""
    return int(value) if value is not None and value.isdecimal() else None


def _same_address(a: str, b: str) -> bool:
    return a.casefold().replace(":", "-") == b.casefold().replace(":", "-")


# --- argument checks --------------------------------------------------------


def _is_arg_text(value: Any) -> bool:
    """A non-empty string that no tool can mistake for an option."""
    return isinstance(value, str) and bool(value) and not value.startswith("-")


def _bad_args(kind: str) -> RemoteError:
    return RemoteError(f"bad args for {kind}")


def _first_line(result: Result, argv: list[str]) -> str:
    for text in (result.err, result.out):
        lines = text.strip().splitlines()
        if lines:
            return lines[0].strip()[:MAX_ERROR_LENGTH]
    return f"{argv[0]} failed (rc {result.rc})"


# --- executor ---------------------------------------------------------------


class MacExecutor(Executor):
    """Runs the allowlisted kinds as macOS commands."""

    def __init__(
        self,
        runner: Runner,
        scripts_dir: Path = DEFAULT_SCRIPTS_DIR,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._scripts_dir = scripts_dir.expanduser()
        self._sleep = sleep
        self._clock = clock
        self.input_prefs: list[str] = []  # from the hello; the poller's input pin uses them
        self.pin_input = False
        self.coreaudio_muted = False  # the mic's mute flag when it has no input volume
        self._saved_input_volume = DEFAULT_INPUT_VOLUME
        self._mic_lock = asyncio.Lock()  # a toggle reads then writes; presses must not interleave
        self._screenshot_task: asyncio.Task[None] | None = None
        self._handlers: dict[str, Callable[[Any], Awaitable[Any]]] = {
            "app": self._app,
            "url": self._url,
            "tab": self._tab,
            "audio.output": self._audio_output,
            "audio.input": self._audio_input,
            "mic.mute": self._mic_mute,
            "volume": self._volume,
            "screenshot": self._screenshot,
            "km": self._km,
            "script": self._script,
            "lock": self._lock,
            "active_url": self._active_url,
        }

    def configure(self, hello: dict[str, Any]) -> None:
        audio = hello.get("audio")
        audio = audio if isinstance(audio, dict) else {}
        inputs = audio.get("inputs")
        prefs = inputs if isinstance(inputs, list) else []
        self.input_prefs = [p for p in prefs if isinstance(p, str)]
        self.pin_input = audio.get("pin_input") is True

    async def aclose(self) -> None:
        """Stop the screenshot selection still running in the background, if any."""
        await self._stop_screenshot()

    async def run(self, kind: str, args: Any) -> Any:
        handler = self._handlers.get(kind)
        if handler is None:
            raise RemoteError(f"kind not allowed: {kind}")
        return await handler(args)

    async def _ok(self, argv: list[str], timeout: float = ACTION_TIMEOUT) -> Result:
        """Run `argv`; a non-zero exit becomes a `RemoteError` with the first line of stderr."""
        result = await self._runner.run(argv, timeout=timeout)
        if result.rc != 0:
            raise RemoteError(_first_line(result, argv))
        return result

    async def _app(self, args: Any) -> None:
        if not _is_arg_text(args):
            raise _bad_args("app")
        await self._ok(app_argv(args))

    async def _url(self, args: Any) -> None:
        if not is_http_url(args):
            raise _bad_args("url")
        await self._ok(url_argv(args))

    async def _tab(self, args: Any) -> None:
        if not (
            isinstance(args, dict)
            and _is_arg_text(args.get("match"))
            and is_http_url(args.get("url"))
            and isinstance(args.get("navigate", False), bool)
        ):
            raise _bad_args("tab")
        await self._ok(tab_argv(args["match"], args["url"], args.get("navigate", False)))

    async def _km(self, args: Any) -> None:
        if not _is_arg_text(args):  # osascript would read a leading "-" as an option
            raise _bad_args("km")
        await self._ok(km_argv(args))

    async def _volume(self, args: Any) -> None:
        if isinstance(args, int) and not isinstance(args, bool) and 0 <= args <= 100:
            await self._ok(volume_argv(args))
        elif isinstance(args, dict) and args == {"mute": "toggle"}:
            await self._ok(volume_mute_argv())
        else:
            raise _bad_args("volume")

    async def _screenshot(self, args: Any) -> None:
        """Bring up the selection UI and return: `screencapture -ic` runs until the user is done."""
        await self._stop_screenshot()
        self._screenshot_task = asyncio.create_task(self._capture())

    async def _capture(self) -> None:
        argv = screenshot_argv()
        try:
            result = await self._runner.run(argv, timeout=SCREENSHOT_TIMEOUT)
        except Exception:
            log.exception("%s failed", argv[0])
            return
        if result.rc != 0:  # Esc cancels the selection, which is normal
            log.debug("%s: %s", argv[0], _first_line(result, argv))

    async def _stop_screenshot(self) -> None:
        task, self._screenshot_task = self._screenshot_task, None
        if task is not None:
            task.cancel()  # the runner kills the process when cancelled
            await asyncio.gather(task, return_exceptions=True)

    async def _lock(self, args: Any) -> None:
        await self._ok(lock_argv())

    async def _active_url(self, args: Any) -> str | None:
        result = await self._ok(active_url_argv())
        return result.out.strip() or None

    async def _audio_input(self, args: Any) -> None:
        if not isinstance(args, str) or not args:
            raise _bad_args("audio.input")
        await self._ok(switch_argv("input", args))

    async def _audio_output(self, args: Any) -> None:
        if not (
            isinstance(args, dict)
            and isinstance(args.get("name"), str)
            and bool(args["name"])
            and isinstance(args.get("match"), str)
            and bool(args["match"])
            and (args.get("bt") is None or _is_arg_text(args["bt"]))
            and isinstance(args.get("disconnect", False), bool)
        ):
            raise _bad_args("audio.output")
        deadline = self._clock() + BT_WAIT
        name, match, bt = args["name"], args["match"], args.get("bt")
        if args.get("disconnect", False):
            if bt is None:
                raise _bad_args("audio.output")
            await self._ok(["blueutil", "--disconnect", bt])
            return
        if bt is None:
            deadline = self._clock()  # nothing to wait for: one look at the devices
        elif not await self._bt_connected(bt):
            await self._ok(["blueutil", "--connect", bt], timeout=BT_CONNECT_TIMEOUT)
        device = await self._wait_for_output(match, deadline)
        if device is None:
            raise RemoteError(f"{name} did not appear")
        await self._ok(switch_argv("output", device))

    async def _bt_connected(self, address: str) -> bool:
        result = await self._ok(BT_CONNECTED)
        try:
            devices = parse_bt_devices(result.out)
        except ValueError as exc:
            raise RemoteError("unexpected blueutil output") from exc
        return any(
            isinstance(d.get("address"), str) and _same_address(d["address"], address)
            for d in devices
        )

    async def _wait_for_output(self, match: str, deadline: float) -> str | None:
        """The first output device matching `match`.

        Polls every 0.5 s; the last poll is at the deadline, and nothing sleeps after it.
        """
        while True:
            result = await self._ok(list_devices_argv("output"), timeout=QUERY_TIMEOUT)
            try:
                names = parse_device_names(result.out)
            except ValueError as exc:
                raise RemoteError("unexpected SwitchAudioSource output") from exc
            for device in names:
                if matches(device, match):
                    return device
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            await self._sleep(min(BT_POLL_INTERVAL, remaining))

    async def _mic_mute(self, args: Any) -> None:
        """Toggle the mic: input volume 0 is muted, because its state reads back."""
        async with self._mic_lock:
            out = (await self._ok(GET_INPUT_VOLUME)).out.strip()
            if out == "missing value":  # the device has no software input volume
                await self._ok(["SwitchAudioSource", "-m", "toggle", "-t", "input"])
                self.coreaudio_muted = not self.coreaudio_muted
                return
            level = _percent(out)
            if level is None:
                raise RemoteError("unexpected input volume")
            if level > 0:
                self._saved_input_volume = level
                await self._ok(input_volume_argv(0))
            else:
                await self._ok(input_volume_argv(self._saved_input_volume))

    async def _script(self, args: Any) -> None:
        if not (isinstance(args, str) and _SCRIPT_NAME.fullmatch(args) and args not in {".", ".."}):
            raise _bad_args("script")
        base = self._scripts_dir.resolve()
        path = (base / args).resolve()
        if not path.is_relative_to(base):  # a symlink pointing out of the directory
            raise _bad_args("script")
        if not path.is_file():
            raise RemoteError(f"script not found: {args}")
        if not os.access(path, os.X_OK):
            raise RemoteError(f"script not executable: {args}")
        await self._ok([str(path)])


# --- poller -----------------------------------------------------------------


class MacPoller(Poller):
    """Reads the Mac's audio, volume and Bluetooth state."""

    def __init__(self, runner: Runner, executor: MacExecutor) -> None:
        self._runner = runner
        self._executor = executor

    async def snapshot(self) -> dict[str, Any]:
        """The current state; a key whose query failed is left out. Values are fresh each call."""
        output, input_, outputs, inputs, settings, bt = await asyncio.gather(
            self._query(current_device_argv("output"), parse_current_device),
            self._query(current_device_argv("input"), parse_current_device),
            self._query(list_devices_argv("output"), parse_device_names),
            self._query(list_devices_argv("input"), parse_device_names),
            self._query(GET_VOLUME_SETTINGS, parse_volume_settings),
            self._query(BT_CONNECTED, parse_bt_addresses),
        )
        if input_ is not None and inputs is not None:
            input_ = await self._pin_input(input_, inputs)

        state: dict[str, Any] = {}
        if output is not None:
            state["audio.output"] = output
        if input_ is not None:
            state["audio.input"] = input_
        if outputs is not None:
            state["audio.outputs"] = list(outputs)
        if inputs is not None:
            state["audio.inputs"] = list(inputs)
        if settings is not None:
            if (volume := _percent(settings.get("output volume"))) is not None:
                state["volume"] = volume
            input_volume = settings.get("input volume")
            if input_volume == "missing value":
                state["mic.muted"] = self._executor.coreaudio_muted
            elif (level := _percent(input_volume)) is not None:
                state["mic.muted"] = level == 0
        if bt is not None:
            state["bt.connected"] = list(bt)
        return state

    async def _query[T](self, argv: list[str], parse: Callable[[str], T]) -> T | None:
        result = await self._runner.run(argv, timeout=QUERY_TIMEOUT)
        if result.rc != 0:
            log.debug("%s failed: %s", argv[0], _first_line(result, argv))
            return None
        try:
            return parse(result.out)
        except (ValueError, KeyError, TypeError) as exc:
            log.debug("%s: unparseable output: %s", argv[0], exc)
            return None

    async def _pin_input(self, current: str, available: list[str]) -> str:
        """With `pin_input`, switch back to the preferred input; return the current input."""
        executor = self._executor
        if not executor.pin_input:
            return current
        preferred = preferred_input(available, executor.input_prefs)
        if preferred is None or preferred == current:
            return current
        argv = switch_argv("input", preferred)
        result = await self._runner.run(argv, timeout=QUERY_TIMEOUT)
        if result.rc != 0:
            log.warning("pinning the input to %s failed: %s", preferred, _first_line(result, argv))
            return current
        return preferred
