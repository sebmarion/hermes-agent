"""Owner-bound, durable async completion lifecycle (no model/tool changes)."""
from __future__ import annotations
import threading
import time
import contextlib
from pathlib import Path
from typing import Any, Callable
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from .method_ctx import bind_module

_ASYNC_DELIVERY_HEARTBEAT_SECONDS = 30.0
_ASYNC_DELIVERY_CLEANUP_RETRY_SECONDS = 1.0
_ASYNC_DELIVERY_MAX_TRANSITION_RETRIES = 8
_RealThread = threading.Thread
def _start_async_delegation_heartbeat(
    evt: dict,
    claim_id: str,
    *,
    on_lost: Callable[[], None] | None = None,
    profile_home: str | Path | None = None,
) -> tuple[threading.Event, threading.Thread]:
    """Renew an async-delivery claim while its parent turn is running."""
    stop = threading.Event()

    def _claim_lost() -> None:
        if stop.is_set():
            return
        stop.set()
        if on_lost is not None:
            try:
                on_lost()
            except Exception:
                logger.warning(
                    "Async delegation %s claim-loss handler failed",
                    evt.get("delegation_id"),
                    exc_info=True,
                )

    def _loop() -> None:
        while not stop.wait(_ASYNC_DELIVERY_HEARTBEAT_SECONDS):
            try:
                from tools.async_delegation import renew_event_delivery

                token = set_hermes_home_override(profile_home) if profile_home else None
                try:
                    renewed = renew_event_delivery(evt, claim_id)
                finally:
                    if token is not None:
                        reset_hermes_home_override(token)
                if not renewed:
                    _claim_lost()
                    return
            except Exception:
                # Continuing after a failed renewal would allow the lease to
                # expire while this parent is still running, permitting a
                # second worker to execute the same delivery.
                logger.warning("Async delegation claim renewal failed", exc_info=True)
                _claim_lost()
                return

    thread = _RealThread(target=_loop, daemon=True)
    thread.start()
    return stop, thread

def _stop_async_delegation_heartbeat(
    stop: threading.Event | None, thread: threading.Thread | None
) -> None:
    if stop is None:
        return
    stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)

def _async_delegation_delivery_is_retryable(
    evt: dict, profile_home: str | Path | None = None
) -> bool:
    """Return whether a failed durable delivery still has a live retry state."""
    try:
        from tools.async_delegation import get_durable_delegation

        durable = _profile_scoped_async_db_call(
            profile_home,
            lambda: get_durable_delegation(str(evt.get("delegation_id") or "")),
        )
    except Exception:
        # A registry read failure must not strand an event already removed from
        # the in-memory queue; the next claimant can make the authoritative
        # decision once the registry is available again.
        return True
    # Missing/pruned rows have no durable recovery state and must not be
    # requeued after a failed legacy delivery.
    return durable is not None and durable.get("delivery_state") == "pending"

def _requeue_async_delegation(
    evt: dict, profile_home: str | Path | None = None
) -> None:
    if profile_home is None:
        retryable = _async_delegation_delivery_is_retryable(evt)
    else:
        retryable = _async_delegation_delivery_is_retryable(evt, profile_home)
    if retryable:
        from tools.process_registry import process_registry

        process_registry.completion_queue.put(evt)

def _profile_scoped_async_db_call(profile_home: str | Path | None, fn: Callable[[], Any]):
    token = set_hermes_home_override(profile_home) if profile_home else None
    try:
        return fn()
    finally:
        if token is not None:
            reset_hermes_home_override(token)

