"""Best-effort local workspace attribution for persisted Hermes sessions."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def normalize_local_workspace(
    cwd: str | os.PathLike[str] | None,
) -> str | None:
    """Return a real absolute directory, rejecting placeholders and dead paths."""
    if cwd is None:
        return None
    raw = str(cwd).strip()
    if not raw or raw.lower() in {"unknown", "none", "null", "."}:
        return None
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        return None
    resolved = os.path.realpath(os.path.abspath(expanded))
    return resolved if os.path.isdir(resolved) else None


def persist_git_metadata_async(
    *,
    db_path: str | os.PathLike[str],
    session_id: str,
    cwd: str | os.PathLike[str] | None,
) -> threading.Thread | None:
    """Enrich a session row with git facts without blocking its creation path."""
    workspace = normalize_local_workspace(cwd)
    if not session_id or not workspace or not isinstance(db_path, (str, Path)):
        return None
    database = Path(db_path).expanduser()

    def _persist() -> None:
        try:
            from hermes_state import SessionDB
            from tui_gateway.git_probe import branch, common_repo_root

            git_branch = branch(workspace) or None
            git_root = common_repo_root(workspace) or None
            if not (git_branch or git_root):
                return
            db = SessionDB(db_path=database)
            try:
                db.update_session_cwd(
                    session_id,
                    workspace,
                    git_branch=git_branch,
                    git_repo_root=git_root,
                )
            finally:
                db.close()
        except Exception:
            logger.debug(
                "failed to persist session git metadata for %s",
                session_id,
                exc_info=True,
            )

    thread = threading.Thread(
        target=_persist,
        name=f"session-git-meta:{session_id[:16]}",
        daemon=True,
    )
    try:
        thread.start()
    except (OSError, RuntimeError):
        logger.debug(
            "failed to start session git metadata worker for %s",
            session_id,
            exc_info=True,
        )
        return None
    return thread
