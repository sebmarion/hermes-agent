"""The canonical runner must never expose its caller's HOME to tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_runner_preserves_caller_cron_outbox_and_auth(tmp_path):
    caller_home = tmp_path / "caller-home"
    sentinels = (
        caller_home / ".hermes" / "cron" / "jobs.json",
        caller_home / ".hermes" / "auth.json",
        caller_home / ".hermes" / "completion_outbox.jsonl",
    )
    for index, path in enumerate(sentinels):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"caller-sentinel-{index}", encoding="utf-8")
        path.chmod(0o600)
    before = {path: _sha256(path) for path in sentinels}

    runner_tmp = tmp_path / "runner-tmp"
    runner_tmp.mkdir()
    repo_root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "HOME": str(caller_home),
        "HERMES_HOME": str(caller_home / ".hermes"),
        "TMPDIR": str(runner_tmp),
        "HERMES_TEST_ISOLATION_SELFTEST": "1",
        # Let the nested canonical runner reuse this test process's already
        # isolated venv without creating a link back into caller HOME.
        "VIRTUAL_ENV": sys.prefix,
    }
    result = subprocess.run(
        [
            str(repo_root / "scripts" / "run_tests.sh"),
            "-j",
            "2",
            "tests/scripts/test_runner_private_home_probe.py",
            "tests/scripts/test_runner_private_home_probe_peer.py",
            "-q",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert {path: _sha256(path) for path in sentinels} == before
    assert not list(runner_tmp.glob("hermes-agent-tests.*"))
