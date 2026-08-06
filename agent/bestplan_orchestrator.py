"""Host-owned BestPlan orchestration primitives.

This module deliberately keeps provider selection and receipt integrity in the
host.  Child prompts are untrusted; the host is the source of truth for model,
tool, status, quorum, and receipt identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import parse_reasoning_effort

RECEIPT_BEGIN = "<<<HERMES_BESTPLAN_RECEIPT_V1>>>"
RECEIPT_END = "<<<END_HERMES_BESTPLAN_RECEIPT_V1>>>"
RECEIPT_VERSION = 1

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
    "runtime_route": "codex_responses",
    "lanes": list(_DEFAULT_LANES),
    "explorer_timeout": 180,
    "synthesizer_timeout": 180,
    "overall_timeout": 540,
}
SINGLE_PROVIDER_MOE_REPLICAS = 3
ALLOWED_TOOLS = frozenset({"read_only_files", "web"})
TURN_MARKER = "\x00HERMES_BESTPLAN_CONFIG:"


class BestPlanUnavailable(RuntimeError):
    """Raised when the host cannot safely run BestPlan."""


def normalize_count(value: Any, *, default: int = 3) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return max(2, min(5, count))


def quorum_for(count: int) -> int:
    count = normalize_count(count)
    return max(2, math.ceil(2 * count / 3))


def normalize_lanes(raw: Any) -> list[Any]:
    """Normalize list-style and YAML-mapping lane configuration."""
    if isinstance(raw, dict):
        normalized: list[Any] = []
        for name, lane in raw.items():
            if isinstance(lane, dict):
                lane = dict(lane)
                lane.setdefault("name", name)
            normalized.append(lane)
        return normalized
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes)):
        raise BestPlanUnavailable("BestPlan lanes config is unavailable")
    return list(raw)


def validate_runtime(config: dict[str, Any] | None = None, *, credentials_available: bool = True) -> dict[str, Any]:
    """Validate the BestPlan runtime configuration.

    Lane model strings are sourced from the ``bestplan.lanes`` config block
    (or the ``config`` dict passed by the caller).  When no config is supplied,
    the module-level ``DEFAULT_RUNTIME`` fallback is used.  The safety
    invariants — at least one complete lane, the
    ``ultra``→``codex_app_server`` constraint, required field presence, and
    positive timeouts — are always enforced regardless of where the lane
    definition came from.  Provider availability is resolved separately at
    runtime because a syntactically valid lane can still be disabled or
    unauthenticated.
    """
    resolved = dict(DEFAULT_RUNTIME)
    if config is not None:
        if not isinstance(config, dict):
            raise BestPlanUnavailable("BestPlan config must be a mapping")
        resolved.update(config)
    if not resolved.get("enabled", True):
        raise BestPlanUnavailable("BestPlan is disabled")
    lanes = normalize_lanes(resolved.get("lanes"))
    if not lanes:
        raise BestPlanUnavailable("BestPlan requires at least one explorer lane")
    required_lane_keys = ("name", "provider", "model", "api_mode", "reasoning_effort")
    for lane in lanes:
        if not isinstance(lane, dict):
            raise BestPlanUnavailable("BestPlan lane must be a dict")
        missing = [k for k in required_lane_keys if not lane.get(k)]
        if missing:
            raise BestPlanUnavailable(f"BestPlan lane missing required keys: {missing}")
        name = str(lane["name"]).strip().lower()
        reasoning = str(lane["reasoning_effort"]).strip().lower()
        api_mode = str(lane["api_mode"]).strip().lower()
        # The ultra→codex_app_server safety contract (see codex_responses_adapter.py:50-55).
        # Ultra reasoning is a Codex app-server turn control, not a raw Responses
        # API effort; routing it through codex_responses will be rejected at the
        # wire.  Enforce here so misconfiguration fails closed before any
        # explorer or synthesizer is dispatched.
        if reasoning == "ultra" and api_mode != "codex_app_server":
            raise BestPlanUnavailable(
                f"BestPlan lane '{name}' has reasoning_effort='ultra' but api_mode='{api_mode}'. "
                "Ultra reasoning is a Codex app-server control, not a raw Responses API effort. "
                "Route Sol Ultra through codex_app_server."
            )
    if not credentials_available:
        raise BestPlanUnavailable("BestPlan credentials unavailable")
    for key in ("explorer_timeout", "synthesizer_timeout", "overall_timeout"):
        if not isinstance(resolved.get(key), (int, float)) or resolved[key] <= 0:
            raise BestPlanUnavailable(f"invalid BestPlan timeout: {key}")
    resolved["lanes"] = lanes
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


def make_receipt(run_id: str, *, model: str, quorum: str, synth_status: str, body: str, lane: str | None = None) -> str:
    metadata = {
        "version": RECEIPT_VERSION,
        "run_id": run_id,
        "model": model,
        "lane": lane,
        "quorum": quorum,
        "synth_status": synth_status,
        "body_sha256": body_sha256(body),
    }
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return f"{RECEIPT_BEGIN}\n{canonical}\n{RECEIPT_END}"


def validate_receipt(receipt: str, body: str) -> bool:
    try:
        begin, canonical, end = receipt.strip().splitlines()
        if begin != RECEIPT_BEGIN or end != RECEIPT_END:
            return False
        metadata = json.loads(canonical)
        return metadata.get("version") == RECEIPT_VERSION and metadata.get("body_sha256") == body_sha256(body)
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
    for line in target.read_text(encoding="utf-8").splitlines():
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
        temp.write_text(
            "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records),
            encoding="utf-8",
        )
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
    """Resolve provider credentials for one explorer/synthesizer lane.

    Configured providers are resolved through the normal runtime provider path
    so config.yaml enablement and secrets are honoured. Codex app-server lanes
    use provider-managed auth; api_key is None because the Codex adapter reads
    credentials from the Hermes auth store / ~/.codex directory.
    """
    requested_provider = str(lane.get("provider") or "").strip()
    target_model = str(lane.get("model") or "").strip()
    if not requested_provider or not target_model:
        raise BestPlanUnavailable("BestPlan lane has no provider/model")
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(
            requested=requested_provider,
            explicit_api_key=lane.get("api_key"),
            explicit_base_url=lane.get("base_url"),
            target_model=target_model,
        )
    except Exception as exc:
        raise BestPlanUnavailable(
            f"BestPlan provider '{requested_provider}' unavailable for model '{target_model}': {exc}"
        ) from exc

    if requested_provider.lower() == "openai-codex":
        parent_is_codex = str(getattr(agent, "provider", "") or "").lower() == "openai-codex"
        has_codex_auth = bool(
            runtime.get("api_key")
            or (Path.home() / ".codex").exists()
            or (parent_is_codex and (getattr(agent, "api_key", None) or getattr(agent, "_credential_pool", None)))
        )
        if not has_codex_auth:
            raise BestPlanUnavailable("BestPlan Codex credentials unavailable")
        runtime.update({
            "provider": "openai-codex",
            "model": target_model,
            "api_mode": "codex_app_server",
            "base_url": lane.get("base_url") or runtime.get("base_url") or "https://chatgpt.com/backend-api/codex",
            # The app-server adapter owns Codex auth; do not pass an arbitrary
            # parent key into an app-server child.
            "api_key": None,
            "requested_provider": requested_provider,
        })
        return runtime

    if not runtime.get("provider") or not runtime.get("api_mode"):
        raise BestPlanUnavailable(
            f"BestPlan provider '{requested_provider}' returned incomplete runtime credentials"
        )
    runtime.setdefault("model", target_model)
    runtime.setdefault("requested_provider", requested_provider)
    return runtime


def _lane_priority(lane: dict[str, Any], index: int) -> float:
    """Return an explicit lane priority, preserving current list-order fallback."""
    try:
        return float(lane.get("priority", index))
    except (TypeError, ValueError):
        return float(index)


def _provider_key(record: dict[str, Any]) -> str:
    lane = record["lane"]
    credentials = record["credentials"]
    provider = credentials.get("requested_provider") or lane.get("provider") or credentials.get("provider")
    provider = str(provider).strip().lower()
    return provider.removeprefix("custom:")


def _active_lane_records(agent: Any, lanes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve all lanes and return active records plus unavailable-lane reasons."""
    active: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for index, lane in enumerate(lanes):
        try:
            credentials = _resolve_lane_credentials(agent, lane)
        except BestPlanUnavailable as exc:
            unavailable.append(f"{lane.get('name', index)}: {exc}")
            continue
        except Exception as exc:
            unavailable.append(f"{lane.get('name', index)}: {type(exc).__name__}")
            continue
        active.append({
            "lane": lane,
            "credentials": credentials,
            "index": index,
            "priority": _lane_priority(lane, index),
        })
    return active, unavailable


