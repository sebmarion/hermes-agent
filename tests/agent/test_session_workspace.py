from __future__ import annotations

import subprocess
import threading
from unittest.mock import MagicMock, patch

from agent.session_workspace import (
    normalize_local_workspace,
    persist_git_metadata_async,
)
from hermes_state import SessionDB


def test_normalize_local_workspace_accepts_only_existing_absolute_directories(
    tmp_path,
):
    assert normalize_local_workspace(None) is None
    assert normalize_local_workspace("") is None
    assert normalize_local_workspace("unknown") is None
    assert normalize_local_workspace(".") is None
    assert normalize_local_workspace("relative/path") is None
    assert normalize_local_workspace(tmp_path / "missing") is None
    assert normalize_local_workspace(tmp_path) == str(tmp_path.resolve())


def test_persist_git_metadata_async_enriches_existing_session(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    nested = repo / "src"
    nested.mkdir()
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("session-1", "cli", cwd=str(nested))
    session = db.get_session("session-1")
    db.close()

    thread = persist_git_metadata_async(
        db_path=db_path,
        session_id="session-1",
        cwd=nested,
        session_started_at=session["started_at"],
    )
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()

    db = SessionDB(db_path=db_path)
    try:
        row = db.get_session("session-1")
    finally:
        db.close()
    assert row["cwd"] == str(nested.resolve())
    assert row["git_repo_root"] == str(repo.resolve())
    assert row["git_branch"] == "main"


def test_persist_git_metadata_async_is_best_effort_for_non_git_workspace(
    tmp_path,
):
    workspace = tmp_path / "plain"
    workspace.mkdir()
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("session-plain", "cli", cwd=str(workspace))
    session = db.get_session("session-plain")
    db.close()

    thread = persist_git_metadata_async(
        db_path=db_path,
        session_id="session-plain",
        cwd=workspace,
        session_started_at=session["started_at"],
    )
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()

    db = SessionDB(db_path=db_path)
    try:
        row = db.get_session("session-plain")
    finally:
        db.close()
    assert row["cwd"] == str(workspace.resolve())
    assert row["git_repo_root"] is None
    assert row["git_branch"] is None


def test_persist_git_metadata_async_skips_invalid_input(tmp_path):
    assert (
        persist_git_metadata_async(
            db_path=tmp_path / "state.db",
            session_id="",
            cwd=tmp_path,
            session_started_at=1.0,
        )
        is None
    )
    assert (
        persist_git_metadata_async(
            db_path=tmp_path / "state.db",
            session_id="session",
            cwd="relative",
            session_started_at=1.0,
        )
        is None
    )
    assert (
        persist_git_metadata_async(
            db_path=MagicMock(),
            session_id="session",
            cwd=tmp_path,
            session_started_at=1.0,
        )
        is None
    )
    assert (
        persist_git_metadata_async(
            db_path=tmp_path / "state.db",
            session_id="session",
            cwd=tmp_path,
            session_started_at=10**1000,
        )
        is None
    )


def test_persist_git_metadata_async_does_not_overwrite_recreated_session(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("reused", "api_server", cwd=str(workspace))
    db._conn.execute(
        "UPDATE sessions SET started_at = ? WHERE id = ?",
        (1.0, "reused"),
    )
    db._conn.commit()
    original = db.get_session("reused")
    db.close()
    entered = threading.Event()
    release = threading.Event()

    def delayed_branch(_cwd):
        entered.set()
        assert release.wait(timeout=5)
        return "stale-branch"

    with (
        patch("tui_gateway.git_probe.branch", side_effect=delayed_branch),
        patch(
            "tui_gateway.git_probe.common_repo_root",
            return_value=str(workspace),
        ),
    ):
        thread = persist_git_metadata_async(
            db_path=db_path,
            session_id="reused",
            cwd=workspace,
            session_started_at=original["started_at"],
        )
        assert thread is not None
        assert entered.wait(timeout=5)

        db = SessionDB(db_path=db_path)
        db.delete_session("reused")
        db.create_session("reused", "api_server", cwd=str(workspace))
        replacement = db.get_session("reused")
        db.close()
        assert replacement["started_at"] != original["started_at"]

        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()

    db = SessionDB(db_path=db_path)
    try:
        row = db.get_session("reused")
    finally:
        db.close()
    assert row["git_branch"] is None
    assert row["git_repo_root"] is None


def test_persist_git_metadata_async_does_not_revert_moved_session(tmp_path):
    old_workspace = tmp_path / "old"
    new_workspace = tmp_path / "new"
    old_workspace.mkdir()
    new_workspace.mkdir()
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("moved", "api_server", cwd=str(old_workspace))
    original = db.get_session("moved")
    db.close()
    entered = threading.Event()
    release = threading.Event()

    def delayed_branch(_cwd):
        entered.set()
        assert release.wait(timeout=5)
        return "stale-branch"

    with (
        patch("tui_gateway.git_probe.branch", side_effect=delayed_branch),
        patch(
            "tui_gateway.git_probe.common_repo_root",
            return_value=str(old_workspace),
        ),
    ):
        thread = persist_git_metadata_async(
            db_path=db_path,
            session_id="moved",
            cwd=old_workspace,
            session_started_at=original["started_at"],
        )
        assert thread is not None
        assert entered.wait(timeout=5)

        db = SessionDB(db_path=db_path)
        db.update_session_cwd(
            "moved",
            str(new_workspace),
            replace_git_meta=True,
        )
        db.close()

        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()

    db = SessionDB(db_path=db_path)
    try:
        row = db.get_session("moved")
    finally:
        db.close()
    assert row["cwd"] == str(new_workspace)
    assert row["git_branch"] is None
    assert row["git_repo_root"] is None
