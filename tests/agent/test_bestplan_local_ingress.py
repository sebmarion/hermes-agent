from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "BestPlan Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _manifest(workspace: str) -> dict:
    return {
        "version": 1,
        "mode": "delegate",
        "risk": "low",
        "slices": [
            {
                "id": "implement",
                "kind": "implement",
                "goal": "Implement the approved change",
                "depends_on": [],
                "capability": "fast_fallback",
                "workspace": workspace,
                "allowed_paths": ["feature.py"],
                "read_only": False,
                "expected_artifacts": ["feature.py"],
                "acceptance": [
                    "pytest -q -- tests/agent/test_execution_plan.py::"
                    "test_compile_valid_plan_orders_dependency_waves",
                ],
            }
        ],
        "merge_policy": "Integrate after exact checks.",
        "stop_condition": "All exact checks pass.",
        "escalation_predicates": ["verification_failed_after_local_repair"],
    }


def _response(workspace: str) -> str:
    from agent.bestplan_state import BESTPLAN_ENVELOPE_END, BESTPLAN_ENVELOPE_START

    payload = {"version": 1, "manifest": _manifest(workspace)}
    return (
        "Suggested plan.\n\n"
        f"{BESTPLAN_ENVELOPE_START}\n"
        f"{json.dumps(payload, sort_keys=True)}\n"
        f"{BESTPLAN_ENVELOPE_END}"
    )


def _local_inputs(snapshot, tmp_path: Path):
    from agent.bestplan_contract import BoundCommand, ControllerIdentity
    from agent.bestplan_local import LocalCheckPlan, LocalExecutionInputs

    controller_source = tmp_path / "controller"
    controller_source.mkdir(exist_ok=True)
    controller = ControllerIdentity(
        repository_id=snapshot.repo.repository_id,
        controller_id="local-controller",
        release_oid=snapshot.head_oid,
        artifact_sha256=hashlib.sha256(b"controller").hexdigest(),
    )
    command = BoundCommand(
        identifier="pytest",
        executable="/usr/bin/true",
        executable_sha256=hashlib.sha256(b"true").hexdigest(),
        argv=(),
        logical_cwd="integration",
        env=(),
        inputs=(),
        cache=(),
        timeout_seconds=60,
        network_allowlist=(),
    )
    check_plan = LocalCheckPlan(
        commands=(command,),
        runtime_read_paths=(),
        sandbox_executable=Path("/usr/bin/sandbox-exec"),
        sandbox_executable_sha256=hashlib.sha256(b"sandbox").hexdigest(),
        policy_version="bestplan-check-v1",
        check_runtime_digest=hashlib.sha256(b"runtime").hexdigest(),
        pytest_module_path=Path("/opt/test/pytest/__init__.py"),
    )
    return LocalExecutionInputs(
        controller_source=controller_source.resolve(),
        controller=controller,
        check_plan=check_plan,
    )


def _capture_local_plan(tmp_path: Path, monkeypatch):
    from agent import bestplan_local
    from agent.bestplan_source import capture_source_snapshot, resolve_repo_identity
    from agent.bestplan_state import BestplanStore, capture_bestplan_response

    repo = _repo(tmp_path)
    snapshot = capture_source_snapshot(
        resolve_repo_identity(str(repo)),
        time.monotonic() + 20.0,
    )
    inputs = _local_inputs(snapshot, tmp_path)
    monkeypatch.setattr(
        bestplan_local,
        "capture_local_execution_inputs",
        lambda **kwargs: inputs,
    )
    store = BestplanStore(db_path=tmp_path / "state" / "state.db")
    captured = capture_bestplan_response(
        _response(snapshot.repo.workspace),
        session_id="local-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        local_execution=True,
    )
    return store, snapshot, captured


def _add_compression_continuation(session_db, parent: str, child: str) -> None:
    """Create one real SessionDB compression edge with a live child."""

    base = time.time() - 10.0
    session_db.create_session(parent, source="cli")
    session_db.end_session(parent, "compression")
    session_db.create_session(child, source="cli", parent_session_id=parent)
    session_db.append_message(child, role="assistant", content="continued")
    session_db._conn.execute(
        "UPDATE sessions SET started_at=?, ended_at=? WHERE id=?",
        (base, base + 1.0, parent),
    )
    session_db._conn.execute(
        "UPDATE sessions SET started_at=? WHERE id=?",
        (base + 2.0, child),
    )
    session_db._conn.commit()
    assert session_db.resolve_resume_session_id(parent) == child