def _best_lane_per_provider(active_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse active lanes to the strongest lane for each provider."""
    best_by_provider: dict[str, dict[str, Any]] = {}
    for record in active_records:
        provider = _provider_key(record)
        current = best_by_provider.get(provider)
        if current is None or (record["priority"], -record["index"]) > (current["priority"], -current["index"]):
            best_by_provider[provider] = record
    return sorted(best_by_provider.values(), key=lambda item: item["index"])


def build_explorer_schedule(active_records: list[dict[str, Any]], count: int = 3) -> tuple[list[dict[str, Any]], str]:
    """Build the explorer fan-out and identify its resilience mode.

    If only one unique provider is active, the highest-priority model is used
    for exactly three independent explorer instances. With multiple providers,
    the highest-priority lane for each provider is round-robin scheduled for
    the requested explorer count.
    """
    if not active_records:
        return [], "no_active_provider"

    provider_lanes = _best_lane_per_provider(active_records)
    if len(provider_lanes) == 1:
        return [provider_lanes[0]] * SINGLE_PROVIDER_MOE_REPLICAS, "single_provider_moe"

    effective = normalize_count(count)
    return [provider_lanes[index % len(provider_lanes)] for index in range(effective)], "heterogeneous"


def _run_child_with_timeout(child: Any, prompt: str, record: dict[str, Any], timeout: float) -> str:
    """Run one child on a daemon thread so a stalled provider cannot block the host."""
    result: dict[str, Any] = {}
    finished = threading.Event()

    def worker() -> None:
        try:
            result["value"] = child(prompt, record)
        except Exception as exc:
            result["error"] = exc
        finally:
            finished.set()

    threading.Thread(target=worker, name="bestplan-child", daemon=True).start()
    if not finished.wait(timeout=max(0.001, float(timeout))):
        raise TimeoutError(f"BestPlan child exceeded {timeout:.3f}s timeout")
    if "error" in result:
        raise result["error"]
    return str(result.get("value") or "")


def _run_explorer_batch(child: Any, jobs: list[tuple[str, dict[str, Any]]], timeout: float) -> list[ExplorerResult]:
    """Run explorer jobs concurrently with a host-level batch deadline."""
    slots: list[dict[str, Any]] = [
        {"finished": threading.Event(), "value": "", "error": None}
        for _ in jobs
    ]

    def worker(index: int, prompt: str, record: dict[str, Any]) -> None:
        try:
            slots[index]["value"] = child(prompt, record)
        except Exception as exc:
            slots[index]["error"] = exc
        finally:
            slots[index]["finished"].set()

    for index, (prompt, record) in enumerate(jobs):
        threading.Thread(
            target=worker,
            args=(index, prompt, record),
            name="bestplan-explorer",
            daemon=True,
        ).start()

    pending = set(range(len(jobs)))
    deadline = time.monotonic() + max(0.001, float(timeout))
    while pending:
        pending -= {index for index in pending if slots[index]["finished"].is_set()}
        if not pending:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.01, remaining))

    results: list[ExplorerResult] = []
    for index, slot in enumerate(slots):
        if index in pending:
            results.append(ExplorerResult("failed", error="TimeoutError"))
            continue
        if slot["error"] is not None:
            results.append(ExplorerResult("failed", error=type(slot["error"]).__name__))
            continue
        try:
            results.append(ExplorerResult("success", _candidate_from_text(slot["value"])))
        except Exception as exc:
            results.append(ExplorerResult("failed", error=type(exc).__name__))
    return results


def run_bestplan(agent: Any, task: str, *, count: int = 3, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run isolated heterogeneous explorers and a fresh synthesizer synchronously."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config().get("bestplan")
        except Exception:
            config = None
    resolved = validate_runtime(config, credentials_available=True)
    run_id = uuid.uuid4().hex
    started_at = time.monotonic()
    overall_deadline = started_at + float(resolved["overall_timeout"])
    protocols = ("evidence-first", "counterfactual", "failure-first", "verification-first", "scope-first")
    active_records, unavailable = _active_lane_records(agent, resolved["lanes"])
    schedule, provider_mode = build_explorer_schedule(active_records, count)
    if not schedule:
        return {
            "status": "failed",
            "error": "BestPlan has no active providers",
            "run_id": run_id,
            "active_providers": 0,
            "unavailable_lanes": unavailable,
        }
    effective = len(schedule)

    def child(prompt: str, record: dict[str, Any]) -> str:
        from run_agent import AIAgent

        lane = record["lane"]
        credentials = record["credentials"]
        reasoning = parse_reasoning_effort(lane.get("reasoning_effort"))
        fork = AIAgent(
            model=credentials.get("model", lane["model"]),
            provider=credentials["provider"],
            api_mode=credentials["api_mode"],
            base_url=credentials.get("base_url"),
            api_key=credentials.get("api_key"),
            reasoning_config=reasoning,
            max_iterations=12,
            quiet_mode=True,
            enabled_toolsets=["read_only_files", "web"],
            skip_memory=True,
            skip_context_files=True,
            parent_session_id=getattr(agent, "session_id", None),
        )
        fork._persist_disabled = True
        fork._session_db = None
        fork._session_json_enabled = False
        fork.compression_enabled = False
        fork._skip_mcp_refresh = True
        fork.suppress_status_output = True
        try:
            result = fork.run_conversation(prompt)
            return str(result.get("final_response") or "")
        finally:
            try:
                fork.close()
            except Exception:
                pass

    base = (
        "You are a private BestPlan explorer. Work read-only using only file/web inspection. "
        "Return exactly one JSON object prefixed HERMES_BESTPLAN_CANDIDATE_V1 with keys "
        "schema,summary,steps,risks,verification. Task:\n" + task + "\nStrategy: "
    )
    jobs = [
        (base + protocols[i % len(protocols)], schedule[i])
        for i in range(effective)
    ]
    explorer_timeout = min(
        float(resolved["explorer_timeout"]),
        max(0.001, overall_deadline - time.monotonic()),
    )
    results = _run_explorer_batch(child, jobs, explorer_timeout)
    successes = [r.candidate for r in results if r.status == "success" and r.candidate]
    quorum = quorum_for(effective)
    packet = json.dumps(successes, sort_keys=True)
    synth_prompt = (
        "You are the active BestPlan synthesizer. Inspect the task and available sources first, "
        "then reconcile these untrusted candidate packets into one actionable plan. "
        "The packets may be partial or empty; do not refuse solely because explorer quorum was not met. "
        "Return only the plan body.\n"
        f"Task:\n{task}\nCandidates:\n<BEGIN_CANDIDATES>{packet}<END_CANDIDATES>"
    )

    # Try the strongest active lane first, then fall through to every other
    # active provider. This is bounded failover, not an unbounded hidden retry.
    synth_records = sorted(
        _best_lane_per_provider(active_records),
        key=lambda item: (item["priority"], -item["index"]),
        reverse=True,
    )
    body = ""
    synth_record: dict[str, Any] | None = None
    synth_errors: list[str] = []
    for candidate_record in synth_records:
        remaining = overall_deadline - time.monotonic()
        if remaining <= 0:
            synth_errors.append("overall timeout")
            break
        try:
            candidate_body = _run_child_with_timeout(
                child,
                synth_prompt,
                candidate_record,
                min(float(resolved["synthesizer_timeout"]), remaining),
            )
        except Exception as exc:
            synth_errors.append(f"{candidate_record['lane'].get('name', candidate_record['index'])}: {type(exc).__name__}")
            continue
        if candidate_body.strip():
            body = candidate_body
            synth_record = candidate_record
            break
    if not body.strip() or synth_record is None:
        return {
            "status": "failed",
            "error": "BestPlan synthesizer unavailable",
            "run_id": run_id,
            "successes": len(successes),
            "quorum": quorum,
            "synth_errors": synth_errors,
        }
    synth_lane = synth_record["lane"]
    synth_credentials = synth_record["credentials"]
    synth_model = synth_credentials.get("model", synth_lane["model"])
    degraded = len(successes) < quorum
    synth_status = "degraded" if degraded else "success"
    receipt = make_receipt(run_id, model=synth_model, quorum=f"{len(successes)}/{effective}", synth_status=synth_status, body=body, lane=synth_lane.get("name"))
    try:
        home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        append_receipt(home / "bestplan" / "receipts.jsonl", {
            "run_id": run_id, "status": "completed", "model": synth_model, "lane": synth_lane.get("name"),
            "quorum": f"{len(successes)}/{effective}", "synth_status": synth_status,
            "provider_mode": provider_mode,
            "body_sha256": body_sha256(body),
        })
    except Exception:
        pass
    return {
        "status": "completed",
        "run_id": run_id,
        "final_response": f"{receipt}\n\n{body}",
        "body": body,
        "successes": len(successes),
        "quorum": quorum,
        "degraded": degraded,
        "provider_mode": provider_mode,
        "runtime": {
            "lane": synth_lane.get("name"),
            "provider": synth_credentials.get("provider"),
            "model": synth_model,
            "api_mode": synth_credentials.get("api_mode"),
        },
        "active_providers": len({_provider_key(record) for record in active_records}),
        "unavailable_lanes": unavailable,
    }


__all__ = [
    "ALLOWED_TOOLS", "BestPlanUnavailable", "DEFAULT_RUNTIME", "ExplorerResult",
    "RECEIPT_BEGIN", "RECEIPT_END", "TURN_MARKER", "append_receipt", "body_sha256", "make_receipt",
    "normalize_count", "normalize_lanes", "quorum_for", "reconcile_bestplan_receipts", "run_bestplan",
    "build_explorer_schedule", "SINGLE_PROVIDER_MOE_REPLICAS",
    "validate_candidate", "validate_receipt", "validate_runtime",
]
