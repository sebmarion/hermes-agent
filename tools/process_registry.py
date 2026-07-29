"""
Process Registry -- In-memory registry for managed background processes.

Tracks processes spawned via terminal(background=true), providing:
  - Output buffering (rolling 200KB window)
  - Status polling and log retrieval
  - Blocking wait with interrupt support
  - Process killing
  - Crash recovery via JSON checkpoint file
  - Session-scoped tracking for gateway reset protection

Background processes execute THROUGH the environment interface -- nothing
runs on the host machine unless TERMINAL_ENV=local. For Docker, Singularity,
Modal, Daytona, and SSH backends, the command runs inside the sandbox.

Usage:
    from tools.process_registry import process_registry

    # Spawn a background process (called from terminal_tool)
    session = process_registry.spawn(env, "pytest -v", task_id="task_123")

    # Poll for status
    result = process_registry.poll(session.id)

    # Block until done
    result = process_registry.wait(session.id, timeout=300)

    # Kill it
    process_registry.kill(session.id)
"""

import hashlib
import json
import logging
import math
import os
import platform
import shlex
import signal
import stat
import subprocess
import threading
import time
import uuid
from contextlib import ExitStack
from functools import wraps
from enum import Enum
from pathlib import Path

_IS_WINDOWS = platform.system() == "Windows"
from tools.environments.local import _find_shell, _resolve_safe_cwd, _sanitize_subprocess_env
from hermes_cli._subprocess_compat import windows_hide_flags
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from hermes_cli.config import get_hermes_home
from tools.durable_state import (
    FileIdentity,
    atomic_write_private_json,
    hold_private_authority_directory,
    interprocess_authority_lock,
    platform_neutral_lifecycle_lock,
    read_private_json,
)

logger = logging.getLogger(__name__)


# Resolve only the trusted parent. Resolving the full leaf would follow a
# pre-existing authority-file symlink before the no-follow open can reject it.
_HERMES_HOME_PATH = Path(get_hermes_home()).resolve()
CHECKPOINT_PATH = _HERMES_HOME_PATH / "processes.json"
NOTIFICATIONS_PATH = _HERMES_HOME_PATH / "process_notifications.json"
COMPLETION_OUTBOX_VERSION = 1
MAX_COMPLETION_OUTBOX_RECORDS = 4096
COMPLETION_OUTBOX_DELIVERED_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_COMPLETION_OUTBOX_BYTES = 16 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024
MAX_MANAGED_RECOVERY_RECORDS = 4096

# Limits
MAX_OUTPUT_CHARS = 200_000      # 200KB rolling output buffer
FINISHED_TTL_SECONDS = 1800     # Keep finished processes for 30 minutes
MAX_PROCESSES = 64              # Max concurrent tracked processes (LRU pruning)
MAX_ACTIVE_PROCESS_AGE = 86400  # 24h default — see session_reset.bg_process_max_age_hours (#29177)

# Watch pattern rate limiting — PER SESSION.
# Hard rule: at most ONE watch-match notification every WATCH_MIN_INTERVAL_SECONDS.
# Any match arriving inside that cooldown window is dropped and counted as a strike.
# After WATCH_STRIKE_LIMIT consecutive strike windows, watch_patterns for that
# session is permanently disabled and the session falls back to notify_on_complete
# semantics (one notification when the process actually exits).
WATCH_MIN_INTERVAL_SECONDS = 15   # Minimum spacing between consecutive watch matches
WATCH_STRIKE_LIMIT = 3            # Strikes in a row → disable watch + promote to notify_on_complete

# Global circuit breaker — across all sessions. Secondary safety net so concurrent
# siblings can't collectively flood the user even when each is under its own cap.
WATCH_GLOBAL_MAX_PER_WINDOW = 15
WATCH_GLOBAL_WINDOW_SECONDS = 10
WATCH_GLOBAL_COOLDOWN_SECONDS = 30


class ManagedProcessRecoveryOutcome(str, Enum):
    PROVED_COMPLETE = "proved-complete"
    PROVED_ABSENT = "proved-absent"


class ManagedProcessRecoveryAmbiguous(RuntimeError):
    """Managed startup could not prove an exact recovery postcondition."""


@dataclass(frozen=True)
class _PrivateJsonReceipt:
    value: object
    identity: Optional[FileIdentity]
    sha256: Optional[str]


@dataclass(frozen=True)
class ManagedProcessRecoveryReceipt:
    outcome: ManagedProcessRecoveryOutcome
    checkpoint_path: str
    checkpoint_before_identity: Optional[FileIdentity]
    checkpoint_before_sha256: Optional[str]
    checkpoint_after_identity: Optional[FileIdentity]
    checkpoint_after_sha256: Optional[str]
    notifications_path: str
    notifications_identity: Optional[FileIdentity]
    notifications_sha256: Optional[str]
    process_pid: int
    process_start_token: str
    registry_epoch: str
    record_classifications: tuple[tuple[str, str], ...]
    recovered_process_ids: tuple[str, ...]
    deduped_process_ids: tuple[str, ...]
    completion_event_ids: tuple[str, ...]
    queued_completion_event_ids: tuple[str, ...]
    deduped_completion_event_ids: tuple[str, ...]
    post_snapshot_sha256: str


def _canonical_authority_path(path: Path) -> Path:
    path = Path(path)
    raw = os.fspath(path)
    if (
        not path.is_absolute()
        or raw != str(path)
        or Path(os.path.normpath(raw)) != path
        or path == Path("/")
    ):
        raise ManagedProcessRecoveryAmbiguous(
            "managed recovery authority path is not canonical"
        )
    return path


