"""A stand-in for a Mac running `deck connect`, to try the PC side without a Mac.

Cross-wire it with `deck attach NAME` on the PC (each one's stdout into the other's stdin, for
example with a bash `coproc`) and the daemon sees a Mac that speaks the real protocol.

Run it by path: `python stub_remote.py [STATE_FILE] [--stubborn]`. After the PC's `hello` it
sends the snapshot read from STATE_FILE (a JSON object; read at every start, so a test can
change what the next connection sees). It then answers requests, and what a request does is
decided by its kind, so whoever drives the PC side scripts the stub with requests of its own:

    log      reply with every message received so far (the hello and all requests but `log`)
    pid      reply with this process's id
    push     send `{"state": args}` now, then reply
    hold     hold the replies to the kinds in `args` until `release` (the kinds' requests are
             still recorded at once)
    release  answer the held requests, then reply
    reject   answer the kinds in `args` with the error "boom" from now on, then reply
    noise    write `args` lines to stderr, then reply
    big      reply with a string of `args` characters
    fail     reply with the error "boom"
    hang     never reply
    die      exit at once, without a reply (the connection drops)
    other    reply ok, with `args` as the result

`--stubborn` ignores SIGTERM and keeps running after stdin closes, to test the final kill.
"""

import json
import os
import signal
import sys
import time


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(request_id: int, result) -> None:
    send({"id": request_id, "ok": True, "result": result})


def main(argv: list[str]) -> None:
    stubborn = "--stubborn" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if stubborn:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    received: list[dict] = []
    held_kinds: set[str] = set()
    held: list[tuple[int, object]] = []
    rejected_kinds: set[str] = set()

    while line := sys.stdin.readline():
        message = json.loads(line)
        if "hello" in message:
            received.append(message)
            snapshot = {}
            if paths:
                with open(paths[0]) as handle:
                    snapshot = json.load(handle)
            send({"state": snapshot})
            continue

        request_id, kind, args = message["id"], message["kind"], message["args"]
        if kind == "log":
            reply(request_id, received)
            continue
        received.append(message)
        if kind == "pid":
            reply(request_id, os.getpid())
        elif kind == "push":
            send({"state": args})
            reply(request_id, None)
        elif kind == "hold":
            held_kinds = set(args)
            reply(request_id, None)
        elif kind == "release":
            for held_id, held_args in held:
                reply(held_id, held_args)
            held.clear()
            held_kinds.clear()
            reply(request_id, None)
        elif kind == "reject":
            rejected_kinds = set(args)
            reply(request_id, None)
        elif kind == "noise":
            for i in range(args):
                print(f"stub noise {i}", file=sys.stderr)
            sys.stderr.flush()
            reply(request_id, None)
        elif kind == "big":
            reply(request_id, "x" * args)
        elif kind == "fail":
            send({"id": request_id, "ok": False, "error": "boom"})
        elif kind == "hang":
            pass
        elif kind == "die":
            os._exit(0)
        elif kind in rejected_kinds:
            send({"id": request_id, "ok": False, "error": "boom"})
        elif kind in held_kinds:
            held.append((request_id, args))
        else:
            reply(request_id, args)

    while stubborn:
        time.sleep(1)


if __name__ == "__main__":
    main(sys.argv[1:])
