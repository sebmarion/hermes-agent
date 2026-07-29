import json
import multiprocessing
import os
import queue
import threading
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from tools import async_delegation as ad
from tools.process_registry import ProcessRegistry


def _delegation_id(profile="default", generation="gen-1"):
    return f"deleg_{profile}_{generation}_{uuid.uuid4()}"


def _manifest(tmp_path, *, include_named=False, expected=None):
    profiles = [
        ad.ManagedAsyncDelegationProfile(
            "default", tmp_path / "default.json"
        )
    ]
    if include_named:
        profiles.append(
            ad.ManagedAsyncDelegationProfile("named", tmp_path / "named.json")
        )
    return ad.ManagedAsyncDelegationProfileManifest(
        generation="gen-1",
        profiles=tuple(profiles),
        expected_profile_ids=tuple(
            expected
            if expected is not None
            else [profile.profile_id for profile in profiles]
        ),
        source_digest="a" * 64,
    )


def _write_tracker(path, profile_id, delegation_id, *, status="completed",
                   delivery="pending", event=True):
    record = {
        "delegation_id": delegation_id,
        "profile_id": profile_id,
        "profile_generation": "gen-1",
        "status": status,
        "delivery_status": delivery,
    }
    entry = {
        "delegation_id": delegation_id,
        "profile_id": profile_id,
        "profile_generation": "gen-1",
        "status": status,
        "delivery_status": delivery,
        "record": dict(record),
    }
    if event:
        entry["event"] = {
            "type": "async_delegation",
            "delegation_id": delegation_id,
            "profile_id": profile_id,
            "profile_generation": "gen-1",
            "status": status,
        }
    path.write_text(
        json.dumps({"version": 1, "records": {delegation_id: entry}}),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _worker(manifest, outbox, boundary):
    from tools import async_delegation as worker_ad
    from tools.process_registry import ProcessRegistry

    registry = ProcessRegistry()
    worker_ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=Path(outbox),
        completion_queue=registry.completion_queue,
        crash_hook=lambda observed: os._exit(74)
        if observed == boundary
        else None,
    )


def _verify_restart_worker(manifest, outbox, receipt, output):
    import queue as queue_module
    from tools import async_delegation as worker_ad

    result = worker_ad.verify_managed_async_delegations_exact(
        receipt,
        manifest,
        completion_queue=queue_module.Queue(),
    )
    output.put(result.outcome.value)


def test_manifest_is_authoritative_and_runtime_epoch_is_internal(tmp_path):
    manifest = _manifest(tmp_path, include_named=True)
    ids = []
    for profile in manifest.profiles:
        delegation_id = _delegation_id(profile.profile_id)
        ids.append(delegation_id)
        _write_tracker(
            profile.tracker_path, profile.profile_id, delegation_id
        )
    result_queue = queue.Queue()

    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=result_queue,
    )

    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE
    assert receipt.process_pid == os.getpid()
    assert receipt.process_start_token
    assert receipt.runtime_generation
    assert receipt.manifest_generation == "gen-1"
    assert receipt.manifest_source_digest == "a" * 64
    assert set(receipt.delegation_ids) == set(ids)
    events = [result_queue.get_nowait(), result_queue.get_nowait()]
    assert {event["managed_delivery"]["profile_id"] for event in events} == {
        "default", "named"
    }
    assert all(event["managed_delivery"]["runtime_generation"] for event in events)
    assert all(
        event["managed_delivery"]["manifest_source_digest"] == "a" * 64
        for event in events
    )


