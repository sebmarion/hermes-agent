#!/usr/bin/env python3
"""Telegram notifications for the improve loop (and any pipeline event).

Wraps the real Hermes transport (`hermes send`) which routes to a configured
platform (Telegram is active in config.yaml). This module is the SECRET GATE:
no message leaves this module with a credential-shaped value still in it. We
keep the pure helpers (redact, build_send_args) trivially testable; the actual
`send()` call is only made when `hermes` exists on PATH.

Event labels allowed: applied, halted, weekly, upgrade.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys

EVENTS = ("applied", "halted", "weekly", "upgrade")

# Same credential shapes as the rest of the pipeline.
CRED_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|apikey)\s*[:=]?\s*['\"]?[A-Za-z0-9_\-]{16,}"), "api key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS key id"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk secret"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "github token"),
    (re.compile(r"\bxo[a-z]+-[A-Za-z0-9\-]{10,}\b"), "slack token"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "JWT"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}"), "bearer token"),
]


def redact(text: str) -> str:
    """Replace credential-shaped substrings with [redacted:LABEL] markers."""
    out = text
    for pat, label in CRED_PATTERNS:
        out = pat.sub(f"[redacted:{label}]", out)
    return out


def build_send_args(event: str, message: str) -> list[str]:
    """Build the argv for `hermes send` (list — no shell interpolation).

    Raises ValueError on unknown event (fail-closed: we never silently send
    with a wrong label)."""
    if event not in EVENTS:
        raise ValueError(f"unknown notification event: {event!r} (allowed: {EVENTS})")
    cleaned = redact(message)
    # Prefix the event label so the receiver can triage at a glance, then
    # `hermes send <platform> <text>` — text stays a single argv element.
    return ["hermes", "send", "telegram", f"[{event}] {cleaned}"]


def send(event: str, message: str, dry_run: bool = False) -> dict:
    """Transmit (or simulate) a redacted notification.

    Never raises for a message problem; returns {"sent": bool, "reason": str}
    so callers can log a failed notify without crashing the pipeline."""
    if event not in EVENTS:
        return {"sent": False, "reason": f"unknown event {event!r}"}
    payload = redact(message)
    if dry_run:
        return {"sent": False, "reason": "dry-run (no transmit)"}
    if shutil.which("hermes") is None:
        return {"sent": False, "reason": "hermes CLI not on PATH"}
    args = build_send_args(event, payload)
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as exc:
        return {"sent": False, "reason": f"hermes send failed: {exc}"}
    if proc.returncode != 0:
        return {"sent": False, "reason": f"hermes send exit {proc.returncode}: {proc.stderr.strip()[:200]}"}
    return {"sent": True, "reason": "ok"}


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--event", required=True, choices=EVENTS, help="Notification event type")
    ap.add_argument("--message", required=True, help="Message body (will be redacted)")
    ap.add_argument("--dry-run", action="store_true", help="Redact + build argv, do not transmit")
    args = ap.parse_args(argv[1:] if argv and not argv[0].startswith("-") else argv)

    result = send(args.event, args.message, dry_run=args.dry_run)
    print(f"notify[{args.event}] sent={result['sent']} reason={result['reason']}")
    print(f"payload: {redact(args.message)}")
    return 0 if result["sent"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))