from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import sqlite3
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "manual-review@example.test")
    _git(path, "config", "user.name", "Manual Review Test")
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


def _session(home: Path, repo: Path, *, session_id: str = "manual-session"):
    from hermes_state import SessionDB

    database = SessionDB(db_path=home / "state.db")
    database.create_session(
        session_id=session_id,
        source="cli",
        cwd=str(repo),
        profile_name="manual-profile",
        git_repo_root=str(repo),
    )
    return database


def _mark_objective(session_id: str, repo: Path, paths: list[str]) -> None:
    from agent.verification_evidence import mark_workspace_edited

    record = mark_workspace_edited(
        session_id=session_id,
        cwd=repo,
        paths=paths,
    )
    assert record is not None
    assert record["changed_paths"] == sorted(paths)


def _packet(request: dict[str, object]) -> dict[str, object]:
    assert request["tools"] == []
    messages = request["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "hermes.bestplan.review-verdict.v1" in messages[0]["content"]
    return json.loads(messages[1]["content"])


def _verdict(
    packet: dict[str, object],
    findings: list[dict[str, object]],
) -> str:
    target = packet["target"]
    integration_oid = target.get("snapshot_tree_oid") or target.get("integration_oid")
    return json.dumps(
        {
            "schema": "hermes.bestplan.review-verdict.v1",
            "target_digest": packet["target_digest"],
            "integration_oid": integration_oid,
            "findings": findings,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _blocking_finding(path: str, quoted_line: str) -> dict[str, object]:
    return {
        "severity": "high",
        "locator": {
            "kind": "changed_lines",
            "path": path,
            "start_line": 2,
            "end_line": 2,
            "quoted_evidence": quoted_line,
        },
        "title": "Unsafe objective value remains",
        "trigger": "The reviewed objective still contains the unsafe value.",
        "observed_failure": "The objective returns the unsafe value.",
        "blast_radius": "The changed objective can ship incorrect behavior.",
        "reproduction": {
            "kind": "not_applicable",
            "reason": "The exact changed line is sufficient evidence.",
        },
    }


class _ManualRuntime:
    """Deterministic credentials and workers for the real manual adapter."""

    adapter_version = "manual_snapshot.v1"

    def __init__(
        self,
        *,
        workspace: Path,
        blocking_path: str | None = None,
        unsafe_line: str = "",
        repaired_text: str = "",
        drift_after_call: int | None = None,
        require_outside_authority: bool = False,
    ) -> None:
        from agent.review_engine import ReviewerBinding

        self.workspace = workspace
        self.reviewer_bindings = (
            ReviewerBinding(
                slot="smart_reviewer",
                provider="anthropic",
                model="claude-opus-5",
                model_family="claude",
            ),
            ReviewerBinding(
                slot="code_worker",
                provider="openai-codex",
                model="gpt-5.6-sol",
                model_family="gpt",
            ),
        )
        self.blocking_path = blocking_path
        self.unsafe_line = unsafe_line
        self.repaired_text = repaired_text
        self.drift_after_call = drift_after_call
        self.require_outside_authority = require_outside_authority
        self.reviewer_requests: list[tuple[str, dict[str, object]]] = []
        self.repair_calls: list[dict[str, object]] = []
        self.check_calls: list[dict[str, object]] = []

    def reviewer_call(self, binding, request: dict[str, object]) -> str:
        packet = _packet(request)
        self.reviewer_requests.append((binding.slot, request))
        diff = base64.b64decode(
            packet["artifact"]["git_diff"]["content_base64"], validate=True
        )
        findings: list[dict[str, object]] = []
        if self.blocking_path and self.unsafe_line.encode("utf-8") in diff:
            findings.append(
                _blocking_finding(self.blocking_path, self.unsafe_line)
            )
        raw = _verdict(packet, findings)
        if self.drift_after_call == len(self.reviewer_requests):
            (self.workspace / self.blocking_path).write_text(
                'def value():\n    return "operator-drift"\n',
                encoding="utf-8",
            )
        return raw

    def repair_generation(
        self,
        *,
        blockers,
        allowed_paths,
        generation: int,
        **_kwargs,
    ) -> dict[str, object]:
        call = {
            "blockers": tuple(blockers),
            "allowed_paths": tuple(allowed_paths),
            "generation": generation,
        }
        self.repair_calls.append(call)
        if self.require_outside_authority:
            return {
                "status": "requires_authority",
                "requested_paths": ["outside/not-owned.py"],
            }
        assert self.blocking_path is not None
        assert self.blocking_path in call["allowed_paths"]
        (self.workspace / self.blocking_path).write_text(
            self.repaired_text,
            encoding="utf-8",
        )
        return {
            "status": "applied",
            "changed_paths": [self.blocking_path],
        }

    def run_checks(
        self,
        *,
        changed_paths,
        generation: int,
        **_kwargs,
    ) -> dict[str, object]:
        call = {
            "changed_paths": tuple(changed_paths),
            "generation": generation,
        }
        self.check_calls.append(call)
        receipt = json.dumps(call, sort_keys=True, default=list).encode("utf-8")
        return {
            "status": "passed",
            "receipt_digest": hashlib.sha256(receipt).hexdigest(),
        }


def _agent(database, *, session_id: str, runtime: _ManualRuntime | None = None):
    values = {
        "session_id": session_id,
        "_session_db": database,
        "platform": "cli",
    }
    if runtime is not None:
        values["manual_review_runtime"] = runtime
    return SimpleNamespace(**values)


def _run(agent, *, scope: str = "", history: list[dict] | None = None) -> dict:
    from agent.conversation_loop import run_conversation

    command = "/review" + (f" {scope}" if scope else "")
    return run_conversation(
        agent,
        command,
        conversation_history=list(history or []),
        review_config={"scope": scope},
    )


def _job_row(database_path: Path) -> sqlite3.Row:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM review_jobs ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 1
    return rows[0]


def _event_kinds(database_path: Path) -> list[str]:
    connection = sqlite3.connect(database_path)
    try:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT kind FROM review_store_events ORDER BY event_seq"
            )
        ]
    finally:
        connection.close()


def _pass_count(database_path: Path) -> int:
    connection = sqlite3.connect(database_path)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM review_pass_receipts"
            ).fetchone()[0]
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "outcome", ["pass", "blocker", "retry", "persistent", "drift"]
)
def test_manual_review_attaches_to_the_exact_active_bestplan_generation(
    tmp_path,
    monkeypatch,
    outcome,
):
    """A named active BestPlan target outranks the standalone-diff fallback."""

    from agent.bestplan_review import bestplan_review_policy_digest
    from agent.review_engine import ReviewArtifact, ReviewStore, ReviewTarget

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    database = _session(home, repo)

    class StaleReviewerBindingRetried(BaseException):
        pass

    class AttachedRuntime(_ManualRuntime):
        def reviewer_call(self, binding, request):
            packet = _packet(request)
            self.reviewer_requests.append((binding.slot, request))
            if outcome == "persistent" or (
                outcome in {"retry", "drift"}
                and len(self.reviewer_requests) == 1
            ):
                from agent.review_engine import ReviewValidationError

                raise ReviewValidationError("one-shot reviewer response is malformed")
            if outcome == "retry":
                if binding is not self.reviewer_bindings[1]:
                    raise StaleReviewerBindingRetried(
                        "the failed slot reused its stale reviewer binding"
                    )
            findings = []
            if outcome == "blocker":
                findings = [
                    {
                        "severity": "high",
                        "locator": {
                            "kind": "changed_lines",
                            "path": "src/objective.py",
                            "start_line": 1,
                            "end_line": 1,
                            "quoted_evidence": "VALUE = 'bestplan-integration'\n",
                        },
                        "title": "BestPlan blocker remains",
                        "trigger": "The exact BestPlan integration is unsafe.",
                        "observed_failure": "The integration still has the bad value.",
                        "blast_radius": "The BestPlan result cannot land.",
                        "reproduction": {
                            "kind": "not_applicable",
                            "reason": "The exact changed line is sufficient evidence.",
                        },
                    }
                ]
            return _verdict(packet, findings)

        def refresh_reviewers(self):
            if outcome not in {"retry", "persistent", "drift"}:
                return
            binding_type = type(self.reviewer_bindings[0])
            if outcome in {"retry", "persistent"}:
                current = self.reviewer_bindings[1]
                self.reviewer_bindings = (
                    self.reviewer_bindings[0],
                    binding_type(
                        slot=current.slot,
                        provider=current.provider,
                        model=current.model,
                        model_family=current.model_family,
                    ),
                )
                return
            self.reviewer_bindings = (
                self.reviewer_bindings[0],
                binding_type(
                    slot="code_worker",
                    provider="anthropic",
                    model="claude-fable-5",
                    model_family="claude",
                ),
            )

    runtime = AttachedRuntime(workspace=repo)
    policy_digest = bestplan_review_policy_digest(runtime.reviewer_bindings)

    base_oid = _git(repo, "rev-parse", "HEAD")
    (repo / "src/objective.py").write_text(
        "VALUE = 'bestplan-integration'\n", encoding="utf-8"
    )
    _git(repo, "add", "src/objective.py")
    integration_tree_oid = _git(repo, "write-tree")
    integration_oid = subprocess.run(
        ["git", "commit-tree", integration_tree_oid, "-p", base_oid],
        cwd=repo,
        input="active BestPlan generation\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    integration_ref = "refs/hermes-bestplan-integrations/active-plan/0"
    _git(repo, "update-ref", integration_ref, integration_oid)
    _git(repo, "reset", "--hard", "-q", base_oid)
    diff_bytes = subprocess.run(
        [
            "git",
            "diff",
            "--binary",
            "--full-index",
            "--no-renames",
            base_oid,
            integration_oid,
            "--",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    target = ReviewTarget.bestplan_integration(
        plan_id="active-plan",
        generation=0,
        base_oid=base_oid,
        local_target_oid=base_oid,
        integration_oid=integration_oid,
        integration_tree_oid=integration_tree_oid,
        integration_ref=integration_ref,
        integration_receipt_digest="1" * 64,
        check_receipt_digest="2" * 64,
        approval_digest="3" * 64,
        contract_digest="4" * 64,
        diff_sha256=hashlib.sha256(diff_bytes).hexdigest(),
        acceptance_digest="5" * 64,
        policy_digest=policy_digest,
    )
    artifact = ReviewArtifact.build(
        target=target,
        diff_bytes=diff_bytes,
        task="Implement the active BestPlan objective.",
        acceptance=("The exact BestPlan integration passes review.",),
        rules=("Report only evidence from the exact frozen integration.",),
        issue_locator_catalog={},
        dispositions=(),
    )
    expected_packet = json.loads(
        __import__("agent.review_engine", fromlist=["build_review_packet"])
        .build_review_packet(target, artifact=artifact)
    )

    job_id = "review-job-active-plan"
    store = ReviewStore(home / "state.db")
    store.create_job(
        job_id=job_id,
        source_kind=target.source_kind,
        source_id=target.plan_id,
        target_digest=target.target_digest,
        policy_digest=target.policy_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
        adapter_version="local-bestplan.v1",
        owner_session_id="manual-session",
        owner_profile="manual-profile",
        workspace=str(repo),
        adapter_state={
            "schema": "hermes.bestplan.local-review-adapter.v1",
        },
        runtime_routes=[
            {
                "route": binding.slot,
                "provider": binding.provider,
                "model": binding.model,
                "runtime_fingerprint": str(index + 1) * 64,
            }
            for index, binding in enumerate(runtime.reviewer_bindings)
        ],
    )
    owner_id = "manual-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:32]
    claim = store.claim_job(
        job_id=job_id,
        owner_id=owner_id,
        now_ns=time.time_ns(),
        lease_duration_ns=60_000_000_000,
        expected_fencing_token=0,
    )
    store.begin_generation(
        job_id=job_id,
        generation=target.generation,
        target=target,
        artifact=artifact,
        owner_id=owner_id,
        fencing_token=claim.fencing_token,
        operation_id="active-bestplan-generation",
    )
    raw = _verdict(expected_packet, [])
    stored_payload = json.dumps(
        {
            "schema": "hermes.bestplan.stored-reviewer-receipt.v1",
            "slot": "smart_reviewer",
            "provider": "anthropic",
            "model": "claude-opus-5",
            "model_family": "claude",
            "runtime_fingerprint": "1" * 64,
            "target_digest": target.target_digest,
            "integration_oid": target.integration_oid,
            "raw_output": raw,
            "findings": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    store.record_reviewer_receipt(
        job_id=job_id,
        generation=target.generation,
        slot="smart_reviewer",
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        output_digest=hashlib.sha256(
            b"hermes.bestplan.review-output.v1\0" + raw.encode("utf-8")
        ).hexdigest(),
        verdict_digest=hashlib.sha256(stored_payload.encode("utf-8")).hexdigest(),
        passed=True,
        receipt_json=stored_payload,
        owner_id=owner_id,
        fencing_token=claim.fencing_token,
        operation_id="active-bestplan-smart-reviewer",
    )
    # The original BestPlan owner is gone before the manual attachment starts.
    # Expire the otherwise-real lease only after its durable setup is complete.
    with sqlite3.connect(home / "state.db") as connection:
        connection.execute(
            "UPDATE review_jobs SET lease_expires_at_ns = 0 WHERE job_id = ?",
            (job_id,),
        )

    from tools import async_delegation

    delegation_id = "delegation-active-plan"
    tracker_path = home / "async_delegations.json"
    recovery_queue: queue.Queue = queue.Queue()
    async_record = {
        "delegation_id": delegation_id,
        "status": "review_waiting",
        "delivery_status": "review_waiting",
        "bestplan_local_execution": True,
        "bestplan_plan_id": target.plan_id,
        "bestplan_review_job_id": job_id,
        "bestplan_state_db_path": str(home / "state.db"),
        "origin_session_id": "manual-session",
        "origin_profile": "manual-profile",
        "origin_tracker_path": str(tracker_path),
    }
    monkeypatch.setitem(async_delegation._records, delegation_id, async_record)
    monkeypatch.setattr(
        async_delegation,
        "_bestplan_review_recovery_queue",
        recovery_queue,
    )
    monkeypatch.setattr(
        async_delegation,
        "_start_bestplan_review_recovery_consumer",
        lambda: None,
    )
    assert async_delegation._persist_record(
        async_record,
        delivery_status="review_waiting",
    )

    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        history=[
            {"role": "user", "content": "Implement the active BestPlan objective."},
            {
                "role": "assistant",
                "content": "BestPlan active-plan generation 0 is under review.",
                "display_metadata": {
                    "bestplan_review": {
                        "schema": "hermes.bestplan.active-review.v1",
                        "job_id": job_id,
                        "plan_id": target.plan_id,
                        "generation": target.generation,
                        "target_digest": target.target_digest,
                    }
                },
            },
        ],
    )

    assert result["completed"] is (outcome in {"pass", "retry"})
    assert result["review_state"] == {
        "pass": "passed",
        "blocker": "blocked",
        "retry": "passed",
        "persistent": "reviewing",
        "drift": "waiting",
    }[outcome]
    assert result["review_job_id"] == job_id
    assert [item[0] for item in runtime.reviewer_requests] == ["code_worker"] * (
        2 if outcome in {"retry", "persistent"} else 1
    )
    assert all(
        _packet(item[1]) == expected_packet for item in runtime.reviewer_requests
    )
    if outcome == "drift":
        assert recovery_queue.empty()
    else:
        request = recovery_queue.get_nowait()
        assert request == {
            "kind": "bestplan_review_resume",
            "delegation_id": delegation_id,
            "job_id": job_id,
            "plan_id": target.plan_id,
            "state_db_path": str(home / "state.db"),
            "tracker_path": str(tracker_path),
            "adapter_version": "local-bestplan.v1",
            "session_id": "manual-session",
            "profile": "manual-profile",
            "workspace": str(repo),
        }
        assert recovery_queue.empty()
        if outcome == "persistent":
            waiting = store.get_job(job_id)
            assert waiting.state == "reviewing"
            assert waiting.owner_id is None
            assert waiting.lease_expires_at_ns is None
            stable_runtime = _ManualRuntime(workspace=repo)
            recovered = _run(
                _agent(
                    database,
                    session_id="manual-session",
                    runtime=stable_runtime,
                ),
                history=[
                    {
                        "role": "assistant",
                        "content": "BestPlan active-plan generation 0 is under review.",
                        "display_metadata": {
                            "bestplan_review": {
                                "schema": "hermes.bestplan.active-review.v1",
                                "job_id": job_id,
                                "plan_id": target.plan_id,
                                "generation": target.generation,
                                "target_digest": target.target_digest,
                            }
                        },
                    },
                ],
            )
            assert recovered["completed"] is True
            assert recovered["review_job_id"] == job_id
            assert [
                item[0] for item in stable_runtime.reviewer_requests
            ] == ["code_worker"]
            assert recovery_queue.empty()
        assert async_delegation.enqueue_bestplan_review_job(
            state_db_path=str(home / "state.db"),
            job_id=job_id,
        )
        assert recovery_queue.empty()
    async_record["status"] = "review_requeued"
    async_record["delivery_status"] = "review_requeued"
    async_record["origin_profile"] = "stale-profile"
    assert not async_delegation.enqueue_bestplan_review_job(
        state_db_path=str(home / "state.db"),
        job_id=job_id,
    )
    assert recovery_queue.empty()
    async_record["origin_profile"] = "manual-profile"
    async_record["bestplan_review_job_id"] = "stale-review-job"
    async_record["status"] = "review_waiting"
    async_record["delivery_status"] = "review_waiting"
    assert not async_delegation.enqueue_bestplan_review_job(
        state_db_path=str(home / "state.db"),
        job_id=job_id,
    )
    assert recovery_queue.empty()
    connection = sqlite3.connect(home / "state.db")
    try:
        assert connection.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM review_reviewer_receipts WHERE job_id=?",
            (job_id,),
        ).fetchone()[0] == (1 if outcome == "drift" else 2)
    finally:
        connection.close()
    released = store.get_job(job_id)
    assert released.owner_id is None
    assert released.lease_expires_at_ns is None
    reclaimed = store.claim_job(
        job_id=job_id,
        owner_id="automatic-recovery-owner",
        now_ns=time.time_ns(),
        lease_duration_ns=1_000_000_000,
        expected_fencing_token=released.fencing_token,
    )
    assert reclaimed.owner_id == "automatic-recovery-owner"


def test_manual_review_rejects_a_stale_active_bestplan_hint(
    tmp_path,
    monkeypatch,
):
    """A stale named target must not fall back to another open BestPlan job."""

    from agent.review_engine import ReviewStore, ReviewStoreConflict

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    database = _session(home, repo)
    runtime = _ManualRuntime(workspace=repo)
    store = ReviewStore(home / "state.db")
    store.create_job(
        job_id="active-job-b",
        source_kind="bestplan_integration",
        source_id="plan-b",
        target_digest="1" * 64,
        policy_digest="2" * 64,
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
        adapter_version="local-bestplan.v1",
        owner_session_id="manual-session",
        owner_profile="manual-profile",
        workspace=str(repo),
        adapter_state={"schema": "hermes.bestplan.local-review-adapter.v1"},
        runtime_routes=[],
    )
    connection = sqlite3.connect(home / "state.db")
    try:
        connection.execute(
            "UPDATE review_jobs SET current_generation=0, state='reviewing' "
            "WHERE job_id='active-job-b'"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ReviewStoreConflict, match="named active BestPlan"):
        _run(
            _agent(database, session_id="manual-session", runtime=runtime),
            history=[
                {
                    "role": "assistant",
                    "content": "Plan A is active.",
                    "display_metadata": {
                        "bestplan_review": {
                            "schema": "hermes.bestplan.active-review.v1",
                            "job_id": "stale-job-a",
                        }
                    },
                }
            ],
        )


def test_manual_review_without_an_active_marker_uses_the_current_objective(
    tmp_path,
    monkeypatch,
):
    """A sole older BestPlan job must not hijack an unrelated bare /review."""

    from agent.bestplan_review import bestplan_review_policy_digest
    from agent.review_engine import ReviewStore

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    (repo / "src/objective.py").write_text(
        "VALUE = 'current-manual-objective'\n", encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    runtime = _ManualRuntime(workspace=repo)
    store = ReviewStore(home / "state.db")
    store.create_job(
        job_id="older-open-bestplan",
        source_kind="bestplan_integration",
        source_id="older-plan",
        target_digest="1" * 64,
        policy_digest=bestplan_review_policy_digest(runtime.reviewer_bindings),
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
        adapter_version="local-bestplan.v1",
        owner_session_id="manual-session",
        owner_profile="manual-profile",
        workspace=str(repo),
        adapter_state={"schema": "hermes.bestplan.local-review-adapter.v1"},
        runtime_routes=[],
    )
    connection = sqlite3.connect(home / "state.db")
    try:
        connection.execute(
            "UPDATE review_jobs SET current_generation=0, state='reviewing' "
            "WHERE job_id='older-open-bestplan'"
        )
        connection.commit()
    finally:
        connection.close()

    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        history=[
            {"role": "user", "content": "Review my current local objective."},
            {"role": "assistant", "content": "The local objective is ready."},
        ],
    )

    assert result["completed"] is True
    assert result["review_job_id"] != "older-open-bestplan"
    assert [item[0] for item in runtime.reviewer_requests] == [
        "smart_reviewer",
        "code_worker",
    ]
    assert _packet(runtime.reviewer_requests[0][1])["target"]["source_kind"] == (
        "manual_snapshot"
    )


def test_active_bestplan_lookup_is_scoped_to_the_exact_owner_profile(
    tmp_path,
    monkeypatch,
):
    from agent.review_engine import ReviewStore

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    store = ReviewStore(home / "state.db")
    for profile, suffix in (("manual-profile", "a"), ("other-profile", "b")):
        store.create_job(
            job_id=f"profile-job-{suffix}",
            source_kind="bestplan_integration",
            source_id=f"profile-plan-{suffix}",
            target_digest=suffix * 64,
            policy_digest="2" * 64,
            integration_oid="3" * 40,
            check_receipt_digest="4" * 64,
            adapter_version="local-bestplan.v1",
            owner_session_id="manual-session",
            owner_profile=profile,
            workspace=str(repo),
            adapter_state={"schema": "hermes.bestplan.local-review-adapter.v1"},
            runtime_routes=[],
        )
    connection = sqlite3.connect(home / "state.db")
    try:
        connection.execute(
            "UPDATE review_jobs SET current_generation=0, state='reviewing'"
        )
        connection.commit()
    finally:
        connection.close()

    found = store.find_active_bestplan_job(
        owner_session_id="manual-session",
        owner_profile="manual-profile",
        workspace=repo,
    )

    assert found is not None
    assert found.job_id == "profile-job-a"


def test_bare_review_builds_default_runtime_and_persists_no_target(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/app.py": "clean = True\n"})
    database = _session(home, repo)
    agent = _agent(database, session_id="manual-session")

    assert not hasattr(agent, "manual_review_runtime")
    result = _run(agent)

    assert result["completed"] is False
    assert "no objective" in result["final_response"].casefold()
    row = _job_row(home / "state.db")
    assert row["source_kind"] == "manual_snapshot"
    assert row["state"] == "blocked_no_target"
    assert row["adapter_version"] == "manual_snapshot.v1"
    assert row["owner_session_id"] == "manual-session"
    assert row["owner_profile"] == "manual-profile"
    assert Path(row["workspace"]) == repo


def test_manual_review_freezes_only_the_prior_objective_and_uses_both_reviewers(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {
            "src/owned_staged.py": "value = 'base'\n",
            "src/owned_unstaged.py": "value = 'base'\n",
            "src/unrelated_staged.py": "value = 'base'\n",
            "src/unrelated_unstaged.py": "value = 'base'\n",
        },
    )
    (repo / "src/owned_staged.py").write_text(
        "value = 'objective-staged'\n", encoding="utf-8"
    )
    _git(repo, "add", "src/owned_staged.py")
    (repo / "src/owned_unstaged.py").write_text(
        "value = 'objective-unstaged'\n", encoding="utf-8"
    )
    (repo / "src/owned_untracked.py").write_text(
        "value = 'objective-untracked'\n", encoding="utf-8"
    )
    (repo / "src/unrelated_staged.py").write_text(
        "value = 'ambient-staged'\n", encoding="utf-8"
    )
    _git(repo, "add", "src/unrelated_staged.py")
    (repo / "src/unrelated_unstaged.py").write_text(
        "value = 'ambient-unstaged'\n", encoding="utf-8"
    )
    (repo / "src/unrelated_untracked.py").write_text(
        "value = 'ambient-untracked'\n", encoding="utf-8"
    )
    objective_paths = [
        "src/owned_staged.py",
        "src/owned_unstaged.py",
        "src/owned_untracked.py",
    ]
    _mark_objective("manual-session", repo, objective_paths)
    database = _session(home, repo)
    runtime = _ManualRuntime(
        workspace=repo,
    )

    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        history=[
            {"role": "user", "content": "Implement the three owned files."},
            {"role": "assistant", "content": "Implementation is ready."},
        ],
    )

    assert result["completed"] is True
    assert [item[0] for item in runtime.reviewer_requests] == [
        "smart_reviewer",
        "code_worker",
    ]
    first_request = runtime.reviewer_requests[0][1]
    second_request = runtime.reviewer_requests[1][1]
    assert first_request == second_request
    packet = _packet(first_request)
    assert packet["schema"] == "hermes.bestplan.review-request.v1"
    assert packet["target"]["source_kind"] == "manual_snapshot"
    diff_bytes = base64.b64decode(
        packet["artifact"]["git_diff"]["content_base64"], validate=True
    )
    for path in objective_paths:
        assert path.encode("utf-8") in diff_bytes
    assert b"objective-staged" in diff_bytes
    assert b"objective-unstaged" in diff_bytes
    assert b"objective-untracked" in diff_bytes
    assert b"unrelated" not in diff_bytes
    assert b"ambient-" not in diff_bytes

    row = _job_row(home / "state.db")
    assert row["state"] == "passed"
    assert row["current_generation"] == 0
    assert row["owner_session_id"] == "manual-session"
    assert row["owner_profile"] == "manual-profile"
    assert Path(row["workspace"]) == repo

    snapshot_root = home / "review-snapshots"
    assert snapshot_root.is_dir()
    assert stat.S_IMODE(snapshot_root.stat().st_mode) & 0o077 == 0
    snapshot_directories = [
        path for path in snapshot_root.rglob("*") if path.is_dir()
    ]
    assert all(
        stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
        for path in snapshot_directories
    )
    snapshot_files = [path for path in snapshot_root.rglob("*") if path.is_file()]
    assert snapshot_files
    assert all(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0 for path in snapshot_files)
    assert any(path.read_bytes() == diff_bytes for path in snapshot_files)
    manifests = []
    for path in snapshot_files:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("schema") == (
            "hermes.manual-review-snapshot.v1"
        ):
            manifests.append(value)
    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest["owner"] == {
        "profile": "manual-profile",
        "session_id": "manual-session",
        "workspace": str(repo),
    }
    assert manifest["changed_paths"] == sorted(objective_paths)
    assert manifest["diff_sha256"] == hashlib.sha256(diff_bytes).hexdigest()

    (repo / "src/owned_staged.py").write_text(
        "value = 'later-live-drift'\n", encoding="utf-8"
    )
    assert any(path.read_bytes() == diff_bytes for path in snapshot_files)
    status = _git(repo, "status", "--short")
    assert "src/unrelated_staged.py" in status
    assert "src/unrelated_unstaged.py" in status
    assert "src/unrelated_untracked.py" in status


def test_manual_review_rejects_same_length_symlink_swap_before_objective_open(
    tmp_path,
    monkeypatch,
):
    from agent import review_engine

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    objective = repo / "src/objective.py"
    objective_bytes = b"VALUE = 'objective'\n"
    secret_bytes = b"S" * len(objective_bytes)
    assert len(secret_bytes) == len(objective_bytes)
    objective.write_bytes(objective_bytes)
    secret = tmp_path / "outside-secret.bin"
    secret.write_bytes(secret_bytes)
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    runtime = _ManualRuntime(workspace=repo)

    secret_oid = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=repo,
        input=secret_bytes,
        check=True,
        capture_output=True,
    ).stdout.strip().decode("ascii")
    original_path_open = Path.open
    original_os_open = os.open
    swapped = False

    def swap_objective() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        objective.unlink()
        objective.symlink_to(secret)

    def racing_path_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == objective and "r" in mode:
            swap_objective()
        return original_path_open(path, *args, **kwargs)

    def racing_os_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and os.fspath(path) == objective.name:
            swap_objective()
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "open", racing_path_open)
    monkeypatch.setattr(review_engine.os, "open", racing_os_open)

    with pytest.raises(review_engine.ReviewValidationError):
        _run(
            _agent(database, session_id="manual-session", runtime=runtime),
            scope="src/objective.py",
        )

    assert swapped is True
    assert runtime.reviewer_requests == []
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{secret_oid}^{{blob}}"],
        cwd=repo,
        capture_output=True,
    ).returncode != 0


