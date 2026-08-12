"""Durable one-shot remote confirmation at the local BestPlan edge."""

from __future__ import annotations

import json
import math
import os
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.parse import urlsplit

from agent.bestplan_contract import source_snapshot_to_dict
from agent.bestplan_local import LOCAL_GO_CONTRACT_SCHEMA

if TYPE_CHECKING:
    from agent.bestplan_state import BestplanStore, ResolvedGo


LOCAL_PUSH_SCHEMA = "hermes.bestplan.local-push.v1"
LOCAL_PUSH_REF = "refs/heads/main"
LOCAL_PUSH_STATES = frozenset({
    "prepared", "awaiting", "pushing", "effect_unknown", "pushed",
    "declined", "expired", "not_landed", "stale",
})
LOCAL_PUSH_ACTIVE_STATES = frozenset({
    "prepared", "awaiting", "pushing", "effect_unknown",
})
LOCAL_PUSH_MAX_TTL_SECONDS = 24 * 60 * 60
LOCAL_PUSH_GIT_SECONDS = 60.0
LOCAL_PUSH_PROMPT_RECOVERY_SECONDS = 10.0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RECORD_KEYS = {
    "schema", "version", "plan_id", "session_id", "profile", "workspace",
    "repository", "source_snapshot_digest", "expected_target_oid",
    "integration_oid", "check_set_digest", "local_ref", "remote_name",
    "display_url", "remote_identity_sha256", "remote_ref",
    "observed_remote_oid", "expires_at",
}


class LocalPushStateError(ValueError):
    """A stored push prompt is malformed or differs from its local plan."""


def canonical_local_push_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=True, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise LocalPushStateError("local push record is not canonical JSON") from exc


def _text(value: Any, label: str, maximum: int = 4096, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not empty and not value)
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise LocalPushStateError(f"local push {label} is malformed")
    return value


def _oid(snapshot: Any, value: Any, label: str) -> str:
    width = 64 if snapshot.repo.object_format == "sha256" else 40
    if not isinstance(value, str) or re.fullmatch(
        rf"[0-9a-f]{{{width}}}", value,
    ) is None:
        raise LocalPushStateError(f"local push {label} is malformed")
    return value


def _display_url(value: Any) -> str:
    display = _text(value, "display URL")
    if "@" in display:
        raise LocalPushStateError("local push display URL contains user information")
    if "://" in display:
        try:
            parsed = urlsplit(display)
        except ValueError as exc:
            raise LocalPushStateError("local push display URL is malformed") from exc
        if (
            not parsed.scheme or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
        ):
            raise LocalPushStateError("local push display URL is not credential-free")
    if any(
        marker in display.casefold()
        for marker in ("password=", "token=", "api_key=", "apikey=", "secret=")
    ):
        raise LocalPushStateError("local push display URL contains credentials")
    return display


