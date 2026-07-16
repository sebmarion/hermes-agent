"""Cheap aggregate revision tokens for local Hermes session databases."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class SessionRevisionProbeError(RuntimeError):
    """Raised when an observed session database cannot be probed safely."""


@dataclass
class _TrackedDatabase:
    ever_present: bool = False
    connection: sqlite3.Connection | None = None
    identity: tuple[int, int] | None = None
    epoch: int = 0
    data_version: int = 0


class SessionRevisionTracker:
    """Retain read-only SQLite handles and expose an opaque aggregate token."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], _TrackedDatabase] = {}
        self._epochs: dict[tuple[str, str], int] = {}

    @staticmethod
    def _close_entry(entry: _TrackedDatabase) -> None:
        connection = entry.connection
        entry.connection = None
        entry.identity = None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SessionRevisionProbeError(
                "Session revision probe unavailable"
            ) from exc
        return int(stat.st_dev), int(stat.st_ino)

    @staticmethod
    def _open_read_only(path: Path) -> sqlite3.Connection:
        try:
            return sqlite3.connect(
                f"{path.as_uri()}?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=1.0,
                isolation_level=None,
            )
        except (OSError, sqlite3.Error) as exc:
            raise SessionRevisionProbeError(
                "Session revision probe unavailable"
            ) from exc

    def _descriptor_for_target(
        self,
        key: tuple[str, str],
        profile: str,
        path: Path,
    ) -> dict:
        entry = self._entries.setdefault(key, _TrackedDatabase())
        identity = self._file_identity(path)
        if identity is None:
            if entry.ever_present:
                self._close_entry(entry)
                raise SessionRevisionProbeError(
                    "Session revision probe unavailable"
                )
            return {
                "profile": profile,
                "path": key[1],
                "state": "absent",
                "identity": None,
                "epoch": entry.epoch,
                "data_version": 0,
            }

        if entry.connection is None or entry.identity != identity:
            self._close_entry(entry)
            entry.connection = self._open_read_only(path)
            entry.identity = identity
            entry.epoch = self._epochs.get(key, 0) + 1
            self._epochs[key] = entry.epoch
            entry.ever_present = True

        try:
            row = entry.connection.execute("PRAGMA data_version").fetchone()
            entry.data_version = int(row[0]) if row else 0
        except (TypeError, ValueError, sqlite3.Error) as exc:
            self._close_entry(entry)
            raise SessionRevisionProbeError(
                "Session revision probe unavailable"
            ) from exc

        return {
            "profile": profile,
            "path": key[1],
            "state": "present",
            "identity": list(identity),
            "epoch": entry.epoch,
            "data_version": entry.data_version,
        }

    def revision(self, targets: Iterable[tuple[str, Path]]) -> str:
        """Return an opaque token for the sorted local profile/database set."""
        normalized: dict[tuple[str, str], tuple[str, Path]] = {}
        for raw_profile, raw_path in targets:
            profile = str(raw_profile or "").strip()
            path = Path(raw_path).expanduser().resolve(strict=False)
            key = (profile, str(path))
            normalized[key] = (profile, path)

        with self._lock:
            requested = set(normalized)
            for stale_key in set(self._entries) - requested:
                self._close_entry(self._entries.pop(stale_key))
            descriptors = [
                self._descriptor_for_target(key, *normalized[key])
                for key in sorted(normalized)
            ]
            payload = json.dumps(
                descriptors,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            return hashlib.sha256(payload).hexdigest()

    def close(self) -> None:
        """Close every retained connection; safe to call repeatedly."""
        with self._lock:
            for entry in self._entries.values():
                self._close_entry(entry)
            self._entries.clear()
            self._epochs.clear()


__all__ = ["SessionRevisionProbeError", "SessionRevisionTracker"]
