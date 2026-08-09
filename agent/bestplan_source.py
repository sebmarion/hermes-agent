"""Fail-closed Git source capture for executable BestPlan workspaces.

The candidate source is always the committed ``HEAD`` tree.  Index and
working-tree state is recorded separately so it can be protected without ever
being imported into an execution sandbox.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import shutil
import signal
import stat
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from typing import Iterable


_DEFAULT_DEADLINE_SECONDS = 10.0
_BUFFER_SIZE = 1024 * 1024
_MAX_STABILIZATION_READS = 16
_CAPTURE_CLEANUP_SECONDS = 1.0


class SourceBoundaryError(ValueError):
    """Base error for an unavailable or unsafe BestPlan source boundary."""

    code = "source_unavailable"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = str(code or self.code)


class ProofStaleError(SourceBoundaryError):
    """Raised when a stable two-read source proof cannot be captured."""

    code = "proof_stale"


class UnsupportedRepositoryError(SourceBoundaryError):
    """Raised for Git shapes whose exported bytes cannot be trusted."""

    code = "unsupported_repository"


@dataclass(frozen=True)
class RepoIdentity:
    workspace: str
    workspace_raw: bytes
    worktree: str
    worktree_raw: bytes
    git_dir: str
    git_dir_raw: bytes
    common_dir: str
    common_dir_raw: bytes
    common_dir_device: int
    common_dir_inode: int
    object_format: str
    repository_id: str


@dataclass(frozen=True)
class IndexEntry:
    path: bytes
    mode: int
    oid: str
    stage: int


@dataclass(frozen=True)
class IndexFlags:
    path: bytes
    tag: bytes
    fsmonitor_tag: bytes
    assume_unchanged: bool
    skip_worktree: bool
    fsmonitor_valid: bool


@dataclass(frozen=True)
class ProtectedPath:
    path: bytes
    tracked: bool
    kind: str
    mode: int | None
    size: int | None
    content_sha256: str | None
    symlink_target: bytes | None


@dataclass(frozen=True)
class ProtectedManifest:
    index_entries: tuple[IndexEntry, ...]
    index_flags: tuple[IndexFlags, ...]
    worktree_entries: tuple[ProtectedPath, ...]
    protected_paths: tuple[bytes, ...]
    staged_diff_sha256: str
    unstaged_diff_sha256: str
    digest: str


@dataclass(frozen=True)
class SourceSnapshot:
    repo: RepoIdentity
    head_symbolic: bool
    head_ref: bytes | None
    head_raw: bytes
    head_oid: str
    tree_oid: str
    protected_manifest: ProtectedManifest
    capture_implementation_sha256: str
    fingerprint: str

    @property
    def baseline_fingerprint(self) -> str:
        return self.fingerprint

    @property
    def baseline_revision(self) -> str:
        return self.head_oid


@dataclass(frozen=True)
class _TreeEntry:
    path: bytes
    mode: int
    object_type: bytes
    oid: str


@dataclass(frozen=True)
class _PreparedDestination:
    path: bytes
    leaf: bytes
    root_fd: int
    root_identity: tuple[int, int]
    raw_parent: bytes
    canonical_parent: bytes
    parent_fds: tuple[int, ...]
    parent_identities: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class _SourceRead:
    head_symbolic: bool
    head_ref: bytes | None
    head_raw: bytes
    head_oid: str
    tree_oid: str
    protected_manifest: ProtectedManifest


class _CaptureChanged(RuntimeError):
    pass


def _git_environment() -> dict[str, str]:
    env = dict(os.environ)
    exact = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_REPLACE_REF_BASE",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_COUNT",
    }
    for key in list(env):
        if key in exact or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    return env


def _remaining(deadline: float) -> float:
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise ProofStaleError("proof_stale: source capture deadline expired")
    return remaining


def _run_git(
    cwd: bytes | str,
    *args: str,
    deadline: float | None = None,
    input_data: bytes | None = None,
    ok_codes: tuple[int, ...] = (0,),
) -> tuple[int, bytes]:
    absolute_deadline = (
        time.monotonic() + _DEFAULT_DEADLINE_SECONDS
        if deadline is None
        else float(deadline)
    )
    _assert_trusted_file_identity(
        _CAPTURE_GIT_PATH,
        _CAPTURE_GIT_SHA256,
        _CAPTURE_GIT_DEVICE,
        _CAPTURE_GIT_INODE,
        require_executable=True,
    )
    try:
        result = subprocess.run(
            [os.fsdecode(_CAPTURE_GIT_PATH), *args],
            cwd=cwd,
            env=_git_environment(),
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_remaining(absolute_deadline),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProofStaleError(
            f"proof_stale: git {' '.join(args)} exceeded the source deadline"
        ) from exc
    except OSError as exc:
        raise SourceBoundaryError(
            f"trusted Git command unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    _assert_trusted_file_identity(
        _CAPTURE_GIT_PATH,
        _CAPTURE_GIT_SHA256,
        _CAPTURE_GIT_DEVICE,
        _CAPTURE_GIT_INODE,
        require_executable=True,
    )
    if result.returncode not in ok_codes:
        detail = os.fsdecode(result.stderr.strip()) or f"exit {result.returncode}"
        raise SourceBoundaryError(
            f"trusted Git command failed ({' '.join(args)}): {detail}"
        )
    return result.returncode, result.stdout


def _without_delimiter(value: bytes) -> bytes:
    return value[:-1] if value.endswith(b"\n") else value


def _hash_fields(
    label: bytes,
    fields: Iterable[bytes],
    *,
    deadline: float | None = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(len(label).to_bytes(8, "big"))
    digest.update(label)
    for field in fields:
        if deadline is not None:
            _remaining(deadline)
        digest.update(len(field).to_bytes(8, "big"))
        digest.update(field)
    return digest.hexdigest()


def _sha256_bytes(value: bytes, *, deadline: float) -> str:
    digest = hashlib.sha256()
    for offset in range(0, len(value), _BUFFER_SIZE):
        _remaining(deadline)
        digest.update(value[offset:offset + _BUFFER_SIZE])
    _remaining(deadline)
    return digest.hexdigest()


def _stable_file_identity(
    path: bytes, *, require_executable: bool = False,
) -> tuple[str, int, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, _BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    before_state = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_state = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if (
        before_state != after_state
        or not stat.S_ISREG(before.st_mode)
        or (
            require_executable
            and os.name == "posix"
            and before.st_mode & 0o111 == 0
        )
    ):
        raise RuntimeError("BestPlan capture implementation changed during import")
    return digest.hexdigest(), before.st_dev, before.st_ino


def _loaded_file_sha256(path: bytes) -> str:
    digest, _device, _inode = _stable_file_identity(path)
    return digest


def _resolve_executable_path(name: str) -> bytes:
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(f"trusted executable is unavailable: {name}")
    canonical = os.path.realpath(os.fsencode(resolved))
    if not os.path.isabs(canonical):
        raise RuntimeError(f"trusted executable is not absolute: {name}")
    return canonical


_CAPTURE_MODULE_PATH = os.path.realpath(os.fsencode(__file__))
_CAPTURE_INTERPRETER_PATH = os.path.realpath(os.fsencode(sys.executable))
_CAPTURE_GIT_PATH = _resolve_executable_path("git")
(
    _CAPTURE_MODULE_SHA256,
    _CAPTURE_MODULE_DEVICE,
    _CAPTURE_MODULE_INODE,
) = _stable_file_identity(_CAPTURE_MODULE_PATH)
(
    _CAPTURE_INTERPRETER_SHA256,
    _CAPTURE_INTERPRETER_DEVICE,
    _CAPTURE_INTERPRETER_INODE,
) = _stable_file_identity(_CAPTURE_INTERPRETER_PATH, require_executable=True)
(
    _CAPTURE_GIT_SHA256,
    _CAPTURE_GIT_DEVICE,
    _CAPTURE_GIT_INODE,
) = _stable_file_identity(_CAPTURE_GIT_PATH, require_executable=True)
_CAPTURE_IMPLEMENTATION_SHA256 = _hash_fields(
    b"bestplan-capture-authority-v2",
    (
        _CAPTURE_MODULE_PATH,
        _CAPTURE_MODULE_SHA256.encode("ascii"),
        _CAPTURE_INTERPRETER_PATH,
        _CAPTURE_INTERPRETER_SHA256.encode("ascii"),
        str(_CAPTURE_INTERPRETER_DEVICE).encode("ascii"),
        str(_CAPTURE_INTERPRETER_INODE).encode("ascii"),
        _CAPTURE_GIT_PATH,
        _CAPTURE_GIT_SHA256.encode("ascii"),
        str(_CAPTURE_GIT_DEVICE).encode("ascii"),
        str(_CAPTURE_GIT_INODE).encode("ascii"),
        sys.version.encode("utf-8"),
        str(sys.implementation.cache_tag or "").encode("ascii"),
    ),
)
_helper_path_entries = [
    os.fsdecode(os.path.dirname(_CAPTURE_GIT_PATH)),
    os.fsdecode(os.path.dirname(_CAPTURE_INTERPRETER_PATH)),
]
if os.name == "posix":
    _helper_path_entries.extend(("/usr/bin", "/bin"))
else:
    _system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    _helper_path_entries.extend(
        (os.path.join(_system_root, "System32"), _system_root)
    )
_CAPTURE_HELPER_PATH = os.pathsep.join(dict.fromkeys(_helper_path_entries))


def _assert_trusted_file_identity(
    path: bytes,
    expected_sha256: str,
    expected_device: int,
    expected_inode: int,
    *,
    require_executable: bool,
) -> None:
    try:
        digest, device, inode = _stable_file_identity(
            path, require_executable=require_executable,
        )
    except (OSError, RuntimeError) as exc:
        raise SourceBoundaryError(
            f"trusted capture executable is unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    if (
        digest != expected_sha256
        or device != expected_device
        or inode != expected_inode
    ):
        raise SourceBoundaryError(
            "trusted capture executable identity changed"
        )


def _canonical_raw(path: str | os.PathLike[str]) -> bytes:
    expanded = os.path.expanduser(os.fsencode(path))
    return os.path.realpath(os.path.abspath(expanded))


def _resolve_repo_identity(workspace: str, deadline: float) -> RepoIdentity:
    workspace_raw = _canonical_raw(workspace or os.getcwd())
    if not os.path.isdir(workspace_raw):
        raise SourceBoundaryError(
            f"trusted Git workspace is not a directory: {os.fsdecode(workspace_raw)}"
        )
    _, worktree_out = _run_git(
        workspace_raw, "rev-parse", "--path-format=absolute", "--show-toplevel",
        deadline=deadline,
    )
    _, git_dir_out = _run_git(
        workspace_raw, "rev-parse", "--path-format=absolute", "--absolute-git-dir",
        deadline=deadline,
    )
    _, common_dir_out = _run_git(
        workspace_raw, "rev-parse", "--path-format=absolute", "--git-common-dir",
        deadline=deadline,
    )
    _, object_format_out = _run_git(
        workspace_raw, "rev-parse", "--show-object-format", deadline=deadline,
    )
    worktree_raw = os.path.realpath(_without_delimiter(worktree_out))
    git_dir_raw = os.path.realpath(_without_delimiter(git_dir_out))
    common_dir_raw = os.path.realpath(_without_delimiter(common_dir_out))
    object_format = _without_delimiter(object_format_out).decode("ascii")
    if object_format not in {"sha1", "sha256"}:
        raise UnsupportedRepositoryError(
            f"unsupported Git object format: {object_format!r}"
        )
    try:
        common_stat = os.stat(common_dir_raw, follow_symlinks=True)
    except OSError as exc:
        raise SourceBoundaryError(
            f"Git common directory is unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    repository_id = _hash_fields(
        b"bestplan-repository-v1",
        (
            common_dir_raw,
            str(common_stat.st_dev).encode("ascii"),
            str(common_stat.st_ino).encode("ascii"),
            object_format.encode("ascii"),
        ),
        deadline=deadline,
    )
    return RepoIdentity(
        workspace=os.fsdecode(workspace_raw),
        workspace_raw=workspace_raw,
        worktree=os.fsdecode(worktree_raw),
        worktree_raw=worktree_raw,
        git_dir=os.fsdecode(git_dir_raw),
        git_dir_raw=git_dir_raw,
        common_dir=os.fsdecode(common_dir_raw),
        common_dir_raw=common_dir_raw,
        common_dir_device=common_stat.st_dev,
        common_dir_inode=common_stat.st_ino,
        object_format=object_format,
        repository_id=repository_id,
    )


def resolve_repo_identity(workspace: str) -> RepoIdentity:
    """Resolve one worktree and its shared repository identity losslessly."""

    return _resolve_repo_identity(
        workspace, time.monotonic() + _DEFAULT_DEADLINE_SECONDS,
    )


def _same_repository(expected: RepoIdentity, actual: RepoIdentity) -> bool:
    return (
        expected.worktree_raw == actual.worktree_raw
        and expected.git_dir_raw == actual.git_dir_raw
        and expected.common_dir_raw == actual.common_dir_raw
        and expected.common_dir_device == actual.common_dir_device
        and expected.common_dir_inode == actual.common_dir_inode
        and expected.object_format == actual.object_format
        and expected.repository_id == actual.repository_id
    )


def _split_nul(value: bytes, *, deadline: float | None = None) -> list[bytes]:
    parts: list[bytes] = []
    offset = 0
    while offset < len(value):
        if deadline is not None:
            _remaining(deadline)
        end = value.find(b"\0", offset)
        if end < 0:
            end = len(value)
        if end > offset:
            parts.append(value[offset:end])
        offset = end + 1
    return parts


def _parse_index_entries(
    value: bytes, *, deadline: float | None = None,
) -> tuple[IndexEntry, ...]:
    entries: list[IndexEntry] = []
    for record in _split_nul(value, deadline=deadline):
        if deadline is not None:
            _remaining(deadline)
        try:
            metadata, path = record.split(b"\t", 1)
            mode_raw, oid_raw, stage_raw = metadata.split(b" ", 2)
            entry = IndexEntry(
                path=path,
                mode=int(mode_raw, 8),
                oid=oid_raw.decode("ascii"),
                stage=int(stage_raw),
            )
        except (ValueError, UnicodeError) as exc:
            raise SourceBoundaryError("Git returned an invalid raw index record") from exc
        entries.append(entry)
    return tuple(sorted(entries, key=lambda item: (item.path, item.stage)))


def _parse_tags(
    value: bytes, *, deadline: float | None = None,
) -> dict[bytes, bytes]:
    tags: dict[bytes, bytes] = {}
    for record in _split_nul(value, deadline=deadline):
        if deadline is not None:
            _remaining(deadline)
        if len(record) < 3 or record[1:2] != b" ":
            raise SourceBoundaryError("Git returned an invalid raw index flag record")
        tags[record[2:]] = record[:1]
    return tags


def _valid_relative_path(path: bytes) -> bool:
    if not path or b"\0" in path or path.startswith(b"/"):
        return False
    return all(part not in {b"", b".", b".."} for part in path.split(b"/"))


def _path_state(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        st.st_dev,
        st.st_ino,
        st.st_mode,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )


def _capture_path(
    repo: RepoIdentity,
    path: bytes,
    *,
    tracked: bool,
    deadline: float,
) -> ProtectedPath:
    if not _valid_relative_path(path):
        raise UnsupportedRepositoryError("Git returned an unsafe repository path")
    full_path = os.path.join(repo.worktree_raw, path)
    _remaining(deadline)
    try:
        before = os.lstat(full_path)
    except FileNotFoundError:
        if tracked:
            return ProtectedPath(path, True, "missing", None, None, None, None)
        raise _CaptureChanged(f"untracked path vanished: {os.fsdecode(path)}")
    except OSError as exc:
        raise SourceBoundaryError(
            f"protected path cannot be inspected: {os.fsdecode(path)}: {exc}"
        ) from exc

    mode = before.st_mode
    if stat.S_ISREG(mode):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(full_path, flags)
        except (FileNotFoundError, OSError) as exc:
            raise _CaptureChanged(f"protected path changed: {os.fsdecode(path)}") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (before.st_dev, before.st_ino):
                raise _CaptureChanged(f"protected path changed: {os.fsdecode(path)}")
            digest = hashlib.sha256()
            size = 0
            while True:
                _remaining(deadline)
                chunk = os.read(fd, _BUFFER_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if _path_state(opened) != _path_state(after) or size != after.st_size:
            raise _CaptureChanged(f"protected file changed: {os.fsdecode(path)}")
        return ProtectedPath(
            path, tracked, "regular", mode, size, digest.hexdigest(), None,
        )
    if stat.S_ISLNK(mode):
        try:
            target = os.readlink(full_path)
            after = os.lstat(full_path)
        except OSError as exc:
            raise _CaptureChanged(f"protected symlink changed: {os.fsdecode(path)}") from exc
        if _path_state(before) != _path_state(after):
            raise _CaptureChanged(f"protected symlink changed: {os.fsdecode(path)}")
        target_raw = os.fsencode(target)
        return ProtectedPath(
            path,
            tracked,
            "symlink",
            mode,
            len(target_raw),
            _sha256_bytes(target_raw, deadline=deadline),
            target_raw,
        )
    if stat.S_ISDIR(mode):
        return ProtectedPath(path, tracked, "directory", mode, None, None, None)
    raise UnsupportedRepositoryError(
        f"nonignored special file is unsupported: {os.fsdecode(path)}"
    )


def _ignored_paths(
    repo: RepoIdentity, paths: list[bytes], *, deadline: float,
) -> set[bytes]:
    if not paths:
        return set()
    code, output = _run_git(
        repo.worktree_raw,
        "check-ignore",
        "-z",
        "--stdin",
        deadline=deadline,
        input_data=b"\0".join(paths) + b"\0",
        ok_codes=(0, 1),
    )
    if code == 1:
        return set()
    return set(_split_nul(output, deadline=deadline))


def _scan_nonignored_specials(repo: RepoIdentity, *, deadline: float) -> None:
    frontier: list[tuple[bytes, bytes]] = [(repo.worktree_raw, b"")]
    metadata_dirs = {repo.git_dir_raw, repo.common_dir_raw}
    while frontier:
        _remaining(deadline)
        candidates: list[tuple[os.DirEntry, bytes, int]] = []
        for directory, prefix in frontier:
            try:
                with os.scandir(directory) as iterator:
                    entries = []
                    for entry in iterator:
                        _remaining(deadline)
                        entries.append(entry)
            except OSError as exc:
                raise SourceBoundaryError(
                    f"repository path cannot be enumerated: {os.fsdecode(directory)}: {exc}"
                ) from exc
            for entry in entries:
                _remaining(deadline)
                name = os.fsencode(entry.name)
                if not prefix and name == b".git":
                    continue
                relative = name if not prefix else prefix + b"/" + name
                if prefix and name == b".git":
                    raise UnsupportedRepositoryError(
                        "nonignored nested Git repository boundary is unsupported: "
                        f"{os.fsdecode(relative)}"
                    )
                try:
                    mode = entry.stat(follow_symlinks=False).st_mode
                except FileNotFoundError as exc:
                    raise _CaptureChanged(
                        f"repository entry changed: {os.fsdecode(relative)}"
                    ) from exc
                if (
                    stat.S_ISDIR(mode)
                    and os.path.realpath(os.fsencode(entry.path)) in metadata_dirs
                ):
                    continue
                candidates.append((entry, relative, mode))
        ignored = _ignored_paths(
            repo, [relative for _entry, relative, _mode in candidates], deadline=deadline,
        )
        next_frontier: list[tuple[bytes, bytes]] = []
        for entry, relative, mode in candidates:
            if relative in ignored:
                continue
            if stat.S_ISDIR(mode):
                next_frontier.append((os.fsencode(entry.path), relative))
            elif not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                raise UnsupportedRepositoryError(
                    f"nonignored special file is unsupported: {os.fsdecode(relative)}"
                )
        frontier = next_frontier


def _read_stable_small_file(
    path: bytes,
    *,
    deadline: float,
    limit: int = 64 * 1024,
) -> bytes:
    _remaining(deadline)
    try:
        before = os.lstat(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
    except OSError as exc:
        raise _CaptureChanged("Git identity file changed during capture") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise _CaptureChanged("Git identity file changed during capture")
        chunks: list[bytes] = []
        size = 0
        while True:
            _remaining(deadline)
            chunk = os.read(fd, min(_BUFFER_SIZE, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise SourceBoundaryError("Git identity file exceeds the trusted limit")
        after_open = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise _CaptureChanged("Git identity file changed during capture") from exc
    if (
        _path_state(before) != _path_state(opened)
        or _path_state(opened) != _path_state(after_open)
        or _path_state(after_open) != _path_state(after_path)
    ):
        raise _CaptureChanged("Git identity file changed during capture")
    _remaining(deadline)
    return b"".join(chunks)


def _reject_nonempty_repository_file(
    repo: RepoIdentity,
    relative: tuple[bytes, ...],
    *,
    label: str,
    deadline: float,
) -> None:
    _remaining(deadline)
    path = os.path.join(repo.common_dir_raw, *relative)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SourceBoundaryError(
            f"repository {label} state cannot be inspected: {exc}"
        ) from exc
    _remaining(deadline)
    if not stat.S_ISREG(info.st_mode) or info.st_size:
        raise UnsupportedRepositoryError(
            f"nonempty or non-regular Git {label} state is unsupported"
        )


def _reject_partial_clone_configuration(
    repo: RepoIdentity, *, deadline: float,
) -> None:
    code, configured = _run_git(
        repo.worktree_raw,
        "config",
        "--local",
        "--get",
        "extensions.partialClone",
        deadline=deadline,
        ok_codes=(0, 1),
    )
    if code == 0 and configured:
        raise UnsupportedRepositoryError(
            "Git partial/promisor clone configuration is unsupported"
        )
    code, configured = _run_git(
        repo.worktree_raw,
        "config",
        "--local",
        "--get-regexp",
        r"^remote\..*\.(promisor|partialclonefilter)$",
        deadline=deadline,
        ok_codes=(0, 1),
    )
    if code == 0 and configured:
        raise UnsupportedRepositoryError(
            "Git partial/promisor clone configuration is unsupported"
        )


def _tree_entries(
    repo: RepoIdentity, treeish: str, *, deadline: float,
) -> tuple[_TreeEntry, ...]:
    _, output = _run_git(
        repo.worktree_raw,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        treeish,
        deadline=deadline,
    )
    entries: list[_TreeEntry] = []
    for record in _split_nul(output, deadline=deadline):
        _remaining(deadline)
        try:
            metadata, path = record.split(b"\t", 1)
            mode_raw, object_type, oid_raw = metadata.split(b" ", 2)
            entry = _TreeEntry(
                path=path,
                mode=int(mode_raw, 8),
                object_type=object_type,
                oid=oid_raw.decode("ascii"),
            )
        except (ValueError, UnicodeError) as exc:
            raise SourceBoundaryError("Git returned an invalid raw tree record") from exc
        if not _valid_relative_path(entry.path):
            raise UnsupportedRepositoryError("Git tree contains an unsafe path")
        entries.append(entry)
    return tuple(entries)


def _tree_filter_names(
    repo: RepoIdentity,
    head_oid: str,
    entries: tuple[_TreeEntry, ...],
    *,
    deadline: float,
) -> set[bytes]:
    paths = [entry.path for entry in entries]
    if not paths:
        return set()
    _, output = _run_git(
        repo.worktree_raw,
        "check-attr",
        f"--source={head_oid}",
        "--stdin",
        "-z",
        "filter",
        deadline=deadline,
        input_data=b"\0".join(paths) + b"\0",
    )
    records = _split_nul(output, deadline=deadline)
    if len(records) % 3:
        raise SourceBoundaryError("Git returned an invalid raw attribute record")
    names: set[bytes] = set()
    for index in range(0, len(records), 3):
        _path, attribute, value = records[index:index + 3]
        if attribute != b"filter":
            raise SourceBoundaryError("Git returned the wrong source attribute")
        if value not in {b"unspecified", b"unset"}:
            names.add(value)
    return names


def _assert_supported_repository(
    repo: RepoIdentity, *, deadline: float, scan_specials: bool,
) -> None:
    absolute_deadline = float(deadline)
    current = _resolve_repo_identity(repo.workspace, absolute_deadline)
    if not _same_repository(repo, current):
        raise ProofStaleError("proof_stale: repository identity changed")
    _reject_nonempty_repository_file(
        repo,
        (b"info", b"grafts"),
        label="grafts",
        deadline=absolute_deadline,
    )
    _reject_nonempty_repository_file(
        repo,
        (b"objects", b"info", b"alternates"),
        label="object alternates",
        deadline=absolute_deadline,
    )
    _reject_partial_clone_configuration(repo, deadline=absolute_deadline)
    _, bare = _run_git(
        repo.worktree_raw, "rev-parse", "--is-bare-repository", deadline=absolute_deadline,
    )
    if _without_delimiter(bare) != b"false":
        raise UnsupportedRepositoryError("bare repositories are unsupported")
    _, shallow = _run_git(
        repo.worktree_raw, "rev-parse", "--is-shallow-repository", deadline=absolute_deadline,
    )
    if _without_delimiter(shallow) == b"true":
        raise UnsupportedRepositoryError("shallow repositories are unsupported")
    for key in ("core.sparseCheckout", "core.sparseCheckoutCone"):
        code, value = _run_git(
            repo.worktree_raw,
            "config",
            "--bool",
            "--get",
            key,
            deadline=absolute_deadline,
            ok_codes=(0, 1),
        )
        if code == 0 and _without_delimiter(value).strip().lower() == b"true":
            raise UnsupportedRepositoryError("sparse checkout is unsupported")
    _, replacements = _run_git(
        repo.worktree_raw,
        "for-each-ref",
        "--format=%(refname)",
        "refs/replace",
        deadline=absolute_deadline,
    )
    if replacements:
        raise UnsupportedRepositoryError("Git replace refs are unsupported")
    _, head_oid_out = _run_git(
        repo.worktree_raw,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        deadline=absolute_deadline,
    )
    head_oid = _without_delimiter(head_oid_out).decode("ascii")
    _, head_out = _run_git(
        repo.worktree_raw,
        "rev-parse",
        "--verify",
        "HEAD^{tree}",
        deadline=absolute_deadline,
    )
    tree_oid = _without_delimiter(head_out).decode("ascii")
    tree_entries = _tree_entries(repo, tree_oid, deadline=absolute_deadline)
    if any(
        entry.mode == 0o160000 or entry.object_type == b"commit"
        for entry in tree_entries
    ):
        raise UnsupportedRepositoryError("Git submodules are unsupported")
    for filter_name in _tree_filter_names(
        repo, head_oid, tree_entries, deadline=absolute_deadline,
    ):
        try:
            decoded_name = filter_name.decode("utf-8")
        except UnicodeError as exc:
            raise UnsupportedRepositoryError(
                "non-UTF-8 Git filter names are unsupported"
            ) from exc
        for suffix in ("clean", "smudge", "process"):
            code, configured = _run_git(
                repo.worktree_raw,
                "config",
                "--get",
                f"filter.{decoded_name}.{suffix}",
                deadline=absolute_deadline,
                ok_codes=(0, 1),
            )
            if code == 0 and configured:
                raise UnsupportedRepositoryError(
                    "configured Git LFS/custom clean, smudge, or process filters are unsupported"
                )
    _, index_raw = _run_git(
        repo.worktree_raw, "ls-files", "--stage", "-z", deadline=absolute_deadline,
    )
    if any(
        entry.mode == 0o160000
        for entry in _parse_index_entries(index_raw, deadline=absolute_deadline)
    ):
        raise UnsupportedRepositoryError("Git submodules are unsupported")
    if scan_specials:
        _scan_nonignored_specials(repo, deadline=absolute_deadline)


def assert_supported_repository(
    repo: RepoIdentity, *, deadline: float | None = None,
) -> None:
    """Reject repository shapes that cannot preserve exact committed bytes."""

    absolute_deadline = (
        time.monotonic() + _DEFAULT_DEADLINE_SECONDS
        if deadline is None
        else float(deadline)
    )
    _assert_supported_repository(
        repo, deadline=absolute_deadline, scan_specials=True,
    )


def _manifest_digest(
    index_entries: tuple[IndexEntry, ...],
    index_flags: tuple[IndexFlags, ...],
    worktree_entries: tuple[ProtectedPath, ...],
    staged_diff_sha256: str,
    unstaged_diff_sha256: str,
    *,
    deadline: float,
) -> str:
    fields: list[bytes] = [
        b"staged-diff",
        staged_diff_sha256.encode("ascii"),
        b"unstaged-diff",
        unstaged_diff_sha256.encode("ascii"),
    ]
    for entry in index_entries:
        _remaining(deadline)
        fields.extend((
            b"index",
            entry.path,
            oct(entry.mode).encode("ascii"),
            entry.oid.encode("ascii"),
            str(entry.stage).encode("ascii"),
        ))
    for entry in index_flags:
        _remaining(deadline)
        fields.extend((
            b"flags",
            entry.path,
            entry.tag,
            entry.fsmonitor_tag,
            b"1" if entry.assume_unchanged else b"0",
            b"1" if entry.skip_worktree else b"0",
            b"1" if entry.fsmonitor_valid else b"0",
        ))
    for entry in worktree_entries:
        _remaining(deadline)
        fields.extend((
            b"worktree",
            entry.path,
            b"1" if entry.tracked else b"0",
            entry.kind.encode("ascii"),
            b"" if entry.mode is None else oct(entry.mode).encode("ascii"),
            b"" if entry.size is None else str(entry.size).encode("ascii"),
            b"" if entry.content_sha256 is None else entry.content_sha256.encode("ascii"),
            b"" if entry.symlink_target is None else entry.symlink_target,
        ))
    return _hash_fields(
        b"bestplan-protected-manifest-v1", fields, deadline=deadline,
    )


def capture_protected_manifest(
    repo: RepoIdentity, *, deadline: float | None = None,
) -> ProtectedManifest:
    """Capture index plus tracked/untracked nonignored ambient state."""

    absolute_deadline = (
        time.monotonic() + _DEFAULT_DEADLINE_SECONDS
        if deadline is None
        else float(deadline)
    )
    _, index_raw = _run_git(
        repo.worktree_raw, "ls-files", "--stage", "-z", deadline=absolute_deadline,
    )
    index_entries = _parse_index_entries(index_raw, deadline=absolute_deadline)
    _, verbose_raw = _run_git(
        repo.worktree_raw, "ls-files", "-v", "-z", deadline=absolute_deadline,
    )
    _, fsmonitor_raw = _run_git(
        repo.worktree_raw, "ls-files", "-f", "-z", deadline=absolute_deadline,
    )
    verbose_tags = _parse_tags(verbose_raw, deadline=absolute_deadline)
    fsmonitor_tags = _parse_tags(fsmonitor_raw, deadline=absolute_deadline)
    all_flag_paths = sorted(set(verbose_tags) | set(fsmonitor_tags))
    index_flags = tuple(
        IndexFlags(
            path=path,
            tag=verbose_tags.get(path, b""),
            fsmonitor_tag=fsmonitor_tags.get(path, b""),
            assume_unchanged=verbose_tags.get(path, b"").islower(),
            skip_worktree=verbose_tags.get(path, b"").upper() == b"S",
            fsmonitor_valid=fsmonitor_tags.get(path, b"").islower(),
        )
        for path in all_flag_paths
    )
    _, cached_raw = _run_git(
        repo.worktree_raw, "ls-files", "--cached", "-z", deadline=absolute_deadline,
    )
    _, untracked_raw = _run_git(
        repo.worktree_raw,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        deadline=absolute_deadline,
    )
    tracked_paths = set(_split_nul(cached_raw, deadline=absolute_deadline))
    untracked_paths = set(_split_nul(untracked_raw, deadline=absolute_deadline))
    worktree_entries = tuple(
        _capture_path(
            repo,
            path,
            tracked=path in tracked_paths,
            deadline=absolute_deadline,
        )
        for path in sorted(tracked_paths | untracked_paths)
    )
    _, staged_diff = _run_git(
        repo.worktree_raw,
        "diff",
        "--cached",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--no-color",
        "HEAD",
        "--",
        deadline=absolute_deadline,
    )
    _, unstaged_diff = _run_git(
        repo.worktree_raw,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--no-color",
        "--",
        deadline=absolute_deadline,
    )
    _, staged_names_raw = _run_git(
        repo.worktree_raw,
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--no-color",
        "HEAD",
        "--",
        deadline=absolute_deadline,
    )
    _, unstaged_names_raw = _run_git(
        repo.worktree_raw,
        "diff",
        "--name-only",
        "-z",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--no-color",
        "--",
        deadline=absolute_deadline,
    )
    _scan_nonignored_specials(repo, deadline=absolute_deadline)
    special_flag_paths = {
        entry.path
        for entry in index_flags
        if entry.assume_unchanged or entry.skip_worktree
    }
    protected_paths = tuple(sorted(
        set(_split_nul(staged_names_raw, deadline=absolute_deadline))
        | set(_split_nul(unstaged_names_raw, deadline=absolute_deadline))
        | untracked_paths
        | special_flag_paths
    ))
    staged_diff_sha256 = _sha256_bytes(staged_diff, deadline=absolute_deadline)
    unstaged_diff_sha256 = _sha256_bytes(unstaged_diff, deadline=absolute_deadline)
    digest = _manifest_digest(
        index_entries,
        index_flags,
        worktree_entries,
        staged_diff_sha256,
        unstaged_diff_sha256,
        deadline=absolute_deadline,
    )
    return ProtectedManifest(
        index_entries=index_entries,
        index_flags=index_flags,
        worktree_entries=worktree_entries,
        protected_paths=protected_paths,
        staged_diff_sha256=staged_diff_sha256,
        unstaged_diff_sha256=unstaged_diff_sha256,
        digest=digest,
    )


def _head_read(repo: RepoIdentity, *, deadline: float) -> _SourceRead:
    _assert_supported_repository(repo, deadline=deadline, scan_specials=False)
    current = _resolve_repo_identity(repo.workspace, deadline)
    if not _same_repository(repo, current):
        raise _CaptureChanged("repository identity changed during capture")
    _, head_path_out = _run_git(
        repo.worktree_raw,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "HEAD",
        deadline=deadline,
    )
    head_path = _without_delimiter(head_path_out)
    head_raw = _read_stable_small_file(head_path, deadline=deadline)
    ref_code, ref_out = _run_git(
        repo.worktree_raw,
        "symbolic-ref",
        "-q",
        "HEAD",
        deadline=deadline,
        ok_codes=(0, 1),
    )
    head_ref = _without_delimiter(ref_out) if ref_code == 0 else None
    _, oid_out = _run_git(
        repo.worktree_raw,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        deadline=deadline,
    )
    _, tree_out = _run_git(
        repo.worktree_raw,
        "rev-parse",
        "--verify",
        "HEAD^{tree}",
        deadline=deadline,
    )
    head_oid = _without_delimiter(oid_out).decode("ascii")
    tree_oid = _without_delimiter(tree_out).decode("ascii")
    expected_length = 40 if repo.object_format == "sha1" else 64
    if any(
        len(value) != expected_length
        or any(character not in "0123456789abcdef" for character in value)
        for value in (head_oid, tree_oid)
    ):
        raise SourceBoundaryError("Git returned a non-canonical full object id")
    if head_ref is not None and head_raw != b"ref: " + head_ref + b"\n":
        raise _CaptureChanged("symbolic Git HEAD changed during capture")
    manifest = capture_protected_manifest(repo, deadline=deadline)
    return _SourceRead(
        head_symbolic=head_ref is not None,
        head_ref=head_ref,
        head_raw=head_raw,
        head_oid=head_oid,
        tree_oid=tree_oid,
        protected_manifest=manifest,
    )


def _snapshot_fingerprint(
    repo: RepoIdentity,
    read: _SourceRead,
    *,
    deadline: float | None = None,
) -> str:
    return _hash_fields(
        b"bestplan-source-snapshot-v1",
        (
            repo.repository_id.encode("ascii"),
            repo.worktree_raw,
            repo.git_dir_raw,
            repo.common_dir_raw,
            str(repo.common_dir_device).encode("ascii"),
            str(repo.common_dir_inode).encode("ascii"),
            _CAPTURE_IMPLEMENTATION_SHA256.encode("ascii"),
            read.head_raw,
            b"" if read.head_ref is None else read.head_ref,
            read.head_oid.encode("ascii"),
            read.tree_oid.encode("ascii"),
            read.protected_manifest.digest.encode("ascii"),
        ),
        deadline=deadline,
    )


def _capture_source_snapshot_in_process(
    repo: RepoIdentity, deadline: float,
) -> SourceSnapshot:
    """In-process primitive used only inside the trusted spawned helper/tests."""

    absolute_deadline = float(deadline)
    previous: _SourceRead | None = None
    for _attempt in range(_MAX_STABILIZATION_READS):
        _remaining(absolute_deadline)
        try:
            current = _head_read(repo, deadline=absolute_deadline)
        except _CaptureChanged:
            previous = None
            continue
        if previous == current:
            return SourceSnapshot(
                repo=repo,
                head_symbolic=current.head_symbolic,
                head_ref=current.head_ref,
                head_raw=current.head_raw,
                head_oid=current.head_oid,
                tree_oid=current.tree_oid,
                protected_manifest=current.protected_manifest,
                capture_implementation_sha256=_CAPTURE_IMPLEMENTATION_SHA256,
                fingerprint=_snapshot_fingerprint(
                    repo, current, deadline=absolute_deadline,
                ),
            )
        previous = current
    raise ProofStaleError(
        "proof_stale: source did not stabilize within the bounded read attempts"
    )


_CAPTURE_HELPER_BOOTSTRAP = r"""
import hashlib
import importlib.util
import os
import pickle
import stat
import sys
import types

