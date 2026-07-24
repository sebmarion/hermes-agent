"""Host-owned BestPlan orchestration primitives.

This module deliberately keeps provider selection and receipt integrity in the
host.  Child prompts are untrusted; the host is the source of truth for model,
tool, status, quorum, and receipt identity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, TimeoutError, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import parse_reasoning_effort

RECEIPT_BEGIN_V1 = "<<<HERMES_BESTPLAN_RECEIPT_V1>>>"
RECEIPT_END_V1 = "<<<END_HERMES_BESTPLAN_RECEIPT_V1>>>"
RECEIPT_BEGIN = "<<<HERMES_BESTPLAN_RECEIPT_V2>>>"
RECEIPT_END = "<<<END_HERMES_BESTPLAN_RECEIPT_V2>>>"
RECEIPT_VERSION = 2

# Host-owned heterogeneous explorer lanes. Each lane is an immutable model/
# provider/api_mode triple.  The host alternates dispatch across lanes and picks
# the strongest available lane for synthesis.
_DEFAULT_LANES = (
    {
        "name": "glm",
        "provider": "custom:neuralwatt",
        "model": "glm-5.2",
        "api_mode": "chat_completions",
        "reasoning_effort": "high",
    },
    {
        "name": "sol",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "api_mode": "codex_app_server",
        "reasoning_effort": "ultra",
    },
)
DEFAULT_RUNTIME = {
    "enabled": True,
    "explorers": list(_DEFAULT_LANES),
    "synthesizer": "sol",
    "explorer_timeout": 180,
    "synthesizer_timeout": 180,
    "overall_timeout": 540,
}
ALLOWED_TOOLS = frozenset({"read_only_files", "web"})
TURN_MARKER = "\x00HERMES_BESTPLAN_CONFIG:"
_CHILD_CLEANUP_GRACE_SECONDS = 5.0
_CHILD_CLEANUP_HARD_SECONDS = 10.0
_EXPLORER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ALLOWED_API_MODES = frozenset({
    "chat_completions", "codex_responses", "anthropic_messages",
    "bedrock_converse", "codex_app_server",
})
_ALLOWED_REASONING_EFFORTS = frozenset({
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
})

logger = logging.getLogger(__name__)


class BestPlanUnavailable(RuntimeError):
    """Raised when the host cannot safely run BestPlan."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BestPlanUnavailable(message)


def _validate_explorer_entry(entry: dict[str, Any], index: int) -> None:
    """Validate a single explorer entry against the canonical schema."""
    required = ("name", "provider", "model", "api_mode", "reasoning_effort")
    _require(isinstance(entry, dict), f"explorer #{index} must be a dict")
    _require(set(entry.keys()) == set(required),
             f"explorer #{index} has unknown keys: {set(entry.keys()) - set(required) or 'missing keys'}")
    _require(all(isinstance(entry.get(k), str) for k in required),
             f"explorer #{index} has non-string values")
    normalized_name = str(entry["name"]).strip().lower()
    _require(normalized_name, f"explorer #{index} name is empty")
    _require(_EXPLORER_NAME_RE.match(normalized_name) is not None,
             f"explorer #{index} name '{normalized_name}' does not match the required grammar")
    _require(str(entry["provider"]).strip(), f"explorer #{index} provider is empty")
    _require(str(entry["model"]).strip(), f"explorer #{index} model is empty")
    api_mode = str(entry["api_mode"]).strip().lower()
    _require(api_mode in _ALLOWED_API_MODES,
             f"explorer #{index} has invalid api_mode '{api_mode}'")
    reasoning = str(entry["reasoning_effort"]).strip().lower()
    _require(reasoning in _ALLOWED_REASONING_EFFORTS,
             f"explorer #{index} has invalid reasoning_effort '{reasoning}'")


def _validate_explorers(explorers: list[dict[str, Any]]) -> None:
    """Validate the explorers list: count, entries, uniqueness, ultra constraint."""
    _require(1 <= len(explorers) <= 5,
             f"BestPlan must have between 1 and 5 explorers, got {len(explorers)}")
    seen: set[str] = set()
    for index, entry in enumerate(explorers):
        _validate_explorer_entry(entry, index)
        normalized_name = str(entry["name"]).strip().lower()
        _require(normalized_name not in seen,
                 f"BestPlan explorer names must be unique; duplicate '{normalized_name}'")
        seen.add(normalized_name)
        reasoning = str(entry["reasoning_effort"]).strip().lower()
        api_mode = str(entry["api_mode"]).strip().lower()
        _require(not (reasoning == "ultra" and api_mode != "codex_app_server"),
                 f"explorer '{normalized_name}' has reasoning_effort='ultra' but api_mode='{api_mode}'. "
                 "Ultra reasoning is a Codex app-server control, not a raw Responses API effort. "
                 "Route this explorer through codex_app_server.")
        _require(not (api_mode == "codex_app_server"
                       and str(entry["provider"]).strip().lower() not in {"openai", "openai-codex"}),
                 f"explorer '{normalized_name}' has api_mode='codex_app_server' but provider "
                 f"'{str(entry['provider']).strip()}' is not 'openai' or 'openai-codex'")


