"""Immutable gateway runtime-identity receipt contract."""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from gateway import status as gateway_status
from gateway.drain_control import (
    pair_open_gate_owner_hash,
    pair_open_gate_path,
    pair_open_gate_receipt,
)
from gateway.status import get_process_start_token
from gateway.runtime_identity import (
    IDENTITY_ENV_FIELDS,
    capture_runtime_identity,
    public_release_identity,
    process_identity_matches,
    runtime_identity_receipt,
)
from gateway.run import _attest_pair_gate_to_runtime
from utils import atomic_json_write


def _complete_identity_env() -> dict[str, str]:
    return {
        "HERMES_AGENT_COMMIT": "1" * 40,
        "HERMES_AGENT_TREE": "2" * 40,
        "HERMES_AGENT_MANIFEST_SHA256": "a" * 64,
        "HERMES_AGENT_SOURCE_PATH": "/opt/Hermes Agent/releases/pair-a/agent",
        "HERMES_RUNTIME_MANIFEST_SHA256": "b" * 64,
        "HERMES_RUNTIME_PATH": "/opt/Hermes Runtime/pair-a/python",
        "HERMES_RELEASE_PAIR_ID": "pair-20260723-a",
        "HERMES_WEBUI_BUILD_ID": "webui-build-42",
        "HERMES_WEBUI_COMMIT": "3" * 40,
        "HERMES_WEBUI_TREE": "4" * 40,
        "HERMES_WEBUI_MANIFEST_SHA256": "c" * 64,
        "HERMES_SELECTOR_GENERATION": "7",
        "HERMES_RELEASE_TRANSACTION_ID": "txn-550e8400-e29b-41d4-a716",
        "HERMES_GATEWAY_LAUNCHD_LABEL": "ai.hermes.gateway.release-pair-a",
    }


def test_complete_sealed_identity_round_trips_exactly():
    env = _complete_identity_env()

    receipt = capture_runtime_identity(
        env,
        pid=43172,
        start_token="darwin-proc:43172:1784784792:530001",
        interpreter="/opt/Hermes Runtime/pair-a/python/bin/python",
        cwd="/opt/Hermes Agent/releases/pair-a/agent",
    )

    assert receipt["schema"] == "hermes.runtime_identity.v1"
    assert receipt["verified"] is True
    assert receipt["missing_fields"] == []
    assert receipt["invalid_fields"] == []
    assert receipt["sealed"] == {
        field.logical_name: env[field.env_name] for field in IDENTITY_ENV_FIELDS
    }
    assert set(receipt["field_status"].values()) == {"verified"}
    assert receipt["process"] == {
        "pid": 43172,
        "start_token": "darwin-proc:43172:1784784792:530001",
        "start_token_status": "verified",
        "interpreter": "/opt/Hermes Runtime/pair-a/python/bin/python",
        "cwd": "/opt/Hermes Agent/releases/pair-a/agent",
        "launchd_label": "ai.hermes.gateway.release-pair-a",
    }


def test_missing_managed_identity_is_explicit_and_never_derived_from_checkout(tmp_path):
    # A checkout that happens to contain git/manifests must not become an
    # identity source. Only the immutable launch environment is authoritative.
    (tmp_path / ".git").mkdir()
    (tmp_path / "manifest.json").write_text('{"commit": "mutable"}')

    receipt = capture_runtime_identity(
        {},
        pid=9,
        start_token=None,
        interpreter="/runtime/python",
        cwd=os.fspath(tmp_path),
    )

    logical_names = [field.logical_name for field in IDENTITY_ENV_FIELDS]
    assert receipt["verified"] is False
    assert receipt["missing_fields"] == [*logical_names, "process.start_token"]
    assert receipt["invalid_fields"] == []
    assert receipt["sealed"] == {name: None for name in logical_names}
    assert set(receipt["field_status"].values()) == {"unverified"}
    assert receipt["process"]["cwd"] == os.fspath(tmp_path)
    assert receipt["process"]["launchd_label"] is None
    assert receipt["process"]["start_token"] is None
    assert receipt["process"]["start_token_status"] == "unverified"
    assert "process.start_token" in receipt["missing_fields"]


def test_unsafe_identity_values_are_rejected_instead_of_echoed():
    env = _complete_identity_env()
    env["HERMES_AGENT_COMMIT"] = "abc\nEnvironment=INJECTED"
    env["HERMES_AGENT_SOURCE_PATH"] = "relative/agent"

    receipt = capture_runtime_identity(
        env,
        pid=1,
        start_token="procfs:1:100",
        interpreter="/runtime/python",
        cwd="/runtime",
    )

    assert receipt["verified"] is False
    assert receipt["sealed"]["agent_commit"] is None
    assert receipt["sealed"]["agent_source_path"] is None
    assert receipt["field_status"]["agent_commit"] == "unverified"
    assert receipt["invalid_fields"] == ["agent_commit", "agent_source_path"]


