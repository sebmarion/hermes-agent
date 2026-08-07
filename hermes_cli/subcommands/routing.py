"""Credential-free explanation of persisted session routing facts."""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from typing import Any


def _clean_route_id(value: Any) -> str:
    return str(value or "").strip()


def _provider_compatible(observed: str, expected: str) -> bool:
    observed = _clean_route_id(observed).lower()
    expected = _clean_route_id(expected).lower()
    if not observed or not expected:
        return False
    if observed == expected:
        return True
    return observed == "custom" and expected.startswith("custom:")


def _is_delegated_session(session: dict[str, Any]) -> bool:
    if _clean_route_id(session.get("source")).lower() == "subagent":
        return True
    raw = session.get("model_config")
    if not raw:
        return False
    try:
        config = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(config, dict) and bool(config.get("_delegate_from"))


def configured_routing_snapshot() -> dict[str, dict[str, Any]]:
    """Read only provider/model identifiers; never resolve or return credentials."""
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    raw_model = config.get("model")
    if isinstance(raw_model, dict):
        main_provider = _clean_route_id(raw_model.get("provider"))
        main_model = _clean_route_id(raw_model.get("default") or raw_model.get("name"))
    elif isinstance(raw_model, str):
        main_provider = ""
        main_model = raw_model.strip()
    else:
        main_provider = ""
        main_model = ""
    if main_provider.lower() == "auto":
        main_provider = ""

    raw_delegation = config.get("delegation")
    delegation = raw_delegation if isinstance(raw_delegation, dict) else {}
    raw_lanes = delegation.get("lanes")
    if "lanes" in delegation:
        candidates: list[dict[str, str]] = []
        complete = isinstance(raw_lanes, dict) and bool(raw_lanes)
        if isinstance(raw_lanes, dict):
            for lane in raw_lanes.values():
                if not isinstance(lane, dict):
                    complete = False
                    continue
                provider = _clean_route_id(lane.get("provider"))
                model = _clean_route_id(lane.get("model"))
                if not provider or not model:
                    complete = False
                    continue
                route = {"provider": provider, "model": model}
                if route not in candidates:
                    candidates.append(route)
        delegation_route: dict[str, Any] = {
            "provider": None,
            "model": None,
            "candidates": candidates,
            "complete": complete,
        }
        return {
            "main": {
                "provider": main_provider or None,
                "model": main_model or None,
            },
            "delegation": delegation_route,
        }

    delegation_provider = _clean_route_id(delegation.get("provider"))
    delegation_model = _clean_route_id(delegation.get("model"))
    provider_is_auto = delegation_provider.lower() == "auto"
    if provider_is_auto:
        delegation_provider = ""
    if (
        not provider_is_auto
        and not delegation_provider
        and delegation_model
        and "base_url" not in delegation
    ):
        delegation_provider = main_provider
    complete = bool(delegation and delegation_provider and delegation_model)

    return {
        "main": {
            "provider": main_provider or None,
            "model": main_model or None,
        },
        "delegation": {
            "provider": delegation_provider or None,
            "model": delegation_model or None,
            "candidates": (
                [{"provider": delegation_provider, "model": delegation_model}]
                if complete
                else []
            ),
            "complete": complete,
        },
    }


def classify_session_route(
    session: dict[str, Any], routes: dict[str, Any]
) -> dict[str, Any]:
    """Classify what persisted facts prove, without inventing a route reason."""
    observed_provider = _clean_route_id(session.get("billing_provider"))
    observed_model = _clean_route_id(session.get("model"))
    if not observed_provider or not observed_model:
        return {
            "reason_code": "unknown_runtime",
            "expected_route": None,
            "note": "Provider or model was not persisted for this session.",
        }

    delegated = _is_delegated_session(session)
    route_name = "delegation" if delegated else "main"
    expected_raw = routes.get(route_name)
    expected = expected_raw if isinstance(expected_raw, dict) else {}
    if delegated and "candidates" in expected:
        candidates = expected.get("candidates")
        if expected.get("complete") is not True or not isinstance(candidates, list):
            return {
                "reason_code": "unknown_configuration",
                "expected_route": None,
                "note": "Configured delegation routes are incomplete.",
            }
        safe_candidates = [
            {
                "provider": _clean_route_id(item.get("provider")),
                "model": _clean_route_id(item.get("model")),
            }
            for item in candidates
            if isinstance(item, dict)
            and _clean_route_id(item.get("provider"))
            and _clean_route_id(item.get("model"))
        ]
        if len(safe_candidates) != len(candidates) or not safe_candidates:
            return {
                "reason_code": "unknown_configuration",
                "expected_route": None,
                "note": "Configured delegation routes are incomplete.",
            }
        for candidate in safe_candidates:
            if observed_model == candidate["model"] and _provider_compatible(
                observed_provider, candidate["provider"]
            ):
                return {
                    "reason_code": "matches_delegation",
                    "expected_route": candidate,
                    "note": (
                        "Runtime matches a configured delegation route; "
                        "lane identity was not persisted."
                    ),
                }
        return {
            "reason_code": "unexplained",
            "expected_route": None,
            "configured_route_count": len(safe_candidates),
            "note": (
                "Persisted runtime differs from every configured delegation "
                "route; override, fallback, and health-cooldown reasons are not persisted."
            ),
        }

    expected_provider = _clean_route_id(expected.get("provider"))
    expected_model = _clean_route_id(expected.get("model"))
    if not expected_provider or not expected_model:
        return {
            "reason_code": "unknown_configuration",
            "expected_route": None,
            "note": f"The configured {route_name} provider/model is incomplete.",
        }

    safe_expected = {
        "provider": expected_provider,
        "model": expected_model,
    }
    if observed_model == expected_model and _provider_compatible(
        observed_provider, expected_provider
    ):
        return {
            "reason_code": ("matches_delegation" if delegated else "matches_main"),
            "expected_route": safe_expected,
        }

    return {
        "reason_code": "unexplained",
        "expected_route": safe_expected,
        "note": (
            "Persisted runtime differs from the configured route; override, "
            "fallback, and health-cooldown reasons are not persisted."
        ),
    }


