"""Credential-free routing audit and explanation commands."""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any


def _provider_compatible(observed: str, expected: str) -> bool:
    observed = str(observed or "").strip().lower()
    expected = str(expected or "").strip().lower()
    if not observed or not expected:
        return False
    if observed == expected:
        return True
    return observed == "custom" and expected.startswith("custom:")


def classify_session_route(session: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Explain a persisted route without pretending an unrecorded reason is known."""
    observed_provider = str(session.get("billing_provider") or "").strip()
    observed_model = str(session.get("model") or "").strip()
    source = str(session.get("source") or "").strip().lower()
    is_child = bool(session.get("parent_session_id")) or source in {
        "subagent",
        "compression",
        "tool",
    } or source.startswith("cron")

    if not observed_provider or not observed_model:
        return {"reason_code": "unknown_runtime", "expected_route": None}

    if is_child:
        delegation = report.get("delegation") or {}
        expected = [delegation.get("fallback") or {}]
        expected.extend((delegation.get("lanes") or {}).values())
        for route in expected:
            if (
                observed_model == str(route.get("model") or "").strip()
                and _provider_compatible(observed_provider, route.get("provider") or "")
            ):
                return {
                    "reason_code": "matches_delegation",
                    "expected_route": {
                        "provider": route.get("provider"),
                        "model": route.get("model"),
                    },
                }
    else:
        main = report.get("main") or {}
        if (
            observed_model == str(main.get("model") or "").strip()
            and _provider_compatible(observed_provider, main.get("provider") or "")
        ):
            return {
                "reason_code": "matches_main",
                "expected_route": {
                    "provider": main.get("provider"),
                    "model": main.get("model"),
                },
            }

    return {
        "reason_code": "unexplained",
        "expected_route": None,
        "note": "Persisted runtime differs from configured routes; explicit overrides and historical fallback reasons are not yet persisted.",
    }


def explain_session(session_id: str) -> dict[str, Any] | None:
    from hermes_cli.status import build_routing_report

    report = build_routing_report(session_id=session_id, session_limit=1)
    sessions = report.get("sessions") or []
    if not sessions:
        return None
    session = sessions[0]
    classification = classify_session_route(session, report)
    return {
        "session_id": session.get("id"),
        "source": session.get("source"),
        "provider": session.get("billing_provider"),
        "model": session.get("model"),
        "started_at": session.get("started_at"),
        "last_active": session.get("last_active"),
        **classification,
    }


def build_routing_audit(*, days: float = 14, limit: int = 2000) -> dict[str, Any]:
    from hermes_cli.status import build_routing_report
    from hermes_state import SessionDB

    report = build_routing_report(session_limit=1)
    cutoff = time.time() - max(0.01, float(days)) * 86400
    db = SessionDB()
    try:
        rows = db.search_sessions(limit=max(1, min(int(limit), 10000)))
    finally:
        db.close()

    audited: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for session in rows:
        last_active = float(session.get("last_active") or session.get("started_at") or 0)
        if last_active < cutoff:
            continue
        classification = classify_session_route(session, report)
        reason = classification["reason_code"]
        counts[reason] += 1
        audited.append(
            {
                "session_id": session.get("id"),
                "source": session.get("source"),
                "provider": session.get("billing_provider"),
                "model": session.get("model"),
                "last_active": last_active,
                **classification,
            }
        )

    return {
        "window_days": float(days),
        "audited_sessions": len(audited),
        "classifications": dict(sorted(counts.items())),
        "sessions": audited,
        "policy": "audit_only",
    }


def _print_explanation(payload: dict[str, Any]) -> None:
    print(f"Session: {payload['session_id']}")
    print(f"Source: {payload.get('source') or '(unknown)'}")
    print(f"Runtime: {payload.get('provider') or '(unknown)'} / {payload.get('model') or '(unknown)'}")
    print(f"Reason: {payload['reason_code']}")
    if payload.get("note"):
        print(f"Note: {payload['note']}")


def cmd_routing(args) -> None:
    command = getattr(args, "routing_command", None)
    if command == "explain":
        payload = explain_session(args.session_id)
        if payload is None:
            raise SystemExit(f"No persisted session found: {args.session_id}")
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_explanation(payload)
        return
    if command == "audit":
        payload = build_routing_audit(days=args.days, limit=args.limit)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Routing audit: {payload['audited_sessions']} sessions / {payload['window_days']:g} days")
            for reason, count in payload["classifications"].items():
                print(f"  {reason}: {count}")
            print("Policy: audit only (no routing enforcement)")
        return
    raise SystemExit("routing requires 'explain' or 'audit'")


def build_routing_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "routing",
        help="Explain and audit effective model/provider routing",
    )
    commands = parser.add_subparsers(dest="routing_command", required=True)

    explain = commands.add_parser("explain", help="Explain one persisted session route")
    explain.add_argument("session_id")
    explain.add_argument("--json", action="store_true")
    explain.set_defaults(func=cmd_routing)

    audit = commands.add_parser("audit", help="Audit recent persisted routes without enforcement")
    audit.add_argument("--days", type=float, default=14)
    audit.add_argument("--limit", type=int, default=2000)
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=cmd_routing)
