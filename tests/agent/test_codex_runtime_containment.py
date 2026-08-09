from types import SimpleNamespace


def test_bestplan_read_only_runtime_enables_isolated_containment(monkeypatch, tmp_path):
    import agent.codex_runtime as codex_runtime
    import agent.transports.codex_app_server_session as session_module

    captured = {}

    class FakeSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.multi_agent_enabled = bool(kwargs.get("enable_multi_agent"))

        def run_turn(self, **kwargs):
            captured["turn"] = kwargs
            return SimpleNamespace(
                final_text="candidate",
                projected_messages=[],
                tool_iterations=0,
                interrupted=False,
                error=None,
                should_retire=False,
                thread_id="thread",
                turn_id="turn",
            )

        def close(self):
            pass

    monkeypatch.setattr(session_module, "CodexAppServerSession", FakeSession)
    monkeypatch.setattr(
        codex_runtime,
        "make_codex_app_server_event_bridge",
        lambda _agent: lambda _note: None,
    )
    monkeypatch.setattr(
        codex_runtime,
        "_record_codex_app_server_compaction",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        codex_runtime,
        "_record_codex_app_server_usage",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "tools.terminal_tool._get_approval_callback",
        lambda: lambda *_args, **_kwargs: "always",
    )
    monkeypatch.setattr("tools.approval.is_approval_bypass_active", lambda: True)
    agent = SimpleNamespace(
        model="gpt-5.6-sol",
        reasoning_config={"enabled": True, "effort": "ultra"},
        session_cwd=str(tmp_path),
        _bestplan_read_only=True,
        _codex_session=None,
        _interrupt_requested=False,
        _interrupt_message=None,
        _iters_since_skill=0,
        _skill_nudge_interval=0,
        valid_tool_names=set(),
        _session_db=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        tool_progress_callback=None,
        _sync_external_memory_for_turn=lambda **_kwargs: None,
    )

    result = codex_runtime.run_codex_app_server_turn(
        agent,
        user_message="inspect",
        original_user_message="inspect",
        messages=[],
        effective_task_id="bestplan-sol",
    )

    assert result["completed"] is True
    assert captured["isolated_read_only"] is True
    assert captured["permission_profile"] == "read-only"
    assert captured["approval_callback"] is None
    assert captured["request_routing"].auto_approve_exec is False
    assert captured["request_routing"].auto_approve_apply_patch is False
    assert captured["client_extra_args"] == [
        "-c",
        'sandbox_mode="read-only"',
        "-c",
        'approval_policy="never"',
    ]


def test_ordinary_runtime_does_not_enable_isolated_containment(monkeypatch, tmp_path):
    import agent.codex_runtime as codex_runtime
    import agent.transports.codex_app_server_session as session_module

    captured = {}
    approval_callback = lambda *_args, **_kwargs: "once"

    class FakeSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.multi_agent_enabled = bool(kwargs.get("enable_multi_agent"))

        def run_turn(self, **_kwargs):
            return SimpleNamespace(
                final_text="ordinary",
                projected_messages=[],
                tool_iterations=0,
                interrupted=False,
                error=None,
                should_retire=False,
                thread_id="thread",
                turn_id="turn",
            )

        def close(self):
            pass

    monkeypatch.setattr(session_module, "CodexAppServerSession", FakeSession)
    monkeypatch.setattr(
        codex_runtime,
        "make_codex_app_server_event_bridge",
        lambda _agent: lambda _note: None,
    )
    monkeypatch.setattr(
        codex_runtime,
        "_record_codex_app_server_compaction",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        codex_runtime,
        "_record_codex_app_server_usage",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "tools.terminal_tool._get_approval_callback",
        lambda: approval_callback,
    )
    monkeypatch.setattr("tools.approval.is_approval_bypass_active", lambda: False)
    agent = SimpleNamespace(
        model="gpt-5.6-sol",
        reasoning_config={"enabled": True, "effort": "high"},
        session_cwd=str(tmp_path),
        _codex_session=None,
        _interrupt_requested=False,
        _interrupt_message=None,
        _iters_since_skill=0,
        _skill_nudge_interval=0,
        valid_tool_names=set(),
        _session_db=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        tool_progress_callback=None,
        _sync_external_memory_for_turn=lambda **_kwargs: None,
    )

    result = codex_runtime.run_codex_app_server_turn(
        agent,
        user_message="work",
        original_user_message="work",
        messages=[],
        effective_task_id="ordinary",
    )

    assert result["completed"] is True
    assert captured.get("isolated_read_only", False) is False
    assert captured["approval_callback"] is approval_callback
    assert "client_extra_args" not in captured
