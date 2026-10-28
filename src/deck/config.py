"""YAML configuration models for the deck daemon.

The whole document is validated at once so `deck check` can report every
problem with a dotted path such as ``pages.home.keys.3.press``.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

MAX_KEYS = 36
MAX_DIALS = 6
MAX_T3_SLOT = 5

Loc = tuple[str | int, ...]


class _Model(BaseModel):
    """Base for all config models: unknown keys are errors, so typos surface."""

    model_config = ConfigDict(extra="forbid")


# --- action arguments -------------------------------------------------------


def _http_url(value: str) -> str:
    # urlsplit silently strips/ignores some whitespace and control characters, so it
    # would validate a different string than the one we return. Reject them up front.
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("must not contain whitespace or control characters")
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError("must be an http or https URL")
    return value


def _not_dot_name(value: str) -> str:
    if value in {".", ".."}:
        raise ValueError("must be a file name, not a directory reference")
    return value


HttpUrl = Annotated[str, AfterValidator(_http_url)]
T3Slot = Annotated[int, Field(ge=0, le=MAX_T3_SLOT)]
ScriptName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9._-]+$"),
    AfterValidator(_not_dot_name),
]


class DdcArgs(_Model):
    code: int
    value: int


class HaArgs(_Model):
    service: str
    entity_id: str
    data: dict[str, Any] = {}


class TabArgs(_Model):
    match: str
    url: HttpUrl
    navigate: bool = False


class OutputArgs(_Model):
    name: str
    disconnect: bool = False


class VolumeArgs(_Model):
    mute: Literal["toggle"]


ACTION_ARGS: dict[str, Any] = {
    "page": str,
    "shell": list[str],
    "ddc": DdcArgs,
    "ha": HaArgs,
    "t3.focus": T3Slot,
    "t3.new": None,
    "mac.app": str,
    "mac.url": HttpUrl,
    "mac.tab": TabArgs,
    "mac.audio.output": OutputArgs,
    "mac.audio.input": str,
    "mac.mic.mute": None,
    "mac.volume": VolumeArgs,
    "mac.screenshot": None,
    "mac.km": str,
    "mac.script": ScriptName,
    "mac.lock": None,
}

_ARG_VALIDATORS = {name: TypeAdapter(tp) for name, tp in ACTION_ARGS.items()}


class Action(_Model):
    """One action. YAML writes it as a single-key mapping: ``{mac.app: Ghostty}``."""

    type: str
    args: Any = None

    @model_validator(mode="before")
    @classmethod
    def _from_yaml(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "type" in data and set(data) <= {"type", "args"}:
            kind, args = data["type"], data.get("args")
        elif len(data) == 1:
            ((kind, args),) = data.items()
        else:
            raise ValueError("expected a single-key mapping such as {mac.app: Ghostty}")
        if not isinstance(kind, str) or kind not in ACTION_ARGS:
            raise ValueError(f"unknown action type {kind!r}; known: {', '.join(ACTION_ARGS)}")
        return {"type": kind, "args": _ARG_VALIDATORS[kind].validate_python(args)}


# --- pages ------------------------------------------------------------------


class HaStateRef(_Model):
    ha: str


class KeySpec(_Model):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    icon: str | None = None
    label: str | None = None
    state: HaStateRef | None = None
    press: Action | None = None
    long_press: Action | None = None
    t3_slot: T3Slot | None = Field(default=None, alias="t3.slot")

    @model_validator(mode="after")
    def _slot_implies_focus(self) -> Self:
        if self.t3_slot is not None and self.press is None:
            self.press = Action(type="t3.focus", args=self.t3_slot)
        return self


class DialSpec(_Model):
    kind: Literal["volume", "input", "brightness", "t3.effort", "t3.model", "ha.brightness"]
    entity_id: str | None = None
    presets: list[int] = [35, 80]


class Page(_Model):
    keys: list[KeySpec | None] = Field(default_factory=list, max_length=MAX_KEYS)
    dials: list[DialSpec | None] = Field(default_factory=list, max_length=MAX_DIALS)


# --- sections ---------------------------------------------------------------


class MacConfig(_Model):
    host: str
    key: Path
    command: list[str] | None = None


class HAConfig(_Model):
    url: str
    token_env: str


class T3Server(_Model):
    name: str
    url: str
    web_url: str
    environment_id: str
    token_env: str


class T3Config(_Model):
    servers: list[T3Server]
    models: dict[str, list[str]]
    efforts: list[str] = ["low", "medium", "high", "xhigh", "max"]


class DDCConfig(_Model):
    model: str


class AudioOutput(_Model):
    name: str
    match: str
    bt: str | None = None


class AudioConfig(_Model):
    outputs: list[AudioOutput]
    inputs: list[str]
    pin_input: bool = False


# --- root -------------------------------------------------------------------


class Config(_Model):
    mac: MacConfig | None = None
    home_assistant: HAConfig | None = None
    t3: T3Config | None = None
    ddc: DDCConfig | None = None
    audio: AudioConfig | None = None
    pages: dict[str, Page]
    start_page: str = "home"

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        """Check cross-section references, reporting every problem with its path."""
        problems: list[InitErrorDetails] = []

        def report(loc: Loc, message: str) -> None:
            problems.append(
                InitErrorDetails(
                    type=PydanticCustomError("config", "{message}", {"message": message}),
                    loc=loc,
                    input=None,
                )
            )

        def need(section: object, name: str, loc: Loc, what: str) -> None:
            if section is None:
                report(loc, f"{what} requires a '{name}' section")

        if self.start_page not in self.pages:
            report(("start_page",), f"unknown page {self.start_page!r}")

        for page_name, page in self.pages.items():
            for i, key in enumerate(page.keys):
                if key is None:
                    continue
                key_loc = ("pages", page_name, "keys", i)
                if key.state is not None:
                    need(self.home_assistant, "home_assistant", (*key_loc, "state"), "ha state")
                for field in ("press", "long_press"):
                    action: Action | None = getattr(key, field)
                    if action is not None:
                        self._check_action(action, (*key_loc, field), report, need)

            for i, dial in enumerate(page.dials):
                if dial is None:
                    continue
                dial_loc = ("pages", page_name, "dials", i)
                what = f"{dial.kind} dial"
                if dial.kind == "brightness":
                    need(self.ddc, "ddc", (*dial_loc, "kind"), what)
                elif dial.kind in {"volume", "input"}:
                    need(self.mac, "mac", (*dial_loc, "kind"), what)
                    if dial.kind == "input":
                        need(self.audio, "audio", (*dial_loc, "kind"), what)
                elif dial.kind in {"t3.effort", "t3.model"}:
                    need(self.t3, "t3", (*dial_loc, "kind"), what)
                elif dial.kind == "ha.brightness":
                    need(self.home_assistant, "home_assistant", (*dial_loc, "kind"), what)
                    if dial.entity_id is None:
                        report((*dial_loc, "entity_id"), f"{what} requires entity_id")

        if problems:
            raise ValidationError.from_exception_data(type(self).__name__, problems)
        return self

    def _check_action(
        self,
        action: Action,
        loc: Loc,
        report: Callable[[Loc, str], None],
        need: Callable[[object, str, Loc, str], None],
    ) -> None:
        kind = action.type
        if kind.startswith("mac."):
            need(self.mac, "mac", loc, kind)
        elif kind == "ha":
            need(self.home_assistant, "home_assistant", loc, kind)
        elif kind == "ddc":
            need(self.ddc, "ddc", loc, kind)
        elif kind.startswith("t3."):
            need(self.t3, "t3", loc, kind)
        elif kind == "page" and action.args not in self.pages:
            report(loc, f"unknown page {action.args!r}")
        if kind == "mac.audio.output":
            defined = [o.name for o in self.audio.outputs] if self.audio else []
            if action.args.name not in defined:
                report(loc, f"unknown audio output {action.args.name!r}; defined: {defined}")


# --- loading ----------------------------------------------------------------


class ConfigError(Exception):
    """Invalid config. Each entry of `errors` reads ``"<dotted.path>: <message>"``."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


def _format_errors(exc: ValidationError) -> list[str]:
    errors = []
    for err in exc.errors(include_url=False):
        path = ".".join(str(part) for part in err["loc"]) or "(root)"
        message = err["msg"].removeprefix("Value error, ")
        errors.append(f"{path}: {message}")
    return errors


def load_config(path: Path) -> Config:
    """Load and validate a YAML config file; raise ConfigError on any problem."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ConfigError([f"file: {exc}"]) from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError([f"yaml: {problem}{where}"]) from exc
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from exc