sys.dont_write_bytecode = True
(
    module_path,
    expected_identity,
    expected_module_sha256,
    expected_interpreter_path,
    expected_interpreter_dev,
    expected_interpreter_ino,
    expected_interpreter_sha256,
    expected_git_path,
    expected_git_dev,
    expected_git_ino,
    expected_git_sha256,
) = sys.argv[1:12]

def verified_bytes(path, expected_sha256, expected_dev, expected_ino, executable):
    path_raw = os.fsencode(path)
    if os.path.normcase(os.path.realpath(path_raw)) != os.path.normcase(path_raw):
        raise RuntimeError("trusted BestPlan executable path is not canonical")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    path_before = os.stat(path_raw, follow_symlinks=False)
    fd = os.open(path_raw, flags)
    try:
        opened_before = os.fstat(fd)
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        opened_after = os.fstat(fd)
    finally:
        os.close(fd)
    path_after = os.stat(path_raw, follow_symlinks=False)
    before_state = (
        opened_before.st_dev,
        opened_before.st_ino,
        opened_before.st_mode,
        opened_before.st_size,
        opened_before.st_mtime_ns,
        opened_before.st_ctime_ns,
    )
    if (
        not stat.S_ISREG(opened_before.st_mode)
        or before_state != (
            opened_after.st_dev,
            opened_after.st_ino,
            opened_after.st_mode,
            opened_after.st_size,
            opened_after.st_mtime_ns,
            opened_after.st_ctime_ns,
        )
        or (path_before.st_dev, path_before.st_ino)
        != (opened_before.st_dev, opened_before.st_ino)
        or (path_after.st_dev, path_after.st_ino)
        != (opened_before.st_dev, opened_before.st_ino)
        or opened_before.st_dev != int(expected_dev)
        or opened_before.st_ino != int(expected_ino)
        or hashlib.sha256(b"".join(chunks)).hexdigest() != expected_sha256
        or (executable and os.name == "posix" and opened_before.st_mode & 0o111 == 0)
    ):
        raise RuntimeError("trusted BestPlan executable identity mismatch")
    return b"".join(chunks)

