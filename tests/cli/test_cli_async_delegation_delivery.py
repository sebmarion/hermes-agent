"""Regression coverage for CLI async-delegation completion ownership."""

import queue
import time

from cli import HermesCLI


def test_cli_completion_drain_uses_visible_session_identity(monkeypatch):
    """A CLI window must not claim another window's restored completion."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()

    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_visible",
        "session_key": "visible-session",
    }
    calls = []

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            calls.append((session_key, owns_event(event)))
            return [(event, "completion payload")]

    claimed = []
    completed = []

    monkeypatch.setattr(
        "tools.process_registry.process_registry",
        FakeRegistry(),
    )
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: claimed.append((evt, consumer)) or "claim-token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: completed.append((evt, token)),
    )

    cli._drain_process_notifications("cli-idle")

    assert calls == [("visible-session", True)]
    assert cli._pending_input.get_nowait() == "completion payload"
    assert claimed == [(event, "cli-idle")]
    assert completed == [(event, "claim-token")]


def test_cli_local_bestplan_completion_prints_prompt_without_a_model_turn(
    tmp_path, monkeypatch,
):
    """A local-main success must stop at the host-owned push question."""

    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_local",
        "session_key": "visible-session",
        "bestplan_plan_id": "bp-local",
        "bestplan_local_execution": True,
    }

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            assert session_key == "visible-session"
            assert owns_event(event)
            return [(event, "untrusted candidate summary")]

    calls = []
    printed = []
    monkeypatch.setattr(
        "tools.process_registry.process_registry",
        FakeRegistry(),
    )
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: calls.append(("claim", evt, consumer)) or "token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: calls.append(("complete", evt, token)),
    )
    recovery_calls = []

    def recover(**kwargs):
        recovery_calls.append(kwargs)
        return "canonical durable push question"

    monkeypatch.setattr(
        "agent.bestplan_local_push.recover_local_push_prompt", recover,
    )
    monkeypatch.setattr(
        "agent.bestplan_local_push.LOCAL_PUSH_PROMPT_RECOVERY_SECONDS", 10.0,
    )
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    monkeypatch.chdir(tmp_path)
    cli._console_print = lambda *args, **kwargs: printed.append((args, kwargs))

    before = time.monotonic()
    cli._drain_process_notifications("cli-idle")

    assert cli._pending_input.empty()
    assert printed == [
        (("canonical durable push question",), {
            "highlight": False,
            "markup": False,
        })
    ]
    assert len(recovery_calls) == 1
    assert recovery_calls[0]["session_id"] == "visible-session"
    assert recovery_calls[0]["profile"] == "coder"
    assert recovery_calls[0]["workspace"] == str(tmp_path)
    assert before < recovery_calls[0]["deadline"] <= before + 10.1
    assert [item[0] for item in calls] == ["claim", "complete"]


def test_cli_local_bestplan_terminal_failure_reports_host_safe_reason(
    tmp_path, monkeypatch,
):
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_local",
        "session_key": "visible-session",
        "bestplan_plan_id": "bp-local",
        "bestplan_local_execution": True,
        "status": "error",
        "error": "candidate_batch_failed",
    }

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            return [(event, "untrusted candidate summary")]

    completed = []
    printed = []
    monkeypatch.setattr(
        "tools.process_registry.process_registry",
        FakeRegistry(),
    )
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda _evt, _consumer: "token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: completed.append((evt, token)),
    )
    monkeypatch.setattr(
        "agent.bestplan_local_push.recover_local_push_prompt",
        lambda **_kwargs: None,
    )
    failed_row = {
        "plan_id": "bp-local",
        "session_id": "visible-session",
        "profile": "",
        "workspace": str(tmp_path),
        "execution_protocol": 2,
        "promotion_contract_version": 1,
        "promotion_mode": "local_main",
        "state": "failed",
        "local_push_state": "not_landed",
    }

    class FakeStore:
        def __init__(self, *args, **kwargs):
            assert kwargs == {"reconcile_push_state": False}

        def list_for_session(self, session_id, *, open_only=True):
            assert session_id == "visible-session"
            assert open_only is False
            return [failed_row]

        def close(self):
            pass

    monkeypatch.setattr("agent.bestplan_state.BestplanStore", FakeStore)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.chdir(tmp_path)
    cli._console_print = lambda *args, **kwargs: printed.append((args, kwargs))

    cli._drain_process_notifications("cli-idle")

    assert cli._pending_input.empty()
    assert printed == [
        ((
            "BestPlan local execution failed: candidate batch failed. "
            "No remote push was attempted.",
        ), {"highlight": False, "markup": False})
    ]
    assert completed == [(event, "token")]


def test_cli_local_bestplan_prepared_completion_retries_until_prompt(
    tmp_path, monkeypatch,
):
    """A lost wrapper must not consume a still-unresolved local Git effect."""

    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_local",
        "session_key": "visible-session",
        "bestplan_plan_id": "bp-local",
        "bestplan_local_execution": True,
        "status": "lost",
        "error": "async delegation lost during process restart",
    }

    class FakeRegistry:
        def __init__(self):
            self.calls = 0
            self.completion_queue = queue.Queue()

        def drain_notifications(self, *, session_key="", owns_event=None):
            self.calls += 1
            if self.calls == 1:
                assert session_key == "visible-session"
                assert owns_event(event)
                return [(event, "untrusted candidate summary")]
            try:
                retried = self.completion_queue.get_nowait()
            except queue.Empty:
                return []
            assert owns_event(retried)
            return [(retried, "untrusted candidate summary")]

    prepared_row = {
        "plan_id": "bp-local",
        "session_id": "visible-session",
        "profile": "",
        "workspace": str(tmp_path),
        "execution_protocol": 2,
        "promotion_contract_version": 1,
        "promotion_mode": "local_main",
        "state": "waiting",
        "local_push_state": "prepared",
    }

    class FakeStore:
        def __init__(self, *args, **kwargs):
            assert kwargs == {"reconcile_push_state": False}

        def list_for_session(self, session_id, *, open_only=True):
            assert session_id == "visible-session"
            assert open_only is False
            return [prepared_row]

        def close(self):
            pass

    registry = FakeRegistry()
    claims = []
    completed = []
    released = []
    printed = []
    recoveries = iter([None, "canonical durable push question"])
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: claims.append((evt, consumer)) or f"token-{len(claims)}",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: completed.append((evt, token)),
    )
    monkeypatch.setattr(
        "tools.async_delegation.release_completion_delivery",
        lambda delegation_id, token: released.append((delegation_id, token)) or True,
    )
    monkeypatch.setattr(
        "agent.bestplan_local_push.recover_local_push_prompt",
        lambda **_kwargs: next(recoveries),
    )
    monkeypatch.setattr("agent.bestplan_state.BestplanStore", FakeStore)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setattr(
        "cli._BESTPLAN_LOCAL_COMPLETION_RETRY_SECONDS", 0.0, raising=False,
    )
    monkeypatch.chdir(tmp_path)
    cli._console_print = lambda *args, **kwargs: printed.append((args, kwargs))

    cli._drain_process_notifications("cli-idle")

    assert printed == []
    assert completed == []
    assert released == [("deleg_local", "token-1")]

    cli._drain_process_notifications("cli-idle")

    assert printed == [
        (("canonical durable push question",), {
            "highlight": False,
            "markup": False,
        })
    ]
    assert len(claims) == 2
    assert completed == [(event, "token-2")]
    assert cli._pending_input.empty()


def test_cli_completion_ownership_rejects_foreign_session():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._session_db = None

    assert not cli._owns_process_notification(
        {"type": "async_delegation", "session_key": "foreign-session"}
    )


def test_cli_completion_ownership_accepts_compression_lineage():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"

    class FakeSessionDB:
        def resolve_resume_session_id(self, session_id):
            assert session_id == "pre-compression-session"
            return "visible-session"

    cli._session_db = FakeSessionDB()

    assert cli._owns_process_notification(
        {
            "type": "async_delegation",
            "session_key": "pre-compression-session",
        }
    )