def test_manual_snapshot_captures_tracked_deletion_with_absent_parent(tmp_path):
    from agent import review_engine

    path = "removed/nested/objective.py"
    repo = _repo(tmp_path / "repo", {path: "VALUE = 'tracked'\n"})
    objective = repo / path
    objective.unlink()
    objective.parent.rmdir()
    objective.parent.parent.rmdir()

    capture = review_engine._capture_manual_snapshot(repo, (path,))

    assert capture.path_states == ({"kind": "absent", "path": path},)
    assert capture.deleted_paths == (path,)
    assert path.encode("utf-8") in capture.diff_bytes
    assert b"VALUE = 'tracked'" in capture.diff_bytes


def test_manual_review_repairs_checks_and_reviews_a_fresh_snapshot_until_pass(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    (repo / "src/objective.py").write_text(
        "def value():\n" + unsafe_line,
        encoding="utf-8",
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    runtime = _ManualRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
        repaired_text='def value():\n    return "fixed"\n',
    )

    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        scope="src/objective.py",
    )

    assert result["completed"] is True
    assert len(runtime.reviewer_requests) == 4
    packets = [_packet(item[1]) for item in runtime.reviewer_requests]
    assert packets[0] == packets[1]
    assert packets[2] == packets[3]
    assert packets[0]["target_digest"] != packets[2]["target_digest"]
    assert packets[0]["target"]["generation"] == 0
    assert packets[2]["target"]["generation"] == 1
    assert len(runtime.repair_calls) == 1
    assert runtime.repair_calls[0]["allowed_paths"] == ("src/objective.py",)
    assert len(runtime.check_calls) == 1
    assert runtime.check_calls[0] == {
        "changed_paths": ("src/objective.py",),
        "generation": 1,
    }
    assert (repo / "src/objective.py").read_text(encoding="utf-8") == (
        'def value():\n    return "fixed"\n'
    )
    row = _job_row(home / "state.db")
    assert row["state"] == "passed"
    assert row["current_generation"] == 1
    assert _event_kinds(home / "state.db").count("review_pass") == 1


def test_manual_review_target_drift_stops_before_repair(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    (repo / "src/objective.py").write_text(
        "def value():\n" + unsafe_line,
        encoding="utf-8",
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    runtime = _ManualRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
        repaired_text='def value():\n    return "fixed"\n',
        drift_after_call=2,
    )

    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        scope="src/objective.py",
    )

    assert result["completed"] is False
    assert "target changed" in result["final_response"].casefold()
    assert runtime.repair_calls == []
    assert runtime.check_calls == []
    row = _job_row(home / "state.db")
    assert row["state"] == "waiting"
    assert "target_drift" in _event_kinds(home / "state.db")
    assert _pass_count(home / "state.db") == 0


def test_manual_review_out_of_authority_repair_is_durably_blocked(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {
            "src/objective.py": 'def value():\n    return "base"\n',
            "outside/not-owned.py": "must_remain = True\n",
        },
    )
    unsafe_line = '    return "unsafe"\n'
    (repo / "src/objective.py").write_text(
        "def value():\n" + unsafe_line,
        encoding="utf-8",
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    runtime = _ManualRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
        repaired_text='def value():\n    return "fixed"\n',
        require_outside_authority=True,
    )

    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        scope="src/objective.py",
    )

    assert result["completed"] is False
    assert "authority" in result["final_response"].casefold()
    assert len(runtime.repair_calls) == 1
    assert runtime.repair_calls[0]["allowed_paths"] == ("src/objective.py",)
    assert runtime.check_calls == []
    assert (repo / "outside/not-owned.py").read_text(encoding="utf-8") == (
        "must_remain = True\n"
    )
    row = _job_row(home / "state.db")
    assert row["state"] == "blocked_requires_authority"
    assert "blocked_requires_authority" in _event_kinds(home / "state.db")
    assert _pass_count(home / "state.db") == 0