def _schedule_async_delivery_transition_retry(
    evt: dict,
    transition: Callable[[], bool],
    *,
    heartbeat: tuple[threading.Event, threading.Thread] | None,
    on_success: Callable[[], None] | None = None,
    on_exhausted: Callable[[], None] | None = None,
    check_retryable: bool = True,
    profile_home: str | Path | None = None,
) -> None:
    """Retry a durable transition, then fail closed without an immortal thread."""

    def _retry() -> None:
        for attempt in range(_ASYNC_DELIVERY_MAX_TRANSITION_RETRIES):
            def _is_retryable() -> bool:
                if profile_home is None:
                    return _async_delegation_delivery_is_retryable(evt)
                return _async_delegation_delivery_is_retryable(evt, profile_home)

            if check_retryable and not _profile_scoped_async_db_call(
                profile_home, _is_retryable
            ):
                if heartbeat is not None:
                    _stop_async_delegation_heartbeat(*heartbeat)
                return
            try:
                if transition():
                    if heartbeat is not None:
                        _stop_async_delegation_heartbeat(*heartbeat)
                    if on_success is not None:
                        on_success()
                    return
            except Exception:
                logger.warning(
                    "Async delegation %s durable transition retry failed",
                    evt.get("delegation_id"),
                    exc_info=True,
                )
            if attempt + 1 < _ASYNC_DELIVERY_MAX_TRANSITION_RETRIES:
                time.sleep(_ASYNC_DELIVERY_CLEANUP_RETRY_SECONDS)
        if heartbeat is not None:
            _stop_async_delegation_heartbeat(*heartbeat)
        if on_exhausted is not None:
            try:
                on_exhausted()
            except Exception:
                logger.warning(
                    "Async delegation %s exhausted durable transition retries",
                    evt.get("delegation_id"),
                    exc_info=True,
                )

    _RealThread(target=_retry, daemon=True).start()

def _ensure_terminal_assistant_identity(
    db: Any, session_id: str, text: str, delivery_id: str,
    *, delegation_id: str | None = None,
) -> bool:
    """Persist the terminal assistant row and its immutable delivery identity."""
    metadata = {"delivery_id": delivery_id}
    if delegation_id:
        metadata["delegation_id"] = delegation_id
    try:
        if db.set_latest_matching_message_display_metadata(
            session_id,
            role="assistant",
            content=text,
            display_metadata=metadata,
        ):
            return True
    except Exception as exc:
        raise RuntimeError("terminal delivery identity persistence failed") from exc

    rows = db.get_messages(session_id, latest=True, limit=1)
    latest = rows[0] if rows else None
    if latest and latest.get("role") == "assistant":
        if latest.get("content") == text:
            raise RuntimeError("terminal delivery identity persistence failed")
        raise RuntimeError("terminal assistant transcript would violate role alternation")

    try:
        db.append_message(
            session_id,
            "assistant",
            text,
            display_metadata=metadata,
        )
    except Exception as exc:
        raise RuntimeError("terminal assistant transcript persistence failed") from exc
    return True

def _async_delegation_terminal_callback(
    evt: dict,
    claim_id: str,
    *,
    heartbeat: tuple[threading.Event, threading.Thread] | None = None,
    profile_home: str | Path | None = None,
):
    """Acknowledge durable delivery only after the parent emits visible text."""
    settled = False
    settled_lock = threading.Lock()

    def _profile_db_call(fn):
        return _profile_scoped_async_db_call(profile_home, fn)

    def _requeue() -> None:
        if profile_home is None:
            _requeue_async_delegation(evt)
        else:
            _requeue_async_delegation(evt, profile_home)

    def _terminal(receipt: dict[str, Any]) -> dict[str, Any]:
        nonlocal settled
        terminal_event = receipt.get("terminal_event")
        if not isinstance(terminal_event, dict):
            raise RuntimeError("durable terminal event required")
        with settled_lock:
            if settled:
                return False
        from tools.async_delegation import commit_terminal_output

        try:
            committed = _profile_db_call(
                lambda: commit_terminal_output(evt, claim_id, terminal_event)
            )
            if isinstance(committed, dict):
                setattr(_terminal, "_durable_delivery_id", committed.get("delivery_id"))
                setattr(
                    _terminal,
                    "_durable_output_already_committed",
                    bool(committed.get("already_committed")),
                )
        except Exception:
            from tools.async_delegation import event_delivery_claim_status

            try:
                claim_status = _profile_db_call(
                    lambda: event_delivery_claim_status(evt, claim_id)
                )
            except Exception:
                claim_status = "owned"
            if claim_status != "owned":
                if claim_status == "unclaimed":
                    # The lease disappeared before durable publication. No
                    # other consumer owns it, so put the durable event back on
                    # the queue rather than leaving it stranded forever.
                    _requeue()
                if heartbeat is not None:
                    _stop_async_delegation_heartbeat(*heartbeat)
                with settled_lock:
                    settled = True
                return False

            def _retry_terminal_outbox_commit() -> bool:
                try:
                    committed_again = bool(
                        _profile_db_call(
                            lambda: commit_terminal_output(evt, claim_id, terminal_event)
                        )
                    )
                except Exception:
                    try:
                        retry_status = _profile_db_call(
                            lambda: event_delivery_claim_status(evt, claim_id)
                        )
                    except Exception:
                        retry_status = "owned"
                    if retry_status != "owned":
                        setattr(_terminal, "_durable_retry_lost", True)
                        return True
                    raise
                if committed_again:
                    setattr(_terminal, "_durable_retry_succeeded", True)
                return committed_again

            def _terminal_commit_exhausted() -> None:
                nonlocal settled
                setattr(_terminal, "_durable_retry_exhausted", True)
                with settled_lock:
                    settled = True
                try:
                    _requeue()
                finally:
                    getattr(_terminal, "_durable_commit_event", threading.Event()).set()

            _schedule_async_delivery_transition_retry(
                evt,
                _retry_terminal_outbox_commit,
                heartbeat=heartbeat,
                on_success=lambda: getattr(
                    _terminal, "_durable_commit_event", threading.Event()
                ).set(),
                on_exhausted=_terminal_commit_exhausted,
                profile_home=profile_home,
            )
            setattr(_terminal, "_durable_retry_started", True)
            raise
        with settled_lock:
            settled = True
        commit_event = getattr(_terminal, "_durable_commit_event", None)
        if commit_event is not None:
            commit_event.set()
        if heartbeat is not None:
            _stop_async_delegation_heartbeat(*heartbeat)
        return committed

    def _mark_live_published(delivery_id: str) -> bool:
        from tools.async_delegation import mark_terminal_output_live_published

        return _profile_db_call(
            lambda: mark_terminal_output_live_published(delivery_id, claim_id)
        )

    setattr(_terminal, "_durable_claim_id", claim_id)
    setattr(_terminal, "_mark_live_published", _mark_live_published)
    setattr(_terminal, "_delegation_id", evt.get("delegation_id"))
    setattr(_terminal, "_is_async_delegation_terminal_callback", True)
    return _terminal