interpreter_path_raw = os.fsencode(expected_interpreter_path)
if os.path.normcase(os.path.realpath(os.fsencode(sys.executable))) != os.path.normcase(
    interpreter_path_raw
):
    raise RuntimeError("trusted BestPlan capture interpreter path mismatch")
verified_bytes(
    expected_interpreter_path,
    expected_interpreter_sha256,
    expected_interpreter_dev,
    expected_interpreter_ino,
    True,
)
verified_bytes(
    expected_git_path,
    expected_git_sha256,
    expected_git_dev,
    expected_git_ino,
    True,
)
module_bytes = verified_bytes(
    module_path,
    expected_module_sha256,
    os.stat(os.fsencode(module_path), follow_symlinks=False).st_dev,
    os.stat(os.fsencode(module_path), follow_symlinks=False).st_ino,
    False,
)

agent_package = types.ModuleType("agent")
agent_package.__path__ = []
sys.modules["agent"] = agent_package
module = types.ModuleType("agent.bestplan_source")
module.__file__ = module_path
module.__package__ = "agent"
module.__spec__ = importlib.util.spec_from_loader(
    "agent.bestplan_source", loader=None, origin=module_path,
)
sys.modules["agent.bestplan_source"] = module
exec(compile(module_bytes, module_path, "exec"), module.__dict__)
if (
    module._CAPTURE_INTERPRETER_PATH != interpreter_path_raw
    or module._CAPTURE_INTERPRETER_SHA256 != expected_interpreter_sha256
    or module._CAPTURE_GIT_PATH != os.fsencode(expected_git_path)
    or module._CAPTURE_GIT_SHA256 != expected_git_sha256
):
    raise RuntimeError("trusted BestPlan capture executable binding mismatch")