def validate_local_push_record(
    value: Any,
    *,
    row: Mapping[str, Any],
    snapshot: Any,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RECORD_KEYS:
        raise LocalPushStateError("local push record fields differ")
    if value["schema"] != LOCAL_PUSH_SCHEMA or value["version"] != 1 or isinstance(
        value["version"], bool,
    ):
        raise LocalPushStateError("local push record version differs")
    context = {
        "plan_id": _text(value["plan_id"], "plan identity", 256),
        "session_id": _text(value["session_id"], "session identity", 1024),
        "profile": _text(value["profile"], "profile", 1024, empty=True),
        "workspace": _text(value["workspace"], "workspace"),
    }
    if any(context[key] != row.get(key) for key in context):
        raise LocalPushStateError("local push context differs from its plan")
    repository = source_snapshot_to_dict(snapshot)["repository"]
    if value["repository"] != repository:
        raise LocalPushStateError("local push repository identity differs")
    snapshot_digest = value["source_snapshot_digest"]
    if (
        snapshot_digest != row.get("source_snapshot_digest")
        or not isinstance(snapshot_digest, str)
        or _SHA256_RE.fullmatch(snapshot_digest) is None
    ):
        raise LocalPushStateError("local push source snapshot differs")
    expected_oid = _oid(snapshot, value["expected_target_oid"], "target object")
    integration_oid = _oid(snapshot, value["integration_oid"], "integration object")
    observed_oid = _oid(snapshot, value["observed_remote_oid"], "remote object")
    if expected_oid != snapshot.head_oid:
        raise LocalPushStateError("local push Git objects differ from its plan")
    check_digest = value["check_set_digest"]
    remote_digest = value["remote_identity_sha256"]
    if any(
        not isinstance(item, str) or _SHA256_RE.fullmatch(item) is None
        for item in (check_digest, remote_digest)
    ):
        raise LocalPushStateError("local push digest is malformed")
    if value["local_ref"] != LOCAL_PUSH_REF or value["remote_ref"] != LOCAL_PUSH_REF:
        raise LocalPushStateError("local push ref is not refs/heads/main")
    remote_name = _text(value["remote_name"], "remote name", 128)
    if _REMOTE_NAME_RE.fullmatch(remote_name) is None:
        raise LocalPushStateError("local push remote name is malformed")
    expires_at = value["expires_at"]
    if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at < 1:
        raise LocalPushStateError("local push expiry is malformed")
    record = {
        "schema": LOCAL_PUSH_SCHEMA,
        "version": 1,
        **context,
        "repository": repository,
        "source_snapshot_digest": snapshot_digest,
        "expected_target_oid": expected_oid,
        "integration_oid": integration_oid,
        "check_set_digest": check_digest,
        "local_ref": LOCAL_PUSH_REF,
        "remote_name": remote_name,
        "display_url": _display_url(value["display_url"]),
        "remote_identity_sha256": remote_digest,
        "remote_ref": LOCAL_PUSH_REF,
        "observed_remote_oid": observed_oid,
        "expires_at": expires_at,
    }
    if canonical_local_push_json(record) != canonical_local_push_json(value):
        raise LocalPushStateError("local push record is not canonical")
    return record


def decode_local_push_row(
    row: Mapping[str, Any],
    validate_plan: Callable[[Mapping[str, Any]], Any],
) -> tuple[dict[str, Any], Any]:
    values = dict(row)
    raw = values.get("local_push_json")
    if values.get("local_push_state") not in LOCAL_PUSH_STATES or not isinstance(raw, str):
        raise LocalPushStateError("local push record is incomplete")
    try:
        plan = validate_plan(values)
    except Exception as exc:
        raise LocalPushStateError("local push plan binding is invalid") from exc
    contract = plan.contract
    if (
        plan.execution_protocol != 2 or not isinstance(contract, Mapping)
        or contract.get("schema") != LOCAL_GO_CONTRACT_SCHEMA
        or contract.get("version") != 1 or contract.get("mode") != "local_main"
        or plan.source_snapshot is None
    ):
        raise LocalPushStateError("local push record is not attached to local go")
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise LocalPushStateError("local push record JSON is invalid") from exc
    record = validate_local_push_record(
        decoded, row=values, snapshot=plan.source_snapshot,
    )
    if canonical_local_push_json(record) != raw:
        raise LocalPushStateError("local push record JSON is not canonical")
    return record, plan


def recover_local_push_prompt(
    *,
    session_id: str,
    profile: str,
    workspace: str,
    store: BestplanStore | None = None,
    now: float | None = None,
    deadline: float | None = None,
    classify_local_main: Callable[..., str] | None = None,
) -> str | None:
    """Recover and read one exact prompt without consuming its push state."""

    from agent.bestplan_state import (
        BestplanStore,
        PlanState,
        _canonical_workspace,
        _validate_stored_plan_row,
    )

    try:
        observed_now = time.time() if now is None else float(now)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(observed_now):
        return None
    owns_store = store is None
    store = store or BestplanStore(reconcile_push_state=False)
    try:
        try:
            expected_workspace = _canonical_workspace(workspace)
        except (OSError, RuntimeError, ValueError):
            return None
        matching = [
            row
            for row in store.list_active_local_pushes(session_id)
            if row.get("profile") == profile
            and row.get("workspace") == expected_workspace
        ]
        if len(matching) != 1:
            return None
        row = matching[0]
        state = str(row.get("local_push_state") or "")
        plan_state = str(row.get("state") or "")
        if state == "awaiting":
            if plan_state != PlanState.COMPLETED_LOCAL:
                return None
        elif state == "prepared":
            if plan_state not in {PlanState.RUNNING, PlanState.WAITING}:
                return None
            if _owner_is_live(row):
                return None
        else:
            return None
        try:
            record, plan = decode_local_push_row(
                row,
                _validate_stored_plan_row,
            )
        except Exception:
            return None
        expired = observed_now >= record["expires_at"]
        if state == "awaiting" and expired:
            return None
        if state == "prepared":
            monotonic_now = time.monotonic()
            maximum_deadline = (
                monotonic_now + LOCAL_PUSH_PROMPT_RECOVERY_SECONDS
            )
            if deadline is None:
                absolute_deadline = maximum_deadline
            else:
                try:
                    absolute_deadline = float(deadline)
                except (TypeError, ValueError, OverflowError):
                    return None
                if not math.isfinite(absolute_deadline):
                    return None
                absolute_deadline = min(absolute_deadline, maximum_deadline)
            if absolute_deadline <= monotonic_now:
                return None
            if classify_local_main is None:
                try:
                    from agent.bestplan_local_git import classify_local_main_for_push
                except ImportError:
                    return None
                classify_local_main = classify_local_main_for_push
            try:
                local_state = classify_local_main(
                    snapshot=plan.source_snapshot,
                    expected_target_oid=record["expected_target_oid"],
                    integration_oid=record["integration_oid"],
                    deadline=absolute_deadline,
                )
            except Exception:
                return None
            if local_state != "integration":
                return None
            raw = row.get("local_push_json")
            if not isinstance(raw, str):
                return None
            store._set_local_push_state(
                str(row.get("plan_id") or ""),
                expected_state="prepared",
                new_state="awaiting",
                expected_json=raw,
            )
            row = store.get_plan(str(row.get("plan_id") or "")) or {}
            if (
                row.get("local_push_state") != "awaiting"
                or row.get("state") != PlanState.COMPLETED_LOCAL
            ):
                return None
            try:
                record, _plan = decode_local_push_row(
                    row,
                    _validate_stored_plan_row,
                )
            except Exception:
                return None
            if expired:
                store._set_local_push_state(
                    str(row.get("plan_id") or ""),
                    expected_state="awaiting",
                    new_state="expired",
                    expected_json=raw,
                )
                return None
        return (
            f"Local `main` is now `{record['integration_oid']}` and approved "
            f"checks passed. Push this exact commit to `{record['display_url']}` "
            f"`{record['remote_ref']}`? Reply `push` or `no`."
        )
    finally:
        if owns_store:
            store.close()


def build_local_push_record(
    *,
    row: Mapping[str, Any],
    plan: Any,
    plan_id: str,
    session_id: str,
    profile: str,
    workspace: str,
    expected_target_oid: str,
    integration_oid: str,
    check_set_digest: str,
    target: Any,
    expires_at: int,
) -> dict[str, Any]:
    snapshot = plan.source_snapshot
    value = {
        "schema": LOCAL_PUSH_SCHEMA,
        "version": 1,
        "plan_id": plan_id,
        "session_id": session_id,
        "profile": profile,
        "workspace": workspace,
        "repository": source_snapshot_to_dict(snapshot)["repository"],
        "source_snapshot_digest": row.get("source_snapshot_digest"),
        "expected_target_oid": expected_target_oid,
        "integration_oid": integration_oid,
        "check_set_digest": check_set_digest,
        "local_ref": LOCAL_PUSH_REF,
        "remote_name": target.remote_name,
        "display_url": target.display_url,
        "remote_identity_sha256": target.remote_identity_sha256,
        "remote_ref": target.remote_ref,
        "observed_remote_oid": target.observed_remote_oid,
        "expires_at": expires_at,
    }
    return validate_local_push_record(value, row=row, snapshot=snapshot)


def _owner_is_live(row: Mapping[str, Any]) -> bool:
    owner = str(row.get("dispatch_owner") or "")
    if not owner.startswith("pid:"):
        return False
    try:
        pid = int(owner.split(":", 1)[1])
        if pid < 1:
            return False
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError):
        return True
    return True


