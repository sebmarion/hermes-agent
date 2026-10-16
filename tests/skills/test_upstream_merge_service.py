"""Contracts for the autoresearch systemd service template."""

from pathlib import Path


SERVICE = Path(__file__).resolve().parents[2] / "scripts/hermes-upstream-merge.service"


def test_upstream_merge_service_reloads_backend_exactly_once() -> None:
    text = SERVICE.read_text()

    assert text.count(
        "ExecStartPost=/usr/local/libexec/hermes-backend-autoreload.py --defer-if-connected"
    ) == 1
