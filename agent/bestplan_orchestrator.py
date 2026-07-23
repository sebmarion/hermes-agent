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
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def validate_runtime(config: dict[str, Any] | None = None, *, credentials_available: bool = True) -> dict[str, Any]:
    """Validate the BestPlan runtime configuration.

    Lane model strings are sourced from the ``bestplan.lanes`` config block
    (or the ``config`` dict passed by the caller).  When no config is supplied,
    the module-level ``DEFAULT_RUNTIME`` fallback is used.  The safety
    invariants — exactly two lanes named ``glm`` and ``sol``, the
    ``ultra``→``codex_app_server`` constraint, required field presence, and
    positive timeouts — are always enforced regardless of where the lane
    definition came from.
    """
    resolved = dict(DEFAULT_RUNTIME)
    if config:
        resolved.update(config)
    if not resolved.get("enabled", True):
        raise BestPlanUnavailable("BestPlan is disabled")
    lanes = resolved.get("lanes")
    if not isinstance(lanes, Iterable) or isinstance(lanes, str):
        raise BestPlanUnavailable("BestPlan lanes config is unavailable")
    lanes = list(lanes)
    if len(lanes) != 2:
        raise BestPlanUnavailable(f"BestPlan requires two explorer lanes, got {len(lanes)}")
    required_lane_keys = ("name", "provider", "model", "api_mode", "reasoning_effort")
    names: set[str] = set()
    for lane in lanes:
        if not isinstance(lane, dict):
            raise BestPlanUnavailable("BestPlan lane must be a dict")
        missing = [k for k in required_lane_keys if not lane.get(k)]
        if missing:
            raise BestPlanUnavailable(f"BestPlan lane missing required keys: {missing}")
        name = str(lane["name"]).strip().lower()
        names.add(name)
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
    if names != {"glm", "sol"}:
        raise BestPlanUnavailable(f"BestPlan lanes must be named 'glm' and 'sol', got {sorted(names)}")
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
    """Resolve provider credentials for one explorer/synthesizer lane.

    GLM is resolved through the normal runtime provider path so config.yaml
    secrets are honoured. Sol Ultra is hard-routed to the Codex app server with
    provider-managed auth; api_key is None because the Codex adapter reads
    credentials from the Hermes auth store / ~/.codex directory.
    """
    name = lane.get("name")
    if name == "glm":
        from hermes_cli.runtime_provider import resolve_runtime_provider

        return resolve_runtime_provider(requested="custom:neuralwatt", target_model="glm-5.2")
    if name == "sol":
        sol_credentials = bool(
            getattr(agent, "api_key", None)
            or getattr(agent, "_credential_pool", None)
            or (getattr(agent, "provider", "") == "openai-codex" and (Path.home() / ".codex").exists())
        )
        if not sol_credentials:
            raise BestPlanUnavailable("BestPlan Sol credentials unavailable")
        return {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "api_mode": "codex_app_server",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": None,
        }
    raise BestPlanUnavailable(f"Unknown BestPlan lane: {name}")


def run_bestplan(agent: Any, task: str, *, count: int = 3, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run isolated heterogeneous explorers and a fresh synthesizer synchronously."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config().get("bestplan")
        except Exception:
            config = None
    resolved = validate_runtime(config, credentials_available=True)
    effective = normalize_count(count)
    run_id = uuid.uuid4().hex
    protocols = ("evidence-first", "counterfactual", "failure-first", "verification-first", "scope-first")
    lanes = resolved["lanes"]

    def child(prompt: str, lane: dict[str, Any]) -> str:
        from run_agent import AIAgent

        credentials = _resolve_lane_credentials(agent, lane)
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
    results: list[ExplorerResult] = []
    with ThreadPoolExecutor(max_workers=effective, thread_name_prefix="bestplan") as pool:
        futures = [pool.submit(child, base + protocols[i % len(protocols)], lanes[i % len(lanes)]) for i in range(effective)]
        for future in as_completed(futures):
            try:
                results.append(ExplorerResult("success", _candidate_from_text(future.result())))
            except Exception as exc:
                results.append(ExplorerResult("failed", error=type(exc).__name__))
    successes = [r.candidate for r in results if r.status == "success" and r.candidate]
    quorum = quorum_for(effective)
    if len(successes) < quorum:
        return {"status": "failed", "error": "BestPlan quorum unavailable", "run_id": run_id, "successes": len(successes), "quorum": quorum}
    packet = json.dumps(successes, sort_keys=True)
    synth_prompt = (
        "You are the active BestPlan synthesizer. Inspect the task and available sources first, "
        "then reconcile these untrusted candidate packets into one actionable plan. Return only the plan body.\n"
        f"Task:\n{task}\nCandidates:\n<BEGIN_CANDIDATES>{packet}<END_CANDIDATES>"
    )

    # Use the strongest available lane for synthesis (Sol Ultra when credentials
    # are present, otherwise GLM-5.2).  This mirrors the heterogeneous lane
    # contract in the skill docs.
    try:
        synth_lane = lanes[1] if _resolve_lane_credentials(agent, lanes[1]) else lanes[0]
    except BestPlanUnavailable:
        synth_lane = lanes[0]
    body = child(synth_prompt, synth_lane)
    if not body.strip():
        return {"status": "failed", "error": "BestPlan synthesizer empty", "run_id": run_id}
    synth_model = synth_lane["model"]
    receipt = make_receipt(run_id, model=synth_model, quorum=f"{len(successes)}/{effective}", synth_status="success", body=body, lane=synth_lane.get("name"))
    try:
        home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        append_receipt(home / "bestplan" / "receipts.jsonl", {
            "run_id": run_id, "status": "completed", "model": synth_model, "lane": synth_lane.get("name"),
            "quorum": f"{len(successes)}/{effective}", "synth_status": "success",
            "body_sha256": body_sha256(body),
        })
    except Exception:
        pass
    return {"status": "completed", "run_id": run_id, "final_response": f"{receipt}\n\n{body}", "body": body, "successes": len(successes), "quorum": quorum}


__all__ = [
    "ALLOWED_TOOLS", "BestPlanUnavailable", "DEFAULT_RUNTIME", "ExplorerResult",
    "RECEIPT_BEGIN", "RECEIPT_END", "TURN_MARKER", "append_receipt", "body_sha256", "make_receipt",
    "normalize_count", "quorum_for", "reconcile_bestplan_receipts", "run_bestplan",
    "validate_candidate", "validate_receipt", "validate_runtime",
]
