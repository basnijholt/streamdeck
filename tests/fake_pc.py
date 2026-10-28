"""A stand-in for the PC end of a `deck connect` session, run as the child instead of ssh.

Run it by path: `python fake_pc.py LOG_FILE [--lines N] [--stubborn]`. It sends the PC's `hello`,
then appends every line it reads from stdin (what the Mac sends) to LOG_FILE. It ends when stdin
closes, or after N lines when `--lines` is given (the connection drops).

`--stubborn` ignores SIGTERM and keeps running after stdin closes, to test the final kill.
"""

import signal
import sys
import time


def main(argv: list[str]) -> None:
    stubborn = "--stubborn" in argv
    limit = int(argv[argv.index("--lines") + 1]) if "--lines" in argv else None
    log_file = argv[0]
    if stubborn:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    sys.stdout.write('{"hello": {}}\n')
    sys.stdout.flush()

    count = 0
    while limit is None or count < limit:
        line = sys.stdin.readline()
        if not line:
            break
        with open(log_file, "a") as handle:
            handle.write(line)
        count += 1

    while stubborn:
        time.sleep(1)


if __name__ == "__main__":
    main(sys.argv[1:])
