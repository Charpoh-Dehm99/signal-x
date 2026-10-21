#!/usr/bin/env python3
"""Daemon watchdog: keep the observation daemons alive (restart if dead).

G0 killed the tested signal space — this supervises observation-only
instrumentation, never anything execution related.

Checks each daemon every --interval seconds by scanning /proc cmdlines
(no pgrep dependency) and restarts missing ones with the standard
setsid/detached launch pattern, appending to logs/<name>.log.

Usage (from repo root):
  .venv/bin/python scripts/watchdog.py            # supervise every 60s
  .venv/bin/python scripts/watchdog.py --once     # single pass, exit
"""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404 - internal daemon supervisor; fixed argv lists, no shell
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MIN_INTERVAL_S = 15.0
RESTART_GRACE_S = 30.0

# name -> argv (python interpreter + flags are prepended at launch)
DAEMONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sentinel", (
        "scripts/sentinel.py", "--top", "40", "--interval", "60", "--alert",
        "--flow", "--flow-top", "10",
    )),
    ("recorder", ("scripts/recorder.py", "--top", "40", "--tf", "1m", "--interval", "60")),
    ("stream", ("scripts/stream.py", "--top", "10", "--min-notional", "25000")),
    ("book", ("scripts/book.py", "--top", "40")),
    ("cvd", ("scripts/cvd.py", "--top", "10")),
)


def _ts() -> str:
    return time.strftime("%H:%M:%S", time.gmtime())


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--interval", type=float, default=60.0,
                    help=f"seconds between checks (>= {MIN_INTERVAL_S:.0f})")
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    return ap.parse_args(argv)


def find_running(name: str) -> int | None:
    """PID of the first process whose cmdline runs scripts/<name>.py, else None."""
    needle = f"scripts/{name}.py"
    me = os.getpid()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if any(a.endswith(needle) for a in argv):
            return pid
    return None


def start(name: str, argv: tuple[str, ...]) -> int:
    """Launch one daemon detached; returns its PID."""
    log_path = ROOT / "logs" / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(  # nosec B603 - fixed internal argv, shell=False
            [sys.executable, "-u", *argv],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return proc.pid


def supervise_once(
    last_start: dict[str, float], skipped: set[str] | None = None
) -> list[str]:
    """Check all daemons; restart the dead ones. Returns restart log lines.

    Specs whose script file does not exist yet are skipped once (noted in
    ``skipped``) and picked up automatically as soon as the file appears.
    """
    events: list[str] = []
    now = time.monotonic()
    for name, argv in DAEMONS:
        script = ROOT / "scripts" / f"{name}.py"
        if not script.is_file():
            if skipped is not None and name not in skipped:
                skipped.add(name)
                events.append(f"[{_ts()}] SKIP {name} — {script} not found yet")
            continue
        pid = find_running(name)
        if pid is not None:
            continue
        if now - last_start.get(name, 0.0) < RESTART_GRACE_S:
            continue  # recently started, still coming up
        new_pid = start(name, argv)
        last_start[name] = now
        events.append(f"[{_ts()}] RESTART {name} (pid {new_pid}) — was down")
    return events


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.interval < MIN_INTERVAL_S:
        print(f"ERROR: --interval must be >= {MIN_INTERVAL_S:.0f}s")
        return 2
    print(f"watchdog: supervising {len(DAEMONS)} daemons every {args.interval:.0f}s")
    last_start: dict[str, float] = {}
    skipped: set[str] = set()
    try:
        while True:
            for line in supervise_once(last_start, skipped):
                print(line, flush=True)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
