"""SQLite-safe restore and archive-member publish plumbing for backups.

Owns the restore side of ``hermes_cli.backup``: the page-copy SQLite restore
(``_safe_restore_db`` plus the foreign-holder scan) and the zip-member publish
helpers used by ``hermes import`` and ``/snapshot restore``.  Backup
*creation* (``run_backup``, full-zip writing) stays in ``hermes_cli.backup``,
which composes these helpers.
"""

import errno
import json
import logging
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from hermes_state_holders import read_only_db_uri
from hermes_cli.backup_sqlite import _file_identity
from utils import (
    _preserve_file_mode, _preserve_file_owner, _restore_file_mode, _restore_file_owner, atomic_replace,
    mkstemp_beside,
)

logger = logging.getLogger(__name__)

_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_DESTINATION_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _foreign_db_holder_pids(db_path: Path) -> Optional[List[int]]:
    """Return other-process holders, or None when holder state is unprovable."""
    if not sys.platform.startswith("linux"):
        return None

    def _canonical(path: str) -> str:
        return os.path.normcase(os.path.realpath(path.removesuffix(" (deleted)")))

    canonical_db = _canonical(os.fspath(db_path))
    watched = {
        canonical_db,
        canonical_db + "-wal",
        canonical_db + "-shm",
        canonical_db + "-journal",
    }
    pids: List[int] = []
    try:
        own_pid = os.getpid()
        for pid_str in os.listdir("/proc"):
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            if pid == own_pid:
                continue
            fd_dir = f"/proc/{pid}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ESRCH, errno.ENOTDIR}:
                    continue
                return None
            for fd in fds:
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError as exc:
                    if exc.errno in {errno.ENOENT, errno.ESRCH, errno.ENOTDIR}:
                        continue
                    return None
                if _canonical(target) in watched:
                    pids.append(pid)
                    break
    except OSError:
        return None
    return pids


def _sqlite_main_file_is_structurally_valid(path: Path) -> bool:
    """Probe the main SQLite file without consulting or mutating sidecars."""
    from hermes_cli.sqlite_safe_read import read_header_bytes_preopen

    if read_header_bytes_preopen(path, length=len(_SQLITE_HEADER)) != _SQLITE_HEADER:
        return False
    probe: Optional[sqlite3.Connection] = None
    try:
        probe = sqlite3.dbapi2.connect(
            f"{path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=1.0,
        )
        probe.execute("PRAGMA schema_version").fetchone()
        probe.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return True
    except (sqlite3.DatabaseError, OSError):
        return False
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass


def _current_process_holds_sqlite_family(path: Path) -> bool:
    """Whether this process has an open descriptor for the database family."""
    if not sys.platform.startswith("linux"):
        return False
    canonical = os.path.realpath(os.fspath(path))
    watched = {
        canonical,
        canonical + "-wal",
        canonical + "-shm",
        canonical + "-journal",
    }
    try:
        for fd in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                continue
            if os.path.realpath(target.removesuffix(" (deleted)")) in watched:
                return True
    except OSError:
        return True
    return False


def _can_remove_sqlite_sidecars(path: Path) -> bool:
    """Prove the database family is offline before deleting sidecars."""
    if _current_process_holds_sqlite_family(path):
        return False
    return _foreign_db_holder_pids(path) == []


def _remove_sqlite_sidecars(
    path: Path, expected_identity: Optional[tuple[int, int]] = None
) -> bool:
    """Remove only the sidecar generation captured before destructive cleanup."""
    sidecars = [
        path.with_name(path.name + suffix)
        for suffix in _SQLITE_DESTINATION_SIDECAR_SUFFIXES
    ]
    try:
        captured = {sidecar: _file_identity(sidecar) for sidecar in sidecars}
        if expected_identity is not None and _file_identity(path) != expected_identity:
            return False
        if all(identity is None for identity in captured.values()):
            return expected_identity is None or _file_identity(path) == expected_identity
        for sidecar in sidecars:
            if expected_identity is not None and _file_identity(path) != expected_identity:
                return False
            if not _can_remove_sqlite_sidecars(path):
                return False
            identity = captured[sidecar]
            if _file_identity(sidecar) != identity:
                return False
            if identity is not None:
                sidecar.unlink()
                if _file_identity(sidecar) is not None:
                    return False
        if expected_identity is not None and _file_identity(path) != expected_identity:
            return False
    except OSError as exc:
        logger.error("Could not remove SQLite sidecar for %s: %s", path, exc)
        return False
    return True


