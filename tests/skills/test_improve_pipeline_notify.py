"""Tests for notify_telegram.py — redacting + shell-safe message construction.

No network. We only test the pure helpers that build the message args and
redact credential-shaped values before the message could be transmitted.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "optional-skills" / "research" / "darwinian-evolver" / "labs" / "scripts"),
)

import notify_telegram as nt  # noqa: E402


# ---------------------------------------------------------------------------
# secret redaction
# ---------------------------------------------------------------------------

def test_secret_redacted_from_message() -> None:
    raw = "applied fix; key used in run: sk-abcdef1234567890abcdef1234567890 ends"
    cleaned = nt.redact(raw)
    assert "sk-abcdef1234567890abcdef1234567890" not in cleaned
    assert "[redacted" in cleaned


def test_apikey_shape_redacted() -> None:
    raw = "token apikey=abcdef0123456789XYZ was in the log"
    cleaned = nt.redact(raw)
    assert "abcdef0123456789XYZ" not in cleaned


def test_clean_message_unchanged() -> None:
    msg = "upgrade applied: v0.20.0 -> v2026.8.13, 3 commits absorbed"
    assert nt.redact(msg) == msg


# ---------------------------------------------------------------------------
# command construction (no shell injection, args passed as a list)
# ---------------------------------------------------------------------------

def test_command_is_arglist_no_shell() -> None:
    args = nt.build_send_args("upgrade", "Telegram message content")
    assert isinstance(args, list)
    assert all(isinstance(a, str) for a in args)
    # channel/platform comes from config; a message with shell metachars must
    # survive as ONE arg (never re-split)
    evil = "x; rm -rf /"
    args2 = nt.build_send_args("halted", evil)
    assert args2[-1] == "[halted] x; rm -rf /", "message must stay a single argv element"


def test_event_label_in_args() -> None:
    args = nt.build_send_args("applied", "something")
    joined = " ".join(args)
    assert "applied" in joined or any("applied" in a for a in args)


def test_unknown_event_rejected() -> None:
    # fail-closed: unknown event type raises, we don't silently send
    import pytest

    with pytest.raises(ValueError):
        nt.build_send_args("mystery-event", "msg")