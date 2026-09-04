import json
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

sys.path.insert(0, "/home/seb/.local/share/agent-chief")

from hermes_state import SessionDB
import chief_decision as ledger


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
                      display_metadata={"delivery_id": "delivery-1", "delegation_id": "delegation-1"})
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
                         expected_proof=proof) is False
    check = SessionDB(db_path=profile_home / "state.db")
    assert check.get_session("child")["archived"] == 0
    check.close()
    assert owner.archive("p", "child", False)
    check = SessionDB(db_path=profile_home / "state.db")
    assert check.get_session("child")["archived"] == 0
    check.close()
    assert owner.archive("p", "child", True, expected_lineage=lineage,
                         expected_proof=proof) is False
    check = SessionDB(db_path=profile_home / "state.db")
    assert check.get_session("child")["archived"] == 0
    check.close()


def _seed_valid_guarded_child(profile_home):
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("parent", "desktop", git_repo_root="repo")
    db.append_message("parent", "assistant", "child result",
                      display_metadata={"delivery_id": "delivery-1", "delegation_id": "delegation-1"})
    db.create_session("child", "delegate", parent_session_id="parent",
                      git_repo_root="repo", model_config={
                          "_origin": {"version": 1, "launch_id": "launch-1",
                                      "created_session_id": "child",
                                      "parent_session_id": "parent"},
                          "_delegate_from": "parent",
                          "_created_by": "agent_delegate",
                          "_origin_kind": "delegated_child",
                      })
    db.append_message("child", "assistant", "completed work")
    db.end_session("child", "completed")
    proof = {"delegation_id": "delegation-1", "launch_id": "launch-1",
             "origin_version": 1, "created_session_id": "child",
             "parent_session_id": "parent", "child_session_id": "child",
             "completion_id": "delegation-1", "delivery_id": "delivery-1",
             "delivery_session_id": "parent", "delivery_acknowledged_at": 1}
    return db, proof


@pytest.mark.parametrize("mutation", ["origin", "end", "launch", "parent", "receipt"])
def test_guarded_archive_rechecks_current_origin_end_and_receipt(
        tmp_path, monkeypatch, mutation):
    from tui_gateway import server
    from tui_gateway.owner_inbox import live_owner_dispatch

    profile_home = tmp_path / "profile"
    db, proof = _seed_valid_guarded_child(profile_home)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "p")
    monkeypatch.setattr(server, "_sessions", {})

    @contextmanager
    def profile_db(_params):
        yield db

    monkeypatch.setattr(server, "_profile_db", profile_db)
    owner = live_owner_dispatch(server, lambda *args: None)
    lineage = owner.lineage("p", "child")
    if mutation == "origin":
        def change_origin(conn):
            row = conn.execute(
                "SELECT model_config FROM sessions WHERE id='child'"
            ).fetchone()
            config = json.loads(row[0])
            config["_origin"]["launch_id"] = "launch-2"
            conn.execute("UPDATE sessions SET model_config=? WHERE id='child'",
                         (json.dumps(config),))
        db._execute_write(change_origin)
    elif mutation == "end":
        db.reopen_session("child")
    elif mutation == "launch":
        proof["launch_id"] = "launch-2"
    elif mutation == "parent":
        db.create_session("wrong-parent", "desktop", git_repo_root="repo")
        db.append_message("wrong-parent", "assistant", "wrong result",
                          display_metadata={"delivery_id": "delivery-1", "delegation_id": "delegation-1"})
        proof["parent_session_id"] = "wrong-parent"
    elif mutation == "receipt":
        def replace_receipt(conn):
            conn.execute(
                "UPDATE messages SET display_metadata=? WHERE session_id='parent'",
                (json.dumps({"delivery_id": "delivery-2"}),),
            )
        db._execute_write(replace_receipt)
    try:
        assert owner.archive("p", "child", True, expected_lineage=lineage,
                             expected_proof=proof) is False
        assert db.get_session("child")["archived"] == 0
    finally:
        db.close()


