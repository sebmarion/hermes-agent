from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _completed(position: int):
    frozen = SimpleNamespace(candidate_id=f"candidate-{position}")
    spec = SimpleNamespace(candidate_id=f"candidate-{position}")
    return frozen, spec, f"slice-{position}"


def _runtime(tmp_path: Path):
    return SimpleNamespace(
        operation_timeout_seconds=30.0,
        integration_root=tmp_path / "integration",
        checks_root=tmp_path / "checks",
        check_runtime=object(),
        check_plan=SimpleNamespace(commands=("pytest-command",)),
    )


def _snapshot(tmp_path: Path):
    return SimpleNamespace(
        head_oid="0" * 40,
        repo=SimpleNamespace(workspace=str(tmp_path)),
    )


def test_local_batch_orders_proof_check_prepare_land_activate_and_prompt(
    tmp_path, monkeypatch,
):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from agent import bestplan_state
    from tools import delegate_tool

    events: list[object] = []
    completed = [_completed(0), _completed(1)]
    integration = SimpleNamespace(
        integration_oid="1" * 40,
        target_oid="0" * 40,
        tree_oid="2" * 40,
        receipt_digest="3" * 64,
    )
    checks = SimpleNamespace(receipt_digest="4" * 64)
    push_target = SimpleNamespace(
        display_url="example.invalid/repo",
        remote_ref="refs/heads/main",
    )
    landing = SimpleNamespace(new_oid=integration.integration_oid)

    def binding(**kwargs):
        events.append(("binding", kwargs["manifest_slice_id"]))
        return f"binding-{kwargs['manifest_slice_id']}"

    def freeze(**kwargs):
        events.append(("freeze", tuple(kwargs["candidates"])))
        return integration

    def run_checks(**kwargs):
        events.append(("checks", kwargs["integration"]))
        return checks

    def observe(**kwargs):
        events.append(("observe", kwargs["integration_oid"]))
        return push_target

    def land(**kwargs):
        events.append(("land", kwargs["checks"].receipt_digest))
        return landing

    class Store:
        def __init__(self, *, db_path):
            events.append(("store", Path(db_path)))

        def prepare_local_push(self, plan_id, **kwargs):
            events.append((
                "prepare",
                plan_id,
                kwargs["check_set_digest"],
                kwargs["expires_at"],
            ))
            return {"state": "prepared"}

        def activate_local_push(self, plan_id, *, landing_receipt):
            events.append(("activate", plan_id, landing_receipt.new_oid))
            return True

        def close(self):
            events.append("close")

    monkeypatch.setattr(delegate_tool, "_build_local_candidate_binding", binding)
    monkeypatch.setattr(bestplan_promotion, "freeze_integration", freeze)
    monkeypatch.setattr(bestplan_checks, "run_integration_checks", run_checks)
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        observe,
    )
    monkeypatch.setattr(bestplan_local_git, "land_checked_integration", land)
    monkeypatch.setattr(bestplan_state, "BestplanStore", Store)

    result = delegate_tool._finish_local_bestplan_batch(
        plan_id="bp-local",
        plan=object(),
        snapshot=_snapshot(tmp_path),
        contract={"commands": []},
        approval_digest="5" * 64,
        contract_digest="6" * 64,
        completed=completed,
        projected_results=[
            {"status": "frozen", "summary": "first"},
            {"status": "frozen", "summary": "second"},
        ],
        runtime=_runtime(tmp_path),
        state_db_path=tmp_path / "state.db",
        session_id="session-local",
        profile="default",
        cancel_event=None,
        now=1.0,
    )

    names = [item[0] if isinstance(item, tuple) else item for item in events]
    assert names == [
        "binding",
        "binding",
        "freeze",
        "checks",
        "observe",
        "store",
        "prepare",
        "land",
        "activate",
        "close",
    ]
    assert events[0:2] == [("binding", "slice-0"), ("binding", "slice-1")]
    prepare = next(item for item in events if isinstance(item, tuple) and item[0] == "prepare")
    assert prepare[3] >= time.time() + 899
    assert "Reply `push` or `no`" in result["results"][-1]["summary"]
    assert "approved checks passed" in result["results"][-1]["summary"]
    assert "all required checks" not in result["results"][-1]["summary"]
    assert result["integration_oid"] == integration.integration_oid
    assert result["check_set_digest"] == checks.receipt_digest


