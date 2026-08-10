"""Fail-closed Git source capture for executable BestPlan workspaces.

The candidate source is always the committed ``HEAD`` tree.  Index and
working-tree state is recorded separately so it can be protected without ever
being imported into an execution sandbox.
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import pickle
import secrets
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import BinaryIO, Iterable


DEFAULT_SOURCE_OPERATION_SECONDS = 20.0
_DEFAULT_DEADLINE_SECONDS = DEFAULT_SOURCE_OPERATION_SECONDS
_BUFFER_SIZE = 1024 * 1024
_MAX_STABILIZATION_READS = 16
_STABLE_EXPORT_OBSERVATIONS = 2
_CAPTURE_CLEANUP_SECONDS = 1.0
_EXPORT_CLEANUP_SECONDS = 1.0
_MAX_GIT_METADATA_BYTES = 64 * 1024 * 1024
_MAX_GIT_STDERR_BYTES = 1024 * 1024
_MAX_GIT_INPUT_BYTES = 64 * 1024 * 1024
_MAX_HELPER_RESPONSE_BYTES = 256 * 1024 * 1024
_MAX_AUTHORITY_RESPONSE_BYTES = 64 * 1024
_MAX_LEGACY_V1_RESPONSE_BYTES = 64 * 1024
_MAX_DIFF_BYTES = 256 * 1024 * 1024
_MAX_PATH_BYTES = 4096
_MAX_TOTAL_PATH_BYTES = 64 * 1024 * 1024
_MAX_PROTECTED_PATHS = 250_000
_MAX_INDEX_ENTRIES = 250_000
_MAX_TREE_ENTRIES = 250_000
_MAX_BLOB_BYTES = 512 * 1024 * 1024
_MAX_EXPORT_BYTES = 4 * 1024 * 1024 * 1024
_MAX_SYMLINK_TARGET_BYTES = 64 * 1024
_EXPORT_DIR_FD_SUPPORTED = hasattr(os, "O_NOFOLLOW") and all(
    operation in os.supports_dir_fd
    for operation in (os.open, os.mkdir, os.stat, os.unlink, os.rmdir, os.symlink)
)


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
    intent_to_add: bool


@dataclass(frozen=True)
class _RawIndexEntry:
    path: bytes
    mode: int
    oid: str
    stage: int
    assume_unchanged: bool
    skip_worktree: bool
    intent_to_add: bool


@dataclass(frozen=True)
class ProtectedPath:
    path: bytes
    tracked: bool
    kind: str
    mode: int | None
    size: int | None
    content_sha256: str | None
    symlink_target: bytes | None
    git_oid: str | None = None


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
class _ExportWitness:
    path: bytes
    size: int
    content_sha256: str


@dataclass(frozen=True)
class _CaptureAuthority:
    module_path: bytes
    module_sha256: str
    module_device: int
    module_inode: int
    interpreter_path: bytes
    interpreter_sha256: str
    interpreter_device: int
    interpreter_inode: int
    git_path: bytes
    git_sha256: str
    git_device: int
    git_inode: int
    implementation_sha256: str
    helper_path: str


@dataclass(frozen=True)
class _PreparedDestination:
    path: bytes
    final_leaf: bytes
    staging_leaf: bytes
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


def strong_source_capture_supported(
    *, os_name: str | None = None, platform: str | None = None,
) -> bool:
    """Return whether the N-1 strong source verifier exists on this host."""

    return (
        (os.name if os_name is None else os_name) == "posix"
        and (sys.platform if platform is None else platform) == "darwin"
    )


def _legacy_v1_git_path(
    *, os_name: str | None = None, platform: str | None = None,
) -> str:
    """Return the fixed Git path used only by candidate-only legacy V1 proofs."""

    host_os = os.name if os_name is None else os_name
    host_platform = sys.platform if platform is None else platform
    if host_os == "posix" and (
        host_platform == "darwin" or host_platform.startswith("linux")
    ):
        return "/usr/bin/git"
    if host_os == "nt" and host_platform.startswith("win"):
        return r"C:\Program Files\Git\cmd\git.exe"
    raise SourceBoundaryError(
        "legacy V1 source fingerprinting is unsupported on this host"
    )


def _legacy_v1_helper_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("TEMP", "TMP", "TMPDIR")
        if key in os.environ
    }
    if os.name == "posix":
        environment["PATH"] = "/usr/bin:/bin"
    else:
        environment["PATH"] = os.pathsep.join((
            r"C:\Program Files\Git\cmd",
            r"C:\Program Files\Git\bin",
            r"C:\Windows\System32",
        ))
        environment["SYSTEMROOT"] = r"C:\Windows"
    environment.update({"LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"})
    return environment


def _git_environment(
    authority: _CaptureAuthority | None = None,
) -> dict[str, str]:
    authority = _get_capture_authority() if authority is None else authority
    allowed = ("HOME", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "XDG_CONFIG_HOME")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    env["PATH"] = authority.helper_path
    return env


def _remaining(deadline: float) -> float:
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise ProofStaleError("proof_stale: source capture deadline expired")
    return remaining


def _trusted_git_argv(
    authority: _CaptureAuthority, args: tuple[str, ...],
) -> list[str]:
    return [
        os.fsdecode(authority.git_path),
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        *args,
    ]


def _read_bounded_file(
    stream: BinaryIO,
    *,
    limit: int,
    label: str,
    deadline: float,
    digest_only: bool,
) -> bytes | str:
    _remaining(deadline)
    size = os.fstat(stream.fileno()).st_size
    if size > limit:
        raise UnsupportedRepositoryError(f"{label} exceeds the trusted limit")
    stream.seek(0)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    while True:
        _remaining(deadline)
        chunk = stream.read(min(_BUFFER_SIZE, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise UnsupportedRepositoryError(f"{label} exceeds the trusted limit")
        if digest_only:
            digest.update(chunk)
        else:
            chunks.append(chunk)
    _remaining(deadline)
    return digest.hexdigest() if digest_only else b"".join(chunks)


def _stop_git_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise ProofStaleError(
                "proof_stale: trusted Git process could not be stopped"
            ) from exc
    try:
        process.wait(timeout=_CAPTURE_CLEANUP_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ProofStaleError(
            "proof_stale: trusted Git process could not be reaped"
        ) from exc


def _run_git_output(
    cwd: bytes | str,
    *args: str,
    deadline: float | None = None,
    input_data: bytes | None = None,
    ok_codes: tuple[int, ...] = (0,),
    max_output_bytes: int,
    digest_only: bool,
) -> tuple[int, bytes | str]:
    absolute_deadline = (
        time.monotonic() + _DEFAULT_DEADLINE_SECONDS
        if deadline is None
        else float(deadline)
    )
    authority = _get_capture_authority(absolute_deadline)
    if input_data is not None and len(input_data) > _MAX_GIT_INPUT_BYTES:
        raise UnsupportedRepositoryError("Git input metadata exceeds the trusted limit")
    stdin_file: BinaryIO | None = None
    stdout_file: BinaryIO | None = None
    stderr_file: BinaryIO | None = None
    process: subprocess.Popen | None = None
    try:
        if input_data is not None:
            stdin_file = tempfile.TemporaryFile()
            for offset in range(0, len(input_data), _BUFFER_SIZE):
                _remaining(absolute_deadline)
                stdin_file.write(input_data[offset:offset + _BUFFER_SIZE])
            stdin_file.seek(0)
        stdout_file = tempfile.TemporaryFile()
        stderr_file = tempfile.TemporaryFile()
        process = subprocess.Popen(
            _trusted_git_argv(authority, tuple(args)),
            cwd=cwd,
            env=_git_environment(authority),
            stdin=subprocess.DEVNULL if stdin_file is None else stdin_file,
            stdout=stdout_file,
            stderr=stderr_file,
            close_fds=True,
        )
    except OSError as exc:
        raise SourceBoundaryError(
            f"trusted Git command unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        assert stdout_file is not None and stderr_file is not None and process is not None
        while process.poll() is None:
            try:
                remaining = _remaining(absolute_deadline)
            except SourceBoundaryError:
                _stop_git_process(process)
                raise
            if os.fstat(stdout_file.fileno()).st_size > max_output_bytes:
                _stop_git_process(process)
                raise UnsupportedRepositoryError(
                    f"Git {' '.join(args)} output exceeds the trusted limit"
                )
            if os.fstat(stderr_file.fileno()).st_size > _MAX_GIT_STDERR_BYTES:
                _stop_git_process(process)
                raise SourceBoundaryError(
                    f"trusted Git {' '.join(args)} stderr exceeds the trusted limit"
                )
            time.sleep(min(0.005, remaining))
        _remaining(absolute_deadline)
        if process.returncode not in ok_codes:
            detail_raw = _read_bounded_file(
                stderr_file,
                limit=_MAX_GIT_STDERR_BYTES,
                label="Git stderr",
                deadline=absolute_deadline,
                digest_only=False,
            )
            assert isinstance(detail_raw, bytes)
            detail = os.fsdecode(detail_raw.strip()) or f"exit {process.returncode}"
            raise SourceBoundaryError(
                f"trusted Git command failed ({' '.join(args)}): {detail}"
            )
        output = _read_bounded_file(
            stdout_file,
            limit=max_output_bytes,
            label=f"Git {' '.join(args)} output",
            deadline=absolute_deadline,
            digest_only=digest_only,
        )
        return process.returncode, output
    finally:
        if process is not None and process.poll() is None:
            _stop_git_process(process)
        for stream in (stdin_file, stdout_file, stderr_file):
            if stream is not None:
                stream.close()


def _run_git(
    cwd: bytes | str,
    *args: str,
    deadline: float | None = None,
    input_data: bytes | None = None,
    ok_codes: tuple[int, ...] = (0,),
) -> tuple[int, bytes]:
    code, output = _run_git_output(
        cwd,
        *args,
        deadline=deadline,
        input_data=input_data,
        ok_codes=ok_codes,
        max_output_bytes=_MAX_GIT_METADATA_BYTES,
        digest_only=False,
    )
    assert isinstance(output, bytes)
    return code, output


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
    path: bytes,
    *,
    require_executable: bool = False,
    deadline: float | None = None,
) -> tuple[str, int, int]:
    if deadline is not None:
        _remaining(deadline)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if deadline is not None:
            _remaining(deadline)
        before = os.fstat(fd)
        digest = hashlib.sha256()
        while True:
            if deadline is not None:
                _remaining(deadline)
            chunk = os.read(fd, _BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        if deadline is not None:
            _remaining(deadline)
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
        raise RuntimeError("BestPlan capture implementation changed during verification")
    return digest.hexdigest(), before.st_dev, before.st_ino


def _loaded_file_sha256(path: bytes) -> str:
    digest, _device, _inode = _stable_file_identity(path)
    return digest


def _helper_path_for(interpreter_path: bytes, git_path: bytes) -> str:
    if os.name == "posix":
        return "/usr/bin:/bin"
    entries = [
        os.fsdecode(os.path.dirname(git_path)),
        os.fsdecode(os.path.dirname(interpreter_path)),
    ]
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    entries.extend((os.path.join(system_root, "System32"), system_root))
    return os.pathsep.join(dict.fromkeys(entries))


def _make_capture_authority(
    *,
    module_path: bytes,
    module_sha256: str,
    module_device: int,
    module_inode: int,
    interpreter_path: bytes,
    interpreter_sha256: str,
    interpreter_device: int,
    interpreter_inode: int,
    git_path: bytes,
    git_sha256: str,
    git_device: int,
    git_inode: int,
) -> _CaptureAuthority:
    implementation_sha256 = _hash_fields(
        b"bestplan-capture-authority-v4",
        (
            module_path,
            module_sha256.encode("ascii"),
            str(module_device).encode("ascii"),
            str(module_inode).encode("ascii"),
            interpreter_path,
            interpreter_sha256.encode("ascii"),
            str(interpreter_device).encode("ascii"),
            str(interpreter_inode).encode("ascii"),
            git_path,
            git_sha256.encode("ascii"),
            str(git_device).encode("ascii"),
            str(git_inode).encode("ascii"),
            sys.version.encode("utf-8"),
            str(sys.implementation.cache_tag or "").encode("ascii"),
        ),
    )
    return _CaptureAuthority(
        module_path=module_path,
        module_sha256=module_sha256,
        module_device=module_device,
        module_inode=module_inode,
        interpreter_path=interpreter_path,
        interpreter_sha256=interpreter_sha256,
        interpreter_device=interpreter_device,
        interpreter_inode=interpreter_inode,
        git_path=git_path,
        git_sha256=git_sha256,
        git_device=git_device,
        git_inode=git_inode,
        implementation_sha256=implementation_sha256,
        helper_path=_helper_path_for(interpreter_path, git_path),
    )


_AUTHORITY_VERIFIER_BOOTSTRAP = r"""
import hashlib
import json
import os
import stat
import sys

TRUST_ROOT = b"/usr/bin/python3"

def fail(message):
    raise RuntimeError(message)

def decode_path(value, label):
    if not isinstance(value, str) or len(value) > 32768:
        fail("invalid " + label)
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        fail("invalid " + label)
    if not raw or b"\0" in raw:
        fail("invalid " + label)
    canonical = os.path.realpath(os.path.abspath(raw))
    if not os.path.isabs(canonical):
        fail("non-absolute " + label)
    return canonical

def stable_identity(path, executable, system_owned):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    path_before = os.stat(path, follow_symlinks=False)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    path_after = os.stat(path, follow_symlinks=False)
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
        or (path_before.st_dev, path_before.st_ino) != (before.st_dev, before.st_ino)
        or (path_after.st_dev, path_after.st_ino) != (before.st_dev, before.st_ino)
        or (executable and before.st_mode & 0o111 == 0)
        or (system_owned and (before.st_uid != 0 or before.st_mode & 0o022))
    ):
        fail("trusted file changed during verification")
    return {
        "path": path.hex(),
        "sha256": digest.hexdigest(),
        "device": before.st_dev,
        "inode": before.st_ino,
    }

root = os.stat(TRUST_ROOT, follow_symlinks=False)
if (
    not stat.S_ISREG(root.st_mode)
    or root.st_uid != 0
    or root.st_mode & 0o022
    or root.st_mode & 0o111 == 0
):
    fail("fixed authority verifier trust root is unsafe")

request_raw = sys.stdin.buffer.read(65537)
if len(request_raw) > 65536:
    fail("authority verifier request exceeds its limit")
request = json.loads(request_raw.decode("ascii"))
if not isinstance(request, dict) or set(request) != {
    "module_path", "interpreter_path", "expected"
}:
    fail("invalid authority verifier request")
expected = request["expected"]
module_path = decode_path(request["module_path"], "module path")
interpreter_path = decode_path(request["interpreter_path"], "interpreter path")
if expected is None:
    git_path = b"/usr/bin/git"
elif isinstance(expected, dict):
    if set(expected) != {"module", "interpreter", "git"}:
        fail("invalid expected authority identity")
    module_path = decode_path(expected["module"]["path"], "expected module path")
    interpreter_path = decode_path(
        expected["interpreter"]["path"], "expected interpreter path"
    )
    git_path = decode_path(expected["git"]["path"], "expected Git path")
else:
    fail("invalid expected authority identity")
result = {
    "module": stable_identity(module_path, False, False),
    "interpreter": stable_identity(interpreter_path, True, False),
    "git": stable_identity(git_path, True, True),
}
if expected is not None and result != expected:
    fail("trusted capture authority identity changed")
sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
sys.stdout.flush()
"""


_LEGACY_V1_HELPER_BOOTSTRAP = r"""
import importlib.util
import json
import sys
import time

