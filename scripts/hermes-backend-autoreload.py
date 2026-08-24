#!/usr/bin/env python3
"""Restart hermes-backend only when the core checkout advanced.

Runs as an ExecStartPost of hermes-upstream-merge.service (root), after the
deployment coordinator has activated the gateway. Idempotent: records the
promoted commit SHA and restarts the backend only when HEAD moved. Git inspects
the user-owned checkout as seb so systemd's root context cannot trigger Git's
safe-directory rejection.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = "/home/seb/projects/hermes-agent"
REPO_USER = "seb"
STATE = pathlib.Path("/var/lib/hermes-deployment/backend-promoted.sha")
SERVICE = "hermes-backend.service"


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv

    proc = subprocess.run(
        [
            "/usr/sbin/runuser",
            "-u",
            REPO_USER,
            "--",
            "/usr/bin/git",
            "-C",
            REPO,
            "rev-parse",
            "HEAD",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        print(f"cannot read {REPO} HEAD: {proc.stderr.strip()}", file=sys.stderr)
        return 1
    head = proc.stdout.strip()

    last = STATE.read_text().strip() if STATE.exists() else ""
    if head == last:
        print(f"hermes-backend already running {head[:12]}")
        return 0

    if dry_run:
        print(f"WOULD restart {SERVICE}: {last[:12] or '(none)'} -> {head[:12]}")
        return 0

    restart = subprocess.run(["systemctl", "restart", SERVICE])
    if restart.returncode:
        print(f"{SERVICE} restart exited {restart.returncode}", file=sys.stderr)
        return 1

    active = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE])
    if active.returncode:
        print(f"{SERVICE} not active after restart", file=sys.stderr)
        return 1

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(head + "\n")
    print(f"{SERVICE} restarted at {head[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