def test_owner_children_reads_supported_sync_result_receipt(tmp_path, monkeypatch):
    from tools import async_delegation
    from tui_gateway import server
    from tui_gateway.owner_inbox import live_owner_dispatch

    profile_home = tmp_path / "profile"
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("parent", "desktop", git_repo_root="repo")
    receipt = {
        "kind": "delegated_child_result", "version": 1,
        "delivery_id": "sync-delegation:delegation-1",
        "delegation_id": "delegation-1", "parent_session_id": "parent",
        "children": [{"task_index": 0, "goal": "do work", "child_session_id": "child",
                       "launch_id": "launch-1", "origin_version": 1,
                       "created_session_id": "child",
                       "parent_session_id": "parent", "status": "completed",
                       "exit_reason": "completed", "truncated": False}],
    }
    db.append_message("parent", "tool", json.dumps({
        "archive_receipt": receipt, "results": receipt["children"],
    }),
                      tool_name="delegate_task", tool_call_id="call-1")
    db.create_session("child", "delegate", parent_session_id="parent",
                      git_repo_root="repo", model_config={
                          "_origin": {"version": 1, "launch_id": "launch-1",
                                      "created_session_id": "child",
                                      "parent_session_id": "parent"},
                          "_delegate_from": "parent",
                          "_created_by": "agent_delegate",
                          "_origin_kind": "delegated_child",
                      })
    db.append_message("child", "assistant", "completed work")
    db.end_session("child", "completed")
    monkeypatch.setattr(server, "_current_profile_name", lambda: "p")
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(async_delegation, "list_durable_delegations", lambda: [])

    @contextmanager
    def profile_db(_params):
        yield db

    monkeypatch.setattr(server, "_profile_db", profile_db)
    try:
        dispatch = live_owner_dispatch(server, lambda *args: None)
        records = dispatch.children("p")
        assert [(row["delegation_id"], row["child_session_id"])
                for row in records] == [("delegation-1", "child")]
        original_content = json.dumps({"archive_receipt": receipt})
        missing = dict(receipt)
        missing["children"] = []
        db._execute_write(lambda conn: conn.execute(
            "UPDATE messages SET content=? WHERE session_id='parent'",
            (json.dumps({"archive_receipt": missing, "results": []}),)))
        assert dispatch.children("p") == []
        reordered = json.loads(original_content)["archive_receipt"]
        reordered["children"][0]["task_index"] = 1
        db._execute_write(lambda conn: conn.execute(
            "UPDATE messages SET content=? WHERE session_id='parent'",
            (json.dumps({"archive_receipt": reordered,
                         "results": reordered["children"]}),)))
        assert dispatch.children("p") == []
        db._execute_write(lambda conn: conn.execute(
            "UPDATE messages SET content=? WHERE session_id='parent'",
            (json.dumps({"archive_receipt": receipt,
                         "results": receipt["children"]}),)))
        record = records[0]
        proof = {
            "delegation_id": record["delegation_id"],
            "launch_id": record["launch_id"],
            "origin_version": record["origin_version"],
            "created_session_id": record["created_session_id"],
            "parent_session_id": record["parent_session_id"],
            "child_session_id": record["child_session_id"],
            "completion_id": record["delegation_id"],
            "delivery_id": record["delivery_receipt"]["delivery_id"],
            "delivery_session_id": record["delivery_receipt"]["session_id"],
            "delivery_acknowledged_at": record["delivery_receipt"]["acknowledged_at"],
        }
        assert dispatch.archive("p", "child", True,
                                expected_lineage=dispatch.lineage("p", "child"),
                                expected_proof=proof)
        assert db.get_session("child")["archived"] == 1
        db.close()
        db = SessionDB(db_path=profile_home / "state.db")
        assert db.get_session("child")["archived"] == 1
    finally:
        db.close()


def test_owner_children_skips_malformed_task_index_without_aborting_valid_candidate(
        tmp_path, monkeypatch):
    from tools import async_delegation
    from tui_gateway import server
    from tui_gateway.owner_inbox import live_owner_dispatch

    profile_home = tmp_path / "profile"
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("parent", "desktop", git_repo_root="repo")
    receipt = {
        "kind": "delegated_child_result", "version": 1,
        "delivery_id": "sync-delegation:malformed-batch",
        "delegation_id": "malformed-batch", "parent_session_id": "parent",
        "is_batch": True,
        "children": [
            {"task_index": 0, "goal": "valid child", "child_session_id": "child-0",
             "launch_id": "launch-0", "origin_version": 1,
             "created_session_id": "child-0", "parent_session_id": "parent",
             "completion_id": "malformed-batch:0", "status": "completed",
             "exit_reason": "completed", "truncated": False},
            {"task_index": "bad", "goal": "malformed child",
             "child_session_id": "child-bad", "launch_id": "launch-bad",
             "origin_version": 1, "created_session_id": "child-bad",
             "parent_session_id": "parent", "completion_id": "malformed-batch:bad",
             "status": "completed", "exit_reason": "completed", "truncated": False},
        ],
    }
    db.append_message("parent", "tool", json.dumps({
        "archive_receipt": receipt, "results": receipt["children"],
    }),
                      tool_name="delegate_task", tool_call_id="call-malformed")
    for child_id, launch_id in (("child-0", "launch-0"), ("child-bad", "launch-bad")):
        db.create_session(child_id, "delegate", parent_session_id="parent",
                          git_repo_root="repo", model_config={
                              "_origin": {"version": 1, "launch_id": launch_id,
                                          "created_session_id": child_id,
                                          "parent_session_id": "parent"},
                              "_delegate_from": "parent",
                              "_created_by": "agent_delegate",
                              "_origin_kind": "delegated_child",
                          })
        db.append_message(child_id, "assistant", "completed work")
        db.end_session(child_id, "completed")
    monkeypatch.setattr(server, "_current_profile_name", lambda: "p")
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(async_delegation, "list_durable_delegations", lambda: [])

    @contextmanager
    def profile_db(_params):
        yield db

    monkeypatch.setattr(server, "_profile_db", profile_db)
    try:
        dispatch = live_owner_dispatch(server, lambda *args: None)
        records = dispatch.children("p")
        assert [(row["child_session_id"], row["completion_id"])
                for row in records] == [("child-0", "malformed-batch:0")]
    finally:
        db.close()


