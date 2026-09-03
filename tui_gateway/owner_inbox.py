"""Generic owner-bound inbox dispatch for TUI plugins."""
from __future__ import annotations

import threading
import uuid
from typing import Callable


class OwnerDispatch:
    def __init__(self, *, profile_name: str, lookup: Callable, submit: Callable,
                 sessions: Callable | None = None, pending: Callable | None = None):
        self.profile_name = profile_name
        self.lookup = lookup
        self.submit_path = submit
        self.sessions_path = sessions or (lambda: [])
        self.pending_path = pending or (lambda live_id: (False, None))

    def submit(self, profile: str, session_id: str, action_id: str,
               text: str, generation: int, admit=None):
        if profile != self.profile_name:
            return {"status": "waiting", "reason": "profile"}
        session = self.lookup(profile, session_id)
        if session is None:
            return {"status": "waiting", "reason": "session"}
        lock = session.get("history_lock")
        if lock is None:
            return {"status": "waiting", "reason": "session_lock"}
        with lock:
            if session.get("running"):
                return {"status": "busy", "reason": "active_turn"}
        live_id = session.get("_owner_live_id", session_id)
        response = self.submit_path({
                "session_id": live_id,
                "_owner_durable_session_id": session_id,
                "profile": profile,
                "text": text,
                "_owner_action_id": action_id,
                "work_generation": generation,
                "queued": False,
                "_owner_admit": admit,
                "_owner_request_id": str(uuid.uuid4()),
        }, session)
        if isinstance(response, dict):
            response.setdefault("status", "failed" if response.get("error")
                                else "submitted")
            return response
        return {"status": "submitted", "result": response}


    def snapshot(self, profile: str, session_id: str):
        if profile != self.profile_name:
            return None
        session = self.lookup(profile, session_id)
        if session is None:
            return None
        lock = session.get("history_lock")
        with lock:
            agent = session.get("agent") if isinstance(session, dict) else getattr(session, "agent", None)
            generation = (session.get("work_generation") if isinstance(session, dict)
                          else getattr(session, "work_generation", None))
            if generation is None:
                generation = getattr(agent, "work_generation", None)
            activity = (agent.get_activity_summary()
                        if callable(getattr(agent, "get_activity_summary", None))
                        else {})
            pending, payload = self.pending_path(session.get("_owner_live_id"))
            snapshot = {key: session.get(key) for key in (
                "session_key", "running", "last_active", "active_tools",
                "active_children", "unresolved_decision")} | {
                "pending_input": pending, "pending_payload": payload,
                "durable_session_id": session_id, "work_generation": generation}
            if activity:
                snapshot["last_active"] = activity.get("last_activity_at")
                snapshot["activity_provenance"] = activity.get("last_activity_provenance")
                snapshot["active_tools"] = activity.get("current_tool") is not None
            return snapshot

    def sessions(self):
        return self.sessions_path()


def live_owner_dispatch(server, prompt_submit):
    """Build a dispatcher bound to this TUI process's live session registry."""
    def lookup(profile, session_id):
        if profile != server._current_profile_name():
            return None
        with server._sessions_lock:
            for live_id, session in server._sessions.items():
                durable_ids = {str(session.get("session_key") or ""),
                               str(getattr(session.get("agent"), "session_id", "") or "")}
                if str(session_id) in durable_ids:
                    session["_owner_live_id"] = live_id
                    return session
        return None
    def pending(live_id):
        for rid, (owner, _event) in server._pending.items():
            if owner == live_id:
                item = server._pending_prompt_payloads.get(rid)
                return True, item[1] if item else None
        return False, None
    return OwnerDispatch(
        profile_name=server._current_profile_name(),
        lookup=lookup,
        submit=prompt_submit,
        sessions=lambda: list(server._sessions.items()),
        pending=pending,
    )


def start_registered_owner_inboxes(server, prompt_submit):
    """Discover plugins and start their providers inside this TUI owner."""
    from hermes_cli.plugins import discover_plugins, get_owner_inbox_providers
    discover_plugins()
    dispatch = live_owner_dispatch(server, prompt_submit)
    stops = []
    for provider in get_owner_inbox_providers():
        try:
            stop = provider(dispatch)
            if callable(stop):
                stops.append(stop)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "owner inbox provider startup failed")
    return stops