def register(server):
    bind_module(globals(), server)

def _fail_closed_async_delivery_claim(session: dict) -> None:
    """Interrupt a parent whose durable delivery claim can no longer renew."""
    session["_async_delivery_claim_lost"] = True
    try:
        from agent.interrupt_compat import request_hard_interrupt

        request_hard_interrupt(session.get("agent"))
    except Exception:
        logger.warning("Unable to interrupt parent after async claim loss", exc_info=True)

def _normalize_terminal_response(result: Any, status: str) -> tuple[str, str]:
    """Normalize every terminal result into visible, durable assistant text."""
    raw = result.get("final_response", "") if isinstance(result, dict) else result
    malformed = raw is not None and not isinstance(raw, str)
    text = raw if isinstance(raw, str) else ""

    if status == "interrupted" and text.strip().startswith(INTERRUPT_WAITING_FOR_MODEL_PREFIX):
        return (
            "Error: The parent turn was interrupted before a final response was available. "
            "The durable delegated result remains available for retry.",
            "error",
        )
    if malformed:
        return (
            "Error: The terminal result payload was malformed; "
            "the durable delegated result remains available for retry.",
            "error",
        )
    if text.strip() not in {"", "(empty)"}:
        return text, status
    if status == "interrupted":
        return (
            "Error: The parent turn was interrupted before a final response was available. "
            "The durable delegated result remains available for retry.",
            "error",
        )
    if isinstance(result, dict) and result.get("error"):
        return f"Error: {result.get('error')}", "error"
    return (
        "Error: The agent ended without a final response. "
        "Any pending background result remains available for retry.",
        "error",
    )