def _safe_session_result(
    session: dict[str, Any], routes: dict[str, Any]
) -> dict[str, Any]:
    last_active = float(session.get("last_active") or session.get("started_at") or 0.0)
    return {
        "session_id": session.get("id"),
        "source": session.get("source"),
        "provider": session.get("billing_provider"),
        "model": session.get("model"),
        "started_at": session.get("started_at"),
        "last_active": last_active,
        **classify_session_route(session, routes),
    }


def explain_session(session_id: str) -> dict[str, Any] | None:
    from hermes_state import SessionDB

    routes = configured_routing_snapshot()
    db = SessionDB(read_only=True)
    try:
        session = db.get_session(session_id)
    finally:
        db.close()
    return _safe_session_result(session, routes) if session else None


def build_routing_audit(*, days: float = 14, limit: int = 2000) -> dict[str, Any]:
    from hermes_state import SessionDB

    if (
        isinstance(days, bool)
        or not isinstance(days, (int, float))
        or not math.isfinite(float(days))
        or days <= 0
    ):
        raise ValueError("days must be positive and finite")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be positive")
    routes = configured_routing_snapshot()
    normalized_limit = min(limit, 10000)
    cutoff = time.time() - float(days) * 86400
    db = SessionDB(read_only=True)
    try:
        rows = db.search_sessions(limit=normalized_limit + 1)
    finally:
        db.close()

    matching: list[dict[str, Any]] = []
    for row in rows:
        safe = _safe_session_result(row, routes)
        if safe["last_active"] >= cutoff:
            matching.append(safe)
    truncated = len(matching) > normalized_limit
    sessions = matching[:normalized_limit]
    counts: Counter[str] = Counter()
    for safe in sessions:
        counts[safe["reason_code"]] += 1
    return {
        "window_days": float(days),
        "limit": normalized_limit,
        "complete": not truncated,
        "truncated": truncated,
        "audited_sessions": len(sessions),
        "classifications": dict(sorted(counts.items())),
        "sessions": sessions,
        "policy": "audit_only",
    }


def _print_explanation(payload: dict[str, Any]) -> None:
    print(f"Session: {payload['session_id']}")
    print(f"Source: {payload.get('source') or '(unknown)'}")
    print(
        "Runtime: "
        f"{payload.get('provider') or '(unknown)'} / "
        f"{payload.get('model') or '(unknown)'}"
    )
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
            print(
                f"Routing audit: {payload['audited_sessions']} sessions / "
                f"{payload['window_days']:g} days"
            )
            if payload["truncated"]:
                print(f"  partial: result limit {payload['limit']} reached")
            for reason, count in payload["classifications"].items():
                print(f"  {reason}: {count}")
            print("Policy: audit only (no routing enforcement)")
        return
    raise SystemExit("routing requires 'explain' or 'audit'")


def build_routing_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "routing",
        help="Explain and audit persisted model/provider routing facts",
    )
    commands = parser.add_subparsers(dest="routing_command", required=True)

    explain = commands.add_parser("explain", help="Explain one persisted session route")
    explain.add_argument("session_id")
    explain.add_argument("--json", action="store_true")
    explain.set_defaults(func=cmd_routing)

    audit = commands.add_parser(
        "audit", help="Audit recent persisted routes without enforcement"
    )
    audit.add_argument("--days", type=float, default=14)
    audit.add_argument("--limit", type=int, default=2000)
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=cmd_routing)