def test_field_specific_formats_fail_closed():
    env = _complete_identity_env()
    env["HERMES_AGENT_COMMIT"] = "f" * 39
    env["HERMES_AGENT_MANIFEST_SHA256"] = "g" * 64
    env["HERMES_AGENT_SOURCE_PATH"] = "/opt/releases/../mutable"
    env["HERMES_SELECTOR_GENERATION"] = "01"
    env["HERMES_GATEWAY_LAUNCHD_LABEL"] = "ai.hermes.gateway/unsafe"

    receipt = capture_runtime_identity(
        env,
        pid=222,
        start_token="procfs:999:1234",
        interpreter="/runtime/python",
        cwd="/runtime",
    )

    assert receipt["verified"] is False
    assert receipt["invalid_fields"] == [
        "agent_commit",
        "agent_manifest_sha256",
        "agent_source_path",
        "selector_generation",
        "gateway_launchd_label",
    ]
    assert receipt["process"]["start_token"] is None
    assert receipt["process"]["start_token_status"] == "unverified"
    assert "process.start_token" in receipt["missing_fields"]


def test_process_receipt_is_frozen_at_import_and_returned_as_a_copy(monkeypatch):
    before = runtime_identity_receipt()
    monkeypatch.setenv("HERMES_AGENT_COMMIT", "late-mutable-value")

    mutated = runtime_identity_receipt()
    mutated["sealed"]["agent_commit"] = "caller-mutation"

    assert runtime_identity_receipt() == before


def test_process_start_token_is_the_os_attestable_pid_start_time():
    receipt = runtime_identity_receipt()
    pid = receipt["process"]["pid"]

    assert receipt["process"]["start_token"] == get_process_start_token(pid)
    assert receipt["process"]["start_token_status"] == "verified"


def test_process_identity_rejects_pid_reuse_or_start_token_mismatch():
    receipt = capture_runtime_identity(
        _complete_identity_env(),
        pid=222,
        start_token="procfs:222:987654",
        interpreter="/runtime/python",
        cwd="/runtime",
    )

    assert process_identity_matches(
        receipt, pid=222, start_token="procfs:222:987654"
    ) is True
    assert process_identity_matches(
        receipt, pid=223, start_token="procfs:222:987654"
    ) is False
    assert process_identity_matches(
        receipt, pid=222, start_token="procfs:222:987655"
    ) is False
    assert process_identity_matches(receipt, pid=222, start_token=None) is False


def test_public_release_identity_has_exact_path_free_shape():
    full = capture_runtime_identity(
        _complete_identity_env(),
        pid=222,
        start_token="procfs:222:987654",
        interpreter="/secret/runtime/python",
        cwd="/secret/agent/source",
    )

    public = public_release_identity(full)

    assert set(public) == {
        "schema",
        "verified",
        "release",
        "process",
    }
    assert public["schema"] == "hermes.public_release_identity.v1"
    assert set(public["release"]) == {
        "agent_commit",
        "agent_tree",
        "agent_manifest_sha256",
        "runtime_manifest_sha256",
        "release_pair_id",
        "webui_build_id",
        "webui_commit",
        "webui_tree",
        "webui_manifest_sha256",
        "selector_generation",
        "release_transaction_id",
        "gateway_launchd_label",
    }
    assert public["process"] == {
        "pid": 222,
        "start_token": "procfs:222:987654",
        "start_token_status": "verified",
    }
    serialized = repr(public)
    assert "/secret/runtime/python" not in serialized
    assert "/secret/agent/source" not in serialized
    assert "agent_source_path" not in serialized
    assert "runtime_path" not in serialized


def test_pair_gate_is_attested_to_exact_live_agent_and_release_pair():
    full = capture_runtime_identity(
        _complete_identity_env(),
        pid=222,
        start_token="procfs:222:987654",
        interpreter="/runtime/python",
        cwd="/runtime/agent",
    )
    public = public_release_identity(full)
    release = public["release"]
    gate = {
        "active": True,
        "verified": True,
        "reason": "verified",
        "transaction_id": release["release_transaction_id"],
        "epoch": 7,
        "agent": {
            "build_id": release["agent_manifest_sha256"],
            "pid": 222,
            "start_time": "procfs:222:987654",
            "instance_epoch": release["release_pair_id"],
        },
        "webui": {
            "build_id": release["webui_build_id"],
            "pid": 333,
            "start_time": "darwin-proc:333:1784784792:530001",
            "instance_epoch": "7",
        },
    }

    attested = _attest_pair_gate_to_runtime(gate, public)

    assert attested["structure_verified"] is True
    assert attested["local_identity_matches"] is True
    assert attested["verified"] is True

    gate["agent"] = dict(gate["agent"], pid=223)
    mismatched = _attest_pair_gate_to_runtime(gate, public)
    assert mismatched["local_identity_matches"] is False
    assert mismatched["verified"] is False
    assert mismatched["reason"] == "identity_mismatch"


