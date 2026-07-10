from __future__ import annotations

import subprocess

from agent.session_workspace import normalize_local_workspace, persist_git_metadata_async
from hermes_state import SessionDB


def test_normalize_local_workspace_rejects_placeholders_and_relative_paths(tmp_path):
    assert normalize_local_workspace(None) is None
    assert normalize_local_workspace("unknown") is None
    assert normalize_local_workspace(".") is None
    assert normalize_local_workspace("relative/path") is None
    assert normalize_local_workspace(tmp_path / "missing") is None
    assert normalize_local_workspace(tmp_path) == str(tmp_path.resolve())


def test_persist_git_metadata_async_enriches_existing_session(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    nested = repo / "src"
    nested.mkdir()

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("session-1", "cli", cwd=str(nested))
    db.close()

    thread = persist_git_metadata_async(
        db_path=db_path,
        session_id="session-1",
        cwd=nested,
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


def test_persist_git_metadata_async_leaves_non_git_workspace_intact(tmp_path):
    workspace = tmp_path / "plain-directory"
    workspace.mkdir()
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("session-plain", "cli", cwd=str(workspace))
    db.close()

    thread = persist_git_metadata_async(
        db_path=db_path,
        session_id="session-plain",
        cwd=workspace,
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
