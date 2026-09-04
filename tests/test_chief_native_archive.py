import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace

sys.path.insert(0, "/home/seb/.local/share/agent-chief")

from hermes_state import SessionDB


def test_archive_precondition_and_native_lineage_lease_are_atomic(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", "desktop")
    assert db.acquire_session_turn_lease("child", "archive:one", wait_seconds=0)
    assert db.set_session_archived(
        "child", True, expected_session_ids=["other"],
        turn_lease_holder="archive:one",
    ) is False
    assert db.set_session_archived(
        "child", True, expected_session_ids=["child"],
        turn_lease_holder="archive:other",
    ) is False
    assert db.set_session_archived(
        "child", True, expected_session_ids=["child"],
        turn_lease_holder="archive:one",
        precondition=lambda conn, rows: rows[0]["archived"] == 0,
    ) is True
    assert db.get_session("child")["archived"] == 1
    db.close()


def test_archive_precondition_refuses_concurrent_activity_without_mutation(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", "desktop")
    seen = []

    def refuse(conn, rows):
        seen.append([row["id"] for row in rows])
        return False

    assert db.set_session_archived(
        "child", True, expected_session_ids=["child"], precondition=refuse
    ) is False
    assert seen == [["child"]]
    assert db.get_session("child")["archived"] == 0
    db.close()


def test_live_owner_archive_uses_real_sessiondb_and_manual_restore(tmp_path, monkeypatch):
    from tui_gateway import server
    from tui_gateway.owner_inbox import live_owner_dispatch

    profile_home = tmp_path / "profile"
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("parent", "desktop", git_repo_root="repo")
    db.append_message("parent", "assistant", "child result",
                      display_metadata={"delivery_id": "delivery-1"})
    db.create_session("child", "desktop", git_repo_root="repo")
    db.append_message("child", "user", "owned work")
    db.close()
    lock = threading.RLock()
    session = {
        "session_key": "child", "agent": SimpleNamespace(
            session_id="child",
            get_activity_summary=lambda: {"last_activity_at": 10,
                                          "genuine_activity_at": 7,
                                          "current_tool": None},
        ),
        "running": False, "queued": False, "history_lock": lock,
        "profile_home": str(profile_home),
    }
    monkeypatch.setattr(server, "_current_profile_name", lambda: "p")
    monkeypatch.setattr(server, "_sessions", {"live-child": session})
    @contextmanager
    def profile_db(_params):
        handle = SessionDB(db_path=profile_home / "state.db")
        try:
            yield handle
        finally:
            handle.close()
    monkeypatch.setattr(server, "_profile_db", profile_db)
    owner = live_owner_dispatch(server, lambda params, current: {"ok": True})
    state = owner.automation("p", "child")
    assert state["pending_tool_results"] is False
    assert state["last_activity"] == 7
    assert isinstance(state["user_message_row_id"], int)
    pending_db = SessionDB(db_path=profile_home / "state.db")
    pending_db.append_message("child", "assistant", tool_calls=[{"id": "call-1"}])
    pending_db.close()
    assert owner.automation("p", "child")["pending_tool_results"] is True
    clean_db = SessionDB(db_path=profile_home / "state.db")
    clean_db.append_message("child", "tool", "done", tool_call_id="call-1")
    clean_db.close()
    server._sessions.clear()
    assert owner.automation("p", "child") is None
    lineage = owner.lineage("p", "child")
    assert lineage == [{"id": "child", "archived": False,
                        "ended_at": None, "end_reason": None,
                        "message_count": 3, "git_repo_root": "repo",
                        "running": False}]
    proof = {"delegation_id": "delegation-1", "launch_id": "launch-1",
             "origin_version": 1, "created_session_id": "child",
             "parent_session_id": "parent", "child_session_id": "child",
             "completion_id": "delegation-1", "delivery_id": "delivery-1",
             "delivery_session_id": "parent", "delivery_acknowledged_at": 1}
    assert owner.archive("p", "child", True, expected_lineage=lineage,
                         expected_proof=proof)
    check = SessionDB(db_path=profile_home / "state.db")
    assert check.get_session("child")["archived"] == 1
    check.close()
    assert owner.archive("p", "child", False)
    check = SessionDB(db_path=profile_home / "state.db")
    assert check.get_session("child")["archived"] == 0
    check.close()


def test_owner_automation_pages_pending_calls_and_reads_native_queue(tmp_path, monkeypatch):
    from tui_gateway import server
    from tui_gateway.owner_inbox import live_owner_dispatch

    profile_home = tmp_path / "profile"
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("child", "desktop", git_repo_root="repo")
    for index in range(500):
        db.append_message("child", "user", f"history-{index}")
    db.append_message("child", "assistant", tool_calls=[{"id": "late-call"}])
    db.close()
    lock = threading.RLock()
    session = {
        "session_key": "child", "agent": SimpleNamespace(
            session_id="child",
            get_activity_summary=lambda: {"last_activity_at": 10,
                                          "genuine_activity_at": 7,
                                          "current_tool": None},
        ),
        "running": False, "queued_prompt": "queued", "history_lock": lock,
        "profile_home": str(profile_home),
    }
    monkeypatch.setattr(server, "_current_profile_name", lambda: "p")
    monkeypatch.setattr(server, "_sessions", {"live-child": session})

    @contextmanager
    def profile_db(_params):
        handle = SessionDB(db_path=profile_home / "state.db")
        try:
            yield handle
        finally:
            handle.close()

    monkeypatch.setattr(server, "_profile_db", profile_db)
    owner = live_owner_dispatch(server, lambda params, current: {"ok": True})
    state = owner.automation("p", "child")
    assert state["pending_tool_results"] is True
    assert state["queued"] is True


def test_owner_automation_uses_newest_genuine_activity_across_db_handles(
    tmp_path, monkeypatch
):
    from tui_gateway import server
    from tui_gateway.owner_inbox import live_owner_dispatch

    profile_home = tmp_path / "profile"
    cached = SessionDB(db_path=profile_home / "state.db")
    cached.create_session("child", "desktop", git_repo_root="repo")
    cached.append_message("child", "user", "owned work")
    writer = SessionDB(db_path=profile_home / "state.db")
    writer.touch_session_activity(
        "child", 7200, description="real owned work", genuine_activity_at=7200
    )

    lock = threading.RLock()
    session = {
        "session_key": "child", "agent": SimpleNamespace(
            session_id="child",
            get_activity_summary=lambda: {
                "last_activity_at": 100,
                "genuine_activity_at": 100,
                "current_tool": None,
            },
        ),
        "running": False, "queued": False, "history_lock": lock,
        "profile_home": str(profile_home),
    }
    monkeypatch.setattr(server, "_current_profile_name", lambda: "p")
    monkeypatch.setattr(server, "_sessions", {"live-child": session})

    @contextmanager
    def profile_db(_params):
        yield cached

    monkeypatch.setattr(server, "_profile_db", profile_db)
    owner = live_owner_dispatch(server, lambda params, current: {"ok": True})
    state = owner.automation("p", "child")
    assert state["last_activity"] == 7200

    writer.close()
    cached.close()


def test_owner_children_enriches_real_completed_child_for_archive(tmp_path, monkeypatch):
    from tools import async_delegation
    from tui_gateway import server
    from tui_gateway.owner_inbox import live_owner_dispatch

    profile_home = tmp_path / "profile"
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("child", "subagent", git_repo_root="repo")
    db.append_message("child", "assistant", "result")
    db.end_session("child", "completed")
    db.close()
    monkeypatch.setattr(server, "_current_profile_name", lambda: "p")
    monkeypatch.setattr(server, "_sessions", {})

    @contextmanager
    def profile_db(_params):
        handle = SessionDB(db_path=profile_home / "state.db")
        try:
            yield handle
        finally:
            handle.close()

    monkeypatch.setattr(server, "_profile_db", profile_db)
    monkeypatch.setattr(async_delegation, "list_durable_delegations", lambda: [{
        "parent_session_id": "parent", "child_session_id": "child",
        "launch_id": "launch", "origin_version": 1,
        "created_session_id": "child", "state": "completed",
        "result": {"status": "completed", "exit_reason": "completed",
                   "truncated": False},
    }])
    owner = live_owner_dispatch(server, lambda params, current: {"ok": True})
    records = owner.children("p")
    assert len(records) == 1
    assert records[0]["repository"] == "repo"
    assert records[0]["active"] is False
    assert records[0]["pending_tool_results"] is False
    assert records[0]["unresolved_failure"] is False


def test_profile_db_is_exact_with_empty_live_registry(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from tui_gateway import server
    from tui_gateway.owner_inbox import live_owner_dispatch
    from tools import async_delegation

    homes = {}
    for profile in ("p1", "p2"):
        home = tmp_path / profile
        db = SessionDB(db_path=home / "state.db")
        db.create_session("child", "subagent", git_repo_root=f"repo-{profile}")
        db.end_session("child", "completed")
        db.close()
        homes[profile] = home

    @contextmanager
    def profile_db(params):
        profile = params["profile"]
        handle = SessionDB(db_path=homes[profile] / "state.db")
        try:
            yield handle
        finally:
            handle.close()

    monkeypatch.setattr(server, "_current_profile_name", lambda: "p1")
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_profile_db", profile_db)
    monkeypatch.setattr(async_delegation, "list_durable_delegations", lambda: [])
    owner = live_owner_dispatch(server, lambda params, current: {"ok": True})
    assert owner.lineage("p1", "child")[0]["git_repo_root"] == "repo-p1"
    assert owner.lineage("p2", "child") == []
    assert owner.automation("p1", "child") is None
