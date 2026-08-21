#!/usr/bin/env python3
"""Daily safe upstream-delta sync wrapper.

Runs the repository merge planner outside the agent's Python process:
- flock serializes it against another daily/manual update;
- the planner applies the recorded upstream delta to the current fork HEAD,
  preserves owned paths, runs the bounded relevant test gate, then publishes;
- Telegram receives a redacted success or halt rundown.

A dirty checkout (including another thread's in-flight work) is intentionally
a halt, never an implicit stash/reset.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/home/seb/projects/hermes-agent")
PYTHON = REPO / ".venv" / "bin" / "python"
MERGER = REPO / "optional-skills/research/darwinian-evolver/labs/scripts/merge_upstream.py"
NOTIFY = REPO / "optional-skills/research/darwinian-evolver/labs/scripts/notify_telegram.py"
STATE = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "labs/bestplan-research/state"
LOCK = STATE / "upstream-merge.lock"
RELEVANT_TEST_PATHS = (
    "tests/skills",
    "tests/gateway/test_scale_to_zero_watcher.py",
    "tests/plugins/test_teams_pipeline_plugin.py",
    "tests/tools/test_memory_tool.py",
)


def _notify(event: str, message: str) -> None:
    if not NOTIFY.is_file():
        return
    try:
        subprocess.run(
            [str(PYTHON), str(NOTIFY), "--event", event, "--message", message],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Notification is advisory; never turn a completed merge into a
        # wrapper failure because Telegram/Hermes is unavailable.
        return


def _build_command() -> list[str]:
    command = [
        str(PYTHON), str(MERGER),
        "--repo", str(REPO),
        "--state-dir", str(STATE),
        "--remote", "sebmarion-fork",
        "--apply", "--publish",
        "--test", str(PYTHON),
        "--test=-m",
        "--test", "pytest",
    ]
    for path in RELEVANT_TEST_PATHS:
        command.extend(["--test", path])
    command.append("--test=-q")
    return command


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("skipped: upstream merge already running")
            return 0

        command = _build_command()
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=3600)
        except (OSError, subprocess.SubprocessError) as exc:
            summary = f"Daily Hermes upstream sync failed to start: {type(exc).__name__}"
            _notify("halted", summary)
            print(summary, file=sys.stderr)
            return 1

        output = (proc.stdout + "\n" + proc.stderr).strip()
        report = STATE / "last-upstream-merge.txt"
        report.write_text(f"started={started}\nexit_code={proc.returncode}\n{output[-12000:]}\n")
        if proc.returncode == 0:
            _notify(
                "upgrade",
                "Daily Hermes upstream sync succeeded. "
                f"The tested upstream-based candidate was applied and published to sebmarion/main. "
                f"Run started {started}. Result: {proc.stdout[-2000:]}",
            )
        else:
            _notify(
                "halted",
                "Daily Hermes upstream sync halted safely; live checkout was not applied. "
                f"Run started {started}. Reason: {output[-2500:]}",
            )
        print(output)
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
