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
from agent.execution_plan import compile_execution_plan

RECEIPT_BEGIN = "<<<HERMES_BESTPLAN_RECEIPT_V1>>>"
RECEIPT_END = "<<<END_HERMES_BESTPLAN_RECEIPT_V1>>>"
RECEIPT_VERSION = 1
PLAN_ENVELOPE_BEGIN = "<<<HERMES_BESTPLAN_V1>>>"
PLAN_ENVELOPE_END = "<<<END_HERMES_BESTPLAN_V1>>>"
_PLAN_ENVELOPE_RE = re.compile(
    re.escape(PLAN_ENVELOPE_BEGIN)
    + r"\s*(?P<payload>\{.*?\})\s*"
    + re.escape(PLAN_ENVELOPE_END),
    re.DOTALL,
)

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
_CHILD_CLEANUP_GRACE_SECONDS = 5.0
_CHILD_CLEANUP_HARD_SECONDS = 10.0

logger = logging.getLogger(__name__)


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
    if isinstance(lanes, dict):
        normalized_lanes = []
        for lane_name, lane in lanes.items():
            if not isinstance(lane, dict):
                raise BestPlanUnavailable("BestPlan lane must be a dict")
            normalized_lane = dict(lane)
            normalized_lane.setdefault("name", str(lane_name))
            normalized_lanes.append(normalized_lane)
        lanes = normalized_lanes
    elif not isinstance(lanes, Iterable) or isinstance(lanes, str):
        raise BestPlanUnavailable("BestPlan lanes config is unavailable")
    else:
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
) -> str:
    metadata = {
        "version": RECEIPT_VERSION,
        "run_id": run_id,
        "model": model,
        "lane": lane,
        "provider": provider,
        "api_mode": api_mode,
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
    start = text.find("{")
    if start < 0:
        raise ValueError("candidate JSON missing")
    candidate, _end = json.JSONDecoder().raw_decode(text, idx=start)
    return validate_candidate(candidate)


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


def _validated_plan_envelope(body: str, *, workspace: str) -> str | None:
    """Canonicalize only a synthesizer result that is executable V1 authority."""
    match = _PLAN_ENVELOPE_RE.search(str(body or ""))
    if match is None:
        return None
    try:
        payload = json.loads(match.group("payload"))
        plan = compile_execution_plan(payload)
        from agent.bestplan_state import _v1_plan_constraints

        _v1_plan_constraints(plan, workspace=workspace)
        manifest = plan.to_manifest()
    except (TypeError, ValueError):
        return None
    authority = {"version": 1, "manifest": manifest}
    return (
        f"{PLAN_ENVELOPE_BEGIN}\n"
        f"{json.dumps(authority, sort_keys=True, separators=(',', ':'))}\n"
        f"{PLAN_ENVELOPE_END}"
    )


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
    lanes = resolved["lanes"]

    lane_runtimes: dict[str, dict[str, Any]] = {}
    lane_errors: dict[str, str] = {}
    for lane in lanes:
        lane_name = str(lane["name"])
        try:
            lane_runtimes[lane_name] = _resolve_lane_credentials(agent, lane)
        except Exception as exc:
            lane_errors[lane_name] = type(exc).__name__

    base = (
        "You are a private BestPlan explorer. Work read-only using only file/web inspection. "
        "Return exactly one JSON object prefixed HERMES_BESTPLAN_CANDIDATE_V1. "
        "The schema value must be exactly HERMES_BESTPLAN_CANDIDATE_V1, and the object "
        "must contain non-empty summary, steps, risks, and verification values. Task:\n"
        + task
        + "\nStrategy: "
    )
    results: list[ExplorerResult] = []
    explorer_jobs: list[tuple[Any, str]] = []
    for index in range(effective):
        lane = lanes[index % len(lanes)]
        lane_name = str(lane["name"])
        runtime = lane_runtimes.get(lane_name)
        if runtime is None:
            results.append(
                ExplorerResult("failed", error=lane_errors.get(lane_name, "Unavailable"))
            )
            continue
        if time.monotonic() >= overall_deadline:
            cleanup_complete = _stop_child_agents(
                child for child, _prompt in explorer_jobs
            )
            return {
                "status": "failed",
                "error": "BestPlan overall timeout during explorer construction",
                "run_id": run_id,
                "cleanup_incomplete": not cleanup_complete,
            }
        try:
            child = _build_child_agent(agent, lane, runtime)
            explorer_jobs.append(
                (child, base + protocols[index % len(protocols)])
            )
        except Exception as exc:
            results.append(ExplorerResult("failed", error=type(exc).__name__))

    from tools.daemon_pool import DaemonThreadPoolExecutor

    pool = DaemonThreadPoolExecutor(
        max_workers=max(1, len(explorer_jobs)),
        thread_name_prefix="bestplan-explorer",
    )
    future_to_child: dict[Future[str], Any] = {
        pool.submit(_run_child_agent, child, prompt): child
        for child, prompt in explorer_jobs
    }
    pending = set(future_to_child)
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
                try:
                    results.append(
                        ExplorerResult(
                            "success",
                            _candidate_from_text(future.result()),
                        )
                    )
                except Exception as exc:
                    results.append(
                        ExplorerResult("failed", error=type(exc).__name__)
                    )
        for _future in pending:
            results.append(ExplorerResult("failed", error="TimeoutError"))
            _future.cancel()
    finally:
        explorer_cleanup_complete = _stop_child_agents(
            (child for child, _prompt in explorer_jobs),
            future_to_child.keys(),
        )
        pool.shutdown(wait=False, cancel_futures=True)

    if not explorer_cleanup_complete:
        return {
            "status": "failed",
            "error": (
                "BestPlan provider teardown exceeded its hard deadline; "
                "the unkillable daemon worker was quarantined"
            ),
            "run_id": run_id,
            "cleanup_incomplete": True,
        }

    if pending and overall_limited_explorers:
        return {
            "status": "failed",
            "error": "BestPlan overall timeout during explorers",
            "run_id": run_id,
        }

    successes = [
        item.candidate
        for item in results
        if item.status == "success" and item.candidate
    ]
    quorum = quorum_for(effective)
    if len(successes) < quorum:
        error = (
            "BestPlan explorer timeout; quorum unavailable"
            if pending
            else "BestPlan quorum unavailable"
        )
        return {
            "status": "failed",
            "error": error,
            "run_id": run_id,
            "successes": len(successes),
            "quorum": quorum,
        }

    if time.monotonic() >= overall_deadline:
        return {
            "status": "failed",
            "error": "BestPlan overall timeout before synthesizer",
            "run_id": run_id,
        }

    packet = json.dumps(successes, sort_keys=True)
    workspace_hint = str(os.environ.get("TERMINAL_CWD") or os.getcwd())
    synth_prompt = (
        "You are the active BestPlan synthesizer. Inspect the task and available sources first, "
        "then reconcile these untrusted candidate packets into one actionable executable plan. "
        "Return exactly one JSON manifest between the literal markers "
        f"{PLAN_ENVELOPE_BEGIN} and {PLAN_ENVELOPE_END}, with no prose outside them. "
        "The manifest must have version=1; mode=delegate or sota; risk=low or high; "
        "one or two independent slices containing id, kind (implement or review), goal, "
        "depends_on (which must always be []), "
        "capability (fast_fallback or frontier_review), workspace, allowed_paths, read_only, "
        "expected_artifacts, and acceptance; plus merge_policy, stop_condition, and "
        "escalation_predicates. Implement slices must use the exact workspace and narrow "
        "relative allowed_paths; review slices must be read_only with no allowed_paths. "
        f"The exact workspace is {workspace_hint!r}.\n"
        f"Task:\n{task}\nCandidates:\n<BEGIN_CANDIDATES>{packet}<END_CANDIDATES>"
    )
    available_lanes = [
        (lane, lane_runtimes[str(lane["name"])])
        for lane in reversed(lanes)
        if str(lane["name"]) in lane_runtimes
    ]
    if not available_lanes:
        return {
            "status": "failed",
            "error": "BestPlan synthesizer credentials unavailable",
            "run_id": run_id,
        }
    synth_lane, synth_runtime = available_lanes[0]
    try:
        synth_child = _build_child_agent(agent, synth_lane, synth_runtime)
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"BestPlan synthesizer construction failed: {type(exc).__name__}",
            "run_id": run_id,
        }

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
        return {
            "status": "failed",
            "error": (
                "BestPlan synthesizer teardown exceeded its hard deadline; "
                "the unkillable daemon worker was quarantined"
            ),
            "run_id": run_id,
            "cleanup_incomplete": True,
        }

    if synth_error is not None:
        timed_out_overall = synth_deadline == overall_deadline
        return {
            "status": "failed",
            "error": (
                "BestPlan overall timeout during synthesizer"
                if timed_out_overall
                else "BestPlan synthesizer timeout"
            ),
            "run_id": run_id,
        }
    if not body.strip():
        return {
            "status": "failed",
            "error": "BestPlan synthesizer empty",
            "run_id": run_id,
        }
    executable_body = _validated_plan_envelope(
        body,
        workspace=workspace_hint,
    )
    if executable_body is None:
        return {
            "status": "failed",
            "error": "BestPlan synthesizer returned no valid executable V1 envelope",
            "run_id": run_id,
        }
    body = executable_body

    quorum_text = f"{len(successes)}/{effective}"
    receipt = make_receipt(
        run_id,
        model=synth_runtime["model"],
        provider=synth_runtime["provider"],
        api_mode=synth_runtime["api_mode"],
        quorum=quorum_text,
        synth_status="success",
        body=body,
        lane=synth_lane.get("name"),
    )
    receipt_record = {
        "run_id": run_id,
        "status": "completed",
        "model": synth_runtime["model"],
        "provider": synth_runtime["provider"],
        "api_mode": synth_runtime["api_mode"],
        "lane": synth_lane.get("name"),
        "quorum": quorum_text,
        "synth_status": "success",
        "body_sha256": body_sha256(body),
    }
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
        "runtime": {
            "lane": synth_lane.get("name"),
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