def _settle_sqlite_sidecars_after_online_backup(
    path: Path, expected_identity: tuple[int, int]
) -> bool:
    """Clean offline sidecars or prove they belong to a known live generation."""
    if _remove_sqlite_sidecars(path, expected_identity):
        return True
    if _current_process_holds_sqlite_family(path):
        return True
    holders = _foreign_db_holder_pids(path)
    if holders:
        return True
    if holders is None:
        logger.error(
            "Could not prove SQLite sidecars for %s are offline or held by a known live generation",
            path,
        )
    return False


def _validate_final_sqlite_destination(
    path: Path, expected_identity: tuple[int, int]
) -> bool:
    """Validate the exact installed database generation and reject swaps."""
    before = _file_identity(path)
    if before != expected_identity:
        logger.error("SQLite destination identity changed before validation for %s", path)
        return False
    from hermes_cli.backup import verify_sqlite_integrity

    integrity = verify_sqlite_integrity(
        path,
        check_header=not _current_process_holds_sqlite_family(path),
        run_pragma=True,
    )
    after = _file_identity(path)
    if after != expected_identity or after != before:
        logger.error("SQLite destination identity changed during validation for %s", path)
        return False
    if not integrity.get("valid"):
        logger.error(
            "SQLite destination failed final integrity verification for %s: %s",
            path,
            integrity.get("message"),
        )
        return False
    return True


def _auth_restore_target(dst: Path) -> Optional[Path]:
    """Writable auth-store path whose lock and publish name the same underlying file.

    A file symlink is resolved so refresh writers and restore take the same auth.lock. A hard-linked
    auth.json cannot be atomically replaced without splitting a deliberately shared store into two
    inodes, so that topology fails closed instead of silently breaking credential sharing.
    """
    try:
        if dst.exists():
            links = dst.stat().st_nlink
            if links > 1:
                logger.error(
                    "Refusing auth.json restore to %s: it has %d hard links; atomic replacement "
                    "would split a shared auth store",
                    dst,
                    links,
                )
                return None
        return dst.resolve(strict=False) if dst.is_symlink() else dst
    except (OSError, RuntimeError) as exc:
        logger.error("Refusing auth.json restore to %s: cannot resolve store identity: %s", dst, exc)
        return None

def _restore_auth_json(src: Path, dst: Path) -> bool:
    """Restore auth.json without rolling back a live single-use OAuth generation.

    The live read and final write share the canonical auth-store lock, so a concurrent refresh
    cannot land between preservation and publish.
    """
    try:
        snapshot_store = json.loads(src.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError) as exc:
        logger.error("Refusing auth.json restore from %s: %s", src, exc)
        return False
    if not isinstance(snapshot_store, dict):
        logger.error("Refusing auth.json restore from %s: top level is not an object", src)
        return False
    if not (
        isinstance(snapshot_store.get("providers"), dict)
        or isinstance(snapshot_store.get("credential_pool"), dict)
        or isinstance(snapshot_store.get("systems"), dict)
    ):
        logger.error("Refusing auth.json restore from %s: unrecognized auth-store shape", src)
        return False
    # Legacy "systems" stores are still valid snapshot input. Mirror auth._load_auth_store's
    # migration without asking that loader to create a .corrupt sidecar inside the snapshot.
    if (
        not isinstance(snapshot_store.get("providers"), dict)
        and not isinstance(snapshot_store.get("credential_pool"), dict)
        and isinstance(snapshot_store.get("systems"), dict)
    ):
        systems = snapshot_store["systems"]
        providers = {"nous": systems["nous_portal"]} if "nous_portal" in systems else {}
        # Match auth._load_auth_store's migration shape exactly: the legacy
        # "systems" container itself is obsolete and must not be written back.
        snapshot_store = {
            "providers": providers,
            "active_provider": "nous" if providers else None,
        }

    try:
        from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store
        from hermes_cli.auth_oauth_grants import (
            merge_snapshot_auth_preserving_live_single_use_grants,
        )

        target = _auth_restore_target(dst)
        if target is None:
            return False
        with _auth_store_lock(target_path=target):
            live_store = _load_auth_store(target)
            restored = merge_snapshot_auth_preserving_live_single_use_grants(
                snapshot_store, live_store
            )
            _save_auth_store(restored, target_path=target)
        return True
    except Exception as exc:
        logger.error("Failed to restore %s safely: %s", dst, exc)
        return False

