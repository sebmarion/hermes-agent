"""Tests for the external drain-control marker contract + gateway state machine.

Task 2.2/2.3. Two layers:
  * drain_control.py — the presence-based marker contract (write/clear/read,
    HERMES_HOME-scoped, never-raises).
  * GatewayRunner enter/exit/watcher + the new-turn accept gate — the
    reversible state machine driven by the marker.

Mocked tests are necessary-not-sufficient here (the HARD live-validation gate,
Q-B, exercises a real `hermes gateway run`); these lock the unit contract.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import gateway.drain_control as dc
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.platforms.base import MessageEvent, MessageType
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from utils import atomic_json_write


# ---------------------------------------------------------------------------
# Marker contract (drain_control.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _pair_gate_payload(
    transaction_id: str = "pair_open_gate_transaction_00000001",
) -> dict:
    payload = {
        "schema": "hermes.pair_open_gate.v1",
        "action": "hold_pair_open",
        "transaction_id": transaction_id,
        "owner_hash": "",
        "created_at": "2026-07-23T00:00:00+00:00",
        "epoch": 7,
        "agent": {
            "build_id": "agent-build-a",
            "pid": 101,
            "start_time": "agent-start-a",
            "instance_epoch": "agent-epoch-a",
        },
        "webui": {
            "build_id": "webui-build-a",
            "pid": 202,
            "start_time": "webui-start-a",
            "instance_epoch": "webui-epoch-a",
        },
    }
    payload["owner_hash"] = dc.pair_open_gate_owner_hash(payload)
    return payload


def _write_pair_gate(home: Path, payload: dict | None = None, mode: int = 0o600) -> dict:
    body = payload or _pair_gate_payload()
    atomic_json_write(
        dc.pair_open_gate_path(home),
        body,
        mode=mode,
        sort_keys=True,
    )
    return body


class TestMarkerContract:
    def test_absent_by_default(self, home):
        assert dc.drain_requested() is False
        assert dc.read_drain_request() is None

    def test_write_then_present(self, home):
        payload = dc.write_drain_request(principal="nas")
        assert dc.drain_requested() is True
        assert payload["action"] == "drain"
        assert payload["principal"] == "nas"
        body = dc.read_drain_request()
        assert body is not None and body["principal"] == "nas"

    def test_clear_removes(self, home):
        dc.write_drain_request()
        assert dc.clear_drain_request() is True
        assert dc.drain_requested() is False
        # idempotent: clearing again is a no-op, returns False
        assert dc.clear_drain_request() is False

    def test_path_respects_hermes_home(self, home):
        assert dc.drain_request_path() == home / ".drain_request.json"

    def test_corrupt_marker_reads_as_present_contentless(self, home):
        # A half-written / malformed marker must still count as "drain active"
        # (fail-safe toward quiescing).
        dc.drain_request_path().write_text("{not valid json", encoding="utf-8")
        assert dc.drain_requested() is True
        assert dc.read_drain_request() == {}

    def test_unreadable_present_marker_fails_closed(self, home, monkeypatch):
        path = dc.drain_request_path()
        dc.write_drain_request(principal="nas")
        real_read_text = Path.read_text

        def fail_marker_read(candidate, *args, **kwargs):
            if candidate == path:
                raise OSError("injected read failure")
            return real_read_text(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_marker_read)

        assert dc.read_drain_request() is None
        assert dc.drain_requested() is True
        assert dc.admission_rejection_requested() is True

    def test_write_is_atomic_json(self, home):
        dc.write_drain_request(principal="x")
        import json

        data = json.loads(dc.drain_request_path().read_text())
        assert data["action"] == "drain"


class TestPairOpenGateContract:
    def test_absent_gate_is_verified_open_state(self, home):
        receipt = dc.pair_open_gate_receipt()

        assert receipt == {
            "active": False,
            "verified": True,
            "reason": "absent",
        }
        assert dc.admission_rejection_requested() is False

    def test_valid_gate_receipt_binds_exact_transaction_and_candidates(self, home):
        payload = _write_pair_gate(home)

        receipt = dc.pair_open_gate_receipt()

        assert receipt["active"] is True
        assert receipt["verified"] is True
        assert receipt["reason"] == "verified"
        assert receipt["transaction_id"] == payload["transaction_id"]
        assert receipt["owner_hash"] == payload["owner_hash"]
        assert receipt["agent"] == payload["agent"]
        assert receipt["webui"] == payload["webui"]
        assert len(receipt["payload_sha256"]) == 64
        assert dc.pair_open_gate_path().stat().st_mode & 0o777 == 0o600
        assert dc.admission_rejection_requested() is True

    def test_different_release_epoch_gate_never_auto_expires(self, home):
        payload = _pair_gate_payload()
        payload["epoch"] = 6
        payload["owner_hash"] = dc.pair_open_gate_owner_hash(payload)
        _write_pair_gate(home, payload)

        receipt = dc.pair_open_gate_receipt()

        assert receipt["active"] is True
        assert receipt["verified"] is True
        assert receipt["epoch"] == 6
        assert dc.admission_rejection_requested() is True

    def test_path_replacement_during_read_is_not_attested(self, home, monkeypatch):
        path = dc.pair_open_gate_path()
        _write_pair_gate(
            home,
            _pair_gate_payload("pair_open_gate_original_0000000001"),
        )
        real_lstat = Path.lstat
        target_calls = 0

        def replace_before_reattest(candidate: Path):
            nonlocal target_calls
            if candidate == path:
                target_calls += 1
                if target_calls == 2:
                    _write_pair_gate(
                        home,
                        _pair_gate_payload("pair_open_gate_replacement_0000001"),
                    )
            return real_lstat(candidate)

        monkeypatch.setattr(Path, "lstat", replace_before_reattest)

        receipt = dc.pair_open_gate_receipt()

        assert receipt == {
            "active": True,
            "verified": False,
            "reason": "identity_changed",
        }

    def test_in_place_content_change_during_read_is_not_attested(
        self, home, monkeypatch
    ):
        path = dc.pair_open_gate_path()
        _write_pair_gate(home)
        original = path.read_bytes()
        assert b"agent-build-a" in original
        replacement = original.replace(b"agent-build-a", b"agent-build-b", 1)
        assert len(replacement) == len(original)
        real_read = dc.os.read
        changed = False

        def change_same_inode_after_read(fd: int, size: int) -> bytes:
            nonlocal changed
            chunk = real_read(fd, size)
            if chunk and not changed:
                changed = True
                path.write_bytes(replacement)
                path.chmod(0o600)
            return chunk

        monkeypatch.setattr(dc.os, "read", change_same_inode_after_read)

        receipt = dc.pair_open_gate_receipt()

        assert receipt == {
            "active": True,
            "verified": False,
            "reason": "identity_changed",
        }

    def test_post_read_mode_change_is_not_attested(self, home, monkeypatch):
        _write_pair_gate(home)
        real_fstat = dc.os.fstat
        fstat_calls = 0

        def change_mode_on_reattest(fd: int):
            nonlocal fstat_calls
            observed = real_fstat(fd)
            fstat_calls += 1
            if fstat_calls == 2:
                values = list(observed)
                values[0] = (values[0] & ~0o777) | 0o644
                return dc.os.stat_result(values)
            return observed

        monkeypatch.setattr(dc.os, "fstat", change_mode_on_reattest)

        receipt = dc.pair_open_gate_receipt()

        assert receipt == {
            "active": True,
            "verified": False,
            "reason": "identity_changed",
        }

    def test_gate_fails_closed_when_nofollow_is_unavailable(
        self, home, monkeypatch
    ):
        _write_pair_gate(home)
        monkeypatch.delattr(dc.os, "O_NOFOLLOW", raising=False)

        assert dc.pair_open_gate_receipt() == {
            "active": True,
            "verified": False,
            "reason": "nofollow_unavailable",
        }

    def test_owner_hash_tamper_is_invalid_but_still_fences(self, home):
        payload = _pair_gate_payload()
        payload["webui"]["build_id"] = "tampered-after-owner-hash"
        _write_pair_gate(home, payload)

        receipt = dc.pair_open_gate_receipt()

        assert receipt["active"] is True
        assert receipt["verified"] is False
        assert receipt["reason"] == "invalid_payload"
        assert dc.admission_rejection_requested() is True

    @pytest.mark.parametrize(
        ("contents", "mode", "reason"),
        [
            ("{not json", 0o600, "malformed"),
            ('{"transaction_id":"tx"}', 0o600, "invalid_payload"),
            ('{"schema":"hermes.pair_open_gate.v1"}', 0o644, "unsafe_mode"),
        ],
    )
    def test_unverified_gate_still_fails_closed(self, home, contents, mode, reason):
        path = dc.pair_open_gate_path()
        path.write_text(contents, encoding="utf-8")
        path.chmod(mode)

        receipt = dc.pair_open_gate_receipt()

        assert receipt["active"] is True
        assert receipt["verified"] is False
        assert receipt["reason"] == reason
        assert dc.admission_rejection_requested() is True


class TestSuppressNotification:
    """The generic suppress_notification flag on the drain marker.

    Gates ONLY the gateway's home-channel shutdown broadcast (NAS auto-update
    sets it true). Default-false so legacy/operator drains behave as before.
    The reader reuses the NS-570 epoch-staleness check so an orphaned marker
    can never silence a fresh gateway.
    """

    def test_default_false(self, home):
        payload = dc.write_drain_request(principal="nas")
        assert payload["suppress_notification"] is False
        assert dc.drain_notification_suppressed() is False

    def test_flag_round_trips_true(self, home):
        payload = dc.write_drain_request(principal="nas", suppress_notification=True)
        assert payload["suppress_notification"] is True
        body = dc.read_drain_request()
        assert body is not None and body["suppress_notification"] is True
        assert dc.drain_notification_suppressed() is True

    def test_suppressed_false_when_no_marker(self, home):
        assert dc.drain_notification_suppressed() is False

    def test_legacy_marker_without_field_not_suppressed(self, home):
        # A marker written before this change has no suppress_notification key →
        # must read as not-suppressed (broadcast still fires), while still being
        # an active drain.
        import json

        dc.drain_request_path().write_text(
            json.dumps({"action": "drain", "epoch": dc.current_instantiation_epoch()}),
            encoding="utf-8",
        )
        assert dc.drain_requested() is True
        assert dc.drain_notification_suppressed() is False

    def test_corrupt_marker_not_suppressed(self, home):
        # Half-written marker → read_drain_request returns {} → no flag → not
        # suppressed (fail toward the louder, visible behaviour) even though the
        # drain itself stays active (fail-safe toward quiescing).
        dc.drain_request_path().write_text("{not valid json", encoding="utf-8")
        assert dc.drain_requested() is True
        assert dc.drain_notification_suppressed() is False

    def test_stale_epoch_marker_not_suppressed(self, home, monkeypatch):
        # THE NS-570 ANALOGUE for suppression: a suppress_notification:true
        # marker that survived a machine restart on the durable volume must NOT
        # silence the freshly-restarted gateway's legitimate shutdown broadcast.
        monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "epoch-OLD")
        dc.write_drain_request(principal="nas", suppress_notification=True)
        assert dc.drain_notification_suppressed() is True  # same epoch → honoured

        monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "epoch-NEW")
        assert dc.drain_request_path().exists() is True
        assert dc.drain_notification_suppressed() is False  # stale → ignored


# ---------------------------------------------------------------------------
# Instantiation-epoch staleness (NS-570: orphaned marker on durable volume)
# ---------------------------------------------------------------------------


class TestInstantiationEpoch:
    def test_write_stamps_current_epoch(self, home):
        payload = dc.write_drain_request(principal="nas")
        assert payload["epoch"] == dc.current_instantiation_epoch()
        body = dc.read_drain_request()
        assert body is not None and body["epoch"] == dc.current_instantiation_epoch()

    def test_current_epoch_is_stable_within_process(self):
        # Memoised — an s6 respawn of just the gateway keeps PID 1, so a
        # repeated call inside one process must return the same value (an
        # in-flight drain stays honoured).
        assert dc.current_instantiation_epoch() == dc.current_instantiation_epoch()

    def test_marker_from_prior_instantiation_reads_as_absent(self, home, monkeypatch):
        # THE NS-570 REGRESSION. A begin-drain marker written by a PREVIOUS
        # container/VM instantiation survives on the durable HERMES_HOME volume
        # across a machine restart. The freshly-restarted gateway (new epoch)
        # must treat it as absent, NOT re-engage drain.
        monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "epoch-OLD")
        dc.write_drain_request(principal="nas")  # stamps "epoch-OLD"
        assert dc.drain_requested() is True  # same epoch → active

        # Simulate the restart: a brand-new instantiation epoch.
        monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "epoch-NEW")
        # The marker file is still physically present on the volume…
        assert dc.drain_request_path().exists() is True
        # …but it is ignored because its epoch belongs to a prior instantiation.
        assert dc.drain_requested() is False

    def test_marker_from_current_instantiation_is_honoured(self, home, monkeypatch):
        monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "epoch-A")
        dc.write_drain_request()
        assert dc.drain_requested() is True

    def test_legacy_marker_without_epoch_still_active(self, home):
        # A marker written before this change (no "epoch" key) must remain
        # fail-safe toward quiescing — never silently ignored.
        import json

        dc.drain_request_path().write_text(
            json.dumps({"action": "drain", "requested_at": "x", "principal": "p"}),
            encoding="utf-8",
        )
        assert dc.drain_requested() is True

    def test_corrupt_marker_with_no_parseable_epoch_still_active(self, home):
        # Half-written / malformed → read_drain_request returns {} → no epoch →
        # lenient check keeps it active (fail-safe), same as before the change.
        dc.drain_request_path().write_text("{not valid json", encoding="utf-8")
        assert dc.drain_requested() is True

    def test_unavailable_epoch_disables_staleness_check(self, home, monkeypatch):
        # No /proc (non-Linux, etc.) → epoch "" → degrade to presence-only:
        # any present marker (even with a foreign epoch) reads as active rather
        # than fail-closed.
        import json

        dc.drain_request_path().write_text(
            json.dumps({"action": "drain", "epoch": "some-other-epoch"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "")
        assert dc.drain_requested() is True

    def test_current_epoch_empty_when_proc_unreadable(self, monkeypatch):
        # When neither /proc identity source is readable, the epoch is "" so
        # the staleness check is disabled rather than crashing.
        from pathlib import Path as _P

        orig_read_text = _P.read_text

        def _boom(self, *a, **k):
            if str(self).startswith("/proc/"):
                raise OSError("no /proc")
            return orig_read_text(self, *a, **k)

        dc.current_instantiation_epoch.cache_clear()
        monkeypatch.setattr(_P, "read_text", _boom)
        try:
            assert dc.current_instantiation_epoch() == ""
        finally:
            dc.current_instantiation_epoch.cache_clear()


# ---------------------------------------------------------------------------
# Gateway state machine (enter / exit / idempotency)
# ---------------------------------------------------------------------------


def _drain_runner():
    runner, adapter = make_restart_runner()
    runner._external_drain_active = False
    # Bind the real methods under test.
    runner._enter_external_drain = GatewayRunner._enter_external_drain.__get__(
        runner, GatewayRunner
    )
    runner._exit_external_drain = GatewayRunner._exit_external_drain.__get__(
        runner, GatewayRunner
    )
    return runner, adapter


class TestDrainStateMachine:
    @pytest.mark.asyncio
    async def test_finite_pre_drain_work_is_counted_and_may_finish(self):
        runner, _ = _drain_runner()
        event = MessageEvent(
            text="durable completion",
            message_type=MessageType.TEXT,
            source=make_restart_source(),
            internal=True,
            metadata={},
        )

        assert runner._try_begin_drain_sensitive_background_work() is True
        event.metadata["_hermes_drain_admission_task"] = asyncio.current_task()
        assert runner._drain_sensitive_background_work == 1

        runner._external_drain_active = True
        assert runner._try_begin_drain_sensitive_background_work() is False
        assert runner._internal_turn_was_admitted_before_drain(event) is True

        runner._end_drain_sensitive_background_work()
        assert runner._drain_sensitive_background_work == 0
        assert runner._internal_turn_was_admitted_before_drain(event) is False

    def test_enter_closes_cron_gate_before_publishing_draining(self):
        runner, _ = _drain_runner()
        order = []
        runner._update_runtime_status.side_effect = lambda state: order.append(
            ("status", state)
        )

        with patch(
            "cron.scheduler.set_cron_dispatch_paused",
            side_effect=lambda paused: order.append(("cron", paused)),
            create=True,
        ):
            runner._enter_external_drain()

        assert order == [("cron", True), ("status", "draining")]

    def test_enter_sets_flag_and_flips_state(self):
        runner, _ = _drain_runner()
        runner._enter_external_drain()
        assert runner._external_drain_active is True
        runner._update_runtime_status.assert_called_with("draining")

    def test_enter_idempotent(self):
        runner, _ = _drain_runner()
        runner._enter_external_drain()
        runner._update_runtime_status.reset_mock()
        runner._enter_external_drain()  # second call — no-op
        runner._update_runtime_status.assert_not_called()

    def test_exit_reverts_to_running(self):
        runner, _ = _drain_runner()
        runner._enter_external_drain()
        runner._update_runtime_status.reset_mock()
        runner._exit_external_drain()
        assert runner._external_drain_active is False
        runner._update_runtime_status.assert_called_with("running")

    def test_exit_reopens_cron_before_publishing_running(self):
        runner, _ = _drain_runner()
        runner._external_drain_active = True
        order = []
        runner._update_runtime_status.side_effect = lambda state: order.append(
            ("status", state)
        )

        with patch(
            "cron.scheduler.set_cron_dispatch_paused",
            side_effect=lambda paused: order.append(("cron", paused)),
        ):
            runner._exit_external_drain()

        assert order == [("cron", False), ("status", "running")]
        assert runner._external_drain_active is False

    def test_exit_remains_draining_when_cron_gate_cannot_reopen(self):
        runner, _ = _drain_runner()
        runner._external_drain_active = True

        with patch(
            "cron.scheduler.set_cron_dispatch_paused",
            side_effect=RuntimeError("shared cron gate unavailable"),
        ), pytest.raises(RuntimeError, match="shared cron gate unavailable"):
            runner._exit_external_drain()

        assert runner._external_drain_active is True
        runner._update_runtime_status.assert_not_called()

    def test_exit_idempotent_when_not_draining(self):
        runner, _ = _drain_runner()
        runner._exit_external_drain()  # never entered — no-op
        runner._update_runtime_status.assert_not_called()

    def test_exit_during_shutdown_does_not_revert_to_running(self):
        runner, _ = _drain_runner()
        runner._enter_external_drain()
        runner._update_runtime_status.reset_mock()
        # A shutdown drain is now in progress — exit must NOT resurrect running.
        runner._draining = True
        runner._exit_external_drain()
        assert runner._external_drain_active is False
        runner._update_runtime_status.assert_not_called()

    def test_exit_when_loop_stopped_does_not_revert(self):
        runner, _ = _drain_runner()
        runner._enter_external_drain()
        runner._update_runtime_status.reset_mock()
        runner._running = False
        runner._exit_external_drain()
        runner._update_runtime_status.assert_not_called()


# ---------------------------------------------------------------------------
# Watcher reconciliation
# ---------------------------------------------------------------------------


class TestDrainWatcher:
    @pytest.mark.asyncio
    async def test_watcher_enters_then_exits_with_marker(self, home):
        runner, _ = _drain_runner()
        runner._drain_control_watcher = GatewayRunner._drain_control_watcher.__get__(
            runner, GatewayRunner
        )
        # Drive a few ticks manually rather than spinning the loop.
        dc.write_drain_request()
        task = asyncio.create_task(runner._drain_control_watcher(interval=0.02))
        await asyncio.sleep(0.06)
        assert runner._external_drain_active is True
        dc.clear_drain_request()
        await asyncio.sleep(0.06)
        assert runner._external_drain_active is False
        runner._running = False
        await asyncio.sleep(0.04)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_pair_gate_holds_admission_after_drain_marker_clears(self, home):
        runner, _ = _drain_runner()
        runner._drain_control_watcher = GatewayRunner._drain_control_watcher.__get__(
            runner, GatewayRunner
        )
        dc.write_drain_request()
        _write_pair_gate(home)
        task = asyncio.create_task(runner._drain_control_watcher(interval=0.02))
        await asyncio.sleep(0.06)
        assert runner._external_drain_active is True

        dc.clear_drain_request()
        await asyncio.sleep(0.06)
        assert runner._external_drain_active is True

        dc.pair_open_gate_path().unlink()
        await asyncio.sleep(0.06)
        assert runner._external_drain_active is False
        runner._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_kind", ["drain", "pair"])
async def test_preexisting_marker_closes_admission_before_startup_producers(
    home, monkeypatch, gate_kind
):
    """Either persistent gate must fence the whole startup path synchronously."""

    if gate_kind == "drain":
        dc.write_drain_request(principal="startup-race-test")
    else:
        _write_pair_gate(home)
    runner = GatewayRunner(
        GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(enabled=True, token="***"),
            },
            sessions_dir=home / "sessions",
        )
    )
    monkeypatch.setattr("gateway.run._hermes_home", home)

    observed: list[str] = []
    cron_gate = {"paused": False}
    published_states: list[str | None] = []

    def set_cron_gate(paused: bool) -> None:
        cron_gate["paused"] = paused
        observed.append(f"cron:{paused}")

    def assert_startup_fenced(label: str) -> None:
        assert runner._external_drain_active is True, label
        assert cron_gate["paused"] is True, label
        assert runner._drain_sensitive_background_work == 0, label
        observed.append(label)

    def recover_processes() -> int:
        assert_startup_fenced("recover-processes")
        return 0

    def recover_process_completions() -> int:
        assert_startup_fenced("recover-process-completions")
        return 0

    def recover_delegations() -> dict[str, int]:
        assert_startup_fenced("recover-delegations")
        return {"queued": 0, "lost": 0}

    def create_adapter(platform, platform_config):
        assert_startup_fenced("create-adapter")
        return None

    def schedule_resumes(platform=None) -> int:
        assert_startup_fenced("schedule-resumes")
        return 0

    async def finish_restore() -> None:
        assert_startup_fenced("finish-restore")

    def create_fenced_task(coro):
        name = getattr(getattr(coro, "cr_code", None), "co_name", type(coro).__name__)
        assert_startup_fenced(f"task:{name}")
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return MagicMock()

    monkeypatch.setattr("cron.scheduler.set_cron_dispatch_paused", set_cron_gate)
    monkeypatch.setattr(
        "tools.process_registry.process_registry.recover_from_checkpoint",
        recover_processes,
    )
    monkeypatch.setattr(
        "tools.process_registry.process_registry.recover_completion_notifications",
        recover_process_completions,
    )
    monkeypatch.setattr(
        "tools.process_registry.process_registry.pending_watchers",
        [],
    )
    monkeypatch.setattr(
        "tools.async_delegation.recover_async_delegations",
        recover_delegations,
    )
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr("agent.shell_hooks.register_from_config", lambda *a, **k: None)
    monkeypatch.setattr(
        "gateway.channel_directory.build_channel_directory",
        lambda adapters: asyncio.sleep(0, result={"platforms": {}}),
    )
    monkeypatch.setattr("gateway.run.asyncio.create_task", create_fenced_task)
    monkeypatch.setattr(runner, "_create_adapter", create_adapter)
    monkeypatch.setattr(runner, "_schedule_resume_pending_sessions", schedule_resumes)
    monkeypatch.setattr(runner, "_finish_startup_restore", finish_restore)
    monkeypatch.setattr(runner, "_send_update_notification", lambda: asyncio.sleep(0, result=True))
    monkeypatch.setattr(runner, "_send_restart_notification", lambda: asyncio.sleep(0))
    monkeypatch.setattr(runner.hooks, "discover_and_load", lambda: None)
    monkeypatch.setattr(runner.hooks, "emit", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(
        runner,
        "_update_runtime_status",
        lambda state=None, *a, **k: published_states.append(state),
    )

    assert await runner.start() is True

    assert observed[0] == "cron:True"
    assert {
        "recover-processes",
        "recover-process-completions",
        "recover-delegations",
        "create-adapter",
        "schedule-resumes",
        "task:_session_expiry_watcher",
        "task:_kanban_notifier_watcher",
        "task:_kanban_dispatcher_watcher",
        "task:_platform_reconnect_watcher",
        "task:_handoff_watcher",
        "task:_async_delegation_watcher",
        "task:_drain_control_watcher",
    }.issubset(observed)
    assert runner._external_drain_active is True
    assert published_states[-1] == "draining"


# ---------------------------------------------------------------------------
# New-turn accept gate
# ---------------------------------------------------------------------------


class TestNewTurnGate:
    @pytest.mark.asyncio
    async def test_new_turn_refused_during_external_drain(self):
        runner, _ = _drain_runner()
        runner._external_drain_active = True
        event = MessageEvent(
            text="hello",
            message_type=MessageType.TEXT,
            source=make_restart_source(),
            message_id="m1",
        )
        result = await runner._handle_message(event)
        assert result is not None
        assert "draining" in result.lower()

    @pytest.mark.asyncio
    async def test_internal_event_cannot_claim_or_run_during_external_drain(self):
        runner, _ = _drain_runner()
        runner._external_drain_active = True
        runner._claim_active_session_slot = MagicMock(
            side_effect=AssertionError("internal event reached session claim")
        )
        runner._handle_message_with_agent = MagicMock(
            side_effect=AssertionError("internal event reached agent runtime")
        )
        event = MessageEvent(
            text="synthetic completion",
            message_type=MessageType.TEXT,
            source=make_restart_source(),
            message_id="internal-1",
            internal=True,
        )

        result = await runner._handle_message(event)

        assert result is None
        assert runner._running_agents == {}
        runner._claim_active_session_slot.assert_not_called()
        runner._handle_message_with_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_flight_turn_not_interrupted_by_drain(self):
        # Entering drain must NOT touch the running-agents set.
        runner, _ = _drain_runner()
        sentinel = MagicMock()
        runner._running_agents["k"] = sentinel
        runner._enter_external_drain()
        assert runner._running_agents.get("k") is sentinel
        sentinel.interrupt.assert_not_called()