def test_pair_gate_receipt_fixture_is_generated_by_live_contract(tmp_path):
    release_pair_id = (
        "pair_6046504bb71fffcdb9f89991d0c1f5b7e51f75e251be33f0d0074b6e0f0624b1"
    )
    transaction_id = "gateway-health-transaction-00000001"
    public = {
        "verified": True,
        "release": {
            "selector_generation": 7,
            "release_transaction_id": transaction_id,
            "agent_manifest_sha256": "6" * 64,
            "release_pair_id": release_pair_id,
            "webui_build_id": "candidate-webui",
        },
        "process": {
            "pid": 41,
            "start_token": "gateway-start",
        },
    }
    payload = {
        "schema": "hermes.pair_open_gate.v1",
        "action": "hold_pair_open",
        "transaction_id": transaction_id,
        "owner_hash": "",
        "created_at": "2026-07-23T00:00:00+00:00",
        "epoch": 7,
        "agent": {
            "build_id": "6" * 64,
            "pid": 41,
            "start_time": "gateway-start",
            "instance_epoch": release_pair_id,
        },
        "webui": {
            "build_id": "candidate-webui",
            "pid": 202,
            "start_time": "candidate-start",
            "instance_epoch": "7",
        },
    }
    payload["owner_hash"] = pair_open_gate_owner_hash(payload)

    absent = _attest_pair_gate_to_runtime(
        pair_open_gate_receipt(home=tmp_path),
        public,
    )
    atomic_json_write(
        pair_open_gate_path(tmp_path),
        payload,
        mode=0o600,
        sort_keys=True,
    )
    active = _attest_pair_gate_to_runtime(
        pair_open_gate_receipt(home=tmp_path),
        public,
    )
    expected = {
        "schema": "hermes.agent_pair_gate_receipts.fixture.v1",
        "active": active,
        "absent": absent,
    }
    fixture_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "agent_pair_gate_receipts.v1.json"
    )
    fixture_bytes = fixture_path.read_bytes()

    assert json.loads(fixture_bytes) == expected
    assert hashlib.sha256(fixture_bytes).hexdigest() == (
        "1cfa3c6ee77874803bcc7871c1304387a448810038077647b2b807cb9819a1f5"
    )


def test_linux_start_token_contract(monkeypatch):
    monkeypatch.setattr(gateway_status.sys, "platform", "linux")
    monkeypatch.setattr(
        gateway_status, "_procfs_process_start_ticks", lambda pid: 7654321
    )

    assert gateway_status.get_process_start_token(222) == "procfs:222:7654321"
    assert gateway_status.get_process_start_token(1) is None


def test_linux_proc_stat_parser_handles_spaces_and_parens_in_comm(monkeypatch):
    # /proc/<pid>/stat field 2 is parenthesized but may itself contain spaces
    # and ")" characters. Field 22 (starttime) must be indexed only after the
    # final closing paren, exactly like the independent cutover observer.
    tail_fields_4_through_21 = " ".join(str(field) for field in range(4, 22))
    raw_stat = f"222 (a b)c) S {tail_fields_4_through_21} 7654321 0 0\n"
    monkeypatch.setattr(
        gateway_status.Path,
        "read_text",
        lambda self, **kwargs: raw_stat,
    )

    assert gateway_status._procfs_process_start_ticks(222) == 7654321


def _independent_darwin_start_token(pid: int) -> str | None:
    class _ProcBSDInfo(ctypes.Structure):
        _fields_ = [
            ("prefix", ctypes.c_byte * 120),
            ("start_sec", ctypes.c_uint64),
            ("start_usec", ctypes.c_uint64),
        ]

    library_path = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
    libproc = ctypes.CDLL(library_path)
    proc_pidinfo = libproc.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    info = _ProcBSDInfo()
    size = ctypes.sizeof(info)
    if proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size:
        return None
    return f"darwin-proc:{pid}:{info.start_sec}:{info.start_usec}"


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin libproc contract")
def test_darwin_start_token_matches_independent_libproc_observer():
    pid = os.getpid()

    assert get_process_start_token(pid) == _independent_darwin_start_token(pid)
