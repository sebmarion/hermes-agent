"""Real-SQLite contracts for the desktop session revision tracker."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli.session_revision import (
    SessionRevisionProbeError,
    SessionRevisionTracker,
)


def _create_database(path: Path, *session_ids: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO sessions(id) VALUES (?)",
            [(session_id,) for session_id in session_ids],
        )


def test_revision_stays_stable_until_external_commit(tmp_path):
    db_path = tmp_path / "state.db"
    _create_database(db_path, "initial")
    tracker = SessionRevisionTracker()

    first = tracker.revision([("default", db_path)])
    assert tracker.revision([("default", db_path)]) == first

    with sqlite3.connect(db_path) as writer:
        writer.execute("INSERT INTO sessions(id) VALUES (?)", ("external",))
        writer.commit()

    assert tracker.revision([("default", db_path)]) != first


def test_never_created_database_is_a_stable_absent_marker(tmp_path):
    db_path = tmp_path / "state.db"
    tracker = SessionRevisionTracker()

    first = tracker.revision([("default", db_path)])

    assert tracker.revision([("default", db_path)]) == first
    assert not db_path.exists()


def test_previously_observed_missing_database_fails_probe(tmp_path):
    db_path = tmp_path / "state.db"
    _create_database(db_path, "initial")
    tracker = SessionRevisionTracker()
    tracker.revision([("default", db_path)])

    db_path.unlink()

    with pytest.raises(SessionRevisionProbeError, match="unavailable"):
        tracker.revision([("default", db_path)])


def test_atomic_replacement_reopens_and_changes_revision(tmp_path):
    db_path = tmp_path / "state.db"
    replacement = tmp_path / "replacement.db"
    _create_database(db_path, "initial")
    _create_database(replacement, "replacement")
    tracker = SessionRevisionTracker()
    first = tracker.revision([("default", db_path)])

    os.replace(replacement, db_path)

    assert tracker.revision([("default", db_path)]) != first


def test_scope_pruning_and_close_are_idempotent(tmp_path):
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    _create_database(first_path, "first")
    _create_database(second_path, "second")
    tracker = SessionRevisionTracker()

    first_revision = tracker.revision(
        [("first", first_path), ("second", second_path)]
    )
    tracker.revision([("second", second_path)])

    assert set(tracker._entries) == {("second", str(second_path.resolve()))}
    with sqlite3.connect(first_path) as writer:
        writer.execute("INSERT INTO sessions(id) VALUES (?)", ("external",))
        writer.commit()
    assert tracker.revision(
        [("first", first_path), ("second", second_path)]
    ) != first_revision
    tracker.close()
    tracker.close()
    assert tracker._entries == {}


def test_probe_and_close_are_serialized(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _create_database(db_path, "initial")
    tracker = SessionRevisionTracker()
    tracker.revision([("default", db_path)])
    probe_entered = threading.Event()
    release_probe = threading.Event()
    close_finished = threading.Event()
    original = tracker._descriptor_for_target

    def blocking_descriptor(*args, **kwargs):
        probe_entered.set()
        assert release_probe.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(tracker, "_descriptor_for_target", blocking_descriptor)
    probe_thread = threading.Thread(
        target=lambda: tracker.revision([("default", db_path)]),
        daemon=True,
    )
    close_thread = threading.Thread(
        target=lambda: (tracker.close(), close_finished.set()),
        daemon=True,
    )

    probe_thread.start()
    assert probe_entered.wait(timeout=5)
    close_thread.start()
    assert not close_finished.wait(timeout=0.1)
    release_probe.set()
    probe_thread.join(timeout=5)
    close_thread.join(timeout=5)

    assert not probe_thread.is_alive()
    assert not close_thread.is_alive()
    assert close_finished.is_set()