def test_chief_archives_each_completed_child_from_real_two_child_sync_batch(
        tmp_path, monkeypatch):
    from chief_plugin import ChiefPlugin
    from tui_gateway import server
    from tui_gateway.owner_inbox import live_owner_dispatch

    profile_home = tmp_path / "profile"
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("parent", "desktop", git_repo_root="repo")
    receipt = {
        "kind": "delegated_child_result", "version": 1,
        "delivery_id": "sync-delegation:batch-real",
        "delegation_id": "batch-real", "parent_session_id": "parent",
        "is_batch": True, "children": [],
    }
    for index, child_id in enumerate(("child-0", "child-1", "child-failed")):
        status = "failed" if child_id == "child-failed" else "completed"
        receipt["children"].append({
            "task_index": index, "goal": f"task {index}",
            "child_session_id": child_id, "launch_id": f"launch-{index}",
            "origin_version": 1, "created_session_id": child_id,
            "parent_session_id": "parent", "completion_id": f"batch-real:{index}",
            "status": status, "exit_reason": status, "truncated": False,
        })
        db.create_session(child_id, "delegate", parent_session_id="parent",
                          git_repo_root="repo", model_config={
                              "_origin": {"version": 1, "launch_id": f"launch-{index}",
                                          "created_session_id": child_id,
                                          "parent_session_id": "parent"},
                              "_delegate_from": "parent",
                              "_created_by": "agent_delegate",
                              "_origin_kind": "delegated_child",
                          })
        db.append_message(child_id, "assistant", "child result")
        db.end_session(child_id, status)
    db.append_message("parent", "tool", json.dumps({
        "archive_receipt": receipt, "results": receipt["children"],
    }), tool_name="delegate_task", tool_call_id="call-batch-real")
    monkeypatch.setattr(server, "_current_profile_name", lambda: "p")
    monkeypatch.setattr(server, "_sessions", {})

    @contextmanager
    def profile_db(_params):
        yield db

    monkeypatch.setattr(server, "_profile_db", profile_db)
    context = SimpleNamespace(get_config=lambda key, default=None: {
        "enabled": True, "owner_inbox_enabled": True,
        "ledger_path": str(tmp_path / "chief.db"), "profile_name": "p",
        "archive_enabled": True, "archive_activation_cutoff": 0,
        "archive_allowlist": [
            {"profile": "p", "session_id": child, "repository": "repo"}
            for child in ("child-0", "child-1", "child-failed")
        ],
    }.get(key, default))
    plugin = ChiefPlugin(context)
    try:
        dispatch = live_owner_dispatch(server, lambda *args: None)
        plugin._archive_children(dispatch)
        assert [db.get_session(child)["archived"] for child in
                ("child-0", "child-1", "child-failed")] == [1, 1, 0]
        assert ledger.archive_receipt_state(plugin.db, "batch-real:0") == "archived"
        assert ledger.archive_receipt_state(plugin.db, "batch-real:1") == "archived"
        assert ledger.archive_receipt_state(plugin.db, "batch-real:2") is None
        db.close()
        db = SessionDB(db_path=profile_home / "state.db")
        assert [db.get_session(child)["archived"] for child in
                ("child-0", "child-1", "child-failed")] == [1, 1, 0]
    finally:
        db.close()