def _safe_restore_db(src: Path, dst: Path) -> bool:
    """Restore a verified SQLite image without accepting identity races."""
    from hermes_cli.backup import verify_sqlite_integrity

    source_integrity = verify_sqlite_integrity(src, run_pragma=True)
    if not source_integrity.get("valid"):
        logger.warning(
            "SQLite restore source failed integrity verification for %s: %s",
            src,
            source_integrity.get("message"),
        )
        return False

    destination_identity = _file_identity(dst)
    destination_invalid = not _sqlite_main_file_is_structurally_valid(dst)
    if _file_identity(dst) != destination_identity:
        logger.error(
            "Refusing SQLite restore of %s: destination identity changed during pre-restore checks",
            dst,
        )
        return False

    if destination_identity is None:
        existing_sidecars = [
            dst.with_name(dst.name + suffix)
            for suffix in _SQLITE_DESTINATION_SIDECAR_SUFFIXES
            if dst.with_name(dst.name + suffix).exists()
        ]
        if existing_sidecars:
            if not _can_remove_sqlite_sidecars(dst):
                logger.error(
                    "Refusing SQLite restore of %s: sidecars exist but the family is not provably offline",
                    dst,
                )
                return False
            if not _remove_sqlite_sidecars(dst):
                return False
        if _file_identity(dst) is not None:
            logger.error(
                "Refusing SQLite restore of %s: destination appeared during pre-restore cleanup",
                dst,
            )
            return False

    dst_conn: Optional[sqlite3.Connection] = None
    src_conn: Optional[sqlite3.Connection] = None
    opened_destination_identity: Optional[tuple[int, int]] = None
    primary_succeeded = False
    try:
        src_conn = sqlite3.connect(read_only_db_uri(src), uri=True)
        dst_conn = sqlite3.connect(str(dst))
        opened_destination_identity = _file_identity(dst)
        if opened_destination_identity is None:
            raise OSError("SQLite restore destination disappeared after opening")
        src_conn.backup(dst_conn)
        primary_succeeded = True
    except Exception as exc:
        logger.warning("SQLite safe restore failed for %s -> %s: %s", src, dst, exc)
    finally:
        for connection in (src_conn, dst_conn):
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    if primary_succeeded:
        final_identity = destination_identity or opened_destination_identity
        if final_identity is None:
            logger.error("SQLite restore completed without an installed destination: %s", dst)
            return False
        try:
            dst.chmod(src.stat().st_mode)
        except Exception:
            pass
        if not _settle_sqlite_sidecars_after_online_backup(dst, final_identity):
            return False
        if not _validate_final_sqlite_destination(dst, final_identity):
            return False
        validation_sidecars = [
            dst.with_name(dst.name + suffix)
            for suffix in _SQLITE_DESTINATION_SIDECAR_SUFFIXES
            if dst.with_name(dst.name + suffix).exists()
        ]
        if validation_sidecars and not _current_process_holds_sqlite_family(dst):
            holders = _foreign_db_holder_pids(dst)
            if holders == [] and not _remove_sqlite_sidecars(dst, final_identity):
                return False
            if holders is None:
                return False
        return True

    if not destination_invalid:
        logger.error(
            "Refusing fallback restore of %s: destination passed its pre-restore structural check",
            dst,
        )
        return False

    from hermes_cli.sqlite_safe_read import LiveConnectionError, offline_file_access

    try:
        holders = _foreign_db_holder_pids(dst)
        if holders is None:
            logger.error(
                "Refusing fallback restore of %s: holder state could not be proven",
                dst,
            )
            return False
        if holders:
            logger.error(
                "Refusing fallback restore of %s: process(es) %s still hold the database family",
                dst,
                holders,
            )
            return False
        with offline_file_access(dst, what="fallback restore of"):
            tmp = dst.parent / f".{dst.name}.snap_restore"
            try:
                shutil.copy2(src, tmp)
                staged_identity = _file_identity(tmp)
                if staged_identity is None:
                    raise OSError("fallback SQLite staging file disappeared")
                if _file_identity(dst) != destination_identity:
                    raise OSError("SQLite destination was substituted before fallback publish")
                if _foreign_db_holder_pids(dst) != []:
                    raise OSError("SQLite destination gained a foreign holder before fallback publish")
                if not _remove_sqlite_sidecars(dst, destination_identity):
                    raise OSError("SQLite fallback sidecar cleanup failed")
                atomic_replace(tmp, dst)
                if not _validate_final_sqlite_destination(dst, staged_identity):
                    raise OSError("installed SQLite fallback destination failed final validation")
                if not _remove_sqlite_sidecars(dst, staged_identity):
                    raise OSError("SQLite fallback sidecar cleanup failed after publish")
                return True
            finally:
                tmp.unlink(missing_ok=True)
    except LiveConnectionError as exc:
        logger.error(
            "Refusing fallback restore of %s: %s Close in-process database handles and retry.",
            dst,
            exc,
        )
        return False
    except Exception as exc:
        logger.error("Fallback restore also failed for %s -> %s: %s", src, dst, exc)
        return False

