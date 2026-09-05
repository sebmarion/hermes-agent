"""Durable terminal delivery receipt extensions for the owner inbox.

Uses the native async ledger transaction and preserves pending output until
its exact assistant transcript identity has been acknowledged.
"""
from __future__ import annotations
import json
import time
_TERMINAL_OUTBOX_CLAIM_SECONDS = 60.0
import sqlite3
from typing import Any, Dict, List, Optional
from tools.async_delegation import _DB_LOCK, _transaction

def bind_child_delegation(delegation_id: str, *, child_session_id: str,
                           launch_id: str, origin_version: int,
                           created_session_id: str, parent_session_id: str) -> bool:
    """Persist the immutable child/launcher mapping for one delegation."""
    if not all((delegation_id, child_session_id, launch_id, created_session_id,
                parent_session_id)) or origin_version != 1:
        return False
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET child_session_id=?, launch_id=?,
                      origin_version=?, created_session_id=?, parent_session_id=?
               WHERE delegation_id=? AND (child_session_id IS NULL
                                          OR child_session_id=?)""",
            (child_session_id, launch_id, origin_version, created_session_id,
             parent_session_id, delegation_id, child_session_id),
        )
        return cur.rowcount == 1

def list_durable_delegations() -> List[Dict[str, Any]]:
    """Return durable child mappings with completion and delivery evidence."""
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT d.delegation_id, d.parent_session_id, d.child_session_id,
 d.launch_id, d.origin_version, d.created_session_id, d.state,
                      d.event_json, d.result_json, d.delivery_state,
                      o.delivery_id, o.session_id, o.acknowledged_at,
                      d.dispatched_at
                 FROM async_delegations d
                 LEFT JOIN async_terminal_outbox o ON o.delegation_id=d.delegation_id
                WHERE d.child_session_id IS NOT NULL
                  OR d.result_json IS NOT NULL"""
        ).fetchall()
        out = []
        for row in rows:
            try:
                event = json.loads(row[7] or "{}")
                result = json.loads(row[8] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            delivery_receipt = (
                {"delivery_id": row[10], "session_id": row[11],
                 "acknowledged_at": row[12]}
                if row[10] is not None else None
            )
            base = {
                "delegation_id": row[0], "parent_session_id": row[1],
                "child_session_id": row[2], "launch_id": row[3],
                "origin_version": row[4], "created_session_id": row[5],
                "status": row[6], "event": event, "result": result,
                "delivery_state": row[9], "delivery_receipt": delivery_receipt,
                "origin_created_at": row[13],
            }
            receipt = result.get("archive_receipt") if isinstance(result, dict) else None
            entries = receipt.get("children") if isinstance(receipt, dict) else None
            if not isinstance(entries, list):
                if row[2] is not None:
                    out.append(base)
                continue
            result_entries = result.get("results") if isinstance(result, dict) else None
            if not isinstance(result_entries, list):
                result_entries = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                task_index = entry.get("task_index")
                mapped_result = next(
                    (candidate for candidate in result_entries
                     if isinstance(candidate, dict)
                     and candidate.get("task_index") == task_index),
                    None,
                )
                item = dict(base)
                item.update({
                    "child_session_id": entry.get("child_session_id"),
                    "launch_id": entry.get("launch_id"),
                    "origin_version": entry.get("origin_version"),
                    "created_session_id": entry.get("created_session_id"),
                    "parent_session_id": entry.get("parent_session_id"),
                    "completion_id": entry.get("completion_id") or (
                        row[0] if not receipt.get("is_batch") else None
                    ),
                    "archive_receipt_entry": dict(entry),
                    "archive_result_entry": (
                        dict(mapped_result) if isinstance(mapped_result, dict) else None
                    ),
                })
                out.append(item)
        return out

def get_delivery_receipt(delegation_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT delivery_id, session_id, acknowledged_at
                 FROM async_terminal_outbox
                WHERE delegation_id=? AND acknowledged_at IS NOT NULL""",
            (delegation_id,),
        ).fetchone()
    if row is None:
        return None
    return {"delivery_id": row[0], "session_id": row[1],
            "acknowledged_at": row[2]}

def commit_terminal_output(
    evt: Dict[str, Any],
    claim_id: str,
    terminal_event: Dict[str, Any],
) -> Dict[str, Any]:
    """Atomically settle one completion and publish its terminal outbox row."""
    delegation_id = str(evt.get("delegation_id") or "")
    session_id = str(
        terminal_event.get("stored_session_id")
        or terminal_event.get("session_id")
        or ""
    )
    payload = terminal_event.get("payload")
    text = payload.get("text") if isinstance(payload, dict) else None
    if evt.get("type") != "async_delegation" or not delegation_id or not claim_id:
        raise ValueError("a claimed async delegation is required")
    if terminal_event.get("type") != "message.complete" or not session_id:
        raise ValueError("a session-scoped message.complete event is required")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("terminal output text must be non-empty")

    expected_session_ids = {
        str(value).strip()
        for value in (
            evt.get("session_key"),
            evt.get("parent_session_id"),
            evt.get("origin_session_id"),
            evt.get("origin_ui_session_id"),
        )
        if value is not None and str(value).strip()
    }
    if not expected_session_ids or session_id not in expected_session_ids:
        raise ValueError("terminal output destination does not belong to the claimed delegation")

    delivery_id = f"async-delegation:{delegation_id}"
    event_json = json.dumps(terminal_event, sort_keys=True, separators=(",", ":"))
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        persisted = conn.execute(
            """SELECT origin_session, origin_ui_session_id,
                      parent_session_id, origin_session_id
               FROM async_delegations WHERE delegation_id=?""",
            (delegation_id,),
        ).fetchone()
        if persisted is None:
            raise RuntimeError("async delegation record is missing")
        persisted_session_ids = {
            str(value).strip()
            for value in persisted
            if value is not None and str(value).strip()
        }
        if session_id not in persisted_session_ids:
            raise ValueError("terminal output destination does not belong to the persisted delegation")

        existing = conn.execute(
            """SELECT session_id, event_json FROM async_terminal_outbox
               WHERE delivery_id=?""",
            (delivery_id,),
        ).fetchone()
        already_committed = existing is not None
        if existing is not None:
            if existing != (session_id, event_json):
                raise ValueError("terminal output conflicts with the durable outbox row")
        else:
            settled = conn.execute(
                """UPDATE async_delegations SET delivery_state='delivered',
                          delivered_at=?, updated_at=?, delivery_claim=NULL,
                          delivery_claimed_at=NULL
                   WHERE delegation_id=? AND delivery_state='pending'
                     AND delivery_claim=?""",
                (now, now, delegation_id, claim_id),
            )
            if settled.rowcount != 1:
                raise RuntimeError("async delegation claim is no longer owned")
            conn.execute(
                """INSERT INTO async_terminal_outbox
                   (delivery_id, delegation_id, session_id, event_json,
                    created_at, live_claim, live_claimed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (delivery_id, delegation_id, session_id, event_json, now, claim_id, now),
            )
    return {
        "delivery_id": delivery_id,
        "delegation_id": delegation_id,
        "session_id": session_id,
        "event": terminal_event,
        "already_committed": already_committed,
    }

def list_terminal_outputs(session_id: str) -> List[Dict[str, Any]]:
    """Return unacknowledged terminal outputs in stable publication order."""
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delivery_id, delegation_id, session_id, event_json
               FROM async_terminal_outbox
               WHERE session_id=? AND acknowledged_at IS NULL
               ORDER BY created_at, delivery_id""",
            (session_id,),
        ).fetchall()
    return [
        {
            "delivery_id": row[0],
            "delegation_id": row[1],
            "session_id": row[2],
            "event": json.loads(row[3]),
        }
        for row in rows
    ]

def claim_terminal_outputs(session_id: str, claim_id: str) -> List[Dict[str, Any]]:
    """Claim pending terminal rows for one attached transport before replay."""
    if not session_id or not claim_id:
        return []
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE async_terminal_outbox
                  SET delivery_claim=?, delivery_claimed_at=?,
                      live_claim=NULL, live_claimed_at=NULL
                WHERE session_id=? AND acknowledged_at IS NULL
                  AND (live_published_at IS NOT NULL
                       OR live_claim IS NULL
                       OR live_claimed_at < ?)
                  AND (delivery_claim IS NULL
                       OR delivery_claim=?
                       OR delivery_claimed_at < ?)""",
            (
                claim_id,
                now,
                session_id,
                now - _TERMINAL_OUTBOX_CLAIM_SECONDS,
                claim_id,
                now - _TERMINAL_OUTBOX_CLAIM_SECONDS,
            ),
        )
        rows = conn.execute(
            """SELECT delivery_id, delegation_id, session_id, event_json
                 FROM async_terminal_outbox
                WHERE session_id=? AND acknowledged_at IS NULL
                  AND delivery_claim=?
                ORDER BY created_at, delivery_id""",
            (session_id, claim_id),
        ).fetchall()
    return [
        {
            "delivery_id": row[0],
            "delegation_id": row[1],
            "session_id": row[2],
            "event": json.loads(row[3]),
        }
        for row in rows
    ]

def mark_terminal_output_live_published(delivery_id: str, claim_id: str) -> bool:
    """Mark live publication while retaining the publisher claim for ACK fencing."""
    if not delivery_id or not claim_id:
        return False
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_terminal_outbox
                  SET live_claimed_at=?,
                     live_published_at=?
                WHERE delivery_id=? AND acknowledged_at IS NULL
                  AND live_claim=?""",
            (now, now, delivery_id, claim_id),
        )
        if cur.rowcount == 1:
            return True
        row = conn.execute(
            """SELECT live_claim, live_published_at FROM async_terminal_outbox
               WHERE delivery_id=?""",
            (delivery_id,),
        ).fetchone()
    # A replay client may have taken over between the write and this mark.
    # That is already a valid durable handoff, so the original publisher must
    # not keep retrying or emit another frame.
    return bool(row and (row[0] != claim_id or row[1] is not None))

def _has_persisted_terminal_assistant(
    conn: sqlite3.Connection, session_id: str, delivery_id: str
) -> bool:
    """Require the immutable delivery identity in the durable transcript."""
    try:
        row = conn.execute(
            """SELECT 1 FROM messages
               WHERE session_id=? AND role='assistant' AND active=1
                 AND json_extract(display_metadata, '$.delivery_id') = ?
               ORDER BY id DESC LIMIT 1""",
            (session_id, delivery_id),
        ).fetchone()
    except sqlite3.Error:
        # ACK is a destructive transition. If the transcript store cannot be
        # read, leave the outbox replayable rather than hiding the answer.
        return False
    return row is not None

def ack_terminal_output(
    delivery_id: str, session_id: str, claim_id: str | None = None
) -> bool:
    """ACK only after the matching assistant identity is durable."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT session_id, acknowledged_at, delivery_claim,
                      live_claim, live_published_at
               FROM async_terminal_outbox
               WHERE delivery_id=?""",
            (delivery_id,),
        ).fetchone()
        if row is None or row[0] != session_id:
            return False
        if not _has_persisted_terminal_assistant(conn, session_id, delivery_id):
            return False
        if not claim_id or claim_id not in {row[2], row[3]}:
            return False
        if row[1] is not None:
            return True
        updated = conn.execute(
            """UPDATE async_terminal_outbox SET acknowledged_at=?
               WHERE delivery_id=? AND session_id=? AND acknowledged_at IS NULL
                 AND (delivery_claim=? OR live_claim=?)""",
            (now, delivery_id, session_id, claim_id, claim_id),
        )
        return updated.rowcount == 1

def event_delivery_claim_status(evt: Dict[str, Any], claim_id: str) -> str:
    """Return whether a durable event claim is ours, free, or terminal."""
    if not claim_id or evt.get("type") != "async_delegation":
        return "unclaimed"
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return "missing"
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT delivery_state, delivery_claim
               FROM async_delegations WHERE delegation_id=?""",
            (delegation_id,),
        ).fetchone()
    if row is None:
        return "missing"
    delivery_state, current_claim = row
    if delivery_state != "pending":
        return "delivered"
    if current_claim == claim_id:
        return "owned"
    if current_claim is None:
        return "unclaimed"
    return "other"

def renew_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Extend a live delivery claim without changing its attempt count."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claimed_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def renew_event_delivery(evt: Dict[str, Any], claim_id: str) -> bool:
    if claim_id and evt.get("type") == "async_delegation":
        return renew_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)
    return True


def replay_terminal_output(row: Dict[str, Any], runtime_session_id: str) -> Dict[str, Any]:
    """Return a transport-bound copy of an outbox event.

    The persisted session key identifies the durable conversation.  The runtime
    session id is process-local and can change after a reconnect, so replay must
    bind a copy rather than mutate the durable record.
    """
    event = json.loads(json.dumps(row["event"]))
    event["session_id"] = str(runtime_session_id)
    payload = event.setdefault("payload", {})
    payload["delivery_id"] = str(row["delivery_id"])
    return event