def test_owner_children_preserves_supported_async_receipt_mapping(tmp_path, monkeypatch):
    from tools import async_delegation
    from tui_gateway import server
    from tui_gateway.owner_inbox import live_owner_dispatch

    profile_home = tmp_path / "profile"
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("parent", "desktop", git_repo_root="repo")
    db.append_message("parent", "assistant", "child result",
                      display_metadata={"delivery_id": "async-delivery-1",
                                        "delegation_id": "async-delegation-1"})
    db.create_session("child", "delegate", parent_session_id="parent",
                      git_repo_root="repo", model_config={
                          "_origin": {"version": 1, "launch_id": "async-launch-1",
                                      "created_session_id": "child",
                                      "parent_session_id": "parent"},
                          "_delegate_from": "parent",
                          "_created_by": "agent_delegate",
                          "_origin_kind": "delegated_child",
                      })
    db.append_message("child", "assistant", "completed work")
    db.end_session("child", "completed")
    raw = {
        "delegation_id": "async-delegation-1", "child_session_id": "child",
        "created_session_id": "child", "parent_session_id": "parent",
        "origin_version": 1, "launch_id": "async-launch-1",
        "event": {"is_batch": False, "goals": ["do work"]},
        "result": {"status": "completed", "exit_reason": "completed",
                   "truncated": False, "results": [{"task_index": 0,
                                                        "status": "completed"}]},
        "delivery_state": "delivered",
        "delivery_receipt": {"delivery_id": "async-delivery-1",
                              "session_id": "parent",
                              "acknowledged_at": 1},
    }
    monkeypatch.setattr(server, "_current_profile_name", lambda: "p")
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(async_delegation, "list_durable_delegations", lambda: [raw])

    @contextmanager
    def profile_db(_params):
        yield db

    monkeypatch.setattr(server, "_profile_db", profile_db)
    try:
        dispatch = live_owner_dispatch(server, lambda *args: None)
        records = dispatch.children("p")
        assert len(records) == 1
        record = records[0]
        proof = {
            "delegation_id": record["delegation_id"],
            "launch_id": record["launch_id"],
            "origin_version": record["origin_version"],
            "created_session_id": record["created_session_id"],
            "parent_session_id": record["parent_session_id"],
            "child_session_id": record["child_session_id"],
            "completion_id": record["delegation_id"],
            "delivery_id": record["delivery_receipt"]["delivery_id"],
            "delivery_session_id": record["delivery_receipt"]["session_id"],
            "delivery_acknowledged_at": record["delivery_receipt"]["acknowledged_at"],
        }
        assert dispatch.archive("p", "child", True,
                                expected_lineage=dispatch.lineage("p", "child"),
                                expected_proof=proof)
    finally:
        db.close()


def test_live_owner_archive_refuses_real_second_session_activity(tmp_path, monkeypatch):
    from tui_gateway import server
    from tui_gateway.owner_inbox import live_owner_dispatch

    profile_home = tmp_path / "profile"
    db = SessionDB(db_path=profile_home / "state.db")
    writer = SessionDB(db_path=profile_home / "state.db")
    db.create_session("parent", "desktop", git_repo_root="repo")
    db.append_message("parent", "assistant", "child result",
                      display_metadata={"delivery_id": "delivery-1", "delegation_id": "delegation-1"})
    db.create_session("child", "delegate", parent_session_id="parent",
                      git_repo_root="repo", model_config={
                          "_origin": {"version": 1, "launch_id": "launch-1",
                                      "created_session_id": "child",
                                      "parent_session_id": "parent"},
                          "_delegate_from": "parent",
                          "_created_by": "agent_delegate",
                          "_origin_kind": "delegated_child",
                      })
    db.append_message("child", "assistant", "completed work")
    db.end_session("child", "completed")
    monkeypatch.setattr(server, "_current_profile_name", lambda: "p")
    monkeypatch.setattr(server, "_sessions", {})

    @contextmanager
    def profile_db(_params):
        yield db

    monkeypatch.setattr(server, "_profile_db", profile_db)
    owner = live_owner_dispatch(server, lambda *args: None)
    lineage = owner.lineage("p", "child")
    proof = {"delegation_id": "delegation-1", "launch_id": "launch-1",
             "origin_version": 1, "created_session_id": "child",
             "parent_session_id": "parent", "child_session_id": "child",
             "completion_id": "delegation-1", "delivery_id": "delivery-1",
             "delivery_session_id": "parent", "delivery_acknowledged_at": 1}
    original = db.set_session_archived

    def append_before_archive(*args, **kwargs):
        writer.append_message("child", "assistant", "late concurrent activity")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "set_session_archived", append_before_archive)
    try:
        assert owner.archive("p", "child", True, expected_lineage=lineage,
                             expected_proof=proof) is False
        assert db.get_session("child")["archived"] == 0
    finally:
        writer.close()
        db.close()


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