def test_incomplete_or_empty_manifest_is_ambiguous(tmp_path):
    incomplete = _manifest(
        tmp_path, include_named=False, expected=("default", "named")
    )
    receipt = ad.recover_managed_async_delegations_exact(
        incomplete,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=queue.Queue(),
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    assert not (tmp_path / "outbox.json").exists()

    empty = ad.ManagedAsyncDelegationProfileManifest("gen-1", (), (), "a" * 64)
    receipt = ad.recover_managed_async_delegations_exact(
        empty,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=queue.Queue(),
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS


def test_live_running_is_explicit_in_progress_not_absent(tmp_path):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    _write_tracker(
        manifest.profiles[0].tracker_path,
        "default",
        delegation_id,
        status="running",
        delivery="running",
        event=False,
    )
    data = json.loads(manifest.profiles[0].tracker_path.read_text())
    data["records"][delegation_id]["record"].update(
        {
            "runtime_owner_id": "runtime-live",
            "runtime_owner_pid": os.getpid(),
            "runtime_owner_start_token": ad._safe_process_start_token(os.getpid()),
        }
    )
    manifest.profiles[0].tracker_path.write_text(json.dumps(data))
    manifest.profiles[0].tracker_path.chmod(0o600)

    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=queue.Queue(),
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE
    assert receipt.record_classifications == (
        (delegation_id, "in_progress"),
    )


def test_terminal_record_without_event_is_ambiguous(tmp_path):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    _write_tracker(
        manifest.profiles[0].tracker_path,
        "default",
        delegation_id,
        event=False,
    )
    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=queue.Queue(),
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS


def test_orphan_outbox_is_ambiguous_and_never_enqueued(tmp_path):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    tracker = manifest.profiles[0].tracker_path
    _write_tracker(tracker, "default", delegation_id)
    first_queue = queue.Queue()
    with pytest.raises(RuntimeError, match="crash"):
        ad.recover_managed_async_delegations_exact(
            manifest,
            outbox_path=tmp_path / "outbox.json",
            completion_queue=first_queue,
            crash_hook=lambda stage: (
                (_ for _ in ()).throw(RuntimeError("crash"))
                if stage == "intent_committed"
                else None
            ),
        )
    tracker.write_text(json.dumps({"version": 1, "records": {}}))
    tracker.chmod(0o600)

    fresh_queue = queue.Queue()
    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=fresh_queue,
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    assert "ACK-capable tracker" in receipt.errors[0]
    assert fresh_queue.empty()


def test_consumer_uses_managed_ack_and_concurrent_ack_is_idempotent(tmp_path):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    tracker = manifest.profiles[0].tracker_path
    outbox = tmp_path / "outbox.json"
    _write_tracker(tracker, "default", delegation_id)
    registry = ProcessRegistry()
    ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=outbox,
        completion_queue=registry.completion_queue,
    )
    event = registry.completion_queue.get_nowait()
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                registry.finish_notification_delivery(event, True)
            )
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert results == [True, True]
    row = json.loads(outbox.read_text())["events"][
        event["managed_event_id"]
    ]
    assert row["state"] == "delivered"
    assert json.loads(tracker.read_text())["records"][delegation_id][
        "delivery_status"
    ] == "delivered"


def test_default_drain_uses_exact_managed_ack(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    tracker = manifest.profiles[0].tracker_path
    outbox = tmp_path / "outbox.json"
    _write_tracker(tracker, "default", delegation_id)
    registry = ProcessRegistry()
    ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=outbox,
        completion_queue=registry.completion_queue,
    )
    legacy = []
    monkeypatch.setattr(
        ad,
        "mark_async_delegation_delivered",
        lambda event: legacy.append(event) or True,
    )

    drained = registry.drain_notifications()

    assert len(drained) == 1
    assert legacy == []
    assert registry.completion_queue.empty()
    row = next(iter(json.loads(outbox.read_text())["events"].values()))
    assert row["state"] == "delivered"


