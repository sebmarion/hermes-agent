"""Tests that internal synthetic events (e.g. background process completion)
bypass user authorization and do not trigger DM pairing.

Regression tests for the durable completion-queue owner. Completion events
must retain their routing identity, bypass user authorization, and never
trigger DM pairing when they are injected as internal messages.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tools.process_registry import ProcessRegistry, ProcessSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_runner(monkeypatch, tmp_path) -> GatewayRunner:
    """Create a GatewayRunner with notifications set to 'all'."""
    (tmp_path / "config.yaml").write_text(
        "display:\n  background_process_notifications: all\n",
        encoding="utf-8",
    )

    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock())
    runner.adapters[Platform.DISCORD] = adapter
    return runner


def _durable_completion_event(**overrides):
    event = {
        "type": "completion",
        "event_id": "process:proc_test_internal:completion",
        "session_id": "proc_test_internal",
        "session_key": "agent:main:discord:dm:123",
        "platform": "discord",
        "chat_type": "dm",
        "chat_id": "123",
        "thread_id": "",
        "command": "echo test",
        "exit_code": 0,
        "output": "done\n",
    }
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_on_complete_sets_internal_flag(monkeypatch, tmp_path):
    """Synthetic completion event must have internal=True."""
    runner = _build_runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.DISCORD]
    event = _durable_completion_event()

    await runner._inject_watch_notification("[SYSTEM: process completed]", event)

    assert adapter.handle_message.await_count == 1
    message = adapter.handle_message.await_args.args[0]
    assert isinstance(message, MessageEvent)
    assert message.internal is True, "Synthetic completion event must be marked internal"
    assert message.metadata["_hermes_durable_notification"] == event


@pytest.mark.asyncio
async def test_poll_does_not_suppress_notify_on_complete_watcher(monkeypatch, tmp_path):
    """Regression: polling an exited process must not suppress queue injection."""
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_polled_completion",
        command="echo done",
        output_buffer="done\n",
        exited=True,
        exit_code=0,
        notify_on_complete=True,
    )
    registry._finished[session.id] = session

    poll_result = registry.poll(session.id)
    assert poll_result["status"] == "exited"
    assert not registry.is_completion_consumed(session.id)

    runner = _build_runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.DISCORD]
    event = _durable_completion_event(
        event_id=f"process:{session.id}:completion",
        session_id=session.id,
        command=session.command,
        exit_code=session.exit_code,
        output=session.output_buffer,
    )

    await runner._inject_watch_notification("[SYSTEM: process completed]", event)

    assert adapter.handle_message.await_count == 1
    message = adapter.handle_message.await_args.args[0]
    assert message.metadata["_hermes_durable_notification"] == event
    assert message.internal is True


@pytest.mark.asyncio
async def test_internal_event_bypasses_authorization(monkeypatch, tmp_path):
    """An internal event should skip _is_user_authorized entirely."""
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")

    runner = GatewayRunner(GatewayConfig())

    # Create an internal event with no user_id (simulates the bug scenario)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="dm",
    )
    event = MessageEvent(
        text="[SYSTEM: Background process completed]",
        source=source,
        internal=True,
    )

    # Track if _is_user_authorized is called
    auth_called = False
    original_auth = GatewayRunner._is_user_authorized

    def tracking_auth(self, src):
        nonlocal auth_called
        auth_called = True
        return original_auth(self, src)

    monkeypatch.setattr(GatewayRunner, "_is_user_authorized", tracking_auth)

    # Stop execution before the agent runner so the test doesn't block in
    # run_in_executor.  Auth check happens before _handle_message_with_agent.
    async def _raise(*_a, **_kw):
        raise RuntimeError("sentinel — stop here")
    monkeypatch.setattr(GatewayRunner, "_handle_message_with_agent", _raise)

    try:
        await runner._handle_message(event)
    except RuntimeError:
        pass  # Expected sentinel

    assert not auth_called, (
        "_is_user_authorized should NOT be called for internal events"
    )


@pytest.mark.asyncio
async def test_internal_event_does_not_trigger_pairing(monkeypatch, tmp_path):
    """An internal event with no user_id must not generate a pairing code."""
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")

    runner = GatewayRunner(GatewayConfig())
    # Add adapter so pairing would have somewhere to send
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters[Platform.DISCORD] = adapter

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="dm",  # DM would normally trigger pairing
    )
    event = MessageEvent(
        text="[SYSTEM: Background process completed]",
        source=source,
        internal=True,
    )

    # Track pairing code generation
    generate_called = False
    original_generate = runner.pairing_store.generate_code

    def tracking_generate(*args, **kwargs):
        nonlocal generate_called
        generate_called = True
        return original_generate(*args, **kwargs)

    runner.pairing_store.generate_code = tracking_generate

    # Stop execution before the agent runner so the test doesn't block in
    # run_in_executor.  Pairing check happens before _handle_message_with_agent.
    async def _raise(*_a, **_kw):
        raise RuntimeError("sentinel — stop here")
    monkeypatch.setattr(GatewayRunner, "_handle_message_with_agent", _raise)

    try:
        await runner._handle_message(event)
    except RuntimeError:
        pass  # Expected sentinel

    assert not generate_called, (
        "Pairing code should NOT be generated for internal events"
    )


@pytest.mark.asyncio
async def test_notify_on_complete_preserves_user_identity(monkeypatch, tmp_path):
    """Synthetic completion event should carry user_id and user_name from the watcher."""
    runner = _build_runner(monkeypatch, tmp_path)
    adapter = runner.adapters[Platform.DISCORD]
    event = _durable_completion_event(user_id="user-42", user_name="alice")

    await runner._inject_watch_notification("[SYSTEM: process completed]", event)

    assert adapter.handle_message.await_count == 1
    message = adapter.handle_message.await_args.args[0]
    assert message.source.user_id == "user-42"
    assert message.source.user_name == "alice"


@pytest.mark.asyncio
async def test_notify_on_complete_uses_session_store_origin_for_group_topic(monkeypatch, tmp_path):
    from gateway.session import SessionSource

    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock())
    runner.adapters[Platform.TELEGRAM] = adapter
    runner.session_store._entries["agent:main:telegram:group:-100:42"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100",
            chat_type="group",
            thread_id="42",
            user_id="user-42",
            user_name="alice",
        )
    )

    event = _durable_completion_event(
        session_key="agent:main:telegram:group:-100:42",
        platform="telegram",
        chat_type="group",
        chat_id="-100",
        thread_id="42",
    )

    await runner._inject_watch_notification("[SYSTEM: process completed]", event)

    assert adapter.handle_message.await_count == 1
    message = adapter.handle_message.await_args.args[0]
    assert message.internal is True
    assert message.source.platform == Platform.TELEGRAM
    assert message.source.chat_id == "-100"
    assert message.source.chat_type == "group"
    assert message.source.thread_id == "42"
    assert message.source.user_id == "user-42"
    assert message.source.user_name == "alice"


@pytest.mark.asyncio
async def test_none_user_id_skips_pairing(monkeypatch, tmp_path):
    """A non-internal event with user_id=None should be silently dropped."""
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")

    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters[Platform.TELEGRAM] = adapter

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_type="dm",
        user_id=None,
    )
    event = MessageEvent(
        text="service message",
        source=source,
        internal=False,
    )

    result = await runner._handle_message(event)

    # Should return None (dropped) and NOT send any pairing message
    assert result is None
    assert adapter.send.await_count == 0


@pytest.mark.asyncio
async def test_none_user_id_does_not_generate_pairing_code(monkeypatch, tmp_path):
    """A message with user_id=None must never call generate_code."""
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")

    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters[Platform.DISCORD] = adapter

    generate_called = False
    original_generate = runner.pairing_store.generate_code

    def tracking_generate(*args, **kwargs):
        nonlocal generate_called
        generate_called = True
        return original_generate(*args, **kwargs)

    runner.pairing_store.generate_code = tracking_generate

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="456",
        chat_type="dm",
        user_id=None,
    )
    event = MessageEvent(text="anonymous", source=source, internal=False)

    await runner._handle_message(event)

    assert not generate_called, (
        "Pairing code should NOT be generated for messages with user_id=None"
    )


@pytest.mark.asyncio
async def test_non_internal_event_without_user_triggers_pairing(monkeypatch, tmp_path):
    """Verify the normal (non-internal) path still triggers pairing for unknown users."""
    import gateway.run as gateway_run
    import gateway.pairing as pairing_mod

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    # gateway.pairing.PAIRING_DIR is a module-level constant captured at
    # import time from whichever HERMES_HOME was set then. Per-test
    # HERMES_HOME redirection in conftest doesn't retroactively move it.
    # Override directly so pairing rate-limit state lives in this test's
    # tmp_path (and so stale state from prior xdist workers can't leak in).
    pairing_dir = tmp_path / "pairing"
    pairing_dir.mkdir()
    monkeypatch.setattr(pairing_mod, "PAIRING_DIR", pairing_dir)
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")

    # Clear env vars that could let all users through (loaded by
    # module-level dotenv in gateway/run.py from the real ~/.hermes/.env).
    monkeypatch.delenv("DISCORD_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)

    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters[Platform.DISCORD] = adapter

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="dm",
        user_id="unknown_user_999",
    )
    # Normal event (not internal)
    event = MessageEvent(
        text="hello",
        source=source,
        internal=False,
    )

    result = await runner._handle_message(event)

    # Should return None (unauthorized) and send pairing message
    assert result is None
    assert adapter.send.await_count == 1
    sent_text = adapter.send.await_args.args[1]
    assert "don't recognize you" in sent_text
