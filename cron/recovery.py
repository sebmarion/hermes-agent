"""Budget-exhaustion recovery state for cron jobs.

When a cron job hits ``max_iterations`` (default 90), the core agent produces
a ``RECOVERY_REQUIRED`` message via
``agent.verification_stop.build_budget_exhausted_verification_response``.
The cron scheduler delivers that message and marks the run as ``success=True``
(because the agent *did* produce a response — just not completed work).

Without this module, the recovery context is delivered once and then lost.
The next tick starts fresh with the original prompt, hits the same budget
limit, and the job loops forever — silently burning tokens with no progress.

This module persists three things across ticks:

1. **Recovery context** — the recovery text (changed paths, verification
   status, pending verification gate, recovery prompt) so the next tick can
   prepend it and the agent knows it's resuming interrupted work.
2. **Consecutive exhaustion count** — how many ticks in a row exhausted the
   budget.  After a configurable threshold (default 3), the job should be
   auto-paused and an escalation alert delivered.
3. **Timestamp** — when the first exhaustion occurred, so stale recovery
   records can be expired.

Storage: ``~/.hermes/cron/recovery/{job_id}.json`` — one file per job.
Cleared on the first successful run (budget not exhausted).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

RECOVERY_DIR_NAME = "recovery"
# After this many consecutive budget exhaustions, the job should auto-pause.
DEFAULT_EXHAUSTION_THRESHOLD = 3
# Recovery records older than this are expired (the job likely succeeded
# via a manual run or was deleted).  7 days.
RECOVERY_EXPIRY_DAYS = 7
_MAX_RECOVERY_TEXT_CHARS = 8000


def _recovery_dir() -> Path:
    """Return the per-profile cron recovery directory, creating it if needed."""
    d = get_hermes_home() / "cron" / RECOVERY_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def _recovery_path(job_id: str) -> Path:
    """Return the recovery file path for a job.

    Job IDs are validated as filesystem-safe path components by
    ``cron.jobs._job_output_dir``; the same constraint applies here.
    """
    safe = "".join(c for c in str(job_id) if c.isalnum() or c in "-_")
    if not safe or safe != str(job_id):
        raise ValueError(f"Invalid job_id for recovery path: {job_id!r}")
    return _recovery_dir() / f"{safe}.json"


def get_recovery_record(job_id: str) -> Optional[Dict[str, Any]]:
    """Read the persisted recovery state for a job.

    Returns ``None`` when no recovery record exists or it has expired.
    Never raises — a corrupt or unreadable file is logged and treated as
    no recovery state (the job runs normally).
    """
    try:
        path = _recovery_path(job_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "Recovery record for job %s is corrupt (%s) — ignoring", job_id, e
        )
        return None
    if not isinstance(data, dict):
        logger.warning("Recovery record for job %s is not a dict — ignoring", job_id)
        return None

    # Expire stale records.
    first_ts = data.get("first_exhaustion_ts")
    if first_ts is not None:
        try:
            first_dt = datetime.fromisoformat(str(first_ts))
            if datetime.now() - first_dt > timedelta(days=RECOVERY_EXPIRY_DAYS):
                logger.info(
                    "Recovery record for job %s is older than %d days — expiring",
                    job_id, RECOVERY_EXPIRY_DAYS,
                )
                clear_recovery_record(job_id)
                return None
        except (ValueError, TypeError):
            pass  # bad timestamp — keep the record, better safe than sorry

    return data


def save_recovery_record(
    job_id: str,
    *,
    recovery_text: str,
    budget_used: int,
    budget_max: int,
    changed_paths: Optional[list] = None,
) -> Dict[str, Any]:
    """Persist (or update) the recovery state for a job.

    Increments ``consecutive_exhaustions`` if a record already exists;
    initializes it to 1 on first save.  Returns the saved record.
    """
    existing = get_recovery_record(job_id)
    now_ts = datetime.now().isoformat()
    now_epoch = time.time()

    consecutive = 1
    first_exhaustion_ts = now_ts
    if existing:
        consecutive = int(existing.get("consecutive_exhaustions", 0)) + 1
        # Preserve the original first-exhaustion timestamp.
        first_exhaustion_ts = existing.get("first_exhaustion_ts") or now_ts

    # Truncate recovery text to prevent unbounded growth across ticks.
    text = recovery_text or ""
    if len(text) > _MAX_RECOVERY_TEXT_CHARS:
        text = text[:_MAX_RECOVERY_TEXT_CHARS] + "\n\n[... recovery text truncated ...]"

    record: Dict[str, Any] = {
        "job_id": job_id,
        "recovery_text": text,
        "budget_used": budget_used,
        "budget_max": budget_max,
        "changed_paths": list(changed_paths) if changed_paths else [],
        "consecutive_exhaustions": consecutive,
        "first_exhaustion_ts": first_exhaustion_ts,
        "last_exhaustion_ts": now_ts,
        "last_exhaustion_epoch": now_epoch,
    }

    try:
        path = _recovery_path(job_id)
        import tempfile

        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".rec_")
        try:
            with __import__("os").fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2)
                f.flush()
                f.seek(0)
            import shutil
            shutil.move(tmp, path)
        except Exception:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.error("Failed to save recovery record for job %s: %s", job_id, e)
    else:
        logger.info(
            "Saved recovery record for job %s (consecutive=%d, budget=%d/%d)",
            job_id, consecutive, budget_used, budget_max,
        )

    return record


def clear_recovery_record(job_id: str) -> None:
    """Delete the recovery record for a job.

    Called after a successful run (budget not exhausted) so the next tick
    starts fresh.  Never raises — a missing file is fine.
    """
    try:
        path = _recovery_path(job_id)
    except ValueError:
        return
    try:
        if path.exists():
            path.unlink()
            logger.info("Cleared recovery record for job %s", job_id)
    except OSError as e:
        logger.warning("Failed to clear recovery record for job %s: %s", job_id, e)


def build_recovery_prompt_prefix(record: Dict[str, Any]) -> str:
    """Build the prompt text to prepend to a job's prompt when recovering.

    The agent sees this context block before its original prompt, so it
    knows it's resuming interrupted work and should prioritize verification
    over new planning.
    """
    consecutive = record.get("consecutive_exhaustions", 1)
    recovery_text = record.get("recovery_text", "")
    budget_used = record.get("budget_used", 0)
    budget_max = record.get("budget_max", 0)

    header = (
        "## ⚠️ Recovering from Budget Exhaustion\n\n"
        "The previous run of this cron job exhausted its iteration budget "
        f"({budget_used}/{budget_max}) before completing.  You are now in a "
        "**recovery run**.  Prioritize verification and repair of the "
        "existing work — do NOT restart or re-plan from scratch.\n\n"
        f"Consecutive exhaustions: {consecutive}\n\n"
        "Recovery context from the interrupted run:\n"
        "```\n"
        f"{recovery_text}\n"
        "```\n\n"
        "---\n\n"
    )
    return header


def should_auto_pause(
    record: Optional[Dict[str, Any]],
    threshold: int = DEFAULT_EXHAUSTION_THRESHOLD,
) -> bool:
    """Return True if the job should be auto-paused due to chronic exhaustion."""
    if record is None:
        return False
    return int(record.get("consecutive_exhaustions", 0)) >= threshold


__all__ = [
    "get_recovery_record",
    "save_recovery_record",
    "clear_recovery_record",
    "build_recovery_prompt_prefix",
    "should_auto_pause",
    "DEFAULT_EXHAUSTION_THRESHOLD",
]