def _validate_timeouts(config: dict[str, Any]) -> None:
    """Validate timeout bounds per the spec: explorer/synthesizer 1..3600, overall 1..7200."""
    for key, low, high in (
        ("explorer_timeout", 1, 3600),
        ("synthesizer_timeout", 1, 3600),
        ("overall_timeout", 1, 7200),
    ):
        value = config.get(key)
        _require(isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value),
                 f"BestPlan {key} must be a finite number, got {value!r}")
        _require(low <= value <= high,
                 f"BestPlan {key} must be between {low} and {high} seconds, got {value}")


def normalize_count(value: Any, *, default: int = 3) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return max(2, min(5, count))


def quorum_for(count: int) -> int:
    count = normalize_count(count)
    return max(2, math.ceil(2 * count / 3))


def validate_runtime(config: dict[str, Any] | None = None, *, credentials_available: bool = True) -> dict[str, Any]:
    """Validate the BestPlan runtime configuration.

    Accepts both canonical ``explorers``/``synthesizer`` keys and the legacy
    ``lanes`` adapter.  When no config is supplied, the module-level
    ``DEFAULT_RUNTIME`` fallback is used.  All safety invariants — explorer
    count, entry field validation, ``ultra``→``codex_app_server`` constraint,
    required field presence, and positive timeouts — are enforced regardless
    of where the lane definition came from.

    When a canonical ``bestplan`` block is present, ``explorers`` and
    ``synthesizer`` are required.  When only legacy ``lanes`` is present,
    ``synthesizer`` defaults to the last entry's normalized name.  Unknown
    top-level keys raise ``BestPlanUnavailable``.
    """
    if config is None:
        raw = dict(DEFAULT_RUNTIME)
    else:
        _require(isinstance(config, dict), "BestPlan config must be a mapping")
        raw = dict(config)

    has_explorers = "explorers" in raw
    has_lanes = "lanes" in raw

    if has_explorers and has_lanes:
        raise BestPlanUnavailable(
            "BestPlan cannot have both 'explorers' and 'lanes'; use one or the other"
        )

    if not has_explorers and not has_lanes:
        raise BestPlanUnavailable(
            "BestPlan must have either 'explorers' or 'lanes'"
        )

    canonical_keys = {
        "enabled", "explorers", "synthesizer", "explorer_timeout",
        "synthesizer_timeout", "overall_timeout",
    }
    legacy_keys = {
        "enabled", "lanes", "synthesizer", "explorer_timeout",
        "synthesizer_timeout", "overall_timeout", "runtime_route",
    }
    allowed_keys = canonical_keys if has_explorers else legacy_keys
    unknown_keys = set(raw) - allowed_keys
    _require(not unknown_keys, f"BestPlan config has unknown keys: {sorted(unknown_keys)}")

    enabled = raw.get("enabled", True)
    _require(isinstance(enabled, bool), "BestPlan enabled must be a boolean")
    if not enabled:
        raise BestPlanUnavailable("BestPlan is disabled")
    if not credentials_available:
        raise BestPlanUnavailable("BestPlan credentials unavailable")

    resolved = {
        "enabled": enabled,
        "explorer_timeout": raw.get("explorer_timeout", 180),
        "synthesizer_timeout": raw.get("synthesizer_timeout", 180),
        "overall_timeout": raw.get("overall_timeout", 540),
    }

    if has_explorers:
        # Canonical form: explorers + synthesizer required
        if "synthesizer" not in raw:
            raise BestPlanUnavailable("BestPlan canonical config requires 'synthesizer'")

        explorers = raw["explorers"]
        _require(isinstance(explorers, list), "BestPlan explorers must be a list")
        _validate_explorers(explorers)

        _require(isinstance(raw["synthesizer"], str),
                 "BestPlan synthesizer must be a string")
        synthesizer = raw["synthesizer"].strip().lower()
        _require(_EXPLORER_NAME_RE.fullmatch(synthesizer) is not None,
                 "BestPlan synthesizer name is invalid")
        _require(synthesizer in {str(e["name"]).strip().lower() for e in explorers},
                 f"BestPlan synthesizer '{synthesizer}' must reference one configured explorer")

        resolved["explorers"] = [
            {
                "name": str(e["name"]).strip().lower(),
                "provider": str(e["provider"]).strip(),
                "model": str(e["model"]).strip(),
                "api_mode": str(e["api_mode"]).strip().lower(),
                "reasoning_effort": str(e["reasoning_effort"]).strip().lower(),
            }
            for e in explorers
        ]
        resolved["synthesizer"] = synthesizer
    else:
        # Legacy form: lanes required, synthesizer optional
        lanes = raw["lanes"]
        _require(isinstance(lanes, list), "BestPlan lanes must be a list")
        _validate_explorers(lanes)

        normalized = [
            {
                "name": str(e["name"]).strip().lower(),
                "provider": str(e["provider"]).strip(),
                "model": str(e["model"]).strip(),
                "api_mode": str(e["api_mode"]).strip().lower(),
                "reasoning_effort": str(e["reasoning_effort"]).strip().lower(),
            }
            for e in lanes
        ]

        resolved["explorers"] = normalized

        # Legacy: synthesizer is optional, defaults to the last entry
        synth_value = raw.get("synthesizer", normalized[-1]["name"])
        _require(isinstance(synth_value, str),
                 "BestPlan synthesizer must be a string")
        synthesizer = synth_value.strip().lower()
        _require(_EXPLORER_NAME_RE.fullmatch(synthesizer) is not None,
                 "BestPlan synthesizer name is invalid")
        _require(synthesizer in {entry["name"] for entry in normalized},
                 f"BestPlan synthesizer '{synthesizer}' must reference one configured explorer")
        resolved["synthesizer"] = synthesizer
        if "runtime_route" in raw:
            logger.warning("BestPlan legacy runtime_route is deprecated and ignored")

    _validate_timeouts(resolved)
    return resolved