def test_real_aiagent_default_runtime_uses_host_repair_and_checks(
    tmp_path,
    monkeypatch,
):
    """The live entrypoint must not depend on test-only Agent callbacks."""

    from agent import review_engine
    from agent.bestplan_local import LocalReviewAuthorityBinding
    from run_agent import AIAgent

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {
            "src/objective.py": 'def value():\n    return "base"\n',
            "tests/test_objective.py": (
                "from src.objective import value\n\n"
                'def test_value():\n    assert value() == "fixed"\n'
            ),
        },
    )
    unsafe_line = '    return "unsafe"\n'
    (repo / "src/objective.py").write_text(
        "def value():\n" + unsafe_line,
        encoding="utf-8",
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = "manual-session"
    agent._session_db = database
    agent.platform = "cli"
    assert not hasattr(agent, "manual_review_runtime")
    assert not hasattr(agent, "manual_review_repair_generation")
    assert not hasattr(agent, "manual_review_run_checks")

    class ReviewAuthority:
        def clone_for_review(self):
            return self

    authority_bindings = (
        LocalReviewAuthorityBinding(
            slot="smart_reviewer",
            provider="anthropic",
            model="claude-opus-5",
            model_family="claude",
            runtime_fingerprint="1" * 64,
            authority=ReviewAuthority(),
        ),
        LocalReviewAuthorityBinding(
            slot="code_worker",
            provider="openai-codex",
            model="gpt-5.6-sol",
            model_family="gpt",
            runtime_fingerprint="2" * 64,
            authority=ReviewAuthority(),
        ),
    )

    def resolve_reviewers(runtime):
        runtime._authority_bindings = authority_bindings
        runtime._reviewer_bindings = tuple(
            review_engine.ReviewerBinding(
                slot=item.slot,
                provider=item.provider,
                model=item.model,
                model_family=item.model_family,
            )
            for item in authority_bindings
        )

    monkeypatch.setattr(
        review_engine._DefaultManualReviewRuntime,
        "_resolve_reviewers",
        resolve_reviewers,
    )
    reviewer_calls = []

    def review_call(_authority, request):
        packet = _packet(request)
        reviewer_calls.append(packet)
        diff = base64.b64decode(
            packet["artifact"]["git_diff"]["content_base64"], validate=True
        )
        findings = (
            [_blocking_finding("src/objective.py", unsafe_line)]
            if unsafe_line.encode("utf-8") in diff
            else []
        )
        return _verdict(packet, findings)

    monkeypatch.setattr(
        "agent.bestplan_local.call_local_review_authority",
        review_call,
    )
    host_calls = []

    def host_repair(**kwargs):
        host_calls.append(kwargs)
        assert kwargs["allowed_paths"] == ("src/objective.py",)
        assert kwargs["generation"] == 1
        (repo / "src/objective.py").write_text(
            'def value():\n    return "fixed"\n',
            encoding="utf-8",
        )
        return {
            "status": "applied",
            "changed_paths": ["src/objective.py"],
            "check_receipt_digest": "a" * 64,
        }

    monkeypatch.setattr(
        "agent.manual_review_runtime.execute_manual_bestplan_repair",
        host_repair,
    )

    result = _run(agent, scope="src/objective.py")

    assert result["completed"] is True
    assert len(reviewer_calls) == 4
    assert len(host_calls) == 1
    assert _job_row(home / "state.db")["state"] == "passed"


def test_real_manual_host_freezes_checks_and_applies_only_objective_paths(
    tmp_path,
    monkeypatch,
):
    from agent import bestplan_candidates, bestplan_checks, review_engine
    from agent import manual_review_runtime
    from run_agent import AIAgent

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {
            "pytest.ini": "[pytest]\n",
            "src/a.py": 'VALUE = "base-a"\n',
            "src/b.py": 'VALUE = "base-b"\n',
            "tests/test_a.py": "def test_a():\n    assert True\n",
            "tests/test_b.py": "def test_b():\n    assert True\n",
            "other/staged.txt": "staged-base\n",
            "other/unstaged.txt": "unstaged-base\n",
        },
    )
    (repo / "src/a.py").write_text('VALUE = "unsafe-a"\n', encoding="utf-8")
    (repo / "src/b.py").write_text('VALUE = "unsafe-b"\n', encoding="utf-8")
    (repo / "other/staged.txt").write_text("staged-live\n", encoding="utf-8")
    _git(repo, "add", "other/staged.txt")
    (repo / "other/unstaged.txt").write_text(
        "unstaged-live\n", encoding="utf-8"
    )
    (repo / "other/untracked.txt").write_text(
        "untracked-live\n", encoding="utf-8"
    )
    allowed_paths = ("src/a.py", "src/b.py")
    capture = review_engine._capture_manual_snapshot(repo, allowed_paths)
    target = review_engine.ReviewTarget.manual_snapshot(
        job_id="manual-host-test",
        generation=0,
        repository_id=review_engine._manual_repository_id(repo),
        base_oid=capture.base_oid,
        snapshot_tree_oid=capture.snapshot_tree_oid,
        snapshot_digest="1" * 64,
        diff_sha256=hashlib.sha256(capture.diff_bytes).hexdigest(),
        acceptance_digest="2" * 64,
        policy_digest="3" * 64,
    )
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = "manual-host-test"
    before_unrelated = {
        path: (repo / path).read_bytes()
        for path in (
            "other/staged.txt",
            "other/unstaged.txt",
            "other/untracked.txt",
        )
    }
    index_tree_before = _git(repo, "write-tree")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        manual_review_runtime,
        "_resolve_write_authority",
        lambda **_kwargs: ({"model": "fake-code-worker"}, object()),
    )

    def run_candidate(**kwargs):
        spec = kwargs["spec"]
        prior = kwargs["candidate_base"]
        attempt_id = kwargs["attempt_id"]
        controller = kwargs["expected_controller"]
        captured["spec"] = spec
        assert spec.allowed_paths == allowed_paths
        assert spec.read_only is False

        index_path = tmp_path / "candidate.index"
        environment = {**os.environ, "GIT_INDEX_FILE": str(index_path)}

        def git_bytes(*args, input_bytes=None):
            return subprocess.run(
                ["git", *args],
                cwd=repo,
                env=environment,
                input=input_bytes,
                capture_output=True,
                check=True,
            ).stdout

        git_bytes("read-tree", prior.tree_oid)
        for path, content in (
            ("src/a.py", b'VALUE = "fixed-a"\n'),
            ("src/b.py", b'VALUE = "fixed-b"\n'),
        ):
            oid = git_bytes("hash-object", "-w", "--stdin", input_bytes=content)
            git_bytes(
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                oid.strip(),
                path.encode("utf-8"),
            )
        tree_oid = git_bytes("write-tree").strip().decode("ascii")
        identity = {
            **environment,
            "GIT_AUTHOR_NAME": "Manual Host Test",
            "GIT_AUTHOR_EMAIL": "manual-host@example.test",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_NAME": "Manual Host Test",
            "GIT_COMMITTER_EMAIL": "manual-host@example.test",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
        commit_oid = subprocess.run(
            ["git", "commit-tree", tree_oid, "-p", prior.integration_oid],
            cwd=repo,
            env=identity,
            input=b"Manual candidate\n",
            capture_output=True,
            check=True,
        ).stdout.strip().decode("ascii")
        ref_name = bestplan_candidates.candidate_ref_name(
            spec.plan_id, spec.slice_id, attempt_id
        )
        _git(repo, "update-ref", ref_name, commit_oid)
        receipt = {"status": "completed", "summary": "fake provider boundary"}
        receipt_bytes = json.dumps(
            receipt,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return bestplan_candidates.FrozenCandidate(
            candidate_id=spec.candidate_id,
            slice_id=spec.slice_id,
            attempt_id=attempt_id,
            commit_oid=commit_oid,
            tree_oid=tree_oid,
            ref_name=ref_name,
            changed_paths=(b"src/a.py", b"src/b.py"),
            raw_receipt=receipt,
            raw_receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
            policy_digest="4" * 64,
            controller_id=controller.controller_id,
            controller_repository_id=controller.repository_id,
            controller_release_oid=controller.release_oid,
            controller_artifact_sha256=controller.artifact_sha256,
            admitted_requests=1,
            admitted_input_tokens=10,
            admitted_output_tokens=10,
        )

    monkeypatch.setattr(
        bestplan_candidates,
        "run_and_freeze_repair_candidate",
        run_candidate,
    )
    check_digest = "5" * 64

    def run_checks(**kwargs):
        integration = kwargs["integration"]
        captured["checked_integration"] = integration
        assert integration.candidates
        assert _git(repo, "show", f"{integration.tree_oid}:src/a.py") == (
            'VALUE = "fixed-a"'
        )
        assert _git(repo, "show", f"{integration.tree_oid}:src/b.py") == (
            'VALUE = "fixed-b"'
        )
        assert _git(
            repo, "show", f"{integration.tree_oid}:other/staged.txt"
        ) == "staged-base"
        return bestplan_checks.CheckSetReceipt(
            integration_oid=integration.integration_oid,
            contract_digest=integration.contract_digest,
            ordered_receipts=(),
            receipt_digest=check_digest,
        )

    monkeypatch.setattr(bestplan_checks, "run_integration_checks", run_checks)
    blockers = tuple(
        SimpleNamespace(
            severity="high",
            locator=SimpleNamespace(
                kind="changed_lines",
                path=path,
                start_line=1,
                end_line=1,
                locator_id="",
            ),
            title="unsafe value",
            trigger="manual objective",
            observed_failure="unsafe value remains",
            blast_radius="selected objective",
            reproduction=SimpleNamespace(kind="reason", argv=(), reason="review"),
            fingerprint=str(index + 6) * 64,
        )
        for index, path in enumerate(allowed_paths)
    )

    result = manual_review_runtime.execute_manual_bestplan_repair(
        agent=agent,
        blockers=blockers,
        allowed_paths=allowed_paths,
        generation=1,
        target=target,
        workspace=repo,
        expected_live_state_digest=capture.live_state_digest,
        task="Fix the unsafe objective values.",
    )

    assert result == {
        "status": "applied",
        "changed_paths": ["src/a.py", "src/b.py"],
        "check_receipt_digest": check_digest,
    }
    assert captured["checked_integration"].tree_oid != capture.snapshot_tree_oid
    assert (repo / "src/a.py").read_text(encoding="utf-8") == (
        'VALUE = "fixed-a"\n'
    )
    assert (repo / "src/b.py").read_text(encoding="utf-8") == (
        'VALUE = "fixed-b"\n'
    )
    assert _git(repo, "write-tree") == index_tree_before
    assert {
        path: (repo / path).read_bytes() for path in before_unrelated
    } == before_unrelated


def test_manual_host_swap_between_check_and_install_is_not_overwritten(
    tmp_path,
    monkeypatch,
):
    from agent import manual_review_runtime

    workspace = tmp_path / "workspace"
    materialized = tmp_path / "materialized"
    for root in (workspace, materialized):
        (root / "src").mkdir(parents=True)
    target = workspace / "src/a.py"
    target.write_bytes(b'VALUE = "before"\n')
    (materialized / "src/a.py").write_bytes(b'VALUE = "repaired"\n')
    external_leaf = ".external-a.py"
    external = b'VALUE = "before"\n'
    (workspace / "src" / external_leaf).write_bytes(external)

    real_atomic_write = manual_review_runtime._atomic_write
    swapped = False

    def swap_before_install(parent_fd, name, entry):
        nonlocal swapped
        if not swapped:
            os.rename(
                external_leaf,
                "a.py",
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            swapped = True
        return real_atomic_write(parent_fd, name, entry)

    monkeypatch.setattr(
        manual_review_runtime, "_atomic_write", swap_before_install
    )

    with pytest.raises(
        manual_review_runtime._ManualRuntimeWaiting,
        match="target changed",
    ):
        manual_review_runtime._apply_repaired_paths(
            workspace=workspace,
            materialized_root=materialized,
            prefix="",
            root_paths=("src/a.py",),
        )

    assert swapped is True
    assert target.read_bytes() == external


@pytest.mark.parametrize("swap_kind", ["same_size_inode", "symlink"])
def test_manual_candidate_capture_is_bound_to_the_lstat_inode(
    tmp_path,
    monkeypatch,
    swap_kind,
):
    from agent import manual_review_runtime

    workspace = tmp_path / "workspace"
    materialized = tmp_path / "materialized"
    for root in (workspace, materialized):
        (root / "src").mkdir(parents=True)
    target = workspace / "src/a.py"
    source = materialized / "src/a.py"
    original = b"O" * 32
    candidate = b"C" * 32
    external = b"E" * 32
    target.write_bytes(original)
    source.write_bytes(candidate)
    replacement = tmp_path / "replacement.py"
    replacement.write_bytes(external)

    real_stat = os.stat
    source_parent = real_stat(source.parent, follow_symlinks=False)
    source_parent_identity = (source_parent.st_dev, source_parent.st_ino)
    swapped = False

    def swap_after_stat(path, *args, **kwargs):
        nonlocal swapped
        info = real_stat(path, *args, **kwargs)
        dir_fd = kwargs.get("dir_fd")
        opened_parent = os.fstat(dir_fd) if dir_fd is not None else None
        if (
            path == "a.py"
            and opened_parent is not None
            and (opened_parent.st_dev, opened_parent.st_ino)
            == source_parent_identity
            and not swapped
        ):
            if swap_kind == "same_size_inode":
                os.replace(replacement, source)
            else:
                source.unlink()
                source.symlink_to(replacement)
            swapped = True
        return info

    monkeypatch.setattr(os, "stat", swap_after_stat)

    with pytest.raises(
        manual_review_runtime._ManualRuntimeWaiting,
        match="candidate artifact changed",
    ):
        manual_review_runtime._apply_repaired_paths(
            workspace=workspace,
            materialized_root=materialized,
            prefix="",
            root_paths=("src/a.py",),
        )

    assert swapped is True
    assert target.read_bytes() == original
    assert target.read_bytes() != external


def test_manual_apply_collision_preserves_external_and_captured_original(
    tmp_path,
    monkeypatch,
):
    from agent import manual_review_runtime

    workspace = tmp_path / "workspace"
    materialized = tmp_path / "materialized"
    for root in (workspace, materialized):
        (root / "src").mkdir(parents=True)
    target = workspace / "src/a.py"
    original = b'VALUE = "original"\n'
    external = b'VALUE = "external"\n'
    target.write_bytes(original)
    (materialized / "src/a.py").write_bytes(b'VALUE = "repaired"\n')

    real_read_live_entry = manual_review_runtime._read_live_entry
    recreated = False

    def recreate_destination_after_capture(parent_fd, name):
        nonlocal recreated
        observed = real_read_live_entry(parent_fd, name)
        if name.startswith(".hermes-manual-before-") and not recreated:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open("a.py", flags, 0o600, dir_fd=parent_fd)
            try:
                assert os.write(descriptor, external) == len(external)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            recreated = True
        return observed

    monkeypatch.setattr(
        manual_review_runtime,
        "_read_live_entry",
        recreate_destination_after_capture,
    )

    with pytest.raises(manual_review_runtime._ManualRuntimeWaiting) as failure:
        manual_review_runtime._apply_repaired_paths(
            workspace=workspace,
            materialized_root=materialized,
            prefix="",
            root_paths=("src/a.py",),
        )

    assert recreated is True
    assert target.read_bytes() == external
    artifacts = list((workspace / "src").glob(".hermes-manual-recovery-*"))
    assert len(artifacts) == 1
    assert artifacts[0].read_bytes() == original
    assert stat.S_IMODE(artifacts[0].stat().st_mode) == 0o600
    assert artifacts[0].name in str(failure.value)


def test_manual_apply_does_not_follow_a_swapped_intermediate_parent(
    tmp_path,
    monkeypatch,
):
    from agent import manual_review_runtime

    workspace = tmp_path / "workspace"
    materialized = tmp_path / "materialized"
    outside = tmp_path / "outside"
    for root in (workspace, materialized, outside):
        (root / "src/nested").mkdir(parents=True)
    relative = Path("src/nested/a.py")
    original = b'VALUE = "original"\n'
    repaired = b'VALUE = "repaired"\n'
    (workspace / relative).write_bytes(original)
    (materialized / relative).write_bytes(repaired)
    outside_target = outside / relative
    outside_target.write_bytes(original)

    real_open = os.open
    swapped = False

    def swap_intermediate_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        absolute_parent_open = dir_fd is None and Path(path) == (
            workspace / "src/nested"
        )
        relative_component_open = dir_fd is not None and path == "src"
        if not swapped and (absolute_parent_open or relative_component_open):
            os.rename(workspace / "src", workspace / "src-captured")
            (workspace / "src").symlink_to(
                outside / "src",
                target_is_directory=True,
            )
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_intermediate_before_open)

    with pytest.raises(
        manual_review_runtime._ManualRuntimeWaiting,
        match="target parent changed",
    ):
        manual_review_runtime._apply_repaired_paths(
            workspace=workspace,
            materialized_root=materialized,
            prefix="",
            root_paths=(relative.as_posix(),),
        )

    assert swapped is True
    assert outside_target.read_bytes() == original
    assert (workspace / "src-captured/nested/a.py").read_bytes() == original


def test_manual_candidate_parent_swap_never_applies_outside_bytes(
    tmp_path,
    monkeypatch,
):
    from agent import manual_review_runtime

    workspace = tmp_path / "workspace"
    materialized = tmp_path / "materialized"
    outside = tmp_path / "outside"
    for root in (workspace, materialized, outside):
        (root / "src/nested").mkdir(parents=True)
    relative = Path("src/nested/a.py")
    original = b'VALUE = "original"\n'
    candidate = b'VALUE = "candidate"\n'
    outside_bytes = b'VALUE = "outside"\n'
    target = workspace / relative
    source = materialized / relative
    target.write_bytes(original)
    source.write_bytes(candidate)
    (outside / relative).write_bytes(outside_bytes)

    real_lstat = Path.lstat
    real_stat = os.stat
    materialized_identity = (
        real_stat(materialized, follow_symlinks=False).st_dev,
        real_stat(materialized, follow_symlinks=False).st_ino,
    )
    swapped = False

    def swap_candidate_parent():
        nonlocal swapped
        os.rename(materialized / "src", materialized / "src-captured")
        (materialized / "src").symlink_to(
            outside / "src",
            target_is_directory=True,
        )
        swapped = True

    def swap_before_absolute_leaf_lstat(path, *args, **kwargs):
        if path == source and not swapped:
            swap_candidate_parent()
        return real_lstat(path, *args, **kwargs)

    def swap_before_relative_component_stat(path, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        if not swapped and path == "src" and dir_fd is not None:
            opened = os.fstat(dir_fd)
            if (opened.st_dev, opened.st_ino) == materialized_identity:
                swap_candidate_parent()
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", swap_before_absolute_leaf_lstat)
    monkeypatch.setattr(os, "stat", swap_before_relative_component_stat)

    failure = None
    try:
        manual_review_runtime._apply_repaired_paths(
            workspace=workspace,
            materialized_root=materialized,
            prefix="",
            root_paths=(relative.as_posix(),),
        )
    except manual_review_runtime._ManualRuntimeWaiting as exc:
        failure = exc

    assert target.read_bytes() == original
    assert isinstance(failure, manual_review_runtime._ManualRuntimeWaiting)
    assert "candidate parent changed" in str(failure)
    assert swapped is True
    assert (materialized / "src-captured/nested/a.py").read_bytes() == candidate
    assert (outside / relative).read_bytes() == outside_bytes


def test_manual_candidate_root_is_opened_by_descriptor_walk(
    tmp_path,
    monkeypatch,
):
    from agent import manual_review_runtime

    workspace = tmp_path / "workspace"
    candidate_anchor = tmp_path / "candidate-anchor"
    captured_anchor = tmp_path / "candidate-anchor-captured"
    outside_anchor = tmp_path / "outside-anchor"
    materialized = candidate_anchor / "materialized"
    outside_materialized = outside_anchor / "materialized"
    (workspace / "src").mkdir(parents=True)
    (materialized / "src").mkdir(parents=True)
    (outside_materialized / "src").mkdir(parents=True)
    original = b'VALUE = "original"\n'
    candidate = b'VALUE = "candidate"\n'
    outside_bytes = b'VALUE = "outside"\n'
    target = workspace / "src/a.py"
    target.write_bytes(original)
    (materialized / "src/a.py").write_bytes(candidate)
    (outside_materialized / "src/a.py").write_bytes(outside_bytes)

    real_lstat = Path.lstat
    real_stat = os.stat
    anchor_parent = real_stat(tmp_path, follow_symlinks=False)
    anchor_parent_identity = (anchor_parent.st_dev, anchor_parent.st_ino)
    swapped = False

    def swap_root_ancestor():
        nonlocal swapped
        os.rename(candidate_anchor, captured_anchor)
        candidate_anchor.symlink_to(outside_anchor, target_is_directory=True)
        swapped = True

    def swap_before_absolute_root_lstat(path, *args, **kwargs):
        if path == materialized and not swapped:
            swap_root_ancestor()
        return real_lstat(path, *args, **kwargs)

    def swap_before_relative_anchor_stat(path, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        if not swapped and path == "candidate-anchor" and dir_fd is not None:
            opened = os.fstat(dir_fd)
            if (opened.st_dev, opened.st_ino) == anchor_parent_identity:
                swap_root_ancestor()
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", swap_before_absolute_root_lstat)
    monkeypatch.setattr(os, "stat", swap_before_relative_anchor_stat)

    failure = None
    try:
        manual_review_runtime._apply_repaired_paths(
            workspace=workspace,
            materialized_root=materialized,
            prefix="",
            root_paths=("src/a.py",),
        )
    except manual_review_runtime._ManualRuntimeWaiting as exc:
        failure = exc

    assert target.read_bytes() == original
    assert isinstance(failure, manual_review_runtime._ManualRuntimeWaiting)
    assert "candidate root changed" in str(failure)
    assert swapped is True
    assert (captured_anchor / "materialized/src/a.py").read_bytes() == candidate
    assert (outside_materialized / "src/a.py").read_bytes() == outside_bytes


def test_manual_host_failed_apply_rolls_back_all_prior_objective_files(
    tmp_path,
    monkeypatch,
):
    from agent import manual_review_runtime

    workspace = tmp_path / "workspace"
    materialized = tmp_path / "materialized"
    for root in (workspace, materialized):
        (root / "src").mkdir(parents=True)
    originals = {
        "src/a.py": b'VALUE = "unsafe-a"\n',
        "src/b.py": b'VALUE = "unsafe-b"\n',
    }
    for path, content in originals.items():
        (workspace / path).write_bytes(content)
        (materialized / path).write_bytes(content.replace(b"unsafe", b"fixed"))
    real_atomic_write = manual_review_runtime._atomic_write
    calls = 0

    def fail_second_write(parent_fd, name, entry):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-file apply failure")
        return real_atomic_write(parent_fd, name, entry)

    monkeypatch.setattr(
        manual_review_runtime, "_atomic_write", fail_second_write
    )

    with pytest.raises(OSError, match="second-file"):
        manual_review_runtime._apply_repaired_paths(
            workspace=workspace,
            materialized_root=materialized,
            prefix="",
            root_paths=("src/a.py", "src/b.py"),
        )

    assert calls == 3
    assert {
        path: (workspace / path).read_bytes() for path in originals
    } == originals


def test_manual_host_failed_apply_does_not_roll_back_external_edit(
    tmp_path,
    monkeypatch,
):
    from agent import manual_review_runtime

    workspace = tmp_path / "workspace"
    materialized = tmp_path / "materialized"
    for root in (workspace, materialized):
        (root / "src").mkdir(parents=True)
    originals = {
        "src/a.py": b'VALUE = "unsafe-a"\n',
        "src/b.py": b'VALUE = "unsafe-b"\n',
    }
    for path, content in originals.items():
        (workspace / path).write_bytes(content)
        (materialized / path).write_bytes(content.replace(b"unsafe", b"fixed"))

    external = b'VALUE = "fixed-a"\n'
    real_compare_and_swap = manual_review_runtime._compare_and_swap_entry
    calls = 0

    def externally_edit_a_then_fail_b(
        parent_fd,
        name,
        expected,
        replacement,
        **kwargs,
    ):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-file apply failure")
        result = real_compare_and_swap(
            parent_fd,
            name,
            expected,
            replacement,
            **kwargs,
        )
        if calls == 1:
            replacement_leaf = ".external-a.py"
            real_compare_and_swap(
                parent_fd,
                replacement_leaf,
                manual_review_runtime._LiveEntry(False),
                manual_review_runtime._LiveEntry(True, external, 0o644),
            )
            os.rename(
                replacement_leaf,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        return result

    monkeypatch.setattr(
        manual_review_runtime,
        "_compare_and_swap_entry",
        externally_edit_a_then_fail_b,
    )

    with pytest.raises(
        manual_review_runtime._ManualRuntimeWaiting,
        match="rollback could not restore",
    ) as failure:
        manual_review_runtime._apply_repaired_paths(
            workspace=workspace,
            materialized_root=materialized,
            prefix="",
            root_paths=("src/a.py", "src/b.py"),
        )

    assert (workspace / "src/a.py").read_bytes() == external
    assert (workspace / "src/b.py").read_bytes() == originals["src/b.py"]
    artifacts = list((workspace / "src").glob(".hermes-manual-recovery-*"))
    assert len(artifacts) == 1
    assert artifacts[0].read_bytes() == originals["src/a.py"]
    assert stat.S_IMODE(artifacts[0].stat().st_mode) == 0o600
    assert artifacts[0].name in str(failure.value)


def test_manual_review_retries_a_transient_reviewer_failure_until_pass(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    (repo / "src/objective.py").write_text(
        "VALUE = 'ready-for-review'\n", encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)

    class TransientReviewerRuntime(_ManualRuntime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.attempts = 0

        def reviewer_call(self, binding, request):
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("transient reviewer timeout")
            return super().reviewer_call(binding, request)

    runtime = TransientReviewerRuntime(workspace=repo)

    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        scope="src/objective.py",
    )

    assert result["completed"] is True
    assert result["review_state"] == "passed"
    assert runtime.attempts == 3
    assert [item[0] for item in runtime.reviewer_requests] == [
        "smart_reviewer",
        "code_worker",
    ]
    assert _event_kinds(home / "state.db").count("reviewer_failure") == 1


def test_persistent_manual_reviewer_failure_waits_without_an_inline_retry_burst(
    tmp_path,
    monkeypatch,
):
    from agent import review_engine

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    (repo / "src/objective.py").write_text(
        "VALUE = 'ready-for-review'\n", encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    queued: list[dict[str, object]] = []
    monkeypatch.setattr(
        review_engine,
        "_enqueue_manual_review_resume",
        lambda **kwargs: queued.append(dict(kwargs)),
    )

    class RetryBurstExceeded(BaseException):
        pass

    class AlwaysMalformedRuntime(_ManualRuntime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.attempts = 0

        def reviewer_call(self, _binding, _request):
            self.attempts += 1
            if self.attempts > 4:
                raise RetryBurstExceeded("manual reviewer retried without a bound")
            raise review_engine.ReviewValidationError("malformed reviewer output")

        def refresh_reviewers(self):
            return None

    runtime = AlwaysMalformedRuntime(workspace=repo)
    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        scope="src/objective.py",
    )

    assert runtime.attempts == 2
    assert result["completed"] is False
    assert result["review_state"] == "waiting"
    durable = review_engine.ReviewStore(home / "state.db").get_job(
        result["review_job_id"]
    )
    assert durable.state == "waiting"
    assert durable.owner_id is None
    assert durable.lease_expires_at_ns is None
    assert queued == [
        {
            "state_db_path": home / "state.db",
            "job_id": result["review_job_id"],
        }
    ]


def test_failed_manual_recovery_enqueue_retries_after_durable_lease_release(
    tmp_path,
    monkeypatch,
):
    from agent import review_engine
    from tools import async_delegation

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    (repo / "src/objective.py").write_text(
        "VALUE = 'ready-for-review'\n", encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    monkeypatch.setattr(
        review_engine,
        "_REVIEW_ENQUEUE_RETRY_BASE_SECONDS",
        0.01,
        raising=False,
    )
    recovered = threading.Event()
    recovery_results: list[dict[str, object]] = []
    enqueue_observations: list[tuple[str, str | None]] = []
    recovery_agent = _agent(
        database,
        session_id="manual-session",
        runtime=_ManualRuntime(workspace=repo),
    )

    def fail_once_then_execute(request):
        job = review_engine.ReviewStore(home / "state.db").get_job(
            str(request["job_id"])
        )
        enqueue_observations.append((job.state, job.owner_id))
        if len(enqueue_observations) == 1:
            return False
        try:
            recovery_results.append(
                review_engine.resume_manual_review_job(recovery_agent, request)
            )
        finally:
            recovered.set()
        return True

    monkeypatch.setattr(
        async_delegation,
        "enqueue_manual_review_recovery",
        fail_once_then_execute,
    )

    class OfflineReviewerRuntime(_ManualRuntime):
        def reviewer_call(self, _binding, _request):
            raise ConnectionError("review host is temporarily offline")

    first = _run(
        _agent(
            database,
            session_id="manual-session",
            runtime=OfflineReviewerRuntime(workspace=repo),
        ),
        scope="src/objective.py",
    )

    assert first["completed"] is False
    assert first["review_state"] == "waiting"
    assert recovered.wait(2)
    assert enqueue_observations == [("waiting", None), ("waiting", None)]
    assert len(recovery_results) == 1
    assert recovery_results[0]["completed"] is True
    assert recovery_results[0]["review_state"] == "passed"
    assert review_engine.ReviewStore(home / "state.db").get_job(
        str(first["review_job_id"])
    ).state == "passed"


def test_failed_attached_recovery_enqueue_retries_in_the_same_process(
    tmp_path,
    monkeypatch,
):
    from agent import review_engine
    from tools import async_delegation

    state_db_path = tmp_path / "state.db"
    state_db_path.touch()
    monkeypatch.setattr(
        review_engine,
        "_REVIEW_ENQUEUE_RETRY_BASE_SECONDS",
        0.01,
        raising=False,
    )
    queued = threading.Event()
    calls: list[tuple[str, str]] = []

    def fail_once_then_queue(*, state_db_path, job_id):
        calls.append((state_db_path, job_id))
        if len(calls) == 1:
            return False
        queued.set()
        return True

    monkeypatch.setattr(
        async_delegation,
        "enqueue_bestplan_review_job",
        fail_once_then_queue,
    )

    accepted = review_engine._enqueue_attached_bestplan_resume(
        state_db_path=state_db_path,
        job_id="attached-job",
    )

    assert accepted is False
    assert queued.wait(2)
    assert calls == [
        (str(state_db_path), "attached-job"),
        (str(state_db_path), "attached-job"),
    ]


def test_expired_manual_lease_cannot_mutate_or_start_an_effect_after_reclaim(
    tmp_path,
):
    from agent import review_engine

    store = review_engine.ReviewStore(tmp_path / "state.db")
    target = review_engine.ReviewTarget.manual_snapshot(
        job_id="manual-expired-lease",
        generation=0,
        repository_id="manual-expired-repository",
        snapshot_tree_oid="1" * 40,
        base_oid="2" * 40,
        snapshot_digest="3" * 64,
        diff_sha256="4" * 64,
        acceptance_digest="5" * 64,
        policy_digest="6" * 64,
    )
    store.create_job(
        job_id="manual-expired-lease",
        source_kind="manual_snapshot",
        source_id="manual-expired-lease",
        target_digest=target.target_digest,
        policy_digest=target.policy_digest,
        integration_oid=target.snapshot_tree_oid,
        check_receipt_digest=target.check_receipt_digest,
    )
    old_claim = store.claim_job(
        job_id="manual-expired-lease",
        owner_id="old-owner",
        now_ns=time.time_ns(),
        lease_duration_ns=10_000_000_000,
        expected_fencing_token=0,
    )
    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute(
            "UPDATE review_jobs SET lease_expires_at_ns=0 WHERE job_id=?",
            ("manual-expired-lease",),
        )

    with pytest.raises(review_engine.ReviewLeaseConflict):
        store.begin_generation(
            job_id="manual-expired-lease",
            generation=0,
            target=target,
            owner_id="old-owner",
            fencing_token=old_claim.fencing_token,
            operation_id="expired-mutation",
        )

    new_claim = store.claim_job(
        job_id="manual-expired-lease",
        owner_id="new-owner",
        now_ns=time.time_ns(),
        lease_duration_ns=10_000_000_000,
        expected_fencing_token=old_claim.fencing_token,
    )
    effects: list[str] = []
    with pytest.raises(review_engine._ManualInvocationCancelled):
        review_engine._manual_controlled_call(
            agent=SimpleNamespace(
                _interrupt_requested=False,
                _hard_interrupt_requested=threading.Event(),
            ),
            store=store,
            job_id="manual-expired-lease",
            owner_id="old-owner",
            fencing_token=old_claim.fencing_token,
            cancel_operation_id="old-owner-cancel",
            cancel_event=threading.Event(),
            adoption_lock=threading.Lock(),
            call=lambda: effects.append("old"),
        )
    assert effects == []

    result = review_engine._manual_controlled_call(
        agent=SimpleNamespace(
            _interrupt_requested=False,
            _hard_interrupt_requested=threading.Event(),
        ),
        store=store,
        job_id="manual-expired-lease",
        owner_id="new-owner",
        fencing_token=new_claim.fencing_token,
        cancel_operation_id="new-owner-cancel",
        cancel_event=threading.Event(),
        adoption_lock=threading.Lock(),
        call=lambda: effects.append("new") or "done",
    )
    assert result == "done"
    assert effects == ["new"]


def test_default_manual_runtime_refreshes_a_consumed_reviewer_before_retry(
    tmp_path,
    monkeypatch,
):
    """A malformed one-shot response must not retry the spent authority forever."""

    from agent.bestplan_authority_client import AuthorityProtocolError
    from agent.bestplan_local import LocalReviewAuthorityBinding
    from agent.review_engine import _DefaultManualReviewRuntime, ReviewerBinding

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    (repo / "src/objective.py").write_text(
        "VALUE = 'ready-for-review'\n", encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    agent = _agent(database, session_id="manual-session")
    runtime = _DefaultManualReviewRuntime(agent, repo)
    agent.manual_review_runtime = runtime

    calls: list[tuple[str, int]] = []
    clones: list[str] = []

    class RetryBoundExceeded(BaseException):
        pass

    class OneShotAuthority:
        def __init__(self, slot: str, *, fail_after_consume: bool = False):
            self.slot = slot
            self.fail_after_consume = fail_after_consume
            self.used = False

        def clone_for_review(self):
            clones.append(self.slot)
            return OneShotAuthority(self.slot)

        def review_request(self, request):
            calls.append((self.slot, id(self)))
            if len(calls) > 6:
                raise RetryBoundExceeded("manual reviewer retry loop did not refresh")
            if self.used:
                raise AuthorityProtocolError("local review authority is already used")
            self.used = True
            body = json.loads(request.request_json)
            packet = json.loads(body["messages"][-1]["content"])
            if self.fail_after_consume:
                raise AuthorityProtocolError(
                    "local review response must be one JSON object"
                )
            return _verdict(packet, [])

    authorities = (
        LocalReviewAuthorityBinding(
            slot="smart_reviewer",
            provider="anthropic",
            model="claude-opus-5",
            model_family="claude",
            runtime_fingerprint="1" * 64,
            authority=OneShotAuthority(
                "smart_reviewer", fail_after_consume=True
            ),
        ),
        LocalReviewAuthorityBinding(
            slot="code_worker",
            provider="openai-codex",
            model="gpt-5.6-sol",
            model_family="gpt",
            runtime_fingerprint="2" * 64,
            authority=OneShotAuthority("code_worker"),
        ),
    )
    runtime._authority_bindings = authorities
    runtime._reviewer_bindings = tuple(
        ReviewerBinding(
            slot=item.slot,
            provider=item.provider,
            model=item.model,
            model_family=item.model_family,
        )
        for item in authorities
    )

    result = _run(agent, scope="src/objective.py")

    assert result["completed"] is True
    assert result["review_state"] == "passed"
    assert [slot for slot, _authority_id in calls] == [
        "smart_reviewer",
        "smart_reviewer",
        "code_worker",
    ]
    assert len({authority_id for slot, authority_id in calls if slot == "smart_reviewer"}) == 2
    assert "smart_reviewer" in clones
    assert _event_kinds(home / "state.db").count("reviewer_failure") == 1


def test_manual_review_retries_a_no_progress_repair_until_it_changes_the_target(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    (repo / "src/objective.py").write_text(
        "def value():\n" + unsafe_line,
        encoding="utf-8",
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)

    class NoProgressThenRepairRuntime(_ManualRuntime):
        def repair_generation(self, *, blockers, allowed_paths, generation, **kwargs):
            if not self.repair_calls:
                self.repair_calls.append(
                    {
                        "blockers": tuple(blockers),
                        "allowed_paths": tuple(allowed_paths),
                        "generation": generation,
                    }
                )
                return {
                    "status": "applied",
                    "changed_paths": [self.blocking_path],
                }
            return super().repair_generation(
                blockers=blockers,
                allowed_paths=allowed_paths,
                generation=generation,
                **kwargs,
            )

    runtime = NoProgressThenRepairRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
        repaired_text='def value():\n    return "fixed"\n',
    )

    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        scope="src/objective.py",
    )

    assert result["completed"] is True
    assert len(runtime.repair_calls) == 2
    assert len(runtime.check_calls) == 1
    assert (repo / "src/objective.py").read_text(encoding="utf-8") == (
        'def value():\n    return "fixed"\n'
    )
    assert "repair_no_progress" in _event_kinds(home / "state.db")


def test_manual_review_repairs_again_after_a_deterministic_check_failure(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    (repo / "src/objective.py").write_text(
        "def value():\n" + unsafe_line,
        encoding="utf-8",
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)

    class CheckFailureThenRepairRuntime(_ManualRuntime):
        def repair_generation(self, *, blockers, allowed_paths, generation, **_kwargs):
            self.repair_calls.append(
                {
                    "blockers": tuple(blockers),
                    "allowed_paths": tuple(allowed_paths),
                    "generation": generation,
                }
            )
            replacement = "intermediate" if len(self.repair_calls) == 1 else "fixed"
            (self.workspace / self.blocking_path).write_text(
                f'def value():\n    return "{replacement}"\n',
                encoding="utf-8",
            )
            return {
                "status": "applied",
                "changed_paths": [self.blocking_path],
            }

        def run_checks(self, *, changed_paths, generation, **kwargs):
            if not self.check_calls:
                self.check_calls.append(
                    {
                        "changed_paths": tuple(changed_paths),
                        "generation": generation,
                    }
                )
                return {
                    "status": "failed",
                    "reason": "the deterministic objective check failed",
                }
            return super().run_checks(
                changed_paths=changed_paths,
                generation=generation,
                **kwargs,
            )

    runtime = CheckFailureThenRepairRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
    )

    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        scope="src/objective.py",
    )

    assert result["completed"] is True
    assert len(runtime.repair_calls) == 2
    assert len(runtime.check_calls) == 2
    assert len(runtime.reviewer_requests) == 4
    assert (repo / "src/objective.py").read_text(encoding="utf-8") == (
        'def value():\n    return "fixed"\n'
    )
    assert "checks_failed" in _event_kinds(home / "state.db")


def test_manual_review_persists_cancellation_before_dispatching_another_reviewer(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    (repo / "src/objective.py").write_text(
        "VALUE = 'ready-for-review'\n", encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)

    class InterruptAfterFirstReviewerRuntime(_ManualRuntime):
        agent = None

        def reviewer_call(self, binding, request):
            raw = super().reviewer_call(binding, request)
            if len(self.reviewer_requests) == 1:
                self.agent._interrupt_requested = True
                self.agent._hard_interrupt_requested.set()
            return raw

    runtime = InterruptAfterFirstReviewerRuntime(workspace=repo)
    agent = _agent(database, session_id="manual-session", runtime=runtime)
    agent._interrupt_requested = False
    agent._hard_interrupt_requested = threading.Event()
    runtime.agent = agent

    result = _run(agent, scope="src/objective.py")

    assert result["completed"] is False
    assert result["interrupted"] is True
    assert result["review_state"] == "cancelled"
    assert [item[0] for item in runtime.reviewer_requests] == ["smart_reviewer"]
    row = _job_row(home / "state.db")
    assert row["cancel_requested"] == 1
    assert row["state"] == "cancelled"
    assert "cancelled" in _event_kinds(home / "state.db")
    assert _pass_count(home / "state.db") == 0


def test_manual_review_cancel_during_repair_signals_child_and_never_applies(
    tmp_path,
    monkeypatch,
):
    """Cancellation must reach a blocked repair, not wait for it to return."""

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    unsafe_text = "def value():\n" + unsafe_line
    (repo / "src/objective.py").write_text(unsafe_text, encoding="utf-8")
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    repair_started = threading.Event()
    child_signalled = threading.Event()

    class MissingRepairCancellation(BaseException):
        pass

    class BlockingRepairRuntime(_ManualRuntime):
        def repair_generation(self, **kwargs):
            self.repair_calls.append(dict(kwargs))
            repair_started.set()
            cancel_event = kwargs.get("cancel_event")
            if not isinstance(cancel_event, threading.Event):
                raise MissingRepairCancellation(
                    "manual repair did not receive the host cancellation event"
                )
            if not cancel_event.wait(2):
                raise MissingRepairCancellation(
                    "manual repair child was not signalled within two seconds"
                )
            child_signalled.set()
            return {"status": "cancelled", "changed_paths": []}

    runtime = BlockingRepairRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
    )
    agent = _agent(database, session_id="manual-session", runtime=runtime)
    agent._interrupt_requested = False
    agent._hard_interrupt_requested = threading.Event()

    def interrupt_repair():
        assert repair_started.wait(2)
        agent._interrupt_requested = True

    interrupter = threading.Thread(target=interrupt_repair, daemon=True)
    interrupter.start()
    result = _run(agent, scope="src/objective.py")
    interrupter.join(timeout=2)

    assert child_signalled.is_set()
    assert result["completed"] is False
    assert result["interrupted"] is True
    assert result["review_state"] == "cancelled"
    assert (repo / "src/objective.py").read_text(encoding="utf-8") == unsafe_text
    row = _job_row(home / "state.db")
    assert row["cancel_requested"] == 1
    assert row["state"] == "cancelled"
    assert "cancelled" in _event_kinds(home / "state.db")
    assert _pass_count(home / "state.db") == 0


def test_manual_cancel_reports_pending_until_noncooperative_child_is_extinct(
    tmp_path,
    monkeypatch,
):
    from agent import review_engine

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    unsafe_text = "def value():\n" + unsafe_line
    (repo / "src/objective.py").write_text(unsafe_text, encoding="utf-8")
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    repair_started = threading.Event()
    release_repair = threading.Event()
    child_extinct = threading.Event()

    class NoncooperativeRepairRuntime(_ManualRuntime):
        def repair_generation(self, **kwargs):
            self.repair_calls.append(dict(kwargs))
            repair_started.set()
            try:
                assert release_repair.wait(10)
                return {"status": "cancelled", "changed_paths": []}
            finally:
                child_extinct.set()

    runtime = NoncooperativeRepairRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
    )
    agent = _agent(database, session_id="manual-session", runtime=runtime)
    agent._interrupt_requested = False
    agent._hard_interrupt_requested = threading.Event()
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def run_review():
        try:
            results.append(_run(agent, scope="src/objective.py"))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    invocation = threading.Thread(target=run_review, daemon=True)
    invocation.start()
    assert repair_started.wait(2)
    agent._interrupt_requested = True
    invocation.join(timeout=3)

    assert errors == []
    assert not invocation.is_alive()
    assert child_extinct.is_set() is False
    assert len(results) == 1
    pending = results[0]
    assert pending["completed"] is False
    assert pending["interrupted"] is True
    assert pending["review_state"] == "cancel_requested"
    assert "cancellation was requested" in pending["final_response"].lower()
    assert "waiting" in pending["final_response"].lower()
    assert "was cancelled" not in pending["final_response"].lower()
    assert _job_row(home / "state.db")["state"] == "cancel_requested"
    assert "cancelled" not in _event_kinds(home / "state.db")

    release_repair.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if _job_row(home / "state.db")["state"] == "cancelled":
            break
        time.sleep(0.005)

    assert child_extinct.is_set()
    assert _job_row(home / "state.db")["state"] == "cancelled"
    assert "cancelled" in _event_kinds(home / "state.db")
    terminal = _run(agent, scope="src/objective.py")
    assert terminal["review_state"] == "cancelled"
    assert "was cancelled" in terminal["final_response"].lower()


def test_manual_cancel_extinction_retries_transient_finalization_failure(
    tmp_path,
    monkeypatch,
):
    from agent import review_engine

    store = review_engine.ReviewStore(tmp_path / "state.db")
    job_id = "manual-cancel-finalize-retry"
    store.create_job(
        job_id=job_id,
        source_kind="manual_snapshot",
        source_id="manual-cancel-finalize-snapshot",
        target_digest="1" * 64,
        policy_digest="2" * 64,
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
    )
    owner_id = "manual-cancel-finalize-owner"
    claim = store.claim_job(
        job_id=job_id,
        owner_id=owner_id,
        now_ns=time.time_ns(),
        lease_duration_ns=10_000_000_000,
        expected_fencing_token=0,
    )
    monkeypatch.setattr(
        review_engine,
        "_MANUAL_REVIEW_CONTROL_POLL_SECONDS",
        0.005,
        raising=False,
    )
    monkeypatch.setattr(
        review_engine,
        "_MANUAL_CANCEL_EXTINCTION_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(
        review_engine,
        "_MANUAL_CANCEL_FINALIZE_RETRY_BASE_SECONDS",
        0.005,
        raising=False,
    )
    child_started = threading.Event()
    release_child = threading.Event()
    child_extinct = threading.Event()
    finalize_calls = 0
    real_finalize = review_engine.ReviewStore.finalize_cancel

    def fail_first_finalize(self, **kwargs):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            raise sqlite3.OperationalError("transient finalize failure")
        return real_finalize(self, **kwargs)

    monkeypatch.setattr(
        review_engine.ReviewStore,
        "finalize_cancel",
        fail_first_finalize,
    )
    agent = SimpleNamespace(
        _interrupt_requested=False,
        _hard_interrupt_requested=threading.Event(),
    )
    outcomes: list[BaseException] = []

    def noncooperative_child():
        child_started.set()
        try:
            assert release_child.wait(5)
        finally:
            child_extinct.set()

    def run_controlled_call():
        try:
            review_engine._manual_controlled_call(
                agent=agent,
                store=store,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=claim.fencing_token,
                cancel_operation_id="manual-cancel-finalize-requested",
                cancel_event=threading.Event(),
                adoption_lock=threading.Lock(),
                call=noncooperative_child,
            )
        except BaseException as exc:  # asserted below
            outcomes.append(exc)

    invocation = threading.Thread(target=run_controlled_call, daemon=True)
    invocation.start()
    assert child_started.wait(2)
    agent._interrupt_requested = True
    invocation.join(timeout=2)

    assert not invocation.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], review_engine._ManualInvocationCancelled)
    assert store.get_job(job_id).state == "cancel_requested"
    assert finalize_calls == 0

    release_child.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if store.get_job(job_id).state == "cancelled":
            break
        time.sleep(0.005)

    assert child_extinct.is_set()
    assert store.get_job(job_id).state == "cancelled"
    assert finalize_calls == 2


def test_manual_cancel_retries_transient_request_write_before_returning(
    tmp_path,
    monkeypatch,
):
    from agent import review_engine

    store = review_engine.ReviewStore(tmp_path / "state.db")
    job_id = "manual-cancel-request-retry"
    store.create_job(
        job_id=job_id,
        source_kind="manual_snapshot",
        source_id="manual-cancel-request-snapshot",
        target_digest="1" * 64,
        policy_digest="2" * 64,
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
    )
    owner_id = "manual-cancel-request-owner"
    claim = store.claim_job(
        job_id=job_id,
        owner_id=owner_id,
        now_ns=time.time_ns(),
        lease_duration_ns=10_000_000_000,
        expected_fencing_token=0,
    )
    monkeypatch.setattr(
        review_engine,
        "_MANUAL_REVIEW_CONTROL_POLL_SECONDS",
        0.005,
        raising=False,
    )
    monkeypatch.setattr(
        review_engine,
        "_MANUAL_CANCEL_REQUEST_RETRY_BASE_SECONDS",
        0.005,
        raising=False,
    )
    request_calls = 0
    real_request = review_engine.ReviewStore.request_manual_cancel_intent

    def fail_first_request(self, **kwargs):
        nonlocal request_calls
        request_calls += 1
        if request_calls == 1:
            raise sqlite3.OperationalError("transient cancel write failure")
        return real_request(self, **kwargs)

    monkeypatch.setattr(
        review_engine.ReviewStore,
        "request_manual_cancel_intent",
        fail_first_request,
    )
    agent = SimpleNamespace(
        _interrupt_requested=False,
        _hard_interrupt_requested=threading.Event(),
    )
    child_started = threading.Event()
    child_cancelled = threading.Event()
    outcomes: list[BaseException] = []

    def child():
        child_started.set()
        assert child_cancelled.wait(2)

    def run_controlled_call():
        try:
            review_engine._manual_controlled_call(
                agent=agent,
                store=store,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=claim.fencing_token,
                cancel_operation_id="manual-cancel-requested",
                cancel_event=child_cancelled,
                adoption_lock=threading.Lock(),
                call=child,
            )
        except BaseException as exc:
            outcomes.append(exc)

    invocation = threading.Thread(target=run_controlled_call, daemon=True)
    invocation.start()
    assert child_started.wait(2)
    agent._interrupt_requested = True
    invocation.join(timeout=2)

    assert not invocation.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], review_engine._ManualInvocationCancelled)
    assert request_calls == 2
    durable = store.get_job(job_id)
    assert durable.cancel_requested is True
    assert durable.state == "cancelled"


def test_manual_cancel_intent_survives_an_expired_owner_fence_takeover(tmp_path):
    """A stop targets the immutable job, not an owner lease that can expire."""

    from agent import review_engine

    store = review_engine.ReviewStore(tmp_path / "state.db")
    job_id = "manual-cancel-fence-takeover"
    target_digest = "1" * 64
    store.create_job(
        job_id=job_id,
        source_kind="manual_snapshot",
        source_id="manual-cancel-fence-snapshot",
        target_digest=target_digest,
        policy_digest="2" * 64,
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
    )
    first = store.claim_job(
        job_id=job_id,
        owner_id="manual-owner-1",
        now_ns=1_000_000,
        lease_duration_ns=10,
        expected_fencing_token=0,
    )
    second = store.claim_job(
        job_id=job_id,
        owner_id="manual-owner-2",
        now_ns=1_000_011,
        lease_duration_ns=10_000,
        expected_fencing_token=first.fencing_token,
    )
    signalled: list[bool] = []

    cancelled = store.request_manual_cancel_intent(
        job_id=job_id,
        expected_target_digest=target_digest,
        operation_id="manual-cancel-after-takeover",
        signal_children=lambda: signalled.append(
            store.get_job(job_id).cancel_requested
        ),
    )

    assert cancelled.owner_id == second.owner_id
    assert cancelled.fencing_token == second.fencing_token
    assert cancelled.cancel_requested is True
    assert cancelled.state == "cancel_requested"
    assert signalled == [True]
    with pytest.raises(review_engine.ReviewLeaseConflict):
        store.renew_lease(
            job_id=job_id,
            owner_id=second.owner_id,
            fencing_token=second.fencing_token,
            now_ns=1_000_012,
            lease_duration_ns=10_000,
        )
    resumed_model_calls = 0

    def resumed_model_call():
        nonlocal resumed_model_calls
        resumed_model_calls += 1

    with pytest.raises(review_engine._ManualInvocationCancelled):
        review_engine._manual_controlled_call(
            agent=SimpleNamespace(
                _interrupt_requested=False,
                _hard_interrupt_requested=threading.Event(),
            ),
            store=store,
            job_id=job_id,
            owner_id=second.owner_id,
            fencing_token=second.fencing_token,
            cancel_operation_id="manual-cancel-owner-2",
            cancel_event=threading.Event(),
            adoption_lock=threading.Lock(),
            call=resumed_model_call,
        )
    assert resumed_model_calls == 0


def test_manual_cancel_retry_blocks_a_reclaimed_owner_before_model_work(
    tmp_path,
    monkeypatch,
):
    """A reclaimed fence cannot enter its host effect during cancel backoff."""

    from agent import review_engine

    store = review_engine.ReviewStore(tmp_path / "state.db")
    job_id = "manual-cancel-reclaim-interleaving"
    store.create_job(
        job_id=job_id,
        source_kind="manual_snapshot",
        source_id="manual-cancel-reclaim-snapshot",
        target_digest="1" * 64,
        policy_digest="2" * 64,
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
    )
    first = store.claim_job(
        job_id=job_id,
        owner_id="manual-owner-1",
        now_ns=time.time_ns(),
        lease_duration_ns=1_000_000_000,
        expected_fencing_token=0,
    )
    monkeypatch.setattr(review_engine, "_MANUAL_REVIEW_LEASE_NS", 20_000_000)
    monkeypatch.setattr(
        review_engine, "_MANUAL_REVIEW_CONTROL_POLL_SECONDS", 0.001
    )
    monkeypatch.setattr(
        review_engine, "_MANUAL_CANCEL_REQUEST_RETRY_BASE_SECONDS", 0.1
    )
    monkeypatch.setattr(
        review_engine, "_MANUAL_CANCEL_REQUEST_RETRY_MAX_SECONDS", 0.1
    )
    first_cancel_failed = threading.Event()
    request_calls = 0
    real_request = review_engine.ReviewStore.request_manual_cancel_intent

    def fail_first_request(self, **kwargs):
        nonlocal request_calls
        request_calls += 1
        if request_calls == 1:
            first_cancel_failed.set()
            raise sqlite3.OperationalError("transient cancel write failure")
        return real_request(self, **kwargs)

    monkeypatch.setattr(
        review_engine.ReviewStore,
        "request_manual_cancel_intent",
        fail_first_request,
    )
    first_agent = SimpleNamespace(
        _interrupt_requested=False,
        _hard_interrupt_requested=threading.Event(),
    )
    first_child_started = threading.Event()
    first_child_cancelled = threading.Event()
    first_outcomes: list[BaseException] = []

    def first_child():
        first_child_started.set()
        assert first_child_cancelled.wait(2)

    def run_first():
        try:
            review_engine._manual_controlled_call(
                agent=first_agent,
                store=store,
                job_id=job_id,
                owner_id="manual-owner-1",
                fencing_token=first.fencing_token,
                cancel_operation_id="manual-cancel-owner-1",
                cancel_event=first_child_cancelled,
                adoption_lock=threading.Lock(),
                call=first_child,
            )
        except BaseException as exc:
            first_outcomes.append(exc)

    first_thread = threading.Thread(target=run_first, daemon=True)
    first_thread.start()
    assert first_child_started.wait(2)
    first_agent._interrupt_requested = True
    assert first_cancel_failed.wait(2)
    time.sleep(0.03)

    second_store = review_engine.ReviewStore(tmp_path / "state.db")
    second = second_store.claim_job(
        job_id=job_id,
        owner_id="manual-owner-2",
        now_ns=time.time_ns(),
        lease_duration_ns=1_000_000_000,
        expected_fencing_token=first.fencing_token,
    )
    second_model_started = threading.Event()
    second_outcomes: list[BaseException] = []

    def run_second():
        try:
            review_engine._manual_controlled_call(
                agent=SimpleNamespace(
                    _interrupt_requested=False,
                    _hard_interrupt_requested=threading.Event(),
                ),
                store=second_store,
                job_id=job_id,
                owner_id="manual-owner-2",
                fencing_token=second.fencing_token,
                cancel_operation_id="manual-cancel-owner-2",
                cancel_event=threading.Event(),
                adoption_lock=threading.Lock(),
                call=second_model_started.set,
            )
        except BaseException as exc:
            second_outcomes.append(exc)

    second_thread = threading.Thread(target=run_second, daemon=True)
    second_thread.start()
    assert not second_model_started.wait(0.04)

    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert request_calls >= 2
    assert second_model_started.is_set() is False
    assert all(
        isinstance(item, review_engine._ManualInvocationCancelled)
        for item in (*first_outcomes, *second_outcomes)
    )
    assert len(first_outcomes) == len(second_outcomes) == 1
    durable = store.get_job(job_id)
    assert durable.cancel_requested is True
    assert durable.state == "cancelled"


def test_external_durable_cancel_during_repair_finalizes_after_child_extinction(
    tmp_path,
    monkeypatch,
):
    """A cancel written by another host must reach the live manual child."""

    from agent import review_engine

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    unsafe_text = "def value():\n" + unsafe_line
    (repo / "src/objective.py").write_text(unsafe_text, encoding="utf-8")
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    monkeypatch.setattr(
        review_engine, "_MANUAL_REVIEW_HEARTBEAT_SECONDS", 0.01, raising=False
    )
    repair_started = threading.Event()
    child_extinct = threading.Event()
    external_errors: list[BaseException] = []

    class BlockingRepairRuntime(_ManualRuntime):
        def repair_generation(self, **kwargs):
            repair_started.set()
            cancel_event = kwargs["cancel_event"]
            try:
                assert cancel_event.wait(2)
                return {"status": "cancelled", "changed_paths": []}
            finally:
                child_extinct.set()

    runtime = BlockingRepairRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
    )

    def cancel_from_store():
        try:
            assert repair_started.wait(2)
            store = review_engine.ReviewStore(home / "state.db")
            active = store.find_active_bestplan_job(
                owner_session_id="manual-session",
                owner_profile="manual-profile",
                workspace=repo,
            )
            assert active is None
            rows = sqlite3.connect(home / "state.db").execute(
                "SELECT job_id, owner_id, fencing_token FROM review_jobs"
            ).fetchall()
            assert len(rows) == 1
            job_id, owner_id, fencing_token = rows[0]
            store.request_cancel(
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id="external-manual-cancel",
                signal_children=lambda: None,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            external_errors.append(exc)

    canceller = threading.Thread(target=cancel_from_store, daemon=True)
    canceller.start()
    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        scope="src/objective.py",
    )
    canceller.join(timeout=2)

    assert external_errors == []
    assert child_extinct.is_set()
    assert result["completed"] is False
    assert result["interrupted"] is True
    assert result["review_state"] == "cancelled"
    assert (repo / "src/objective.py").read_text(encoding="utf-8") == unsafe_text
    assert _job_row(home / "state.db")["state"] == "cancelled"
    assert "cancelled" in _event_kinds(home / "state.db")


def test_external_cancel_after_host_call_completion_is_finalized(
    tmp_path,
    monkeypatch,
):
    """A cancel that wins the post-call race must not remain pending forever."""

    from agent import review_engine

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    unsafe_text = "def value():\n" + unsafe_line
    (repo / "src/objective.py").write_text(unsafe_text, encoding="utf-8")
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)

    class CancelsBeforeReturningRuntime(_ManualRuntime):
        def repair_generation(self, **_kwargs):
            connection = sqlite3.connect(home / "state.db")
            try:
                job_id, owner_id, fencing_token = connection.execute(
                    "SELECT job_id, owner_id, fencing_token FROM review_jobs"
                ).fetchone()
            finally:
                connection.close()
            review_engine.ReviewStore(home / "state.db").request_cancel(
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=fencing_token,
                operation_id="external-post-call-cancel",
                signal_children=lambda: None,
            )
            return {"status": "cancelled", "changed_paths": []}

    result = _run(
        _agent(
            database,
            session_id="manual-session",
            runtime=CancelsBeforeReturningRuntime(
                workspace=repo,
                blocking_path="src/objective.py",
                unsafe_line=unsafe_line,
            ),
        ),
        scope="src/objective.py",
    )

    assert result["completed"] is False
    assert result["interrupted"] is True
    assert result["review_state"] == "cancelled"
    assert (repo / "src/objective.py").read_text(encoding="utf-8") == unsafe_text
    assert _job_row(home / "state.db")["state"] == "cancelled"