def test_local_batch_check_failure_has_no_push_or_main_effect(tmp_path, monkeypatch):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from tools import delegate_tool

    monkeypatch.setattr(
        delegate_tool,
        "_build_local_candidate_binding",
        lambda **kwargs: kwargs["manifest_slice_id"],
    )
    monkeypatch.setattr(
        bestplan_promotion,
        "freeze_integration",
        lambda **kwargs: SimpleNamespace(integration_oid="1" * 40),
    )
    monkeypatch.setattr(
        bestplan_checks,
        "run_integration_checks",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("checks failed")),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **kwargs: pytest.fail("push target observed after failed checks"),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **kwargs: pytest.fail("local main changed after failed checks"),
    )

    with pytest.raises(RuntimeError, match="checks failed"):
        delegate_tool._finish_local_bestplan_batch(
            plan_id="bp-local",
            plan=object(),
            snapshot=_snapshot(tmp_path),
            contract={"commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=[_completed(0)],
            projected_results=[{"status": "frozen", "summary": "first"}],
            runtime=_runtime(tmp_path),
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            cancel_event=None,
            now=time.time(),
        )


def test_local_batch_requires_durable_prepare_before_local_main(tmp_path, monkeypatch):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from agent import bestplan_state
    from tools import delegate_tool

    monkeypatch.setattr(
        delegate_tool,
        "_build_local_candidate_binding",
        lambda **kwargs: kwargs["manifest_slice_id"],
    )
    integration = SimpleNamespace(
        integration_oid="1" * 40,
        target_oid="0" * 40,
    )
    monkeypatch.setattr(
        bestplan_promotion, "freeze_integration", lambda **kwargs: integration,
    )
    monkeypatch.setattr(
        bestplan_checks,
        "run_integration_checks",
        lambda **kwargs: SimpleNamespace(receipt_digest="4" * 64),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **kwargs: SimpleNamespace(display_url="example.invalid/repo"),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **kwargs: pytest.fail("local main changed without durable prepare"),
    )

    class Store:
        def __init__(self, *, db_path):
            pass

        def prepare_local_push(self, plan_id, **kwargs):
            return None

        def close(self):
            pass

    monkeypatch.setattr(bestplan_state, "BestplanStore", Store)

    with pytest.raises(delegate_tool.BestplanCandidateBatchError):
        delegate_tool._finish_local_bestplan_batch(
            plan_id="bp-local",
            plan=object(),
            snapshot=_snapshot(tmp_path),
            contract={"commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=[_completed(0)],
            projected_results=[{"status": "frozen", "summary": "first"}],
            runtime=_runtime(tmp_path),
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            cancel_event=None,
            now=time.time(),
        )


@pytest.mark.parametrize(
    ("classification", "terminal_state"),
    (
        ("expected", "not_landed"),
        ("integration", "stale"),
        ("other", "stale"),
        ("unavailable", "stale"),
    ),
)
def test_local_batch_classifies_a_known_landing_failure_after_git_returns(
    tmp_path, monkeypatch, classification, terminal_state,
):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from agent import bestplan_state
    from tools import delegate_tool

    events: list[object] = []
    integration = SimpleNamespace(
        integration_oid="1" * 40,
        target_oid="0" * 40,
    )
    checks = SimpleNamespace(receipt_digest="4" * 64)

    monkeypatch.setattr(
        delegate_tool,
        "_build_local_candidate_binding",
        lambda **kwargs: kwargs["manifest_slice_id"],
    )
    monkeypatch.setattr(
        bestplan_promotion, "freeze_integration", lambda **kwargs: integration,
    )
    monkeypatch.setattr(
        bestplan_checks, "run_integration_checks", lambda **kwargs: checks,
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **kwargs: SimpleNamespace(
            display_url="example.invalid/repo",
            remote_ref="refs/heads/main",
        ),
    )

    def land(**_kwargs):
        events.append("land_returned")
        raise bestplan_local_git.LocalMainConflict("landing failed")

    def classify(**_kwargs):
        assert events[-1] == "land_returned"
        events.append(("classified", classification))
        return classification

    monkeypatch.setattr(bestplan_local_git, "land_checked_integration", land)
    monkeypatch.setattr(
        bestplan_local_git, "classify_local_main_for_push", classify,
    )

    class Store:
        def __init__(self, *, db_path):
            events.append(("store", Path(db_path)))

        def prepare_local_push(self, plan_id, **_kwargs):
            events.append(("prepare", plan_id))
            return {"state": "prepared"}

        def _set_local_push_state(self, plan_id, **kwargs):
            events.append((
                "terminalize",
                plan_id,
                kwargs["expected_state"],
                kwargs["new_state"],
            ))
            return True

        def activate_local_push(self, *_args, **_kwargs):
            raise AssertionError("a failed landing must not activate push")

        def close(self):
            events.append("close")

    monkeypatch.setattr(bestplan_state, "BestplanStore", Store)

    with pytest.raises(bestplan_local_git.LocalMainConflict, match="landing failed"):
        delegate_tool._finish_local_bestplan_batch(
            plan_id="bp-local",
            plan=object(),
            snapshot=_snapshot(tmp_path),
            contract={"commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=[_completed(0)],
            projected_results=[{"status": "frozen", "summary": "first"}],
            runtime=_runtime(tmp_path),
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            cancel_event=None,
            now=time.time(),
        )

    assert events[-4:] == [
        "land_returned",
        ("classified", classification),
        ("terminalize", "bp-local", "prepared", terminal_state),
        "close",
    ]


def test_local_batch_leaves_an_unknown_landing_effect_prepared(
    tmp_path, monkeypatch,
):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from agent import bestplan_state
    from tools import delegate_tool

    integration = SimpleNamespace(
        integration_oid="1" * 40,
        target_oid="0" * 40,
    )
    monkeypatch.setattr(
        delegate_tool,
        "_build_local_candidate_binding",
        lambda **kwargs: kwargs["manifest_slice_id"],
    )
    monkeypatch.setattr(
        bestplan_promotion, "freeze_integration", lambda **kwargs: integration,
    )
    monkeypatch.setattr(
        bestplan_checks,
        "run_integration_checks",
        lambda **kwargs: SimpleNamespace(receipt_digest="4" * 64),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **kwargs: SimpleNamespace(
            display_url="example.invalid/repo",
            remote_ref="refs/heads/main",
        ),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **kwargs: (_ for _ in ()).throw(
            bestplan_local_git.LocalMainEffectUnknown("landing outcome unknown")
        ),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "classify_local_main_for_push",
        lambda **kwargs: pytest.fail("an unknown effect must remain prepared"),
    )

    class Store:
        def __init__(self, *, db_path):
            pass

        def prepare_local_push(self, plan_id, **_kwargs):
            return {"state": "prepared"}

        def _set_local_push_state(self, *_args, **_kwargs):
            raise AssertionError("an unknown effect must remain prepared")

        def close(self):
            pass

    monkeypatch.setattr(bestplan_state, "BestplanStore", Store)

    with pytest.raises(
        bestplan_local_git.LocalMainEffectUnknown,
        match="landing outcome unknown",
    ):
        delegate_tool._finish_local_bestplan_batch(
            plan_id="bp-local",
            plan=object(),
            snapshot=_snapshot(tmp_path),
            contract={"commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=[_completed(0)],
            projected_results=[{"status": "frozen", "summary": "first"}],
            runtime=_runtime(tmp_path),
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            cancel_event=None,
            now=time.time(),
        )


def test_local_batch_cancellation_after_prepare_closes_the_known_empty_effect(
    tmp_path, monkeypatch,
):
    from agent import bestplan_checks, bestplan_local_git, bestplan_promotion
    from agent import bestplan_state
    from tools import delegate_tool

    cancel_event = threading.Event()
    transitions: list[tuple[str, str]] = []
    integration = SimpleNamespace(
        integration_oid="1" * 40,
        target_oid="0" * 40,
    )
    monkeypatch.setattr(
        delegate_tool,
        "_build_local_candidate_binding",
        lambda **kwargs: kwargs["manifest_slice_id"],
    )
    monkeypatch.setattr(
        bestplan_promotion, "freeze_integration", lambda **kwargs: integration,
    )
    monkeypatch.setattr(
        bestplan_checks,
        "run_integration_checks",
        lambda **kwargs: SimpleNamespace(receipt_digest="4" * 64),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "observe_prelanding_local_main_push_target",
        lambda **kwargs: SimpleNamespace(
            display_url="example.invalid/repo",
            remote_ref="refs/heads/main",
        ),
    )
    monkeypatch.setattr(
        bestplan_local_git,
        "land_checked_integration",
        lambda **kwargs: pytest.fail("cancelled work must not launch Git"),
    )

    class Store:
        def __init__(self, *, db_path):
            pass

        def prepare_local_push(self, plan_id, **_kwargs):
            cancel_event.set()
            return {"state": "prepared"}

        def _set_local_push_state(self, _plan_id, **kwargs):
            transitions.append((kwargs["expected_state"], kwargs["new_state"]))
            return True

        def close(self):
            pass

    monkeypatch.setattr(bestplan_state, "BestplanStore", Store)

    with pytest.raises(delegate_tool.BestplanCandidateBatchError):
        delegate_tool._finish_local_bestplan_batch(
            plan_id="bp-local",
            plan=object(),
            snapshot=_snapshot(tmp_path),
            contract={"commands": []},
            approval_digest="5" * 64,
            contract_digest="6" * 64,
            completed=[_completed(0)],
            projected_results=[{"status": "frozen", "summary": "first"}],
            runtime=_runtime(tmp_path),
            state_db_path=tmp_path / "state.db",
            session_id="session-local",
            profile="default",
            cancel_event=cancel_event,
            now=time.time(),
        )

    assert transitions == [("prepared", "not_landed")]
