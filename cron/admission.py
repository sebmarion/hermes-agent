"""Profile-scoped, cross-process cron admission fence.

Every cron firing path must acquire a durable lease here *before* it mutates
``jobs.json``.  The external drain watcher closes the same gate and reads the
gate epoch plus active leases under the same file lock.  Therefore a zero-work
receipt is a linearization point: an entrant is either present in that receipt
or observes the closed gate and is rejected.

The lock is deliberately separate from ``.jobs.lock``.  Cron workers need the
jobs lock while settling a run, so holding that lock for the run lifetime would
deadlock drain.  Admission leases are short state mutations under
``.admission.lock``; the actual job runs without holding either file lock.
"""

from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from typing import Any, Callable, TypeVar
import uuid

from hermes_constants import get_hermes_home

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None


CRON_ADMISSION_SCHEMA = "hermes.cron_admission.v1"
_LOCK_FILENAME = ".admission.lock"
_STATE_FILENAME = ".admission.json"
_MAX_STATE_BYTES = 1024 * 1024
_LOCK_TIMEOUT_SECONDS = 30.0
_MAX_LEASES = 4096
_SAFE_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")
_HEX_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_STATE_KEYS = {
    "schema",
    "gate_epoch",
    "accepting",
    "marker_pause",
    "manual_pause",
    "leases",
    "updated_at",
}
_LEASE_KEYS = {
    "token",
    "job_id",
    "source",
    "pid",
    "process_start_token",
    "admitted_at",
    "gate_epoch",
}
_MANUAL_PAUSE_KEYS = {
    "pid",
    "process_start_token",
    "reason",
    "paused_at",
}

_process_lock = threading.RLock()
_transaction_state = threading.local()
_release_retry_lock = threading.Lock()
_release_retry_wakeup = threading.Event()
_release_retry_pending: dict[str, "CronAdmissionLease"] = {}
_release_retry_thread: threading.Thread | None = None
_T = TypeVar("_T")


class CronAdmissionUnavailable(RuntimeError):
    """The gate could not be verified securely; admissions must fail closed."""


class CronAdmissionClosed(RuntimeError):
    """A manual cron request was rejected by the profile-wide drain fence."""


@dataclass(frozen=True)
class CronAdmissionLease:
    token: str
    job_id: str
    source: str
    pid: int
    process_start_token: str
    admitted_at: str
    gate_epoch: int


@dataclass(frozen=True)
class CronAdmissionSelection:
    """One value returned by a jobs-store selection callback."""

    job_id: str
    source: str
    value: Any


@dataclass
class _AdmissionStateTransaction:
    """Explicit persistence boundary while the admission lock is held."""

    state_path: Path
    state: dict[str, Any]
    persisted: dict[str, Any]

    def persist(self) -> None:
        if self.state == self.persisted:
            return
        self.state["updated_at"] = _utc_now()
        _secure_write_state(self.state_path, self.state)
        self.persisted = copy.deepcopy(self.state)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return (
        parsed.utcoffset() == timezone.utc.utcoffset(parsed)
        and parsed.astimezone(timezone.utc).isoformat() == value
    )


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and _SAFE_TEXT_RE.fullmatch(value) is not None


def _current_process_identity() -> tuple[int, str]:
    pid = os.getpid()
    try:
        from gateway.status import get_process_start_token

        token = get_process_start_token(pid)
    except Exception as exc:  # pragma: no cover - import/runtime corruption
        raise CronAdmissionUnavailable(
            "process start identity is unavailable"
        ) from exc
    if not isinstance(token, str) or not token:
        raise CronAdmissionUnavailable("process start identity is unavailable")
    return pid, token