def test_concurrent_manual_review_invocations_never_share_one_owner_lease(
    tmp_path,
    monkeypatch,
):
    """An active invocation must fence a second invocation for the same job."""

    from agent import review_engine

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    (repo / "src/objective.py").write_text(
        "VALUE = 'ready-for-review'\n", encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    first_reviewer_started = threading.Event()
    release_first_reviewer = threading.Event()

    class BlockingRuntime(_ManualRuntime):
        def reviewer_call(self, binding, request):
            if not self.reviewer_requests:
                first_reviewer_started.set()
                assert release_first_reviewer.wait(2)
            return super().reviewer_call(binding, request)

    class ForbiddenConcurrentRuntime(_ManualRuntime):
        def reviewer_call(self, binding, request):
            raise AssertionError("the fenced invocation dispatched a reviewer")

    first_runtime = BlockingRuntime(workspace=repo)
    second_runtime = ForbiddenConcurrentRuntime(workspace=repo)
    first_results: list[dict[str, object]] = []
    first_errors: list[BaseException] = []
    claimed_owners: list[str] = []
    real_claim = review_engine.ReviewStore.claim_job

    def record_claim(self, **kwargs):
        claimed_owners.append(str(kwargs["owner_id"]))
        return real_claim(self, **kwargs)

    monkeypatch.setattr(review_engine.ReviewStore, "claim_job", record_claim)

    def run_first():
        try:
            first_results.append(
                _run(
                    _agent(
                        database,
                        session_id="manual-session",
                        runtime=first_runtime,
                    ),
                    scope="src/objective.py",
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports below
            first_errors.append(exc)

    worker = threading.Thread(target=run_first, daemon=True)
    worker.start()
    assert first_reviewer_started.wait(2)
    second_result = _run(
        _agent(
            database,
            session_id="manual-session",
            runtime=second_runtime,
        ),
        scope="src/objective.py",
    )
    release_first_reviewer.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert first_errors == []
    assert first_results and first_results[0]["completed"] is True
    assert second_result["completed"] is False
    assert second_result["review_state"] == "reviewing"
    assert second_runtime.reviewer_requests == []
    assert len(claimed_owners) == 2
    assert claimed_owners[0] != claimed_owners[1]
    assert _pass_count(home / "state.db") == 1


@pytest.mark.parametrize("phase", ("repair", "checks"))
def test_manual_transient_worker_failure_yields_to_durable_recovery(
    tmp_path,
    monkeypatch,
    phase,
):
    """Transient repair/check failures must not create an inline retry storm."""

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    (repo / "src/objective.py").write_text(
        "def value():\n" + unsafe_line, encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)

    class WaitingRuntime(_ManualRuntime):
        def repair_generation(self, **kwargs):
            self.repair_calls.append(dict(kwargs))
            if len(self.repair_calls) > 1:
                raise AssertionError("manual repair retried inline")
            if phase == "repair":
                return {"status": "waiting", "changed_paths": []}
            (self.workspace / "src/objective.py").write_text(
                'def value():\n    return "fixed"\n', encoding="utf-8"
            )
            return {"status": "applied", "changed_paths": ["src/objective.py"]}

        def run_checks(self, **kwargs):
            self.check_calls.append(dict(kwargs))
            if len(self.check_calls) > 1:
                raise AssertionError("manual checks retried inline")
            if phase == "checks":
                return {"status": "waiting"}
            return super().run_checks(**kwargs)

    runtime = WaitingRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
    )
    enqueued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "agent.review_engine._enqueue_manual_review_resume",
        lambda *, state_db_path, job_id: enqueued.append(
            (str(state_db_path), job_id)
        ),
    )

    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        scope="src/objective.py",
    )

    assert result["completed"] is False
    assert result["review_state"] == (
        "checking" if phase == "checks" else "waiting"
    )
    assert len(runtime.repair_calls) == 1
    assert len(runtime.check_calls) == (1 if phase == "checks" else 0)
    assert len(enqueued) == 1
    job = _job_row(home / "state.db")
    assert job["owner_id"] is None
    assert job["lease_expires_at_ns"] is None