def _validate_backup_zip(zf: zipfile.ZipFile) -> tuple[bool, str]:
    """Check that a zip looks like a Hermes backup.

    Returns (ok, reason).
    """
    names = zf.namelist()
    if not names:
        return False, "zip archive is empty"

    # Look for telltale files that a hermes home would have
    markers = {"config.yaml", ".env", "state.db"}
    found = set()
    for n in names:
        # Could be at the root or one level deep (if someone zipped the directory)
        basename = Path(n).name
        if basename in markers:
            found.add(basename)

    if not found:
        return False, (
            "zip does not appear to be a Hermes backup "
            "(no config.yaml, .env, or state databases found)"
        )

    return True, ""


def _detect_prefix(zf: zipfile.ZipFile) -> str:
    """Detect if the zip has a common directory prefix wrapping all entries.

    Some tools zip as `.hermes/config.yaml` instead of `config.yaml`.
    Returns the prefix to strip (empty string if none).
    """
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        return ""

    # Find common prefix
    parts_list = [Path(n).parts for n in names]

    # Check if all entries share a common first directory
    first_parts = {p[0] for p in parts_list if len(p) > 1}
    if len(first_parts) == 1:
        prefix = first_parts.pop()
        # Only strip if it looks like a hermes dir name
        if prefix in {".hermes", "hermes"}:
            return prefix + "/"

    return ""


def _default_new_file_mode() -> Optional[int]:
    """Return the mode ``open(path, "wb")`` gives a file it has to create.

    ``tempfile.mkstemp`` always creates at 0600, so staging an import through a
    temp file would tighten every *newly created* file to owner-only — the same
    hazard ``utils._restore_file_mode`` documents for Docker/NAS volume mounts
    that rely on broader permissions.  The umask can only be read by setting it,
    so this is resolved once per import rather than once per member.  The probe
    installs a *restrictive* mask rather than 0 so that anything another thread
    creates inside the two-syscall window is owner-only, never world-writable.
    Returns ``None`` if the umask cannot be read, in which case the caller
    leaves mkstemp's mode alone.
    """
    try:
        current = os.umask(0o077)
        os.umask(current)
    except OSError:
        return None
    return 0o666 & ~current