def _process_identity_state(pid: int, expected_token: str) -> bool | None:
    """Return True for the same live process, False for dead/reused, else None."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return None
        return None

    try:
        from gateway.status import get_process_start_token

        observed = get_process_start_token(pid)
    except Exception:
        return None
    if observed is None:
        return None
    return observed == expected_token


def _cron_dir() -> Path:
    return Path(get_hermes_home()) / "cron"


def _ensure_secure_cron_dir() -> Path:
    path = _cron_dir()
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise CronAdmissionUnavailable("cron admission directory unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise CronAdmissionUnavailable("cron admission directory is not a real directory")
    if os.name != "nt":
        getuid = getattr(os, "geteuid", None)
        if callable(getuid) and info.st_uid != getuid():
            raise CronAdmissionUnavailable("cron admission directory owner mismatch")
        if stat.S_IMODE(info.st_mode) != 0o700:
            try:
                path.chmod(0o700)
                info = path.lstat()
            except OSError as exc:
                raise CronAdmissionUnavailable(
                    "cron admission directory mode is unsafe"
                ) from exc
            if stat.S_IMODE(info.st_mode) != 0o700:
                raise CronAdmissionUnavailable(
                    "cron admission directory mode is unsafe"
                )
    return path


def _secure_open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if isinstance(cloexec, int):
        flags |= cloexec
    if os.name != "nt":
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if not isinstance(nofollow, int) or nofollow == 0:
            raise CronAdmissionUnavailable("O_NOFOLLOW is unavailable")
        flags |= nofollow
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CronAdmissionUnavailable("cron admission lock unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CronAdmissionUnavailable("cron admission lock has unsafe type")
        if os.name != "nt":
            getuid = getattr(os, "geteuid", None)
            if callable(getuid) and info.st_uid != getuid():
                raise CronAdmissionUnavailable("cron admission lock owner mismatch")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
                info = os.fstat(fd)
                if stat.S_IMODE(info.st_mode) != 0o600:
                    raise CronAdmissionUnavailable(
                        "cron admission lock mode is unsafe"
                    )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _acquire_file_lock(fd: int) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    if fcntl is not None:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (OSError, IOError) as exc:
                if getattr(exc, "errno", None) not in {
                    errno.EACCES,
                    errno.EAGAIN,
                }:
                    raise CronAdmissionUnavailable(
                        "cron admission lock failed"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise CronAdmissionUnavailable(
                        "timed out waiting for cron admission lock"
                    ) from exc
                time.sleep(0.05)
    if msvcrt is not None:  # pragma: no cover - Windows
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError as exc:
            raise CronAdmissionUnavailable("cron admission lock failed") from exc
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise CronAdmissionUnavailable(
                        "timed out waiting for cron admission lock"
                    ) from exc
                time.sleep(0.05)
    raise CronAdmissionUnavailable(
        "no supported cross-process cron admission lock is available"
    )


def _release_file_lock(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _default_state() -> dict[str, Any]:
    return {
        "schema": CRON_ADMISSION_SCHEMA,
        "gate_epoch": 1,
        "accepting": True,
        "marker_pause": False,
        "manual_pause": None,
        "leases": {},
        "updated_at": _utc_now(),
    }


def _valid_manual_pause(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _MANUAL_PAUSE_KEYS
        and isinstance(value.get("pid"), int)
        and not isinstance(value.get("pid"), bool)
        and value["pid"] > 1
        and _valid_text(value.get("process_start_token"))
        and _valid_text(value.get("reason"))
        and _valid_utc_timestamp(value.get("paused_at"))
    )


def _valid_lease(value: object, token: str, gate_epoch: int) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _LEASE_KEYS
        and value.get("token") == token
        and _HEX_TOKEN_RE.fullmatch(token) is not None
        and _valid_text(value.get("job_id"))
        and _valid_text(value.get("source"))
        and isinstance(value.get("pid"), int)
        and not isinstance(value.get("pid"), bool)
        and value["pid"] > 1
        and _valid_text(value.get("process_start_token"))
        and _valid_utc_timestamp(value.get("admitted_at"))
        and isinstance(value.get("gate_epoch"), int)
        and not isinstance(value.get("gate_epoch"), bool)
        and 1 <= value["gate_epoch"] <= gate_epoch
    )


def _validate_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _STATE_KEYS:
        raise CronAdmissionUnavailable("cron admission state shape is invalid")
    if value.get("schema") != CRON_ADMISSION_SCHEMA:
        raise CronAdmissionUnavailable("cron admission state schema is invalid")
    gate_epoch = value.get("gate_epoch")
    if (
        isinstance(gate_epoch, bool)
        or not isinstance(gate_epoch, int)
        or gate_epoch < 1
    ):
        raise CronAdmissionUnavailable("cron admission gate epoch is invalid")
    if not isinstance(value.get("accepting"), bool):
        raise CronAdmissionUnavailable("cron admission gate state is invalid")
    if not isinstance(value.get("marker_pause"), bool):
        raise CronAdmissionUnavailable("cron admission marker state is invalid")
    manual_pause = value.get("manual_pause")
    if manual_pause is not None and not _valid_manual_pause(manual_pause):
        raise CronAdmissionUnavailable("cron admission pause owner is invalid")
    if not _valid_utc_timestamp(value.get("updated_at")):
        raise CronAdmissionUnavailable("cron admission update timestamp is invalid")
    leases = value.get("leases")
    if not isinstance(leases, dict) or len(leases) > _MAX_LEASES:
        raise CronAdmissionUnavailable("cron admission lease set is invalid")
    for token, lease in leases.items():
        if not isinstance(token, str) or not _valid_lease(
            lease, token, gate_epoch
        ):
            raise CronAdmissionUnavailable("cron admission lease is invalid")
    expected_accepting = not value["marker_pause"] and manual_pause is None
    if value["accepting"] is not expected_accepting:
        raise CronAdmissionUnavailable("cron admission gate fields disagree")
    return value


def _secure_read_state(path: Path) -> dict[str, Any]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return _default_state()
    except OSError as exc:
        raise CronAdmissionUnavailable("cron admission state unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > _MAX_STATE_BYTES
    ):
        raise CronAdmissionUnavailable("cron admission state has unsafe type")
    if os.name != "nt":
        getuid = getattr(os, "geteuid", None)
        if callable(getuid) and before.st_uid != getuid():
            raise CronAdmissionUnavailable("cron admission state owner mismatch")
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise CronAdmissionUnavailable("cron admission state mode is unsafe")

    flags = os.O_RDONLY
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if isinstance(cloexec, int):
        flags |= cloexec
    if os.name != "nt":
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if not isinstance(nofollow, int) or nofollow == 0:
            raise CronAdmissionUnavailable("O_NOFOLLOW is unavailable")
        flags |= nofollow
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CronAdmissionUnavailable("cron admission state unavailable") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > _MAX_STATE_BYTES
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise CronAdmissionUnavailable(
                "cron admission state identity changed"
            )
        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_STATE_BYTES:
            chunk = os.read(fd, min(65536, _MAX_STATE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _MAX_STATE_BYTES:
            raise CronAdmissionUnavailable("cron admission state is oversized")
        raw = b"".join(chunks)
        current = path.lstat()
        if current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
            raise CronAdmissionUnavailable(
                "cron admission state identity changed"
            )
    except OSError as exc:
        raise CronAdmissionUnavailable("cron admission state read failed") from exc
    finally:
        os.close(fd)
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise CronAdmissionUnavailable("cron admission state is malformed") from exc
    return _validate_state(decoded)


def _canonical_state_bytes(state: dict[str, Any]) -> bytes:
    _validate_state(state)
    return (
        json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    # Windows does not support opening directories through the CRT fd API.
    # os.replace() is still atomic there; skip only the POSIX durability flush.
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        return
    flags = os.O_RDONLY
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if isinstance(directory_flag, int):
        flags |= directory_flag
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.name != "nt" and isinstance(nofollow, int):
        flags |= nofollow
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CronAdmissionUnavailable(
            "cron admission directory fsync unavailable"
        ) from exc
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _secure_write_state(path: Path, state: dict[str, Any]) -> None:
    raw = _canonical_state_bytes(state)
    if len(raw) > _MAX_STATE_BYTES:
        raise CronAdmissionUnavailable("cron admission state is oversized")
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    except OSError as exc:
        raise CronAdmissionUnavailable("cron admission state unavailable") from exc
    if current is not None and (
        not stat.S_ISREG(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or current.st_nlink != 1
    ):
        raise CronAdmissionUnavailable("cron admission state has unsafe type")

    fd, temporary_name = tempfile.mkstemp(
        prefix=".admission-state-",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        written = 0
        while written < len(raw):
            written += os.write(fd, raw[written:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise CronAdmissionUnavailable("cron admission state write failed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _external_admission_rejection_requested() -> bool:
    try:
        from gateway.drain_control import admission_rejection_requested

        return bool(admission_rejection_requested())
    except Exception:
        # An unreadable gate can never be treated as permission to dispatch.
        return True


def _reconcile_state(state: dict[str, Any]) -> None:
    changed = False
    leases = state["leases"]
    for token, lease in list(leases.items()):
        alive = _process_identity_state(
            lease["pid"], lease["process_start_token"]
        )
        if alive is False:
            del leases[token]
            changed = True

    manual_pause = state["manual_pause"]
    if manual_pause is not None:
        owner_alive = _process_identity_state(
            manual_pause["pid"], manual_pause["process_start_token"]
        )
        if owner_alive is False:
            state["manual_pause"] = None
            changed = True

    marker_pause = _external_admission_rejection_requested()
    if marker_pause != state["marker_pause"]:
        state["marker_pause"] = marker_pause
        changed = True

    accepting = not marker_pause and state["manual_pause"] is None
    if accepting != state["accepting"]:
        state["accepting"] = accepting
        state["gate_epoch"] += 1
        changed = True
    if changed:
        state["updated_at"] = _utc_now()


@contextlib.contextmanager
def _locked_state_transaction():
    """Hold the process/file lock without implicitly committing mutations."""

    if getattr(_transaction_state, "depth", 0):
        raise RuntimeError("nested explicit cron admission transactions are unsupported")
    with _process_lock:
        cron_dir = _ensure_secure_cron_dir()
        lock_fd = _secure_open_lock(cron_dir / _LOCK_FILENAME)
        try:
            _acquire_file_lock(lock_fd)
            state_path = cron_dir / _STATE_FILENAME
            state = _secure_read_state(state_path)
            persisted = copy.deepcopy(state)
            _reconcile_state(state)
            transaction = _AdmissionStateTransaction(
                state_path=state_path,
                state=state,
                persisted=persisted,
            )
            _transaction_state.depth = 1
            _transaction_state.value = state
            _transaction_state.transaction = transaction
            try:
                yield transaction
            finally:
                _transaction_state.depth = 0
                _transaction_state.value = None
                _transaction_state.transaction = None
        finally:
            _release_file_lock(lock_fd)
            os.close(lock_fd)


@contextlib.contextmanager
def _locked_state():
    depth = getattr(_transaction_state, "depth", 0)
    if depth:
        _transaction_state.depth = depth + 1
        try:
            yield _transaction_state.value
        finally:
            _transaction_state.depth -= 1
        return

    with _locked_state_transaction() as transaction:
        try:
            yield transaction.state
        finally:
            transaction.persist()


def _lease_from_record(record: dict[str, Any]) -> CronAdmissionLease:
    return CronAdmissionLease(
        token=record["token"],
        job_id=record["job_id"],
        source=record["source"],
        pid=record["pid"],
        process_start_token=record["process_start_token"],
        admitted_at=record["admitted_at"],
        gate_epoch=record["gate_epoch"],
    )


def _new_lease_record(
    *,
    state: dict[str, Any],
    job_id: str,
    source: str,
    pid: int,
    process_start_token: str,
) -> dict[str, Any]:
    if not _valid_text(job_id):
        raise CronAdmissionUnavailable("cron job id is unsafe for admission")
    if not _valid_text(source):
        raise CronAdmissionUnavailable("cron admission source is invalid")
    if len(state["leases"]) >= _MAX_LEASES:
        raise CronAdmissionUnavailable("cron admission lease limit reached")
    token = uuid.uuid4().hex
    return {
        "token": token,
        "job_id": job_id,
        "source": source,
        "pid": pid,
        "process_start_token": process_start_token,
        "admitted_at": _utc_now(),
        "gate_epoch": state["gate_epoch"],
    }


def _snapshot_from_state(state: dict[str, Any]) -> dict[str, Any]:
    active = sorted(
        (
            {
                "job_id": lease["job_id"],
                "source": lease["source"],
                "pid": lease["pid"],
                "gate_epoch": lease["gate_epoch"],
                "admitted_at": lease["admitted_at"],
            }
            for lease in state["leases"].values()
        ),
        key=lambda item: (
            item["job_id"],
            item["source"],
            item["pid"],
            item["admitted_at"],
        ),
    )
    return {
        "schema": CRON_ADMISSION_SCHEMA,
        "verified": True,
        "accepting": state["accepting"],
        "gate_epoch": state["gate_epoch"],
        "active_count": len(active),
        "active_job_ids": sorted({item["job_id"] for item in active}),
        "active_leases": active,
    }


def _unverified_snapshot(reason: str) -> dict[str, Any]:
    return {
        "schema": CRON_ADMISSION_SCHEMA,
        "verified": False,
        "accepting": False,
        "gate_epoch": None,
        "active_count": None,
        "active_job_ids": None,
        "active_leases": None,
        "reason": reason,
    }


def cron_admission_snapshot() -> dict[str, Any]:
    """Atomically read gate epoch and the complete cross-process active set."""

    try:
        with _locked_state() as state:
            return _snapshot_from_state(state)
    except CronAdmissionUnavailable as exc:
        return _unverified_snapshot(str(exc))


def set_cron_admission_paused(
    paused: bool,
    *,
    reason: str = "gateway-drain",
) -> dict[str, Any]:
    """Close/open manual admission and return the same-lock state receipt."""

    if not _valid_text(reason):
        return _unverified_snapshot("cron admission pause reason is invalid")
    try:
        pid, start_token = _current_process_identity()
        with _locked_state() as state:
            before_accepting = state["accepting"]
            if paused:
                state["manual_pause"] = {
                    "pid": pid,
                    "process_start_token": start_token,
                    "reason": reason,
                    "paused_at": _utc_now(),
                }
            else:
                owner = state["manual_pause"]
                if (
                    owner is not None
                    and owner["pid"] == pid
                    and owner["process_start_token"] == start_token
                ):
                    state["manual_pause"] = None
            accepting = not state["marker_pause"] and state["manual_pause"] is None
            state["accepting"] = accepting
            if accepting != before_accepting:
                state["gate_epoch"] += 1
            state["updated_at"] = _utc_now()
            return _snapshot_from_state(state)
    except CronAdmissionUnavailable as exc:
        return _unverified_snapshot(str(exc))


def claim_cron_admission(
    job_id: str,
    *,
    source: str,
) -> CronAdmissionLease | None:
    """Atomically admit one job, or return ``None`` when fenced/deduplicated."""

    try:
        pid, start_token = _current_process_identity()
        with _locked_state() as state:
            if not state["accepting"]:
                return None
            if any(
                lease["job_id"] == job_id for lease in state["leases"].values()
            ):
                return None
            record = _new_lease_record(
                state=state,
                job_id=job_id,
                source=source,
                pid=pid,
                process_start_token=start_token,
            )
            state["leases"][record["token"]] = record
            state["updated_at"] = _utc_now()
            return _lease_from_record(record)
    except CronAdmissionUnavailable:
        return None


def _release_cron_admission_once(lease: CronAdmissionLease) -> bool:
    """Attempt one exact release; infrastructure failures remain observable."""

    with _locked_state() as state:
        current = state["leases"].get(lease.token)
        if not isinstance(current, dict):
            return False
        if (
            current["job_id"] != lease.job_id
            or current["source"] != lease.source
            or current["pid"] != lease.pid
            or current["process_start_token"] != lease.process_start_token
            or current["gate_epoch"] != lease.gate_epoch
        ):
            return False
        del state["leases"][lease.token]
        state["updated_at"] = _utc_now()
        return True


def _release_retry_worker() -> None:
    """Keep live-process leases visible until an exact release can persist."""

    global _release_retry_thread
    while True:
        _release_retry_wakeup.wait(timeout=0.25)
        _release_retry_wakeup.clear()
        with _release_retry_lock:
            pending = list(_release_retry_pending.values())
            if not pending:
                _release_retry_thread = None
                return
        for lease in pending:
            try:
                _release_cron_admission_once(lease)
            except CronAdmissionUnavailable:
                continue
            # False means the exact token is already absent or no longer ours;
            # either way there is no matching durable lease left to release.
            with _release_retry_lock:
                current = _release_retry_pending.get(lease.token)
                if current == lease:
                    _release_retry_pending.pop(lease.token, None)


def _queue_release_retry(lease: CronAdmissionLease) -> None:
    global _release_retry_thread
    with _release_retry_lock:
        _release_retry_pending[lease.token] = lease
        if _release_retry_thread is None or not _release_retry_thread.is_alive():
            _release_retry_thread = threading.Thread(
                target=_release_retry_worker,
                name="cron-admission-release",
                daemon=True,
            )
            _release_retry_thread.start()
    _release_retry_wakeup.set()


def release_cron_admission(lease: CronAdmissionLease) -> bool:
    """Release the exact lease, retrying transient persistence failures."""

    if not isinstance(lease, CronAdmissionLease):
        return False
    for _attempt in range(3):
        try:
            return _release_cron_admission_once(lease)
        except CronAdmissionUnavailable:
            continue
    _queue_release_retry(lease)
    return False


def admit_cron_selection(
    selector: Callable[[frozenset[str]], list[CronAdmissionSelection]],
) -> list[tuple[Any, CronAdmissionLease]]:
    """Admit a jobs-store selection and its mutations as one linearized step.

    ``selector`` runs while the admission lock is held and receives the job IDs
    that were already active before this selection.  It may take the short
    ``jobs.json`` lock to claim recovery/due work, but it must not execute a job.
    Selected leases are persisted before this function releases the admission
    lock, so a concurrently-written drain marker can never yield a zero receipt
    after those jobs were selected.
    """

    possibly_persisted: list[CronAdmissionLease] = []
    committed = False
    try:
        pid, start_token = _current_process_identity()
        with _locked_state_transaction() as transaction:
            state = transaction.state
            if not state["accepting"]:
                transaction.persist()
                return []

            # The admission lock is always acquired before the jobs lock.
            # Store helpers buffer all mutations until the complete lease batch
            # is durable, eliminating both partial selection and lock inversion.
            from cron.jobs import _buffer_cron_jobs_updates

            with _buffer_cron_jobs_updates() as jobs_transaction:
                active_job_ids = frozenset(
                    lease["job_id"] for lease in state["leases"].values()
                )
                selected = selector(active_job_ids)
                if not isinstance(selected, list):
                    raise CronAdmissionUnavailable(
                        "cron admission selector returned an invalid value"
                    )

                seen = set(active_job_ids)
                for item in selected:
                    if not isinstance(item, CronAdmissionSelection):
                        raise CronAdmissionUnavailable(
                            "cron admission selector returned an invalid item"
                        )
                    if not _valid_text(item.job_id) or not _valid_text(item.source):
                        raise CronAdmissionUnavailable(
                            "cron admission selector returned unsafe fields"
                        )
                    # A selector that consumes store state for an already-active
                    # or duplicate job cannot be committed without a new lease.
                    if item.job_id in seen:
                        raise CronAdmissionUnavailable(
                            "cron admission selector returned a duplicate job"
                        )
                    seen.add(item.job_id)

                if len(state["leases"]) + len(selected) > _MAX_LEASES:
                    raise CronAdmissionUnavailable(
                        "cron admission lease limit reached"
                    )

                records: list[tuple[Any, CronAdmissionLease]] = []
                raw_records: list[dict[str, Any]] = []
                for item in selected:
                    record = _new_lease_record(
                        state=state,
                        job_id=item.job_id,
                        source=item.source,
                        pid=pid,
                        process_start_token=start_token,
                    )
                    raw_records.append(record)
                    lease = _lease_from_record(record)
                    possibly_persisted.append(lease)
                    records.append((item.value, lease))

                for record in raw_records:
                    state["leases"][record["token"]] = record
                if records:
                    state["updated_at"] = _utc_now()

                # Persist the complete lease set before jobs.json can expose
                # any selected/advanced state. The admission lock stays held
                # through both writes, so drain cannot observe the midpoint.
                transaction.persist()
                jobs_transaction.commit()
                committed = True
                return records
    except CronAdmissionUnavailable:
        return []
    finally:
        if not committed:
            # A write may have reached disk even when its final durability
            # flush raised. Exact-token release is safe whether it did or not,
            # and its retry worker prevents a live-process orphan from wedging.
            for lease in possibly_persisted:
                release_cron_admission(lease)
