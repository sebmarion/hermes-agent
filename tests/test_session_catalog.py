from session_catalog import (
    classify_session_workspace,
    normalize_agent_session_source,
    project_agent_session_rows,
)


def test_normalize_agent_session_source_is_shared_catalog_contract():
    assert normalize_agent_session_source("tui") == {
        "raw_source": "tui",
        "session_source": "cli",
        "source_label": "TUI",
    }
    assert normalize_agent_session_source("telegram")["session_source"] == "messaging"


def test_classify_session_workspace_uses_worktree_heuristic():
    meta = classify_session_workspace("/work/hermes-agent-wt-api")

    assert meta["workspace_kind"] == "worktree"
    assert meta["workspace_parent_id"] == "/work/hermes-agent"
    assert meta["workspace_parent_label"] == "hermes-agent"
    assert meta["workspace_group_label"] == "api"


def test_classify_session_workspace_accepts_agent_git_aliases():
    meta = classify_session_workspace(
        cwd="/work/hermes-agent-wt-api",
        git_branch="api",
        git_repo_root="/work/hermes-agent",
    )

    assert meta["workspace_kind"] == "worktree"
    assert meta["workspace_id"] == "/work/hermes-agent-wt-api"
    assert meta["workspace_parent_id"] == "/work/hermes-agent"
    assert meta["workspace_parent_label"] == "hermes-agent"
    assert meta["workspace_group_label"] == "api"


def test_project_agent_session_rows_collapses_compression_chain_and_preserves_workspace():
    rows = [
        {
            "id": "root",
            "source": "cli",
            "title": "Root title",
            "model": "m",
            "message_count": 3,
            "actual_message_count": 3,
            "actual_user_message_count": 1,
            "started_at": 100.0,
            "ended_at": 120.0,
            "end_reason": "compression",
            "last_activity": 119.0,
            "cwd": "/work/app",
        },
        {
            "id": "tip",
            "source": "cli",
            "title": "Tip title",
            "model": "m2",
            "message_count": 2,
            "actual_message_count": 2,
            "actual_user_message_count": 1,
            "started_at": 130.0,
            "ended_at": None,
            "end_reason": None,
            "last_activity": 140.0,
            "parent_session_id": "root",
            "cwd": "/work/app-wt-next",
        },
    ]

    projected = project_agent_session_rows(rows)

    assert [row["id"] for row in projected] == ["tip"]
    row = projected[0]
    assert row["_lineage_root_id"] == "root"
    assert row["_lineage_tip_id"] == "tip"
    assert row["session_source"] == "cli"
    assert row["source_label"] == "CLI"
    assert row["workspace_kind"] == "worktree"
    assert row["workspace_parent_id"] == "/work/app"
    assert row["workspace_group_label"] == "next"


def test_project_agent_session_rows_actual_count_beats_stale_denormalized_count():
    rows = [
        {
            "id": "root",
            "source": "cli",
            "title": "Root title",
            "message_count": 1,
            "actual_message_count": 1,
            "started_at": 100.0,
            "ended_at": 110.0,
            "end_reason": "compression",
            "last_activity": 109.0,
        },
        {
            "id": "fresh",
            "source": "cli",
            "title": "Fresh tip",
            "message_count": 2,
            "actual_message_count": 2,
            "started_at": 120.0,
            "last_activity": 130.0,
            "parent_session_id": "root",
        },
        {
            "id": "empty-stale",
            "source": "cli",
            "title": "Empty stale tip",
            "message_count": 3,
            "actual_message_count": 0,
            "started_at": 140.0,
            "last_activity": 150.0,
            "parent_session_id": "root",
        },
    ]

    projected = project_agent_session_rows(rows)

    assert [row["id"] for row in projected] == ["fresh"]
    assert projected[0]["_lineage_tip_id"] == "fresh"
    assert projected[0]["message_count"] == 2


def test_project_agent_session_rows_keeps_visible_rows_sorted_by_activity():
    projected = project_agent_session_rows([
        {
            "id": "old",
            "source": "cli",
            "title": "Old",
            "message_count": 1,
            "actual_message_count": 1,
            "started_at": 10.0,
            "last_activity": 20.0,
        },
        {
            "id": "new",
            "source": "cli",
            "title": "New",
            "message_count": 1,
            "actual_message_count": 1,
            "started_at": 30.0,
            "last_activity": 40.0,
        },
    ])

    assert [row["id"] for row in projected] == ["new", "old"]


def test_project_agent_session_rows_uses_tip_title_when_root_title_is_empty():
    projected = project_agent_session_rows([
        {
            "id": "root",
            "source": "cli",
            "title": "",
            "message_count": 1,
            "actual_message_count": 1,
            "started_at": 100.0,
            "ended_at": 110.0,
            "end_reason": "compression",
            "last_activity": 109.0,
        },
        {
            "id": "tip",
            "source": "cli",
            "title": "Tip title",
            "message_count": 2,
            "actual_message_count": 2,
            "started_at": 120.0,
            "last_activity": 130.0,
            "parent_session_id": "root",
        },
    ])

    assert projected[0]["title"] == "Tip title"