def validate_candidate(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict) or candidate.get("schema") != "HERMES_BESTPLAN_CANDIDATE_V1":
        raise ValueError("invalid BestPlan candidate schema")
    required = ("summary", "steps", "risks", "verification")
    if any(not candidate.get(key) for key in required):
        raise ValueError("incomplete BestPlan candidate")
    return {key: candidate[key] for key in ("schema", *required)}


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def make_receipt(
    run_id: str,
    *,
    model: str,
    quorum: str,
    synth_status: str,
    body: str,
    lane: str | None = None,
    provider: str | None = None,
    api_mode: str | None = None,
    requested_count: int,
    effective_count: int,
    quorum_required: int,
    attempts: list[dict[str, Any]],
    synthesizer: dict[str, Any],
    status: str = "completed",
    reason_code: str | None = None,
) -> str:
    metadata = {
        "version": RECEIPT_VERSION,
        "run_id": run_id,
        "requested_count": requested_count,
        "effective_count": effective_count,
        "quorum_required": quorum_required,
        "attempts": attempts,
        "synthesizer": synthesizer,
        "status": status,
        "reason_code": reason_code,
        "body_sha256": body_sha256(body) if body else None,
    }
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return f"{RECEIPT_BEGIN}\n{canonical}\n{RECEIPT_END}"


def _valid_v2_receipt_metadata(metadata: dict[str, Any], body: str) -> bool:
    top_keys = {
        "version", "run_id", "requested_count", "effective_count",
        "quorum_required", "attempts", "synthesizer", "status",
        "reason_code", "body_sha256",
    }
    attempt_keys = {
        "index", "strategy", "explorer", "configured", "resolved",
        "status", "reason_code",
    }
    synth_keys = {
        "name", "configured", "resolved", "status", "reason_code",
    }
    identity_keys = {"provider", "model"}
    if set(metadata) != top_keys or metadata.get("version") != 2:
        return False
    requested_count = metadata.get("requested_count")
    if not isinstance(requested_count, int) or isinstance(requested_count, bool):
        return False
    for key in ("effective_count", "quorum_required"):
        value = metadata.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            return False
    if not 2 <= metadata["effective_count"] <= 5:
        return False
    if metadata["quorum_required"] != quorum_for(metadata["effective_count"]):
        return False
    attempts = metadata.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != metadata["effective_count"]:
        return False
    for expected_index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict) or set(attempt) != attempt_keys:
            return False
        if attempt.get("index") != expected_index:
            return False
        if attempt.get("status") not in {"success", "failed", "timeout"}:
            return False
        configured = attempt.get("configured")
        resolved = attempt.get("resolved")
        if not isinstance(configured, dict) or set(configured) != identity_keys:
            return False
        if resolved is not None and (
            not isinstance(resolved, dict) or set(resolved) != identity_keys
        ):
            return False
    synthesizer = metadata.get("synthesizer")
    if not isinstance(synthesizer, dict) or set(synthesizer) != synth_keys:
        return False
    if synthesizer.get("status") not in {
        "success", "failed", "timeout", "not_started",
    }:
        return False
    for key in ("configured", "resolved"):
        identity = synthesizer.get(key)
        if identity is not None and (
            not isinstance(identity, dict) or set(identity) != identity_keys
        ):
            return False
    if metadata.get("status") not in {"completed", "failed"}:
        return False
    expected_hash = body_sha256(body) if body else None
    if metadata["status"] == "completed":
        expected_hash = body_sha256(body)
    return metadata.get("body_sha256") == expected_hash


