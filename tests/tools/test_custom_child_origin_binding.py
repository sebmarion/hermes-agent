"""Our immutable origin mapping must survive retries without being rewritten."""
import time
import pytest
from tools import async_delegation as ad

@pytest.fixture
def binding(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "get_hermes_home", lambda: tmp_path)
    ad._persist_dispatch({"delegation_id": "d-1", "session_key": "parent",
                          "parent_session_id": "parent", "dispatched_at": time.time()})
    return dict(child_session_id="child", launch_id="launch", origin_version=1,
                created_session_id="child", parent_session_id="parent")

def read_row():
    with ad._DB_LOCK, ad._transaction() as conn:
        return conn.execute("SELECT child_session_id,launch_id,origin_version,"
                            "created_session_id,parent_session_id FROM async_delegations"
                            " WHERE delegation_id='d-1'").fetchone()

def test_exact_retry_is_idempotent(binding):
    assert ad.bind_child_delegation("d-1", **binding)
    before = read_row()
    assert ad.bind_child_delegation("d-1", **binding)
    assert read_row() == before

@pytest.mark.parametrize("key,value", [("child_session_id","other"),
    ("launch_id","other"),("created_session_id","other"),("parent_session_id","other"),
    ("origin_version",True),("origin_version",2)])
def test_existing_origin_cannot_be_rebound(binding, key, value):
    assert ad.bind_child_delegation("d-1", **binding)
    before = read_row()
    assert not ad.bind_child_delegation("d-1", **{**binding, key:value})
    assert read_row() == before

def test_initial_binding_cannot_change_dispatched_parent(binding):
    before = read_row()
    assert not ad.bind_child_delegation("d-1", **{**binding,"parent_session_id":"foreign"})
    assert read_row() == before
