"""Regression tests for our compression/archive extension, not upstream policy."""
import json
import time
import pytest
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    value = SessionDB(tmp_path / 'state.db')
    yield value
    value.close()


def lineage_with_branch(db):
    db.create_session('root', source='cli')
    db.publish_compression_child(parent_session_id='root', child_session_id='tip',
        source='cli', messages=[{'role': 'user', 'content': 'fixture'}],
        require_compression_lease=False)
    db.reopen_session('root')
    db.create_session('branch', source='cli', parent_session_id='root',
        model_config={'_branched_from': 'root'})


def test_archiving_reopened_compression_root_does_not_archive_branch(db):
    lineage_with_branch(db)
    assert db.set_session_archived('root', True)
    assert {s: db.get_session(s)['archived'] for s in ('root', 'tip', 'branch')} == {
        'root': 1, 'tip': 1, 'branch': 0}


def test_unarchiving_compression_root_does_not_unarchive_branch(db):
    lineage_with_branch(db)
    db._conn.execute('UPDATE sessions SET archived=1')
    db._conn.commit()
    assert db.set_session_archived('root', False)
    assert {s: db.get_session(s)['archived'] for s in ('root', 'tip', 'branch')} == {
        'root': 0, 'tip': 0, 'branch': 1}


def test_unarchive_override_applies_to_the_whole_logical_conversation(db):
    lineage_with_branch(db)
    assert db.set_session_archived('tip', False)
    db._conn.execute('CREATE TABLE IF NOT EXISTS session_turn_leases '
                     '(conversation_id TEXT PRIMARY KEY,holder TEXT,acquired_at REAL,expires_at REAL)')
    with db._read_ctx() as conn:
        key = db._session_turn_lease_key_on_conn(conn, 'root')
    db._conn.execute('INSERT OR REPLACE INTO session_turn_leases VALUES (?,?,?,?)',
                     (key, 'archive-worker', time.time(), time.time() + 60))
    db._conn.commit()
    assert not db.set_session_archived('root', True, turn_lease_holder='archive-worker')
    assert db.get_session('root')['archived'] == 0
    assert db.get_session('tip')['archived'] == 0


@pytest.mark.parametrize("marker", ["_branched_from", "_delegate_from", "_reset_from"])
def test_explicit_other_child_is_not_stamped_or_cascaded(db, marker):
    db.create_session('root', source='cli')
    db.publish_compression_child(parent_session_id='root', child_session_id='tip',
        source='cli', messages=[{'role': 'user', 'content': 'fixture'}],
        require_compression_lease=False)
    db.create_session('other', source='cli', parent_session_id='root',
        model_config={marker: 'root'})
    assert '_compression_from' not in json.loads(db.get_session('other')['model_config'])
    assert db.set_session_archived('root', True)
    assert db.get_session('other')['archived'] == 0
    assert db.set_session_pinned('root', True)
    assert not db.get_session('other')['pinned']


def test_rejected_manual_restore_has_no_keep_override(db):
    lineage_with_branch(db)
    assert not db.set_session_archived('root', False, expected_session_ids=['foreign'])
    assert db._conn.execute('SELECT COUNT(*) FROM session_archive_overrides').fetchone()[0] == 0