@pytest.mark.parametrize(
    "boundary", ["intent_committed", "event_enqueued", "outbox_enqueued"]
)
def test_real_exit_and_fresh_queue_restart_converges(tmp_path, boundary):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    _write_tracker(
        manifest.profiles[0].tracker_path, "default", delegation_id
    )
    outbox = tmp_path / "outbox.json"
    process = multiprocessing.get_context("spawn").Process(
        target=_worker,
        args=(manifest, str(outbox), boundary),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 74

    fresh = queue.Queue()
    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=outbox,
        completion_queue=fresh,
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE
    assert fresh.qsize() == 1


def test_duplicate_delegation_ids_across_profiles_do_not_collide(tmp_path):
    manifest = _manifest(tmp_path, include_named=True)
    shared_uuid = uuid.uuid4()
    for profile in manifest.profiles:
        delegation_id = f"deleg_{profile.profile_id}_gen-1_{shared_uuid}"
        _write_tracker(
            profile.tracker_path, profile.profile_id, delegation_id
        )
    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=queue.Queue(),
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE
    assert len(set(receipt.event_ids)) == 2


def test_closed_outbox_schema_and_aggregate_caps_fail_closed(
    tmp_path, monkeypatch
):
    manifest = _manifest(tmp_path)
    outbox = tmp_path / "outbox.json"
    outbox.write_text(
        json.dumps({"version": 2, "events": {}, "tombstones": {}, "extra": 1})
    )
    outbox.chmod(0o600)
    receipt = ad.recover_managed_async_delegations_exact(
        manifest, outbox_path=outbox, completion_queue=queue.Queue()
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    outbox.write_text(
        json.dumps({"version": 2, "events": {}, "tombstones": {}})
    )
    outbox.chmod(0o600)
    monkeypatch.setattr(ad, "_MAX_MANAGED_AGGREGATE_BYTES", 1)
    receipt = ad.recover_managed_async_delegations_exact(
        manifest, outbox_path=outbox, completion_queue=queue.Queue()
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS


def test_delivered_rows_prune_to_dedupe_tombstones(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    tracker = manifest.profiles[0].tracker_path
    outbox = tmp_path / "outbox.json"
    _write_tracker(tracker, "default", delegation_id)
    completion_queue = queue.Queue()
    ad.recover_managed_async_delegations_exact(
        manifest, outbox_path=outbox, completion_queue=completion_queue
    )
    event = completion_queue.get_nowait()
    assert (
        ad.mark_managed_async_delegation_delivered_exact(event).outcome
        is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE
    )
    monkeypatch.setattr(ad, "_MANAGED_DELIVERED_RETENTION_SECONDS", -1)

    fresh = queue.Queue()
    receipt = ad.recover_managed_async_delegations_exact(
        manifest, outbox_path=outbox, completion_queue=fresh
    )

    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE
    durable = json.loads(outbox.read_text())
    assert event["managed_event_id"] not in durable["events"]
    assert event["managed_event_id"] in durable["tombstones"]
    assert fresh.empty()


def test_outbox_payload_collision_fails_closed(tmp_path):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    tracker = manifest.profiles[0].tracker_path
    outbox = tmp_path / "outbox.json"
    _write_tracker(tracker, "default", delegation_id)
    ad.recover_managed_async_delegations_exact(
        manifest, outbox_path=outbox, completion_queue=queue.Queue()
    )
    durable = json.loads(outbox.read_text())
    row = next(iter(durable["events"].values()))
    row["event"]["status"] = "tampered"
    outbox.write_text(json.dumps(durable))
    outbox.chmod(0o600)

    receipt = ad.recover_managed_async_delegations_exact(
        manifest, outbox_path=outbox, completion_queue=queue.Queue()
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    assert "collision" in receipt.errors[0]


def test_manifest_fd_budget_fails_closed(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path, include_named=True)
    monkeypatch.setattr(ad, "_MAX_MANAGED_AUTHORITY_FDS", 2)

    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=queue.Queue(),
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS


def test_manifest_missing_canonical_source_digest_is_ambiguous(tmp_path):
    manifest = _manifest(tmp_path)
    untrusted = ad.ManagedAsyncDelegationProfileManifest(
        manifest.generation,
        manifest.profiles,
        manifest.expected_profile_ids,
        "",
    )

    receipt = ad.recover_managed_async_delegations_exact(
        untrusted,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=queue.Queue(),
    )

    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    assert not (tmp_path / "outbox.json").exists()


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_nonfinite_managed_timestamps_fail_closed(tmp_path, bad_value):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    tracker = manifest.profiles[0].tracker_path
    _write_tracker(tracker, "default", delegation_id)
    data = json.loads(tracker.read_text())
    data["records"][delegation_id]["updated_at"] = bad_value
    tracker.write_text(json.dumps(data))
    tracker.chmod(0o600)

    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=queue.Queue(),
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS


def test_unknown_terminal_status_fails_closed(tmp_path):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    tracker = manifest.profiles[0].tracker_path
    _write_tracker(tracker, "default", delegation_id)
    data = json.loads(tracker.read_text())
    entry = data["records"][delegation_id]
    entry["status"] = "surprise"
    entry["record"]["status"] = "surprise"
    entry["event"]["status"] = "surprise"
    tracker.write_text(json.dumps(data))
    tracker.chmod(0o600)

    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=queue.Queue(),
    )
    assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS


def test_managed_async_verifier_is_read_only(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    tracker = manifest.profiles[0].tracker_path
    outbox = tmp_path / "outbox.json"
    _write_tracker(tracker, "default", delegation_id)
    completion_queue = queue.Queue()
    receipt = ad.recover_managed_async_delegations_exact(
        manifest, outbox_path=outbox, completion_queue=completion_queue
    )
    before_files = (tracker.read_bytes(), outbox.read_bytes())
    with completion_queue.mutex:
        before_queue = tuple(completion_queue.queue)

    monkeypatch.setattr(
        ad,
        "atomic_write_private_json",
        lambda *_a, **_k: pytest.fail("verifier must not write"),
    )
    monkeypatch.setattr(
        ad,
        "_managed_queue_event_once",
        lambda *_a, **_k: pytest.fail("verifier must not enqueue"),
    )
    monkeypatch.setattr(
        completion_queue,
        "put",
        lambda *_a, **_k: pytest.fail("verifier must not enqueue"),
    )

    verified = ad.verify_managed_async_delegations_exact(
        receipt, manifest, completion_queue=completion_queue
    )

    assert verified.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE
    assert (tracker.read_bytes(), outbox.read_bytes()) == before_files
    with completion_queue.mutex:
        assert tuple(completion_queue.queue) == before_queue


def test_managed_async_verifier_detects_partial_and_tamper(tmp_path):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    tracker = manifest.profiles[0].tracker_path
    outbox = tmp_path / "outbox.json"
    _write_tracker(tracker, "default", delegation_id)
    completion_queue = queue.Queue()
    receipt = ad.recover_managed_async_delegations_exact(
        manifest, outbox_path=outbox, completion_queue=completion_queue
    )
    tampered_receipt = replace(receipt, event_postconditions=())
    assert (
        ad.verify_managed_async_delegations_exact(
            tampered_receipt, manifest, completion_queue=completion_queue
        ).outcome
        is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    )
    completion_queue.get_nowait()
    assert (
        ad.verify_managed_async_delegations_exact(
            receipt, manifest, completion_queue=completion_queue
        ).outcome
        is ad.ManagedAsyncDelegationRecoveryOutcome.PARTIAL
    )

    durable = json.loads(outbox.read_text())
    row = next(iter(durable["events"].values()))
    row["event"]["status"] = "tampered"
    outbox.write_text(json.dumps(durable), encoding="utf-8")
    outbox.chmod(0o600)
    assert (
        ad.verify_managed_async_delegations_exact(
            receipt, manifest, completion_queue=completion_queue
        ).outcome
        is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    )


def test_managed_async_verifier_rejects_receipt_manifest_and_runtime_mismatch(
    tmp_path, monkeypatch
):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    _write_tracker(
        manifest.profiles[0].tracker_path, "default", delegation_id
    )
    completion_queue = queue.Queue()
    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=completion_queue,
    )
    wrong_receipt = replace(receipt, manifest_source_digest="b" * 64)
    assert (
        ad.verify_managed_async_delegations_exact(
            wrong_receipt, manifest, completion_queue=completion_queue
        ).outcome
        is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    )
    wrong_manifest = replace(manifest, source_digest="b" * 64)
    assert (
        ad.verify_managed_async_delegations_exact(
            receipt, wrong_manifest, completion_queue=completion_queue
        ).outcome
        is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    )
    runtime = ad._managed_v2_runtime()
    monkeypatch.setattr(
        ad,
        "_managed_v2_runtime_current",
        lambda: (runtime[0], runtime[1], "foreign-generation", "foreign-epoch"),
    )
    assert (
        ad.verify_managed_async_delegations_exact(
            receipt, manifest, completion_queue=completion_queue
        ).outcome
        is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE
    )


def test_managed_async_verifier_accepts_exact_terminal_ack(tmp_path):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    _write_tracker(
        manifest.profiles[0].tracker_path, "default", delegation_id
    )
    completion_queue = queue.Queue()
    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=completion_queue,
    )
    event = completion_queue.get_nowait()
    ack = ad.mark_managed_async_delegation_delivered_exact(event)
    assert ack.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE

    verified = ad.verify_managed_async_delegations_exact(
        receipt, manifest, completion_queue=completion_queue
    )

    assert verified.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE


def test_managed_async_verifier_rejects_byte_identical_duplicate_queue_event(
    tmp_path,
):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    _write_tracker(
        manifest.profiles[0].tracker_path, "default", delegation_id
    )
    completion_queue = queue.Queue()
    receipt = ad.recover_managed_async_delegations_exact(
        manifest,
        outbox_path=tmp_path / "outbox.json",
        completion_queue=completion_queue,
    )
    with completion_queue.mutex:
        duplicate = dict(completion_queue.queue[0])
        completion_queue.queue.append(duplicate)

    verified = ad.verify_managed_async_delegations_exact(
        receipt, manifest, completion_queue=completion_queue
    )

    assert (
        verified.outcome
        is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    )


def test_managed_async_verifier_rejects_arbitrary_ack_edit(tmp_path):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    _write_tracker(
        manifest.profiles[0].tracker_path, "default", delegation_id
    )
    outbox = tmp_path / "outbox.json"
    completion_queue = queue.Queue()
    receipt = ad.recover_managed_async_delegations_exact(
        manifest, outbox_path=outbox, completion_queue=completion_queue
    )
    event = completion_queue.get_nowait()
    ad.mark_managed_async_delegation_delivered_exact(event)
    durable = json.loads(outbox.read_text())
    durable["events"][event["managed_event_id"]]["created_at"] += 1
    outbox.write_text(json.dumps(durable), encoding="utf-8")
    outbox.chmod(0o600)

    assert (
        ad.verify_managed_async_delegations_exact(
            receipt, manifest, completion_queue=completion_queue
        ).outcome
        is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    )


def test_managed_async_verifier_never_creates_missing_lock(
    tmp_path, monkeypatch
):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    _write_tracker(
        manifest.profiles[0].tracker_path, "default", delegation_id
    )
    outbox = tmp_path / "outbox.json"
    completion_queue = queue.Queue()
    receipt = ad.recover_managed_async_delegations_exact(
        manifest, outbox_path=outbox, completion_queue=completion_queue
    )
    monkeypatch.setattr(ad, "_runtime_owner_id", "")
    assert (
        ad.verify_managed_async_delegations_exact(
            receipt, manifest, completion_queue=completion_queue
        ).outcome
        is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE
    )
    assert ad._runtime_owner_id == ""

    lock_path = tmp_path / ".outbox.json.lock"
    lock_path.unlink()
    before = tuple(sorted(path.name for path in tmp_path.iterdir()))

    verified = ad.verify_managed_async_delegations_exact(
        receipt, manifest, completion_queue=completion_queue
    )

    assert verified.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    assert tuple(sorted(path.name for path in tmp_path.iterdir())) == before



def test_managed_async_verifier_restart_reports_partial(tmp_path):
    manifest = _manifest(tmp_path)
    delegation_id = _delegation_id()
    _write_tracker(
        manifest.profiles[0].tracker_path, "default", delegation_id
    )
    outbox = tmp_path / "outbox.json"
    receipt = ad.recover_managed_async_delegations_exact(
        manifest, outbox_path=outbox, completion_queue=queue.Queue()
    )
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    child = context.Process(
        target=_verify_restart_worker,
        args=(manifest, str(outbox), receipt, output),
    )
    child.start()
    child.join(10)
    assert child.exitcode == 0
    assert output.get(timeout=2) == "PARTIAL"