def test_manual_review_renews_its_lease_while_a_reviewer_is_blocked(
    tmp_path,
    monkeypatch,
):
    """A slow host call must not let another invocation reclaim live work."""

    from agent import review_engine

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    (repo / "src/objective.py").write_text(
        "VALUE = 'ready-for-review'\n", encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    monkeypatch.setattr(
        review_engine, "_MANUAL_REVIEW_LEASE_NS", 200_000_000, raising=False
    )
    monkeypatch.setattr(
        review_engine, "_MANUAL_REVIEW_HEARTBEAT_SECONDS", 0.02, raising=False
    )
    renewals: list[tuple[str, int]] = []
    real_renew = review_engine.ReviewStore.renew_lease

    def record_renewal(self, **kwargs):
        renewals.append((str(kwargs["owner_id"]), int(kwargs["fencing_token"])))
        return real_renew(self, **kwargs)

    monkeypatch.setattr(review_engine.ReviewStore, "renew_lease", record_renewal)

    class SlowReviewerRuntime(_ManualRuntime):
        def reviewer_call(self, binding, request):
            if not self.reviewer_requests:
                time.sleep(0.09)
            return super().reviewer_call(binding, request)

    result = _run(
        _agent(
            database,
            session_id="manual-session",
            runtime=SlowReviewerRuntime(workspace=repo),
        ),
        scope="src/objective.py",
    )

    assert result["completed"] is True
    assert len(renewals) >= 2
    assert len({owner for owner, _token in renewals}) == 1
    assert len({token for _owner, token in renewals}) == 1


def test_manual_heartbeat_loss_extinguishes_repair_before_any_retry(
    tmp_path,
    monkeypatch,
):
    """A fenced repair child must stop before the invocation can dispatch again."""

    from agent import review_engine

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    unsafe_text = "def value():\n" + unsafe_line
    (repo / "src/objective.py").write_text(unsafe_text, encoding="utf-8")
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    monkeypatch.setattr(
        review_engine, "_MANUAL_REVIEW_HEARTBEAT_SECONDS", 0.01, raising=False
    )
    child_started = threading.Event()
    child_extinct = threading.Event()
    real_renew_lease = review_engine.ReviewStore.renew_lease

    def lose_lease(self, **kwargs):
        if child_started.is_set():
            raise review_engine.ReviewLeaseConflict("simulated heartbeat loss")
        return real_renew_lease(self, **kwargs)

    monkeypatch.setattr(review_engine.ReviewStore, "renew_lease", lose_lease)

    class DelayedRepairRuntime(_ManualRuntime):
        calls = 0

        def repair_generation(self, **kwargs):
            self.calls += 1
            if self.calls > 1:
                if not child_extinct.is_set():
                    raise AssertionError("a new repair started before the fenced child stopped")
                raise AssertionError("a heartbeat-lost invocation dispatched another repair")
            try:
                child_started.set()
                assert kwargs["cancel_event"].wait(2)
                return {"status": "cancelled", "changed_paths": []}
            finally:
                child_extinct.set()

    runtime = DelayedRepairRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
    )
    result = _run(
        _agent(database, session_id="manual-session", runtime=runtime),
        scope="src/objective.py",
    )

    assert child_extinct.is_set()
    assert runtime.calls == 1
    assert result["review_state"] == "cancelled"
    assert result["interrupted"] is True
    assert (repo / "src/objective.py").read_text(encoding="utf-8") == unsafe_text
    assert _pass_count(home / "state.db") == 0