def _settle_owner_terminal(session, st, payload):
    """Commit terminal output before publishing; transcript ACK stays separate."""
    callback = st.terminal_callback
    text, status = _normalize_terminal_response(
        {"final_response": payload.get("text"), "error": payload.get("error")},
        str(payload.get("status") or "error"))
    if payload.get("text") != text:
        payload.pop("rendered", None)
    payload.update(text=text, status=status)
    if session.get("_async_delivery_claim_lost"):
        raise RuntimeError("async delivery claim is no longer owned")
    stored = str(session.get("resume_session_id") or session.get("session_key") or st.runtime_session_id)
    event = {"type": "message.complete", "session_id": st.runtime_session_id,
             "stored_session_id": stored, "payload": dict(payload)}
    receipt = {"status": {"interrupted": "cancelled", "error": "failed"}.get(status, "settled"),
               "text": text, "terminal_event": event}
    commit_event = threading.Event()
    setattr(callback, "_durable_commit_event", commit_event)
    st.receipt_attempted = True
    try:
        result = callback(receipt)
    except Exception:
        if not getattr(callback, "_durable_retry_started", False):
            raise
        # Existing bounded retry owns the exact same output, never a new turn.
        commit_event.wait(_ASYNC_DELIVERY_MAX_TRANSITION_RETRIES * _ASYNC_DELIVERY_CLEANUP_RETRY_SECONDS + 2)
        if not getattr(callback, "_durable_retry_succeeded", False):
            raise
        result = {"delivery_id": "async-delegation:" + str(getattr(callback, "_delegation_id", ""))}
    if not isinstance(result, dict) or not isinstance(result.get("delivery_id"), str) or not result["delivery_id"]:
        raise RuntimeError("terminal output was not durably committed")
    delivery_id = result["delivery_id"]
    delegation_id = getattr(callback, "_delegation_id", None)
    if delegation_id and delivery_id != "async-delegation:" + str(delegation_id):
        raise RuntimeError("terminal delivery identity differs from its claimed delegation")
    st.receipt_committed = True
    setattr(callback, "_owner_terminal_committed", True)
    payload["delivery_id"] = delivery_id
    st.skip_terminal_publication = bool(result.get("already_committed") or getattr(callback, "_durable_output_already_committed", False))
    # An absent/unavailable transcript does not delete a committed answer: the
    # outbox stays replayable, and ACK still requires its persisted identity.
    db = getattr(st.agent, "_session_db", None)
    if db is not None:
        _ensure_terminal_assistant_identity(db, stored, text, delivery_id, delegation_id=delegation_id)
    transport = session.get("transport")
    if transport is not None:
        session.setdefault("_terminal_outbox_live_claims", {}).setdefault(id(transport), {})[delivery_id] = getattr(callback, "_durable_claim_id", None)


def _publish_owner_terminal(sid, session, st, payload):
    if st.skip_terminal_publication or st.terminal_published:
        return
    published = _emit("message.complete", sid, payload)
    st.terminal_published = published is not False
    if st.terminal_published and payload.get("delivery_id"):
        mark_live = getattr(st.terminal_callback, "_mark_live_published", None)
        if callable(mark_live):
            with contextlib.suppress(Exception):
                mark_live(payload["delivery_id"])
    # A failed cleanup cannot cause a second model turn or terminal frame.
    # The outer finally retries the same marker if this first retirement fails.
    if st.terminal_published and st.receipt_committed and not st.marker_retired:
        with contextlib.suppress(Exception):
            _retire_turn_marker(session, st.marker_key)
            st.marker_retired = True


def _settle_owner_terminal_error(sid, session, st, error, *, publish=True):
    if not getattr(st.terminal_callback, "_is_async_delegation_terminal_callback", False):
        return
    if st.receipt_committed or st.receipt_attempted:
        return
    payload = {"text": "Error: " + str(error), "status": "error", "error": str(error)}
    _settle_owner_terminal(session, st, payload)
    if publish:
        _publish_owner_terminal(sid, session, st, payload)


def _make_delivery_release_once(
    evt: dict, claim_id: str, profile_home: str | Path | None = None
):
    """Release a delivery claim at most once, including when release raises."""
    release_attempted = False

    def _release() -> bool:
        nonlocal release_attempted
        if release_attempted:
            return False
        release_attempted = True
        try:
            from tools.async_delegation import release_event_delivery

            result = _profile_scoped_async_db_call(
                profile_home, lambda: release_event_delivery(evt, claim_id)
            )
            return True if result is None else result
        except Exception:
            logger.warning(
                "Async delegation %s delivery release failed",
                evt.get("delegation_id"),
                exc_info=True,
            )
            return False

    return _release

def _require_profile_home(profile: str | None) -> Path | None:
    name = (profile or "").strip()
    home = _profile_home(name)
    if name and home is None:
        from hermes_cli.profiles import profile_matches_home

        if not profile_matches_home(name, Path(_hermes_home)):
            raise ValueError(f"Unknown Hermes profile: {name}")
    return home
