"""Fail-closed JSON authority files shared by independent Hermes runtimes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Iterator, Optional


@dataclass(frozen=True)
class FileIdentity:
    """Exact path identity captured from the descriptor used for the read."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    links: int
    uid: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileIdentity":
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            size=value.st_size,
            mtime_ns=value.st_mtime_ns,
            ctime_ns=value.st_ctime_ns,
            mode=value.st_mode,
            links=value.st_nlink,
            uid=value.st_uid,
        )


def _nofollow_flag() -> int:
    value = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(value, int) or value == 0:
        raise ValueError("O_NOFOLLOW is unavailable for durable authority state")
    return value


def _owner_uid() -> Optional[int]:
    getter = getattr(os, "geteuid", None)
    return getter() if callable(getter) else None


def _validate_private_parent(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("durable authority path must be absolute")
    parent = path.parent
    resolved = parent.resolve(strict=True)
    if resolved != parent:
        raise ValueError("durable authority parent must be canonical")
    parent_state = os.lstat(parent)
    owner = _owner_uid()
    if (
        not stat.S_ISDIR(parent_state.st_mode)
        or (owner is not None and parent_state.st_uid != owner)
        or (
            os.name != "nt"
            and stat.S_IMODE(parent_state.st_mode) != 0o700
        )
    ):
        raise ValueError("durable authority parent must be a private owned directory")
    return parent


def _validate_private_regular(
    state: os.stat_result,
    *,
    max_bytes: Optional[int] = None,
) -> None:
    owner = _owner_uid()
    if (
        not stat.S_ISREG(state.st_mode)
        or state.st_nlink != 1
        or (owner is not None and state.st_uid != owner)
        or (os.name != "nt" and stat.S_IMODE(state.st_mode) != 0o600)
        or (max_bytes is not None and state.st_size > max_bytes)
    ):
        raise ValueError("durable authority is not a private bounded regular file")


def _lstat_private_identity(
    path: Path,
    *,
    max_bytes: Optional[int] = None,
) -> FileIdentity:
    state = os.lstat(path)
    _validate_private_regular(state, max_bytes=max_bytes)
    return FileIdentity.from_stat(state)


def _fsync_parent(parent: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _acquire_platform_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX)


def _release_platform_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _same_file_object(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare stable object identity while permitting benign metadata races."""
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


@contextmanager
def interprocess_authority_lock(path: Path) -> Iterator[None]:
    """Serialize one complete authority-file read/merge/CAS/write mutation."""

    path = Path(path)
    parent = _validate_private_parent(path)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = (
        os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | _nofollow_flag()
    )
    created = False
    try:
        descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        if os.name == "nt":
            os.write(descriptor, b"\0")
        else:
            os.fchmod(descriptor, 0o600)
    except FileExistsError:
        descriptor = os.open(lock_path, flags)
    try:
        opened = os.fstat(descriptor)
        _validate_private_regular(opened)
        path_state = os.lstat(lock_path)
        _validate_private_regular(path_state)
        if not _same_file_object(opened, path_state):
            raise ValueError("durable authority lock path identity changed")
        if created:
            os.fsync(descriptor)
            _fsync_parent(parent)
        _acquire_platform_lock(descriptor)
        try:
            locked = os.fstat(descriptor)
            current = os.lstat(lock_path)
            if not _same_file_object(locked, current):
                raise ValueError("durable authority lock changed while waiting")
            yield
            current_after = os.lstat(lock_path)
            _validate_private_regular(current_after)
            if not _same_file_object(locked, current_after):
                raise ValueError("durable authority lock changed while held")
        finally:
            _release_platform_lock(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def platform_neutral_lifecycle_lock(path: Path) -> Iterator[None]:
    """Legacy cross-platform lifecycle lock without strict POSIX authority IO.

    Managed recovery uses held directory descriptors. Windows legacy paths
    cannot rely on ``O_NOFOLLOW`` or directory fsync, but still require one
    interprocess serialization point.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt" and os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        _acquire_platform_lock(descriptor)
        try:
            yield
        finally:
            _release_platform_lock(descriptor)
    finally:
        os.close(descriptor)


def read_private_json(
    path: Path,
    *,
    max_bytes: int,
    missing_ok: bool = False,
) -> tuple[Any, Optional[FileIdentity]]:
    """Read and validate JSON from the same no-follow file descriptor."""

    path = Path(path)
    _validate_private_parent(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _nofollow_flag()
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None, None
        raise
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        _validate_private_regular(opened, max_bytes=max_bytes)
        payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("durable authority exceeds its byte limit")
        after = os.fstat(handle.fileno())
        current = os.lstat(path)
        _validate_private_regular(after, max_bytes=max_bytes)
        _validate_private_regular(current, max_bytes=max_bytes)
        opened_identity = FileIdentity.from_stat(opened)
        if (
            opened_identity != FileIdentity.from_stat(after)
            or opened_identity != FileIdentity.from_stat(current)
        ):
            raise ValueError("durable authority changed while being read")
    try:
        return json.loads(payload.decode("utf-8")), opened_identity
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ValueError("durable authority JSON is malformed") from exc


def atomic_write_private_json(
    path: Path,
    value: Any,
    *,
    expected: Optional[FileIdentity],
    max_bytes: int,
    sort_keys: bool = False,
) -> FileIdentity:
    """CAS-replace a private authority file and fsync both file and parent."""

    path = Path(path)
    parent = _validate_private_parent(path)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=sort_keys,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > max_bytes:
        raise ValueError("durable authority exceeds its byte limit")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    replaced = False
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            current = _lstat_private_identity(path, max_bytes=max_bytes)
        except FileNotFoundError:
            current = None
        if current != expected:
            raise RuntimeError("durable authority compare-and-swap conflict")

        os.replace(temporary, path)
        replaced = True
        _fsync_parent(parent)
        committed = _lstat_private_identity(path, max_bytes=max_bytes)
        if committed.size != len(payload):
            raise RuntimeError("durable authority commit size is inconsistent")
        return committed
    finally:
        if not replaced:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class HeldPrivateAuthorityDirectory:
    """Directory-fd-bound authority operations immune to parent rebinding."""

    def __init__(self, parent: Path, descriptor: int, identity: FileIdentity):
        self.parent = parent
        self.descriptor = descriptor
        self.identity = identity

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def assert_current(self) -> None:
        if self.descriptor < 0:
            raise ValueError("held authority directory is closed")
        opened = FileIdentity.from_stat(os.fstat(self.descriptor))
        current = FileIdentity.from_stat(os.lstat(self.parent))
        def stable(value: FileIdentity) -> tuple[int, int, int, int]:
            return (value.device, value.inode, value.mode, value.uid)

        if stable(opened) != stable(self.identity) or stable(current) != stable(
            self.identity
        ):
            raise ValueError("durable authority parent identity changed")

    def _leaf(self, path: Path) -> str:
        path = Path(path)
        if path.parent != self.parent or path.name in {"", ".", ".."}:
            raise ValueError("authority path is outside held parent")
        return path.name

    def read_json(
        self,
        path: Path,
        *,
        max_bytes: int,
        missing_ok: bool = False,
    ) -> tuple[Any, Optional[FileIdentity], Optional[bytes]]:
        self.assert_current()
        leaf = self._leaf(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _nofollow_flag()
        try:
            descriptor = os.open(leaf, flags, dir_fd=self.descriptor)
        except FileNotFoundError:
            if missing_ok:
                self.assert_current()
                return None, None, None
            raise
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            _validate_private_regular(opened, max_bytes=max_bytes)
            payload = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
        current = os.stat(
            leaf,
            dir_fd=self.descriptor,
            follow_symlinks=False,
        )
        _validate_private_regular(after, max_bytes=max_bytes)
        _validate_private_regular(current, max_bytes=max_bytes)
        identity = FileIdentity.from_stat(opened)
        if (
            len(payload) > max_bytes
            or FileIdentity.from_stat(after) != identity
            or FileIdentity.from_stat(current) != identity
        ):
            raise ValueError("durable authority changed while being read")
        self.assert_current()
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise ValueError("durable authority JSON is malformed") from exc
        return value, identity, payload

    def atomic_write_json(
        self,
        path: Path,
        value: Any,
        *,
        expected: Optional[FileIdentity],
        max_bytes: int,
        sort_keys: bool = False,
    ) -> FileIdentity:
        self.assert_current()
        leaf = self._leaf(path)
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=sort_keys,
            )
            + "\n"
        ).encode("utf-8")
        if len(payload) > max_bytes:
            raise ValueError("durable authority exceeds its byte limit")
        temporary = f".{leaf}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | _nofollow_flag()
        )
        descriptor = os.open(
            temporary,
            flags,
            0o600,
            dir_fd=self.descriptor,
        )
        replaced = False
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                current_state = os.stat(
                    leaf,
                    dir_fd=self.descriptor,
                    follow_symlinks=False,
                )
                _validate_private_regular(current_state, max_bytes=max_bytes)
                current = FileIdentity.from_stat(current_state)
            except FileNotFoundError:
                current = None
            if current != expected:
                raise RuntimeError("durable authority compare-and-swap conflict")
            self.assert_current()
            os.replace(
                temporary,
                leaf,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
            replaced = True
            os.fsync(self.descriptor)
            committed_state = os.stat(
                leaf,
                dir_fd=self.descriptor,
                follow_symlinks=False,
            )
            _validate_private_regular(committed_state, max_bytes=max_bytes)
            committed = FileIdentity.from_stat(committed_state)
            if committed.size != len(payload):
                raise RuntimeError("durable authority commit size is inconsistent")
            self.assert_current()
            return committed
        finally:
            if not replaced:
                try:
                    os.unlink(temporary, dir_fd=self.descriptor)
                except FileNotFoundError:
                    pass

    @contextmanager
    def lock(self, path: Path) -> Iterator[None]:
        self.assert_current()
        leaf = self._leaf(path)
        lock_leaf = f".{leaf}.lock"
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | _nofollow_flag()
        created = False
        try:
            descriptor = os.open(
                lock_leaf,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=self.descriptor,
            )
            created = True
            if os.name == "nt":
                os.write(descriptor, b"\0")
            else:
                os.fchmod(descriptor, 0o600)
        except FileExistsError:
            descriptor = os.open(lock_leaf, flags, dir_fd=self.descriptor)
        try:
            opened = os.fstat(descriptor)
            _validate_private_regular(opened)
            current = os.stat(
                lock_leaf,
                dir_fd=self.descriptor,
                follow_symlinks=False,
            )
            _validate_private_regular(current)
            if not _same_file_object(opened, current):
                raise ValueError("durable authority lock path identity changed")
            if created:
                os.fsync(descriptor)
                os.fsync(self.descriptor)
            _acquire_platform_lock(descriptor)
            try:
                self.assert_current()
                locked = os.fstat(descriptor)
                current = os.stat(
                    lock_leaf,
                    dir_fd=self.descriptor,
                    follow_symlinks=False,
                )
                if not _same_file_object(locked, current):
                    raise ValueError("durable authority lock changed while waiting")
                yield
                self.assert_current()
                current_after = os.stat(
                    lock_leaf,
                    dir_fd=self.descriptor,
                    follow_symlinks=False,
                )
                _validate_private_regular(current_after)
                if not _same_file_object(locked, current_after):
                    raise ValueError("durable authority lock changed while held")
            finally:
                _release_platform_lock(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def hold_private_authority_directory(
    path: Path,
) -> Iterator[HeldPrivateAuthorityDirectory]:
    """Hold and validate the exact private parent directory for an operation."""
    path = Path(path)
    parent = _validate_private_parent(path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | _nofollow_flag()
    )
    descriptor = os.open(parent, flags)
    try:
        opened = os.fstat(descriptor)
        owner = _owner_uid()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (owner is not None and opened.st_uid != owner)
            or (os.name != "nt" and stat.S_IMODE(opened.st_mode) != 0o700)
        ):
            raise ValueError("durable authority parent is not private and owned")
        identity = FileIdentity.from_stat(opened)
        current = FileIdentity.from_stat(os.lstat(parent))
        if current != identity:
            raise ValueError("durable authority parent identity changed")
        held = HeldPrivateAuthorityDirectory(parent, descriptor, identity)
        try:
            yield held
            held.assert_current()
        finally:
            held.close()
            descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
