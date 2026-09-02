#!/usr/bin/python3
"""Create and verify one immutable SQLite state backup.

This file is deliberately self-contained.  It is a backup-only cron artifact:
it does not import the Hermes checkout and it does not run any subprocess.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from typing import Any, Dict, Optional, Sequence
from urllib.parse import quote


DEFAULT_KEEP = 7
# The live database is about 16 GiB.  A 30-second total bound is shorter than
# a normal copy plus two full integrity scans, so use a bounded but operable
# fifteen-minute window for the nightly job.
DEFAULT_DEADLINE_SECONDS = 900.0
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
GENERATION_PREFIX = "state.db.verified-"
STATUS_NAME = "maintenance.latest.json"
FAILED_MARKER_NAME = "maintenance.last-run-failed"
SQLITE_BUSY = getattr(sqlite3, "SQLITE_BUSY", 5)
SQLITE_LOCKED = getattr(sqlite3, "SQLITE_LOCKED", 6)
GENERATION_PATTERN = re.compile(
    r"^state\.db\.verified-\d{8}T\d{6}Z-[0-9a-f]{16}\.db$"
)


class MaintenanceError(RuntimeError):
    """A failed backup operation that must make the cron job nonzero."""


class DeadlineExceeded(MaintenanceError):
    """A backup or integrity check exceeded its deadline."""


class StatusWriteError(MaintenanceError):
    """The latest-run status could not be made durable."""


def _resolve_home(raw_home: Optional[str]) -> Path:
    if raw_home:
        return Path(raw_home).expanduser()
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes"


def _resolve_backup_dir(raw_backup_dir: Optional[str], home: Path) -> Path:
    if raw_backup_dir:
        return Path(raw_backup_dir).expanduser()
    return home / "backups"


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _script_sha256() -> str:
    digest = hashlib.sha256()
    with Path(__file__).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _interpreter_identity() -> Dict[str, str]:
    return {
        "executable": os.path.realpath(sys.executable),
        "version": platform.python_version(),
    }


def _deployment_receipt_reference(home: Path) -> str:
    """Return the deploy-time receipt reference without inspecting Git."""
    return os.environ.get(
        "HERMES_DEPLOYMENT_RECEIPT",
        str(home / "maintenance-deployment.json"),
    )


def _redact_message(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    # Exception text is not trusted operator output.  Keep diagnostics useful
    # while avoiding absolute home/backup paths and common credential labels.
    for token in ("HERMES_HOME", "API_KEY", "TOKEN", "PASSWORD", "SECRET"):
        message = message.replace(token, "[redacted]")
    return message[:400]


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    """Write a small JSON receipt durably, replacing only the receipt path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix="." + path.name + ".",
        suffix=".partial",
        dir=str(path.parent),
    )
    temp_path = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
        _fsync_directory(path.parent)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _write_status(home: Path, payload: Dict[str, Any]) -> None:
    try:
        _atomic_json_write(home / STATUS_NAME, payload)
    except BaseException as exc:
        raise StatusWriteError("could not write latest-run status") from exc


