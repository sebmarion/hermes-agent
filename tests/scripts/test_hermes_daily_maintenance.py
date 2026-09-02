"""Behavior tests for the standalone, backup-only maintenance artifact."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "hermes-daily-maintenance.py"


def _python_for_standalone():
    homebrew = Path("/opt/homebrew/bin/python3")
    if homebrew.is_file():
        return str(homebrew)
    candidate = Path("/usr/bin/python3")
    return str(candidate if candidate.is_file() else Path(sys.executable))


def _clean_env(home: Path, *, path: str | None = None) -> dict[str, str]:
    return {
        "HOME": str(home.parent),
        "HERMES_HOME": str(home),
        "PATH": path or os.environ.get("PATH", ""),
        "PYTHONPATH": "",
        "TZ": "UTC",
        "LC_ALL": "C.UTF-8",
    }


def _run_job(
    home: Path,
    backup_dir: Path | None = None,
    *extra: str,
    env: dict[str, str] | None = None,
    timeout: float = 20,
) -> subprocess.CompletedProcess[str]:
    argv = [_python_for_standalone(), str(SCRIPT_PATH), "--home", str(home)]
    if backup_dir is not None:
        argv.extend(("--backup-dir", str(backup_dir)))
    argv.extend(extra)
    return subprocess.run(
        argv,
        cwd=str(home.parent),
        env=env or _clean_env(home),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=timeout,
    )


def _create_live_db(home: Path, *, rows: int = 20) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO records(value) VALUES (?)",
            [(f"value-{i}",) for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _new_generations(backup_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in backup_dir.glob("state.db.verified-*.db")
        if path.is_file()
    )


def _load_status(home: Path) -> dict:
    return json.loads((home / "maintenance.latest.json").read_text(encoding="utf-8"))


def _load_script_module():
    module_name = "hermes_daily_maintenance_test_module"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_standalone_job_creates_verified_generation_and_status(tmp_path):
    home = tmp_path / "hermes"
    backup_dir = home / "backups"
    live = _create_live_db(home)

    result = _run_job(home, backup_dir)

    assert result.returncode == 0, result.stderr
    generations = _new_generations(backup_dir)
    assert len(generations) == 1
    with sqlite3.connect(generations[0]) as snapshot:
        assert snapshot.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert snapshot.execute("SELECT count(*) FROM records").fetchone() == (20,)
    status = _load_status(home)
    assert status["ok"] is True
    assert status["generation"] == generations[0].name
    assert status["generation_sha256"] == hashlib.sha256(generations[0].read_bytes()).hexdigest()
    assert len(status["script_sha256"]) == 64
    assert status["interpreter"]["executable"]
    assert status["deployment_receipt"]
    assert not (home / "maintenance.last-run-failed").exists()
    assert hashlib.sha256(live.read_bytes()).hexdigest()


def test_defaults_resolve_hermes_home_and_backup_directory(tmp_path):
    home = tmp_path / "profile"
    _create_live_db(home, rows=2)
    env = _clean_env(home)

    result = subprocess.run(
        [_python_for_standalone(), str(SCRIPT_PATH)],
        cwd=str(tmp_path),
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert len(_new_generations(home / "backups")) == 1


def test_live_and_staged_quick_checks_are_read_only(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    backup_dir = home / "backups"
    _create_live_db(home, rows=3)
    module = _load_script_module()
    real_connect = module.sqlite3.connect
    trace_sql: list[str] = []
    authorizer_actions: list[tuple[int, str | None, str | None]] = []

    write_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_TRANSACTION,
    }

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        uri = args[0] if args else kwargs.get("database", "")
        if isinstance(uri, str) and uri.startswith("file:") and "mode=ro" in uri:
            conn.set_trace_callback(trace_sql.append)

            def authorizer(action, arg1, arg2, db_name, source):
                authorizer_actions.append((action, arg1, arg2))
                return sqlite3.SQLITE_DENY if action in write_actions else sqlite3.SQLITE_OK

            conn.set_authorizer(authorizer)
        return conn

    monkeypatch.setattr(module.sqlite3, "connect", traced_connect)
    assert module.main(["--home", str(home), "--backup-dir", str(backup_dir)]) == 0

    quick_checks = [sql for sql in trace_sql if "quick_check" in sql.lower()]
    assert len(quick_checks) >= 2
    assert not [item for item in authorizer_actions if item[0] in write_actions]
    lowered = " ".join(trace_sql).lower()
    assert "wal_checkpoint" not in lowered
    assert "vacuum" not in lowered
    assert "optimize" not in lowered


def test_concurrent_wal_writer_does_not_break_snapshot(tmp_path):
    home = tmp_path / "hermes"
    backup_dir = home / "backups"
    live = _create_live_db(home, rows=5)
    ready = tmp_path / "writer.ready"
    stop = tmp_path / "writer.stop"
    writer_code = """
import pathlib, sqlite3, sys, time
db = pathlib.Path(sys.argv[1])
ready = pathlib.Path(sys.argv[2])
stop = pathlib.Path(sys.argv[3])
conn = sqlite3.connect(db, timeout=5)
conn.execute('PRAGMA journal_mode=WAL')
ready.touch()
i = 0
try:
    while not stop.exists():
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO records(value) VALUES (?)', (f'writer-{i}',))
        time.sleep(0.02)
        conn.commit()
        i += 1
finally:
    conn.rollback()
    conn.close()