def test_manual_cancel_during_scoped_apply_is_durable_and_rolls_back(
    tmp_path,
    monkeypatch,
):
    """A cancel must persist while apply is locked, then roll back written files."""

    from agent import manual_review_runtime, review_engine

    workspace = tmp_path / "workspace"
    materialized = tmp_path / "materialized"
    (workspace / "src").mkdir(parents=True)
    (materialized / "src").mkdir(parents=True)
    originals = {
        "src/first.py": b"FIRST = 'old'\n",
        "src/second.py": b"SECOND = 'old'\n",
    }
    replacements = {
        "src/first.py": b"FIRST = 'new'\n",
        "src/second.py": b"SECOND = 'new'\n",
    }
    for relative, content in originals.items():
        (workspace / relative).write_bytes(content)
    for relative, content in replacements.items():
        (materialized / relative).write_bytes(content)

    store = review_engine.ReviewStore(tmp_path / "state.db")
    job_id = "manual-apply-cancel"
    store.create_job(
        job_id=job_id,
        source_kind="manual_snapshot",
        source_id="manual-apply-snapshot",
        target_digest="1" * 64,
        policy_digest="2" * 64,
        integration_oid="3" * 40,
        check_receipt_digest="4" * 64,
        adapter_version="manual_snapshot.v1",
        owner_session_id="manual-session",
        owner_profile="manual-profile",
        workspace=str(workspace),
        adapter_state={"schema": "manual-apply-race-test.v1"},
        runtime_routes=[],
    )
    owner_id = "manual-apply-owner"
    claim = store.claim_job(
        job_id=job_id,
        owner_id=owner_id,
        now_ns=time.time_ns(),
        lease_duration_ns=10_000_000_000,
        expected_fencing_token=0,
    )

    first_write_finished = threading.Event()
    release_first_write = threading.Event()
    child_extinct = threading.Event()
    real_atomic_write = manual_review_runtime._atomic_write
    writes: list[str] = []

    def pause_after_first_write(parent_fd, name, entry):
        real_atomic_write(parent_fd, name, entry)
        writes.append(name)
        if len(writes) == 1:
            first_write_finished.set()
            assert release_first_write.wait(2)

    monkeypatch.setattr(manual_review_runtime, "_atomic_write", pause_after_first_write)
    monkeypatch.setattr(
        review_engine, "_MANUAL_REVIEW_CONTROL_POLL_SECONDS", 0.005, raising=False
    )

    agent = SimpleNamespace(
        _interrupt_requested=False,
        _hard_interrupt_requested=threading.Event(),
    )
    cancel_event = threading.Event()
    adoption_lock = threading.Lock()
    outcomes: list[BaseException] = []

    def apply() -> tuple[str, ...]:
        try:
            return manual_review_runtime._apply_repaired_paths(
                workspace=workspace,
                materialized_root=materialized,
                prefix="",
                root_paths=tuple(sorted(originals)),
                cancel_event=cancel_event,
                adoption_lock=adoption_lock,
            )
        finally:
            child_extinct.set()

    def run_controlled_apply() -> None:
        try:
            review_engine._manual_controlled_call(
                agent=agent,
                store=store,
                job_id=job_id,
                owner_id=owner_id,
                fencing_token=claim.fencing_token,
                cancel_operation_id="manual-apply-cancel-requested",
                cancel_event=cancel_event,
                adoption_lock=adoption_lock,
                call=apply,
            )
        except BaseException as exc:  # asserted below
            outcomes.append(exc)

    worker = threading.Thread(target=run_controlled_apply, daemon=True)
    worker.start()
    assert first_write_finished.wait(2)
    agent._interrupt_requested = True

    durable_before_release = False
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if store.get_job(job_id).cancel_requested:
            durable_before_release = True
            break
        time.sleep(0.005)
    release_first_write.set()
    worker.join(timeout=3)

    assert durable_before_release is True
    assert not worker.is_alive()
    assert child_extinct.is_set()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], review_engine._ManualInvocationCancelled)
    assert store.get_job(job_id).state == "cancelled"
    assert "second.py" not in writes
    for relative, content in originals.items():
        assert (workspace / relative).read_bytes() == content