try:
    request_identity, repo, remaining_budget = pickle.loads(sys.stdin.buffer.read())
    if (
        request_identity != expected_identity
        or module._CAPTURE_IMPLEMENTATION_SHA256 != expected_identity
        or not isinstance(repo, module.RepoIdentity)
    ):
        raise module.SourceBoundaryError(
            "trusted capture helper implementation identity mismatch"
        )
    remaining_budget = float(remaining_budget)
    if remaining_budget <= 0.0:
        raise module.ProofStaleError(
            "proof_stale: source capture deadline expired before helper startup"
        )
    child_deadline = module.time.monotonic() + remaining_budget
    snapshot = module._capture_source_snapshot_in_process(repo, child_deadline)
    payload = ("ok", expected_identity, snapshot)
except module.SourceBoundaryError as exc:
    payload = (
        "error",
        expected_identity,
        type(exc).__name__,
        exc.code,
        str(exc),
    )
except BaseException as exc:
    payload = (
        "error",
        expected_identity,
        "SourceBoundaryError",
        "source_unavailable",
        f"trusted capture helper failed closed: {type(exc).__name__}: {exc}",
    )
sys.stdout.buffer.write(pickle.dumps(payload, protocol=5))
sys.stdout.buffer.flush()
"""


def _capture_helper_argv() -> list[str]:
    return [
        os.fsdecode(_CAPTURE_INTERPRETER_PATH),
        "-I",
        "-S",
        "-c",
        _CAPTURE_HELPER_BOOTSTRAP,
        os.fsdecode(_CAPTURE_MODULE_PATH),
        _CAPTURE_IMPLEMENTATION_SHA256,
        _CAPTURE_MODULE_SHA256,
        os.fsdecode(_CAPTURE_INTERPRETER_PATH),
        str(_CAPTURE_INTERPRETER_DEVICE),
        str(_CAPTURE_INTERPRETER_INODE),
        _CAPTURE_INTERPRETER_SHA256,
        os.fsdecode(_CAPTURE_GIT_PATH),
        str(_CAPTURE_GIT_DEVICE),
        str(_CAPTURE_GIT_INODE),
        _CAPTURE_GIT_SHA256,
    ]


def _capture_helper_environment() -> dict[str, str]:
    allowed = (
        "HOME",
        "LANG",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_CONFIG_HOME",
    )
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env["PATH"] = _CAPTURE_HELPER_PATH
    env["LC_ALL"] = "C"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _attach_capture_helper_containment(
    process: subprocess.Popen,
) -> object | None:
    """Attach a Windows kill-on-close job; POSIX uses the new session group."""

    if os.name != "nt":
        return None

    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operations", ctypes.c_ulonglong),
            ("write_operations", ctypes.c_ulonglong),
            ("other_operations", ctypes.c_ulonglong),
            ("read_bytes", ctypes.c_ulonglong),
            ("write_bytes", ctypes.c_ulonglong),
            ("other_bytes", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_time", ctypes.c_longlong),
            ("per_job_time", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic", _BasicLimitInformation),
            ("io", _IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = _ExtendedLimitInformation()
        limits.basic.limit_flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(
            job, wintypes.HANDLE(int(process._handle)),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel32.CloseHandle(job)
        raise
    return job


def _close_capture_helper_containment(handle: object | None) -> None:
    if handle is None:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


def _capture_posix_process_group(process: subprocess.Popen) -> int | None:
    if os.name != "posix":
        return None
    try:
        process_group = os.getpgid(process.pid)
    except OSError as exc:
        raise SourceBoundaryError(
            "trusted capture helper process group could not be identified"
        ) from exc
    if process_group != process.pid:
        raise SourceBoundaryError(
            "trusted capture helper does not own its isolated process group"
        )
    return process_group


def _signal_capture_helper(
    process: subprocess.Popen,
    sig: int | None,
    *,
    force: bool = False,
    process_group: int | None = None,
) -> bool:
    if os.name == "posix" and sig is not None:
        try:
            os.killpg(process.pid if process_group is None else process_group, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return True
    if process.poll() is not None:
        return True
    try:
        if force:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _wait_for_posix_group_extinction(process_group: int) -> None:
    cleanup_deadline = time.monotonic() + _CAPTURE_CLEANUP_SECONDS
    while True:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise ProofStaleError(
                "proof_stale: capture helper process group extinction query failed"
            ) from exc
        remaining = cleanup_deadline - time.monotonic()
        if remaining <= 0:
            raise ProofStaleError(
                "proof_stale: capture helper process group extinction was not proven"
            )
        time.sleep(min(0.01, remaining))


def _terminate_windows_job_and_wait(handle: object) -> None:
    import ctypes
    from ctypes import wintypes

    class _BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("total_user_time", ctypes.c_longlong),
            ("total_kernel_time", ctypes.c_longlong),
            ("period_user_time", ctypes.c_longlong),
            ("period_kernel_time", ctypes.c_longlong),
            ("total_page_faults", wintypes.DWORD),
            ("total_processes", wintypes.DWORD),
            ("active_processes", wintypes.DWORD),
            ("terminated_processes", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    if not kernel32.TerminateJobObject(handle, 1):
        raise ProofStaleError(
            "proof_stale: capture helper Windows Job termination failed"
        )
    cleanup_deadline = time.monotonic() + _CAPTURE_CLEANUP_SECONDS
    while True:
        accounting = _BasicAccountingInformation()
        if not kernel32.QueryInformationJobObject(
            handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
        ):
            raise ProofStaleError(
                "proof_stale: capture helper Windows Job extinction query failed"
            )
        if accounting.active_processes == 0:
            return
        remaining = cleanup_deadline - time.monotonic()
        if remaining <= 0:
            raise ProofStaleError(
                "proof_stale: capture helper Windows Job extinction was not proven"
            )
        time.sleep(min(0.01, remaining))


def _terminate_capture_helper(
    process: subprocess.Popen,
    containment: object | None = None,
    process_group: int | None = None,
) -> None:
    if os.name == "nt" and containment is not None:
        try:
            _terminate_windows_job_and_wait(containment)
        finally:
            _close_capture_helper_containment(containment)
        try:
            process.communicate(timeout=_CAPTURE_CLEANUP_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise ProofStaleError(
                "proof_stale: capture helper could not be reaped after Job termination"
            ) from exc
        return

    _signal_capture_helper(
        process, getattr(signal, "SIGTERM", None), process_group=process_group,
    )
    leader_reaped = False
    try:
        process.communicate(timeout=0.2)
        leader_reaped = True
    except subprocess.TimeoutExpired:
        pass
    # The leader can exit on SIGTERM while a Git/FS descendant ignores it.
    # Always kill the original POSIX process group before declaring cleanup.
    group_killed = _signal_capture_helper(
        process,
        getattr(signal, "SIGKILL", None),
        force=True,
        process_group=process_group,
    )
    if leader_reaped:
        if not group_killed:
            raise ProofStaleError(
                "proof_stale: capture helper process group could not be killed"
            )
        if os.name == "posix":
            _wait_for_posix_group_extinction(
                process.pid if process_group is None else process_group
            )
        return
    try:
        process.communicate(timeout=1.0)
    except subprocess.TimeoutExpired as exc:
        raise ProofStaleError(
            "proof_stale: capture helper could not be reaped after kill"
        ) from exc
    if not group_killed:
        raise ProofStaleError(
            "proof_stale: capture helper process group could not be killed"
        )
    if os.name == "posix":
        _wait_for_posix_group_extinction(
            process.pid if process_group is None else process_group
        )


def _raise_capture_helper_error(
    class_name: str, code: str, message: str,
) -> None:
    if code == ProofStaleError.code or class_name == "ProofStaleError":
        raise ProofStaleError(message, code=code)
    if (
        code == UnsupportedRepositoryError.code
        or class_name == "UnsupportedRepositoryError"
    ):
        raise UnsupportedRepositoryError(message, code=code)
    raise SourceBoundaryError(message, code=code)


def capture_source_snapshot(repo: RepoIdentity, deadline: float) -> SourceSnapshot:
    """Capture in an isolated, deadline-killable trusted helper process."""

    absolute_deadline = float(deadline)
    remaining_budget = _remaining(absolute_deadline)
    _assert_trusted_file_identity(
        _CAPTURE_INTERPRETER_PATH,
        _CAPTURE_INTERPRETER_SHA256,
        _CAPTURE_INTERPRETER_DEVICE,
        _CAPTURE_INTERPRETER_INODE,
        require_executable=True,
    )
    _assert_trusted_file_identity(
        _CAPTURE_GIT_PATH,
        _CAPTURE_GIT_SHA256,
        _CAPTURE_GIT_DEVICE,
        _CAPTURE_GIT_INODE,
        require_executable=True,
    )
    request = pickle.dumps(
        (_CAPTURE_IMPLEMENTATION_SHA256, repo, remaining_budget), protocol=5,
    )
    _remaining(absolute_deadline)
    try:
        process = subprocess.Popen(
            _capture_helper_argv(),
            cwd=os.sep,
            env=_capture_helper_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        raise SourceBoundaryError(
            f"trusted capture helper could not start: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        process_group = _capture_posix_process_group(process)
    except SourceBoundaryError:
        process.kill()
        process.communicate(timeout=_CAPTURE_CLEANUP_SECONDS)
        raise
    containment: object | None = None
    try:
        containment = _attach_capture_helper_containment(process)
    except OSError as exc:
        _terminate_capture_helper(process, process_group=process_group)
        raise SourceBoundaryError(
            f"trusted capture helper containment unavailable: {exc}"
        ) from exc
    try:
        try:
            timeout = _remaining(absolute_deadline)
        except SourceBoundaryError:
            owned_containment = containment
            containment = None
            _terminate_capture_helper(
                process, owned_containment, process_group,
            )
            raise
        try:
            output, stderr = process.communicate(input=request, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            owned_containment = containment
            containment = None
            _terminate_capture_helper(
                process, owned_containment, process_group,
            )
            raise ProofStaleError(
                "proof_stale: trusted capture helper exceeded the source deadline"
            ) from exc
        # A coded helper result is not proof that a Git/FS descendant exited.
        # Sweep the isolated POSIX process group before trusting any payload.
        if os.name == "posix":
            if not _signal_capture_helper(
                process,
                getattr(signal, "SIGKILL", None),
                force=True,
                process_group=process_group,
            ):
                raise ProofStaleError(
                    "proof_stale: capture helper process group cleanup failed"
                )
            if process_group is None:
                raise ProofStaleError(
                    "proof_stale: capture helper process group ownership was lost"
                )
            _wait_for_posix_group_extinction(process_group)
        elif containment is not None:
            try:
                _terminate_windows_job_and_wait(containment)
            finally:
                _close_capture_helper_containment(containment)
                containment = None
        else:
            raise ProofStaleError(
                "proof_stale: capture helper containment is unavailable"
            )
    except BaseException:
        if containment is not None or process.poll() is None:
            owned_containment = containment
            containment = None
            _terminate_capture_helper(
                process, owned_containment, process_group,
            )
        raise
    finally:
        _close_capture_helper_containment(containment)

    _remaining(absolute_deadline)
    _assert_trusted_file_identity(
        _CAPTURE_INTERPRETER_PATH,
        _CAPTURE_INTERPRETER_SHA256,
        _CAPTURE_INTERPRETER_DEVICE,
        _CAPTURE_INTERPRETER_INODE,
        require_executable=True,
    )
    _assert_trusted_file_identity(
        _CAPTURE_GIT_PATH,
        _CAPTURE_GIT_SHA256,
        _CAPTURE_GIT_DEVICE,
        _CAPTURE_GIT_INODE,
        require_executable=True,
    )
    if process.returncode != 0:
        detail = os.fsdecode(stderr[-4096:]).strip() or f"exit {process.returncode}"
        raise SourceBoundaryError(
            f"trusted capture helper failed: {detail}"
        )
    try:
        payload = pickle.loads(output)
    except (EOFError, pickle.PickleError, AttributeError, ValueError) as exc:
        raise SourceBoundaryError(
            "trusted capture helper returned an invalid response"
        ) from exc
    _remaining(absolute_deadline)
    if not isinstance(payload, tuple) or len(payload) < 3:
        raise SourceBoundaryError("trusted capture helper returned a malformed response")
    status, identity, *fields = payload
    if identity != _CAPTURE_IMPLEMENTATION_SHA256:
        raise SourceBoundaryError(
            "trusted capture helper response identity mismatch"
        )
    if status == "error" and len(fields) == 3:
        class_name, code, message = fields
        _raise_capture_helper_error(str(class_name), str(code), str(message))
    if status != "ok" or len(fields) != 1:
        raise SourceBoundaryError("trusted capture helper returned a malformed result")
    snapshot = fields[0]
    if (
        not isinstance(snapshot, SourceSnapshot)
        or snapshot.repo != repo
        or snapshot.capture_implementation_sha256
        != _CAPTURE_IMPLEMENTATION_SHA256
    ):
        raise SourceBoundaryError("trusted capture helper returned an unbound snapshot")
    return snapshot


def recapture_matches(
    expected: SourceSnapshot, *, deadline: float | None = None,
) -> bool:
    """Return whether the repository and protected state still match exactly."""

    absolute_deadline = (
        time.monotonic() + _DEFAULT_DEADLINE_SECONDS
        if deadline is None
        else float(deadline)
    )
    try:
        actual_repo = _resolve_repo_identity(expected.repo.workspace, absolute_deadline)
        if not _same_repository(expected.repo, actual_repo):
            return False
        actual = capture_source_snapshot(actual_repo, absolute_deadline)
    except SourceBoundaryError:
        return False
    return actual == expected


def _path_is_within(path: bytes, root: bytes) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _assert_tree_path_aliases(
    entries: tuple[_TreeEntry, ...], *, deadline: float,
) -> None:
    siblings: dict[tuple[str, ...], dict[str, bytes]] = {}
    for entry in entries:
        _remaining(deadline)
        normalized_parent: list[str] = []
        for component in entry.path.split(b"/"):
            _remaining(deadline)
            normalized = unicodedata.normalize(
                "NFC", os.fsdecode(component),
            ).casefold()
            parent_key = tuple(normalized_parent)
            previous = siblings.setdefault(parent_key, {}).get(normalized)
            if previous is not None and previous != component:
                raise UnsupportedRepositoryError(
                    "Git tree contains a case-fold or Unicode-normalization path alias"
                )
            siblings[parent_key][normalized] = component
            normalized_parent.append(normalized)


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    return flags | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _close_fds(fds: Iterable[int]) -> None:
    for fd in reversed(tuple(fds)):
        try:
            os.close(fd)
        except OSError:
            pass


def _open_canonical_parent_chain(
    raw_parent: bytes, *, deadline: float,
) -> tuple[bytes, tuple[int, ...], tuple[tuple[int, int], ...]]:
    _remaining(deadline)
    canonical_before = os.path.realpath(raw_parent)
    _remaining(deadline)
    if canonical_before != raw_parent:
        raise SourceBoundaryError(
            "exact-tree destination parent contains a symlink or alias"
        )
    if not os.path.isabs(raw_parent):
        raise SourceBoundaryError("exact-tree destination parent is not absolute")

    components = tuple(
        component for component in raw_parent.split(os.sep.encode()) if component
    )
    fds: list[int] = []
    identities: list[tuple[int, int]] = []
    try:
        root_fd = os.open(os.sep.encode(), _directory_open_flags())
        root_info = os.fstat(root_fd)
        fds.append(root_fd)
        identities.append((root_info.st_dev, root_info.st_ino))
        for component in components:
            _remaining(deadline)
            if component in {b"", b".", b".."}:
                raise SourceBoundaryError(
                    "exact-tree destination parent component is unsafe"
                )
            before = os.stat(
                component, dir_fd=fds[-1], follow_symlinks=False,
            )
            if not stat.S_ISDIR(before.st_mode):
                raise SourceBoundaryError(
                    "exact-tree destination parent contains a symlink or non-directory"
                )
            opened_fd = os.open(
                component, _directory_open_flags(), dir_fd=fds[-1],
            )
            opened = os.fstat(opened_fd)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                os.close(opened_fd)
                raise SourceBoundaryError(
                    "exact-tree destination parent changed while opening"
                )
            fds.append(opened_fd)
            identities.append((opened.st_dev, opened.st_ino))
        canonical_after = os.path.realpath(raw_parent)
        _remaining(deadline)
        if canonical_after != canonical_before:
            raise SourceBoundaryError(
                "exact-tree destination parent changed while opening"
            )
    except OSError as exc:
        _close_fds(fds)
        raise SourceBoundaryError(
            f"exact-tree destination parent is unsafe: {exc}"
        ) from exc
    except BaseException:
        _close_fds(fds)
        raise
    return canonical_before, tuple(fds), tuple(identities)


def _verify_destination_parent(
    prepared: _PreparedDestination, *, deadline: float,
) -> None:
    for fd, expected in zip(prepared.parent_fds, prepared.parent_identities):
        _remaining(deadline)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != expected:
            raise SourceBoundaryError(
                "exact-tree destination parent descriptor changed"
            )
    canonical, verification_fds, identities = _open_canonical_parent_chain(
        prepared.raw_parent, deadline=deadline,
    )
    try:
        if (
            canonical != prepared.canonical_parent
            or identities != prepared.parent_identities
        ):
            raise SourceBoundaryError(
                "exact-tree destination parent identity changed"
            )
    finally:
        _close_fds(verification_fds)


def _prepare_destination(
    repo: RepoIdentity,
    destination: str | os.PathLike[str],
    *,
    deadline: float,
) -> _PreparedDestination:
    _remaining(deadline)
    raw = os.path.abspath(os.path.expanduser(os.fsencode(destination)))
    leaf = os.path.basename(raw)
    if leaf in {b"", b".", b".."} or b"/" in leaf or b"\0" in leaf:
        raise SourceBoundaryError("exact-tree destination path is unsafe")
    raw_parent = os.path.dirname(raw)
    canonical_parent = os.path.realpath(raw_parent)
    canonical = os.path.join(canonical_parent, leaf)
    for source_root in (
        repo.worktree_raw,
        repo.git_dir_raw,
        repo.common_dir_raw,
    ):
        if _path_is_within(canonical, source_root):
            raise SourceBoundaryError(
                "exact-tree destination cannot be inside the source repository or Git state"
            )

    canonical_parent, parent_fds, parent_identities = _open_canonical_parent_chain(
        raw_parent, deadline=deadline,
    )
    parent_fd = parent_fds[-1]
    root_fd: int | None = None
    try:
        _remaining(deadline)
        try:
            os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SourceBoundaryError(
                "exact-tree destination must not already exist"
            )
        try:
            os.mkdir(leaf, mode=0o700, dir_fd=parent_fd)
            created = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            root_fd = os.open(leaf, _directory_open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise SourceBoundaryError(
                f"exact-tree destination could not be created safely: {exc}"
            ) from exc
        opened = os.fstat(root_fd)
        if not stat.S_ISDIR(created.st_mode) or (
            created.st_dev,
            created.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise SourceBoundaryError(
                "exact-tree destination changed while it was being opened"
            )
        prepared = _PreparedDestination(
            path=canonical,
            leaf=leaf,
            root_fd=root_fd,
            root_identity=(opened.st_dev, opened.st_ino),
            raw_parent=raw_parent,
            canonical_parent=canonical_parent,
            parent_fds=parent_fds,
            parent_identities=parent_identities,
        )
        _verify_destination_parent(prepared, deadline=deadline)
        return prepared
    except BaseException:
        if root_fd is not None:
            os.close(root_fd)
        _close_fds(parent_fds)
        raise


def _batch_blobs(
    repo: RepoIdentity,
    entries: tuple[_TreeEntry, ...],
    *,
    deadline: float,
) -> tuple[bytes, ...]:
    request_parts: list[bytes] = []
    for entry in entries:
        _remaining(deadline)
        request_parts.append(entry.oid.encode("ascii") + b"\n")
    request = b"".join(request_parts)
    _remaining(deadline)
    _, output = _run_git(
        repo.worktree_raw,
        "cat-file",
        "--batch",
        deadline=deadline,
        input_data=request,
    )
    blobs: list[bytes] = []
    offset = 0
    for entry in entries:
        _remaining(deadline)
        header_end = output.find(b"\n", offset)
        if header_end < 0:
            raise SourceBoundaryError("git cat-file returned an incomplete blob header")
        header = output[offset:header_end]
        try:
            returned_oid, object_type, size_raw = header.split(b" ", 2)
            size = int(size_raw)
        except (ValueError, UnicodeError) as exc:
            raise SourceBoundaryError(
                "git cat-file returned an invalid blob header"
            ) from exc
        if returned_oid.decode("ascii") != entry.oid or object_type != b"blob":
            raise SourceBoundaryError("git cat-file returned the wrong committed object")
        content_start = header_end + 1
        content_end = content_start + size
        if size < 0 or content_end >= len(output) or output[content_end:content_end + 1] != b"\n":
            raise SourceBoundaryError("git cat-file returned an incomplete blob")
        blobs.append(output[content_start:content_end])
        offset = content_end + 1
    if offset != len(output):
        raise SourceBoundaryError("git cat-file returned trailing batch data")
    return tuple(blobs)


def _export_parent_fd(
    root_fd: int,
    directories: dict[tuple[bytes, ...], int],
    parts: tuple[bytes, ...],
    *,
    deadline: float,
) -> int:
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    key: tuple[bytes, ...] = ()
    for component in parts:
        _remaining(deadline)
        next_key = (*key, component)
        if next_key not in directories:
            try:
                os.mkdir(component, mode=0o755, dir_fd=directories[key])
                directories[next_key] = os.open(
                    component, directory_flags, dir_fd=directories[key],
                )
            except OSError as exc:
                raise SourceBoundaryError(
                    f"exact-tree directory changed during export: {exc}"
                ) from exc
        key = next_key
    return directories.get(key, root_fd)


def _write_export_file(
    parent_fd: int,
    name: bytes,
    content: bytes,
    mode: int,
    *,
    deadline: float,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, mode, dir_fd=parent_fd)
    except OSError as exc:
        raise SourceBoundaryError(
            f"exact-tree file could not be created safely: {exc}"
        ) from exc
    try:
        offset = 0
        while offset < len(content):
            _remaining(deadline)
            written = os.write(fd, content[offset:offset + _BUFFER_SIZE])
            if written <= 0:
                raise SourceBoundaryError("exact-tree file write made no progress")
            offset += written
        _remaining(deadline)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def export_exact_tree(
    snapshot: SourceSnapshot, destination: str | os.PathLike[str],
) -> None:
    """Materialize only the captured committed tree, bypassing checkout filters."""

    deadline = time.monotonic() + _DEFAULT_DEADLINE_SECONDS
    if not recapture_matches(snapshot, deadline=deadline):
        raise ProofStaleError("proof_stale: repository or protected state changed")
    entries = _tree_entries(snapshot.repo, snapshot.tree_oid, deadline=deadline)
    if any(entry.object_type != b"blob" for entry in entries):
        raise UnsupportedRepositoryError("exact source tree contains a non-blob entry")
    for entry in entries:
        _remaining(deadline)
        if entry.mode not in {0o100644, 0o100755, 0o120000}:
            raise UnsupportedRepositoryError(
                f"unsupported Git tree mode: {entry.mode:o}"
            )
    _assert_tree_path_aliases(entries, deadline=deadline)
    blobs = _batch_blobs(snapshot.repo, entries, deadline=deadline)
    for entry, content in zip(entries, blobs):
        if entry.mode == 0o120000 and (not content or b"\0" in content):
            raise UnsupportedRepositoryError("Git tree contains an unsafe symlink target")

    prepared = _prepare_destination(
        snapshot.repo, destination, deadline=deadline,
    )
    directories: dict[tuple[bytes, ...], int] = {(): prepared.root_fd}
    try:
        for entry, content in zip(entries, blobs):
            _remaining(deadline)
            parts = tuple(entry.path.split(b"/"))
            parent_fd = _export_parent_fd(
                prepared.root_fd, directories, parts[:-1], deadline=deadline,
            )
            if entry.mode == 0o120000:
                try:
                    os.symlink(content, parts[-1], dir_fd=parent_fd)
                except OSError as exc:
                    raise SourceBoundaryError(
                        f"exact-tree symlink could not be created safely: {exc}"
                    ) from exc
            else:
                _write_export_file(
                    parent_fd,
                    parts[-1],
                    content,
                    entry.mode & 0o777,
                    deadline=deadline,
                )
        _remaining(deadline)
        os.fchmod(prepared.root_fd, 0o755)
        final = os.stat(
            prepared.leaf,
            dir_fd=prepared.parent_fds[-1],
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(final.st_mode) or (
            final.st_dev,
            final.st_ino,
        ) != prepared.root_identity:
            raise SourceBoundaryError(
                "exact-tree destination was substituted during export"
            )
        _verify_destination_parent(prepared, deadline=deadline)
    finally:
        _close_fds(directories.values())
        _close_fds(prepared.parent_fds)