def reconcile_local_pushes(
    store: BestplanStore,
    *,
    classify_local_main: Callable[..., str] | None = None,
    classify_remote: Callable[..., str] | None = None,
    now: float | None = None,
    plan_id: str | None = None,
) -> int:
    from agent.bestplan_state import _validate_stored_plan_row

    observed_now = time.time() if now is None else float(now)
    if not math.isfinite(observed_now):
        return 0
    where = "local_push_state IN ('prepared','awaiting','pushing','effect_unknown')"
    params: tuple[str, ...] = ()
    if plan_id is not None:
        where += " AND plan_id=?"
        params = (str(plan_id),)
    with store._read_lock():
        rows = store._connection().execute(
            f"SELECT * FROM bestplan_plans WHERE {where} ORDER BY created_at ASC",
            params,
        ).fetchall()
    if not rows:
        return 0
    if classify_local_main is None:
        try:
            from agent.bestplan_local_git import classify_local_main_for_push
        except ImportError:
            classify_local_main = None
        else:
            classify_local_main = classify_local_main_for_push
    if classify_remote is None:
        try:
            from agent.bestplan_local_git import classify_local_push_remote
        except ImportError:
            classify_remote = None
        else:
            classify_remote = classify_local_push_remote
    changed = 0
    for raw_row in rows:
        row = dict(raw_row)
        state = str(row.get("local_push_state") or "")
        raw = row.get("local_push_json")
        plan_id = str(row.get("plan_id") or "")
        try:
            record, plan = decode_local_push_row(row, _validate_stored_plan_row)
        except Exception:
            if state == "prepared":
                continue
            next_state = "stale"
        else:
            if state in {"prepared", "pushing"} and _owner_is_live(row):
                continue
            expired = observed_now >= record["expires_at"]
            if state in {"awaiting", "effect_unknown"}:
                next_state = "expired" if expired else state
            elif classify_local_main is None:
                if state == "prepared":
                    continue
                next_state = "effect_unknown"
            else:
                try:
                    local = classify_local_main(
                        snapshot=plan.source_snapshot,
                        expected_target_oid=record["expected_target_oid"],
                        integration_oid=record["integration_oid"],
                        deadline=time.monotonic() + 10.0,
                    )
                except Exception:
                    local = "unavailable"
                if state == "prepared":
                    if local == "integration":
                        if store._set_local_push_state(
                            plan_id,
                            expected_state="prepared",
                            new_state="awaiting",
                            expected_json=(raw if isinstance(raw, str) else None),
                        ):
                            changed += 1
                            if expired and store._set_local_push_state(
                                plan_id,
                                expected_state="awaiting",
                                new_state="expired",
                                expected_json=(
                                    raw if isinstance(raw, str) else None
                                ),
                            ):
                                changed += 1
                        continue
                    # A dead Hermes process does not prove that its detached
                    # local Git child is dead.  Keep every unresolved prepared
                    # effect active so a late fast-forward cannot escape the
                    # push decision state machine.
                    continue
                elif local != "integration":
                    next_state = "stale"
                elif classify_remote is None:
                    next_state = "effect_unknown"
                else:
                    from agent.bestplan_local_git import LocalMainPushTarget

                    target = LocalMainPushTarget(
                        remote_name=record["remote_name"],
                        remote_ref=record["remote_ref"],
                        display_url=record["display_url"],
                        remote_identity_sha256=record["remote_identity_sha256"],
                        observed_remote_oid=record["observed_remote_oid"],
                        integration_oid=record["integration_oid"],
                    )
                    try:
                        remote = classify_remote(
                            snapshot=plan.source_snapshot, target=target,
                            deadline=time.monotonic() + 20.0,
                        )
                    except Exception:
                        remote = "unavailable"
                    next_state = (
                        "pushed" if remote == "integration"
                        else "stale" if remote == "other"
                        else "expired" if expired else "effect_unknown"
                    )
        if next_state != state and store._set_local_push_state(
            plan_id, expected_state=state, new_state=next_state,
            expected_json=raw if isinstance(raw, str) else None,
        ):
            changed += 1
    return changed