"""
    writer = subprocess.Popen(
        [sys.executable, "-c", writer_code, str(live), str(ready), str(stop)],
        cwd=str(tmp_path),
        env=_clean_env(home),
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "writer did not start"
        result = _run_job(home, backup_dir, "--deadline-seconds", "5")
    finally:
        stop.touch()
        writer.wait(timeout=5)

    assert result.returncode == 0, result.stderr
    assert len(_new_generations(backup_dir)) == 1


def test_live_database_bytes_are_unchanged(tmp_path):
    home = tmp_path / "hermes"
    live = _create_live_db(home, rows=4)
    before = (hashlib.sha256(live.read_bytes()).hexdigest(), live.stat().st_mtime_ns)

    result = _run_job(home)

    assert result.returncode == 0, result.stderr
    after = (hashlib.sha256(live.read_bytes()).hexdigest(), live.stat().st_mtime_ns)
    assert after == before


def test_deadline_failure_is_nonzero_and_does_not_publish(tmp_path):
    home = tmp_path / "hermes"
    backup_dir = home / "backups"
    _create_live_db(home)

    result = _run_job(home, backup_dir, "--deadline-seconds", "0")

    assert result.returncode != 0
    assert not _new_generations(backup_dir)
    assert (home / "maintenance.last-run-failed").exists()
    status = _load_status(home)
    assert status["ok"] is False
    assert "deadline" in status["error"].lower()


def test_failed_run_preserves_prior_verified_generation(tmp_path):
    home = tmp_path / "hermes"
    backup_dir = home / "backups"
    _create_live_db(home)
    first = _run_job(home, backup_dir)
    assert first.returncode == 0, first.stderr
    prior = _new_generations(backup_dir)[0]
    prior_bytes = prior.read_bytes()

    failed = _run_job(home, backup_dir, "--deadline-seconds", "0")

    assert failed.returncode != 0
    assert prior.read_bytes() == prior_bytes
    assert _new_generations(backup_dir) == [prior]


def test_rotation_failure_keeps_new_verified_generation(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    backup_dir = home / "backups"
    _create_live_db(home, rows=2)
    module = _load_script_module()

    def fail_rotation(*args, **kwargs):
        raise OSError("rotation failed")

    monkeypatch.setattr(module, "_rotate", fail_rotation)
    result = module.main(["--home", str(home), "--backup-dir", str(backup_dir)])

    assert result == 1
    generations = _new_generations(backup_dir)
    assert len(generations) == 1
    status = _load_status(home)
    assert status["ok"] is False
    assert (home / "maintenance.last-run-failed").exists()


def test_exact_generation_collision_retries_without_clobbering(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    backup_dir = home / "backups"
    _create_live_db(home, rows=2)
    module = _load_script_module()
    assert module.main(["--home", str(home), "--backup-dir", str(backup_dir)]) == 0
    prior = _new_generations(backup_dir)[0]
    prior_bytes = prior.read_bytes()
    replacement = "state.db.verified-20990101T000000Z-aaaaaaaaaaaaaaaa.db"
    names = iter([prior.name, replacement])
    monkeypatch.setattr(module, "_generation_name", lambda: next(names))

    assert module.main(["--home", str(home), "--backup-dir", str(backup_dir)]) == 0

    assert prior.read_bytes() == prior_bytes
    assert (backup_dir / replacement).exists()
    assert len(_new_generations(backup_dir)) == 2


def test_rotation_keeps_seven_new_generations_and_legacy_backups(tmp_path):
    home = tmp_path / "hermes"
    backup_dir = home / "backups"
    _create_live_db(home, rows=2)
    backup_dir.mkdir(parents=True)
    old_generations = []
    for i in range(8):
        path = backup_dir / f"state.db.verified-20260902T0000{i:02d}Z-{i:016x}.db"
        path.write_bytes(f"old-{i}".encode())
        old_generations.append(path)
    sidecar = backup_dir / (
        "state.db.verified-20260902T000007Z-0000000000000007.db.receipt.json"
    )
    sidecar.write_bytes(b"sidecar-not-a-generation")
    legacy = backup_dir / "state.db.daily-2026-09-01"
    legacy.write_bytes(b"legacy-recovery-copy")

    result = _run_job(home, backup_dir, "--keep", "7")

    assert result.returncode == 0, result.stderr
    generations = _new_generations(backup_dir)
    assert len(generations) == 7
    assert old_generations[0] not in generations
    assert sidecar.read_bytes() == b"sidecar-not-a-generation"
    assert legacy.read_bytes() == b"legacy-recovery-copy"


def test_lock_contention_fails_closed(tmp_path):
    home = tmp_path / "hermes"
    _create_live_db(home)
    lock_path = home / ".backup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        result = _run_job(home, None, "--lock-timeout-seconds", "0.05")
    assert result.returncode != 0
    assert "lock" in result.stderr.lower()
    assert (home / "maintenance.last-run-failed").exists()


def test_failure_receipt_storage_falls_back_to_redacted_stderr(tmp_path):
    home = tmp_path / "hermes"
    _create_live_db(home)
    status_path = home / "maintenance.latest.json"
    status_path.mkdir(parents=True)

    result = _run_job(home, None, "--deadline-seconds", "0")

    assert result.returncode != 0
    assert "could not write" in result.stderr.lower()
    assert "api_key" not in result.stderr.lower()
    assert "token" not in result.stderr.lower()


def test_script_runs_isolated_without_checkout_imports(tmp_path):
    home = tmp_path / "isolated"
    _create_live_db(home, rows=1)
    env = _clean_env(home, path="/usr/bin:/bin")

    result = _run_job(home, env=env)

    assert result.returncode == 0, result.stderr


def test_no_archive_sweep_subprocess_is_observed(tmp_path):
    home = tmp_path / "hermes"
    _create_live_db(home, rows=1)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "archive-sweep.invoked"
    fake = fake_bin / "hermes-archive-sweep"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    fake.chmod(0o700)
    env = _clean_env(home, path=f"{fake_bin}:/usr/bin:/bin")

    result = _run_job(home, env=env)

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
