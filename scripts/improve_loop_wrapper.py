#!/usr/bin/env python3
"""
Cron wrapper — autonomous improve loop (bounded to config + Seb's own skills).

Thin no-agent cron entry (mirrors the existing ~/.hermes/scripts convention).
It locates the repo's improve_cron_entry.py under the live hermes-agent
checkout and runs it with the venv python. Idempotent by design:
  - pre-creates + validates ~/.hermes/labs/bestplan-research/state (B4)
  - takes an flock on state/loop.lock; if another run holds it, exits 0 with
    "skipped: locked" (B5) — never corrupts state via overlap
  - writes a per-run report and propagates the child result; unavailable
    pipeline wiring is therefore visible as a failed cron run

Core Hermes is NEVER mutated by this entry's contract (scope_guard enforced in
the invoked loop). The invoked script is the repo's
optional-skills/research/darwinian-evolver/labs/scripts/improve_cron_entry.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/home/seb/projects/hermes-agent")
VENV_PY = REPO / ".venv" / "bin" / "python"
ENTRY = (
    REPO
    / "optional-skills"
    / "research"
    / "darwinian-evolver"
    / "labs"
    / "scripts"
    / "improve_cron_entry.py"
)

STATE_DIR = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "labs" / "bestplan-research" / "state"
TOOL_PATHS = (
    REPO / ".venv" / "bin",
    Path.home() / ".local" / "bin",
    Path.home() / "tools" / "node-v22.23.2-linux-x64" / "bin",
)
IMPROVE_TIMEOUT_SECONDS = 3600


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    current = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    preferred = [str(path) for path in TOOL_PATHS if path.is_dir()]
    env["PATH"] = os.pathsep.join(dict.fromkeys(preferred + current))
    return env


def main(argv=None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]

    # B4: pre-create + validate the state dir (bootstrap is idempotent)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: cannot create state dir {STATE_DIR}: {exc}", file=sys.stderr)
        return 2

    # B15: keep the descriptor open for the entire child run. Closing it before
    # launching the child would release the advisory lock and allow overlap.
    lock_path = STATE_DIR / "loop.lock"
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        print(f"ERROR: cannot open loop.lock: {exc}", file=sys.stderr)
        return 2
    try:
        try:
            import fcntl
        except ImportError:
            fcntl = None
        if fcntl:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("skipped: locked (concurrent improve-loop already running)")
                return 0
        else:
            print("notice: fcntl unavailable on this platform; running without lock", file=sys.stderr)

        if not ENTRY.is_file():
            print(f"ERROR: improve_cron_entry.py not found at {ENTRY}", file=sys.stderr)
            return 2
        if not VENV_PY.is_file():
            print(f"ERROR: venv python not found at {VENV_PY}", file=sys.stderr)
            return 2

        cmd = [str(VENV_PY), str(ENTRY), "--state-dir", str(STATE_DIR)]
        cmd.extend(str(a) for a in argv)
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(),
                timeout=IMPROVE_TIMEOUT_SECONDS,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"ERROR: improve loop failed to run: {exc}", file=sys.stderr)
            return 1

        report = {
            "run_ts": ts,
            "entry_stdout": proc.stdout[-4000:],
            "entry_stderr": proc.stderr[-2000:],
            "exit_code": proc.returncode,
        }
        report_path = STATE_DIR / f"cron-report-{ts}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(proc.stdout)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        return proc.returncode
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())