def _read_private_json_receipt(
    path: Path,
    *,
    max_bytes: int,
    missing_ok: bool,
) -> _PrivateJsonReceipt:
    """Bind parsed JSON to the exact private file bytes used for its hash."""
    path = _canonical_authority_path(path)
    try:
        held = _active_process_authority(path)
        if held is not None:
            try:
                value, identity, payload = held.read_json(
                    path,
                    max_bytes=max_bytes,
                    missing_ok=missing_ok,
                )
            except Exception as exc:
                raise ManagedProcessRecoveryAmbiguous(
                    "managed recovery authority is unreadable or malformed"
                ) from exc
            return _PrivateJsonReceipt(
                value,
                identity,
                hashlib.sha256(payload).hexdigest()
                if payload is not None
                else None,
            )
        value, identity = read_private_json(
            path,
            max_bytes=max_bytes,
            missing_ok=missing_ok,
        )
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise ManagedProcessRecoveryAmbiguous(
            "managed recovery authority is unreadable or malformed"
        ) from exc
    if identity is None:
        return _PrivateJsonReceipt(value, None, None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ManagedProcessRecoveryAmbiguous(
            "managed recovery requires O_NOFOLLOW"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = FileIdentity.from_stat(os.fstat(handle.fileno()))
            payload = handle.read(max_bytes + 1)
            after = FileIdentity.from_stat(os.fstat(handle.fileno()))
        current = FileIdentity.from_stat(os.lstat(path))
    except OSError as exc:
        raise ManagedProcessRecoveryAmbiguous(
            "managed recovery authority changed while hashing"
        ) from exc
    if (
        len(payload) > max_bytes
        or opened != identity
        or after != identity
        or current != identity
    ):
        raise ManagedProcessRecoveryAmbiguous(
            "managed recovery authority changed while hashing"
        )
    return _PrivateJsonReceipt(
        value,
        identity,
        hashlib.sha256(payload).hexdigest(),
    )


def _authority_receipt_is_current(
    path: Path,
    receipt: _PrivateJsonReceipt,
) -> bool:
    try:
        current = FileIdentity.from_stat(os.lstat(path))
    except FileNotFoundError:
        return receipt.identity is None
    except OSError:
        return False
    return receipt.identity == current


def _process_admission_anchor(
    checkpoint_path: Optional[Path] = None,
) -> Path:
    """Return the stable anchor whose sibling lock gates process creation."""
    authority = Path(
        CHECKPOINT_PATH if checkpoint_path is None else checkpoint_path
    )
    return authority.with_name(f"{authority.name}.admission")


_PROCESS_LIFECYCLE_FENCE = threading.RLock()
_PROCESS_LIFECYCLE_FENCE_PID = os.getpid()
_PROCESS_AUTHORITY_CONTEXT = threading.local()


def _active_process_authority(path: Path):
    held_by_parent = getattr(_PROCESS_AUTHORITY_CONTEXT, "held_by_parent", {})
    return held_by_parent.get(Path(path).parent)


def _process_authority_lock(path: Path):
    held = _active_process_authority(path)
    return held.lock(path) if held is not None else interprocess_authority_lock(path)


def _process_authority_read_json(path: Path, *, max_bytes: int, missing_ok=False):
    held = _active_process_authority(path)
    if held is None:
        return read_private_json(path, max_bytes=max_bytes, missing_ok=missing_ok)
    value, identity, _payload = held.read_json(
        path,
        max_bytes=max_bytes,
        missing_ok=missing_ok,
    )
    return value, identity


def _process_authority_atomic_write(path: Path, value: Any, **kwargs):
    held = _active_process_authority(path)
    if held is None:
        return atomic_write_private_json(path, value, **kwargs)
    return held.atomic_write_json(path, value, **kwargs)


def _process_lifecycle_fenced(method):
    """Serialize spawn, managed recovery, and finalization through one cut."""
    @wraps(method)
    def fenced(*args, **kwargs):
        global _PROCESS_LIFECYCLE_FENCE, _PROCESS_LIFECYCLE_FENCE_PID
        current_pid = os.getpid()
        if _PROCESS_LIFECYCLE_FENCE_PID != current_pid:
            # A lock inherited while held across fork can never be released in
            # the child. Establish a child-local lifecycle fence before use.
            _PROCESS_LIFECYCLE_FENCE = threading.RLock()
            _PROCESS_LIFECYCLE_FENCE_PID = current_pid
        with _PROCESS_LIFECYCLE_FENCE:
            if getattr(_PROCESS_AUTHORITY_CONTEXT, "lifecycle_depth", 0):
                return method(*args, **kwargs)
            _PROCESS_AUTHORITY_CONTEXT.lifecycle_depth = 1
            try:
                return _run_process_lifecycle_fenced(method, args, kwargs)
            finally:
                _PROCESS_AUTHORITY_CONTEXT.lifecycle_depth = 0

    return fenced


def _run_process_lifecycle_fenced(method, args, kwargs):
    """Run one outermost lifecycle cut under its platform authority."""
    if _IS_WINDOWS:
        # dir_fd-bound authorities are POSIX-only. Legacy Windows lifecycle
        # operations retain a platform file lock.
        with platform_neutral_lifecycle_lock(_process_admission_anchor()):
            return method(*args, **kwargs)
    with ExitStack() as stack:
        held_by_parent = {}
        for authority_path in (CHECKPOINT_PATH, NOTIFICATIONS_PATH):
            parent = Path(authority_path).parent
            if parent not in held_by_parent:
                held_by_parent[parent] = stack.enter_context(
                    hold_private_authority_directory(authority_path)
                )
        _PROCESS_AUTHORITY_CONTEXT.held_by_parent = held_by_parent
        try:
            admission = held_by_parent[Path(CHECKPOINT_PATH).parent]
            with admission.lock(_process_admission_anchor()):
                return method(*args, **kwargs)
        finally:
            _PROCESS_AUTHORITY_CONTEXT.held_by_parent = {}


def format_uptime_short(seconds: int) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    mins, secs = divmod(s, 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"


@dataclass
class ProcessSession:
    """A tracked background process with output buffering."""
    id: str                                     # Unique session ID ("proc_xxxxxxxxxxxx")
    command: str                                 # Original command string
    task_id: str = ""                           # Task/sandbox isolation key
    session_key: str = ""                       # Gateway session key (for reset protection)
    pid: Optional[int] = None                   # OS process ID
    process: Optional[subprocess.Popen] = None  # Popen handle (local only)
    env_ref: Any = None                         # Reference to the environment object
    cwd: Optional[str] = None                   # Working directory
    started_at: float = 0.0                     # time.time() of spawn (wall clock)
    host_start_time: Optional[int] = None       # kernel start ticks (/proc/<pid>/stat f22) — PID-reuse guard
    process_start_token: Optional[str] = None   # canonical exact OS PID/start token
    exited: bool = False                        # Whether the process has finished
    exit_code: Optional[int] = None             # Exit code (None if still running)
    completion_reason: str = "exited"           # exited|killed|lost|failed_start|already_exited
    termination_source: str = ""                # process.kill|kill_all|backend_lost|failed_start
    output_buffer: str = ""                     # Rolling output (last MAX_OUTPUT_CHARS)
    max_output_chars: int = MAX_OUTPUT_CHARS
    detached: bool = False                      # True if recovered from crash (no pipe)
    pid_scope: str = "host"                     # "host" for local/PTY PIDs, "sandbox" for env-local PIDs
    # Watcher/notification metadata (persisted for crash recovery)
    watcher_platform: str = ""
    watcher_chat_id: str = ""
    watcher_user_id: str = ""
    watcher_user_name: str = ""
    watcher_thread_id: str = ""
    watcher_message_id: str = ""                # Triggering message id — reply anchor for topic routing
    watcher_interval: int = 0                   # 0 = no watcher configured
    notify_on_complete: bool = False             # Queue agent notification on exit
    # Watch patterns — trigger agent notification when output matches any pattern
    watch_patterns: List[str] = field(default_factory=list)
    _watch_hits: int = field(default=0, repr=False)          # total matches delivered
    _watch_suppressed: int = field(default=0, repr=False)    # matches dropped by rate limit
    _watch_disabled: bool = field(default=False, repr=False) # permanently killed after strike limit
    # Per-session rate limit state: at most one match every WATCH_MIN_INTERVAL_SECONDS.
    # When an emission happens, _watch_cooldown_until is set to now + interval and
    # _watch_strike_candidate becomes True. The next match to arrive before that
    # deadline counts as one strike (regardless of how many matches were dropped in
    # between — a strike is a window, not a match). After WATCH_STRIKE_LIMIT strikes
    # in a row, watch_patterns is disabled and the session promotes to
    # notify_on_complete.
    _watch_last_emit_at: float = field(default=0.0, repr=False)
    _watch_cooldown_until: float = field(default=0.0, repr=False)
    _watch_strike_candidate: bool = field(default=False, repr=False)
    _watch_consecutive_strikes: int = field(default=0, repr=False)
    _completion_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _reader_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _pty: Any = field(default=None, repr=False)  # ptyprocess handle (when use_pty=True)


class ProcessRegistry:
    """
    In-memory registry of running and finished background processes.

    Thread-safe. Accessed from:
      - Executor threads (terminal_tool, process tool handlers)
      - Gateway asyncio loop (watcher tasks, session reset checks)
      - Cleanup thread (sandbox reaping coordination)
    """

    _SHELL_NOISE_SUBSTRINGS = (
        "bash: cannot set terminal process group",
        "bash: no job control in this shell",
        "no job control in this shell",
        "cannot set terminal process group",
        "tcsetattr: Inappropriate ioctl for device",
    )

    def __init__(self):
        self._running: Dict[str, ProcessSession] = {}
        self._finished: Dict[str, ProcessSession] = {}
        self._finalizing: Dict[str, ProcessSession] = {}
        self._lock = threading.Lock()

        # Completion notifications use a separate durable outbox. A process
        # remains release-visible in ``_running`` + ``_finalizing`` until its
        # completion record is fsynced and queued. Consumers ACK the stable
        # event id only after they durably own the resulting continuation.
        self._completion_outbox_lock = threading.Lock()
        self._completion_outbox: Dict[str, Dict[str, Any]] = {}
        self._completion_outbox_loaded = False
        self._completion_outbox_available = True
        self._completion_outbox_replayed: set[str] = set()

        # ``processes.json`` is the crash-recovery authority for background
        # work. Until startup has securely reconciled it (or a spawn has
        # durably created it), an in-memory zero is not proof of quiescence.
        # A malformed/unsafe recovery source blocks later writes so evidence
        # is never silently replaced with ``[]``.
        self._checkpoint_io_lock = threading.RLock()
        self._process_checkpoint_available = False
        self._process_checkpoint_reason = "unverified"
        self._checkpoint_write_blocked = False
        self._checkpoint_owner_id = ""
        self._checkpoint_owner_pid = 0
        self._checkpoint_owner_start_token = ""
        self._foreign_owner_active = 0

        # Side-channel for check_interval watchers (gateway reads after agent run)
        self.pending_watchers: List[Dict[str, Any]] = []

        # Notification queue — unified queue for all background process events.
        # Completion notifications (notify_on_complete) and watch pattern matches
        # both land here, distinguished by "type" field.  CLI process_loop and
        # gateway drain this after each agent turn to auto-trigger new turns.
        import queue as _queue_mod
        self.completion_queue: _queue_mod.Queue = _queue_mod.Queue()

        # Track sessions whose completion was already consumed by the agent
        # via wait/log.  Drain loops AND gateway/tui watchers skip notifications
        # for these — a blocking wait() or a full read_log() means the agent
        # has the output in hand and is acting on it this turn.
        self._completion_consumed: set = set()

        # Track sessions the agent merely *observed* exited via poll().  poll()
        # is a read-only status check, so it does NOT mark _completion_consumed
        # (that would let a status check suppress the gateway/tui watcher's
        # autonomous delivery turn — #10156).  But on the CLI the poll result
        # is returned inline in the same turn, so the idle/post-turn drain must
        # still skip the queued completion to avoid a duplicate [SYSTEM: ...]
        # injection (the bug #8228 originally fixed).  drain_notifications()
        # consults this set; the gateway/tui watchers deliberately do NOT.
        self._poll_observed: set = set()

        # Global watch-match circuit breaker — across all sessions.
        # Prevents sibling processes from collectively flooding the user even
        # when each stays under its own per-session cap.
        self._global_watch_lock = threading.Lock()
        self._global_watch_window_start: float = 0.0
        self._global_watch_window_hits: int = 0
        self._global_watch_tripped_until: float = 0.0
        self._global_watch_suppressed_during_trip: int = 0
        # Live-output sink set by a driver (e.g. the desktop gateway): called from
        # reader threads with (session, chunk) to stream output to a UI in
        # real time, instead of polling the output tail.
        self.on_output = None
        # Close-view sink set by a driver (desktop gateway): called with
        # (session_or_none, process_id) when the agent asks to close a read-only
        # terminal tab. Distinct from kill — the process keeps running; only the
        # UI view is dropped (the user can reopen it from the status stack).
        self.on_close = None

    @staticmethod
    def _completion_event_id(
        session_id: str,
        process_start_token: Optional[str] = None,
    ) -> str:
        if process_start_token:
            generation = hashlib.sha256(
                process_start_token.encode("utf-8")
            ).hexdigest()[:24]
            return f"process:{session_id}:{generation}:completion"
        return f"process:{session_id}:completion"

    @staticmethod
    def _validate_completion_record(event_id: str, raw: object) -> Dict[str, Any]:
        if not isinstance(raw, dict) or raw.get("event_id") != event_id:
            raise ValueError("process completion record identity is invalid")
        if raw.get("type") != "completion":
            raise ValueError("process completion record type is invalid")
        session_id = raw.get("session_id")
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id.encode("utf-8")) > 512
            or len(event_id.encode("utf-8")) > 1024
        ):
            raise ValueError("process completion identity is unbounded")
        process_start_token = raw.get("process_start_token")
        if process_start_token is not None and (
            not isinstance(process_start_token, str) or not process_start_token
        ):
            raise ValueError("process completion generation is invalid")
        expected_event_id = ProcessRegistry._completion_event_id(
            session_id,
            process_start_token,
        )
        if event_id != expected_event_id:
            raise ValueError("process completion session identity is invalid")
        if not isinstance(raw.get("delivered"), bool):
            raise ValueError("process completion delivery state is invalid")
        created_at = raw.get("created_at")
        if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
            raise ValueError("process completion timestamp is invalid")
        record = dict(raw)
        record["session_id"] = session_id
        record["event_id"] = event_id
        return record

    def _read_completion_outbox_snapshot_locked(
        self,
    ) -> tuple[Dict[str, Dict[str, Any]], Optional[FileIdentity]]:
        try:
            raw, identity = _process_authority_read_json(
                NOTIFICATIONS_PATH,
                max_bytes=MAX_COMPLETION_OUTBOX_BYTES,
            )
        except FileNotFoundError:
            return {}, None
        if (
            not isinstance(raw, dict)
            or raw.get("version") != COMPLETION_OUTBOX_VERSION
            or not isinstance(raw.get("events"), dict)
        ):
            raise ValueError("process completion outbox schema is invalid")
        events = {
            event_id: self._validate_completion_record(event_id, record)
            for event_id, record in raw["events"].items()
            if isinstance(event_id, str)
        }
        if len(events) != len(raw["events"]):
            raise ValueError("process completion outbox event id is invalid")
        return events, identity

    def _ensure_completion_outbox_loaded_locked(self) -> None:
        try:
            with _process_authority_lock(NOTIFICATIONS_PATH):
                events, _identity = self._read_completion_outbox_snapshot_locked()
        except Exception:
            self._completion_outbox_available = False
            raise
        self._completion_outbox = events
        self._completion_outbox_loaded = True
        self._completion_outbox_available = True

    def _prune_completion_outbox_locked(self) -> None:
        now = time.time()
        expired = [
            event_id
            for event_id, record in self._completion_outbox.items()
            if record.get("delivered") is True
            and now - float(record.get("delivered_at") or record["created_at"])
            > COMPLETION_OUTBOX_DELIVERED_TTL_SECONDS
        ]
        for event_id in expired:
            self._completion_outbox.pop(event_id, None)
            self._completion_outbox_replayed.discard(event_id)
        if len(self._completion_outbox) <= MAX_COMPLETION_OUTBOX_RECORDS:
            return
        delivered = sorted(
            (
                (float(record.get("delivered_at") or record["created_at"]), event_id)
                for event_id, record in self._completion_outbox.items()
                if record.get("delivered") is True
            )
        )
        for _timestamp, event_id in delivered:
            if len(self._completion_outbox) <= MAX_COMPLETION_OUTBOX_RECORDS:
                break
            self._completion_outbox.pop(event_id, None)
            self._completion_outbox_replayed.discard(event_id)
        if len(self._completion_outbox) > MAX_COMPLETION_OUTBOX_RECORDS:
            raise RuntimeError("process completion outbox capacity is exhausted")

    def _write_completion_outbox_locked(self) -> None:
        proposed = {
            event_id: self._validate_completion_record(event_id, record)
            for event_id, record in self._completion_outbox.items()
        }
        try:
            with _process_authority_lock(NOTIFICATIONS_PATH):
                current, expected_identity = (
                    self._read_completion_outbox_snapshot_locked()
                )
                merged = dict(current)
                for event_id, candidate in proposed.items():
                    durable = merged.get(event_id)
                    if durable is None:
                        merged[event_id] = candidate
                        continue
                    durable_payload = {
                        key: value
                        for key, value in durable.items()
                        if key not in {"delivered", "delivered_at"}
                    }
                    candidate_payload = {
                        key: value
                        for key, value in candidate.items()
                        if key not in {"delivered", "delivered_at"}
                    }
                    if durable_payload != candidate_payload:
                        raise ValueError(
                            "process completion event identity collision"
                        )
                    if (
                        candidate.get("delivered") is True
                        and durable.get("delivered") is not True
                    ):
                        merged[event_id] = candidate

                self._completion_outbox = merged
                self._prune_completion_outbox_locked()
                _process_authority_atomic_write(
                    NOTIFICATIONS_PATH,
                    {
                        "version": COMPLETION_OUTBOX_VERSION,
                        "events": self._completion_outbox,
                    },
                    expected=expected_identity,
                    max_bytes=MAX_COMPLETION_OUTBOX_BYTES,
                )
        except Exception:
            self._completion_outbox_available = False
            raise
        self._completion_outbox_loaded = True
        self._completion_outbox_available = True

    @staticmethod
    def _public_completion_event(record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key not in {"delivered", "delivered_at"}
        }

    def _build_completion_record(self, session: ProcessSession) -> Dict[str, Any]:
        from tools.ansi_strip import strip_ansi

        with session._lock:
            output_tail = (
                strip_ansi(session.output_buffer[-2000:])
                if session.output_buffer
                else ""
            )
            return {
                "event_id": self._completion_event_id(
                    session.id,
                    session.process_start_token,
                ),
                "type": "completion",
                "session_id": session.id,
                "process_pid": session.pid,
                "process_start_token": session.process_start_token,
                "session_key": session.session_key,
                "platform": session.watcher_platform,
                "chat_id": session.watcher_chat_id,
                "user_id": session.watcher_user_id,
                "user_name": session.watcher_user_name,
                "thread_id": session.watcher_thread_id,
                "message_id": session.watcher_message_id,
                "command": session.command,
                "exit_code": session.exit_code,
                "completion_reason": session.completion_reason,
                "termination_source": session.termination_source,
                "output": output_tail,
                "created_at": time.time(),
                "delivered": False,
                "delivered_at": None,
            }

    def _build_recovered_terminal_record(
        self,
        entry: Dict[str, Any],
        classification: str,
    ) -> Dict[str, Any]:
        """Build a retry-stable event before removing dead checkpoint evidence."""
        session_id = entry["session_id"]
        process_token = entry["process_start_token"]
        reason = (
            "already_exited"
            if classification == "process_absent"
            else "lost"
        )
        return {
            "event_id": self._completion_event_id(session_id, process_token),
            "type": "completion",
            "session_id": session_id,
            "process_pid": entry["pid"],
            "process_start_token": process_token,
            "session_key": entry.get("session_key", ""),
            "platform": entry.get("watcher_platform", ""),
            "chat_id": entry.get("watcher_chat_id", ""),
            "user_id": entry.get("watcher_user_id", ""),
            "user_name": entry.get("watcher_user_name", ""),
            "thread_id": entry.get("watcher_thread_id", ""),
            "message_id": entry.get("watcher_message_id", ""),
            "command": entry.get("command", "unknown"),
            "exit_code": None,
            "completion_reason": reason,
            "termination_source": "startup_recovery",
            "output": "",
            "created_at": float(entry.get("started_at") or 0.0),
            "delivered": False,
            "delivered_at": None,
        }

    @staticmethod
    def _clean_shell_noise(text: str) -> str:
        """Strip shell startup warnings from the beginning of output."""
        lines = text.split("\n")
        while lines and any(noise in lines[0] for noise in ProcessRegistry._SHELL_NOISE_SUBSTRINGS):
            lines.pop(0)
        return "\n".join(lines)

    def _emit_output(self, session: ProcessSession, chunk: str) -> None:
        """Forward a freshly-read chunk to the live-output sink, if one is set.
        Called from reader threads; never raise into the read loop."""
        sink = self.on_output
        if sink is None or not chunk:
            return
        try:
            sink(session, chunk)
        except Exception:
            pass

    def _check_watch_patterns(self, session: ProcessSession, new_text: str) -> None:
        """Scan new output for watch patterns and queue notifications.

        Called from reader threads with new_text being the freshly-read chunk.

        Per-session rate limit: at most ONE watch-match notification per
        WATCH_MIN_INTERVAL_SECONDS. Any match arriving inside the cooldown
        window is dropped and counts as ONE strike for that window. After
        WATCH_STRIKE_LIMIT consecutive strike windows, watch_patterns is
        disabled for this session and the session is promoted to
        notify_on_complete semantics — one notification when the process
        actually exits, no more mid-process spam.
        """
        if not session.watch_patterns or session._watch_disabled:
            return
        # Suppress-after-exit: once the reader loop has declared the process
        # exited, any late chunk we still see is post-exit noise. Dropping these
        # prevents the "stale notifications delivered minutes after the process
        # ended" spam when completion_queue consumers run async.
        if session.exited:
            return

        # Scan new text line-by-line for pattern matches
        matched_lines = []
        matched_pattern = None
        for line in new_text.splitlines():
            for pat in session.watch_patterns:
                if pat in line:
                    matched_lines.append(line.rstrip())
                    if matched_pattern is None:
                        matched_pattern = pat
                    break  # one match per line is enough

        if not matched_lines:
            return

        now = time.time()
        should_disable = False
        with session._lock:
            # Case 1: still inside the cooldown from the last emission.
            # Count this as a strike for the current window (only once per window)
            # and drop the event. If we've hit the strike limit, disable watch
            # and promote to notify_on_complete.
            if session._watch_cooldown_until and now < session._watch_cooldown_until:
                session._watch_suppressed += len(matched_lines)
                if not session._watch_strike_candidate:
                    # First drop in this window — count one strike.
                    session._watch_strike_candidate = True
                    session._watch_consecutive_strikes += 1
                    if session._watch_consecutive_strikes >= WATCH_STRIKE_LIMIT:
                        session._watch_disabled = True
                        # Promote to notify_on_complete so the agent still gets
                        # exactly one notification when the process actually ends.
                        session.notify_on_complete = True
                        should_disable = True
                return_early = True
            else:
                # Case 2: cooldown has expired.
                # Decide whether this window was a "clean" one (no drops) or a
                # strike window. If no strike candidate was set during the prior
                # cooldown, reset the consecutive-strike counter — we're back to
                # healthy emission cadence.
                if (
                    session._watch_cooldown_until
                    and not session._watch_strike_candidate
                ):
                    session._watch_consecutive_strikes = 0
                session._watch_strike_candidate = False

                # Emit the notification and start a new cooldown window.
                session._watch_last_emit_at = now
                session._watch_cooldown_until = now + WATCH_MIN_INTERVAL_SECONDS
                session._watch_hits += 1
                suppressed = session._watch_suppressed
                session._watch_suppressed = 0
                return_early = False

        if return_early:
            if should_disable:
                # Emit exactly one "watch disabled, falling back to notify_on_complete"
                # summary event so the agent/user sees why things went quiet.
                self.completion_queue.put({
                    "session_id": session.id,
                    "session_key": session.session_key,
                    "command": session.command,
                    "type": "watch_disabled",
                    "suppressed": session._watch_suppressed,
                    "platform": session.watcher_platform,
                    "chat_id": session.watcher_chat_id,
                    "user_id": session.watcher_user_id,
                    "user_name": session.watcher_user_name,
                    "thread_id": session.watcher_thread_id,
                    "message_id": session.watcher_message_id,
                    "message": (
                        f"Watch patterns disabled for process {session.id} — "
                        f"{WATCH_STRIKE_LIMIT} consecutive rate-limit windows triggered "
                        f"(min spacing {WATCH_MIN_INTERVAL_SECONDS}s). "
                        f"Falling back to notify_on_complete semantics; you'll get "
                        f"exactly one notification when the process exits."
                    ),
                })
            return

        # Trim matched output to a reasonable size
        output = "\n".join(matched_lines[:20])
        if len(output) > 2000:
            output = output[:2000] + "\n...(truncated)"

        # Global circuit breaker — across all sessions (secondary safety net).
        if not self._global_watch_admit(now):
            return

        self.completion_queue.put({
            "session_id": session.id,
            "session_key": session.session_key,
            "command": session.command,
            "type": "watch_match",
            "pattern": matched_pattern,
            "output": output,
            "suppressed": suppressed,
            "platform": session.watcher_platform,
            "chat_id": session.watcher_chat_id,
            "user_id": session.watcher_user_id,
            "user_name": session.watcher_user_name,
            "thread_id": session.watcher_thread_id,
            "message_id": session.watcher_message_id,
        })

    def _global_watch_admit(self, now: float) -> bool:
        """Return True if this watch_match event is allowed through the global breaker.

        Semantics:
        - If we're currently in a cooldown period, drop the event and count it.
        - Otherwise, slide the rolling window and check the global cap.
        - If the cap is exceeded, trip the breaker for WATCH_GLOBAL_COOLDOWN_SECONDS
          and emit ONE summary event so the agent/user sees "N notifications were
          suppressed" instead of getting them individually.
        - When the cooldown ends, emit a release summary and reset counters.
        """
        with self._global_watch_lock:
            # Handle cooldown expiry first so we can emit the release summary.
            if self._global_watch_tripped_until and now >= self._global_watch_tripped_until:
                suppressed = self._global_watch_suppressed_during_trip
                self._global_watch_tripped_until = 0.0
                self._global_watch_suppressed_during_trip = 0
                self._global_watch_window_start = now
                self._global_watch_window_hits = 0
                if suppressed > 0:
                    # Queue a summary event outside the lock (below).
                    release_msg = {
                        "session_id": "",
                        "session_key": "",
                        "command": "",
                        "type": "watch_overflow_released",
                        "suppressed": suppressed,
                        "message": (
                            f"Watch-pattern notifications resumed. "
                            f"{suppressed} match event(s) were suppressed during the flood."
                        ),
                        "platform": "",
                        "chat_id": "",
                        "user_id": "",
                        "user_name": "",
                        "thread_id": "",
                    }
                else:
                    release_msg = None
            else:
                release_msg = None

            # Still in cooldown — drop and count.
            if self._global_watch_tripped_until and now < self._global_watch_tripped_until:
                self._global_watch_suppressed_during_trip += 1
                admit = False
                trip_now = None
            else:
                # Slide the window.
                if now - self._global_watch_window_start >= WATCH_GLOBAL_WINDOW_SECONDS:
                    self._global_watch_window_start = now
                    self._global_watch_window_hits = 0

                if self._global_watch_window_hits >= WATCH_GLOBAL_MAX_PER_WINDOW:
                    # Trip the breaker.
                    self._global_watch_tripped_until = now + WATCH_GLOBAL_COOLDOWN_SECONDS
                    self._global_watch_suppressed_during_trip += 1
                    trip_now = now
                    admit = False
                else:
                    self._global_watch_window_hits += 1
                    trip_now = None
                    admit = True

        # Queue summary events outside the lock.
        if release_msg is not None:
            self.completion_queue.put(release_msg)
        if trip_now is not None:
            self.completion_queue.put({
                "session_id": "",
                "session_key": "",
                "command": "",
                "type": "watch_overflow_tripped",
                "message": (
                    f"Watch-pattern overflow: >{WATCH_GLOBAL_MAX_PER_WINDOW} "
                    f"notifications in {WATCH_GLOBAL_WINDOW_SECONDS}s across all processes. "
                    f"Suppressing further watch_match events for "
                    f"{WATCH_GLOBAL_COOLDOWN_SECONDS}s."
                ),
                "platform": "",
                "chat_id": "",
                "user_id": "",
                "user_name": "",
                "thread_id": "",
            })
        return admit

    @staticmethod
    def _is_host_pid_alive(pid: Optional[int]) -> bool:
        """Best-effort liveness check for host-visible PIDs."""
        if not pid:
            return False
        # ``os.kill(pid, 0)`` is NOT a no-op on Windows (bpo-14484) — use
        # the cross-platform existence check.
        from gateway.status import _pid_exists
        return _pid_exists(pid)

    @staticmethod
    def _safe_host_start_time(pid: Optional[int]) -> Optional[int]:
        """Kernel start ticks for a host PID, or None when unavailable."""
        if not pid:
            return None
        try:
            from gateway.status import get_process_start_time
            return get_process_start_time(pid)
        except Exception:
            return None

    @staticmethod
    def _safe_host_start_token(pid: Optional[int]) -> Optional[str]:
        """Canonical exact OS process identity token, or None when unavailable."""
        if not pid:
            return None
        try:
            from gateway.status import get_process_start_token

            token = get_process_start_token(pid)
            return token if isinstance(token, str) and token else None
        except Exception:
            return None

    @classmethod
    def _host_pid_matches_exact_token(
        cls,
        pid: Optional[int],
        expected_token: object,
    ) -> bool:
        """Match a live process only with its canonical PID-bound start token."""
        return (
            isinstance(expected_token, str)
            and bool(expected_token)
            and cls._is_host_pid_alive(pid)
            and cls._safe_host_start_token(pid) == expected_token
        )

    def _ensure_checkpoint_owner_identity(self) -> tuple[str, int, str]:
        """Return a fork-safe runtime owner identity for checkpoint merges."""
        pid = os.getpid()
        token = self._safe_host_start_token(pid)
        if token is None:
            raise RuntimeError("current runtime process identity is unavailable")
        if (
            self._checkpoint_owner_pid != pid
            or self._checkpoint_owner_start_token != token
            or not self._checkpoint_owner_id
        ):
            self._checkpoint_owner_pid = pid
            self._checkpoint_owner_start_token = token
            self._checkpoint_owner_id = f"runtime_{uuid.uuid4().hex}"
        return (
            self._checkpoint_owner_id,
            self._checkpoint_owner_pid,
            self._checkpoint_owner_start_token,
        )

    @classmethod
    def _host_pid_is_ours(cls, pid: Optional[int], expected_start: Optional[int]) -> bool:
        """True only if ``pid`` is alive AND still the process we spawned.

        The kernel recycles PID/PGID numbers once a process exits and is reaped,
        so a stored PID can later name an *unrelated* process — observed in the
        wild as a recycled number landing on a desktop browser's session leader,
        which our tree-kill then SIGTERMs (Firefox dying at irregular intervals).
        We compare the kernel start time captured at spawn against the live one;
        a mismatch means the number was recycled and must never be signalled.

        When no baseline was captured (legacy checkpoints, or platforms without
        ``/proc``) we degrade to a bare liveness check rather than refusing to
        act, preserving prior best-effort behaviour.
        """
        if not cls._is_host_pid_alive(pid):
            return False
        if expected_start is None:
            return True
        return cls._safe_host_start_time(pid) == expected_start

    def _refresh_detached_session(self, session: Optional[ProcessSession]) -> Optional[ProcessSession]:
        """Update recovered host-PID sessions when the underlying process has exited."""
        if session is None or session.exited or not session.detached or session.pid_scope != "host":
            return session

        # Identity-aware liveness: a recycled PID (alive but a different process
        # than we spawned) must be treated as "our process exited", so it is
        # moved to finished and can never be tree-killed by a later kill().
        if (
            isinstance(session.process_start_token, str)
            and session.process_start_token
            and self._host_pid_matches_exact_token(
                session.pid,
                session.process_start_token,
            )
        ):
            return session

        with session._lock:
            if session.exited:
                return session
            session.exited = True
            # Recovered sessions no longer have a waitable handle, so the real
            # exit code is unavailable once the original process object is gone.
            session.exit_code = None

        self._move_to_finished(session)
        return session

    @staticmethod
    def _proc_alive(proc) -> bool:
        """True if a psutil.Process is running and not a zombie.

        A zombie is already dead (just unreaped), so there's nothing to SIGKILL.
        """
        try:
            import psutil
            if not proc.is_running():
                return False
            return proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    @staticmethod
    def _daemon_term_grace_seconds() -> float:
        """Grace window (s) between SIGTERM and escalated SIGKILL.

        Read from ``terminal.daemon_term_grace_seconds`` in config.yaml; floored
        at 0 (0 disables escalation). Falls back to the DEFAULT_CONFIG value if
        config is unreadable, so callers always get a sane number.
        """
        try:
            from hermes_cli.config import read_raw_config, cfg_get, DEFAULT_CONFIG
            cfg = read_raw_config()
            val = cfg_get(cfg, "terminal", "daemon_term_grace_seconds")
            if val is None:
                val = DEFAULT_CONFIG["terminal"]["daemon_term_grace_seconds"]
            return max(float(val), 0.0)
        except Exception:
            return 2.0

    @classmethod
    def _terminate_host_pid(cls, pid: int, expected_start: Optional[int] = None) -> None:
        """Terminate a host-visible PID and its descendants.

        ``expected_start`` is the kernel start time captured when we spawned the
        process. When provided, it is re-validated against the live PID before
        any signal is sent; a mismatch (or a dead PID) means the number was
        recycled onto an unrelated process and we refuse to touch it, so a stale
        background-session PID can never tree-kill a browser or other stranger.

        POSIX: walks the process tree with ``psutil`` and SIGTERMs
        children before the parent so subprocess trees (e.g. Chromium
        renderers/GPU helpers spawned by an ``agent-browser`` daemon)
        don't get reparented to init and survive cleanup.  After a bounded
        grace window (``terminal.daemon_term_grace_seconds``) any tree member
        that ignored SIGTERM — a daemon stalled in its signal handler — is
        escalated to SIGKILL so it can't leak indefinitely.  Set the grace to
        0 to disable escalation (SIGTERM only).

        Windows: shells out to ``taskkill /PID <pid> /T /F``. This is
        the documented Microsoft primitive for tree-kill and matches the
        existing convention in ``gateway.status.terminate_pid``.  ``/F`` is
        already a hard kill, so no separate escalation step is needed.  We
        can't reuse the POSIX psutil path on Windows because:

          1. Windows doesn't maintain a Unix-style process tree —
             ``psutil.Process.children(recursive=True)`` walks PPID
             links that go stale when intermediate processes exit, so
             enumeration is best-effort and misses orphaned descendants.
          2. ``psutil.Process.terminate()`` on Windows is
             ``TerminateProcess()`` which kills only the target handle
             and is a hard kill — there is no Windows equivalent of a
             SIGTERM that cascades through a process group. (See the
             warning in ``gateway/status.py::terminate_pid``: "os.kill
             with SIGTERM is not equivalent to a tree-killing hard stop"
             on Windows.) Headless Chromium has no GUI window, so the
             softer ``taskkill /T`` without ``/F`` won't reach it either.

        ``psutil`` is a hard dependency (see ``pyproject.toml``); the
        bare-``os.kill`` fallback covers OSError / PermissionError on
        POSIX and a missing ``taskkill.exe`` on Windows (effectively
        unreachable on real Windows installs, but cheap insurance).
        """
        if expected_start is not None and not cls._host_pid_is_ours(pid, expected_start):
            # PID was recycled (start time changed) or is gone — never signal a
            # stranger. A leaked orphan is strictly preferable to killing e.g.
            # a browser whose session leader reused this dead session's PID.
            logger.warning(
                "Refusing to terminate host pid %d: start-time mismatch — "
                "PID was recycled onto an unrelated process.", pid,
            )
            return
        if _IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=windows_hide_flags(),
                    stdin=subprocess.DEVNULL,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError, PermissionError):
                    pass
            return

        import psutil
        owned_process_group = None
        if expected_start is not None:
            try:
                candidate_group = os.getpgid(pid)
                if candidate_group == pid:
                    owned_process_group = candidate_group
            except (OSError, ProcessLookupError, PermissionError):
                pass
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        except (OSError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError, PermissionError):
                pass
            return

        # Snapshot the whole tree (children before parent) and SIGTERM each.
        try:
            targets = parent.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            targets = []
        targets.append(parent)

        if owned_process_group is not None:
            try:
                os.killpg(owned_process_group, signal.SIGTERM)
            except (OSError, ProcessLookupError, PermissionError):
                owned_process_group = None
        if owned_process_group is None:
            for proc in targets:
                try:
                    proc.terminate()
                except psutil.NoSuchProcess:
                    pass
                except (psutil.AccessDenied, OSError):
                    pass

        # Escalate to SIGKILL for anything that ignored SIGTERM within the
        # grace window — a daemon stalled in its signal handler would otherwise
        # leak indefinitely.
        grace = cls._daemon_term_grace_seconds()
        if grace <= 0:
            return
        # Sleep out the grace window, then independently re-probe every target
        # and SIGKILL any survivor.  We deliberately do NOT trust
        # ``psutil.wait_procs``'s gone/alive partition here: it reaps via
        # ``Process.wait()`` and can mis-partition when a target transitions
        # through a zombie state or when reaping is racy across a parent/child
        # tree, which left survivors un-killed.  A direct liveness re-probe is
        # deterministic.
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not any(cls._proc_alive(_p) for _p in targets):
                break
            time.sleep(0.05)
        group_still_owned = False
        if owned_process_group is not None:
            for proc in targets:
                try:
                    if (
                        cls._proc_alive(proc)
                        and os.getpgid(proc.pid) == owned_process_group
                    ):
                        group_still_owned = True
                        break
                except (OSError, ProcessLookupError, PermissionError):
                    continue
        if group_still_owned:
            try:
                os.killpg(owned_process_group, signal.SIGKILL)
                return
            except (OSError, ProcessLookupError, PermissionError):
                pass
        for proc in targets:
            try:
                if not cls._proc_alive(proc):
                    continue
                proc.kill()  # SIGKILL on POSIX
                logger.info(
                    "Escalated to SIGKILL for pid %d (ignored SIGTERM within "
                    "%.1fs grace)", proc.pid, grace,
                )
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, OSError):
                pass

    # ----- Spawn -----

    @staticmethod
    def _env_temp_dir(env: Any) -> str:
        """Return the writable sandbox temp dir for env-backed background tasks."""
        get_temp_dir = getattr(env, "get_temp_dir", None)
        if callable(get_temp_dir):
            try:
                temp_dir = get_temp_dir()
                if isinstance(temp_dir, str) and temp_dir.startswith("/"):
                    return temp_dir.rstrip("/") or "/"
            except Exception as exc:
                logger.debug("Could not resolve environment temp dir: %s", exc)
        return "/tmp"

    @staticmethod
    def _terminate_env_process_group(env: Any, pid: Optional[int]) -> bool:
        """Terminate and verify the dedicated sandbox process group."""
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            return False
        command = (
            f"kill -TERM -- -{pid} 2>/dev/null || true; "
            "attempt=0; "
            f"while kill -0 -- -{pid} 2>/dev/null && [ $attempt -lt 20 ]; do "
            "sleep 0.05; attempt=$((attempt + 1)); done; "
            f"if kill -0 -- -{pid} 2>/dev/null; then "
            f"kill -KILL -- -{pid} 2>/dev/null || true; sleep 0.05; fi; "
            f"! kill -0 -- -{pid} 2>/dev/null"
        )
        try:
            result = env.execute(
                command,
                timeout=5,
                rewrite_compound_background=False,
            )
        except Exception:
            logger.error(
                "Sandbox process-group termination probe failed for pid %d",
                pid,
                exc_info=True,
            )
            return False
        verified = result.get("returncode", 0) == 0
        if not verified:
            logger.error(
                "Sandbox process group %d survived termination attempt", pid
            )
        return verified

    @_process_lifecycle_fenced
    def spawn_local(
        self,
        command: str,
        cwd: str = None,
        task_id: str = "",
        session_key: str = "",
        env_vars: dict = None,
        use_pty: bool = False,
    ) -> ProcessSession:
        """Admit and durably register one local process as one transaction."""
        return self._spawn_local_admitted(
            command,
            cwd=cwd,
            task_id=task_id,
            session_key=session_key,
            env_vars=env_vars,
            use_pty=use_pty,
        )

    def _spawn_local_admitted(
        self,
        command: str,
        cwd: str = None,
        task_id: str = "",
        session_key: str = "",
        env_vars: dict = None,
        use_pty: bool = False,
    ) -> ProcessSession:
        """
        Spawn a background process locally.

        Only for TERMINAL_ENV=local. Other backends use spawn_via_env().

        Args:
            use_pty: If True, use a pseudo-terminal via ptyprocess for interactive
                     CLI tools (Codex, Claude Code, Python REPL). Falls back to
                     subprocess.Popen if ptyprocess is not installed.
        """
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            task_id=task_id,
            session_key=session_key,
            cwd=_resolve_safe_cwd(cwd or os.getcwd()),
            started_at=time.time(),
        )

        if use_pty:
            # Try PTY mode for interactive CLI tools
            pty_proc = None
            checkpoint_committed = False
            try:
                if _IS_WINDOWS:
                    from winpty import PtyProcess as _PtyProcessCls
                else:
                    from ptyprocess import PtyProcess as _PtyProcessCls
                user_shell = _find_shell()
                pty_env = _sanitize_subprocess_env(os.environ, env_vars)
                pty_env["PYTHONUNBUFFERED"] = "1"
                pty_proc = _PtyProcessCls.spawn(
                    [user_shell, "-lic", f"set +m; {command}"],
                    cwd=session.cwd,
                    env=pty_env,
                    dimensions=(30, 120),
                )
                session.pid = pty_proc.pid
                session.host_start_time = self._safe_host_start_time(session.pid)
                # Store the pty handle on the session for read/write
                session._pty = pty_proc

                with self._lock:
                    self._prune_if_needed()
                    self._running[session.id] = session

                checkpoint_receipt = self._write_checkpoint()
                if checkpoint_receipt is not True:
                    raise RuntimeError(
                        "Background process checkpoint is not durable"
                    )
                checkpoint_committed = checkpoint_receipt is True

                # Start consuming output only after the process is durably
                # discoverable by the next gateway instance.
                reader = threading.Thread(
                    target=self._pty_reader_loop,
                    args=(session,),
                    daemon=True,
                    name=f"proc-pty-reader-{session.id}",
                )
                session._reader_thread = reader
                reader.start()
                return session

            except ImportError:
                logger.warning("ptyprocess not installed, falling back to pipe mode")
            except Exception as e:
                if pty_proc is None:
                    logger.warning("PTY spawn failed (%s), falling back to pipe mode", e)
                else:
                    # Once a PTY exists, falling through to Popen would launch
                    # the command twice. Terminate this exact PTY and surface
                    # the setup failure instead.
                    try:
                        pty_proc.terminate(force=True)
                    except Exception:
                        if session.pid:
                            self._terminate_host_pid(
                                session.pid, session.host_start_time
                            )
                    with self._lock:
                        self._running.pop(session.id, None)
                    if checkpoint_committed:
                        self._write_checkpoint()
                    raise

        # Standard Popen path (non-PTY or PTY fallback)
        # Use the user's login shell for consistency with LocalEnvironment --
        # ensures rc files are sourced and user tools are available.
        user_shell = _find_shell()
        # Force unbuffered output for Python scripts so progress is visible
        # during background execution (libraries like tqdm/datasets buffer when
        # stdout is a pipe, hiding output from process(action="poll")).
        bg_env = _sanitize_subprocess_env(os.environ, env_vars)
        bg_env["PYTHONUNBUFFERED"] = "1"
        _popen_kwargs = {"creationflags": windows_hide_flags()} if _IS_WINDOWS else {}

        proc = subprocess.Popen(
            [user_shell, "-lic", f"set +m; {command}"],
            text=True,
            cwd=session.cwd,
            env=bg_env,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            **_popen_kwargs,
        )

        session.process = proc
        session.pid = proc.pid
        session.host_start_time = self._safe_host_start_time(session.pid)

        checkpoint_committed = False
        try:
            with self._lock:
                self._prune_if_needed()
                self._running[session.id] = session

            checkpoint_receipt = self._write_checkpoint()
            if checkpoint_receipt is not True:
                raise RuntimeError("Background process checkpoint is not durable")
            checkpoint_committed = checkpoint_receipt is True

            # Start the reader only after the durable registry commit.
            reader = threading.Thread(
                target=self._reader_loop,
                args=(session,),
                daemon=True,
                name=f"proc-reader-{session.id}",
            )
            session._reader_thread = reader
            reader.start()
        except Exception:
            # Post-Popen setup failed — kill the orphaned subprocess (and any
            # descendants spawned via setsid) before re-raising so they do not
            # leak as untracked background processes.
            try:
                if not _IS_WINDOWS:
                    try:
                        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
                        os.killpg(os.getpgid(proc.pid), kill_signal)  # windows-footgun: ok - guarded by _IS_WINDOWS above
                    except (ProcessLookupError, PermissionError, OSError):
                        proc.kill()
                else:
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            with self._lock:
                self._running.pop(session.id, None)
            if checkpoint_committed:
                self._write_checkpoint()
            raise

        return session

    @_process_lifecycle_fenced
    def spawn_via_env(
        self,
        env: Any,
        command: str,
        cwd: str = None,
        task_id: str = "",
        session_key: str = "",
        timeout: int = 10,
    ) -> ProcessSession:
        """Admit and durably register one sandbox process as one transaction."""
        return self._spawn_via_env_admitted(
            env,
            command,
            cwd=cwd,
            task_id=task_id,
            session_key=session_key,
            timeout=timeout,
        )

    def _spawn_via_env_admitted(
        self,
        env: Any,
        command: str,
        cwd: str = None,
        task_id: str = "",
        session_key: str = "",
        timeout: int = 10,
    ) -> ProcessSession:
        """
        Spawn a background process through a non-local environment backend.

        For Docker/Singularity/Modal/Daytona/SSH: runs the command inside the sandbox
        using the environment's execute() interface. We wrap the command to
        capture the in-sandbox PID and redirect output to a log file inside
        the sandbox, then poll the log via subsequent execute() calls.

        This is less capable than local spawn (no live stdout pipe, no stdin),
        but it ensures the command runs in the correct sandbox context.
        """
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            task_id=task_id,
            session_key=session_key,
            cwd=cwd,
            started_at=time.time(),
            env_ref=env,
            pid_scope="sandbox",
        )

        # Run the command in the sandbox with output capture
        temp_dir = self._env_temp_dir(env)
        log_path = f"{temp_dir}/hermes_bg_{session.id}.log"
        pid_path = f"{temp_dir}/hermes_bg_{session.id}.pid"
        exit_path = f"{temp_dir}/hermes_bg_{session.id}.exit"
        quoted_temp_dir = shlex.quote(temp_dir)
        quoted_log_path = shlex.quote(log_path)
        quoted_pid_path = shlex.quote(pid_path)
        quoted_exit_path = shlex.quote(exit_path)
        inner_command = (
            f"{command}; rc=$?; "
            f"printf '%s\\n' \"$rc\" > {quoted_exit_path}; exit \"$rc\""
        )
        quoted_inner_command = shlex.quote(inner_command)
        bg_command = (
            "set +m; "
            f"mkdir -p {quoted_temp_dir} || exit $?; "
            "if ! command -v setsid >/dev/null 2>&1; then "
            "printf '%s\\n' 'setsid is required for managed background work' >&2; "
            "exit 126; fi; "
            f"nohup setsid bash -lc {quoted_inner_command} "
            f"> {quoted_log_path} 2>&1 < /dev/null & "
            f"bg_pid=$!; printf '%s\\n' \"$bg_pid\" > {quoted_pid_path}; "
            f"cat {quoted_pid_path}"
        )

        try:
            result = env.execute(
                bg_command,
                timeout=timeout,
                rewrite_compound_background=False,
            )
            output = result.get("output", "").strip()
            # Try to extract the PID from the output
            for line in output.splitlines():
                line = line.strip()
                if line.isdigit():
                    session.pid = int(line)
                    break
            # If the wrapper couldn't produce a PID (for example, syntax
            # error or broken redirect), treat it as a failed launch instead
            # of exposing a fake running session.
            if session.pid is None:
                session.exited = True
                session.exit_code = int(result.get("returncode", -1))
                if session.exit_code == 0:
                    session.exit_code = -1
                session.completion_reason = "failed_start"
                session.termination_source = "failed_start"
                session.output_buffer = result.get("output", "").strip()
        except Exception as e:
            session.exited = True
            session.exit_code = -1
            session.completion_reason = "failed_start"
            session.termination_source = "failed_start"
            session.output_buffer = f"Failed to start: {e}"

        with self._lock:
            self._prune_if_needed()
            if not session.exited:
                self._running[session.id] = session

        if not session.exited:
            checkpoint_committed = False
            try:
                checkpoint_receipt = self._write_checkpoint()
                if checkpoint_receipt is not True:
                    raise RuntimeError(
                        "Background process checkpoint is not durable"
                    )
                checkpoint_committed = checkpoint_receipt is True

                # Start the poller only after the durable registry commit.
                reader = threading.Thread(
                    target=self._env_poller_loop,
                    args=(session, env, log_path, pid_path, exit_path),
                    daemon=True,
                    name=f"proc-poller-{session.id}",
                )
                session._reader_thread = reader
                reader.start()
            except Exception as setup_error:
                cleanup_verified = self._terminate_env_process_group(
                    env, session.pid
                )
                with self._lock:
                    self._running.pop(session.id, None)
                if checkpoint_committed:
                    self._write_checkpoint()
                if not cleanup_verified:
                    raise RuntimeError(
                        "Background process setup failed and sandbox process-group "
                        "termination could not be verified"
                    ) from setup_error
                raise

        return session

    # ----- Reader / Poller Threads -----

    def _reader_loop(self, session: ProcessSession):
        """Background thread: read stdout from a local Popen process.

        IMPORTANT: avoid ``TextIOWrapper.read(4096)`` here. On pipes that call can
        block until EOF (or a large buffer fills), which makes "live" output land
        in one burst at process exit. ``buffer.read1(4096)`` yields incremental
        chunks as bytes become available, then we decode to text.
        """
        first_chunk = True
        try:
            stdout = session.process.stdout
            if stdout is None:
                return

            raw_read = getattr(getattr(stdout, "buffer", None), "read1", None)
            while True:
                if raw_read is not None:
                    raw = raw_read(4096)
                    if not raw:
                        break
                    chunk = raw.decode("utf-8", errors="replace")
                else:
                    # Fallback for mocked/alternate streams without a buffered raw
                    # interface. This may be less "live", but keeps compatibility.
                    chunk = stdout.read(4096)
                    if not chunk:
                        break

                if first_chunk:
                    chunk = self._clean_shell_noise(chunk)
                    first_chunk = False
                with session._lock:
                    session.output_buffer += chunk
                    if len(session.output_buffer) > session.max_output_chars:
                        session.output_buffer = session.output_buffer[-session.max_output_chars:]
                self._check_watch_patterns(session, chunk)
                self._emit_output(session, chunk)
        except Exception as e:
            logger.debug("Process stdout reader ended: %s", e)
        finally:
            # Always reap the child to prevent zombie processes.
            try:
                session.process.wait(timeout=5)
            except Exception as e:
                logger.debug("Process wait timed out or failed: %s", e)
            session.exited = True
            if session.completion_reason != "killed":
                session.exit_code = session.process.returncode
                session.completion_reason = "exited"
            self._move_to_finished(session)

    def _env_poller_loop(
        self, session: ProcessSession, env: Any, log_path: str, pid_path: str, exit_path: str
    ):
        """Background thread: poll a sandbox log file for non-local backends."""
        quoted_log_path = shlex.quote(log_path)
        quoted_pid_path = shlex.quote(pid_path)
        quoted_exit_path = shlex.quote(exit_path)
        prev_output_len = 0  # track delta for watch pattern scanning
        while not session.exited:
            time.sleep(2)  # Poll every 2 seconds
            try:
                # Read new output from the log file
                result = env.execute(f"cat {quoted_log_path} 2>/dev/null", timeout=10)
                new_output = result.get("output", "")
                if new_output:
                    # Compute delta for watch pattern scanning
                    delta = new_output[prev_output_len:] if len(new_output) > prev_output_len else ""
                    prev_output_len = len(new_output)
                    with session._lock:
                        session.output_buffer = new_output
                        if len(session.output_buffer) > session.max_output_chars:
                            session.output_buffer = session.output_buffer[-session.max_output_chars:]
                    if delta:
                        self._check_watch_patterns(session, delta)
                        self._emit_output(session, delta)

                # Check if process is still running
                check = env.execute(
                    f"kill -0 -- -\"$(cat {quoted_pid_path} 2>/dev/null)\" 2>/dev/null; echo $?",
                    timeout=5,
                )
                check_output = check.get("output", "").strip()
                if check_output and check_output.splitlines()[-1].strip() != "0":
                    # Process has exited -- get exit code captured by the wrapper shell.
                    exit_result = env.execute(
                        f"cat {quoted_exit_path} 2>/dev/null",
                        timeout=5,
                    )
                    exit_str = exit_result.get("output", "").strip()
                    try:
                        session.exit_code = int(exit_str.splitlines()[-1].strip())
                    except (ValueError, IndexError):
                        session.exit_code = -1
                    session.exited = True
                    if session.completion_reason != "killed":
                        session.completion_reason = "exited"
                    self._move_to_finished(session)
                    return

            except Exception:
                # Environment might be gone (sandbox reaped, etc.)
                session.exited = True
                session.exit_code = -1
                session.completion_reason = "lost"
                session.termination_source = "backend_lost"
                self._move_to_finished(session)
                return

    def _pty_reader_loop(self, session: ProcessSession):
        """Background thread: read output from a PTY process."""
        pty = session._pty
        try:
            while pty.isalive():
                try:
                    chunk = pty.read(4096)
                    if chunk:
                        # ptyprocess returns bytes
                        text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
                        with session._lock:
                            session.output_buffer += text
                            if len(session.output_buffer) > session.max_output_chars:
                                session.output_buffer = session.output_buffer[-session.max_output_chars:]
                        self._check_watch_patterns(session, text)
                        self._emit_output(session, text)
                except EOFError:
                    break
                except Exception:
                    break
        except Exception as e:
            logger.debug("PTY stdout reader ended: %s", e)

        # Process exited
        try:
            pty.wait()
        except Exception as e:
            logger.debug("PTY wait timed out or failed: %s", e)
        session.exited = True
        if session.completion_reason != "killed":
            session.exit_code = pty.exitstatus if hasattr(pty, 'exitstatus') else -1
            session.completion_reason = "exited"
        self._move_to_finished(session)

    @_process_lifecycle_fenced
    def _move_to_finished(self, session: ProcessSession) -> bool:
        """Move a session from running to finished.

        Idempotent: if the session was already moved (e.g. kill_process raced
        with the reader thread), the second call is a no-op — no duplicate
        completion notification is enqueued. A notifying process remains in
        the release barrier until its stable event is durably persisted and
        published; persistence failure leaves it in ``_running`` for retry.
        """
        with self._lock:
            if session.id not in self._running or session.id in self._finalizing:
                return False
            self._finalizing[session.id] = session

        event = None
        try:
            if session.notify_on_complete:
                proposed = self._build_completion_record(session)
                event_id = proposed["event_id"]
                with self._completion_outbox_lock:
                    self._ensure_completion_outbox_loaded_locked()
                    previous = self._completion_outbox.get(event_id)
                    if previous is None:
                        self._completion_outbox[event_id] = proposed
                        try:
                            self._write_completion_outbox_locked()
                        except Exception:
                            self._completion_outbox.pop(event_id, None)
                            self._completion_outbox_available = False
                            raise
                        record = proposed
                    else:
                        durable_payload = {
                            key: value
                            for key, value in previous.items()
                            if key not in {
                                "created_at",
                                "delivered",
                                "delivered_at",
                            }
                        }
                        proposed_payload = {
                            key: value
                            for key, value in proposed.items()
                            if key not in {
                                "created_at",
                                "delivered",
                                "delivered_at",
                            }
                        }
                        if durable_payload != proposed_payload:
                            raise ValueError(
                                "process completion event identity collision"
                            )
                        record = previous
                    if record.get("delivered") is not True:
                        event = self._public_completion_event(record)

            checkpointed = self._write_checkpoint()
            if checkpointed is False:
                raise OSError("process checkpoint persistence failed")
            if event is not None:
                event_id = str(event.get("event_id") or "")
                with self._completion_outbox_lock:
                    # Recovery may be called more than once as additional TUI
                    # sessions attach. Treat a live publish as this process's
                    # one replay claim so startup recovery cannot enqueue the
                    # same durable event a second time.
                    if event_id not in self._completion_outbox_replayed:
                        self.completion_queue.put(event)
                        self._completion_outbox_replayed.add(event_id)

            with self._lock:
                current = self._running.get(session.id)
                if current is not session:
                    raise RuntimeError("process registry identity changed while finalizing")
                self._running.pop(session.id, None)
                self._finished[session.id] = session
                self._finalizing.pop(session.id, None)
            session._completion_event.set()
            return True
        except Exception:
            with self._lock:
                self._finalizing.pop(session.id, None)
            logger.error(
                "Failed to durably finalize background process %s; keeping it release-visible",
                session.id,
                exc_info=True,
            )
            return False

    # ----- Query Methods -----

    def is_completion_consumed(self, session_id: str) -> bool:
        """Check if a completion notification was already consumed via wait/log."""
        with self._lock:
            return session_id in self._completion_consumed

    @_process_lifecycle_fenced
    def mark_completion_consumed(self, event_or_session_id: object) -> bool:
        """Durably ACK one completion after a consumer owns its continuation."""
        if isinstance(event_or_session_id, dict):
            session_id = str(event_or_session_id.get("session_id") or "")
            event_id = str(event_or_session_id.get("event_id") or "")
            expected_event_id = self._completion_event_id(
                session_id,
                event_or_session_id.get("process_start_token"),
            )
            if not session_id or event_id != expected_event_id:
                return False
        else:
            session_id = str(event_or_session_id or "")
            event_id = ""
        if not session_id:
            return False

        with self._completion_outbox_lock:
            try:
                self._ensure_completion_outbox_loaded_locked()
                if not event_id:
                    matches = [
                        candidate_id
                        for candidate_id, candidate in self._completion_outbox.items()
                        if candidate.get("session_id") == session_id
                        and candidate.get("delivered") is not True
                    ]
                    if len(matches) > 1:
                        return False
                    event_id = (
                        matches[0]
                        if matches
                        else self._completion_event_id(session_id)
                    )
                record = self._completion_outbox.get(event_id)
                if isinstance(event_or_session_id, dict) and record is None:
                    return False
                if record is not None and record.get("delivered") is not True:
                    public_record = self._public_completion_event(record)
                    if isinstance(event_or_session_id, dict) and (
                        public_record != event_or_session_id
                    ):
                        return False
                    updated = dict(record)
                    updated["delivered"] = True
                    updated["delivered_at"] = time.time()
                    self._completion_outbox[event_id] = updated
                    try:
                        self._write_completion_outbox_locked()
                    except Exception:
                        self._completion_outbox[event_id] = record
                        self._completion_outbox_available = False
                        raise
            except Exception:
                logger.error(
                    "Failed to persist process completion ACK for %s",
                    session_id,
                    exc_info=True,
                )
                return False
        with self._lock:
            self._completion_consumed.add(session_id)
        return True

    def finish_notification_delivery(self, event: dict, committed: bool) -> bool:
        """Finalize an automatic notification after its agent turn commits.

        Durable process completions are ACKed only after the owner conversation
        accepted the synthetic turn. A failed turn or failed ACK is re-queued,
        leaving the durable outbox record available for restart replay.
        """
        event_type = event.get("type")
        if event_type == "completion" and event.get("event_id"):
            if committed and self.mark_completion_consumed(event):
                return True
            self.completion_queue.put(event)
            return False
        if event_type == "async_delegation":
            if committed:
                try:
                    if event.get("managed_delivery") is not None:
                        from tools.async_delegation import (
                            ManagedAsyncDelegationRecoveryOutcome,
                            mark_managed_async_delegation_delivered_exact,
                        )

                        receipt = mark_managed_async_delegation_delivered_exact(event)
                        if (
                            receipt.outcome
                            is ManagedAsyncDelegationRecoveryOutcome.COMPLETE
                        ):
                            return True
                    else:
                        from tools.async_delegation import (
                            mark_async_delegation_delivered,
                        )

                        if mark_async_delegation_delivered(event) is True:
                            return True
                    logger.warning(
                        "Async delegation delivery ACK was not persisted; requeueing"
                    )
                except Exception:
                    logger.debug(
                        "Failed to ACK async delegation delivery",
                        exc_info=True,
                    )
            self.completion_queue.put(event)
            return False
        return bool(committed)

    @_process_lifecycle_fenced
    def recover_completion_notifications(self) -> int:
        """Replay each durable undelivered completion once in this process."""
        with self._completion_outbox_lock:
            try:
                self._ensure_completion_outbox_loaded_locked()
            except Exception:
                logger.error(
                    "Failed to recover durable process completion notifications",
                    exc_info=True,
                )
                return 0
            records = [
                (event_id, self._public_completion_event(record))
                for event_id, record in sorted(self._completion_outbox.items())
                if record.get("delivered") is not True
                and event_id not in self._completion_outbox_replayed
            ]
            for event_id, _event in records:
                self._completion_outbox_replayed.add(event_id)
        replayed = 0
        for event_id, event in records:
            try:
                self.completion_queue.put(event)
                replayed += 1
            except Exception:
                with self._completion_outbox_lock:
                    self._completion_outbox_replayed.discard(event_id)
                logger.error(
                    "Failed to replay process completion notification %s",
                    event_id,
                    exc_info=True,
                )
        return replayed

    def completion_activity_snapshot(self) -> Dict[str, Any]:
        """Return fail-closed process and durable completion barrier state."""
        with self._lock:
            foreign_owner_active = self._foreign_owner_active
            running = len(self._running) + foreign_owner_active
            finalizing = len(self._finalizing)
        with self._completion_outbox_lock:
            try:
                self._ensure_completion_outbox_loaded_locked()
                undelivered = sum(
                    record.get("delivered") is not True
                    for record in self._completion_outbox.values()
                )
                available = self._completion_outbox_available
            except Exception:
                undelivered = 0
                available = False
        with self._checkpoint_io_lock:
            checkpoint_available = self._process_checkpoint_available
            checkpoint_reason = self._process_checkpoint_reason
        return {
            "running_processes": running,
            "foreign_owner_active_processes": foreign_owner_active,
            "finalizing_processes": finalizing,
            "durable_undelivered_completions": int(undelivered),
            "process_completion_activity_available": bool(available),
            "process_checkpoint_available": bool(checkpoint_available),
            "process_checkpoint_reason": checkpoint_reason,
        }

    def is_session_waiting(self, session_id: str) -> bool:
        """Whether a goal loop parked on this session should still be parked.

        Used by the goal-loop wait barrier (``hermes_cli.goals``) to support
        waiting on a process's OWN trigger, not just its exit. A session is
        "still waiting" when:
          - it is still running, AND
          - if it has ``watch_patterns``, none has matched yet (so a
            long-lived watcher that fires a trigger mid-run — and may never
            exit — unblocks the moment its pattern hits, not on exit).

        Returns False (don't wait) when the session has exited, its watch
        pattern has already fired, or the session is unknown — so a stale or
        already-triggered barrier can never wedge the loop.
        """
        if not session_id:
            return False
        with self._lock:
            session = self._running.get(session_id) or self._finished.get(session_id)
        if session is None:
            return False
        # Refresh detached/remote state so .exited is current.
        try:
            self._refresh_detached_session(session)
        except Exception:
            pass
        if session.exited:
            return False
        # Watch-pattern process: the trigger is a pattern match, not exit.
        # Once any match has been delivered, the wait is satisfied even though
        # the process keeps running (server/daemon/watcher case).
        if session.watch_patterns and not session._watch_disabled:
            if session._watch_hits > 0:
                return False
        return True

    def _drain_should_skip(self, session_id: str) -> bool:
        """Whether the CLI drain should skip a completion event for this session.

        Skips when the agent has either truly consumed the output (wait/log →
        ``_completion_consumed``) or observed the exit inline via poll()
        (``_poll_observed``).  In both cases the CLI agent already has the
        result this turn, so injecting a [SYSTEM: ...] completion would be a
        duplicate (#8228).  The gateway/tui watchers do NOT use this — they
        check only ``is_completion_consumed`` so a read-only poll never
        suppresses their autonomous delivery turn (#10156).
        """
        return session_id in self._completion_consumed or session_id in self._poll_observed

    def drain_notifications(
        self,
        session_key: str = "",
        owns_event=None,
        *,
        ack_async: bool = True,
    ) -> "list[tuple[dict, str]]":
        """Pop all pending notification events and return formatted pairs.

        Returns a list of (raw_event, formatted_text) tuples.
        Skips completion events the agent already consumed via wait/log or
        observed inline via poll() (see ``_drain_should_skip``).

        Async-delegation events carry a conversation payload, so draining one
        into the wrong session is a cross-chat leak (#58684, #55578). Two
        filter modes, strongest wins:

        - ``owns_event(evt) -> bool``: positive-proof ownership callback.
          When provided, an async-delegation event is consumed ONLY if the
          callback returns True; everything else is re-queued for its owner.
          The TUI passes its compression-chain-aware ownership check here so
          a post-compression session still claims its own pre-compression
          dispatches.
        - ``session_key``: plain key equality (CLI and other single-session
          callers). Non-matching async-delegation events are re-queued.

        With neither set, all events are consumed (legacy single-session
        behavior, backward compatible).

        Set ``ack_async=False`` when the caller will inject the result through
        an agent turn and ACK it later via ``finish_notification_delivery``.
        """
        results: "list[tuple[dict, str]]" = []
        requeue: "list[dict]" = []
        while not self.completion_queue.empty():
            try:
                evt = self.completion_queue.get_nowait()
            except Exception:
                break
            _evt_sid = evt.get("session_id", "")
            if evt.get("type") == "completion" and self._drain_should_skip(_evt_sid):
                continue
            # Filter async-delegation events so they are not delivered to the
            # wrong session/thread (#58684). Positive-proof callback beats
            # bare key equality when the caller can provide one.
            if evt.get("type") == "async_delegation":
                if owns_event is not None:
                    try:
                        owned = bool(owns_event(evt))
                    except Exception:
                        owned = False  # fail closed — never leak on a broken check
                    if not owned:
                        requeue.append(evt)
                        continue
                elif session_key:
                    evt_session_key = evt.get("session_key", "") or ""
                    if evt_session_key != session_key:
                        requeue.append(evt)
                        continue
            text = format_process_notification(evt)
            if text:
                results.append((evt, text))
                if evt.get("type") == "async_delegation" and ack_async:
                    try:
                        if evt.get("managed_delivery") is not None:
                            from tools.async_delegation import (
                                ManagedAsyncDelegationRecoveryOutcome,
                                mark_managed_async_delegation_delivered_exact,
                            )

                            receipt = (
                                mark_managed_async_delegation_delivered_exact(
                                    evt
                                )
                            )
                            acked = (
                                receipt.outcome
                                is ManagedAsyncDelegationRecoveryOutcome.COMPLETE
                            )
                        else:
                            from tools.async_delegation import (
                                mark_async_delegation_delivered,
                            )

                            acked = (
                                mark_async_delegation_delivered(evt) is True
                            )
                        if not acked:
                            requeue.append(evt)
                    except Exception:
                        logger.debug("Failed to ACK async delegation delivery", exc_info=True)
                        requeue.append(evt)
        for evt in requeue:
            self.completion_queue.put(evt)
        return results

    def get(self, session_id: str) -> Optional[ProcessSession]:
        """Get a session by ID (running or finished)."""
        with self._lock:
            session = self._running.get(session_id) or self._finished.get(session_id)
        return self._refresh_detached_session(session)

    def _reconcile_local_exit(self, session: "ProcessSession") -> None:
        """Reconcile session.exited against the real child process state.

        The reader thread (`_reader_loop`) sets `session.exited = True` only
        in its `finally` block, which runs when `stdout.read()` returns EOF.
        If the direct `Popen` child has exited but a descendant process (e.g.
        a daemon spawned by `hermes update` restarting the gateway) is still
        holding the stdout pipe open, the reader blocks forever and poll()
        keeps returning "running" indefinitely (issue #17327 — 74 polls over
        7 minutes on Feishu).

        This helper closes that window: when `session.exited` is still False
        but the direct child's `Popen.poll()` reports an exit code, drain any
        readable bytes non-blocking and flip `session.exited`. The orphaned
        reader thread remains stuck on its blocking `read()` but is a daemon
        thread and will be reaped with the process.

        An already-exited session that remains in ``_running`` is a durable
        finalization retry (for example after a transient checkpoint/outbox
        write failure), so reconcile it through ``_move_to_finished`` again.
        Otherwise this is a safe no-op on sessions without a local `Popen`
        (env/PTY) and detached-recovered sessions.
        """
        if session is None:
            return
        if session.exited:
            with self._lock:
                retry_finalization = self._running.get(session.id) is session
            if retry_finalization:
                self._move_to_finished(session)
            return
        proc = getattr(session, "process", None)
        if proc is None:
            return
        try:
            rc = proc.poll()
        except Exception:
            return
        if rc is None:
            return  # Direct child still running — reader block is legitimate.

        # Direct child exited. Try to drain any bytes the reader hasn't
        # consumed yet. This is best-effort: if the pipe is held open by a
        # descendant, the non-blocking read returns what's immediately
        # available and we stop.
        drained = ""
        stdout = getattr(proc, "stdout", None)
        if stdout is not None and not _IS_WINDOWS:
            try:
                import fcntl
                fd = stdout.fileno()
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                try:
                    chunk = stdout.read()
                    if chunk:
                        drained = chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
                except (BlockingIOError, OSError, ValueError):
                    pass
                finally:
                    try:
                        fcntl.fcntl(fd, fcntl.F_SETFL, flags)
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("Non-blocking drain failed for %s: %s", session.id, e)

        with session._lock:
            if drained:
                session.output_buffer += drained
                if len(session.output_buffer) > session.max_output_chars:
                    session.output_buffer = session.output_buffer[-session.max_output_chars:]
            session.exited = True
            if session.completion_reason != "killed":
                session.exit_code = rc
                session.completion_reason = "exited"
        logger.info(
            "Reconciled session %s: direct child exited with code %s but reader "
            "was still blocked (orphaned pipe). Flipped to exited.",
            session.id, rc,
        )
        self._move_to_finished(session)

    def poll(self, session_id: str) -> dict:
        """Check status and get new output for a background process."""
        from tools.ansi_strip import strip_ansi

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        # Reconcile against real child state before reading session.exited.
        # Guards against orphaned-pipe reader hangs (issue #17327).
        self._reconcile_local_exit(session)

        with session._lock:
            output_preview = strip_ansi(session.output_buffer[-1000:]) if session.output_buffer else ""

        result = {
            "session_id": session.id,
            "command": session.command,
            "status": "exited" if session.exited else "running",
            "pid": session.pid,
            "uptime_seconds": int(time.time() - session.started_at),
            "output_preview": output_preview,
        }
        if session.exited:
            result["exit_code"] = session.exit_code
            result["completion_reason"] = session.completion_reason
            result["termination_source"] = session.termination_source
            # NOTE: poll() is a read-only status query and deliberately does
            # NOT mark the session _completion_consumed. wait()/read_log()
            # represent actual output consumption and do mark it. Marking
            # consumed here would let a status check silently suppress the
            # notify_on_complete watcher's autonomous delivery turn (#10156).
            #
            # We DO record it in _poll_observed so the CLI's inline drain still
            # dedups (the agent already saw the exit in this turn's poll result)
            # without affecting the gateway/tui watchers, which only consult
            # _completion_consumed.
            self._poll_observed.add(session_id)
        if session.detached:
            result["detached"] = True
            result["note"] = "Process recovered after restart -- output history unavailable"
        return result

    def read_log(self, session_id: str, offset: int = 0, limit: int = 200) -> dict:
        """Read the full output log with optional pagination by lines."""
        from tools.ansi_strip import strip_ansi

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        with session._lock:
            full_output = strip_ansi(session.output_buffer)

        lines = full_output.splitlines()
        total_lines = len(lines)

        # Default: last N lines
        if offset == 0 and limit > 0:
            selected = lines[-limit:]
        else:
            selected = lines[offset:offset + limit]

        result = {
            "session_id": session.id,
            "command": session.command,
            "status": "exited" if session.exited else "running",
            "output": "\n".join(selected),
            "total_lines": total_lines,
            "showing": f"{len(selected)} lines",
        }
        if session.exited:
            self.mark_completion_consumed(session_id)
        return result

    def wait(self, session_id: str, timeout: int = None) -> dict:
        """
        Block until a process exits, timeout, or interrupt.

        Args:
            session_id: The process to wait for.
            timeout: Max seconds to block. Falls back to TERMINAL_TIMEOUT config.

        Returns:
            dict with status ("exited", "timeout", "interrupted", "not_found")
            and output snapshot.
        """
        from tools.ansi_strip import strip_ansi
        from tools.interrupt import is_interrupted as _is_interrupted

        try:
            default_timeout = int(os.getenv("TERMINAL_TIMEOUT", "180"))
        except (ValueError, TypeError):
            default_timeout = 180
        max_timeout = default_timeout
        requested_timeout = timeout
        timeout_note = None

        if requested_timeout and requested_timeout > max_timeout:
            effective_timeout = max_timeout
            timeout_note = (
                f"Requested wait of {requested_timeout}s was clamped "
                f"to configured limit of {max_timeout}s"
            )
        else:
            effective_timeout = requested_timeout or max_timeout

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        deadline = time.monotonic() + effective_timeout

        while time.monotonic() < deadline:
            session = self._refresh_detached_session(session)
            if session is None:
                return {"status": "not_found", "error": f"No process with ID {session_id}"}
            # Reconcile against real child state — guards against orphaned-
            # pipe reader hangs where the reader is blocked but the direct
            # child has already exited (issue #17327).
            self._reconcile_local_exit(session)
            if session.exited:
                self.mark_completion_consumed(session_id)
                result = {
                    "status": "exited",
                    "command": session.command,
                    "exit_code": session.exit_code,
                    "completion_reason": session.completion_reason,
                    "termination_source": session.termination_source,
                    "output": strip_ansi(session.output_buffer[-2000:]),
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result

            if _is_interrupted():
                result = {
                    "status": "interrupted",
                    "command": session.command,
                    "output": strip_ansi(session.output_buffer[-1000:]),
                    "note": "User sent a new message -- wait interrupted",
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            session._completion_event.wait(timeout=min(1.0, remaining))

        result = {
            "status": "timeout",
            "command": session.command,
            "output": strip_ansi(session.output_buffer[-1000:]),
        }
        if timeout_note:
            result["timeout_note"] = timeout_note
        else:
            result["timeout_note"] = f"Waited {effective_timeout}s, process still running"
        return result

    @_process_lifecycle_fenced
    def kill_process(self, session_id: str, *, source: str = "process.kill") -> dict:
        """Kill a background process."""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        if session.exited:
            return {
                "status": "already_exited",
                "exit_code": session.exit_code,
            }

        # Kill via PTY, Popen (local), or env execute (non-local)
        try:
            if session._pty:
                # PTY process -- terminate via ptyprocess
                try:
                    session._pty.terminate(force=True)
                except Exception:
                    if session.pid:
                        os.kill(session.pid, signal.SIGTERM)
            elif session.process:
                # Local process -- kill the process tree. On Windows this
                # must be taskkill /T /F; Popen.terminate() only kills the
                # shell wrapper and leaves Git Bash descendants behind.
                self._terminate_host_pid(session.process.pid, session.host_start_time)
            elif session.env_ref and session.pid:
                # Non-local -- each managed background command owns a
                # dedicated process group. Do not report success until the
                # sandbox verifies that the whole group is gone.
                if not self._terminate_env_process_group(
                    session.env_ref, session.pid
                ):
                    raise RuntimeError(
                        "Sandbox process-group termination could not be verified"
                    )
            elif session.detached and session.pid_scope == "host" and session.pid:
                # Identity check, not bare liveness: if the PID is gone OR was
                # recycled onto an unrelated process, treat our process as
                # exited and never tree-kill the stranger.
                if (
                    not isinstance(session.process_start_token, str)
                    or not session.process_start_token
                    or not self._host_pid_matches_exact_token(
                        session.pid,
                        session.process_start_token,
                    )
                ):
                    with session._lock:
                        session.exited = True
                        session.exit_code = None
                    self._move_to_finished(session)
                    return {
                        "status": "already_exited",
                        "exit_code": session.exit_code,
                    }
                live_token = self._safe_host_start_token(session.pid)
                if live_token != session.process_start_token:
                    raise RuntimeError(
                        "Recovered process identity changed before termination"
                    )
                self._terminate_host_pid(session.pid, session.host_start_time)
            else:
                return {
                    "status": "error",
                    "error": (
                        "Recovered process cannot be killed after restart because "
                        "its original runtime handle is no longer available"
                    ),
                }
            session.exited = True
            session.exit_code = -15  # SIGTERM
            session.completion_reason = "killed"
            session.termination_source = source
            self._move_to_finished(session)
            return {
                "status": "killed",
                "session_id": session.id,
                "completion_reason": session.completion_reason,
                "termination_source": session.termination_source,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def write_stdin(self, session_id: str, data: str) -> dict:
        """Send raw data to a running process's stdin (no newline appended)."""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "error": "Process has already finished"}

        # PTY mode -- write through pty handle.
        if hasattr(session, '_pty') and session._pty:
            try:
                # pywinpty expects str on Windows; ptyprocess expects bytes on POSIX.
                if _IS_WINDOWS:
                    pty_data = data.decode("utf-8") if isinstance(data, bytes) else str(data)
                else:
                    pty_data = data.encode("utf-8") if isinstance(data, str) else data
                session._pty.write(pty_data)
                return {"status": "ok", "bytes_written": len(data)}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        # Popen mode -- write through stdin pipe
        if not session.process or not session.process.stdin:
            return {"status": "error", "error": "Process stdin not available (non-local backend or stdin closed)"}
        try:
            session.process.stdin.write(data)
            session.process.stdin.flush()
            return {"status": "ok", "bytes_written": len(data)}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def submit_stdin(self, session_id: str, data: str = "") -> dict:
        """Send data + newline to a running process's stdin (like pressing Enter)."""
        return self.write_stdin(session_id, data + "\n")

    def request_close_terminal(self, session_id: str) -> dict:
        """Ask the desktop GUI to close the read-only terminal tab mirroring this
        background process.

        This does NOT kill the process — it only drops the view. Output keeps
        streaming into the (capped) buffer and the user can reopen the tab from
        the status stack. Desktop-only: returns an error if no UI close sink is
        wired (e.g. CLI / messaging)."""
        sink = self.on_close
        if sink is None:
            return {
                "status": "error",
                "error": "close_terminal is only available in the Hermes desktop app.",
            }
        # The session may already be finished (or pruned) — the tab can still
        # linger and be closed, so a missing session is not an error here.
        session = self.get(session_id)
        try:
            sink(session, session_id)
        except Exception as e:
            return {"status": "error", "error": str(e)}
        return {
            "status": "ok",
            "closed": session_id,
            "note": (
                "Closed the read-only terminal tab. The process was not killed; "
                "its output remains available and the user can reopen the tab "
                "from the status stack."
            ),
        }

    def close_stdin(self, session_id: str) -> dict:
        """Close a running process's stdin / send EOF without killing the process."""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "error": "Process has already finished"}

        if hasattr(session, '_pty') and session._pty:
            try:
                session._pty.sendeof()
                return {"status": "ok", "message": "EOF sent"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        if not session.process or not session.process.stdin:
            return {"status": "error", "error": "Process stdin not available (non-local backend or stdin closed)"}
        try:
            session.process.stdin.close()
            return {"status": "ok", "message": "stdin closed"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def count_running(self) -> int:
        """Return the count of currently-running background processes.

        Cheap O(1) read of the running dict, suitable for status-bar polling
        on every render tick. CPython dict ``len()`` is atomic; callers do not
        need to hold ``self._lock``. Reflects ``_running`` only: sessions are
        moved to ``_finished`` when their subprocess exits.
        """
        try:
            return len(self._running)
        except Exception:
            return 0

    def list_sessions(self, task_id: str = None, session_key: str = None) -> list:
        """List all running and recently-finished processes.

        When ``task_id`` is given, processes for that task are included. When
        ``session_key`` is also given, session-scoped background processes
        (``background: true``) registered under that gateway session are
        surfaced too, even if they belong to a different task — so the agent
        can discover a forgotten preview server that is blocking session
        reset (#29177). Such cross-task entries are flagged with
        ``"session_scoped": true``.
        """
        with self._lock:
            all_sessions = list(self._running.values()) + list(self._finished.values())

        all_sessions = [self._refresh_detached_session(s) for s in all_sessions]

        if task_id or session_key:
            all_sessions = [
                s for s in all_sessions
                if (task_id and s.task_id == task_id)
                or (session_key and s.session_key == session_key)
            ]

        result = []
        for s in all_sessions:
            entry = {
                "session_id": s.id,
                "command": s.command[:200],
                "cwd": s.cwd,
                "pid": s.pid,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(s.started_at)),
                "uptime_seconds": int(time.time() - s.started_at),
                "status": "exited" if s.exited else "running",
                "output_preview": s.output_buffer[-200:] if s.output_buffer else "",
            }
            # Flag processes surfaced only because they share the gateway
            # session (not the current task) — these are the long-lived
            # background processes a user may have forgotten about (#29177).
            if task_id and session_key and s.task_id != task_id and s.session_key == session_key:
                entry["session_scoped"] = True
            # Trigger metadata so a goal-loop judge can decide to wait on this
            # process's OWN signal (a watch-pattern match or completion), not
            # just its exit. A watcher with watch_patterns may never exit.
            if s.watch_patterns and not s._watch_disabled:
                entry["watch_patterns"] = list(s.watch_patterns)
                entry["watch_hit"] = s._watch_hits > 0
            if s.notify_on_complete:
                entry["notify_on_complete"] = True
            if s.exited:
                entry["exit_code"] = s.exit_code
            if s.detached:
                entry["detached"] = True
            result.append(entry)
        return result

    # ----- Session/Task Queries (for gateway integration) -----

    def has_active_processes(self, task_id: str) -> bool:
        """Check if there are active (running) processes for a task_id."""
        with self._lock:
            sessions = list(self._running.values())

        for session in sessions:
            self._refresh_detached_session(session)

        with self._lock:
            return any(
                s.task_id == task_id and not s.exited
                for s in self._running.values()
            )

    def has_active_for_session(
        self, session_key: str, max_active_age: Optional[float] = None,
    ) -> bool:
        """Check if there are active processes for a gateway session key.

        When *max_active_age* is set (seconds), processes that started more
        than that many seconds ago are **ignored** — they are still running
        but are considered stale and must not block session idle / daily
        reset.  This prevents a forgotten ``http.server`` (or any long-lived
        preview process) from permanently freezing the session lifecycle.

        Args:
            session_key: Gateway session key to check.
            max_active_age: If set, ignore processes older than this many
                seconds.  ``None`` retains the legacy behaviour (any running
                process blocks).
        """
        with self._lock:
            sessions = list(self._running.values())

        for session in sessions:
            self._refresh_detached_session(session)

        now = time.time()
        with self._lock:
            return any(
                s.session_key == session_key
                and not s.exited
                and (max_active_age is None or (now - s.started_at) < max_active_age)
                for s in self._running.values()
            )

    def has_any_active(self) -> bool:
        """Whether ANY background process is still running (across all sessions).

        Used by scale-to-zero idle detection (gateway/scale_to_zero): a gateway
        with a live background process (terminal background=true) is NOT idle and
        must not be suspended, or the process is lost. Refreshes detached
        sessions first so a finished-but-unreaped process reads as inactive.
        """
        with self._lock:
            sessions = list(self._running.values())

        for session in sessions:
            self._refresh_detached_session(session)

        with self._lock:
            return any(not s.exited for s in self._running.values())

    def kill_all(self, task_id: str = None) -> int:
        """Kill all running processes, optionally filtered by task_id. Returns count killed."""
        with self._lock:
            targets = [
                s for s in self._running.values()
                if (task_id is None or s.task_id == task_id) and not s.exited
            ]

        killed = 0
        for session in targets:
            result = self.kill_process(session.id, source="kill_all")
            if result.get("status") in {"killed", "already_exited"}:
                killed += 1
        return killed

    # ----- Cleanup / Pruning -----

    def _prune_if_needed(self):
        """Remove oldest finished sessions if over MAX_PROCESSES. Must hold _lock."""
        # First prune expired finished sessions
        now = time.time()
        expired = [
            sid for sid, s in self._finished.items()
            if (now - s.started_at) > FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            del self._finished[sid]
            self._completion_consumed.discard(sid)
            self._poll_observed.discard(sid)

        # If still over limit, remove oldest finished
        total = len(self._running) + len(self._finished)
        if total >= MAX_PROCESSES and self._finished:
            oldest_id = min(self._finished, key=lambda sid: self._finished[sid].started_at)
            del self._finished[oldest_id]
            self._completion_consumed.discard(oldest_id)
            self._poll_observed.discard(oldest_id)

        # Drop any _completion_consumed / _poll_observed entries whose sessions
        # are no longer tracked at all — belt-and-suspenders against
        # module-lifetime growth on registry lookup paths that don't reach the
        # dict prunes.
        tracked = self._running.keys() | self._finished.keys()
        stale = self._completion_consumed - tracked
        if stale:
            self._completion_consumed -= stale
        stale_polls = self._poll_observed - tracked
        if stale_polls:
            self._poll_observed -= stale_polls

    # ----- Checkpoint (crash recovery) -----

    def _checkpoint_entry_for_session(
        self,
        session: ProcessSession,
        *,
        owner_id: str,
        owner_pid: int,
        owner_start_token: str,
    ) -> Dict[str, Any]:
        if (
            isinstance(session.pid, bool)
            or not isinstance(session.pid, int)
            or session.pid <= 1
        ):
            raise ValueError("running process has no valid checkpoint PID")
        if session.pid_scope == "host":
            if session.process_start_token is None:
                session.process_start_token = self._safe_host_start_token(session.pid)
            if session.host_start_time is None:
                session.host_start_time = self._safe_host_start_time(session.pid)
            if not session.process_start_token:
                raise ValueError("running host process identity is unavailable")
        elif session.pid_scope != "sandbox":
            raise ValueError("running process PID scope is invalid")
        return {
            "session_id": session.id,
            "command": session.command,
            "pid": session.pid,
            "pid_scope": session.pid_scope,
            "host_start_time": session.host_start_time,
            "process_start_token": session.process_start_token,
            "checkpoint_owner_id": owner_id,
            "checkpoint_owner_pid": owner_pid,
            "checkpoint_owner_start_token": owner_start_token,
            "cwd": session.cwd,
            "started_at": session.started_at,
            "task_id": session.task_id,
            "session_key": session.session_key,
            "watcher_platform": session.watcher_platform,
            "watcher_chat_id": session.watcher_chat_id,
            "watcher_user_id": session.watcher_user_id,
            "watcher_user_name": session.watcher_user_name,
            "watcher_thread_id": session.watcher_thread_id,
            "watcher_message_id": session.watcher_message_id,
            "watcher_interval": session.watcher_interval,
            "notify_on_complete": session.notify_on_complete,
            "watch_patterns": session.watch_patterns,
        }

    def _foreign_checkpoint_entry_is_active(
        self,
        entry: Dict[str, Any],
    ) -> bool:
        """Keep foreign evidence only while owner or exact process is live."""
        owner_id = entry.get("checkpoint_owner_id")
        owner_pid = entry.get("checkpoint_owner_pid")
        owner_token = entry.get("checkpoint_owner_start_token")
        if (
            owner_id is not None
            and self._host_pid_matches_exact_token(owner_pid, owner_token)
        ):
            return True
        if entry.get("pid_scope") != "host":
            # Sandbox PIDs cannot be safely probed from this runtime. Preserve
            # the evidence rather than manufacturing a false global zero.
            return True
        process_token = entry.get("process_start_token")
        if isinstance(process_token, str):
            return self._host_pid_matches_exact_token(
                entry.get("pid"),
                process_token,
            )
        # Legacy host evidence without an exact token can be discarded only
        # when the PID is definitely gone; a live PID remains fail-closed.
        return self._is_host_pid_alive(entry.get("pid"))

    def _write_checkpoint(self) -> bool:
        """Merge this runtime's exact process set into the global authority."""
        with self._checkpoint_io_lock:
            if self._checkpoint_write_blocked:
                self._process_checkpoint_available = False
                return False
            try:
                owner_id, owner_pid, owner_start_token = (
                    self._ensure_checkpoint_owner_identity()
                )
                with self._lock:
                    local_entries = [
                        self._checkpoint_entry_for_session(
                            session,
                            owner_id=owner_id,
                            owner_pid=owner_pid,
                            owner_start_token=owner_start_token,
                        )
                        for session in self._running.values()
                        if not session.exited
                    ]

                with _process_authority_lock(CHECKPOINT_PATH):
                    existing, expected = self._read_checkpoint_snapshot_secure(
                        missing_ok=True
                    )
                    foreign = []
                    for entry in existing:
                        if entry.get("checkpoint_owner_id") == owner_id:
                            continue
                        if self._foreign_checkpoint_entry_is_active(entry):
                            foreign.append(entry)
                        else:
                            logger.info(
                                "Reconciled inactive foreign process "
                                "checkpoint row %s",
                                entry.get("session_id", "?"),
                            )
                    foreign_ids = {entry["session_id"] for entry in foreign}
                    local_ids = {entry["session_id"] for entry in local_entries}
                    collision = foreign_ids & local_ids
                    if collision:
                        raise ValueError(
                            "process checkpoint session identity collides across runtimes"
                        )
                    merged = sorted(
                        [*foreign, *local_entries],
                        key=lambda entry: entry["session_id"],
                    )
                    _process_authority_atomic_write(
                        CHECKPOINT_PATH,
                        merged,
                        expected=expected,
                        max_bytes=MAX_CHECKPOINT_BYTES,
                        sort_keys=True,
                    )
                self._process_checkpoint_available = True
                self._process_checkpoint_reason = "verified"
                return True
            except ValueError as exc:
                self._checkpoint_write_blocked = True
                self._process_checkpoint_available = False
                self._process_checkpoint_reason = "invalid"
                logger.error(
                    "Process checkpoint evidence is unsafe or malformed",
                    exc_info=True,
                )
                return False
            except Exception as e:
                self._process_checkpoint_available = False
                self._process_checkpoint_reason = "write_failed"
                logger.debug("Failed to write checkpoint file: %s", e, exc_info=True)
                return False

    @staticmethod
    def _validate_checkpoint_entries(raw: object) -> List[Dict[str, Any]]:
        """Validate the bounded JSON checkpoint before mutating registry state."""
        if not isinstance(raw, list):
            raise ValueError("process checkpoint must be a JSON list")
        entries: List[Dict[str, Any]] = []
        session_ids: set[str] = set()
        for raw_entry in raw:
            if not isinstance(raw_entry, dict):
                raise ValueError("process checkpoint entry is not an object")
            entry = dict(raw_entry)
            session_id = entry.get("session_id")
            pid = entry.get("pid")
            pid_scope = entry.get("pid_scope", "host")
            start_time = entry.get("host_start_time")
            process_start_token = entry.get("process_start_token")
            owner_values = (
                entry.get("checkpoint_owner_id"),
                entry.get("checkpoint_owner_pid"),
                entry.get("checkpoint_owner_start_token"),
            )
            if (
                not isinstance(session_id, str)
                or not session_id
                or session_id in session_ids
            ):
                raise ValueError("process checkpoint session id is invalid")
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
                raise ValueError("process checkpoint pid is invalid")
            if pid_scope not in {"host", "sandbox"}:
                raise ValueError("process checkpoint PID scope is invalid")
            if (
                start_time is not None
                and (
                    isinstance(start_time, bool)
                    or not isinstance(start_time, int)
                    or start_time <= 0
                )
            ):
                raise ValueError("process checkpoint start time is invalid")
            if process_start_token is not None and (
                not isinstance(process_start_token, str)
                or not process_start_token
            ):
                raise ValueError("process checkpoint exact start token is invalid")
            owner_present = [value is not None for value in owner_values]
            if any(owner_present) and not all(owner_present):
                raise ValueError("process checkpoint owner identity is incomplete")
            if all(owner_present):
                owner_id, owner_pid, owner_start_token = owner_values
                if (
                    not isinstance(owner_id, str)
                    or not owner_id
                    or isinstance(owner_pid, bool)
                    or not isinstance(owner_pid, int)
                    or owner_pid <= 1
                    or not isinstance(owner_start_token, str)
                    or not owner_start_token
                ):
                    raise ValueError("process checkpoint owner identity is invalid")
                if pid_scope == "host" and process_start_token is None:
                    raise ValueError(
                        "owned host checkpoint lacks an exact start token"
                    )
            session_ids.add(session_id)
            entry["pid_scope"] = pid_scope
            entries.append(entry)
        return entries

    @classmethod
    def _read_checkpoint_snapshot_secure(
        cls,
        *,
        missing_ok: bool,
    ) -> tuple[List[Dict[str, Any]], Optional[FileIdentity]]:
        raw, identity = _process_authority_read_json(
            CHECKPOINT_PATH,
            max_bytes=MAX_CHECKPOINT_BYTES,
            missing_ok=missing_ok,
        )
        if raw is None and identity is None:
            return [], None
        return cls._validate_checkpoint_entries(raw), identity

    @classmethod
    def _read_checkpoint_entries_secure(cls) -> List[Dict[str, Any]]:
        """Read ``processes.json`` from one private, stable file descriptor."""
        entries, _identity = cls._read_checkpoint_snapshot_secure(missing_ok=False)
        return entries

    def recover_from_checkpoint(self) -> int:
        """
        On gateway startup, probe PIDs from checkpoint file.

        Returns the number of processes recovered as detached.
        """
        with self._checkpoint_io_lock:
            self._process_checkpoint_available = False
            self._process_checkpoint_reason = "unverified"
            self._checkpoint_write_blocked = True
            try:
                owner_id, owner_pid, owner_start_token = (
                    self._ensure_checkpoint_owner_identity()
                )
                with _process_authority_lock(CHECKPOINT_PATH):
                    entries, expected = self._read_checkpoint_snapshot_secure(
                        missing_ok=False
                    )

                    survivors: List[Dict[str, Any]] = []
                    adopted_entries: List[Dict[str, Any]] = []
                    for entry in entries:
                        entry_owner_id = entry.get("checkpoint_owner_id")
                        entry_owner_pid = entry.get("checkpoint_owner_pid")
                        entry_owner_token = entry.get(
                            "checkpoint_owner_start_token"
                        )
                        owner_alive = (
                            entry_owner_id is not None
                            and self._host_pid_matches_exact_token(
                                entry_owner_pid,
                                entry_owner_token,
                            )
                        )
                        if owner_alive and entry_owner_id != owner_id:
                            survivors.append(entry)
                            continue

                        if entry["pid_scope"] != "host":
                            self._process_checkpoint_reason = (
                                "identity_unverified"
                            )
                            return 0

                        pid = entry["pid"]
                        if not self._is_host_pid_alive(pid):
                            continue
                        process_start_token = entry.get("process_start_token")
                        if not isinstance(process_start_token, str):
                            self._process_checkpoint_reason = (
                                "identity_unverified"
                            )
                            return 0
                        if not self._host_pid_matches_exact_token(
                            pid,
                            process_start_token,
                        ):
                            logger.info(
                                "Not recovering session %s: exact process "
                                "identity no longer matches",
                                entry.get("session_id", "?"),
                            )
                            continue

                        adopted = dict(entry)
                        adopted["checkpoint_owner_id"] = owner_id
                        adopted["checkpoint_owner_pid"] = owner_pid
                        adopted["checkpoint_owner_start_token"] = (
                            owner_start_token
                        )
                        survivors.append(adopted)
                        adopted_entries.append(adopted)

                    _process_authority_atomic_write(
                        CHECKPOINT_PATH,
                        sorted(
                            survivors,
                            key=lambda entry: entry["session_id"],
                        ),
                        expected=expected,
                        max_bytes=MAX_CHECKPOINT_BYTES,
                        sort_keys=True,
                    )
            except FileNotFoundError:
                self._process_checkpoint_reason = "missing"
                return 0
            except Exception:
                self._process_checkpoint_reason = "invalid"
                logger.error(
                    "Process checkpoint could not be securely reconciled",
                    exc_info=True,
                )
                return 0

            self._checkpoint_write_blocked = False

            recovered = 0
            for entry in adopted_entries:
                pid = entry["pid"]

                session = ProcessSession(
                    id=entry["session_id"],
                    command=entry.get("command", "unknown"),
                    task_id=entry.get("task_id", ""),
                    session_key=entry.get("session_key", ""),
                    pid=pid,
                    host_start_time=entry.get("host_start_time"),
                    process_start_token=entry.get("process_start_token"),
                    pid_scope="host",
                    cwd=entry.get("cwd"),
                    started_at=entry.get("started_at", time.time()),
                    detached=True,  # Can't read output, but can report status + kill
                    watcher_platform=entry.get("watcher_platform", ""),
                    watcher_chat_id=entry.get("watcher_chat_id", ""),
                    watcher_user_id=entry.get("watcher_user_id", ""),
                    watcher_user_name=entry.get("watcher_user_name", ""),
                    watcher_thread_id=entry.get("watcher_thread_id", ""),
                    watcher_message_id=entry.get("watcher_message_id", ""),
                    watcher_interval=entry.get("watcher_interval", 0),
                    notify_on_complete=entry.get("notify_on_complete", False),
                    watch_patterns=entry.get("watch_patterns", []),
                )
                with self._lock:
                    self._running[session.id] = session
                recovered += 1
                logger.info(
                    "Recovered detached process: %s (pid=%d)",
                    session.command[:60],
                    pid,
                )

                # Re-enqueue watcher so gateway can resume notifications.
                if session.watcher_interval > 0:
                    self.pending_watchers.append({
                        "session_id": session.id,
                        "check_interval": session.watcher_interval,
                        "session_key": session.session_key,
                        "platform": session.watcher_platform,
                        "chat_id": session.watcher_chat_id,
                        "user_id": session.watcher_user_id,
                        "user_name": session.watcher_user_name,
                        "thread_id": session.watcher_thread_id,
                        "message_id": session.watcher_message_id,
                        "notify_on_complete": session.notify_on_complete,
                    })

            self._process_checkpoint_available = True
            self._process_checkpoint_reason = "verified"
            with self._lock:
                self._foreign_owner_active = max(
                    0, len(survivors) - len(adopted_entries)
                )
            return recovered

    def _managed_recovery_epoch(self) -> tuple[int, str, str]:
        """Return one fork-safe process and registry-instance identity."""
        pid = os.getpid()
        token = self._safe_host_start_token(pid)
        if not isinstance(token, str) or not token:
            raise ManagedProcessRecoveryAmbiguous(
                "managed recovery process identity is unavailable"
            )
        current = getattr(self, "_managed_process_recovery_epoch", None)
        if (
            not isinstance(current, tuple)
            or len(current) != 3
            or current[0] != pid
            or current[1] != token
            or not isinstance(current[2], str)
            or not current[2]
        ):
            current = (pid, token, f"registry_{uuid.uuid4().hex}")
            self._managed_process_recovery_epoch = current
        return current

    @staticmethod
    def _validate_managed_checkpoint_entry(entry: Dict[str, Any]) -> None:
        """Apply strict bounds beyond the legacy compatibility validator."""
        allowed = {
            "session_id", "command", "pid", "pid_scope", "host_start_time",
            "process_start_token", "checkpoint_owner_id",
            "checkpoint_owner_pid", "checkpoint_owner_start_token", "cwd",
            "started_at", "task_id", "session_key", "watcher_platform",
            "watcher_chat_id", "watcher_user_id", "watcher_user_name",
            "watcher_thread_id", "watcher_message_id", "watcher_interval",
            "notify_on_complete", "watch_patterns",
        }
        if set(entry) - allowed:
            raise ManagedProcessRecoveryAmbiguous(
                "managed process checkpoint contains unknown fields"
            )
        text_fields = (
            "session_id",
            "command",
            "task_id",
            "session_key",
            "cwd",
            "watcher_platform",
            "watcher_chat_id",
            "watcher_user_id",
            "watcher_user_name",
            "watcher_thread_id",
            "watcher_message_id",
        )
        for name in text_fields:
            value = entry.get(name)
            if value is not None and (
                not isinstance(value, str) or len(value.encode("utf-8")) > 65_536
            ):
                raise ManagedProcessRecoveryAmbiguous(
                    "managed process checkpoint text field is invalid"
                )
        session_id = entry.get("session_id")
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id.encode("utf-8")) > 512
        ):
            raise ManagedProcessRecoveryAmbiguous(
                "managed process session identity is invalid"
            )
        started_at = entry.get("started_at")
        if started_at is not None and (
            isinstance(started_at, bool)
            or not isinstance(started_at, (int, float))
            or not math.isfinite(float(started_at))
        ):
            raise ManagedProcessRecoveryAmbiguous(
                "managed process checkpoint timestamp is invalid"
            )
        interval = entry.get("watcher_interval", 0)
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int)
            or interval < 0
            or interval > 86_400
        ):
            raise ManagedProcessRecoveryAmbiguous(
                "managed process watcher interval is invalid"
            )
        pid = entry.get("pid")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 1
            or pid > 2**31 - 1
            or entry.get("pid_scope") not in {"host", "sandbox"}
        ):
            raise ManagedProcessRecoveryAmbiguous(
                "managed process PID authority is invalid"
            )
        notify = entry.get("notify_on_complete", False)
        if not isinstance(notify, bool):
            raise ManagedProcessRecoveryAmbiguous(
                "managed process notification flag is invalid"
            )
        patterns = entry.get("watch_patterns", [])
        if (
            not isinstance(patterns, list)
            or len(patterns) > 64
            or any(
                not isinstance(pattern, str)
                or len(pattern.encode("utf-8")) > 4096
                for pattern in patterns
            )
        ):
            raise ManagedProcessRecoveryAmbiguous(
                "managed process watch patterns are invalid"
            )

    @staticmethod
    def _validate_managed_completion_record(record: Dict[str, Any]) -> None:
        allowed = {
            "event_id", "type", "session_id", "process_pid",
            "process_start_token", "session_key", "platform", "chat_id",
            "user_id", "user_name", "thread_id", "message_id", "command",
            "exit_code", "completion_reason", "termination_source", "output",
            "created_at", "delivered", "delivered_at",
        }
        if set(record) - allowed:
            raise ManagedProcessRecoveryAmbiguous(
                "managed completion record contains unknown fields"
            )
        delivered_at = record.get("delivered_at")
        if delivered_at is not None and (
            isinstance(delivered_at, bool)
            or not isinstance(delivered_at, (int, float))
            or not math.isfinite(float(delivered_at))
        ):
            raise ManagedProcessRecoveryAmbiguous(
                "managed completion delivered timestamp is invalid"
            )
        if record.get("delivered") is True and delivered_at is None:
            raise ManagedProcessRecoveryAmbiguous(
                "managed delivered completion lacks delivered_at"
            )
        if record.get("delivered") is not True and delivered_at is not None:
            raise ManagedProcessRecoveryAmbiguous(
                "managed undelivered completion has delivered_at"
            )
        created_at = record.get("created_at")
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
        ):
            raise ManagedProcessRecoveryAmbiguous(
                "managed completion created timestamp is invalid"
            )
        exit_code = record.get("exit_code")
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise ManagedProcessRecoveryAmbiguous(
                "managed completion exit code is invalid"
            )
        process_pid = record.get("process_pid")
        if process_pid is not None and (
            isinstance(process_pid, bool)
            or not isinstance(process_pid, int)
            or process_pid <= 1
        ):
            raise ManagedProcessRecoveryAmbiguous(
                "managed completion process PID is invalid"
            )
        for name in (
            "session_id", "process_start_token", "session_key", "platform",
            "chat_id", "user_id", "user_name", "thread_id", "message_id",
            "command", "completion_reason", "termination_source", "output",
        ):
            value = record.get(name)
            if value is not None and (
                not isinstance(value, str)
                or len(value.encode("utf-8")) > 65_536
            ):
                raise ManagedProcessRecoveryAmbiguous(
                    "managed completion payload field is invalid"
                )

    @staticmethod
    def _managed_session_from_entry(entry: Dict[str, Any]) -> ProcessSession:
        return ProcessSession(
            id=entry["session_id"],
            command=entry.get("command", "unknown"),
            task_id=entry.get("task_id", ""),
            session_key=entry.get("session_key", ""),
            pid=entry["pid"],
            host_start_time=entry.get("host_start_time"),
            process_start_token=entry.get("process_start_token"),
            pid_scope="host",
            cwd=entry.get("cwd"),
            started_at=entry.get("started_at", time.time()),
            detached=True,
            watcher_platform=entry.get("watcher_platform", ""),
            watcher_chat_id=entry.get("watcher_chat_id", ""),
            watcher_user_id=entry.get("watcher_user_id", ""),
            watcher_user_name=entry.get("watcher_user_name", ""),
            watcher_thread_id=entry.get("watcher_thread_id", ""),
            watcher_message_id=entry.get("watcher_message_id", ""),
            watcher_interval=entry.get("watcher_interval", 0),
            notify_on_complete=entry.get("notify_on_complete", False),
            watch_patterns=entry.get("watch_patterns", []),
        )

    @_process_lifecycle_fenced
    def recover_managed_startup_exact(
        self,
        *,
        crash_hook=None,
    ) -> ManagedProcessRecoveryReceipt:
        """Recover process and notification authorities with exact receipts.

        The legacy count-returning startup APIs remain unchanged. This managed
        path instead binds both authority files, records every checkpoint-row
        disposition, and independently proves the registry and durable outbox
        postconditions before returning.
        """
        if _IS_WINDOWS:
            raise ManagedProcessRecoveryAmbiguous(
                "managed exact recovery requires held POSIX directory authority"
            )
        checkpoint_path = _canonical_authority_path(CHECKPOINT_PATH)
        notifications_path = _canonical_authority_path(NOTIFICATIONS_PATH)
        process_pid = 0
        process_token = ""
        registry_epoch = ""
        classifications: list[tuple[str, str]] = []
        adopted_entries: list[Dict[str, Any]] = []
        recovered_ids: list[str] = []
        deduped_process_ids: list[str] = []
        queued_event_ids: list[str] = []
        deduped_event_ids: list[str] = []
        terminal_records: list[Dict[str, Any]] = []

        # Match the lock order used by completion finalization:
        # completion outbox -> checkpoint -> authority-file locks.
        with self._completion_outbox_lock:
            with self._checkpoint_io_lock:
                process_pid, process_token, registry_epoch = (
                    self._managed_recovery_epoch()
                )
                self._process_checkpoint_available = False
                self._process_checkpoint_reason = "unverified"
                self._checkpoint_write_blocked = True
                owner_id, owner_pid, owner_token = (
                    self._ensure_checkpoint_owner_identity()
                )
                with _process_authority_lock(checkpoint_path):
                    checkpoint_before = _read_private_json_receipt(
                        checkpoint_path,
                        max_bytes=MAX_CHECKPOINT_BYTES,
                        missing_ok=True,
                    )
                    if checkpoint_before.identity is None:
                        entries: List[Dict[str, Any]] = []
                    else:
                        try:
                            entries = self._validate_checkpoint_entries(
                                checkpoint_before.value
                            )
                        except Exception as exc:
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed process checkpoint schema is invalid"
                            ) from exc
                    if len(entries) > MAX_MANAGED_RECOVERY_RECORDS:
                        raise ManagedProcessRecoveryAmbiguous(
                            "managed process checkpoint record budget exceeded"
                        )
                    for entry in entries:
                        self._validate_managed_checkpoint_entry(entry)

                    # Validate the complete notification authority before any
                    # checkpoint, registry, or queue mutation. This prevents a
                    # malformed/unbounded event from being discovered only
                    # after terminal checkpoint rows have been removed.
                    with _process_authority_lock(notifications_path):
                        notification_preflight = _read_private_json_receipt(
                            notifications_path,
                            max_bytes=MAX_COMPLETION_OUTBOX_BYTES,
                            missing_ok=True,
                        )
                        if notification_preflight.identity is not None:
                            raw_preflight = notification_preflight.value
                            if (
                                not isinstance(raw_preflight, dict)
                                or set(raw_preflight) != {"version", "events"}
                                or raw_preflight.get("version")
                                != COMPLETION_OUTBOX_VERSION
                                or not isinstance(
                                    raw_preflight.get("events"), dict
                                )
                                or len(raw_preflight["events"])
                                > MAX_COMPLETION_OUTBOX_RECORDS
                            ):
                                raise ManagedProcessRecoveryAmbiguous(
                                    "managed completion outbox schema is invalid"
                                )
                            try:
                                preflight_events = {
                                    event_id: self._validate_completion_record(
                                        event_id, record
                                    )
                                    for event_id, record in raw_preflight[
                                        "events"
                                    ].items()
                                    if isinstance(event_id, str)
                                }
                                if len(preflight_events) != len(
                                    raw_preflight["events"]
                                ):
                                    raise ValueError("invalid event id")
                                for record in preflight_events.values():
                                    self._validate_managed_completion_record(
                                        record
                                    )
                            except Exception as exc:
                                raise ManagedProcessRecoveryAmbiguous(
                                    "managed completion record is invalid"
                                ) from exc

                    survivors: list[Dict[str, Any]] = []
                    for entry in entries:
                        session_id = entry["session_id"]
                        entry_owner_id = entry.get("checkpoint_owner_id")
                        entry_owner_pid = entry.get("checkpoint_owner_pid")
                        entry_owner_token = entry.get(
                            "checkpoint_owner_start_token"
                        )
                        foreign_owner_alive = (
                            entry_owner_id is not None
                            and entry_owner_id != owner_id
                            and self._host_pid_matches_exact_token(
                                entry_owner_pid,
                                entry_owner_token,
                            )
                        )
                        if foreign_owner_alive:
                            survivors.append(entry)
                            classifications.append(
                                (session_id, "foreign_owner_active")
                            )
                            continue
                        if entry["pid_scope"] != "host":
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed recovery cannot prove sandbox PID identity"
                            )
                        expected_token = entry.get("process_start_token")
                        if not isinstance(expected_token, str) or not expected_token:
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed host checkpoint lacks exact PID identity"
                            )
                        pid = entry["pid"]
                        if not self._is_host_pid_alive(pid):
                            classifications.append((session_id, "process_absent"))
                            if entry.get("notify_on_complete") is True:
                                terminal_records.append(
                                    self._build_recovered_terminal_record(
                                        entry, "process_absent"
                                    )
                                )
                            continue
                        if self._safe_host_start_token(pid) != expected_token:
                            classifications.append(
                                (session_id, "pid_identity_mismatch")
                            )
                            if entry.get("notify_on_complete") is True:
                                terminal_records.append(
                                    self._build_recovered_terminal_record(
                                        entry, "pid_identity_mismatch"
                                    )
                                )
                            continue
                        adopted = dict(entry)
                        adopted["checkpoint_owner_id"] = owner_id
                        adopted["checkpoint_owner_pid"] = owner_pid
                        adopted["checkpoint_owner_start_token"] = owner_token
                        survivors.append(adopted)
                        adopted_entries.append(adopted)
                        classifications.append((session_id, "eligible_recovered"))

                    survivors = sorted(
                        survivors, key=lambda entry: entry["session_id"]
                    )
                    if len(classifications) != len(entries):
                        raise ManagedProcessRecoveryAmbiguous(
                            "managed checkpoint classification is incomplete"
                        )
                    if terminal_records:
                        with _process_authority_lock(notifications_path):
                            existing_events, notification_identity = (
                                self._read_completion_outbox_snapshot_locked()
                            )
                            merged_events = dict(existing_events)
                            for terminal_record in terminal_records:
                                self._validate_completion_record(
                                    terminal_record["event_id"], terminal_record
                                )
                                self._validate_managed_completion_record(
                                    terminal_record
                                )
                                event_id = terminal_record["event_id"]
                                existing_record = merged_events.get(event_id)
                                if existing_record is None:
                                    merged_events[event_id] = terminal_record
                                elif existing_record != terminal_record:
                                    raise ManagedProcessRecoveryAmbiguous(
                                        "managed terminal event identity collision"
                                    )
                            now = time.time()
                            for event_id, record in list(merged_events.items()):
                                if (
                                    record.get("delivered") is True
                                    and now
                                    - float(
                                        record.get("delivered_at")
                                        or record["created_at"]
                                    )
                                    > COMPLETION_OUTBOX_DELIVERED_TTL_SECONDS
                                ):
                                    merged_events.pop(event_id)
                            if len(merged_events) > MAX_COMPLETION_OUTBOX_RECORDS:
                                raise ManagedProcessRecoveryAmbiguous(
                                    "managed completion outbox capacity is exhausted"
                                )
                            encoded_outbox = json.dumps(
                                {
                                    "version": COMPLETION_OUTBOX_VERSION,
                                    "events": merged_events,
                                },
                                ensure_ascii=False,
                                allow_nan=False,
                            ).encode("utf-8")
                            if len(encoded_outbox) > MAX_COMPLETION_OUTBOX_BYTES:
                                raise ManagedProcessRecoveryAmbiguous(
                                    "managed completion outbox byte budget is exhausted"
                                )
                            _process_authority_atomic_write(
                                notifications_path,
                                {
                                    "version": COMPLETION_OUTBOX_VERSION,
                                    "events": merged_events,
                                },
                                expected=notification_identity,
                                max_bytes=MAX_COMPLETION_OUTBOX_BYTES,
                                sort_keys=True,
                            )
                        if crash_hook is not None:
                            crash_hook("after_terminal_outbox_commit")
                    if checkpoint_before.identity is not None and (
                        survivors != checkpoint_before.value
                    ):
                        try:
                            _process_authority_atomic_write(
                                checkpoint_path,
                                survivors,
                                expected=checkpoint_before.identity,
                                max_bytes=MAX_CHECKPOINT_BYTES,
                                sort_keys=True,
                            )
                        except Exception as exc:
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed checkpoint commit failed"
                            ) from exc
                    checkpoint_after = _read_private_json_receipt(
                        checkpoint_path,
                        max_bytes=MAX_CHECKPOINT_BYTES,
                        missing_ok=True,
                    )
                    expected_after = (
                        []
                        if checkpoint_before.identity is None
                        else survivors
                    )
                    if (
                        checkpoint_after.identity is None
                        and checkpoint_before.identity is not None
                    ) or (
                        checkpoint_after.identity is not None
                        and checkpoint_after.value != expected_after
                    ):
                        raise ManagedProcessRecoveryAmbiguous(
                            "managed checkpoint post-snapshot is inconsistent"
                        )

                if crash_hook is not None:
                    crash_hook("after_checkpoint_commit")

                for entry in adopted_entries:
                    session_id = entry["session_id"]
                    with self._lock:
                        existing = self._running.get(session_id)
                        if existing is None:
                            session = self._managed_session_from_entry(entry)
                            self._running[session_id] = session
                            recovered_ids.append(session_id)
                        elif (
                            existing.pid == entry["pid"]
                            and existing.process_start_token
                            == entry["process_start_token"]
                        ):
                            session = existing
                            deduped_process_ids.append(session_id)
                        else:
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed process registry identity collides"
                            )
                    if session.watcher_interval > 0 and not any(
                        watcher.get("session_id") == session.id
                        for watcher in self.pending_watchers
                    ):
                        self.pending_watchers.append(
                            {
                                "session_id": session.id,
                                "check_interval": session.watcher_interval,
                                "session_key": session.session_key,
                                "platform": session.watcher_platform,
                                "chat_id": session.watcher_chat_id,
                                "user_id": session.watcher_user_id,
                                "user_name": session.watcher_user_name,
                                "thread_id": session.watcher_thread_id,
                                "message_id": session.watcher_message_id,
                                "notify_on_complete": (
                                    session.notify_on_complete
                                ),
                            }
                        )

                if crash_hook is not None:
                    crash_hook("after_registry_publish")

                with _process_authority_lock(notifications_path):
                    notifications = _read_private_json_receipt(
                        notifications_path,
                        max_bytes=MAX_COMPLETION_OUTBOX_BYTES,
                        missing_ok=True,
                    )
                    if notifications.identity is None:
                        events: Dict[str, Dict[str, Any]] = {}
                    else:
                        raw = notifications.value
                        if (
                            not isinstance(raw, dict)
                            or raw.get("version") != COMPLETION_OUTBOX_VERSION
                            or not isinstance(raw.get("events"), dict)
                        ):
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed completion outbox schema is invalid"
                            )
                        if len(raw["events"]) > MAX_COMPLETION_OUTBOX_RECORDS:
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed completion outbox record budget exceeded"
                            )
                        try:
                            events = {
                                event_id: self._validate_completion_record(
                                    event_id, record
                                )
                                for event_id, record in raw["events"].items()
                                if isinstance(event_id, str)
                            }
                        except Exception as exc:
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed completion record is invalid"
                            ) from exc
                        if len(events) != len(raw["events"]):
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed completion event identity is invalid"
                            )
                        for record in events.values():
                            self._validate_managed_completion_record(record)
                        if any(
                            len(event_id.encode("utf-8")) > 512
                            or not math.isfinite(
                                float(record["created_at"])
                            )
                            for event_id, record in events.items()
                        ):
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed completion event is unbounded or invalid"
                            )

                    self._completion_outbox = events
                    self._completion_outbox_loaded = True
                    self._completion_outbox_available = True
                    completion_event_ids = tuple(
                        event_id
                        for event_id, record in sorted(events.items())
                        if record.get("delivered") is not True
                    )
                    for event_id in completion_event_ids:
                        if event_id in self._completion_outbox_replayed:
                            deduped_event_ids.append(event_id)
                            continue
                        try:
                            self.completion_queue.put(
                                self._public_completion_event(events[event_id])
                            )
                        except Exception as exc:
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed completion event could not be queued"
                            ) from exc
                        self._completion_outbox_replayed.add(event_id)
                        queued_event_ids.append(event_id)

                    if crash_hook is not None:
                        crash_hook("after_notification_queue")

                    notifications_after = _read_private_json_receipt(
                        notifications_path,
                        max_bytes=MAX_COMPLETION_OUTBOX_BYTES,
                        missing_ok=True,
                    )
                    if notifications_after != notifications:
                        raise ManagedProcessRecoveryAmbiguous(
                            "managed completion outbox changed during recovery"
                        )

                if not _authority_receipt_is_current(
                    checkpoint_path, checkpoint_after
                ) or not _authority_receipt_is_current(
                    notifications_path, notifications_after
                ):
                    raise ManagedProcessRecoveryAmbiguous(
                        "managed recovery authority changed after post-snapshot"
                    )
                with self._lock:
                    for entry in adopted_entries:
                        session = self._running.get(entry["session_id"])
                        if (
                            session is None
                            or session.pid != entry["pid"]
                            or session.process_start_token
                            != entry["process_start_token"]
                        ):
                            raise ManagedProcessRecoveryAmbiguous(
                                "managed process registry postcondition failed"
                            )
                if any(
                    event_id not in self._completion_outbox_replayed
                    for event_id in completion_event_ids
                ):
                    raise ManagedProcessRecoveryAmbiguous(
                        "managed completion queue postcondition failed"
                    )
                self._checkpoint_write_blocked = False
                self._process_checkpoint_available = True
                self._process_checkpoint_reason = "verified"
                with self._lock:
                    self._foreign_owner_active = sum(
                        classification == "foreign_owner_active"
                        for _session_id, classification in classifications
                    )

        classifications_tuple = tuple(sorted(classifications))
        recovered_tuple = tuple(sorted(recovered_ids))
        deduped_process_tuple = tuple(sorted(deduped_process_ids))
        queued_tuple = tuple(sorted(queued_event_ids))
        deduped_event_tuple = tuple(sorted(deduped_event_ids))
        absent = (
            checkpoint_before.identity is None
            and notifications.identity is None
        )
        canonical = json.dumps(
            {
                "checkpoint_path": str(checkpoint_path),
                "checkpoint_before": (
                    checkpoint_before.identity.__dict__
                    if checkpoint_before.identity is not None
                    else None
                ),
                "checkpoint_before_sha256": checkpoint_before.sha256,
                "checkpoint_after": (
                    checkpoint_after.identity.__dict__
                    if checkpoint_after.identity is not None
                    else None
                ),
                "checkpoint_after_sha256": checkpoint_after.sha256,
                "notifications_path": str(notifications_path),
                "notifications": (
                    notifications_after.identity.__dict__
                    if notifications_after.identity is not None
                    else None
                ),
                "notifications_sha256": notifications_after.sha256,
                "process_pid": process_pid,
                "process_start_token": process_token,
                "registry_epoch": registry_epoch,
                "classifications": classifications_tuple,
                "recovered": recovered_tuple,
                "deduped_processes": deduped_process_tuple,
                "completion_events": completion_event_ids,
                "queued_events": queued_tuple,
                "deduped_events": deduped_event_tuple,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return ManagedProcessRecoveryReceipt(
            (
                ManagedProcessRecoveryOutcome.PROVED_ABSENT
                if absent
                else ManagedProcessRecoveryOutcome.PROVED_COMPLETE
            ),
            str(checkpoint_path),
            checkpoint_before.identity,
            checkpoint_before.sha256,
            checkpoint_after.identity,
            checkpoint_after.sha256,
            str(notifications_path),
            notifications_after.identity,
            notifications_after.sha256,
            process_pid,
            process_token,
            registry_epoch,
            classifications_tuple,
            recovered_tuple,
            deduped_process_tuple,
            completion_event_ids,
            queued_tuple,
            deduped_event_tuple,
            hashlib.sha256(canonical).hexdigest(),
        )


# Module-level singleton
process_registry = ProcessRegistry()


def _format_age(seconds: float) -> str:
    """Human-friendly elapsed string ('18m', '2h3m', '45s')."""
    try:
        s = int(max(0, seconds))
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m" if s == 0 else f"{m}m{s}s"
    h, m = divmod(m, 60)
    return f"{h}h" if m == 0 else f"{h}h{m}m"


def _format_async_delegation(evt: dict) -> str:
    """Format an async-delegation completion into a self-contained re-injection.

    Carries the FULL original task source (goal, the context the parent
    supplied, toolsets, role, model) plus dispatch time, status, and the
    complete result summary. When this re-enters the conversation the agent
    may be deep in unrelated context and won't remember why the subagent
    existed, so the block is written to stand entirely on its own — enough to
    use the result OR re-dispatch if the world has moved on.
    """
    import time as _time

    deleg_id = evt.get("delegation_id", "unknown")
    goal = evt.get("goal", "") or ""
    context = evt.get("context")
    toolsets = evt.get("toolsets")
    role = evt.get("role") or "leaf"
    model = evt.get("model") or "?"
    status = evt.get("status") or "completed"
    summary = evt.get("summary")
    error = evt.get("error")
    api_calls = evt.get("api_calls", 0)
    duration = evt.get("duration_seconds", "?")
    dispatched_at = evt.get("dispatched_at")
    completed_at = evt.get("completed_at") or _time.time()

    # ----- Batch (fan-out) completion: consolidated multi-task block -----
    # A whole delegate_task fan-out dispatched as one background unit finishes
    # together and carries a per-task `results` list. Render every subagent's
    # summary in one block so the model gets the consolidated outcome at once.
    batch_results = evt.get("results")
    if evt.get("is_batch") or isinstance(batch_results, list):
        results = batch_results or []
        goals = evt.get("goals") or []
        n = len(results) if results else len(goals)
        total_dur = evt.get("total_duration_seconds", duration)
        lines = [
            f"[ASYNC DELEGATION BATCH COMPLETE — {deleg_id}]",
            f"A background fan-out of {n} subagent(s) you dispatched earlier "
            "has finished. All ran in parallel and waited on each other; their "
            "consolidated results are below. You may have moved on since "
            "dispatching — act on these or re-dispatch if things have changed.",
            "",
        ]
        if isinstance(dispatched_at, (int, float)):
            ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(dispatched_at))
            age = f" ({_format_age(completed_at - dispatched_at)} ago)"
            lines.append(f"Dispatched: {ts}{age}")
        if context:
            lines.append(f"Context you provided: {context}")
        if toolsets:
            lines.append(f"Toolsets: {', '.join(toolsets)}")
        lines.append(f"Role: {role}   Model: {model}   Total duration: {total_dur}s")
        if error and not results:
            lines.append("--- ERROR ---")
            lines.append(f"The batch did not complete successfully: {error}")
            return "\n".join(lines)
        for r in sorted(results, key=lambda x: x.get("task_index", 0)):
            idx = r.get("task_index", 0)
            r_status = r.get("status", "?")
            r_summary = r.get("summary")
            r_error = r.get("error")
            r_goal = goals[idx] if idx < len(goals) else r.get("goal", "")
            icon = "✓" if r_status in ("completed", "success") else "✗"
            lines.append("")
            header = f"--- {icon} TASK {idx + 1}/{n}"
            if r_goal:
                header += f": {r_goal}"
            header += f"  (status={r_status}"
            if r.get("api_calls"):
                header += f", api_calls={r['api_calls']}"
            if r.get("duration_seconds") is not None:
                header += f", {r['duration_seconds']}s"
            header += ") ---"
            lines.append(header)
            if r_status in ("completed", "success") and r_summary:
                lines.append(r_summary)
            elif r_summary:
                if r_error:
                    lines.append(f"({r_status}: {r_error})")
                lines.append("Partial output:")
                lines.append(r_summary)
            else:
                lines.append(
                    f"(no summary — status={r_status}"
                    + (f": {r_error}" if r_error else "")
                    + ")"
                )
        return "\n".join(lines)

    age = ""
    if isinstance(dispatched_at, (int, float)):
        age = f" ({_format_age(completed_at - dispatched_at)} ago)"

    lines = [
        f"[ASYNC DELEGATION COMPLETE — {deleg_id}]",
        "A background subagent you dispatched earlier has finished. You may "
        "have moved on since dispatching it; the full task source is below so "
        "you can act on the result or re-dispatch if things have changed.",
        "",
    ]
    if isinstance(dispatched_at, (int, float)):
        ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(dispatched_at))
        lines.append(f"Dispatched: {ts}{age}")
    lines.append(f"Original goal: {goal}")
    if context:
        lines.append(f"Context you provided: {context}")
    if toolsets:
        lines.append(f"Toolsets: {', '.join(toolsets)}")
    lines.append(f"Role: {role}   Model: {model}")
    lines.append(f"Status: {status}   API calls: {api_calls}   Duration: {duration}s")
    lines.append("--- RESULT ---")
    if status in ("completed", "success") and summary:
        lines.append(summary)
    elif status == "interrupted":
        lines.append(
            "The subagent was interrupted before completing"
            + (f": {error}" if error else ".")
        )
        if summary:
            lines.append("Partial output:")
            lines.append(summary)
    else:
        # error / timeout / failed
        lines.append(
            f"The subagent did not complete successfully (status={status})."
            + (f"\n{error}" if error else "")
        )
        if summary:
            lines.append("Partial output:")
            lines.append(summary)
    return "\n".join(lines)


