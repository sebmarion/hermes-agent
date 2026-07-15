"""Cheap committed-change detection for local Hermes session databases."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class SessionRevisionProbeError(RuntimeError):
    """Raised when a requested database revision cannot be read safely."""


@dataclass
class _TrackedDatabase:
    connection: sqlite3.Connection | None
    identity: tuple[int, ...]
    epoch: int


class SessionRevisionTracker:
    """Track SQLite revisions using stable, read-only connections."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, Path], _TrackedDatabase] = {}

    def revision(self, targets: Iterable[tuple[str, Path]]) -> str:
        """Return an opaque token for the sorted local profile/database set."""

        normalized_targets = sorted(
            {
                (str(profile), _normalize_path(db_path))
                for profile, db_path in targets
            },
            key=lambda target: (target[0], str(target[1])),
        )

        with self._lock:
            requested = set(normalized_targets)
            for key in tuple(self._entries):
                if key not in requested:
                    self._close_entry(self._entries.pop(key))

            descriptors = [
                self._descriptor(profile, db_path)
                for profile, db_path in normalized_targets
            ]
            payload = json.dumps(
                descriptors,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return hashlib.sha256(payload).hexdigest()

    def close(self) -> None:
        """Close every retained connection; safe to call repeatedly."""

        with self._lock:
            for entry in self._entries.values():
                self._close_entry(entry)
            self._entries.clear()

    def _descriptor(self, profile: str, db_path: Path) -> dict[str, object]:
        key = (profile, db_path)
        entry = self._entries.get(key)

        try:
            identity = _file_identity(db_path)
        except FileNotFoundError as exc:
            if entry is None:
                return {
                    "profile": profile,
                    "path": str(db_path),
                    "present": False,
                }
            self._close_entry(entry)
            raise SessionRevisionProbeError("Session database is unavailable") from exc
        except OSError as exc:
            if entry is not None:
                self._close_entry(entry)
            raise SessionRevisionProbeError("Session database cannot be inspected") from exc

        if entry is None:
            if not _retains_sqlite_connections():
                entry = _TrackedDatabase(None, identity, epoch=1)
                self._entries[key] = entry
            else:
                entry = self._open_entry(db_path, identity, epoch=1)
                self._entries[key] = entry
        elif entry.connection is None or entry.identity != identity:
            self._close_entry(entry)
            if not _retains_sqlite_connections():
                next_epoch = (
                    entry.epoch + 1 if entry.identity != identity else entry.epoch
                )
                entry = _TrackedDatabase(None, identity, epoch=next_epoch)
            else:
                next_epoch = entry.epoch + 1
                entry = self._open_entry(db_path, identity, epoch=next_epoch)
            self._entries[key] = entry

        if not _retains_sqlite_connections():
            return {
                "profile": profile,
                "path": str(db_path),
                "present": True,
                "identity": entry.identity,
                "epoch": entry.epoch,
                "data_version": None,
                "files": {
                    "database": _file_change_fingerprint(
                        db_path,
                        expected_identity=identity,
                        missing_ok=False,
                    ),
                    "wal": _file_change_fingerprint(Path(f"{db_path}-wal")),
                },
            }

        connection = entry.connection
        if connection is None:
            raise SessionRevisionProbeError("Session database connection is unavailable")
        try:
            row = connection.execute("PRAGMA data_version").fetchone()
            if row is None:
                raise sqlite3.DatabaseError("PRAGMA data_version returned no row")
            data_version = int(row[0])
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            self._close_entry(entry)
            raise SessionRevisionProbeError("Session database revision is unavailable") from exc

        return {
            "profile": profile,
            "path": str(db_path),
            "present": True,
            "identity": entry.identity,
            "epoch": entry.epoch,
            "data_version": data_version,
        }

    def _open_entry(
        self, db_path: Path, expected_identity: tuple[int, ...], epoch: int
    ) -> _TrackedDatabase:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{db_path.as_uri()}?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=1.0,
                isolation_level=None,
            )
            actual_identity = _file_identity(db_path)
            if actual_identity != expected_identity:
                raise SessionRevisionProbeError(
                    "Session database changed while opening"
                )
            return _TrackedDatabase(connection, actual_identity, epoch)
        except SessionRevisionProbeError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise SessionRevisionProbeError("Session database cannot be opened") from exc

    @staticmethod
    def _close_entry(entry: _TrackedDatabase) -> None:
        if entry.connection is not None:
            entry.connection.close()
            entry.connection = None


def _normalize_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path.expanduser()))))


def _file_identity(path: Path) -> tuple[int, ...]:
    return _file_identity_from_stat(path.stat())


def _file_identity_from_stat(stat_result: os.stat_result) -> tuple[int, ...]:
    if stat_result.st_ino:
        return (stat_result.st_dev, stat_result.st_ino)
    return (
        stat_result.st_dev,
        stat_result.st_ctime_ns,
        stat_result.st_mtime_ns,
        stat_result.st_size,
    )


def _file_change_fingerprint(
    path: Path,
    *,
    expected_identity: tuple[int, ...] | None = None,
    missing_ok: bool = True,
) -> tuple[object, ...]:
    try:
        stat_result = path.stat()
    except FileNotFoundError as exc:
        if missing_ok:
            return ("absent",)
        raise SessionRevisionProbeError("Session database is unavailable") from exc
    except OSError as exc:
        raise SessionRevisionProbeError(
            "Session database files cannot be inspected"
        ) from exc
    identity = _file_identity_from_stat(stat_result)
    if expected_identity is not None and identity != expected_identity:
        raise SessionRevisionProbeError("Session database changed during probe")
    return (
        "present",
        *identity,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _retains_sqlite_connections() -> bool:
    """Avoid Windows handles that block profile database replacement/removal."""

    return os.name != "nt"
