import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

import hermes_cli.session_revision as session_revision
from hermes_cli.session_revision import (
    SessionRevisionProbeError,
    SessionRevisionTracker,
)


def _create_database(path: Path, row_id: str = "initial") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO sessions(id) VALUES (?)", (row_id,))
    connection.commit()
    return connection


def test_revision_stays_stable_until_external_commit(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    writer = _create_database(db_path)
    tracker = SessionRevisionTracker()

    try:
        first = tracker.revision([("default", db_path)])

        assert tracker.revision([("default", db_path)]) == first

        writer.execute("INSERT INTO sessions(id) VALUES (?)", ("external",))
        writer.commit()

        assert tracker.revision([("default", db_path)]) != first
    finally:
        tracker.close()
        writer.close()


def test_never_created_database_is_a_stable_absent_marker(tmp_path: Path) -> None:
    db_path = tmp_path / "missing" / "state.db"
    tracker = SessionRevisionTracker()

    try:
        first = tracker.revision([("default", db_path)])

        assert tracker.revision([("default", db_path)]) == first
        assert not db_path.exists()
    finally:
        tracker.close()


def test_previously_observed_missing_database_fails_probe(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    writer = _create_database(db_path)
    writer.close()
    tracker = SessionRevisionTracker()

    try:
        tracker.revision([("default", db_path)])
        db_path.unlink()

        with pytest.raises(SessionRevisionProbeError):
            tracker.revision([("default", db_path)])
    finally:
        tracker.close()


def test_atomic_replacement_reopens_and_changes_revision(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    writer = _create_database(db_path)
    writer.close()
    tracker = SessionRevisionTracker()

    try:
        first = tracker.revision([("default", db_path)])
        replacement_path = tmp_path / "replacement.db"
        replacement = _create_database(replacement_path, row_id="replacement")
        replacement.close()

        os.replace(replacement_path, db_path)

        assert tracker.revision([("default", db_path)]) != first
    finally:
        tracker.close()


def test_scope_pruning_and_close_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_path = tmp_path / "default.db"
    worker_path = tmp_path / "worker.db"
    _create_database(default_path).close()
    _create_database(worker_path).close()
    opened: list[_TrackingConnection] = []
    real_connect = sqlite3.connect

    def tracking_connect(database: str, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = _TrackingConnection
        connection = real_connect(database, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(session_revision.sqlite3, "connect", tracking_connect)
    tracker = SessionRevisionTracker()

    tracker.revision([("worker", worker_path), ("default", default_path)])
    tracker.revision([("default", default_path)])

    assert len(opened) == 2
    assert sum(connection.close_calls for connection in opened) == 1

    tracker.close()
    tracker.close()

    assert [connection.close_calls for connection in opened] == [1, 1]


def test_failed_data_version_probe_closes_and_reopens_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    _create_database(db_path).close()
    opened: list[_FlakyConnection] = []
    real_connect = sqlite3.connect

    def flaky_connect(database: str, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = _FlakyConnection
        connection = real_connect(database, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(session_revision.sqlite3, "connect", flaky_connect)
    tracker = SessionRevisionTracker()

    try:
        tracker.revision([("default", db_path)])
        opened[0].fail_data_version = True

        with pytest.raises(SessionRevisionProbeError):
            tracker.revision([("default", db_path)])

        assert opened[0].close_calls == 1
        tracker.revision([("default", db_path)])
        assert len(opened) == 2
    finally:
        tracker.close()


def test_windows_fallback_does_not_retain_a_sqlite_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    writer = _create_database(db_path)
    tracker = SessionRevisionTracker()
    monkeypatch.setattr(
        session_revision, "_retains_sqlite_connections", lambda: False
    )

    def unexpected_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        raise AssertionError("Windows revision probes must not retain SQLite handles")

    monkeypatch.setattr(session_revision.sqlite3, "connect", unexpected_connect)

    try:
        first = tracker.revision([("default", db_path)])
        writer.execute("INSERT INTO sessions(id) VALUES (?)", ("external",))
        writer.commit()

        assert tracker.revision([("default", db_path)]) != first
    finally:
        tracker.close()
        writer.close()


def test_windows_fallback_fails_if_database_disappears_mid_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    _create_database(db_path).close()
    tracker = SessionRevisionTracker()
    real_file_identity = session_revision._file_identity

    def identity_then_remove(path: Path) -> tuple[int, ...]:
        identity = real_file_identity(path)
        path.unlink()
        return identity

    monkeypatch.setattr(
        session_revision, "_retains_sqlite_connections", lambda: False
    )
    monkeypatch.setattr(session_revision, "_file_identity", identity_then_remove)

    try:
        with pytest.raises(SessionRevisionProbeError):
            tracker.revision([("default", db_path)])
    finally:
        tracker.close()


def test_windows_fallback_wraps_fingerprint_stat_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    _create_database(db_path).close()
    tracker = SessionRevisionTracker()
    real_stat = Path.stat

    def fail_wal_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path).endswith("-wal"):
            raise PermissionError("synthetic permission failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(
        session_revision, "_retains_sqlite_connections", lambda: False
    )
    monkeypatch.setattr(Path, "stat", fail_wal_stat)

    try:
        with pytest.raises(SessionRevisionProbeError):
            tracker.revision([("default", db_path)])
    finally:
        tracker.close()


def test_probe_and_close_are_serialized(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_database(db_path).close()
    tracker = SessionRevisionTracker()
    tracker.revision([("default", db_path)])
    lock = _GateLock()
    tracker._lock = lock
    probe_finished = threading.Event()
    close_finished = threading.Event()

    def probe() -> None:
        tracker.revision([("default", db_path)])
        probe_finished.set()

    def close() -> None:
        tracker.close()
        close_finished.set()

    probe_thread = threading.Thread(target=probe)
    close_thread = threading.Thread(target=close)

    probe_thread.start()
    assert lock.first_entered.wait(timeout=2)
    close_thread.start()
    assert lock.second_attempted.wait(timeout=2)
    assert not close_finished.is_set()

    lock.release_first.set()
    probe_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert probe_finished.is_set()
    assert close_finished.is_set()
    assert lock.enter_order == [1, 2]
    assert lock.exit_order == [1, 2]


class _TrackingConnection(sqlite3.Connection):
    close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class _FlakyConnection(_TrackingConnection):
    fail_data_version = False

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if self.fail_data_version and sql == "PRAGMA data_version":
            raise sqlite3.OperationalError("synthetic probe failure")
        return super().execute(sql, parameters)


class _GateLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counter_lock = threading.Lock()
        self._next_identifier = 0
        self._local = threading.local()
        self.first_entered = threading.Event()
        self.second_attempted = threading.Event()
        self.release_first = threading.Event()
        self.enter_order: list[int] = []
        self.exit_order: list[int] = []

    def __enter__(self) -> "_GateLock":
        with self._counter_lock:
            self._next_identifier += 1
            identifier = self._next_identifier
        if identifier == 2:
            self.second_attempted.set()
        self._lock.acquire()
        self._local.identifier = identifier
        self.enter_order.append(identifier)
        if identifier == 1:
            self.first_entered.set()
            assert self.release_first.wait(timeout=2)
        return self

    def __exit__(self, *exc_info: object) -> None:
        identifier = self._local.identifier
        self.exit_order.append(identifier)
        self._lock.release()