def format_process_notification(evt: dict) -> "str | None":
    """Format a process notification event into a [IMPORTANT: ...] message.

    Handles completion events (notify_on_complete), watch pattern matches,
    and watch disabled events from the unified completion_queue.
    """
    evt_type = evt.get("type", "completion")
    _sid = evt.get("session_id", "unknown")
    _cmd = evt.get("command", "unknown")

    if evt_type == "watch_disabled":
        return f"[IMPORTANT: {evt.get('message', '')}]"

    if evt_type == "watch_match":
        _pat = evt.get("pattern", "?")
        _out = evt.get("output", "")
        _sup = evt.get("suppressed", 0)
        text = (
            f"[IMPORTANT: Background process {_sid} matched "
            f"watch pattern \"{_pat}\".\n"
            f"Command: {_cmd}\n"
            f"Matched output:\n{_out}"
        )
        if _sup:
            text += f"\n({_sup} earlier matches were suppressed by rate limit)"
        text += "]"
        return text

    if evt_type == "async_delegation":
        return _format_async_delegation(evt)

    _exit = evt.get("exit_code", "?")
    _out = evt.get("output", "")
    _reason = evt.get("completion_reason") or "exited"
    _source = evt.get("termination_source") or ""
    _signal = ""
    if _exit in {-15, 143, "-15", "143"}:
        _signal = ", SIGTERM"
    if _reason == "killed":
        _status = f"terminated by {_source or 'Hermes'}"
    elif _reason == "lost":
        _status = "marked lost because the process backend disappeared"
    elif _reason == "failed_start":
        _status = "failed to start"
    elif _exit == 0:
        _status = "completed normally"
    else:
        _status = "exited"
    return (
        f"[IMPORTANT: Background process {_sid} {_status} "
        f"(exit code {_exit}{_signal}).\n"
        f"Command: {_cmd}\n"
        f"Output:\n{_out}]"
    )


