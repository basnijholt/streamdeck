"""Command line entry point: `deck <subcommand>`."""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Coroutine
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deck.actions import ActionRunner
from deck.config import Action, Config, ConfigError, HAConfig, load_config
from deck.control import call, default_control_path
from deck.daemon import Daemon
from deck.ddc import Ddc
from deck.device import StreamDeckDevice
from deck.dials import Services
from deck.ha import HomeAssistant, entities_in
from deck.local import register_local_actions
from deck.mac import dials as _mac_dials  # noqa: F401  (registers the volume and input dials)
from deck.mac.client import MacClient, handle_mac_action
from deck.remote.mac import MacExecutor, MacPoller
from deck.remote.server import serve_stdio
from deck.render import compose_deck, draw_key, draw_segment
from deck.runner import Runner
from deck.state import StateStore
from deck.t3.model import Thread, visible_threads
from deck.views import key_view, segment_view

log = logging.getLogger(__name__)

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


def _start_background(stack: AsyncExitStack, coro: Coroutine[Any, Any, None]) -> None:
    """Run `coro` as a task until `stack` closes, which cancels it."""
    task = asyncio.create_task(coro)

    async def stop() -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    stack.push_async_callback(stop)


def _start_home_assistant(
    cfg: HAConfig, services: Services, store: StateStore, stack: AsyncExitStack
) -> None:
    """Start the Home Assistant client and register its actions.

    Without a usable token or URL the daemon keeps running: Home Assistant stays offline, its
    keys are dimmed and its actions fail with a message.
    """
    store.set("ha.online", False)
    token = os.environ.get(cfg.token_env, "").strip()
    if not token:
        log.error("Home Assistant is disabled: environment variable %s is not set", cfg.token_env)
    else:
        try:
            ha = HomeAssistant(cfg, token, store, lambda: entities_in(services.config))
        except ValueError as exc:
            log.error("Home Assistant is disabled: %s", exc)
        else:
            services.ha = ha
            _start_background(stack, ha.run())

    async def ha_action(action: Action) -> None:
        if services.ha is None:
            raise RuntimeError("Home Assistant is disabled (see the error logged at startup)")
        args = action.args
        await services.ha.call_service(args.service, args.entity_id, args.data)

    services.actions.register("ha", ha_action)


async def _services(config: Config, store: StateStore, stack: AsyncExitStack) -> Services:
    """Build the services. Later tasks start their clients here and register cleanup on `stack`."""
    services = Services(store, config, actions=ActionRunner())
    runner = Runner()
    register_local_actions(services, runner)
    if config.ddc is not None:
        services.ddc = Ddc(runner, config.ddc.model, store)
        _start_background(stack, services.ddc.poll_forever())
    if config.mac is not None:
        mac = services.mac = MacClient(config.mac, lambda: services.config.audio, store)
        _start_background(stack, mac.run())

        async def mac_action(action: Action) -> None:
            await handle_mac_action(mac, services.config.audio, action)

        services.actions.register("mac.", mac_action)
    if config.home_assistant is not None:
        _start_home_assistant(config.home_assistant, services, store, stack)
    return services


def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        daemon = Daemon(args.config, StreamDeckDevice(), _services)
    except ConfigError as exc:
        print("\n".join(exc.errors))
        return 1
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    return 0


def _remote(args: argparse.Namespace) -> int:
    """Serve the PC's JSON Lines protocol on stdin/stdout (run by its SSH forced command)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout carries the protocol and nothing else
    )
    executor = MacExecutor(Runner())

    async def serve() -> None:
        try:
            await serve_stdio(executor, MacPoller(Runner(), executor))
        finally:
            await executor.aclose()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return 0


def _control(message: dict[str, Any], pretty: bool = False) -> int:
    """Send `message` to the running daemon, print its response; 0 if it said `ok`."""
    path = default_control_path()
    try:
        response = asyncio.run(call(path, message))
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"deck is not running (nothing is listening on {path})", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"deck: control socket {path}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(response, indent=2 if pretty else None, default=str))
    return 0 if response.get("ok") is True else 1


def _reload(args: argparse.Namespace) -> int:
    return _control({"cmd": "reload"})


def _state(args: argparse.Namespace) -> int:
    return _control({"cmd": "state"}, pretty=True)


def _press(args: argparse.Namespace) -> int:
    return _control({"cmd": "press", "page": args.page, "key": args.key, "long": args.long})


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

    run = sub.add_parser("run", help="run the daemon")
    run.add_argument("config", type=Path)
    run.set_defaults(func=_run)

    remote = sub.add_parser("remote", help="serve Mac actions and state over stdin/stdout")
    remote.set_defaults(func=_remote)

    reload = sub.add_parser("reload", help="make the running daemon reload its config")
    reload.set_defaults(func=_reload)

    state = sub.add_parser("state", help="print the running daemon's state as JSON")
    state.set_defaults(func=_state)

    press = sub.add_parser("press", help="press a key of the running daemon")
    press.add_argument("page", help="page name")
    press.add_argument("key", type=int, help="key index on that page")
    press.add_argument("--long", action="store_true", help="long press")
    press.set_defaults(func=_press)

    args = parser.parse_args(argv)
    return args.func(args)
