"""Generation checks must preserve SQLite locks held by this process."""

import os
import sqlite3
import subprocess
import sys

import pytest

from hermes_state import _connect_tracked_db, _read_sqlite_application_id


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-wide lock contract")
def test_generation_read_keeps_competing_writer_blocked(tmp_path):
    path = tmp_path / "state #1.db"
    conn = _connect_tracked_db(path)
    probe = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=0)
try:
    conn.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError:
    print("blocked")
else:
    print("admitted")
    conn.rollback()
finally:
    conn.close()
"""

    def competing_writer():
        return subprocess.check_output(
            [sys.executable, "-c", probe, str(path)], text=True, timeout=10
        ).strip()

    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE payload (value INTEGER)")
        conn.execute("PRAGMA application_id=123")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO payload VALUES (1)")
        assert competing_writer() == "blocked"
        assert _read_sqlite_application_id(path) == 123
        assert competing_writer() == "blocked"
    finally:
        conn.rollback()
        conn.close()


def test_generation_read_does_not_create_missing_database(tmp_path):
    path = tmp_path / "missing.db"
    assert _read_sqlite_application_id(path) is None
    assert not path.exists()


def test_generation_read_releases_connection_after_invalid_database(tmp_path):
    from hermes_cli.sqlite_safe_read import has_live_connection

    path = tmp_path / "invalid.db"
    path.write_bytes(b"not a database")
    assert _read_sqlite_application_id(path) is None
    assert not has_live_connection(path)


def test_generation_read_is_nonblocking_under_exclusive_lock(tmp_path):
    path = tmp_path / "exclusive.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE payload (value INTEGER)")
        conn.commit()
        conn.execute("BEGIN EXCLUSIVE")
        # Latest upstream uses lock-safe cached pread, so the committed header
        # can be read while the writer is locked. Lock exclusion is tested above.
        assert _read_sqlite_application_id(path) in (None, 0)
    finally:
        conn.rollback()
        conn.close()