def test_manual_review_restart_resumes_the_same_job_without_replaying_a_stored_slot(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    (repo / "src/objective.py").write_text(
        "VALUE = 'ready-for-review'\n", encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    from tools import async_delegation

    recovery_queue: queue.Queue = queue.Queue()
    monkeypatch.setattr(
        async_delegation,
        "_manual_review_recovery_queue",
        recovery_queue,
        raising=False,
    )
    monkeypatch.setattr(
        async_delegation,
        "_start_manual_review_recovery_consumer",
        lambda: None,
        raising=False,
    )

    class FailSecondReviewerRuntime(_ManualRuntime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.attempts = 0

        def reviewer_call(self, binding, request):
            self.attempts += 1
            if self.attempts == 2:
                raise ConnectionError("review host restarted")
            return super().reviewer_call(binding, request)

    first_runtime = FailSecondReviewerRuntime(workspace=repo)
    first = _run(
        _agent(database, session_id="manual-session", runtime=first_runtime),
        scope="src/objective.py",
    )
    assert first["completed"] is False
    assert first["review_state"] == "waiting"

    expected_request = __import__(
        "agent.review_engine",
        fromlist=["build_manual_review_resume_request"],
    ).build_manual_review_resume_request(
        state_db_path=home / "state.db",
        job_id=first["review_job_id"],
    )
    assert list(recovery_queue.queue) == [expected_request]

    resumed_runtimes: list[_ManualRuntime] = []
    resumed_results: list[dict] = []

    def fresh_worker(request):
        from agent.review_engine import resume_manual_review_job
        from hermes_state import SessionDB

        fresh_database = SessionDB(db_path=home / "state.db")
        fresh_runtime = _ManualRuntime(workspace=repo)
        resumed_runtimes.append(fresh_runtime)
        result = resume_manual_review_job(
            _agent(
                fresh_database,
                session_id="manual-session",
                runtime=fresh_runtime,
            ),
            request,
        )
        resumed_results.append(result)
        return result

    consumed = async_delegation.consume_manual_review_recoveries(
        recovery_queue,
        worker=fresh_worker,
        max_items=1,
    )

    assert consumed == {"completed": 1, "consumed": 1, "deferred": 0}, results
    assert len(resumed_runtimes) == 1
    resumed_runtime = resumed_runtimes[0]
    resumed = resumed_results[0]
    assert resumed["completed"] is True
    assert resumed["review_job_id"] == first["review_job_id"]
    assert [item[0] for item in resumed_runtime.reviewer_requests] == [
        "code_worker"
    ]
    connection = sqlite3.connect(home / "state.db")
    try:
        assert connection.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 1
    finally:
        connection.close()


def test_manual_recovery_batch_retries_a_later_transient_after_a_completion(
    monkeypatch,
):
    from tools import async_delegation

    first = {
        "adapter_version": "manual_snapshot.v1",
        "job_id": "manual-first",
        "kind": "manual_review_resume",
        "profile": "manual-profile",
        "schema": "hermes.manual-review-resume.v1",
        "session_id": "manual-session",
        "state_db_path": "/tmp/manual-first.db",
        "workspace": "/tmp/manual-workspace",
    }
    second = {**first, "job_id": "manual-second"}
    recovery_queue: queue.Queue = queue.Queue()
    recovery_queue.put(first)
    recovery_queue.put(second)
    monkeypatch.setattr(
        async_delegation,
        "_validate_manual_review_recovery_request",
        lambda request: dict(request),
    )
    scheduled: list[dict] = []
    monkeypatch.setattr(
        async_delegation,
        "_schedule_manual_review_recovery_retry",
        lambda request, **_kwargs: scheduled.append(dict(request)),
    )

    def worker(request):
        if request["job_id"] == "manual-first":
            return {"completed": True, "review_state": "passed"}
        raise ConnectionError("transient recovery host loss")

    consumed = async_delegation.consume_manual_review_recoveries(
        recovery_queue,
        worker=worker,
        max_items=2,
    )

    assert consumed == {"completed": 1, "consumed": 2, "deferred": 1}
    assert scheduled == [second]


def test_manual_recovery_retry_ordinal_advances_and_worker_sees_exact_request(
    monkeypatch,
):
    from tools import async_delegation

    request = {
        "adapter_version": "manual_snapshot.v1",
        "job_id": "manual-retry",
        "kind": "manual_review_resume",
        "profile": "manual-profile",
        "schema": "hermes.manual-review-resume.v1",
        "session_id": "manual-session",
        "state_db_path": "/tmp/manual-retry.db",
        "workspace": "/tmp/manual-workspace",
    }
    recovery_queue: queue.Queue = queue.Queue()
    recovery_queue.put(request)
    monkeypatch.setattr(
        async_delegation,
        "_validate_manual_review_recovery_request",
        lambda candidate: dict(candidate) if dict(candidate) == request else None,
    )
    scheduled_attempts: list[int] = []

    def schedule(candidate, *, attempts):
        scheduled_attempts.append(attempts)
        recovery_queue.put({**candidate, "_transient_attempt": attempts + 1})

    monkeypatch.setattr(
        async_delegation,
        "_schedule_manual_review_recovery_retry",
        schedule,
    )
    seen: list[dict] = []

    def worker(exact_request):
        seen.append(dict(exact_request))
        raise ConnectionError("transient host loss")

    first = async_delegation.consume_manual_review_recoveries(
        recovery_queue,
        worker=worker,
        max_items=1,
    )
    second = async_delegation.consume_manual_review_recoveries(
        recovery_queue,
        worker=worker,
        max_items=1,
    )

    assert first == {"completed": 0, "consumed": 1, "deferred": 1}
    assert second == {"completed": 0, "consumed": 1, "deferred": 1}
    assert scheduled_attempts == [0, 1]
    assert seen == [request, request]


def test_manual_recovery_integrity_failure_terminalizes_and_clears_pending(
    monkeypatch,
):
    from agent.review_engine import ReviewStoreConflict
    from tools import async_delegation

    request = {
        "adapter_version": "manual_snapshot.v1",
        "job_id": "manual-corrupt",
        "kind": "manual_review_resume",
        "profile": "manual-profile",
        "schema": "hermes.manual-review-resume.v1",
        "session_id": "manual-session",
        "state_db_path": "/tmp/manual-corrupt.db",
        "workspace": "/tmp/manual-workspace",
    }
    key = (request["state_db_path"], request["job_id"])
    recovery_queue: queue.Queue = queue.Queue()
    recovery_queue.put(request)
    async_delegation._manual_review_recovery_pending.add(key)
    monkeypatch.setattr(
        async_delegation,
        "_validate_manual_review_recovery_request",
        lambda candidate: dict(candidate),
    )
    terminalized: list[dict] = []
    scheduled: list[dict] = []
    monkeypatch.setattr(
        async_delegation,
        "_terminalize_manual_review_integrity_failure",
        lambda candidate, exc: terminalized.append(dict(candidate)) or True,
        raising=False,
    )
    monkeypatch.setattr(
        async_delegation,
        "_schedule_manual_review_recovery_retry",
        lambda candidate, **_kwargs: scheduled.append(dict(candidate)),
    )

    consumed = async_delegation.consume_manual_review_recoveries(
        recovery_queue,
        worker=lambda _request: (_ for _ in ()).throw(
            ReviewStoreConflict("corrupt checkpoint")
        ),
        max_items=1,
    )

    assert consumed == {"completed": 0, "consumed": 1, "deferred": 1}
    assert terminalized == [request]
    assert scheduled == []
    assert key not in async_delegation._manual_review_recovery_pending


def test_manual_recovery_retries_when_integrity_terminalization_fails(
    monkeypatch,
):
    from agent.review_engine import ReviewStoreConflict
    from tools import async_delegation

    request = {
        "adapter_version": "manual_snapshot.v1",
        "job_id": "manual-corrupt-retry",
        "kind": "manual_review_resume",
        "profile": "manual-profile",
        "schema": "hermes.manual-review-resume.v1",
        "session_id": "manual-session",
        "state_db_path": "/tmp/manual-corrupt-retry.db",
        "workspace": "/tmp/manual-workspace",
    }
    key = (request["state_db_path"], request["job_id"])
    recovery_queue: queue.Queue = queue.Queue()
    recovery_queue.put(request)
    monkeypatch.setattr(
        async_delegation,
        "_validate_manual_review_recovery_request",
        lambda candidate: dict(candidate),
    )
    monkeypatch.setattr(
        async_delegation,
        "_terminalize_manual_review_integrity_failure",
        lambda _candidate, _exc: False,
    )
    scheduled: list[dict] = []
    monkeypatch.setattr(
        async_delegation,
        "_schedule_manual_review_recovery_retry",
        lambda candidate, **_kwargs: scheduled.append(dict(candidate)),
    )

    try:
        consumed = async_delegation.consume_manual_review_recoveries(
            recovery_queue,
            worker=lambda _request: (_ for _ in ()).throw(
                ReviewStoreConflict("corrupt checkpoint")
            ),
            max_items=1,
        )

        assert consumed == {"completed": 0, "consumed": 1, "deferred": 1}
        assert scheduled == [request]
        assert key in async_delegation._manual_review_recovery_pending
    finally:
        async_delegation._manual_review_recovery_pending.discard(key)


def test_manual_startup_recovery_scans_only_the_configured_profile_state(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import profiles
    from tools import async_delegation

    state_db_path = tmp_path / "configured-profile" / "state.db"
    state_db_path.parent.mkdir()
    state_db_path.touch()
    completion_queue: queue.Queue = queue.Queue()
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        async_delegation,
        "recover_async_delegations",
        lambda **_kwargs: {"queued": 0, "lost": 0},
    )
    monkeypatch.setattr(
        async_delegation,
        "_start_bestplan_review_recovery_consumer",
        lambda: None,
    )
    monkeypatch.setattr(
        async_delegation,
        "_restore_sqlite_undelivered",
        lambda _queue: 0,
    )
    monkeypatch.setattr(async_delegation, "_db_path", lambda: state_db_path)
    monkeypatch.setattr(
        profiles,
        "get_active_profile_name",
        lambda: "manual-profile",
    )

    def recover_manual(*, state_db_path, profile, **_kwargs):
        calls.append((Path(state_db_path), profile))
        return {"queued": 0}

    monkeypatch.setattr(
        async_delegation,
        "recover_manual_review_jobs",
        recover_manual,
    )

    assert async_delegation.restore_undelivered_completions(
        completion_queue
    ) == 0
    assert calls == [(state_db_path.resolve(), "manual-profile")]


def _expire_manual_recovery_lease(database_path: Path, job_id: str) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "UPDATE review_jobs SET lease_expires_at_ns=0 WHERE job_id=?",
            (job_id,),
        )
        connection.commit()
    finally:
        connection.close()


def test_manual_startup_finalizes_expired_cancel_without_queuing_model_work(
    tmp_path,
    monkeypatch,
):
    from agent import review_engine
    from tools import async_delegation

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    (repo / "src/objective.py").write_text(
        "VALUE = 'ready-for-review'\n", encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    monkeypatch.setattr(
        review_engine,
        "_enqueue_manual_review_resume",
        lambda **_kwargs: True,
    )

    class OfflineReviewerRuntime(_ManualRuntime):
        def reviewer_call(self, _binding, _request):
            raise ConnectionError("review host stopped before restart")

    first = _run(
        _agent(
            database,
            session_id="manual-session",
            runtime=OfflineReviewerRuntime(workspace=repo),
        ),
        scope="src/objective.py",
    )
    assert first["completed"] is False
    assert first["review_state"] == "waiting"

    job_id = str(first["review_job_id"])
    store = review_engine.ReviewStore(home / "state.db")
    waiting = store.get_job(job_id)
    claim = store.claim_job(
        job_id=job_id,
        owner_id="manual-owner-that-exited",
        now_ns=time.time_ns(),
        lease_duration_ns=10_000_000_000,
        expected_fencing_token=waiting.fencing_token,
    )
    store.request_cancel(
        job_id=job_id,
        owner_id="manual-owner-that-exited",
        fencing_token=claim.fencing_token,
        operation_id="manual-cancel-before-process-exit",
        signal_children=lambda: None,
    )
    _expire_manual_recovery_lease(home / "state.db", job_id)

    recovery_queue: queue.Queue = queue.Queue()
    worker_calls: list[dict[str, str]] = []
    recovered = async_delegation.recover_manual_review_jobs(
        state_db_path=home / "state.db",
        profile="manual-profile",
        recovery_queue=recovery_queue,
    )
    consumed = async_delegation.consume_manual_review_recoveries(
        recovery_queue,
        worker=lambda request: worker_calls.append(dict(request)) or {},
        max_items=1,
    )

    assert recovered == {"queued": 0}
    assert consumed == {"completed": 0, "consumed": 0, "deferred": 0}
    assert worker_calls == []
    assert store.get_job(job_id).state == "cancelled"
    assert _event_kinds(home / "state.db").count("cancelled") == 1

    # The now-terminal job stays outside all later startup recovery scans.
    assert async_delegation.recover_manual_review_jobs(
        state_db_path=home / "state.db",
        profile="manual-profile",
        recovery_queue=recovery_queue,
    ) == {"queued": 0}
    assert recovery_queue.empty()
    assert _event_kinds(home / "state.db").count("cancelled") == 1


def _recover_one_manual_job(
    *,
    home: Path,
    job_id: str,
    runtime: _ManualRuntime,
):
    from agent.review_engine import resume_manual_review_job
    from hermes_state import SessionDB
    from tools import async_delegation

    recovery_queue: queue.Queue = queue.Queue()
    recovered = async_delegation.recover_manual_review_jobs(
        state_db_path=home / "state.db",
        profile="manual-profile",
        recovery_queue=recovery_queue,
    )
    assert recovered == {"queued": 1}
    request = recovery_queue.get_nowait()
    assert request["job_id"] == job_id
    recovery_queue.put(request)
    results: list[dict] = []

    def worker(exact_request):
        fresh_database = SessionDB(db_path=home / "state.db")
        try:
            result = resume_manual_review_job(
                _agent(
                    fresh_database,
                    session_id="manual-session",
                    runtime=runtime,
                ),
                exact_request,
            )
        except Exception as exc:
            results.append({"error": repr(exc)})
            raise
        results.append(result)
        return result

    consumed = async_delegation.consume_manual_review_recoveries(
        recovery_queue,
        worker=worker,
        max_items=1,
    )
    assert consumed == {"completed": 1, "consumed": 1, "deferred": 0}, results
    assert len(results) == 1
    return results[0]


def test_manual_startup_recovers_crash_after_initial_persist_before_claim(
    tmp_path,
    monkeypatch,
):
    """Generation zero must exist whenever a new manual job becomes visible."""

    from agent.review_engine import ReviewStore

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "repo", {"src/objective.py": "VALUE = 'base'\n"})
    (repo / "src/objective.py").write_text(
        "VALUE = 'ready-for-review'\n", encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)

    class SimulatedProcessLoss(BaseException):
        pass

    real_claim = ReviewStore.claim_job

    def crash_before_claim(_store, **_kwargs):
        raise SimulatedProcessLoss("process exited before the first claim")

    monkeypatch.setattr(ReviewStore, "claim_job", crash_before_claim)
    with pytest.raises(SimulatedProcessLoss, match="before the first claim"):
        _run(
            _agent(
                database,
                session_id="manual-session",
                runtime=_ManualRuntime(workspace=repo),
            ),
            scope="src/objective.py",
        )

    orphan = _job_row(home / "state.db")
    assert orphan["state"] == "reviewing"
    assert orphan["current_generation"] == 0
    assert orphan["owner_id"] is None
    job_id = str(orphan["job_id"])

    monkeypatch.setattr(ReviewStore, "claim_job", real_claim)
    resumed = _recover_one_manual_job(
        home=home,
        job_id=job_id,
        runtime=_ManualRuntime(workspace=repo),
    )

    assert resumed["completed"] is True
    assert resumed["review_job_id"] == job_id
    connection = sqlite3.connect(home / "state.db")
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_jobs"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM review_generations WHERE job_id=?",
            (job_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_manual_check_wait_recovers_exact_repair_before_fresh_review(
    tmp_path,
    monkeypatch,
):
    """A transient post-repair check must resume from the repaired snapshot."""

    from agent.review_engine import ReviewStore, resume_manual_review_job
    from hermes_state import SessionDB
    from tools import async_delegation

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe = 'def value():\n    return "unsafe"\n'
    fixed = 'def value():\n    return "fixed"\n'
    (repo / "src/objective.py").write_text(unsafe, encoding="utf-8")
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    queued_inline: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        "agent.review_engine._enqueue_manual_review_resume",
        lambda *, state_db_path, job_id: queued_inline.append(
            (Path(state_db_path), str(job_id))
        ),
    )

    class WaitAfterRepairRuntime(_ManualRuntime):
        def run_checks(self, **kwargs):
            self.check_calls.append(dict(kwargs))
            return {"status": "waiting"}

    first_runtime = WaitAfterRepairRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line='    return "unsafe"\n',
        repaired_text=fixed,
    )
    first = _run(
        _agent(database, session_id="manual-session", runtime=first_runtime),
        scope="src/objective.py",
    )

    assert first["completed"] is False
    assert first["review_state"] == "checking"
    assert (repo / "src/objective.py").read_text(encoding="utf-8") == fixed
    assert len(first_runtime.repair_calls) == 1
    assert len(first_runtime.check_calls) == 1
    assert len(queued_inline) == 1
    job_id = str(first["review_job_id"])
    store = ReviewStore(home / "state.db")
    applied = store.get_manual_checkpoint(
        job_id=job_id,
        generation=1,
        phase="repair_applied",
    )
    assert applied is not None
    assert store.get_manual_checkpoint(
        job_id=job_id,
        generation=1,
        phase="checks_passed",
    ) is None

    recovery_queue: queue.Queue = queue.Queue()
    recovered = async_delegation.recover_manual_review_jobs(
        state_db_path=home / "state.db",
        profile="manual-profile",
        recovery_queue=recovery_queue,
    )
    assert recovered == {"queued": 1}
    request = recovery_queue.get_nowait()
    check_targets: list[object] = []

    class FreshCheckRuntime(_ManualRuntime):
        def repair_generation(self, **_kwargs):
            raise AssertionError("the durable repair was replayed")

        def run_checks(self, **kwargs):
            check_targets.append(kwargs["target"])
            return super().run_checks(**kwargs)

    fresh_runtime = FreshCheckRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line='    return "unsafe"\n',
    )
    resumed = resume_manual_review_job(
        _agent(
            SessionDB(db_path=home / "state.db"),
            session_id="manual-session",
            runtime=fresh_runtime,
        ),
        request,
    )

    assert resumed["completed"] is True
    assert fresh_runtime.repair_calls == []
    assert len(fresh_runtime.check_calls) == 1
    assert len(check_targets) == 1
    assert check_targets[0].generation == 1
    assert check_targets[0].snapshot_digest == applied.snapshot_digest
    assert [item[0] for item in fresh_runtime.reviewer_requests] == [
        "smart_reviewer",
        "code_worker",
    ]
    packets = [_packet(item[1]) for item in fresh_runtime.reviewer_requests]
    assert {packet["target"]["generation"] for packet in packets} == {1}
    passed_checks = store.get_manual_checkpoint(
        job_id=job_id,
        generation=1,
        phase="checks_passed",
    )
    assert passed_checks is not None
    assert passed_checks.snapshot_digest == applied.snapshot_digest
    completed_job = store.get_job(job_id)
    assert completed_job.current_generation == 1
    assert completed_job.state == "passed"