# ---------------------------------------------------------------------------
# Registry -- the "process" tool schema + handler
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error

PROCESS_SCHEMA = {
    "name": "process",
    "description": (
        "Manage background processes started with terminal(background=true). "
        "Actions: 'list' (show all), 'poll' (check status + new output), "
        "'log' (full output with pagination), 'wait' (block until done or timeout), "
        "'kill' (terminate), 'write' (send raw stdin data without newline), "
        "'submit' (send data + Enter, for answering prompts), 'close' (close stdin/send EOF)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "poll", "log", "wait", "kill", "write", "submit", "close"],
                "description": "Action to perform on background processes"
            },
            "session_id": {
                "type": "string",
                "description": "Process session ID (from terminal background output). Required for all actions except 'list'."
            },
            "data": {
                "type": "string",
                "description": "Text to send to process stdin (for 'write' and 'submit' actions)"
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to block for 'wait' action. Returns partial output on timeout.",
                "minimum": 1
            },
            "offset": {
                "type": "integer",
                "description": "Line offset for 'log' action (default: last 200 lines)"
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to return for 'log' action",
                "minimum": 1
            }
        },
        "required": ["action"]
    }
}


def _redact_process_result(result: dict) -> dict:
    """Redact secrets from background-process output before it reaches the
    model, session.db, and CLI display.

    Mirrors the foreground ``terminal`` redaction (terminal_tool.py) so the
    two surfaces can't diverge — issue #43025 (background output was returned
    verbatim). Respects ``security.redact_secrets`` (no force): output fields
    pass through ``redact_terminal_output`` which picks ``code_file`` based on
    the recorded command (env dumps get the ENV-assignment pass). The command
    string itself is also redacted in case it carried an inline credential.
    """
    if not isinstance(result, dict):
        return result
    from agent.redact import redact_sensitive_text, redact_terminal_output

    command = result.get("command") or ""
    for field in ("output", "output_preview"):
        value = result.get(field)
        if isinstance(value, str) and value:
            result[field] = redact_terminal_output(value, command)
    if isinstance(result.get("command"), str) and result["command"]:
        result["command"] = redact_sensitive_text(result["command"], code_file=True)
    return result


