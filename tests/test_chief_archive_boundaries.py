"""Native archive checks after an earlier child-state snapshot."""
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.owner_inbox import live_owner_dispatch


@pytest.mark.parametrize("late_change", [None, "pin", "queue", "rewind"])
def test_archive_rechecks_late_changes(tmp_path, monkeypatch, late_change):
    db = SessionDB(db_path=tmp_path / "state.db")
    parent, child, unrelated = "parent", "child", "unrelated"
    for session_id in (parent, unrelated):
        db.create_session(session_id, "desktop", git_repo_root="repo")
    parent_user = db.append_message(parent, "user", "Synthetic parent request")
    db.append_message(parent, "assistant", "Synthetic child result",
                      display_metadata={"delivery_id": "delivery-1", "delegation_id": "delegation-1"})
    db.create_session(child, "delegate", parent_session_id=parent,
                      git_repo_root="repo", model_config={
                          "_origin": {"version": 1, "launch_id": "launch-1",
                                      "created_session_id": child,
                                      "parent_session_id": parent},
                          "_delegate_from": parent,
                          "_created_by": "agent_delegate",
                          "_origin_kind": "delegated_child",
                      })
    db.append_message(child, "assistant", "Synthetic completed work")
    db.end_session(child, "completed")
    live = {"session_key": child, "agent": SimpleNamespace(session_id=child),
            "history_lock": threading.RLock(), "running": False,
            "queued_prompt": None, "queued_prompts": []}
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_sessions", {"live-child": live})
    monkeypatch.setattr(server, "_sessions_lock", threading.RLock())
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_pending_prompt_payloads", {})

    @contextmanager
    def profile_db(_params):
        yield db

    monkeypatch.setattr(server, "_profile_db", profile_db)
    owner = live_owner_dispatch(server, lambda *args: None)
    lineage = owner.lineage("default", child)
    assert [row["id"] for row in lineage] == [child]
    proof = {"delegation_id": "delegation-1", "launch_id": "launch-1",
             "origin_version": 1, "created_session_id": child,
             "parent_session_id": parent, "child_session_id": child,
             "completion_id": "delegation-1", "delivery_id": "delivery-1",
             "delivery_session_id": parent, "delivery_acknowledged_at": 1}
    try:
        if late_change == "pin":
            assert db.set_session_pinned(child, True)
        elif late_change == "queue":
            with live["history_lock"]:
                server._enqueue_prompt(live, "Synthetic next request", None)
            assert live["queued_prompt"] is not None
        elif late_change == "rewind":
            assert db.rewind_to_message(parent, parent_user)["rewound_count"] == 2
            assert not db.has_persisted_message_marker(
                parent, role="assistant", key="delivery_id", value="delivery-1")
            assert any(not row["active"] and row["role"] == "assistant"
                       for row in db.get_messages(parent, include_inactive=True))
        result = owner.archive("default", child, True,
                               expected_lineage=lineage, expected_proof=proof)
        assert result is (late_change is None)
        assert {sid: bool(db.get_session(sid)["archived"])
                for sid in (parent, child, unrelated)} == {
                    parent: False, child: late_change is None, unrelated: False}
    finally:
        db.close()