def _extract_member_atomically(
    zf: zipfile.ZipFile,
    member: str,
    target: Path,
    new_file_mode: Optional[int] = None,
) -> None:
    """Restore one zip member onto *target* with no truncation window.

    ``open(target, "wb")`` truncates the user's existing file to zero *before*
    any replacement bytes exist.  A Ctrl-C, an ENOSPC, a corrupt member, or a
    crash between the truncate and the write therefore leaves that file empty
    with nothing behind it — during ``hermes import``, which is the
    disaster-recovery path a user reaches for *because* they already lost
    something.  Staging into the target's own directory and publishing with a
    rename means the target only ever moves from its old contents to the
    complete new contents.

    ``atomic_replace`` rather than a bare ``os.replace``: it resolves a
    symlinked target first, so a deployment that links ``config.yaml`` into a
    dotfiles repo keeps the link instead of having it silently swapped for a
    regular file (GitHub #16743), and it falls back to copy/fsync/unlink on
    ``EXDEV``/``EBUSY`` for cross-device and bind-mount installs.  That
    fallback uses ``shutil.copyfile``, which does truncate in place, so on the
    cross-device path the guarantee above degrades to today's behaviour rather
    than improving on it; closing that belongs in ``utils.atomic_replace``,
    where every atomic writer in the repo would benefit, not here.

    Permission bits *and* ownership are carried across the replace so routing
    through mkstemp does not change the file the caller would otherwise have
    produced.  ``os.replace`` swaps in a temp file owned by the *writing* user,
    so without the chown a ``sudo hermes import`` would silently re-own every
    restored file to root — on the disaster-recovery path, and on exactly the
    Docker/NAS installs ``utils._restore_file_owner`` documents.  Both concerns
    delegate to the shared ``utils`` helpers rather than being re-derived here.
    The temp file is removed on any failure so a partial import leaves no
    residue.

    The one bit of the old file *not* carried across is setuid/setgid.  The
    replacement bytes come out of the zip, so preserving those would let an
    archive take over the identity an existing privileged file executes as —
    and unlike the other ``utils`` writers, which re-serialize content this
    process produced, the trust boundary here is an untrusted archive.  The
    mask is applied once, before the temp file is chmod'd, so neither the
    pre-replace ``fchmod`` nor the post-replace restore can re-elevate the
    target.
    """
    # ``_preserve_file_mode`` returns None when the target does not exist (or
    # cannot be stat'd), in which case the umask-derived create-mode applies —
    # the same shape as ``atomic_yaml_write``'s ``create_mode`` fallback.
    mode = _preserve_file_mode(target)
    owner = _preserve_file_owner(target)
    if mode is None:
        mode = new_file_mode
    else:
        # Deliberately NOT a faithful mode copy: setuid/setgid are dropped.
        # ``_preserve_file_mode`` returns ``stat.S_IMODE``, i.e. all twelve
        # bits, and the content replacing this file comes from the archive.
        # Carrying the elevated bits across would let archive-controlled bytes
        # take over an existing setuid/setgid file, so ``hermes import`` would
        # hand whoever produced the zip the identity that file runs as.  Nothing
        # constrains that to Hermes' own state either: the ``_external/`` branch
        # of ``run_import`` publishes members anywhere under ``$HOME``.  The
        # sticky bit is kept — it is inert on a regular file.
        mode &= ~(stat.S_ISUID | stat.S_ISGID)

    # Truncate the stem: mkstemp adds ~16 characters, and a member already near
    # NAME_MAX would otherwise fail here on a write that used to succeed.
    fd, tmp_name = mkstemp_beside(target, prefix=f".{target.name[:80]}.", suffix=".partial")
    try:
        with os.fdopen(fd, "wb") as dst:
            if mode is not None:
                # Apply the mode to the temp file BEFORE the replace so the
                # target never transits through mkstemp's 0600, and so
                # ``atomic_replace``'s EXDEV/EBUSY ``shutil.copystat`` fallback
                # copies the intended bits rather than 0600.  fchmod is
                # Unix-only; Windows takes the path-based chmod.
                if hasattr(os, "fchmod"):
                    os.fchmod(dst.fileno(), mode)
                else:
                    os.chmod(tmp_name, mode)
            # Stream instead of ``src.read()``: a multi-gigabyte state.db member
            # must not be held in memory in one piece.
            with zf.open(member) as src:
                shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        real_path = Path(atomic_replace(tmp_name, target))
        # Owner first, mode second — the ordering ``atomic_yaml_write`` uses,
        # because chown drops setuid/setgid and a mode restore that ran first
        # would be partly undone.  Here ``mode`` no longer carries those bits,
        # so the two agree: neither step can re-elevate the restored file.
        _restore_file_owner(real_path, owner)
        _restore_file_mode(real_path, mode)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _count_session_rows(path: Path) -> Optional[Tuple[int, int]]:
    """Return ``(sessions, messages)`` stored in the session database *path*.

    Read-only and best effort.  ``None`` means "unknown" — a missing file, a
    database that is not a Hermes session store, or one that cannot be read.
    Callers must never read ``None`` as "zero rows": acting on an unreadable
    database would mask the very loss this count exists to surface.  Same
    contract as :func:`_count_cron_jobs`.
    """
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
    except sqlite3.Error:
        return None
    try:
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return int(sessions), int(messages)
    except (sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        conn.close()


def _import_db_member(
    zf: zipfile.ZipFile,
    member: str,
    target: Path,
    new_file_mode: Optional[int] = None,
) -> None:
    """Stage, verify, and publish one SQLite archive member safely."""
    from hermes_cli.backup import verify_sqlite_integrity

    target_exists = target.exists()
    target_identity = _file_identity(target) if target_exists else None
    mode = _preserve_file_mode(target) if target_exists else None
    owner = _preserve_file_owner(target) if target_exists else None

    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name[:80]}.", suffix=".dbimport"
    )
    try:
        with os.fdopen(fd, "wb") as dst:
            with zf.open(member) as src:
                shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        tmp_path = Path(tmp_name)
        if not _sqlite_main_file_is_structurally_valid(tmp_path):
            _extract_member_atomically(zf, member, target, new_file_mode)
            return

        integrity = verify_sqlite_integrity(tmp_path, run_pragma=True)
        if not integrity.get("valid"):
            raise OSError(
                "archive database failed integrity verification; restore refused: "
                f"{integrity.get('message')}"
            )

        if not target_exists:
            if _file_identity(target) is not None:
                raise OSError(
                    "SQLite destination appeared or was substituted before publish; restore refused"
                )
            expected_identity = _file_identity(tmp_path)
            if expected_identity is None:
                raise OSError("staged SQLite database disappeared; restore refused")
            if not _remove_sqlite_sidecars(target):
                raise OSError("SQLite destination sidecar cleanup failed; restore refused")
            if _file_identity(target) is not None:
                raise OSError(
                    "SQLite destination appeared during sidecar cleanup; restore refused"
                )
            os.replace(tmp_path, target)
            if not _remove_sqlite_sidecars(target, expected_identity):
                raise OSError("installed SQLite sidecar cleanup failed; restore refused")
            if not _validate_final_sqlite_destination(target, expected_identity):
                raise OSError(
                    "installed SQLite destination failed final validation; restore refused"
                )
            if new_file_mode is not None:
                _restore_file_mode(target, new_file_mode)
            return

        if _file_identity(target) != target_identity:
            raise OSError("SQLite destination was substituted while staging; restore refused")
        if not _safe_restore_db(tmp_path, target):
            raise OSError(
                "live-safe restore refused or failed; the existing database was left untouched. "
                "Stop processes holding it open and re-run the import."
            )
        _restore_file_owner(target, owner)
        _restore_file_mode(target, mode)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