def test_manual_post_check_identity_drift_yields_to_backed_off_restart(
    tmp_path,
    monkeypatch,
):
    """Reviewer replacement after checks is transient, not an inline loop."""

    from agent.review_engine import (
        ReviewStore,
        build_manual_review_resume_request,
        resume_manual_review_job,
    )
    from hermes_state import SessionDB
    from tools import async_delegation

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    (repo / "src/objective.py").write_text(
        "def value():\n" + unsafe_line, encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    queued: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        "agent.review_engine._enqueue_manual_review_resume",
        lambda *, state_db_path, job_id: queued.append(
            (Path(state_db_path), str(job_id))
        ),
    )

    class InlineSpinDetected(BaseException):
        pass

    class DriftAfterChecksRuntime(_ManualRuntime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.refresh_calls = 0

        def refresh_reviewers(self):
            from agent.review_engine import ReviewerBinding

            self.refresh_calls += 1
            if self.refresh_calls > 1:
                raise InlineSpinDetected("reviewer drift retried inline")
            self.reviewer_bindings = (
                self.reviewer_bindings[0],
                ReviewerBinding(
                    slot="code_worker",
                    provider="google",
                    model="gemini-3-pro",
                    model_family="gemini",
                ),
            )

    first_runtime = DriftAfterChecksRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
        repaired_text='def value():\n    return "fixed"\n',
    )
    first = _run(
        _agent(database, session_id="manual-session", runtime=first_runtime),
        scope="src/objective.py",
    )

    assert first["completed"] is False
    assert first["review_state"] == "checking"
    assert first_runtime.refresh_calls == 1
    assert len(queued) == 1
    job_id = str(first["review_job_id"])
    assert ReviewStore(home / "state.db").get_job(job_id).owner_id is None
    request = build_manual_review_resume_request(
        state_db_path=home / "state.db",
        job_id=job_id,
    )

    drifted_runtime = _ManualRuntime(workspace=repo)
    drifted_runtime.reviewer_bindings = first_runtime.reviewer_bindings
    recovery_queue: queue.Queue = queue.Queue()
    recovery_queue.put(request)
    scheduled: list[tuple[dict[str, str], int]] = []
    monkeypatch.setattr(
        async_delegation,
        "_schedule_manual_review_recovery_retry",
        lambda candidate, *, attempts: scheduled.append(
            (dict(candidate), attempts)
        ),
    )

    def drifted_worker(exact_request):
        return resume_manual_review_job(
            _agent(
                SessionDB(db_path=home / "state.db"),
                session_id="manual-session",
                runtime=drifted_runtime,
            ),
            exact_request,
        )

    consumed = async_delegation.consume_manual_review_recoveries(
        recovery_queue,
        worker=drifted_worker,
        max_items=1,
    )

    assert consumed == {"completed": 0, "consumed": 1, "deferred": 1}
    assert scheduled == [(request, 0)]
    assert drifted_runtime.reviewer_requests == []
    stable_runtime = _ManualRuntime(workspace=repo)
    completed = resume_manual_review_job(
        _agent(
            SessionDB(db_path=home / "state.db"),
            session_id="manual-session",
            runtime=stable_runtime,
        ),
        request,
    )
    assert completed["completed"] is True
    assert [item[0] for item in stable_runtime.reviewer_requests] == [
        "smart_reviewer",
        "code_worker",
    ]


def test_manual_fresh_snapshot_failure_yields_then_restart_uses_checkpoint(
    tmp_path,
    monkeypatch,
):
    """A persistent post-check capture error must release the worker lease."""

    from agent import review_engine

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe_line = '    return "unsafe"\n'
    fixed = 'def value():\n    return "fixed"\n'
    (repo / "src/objective.py").write_text(
        "def value():\n" + unsafe_line, encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)
    real_capture = review_engine._capture_manual_snapshot
    capture_calls = 0

    class InlineSpinDetected(BaseException):
        pass

    def fail_post_check_capture(*args, **kwargs):
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls < 3:
            return real_capture(*args, **kwargs)
        if capture_calls == 3:
            raise review_engine.ReviewValidationError(
                "fresh snapshot host is unavailable"
            )
        raise InlineSpinDetected("fresh snapshot retried inline")

    queued: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        review_engine, "_capture_manual_snapshot", fail_post_check_capture
    )
    monkeypatch.setattr(
        review_engine,
        "_enqueue_manual_review_resume",
        lambda *, state_db_path, job_id: queued.append(
            (Path(state_db_path), str(job_id))
        ),
    )
    first_runtime = _ManualRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line=unsafe_line,
        repaired_text=fixed,
    )
    first = _run(
        _agent(database, session_id="manual-session", runtime=first_runtime),
        scope="src/objective.py",
    )

    assert first["completed"] is False
    assert first["review_state"] == "checking"
    assert capture_calls == 3
    assert len(queued) == 1
    assert "fresh_snapshot_failed" in _event_kinds(home / "state.db")
    job_id = str(first["review_job_id"])
    monkeypatch.setattr(review_engine, "_capture_manual_snapshot", real_capture)

    class NoReplayRuntime(_ManualRuntime):
        def repair_generation(self, **_kwargs):
            raise AssertionError("checkpointed repair was replayed")

        def run_checks(self, **_kwargs):
            raise AssertionError("checkpointed checks were replayed")

    resumed_runtime = NoReplayRuntime(workspace=repo)
    resumed = _recover_one_manual_job(
        home=home,
        job_id=job_id,
        runtime=resumed_runtime,
    )
    assert resumed["completed"] is True
    assert [item[0] for item in resumed_runtime.reviewer_requests] == [
        "smart_reviewer",
        "code_worker",
    ]


def test_manual_hard_crash_before_repair_checkpoint_retries_safely(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    (repo / "src/objective.py").write_text(
        'def value():\n    return "unsafe"\n', encoding="utf-8"
    )
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)

    class SimulatedProcessLoss(BaseException):
        pass

    attempts_root = tmp_path / "manual-repair-attempts"
    attempts_root.mkdir()
    observed_attempts: list[int] = []

    class CrashBeforeCheckpoint(_ManualRuntime):
        def repair_generation(self, *, repair_attempt, **_kwargs):
            observed_attempts.append(repair_attempt)
            orphan = attempts_root / f"attempt-{repair_attempt}"
            orphan.mkdir()
            (orphan / "worker-started").write_text("orphaned\n", encoding="utf-8")
            raise SimulatedProcessLoss("repair worker process exited")

    with pytest.raises(SimulatedProcessLoss, match="process exited"):
        _run(
            _agent(
                database,
                session_id="manual-session",
                runtime=CrashBeforeCheckpoint(
                    workspace=repo,
                    blocking_path="src/objective.py",
                    unsafe_line='    return "unsafe"\n',
                ),
            ),
            scope="src/objective.py",
        )
    job_id = str(_job_row(home / "state.db")["job_id"])
    _expire_manual_recovery_lease(home / "state.db", job_id)

    class RejectOrphanReuseRuntime(_ManualRuntime):
        def repair_generation(self, *, repair_attempt, **kwargs):
            observed_attempts.append(repair_attempt)
            attempt_root = attempts_root / f"attempt-{repair_attempt}"
            attempt_root.mkdir()
            return super().repair_generation(**kwargs)

    retry_runtime = RejectOrphanReuseRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line='    return "unsafe"\n',
        repaired_text='def value():\n    return "fixed"\n',
    )
    result = _recover_one_manual_job(
        home=home,
        job_id=job_id,
        runtime=retry_runtime,
    )
    assert result["completed"] is True
    assert observed_attempts == [1, 2]
    assert sorted(path.name for path in attempts_root.iterdir()) == [
        "attempt-1",
        "attempt-2",
    ]
    assert _job_row(home / "state.db")["next_manual_repair_attempt"] == 2
    assert len(retry_runtime.repair_calls) == 1
    assert (repo / "src/objective.py").read_text(encoding="utf-8") == (
        'def value():\n    return "fixed"\n'
    )


def test_manual_restart_after_repair_apply_adopts_checkpoint_without_replay(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe = 'def value():\n    return "unsafe"\n'
    fixed = 'def value():\n    return "fixed"\n'
    (repo / "src/objective.py").write_text(unsafe, encoding="utf-8")
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)

    class SimulatedProcessLoss(BaseException):
        pass

    class CrashAfterRepairCheckpoint(_ManualRuntime):
        def repair_generation(self, *, checkpoint_callback=None, **kwargs):
            result = dict(super().repair_generation(**kwargs))
            result["check_receipt_digest"] = "a" * 64
            if checkpoint_callback is not None:
                checkpoint_callback(result)
            raise SimulatedProcessLoss("crash after repair apply checkpoint")

    first_runtime = CrashAfterRepairCheckpoint(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line='    return "unsafe"\n',
        repaired_text=fixed,
    )
    with pytest.raises(SimulatedProcessLoss, match="repair apply checkpoint"):
        _run(
            _agent(database, session_id="manual-session", runtime=first_runtime),
            scope="src/objective.py",
        )
    assert (repo / "src/objective.py").read_text(encoding="utf-8") == fixed
    job_id = str(_job_row(home / "state.db")["job_id"])
    _expire_manual_recovery_lease(home / "state.db", job_id)
    connection = sqlite3.connect(home / "state.db")
    try:
        connection.execute(
            "UPDATE review_jobs SET state='repairing' WHERE job_id=?",
            (job_id,),
        )
        connection.execute(
            "UPDATE review_generations SET state='repairing' "
            "WHERE job_id=? AND generation=0",
            (job_id,),
        )
        connection.commit()
    finally:
        connection.close()

    class NoReplayRuntime(_ManualRuntime):
        def repair_generation(self, **_kwargs):
            raise AssertionError("durable repair was replayed")

        def run_checks(self, **_kwargs):
            raise AssertionError("durable repair checks were replayed")

    resumed_runtime = NoReplayRuntime(workspace=repo)
    resumed = _recover_one_manual_job(
        home=home,
        job_id=job_id,
        runtime=resumed_runtime,
    )

    assert resumed["completed"] is True
    assert resumed_runtime.repair_calls == []
    assert resumed_runtime.check_calls == []
    assert [item[0] for item in resumed_runtime.reviewer_requests] == [
        "smart_reviewer",
        "code_worker",
    ]


@pytest.mark.parametrize("mixed_live_state", [False, True])
def test_manual_restart_reconciles_prepared_repair_before_live_apply(
    tmp_path,
    monkeypatch,
    mixed_live_state,
):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {
            "src/a.py": 'def value_a():\n    return "base-a"\n',
            "src/b.py": 'def value_b():\n    return "base-b"\n',
        },
    )
    unsafe = {
        "src/a.py": 'def value_a():\n    return "unsafe-a"\n',
        "src/b.py": 'def value_b():\n    return "unsafe-b"\n',
    }
    fixed = {
        "src/a.py": 'def value_a():\n    return "fixed-a"\n',
        "src/b.py": 'def value_b():\n    return "fixed-b"\n',
    }
    for path, content in unsafe.items():
        (repo / path).write_text(content, encoding="utf-8")
    _mark_objective("manual-session", repo, sorted(unsafe))
    database = _session(home, repo)

    class SimulatedProcessLoss(BaseException):
        pass

    class CrashAfterPreparedCheckpoint(_ManualRuntime):
        def repair_generation(
            self,
            *,
            target,
            checkpoint_callback=None,
            **_kwargs,
        ):
            index_path = tmp_path / (
                "mixed.index" if mixed_live_state else "old.index"
            )
            environment = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
            subprocess.run(
                ["git", "read-tree", target.snapshot_tree_oid],
                cwd=repo,
                env=environment,
                check=True,
            )
            for path, content in fixed.items():
                oid = subprocess.run(
                    ["git", "hash-object", "-w", "--stdin"],
                    cwd=repo,
                    input=content.encode("utf-8"),
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                subprocess.run(
                    [
                        "git",
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        "100644",
                        oid,
                        path.encode("utf-8"),
                    ],
                    cwd=repo,
                    env=environment,
                    check=True,
                )
            tree_oid = subprocess.run(
                ["git", "write-tree"],
                cwd=repo,
                env=environment,
                capture_output=True,
                check=True,
                text=True,
            ).stdout.strip()
            _git(
                repo,
                "update-ref",
                "refs/hermes-test/manual-prepared",
                tree_oid,
            )
            assert checkpoint_callback is not None
            checkpoint_callback(
                {
                    "changed_paths": sorted(fixed),
                    "check_receipt_digest": "b" * 64,
                    "snapshot_tree_oid": tree_oid,
                    "status": "prepared",
                }
            )
            if mixed_live_state:
                (repo / "src/a.py").write_text(
                    fixed["src/a.py"], encoding="utf-8"
                )
            raise SimulatedProcessLoss("crash before scoped live apply")

    first_runtime = CrashAfterPreparedCheckpoint(
        workspace=repo,
        blocking_path="src/a.py",
        unsafe_line='    return "unsafe-a"\n',
    )
    with pytest.raises(SimulatedProcessLoss, match="before scoped live apply"):
        _run(
            _agent(database, session_id="manual-session", runtime=first_runtime),
            scope="src/a.py src/b.py",
        )
    assert (repo / "src/b.py").read_text(encoding="utf-8") == unsafe["src/b.py"]
    if not mixed_live_state:
        assert (repo / "src/a.py").read_text(encoding="utf-8") == unsafe["src/a.py"]
    job_id = str(_job_row(home / "state.db")["job_id"])
    _expire_manual_recovery_lease(home / "state.db", job_id)

    class NoReplayRuntime(_ManualRuntime):
        def repair_generation(self, **_kwargs):
            raise AssertionError("prepared durable repair was replayed")

        def run_checks(self, **_kwargs):
            raise AssertionError("prepared durable checks were replayed")

    resumed_runtime = NoReplayRuntime(workspace=repo)
    resumed = _recover_one_manual_job(
        home=home,
        job_id=job_id,
        runtime=resumed_runtime,
    )

    assert resumed["completed"] is True
    for path, content in fixed.items():
        assert (repo / path).read_text(encoding="utf-8") == content
    assert [item[0] for item in resumed_runtime.reviewer_requests] == [
        "smart_reviewer",
        "code_worker",
    ]


def test_manual_restart_after_checks_pass_adopts_new_generation_without_replay(
    tmp_path,
    monkeypatch,
):
    from agent.review_engine import ReviewStore

    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(
        tmp_path / "repo",
        {"src/objective.py": 'def value():\n    return "base"\n'},
    )
    unsafe = 'def value():\n    return "unsafe"\n'
    fixed = 'def value():\n    return "fixed"\n'
    (repo / "src/objective.py").write_text(unsafe, encoding="utf-8")
    _mark_objective("manual-session", repo, ["src/objective.py"])
    database = _session(home, repo)

    class SimulatedProcessLoss(BaseException):
        pass

    real_begin_generation = ReviewStore.begin_generation
    crashed = False

    def crash_before_new_generation(self, **kwargs):
        nonlocal crashed
        if kwargs["generation"] == 1 and not crashed:
            crashed = True
            raise SimulatedProcessLoss("crash after checks-pass checkpoint")
        return real_begin_generation(self, **kwargs)

    monkeypatch.setattr(ReviewStore, "begin_generation", crash_before_new_generation)
    first_runtime = _ManualRuntime(
        workspace=repo,
        blocking_path="src/objective.py",
        unsafe_line='    return "unsafe"\n',
        repaired_text=fixed,
    )
    with pytest.raises(SimulatedProcessLoss, match="checks-pass checkpoint"):
        _run(
            _agent(database, session_id="manual-session", runtime=first_runtime),
            scope="src/objective.py",
        )
    job_id = str(_job_row(home / "state.db")["job_id"])
    _expire_manual_recovery_lease(home / "state.db", job_id)

    class NoReplayRuntime(_ManualRuntime):
        def repair_generation(self, **_kwargs):
            raise AssertionError("durable repair was replayed")

        def run_checks(self, **_kwargs):
            raise AssertionError("durable checks were replayed")

    resumed_runtime = NoReplayRuntime(workspace=repo)
    resumed = _recover_one_manual_job(
        home=home,
        job_id=job_id,
        runtime=resumed_runtime,
    )

    assert resumed["completed"] is True
    assert resumed_runtime.repair_calls == []
    assert resumed_runtime.check_calls == []
    assert [item[0] for item in resumed_runtime.reviewer_requests] == [
        "smart_reviewer",
        "code_worker",
    ]