def test_cli_capture_requests_local_execution_contract(monkeypatch):
    import cli

    seen = {}

    class Store:
        def commit_provisional_plan(self, plan_id):
            return plan_id == "bp-local"

        def close(self):
            raise AssertionError("injected store must not be closed")

    def capture(_result, **kwargs):
        seen.update(kwargs)
        return {
            "bestplan_capture": {
                "executable": True,
                "plan_id": "bp-local",
                "receipt_persisted": True,
            }
        }

    monkeypatch.setattr("agent.bestplan_state.capture_bestplan_agent_result", capture)
    host = SimpleNamespace(_persist_session=lambda *_args, **_kwargs: True)

    cli._capture_cli_bestplan_result(
        {"final_response": "plan"},
        invocation_message="/bestplan improve this",
        session_id="local-session",
        profile="coder",
        workspace="/tmp/workspace",
        host_agent=host,
        store=Store(),
    )

    assert seen["local_execution"] is True


def test_local_capture_persists_and_renders_exact_local_main_contract(
    tmp_path, monkeypatch,
):
    from agent.bestplan_local import LOCAL_GO_CONTRACT_SCHEMA

    store, snapshot, captured = _capture_local_plan(tmp_path, monkeypatch)

    assert captured.executable is True
    row = store.get_plan(captured.plan_id)
    contract = json.loads(row["promotion_contract_json"])
    assert row["execution_protocol"] == 2
    assert row["promotion_contract_version"] == 1
    assert row["promotion_mode"] == "local_main"
    assert row["baseline_revision"] == snapshot.head_oid
    assert contract["schema"] == LOCAL_GO_CONTRACT_SCHEMA
    assert "fast-forward local `main`" in captured.response
    assert "does not authorize a remote push" in captured.response


def test_new_durable_local_capture_replaces_prior_unstarted_plan(
    tmp_path, monkeypatch,
):
    from agent.bestplan_proof import ProofLedger
    from agent.bestplan_state import PlanState, capture_bestplan_response

    store, snapshot, prior = _capture_local_plan(tmp_path, monkeypatch)
    replacement = capture_bestplan_response(
        _response(snapshot.repo.workspace),
        session_id="local-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        provisional=True,
        local_execution=True,
    )

    assert store.commit_provisional_plan(replacement.plan_id)
    assert store.get_plan(prior.plan_id)["state"] == PlanState.REJECTED
    assert store.get_plan(replacement.plan_id)["state"] == PlanState.PENDING
    assert ProofLedger(store).verify_chain(prior.plan_id)