def validate_receipt(receipt: str, body: str) -> bool:
    try:
        begin, canonical, end = receipt.strip().splitlines()
        marker_version = None
        if begin == RECEIPT_BEGIN and end == RECEIPT_END:
            marker_version = 2
        elif begin == RECEIPT_BEGIN_V1 and end == RECEIPT_END_V1:
            marker_version = 1
        if marker_version is None:
            return False
        metadata = json.loads(canonical)
        if marker_version == 1:
            return (
                metadata.get("version") == 1
                and metadata.get("body_sha256") == body_sha256(body)
            )
        return _valid_v2_receipt_metadata(metadata, body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


def append_receipt(path: str | Path, record: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, tmp = tempfile.mkstemp(prefix="bestplan-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        with target.open("ab") as handle, open(tmp, "rb") as source:
            handle.write(source.read())
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def reconcile_bestplan_receipts(path: str | Path) -> list[str]:
    target = Path(path)
    if not target.exists():
        return []
    changed: list[str] = []
    records: list[dict[str, Any]] = []
    for line in target.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") not in {"completed", "failed", "interrupted", "unknown"}:
            record["status"] = "interrupted"
            changed.append(str(record.get("run_id", "unknown")))
        records.append(record)
    if changed:
        temp = target.with_suffix(target.suffix + ".reconciled")
        temp.write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records))
        os.replace(temp, target)
    return changed


@dataclass(frozen=True)
class ExplorerResult:
    status: str
    candidate: dict[str, Any] | None = None
    error: str | None = None


def _candidate_from_text(text: str) -> dict[str, Any]:
    marker = "HERMES_BESTPLAN_CANDIDATE_V1"
    if marker in text:
        text = text.split(marker, 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("candidate JSON missing")
    return validate_candidate(json.loads(text[start : end + 1]))


def _resolve_lane_credentials(agent: Any, lane: dict[str, Any]) -> dict[str, Any]:
    """Resolve credentials without replacing the configured lane identity."""
    from hermes_cli.runtime_provider import resolve_runtime_provider

    configured_provider = str(lane.get("provider") or "").strip()
    configured_model = str(lane.get("model") or "").strip()
    configured_api_mode = str(lane.get("api_mode") or "").strip()
    runtime = resolve_runtime_provider(
        requested=configured_provider,
        explicit_api_key=lane.get("api_key"),
        explicit_base_url=lane.get("base_url"),
        target_model=configured_model,
    )
    provider = str(runtime.get("provider") or configured_provider).strip()
    model = str(runtime.get("model") or configured_model).strip()
    api_mode = configured_api_mode or str(runtime.get("api_mode") or "").strip()
    if not provider or not model or not api_mode:
        raise BestPlanUnavailable(
            f"BestPlan lane {lane.get('name')!r} resolved an incomplete runtime identity"
        )
    return {
        **runtime,
        "provider": provider,
        "model": model,
        "api_mode": api_mode,
    }


def _build_child_agent(
    parent: Any,
    lane: dict[str, Any],
    runtime: dict[str, Any],
) -> Any:
    """Construct one child without leaking its toolset into process globals."""
    from run_agent import AIAgent
    import model_tools

    with model_tools.preserve_last_resolved_tool_names():
        fork = AIAgent(
            model=runtime["model"],
            provider=runtime["provider"],
            api_mode=runtime["api_mode"],
            base_url=runtime.get("base_url"),
            api_key=runtime.get("api_key"),
            reasoning_config=parse_reasoning_effort(
                lane.get("reasoning_effort")
            ),
            max_iterations=12,
            quiet_mode=True,
            enabled_toolsets=["read_only_files", "web"],
            skip_memory=True,
            skip_context_files=True,
            parent_session_id=getattr(parent, "session_id", None),
        )
    fork._persist_disabled = True
    fork._session_db = None
    fork._session_json_enabled = False
    fork.compression_enabled = False
    fork._skip_mcp_refresh = True
    fork.suppress_status_output = True
    return fork


def _run_child_agent(fork: Any, prompt: str) -> str:
    result = fork.run_conversation(prompt)
    return str(result.get("final_response") or "")


def _force_retire_child_transport(child: Any) -> None:
    """Best-effort hard retirement for the two BestPlan provider lanes.

    CPython cannot safely terminate an arbitrary thread. We can, however,
    abort the active HTTP request, force-close its sockets, and terminate the
    owned Codex app-server subprocess. This function always runs on a daemon
    teardown thread because a hostile third-party client may block in close().
    """
    abort = getattr(child, "_active_request_abort", None)
    if callable(abort):
        try:
            abort("bestplan_hard_deadline")
        except Exception:
            pass

    client = getattr(child, "client", None)
    if client is not None:
        force_close = getattr(child, "_force_close_tcp_sockets", None)
        if callable(force_close):
            try:
                force_close(client)
            except Exception:
                pass
        close_client = getattr(client, "close", None)
        if callable(close_client):
            try:
                close_client()
            except Exception:
                pass

    codex_session = getattr(child, "_codex_session", None)
    codex_client = getattr(codex_session, "_client", None)
    close_codex = getattr(codex_client, "close", None)
    if callable(close_codex):
        try:
            close_codex(timeout=0.5)
        except TypeError:
            try:
                close_codex()
            except Exception:
                pass
        except Exception:
            pass


def _stop_child_agents(
    children: Iterable[Any],
    futures: Iterable[Future[Any]] = (),
) -> bool:
    """Retire provider work within a finite hard teardown deadline.

    Returns False only when an unkillable Python worker or hostile close call
    survives the deadline. Such workers run on daemon executors and their
    children are persistence-disabled/read-only, so they are quarantined
    rather than allowed to hang the BestPlan command indefinitely.
    """
    unique = list({id(child): child for child in children}.values())
    submitted = list(dict.fromkeys(futures))
    teardown_threads = []

    def start_teardown(target, *, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        teardown_threads.append(thread)

    for child in unique:
        # Quarantine any late activity from a worker that proves unkillable.
        try:
            child._persist_disabled = True
            child.tool_progress_callback = None
            child.suppress_status_output = True
            child._bestplan_quarantined = True
        except Exception:
            pass

        def interrupt_one(target=child):
            try:
                target.interrupt("BestPlan deadline or completion cleanup")
            except TypeError:
                try:
                    target.interrupt()
                except Exception:
                    pass
            except Exception:
                pass

        def close_one(target=child):
            try:
                target.close()
            except Exception:
                pass

        start_teardown(interrupt_one, name="bestplan-interrupt")
        start_teardown(close_one, name="bestplan-close")

    started = time.monotonic()
    hard_seconds = max(0.0, float(_CHILD_CLEANUP_HARD_SECONDS))
    hard_deadline = started + hard_seconds
    grace_deadline = min(
        hard_deadline,
        started + max(0.0, float(_CHILD_CLEANUP_GRACE_SECONDS)),
    )
    pending = [future for future in submitted if not future.done()]
    if pending:
        wait(pending, timeout=max(0.0, grace_deadline - time.monotonic()))
    for thread in teardown_threads:
        thread.join(timeout=max(0.0, grace_deadline - time.monotonic()))

    still_running = [future for future in submitted if not future.done()]
    still_tearing_down = [thread for thread in teardown_threads if thread.is_alive()]
    if still_running or still_tearing_down:
        for child in unique:
            start_teardown(
                lambda target=child: _force_retire_child_transport(target),
                name="bestplan-force-retire",
            )

    if still_running:
        wait(still_running, timeout=max(0.0, hard_deadline - time.monotonic()))
    for thread in teardown_threads:
        thread.join(timeout=max(0.0, hard_deadline - time.monotonic()))

    still_running = [future for future in submitted if not future.done()]
    still_tearing_down = [thread for thread in teardown_threads if thread.is_alive()]
    complete = not still_running and not still_tearing_down
    if not complete:
        logger.error(
            "BestPlan hard teardown deadline reached; quarantined %d provider "
            "worker(s) and %d teardown call(s)",
            len(still_running),
            len(still_tearing_down),
        )
    return complete


def run_bestplan(
    agent: Any,
    task: str,
    *,
    count: int = 3,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run bounded explorers and synthesis with hard-bounded teardown."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config().get("bestplan")
        except Exception:
            config = None
    resolved = validate_runtime(config, credentials_available=True)
    effective = normalize_count(count)
    try:
        requested_count = int(count)
    except (TypeError, ValueError):
        requested_count = 3
    run_id = uuid.uuid4().hex
    started_at = time.monotonic()
    overall_deadline = started_at + float(resolved["overall_timeout"])
    protocols = (
        "evidence-first",
        "counterfactual",
        "failure-first",
        "verification-first",
        "scope-first",
    )
    explorers = resolved["explorers"]

    synth_name = resolved["synthesizer"]
    synth_explorer = next(e for e in explorers if e["name"] == synth_name)
    attempts: list[dict[str, Any]] = []
    for index in range(effective):
        explorer = explorers[index % len(explorers)]
        attempts.append({
            "index": index,
            "strategy": protocols[index % len(protocols)],
            "explorer": str(explorer["name"]),
            "configured": {
                "provider": explorer["provider"],
                "model": explorer["model"],
            },
            "resolved": None,
            "status": "pending",
            "reason_code": None,
            "_candidate": None,
        })

    def visible_attempts() -> list[dict[str, Any]]:
        return [
            {key: value for key, value in attempt.items() if not key.startswith("_")}
            for attempt in attempts
        ]

    synth_configured = {
        "provider": synth_explorer["provider"],
        "model": synth_explorer["model"],
    }
    synth_runtime: dict[str, Any] | None = None

    def fail_terminal(
        *,
        error: str,
        reason_code: str,
        synthesizer_status: str = "not_started",
        synthesizer_reason_code: str | None = None,
        attempt_reason_code: str | None = None,
        cleanup_incomplete: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        terminal_attempt_reason = attempt_reason_code or reason_code
        for attempt in attempts:
            if attempt["status"] == "pending":
                attempt["status"] = (
                    "timeout"
                    if terminal_attempt_reason in {"timeout", "overall_timeout"}
                    else "failed"
                )
                attempt["reason_code"] = terminal_attempt_reason
        receipt_attempts = visible_attempts()
        receipt_synthesizer = {
            "name": synth_explorer["name"],
            "configured": synth_configured,
            "resolved": (
                {
                    "provider": synth_runtime["provider"],
                    "model": synth_runtime["model"],
                }
                if synth_runtime is not None
                else None
            ),
            "status": synthesizer_status,
            "reason_code": synthesizer_reason_code or reason_code,
        }
        receipt = make_receipt(
            run_id,
            model=(
                synth_runtime["model"]
                if synth_runtime is not None
                else synth_explorer["model"]
            ),
            provider=(
                synth_runtime["provider"]
                if synth_runtime is not None
                else synth_explorer["provider"]
            ),
            api_mode=(
                synth_runtime["api_mode"]
                if synth_runtime is not None
                else synth_explorer["api_mode"]
            ),
            quorum=f"0/{effective}",
            synth_status=synthesizer_status,
            body="",
            lane=synth_explorer.get("name"),
            requested_count=requested_count,
            effective_count=effective,
            quorum_required=quorum_for(effective),
            attempts=receipt_attempts,
            synthesizer=receipt_synthesizer,
            status="failed",
            reason_code=reason_code,
        )
        receipt_record = json.loads(receipt.splitlines()[1])
        receipt_persisted = True
        try:
            home = Path(
                os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
            )
            append_receipt(home / "bestplan" / "receipts.jsonl", receipt_record)
        except Exception:
            receipt_persisted = False
            logger.error("BestPlan terminal receipt persistence failed")
        payload: dict[str, Any] = {
            "status": "failed",
            "error": error,
            "reason_code": (
                reason_code if receipt_persisted else "receipt_persistence_failed"
            ),
            "run_id": run_id,
            "attempts": receipt_attempts,
            "receipt": receipt,
        }
        if cleanup_incomplete:
            payload["cleanup_incomplete"] = True
        if not receipt_persisted:
            payload["receipt_persisted"] = False
        if extra:
            payload.update(extra)
        return payload

    try:
        synth_runtime = _resolve_lane_credentials(agent, synth_explorer)
    except Exception:
        return fail_terminal(
            error="BestPlan synthesizer credentials unavailable",
            reason_code="credential_unavailable",
        )
    try:
        synth_preflight_child = _build_child_agent(
            agent, synth_explorer, synth_runtime
        )
    except Exception:
        return fail_terminal(
            error="BestPlan synthesizer construction failed",
            reason_code="construction_failed",
        )
    if not _stop_child_agents([synth_preflight_child]):
        return fail_terminal(
            error="BestPlan synthesizer preflight teardown failed",
            reason_code="runtime_invalid",
            cleanup_incomplete=True,
        )

    explorer_runtimes: dict[str, dict[str, Any]] = {synth_name: synth_runtime}
    explorer_errors: dict[str, str] = {}
    for explorer in explorers:
        explorer_name = str(explorer["name"])
        if explorer_name == synth_name:
            continue
        try:
            explorer_runtimes[explorer_name] = _resolve_lane_credentials(agent, explorer)
        except Exception:
            explorer_errors[explorer_name] = "credential_unavailable"

    for attempt in attempts:
        explorer_name = attempt["explorer"]
        runtime = explorer_runtimes.get(explorer_name)
        attempt["resolved"] = (
                {"provider": runtime["provider"], "model": runtime["model"]}
                if runtime is not None
                else None
            )

    base = (
        "You are a private BestPlan explorer. Work read-only using only file/web inspection. "
        "Return exactly one JSON object prefixed HERMES_BESTPLAN_CANDIDATE_V1 with keys "
        "schema,summary,steps,risks,verification. Task:\n" + task + "\nStrategy: "
    )
    explorer_jobs: list[tuple[Any, str, int]] = []
    for index, attempt in enumerate(attempts):
        explorer = explorers[index % len(explorers)]
        explorer_name = str(explorer["name"])
        runtime = explorer_runtimes.get(explorer_name)
        if runtime is None:
            attempt["status"] = "failed"
            attempt["reason_code"] = explorer_errors.get(
                explorer_name, "credential_unavailable"
            )
            continue
        if time.monotonic() >= overall_deadline:
            for pending_attempt in attempts:
                if pending_attempt["status"] == "pending":
                    pending_attempt["status"] = "failed"
                    pending_attempt["reason_code"] = "overall_timeout"
            cleanup_complete = _stop_child_agents(
                child for child, _prompt, _index in explorer_jobs
            )
            return fail_terminal(
                error="BestPlan overall timeout during explorer construction",
                reason_code="overall_timeout",
                cleanup_incomplete=not cleanup_complete,
            )
        try:
            child = _build_child_agent(agent, explorer, runtime)
            explorer_jobs.append(
                (child, base + protocols[index % len(protocols)], index)
            )
        except Exception:
            attempt["status"] = "failed"
            attempt["reason_code"] = "construction_failed"

    from tools.daemon_pool import DaemonThreadPoolExecutor

    pool = DaemonThreadPoolExecutor(
        max_workers=max(1, len(explorer_jobs)),
        thread_name_prefix="bestplan-explorer",
    )
    future_to_job: dict[Future[str], tuple[Any, int]] = {
        pool.submit(_run_child_agent, child, prompt): (child, index)
        for child, prompt, index in explorer_jobs
    }
    pending = set(future_to_job)
    explorer_deadline = min(
        overall_deadline,
        time.monotonic() + float(resolved["explorer_timeout"]),
    )
    overall_limited_explorers = explorer_deadline == overall_deadline
    explorer_cleanup_complete = True
    try:
        while pending:
            remaining = explorer_deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(
                pending,
                timeout=remaining,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                break
            for future in done:
                _child, attempt_index = future_to_job[future]
                attempt = attempts[attempt_index]
                try:
                    attempt["_candidate"] = _candidate_from_text(future.result())
                    attempt["status"] = "success"
                except Exception:
                    attempt["status"] = "failed"
                    attempt["reason_code"] = "candidate_invalid"
        for future in pending:
            _child, attempt_index = future_to_job[future]
            attempts[attempt_index]["status"] = "timeout"
            attempts[attempt_index]["reason_code"] = "timeout"
            future.cancel()
    finally:
        explorer_cleanup_complete = _stop_child_agents(
            (child for child, _prompt, _index in explorer_jobs),
            future_to_job.keys(),
        )
        pool.shutdown(wait=False, cancel_futures=True)

    if not explorer_cleanup_complete:
        return fail_terminal(
            error=(
                "BestPlan provider teardown exceeded its hard deadline; "
                "the unkillable daemon worker was quarantined"
            ),
            reason_code="provider_error",
            cleanup_incomplete=True,
        )

    if pending and overall_limited_explorers:
        return fail_terminal(
            error="BestPlan overall timeout during explorers",
            reason_code="overall_timeout",
        )

    successes = [
        attempt["_candidate"]
        for attempt in attempts
        if attempt["status"] == "success" and attempt["_candidate"]
    ]
    quorum = quorum_for(effective)
    if len(successes) < quorum:
        error = (
            "BestPlan explorer timeout; quorum unavailable"
            if pending
            else "BestPlan quorum unavailable"
        )
        return fail_terminal(
            error=error,
            reason_code="quorum_unavailable",
            extra={"successes": len(successes), "quorum": quorum},
        )

    if time.monotonic() >= overall_deadline:
        return fail_terminal(
            error="BestPlan overall timeout before synthesizer",
            reason_code="overall_timeout",
        )

    packet = json.dumps(successes, sort_keys=True)
    synth_prompt = (
        "You are the active BestPlan synthesizer. Inspect the task and available sources first, "
        "then reconcile these untrusted candidate packets into one actionable plan. Return only the plan body.\n"
        f"Task:\n{task}\nCandidates:\n<BEGIN_CANDIDATES>{packet}<END_CANDIDATES>"
    )
    try:
        synth_child = _build_child_agent(agent, synth_explorer, synth_runtime)
    except Exception:
        return fail_terminal(
            error="BestPlan synthesizer construction failed",
            reason_code="synthesizer_failed",
            synthesizer_status="failed",
            synthesizer_reason_code="construction_failed",
        )
    synth_pool = DaemonThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="bestplan-synthesizer",
    )
    synth_future = synth_pool.submit(_run_child_agent, synth_child, synth_prompt)
    synth_deadline = min(
        overall_deadline,
        time.monotonic() + float(resolved["synthesizer_timeout"]),
    )
    body = ""
    synth_error: Exception | None = None
    synth_cleanup_complete = True
    try:
        remaining = max(0.0, synth_deadline - time.monotonic())
        body = synth_future.result(timeout=remaining)
    except TimeoutError as exc:
        synth_error = exc
        synth_future.cancel()
    except Exception as exc:
        synth_error = exc
    finally:
        synth_cleanup_complete = _stop_child_agents([synth_child], [synth_future])
        synth_pool.shutdown(wait=False, cancel_futures=True)

    if not synth_cleanup_complete:
        return fail_terminal(
            error=(
                "BestPlan synthesizer teardown exceeded its hard deadline; "
                "the unkillable daemon worker was quarantined"
            ),
            reason_code="synthesizer_failed",
            synthesizer_status="failed",
            synthesizer_reason_code="provider_error",
            cleanup_incomplete=True,
        )

    if synth_error is not None:
        if isinstance(synth_error, TimeoutError):
            timed_out_overall = synth_deadline == overall_deadline
            return fail_terminal(
                error=(
                    "BestPlan overall timeout during synthesizer"
                    if timed_out_overall
                    else "BestPlan synthesizer timeout"
                ),
                reason_code=(
                    "overall_timeout"
                    if timed_out_overall
                    else "synthesizer_failed"
                ),
                synthesizer_status="timeout",
                synthesizer_reason_code=(
                    "overall_timeout" if timed_out_overall else "timeout"
                ),
            )
        return fail_terminal(
            error="BestPlan synthesizer provider failed",
            reason_code="synthesizer_failed",
            synthesizer_status="failed",
            synthesizer_reason_code="provider_error",
        )
    if not body.strip():
        return fail_terminal(
            error="BestPlan synthesizer empty",
            reason_code="synthesizer_failed",
            synthesizer_status="failed",
            synthesizer_reason_code="synthesizer_failed",
        )

    quorum_text = f"{len(successes)}/{effective}"
    receipt_attempts = visible_attempts()
    receipt_synthesizer = {
        "name": synth_explorer["name"],
        "configured": {
            "provider": synth_explorer["provider"],
            "model": synth_explorer["model"],
        },
        "resolved": {
            "provider": synth_runtime["provider"],
            "model": synth_runtime["model"],
        },
        "status": "success",
        "reason_code": None,
    }
    receipt = make_receipt(
        run_id,
        model=synth_runtime["model"],
        provider=synth_runtime["provider"],
        api_mode=synth_runtime["api_mode"],
        quorum=quorum_text,
        synth_status="success",
        body=body,
        lane=synth_explorer.get("name"),
        requested_count=requested_count,
        effective_count=effective,
        quorum_required=quorum,
        attempts=receipt_attempts,
        synthesizer=receipt_synthesizer,
    )
    receipt_record = json.loads(receipt.splitlines()[1])
    try:
        home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        append_receipt(home / "bestplan" / "receipts.jsonl", receipt_record)
    except Exception:
        pass
    return {
        "status": "completed",
        "run_id": run_id,
        "final_response": f"{receipt}\n\n{body}",
        "body": body,
        "successes": len(successes),
        "quorum": quorum,
        "attempts": visible_attempts(),
        "runtime": {
            "lane": synth_explorer.get("name"),
            "provider": synth_runtime["provider"],
            "model": synth_runtime["model"],
            "api_mode": synth_runtime["api_mode"],
        },
    }


__all__ = [
    "ALLOWED_TOOLS", "BestPlanUnavailable", "DEFAULT_RUNTIME", "ExplorerResult",
    "RECEIPT_BEGIN", "RECEIPT_END", "TURN_MARKER", "append_receipt", "body_sha256", "make_receipt",
    "normalize_count", "quorum_for", "reconcile_bestplan_receipts", "run_bestplan",
    "validate_candidate", "validate_receipt", "validate_runtime",
]
