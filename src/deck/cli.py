"""Command line entry point: `deck <subcommand>`."""

import argparse
import json
from pathlib import Path

from deck.config import Config, ConfigError, load_config
from deck.render import KeyView, SegmentView, compose_deck, draw_key, draw_segment


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
    if args.state is not None:
        try:
            json.loads(args.state.read_text(encoding="utf-8"))  # consumed once views use state
        except (OSError, ValueError) as exc:
            print(f"state: {exc}")
            return 1

    keys = [
        None if spec is None else draw_key(KeyView(spec.icon, spec.label, None))
        for spec in page.keys
    ]
    segments = [
        None if spec is None else draw_segment(SegmentView(title=spec.kind, value=""))
        for spec in page.dials
    ]
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