def test_new_local_capture_supersedes_unstarted_compression_ancestor(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local
    from agent.bestplan_source import capture_source_snapshot, resolve_repo_identity
    from agent.bestplan_state import BestplanStore, PlanState, capture_bestplan_response
    from hermes_state import SessionDB

    repo = _repo(tmp_path)
    snapshot = capture_source_snapshot(
        resolve_repo_identity(str(repo)),
        time.monotonic() + 20.0,
    )
    inputs = _local_inputs(snapshot, tmp_path)
    monkeypatch.setattr(
        bestplan_local,
        "capture_local_execution_inputs",
        lambda **_kwargs: inputs,
    )
    session_db = SessionDB(db_path=tmp_path / "state" / "state.db")
    _add_compression_continuation(session_db, "session-parent", "session-child")
    store = BestplanStore(session_db=session_db)
    prior = capture_bestplan_response(
        _response(snapshot.repo.workspace),
        session_id="session-parent",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        local_execution=True,
    )
    replacement = capture_bestplan_response(
        _response(snapshot.repo.workspace),
        session_id="session-child",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        provisional=True,
        local_execution=True,
    )

    assert store.commit_provisional_plan(replacement.plan_id)
    assert store.get_plan(prior.plan_id)["state"] == PlanState.REJECTED
    assert store.get_plan(replacement.plan_id)["state"] == PlanState.PENDING


def test_delayed_older_compression_child_commit_does_not_supersede_newer_ancestor(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local, bestplan_state
    from agent.bestplan_source import capture_source_snapshot, resolve_repo_identity
    from agent.bestplan_state import BestplanStore, PlanState, capture_bestplan_response
    from hermes_state import SessionDB

    repo = _repo(tmp_path)
    snapshot = capture_source_snapshot(
        resolve_repo_identity(str(repo)),
        time.monotonic() + 20.0,
    )
    inputs = _local_inputs(snapshot, tmp_path)
    monkeypatch.setattr(
        bestplan_local,
        "capture_local_execution_inputs",
        lambda **_kwargs: inputs,
    )
    session_db = SessionDB(db_path=tmp_path / "state" / "state.db")
    _add_compression_continuation(session_db, "session-parent", "session-child")
    store = BestplanStore(session_db=session_db)
    monkeypatch.setattr(bestplan_state.time, "time", lambda: 100.0)
    older = capture_bestplan_response(
        _response(snapshot.repo.workspace),
        session_id="session-child",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        provisional=True,
        local_execution=True,
    )
    monkeypatch.setattr(bestplan_state.time, "time", lambda: 200.0)
    newer = capture_bestplan_response(
        _response(snapshot.repo.workspace),
        session_id="session-parent",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        provisional=True,
        local_execution=True,
    )

    assert store.commit_provisional_plan(newer.plan_id)
    assert store.commit_provisional_plan(older.plan_id)

    assert store.get_plan(older.plan_id)["state"] == PlanState.PENDING
    assert store.get_plan(newer.plan_id)["state"] == PlanState.PENDING


def test_local_capture_binds_exact_manifest_acceptance_checks(tmp_path, monkeypatch):
    from agent import bestplan_local
    from agent.bestplan_source import capture_source_snapshot, resolve_repo_identity
    from agent.bestplan_state import BestplanStore, capture_bestplan_response

    repo = _repo(tmp_path)
    snapshot = capture_source_snapshot(
        resolve_repo_identity(str(repo)),
        time.monotonic() + 20.0,
    )
    inputs = _local_inputs(snapshot, tmp_path)
    calls = []

    def capture_inputs(**kwargs):
        calls.append(kwargs)
        return inputs

    monkeypatch.setattr(
        bestplan_local,
        "capture_local_execution_inputs",
        capture_inputs,
    )
    store = BestplanStore(db_path=tmp_path / "state" / "state.db")
    captured = capture_bestplan_response(
        _response(snapshot.repo.workspace),
        session_id="local-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        store=store,
        config={
            "bestplan": {
                "pytest_nodes": [
                    "tests/hostile_global_smoke.py",
                ],
            },
        },
        local_execution=True,
    )

    assert captured.executable is True
    assert calls[0]["manifest"] == _manifest(snapshot.repo.workspace)
    assert "check_config" not in calls[0]


def test_local_capture_leaves_runtime_deadline_to_local_policy(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local
    from agent.bestplan_state import BestplanStore, capture_bestplan_response

    repo = _repo(tmp_path)
    calls = []

    def capture_inputs(**kwargs):
        calls.append(kwargs)
        return _local_inputs(kwargs["snapshot"], tmp_path)

    monkeypatch.setattr(
        bestplan_local,
        "capture_local_execution_inputs",
        capture_inputs,
    )
    store = BestplanStore(db_path=tmp_path / "state" / "state.db")
    captured = capture_bestplan_response(
        _response(str(repo)),
        session_id="local-session",
        profile="coder",
        workspace=str(repo),
        store=store,
        local_execution=True,
    )

    assert captured.executable is True
    assert "deadline" not in calls[0]


def test_bare_go_rejects_a_second_local_plan_while_push_decision_is_pending(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local
    from agent.bestplan_state import try_resolve_go

    store, snapshot, _captured = _capture_local_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(
        store,
        "list_active_local_pushes",
        lambda session_id: [
            {
                "session_id": session_id,
                "profile": "coder",
                "workspace": snapshot.repo.workspace,
                "local_push_state": "awaiting",
            },
        ],
    )
    runtime_calls = []
    monkeypatch.setattr(
        bestplan_local,
        "build_local_execution_runtime",
        lambda **kwargs: runtime_calls.append(kwargs),
    )

    result = try_resolve_go(
        "go",
        session_id="local-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        parent_agent=SimpleNamespace(),
        store=store,
    )

    assert result.resolved is True
    assert result.status == "push_pending"
    assert runtime_calls == []


def test_compressed_continuation_go_finds_ancestor_plan_and_active_push(
    tmp_path,
):
    from agent.bestplan_state import BestplanStore, try_resolve_go
    from hermes_state import SessionDB
    from tests.agent.test_bestplan_local_push_state import (
        _activate,
        _prepare,
        _repo,
        _seed_local_plan,
        _snapshot,
    )

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    session_db = SessionDB(db_path=tmp_path / "state" / "state.db")
    store = BestplanStore(session_db=session_db)
    _seed_local_plan(
        store,
        snapshot,
        integration_oid,
        session_id="session-1",
    )
    assert _prepare(store, snapshot, integration_oid)
    assert _activate(store, snapshot, integration_oid)
    _seed_local_plan(
        store,
        snapshot,
        integration_oid,
        plan_id="bp-next",
        session_id="session-1",
    )
    _add_compression_continuation(
        session_db,
        "session-1",
        "session-2",
    )

    result = try_resolve_go(
        "go",
        session_id="session-2",
        profile="coder",
        workspace=snapshot.repo.workspace,
        parent_agent=SimpleNamespace(),
        config={},
        store=store,
    )
    unrelated = try_resolve_go(
        "go",
        session_id="unrelated-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        parent_agent=SimpleNamespace(),
        config={},
        store=store,
    )

    assert result.resolved is True
    assert result.status == "push_pending"
    assert result.plan_id == "bp-next"
    assert unrelated.resolved is False
    assert unrelated.status == "disabled"
    assert store.get_plan("bp-local")["session_id"] == "session-1"
    assert store.get_plan("bp-next")["session_id"] == "session-1"
    session_db.close()


def test_bare_go_builds_local_runtime_and_hands_exact_local_batch_to_dispatcher(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local
    from agent.bestplan_state import try_resolve_go
    from tools import delegate_tool

    store, snapshot, captured = _capture_local_plan(tmp_path, monkeypatch)
    local_runtime = SimpleNamespace(candidate_runtime=object())
    runtime_calls = []
    authority_calls = []
    dispatched = {}

    def build_runtime(**kwargs):
        runtime_calls.append(kwargs)
        return local_runtime

    def build_authorities(runtimes):
        authority_calls.append(runtimes)
        return ("authority-binding",)

    monkeypatch.setattr(bestplan_local, "build_local_execution_runtime", build_runtime)
    monkeypatch.setattr(bestplan_local, "build_local_authority_bindings", build_authorities)
    monkeypatch.setattr(
        delegate_tool,
        "_validate_bestplan_host_runtime",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        delegate_tool,
        "_bestplan_host_runtime_projection",
        lambda _runtime: {"candidate_host_runtime_digest": "d" * 64},
    )
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True,
    )

    raw_runtime = {
        "route": "code_worker",
        "provider": "custom",
        "model": "local-model",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "host-only-secret",
        "api_mode": "chat_completions",
        "runtime_fingerprint": "a" * 64,
    }

    def strict_dispatcher(**kwargs):
        dispatched.update(kwargs)
        return {"status": "dispatched", "delegation_id": "local-delegation"}

    result = try_resolve_go(
        "go",
        session_id="local-session",
        profile="coder",
        workspace=snapshot.repo.workspace,
        baseline_fingerprint=snapshot.fingerprint,
        parent_agent=SimpleNamespace(),
        store=store,
        runtime_resolver=lambda _tasks, _parent: [dict(raw_runtime)],
        strict_dispatcher=strict_dispatcher,
    )

    assert result.status == "waiting"
    assert len(runtime_calls) == 1
    assert runtime_calls[0]["plan_id"] == captured.plan_id
    assert runtime_calls[0]["snapshot"] == snapshot
    assert authority_calls == [[raw_runtime]]
    assert dispatched["promotion_mode"] == "local_main"
    assert dispatched["execution_plan"].to_manifest() == _manifest(
        snapshot.repo.workspace
    )
    assert dispatched["local_execution_runtime"] is local_runtime
    assert dispatched["candidate_host_runtime"] is local_runtime.candidate_runtime
    assert dispatched["authority_client"] is None
    assert dispatched["authority_bindings"] == ("authority-binding",)
    stored = store.get_plan(captured.plan_id)["resolved_runtime_json"]
    assert "host-only-secret" not in stored


def test_fast_local_completion_cannot_turn_dispatch_ack_into_an_error(tmp_path):
    from tests.agent.test_bestplan_local_push_state import (
        _activate,
        _prepare,
        _repo,
        _seed_local_plan,
        _snapshot,
        _store,
    )

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)
    assert _prepare(store, snapshot, integration_oid)
    assert _activate(store, snapshot, integration_oid)
    before = store.get_plan("bp-local")

    assert store.record_dispatch(
        "bp-local",
        delegation_ids=["local-delegation"],
    )

    after = store.get_plan("bp-local")
    assert after["state"] == "completed_local"
    assert after["local_push_state"] == "awaiting"
    assert after["local_push_json"] == before["local_push_json"]


def test_local_batch_failure_terminalizes_without_a_push_record(tmp_path):
    from tests.agent.test_bestplan_local_push_state import (
        _repo,
        _seed_local_plan,
        _snapshot,
        _store,
    )

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid = "1" * len(snapshot.head_oid)
    store = _store(tmp_path)
    _seed_local_plan(store, snapshot, integration_oid)

    assert store.mark_completed_unverified(
        "bp-local",
        {"status": "failed", "error": "candidate failed"},
    )

    row = store.get_plan("bp-local")
    assert row["state"] == "failed"
    assert row["dispatch_state"] == "terminal"
    assert row["dispatch_owner"] is None
    assert row["error"] == "dispatch_failed"
    assert row["local_push_json"] is None