def _write_failure_marker(home: Path, error: str) -> None:
    path = home / FAILED_MARKER_NAME
    fd, raw_tmp = tempfile.mkstemp(
        prefix="." + path.name + ".",
        suffix=".partial",
        dir=str(home),
    )
    temp_path = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(error[:400] + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
        _fsync_directory(home)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _clear_failure_marker(home: Path) -> None:
    path = home / FAILED_MARKER_NAME
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(home)


class _BackupLock:
    def __init__(self, path: Path, timeout_seconds: float):
        self.path = path
        self.timeout_seconds = max(0.0, timeout_seconds)
        self.handle = None

    def __enter__(self) -> "_BackupLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    self.handle.close()
                    self.handle = None
                    raise
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise MaintenanceError("backup lock acquisition timed out")
                time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise DeadlineExceeded("maintenance deadline exceeded")


def _sqlite_uri(path: Path, *, immutable: bool) -> str:
    encoded = quote(str(path.resolve()), safe="/")
    suffix = "mode=ro"
    if immutable:
        suffix += "&immutable=1"
    return "file:" + encoded + "?" + suffix


def _open_readonly(path: Path, *, immutable: bool, timeout_seconds: float):
    timeout = max(0.001, timeout_seconds)
    return sqlite3.connect(
        _sqlite_uri(path, immutable=immutable),
        uri=True,
        timeout=timeout,
    )


def _quick_check(
    path: Path,
    *,
    immutable: bool,
    deadline: float,
) -> None:
    """Run the complete read-only quick_check result set before publication."""
    _check_deadline(deadline)
    timeout_seconds = max(0.001, deadline - time.monotonic())
    conn = _open_readonly(
        path,
        immutable=immutable,
        timeout_seconds=timeout_seconds,
    )
    try:
        conn.execute("PRAGMA busy_timeout = " + str(int(timeout_seconds * 1000)))

        def progress() -> int:
            return 1 if time.monotonic() >= deadline else 0

        conn.set_progress_handler(progress, 1000)
        rows = conn.execute("PRAGMA quick_check").fetchall()
        if not rows:
            raise MaintenanceError("quick_check returned no rows")
        failures = [str(row[0]) for row in rows if str(row[0]).lower() != "ok"]
        if failures:
            raise MaintenanceError("quick_check failed: " + "; ".join(failures[:3]))
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower() or time.monotonic() >= deadline:
            raise DeadlineExceeded("quick_check deadline exceeded") from exc
        raise MaintenanceError("quick_check failed") from exc
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


def _create_stage(backup_dir: Path) -> Path:
    fd, raw_path = tempfile.mkstemp(
        prefix="." + GENERATION_PREFIX,
        suffix=".partial",
        dir=str(backup_dir),
    )
    stage = Path(raw_path)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return stage


def _backup_to_stage(live_db: Path, stage: Path, *, deadline: float) -> None:
    _check_deadline(deadline)
    timeout_seconds = max(0.001, deadline - time.monotonic())
    source = _open_readonly(
        live_db,
        immutable=False,
        timeout_seconds=timeout_seconds,
    )
    destination = None
    try:
        destination = sqlite3.connect(str(stage), timeout=timeout_seconds)

        def progress(status: int, remaining: int, total: int) -> None:
            if status in (SQLITE_BUSY, SQLITE_LOCKED):
                _check_deadline(deadline)
            _check_deadline(deadline)

        source.backup(
            destination,
            pages=256,
            progress=progress,
            sleep=0.05,
        )
        destination.commit()
    except sqlite3.OperationalError as exc:
        if time.monotonic() >= deadline or "busy" in str(exc).lower():
            raise DeadlineExceeded("SQLite backup deadline exceeded") from exc
        raise MaintenanceError("SQLite online backup failed") from exc
    finally:
        if destination is not None:
            destination.close()
        source.close()
    _fsync_file(stage)


def _generation_name() -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return GENERATION_PREFIX + stamp + "-" + secrets.token_hex(8) + ".db"


def _publish_stage(stage: Path, backup_dir: Path, *, deadline: float) -> Path:
    """Publish by hard-linking the staged inode; never clobber a generation."""
    while True:
        _check_deadline(deadline)
        final = backup_dir / _generation_name()
        linked = False
        try:
            # The stage was created with O_CREAT|O_EXCL.  os.link is atomic and
            # fails with FileExistsError instead of replacing a prior copy.
            os.link(str(stage), str(final))
            linked = True
            _fsync_file(final)
            _fsync_directory(backup_dir)
            stage.unlink()
            _fsync_directory(backup_dir)
            return final
        except FileExistsError:
            continue
        except BaseException:
            # Do not leave an unacknowledged generation after a durability
            # failure between link and the successful return.
            if linked:
                try:
                    final.unlink()
                    _fsync_directory(backup_dir)
                except OSError:
                    pass
            raise


def _discard(path: Optional[Path], backup_dir: Path) -> None:
    """Remove only a stage or newly-created generation after a failed run."""
    if path is None or not path.exists():
        return
    try:
        path.unlink()
        _fsync_directory(backup_dir)
    except OSError:
        # The caller will still return nonzero and print a redacted fallback.
        pass


def _verified_generations(backup_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in backup_dir.iterdir()
        if path.is_file() and GENERATION_PATTERN.fullmatch(path.name)
    )


def _rotate(backup_dir: Path, keep: int) -> None:
    generations = _verified_generations(backup_dir)
    if len(generations) <= keep:
        return
    for path in generations[: len(generations) - keep]:
        path.unlink()
    _fsync_directory(backup_dir)


def _success_status(home: Path, generation: Path) -> Dict[str, Any]:
    return {
        "ok": True,
        "finished_at": _now_utc(),
        "generation": generation.name,
        "generation_sha256": _file_sha256(generation),
        "script_sha256": _script_sha256(),
        "interpreter": _interpreter_identity(),
        "deployment_receipt": _deployment_receipt_reference(home),
        "checks": {"staged_quick_check": "ok", "live_quick_check": "ok"},
    }


def _failure_status(home: Path, exc: BaseException) -> Dict[str, Any]:
    return {
        "ok": False,
        "finished_at": _now_utc(),
        "error": _redact_message(exc),
        "script_sha256": _script_sha256(),
        "interpreter": _interpreter_identity(),
        "deployment_receipt": _deployment_receipt_reference(home),
    }


def _record_failure(home: Path, exc: BaseException) -> None:
    message = _redact_message(exc)
    status_error = None
    try:
        _write_status(home, _failure_status(home, exc))
    except BaseException as status_exc:
        status_error = status_exc
    try:
        _write_failure_marker(home, message)
    except BaseException as marker_exc:
        if status_error is None:
            status_error = marker_exc
    if status_error is not None:
        print(
            "hermes daily maintenance failed; could not write failure receipt "
            "or marker; see process exit status",
            file=sys.stderr,
        )


def run(
    *,
    home: Path,
    backup_dir: Path,
    keep: int,
    deadline_seconds: float,
    lock_timeout_seconds: float,
) -> int:
    if keep < 1:
        raise MaintenanceError("--keep must be at least 1")
    home.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    live_db = home / "state.db"
    if not live_db.is_file():
        raise MaintenanceError("live state database is missing")

    stage = None
    generation = None
    preserve_generation = False
    deadline = time.monotonic() + max(0.0, deadline_seconds)
    lock = _BackupLock(home / ".backup.lock", lock_timeout_seconds)
    try:
        lock.__enter__()
    except BaseException as exc:
        _record_failure(home, exc)
        print("hermes daily maintenance failed: " + _redact_message(exc), file=sys.stderr)
        return 1
    try:
        _check_deadline(deadline)
        stage = _create_stage(backup_dir)
        _backup_to_stage(live_db, stage, deadline=deadline)
        _quick_check(stage, immutable=True, deadline=deadline)
        _quick_check(live_db, immutable=False, deadline=deadline)
        generation = _publish_stage(stage, backup_dir, deadline=deadline)
        stage = None

        # The receipt is durable before rotation.  If it cannot be written,
        # the newly published copy is discarded and prior generations stay.
        try:
            _write_status(home, _success_status(home, generation))
        except BaseException as exc:
            _discard(generation, backup_dir)
            generation = None
            raise StatusWriteError("could not write successful status receipt") from exc
        # From this point the new generation and receipt are durable.  A later
        # rotation or marker failure must not discard that verified backup.
        preserve_generation = True
        _rotate(backup_dir, keep)
        _clear_failure_marker(home)
        return 0
    except BaseException as exc:
        _discard(stage, backup_dir)
        if not preserve_generation:
            _discard(generation, backup_dir)
        _record_failure(home, exc)
        print("hermes daily maintenance failed: " + _redact_message(exc), file=sys.stderr)
        return 1
    finally:
        lock.__exit__(None, None, None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", help="HERMES_HOME directory")
    parser.add_argument("--backup-dir", help="directory for verified generations")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_DEADLINE_SECONDS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--lock-timeout-seconds",
        type=float,
        default=DEFAULT_LOCK_TIMEOUT_SECONDS,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    home = _resolve_home(args.home)
    backup_dir = _resolve_backup_dir(args.backup_dir, home)
    try:
        return run(
            home=home,
            backup_dir=backup_dir,
            keep=args.keep,
            deadline_seconds=args.deadline_seconds,
            lock_timeout_seconds=args.lock_timeout_seconds,
        )
    except BaseException as exc:
        _record_failure(home, exc)
        print("hermes daily maintenance failed: " + _redact_message(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
