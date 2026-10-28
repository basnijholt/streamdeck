"""Command line entry point: `deck <subcommand>`."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deck.config import Config, ConfigError, load_config
from deck.render import compose_deck, draw_key, draw_segment
from deck.state import StateStore
from deck.t3.model import Thread, visible_threads
from deck.views import key_view, segment_view

_THREAD_DEFAULTS: dict[str, Any] = {
    "server": "pc",
    "thread_id": "",
    "project_id": "",
    "title": "",
    "status": "idle",
    "pending_requests": 0,
    "model": "",
    "instance_id": "",
    "options": [],
    "settled": False,
    "archived": False,
    "parent_thread_id": None,
    "updated_at": "1970-01-01T00:00:00+00:00",
}


def _check(args: argparse.Namespace) -> int:
    try:
        load_config(args.path)
    except ConfigError as exc:
        print("\n".join(exc.errors))
        return 1
    print("ok")
    return 0


def _schema(args: argparse.Namespace) -> int:
    print(json.dumps(Config.model_json_schema(), indent=2))
    return 0


def _thread_from_json(raw: dict[str, Any]) -> Thread:
    """Build a Thread from a state-file entry; omitted fields take preview defaults."""
    fields = {**_THREAD_DEFAULTS, **raw}
    fields["options"] = tuple((name, value) for name, value in fields["options"])
    updated_at = datetime.fromisoformat(fields["updated_at"])
    fields["updated_at"] = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
    return Thread(**fields)


def _load_state(path: Path) -> StateStore:
    """Read a JSON object of state keys into a store (`t3.threads` holds thread objects)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object of state keys")
    try:
        data["t3.threads"] = [_thread_from_json(t) for t in data.get("t3.threads", [])]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"t3.threads: {exc}") from exc
    store = StateStore()
    store.update("", data)
    return store


def _render(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("\n".join(exc.errors))
        return 1
    page_name = args.page or config.start_page
    page = config.pages.get(page_name)
    if page is None:
        print(f"unknown page {page_name!r}; known: {', '.join(config.pages)}")
        return 1
    store = StateStore()
    if args.state is not None:
        try:
            store = _load_state(args.state)
        except (OSError, ValueError) as exc:
            print(f"state: {exc}")
            return 1

    slot_threads = visible_threads(store.get("t3.threads", []))
    keys = [draw_key(key_view(spec, store, config, slot_threads)) for spec in page.keys]
    segments = [draw_segment(segment_view(spec, store, config)) for spec in page.dials]
    compose_deck(keys, segments).save(args.png)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deck", description="Stream Deck + XL daemon")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate a config file")
    check.add_argument("path", type=Path)
    check.set_defaults(func=_check)

    schema = sub.add_parser("schema", help="print the config JSON schema")
    schema.set_defaults(func=_schema)

    render = sub.add_parser("render", help="render a page to a PNG preview")
    render.add_argument("config", type=Path)
    render.add_argument("--png", type=Path, required=True, help="output PNG path")
    render.add_argument("--page", help="page name (default: the config's start_page)")
    render.add_argument("--state", type=Path, help="JSON state file")
    render.set_defaults(func=_render)

    args = parser.parse_args(argv)
    return args.func(args)