def _handle_process(args, **kw):
    task_id = kw.get("task_id")
    action = args.get("action", "")
    # Coerce to string — some models send session_id as an integer
    session_id = str(args.get("session_id", "")) if args.get("session_id") is not None else ""

    if action == "list":
        # Surface session-scoped background processes (e.g. a forgotten
        # preview server) in addition to this task's own — they share the
        # gateway session_key and can block session reset (#29177).
        try:
            from tools.approval import get_current_session_key
            session_key = get_current_session_key(default="") or ""
        except Exception:
            session_key = ""
        return json.dumps(
            {"processes": process_registry.list_sessions(task_id=task_id, session_key=session_key or None)},
            ensure_ascii=False,
        )
    elif action in {"poll", "log", "wait", "kill", "write", "submit", "close"}:
        if not session_id:
            return tool_error(f"session_id is required for {action}")
        if action == "poll":
            return json.dumps(_redact_process_result(process_registry.poll(session_id)), ensure_ascii=False)
        elif action == "log":
            return json.dumps(_redact_process_result(process_registry.read_log(
                session_id, offset=args.get("offset", 0), limit=args.get("limit", 200))), ensure_ascii=False)
        elif action == "wait":
            return json.dumps(_redact_process_result(process_registry.wait(session_id, timeout=args.get("timeout"))), ensure_ascii=False)
        elif action == "kill":
            return json.dumps(process_registry.kill_process(session_id), ensure_ascii=False)
        elif action == "write":
            return json.dumps(process_registry.write_stdin(session_id, str(args.get("data", ""))), ensure_ascii=False)
        elif action == "submit":
            return json.dumps(process_registry.submit_stdin(session_id, str(args.get("data", ""))), ensure_ascii=False)
        elif action == "close":
            return json.dumps(process_registry.close_stdin(session_id), ensure_ascii=False)
    return tool_error(f"Unknown process action: {action}. Use: list, poll, log, wait, kill, write, submit, close")


registry.register(
    name="process",
    toolset="terminal",
    schema=PROCESS_SCHEMA,
    handler=_handle_process,
    emoji="⚙️",
)
