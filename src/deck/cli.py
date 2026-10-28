"""Command line entry point: `deck <subcommand>`."""

import argparse
import json
from pathlib import Path

from deck.config import Config, ConfigError, load_config


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deck", description="Stream Deck + XL daemon")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate a config file")
    check.add_argument("path", type=Path)
    check.set_defaults(func=_check)

    schema = sub.add_parser("schema", help="print the config JSON schema")
    schema.set_defaults(func=_schema)

    args = parser.parse_args(argv)
    return args.func(args)