def try_resolve_local_push(
    message: str,
    *,
    session_id: str,
    profile: str,
    workspace: str,
    store: BestplanStore | None = None,
    push_fn: Callable[..., Any] | None = None,
    now: float | None = None,
) -> ResolvedGo:
    """Consume exact CLI-only ``push`` or ``no`` before any model call."""

    from agent.bestplan_state import (
        BestplanStore,
        ResolvedGo,
        _canonical_workspace,
        _validate_stored_plan_row,
    )

    if not isinstance(message, str) or message.strip().casefold() not in {"push", "no"}:
        return ResolvedGo(False, "not_a_push_reply")
    token = message.strip().casefold()
    owns_store = store is None
    store = store or BestplanStore(reconcile_push_state=False)
    try:
        rows = store.list_active_local_pushes(session_id)
        if not rows:
            return ResolvedGo(False, "no_push_prompt")
        expected_workspace = _canonical_workspace(workspace)
        matching = [
            row for row in rows
            if row.get("profile") == profile and row.get("workspace") == expected_workspace
        ]
        if len(matching) != 1:
            status = "push_context_mismatch" if not matching else "push_ambiguous"
            return ResolvedGo(True, status, reason="push prompt context is not unique")
        row = matching[0]
        plan_id = str(row.get("plan_id") or "")
        state = str(row.get("local_push_state") or "")
        raw = row.get("local_push_json")
        try:
            record, _plan = decode_local_push_row(row, _validate_stored_plan_row)
        except Exception:
            store._set_local_push_state(
                plan_id, expected_state=state, new_state="stale",
                expected_json=raw if isinstance(raw, str) else None,
            )
            return ResolvedGo(True, "push_stale", plan_id=plan_id)
        observed_now = time.time() if now is None else float(now)
        if owns_store and token == "push" and state in {"prepared", "pushing"}:
            reconcile_local_pushes(store, now=observed_now, plan_id=plan_id)
            row = store.get_plan(plan_id) or {}
            state = str(row.get("local_push_state") or "")
            terminal_status = {
                "pushed": "push_complete",
                "expired": "push_expired",
                "not_landed": "push_stale",
                "stale": "push_stale",
            }.get(state)
            if terminal_status is not None:
                return ResolvedGo(True, terminal_status, plan_id=plan_id)
            raw = row.get("local_push_json")
            try:
                record, _plan = decode_local_push_row(row, _validate_stored_plan_row)
            except Exception:
                return ResolvedGo(True, "push_stale", plan_id=plan_id)
        if not math.isfinite(observed_now) or observed_now >= record["expires_at"]:
            store._set_local_push_state(
                plan_id, expected_state=state, new_state="expired", expected_json=raw,
            )
            return ResolvedGo(True, "push_expired", plan_id=plan_id)
        if state in {"prepared", "pushing"}:
            return ResolvedGo(True, "push_in_flight", plan_id=plan_id)
        if state == "effect_unknown" and token == "no":
            return ResolvedGo(True, "push_effect_unknown", plan_id=plan_id)
        if token == "no":
            consumed = store._set_local_push_state(
                plan_id, expected_state="awaiting", new_state="declined",
                expected_json=raw,
            )
            return ResolvedGo(
                True, "push_declined" if consumed else "push_in_flight",
                plan_id=plan_id,
            )
        if store.claim_local_push(plan_id, now=observed_now) is None:
            refreshed = store.get_plan(plan_id) or {}
            status = {
                "expired": "push_expired", "stale": "push_stale",
            }.get(refreshed.get("local_push_state"), "push_in_flight")
            return ResolvedGo(True, status, plan_id=plan_id)
        refreshed = store.get_plan(plan_id)
        try:
            claimed, plan = decode_local_push_row(refreshed or {}, _validate_stored_plan_row)
        except Exception:
            store._set_local_push_state(
                plan_id, expected_state="pushing", new_state="stale",
            )
            return ResolvedGo(True, "push_stale", plan_id=plan_id)
        from agent.bestplan_local_git import (
            LocalMainPushReceipt,
            LocalMainPushTarget,
            LocalPushConflict,
            LocalPushEffectUnknown,
            LocalPushStale,
        )

        target = LocalMainPushTarget(
            remote_name=claimed["remote_name"], remote_ref=claimed["remote_ref"],
            display_url=claimed["display_url"],
            remote_identity_sha256=claimed["remote_identity_sha256"],
            observed_remote_oid=claimed["observed_remote_oid"],
            integration_oid=claimed["integration_oid"],
        )
        if push_fn is None:
            from agent.bestplan_local_git import push_exact_local_main

            push_fn = push_exact_local_main
        try:
            receipt = push_fn(
                snapshot=plan.source_snapshot, target=target,
                deadline=time.monotonic() + LOCAL_PUSH_GIT_SECONDS,
            )
        except LocalPushEffectUnknown:
            next_state, status = "effect_unknown", "push_effect_unknown"
        except (LocalPushStale, LocalPushConflict):
            next_state, status = "stale", "push_stale"
        except BaseException:
            next_state, status = "effect_unknown", "push_effect_unknown"
        else:
            exact = (
                isinstance(receipt, LocalMainPushReceipt)
                and receipt.remote_name == target.remote_name
                and receipt.remote_ref == target.remote_ref
                and receipt.integration_oid == target.integration_oid
                and receipt.remote_oid == target.integration_oid
            )
            next_state, status = (
                ("pushed", "push_complete") if exact
                else ("effect_unknown", "push_effect_unknown")
            )
        changed = store._set_local_push_state(
            plan_id, expected_state="pushing", new_state=next_state,
            expected_json=refreshed.get("local_push_json"),
        )
        if not changed and status == "push_complete":
            status = "push_effect_unknown"
        return ResolvedGo(True, status, plan_id=plan_id)
    finally:
        if owns_store:
            store.close()
