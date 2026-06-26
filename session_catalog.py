"""Shared session-list projection helpers for Hermes surfaces.

This module is intentionally dependency-light: Hermes Agent, Desktop API routes,
and the legacy WebUI can all import it without starting a server.  It owns the
stable, presentation-facing session catalog facts that otherwise tend to drift
between surfaces: raw-source normalization, compression-chain projection, and
workspace grouping/classification.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

MESSAGING_SOURCES = {
    "discord",
    "email",
    "wecom",
    "wecom_callback",
    "slack",
    "telegram",
    "weixin",
}

CLI_MIN_UNTITLED_MESSAGE_COUNT = 6
CLI_MIN_UNTITLED_USER_MESSAGE_COUNT = 2
NO_WORKSPACE_ID = "__no_workspace__"

SOURCE_LABELS = {
    "api_server": "API",
    "cli": "CLI",
    "cron": "Cron",
    "discord": "Discord",
    "email": "Email",
    "wecom": "WeCom",
    "wecom_callback": "WeCom Callback",
    "slack": "Slack",
    "telegram": "Telegram",
    "tool": "Tool",
    "tui": "TUI",
    "webui": "WebUI",
    "weixin": "Weixin",
}


def normalize_agent_session_source(raw_source: str | None) -> dict[str, str | None]:
    """Return stable source metadata for a raw ``sessions.source`` value."""
    raw = str(raw_source or "").strip().lower() or "unknown"

    if raw == "webui":
        session_source = "webui"
    elif raw in {"cli", "tui"}:
        session_source = "cli"
    elif raw in MESSAGING_SOURCES:
        session_source = "messaging"
    elif raw == "cron":
        session_source = "cron"
    elif raw == "tool":
        session_source = "tool"
    elif raw == "api_server":
        session_source = "api"
    else:
        session_source = "other"

    label = SOURCE_LABELS.get(raw)
    if not label:
        label = raw.replace("_", " ").title() if raw != "unknown" else "Agent"

    return {
        "raw_source": None if raw == "unknown" else raw,
        "session_source": session_source,
        "source_label": label,
    }


def _with_normalized_source(row: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_agent_session_source(row.get("source"))
    return {**row, **normalized}


def _safe_lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_source_name(value: object) -> str:
    source = _safe_lower(value)
    if not source:
        return ""
    if source.endswith(" session"):
        source = source[: -len(" session")].strip()
    return source


def _looks_like_default_cli_title(row: dict[str, Any]) -> bool:
    """Return True when a CLI row looks like framework-generated metadata."""
    title = _safe_lower(row.get("title"))
    if not title or title == "untitled":
        return True
    if title in {"cli", "cli session"}:
        return True

    source_candidates = {
        _normalize_source_name(row.get("source")),
        _normalize_source_name(row.get("session_source")),
        _normalize_source_name(row.get("source_tag")),
        _normalize_source_name(row.get("raw_source")),
        _normalize_source_name(row.get("source_label")),
    }
    source_candidates.discard("")
    source_candidates.add("cli")
    return any(title == f"{candidate} session" for candidate in source_candidates)


def _as_positive_int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _as_score(*values: Any) -> float:
    """Return the first numerically coercible value, else ``0.0``."""
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _count_user_turns(row: dict[str, Any]) -> int:
    user_turns = row.get("actual_user_message_count")
    if user_turns is None:
        user_turns = row.get("user_message_count")
    if user_turns is None:
        messages = row.get("messages") or []
        if isinstance(messages, list):
            return sum(
                1
                for msg in messages
                if _safe_lower(msg.get("role") if isinstance(msg, dict) else msg) == "user"
            )
        return 0
    return _as_positive_int(user_turns)


def _message_count(row: dict[str, Any]) -> int:
    """Return real message count, falling back only when no real count exists."""
    if "actual_message_count" in row and row.get("actual_message_count") not in (None, ""):
        return _as_positive_int(row.get("actual_message_count"))
    return _as_positive_int(row.get("message_count"))


def _has_cli_lineage(row: dict[str, Any]) -> bool:
    segment_count = _as_positive_int(row.get("_compression_segment_count"))
    return segment_count > 1 or bool(row.get("_lineage_root_id"))


def is_cli_session_row(row: dict[str, Any]) -> bool:
    """Return True for rows that should be treated as CLI-imported sessions."""
    if not isinstance(row, dict):
        return False
    source = _safe_lower(row.get("session_source"))
    source_tag = _safe_lower(row.get("source_tag"))
    raw_source = _safe_lower(row.get("raw_source"))
    source_name = _safe_lower(row.get("source"))
    source_label = _safe_lower(row.get("source_label"))
    if "webui" in {source, source_tag, raw_source, source_name, source_label}:
        return False
    non_cli_sources = MESSAGING_SOURCES | {"cron", "tool", "api", "api_server"}
    if {source, source_tag, raw_source, source_name, source_label} & non_cli_sources:
        return False
    if source == "messaging":
        return False
    if source == "cli":
        return True
    if (
        source_tag in {"cli", "tui"}
        or raw_source in {"cli", "tui"}
        or source_name in {"cli", "tui"}
        or source_label in {"cli", "tui"}
    ):
        return True

    # Legacy imported CLI rows may only be marked as CLI in sidebar metadata.
    return bool(
        row.get("is_cli_session")
        and source not in MESSAGING_SOURCES
        and source_tag not in MESSAGING_SOURCES
        and raw_source not in MESSAGING_SOURCES
        and source_name not in MESSAGING_SOURCES
        and _looks_like_default_cli_title(row)
    )


def is_cli_session_row_visible(row: dict[str, Any]) -> bool:
    """Return whether a CLI-related row should remain visible in the sidebar."""
    if not isinstance(row, dict):
        return False
    if not is_cli_session_row(row):
        return True

    message_count = _message_count(row)
    if message_count <= 0:
        return False

    if "tui" in {
        _normalize_source_name(row.get("source")),
        _normalize_source_name(row.get("source_tag")),
        _normalize_source_name(row.get("raw_source")),
        _normalize_source_name(row.get("source_label")),
    }:
        return True

    if _has_cli_lineage(row):
        return True
    if not _looks_like_default_cli_title(row):
        return True
    return _count_user_turns(row) >= CLI_MIN_UNTITLED_USER_MESSAGE_COUNT


def _is_continuation_session(parent: dict[str, Any] | None, child: dict[str, Any] | None) -> bool:
    """Return True when ``child`` is the next segment of the same conversation."""
    if not parent or not child:
        return False
    if str(child.get("session_source") or "").strip().lower() == "fork":
        return False
    parent_source = str(parent.get("source") or "").strip().lower()
    child_source = str(child.get("source") or "").strip().lower()
    if parent_source and child_source and parent_source != child_source:
        return False
    if parent.get("end_reason") not in {"compression", "cli_close"}:
        return False
    ended_at = parent.get("ended_at")
    if ended_at is None:
        return True
    try:
        return float(child.get("started_at") or 0) >= float(ended_at)
    except (TypeError, ValueError):
        return False


def _continuation_root_id(rows_by_id: dict[str, dict[str, Any]], session_id: str | None) -> str | None:
    """Return the visible lineage root for ``session_id`` by walking continuations."""
    if not session_id:
        return None
    root_id = str(session_id)
    current_id = root_id
    seen = {current_id}
    for _ in range(len(rows_by_id) + 1):
        current = rows_by_id.get(current_id)
        parent_id = current.get("parent_session_id") if current else None
        parent = rows_by_id.get(parent_id) if parent_id else None
        if not parent or not _is_continuation_session(parent, current):
            return root_id
        if parent_id in seen:
            return root_id
        root_id = str(parent_id)
        current_id = str(parent_id)
        seen.add(current_id)
    return root_id


def _segments(path: str) -> list[str]:
    return str(path or "").rstrip("/\\").replace("\\", "/").split("/") if str(path or "").strip() else []


def _base_name(path: str) -> str | None:
    parts = [p for p in _segments(path) if p]
    return parts[-1] if parts else None


def _with_base_name(path: str, name: str) -> str:
    raw = str(path or "").rstrip("/\\")
    if not raw:
        return name
    sep = "\\" if "\\" in raw and "/" not in raw else "/"
    parts = raw.replace("\\", "/").split("/")
    parts[-1] = name
    joined = "/".join(parts)
    return joined.replace("/", sep) if sep == "\\" else joined


def _clean_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser())
    except Exception:
        return text


def classify_session_workspace(
    workspace: Any = None,
    *,
    cwd: Any = None,
    worktree_path: Any = None,
    worktree_branch: Any = None,
    worktree_repo_root: Any = None,
    git_branch: Any = None,
    git_repo_root: Any = None,
    no_workspace_label: str = "No workspace",
) -> dict[str, Any]:
    """Return stable workspace grouping metadata for a session row.

    The output mirrors Desktop's ``parent → worktree → sessions`` model while
    remaining useful for WebUI and API clients that only need a flat workspace
    label.  Git/worktree metadata supplied by a caller wins; otherwise we use the
    same path heuristic Desktop uses for remote/unprobeable paths:
    ``<repo>-wt-<branch>`` groups under sibling ``<repo>``.

    ``git_branch`` / ``git_repo_root`` are Hermes Agent's persisted field names;
    ``worktree_branch`` / ``worktree_repo_root`` are the legacy WebUI aliases.
    """
    path = _clean_path(cwd) or _clean_path(workspace) or _clean_path(worktree_path)
    wt_path = _clean_path(worktree_path) or path
    repo_root = _clean_path(worktree_repo_root) or _clean_path(git_repo_root)
    branch = str(worktree_branch or git_branch or "").strip()

    if repo_root and wt_path:
        worktree_key = wt_path
        parent_key = repo_root
        parent_label = _base_name(repo_root) or repo_root
        worktree_label = branch or _base_name(wt_path) or wt_path
        kind = "worktree" if wt_path != repo_root else "directory"
    elif path:
        base = _base_name(path) or path
        match = None
        try:
            import re

            match = re.match(r"^(.+)-wt-(.+)$", base)
        except Exception:
            match = None
        if match:
            parent_label = match.group(1)
            parent_key = _with_base_name(path, parent_label)
            worktree_key = path
            worktree_label = match.group(2)
            kind = "worktree"
        else:
            parent_key = path
            parent_label = base
            worktree_key = path
            worktree_label = base
            kind = "directory"
        wt_path = worktree_key
        repo_root = parent_key
    else:
        return {
            "workspace_type": "none",
            "workspace_kind": "none",
            "workspace_id": NO_WORKSPACE_ID,
            "workspace_label": no_workspace_label,
            "workspace_path": None,
            "workspace_group_id": NO_WORKSPACE_ID,
            "workspace_group_label": no_workspace_label,
            "workspace_parent_id": NO_WORKSPACE_ID,
            "workspace_parent_label": no_workspace_label,
            "workspace_parent_path": None,
        }

    return {
        "workspace_type": kind,
        "workspace_kind": kind,
        "workspace_id": worktree_key,
        "workspace_label": worktree_label,
        "workspace_path": wt_path,
        "workspace_group_id": worktree_key,
        "workspace_group_label": worktree_label,
        "workspace_parent_id": parent_key,
        "workspace_parent_label": parent_label,
        "workspace_parent_path": repo_root,
    }


def normalize_session_catalog_row(row: dict[str, Any], *, no_workspace_label: str = "No workspace") -> dict[str, Any]:
    """Attach shared source + workspace catalog fields to one session row."""
    if not isinstance(row, dict):
        return row
    out = dict(row)

    source_seed = out.get("source") or out.get("raw_source") or out.get("source_tag")
    source_meta = normalize_agent_session_source(source_seed)
    for key, value in source_meta.items():
        if out.get(key) in (None, ""):
            out[key] = value
    if out.get("source_tag") in (None, "") and source_meta.get("raw_source"):
        out["source_tag"] = source_meta["raw_source"]

    workspace_path = out.get("cwd") or out.get("workspace") or out.get("worktree_path")
    workspace_meta = classify_session_workspace(
        workspace_path,
        cwd=out.get("cwd"),
        worktree_path=out.get("worktree_path"),
        worktree_branch=out.get("worktree_branch"),
        worktree_repo_root=out.get("worktree_repo_root"),
        git_branch=out.get("git_branch"),
        git_repo_root=out.get("git_repo_root"),
        no_workspace_label=no_workspace_label,
    )
    for key, value in workspace_meta.items():
        if out.get(key) in (None, ""):
            out[key] = value

    resolved_path = workspace_meta.get("workspace_path") or workspace_path
    if resolved_path:
        if out.get("workspace") in (None, ""):
            out["workspace"] = resolved_path
        if out.get("cwd") in (None, ""):
            out["cwd"] = resolved_path

    out["catalog_projection_version"] = 1
    return out


def project_agent_session_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse continuation chains into one logical session-catalog row.

    The visible conversation keeps the chain head's display identity where
    useful, but navigation/import points at the freshest messageful tip. Branches
    and cross-source children remain distinct rows.
    """
    copied_rows = [dict(row) for row in (rows or []) if isinstance(row, dict) and row.get("id")]
    rows_by_id = {str(row["id"]): row for row in copied_rows}
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    continuation_child_ids: set[str] = set()

    for row in copied_rows:
        parent_id = row.get("parent_session_id")
        if not parent_id:
            continue
        parent_id = str(parent_id)
        children_by_parent.setdefault(parent_id, []).append(row)
        parent = rows_by_id.get(parent_id)
        if _is_continuation_session(parent, row):
            continuation_child_ids.add(str(row["id"]))
        else:
            row["relationship_type"] = "child_session"
            row["parent_title"] = parent.get("title") if parent else None
            row["parent_source"] = parent.get("source") if parent else None
            parent_root = _continuation_root_id(rows_by_id, parent_id)
            if parent_root:
                row["_parent_lineage_root_id"] = parent_root

    for children in children_by_parent.values():
        children.sort(key=lambda row: row.get("started_at") or 0, reverse=True)

    def continuation_tip(row: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
        latest_importable = row if _message_count(row) > 0 else None
        segment_count = 0
        best_depth = 1
        best_score = (
            _as_score(latest_importable.get("last_activity"), latest_importable.get("last_active"), latest_importable.get("started_at"))
            if latest_importable
            else 0
        )
        stack: list[tuple[dict[str, Any], int]] = [(row, 1)]
        seen: set[str] = set()
        while stack:
            current, depth = stack.pop()
            current_id = current.get("id")
            if not current_id or current_id in seen:
                continue
            seen.add(str(current_id))
            segment_count += 1
            current_score = _as_score(current.get("last_activity"), current.get("last_active"), current.get("started_at"))
            current_count = _message_count(current)
            if current_count > 0 and (current_score > best_score or (current_score == best_score and depth >= best_depth)):
                latest_importable = current
                best_depth = depth
                best_score = current_score
            for child in children_by_parent.get(str(current_id), []):
                child_id = child.get("id")
                if not child_id or str(child_id) in seen:
                    continue
                if not _is_continuation_session(current, child):
                    continue
                stack.append((child, depth + 1))
        return latest_importable, max(segment_count, 1)

    projected: list[dict[str, Any]] = []
    for row in copied_rows:
        if str(row["id"]) in continuation_child_ids:
            continue
        segment_count = 1
        tip = row
        if row.get("end_reason") in {"compression", "cli_close"}:
            tip, segment_count = continuation_tip(row)
        if not tip or _message_count(tip) <= 0:
            continue
        if tip is row:
            projected.append(normalize_session_catalog_row(_with_normalized_source(dict(row))))
            continue
        merged = dict(row)
        for key in (
            "id",
            "model",
            "message_count",
            "actual_message_count",
            "actual_user_message_count",
            "ended_at",
            "end_reason",
            "last_activity",
            "last_active",
            "cwd",
            "worktree_path",
            "worktree_branch",
            "worktree_repo_root",
        ):
            if key in tip:
                merged[key] = tip[key]
        if str(tip.get("source") or "").strip().lower() == "tui":
            for key in ("title",):
                if key in tip:
                    merged[key] = tip[key]
        else:
            if not merged.get("title"):
                merged["title"] = tip.get("title")
            if not merged.get("source"):
                merged["source"] = tip.get("source")
        merged["_lineage_root_id"] = row["id"]
        merged["_lineage_tip_id"] = tip["id"]
        merged["_compression_segment_count"] = segment_count
        projected.append(normalize_session_catalog_row(_with_normalized_source(merged)))

    projected.sort(
        key=lambda row: _as_score(row.get("last_activity"), row.get("started_at")),
        reverse=True,
    )
    return projected


__all__ = [
    "CLI_MIN_UNTITLED_MESSAGE_COUNT",
    "CLI_MIN_UNTITLED_USER_MESSAGE_COUNT",
    "MESSAGING_SOURCES",
    "NO_WORKSPACE_ID",
    "SOURCE_LABELS",
    "classify_session_workspace",
    "is_cli_session_row",
    "is_cli_session_row_visible",
    "normalize_agent_session_source",
    "normalize_session_catalog_row",
    "project_agent_session_rows",
    "_continuation_root_id",
    "_is_continuation_session",
    "_looks_like_default_cli_title",
    "_with_normalized_source",
]