request = json.loads(sys.stdin.buffer.read(65537).decode("utf-8"))
spec = importlib.util.spec_from_file_location("agent.bestplan_source", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
try:
    deadline = time.monotonic() + float(request["budget"])
    if request["operation"] == "fingerprint":
        result = module._capture_legacy_v1_in_process(
            request["workspace"], deadline,
        )
    elif request["operation"] == "inspect":
        result = module._inspect_workspace_boundary_in_process(
            request["workspace"], deadline,
        )
    else:
        raise module.SourceBoundaryError("unsupported legacy V1 operation")
    payload = {"ok": True, "result": result}
except module.SourceBoundaryError as exc:
    payload = {"ok": False, "code": exc.code, "message": str(exc)}
except BaseException as exc:
    payload = {
        "ok": False,
        "code": "source_unavailable",
        "message": "legacy V1 helper failed closed: "
        + type(exc).__name__
        + ": "
        + str(exc),
    }
sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
sys.stdout.flush()
"""


def _authority_verifier_argv() -> list[str]:
    if os.name != "posix" or sys.platform != "darwin":
        raise SourceBoundaryError(
            "trusted source authority verification is unsupported on this host"
        )
    return [
        "/usr/bin/python3",
        "-I",
        "-S",
        "-c",
        _AUTHORITY_VERIFIER_BOOTSTRAP,
    ]


def _authority_verifier_environment() -> dict[str, str]:
    env = {
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in ("SYSTEMROOT",):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _authority_identity_payload(authority: _CaptureAuthority) -> dict[str, object]:
    def item(path: bytes, sha256: str, device: int, inode: int) -> dict[str, object]:
        return {
            "path": path.hex(),
            "sha256": sha256,
            "device": device,
            "inode": inode,
        }

    return {
        "module": item(
            authority.module_path,
            authority.module_sha256,
            authority.module_device,
            authority.module_inode,
        ),
        "interpreter": item(
            authority.interpreter_path,
            authority.interpreter_sha256,
            authority.interpreter_device,
            authority.interpreter_inode,
        ),
        "git": item(
            authority.git_path,
            authority.git_sha256,
            authority.git_device,
            authority.git_inode,
        ),
    }


def _parse_authority_identity(value: object) -> _CaptureAuthority:
    if not isinstance(value, dict) or set(value) != {"module", "interpreter", "git"}:
        raise SourceBoundaryError("trusted authority verifier returned invalid metadata")

    def item(label: str) -> tuple[bytes, str, int, int]:
        raw = value[label]
        if not isinstance(raw, dict) or set(raw) != {
            "path", "sha256", "device", "inode"
        }:
            raise SourceBoundaryError(
                "trusted authority verifier returned invalid metadata"
            )
        if (
            not isinstance(raw["path"], str)
            or not isinstance(raw["sha256"], str)
            or type(raw["device"]) is not int
            or type(raw["inode"]) is not int
        ):
            raise SourceBoundaryError(
                "trusted authority verifier returned invalid metadata"
            )
        try:
            path = bytes.fromhex(raw["path"])
        except ValueError as exc:
            raise SourceBoundaryError(
                "trusted authority verifier returned invalid metadata"
            ) from exc
        digest = raw["sha256"]
        device = raw["device"]
        inode = raw["inode"]
        if (
            not path
            or b"\0" in path
            or not os.path.isabs(path)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or device < 0
            or inode <= 0
        ):
            raise SourceBoundaryError(
                "trusted authority verifier returned invalid identity"
            )
        return path, digest, device, inode

    module = item("module")
    interpreter = item("interpreter")
    git = item("git")
    return _make_capture_authority(
        module_path=module[0],
        module_sha256=module[1],
        module_device=module[2],
        module_inode=module[3],
        interpreter_path=interpreter[0],
        interpreter_sha256=interpreter[1],
        interpreter_device=interpreter[2],
        interpreter_inode=interpreter[3],
        git_path=git[0],
        git_sha256=git[1],
        git_device=git[2],
        git_inode=git[3],
    )


def _run_authority_verifier(
    *, deadline: float, expected: _CaptureAuthority | None,
) -> _CaptureAuthority:
    absolute_deadline = float(deadline)
    _remaining(absolute_deadline)
    request = json.dumps(
        {
            "module_path": os.fsencode(__file__).hex(),
            "interpreter_path": os.fsencode(sys.executable).hex(),
            "expected": (
                None if expected is None else _authority_identity_payload(expected)
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    try:
        stdout_file = tempfile.TemporaryFile()
        stderr_file = tempfile.TemporaryFile()
    except OSError as exc:
        raise SourceBoundaryError(
            f"trusted authority verifier output could not be isolated: {exc}"
        ) from exc
    try:
        try:
            process = subprocess.Popen(
                _authority_verifier_argv(),
                cwd=os.sep,
                env=_authority_verifier_environment(),
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                close_fds=True,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise SourceBoundaryError(
                f"trusted authority verifier could not start: {exc}"
            ) from exc
        try:
            process_group = _capture_posix_process_group(process)
        except SourceBoundaryError:
            process.kill()
            process.communicate(timeout=_CAPTURE_CLEANUP_SECONDS)
            raise
        containment: object | None = None
        try:
            try:
                containment = _attach_capture_helper_containment(process)
            except OSError as exc:
                raise SourceBoundaryError(
                    f"trusted authority verifier containment unavailable: {exc}"
                ) from exc
            try:
                process.communicate(
                    input=request, timeout=_remaining(absolute_deadline),
                )
            except subprocess.TimeoutExpired as exc:
                raise ProofStaleError(
                    "proof_stale: trusted authority verifier exceeded the source deadline"
                ) from exc
            if os.name == "posix":
                if process_group is None or not _signal_capture_helper(
                    process,
                    getattr(signal, "SIGKILL", None),
                    force=True,
                    process_group=process_group,
                ):
                    raise ProofStaleError(
                        "proof_stale: authority verifier process group cleanup failed"
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
                    "proof_stale: authority verifier containment is unavailable"
                )
        except BaseException:
            owned_containment = containment
            containment = None
            _terminate_capture_helper(
                process, owned_containment, process_group,
            )
            raise
        finally:
            _close_capture_helper_containment(containment)

        _remaining(absolute_deadline)
        output = _read_bounded_file(
            stdout_file,
            limit=_MAX_AUTHORITY_RESPONSE_BYTES,
            label="trusted authority verifier response",
            deadline=absolute_deadline,
            digest_only=False,
        )
        stderr = _read_bounded_file(
            stderr_file,
            limit=_MAX_GIT_STDERR_BYTES,
            label="trusted authority verifier stderr",
            deadline=absolute_deadline,
            digest_only=False,
        )
        assert isinstance(output, bytes) and isinstance(stderr, bytes)
        if process.returncode != 0:
            detail = os.fsdecode(stderr[-4096:]).strip() or f"exit {process.returncode}"
            raise SourceBoundaryError(
                f"trusted source capture authority is unavailable: {detail}"
            )
        try:
            decoded = json.loads(output.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SourceBoundaryError(
                "trusted authority verifier returned an invalid response"
            ) from exc
        authority = _parse_authority_identity(decoded)
        if expected is not None and authority != expected:
            raise SourceBoundaryError("trusted capture authority identity changed")
        return authority
    finally:
        stdout_file.close()
        stderr_file.close()


def _verify_capture_authority(
    authority: _CaptureAuthority, *, deadline: float,
) -> None:
    verified = _run_authority_verifier(deadline=deadline, expected=authority)
    if verified != authority:
        raise SourceBoundaryError("trusted capture authority identity changed")


def _build_capture_authority(deadline: float) -> _CaptureAuthority:
    return _run_authority_verifier(deadline=deadline, expected=None)


_AUTHORITY_LOCK = threading.Lock()
_CAPTURE_AUTHORITY: _CaptureAuthority | None = None
_CAPTURE_AUTHORITY_PRESEEDED = False
_CAPTURE_MODULE_PATH: bytes | None = None
_CAPTURE_MODULE_SHA256: str | None = None
_CAPTURE_MODULE_DEVICE: int | None = None
_CAPTURE_MODULE_INODE: int | None = None
_CAPTURE_INTERPRETER_PATH: bytes | None = None
_CAPTURE_INTERPRETER_SHA256: str | None = None
_CAPTURE_INTERPRETER_DEVICE: int | None = None
_CAPTURE_INTERPRETER_INODE: int | None = None
_CAPTURE_GIT_PATH: bytes | None = None
_CAPTURE_GIT_SHA256: str | None = None
_CAPTURE_GIT_DEVICE: int | None = None
_CAPTURE_GIT_INODE: int | None = None
_CAPTURE_IMPLEMENTATION_SHA256: str | None = None
_CAPTURE_HELPER_PATH: str | None = None


def _publish_capture_authority(authority: _CaptureAuthority) -> None:
    global _CAPTURE_MODULE_PATH
    global _CAPTURE_MODULE_SHA256
    global _CAPTURE_MODULE_DEVICE
    global _CAPTURE_MODULE_INODE
    global _CAPTURE_INTERPRETER_PATH
    global _CAPTURE_INTERPRETER_SHA256
    global _CAPTURE_INTERPRETER_DEVICE
    global _CAPTURE_INTERPRETER_INODE
    global _CAPTURE_GIT_PATH
    global _CAPTURE_GIT_SHA256
    global _CAPTURE_GIT_DEVICE
    global _CAPTURE_GIT_INODE
    global _CAPTURE_IMPLEMENTATION_SHA256
    global _CAPTURE_HELPER_PATH
    _CAPTURE_MODULE_PATH = authority.module_path
    _CAPTURE_MODULE_SHA256 = authority.module_sha256
    _CAPTURE_MODULE_DEVICE = authority.module_device
    _CAPTURE_MODULE_INODE = authority.module_inode
    _CAPTURE_INTERPRETER_PATH = authority.interpreter_path
    _CAPTURE_INTERPRETER_SHA256 = authority.interpreter_sha256
    _CAPTURE_INTERPRETER_DEVICE = authority.interpreter_device
    _CAPTURE_INTERPRETER_INODE = authority.interpreter_inode
    _CAPTURE_GIT_PATH = authority.git_path
    _CAPTURE_GIT_SHA256 = authority.git_sha256
    _CAPTURE_GIT_DEVICE = authority.git_device
    _CAPTURE_GIT_INODE = authority.git_inode
    _CAPTURE_IMPLEMENTATION_SHA256 = authority.implementation_sha256
    _CAPTURE_HELPER_PATH = authority.helper_path


def _seed_capture_authority(authority: _CaptureAuthority) -> None:
    global _CAPTURE_AUTHORITY
    global _CAPTURE_AUTHORITY_PRESEEDED
    with _AUTHORITY_LOCK:
        if _CAPTURE_AUTHORITY is not None and _CAPTURE_AUTHORITY != authority:
            raise SourceBoundaryError("trusted capture authority changed")
        _CAPTURE_AUTHORITY = authority
        _CAPTURE_AUTHORITY_PRESEEDED = True
        _publish_capture_authority(authority)


def _get_capture_authority(deadline: float | None = None) -> _CaptureAuthority:
    global _CAPTURE_AUTHORITY
    absolute_deadline = (
        time.monotonic() + _DEFAULT_DEADLINE_SECONDS
        if deadline is None
        else float(deadline)
    )
    _remaining(absolute_deadline)
    authority = _CAPTURE_AUTHORITY
    if authority is not None:
        return authority
    if not _AUTHORITY_LOCK.acquire(timeout=_remaining(absolute_deadline)):
        raise ProofStaleError(
            "proof_stale: capture authority lock exceeded the source deadline"
        )
    try:
        _remaining(absolute_deadline)
        authority = _CAPTURE_AUTHORITY
        if authority is None:
            authority = _build_capture_authority(absolute_deadline)
            _remaining(absolute_deadline)
            _CAPTURE_AUTHORITY = authority
            _publish_capture_authority(authority)
    finally:
        _AUTHORITY_LOCK.release()
    return authority


def _capture_implementation_sha256() -> str:
    return _get_capture_authority().implementation_sha256


def _verify_public_authority(*, deadline: float) -> _CaptureAuthority:
    authority_was_missing = _CAPTURE_AUTHORITY is None
    authority = _get_capture_authority(deadline)
    if not authority_was_missing and not _CAPTURE_AUTHORITY_PRESEEDED:
        _verify_capture_authority(authority, deadline=deadline)
    return authority


def _verify_public_authority_after(
    authority: _CaptureAuthority, *, deadline: float,
) -> None:
    """Close the executable-identity bracket around one public operation."""

    if not _CAPTURE_AUTHORITY_PRESEEDED:
        _verify_capture_authority(authority, deadline=deadline)


"""Compatibility aliases above are populated only after a source operation.

Importing BestPlan state must not require Git or hash executable bytes. Private
tests and legacy diagnostics that inspect the old names after capture continue
to see the exact pinned values, while ordinary module import remains inert.
"""


def _assert_trusted_file_identity(
    path: bytes,
    expected_sha256: str,
    expected_device: int,
    expected_inode: int,
    *,
    require_executable: bool,
    deadline: float | None = None,
) -> None:
    try:
        if deadline is None:
            digest, device, inode = _stable_file_identity(
                path, require_executable=require_executable,
            )
        else:
            digest, device, inode = _stable_file_identity(
                path,
                require_executable=require_executable,
                deadline=deadline,
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


def _inspect_workspace_boundary_in_process(
    workspace: str, deadline: float,
) -> tuple[str, bool]:
    """Canonicalize a legacy hint and conservatively detect a Git boundary."""

    workspace_raw = _canonical_raw(workspace or os.getcwd())
    _remaining(deadline)
    directory = workspace_raw
    while True:
        _remaining(deadline)
        try:
            os.lstat(os.path.join(directory, b".git"))
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError:
            return os.fsdecode(workspace_raw), True
        else:
            _remaining(deadline)
            return os.fsdecode(workspace_raw), True
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent

    try:
        _remaining(deadline)
        head = os.stat(os.path.join(workspace_raw, b"HEAD"))
        _remaining(deadline)
        objects = os.stat(os.path.join(workspace_raw, b"objects"))
        _remaining(deadline)
    except (FileNotFoundError, NotADirectoryError):
        has_bare_boundary = False
    except OSError:
        has_bare_boundary = True
    else:
        has_bare_boundary = stat.S_ISREG(head.st_mode) and stat.S_ISDIR(
            objects.st_mode,
        )
    return os.fsdecode(workspace_raw), has_bare_boundary


def _run_legacy_v1_git_output(
    cwd: bytes,
    *args: str,
    deadline: float,
    max_output_bytes: int,
    digest_only: bool = False,
    owns_process_tree: bool = False,
) -> bytes | str:
    """Run fixed-path Git inside the already isolated legacy helper process."""

    environment = _legacy_v1_helper_environment()
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    output = _run_bounded_helper_process(
        [
            _legacy_v1_git_path(),
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            *args,
        ],
        environment,
        b"",
        deadline,
        cwd=cwd,
        label="legacy V1 Git command",
        response_limit=max_output_bytes,
        owns_process_tree=owns_process_tree,
    )
    if digest_only:
        return _sha256_bytes(output, deadline=deadline)
    return output


def _capture_legacy_v1_in_process(
    workspace: str, deadline: float,
) -> dict[str, str]:
    """Capture a bounded candidate-only fingerprint without scanning ignored trees."""

    workspace_raw = _canonical_raw(workspace or os.getcwd())
    if not os.path.isdir(workspace_raw):
        raise SourceBoundaryError("legacy V1 workspace is not a directory")
    root_output = _run_legacy_v1_git_output(
        workspace_raw,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        deadline=deadline,
        max_output_bytes=_MAX_PATH_BYTES + 2,
    )
    assert isinstance(root_output, bytes)
    root_value = _without_delimiter(root_output)
    if not root_value or b"\0" in root_value or b"\n" in root_value:
        raise SourceBoundaryError("legacy V1 Git root is malformed")
    root_raw = os.path.realpath(root_value)
    head_output = _run_legacy_v1_git_output(
        root_raw,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        deadline=deadline,
        max_output_bytes=128,
    )
    assert isinstance(head_output, bytes)
    head = _without_delimiter(head_output)
    if (
        len(head) not in {40, 64}
        or any(character not in b"0123456789abcdef" for character in head)
    ):
        raise SourceBoundaryError("legacy V1 Git HEAD is malformed")
    object_format_output = _run_legacy_v1_git_output(
        root_raw,
        "rev-parse",
        "--show-object-format",
        deadline=deadline,
        max_output_bytes=16,
    )
    assert isinstance(object_format_output, bytes)
    object_format_raw = _without_delimiter(object_format_output)
    if object_format_raw not in {b"sha1", b"sha256"}:
        raise UnsupportedRepositoryError(
            "legacy V1 Git object format is unsupported"
        )
    object_format = object_format_raw.decode("ascii")
    if len(head) != {"sha1": 40, "sha256": 64}[object_format]:
        raise SourceBoundaryError("legacy V1 Git HEAD format is inconsistent")
    index_output = _run_legacy_v1_git_output(
        root_raw,
        "ls-files",
        "--stage",
        "-z",
        deadline=deadline,
        max_output_bytes=_MAX_GIT_METADATA_BYTES,
    )
    flags_output = _run_legacy_v1_git_output(
        root_raw,
        "ls-files",
        "-v",
        "-z",
        deadline=deadline,
        max_output_bytes=_MAX_GIT_METADATA_BYTES,
    )
    tracked_output = _run_legacy_v1_git_output(
        root_raw,
        "ls-files",
        "--cached",
        "-z",
        deadline=deadline,
        max_output_bytes=_MAX_GIT_METADATA_BYTES,
    )
    untracked_output = _run_legacy_v1_git_output(
        root_raw,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        deadline=deadline,
        max_output_bytes=_MAX_GIT_METADATA_BYTES,
    )
    assert isinstance(index_output, bytes)
    assert isinstance(flags_output, bytes)
    assert isinstance(tracked_output, bytes)
    assert isinstance(untracked_output, bytes)
    for label, output in (
        ("index", index_output),
        ("flags", flags_output),
        ("tracked", tracked_output),
        ("untracked", untracked_output),
    ):
        if output and not output.endswith(b"\0"):
            raise SourceBoundaryError(
                f"legacy V1 {label} path output is malformed"
            )
    index_entries = _parse_index_entries(index_output, deadline=deadline)
    index_flags = _parse_tags(flags_output, deadline=deadline)
    tracked_paths = set(_split_nul(tracked_output, deadline=deadline))
    untracked_paths = set(_split_nul(untracked_output, deadline=deadline))
    if tracked_paths & untracked_paths:
        raise SourceBoundaryError("legacy V1 Git path sets overlap")
    paths = sorted(tracked_paths | untracked_paths)
    _assert_path_scale(
        paths,
        label="legacy V1",
        max_count=_MAX_PROTECTED_PATHS,
        deadline=deadline,
    )
    legacy_repo = RepoIdentity(
        workspace=os.fsdecode(root_raw),
        workspace_raw=root_raw,
        worktree=os.fsdecode(root_raw),
        worktree_raw=root_raw,
        git_dir="",
        git_dir_raw=b"",
        common_dir="",
        common_dir_raw=b"",
        common_dir_device=0,
        common_dir_inode=0,
        object_format=object_format,
        repository_id="legacy-v1",
    )
    digest = hashlib.sha256()

    def add(value: bytes) -> None:
        _remaining(deadline)
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    for value in (b"bestplan-legacy-v1-raw", root_raw, head):
        add(value)
    for entry in index_entries:
        add(b"index")
        add(entry.path)
        add(oct(entry.mode).encode("ascii"))
        add(entry.oid.encode("ascii"))
        add(str(entry.stage).encode("ascii"))
    for path in sorted(index_flags):
        add(b"flags")
        add(path)
        add(index_flags[path])
    total_size = 0
    for path in paths:
        _remaining(deadline)
        captured = _capture_path(
            legacy_repo,
            path,
            tracked=path in tracked_paths,
            deadline=deadline,
        )
        total_size += captured.size or 0
        if total_size > _MAX_EXPORT_BYTES:
            raise UnsupportedRepositoryError(
                "legacy V1 content exceeds the trusted limit"
            )
        for value in (
            b"worktree",
            captured.path,
            b"1" if captured.tracked else b"0",
            captured.kind.encode("ascii"),
            str(captured.mode or 0).encode("ascii"),
            str(captured.size or 0).encode("ascii"),
            (captured.content_sha256 or "").encode("ascii"),
            captured.symlink_target or b"",
            (captured.git_oid or "").encode("ascii"),
        ):
            add(value)
    return {
        "workspace": os.fsdecode(root_raw),
        "fingerprint": "legacy-v1:" + digest.hexdigest(),
    }


def resolve_repo_identity(workspace: str) -> RepoIdentity:
    """Resolve one worktree and its shared repository identity losslessly."""

    deadline = time.monotonic() + _DEFAULT_DEADLINE_SECONDS
    authority = _verify_public_authority(deadline=deadline)
    try:
        resolved = _run_source_helper(
            authority, "resolve", str(workspace), deadline,
        )
    finally:
        _verify_public_authority_after(authority, deadline=deadline)
    if not isinstance(resolved, RepoIdentity):
        raise SourceBoundaryError(
            "trusted capture helper returned an invalid repository identity"
        )
    return resolved


def inspect_workspace_boundary(
    workspace: str, deadline: float,
) -> tuple[str, bool]:
    """Resolve a legacy workspace and inspect Git markers in the bounded helper."""

    absolute_deadline = float(deadline)
    authority = _verify_public_authority(deadline=absolute_deadline)
    try:
        result = _run_source_helper(
            authority, "inspect_boundary", str(workspace), absolute_deadline,
        )
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], str)
            or not isinstance(result[1], bool)
        ):
            raise SourceBoundaryError(
                "trusted capture helper returned an invalid workspace inspection"
            )
        return result
    finally:
        _verify_public_authority_after(authority, deadline=absolute_deadline)


def capture_legacy_v1_fingerprint(
    workspace: str, deadline: float,
) -> tuple[str, str]:
    """Return an explicitly untrusted, candidate-only legacy V1 proof."""

    result = _run_legacy_v1_helper("fingerprint", workspace, float(deadline))
    if (
        not isinstance(result, dict)
        or set(result) != {"workspace", "fingerprint"}
        or not isinstance(result["workspace"], str)
        or not isinstance(result["fingerprint"], str)
        or not result["fingerprint"].startswith("legacy-v1:")
    ):
        raise SourceBoundaryError(
            "legacy V1 helper returned an invalid fingerprint"
        )
    return result["workspace"], result["fingerprint"]


def inspect_legacy_v1_workspace(
    workspace: str, deadline: float,
) -> tuple[str, bool]:
    """Inspect a legacy workspace without claiming a trusted source proof."""

    result = _run_legacy_v1_helper("inspect", workspace, float(deadline))
    if (
        not isinstance(result, list)
        or len(result) != 2
        or not isinstance(result[0], str)
        or not isinstance(result[1], bool)
    ):
        raise SourceBoundaryError(
            "legacy V1 helper returned an invalid workspace inspection"
        )
    return result[0], result[1]


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


def _assert_path_scale(
    paths: Iterable[bytes],
    *,
    label: str,
    max_count: int,
    deadline: float,
) -> None:
    count = 0
    total_bytes = 0
    for path in paths:
        _remaining(deadline)
        count += 1
        total_bytes += len(path)
        if len(path) > _MAX_PATH_BYTES:
            raise UnsupportedRepositoryError(
                f"{label} path exceeds the trusted path limit"
            )
        if count > max_count or total_bytes > _MAX_TOTAL_PATH_BYTES:
            raise UnsupportedRepositoryError(
                f"{label} path metadata exceeds the trusted limit"
            )


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
        if len(entries) > _MAX_INDEX_ENTRIES:
            raise UnsupportedRepositoryError(
                "Git index metadata exceeds the trusted entry limit"
            )
    _assert_path_scale(
        (entry.path for entry in entries),
        label="Git index",
        max_count=_MAX_INDEX_ENTRIES,
        deadline=deadline or time.monotonic() + _DEFAULT_DEADLINE_SECONDS,
    )
    return tuple(sorted(entries, key=lambda item: (item.path, item.stage)))


def _decode_index_v4_varint(
    value: bytes, offset: int, end: int, *, deadline: float,
) -> tuple[int, int]:
    result = 0
    for _index in range(10):
        _remaining(deadline)
        if offset >= end:
            raise SourceBoundaryError("Git index v4 path prefix is truncated")
        byte = value[offset]
        offset += 1
        result = (result << 7) + (byte & 0x7F)
        if not byte & 0x80:
            return result, offset
        result += 1
    raise SourceBoundaryError("Git index v4 path prefix is malformed")


def _index_extension_records(
    value: bytes,
    *,
    object_format: str,
    deadline: float,
) -> tuple[tuple[_RawIndexEntry, ...], tuple[tuple[bytes, bytes], ...]]:
    """Return raw semantic index entries and lossless extension payloads.

    This parser exists only to interpret persisted index flags that Git cannot
    safely report while repository-configured fsmonitor execution is disabled.
    It deliberately supports only Git's documented index versions and validates
    every boundary before advancing.
    """

    _remaining(deadline)
    hash_size = {"sha1": 20, "sha256": 32}.get(object_format)
    if hash_size is None:
        raise UnsupportedRepositoryError("unsupported Git index object format")
    if len(value) < 12 + hash_size or value[:4] != b"DIRC":
        raise SourceBoundaryError("Git index header is malformed")
    version = int.from_bytes(value[4:8], "big")
    if version not in {2, 3, 4}:
        raise UnsupportedRepositoryError(
            f"Git index version {version} is unsupported"
        )
    entry_count = int.from_bytes(value[8:12], "big")
    if entry_count > _MAX_INDEX_ENTRIES:
        raise UnsupportedRepositoryError(
            "Git index metadata exceeds the trusted entry limit"
        )
    checksum_offset = len(value) - hash_size
    try:
        digest = hashlib.new(object_format, usedforsecurity=False)
    except TypeError:  # pragma: no cover - compatibility with older Python
        digest = hashlib.new(object_format)
    digest.update(value[:checksum_offset])
    expected_checksum = value[checksum_offset:]
    if any(expected_checksum) and digest.digest() != expected_checksum:
        raise SourceBoundaryError("Git index checksum is invalid")

    offset = 12
    previous_path = b""
    entries: list[_RawIndexEntry] = []
    for _entry_index in range(entry_count):
        _remaining(deadline)
        entry_start = offset
        mode_offset = entry_start + 24
        flags_offset = entry_start + 40 + hash_size
        if mode_offset + 4 > checksum_offset or flags_offset + 2 > checksum_offset:
            raise SourceBoundaryError("Git index entry metadata is truncated")
        raw_mode = int.from_bytes(value[mode_offset : mode_offset + 4], "big")
        raw_oid = value[entry_start + 40 : entry_start + 40 + hash_size].hex()
        if raw_mode & 0o170000 == 0o040000:
            raise UnsupportedRepositoryError(
                "persisted sparse Git index directory is unsupported"
            )
        flags = int.from_bytes(value[flags_offset : flags_offset + 2], "big")
        stage = (flags >> 12) & 0x3
        name_length = flags & 0x0FFF
        name_offset = flags_offset + 2
        extended = 0
        if flags & 0x4000:
            if version < 3 or name_offset + 2 > checksum_offset:
                raise SourceBoundaryError("Git index extended flags are malformed")
            extended = int.from_bytes(value[name_offset : name_offset + 2], "big")
            if extended & ~0x6000:
                raise UnsupportedRepositoryError(
                    "Git index entry uses unsupported extended flags"
                )
            name_offset += 2

        if version == 4:
            strip_length, suffix_offset = _decode_index_v4_varint(
                value, name_offset, checksum_offset, deadline=deadline,
            )
            if strip_length > len(previous_path):
                raise SourceBoundaryError("Git index v4 path prefix is malformed")
            nul = value.find(b"\0", suffix_offset, checksum_offset)
            if nul < 0:
                raise SourceBoundaryError("Git index v4 path is unterminated")
            path = previous_path[: len(previous_path) - strip_length] + value[
                suffix_offset:nul
            ]
            if name_length != 0x0FFF and name_length != len(path):
                raise SourceBoundaryError("Git index v4 path length is inconsistent")
            offset = nul + 1
        else:
            if name_length == 0x0FFF:
                nul = value.find(b"\0", name_offset, checksum_offset)
                if nul < 0:
                    raise SourceBoundaryError("Git index path is unterminated")
                path = value[name_offset:nul]
            else:
                nul = name_offset + name_length
                if nul >= checksum_offset or value[nul] != 0:
                    raise SourceBoundaryError("Git index path length is inconsistent")
                path = value[name_offset:nul]
            entry_size = nul + 1 - entry_start
            padding = (-entry_size) % 8
            offset = nul + 1 + padding
            if offset > checksum_offset or any(value[nul + 1 : offset]):
                raise SourceBoundaryError("Git index entry padding is malformed")
        if not _valid_relative_path(path):
            raise UnsupportedRepositoryError("Git index contains an unsafe path")
        if len(path) > _MAX_PATH_BYTES:
            raise UnsupportedRepositoryError(
                "Git index path exceeds the trusted path limit"
            )
        entries.append(_RawIndexEntry(
            path=path,
            mode=raw_mode,
            oid=raw_oid,
            stage=stage,
            assume_unchanged=bool(flags & 0x8000),
            skip_worktree=bool(extended & 0x4000),
            intent_to_add=bool(extended & 0x2000),
        ))
        previous_path = path

    extensions: list[tuple[bytes, bytes]] = []
    while offset < checksum_offset:
        _remaining(deadline)
        if checksum_offset - offset < 8:
            raise SourceBoundaryError("Git index extension header is truncated")
        signature = value[offset : offset + 4]
        extension_size = int.from_bytes(value[offset + 4 : offset + 8], "big")
        offset += 8
        extension_end = offset + extension_size
        if extension_end > checksum_offset:
            raise SourceBoundaryError("Git index extension size is malformed")
        extensions.append((signature, value[offset:extension_end]))
        offset = extension_end
    return tuple(entries), tuple(extensions)


def _decode_ewah_set_bits(
    value: bytes, *, entry_count: int, deadline: float,
) -> set[int]:
    if len(value) < 12:
        raise SourceBoundaryError("Git fsmonitor EWAH bitmap is truncated")
    bit_size = int.from_bytes(value[:4], "big")
    word_count = int.from_bytes(value[4:8], "big")
    if bit_size > entry_count:
        raise SourceBoundaryError(
            "Git fsmonitor bitmap has more entries than the index"
        )
    max_words = max(1, 2 * ((entry_count + 63) // 64) + 1)
    if word_count == 0 or word_count > max_words:
        raise UnsupportedRepositoryError(
            "Git fsmonitor bitmap exceeds the trusted word limit"
        )
    expected_size = 12 + word_count * 8
    if len(value) != expected_size:
        raise SourceBoundaryError("Git fsmonitor EWAH bitmap size is malformed")
    words = tuple(
        int.from_bytes(value[8 + index * 8 : 16 + index * 8], "big")
        for index in range(word_count)
    )
    rlw_position = int.from_bytes(value[-4:], "big")
    if rlw_position >= word_count:
        raise SourceBoundaryError("Git fsmonitor EWAH run position is malformed")

    rounded_bit_size = ((bit_size + 63) // 64) * 64
    dirty: set[int] = set()
    rlw_offsets: set[int] = set()
    pointer = 0
    position = 0
    while pointer < word_count:
        _remaining(deadline)
        rlw_offsets.add(pointer)
        run_word = words[pointer]
        pointer += 1
        run_bit = bool(run_word & 1)
        run_words = (run_word >> 1) & 0xFFFFFFFF
        literal_words = run_word >> 33
        if pointer + literal_words > word_count:
            raise SourceBoundaryError("Git fsmonitor EWAH literals are malformed")
        run_end = position + run_words * 64
        if run_end > rounded_bit_size:
            raise SourceBoundaryError("Git fsmonitor EWAH run exceeds its bit size")
        if run_bit:
            if run_end > bit_size:
                raise SourceBoundaryError(
                    "Git fsmonitor EWAH run sets bits outside its bit size"
                )
            for bit in range(position, run_end):
                _remaining(deadline)
                dirty.add(bit)
        position = run_end
        for _literal_index in range(literal_words):
            _remaining(deadline)
            literal = words[pointer]
            pointer += 1
            literal_limit = min(64, max(0, bit_size - position))
            if literal_limit < 64 and literal >> literal_limit:
                raise SourceBoundaryError(
                    "Git fsmonitor EWAH literal sets bits outside its bit size"
                )
            while literal:
                _remaining(deadline)
                lowest = literal & -literal
                bit = lowest.bit_length() - 1
                dirty.add(position + bit)
                literal ^= lowest
            position += 64
            if position > rounded_bit_size:
                raise SourceBoundaryError(
                    "Git fsmonitor EWAH literals exceed its bit size"
                )
    if rlw_position not in rlw_offsets or position < bit_size:
        raise SourceBoundaryError("Git fsmonitor EWAH structure is malformed")
    return dirty


def _parse_index_fsmonitor_valid_paths(
    value: bytes,
    *,
    index_paths: tuple[bytes, ...],
    object_format: str,
    deadline: float,
) -> set[bytes]:
    raw_index_entries, extensions = _index_extension_records(
        value, object_format=object_format, deadline=deadline,
    )
    if any(signature == b"link" for signature, _payload in extensions):
        raise UnsupportedRepositoryError(
            "split Git indexes are unsupported for source proof capture"
        )
    if any(signature == b"sdir" for signature, _payload in extensions):
        raise UnsupportedRepositoryError(
            "persisted sparse Git indexes are unsupported for source proof capture"
        )
    raw_index_paths = tuple(entry.path for entry in raw_index_entries)
    if raw_index_paths != index_paths:
        raise _CaptureChanged("Git index entries changed during capture")
    entry_count = len(raw_index_paths)
    fsmonitor_payloads = [
        payload for signature, payload in extensions if signature == b"FSMN"
    ]
    if not fsmonitor_payloads:
        return set()
    if len(fsmonitor_payloads) != 1:
        raise SourceBoundaryError("Git index has duplicate fsmonitor extensions")
    payload = fsmonitor_payloads[0]
    if len(payload) < 9:
        raise SourceBoundaryError("Git fsmonitor extension is too short")
    version = int.from_bytes(payload[:4], "big")
    if version == 1:
        cursor = 12
    elif version == 2:
        token_end = payload.find(b"\0", 4)
        if token_end < 0:
            raise SourceBoundaryError("Git fsmonitor token is unterminated")
        cursor = token_end + 1
    else:
        raise SourceBoundaryError(
            f"Git fsmonitor extension version {version} is unsupported"
        )
    if cursor + 4 > len(payload):
        raise SourceBoundaryError("Git fsmonitor bitmap size is truncated")
    bitmap_size = int.from_bytes(payload[cursor : cursor + 4], "big")
    cursor += 4
    if bitmap_size != len(payload) - cursor:
        raise SourceBoundaryError("Git fsmonitor bitmap size is malformed")
    dirty_positions = _decode_ewah_set_bits(
        payload[cursor:], entry_count=entry_count, deadline=deadline,
    )
    return {
        path for position, path in enumerate(index_paths)
        if position not in dirty_positions
    }


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
        if len(tags) > _MAX_INDEX_ENTRIES:
            raise UnsupportedRepositoryError(
                "Git index flag metadata exceeds the trusted entry limit"
            )
    _assert_path_scale(
        tags,
        label="Git index flags",
        max_count=_MAX_INDEX_ENTRIES,
        deadline=deadline or time.monotonic() + _DEFAULT_DEADLINE_SECONDS,
    )
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
            return ProtectedPath(path, True, "missing", None, None, None, None, None)
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
            if opened.st_size > _MAX_BLOB_BYTES:
                raise UnsupportedRepositoryError(
                    "protected file exceeds the trusted blob limit"
                )
            digest = hashlib.sha256()
            git_digest = hashlib.new(repo.object_format)
            git_digest.update(f"blob {opened.st_size}\0".encode("ascii"))
            size = 0
            while True:
                _remaining(deadline)
                chunk = os.read(fd, _BUFFER_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                git_digest.update(chunk)
                size += len(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if _path_state(opened) != _path_state(after) or size != after.st_size:
            raise _CaptureChanged(f"protected file changed: {os.fsdecode(path)}")
        return ProtectedPath(
            path,
            tracked,
            "regular",
            mode,
            size,
            digest.hexdigest(),
            None,
            git_digest.hexdigest(),
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
        if len(target_raw) > _MAX_SYMLINK_TARGET_BYTES:
            raise UnsupportedRepositoryError(
                "protected symlink target exceeds the trusted blob limit"
            )
        git_digest = hashlib.new(repo.object_format)
        git_digest.update(f"blob {len(target_raw)}\0".encode("ascii"))
        git_digest.update(target_raw)
        return ProtectedPath(
            path,
            tracked,
            "symlink",
            mode,
            len(target_raw),
            _sha256_bytes(target_raw, deadline=deadline),
            target_raw,
            git_digest.hexdigest(),
        )
    if stat.S_ISDIR(mode):
        return ProtectedPath(path, tracked, "directory", mode, None, None, None, None)
    raise UnsupportedRepositoryError(
        f"nonignored special file is unsupported: {os.fsdecode(path)}"
    )


def _ignored_paths(
    repo: RepoIdentity, paths: list[bytes], *, deadline: float,
) -> set[bytes]:
    if not paths:
        return set()
    _assert_path_scale(
        paths,
        label="ignore query",
        max_count=_MAX_PROTECTED_PATHS,
        deadline=deadline,
    )
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
    ignored = set(_split_nul(output, deadline=deadline))
    _assert_path_scale(
        ignored,
        label="ignored repository",
        max_count=_MAX_PROTECTED_PATHS,
        deadline=deadline,
    )
    return ignored


def _scan_nonignored_specials(repo: RepoIdentity, *, deadline: float) -> None:
    frontier: list[tuple[bytes, bytes]] = [(repo.worktree_raw, b"")]
    metadata_dirs = {repo.git_dir_raw, repo.common_dir_raw}
    seen_entries = 0
    seen_path_bytes = 0
    while frontier:
        _remaining(deadline)
        candidates: list[tuple[os.DirEntry, bytes, int]] = []
        for directory, prefix in frontier:
            try:
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        _remaining(deadline)
                        name = os.fsencode(entry.name)
                        if not prefix and name == b".git":
                            continue
                        relative = name if not prefix else prefix + b"/" + name
                        seen_entries += 1
                        seen_path_bytes += len(relative)
                        if len(relative) > _MAX_PATH_BYTES or (
                            seen_entries > _MAX_PROTECTED_PATHS
                            or seen_path_bytes > _MAX_TOTAL_PATH_BYTES
                        ):
                            raise UnsupportedRepositoryError(
                                "repository path metadata exceeds the trusted limit"
                            )
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
            except OSError as exc:
                raise SourceBoundaryError(
                    f"repository path cannot be enumerated: {os.fsdecode(directory)}: {exc}"
                ) from exc
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
    code, _configured = _run_git(
        repo.worktree_raw,
        "config",
        "--get",
        "extensions.partialClone",
        deadline=deadline,
        ok_codes=(0, 1),
    )
    if code == 0:
        raise UnsupportedRepositoryError(
            "Git partial/promisor clone configuration is unsupported"
        )
    code, _configured = _run_git(
        repo.worktree_raw,
        "config",
        "--get-regexp",
        r"^remote\..*\.(promisor|partialclonefilter)$",
        deadline=deadline,
        ok_codes=(0, 1),
    )
    if code == 0:
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
        if len(entries) > _MAX_TREE_ENTRIES:
            raise UnsupportedRepositoryError(
                "Git tree metadata exceeds the trusted entry limit"
            )
    _assert_path_scale(
        (entry.path for entry in entries),
        label="Git tree",
        max_count=_MAX_TREE_ENTRIES,
        deadline=deadline,
    )
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
) -> tuple[str, str, tuple[_TreeEntry, ...]]:
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
    _, index_path_raw = _run_git(
        repo.worktree_raw,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "index",
        deadline=absolute_deadline,
    )
    raw_index = _read_stable_small_file(
        _without_delimiter(index_path_raw),
        deadline=absolute_deadline,
        limit=_MAX_GIT_METADATA_BYTES,
    )
    _raw_index_entries, index_extensions = _index_extension_records(
        raw_index,
        object_format=repo.object_format,
        deadline=absolute_deadline,
    )
    if any(signature == b"sdir" for signature, _payload in index_extensions):
        raise UnsupportedRepositoryError("persisted sparse Git index is unsupported")
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
    parsed_index_entries = _parse_index_entries(
        index_raw, deadline=absolute_deadline,
    )
    if any(entry.mode == 0o160000 for entry in parsed_index_entries):
        raise UnsupportedRepositoryError("Git submodules are unsupported")
    if any(entry.mode == 0o040000 for entry in parsed_index_entries):
        raise UnsupportedRepositoryError("persisted sparse Git index is unsupported")
    if scan_specials:
        _scan_nonignored_specials(repo, deadline=absolute_deadline)
    return head_oid, tree_oid, tree_entries


def assert_supported_repository(
    repo: RepoIdentity, *, deadline: float | None = None,
) -> None:
    """Reject repository shapes that cannot preserve exact committed bytes."""

    absolute_deadline = (
        time.monotonic() + _DEFAULT_DEADLINE_SECONDS
        if deadline is None
        else float(deadline)
    )
    authority = _verify_public_authority(deadline=absolute_deadline)
    try:
        _assert_supported_repository(
            repo, deadline=absolute_deadline, scan_specials=True,
        )
    finally:
        _verify_public_authority_after(authority, deadline=absolute_deadline)


def _manifest_digest(
    index_entries: tuple[IndexEntry, ...],
    index_flags: tuple[IndexFlags, ...],
    worktree_entries: tuple[ProtectedPath, ...],
    staged_diff_sha256: str,
    unstaged_diff_sha256: str,
    *,
    deadline: float,
) -> str:
    digest = hashlib.sha256()
    label = b"bestplan-protected-manifest-v2"
    digest.update(len(label).to_bytes(8, "big"))
    digest.update(label)

    def add(field: bytes) -> None:
        _remaining(deadline)
        digest.update(len(field).to_bytes(8, "big"))
        digest.update(field)

    add(b"staged-diff")
    add(staged_diff_sha256.encode("ascii"))
    add(b"unstaged-diff")
    add(unstaged_diff_sha256.encode("ascii"))
    for entry in index_entries:
        add(b"index")
        add(entry.path)
        add(oct(entry.mode).encode("ascii"))
        add(entry.oid.encode("ascii"))
        add(str(entry.stage).encode("ascii"))
    for entry in index_flags:
        add(b"flags")
        add(entry.path)
        add(entry.tag)
        add(entry.fsmonitor_tag)
        add(b"1" if entry.assume_unchanged else b"0")
        add(b"1" if entry.skip_worktree else b"0")
        add(b"1" if entry.fsmonitor_valid else b"0")
        add(b"1" if entry.intent_to_add else b"0")
    for entry in worktree_entries:
        add(b"worktree")
        add(entry.path)
        add(b"1" if entry.tracked else b"0")
        add(entry.kind.encode("ascii"))
        add(b"" if entry.mode is None else oct(entry.mode).encode("ascii"))
        add(b"" if entry.size is None else str(entry.size).encode("ascii"))
        add(
            b""
            if entry.content_sha256 is None
            else entry.content_sha256.encode("ascii")
        )
        add(b"" if entry.symlink_target is None else entry.symlink_target)
        add(b"" if entry.git_oid is None else entry.git_oid.encode("ascii"))
    _remaining(deadline)
    return digest.hexdigest()


def _tracked_entry_differs_from_index(
    worktree: ProtectedPath, index: IndexEntry | None,
) -> bool:
    if index is None or index.stage != 0:
        return True
    if index.mode == 0o120000:
        return worktree.kind != "symlink" or worktree.git_oid != index.oid
    if index.mode not in {0o100644, 0o100755}:
        return True
    if worktree.kind != "regular" or worktree.git_oid != index.oid:
        return True
    if worktree.mode is None:
        return True
    executable = bool(worktree.mode & 0o111)
    return executable != (index.mode == 0o100755)


def _index_mode_kind(mode: int) -> bytes:
    kind = mode & 0o170000
    if kind == 0o100000:
        return b"regular"
    if kind == 0o120000:
        return b"symlink"
    if kind == 0o160000:
        return b"gitlink"
    if kind == 0o040000:
        return b"directory"
    return b"unsupported"


def _group_index_entries(
    entries: tuple[IndexEntry, ...], *, deadline: float,
) -> dict[bytes, tuple[IndexEntry, ...]]:
    grouped: dict[bytes, list[IndexEntry]] = {}
    for entry in entries:
        _remaining(deadline)
        if entry.stage not in {0, 1, 2, 3}:
            raise SourceBoundaryError("Git index contains an invalid stage")
        grouped.setdefault(entry.path, []).append(entry)
    result: dict[bytes, tuple[IndexEntry, ...]] = {}
    for path, values in grouped.items():
        _remaining(deadline)
        ordered = tuple(sorted(values, key=lambda item: item.stage))
        stages = {entry.stage for entry in ordered}
        if len(stages) != len(ordered) or (0 in stages and len(stages) != 1):
            raise SourceBoundaryError(
                "Git index contains inconsistent conflict stages"
            )
        result[path] = ordered
    return result


def _index_flags_by_path(
    entries: tuple[IndexFlags, ...], *, deadline: float,
) -> dict[bytes, IndexFlags]:
    result: dict[bytes, IndexFlags] = {}
    for entry in entries:
        _remaining(deadline)
        if entry.path in result:
            raise SourceBoundaryError("Git index contains duplicate path flags")
        result[entry.path] = entry
    return result


def _bounded_delta_digest(
    label: bytes,
    records: Iterable[tuple[bytes, ...]],
    *,
    deadline: float,
) -> str:
    digest = hashlib.sha256()
    total = 0

    def add(field: bytes) -> None:
        nonlocal total
        _remaining(deadline)
        total += 8 + len(field)
        if total > _MAX_DIFF_BYTES:
            raise UnsupportedRepositoryError(
                "canonical source delta exceeds the trusted diff limit"
            )
        digest.update(len(field).to_bytes(8, "big"))
        digest.update(field)

    add(label)
    for record in records:
        add(b"record")
        for field in record:
            add(field)
    _remaining(deadline)
    return digest.hexdigest()


def _index_record_fields(
    entries: tuple[IndexEntry, ...], *, intent_to_add: bool,
) -> tuple[bytes, ...]:
    fields: list[bytes] = [
        b"index",
        b"present" if entries else b"absent",
        str(len(entries)).encode("ascii"),
        b"intent-to-add",
        b"1" if intent_to_add else b"0",
    ]
    for entry in entries:
        fields.extend((
            b"entry",
            str(entry.stage).encode("ascii"),
            oct(entry.mode).encode("ascii"),
            _index_mode_kind(entry.mode),
            entry.oid.encode("ascii"),
        ))
    return tuple(fields)


def _head_record_fields(entry: _TreeEntry | None) -> tuple[bytes, ...]:
    if entry is None:
        return (b"head", b"absent")
    return (
        b"head",
        b"present",
        oct(entry.mode).encode("ascii"),
        entry.object_type,
        entry.oid.encode("ascii"),
    )


def _worktree_record_fields(entry: ProtectedPath) -> tuple[bytes, ...]:
    return (
        b"worktree",
        b"present",
        entry.kind.encode("ascii"),
        b"" if entry.mode is None else oct(entry.mode).encode("ascii"),
        b"" if entry.size is None else str(entry.size).encode("ascii"),
        b"" if entry.content_sha256 is None else entry.content_sha256.encode("ascii"),
        b"" if entry.symlink_target is None else entry.symlink_target,
        b"" if entry.git_oid is None else entry.git_oid.encode("ascii"),
    )


def _empty_blob_oid(object_format: str) -> str:
    try:
        digest = hashlib.new(object_format, usedforsecurity=False)
    except TypeError:  # pragma: no cover - compatibility with older Python
        digest = hashlib.new(object_format)
    digest.update(b"blob 0\0")
    return digest.hexdigest()


def _staged_delta(
    head_entries: tuple[_TreeEntry, ...],
    index_entries: tuple[IndexEntry, ...],
    index_flags: tuple[IndexFlags, ...],
    *,
    object_format: str,
    deadline: float,
) -> tuple[set[bytes], str]:
    head_by_path: dict[bytes, _TreeEntry] = {}
    for entry in head_entries:
        _remaining(deadline)
        if entry.path in head_by_path:
            raise SourceBoundaryError("Git HEAD tree contains duplicate paths")
        head_by_path[entry.path] = entry
    index_by_path = _group_index_entries(index_entries, deadline=deadline)
    flags_by_path = _index_flags_by_path(index_flags, deadline=deadline)
    empty_oid = _empty_blob_oid(object_format)
    changed: set[bytes] = set()
    for path in sorted(set(head_by_path) | set(index_by_path)):
        _remaining(deadline)
        head = head_by_path.get(path)
        indexed = index_by_path.get(path, ())
        flag = flags_by_path.get(path)
        intent_to_add = bool(flag and flag.intent_to_add)
        if intent_to_add:
            if (
                head is not None
                or len(indexed) != 1
                or indexed[0].stage != 0
                or indexed[0].mode not in {0o100644, 0o100755}
                or indexed[0].oid != empty_oid
            ):
                raise SourceBoundaryError(
                    "Git intent-to-add index state is malformed"
                )
            effective_index: tuple[IndexEntry, ...] = ()
        else:
            effective_index = indexed
        clean = (
            head is not None
            and head.object_type == b"blob"
            and len(effective_index) == 1
            and effective_index[0].stage == 0
            and effective_index[0].mode == head.mode
            and effective_index[0].oid == head.oid
        ) or (head is None and not effective_index)
        if not clean:
            changed.add(path)
    for path, flag in flags_by_path.items():
        _remaining(deadline)
        if flag.intent_to_add and path not in index_by_path:
            raise SourceBoundaryError("Git intent-to-add flag has no index entry")
    _assert_path_scale(
        changed,
        label="canonical staged delta",
        max_count=_MAX_PROTECTED_PATHS,
        deadline=deadline,
    )

    def records() -> Iterable[tuple[bytes, ...]]:
        for path in sorted(changed):
            _remaining(deadline)
            head = head_by_path.get(path)
            indexed = index_by_path.get(path, ())
            flag = flags_by_path.get(path)
            yield (
                b"path",
                path,
                *_head_record_fields(head),
                *_index_record_fields(
                    indexed,
                    intent_to_add=bool(flag and flag.intent_to_add),
                ),
            )

    return changed, _bounded_delta_digest(
        b"bestplan-staged-delta-v2", records(), deadline=deadline,
    )


def _unstaged_delta(
    index_entries: tuple[IndexEntry, ...],
    index_flags: tuple[IndexFlags, ...],
    worktree_entries: tuple[ProtectedPath, ...],
    *,
    deadline: float,
) -> tuple[set[bytes], str]:
    index_by_path = _group_index_entries(index_entries, deadline=deadline)
    flags_by_path = _index_flags_by_path(index_flags, deadline=deadline)
    worktree_by_path: dict[bytes, ProtectedPath] = {}
    for entry in worktree_entries:
        _remaining(deadline)
        if not entry.tracked:
            continue
        if entry.path in worktree_by_path:
            raise SourceBoundaryError("protected manifest contains duplicate paths")
        worktree_by_path[entry.path] = entry
    if set(worktree_by_path) != set(index_by_path):
        raise _CaptureChanged("Git index paths changed during worktree capture")
    changed: set[bytes] = set()
    for path in sorted(index_by_path):
        _remaining(deadline)
        indexed = index_by_path[path]
        flag = flags_by_path.get(path)
        stage_zero = indexed[0] if len(indexed) == 1 and indexed[0].stage == 0 else None
        if (
            bool(flag and flag.intent_to_add)
            or _tracked_entry_differs_from_index(worktree_by_path[path], stage_zero)
        ):
            changed.add(path)
    _assert_path_scale(
        changed,
        label="canonical unstaged delta",
        max_count=_MAX_PROTECTED_PATHS,
        deadline=deadline,
    )

    def records() -> Iterable[tuple[bytes, ...]]:
        for path in sorted(changed):
            _remaining(deadline)
            flag = flags_by_path.get(path)
            yield (
                b"path",
                path,
                *_index_record_fields(
                    index_by_path[path],
                    intent_to_add=bool(flag and flag.intent_to_add),
                ),
                *_worktree_record_fields(worktree_by_path[path]),
            )

    return changed, _bounded_delta_digest(
        b"bestplan-unstaged-delta-v2", records(), deadline=deadline,
    )


def _capture_protected_manifest(
    repo: RepoIdentity,
    *,
    deadline: float | None = None,
    head_tree_entries: tuple[_TreeEntry, ...] | None = None,
) -> ProtectedManifest:
    """Capture ambient state after the public trust boundary is established."""

    absolute_deadline = (
        time.monotonic() + _DEFAULT_DEADLINE_SECONDS
        if deadline is None
        else float(deadline)
    )
    _, index_path_raw = _run_git(
        repo.worktree_raw,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "index",
        deadline=absolute_deadline,
    )
    index_state = _read_stable_small_file(
        _without_delimiter(index_path_raw),
        deadline=absolute_deadline,
        limit=_MAX_GIT_METADATA_BYTES,
    )
    raw_index_entries, _index_extensions = _index_extension_records(
        index_state,
        object_format=repo.object_format,
        deadline=absolute_deadline,
    )
    _, index_raw = _run_git(
        repo.worktree_raw, "ls-files", "--stage", "-z", deadline=absolute_deadline,
    )
    index_entries = _parse_index_entries(index_raw, deadline=absolute_deadline)
    raw_semantic_entries = tuple(
        IndexEntry(entry.path, entry.mode, entry.oid, entry.stage)
        for entry in raw_index_entries
    )
    if raw_semantic_entries != index_entries:
        raise _CaptureChanged("Git index entries changed during capture")
    if any(
        entry.stage != 0
        and (
            entry.assume_unchanged
            or entry.skip_worktree
            or entry.intent_to_add
        )
        for entry in raw_index_entries
    ):
        raise UnsupportedRepositoryError(
            "nonzero conflict-stage index flags are unsupported"
        )
    _, verbose_raw = _run_git(
        repo.worktree_raw, "ls-files", "-v", "-z", deadline=absolute_deadline,
    )
    verbose_tags = _parse_tags(verbose_raw, deadline=absolute_deadline)
    fsmonitor_valid_paths = _parse_index_fsmonitor_valid_paths(
        index_state,
        index_paths=tuple(entry.path for entry in index_entries),
        object_format=repo.object_format,
        deadline=absolute_deadline,
    )
    raw_flags_by_path: dict[bytes, list[_RawIndexEntry]] = {}
    for entry in raw_index_entries:
        _remaining(absolute_deadline)
        raw_flags_by_path.setdefault(entry.path, []).append(entry)
    if set(verbose_tags) != set(raw_flags_by_path):
        raise _CaptureChanged("Git index flags changed during capture")
    all_flag_paths = sorted(
        raw_flags_by_path
    )
    index_flags = tuple(
        IndexFlags(
            path=path,
            tag=verbose_tags.get(path, b""),
            fsmonitor_tag=b"f" if path in fsmonitor_valid_paths else b"F",
            assume_unchanged=any(
                entry.assume_unchanged
                for entry in raw_flags_by_path.get(path, ())
            ),
            skip_worktree=any(
                entry.skip_worktree for entry in raw_flags_by_path.get(path, ())
            ),
            fsmonitor_valid=path in fsmonitor_valid_paths,
            intent_to_add=any(
                entry.intent_to_add for entry in raw_flags_by_path.get(path, ())
            ),
        )
        for path in all_flag_paths
    )
    _, untracked_raw = _run_git(
        repo.worktree_raw,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        deadline=absolute_deadline,
    )
    tracked_paths = {entry.path for entry in index_entries}
    untracked_paths = set(_split_nul(untracked_raw, deadline=absolute_deadline))
    if tracked_paths & untracked_paths:
        raise _CaptureChanged("Git tracked and untracked paths overlap")
    all_worktree_paths = sorted(tracked_paths | untracked_paths)
    _assert_path_scale(
        all_worktree_paths,
        label="protected manifest",
        max_count=_MAX_PROTECTED_PATHS,
        deadline=absolute_deadline,
    )
    worktree_entries_list: list[ProtectedPath] = []
    for path in all_worktree_paths:
        _remaining(absolute_deadline)
        worktree_entries_list.append(
            _capture_path(
                repo,
                path,
                tracked=path in tracked_paths,
                deadline=absolute_deadline,
            )
        )
    worktree_entries = tuple(worktree_entries_list)
    if head_tree_entries is None:
        _, tree_oid_raw = _run_git(
            repo.worktree_raw,
            "rev-parse",
            "--verify",
            "HEAD^{tree}",
            deadline=absolute_deadline,
        )
        try:
            head_tree_oid = _without_delimiter(tree_oid_raw).decode("ascii")
        except UnicodeError as exc:
            raise SourceBoundaryError(
                "Git returned a non-canonical HEAD tree id"
            ) from exc
        head_tree_entries = _tree_entries(
            repo, head_tree_oid, deadline=absolute_deadline,
        )
    staged_names, staged_diff_sha256 = _staged_delta(
        head_tree_entries,
        index_entries,
        index_flags,
        object_format=repo.object_format,
        deadline=absolute_deadline,
    )
    unstaged_names, unstaged_diff_sha256 = _unstaged_delta(
        index_entries,
        index_flags,
        worktree_entries,
        deadline=absolute_deadline,
    )
    _scan_nonignored_specials(repo, deadline=absolute_deadline)
    _assert_path_scale(
        staged_names | unstaged_names,
        label="dirty Git",
        max_count=_MAX_PROTECTED_PATHS,
        deadline=absolute_deadline,
    )
    special_flag_paths = {
        entry.path
        for entry in index_flags
        if entry.assume_unchanged or entry.skip_worktree or entry.intent_to_add
    }
    protected_paths = tuple(sorted(
        staged_names
        | unstaged_names
        | untracked_paths
        | special_flag_paths
    ))
    _assert_path_scale(
        protected_paths,
        label="protected",
        max_count=_MAX_PROTECTED_PATHS,
        deadline=absolute_deadline,
    )
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


def capture_protected_manifest(
    repo: RepoIdentity, *, deadline: float | None = None,
) -> ProtectedManifest:
    """Capture index plus tracked/untracked nonignored ambient state."""

    absolute_deadline = (
        time.monotonic() + _DEFAULT_DEADLINE_SECONDS
        if deadline is None
        else float(deadline)
    )
    authority = _verify_public_authority(deadline=absolute_deadline)
    try:
        return _capture_protected_manifest(repo, deadline=absolute_deadline)
    finally:
        _verify_public_authority_after(authority, deadline=absolute_deadline)


def _head_read(repo: RepoIdentity, *, deadline: float) -> _SourceRead:
    supported_head_oid, supported_tree_oid, head_tree_entries = (
        _assert_supported_repository(repo, deadline=deadline, scan_specials=False)
    )
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
    if (head_oid, tree_oid) != (supported_head_oid, supported_tree_oid):
        raise _CaptureChanged("Git HEAD tree changed during capture")
    if head_ref is not None and head_raw != b"ref: " + head_ref + b"\n":
        raise _CaptureChanged("symbolic Git HEAD changed during capture")
    manifest = _capture_protected_manifest(
        repo,
        deadline=deadline,
        head_tree_entries=head_tree_entries,
    )
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
    implementation_sha256 = _capture_implementation_sha256()
    return _hash_fields(
        b"bestplan-source-snapshot-v2",
        (
            repo.repository_id.encode("ascii"),
            repo.worktree_raw,
            repo.git_dir_raw,
            repo.common_dir_raw,
            str(repo.common_dir_device).encode("ascii"),
            str(repo.common_dir_inode).encode("ascii"),
            implementation_sha256.encode("ascii"),
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
    implementation_sha256 = _capture_implementation_sha256()
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
                capture_implementation_sha256=implementation_sha256,
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
    expected_module_dev,
    expected_module_ino,
    expected_interpreter_path,
    expected_interpreter_dev,
    expected_interpreter_ino,
    expected_interpreter_sha256,
    expected_git_path,
    expected_git_dev,
    expected_git_ino,
    expected_git_sha256,
) = sys.argv[1:14]

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
    expected_module_dev,
    expected_module_ino,
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
child_authority = module._make_capture_authority(
    module_path=os.fsencode(module_path),
    module_sha256=expected_module_sha256,
    module_device=int(expected_module_dev),
    module_inode=int(expected_module_ino),
    interpreter_path=interpreter_path_raw,
    interpreter_sha256=expected_interpreter_sha256,
    interpreter_device=int(expected_interpreter_dev),
    interpreter_inode=int(expected_interpreter_ino),
    git_path=os.fsencode(expected_git_path),
    git_sha256=expected_git_sha256,
    git_device=int(expected_git_dev),
    git_inode=int(expected_git_ino),
)
module._seed_capture_authority(child_authority)
if child_authority.implementation_sha256 != expected_identity:
    raise RuntimeError("trusted BestPlan capture executable binding mismatch")

try:
    request = pickle.loads(sys.stdin.buffer.read())
    if isinstance(request, tuple) and len(request) == 3:
        request_identity, request_value, remaining_budget = request
        operation = "capture"
    elif isinstance(request, tuple) and len(request) == 4:
        request_identity, operation, request_value, remaining_budget = request
    else:
        raise module.SourceBoundaryError(
            "trusted capture helper request is malformed"
        )
    if (
        request_identity != expected_identity
        or child_authority.implementation_sha256 != expected_identity
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
    if operation == "capture" and isinstance(request_value, module.RepoIdentity):
        result = module._capture_source_snapshot_in_process(
            request_value, child_deadline,
        )
    elif operation == "resolve" and isinstance(request_value, str):
        result = module._resolve_repo_identity(request_value, child_deadline)
    elif operation == "inspect_boundary" and isinstance(request_value, str):
        result = module._inspect_workspace_boundary_in_process(
            request_value, child_deadline,
        )
    else:
        raise module.SourceBoundaryError(
            "trusted capture helper operation is unsupported"
        )
    payload = ("ok", expected_identity, result)
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


def _capture_helper_argv(authority: _CaptureAuthority) -> list[str]:
    return [
        os.fsdecode(authority.interpreter_path),
        "-I",
        "-S",
        "-c",
        _CAPTURE_HELPER_BOOTSTRAP,
        os.fsdecode(authority.module_path),
        authority.implementation_sha256,
        authority.module_sha256,
        str(authority.module_device),
        str(authority.module_inode),
        os.fsdecode(authority.interpreter_path),
        str(authority.interpreter_device),
        str(authority.interpreter_inode),
        authority.interpreter_sha256,
        os.fsdecode(authority.git_path),
        str(authority.git_device),
        str(authority.git_inode),
        authority.git_sha256,
    ]


def _capture_helper_environment(authority: _CaptureAuthority) -> dict[str, str]:
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
    env["PATH"] = authority.helper_path
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
        except PermissionError:
            # Darwin can transiently report EPERM for a killed group while its
            # orphaned zombie is being reaped.  EPERM is not extinction proof:
            # keep polling and fail closed if ESRCH never arrives.
            pass
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




def _run_bounded_helper_process(
    argv: list[str],
    environment: dict[str, str],
    request: bytes,
    deadline: float,
    *,
    cwd: str | bytes | None,
    label: str,
    response_limit: int,
    owns_process_tree: bool = True,
) -> bytes:
    """Run one isolated helper with bounded I/O and proven descendant cleanup."""

    absolute_deadline = float(deadline)
    _remaining(absolute_deadline)
    if len(request) > _MAX_GIT_INPUT_BYTES:
        raise UnsupportedRepositoryError(f"{label} request exceeds the trusted limit")
    try:
        stdin_file = tempfile.TemporaryFile()
        stdout_file = tempfile.TemporaryFile()
        stderr_file = tempfile.TemporaryFile()
    except OSError as exc:
        raise SourceBoundaryError(
            f"{label} output could not be isolated: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        for offset in range(0, len(request), _BUFFER_SIZE):
            _remaining(absolute_deadline)
            stdin_file.write(request[offset:offset + _BUFFER_SIZE])
        stdin_file.seek(0)
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdin=stdin_file,
                stdout=stdout_file,
                stderr=stderr_file,
                close_fds=True,
                start_new_session=(owns_process_tree and os.name == "posix"),
            )
        except OSError as exc:
            raise SourceBoundaryError(
                f"{label} could not start: {type(exc).__name__}: {exc}"
            ) from exc
        if owns_process_tree:
            try:
                process_group = _capture_posix_process_group(process)
            except SourceBoundaryError:
                process.kill()
                process.communicate(timeout=_CAPTURE_CLEANUP_SECONDS)
                raise
        else:
            process_group = None
        containment: object | None = None
        if owns_process_tree:
            try:
                containment = _attach_capture_helper_containment(process)
            except OSError as exc:
                _terminate_capture_helper(process, process_group=process_group)
                raise SourceBoundaryError(
                    f"{label} containment unavailable: {exc}"
                ) from exc
        try:
            while process.poll() is None:
                remaining = _remaining(absolute_deadline)
                if os.fstat(stdout_file.fileno()).st_size > response_limit:
                    raise UnsupportedRepositoryError(
                        f"{label} response exceeds the trusted limit"
                    )
                if (
                    os.fstat(stderr_file.fileno()).st_size
                    > _MAX_GIT_STDERR_BYTES
                ):
                    raise UnsupportedRepositoryError(
                        f"{label} stderr exceeds the trusted limit"
                    )
                time.sleep(min(0.005, remaining))
            # A coded result is not proof that a Git/FS descendant exited.
            if not owns_process_tree:
                pass
            elif os.name == "posix":
                if not _signal_capture_helper(
                    process,
                    getattr(signal, "SIGKILL", None),
                    force=True,
                    process_group=process_group,
                ):
                    raise ProofStaleError(
                        f"proof_stale: {label} process group cleanup failed"
                    )
                if process_group is None:
                    raise ProofStaleError(
                        f"proof_stale: {label} process group ownership was lost"
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
                    f"proof_stale: {label} containment is unavailable"
                )
        except BaseException:
            if owns_process_tree:
                owned_containment = containment
                containment = None
                _terminate_capture_helper(
                    process, owned_containment, process_group,
                )
            else:
                _stop_git_process(process)
            raise
        finally:
            _close_capture_helper_containment(containment)

        _remaining(absolute_deadline)
        output = _read_bounded_file(
            stdout_file,
            limit=response_limit,
            label=f"{label} response",
            deadline=absolute_deadline,
            digest_only=False,
        )
        stderr = _read_bounded_file(
            stderr_file,
            limit=_MAX_GIT_STDERR_BYTES,
            label=f"{label} stderr",
            deadline=absolute_deadline,
            digest_only=False,
        )
        assert isinstance(output, bytes) and isinstance(stderr, bytes)
        if process.returncode != 0:
            detail = os.fsdecode(stderr[-4096:]).strip() or f"exit {process.returncode}"
            raise SourceBoundaryError(f"{label} failed: {detail}")
        return output
    finally:
        stdin_file.close()
        stdout_file.close()
        stderr_file.close()


def _run_source_helper(
    authority: _CaptureAuthority,
    operation: str,
    request_value: object,
    deadline: float,
) -> object:
    """Run one source operation in the isolated, deadline-owned helper."""

    absolute_deadline = float(deadline)
    request = pickle.dumps(
        (
            authority.implementation_sha256,
            operation,
            request_value,
            _remaining(absolute_deadline),
        ),
        protocol=5,
    )
    output = _run_bounded_helper_process(
        _capture_helper_argv(authority),
        _capture_helper_environment(authority),
        request,
        absolute_deadline,
        # Relative workspace hints are resolved inside the bounded helper
        # against the kernel-inherited cwd.
        cwd=None if operation in {"resolve", "inspect_boundary"} else os.sep,
        label="trusted capture helper",
        response_limit=_MAX_HELPER_RESPONSE_BYTES,
    )
    _verify_capture_authority(authority, deadline=absolute_deadline)
    try:
        payload = pickle.loads(output)
    except (EOFError, pickle.PickleError, AttributeError, ValueError) as exc:
        raise SourceBoundaryError(
            "trusted capture helper returned an invalid response"
        ) from exc
    _remaining(absolute_deadline)
    if not isinstance(payload, tuple) or len(payload) < 3:
        raise SourceBoundaryError(
            "trusted capture helper returned a malformed response"
        )
    status, identity, *fields = payload
    if identity != authority.implementation_sha256:
        raise SourceBoundaryError(
            "trusted capture helper response identity mismatch"
        )
    if status == "error" and len(fields) == 3:
        class_name, code, message = fields
        _raise_capture_helper_error(str(class_name), str(code), str(message))
    if status != "ok" or len(fields) != 1:
        raise SourceBoundaryError(
            "trusted capture helper returned a malformed result"
        )
    return fields[0]


def _run_legacy_v1_helper(
    operation: str, workspace: str, deadline: float,
) -> object:
    """Run the candidate-only legacy proof in the shared bounded container."""

    absolute_deadline = float(deadline)
    request = json.dumps(
        {
            "operation": operation,
            "workspace": str(workspace),
            "budget": _remaining(absolute_deadline),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    output = _run_bounded_helper_process(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _LEGACY_V1_HELPER_BOOTSTRAP,
            __file__,
        ],
        _legacy_v1_helper_environment(),
        request,
        absolute_deadline,
        cwd=None,
        label="legacy V1 source helper",
        response_limit=_MAX_LEGACY_V1_RESPONSE_BYTES,
    )
    try:
        payload = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceBoundaryError(
            "legacy V1 helper returned an invalid response"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"ok", "result"}:
        if isinstance(payload, dict) and set(payload) == {"ok", "code", "message"}:
            _raise_capture_helper_error(
                "", str(payload["code"]), str(payload["message"]),
            )
        raise SourceBoundaryError(
            "legacy V1 helper returned a malformed response"
        )
    if payload["ok"] is not True:
        raise SourceBoundaryError(
            "legacy V1 helper returned a malformed result"
        )
    return payload["result"]


def capture_source_snapshot(repo: RepoIdentity, deadline: float) -> SourceSnapshot:
    """Capture in an isolated, deadline-killable trusted helper process."""

    absolute_deadline = float(deadline)
    authority = _verify_public_authority(deadline=absolute_deadline)
    try:
        snapshot = _run_source_helper(
            authority, "capture", repo, absolute_deadline,
        )
        if (
            not isinstance(snapshot, SourceSnapshot)
            or snapshot.repo != repo
            or snapshot.capture_implementation_sha256
            != authority.implementation_sha256
        ):
            raise SourceBoundaryError(
                "trusted capture helper returned an unbound snapshot"
            )
        return snapshot
    finally:
        _verify_public_authority_after(authority, deadline=absolute_deadline)


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
        authority = _verify_public_authority(deadline=absolute_deadline)
    except SourceBoundaryError:
        return False
    matches = False
    try:
        try:
            actual_repo = _run_source_helper(
                authority,
                "resolve",
                expected.repo.workspace,
                absolute_deadline,
            )
            if not isinstance(actual_repo, RepoIdentity):
                raise SourceBoundaryError(
                    "trusted capture helper returned an invalid repository identity"
                )
            if _same_repository(expected.repo, actual_repo):
                actual = capture_source_snapshot(actual_repo, absolute_deadline)
                matches = actual == expected
        except SourceBoundaryError:
            matches = False
    finally:
        try:
            _verify_public_authority_after(authority, deadline=absolute_deadline)
        except SourceBoundaryError:
            matches = False
    return matches


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


def _atomic_publish_backend() -> str:
    if os.name != "posix":
        raise UnsupportedRepositoryError(
            "exact-tree export requires a supported POSIX host"
        )
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        return "darwin"
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        return "linux"
    raise UnsupportedRepositoryError(
        "exact-tree export host lacks atomic no-replace publication"
    )


def _assert_export_host_supported() -> str:
    backend = _atomic_publish_backend()
    if not _EXPORT_DIR_FD_SUPPORTED:
        raise UnsupportedRepositoryError(
            "exact-tree export host lacks required POSIX no-follow primitives"
        )
    return backend


def _remove_empty_owned_staging(
    parent_fd: int,
    staging_leaf: bytes,
    root_fd: int,
    root_identity: tuple[int, int],
) -> None:
    opened = os.fstat(root_fd)
    current = os.stat(staging_leaf, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != root_identity
        or (current.st_dev, current.st_ino) != root_identity
    ):
        raise SourceBoundaryError(
            "owned exact-tree staging directory identity changed"
        )
    os.rmdir(staging_leaf, dir_fd=parent_fd)


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
    staging_leaf: bytes | None = None
    root_identity: tuple[int, int] | None = None
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
        for _attempt in range(16):
            _remaining(deadline)
            token = secrets.token_hex(16).encode("ascii")
            staging_leaf = b"." + leaf[:64] + b".bestplan-staging-" + token
            try:
                os.mkdir(staging_leaf, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise SourceBoundaryError(
                    f"exact-tree staging directory could not be created safely: {exc}"
                ) from exc
            break
        else:
            raise SourceBoundaryError(
                "exact-tree staging directory name could not be reserved"
            )
        created = os.stat(
            staging_leaf, dir_fd=parent_fd, follow_symlinks=False,
        )
        root_fd = os.open(
            staging_leaf, _directory_open_flags(), dir_fd=parent_fd,
        )
        opened = os.fstat(root_fd)
        if not stat.S_ISDIR(created.st_mode) or (
            created.st_dev,
            created.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise SourceBoundaryError(
                "exact-tree destination changed while it was being opened"
            )
        root_identity = (opened.st_dev, opened.st_ino)
        prepared = _PreparedDestination(
            path=canonical,
            final_leaf=leaf,
            staging_leaf=staging_leaf,
            root_fd=root_fd,
            root_identity=root_identity,
            raw_parent=raw_parent,
            canonical_parent=canonical_parent,
            parent_fds=parent_fds,
            parent_identities=parent_identities,
        )
        _verify_destination_parent(prepared, deadline=deadline)
        return prepared
    except BaseException:
        if root_fd is not None:
            cleanup_error: BaseException | None = None
            if staging_leaf is not None and root_identity is not None:
                try:
                    _remove_empty_owned_staging(
                        parent_fd, staging_leaf, root_fd, root_identity,
                    )
                except BaseException as exc:
                    cleanup_error = exc
            os.close(root_fd)
            if cleanup_error is not None:
                _close_fds(parent_fds)
                raise SourceBoundaryError(
                    "exact-tree owned staging cleanup failed"
                ) from cleanup_error
        _close_fds(parent_fds)
        raise


class _DeadlinePipeReader:
    def __init__(self, fd: int, deadline: float):
        self.fd = fd
        self.deadline = deadline
        self.buffer = bytearray()
        self.eof = False

    def _fill(self) -> None:
        if self.eof:
            return
        while True:
            try:
                readable, _writable, _exceptional = select.select(
                    [self.fd], [], [], _remaining(self.deadline),
                )
            except InterruptedError:
                continue
            if not readable:
                raise ProofStaleError(
                    "proof_stale: git cat-file exceeded the export deadline"
                )
            try:
                chunk = os.read(self.fd, _BUFFER_SIZE)
            except BlockingIOError:
                continue
            if not chunk:
                self.eof = True
                return
            self.buffer.extend(chunk)
            return

    def read_line(self, limit: int) -> bytes:
        while True:
            _remaining(self.deadline)
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline > limit:
                    raise UnsupportedRepositoryError(
                        "git cat-file header exceeds the trusted metadata limit"
                    )
                value = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                return value
            if len(self.buffer) > limit:
                raise UnsupportedRepositoryError(
                    "git cat-file header exceeds the trusted metadata limit"
                )
            self._fill()
            if self.eof:
                raise SourceBoundaryError(
                    "git cat-file returned an incomplete blob header"
                )

    def iter_exact(self, size: int) -> Iterable[bytes]:
        remaining = size
        while remaining:
            _remaining(self.deadline)
            if not self.buffer:
                self._fill()
                if self.eof:
                    raise SourceBoundaryError(
                        "git cat-file returned an incomplete blob"
                    )
            length = min(remaining, len(self.buffer), _BUFFER_SIZE)
            chunk = bytes(self.buffer[:length])
            del self.buffer[:length]
            remaining -= length
            yield chunk

    def read_exact_bytes(self, size: int) -> bytes:
        chunks: list[bytes] = []
        for chunk in self.iter_exact(size):
            chunks.append(chunk)
        return b"".join(chunks)

    def require_eof(self) -> None:
        if self.buffer:
            raise SourceBoundaryError("git cat-file returned trailing batch data")
        self._fill()
        if self.buffer or not self.eof:
            raise SourceBoundaryError("git cat-file returned trailing batch data")


def _write_pipe_all(fd: int, value: bytes, *, deadline: float) -> None:
    offset = 0
    while offset < len(value):
        _remaining(deadline)
        try:
            _readable, writable, _exceptional = select.select(
                [], [fd], [], _remaining(deadline),
            )
        except InterruptedError:
            continue
        if not writable:
            raise ProofStaleError(
                "proof_stale: git cat-file request exceeded the export deadline"
            )
        try:
            written = os.write(fd, value[offset:])
        except BlockingIOError:
            continue
        if written <= 0:
            raise SourceBoundaryError("git cat-file request made no progress")
        offset += written


def _wait_for_git_batch(
    process: subprocess.Popen,
    stderr_file: BinaryIO,
    *,
    deadline: float,
) -> None:
    while process.poll() is None:
        remaining = _remaining(deadline)
        if os.fstat(stderr_file.fileno()).st_size > _MAX_GIT_STDERR_BYTES:
            _stop_git_process(process)
            raise SourceBoundaryError(
                "git cat-file stderr exceeds the trusted limit"
            )
        time.sleep(min(0.005, remaining))


def _materialize_blobs(
    repo: RepoIdentity,
    entries: tuple[_TreeEntry, ...],
    root_fd: int,
    *,
    deadline: float,
) -> tuple[_ExportWitness, ...]:
    _remaining(deadline)
    authority = _get_capture_authority(deadline)
    stderr_file = tempfile.TemporaryFile()
    witnesses: list[_ExportWitness] = []
    try:
        try:
            process = subprocess.Popen(
                _trusted_git_argv(authority, ("cat-file", "--batch")),
                cwd=repo.worktree_raw,
                env=_git_environment(authority),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                close_fds=True,
            )
        except OSError as exc:
            raise SourceBoundaryError(
                f"trusted git cat-file could not start: {exc}"
            ) from exc
        try:
            if process.stdin is None or process.stdout is None:
                raise SourceBoundaryError("git cat-file pipes are unavailable")
            input_fd = process.stdin.fileno()
            output_fd = process.stdout.fileno()
            os.set_blocking(input_fd, False)
            os.set_blocking(output_fd, False)
            reader = _DeadlinePipeReader(output_fd, deadline)
            total_size = 0
            for entry in entries:
                _remaining(deadline)
                _write_pipe_all(
                    input_fd, entry.oid.encode("ascii") + b"\n", deadline=deadline,
                )
                header = reader.read_line(1024)
                try:
                    returned_oid, object_type, size_raw = header.split(b" ", 2)
                    size = int(size_raw)
                except (ValueError, UnicodeError) as exc:
                    raise SourceBoundaryError(
                        "git cat-file returned an invalid blob header"
                    ) from exc
                if (
                    returned_oid.decode("ascii") != entry.oid
                    or object_type != b"blob"
                ):
                    raise SourceBoundaryError(
                        "git cat-file returned the wrong committed object"
                    )
                if size < 0 or size > _MAX_BLOB_BYTES:
                    raise UnsupportedRepositoryError(
                        "committed blob exceeds the trusted blob limit"
                    )
                total_size += size
                if total_size > _MAX_EXPORT_BYTES:
                    raise UnsupportedRepositoryError(
                        "committed tree exceeds the trusted aggregate blob limit"
                    )
                parts = tuple(entry.path.split(b"/"))
                parent_fd = _export_parent_fd(
                    root_fd, parts[:-1], deadline=deadline,
                )
                try:
                    if entry.mode == 0o120000:
                        if size > _MAX_SYMLINK_TARGET_BYTES:
                            raise UnsupportedRepositoryError(
                                "Git symlink target exceeds the trusted blob limit"
                            )
                        target = reader.read_exact_bytes(size)
                        if not target or b"\0" in target:
                            raise UnsupportedRepositoryError(
                                "Git tree contains an unsafe symlink target"
                            )
                        try:
                            os.symlink(target, parts[-1], dir_fd=parent_fd)
                        except OSError as exc:
                            raise SourceBoundaryError(
                                "exact-tree symlink could not be created safely: "
                                f"{exc}"
                            ) from exc
                        witnesses.append(
                            _ExportWitness(
                                path=entry.path,
                                size=len(target),
                                content_sha256=hashlib.sha256(target).hexdigest(),
                            )
                        )
                    else:
                        written_size, content_sha256 = _write_export_file(
                            parent_fd,
                            parts[-1],
                            reader,
                            size,
                            entry.mode & 0o777,
                            deadline=deadline,
                        )
                        witnesses.append(
                            _ExportWitness(
                                path=entry.path,
                                size=written_size,
                                content_sha256=content_sha256,
                            )
                        )
                finally:
                    os.close(parent_fd)
                if reader.read_exact_bytes(1) != b"\n":
                    raise SourceBoundaryError(
                        "git cat-file returned an incomplete blob delimiter"
                    )
            process.stdin.close()
            reader.require_eof()
            _wait_for_git_batch(process, stderr_file, deadline=deadline)
            if process.returncode != 0:
                detail = _read_bounded_file(
                    stderr_file,
                    limit=_MAX_GIT_STDERR_BYTES,
                    label="git cat-file stderr",
                    deadline=deadline,
                    digest_only=False,
                )
                assert isinstance(detail, bytes)
                raise SourceBoundaryError(
                    "trusted git cat-file failed: "
                    + (os.fsdecode(detail.strip()) or f"exit {process.returncode}")
                )
        except BaseException:
            _stop_git_process(process)
            raise
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
    finally:
        stderr_file.close()
    return tuple(witnesses)


def _export_parent_fd(
    root_fd: int,
    parts: tuple[bytes, ...],
    *,
    deadline: float,
) -> int:
    """Open one destination parent without retaining a descriptor per directory."""

    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.dup(root_fd)
    try:
        for component in parts:
            _remaining(deadline)
            created = False
            try:
                os.mkdir(component, mode=0o755, dir_fd=current_fd)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise SourceBoundaryError(
                    f"exact-tree directory changed during export: {exc}"
                ) from exc
            try:
                next_fd = os.open(
                    component, directory_flags, dir_fd=current_fd,
                )
            except OSError as exc:
                raise SourceBoundaryError(
                    f"exact-tree directory changed during export: {exc}"
                ) from exc
            try:
                if created:
                    os.fchmod(next_fd, 0o755)
                opened = os.fstat(next_fd)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or stat.S_IMODE(opened.st_mode) != 0o755
                ):
                    raise SourceBoundaryError(
                        "exact-tree directory mode changed during export"
                    )
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_owned_relative_directory(
    root_fd: int,
    parts: tuple[bytes, ...],
    expected_identity: tuple[int, int],
    *,
    deadline: float,
) -> int:
    """Reopen one owned directory with a constant number of live descriptors."""

    _remaining(deadline)
    current_fd = os.dup(root_fd)
    try:
        for component in parts:
            _remaining(deadline)
            next_fd = os.open(
                component, _directory_open_flags(), dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        opened = os.fstat(current_fd)
        if not stat.S_ISDIR(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != expected_identity:
            raise SourceBoundaryError(
                "owned exact-tree staging directory was substituted"
            )
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _remove_owned_tree_contents(directory_fd: int, *, deadline: float) -> None:
    """Remove an owned tree iteratively without retaining one FD per depth."""

    root = os.fstat(directory_fd)
    root_identity = (root.st_dev, root.st_ino)
    stack: list[
        tuple[
            tuple[bytes, ...],
            tuple[int, int],
            tuple[int, int] | None,
            bool,
        ]
    ] = [((), root_identity, None, False)]
    entry_count = 0
    total_path_bytes = 0
    while stack:
        _remaining(deadline)
        parts, identity, parent_identity, expanded = stack.pop()
        if expanded:
            if not parts:
                continue
            assert parent_identity is not None
            parent_fd = _open_owned_relative_directory(
                directory_fd,
                parts[:-1],
                parent_identity,
                deadline=deadline,
            )
            try:
                current = os.stat(
                    parts[-1], dir_fd=parent_fd, follow_symlinks=False,
                )
                if not stat.S_ISDIR(current.st_mode) or (
                    current.st_dev,
                    current.st_ino,
                ) != identity:
                    raise SourceBoundaryError(
                        "owned exact-tree staging directory was substituted"
                    )
                child_fd = os.open(
                    parts[-1], _directory_open_flags(), dir_fd=parent_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != identity:
                        raise SourceBoundaryError(
                            "owned exact-tree staging directory was substituted"
                        )
                finally:
                    os.close(child_fd)
                os.rmdir(parts[-1], dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
            continue

        current_fd = _open_owned_relative_directory(
            directory_fd, parts, identity, deadline=deadline,
        )
        child_directories: list[
            tuple[tuple[bytes, ...], tuple[int, int], tuple[int, int], bool]
        ] = []
        try:
            try:
                with os.scandir(current_fd) as iterator:
                    names: list[bytes] = []
                    for entry in iterator:
                        _remaining(deadline)
                        name = os.fsencode(entry.name)
                        entry_count += 1
                        total_path_bytes += sum(len(part) + 1 for part in parts) + len(name)
                        if (
                            entry_count > _MAX_TREE_ENTRIES
                            or len(name) > _MAX_PATH_BYTES
                            or total_path_bytes > _MAX_TOTAL_PATH_BYTES
                        ):
                            raise UnsupportedRepositoryError(
                                "owned staging metadata exceeds the trusted limit"
                            )
                        names.append(name)
            except OSError as exc:
                raise SourceBoundaryError(
                    "owned exact-tree staging directory could not be enumerated: "
                    f"{exc}"
                ) from exc
            for name in names:
                _remaining(deadline)
                info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(
                        name, _directory_open_flags(), dir_fd=current_fd,
                    )
                    try:
                        opened = os.fstat(child_fd)
                        child_identity = (opened.st_dev, opened.st_ino)
                        if child_identity != (info.st_dev, info.st_ino):
                            raise SourceBoundaryError(
                                "owned exact-tree staging directory was substituted"
                            )
                    finally:
                        os.close(child_fd)
                    child_directories.append(
                        ((*parts, name), child_identity, identity, False)
                    )
                elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    os.unlink(name, dir_fd=current_fd)
                else:
                    raise SourceBoundaryError(
                        "owned exact-tree staging contains an unexpected file type"
                    )
        except OSError as exc:
            raise SourceBoundaryError(
                f"owned exact-tree staging cleanup failed: {exc}"
            ) from exc
        finally:
            os.close(current_fd)
        stack.append((parts, identity, parent_identity, True))
        stack.extend(child_directories)


def _cleanup_owned_tree(
    prepared: _PreparedDestination, leaf: bytes, *, label: str,
) -> None:
    cleanup_deadline = time.monotonic() + _EXPORT_CLEANUP_SECONDS
    opened = os.fstat(prepared.root_fd)
    current = os.stat(
        leaf,
        dir_fd=prepared.parent_fds[-1],
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != prepared.root_identity
        or (current.st_dev, current.st_ino) != prepared.root_identity
    ):
        raise SourceBoundaryError(
            f"owned exact-tree {label} identity changed; cleanup quarantined"
        )
    _remove_owned_tree_contents(prepared.root_fd, deadline=cleanup_deadline)
    _remaining(cleanup_deadline)
    current = os.stat(
        leaf,
        dir_fd=prepared.parent_fds[-1],
        follow_symlinks=False,
    )
    if (current.st_dev, current.st_ino) != prepared.root_identity:
        raise SourceBoundaryError(
            f"owned exact-tree {label} identity changed; cleanup quarantined"
        )
    os.rmdir(leaf, dir_fd=prepared.parent_fds[-1])


def _cleanup_owned_staging(prepared: _PreparedDestination) -> None:
    _cleanup_owned_tree(prepared, prepared.staging_leaf, label="staging")


def _rename_leaf_no_replace(
    parent_fd: int,
    source_leaf: bytes,
    destination_leaf: bytes,
    *,
    backend: str,
) -> None:
    """Atomically rename one sibling without replacing an existing name."""

    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if backend == "darwin":
        rename = libc.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            parent_fd,
            source_leaf,
            parent_fd,
            destination_leaf,
            0x00000004,  # RENAME_EXCL
        )
    elif backend == "linux":
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            parent_fd,
            source_leaf,
            parent_fd,
            destination_leaf,
            1,  # RENAME_NOREPLACE
        )
    else:
        raise UnsupportedRepositoryError(
            "exact-tree export host lacks atomic no-replace publication"
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number))
    if error_number in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
        raise UnsupportedRepositoryError(
            "exact-tree export host rejected atomic no-replace publication"
        )
    raise SourceBoundaryError(
        f"exact-tree atomic rename failed: {os.strerror(error_number)}"
    )


def _publish_staging_no_replace(
    prepared: _PreparedDestination, *, backend: str, deadline: float,
) -> None:
    _remaining(deadline)
    try:
        os.stat(
            prepared.final_leaf,
            dir_fd=prepared.parent_fds[-1],
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        raise SourceBoundaryError(
            "exact-tree destination appeared before atomic publication"
        )
    _verify_destination_parent(prepared, deadline=deadline)
    opened = os.fstat(prepared.root_fd)
    if (opened.st_dev, opened.st_ino) != prepared.root_identity:
        raise SourceBoundaryError("exact-tree staging root identity changed")
    parent_fd = prepared.parent_fds[-1]
    try:
        _rename_leaf_no_replace(
            parent_fd,
            prepared.staging_leaf,
            prepared.final_leaf,
            backend=backend,
        )
    except FileExistsError as exc:
        raise SourceBoundaryError(
            "exact-tree destination collided during atomic publication"
        ) from exc


def _verify_owned_destination_name(
    prepared: _PreparedDestination,
    leaf: bytes,
    *,
    deadline: float,
) -> None:
    _remaining(deadline)
    named = os.stat(
        leaf,
        dir_fd=prepared.parent_fds[-1],
        follow_symlinks=False,
    )
    if not stat.S_ISDIR(named.st_mode) or (
        named.st_dev,
        named.st_ino,
    ) != prepared.root_identity:
        raise SourceBoundaryError(
            "exact-tree destination was substituted during publication"
        )
    _verify_destination_parent(prepared, deadline=deadline)


def _verify_published_destination(
    prepared: _PreparedDestination, *, deadline: float,
) -> None:
    _verify_owned_destination_name(
        prepared, prepared.final_leaf, deadline=deadline,
    )


def _verify_exported_tree(
    prepared: _PreparedDestination,
    entries: tuple[_TreeEntry, ...],
    witnesses: tuple[_ExportWitness, ...],
    *,
    object_format: str,
    deadline: float,
) -> None:
    """Verify every published byte through the retained no-follow root FD."""

    oid_length = {"sha1": 40, "sha256": 64}.get(object_format)
    if oid_length is None:
        raise UnsupportedRepositoryError(
            "exact-tree verifier received an unsupported Git object format"
        )
    expected_files: dict[bytes, _TreeEntry] = {}
    for entry in entries:
        _remaining(deadline)
        if (
            entry.path in expected_files
            or entry.object_type != b"blob"
            or entry.mode not in {0o100644, 0o100755, 0o120000}
            or len(entry.oid) != oid_length
            or any(character not in "0123456789abcdef" for character in entry.oid)
        ):
            raise SourceBoundaryError(
                "exact-tree verifier received inconsistent tree metadata"
            )
        expected_files[entry.path] = entry
    expected_witnesses: dict[bytes, _ExportWitness] = {}
    for witness in witnesses:
        _remaining(deadline)
        if (
            witness.path in expected_witnesses
            or witness.size < 0
            or witness.size > _MAX_BLOB_BYTES
            or len(witness.content_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in witness.content_sha256
            )
        ):
            raise SourceBoundaryError(
                "exact-tree verifier received inconsistent content witnesses"
            )
        expected_witnesses[witness.path] = witness
    if set(expected_witnesses) != set(expected_files):
        raise SourceBoundaryError(
            "exact-tree verifier content witnesses are incomplete"
        )
    expected_directories: set[bytes] = set()
    for entry in entries:
        _remaining(deadline)
        parts = entry.path.split(b"/")
        for length in range(1, len(parts)):
            _remaining(deadline)
            expected_directories.add(b"/".join(parts[:length]))

    root_before = os.fstat(prepared.root_fd)
    if (
        not stat.S_ISDIR(root_before.st_mode)
        or (root_before.st_dev, root_before.st_ino) != prepared.root_identity
        or stat.S_IMODE(root_before.st_mode) != 0o755
    ):
        raise SourceBoundaryError(
            "exact-tree published root does not match the exported tree"
        )
    seen_files: set[bytes] = set()
    seen_directories: set[bytes] = set()
    stack: list[tuple[tuple[bytes, ...], tuple[int, int]]] = [
        ((), prepared.root_identity),
    ]
    entry_count = 0
    total_path_bytes = 0
    total_content_bytes = 0
    while stack:
        _remaining(deadline)
        parts, identity = stack.pop()
        directory_fd = _open_owned_relative_directory(
            prepared.root_fd, parts, identity, deadline=deadline,
        )
        directory_after: os.stat_result | None = None
        try:
            directory_before = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(directory_before.st_mode)
                or (directory_before.st_dev, directory_before.st_ino) != identity
                or stat.S_IMODE(directory_before.st_mode) != 0o755
            ):
                raise SourceBoundaryError(
                    "exact-tree published directory metadata changed"
                )
            prefix_size = sum(len(part) + 1 for part in parts)
            with os.scandir(directory_fd) as iterator:
                names: list[bytes] = []
                for item in iterator:
                    _remaining(deadline)
                    name = os.fsencode(item.name)
                    entry_count += 1
                    total_path_bytes += prefix_size + len(name)
                    if (
                        entry_count > _MAX_TREE_ENTRIES
                        or len(name) > _MAX_PATH_BYTES
                        or total_path_bytes > _MAX_TOTAL_PATH_BYTES
                    ):
                        raise UnsupportedRepositoryError(
                            "exact-tree verification metadata exceeds the trusted limit"
                        )
                    names.append(name)
            for name in names:
                _remaining(deadline)
                path_parts = (*parts, name)
                path = b"/".join(path_parts)
                info = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False,
                )
                if stat.S_ISDIR(info.st_mode):
                    if (
                        path not in expected_directories
                        or stat.S_IMODE(info.st_mode) != 0o755
                    ):
                        raise SourceBoundaryError(
                            "exact-tree published directory set or mode changed"
                        )
                    child_fd = os.open(
                        name, _directory_open_flags(), dir_fd=directory_fd,
                    )
                    try:
                        opened = os.fstat(child_fd)
                        child_identity = (opened.st_dev, opened.st_ino)
                        if (
                            child_identity != (info.st_dev, info.st_ino)
                            or stat.S_IMODE(opened.st_mode) != 0o755
                        ):
                            raise SourceBoundaryError(
                                "exact-tree published directory was substituted"
                            )
                    finally:
                        os.close(child_fd)
                    seen_directories.add(path)
                    stack.append((path_parts, child_identity))
                    continue

                expected = expected_files.get(path)
                witness = expected_witnesses.get(path)
                if expected is None or witness is None:
                    raise SourceBoundaryError(
                        "exact-tree published path set contains an extra entry"
                    )
                if stat.S_ISREG(info.st_mode):
                    expected_mode = expected.mode & 0o777
                    if (
                        expected.mode not in {0o100644, 0o100755}
                        or stat.S_IMODE(info.st_mode) != expected_mode
                        or info.st_size != witness.size
                    ):
                        raise SourceBoundaryError(
                            "exact-tree published regular file metadata changed"
                        )
                    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    flags |= getattr(os, "O_NONBLOCK", 0)
                    fd = os.open(name, flags, dir_fd=directory_fd)
                    try:
                        opened = os.fstat(fd)
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or (opened.st_dev, opened.st_ino)
                            != (info.st_dev, info.st_ino)
                            or stat.S_IMODE(opened.st_mode) != expected_mode
                            or opened.st_size != witness.size
                            or opened.st_size > _MAX_BLOB_BYTES
                        ):
                            raise SourceBoundaryError(
                                "exact-tree published regular file was substituted"
                            )
                        content_digest = hashlib.sha256()
                        git_digest = hashlib.new(object_format)
                        git_digest.update(
                            f"blob {witness.size}\0".encode("ascii")
                        )
                        size = 0
                        while True:
                            _remaining(deadline)
                            chunk = os.read(fd, _BUFFER_SIZE)
                            if not chunk:
                                break
                            size += len(chunk)
                            total_content_bytes += len(chunk)
                            if total_content_bytes > _MAX_EXPORT_BYTES:
                                raise UnsupportedRepositoryError(
                                    "exact-tree verification content exceeds "
                                    "the trusted limit"
                                )
                            content_digest.update(chunk)
                            git_digest.update(chunk)
                        after = os.fstat(fd)
                    finally:
                        os.close(fd)
                    after_path = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if (
                        _path_state(opened) != _path_state(after)
                        or _path_state(after) != _path_state(after_path)
                        or not stat.S_ISREG(after_path.st_mode)
                        or size != witness.size
                        or content_digest.hexdigest() != witness.content_sha256
                        or git_digest.hexdigest() != expected.oid
                    ):
                        raise SourceBoundaryError(
                            "exact-tree published regular file content changed"
                        )
                elif stat.S_ISLNK(info.st_mode):
                    if expected.mode != 0o120000:
                        raise SourceBoundaryError(
                            "exact-tree published path type changed"
                        )
                    target = os.readlink(name, dir_fd=directory_fd)
                    target_raw = os.fsencode(target)
                    if (
                        len(target_raw) != witness.size
                        or len(target_raw) > _MAX_SYMLINK_TARGET_BYTES
                    ):
                        raise SourceBoundaryError(
                            "exact-tree published symlink length changed"
                        )
                    content_digest = hashlib.sha256(target_raw)
                    git_digest = hashlib.new(object_format)
                    git_digest.update(
                        f"blob {len(target_raw)}\0".encode("ascii")
                    )
                    git_digest.update(target_raw)
                    after = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False,
                    )
                    total_content_bytes += len(target_raw)
                    if (
                        total_content_bytes > _MAX_EXPORT_BYTES
                        or _path_state(info) != _path_state(after)
                        or content_digest.hexdigest() != witness.content_sha256
                        or git_digest.hexdigest() != expected.oid
                    ):
                        raise SourceBoundaryError(
                            "exact-tree published symlink target changed"
                        )
                else:
                    raise SourceBoundaryError(
                        "exact-tree published path type is unsupported"
                    )
                seen_files.add(path)
            directory_after = os.fstat(directory_fd)
            if _path_state(directory_before) != _path_state(directory_after):
                raise SourceBoundaryError(
                    "exact-tree published directory changed during verification"
                )
        finally:
            os.close(directory_fd)
        assert directory_after is not None
        rebound_fd = _open_owned_relative_directory(
            prepared.root_fd, parts, identity, deadline=deadline,
        )
        try:
            rebound = os.fstat(rebound_fd)
            if _path_state(directory_after) != _path_state(rebound):
                raise SourceBoundaryError(
                    "exact-tree published directory pathname was rebound"
                )
        finally:
            os.close(rebound_fd)
    if (
        seen_files != set(expected_files)
        or seen_directories != expected_directories
    ):
        raise SourceBoundaryError(
            "exact-tree published raw path set does not match the committed tree"
        )
    root_after = os.fstat(prepared.root_fd)
    if _path_state(root_before) != _path_state(root_after):
        raise SourceBoundaryError(
            "exact-tree published root changed during verification"
        )


def _verify_published_exact_tree(
    prepared: _PreparedDestination,
    entries: tuple[_TreeEntry, ...],
    witnesses: tuple[_ExportWitness, ...],
    *,
    object_format: str,
    deadline: float,
) -> None:
    """Require two stable, name-bound observations of every published byte."""

    for _observation in range(_STABLE_EXPORT_OBSERVATIONS):
        _verify_published_destination(prepared, deadline=deadline)
        _verify_exported_tree(
            prepared,
            entries,
            witnesses,
            object_format=object_format,
            deadline=deadline,
        )
        _verify_published_destination(prepared, deadline=deadline)


def _quarantined_tree_matches_export(
    prepared: _PreparedDestination,
    entries: tuple[_TreeEntry, ...],
    witnesses: tuple[_ExportWitness, ...],
    *,
    quarantine_leaf: bytes,
    object_format: str,
    deadline: float,
) -> bool:
    """Classify a quarantine only after two stable, name-bound observations."""

    try:
        for _observation in range(_STABLE_EXPORT_OBSERVATIONS):
            _verify_owned_destination_name(
                prepared, quarantine_leaf, deadline=deadline,
            )
            _verify_exported_tree(
                prepared,
                entries,
                witnesses,
                object_format=object_format,
                deadline=deadline,
            )
            _verify_owned_destination_name(
                prepared, quarantine_leaf, deadline=deadline,
            )
    except (OSError, SourceBoundaryError):
        return False
    return True


def _quarantine_owned_published(
    prepared: _PreparedDestination,
    entries: tuple[_TreeEntry, ...],
    witnesses: tuple[_ExportWitness, ...],
    *,
    backend: str,
    object_format: str,
) -> tuple[str, bool]:
    """Move a failed published tree aside without deleting concurrent bytes."""

    deadline = time.monotonic() + _DEFAULT_DEADLINE_SECONDS
    _verify_destination_parent(prepared, deadline=deadline)
    opened = os.fstat(prepared.root_fd)
    current = os.stat(
        prepared.final_leaf,
        dir_fd=prepared.parent_fds[-1],
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != prepared.root_identity
        or (current.st_dev, current.st_ino) != prepared.root_identity
    ):
        raise SourceBoundaryError(
            "owned exact-tree destination identity changed; cleanup quarantined"
        )
    for _attempt in range(16):
        quarantine_leaf = (
            b"."
            + prepared.final_leaf[:64]
            + b".bestplan-quarantine-"
            + secrets.token_hex(16).encode("ascii")
        )
        try:
            _rename_leaf_no_replace(
                prepared.parent_fds[-1],
                prepared.final_leaf,
                quarantine_leaf,
                backend=backend,
            )
        except FileExistsError:
            continue
        break
    else:
        raise SourceBoundaryError(
            "owned exact-tree quarantine name could not be reserved"
        )
    _remaining(deadline)
    quarantined = os.stat(
        quarantine_leaf,
        dir_fd=prepared.parent_fds[-1],
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(quarantined.st_mode)
        or (quarantined.st_dev, quarantined.st_ino) != prepared.root_identity
    ):
        raise SourceBoundaryError(
            "owned exact-tree quarantine identity changed"
        )
    quarantine_path = os.path.join(prepared.canonical_parent, quarantine_leaf)
    classification_deadline = time.monotonic() + _DEFAULT_DEADLINE_SECONDS
    unchanged = _quarantined_tree_matches_export(
        prepared,
        entries,
        witnesses,
        quarantine_leaf=quarantine_leaf,
        object_format=object_format,
        deadline=classification_deadline,
    )
    return os.fsdecode(quarantine_path), unchanged


def _write_export_file(
    parent_fd: int,
    name: bytes,
    reader: _DeadlinePipeReader,
    size: int,
    mode: int,
    *,
    deadline: float,
) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, mode, dir_fd=parent_fd)
    except OSError as exc:
        raise SourceBoundaryError(
            f"exact-tree file could not be created safely: {exc}"
        ) from exc
    try:
        total = 0
        digest = hashlib.sha256()
        for chunk in reader.iter_exact(size):
            _remaining(deadline)
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                _remaining(deadline)
                written = os.write(fd, chunk[offset:])
                if written <= 0:
                    raise SourceBoundaryError(
                        "exact-tree file write made no progress"
                    )
                offset += written
                total += written
        if total != size:
            raise SourceBoundaryError("exact-tree file write was incomplete")
        _remaining(deadline)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    return total, digest.hexdigest()


def export_exact_tree(
    snapshot: SourceSnapshot, destination: str | os.PathLike[str],
) -> None:
    """Materialize and verify the captured committed tree without filters.

    The final bounded content walk establishes exactness immediately before this
    call returns. Mutation after return belongs to the later consumer/root
    authority boundary and is intentionally outside this Task 1 API.
    """

    backend = _assert_export_host_supported()
    authority_deadline = time.monotonic() + _DEFAULT_DEADLINE_SECONDS
    authority = _verify_public_authority(deadline=authority_deadline)
    prepared: _PreparedDestination | None = None
    entries: tuple[_TreeEntry, ...] = ()
    witnesses: tuple[_ExportWitness, ...] = ()
    published = False
    try:
        try:
            recapture_deadline = time.monotonic() + _DEFAULT_DEADLINE_SECONDS
            if not recapture_matches(snapshot, deadline=recapture_deadline):
                raise ProofStaleError(
                    "proof_stale: repository or protected state changed"
                )
            deadline = time.monotonic() + _DEFAULT_DEADLINE_SECONDS
            entries = _tree_entries(
                snapshot.repo, snapshot.tree_oid, deadline=deadline,
            )
            if any(entry.object_type != b"blob" for entry in entries):
                raise UnsupportedRepositoryError(
                    "exact source tree contains a non-blob entry"
                )
            for entry in entries:
                _remaining(deadline)
                if entry.mode not in {0o100644, 0o100755, 0o120000}:
                    raise UnsupportedRepositoryError(
                        f"unsupported Git tree mode: {entry.mode:o}"
                    )
            _assert_tree_path_aliases(entries, deadline=deadline)
            _verify_public_authority_after(authority, deadline=deadline)

            prepared = _prepare_destination(
                snapshot.repo, destination, deadline=deadline,
            )
            witnesses = _materialize_blobs(
                snapshot.repo,
                entries,
                prepared.root_fd,
                deadline=deadline,
            )
            _verify_public_authority_after(authority, deadline=deadline)
            _remaining(deadline)
            os.fchmod(prepared.root_fd, 0o755)
            staged = os.stat(
                prepared.staging_leaf,
                dir_fd=prepared.parent_fds[-1],
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(staged.st_mode) or (
                staged.st_dev,
                staged.st_ino,
            ) != prepared.root_identity:
                raise SourceBoundaryError(
                    "exact-tree staging directory was substituted during export"
                )
            _verify_destination_parent(prepared, deadline=deadline)
            _publish_staging_no_replace(
                prepared, backend=backend, deadline=deadline,
            )
            published = True
            _verify_public_authority_after(
                authority,
                deadline=time.monotonic() + _DEFAULT_DEADLINE_SECONDS,
            )
            verification_deadline = (
                time.monotonic() + _DEFAULT_DEADLINE_SECONDS
            )
            _verify_published_exact_tree(
                prepared,
                entries,
                witnesses,
                object_format=snapshot.repo.object_format,
                deadline=verification_deadline,
            )
        finally:
            _verify_public_authority_after(
                authority,
                deadline=time.monotonic() + _DEFAULT_DEADLINE_SECONDS,
            )
    except BaseException as export_error:
        if prepared is not None:
            if published:
                try:
                    quarantine_path, unchanged = _quarantine_owned_published(
                        prepared,
                        entries,
                        witnesses,
                        backend=backend,
                        object_format=snapshot.repo.object_format,
                    )
                except BaseException as cleanup_error:
                    raise SourceBoundaryError(
                        "exact-tree export failed and published output could "
                        "not be quarantined"
                    ) from cleanup_error
                state = "unchanged" if unchanged else "contains concurrent changes"
                raise SourceBoundaryError(
                    f"{export_error}; published output quarantined at "
                    f"{quarantine_path} ({state})"
                ) from export_error
            try:
                _cleanup_owned_staging(prepared)
            except BaseException as cleanup_error:
                raise SourceBoundaryError(
                    "exact-tree export failed and owned output was quarantined"
                ) from cleanup_error
        raise
    finally:
        if prepared is not None:
            try:
                os.close(prepared.root_fd)
            except OSError:
                pass
            _close_fds(prepared.parent_fds)
