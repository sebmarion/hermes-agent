"""Probe executed by the canonical-runner isolation self-test."""

from __future__ import annotations

import os
from pathlib import Path
import time


def test_probe_can_only_write_inside_runner_owned_state():
    state_root = Path(os.environ["HERMES_TEST_FILE_STATE_ROOT"])
    home = Path.home()
    hermes_home = Path(os.environ["HERMES_HOME"])
    xdg_state = Path(os.environ["XDG_STATE_HOME"])

    assert state_root.name.startswith("ht.")
    resolved_root = state_root.resolve()
    assert home.resolve().is_relative_to(resolved_root)
    assert hermes_home.resolve().is_relative_to(resolved_root)
    assert xdg_state.resolve().is_relative_to(resolved_root)

    targets = (
        home / ".hermes" / "cron" / "jobs.json",
        home / ".hermes" / "auth.json",
        home / ".hermes" / "completion_outbox.jsonl",
        hermes_home / "cron" / "jobs.json",
        hermes_home / "auth.json",
        hermes_home / "completion_outbox.jsonl",
        xdg_state / "hermes" / "probe.state",
    )
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic-runner-probe", encoding="utf-8")

    if os.environ.get("HERMES_TEST_ISOLATION_SELFTEST") == "1":
        control = Path(os.environ["HERMES_TEST_ISOLATION_CONTROL_DIR"])
        marker = home / ".hermes" / "parallel-marker"
        marker.write_text("primary", encoding="utf-8")
        (control / "primary.ready").write_text(str(home), encoding="utf-8")

        deadline = time.monotonic() + 10
        while len(list(control.glob("*.ready"))) < 2:
            if time.monotonic() >= deadline:
                raise AssertionError("parallel isolation peer never reached barrier")
            time.sleep(0.01)

        assert marker.read_text(encoding="utf-8") == "primary"
