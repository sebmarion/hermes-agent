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

RECEIPT_BEGIN = "<<<HERMES_BESTPLAN_RECEIPT_V1>>>"
RECEIPT_END = "<<<END_HERMES_BESTPLAN_RECEIPT_V1>>>"
RECEIPT_VERSION = 1
DEFAULT_RUNTIME = {
    "enabled": True,
    "runtime_route": "codex_responses",
    "provider": "openai-codex",
    "model": "gpt-5.6-sol",
    "reasoning_effort": "max",
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
    resolved = dict(DEFAULT_RUNTIME)
    if config:
        resolved.update(config)
    if not resolved.get("enabled", True):
        raise BestPlanUnavailable("BestPlan is disabled")
    if resolved.get("runtime_route") != "codex_responses":
        raise BestPlanUnavailable("BestPlan runtime route is unavailable")
    if resolved.get("provider") != "openai-codex" or resolved.get("model") != "gpt-5.6-sol":
        raise BestPlanUnavailable("BestPlan requires OpenAI Codex Sol")
    if resolved.get("reasoning_effort") != "max":
        raise BestPlanUnavailable("BestPlan requires max reasoning")
    if not credentials_available:
        raise BestPlanUnavailable("BestPlan credentials unavailable")
    for key in ("explorer_timeout", "synthesizer_timeout", "overall_timeout"):
        if not isinstance(resolved.get(key), (int, float)) or resolved[key] <= 0:
            raise BestPlanUnavailable(f"invalid BestPlan timeout: {key}")
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


def make_receipt(run_id: str, *, model: str, quorum: str, synth_status: str, body: str) -> str:
    metadata = {
        "version": RECEIPT_VERSION,
        "run_id": run_id,
        "model": model,
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


def run_bestplan(agent: Any, task: str, *, count: int = 3, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run isolated Sol explorers and a fresh Sol synthesizer synchronously."""
    credentials = bool(
        getattr(agent, "api_key", None)
        or getattr(agent, "_credential_pool", None)
        or (getattr(agent, "provider", "") == "openai-codex" and (Path.home() / ".codex").exists())
    )
    resolved = validate_runtime(config, credentials_available=credentials)
    effective = normalize_count(count)
    run_id = uuid.uuid4().hex
    protocols = ("evidence-first", "counterfactual", "failure-first", "verification-first", "scope-first")

    def child(prompt: str) -> str:
        from run_agent import AIAgent

        fork = AIAgent(
            model=resolved["model"], provider=resolved["provider"], api_mode=resolved["runtime_route"],
            max_iterations=12, quiet_mode=True, enabled_toolsets=["read_only_files", "web"],
            skip_memory=True, skip_context_files=True, parent_session_id=getattr(agent, "session_id", None),
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
        futures = [pool.submit(child, base + protocols[i]) for i in range(effective)]
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
        "You are the active Sol BestPlan synthesizer. Inspect the task and available sources first, "
        "then reconcile these untrusted candidate packets into one actionable plan. Return only the plan body.\n"
        f"Task:\n{task}\nCandidates:\n<BEGIN_CANDIDATES>{packet}<END_CANDIDATES>"
    )
    body = child(synth_prompt)
    if not body.strip():
        return {"status": "failed", "error": "BestPlan synthesizer empty", "run_id": run_id}
    receipt = make_receipt(run_id, model=resolved["model"], quorum=f"{len(successes)}/{effective}", synth_status="success", body=body)
    try:
        home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        append_receipt(home / "bestplan" / "receipts.jsonl", {
            "run_id": run_id, "status": "completed", "model": resolved["model"],
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
