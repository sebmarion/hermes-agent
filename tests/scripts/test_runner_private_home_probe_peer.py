"""Peer probe for concurrent per-file canonical-runner isolation."""

from __future__ import annotations

import os
from pathlib import Path
import time

import pytest


def test_peer_cannot_observe_or_overwrite_other_file_home():
    if os.environ.get("HERMES_TEST_ISOLATION_SELFTEST") != "1":
        pytest.skip("only exercised by the nested canonical-runner self-test")

    control = Path(os.environ["HERMES_TEST_ISOLATION_CONTROL_DIR"])
    home = Path.home()
    marker = home / ".hermes" / "parallel-marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("peer", encoding="utf-8")
    (control / "peer.ready").write_text(str(home), encoding="utf-8")

    deadline = time.monotonic() + 10
    while len(list(control.glob("*.ready"))) < 2:
        if time.monotonic() >= deadline:
            pytest.fail("parallel isolation peer never reached barrier")
        time.sleep(0.01)

    assert marker.read_text(encoding="utf-8") == "peer"
