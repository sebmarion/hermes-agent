"""Contracts for the lightweight Agent-owned sidebar projection."""

import sqlite3
import time

from hermes_state import SCHEMA_VERSION, SessionDB


def _generation(db: SessionDB) -> int:
    return db.get_session_projection_generation()


def test_projection_schema_is_additive_and_indexed(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        columns = {
            row[1]
            for row in db._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        indexes = {
            row[1]
            for row in db._conn.execute("PRAGMA index_list(sessions)").fetchall()
        }
        meta = db._conn.execute(
            "SELECT generation, backfill_rowid, backfill_complete "
            "FROM session_projection_meta WHERE id = 1"
        ).fetchone()

        assert SCHEMA_VERSION >= 20
        assert "last_activity_at" in columns
        assert "idx_sessions_projection_activity" in indexes
        assert tuple(meta) == (0, 0, 1)
    finally:
        db.close()


def test_visible_message_updates_activity_and_generation_once(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("visible", "cli")
        before = _generation(db)

        db.append_message("visible", "user", "hello", timestamp=123.5)

        row = db.get_session("visible")
        assert row["last_activity_at"] == 123.5
        assert _generation(db) == before + 1
    finally:
        db.close()


def test_subagent_messages_never_invalidate_default_projection(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("child", "subagent")
        before = _generation(db)

        db.append_message("child", "assistant", "working", timestamp=456.0)
        db.append_message("child", "tool", "done", timestamp=457.0)

        assert db.get_session("child")["last_activity_at"] == 457.0
        assert _generation(db) == before
        assert db.list_session_projection() == []
    finally:
        db.close()


def test_projection_rows_are_bounded_and_activity_ordered(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        for sid, source, timestamp in (
            ("old", "cli", 10.0),
            ("new", "webui", 30.0),
            ("middle", "telegram", 20.0),
            ("child", "subagent", 40.0),
        ):
            db.create_session(sid, source)
            db.append_message(sid, "user", sid, timestamp=timestamp)

        rows = db.list_session_projection(limit=2)

        assert [row["id"] for row in rows] == ["new", "middle"]
        plan = db.explain_session_projection(limit=2)
        assert any("idx_sessions_projection_activity" in detail for detail in plan)
    finally:
        db.close()


def test_sidebar_metadata_generation_changes_only_on_real_change(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("visible", "cli", model="first")
        before = _generation(db)

        assert db.set_session_title("visible", "Title") is True
        after_title = _generation(db)
        assert after_title == before + 1
        assert db.set_session_title("visible", "Title") is True
        assert _generation(db) == after_title

        db.update_session_model("visible", "second")
        after_model = _generation(db)
        assert after_model == after_title + 1
        db.update_session_model("visible", "second")
        assert _generation(db) == after_model

        db.end_session("visible", "user_exit")
        after_end = _generation(db)
        assert after_end == after_model + 1
        db.end_session("visible", "ignored")
        assert _generation(db) == after_end

        db.reopen_session("visible")
        after_reopen = _generation(db)
        assert after_reopen == after_end + 1
        db.reopen_session("visible")
        assert _generation(db) == after_reopen
    finally:
        db.close()


def test_transcript_rewrites_update_projection_once(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("visible", "cli")

        before = _generation(db)
        db.replace_messages(
            "visible",
            [{"role": "user", "content": "replacement", "timestamp": 200.0}],
        )
        assert db.get_session("visible")["last_activity_at"] == 200.0
        assert _generation(db) == before + 1

        before = _generation(db)
        assert db.archive_and_compact(
            "visible",
            [{"role": "assistant", "content": "summary", "timestamp": 300.0}],
        ) == 1
        assert db.get_session("visible")["last_activity_at"] == 300.0
        assert _generation(db) == before + 1

        before = _generation(db)
        db.clear_messages("visible")
        row = db.get_session("visible")
        assert row["last_activity_at"] is None
        assert row["message_count"] == 0
        assert _generation(db) == before + 1
    finally:
        db.close()


def test_archive_and_delete_change_visible_generation_only_on_real_change(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("visible", "cli")
        db.create_session("child", "subagent")

        before = _generation(db)
        assert db.set_session_archived("visible", True) is True
        assert _generation(db) == before + 1
        assert db.set_session_archived("visible", True) is False
        assert _generation(db) == before + 1

        assert db.set_session_archived("child", True) is True
        assert _generation(db) == before + 1

        assert db.delete_session("visible") is True
        assert _generation(db) == before + 2
        assert db.delete_session("missing") is False
        assert _generation(db) == before + 2
    finally:
        db.close()


def test_bulk_delete_invalidates_projection_once_and_ignores_subagent_only_batch(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("one", "cli")
        db.create_session("two", "webui")
        db.create_session("child", "subagent")
        before = _generation(db)

        assert db.delete_sessions(["one", "two"]) == 2
        assert _generation(db) == before + 1

        assert db.delete_sessions(["child"]) == 1
        assert _generation(db) == before + 1
    finally:
        db.close()


def test_legacy_activity_backfill_is_resumable_and_batched(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path, _start_projection_backfill=False)
    try:
        for idx in range(3):
            sid = f"legacy-{idx}"
            db.create_session(sid, "cli")
            db.append_message(sid, "user", sid, timestamp=100.0 + idx)
        db._conn.execute("UPDATE sessions SET last_activity_at = NULL")
        db._conn.execute(
            "UPDATE session_projection_meta SET backfill_rowid = 0, backfill_complete = 0 "
            "WHERE id = 1"
        )

        first = db.backfill_session_projection_batch(batch_size=2)
        second = db.backfill_session_projection_batch(batch_size=2)

        assert first["rows_scanned"] == 2
        assert first["complete"] is False
        assert second["rows_scanned"] == 1
        assert second["complete"] is True
        assert db._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE last_activity_at IS NULL"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_legacy_projection_backfill_runs_off_startup_path(tmp_path):
    path = tmp_path / "legacy-background.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (19);
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL,
            parent_session_id TEXT, session_key TEXT, user_id TEXT, chat_id TEXT,
            chat_type TEXT, thread_id TEXT, handoff_state TEXT,
            message_count INTEGER DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, platform_message_id TEXT
        );
        CREATE TABLE compression_locks (
            session_id TEXT PRIMARY KEY, holder TEXT NOT NULL,
            acquired_at REAL NOT NULL, expires_at REAL NOT NULL
        );
        INSERT INTO sessions(id, source, started_at, message_count)
        VALUES ('legacy', 'cli', 1.0, 1);
        INSERT INTO messages(session_id, role, content, timestamp)
        VALUES ('legacy', 'user', 'hello', 9.0);
        """
    )
    conn.commit()
    conn.close()

    started = time.monotonic()
    db = SessionDB(path)
    startup_elapsed = time.monotonic() - started
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            meta = db._conn.execute(
                "SELECT backfill_complete FROM session_projection_meta WHERE id = 1"
            ).fetchone()
            if meta[0]:
                break
            time.sleep(0.01)

        assert startup_elapsed < 1.0
        assert meta[0] == 1
        assert db.get_session("legacy")["last_activity_at"] == 9.0
    finally:
        db.close()


def test_v19_database_migrates_without_rewriting_messages(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (19);
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL,
            parent_session_id TEXT, session_key TEXT, user_id TEXT, chat_id TEXT,
            chat_type TEXT, thread_id TEXT, handoff_state TEXT,
            message_count INTEGER DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, platform_message_id TEXT
        );
        CREATE TABLE compression_locks (
            session_id TEXT PRIMARY KEY, holder TEXT NOT NULL,
            acquired_at REAL NOT NULL, expires_at REAL NOT NULL
        );
        INSERT INTO sessions(id, source, started_at, message_count)
        VALUES ('legacy', 'cli', 1.0, 1);
        INSERT INTO messages(session_id, role, content, timestamp)
        VALUES ('legacy', 'user', 'hello', 9.0);
        """
    )
    conn.commit()
    conn.close()

    db = SessionDB(path, _start_projection_backfill=False)
    try:
        assert db.get_session("legacy")["last_activity_at"] is None
        assert db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert db.backfill_session_projection_batch(batch_size=10)["complete"] is True
        assert db.get_session("legacy")["last_activity_at"] == 9.0
    finally:
        db.close()
