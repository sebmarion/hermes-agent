"""Host-owned, commit-bound checks for Hermes BestPlan integrations.

The candidate tree never supplies an executor, environment, policy, cache
allowlist, or proof.  This module materializes one already-frozen integration
commit without Git metadata, launches enrollment-bound commands through a
default-deny macOS Seatbelt profile, and records only bounded output hashes.
"""

from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import math
import os
import secrets
import signal
import shutil  # noqa: F401 - retained as a negative test seam; never called here
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Task 6 execution is macOS-only.
    fcntl = None  # type: ignore[assignment]

from agent.bestplan_candidates import (
    CandidateError,
    _TreeRecord,
    _scan_candidate_tree,
)
from agent.bestplan_contract import (
    BoundCommand,
    ControllerIdentity,
    PinnedInput,
    canonical_json,
    source_snapshot_digest,
)
from agent.bestplan_sandbox import (
    _launcher_identity,
    _new_artifact_budget,
    _stable_artifact_tree_identity,
)


CHECK_SANDBOX_POLICY_VERSION = "bestplan-check-sandbox-v1"
MAX_CHECK_FILE_BYTES = 64 * 1024 * 1024
MAX_CHECK_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_CHECK_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_CHECK_CLEANUP_ENTRIES = 100_000
MAX_CHECK_CLEANUP_PATH_BYTES = 16 * 1024 * 1024
MAX_CHECK_CLEANUP_DEPTH = 64
EMPTY_CACHE_SHA256 = hashlib.sha256(b"hermes.bestplan.empty-cache.v1\0").hexdigest()
_SHA256 = frozenset("0123456789abcdef")
_SYSTEM_READ_ROOTS = (
    Path("/System/Library/Frameworks"),
    Path("/System/Library/PrivateFrameworks"),
    Path("/usr/lib"),
    Path("/usr/share/zoneinfo"),
)
_FIXED_ENVIRONMENT = {
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTHONSAFEPATH": "1",
}


class CheckError(RuntimeError):
    """Base class for deterministic check failures."""


class CheckValidationError(CheckError):
    """The approval-bound check input is malformed or unsupported."""


class CheckProofStale(CheckError):
    """A pinned host or integration input no longer has the admitted bytes."""


class CheckExecutionError(CheckError):
    """A check did not complete inside its bounded execution contract."""


class CheckContainmentError(CheckExecutionError):
    """Writer/process extinction could not be proved; evidence is retained."""


class CheckMutationError(CheckError):
    """A checker changed the immutable integration or an unenrolled path."""


def _check_absolute_deadline(deadline: float, label: str = "check") -> None:
    if time.monotonic() >= deadline:
        raise CheckExecutionError(f"{label} deadline expired")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= _SHA256
    )


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise CheckValidationError("check receipt input is not canonical JSON") from None


def _domain_digest(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + b"\0" + _canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class PinnedRuntimePath:
    path: Path
    sha256: str

    def __post_init__(self) -> None:
        path = Path(self.path)
        if not path.is_absolute() or not _is_sha256(self.sha256):
            raise CheckValidationError("check runtime pin is invalid")
        object.__setattr__(self, "path", path)


@dataclass(frozen=True)
class CheckHostRuntime:
    controller_source: Path
    controller: ControllerIdentity
    sandbox_executable: Path
    sandbox_executable_sha256: str
    runtime_read_paths: tuple[PinnedRuntimePath, ...]
    cache_seed_root: Path
    policy_version: str = CHECK_SANDBOX_POLICY_VERSION
    max_output_bytes: int = 4 * 1024 * 1024
    reap_grace_seconds: float = 1.0
    controller_python_launcher: Path | None = None
    pytest_module_path: Path | None = None

    def __post_init__(self) -> None:
        controller_source = Path(self.controller_source)
        sandbox = Path(self.sandbox_executable)
        cache_seed = Path(self.cache_seed_root)
        launcher = (
            None
            if self.controller_python_launcher is None
            else Path(self.controller_python_launcher)
        )
        pytest_module = (
            None
            if self.pytest_module_path is None
            else Path(self.pytest_module_path)
        )
        if not isinstance(self.controller, ControllerIdentity):
            raise CheckValidationError("check controller identity is invalid")
        if not controller_source.is_absolute() or not cache_seed.is_absolute():
            raise CheckValidationError("check host roots must be absolute")
        if sandbox != Path("/usr/bin/sandbox-exec"):
            raise CheckValidationError("check sandbox executable is unsupported")
        if not _is_sha256(self.sandbox_executable_sha256):
            raise CheckValidationError("check sandbox executable digest is invalid")
        runtime_paths = tuple(self.runtime_read_paths)
        if any(not isinstance(item, PinnedRuntimePath) for item in runtime_paths):
            raise CheckValidationError("check runtime pins are invalid")
        if tuple(sorted(runtime_paths, key=lambda item: str(item.path))) != runtime_paths:
            raise CheckValidationError("check runtime pins must be sorted")
        if len({str(item.path) for item in runtime_paths}) != len(runtime_paths):
            raise CheckValidationError("check runtime pins are duplicated")
        if (launcher is None) != (pytest_module is None):
            raise CheckValidationError("local check runtime proof is incomplete")
        if launcher is not None and (
            not launcher.is_absolute()
            or pytest_module is None
            or not pytest_module.is_absolute()
        ):
            raise CheckValidationError("local check runtime proof paths must be absolute")
        if self.policy_version != CHECK_SANDBOX_POLICY_VERSION:
            raise CheckValidationError("check sandbox policy version is unsupported")
        if (
            isinstance(self.max_output_bytes, bool)
            or not isinstance(self.max_output_bytes, int)
            or not 1 <= self.max_output_bytes <= MAX_CHECK_OUTPUT_BYTES
        ):
            raise CheckValidationError("check output bound is invalid")
        if (
            isinstance(self.reap_grace_seconds, bool)
            or not isinstance(self.reap_grace_seconds, (int, float))
            or not 0 < float(self.reap_grace_seconds) <= 10.0
        ):
            raise CheckValidationError("check reaper grace is invalid")
        object.__setattr__(self, "controller_source", controller_source)
        object.__setattr__(self, "sandbox_executable", sandbox)
        object.__setattr__(self, "cache_seed_root", cache_seed)
        object.__setattr__(self, "runtime_read_paths", runtime_paths)
        object.__setattr__(self, "controller_python_launcher", launcher)
        object.__setattr__(self, "pytest_module_path", pytest_module)


@dataclass(frozen=True)
class CheckTree:
    records: tuple[_TreeRecord, ...]
    digest: str


@dataclass
class _PreparedChecksRoot:
    path: Path
    descriptor: int
    identity: tuple[int, int, int, int]

    def verify(self) -> None:
        if self.descriptor < 0:
            raise CheckValidationError("check root descriptor is closed")
        try:
            path_info = self.path.lstat()
            opened = os.fstat(self.descriptor)
            resolved = self.path.resolve(strict=True)
        except OSError:
            raise CheckValidationError("check root identity changed") from None
        current = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
        )
        visible = (
            path_info.st_dev,
            path_info.st_ino,
            path_info.st_mode,
            path_info.st_uid,
        )
        if (
            resolved != self.path
            or current != self.identity
            or visible != self.identity
        ):
            raise CheckValidationError("check root identity changed")

    def close(self) -> None:
        descriptor = self.descriptor
        self.descriptor = -1
        if descriptor < 0:
            return
        try:
            os.close(descriptor)
        except OSError:
            pass


@dataclass
class _OwnedCheckAttempt:
    root: _PreparedChecksRoot
    leaf: str
    path: Path
    descriptor: int
    identity: tuple[int, int, int, int]

    def verify(self) -> None:
        if self.descriptor < 0:
            raise CheckValidationError("check attempt descriptor is closed")
        self.root.verify()
        try:
            named = os.stat(
                self.leaf,
                dir_fd=self.root.descriptor,
                follow_symlinks=False,
            )
            opened = os.fstat(self.descriptor)
        except OSError:
            raise CheckValidationError("check attempt identity changed") from None
        named_identity = (
            named.st_dev,
            named.st_ino,
            named.st_mode,
            named.st_uid,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
        )
        if (
            not stat.S_ISDIR(named.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or named_identity != self.identity
            or opened_identity != self.identity
        ):
            raise CheckValidationError("check attempt identity changed")

    def close(self) -> None:
        descriptor = self.descriptor
        self.descriptor = -1
        if descriptor < 0:
            return
        try:
            os.close(descriptor)
        except OSError:
            pass


@dataclass(frozen=True)
class CapturedCheckProcess:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class CheckReceipt:
    integration_oid: str
    command_id: str
    command_digest: str
    policy_digest: str
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str
    stdout_size: int
    stderr_size: int
    output_framed_sha256: str
    pre_tree_digest: str
    post_tree_digest: str
    receipt_digest: str


@dataclass(frozen=True)
class CheckSetReceipt:
    integration_oid: str
    contract_digest: str
    ordered_receipts: tuple[CheckReceipt, ...]
    receipt_digest: str


def _stable_regular_bytes(
    path: Path,
    *,
    deadline: float | None = None,
) -> bytes:
    absolute_deadline = (
        time.monotonic() + 20.0 if deadline is None else float(deadline)
    )
    _check_absolute_deadline(absolute_deadline, "pinned file")
    try:
        before = path.lstat()
    except OSError:
        raise CheckProofStale("pinned regular file is unavailable") from None
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > MAX_CHECK_FILE_BYTES
    ):
        raise CheckProofStale("pinned regular file shape is unsupported")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise CheckProofStale("pinned regular file is unavailable") from None
    try:
        opened = os.fstat(descriptor)
        data = bytearray()
        while len(data) <= MAX_CHECK_FILE_BYTES:
            _check_absolute_deadline(absolute_deadline, "pinned file")
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_CHECK_FILE_BYTES + 1 - len(data)),
            )
            if not chunk:
                break
            data.extend(chunk)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = path.lstat()
    except OSError:
        raise CheckProofStale("pinned regular file changed") from None
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if len(data) > MAX_CHECK_FILE_BYTES or not (
        identity(before)
        == identity(opened)
        == identity(after_open)
        == identity(after_path)
    ):
        raise CheckProofStale("pinned regular file changed")
    return bytes(data)


def pinned_path_sha256(
    path: str | Path,
    *,
    deadline: float | None = None,
) -> str:
    """Hash a stable regular file or a bounded no-follow directory tree."""

    absolute_deadline = (
        time.monotonic() + 20.0 if deadline is None else float(deadline)
    )
    _check_absolute_deadline(absolute_deadline, "pinned path")
    value = Path(path)
    try:
        info = value.lstat()
    except OSError:
        raise CheckProofStale("pinned path is unavailable") from None
    if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        _check_absolute_deadline(absolute_deadline, "pinned path")
        return hashlib.sha256(
            _stable_regular_bytes(value, deadline=absolute_deadline)
        ).hexdigest()
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        tree = _capture_check_tree(value, deadline=absolute_deadline)
        if not tree.records:
            return EMPTY_CACHE_SHA256
        return tree.digest
    raise CheckProofStale("pinned path shape is unsupported")


def _require_pinned_regular_file(
    path: Path,
    expected: str,
    label: str,
    *,
    deadline: float | None = None,
) -> None:
    if not _is_sha256(expected):
        raise CheckValidationError(f"{label} digest is invalid")
    actual = hashlib.sha256(
        _stable_regular_bytes(Path(path), deadline=deadline)
    ).hexdigest()
    if actual != expected:
        raise CheckProofStale(f"{label} digest changed")


def _tree_digest(records: Sequence[_TreeRecord]) -> str:
    body = [
        {
            "path_hex": item.path.hex(),
            "kind": item.kind,
            "mode": item.mode,
            "size": len(item.data),
            "data_sha256": hashlib.sha256(item.data).hexdigest(),
        }
        for item in records
    ]
    return _domain_digest(b"hermes.bestplan.check-tree.v1", body)


def _capture_check_tree(root: str | Path, *, deadline: float) -> CheckTree:
    try:
        records, _paths, _witness = _scan_candidate_tree(root, deadline=deadline)
    except CandidateError as exc:
        raise CheckProofStale("check tree did not remain stable") from exc
    return CheckTree(records=records, digest=_tree_digest(records))


def candidate_controller_artifact_sha256(
    controller_source: str | Path,
    *,
    deadline: float | None = None,
) -> str:
    absolute_deadline = (
        time.monotonic() + 20.0 if deadline is None else float(deadline)
    )
    _check_absolute_deadline(absolute_deadline, "check controller")
    try:
        return str(
            _stable_artifact_tree_identity(
                Path(controller_source),
                _new_artifact_budget(absolute_deadline),
            )["sha256"]
        )
    except (OSError, TypeError, ValueError):
        raise CheckProofStale("check controller artifact changed") from None


def _path_alias(path: bytes) -> str:
    try:
        text = path.decode("utf-8", "strict")
    except UnicodeError:
        raise CheckValidationError("check path is not valid UTF-8") from None
    return "/".join(
        unicodedata.normalize("NFC", part).casefold() for part in text.split("/")
    )


def _under(path: bytes, root: bytes) -> bool:
    return path == root or path.startswith(root + b"/")


def _cache_related_directory(path: bytes, caches: tuple[bytes, ...]) -> bool:
    return any(_under(path, root) or _under(root, path) for root in caches)


def _validate_overlay_mutations(
    *,
    baseline: CheckTree,
    current: CheckTree,
    tracked_paths: Iterable[bytes],
    cache_paths: Iterable[bytes],
) -> None:
    before = {item.path: item for item in baseline.records}
    after = {item.path: item for item in current.records}
    tracked = tuple(tracked_paths)
    caches = tuple(cache_paths)
    for path in tracked:
        if before.get(path) != after.get(path):
            raise CheckMutationError("check changed a tracked integration path")
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        if path in tracked:
            raise CheckMutationError("check changed a tracked integration path")
        record = after.get(path) or before.get(path)
        if record is not None and record.kind == "directory" and _cache_related_directory(path, caches):
            continue
        if any(_under(path, root) for root in caches):
            continue
        raise CheckMutationError("check changed an untracked path outside the frozen cache")


def parse_network_allowlist(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise CheckValidationError("check network endpoint is invalid")
        host: str
        port_raw: str
        if raw.startswith("["):
            match = __import__("re").fullmatch(r"\[([^]]+)\]:(\d+)", raw)
            if match is None:
                raise CheckValidationError("check network endpoint is invalid")
            host, port_raw = match.groups()
        else:
            if raw.count(":") != 1:
                raise CheckValidationError("check network endpoint is invalid")
            host, port_raw = raw.rsplit(":", 1)
        try:
            address = ipaddress.ip_address(host)
            port = int(port_raw)
        except (ValueError, TypeError):
            raise CheckValidationError("check network endpoint must use a numeric address") from None
        if not 1 <= port <= 65535 or str(port) != port_raw:
            raise CheckValidationError("check network endpoint port is invalid")
        canonical_host = address.compressed
        canonical = (
            f"[{canonical_host}]:{port}"
            if address.version == 6
            else f"{canonical_host}:{port}"
        )
        if canonical != raw:
            raise CheckValidationError("check network endpoint is not canonical")
        output.append(canonical)
    if len(set(output)) != len(output):
        raise CheckValidationError("check network endpoints are duplicated")
    return tuple(output)


def _sbpl_quote(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _check_profile_text(
    *,
    integration_root: Path,
    runtime_root: Path,
    scratch_root: Path,
    cache_roots: Sequence[Path],
    executable: Path,
    runtime_read_paths: Sequence[Path],
    network_allowlist: Sequence[str],
) -> str:
    integration = Path(integration_root).absolute()
    runtime = Path(runtime_root).absolute()
    scratch = Path(scratch_root).absolute()
    executable_path = Path(executable).absolute()
    resolved_executable = executable_path.resolve(strict=True)
    dependencies = tuple(Path(path).resolve(strict=True) for path in runtime_read_paths)
    caches = tuple(Path(path).absolute() for path in cache_roots)
    endpoints = parse_network_allowlist(network_allowlist)
    rules = [
        "(version 1)",
        "(deny default)",
        "(deny network*)",
        "(deny mach-lookup)",
        "(deny signal)",
        "(deny process-info*)",
        "(deny process-fork)",
        "(allow sysctl-read)",
        '(allow file-read* (literal "/"))',
        '(allow file-read* (literal "/dev/null"))',
        '(allow file-read* (literal "/dev/urandom"))',
        '(allow file-write* (literal "/dev/null"))',
        f"(allow process-exec (literal {_sbpl_quote(executable_path)}))",
    ]
    if resolved_executable != executable_path:
        rules.append(
            f"(allow process-exec (literal {_sbpl_quote(resolved_executable)}))"
        )
    read_roots = (
        integration,
        runtime,
        scratch,
        *dependencies,
        *(path for path in _SYSTEM_READ_ROOTS if path.exists()),
    )
    for path in read_roots:
        operation = "literal" if path.is_file() else "subpath"
        rules.append(f"(allow file-read* ({operation} {_sbpl_quote(path)}))")
    rules.append(f"(allow file-read* (literal {_sbpl_quote(executable_path)}))")
    if resolved_executable != executable_path:
        rules.append(
            f"(allow file-read* (literal {_sbpl_quote(resolved_executable)}))"
        )
    rules.extend((
        f"(allow file-write* (subpath {_sbpl_quote(runtime)}))",
        f"(allow file-write* (subpath {_sbpl_quote(scratch)}))",
    ))
    for cache in caches:
        operation = "literal" if cache.is_file() else "subpath"
        rules.append(f"(allow file-write* ({operation} {_sbpl_quote(cache)}))")
    for endpoint in endpoints:
        rules.append(
            f"(allow network-outbound (remote tcp {_sbpl_quote(endpoint)}))"
        )
    return "\n".join(rules) + "\n"


def _launch_check_process(
    *,
    executable: Path,
    executable_sha256: str | None = None,
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    profile_path: Path,
    deadline: float | None = None,
) -> subprocess.Popen[bytes]:
    if executable_sha256 is not None:
        _require_pinned_regular_file(
            executable,
            executable_sha256,
            "check executable",
            deadline=deadline,
        )
    return subprocess.Popen(
        [
            "/usr/bin/sandbox-exec",
            "-f",
            str(profile_path),
            str(executable),
            *argv,
        ],
        cwd=str(cwd),
        env=dict(environment),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )


def _check_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin can transiently report EPERM for a just-terminated session
        # leader until waitpid reaps it.  Treat that as still present; the
        # bounded loop must either observe ESRCH or fail containment.
        return True
    return True


def _signal_check_group(process_group: int, value: int) -> None:
    try:
        os.killpg(process_group, value)
    except ProcessLookupError:
        return
    except OSError:
        raise CheckContainmentError(
            "check process group could not be signalled; evidence retained"
        ) from None


def _extinguish_check_group(
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
    reap_grace_seconds: float,
) -> None:
    process_group = int(process.pid)
    _signal_check_group(process_group, signal.SIGTERM)
    term_deadline = min(deadline, time.monotonic() + float(reap_grace_seconds))
    while time.monotonic() < term_deadline:
        process.poll()
        if not _check_group_exists(process_group):
            break
        time.sleep(0.005)
    process.poll()
    if _check_group_exists(process_group):
        _signal_check_group(process_group, signal.SIGKILL)
    while time.monotonic() < deadline:
        process.poll()
        if not _check_group_exists(process_group):
            break
        time.sleep(0.005)
    if _check_group_exists(process_group):
        raise CheckContainmentError(
            "check process extinction proof failed; evidence retained"
        )
    remaining = deadline - time.monotonic()
    if process.poll() is None:
        if remaining <= 0:
            raise CheckContainmentError(
                "check process could not be reaped; evidence retained"
            )
        try:
            process.wait(timeout=remaining)
        except (subprocess.TimeoutExpired, OSError):
            raise CheckContainmentError(
                "check process could not be reaped; evidence retained"
            ) from None


def _supervise_check_process(
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
    cancel_event: threading.Event | None,
    max_output_bytes: int,
    reap_grace_seconds: float,
    cleanup_deadline: float | None = None,
) -> CapturedCheckProcess:
    hard_deadline = (
        max(deadline, time.monotonic()) + float(reap_grace_seconds)
        if cleanup_deadline is None
        else float(cleanup_deadline)
    )
    if hard_deadline < deadline:
        raise CheckValidationError("check cleanup deadline precedes run deadline")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    read_error = threading.Event()
    finished = {"stdout": threading.Event(), "stderr": threading.Event()}

    def drain(name: str, stream: object) -> None:
        try:
            read = getattr(stream, "read1", None) or getattr(stream, "read")
            while True:
                chunk = read(64 * 1024)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                remaining = max(0, max_output_bytes + 1 - len(buffers[name]))
                if remaining:
                    buffers[name].extend(chunk[:remaining])
                if len(buffers[name]) > max_output_bytes or len(chunk) > remaining:
                    overflow.set()
                    return
        except BaseException:
            read_error.set()
        finally:
            finished[name].set()

    threads: list[threading.Thread] = []
    for name in ("stdout", "stderr"):
        stream = getattr(process, name, None)
        if stream is None:
            finished[name].set()
            continue
        thread = threading.Thread(target=drain, args=(name, stream), daemon=True)
        thread.start()
        threads.append(thread)
    terminal_error: str | None = None
    while True:
        if overflow.is_set():
            terminal_error = "check output limit exceeded"
            break
        if read_error.is_set():
            terminal_error = "check output capture failed"
            break
        if cancel_event is not None and cancel_event.is_set():
            terminal_error = "check cancelled"
            break
        if time.monotonic() >= deadline:
            terminal_error = "check timeout"
            break
        if process.poll() is not None:
            break
        time.sleep(0.005)
    _extinguish_check_group(
        process,
        deadline=hard_deadline,
        reap_grace_seconds=float(reap_grace_seconds),
    )
    for thread in threads:
        thread.join(
            timeout=max(0.0, min(1.0, hard_deadline - time.monotonic()))
        )
    if any(thread.is_alive() for thread in threads) or read_error.is_set():
        raise CheckContainmentError(
            "check output capture did not become extinct; evidence retained"
        )
    if terminal_error is not None:
        raise CheckExecutionError(terminal_error)
    returncode = process.returncode
    if returncode is None:
        raise CheckContainmentError("check process result is unavailable")
    return CapturedCheckProcess(
        returncode=int(returncode),
        stdout=bytes(buffers["stdout"]),
        stderr=bytes(buffers["stderr"]),
    )


def _build_check_receipt(
    *,
    integration_oid: str,
    command_id: str,
    command_digest: str,
    policy_digest: str,
    exit_code: int,
    stdout: bytes,
    stderr: bytes,
    pre_tree_digest: str,
    post_tree_digest: str,
) -> CheckReceipt:
    stdout_digest = hashlib.sha256(stdout).hexdigest()
    stderr_digest = hashlib.sha256(stderr).hexdigest()
    output_body = {
        "integration_oid": integration_oid,
        "command_digest": command_digest,
        "exit_code": exit_code,
        "stdout_sha256": stdout_digest,
        "stderr_sha256": stderr_digest,
        "stdout_size": len(stdout),
        "stderr_size": len(stderr),
    }
    output_framed = _domain_digest(
        b"hermes.bestplan.check-output.v1", output_body,
    )
    receipt_body = {
        **output_body,
        "command_id": command_id,
        "policy_digest": policy_digest,
        "output_framed_sha256": output_framed,
        "pre_tree_digest": pre_tree_digest,
        "post_tree_digest": post_tree_digest,
    }
    receipt_digest = _domain_digest(
        b"hermes.bestplan.check-receipt.v1", receipt_body,
    )
    return CheckReceipt(
        integration_oid=integration_oid,
        command_id=command_id,
        command_digest=command_digest,
        policy_digest=policy_digest,
        exit_code=exit_code,
        stdout_sha256=stdout_digest,
        stderr_sha256=stderr_digest,
        stdout_size=len(stdout),
        stderr_size=len(stderr),
        output_framed_sha256=output_framed,
        pre_tree_digest=pre_tree_digest,
        post_tree_digest=post_tree_digest,
        receipt_digest=receipt_digest,
    )


def _logical_path(value: str, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CheckValidationError(f"{label} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or value in {".", "./"} or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise CheckValidationError(f"{label} must be a non-root relative path")
    if any(
        unicodedata.normalize("NFC", part).casefold() == ".git"
        for part in path.parts
    ):
        raise CheckValidationError(f"{label} cannot address Git metadata")
    return path


def _check_control(
    *,
    deadline: float,
    cancel_event: threading.Event | None,
    label: str,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CheckExecutionError(f"{label} cancelled")
    _check_absolute_deadline(deadline, label)


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    return flags | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _descriptor_path(descriptor: int, *, label: str) -> Path:
    try:
        if sys.platform == "darwin":
            if fcntl is None:
                raise OSError("descriptor path lookup is unavailable")
            raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)
            value = bytes(raw).split(b"\0", 1)[0]
        elif sys.platform.startswith("linux"):
            value = os.fsencode(os.readlink(f"/proc/self/fd/{descriptor}"))
        else:
            raise OSError("descriptor path lookup is unavailable")
        if not value or not os.path.isabs(value):
            raise OSError("descriptor path lookup returned an invalid path")
        return Path(os.fsdecode(value))
    except (OSError, ValueError):
        raise CheckValidationError(f"{label} path identity is unavailable") from None


def _verified_directory_path(
    descriptor: int,
    *,
    identity: tuple[int, int, int, int] | None = None,
    label: str,
) -> Path:
    try:
        opened = os.fstat(descriptor)
        path = _descriptor_path(descriptor, label=label)
        visible = path.lstat()
    except OSError:
        raise CheckValidationError(f"{label} identity changed") from None
    opened_identity = (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_uid,
    )
    visible_identity = (
        visible.st_dev,
        visible.st_ino,
        visible.st_mode,
        visible.st_uid,
    )
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(visible.st_mode)
        or visible_identity != opened_identity
        or (identity is not None and opened_identity != identity)
    ):
        raise CheckValidationError(f"{label} identity changed")
    return path


def _open_relative_directory(
    root_fd: int,
    parts: Sequence[bytes],
    *,
    label: str,
) -> tuple[int, tuple[int, int, int, int]]:
    current_fd = os.dup(root_fd)
    try:
        for component in parts:
            if component in {b"", b".", b".."} or b"/" in component:
                raise CheckValidationError(f"{label} contains an unsafe component")
            before = os.stat(
                component,
                dir_fd=current_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise CheckProofStale(f"{label} contains a symlink or non-directory")
            next_fd = os.open(
                component,
                _directory_open_flags(),
                dir_fd=current_fd,
            )
            opened = os.fstat(next_fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (before.st_dev, before.st_ino)
            ):
                os.close(next_fd)
                raise CheckProofStale(f"{label} changed while opening")
            os.close(current_fd)
            current_fd = next_fd
        opened = os.fstat(current_fd)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
        )
        return current_fd, identity
    except BaseException:
        os.close(current_fd)
        raise


def _create_owned_directory(
    parent_fd: int,
    leaf: bytes,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
    label: str,
) -> tuple[int, Path, tuple[int, int, int, int]]:
    _check_control(deadline=deadline, cancel_event=cancel_event, label=label)
    if leaf in {b"", b".", b".."} or b"/" in leaf or b"\0" in leaf:
        raise CheckValidationError(f"{label} name is unsafe")
    try:
        os.mkdir(leaf, 0o700, dir_fd=parent_fd)
        before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(leaf, _directory_open_flags(), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
    except OSError:
        raise CheckValidationError(f"{label} could not be created safely") from None
    identity = (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_uid,
    )
    try:
        if (
            not stat.S_ISDIR(before.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or stat.S_IMODE(opened.st_mode) != 0o700
            or opened.st_uid != os.geteuid()
        ):
            raise CheckValidationError(f"{label} identity is unsafe")
        path = _verified_directory_path(
            descriptor,
            identity=identity,
            label=label,
        )
        return descriptor, path, identity
    except BaseException:
        os.close(descriptor)
        try:
            os.rmdir(leaf, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _write_owned_regular_file(
    parent_fd: int,
    parent_identity: tuple[int, int, int, int],
    leaf: bytes,
    data: bytes,
    *,
    mode: int,
    deadline: float,
    cancel_event: threading.Event | None,
    label: str,
) -> Path:
    _check_control(deadline=deadline, cancel_event=cancel_event, label=label)
    parent_path = _verified_directory_path(
        parent_fd,
        identity=parent_identity,
        label=label,
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(leaf, flags, mode, dir_fd=parent_fd)
        offset = 0
        while offset < len(data):
            _check_control(
                deadline=deadline,
                cancel_event=cancel_event,
                label=label,
            )
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise CheckValidationError(f"{label} write made no progress")
            offset += written
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        named = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or stat.S_IMODE(opened.st_mode) != mode
            or opened.st_size != len(data)
        ):
            raise CheckValidationError(f"{label} identity changed")
        if _verified_directory_path(
            parent_fd,
            identity=parent_identity,
            label=label,
        ) != parent_path:
            raise CheckValidationError(f"{label} parent path changed")
        return parent_path / os.fsdecode(leaf)
    except OSError:
        raise CheckValidationError(f"{label} could not be written safely") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _command_cwd(
    root: Path,
    logical_cwd: str,
    *,
    root_fd: int | None = None,
) -> Path:
    relative_parts: tuple[bytes, ...] = ()
    if logical_cwd == "integration":
        relative_parts = ()
    else:
        prefix = "integration/"
        if not logical_cwd.startswith(prefix):
            raise CheckValidationError("check logical cwd is outside the integration")
        relative = _logical_path(logical_cwd[len(prefix):], "check logical cwd")
        relative_parts = tuple(os.fsencode(part) for part in relative.parts)
    owned_root = False
    if root_fd is None:
        try:
            root_fd = os.open(root, _directory_open_flags())
        except OSError:
            raise CheckProofStale("check integration root is unavailable") from None
        owned_root = True
    cwd_fd: int | None = None
    try:
        cwd_fd, identity = _open_relative_directory(
            root_fd,
            relative_parts,
            label="check logical cwd",
        )
        return _verified_directory_path(
            cwd_fd,
            identity=identity,
            label="check logical cwd",
        )
    finally:
        if cwd_fd is not None:
            os.close(cwd_fd)
        if owned_root:
            os.close(root_fd)


def _remove_owned_tree_contents(
    directory_fd: int,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
    label: str,
) -> None:
    """Remove one owned tree without following links or escaping control."""

    counters = {"entries": 0, "path_bytes": 0}

    def walk(current_fd: int, *, depth: int, prefix_bytes: int) -> None:
        _check_control(
            deadline=deadline,
            cancel_event=cancel_event,
            label=label,
        )
        if depth > MAX_CHECK_CLEANUP_DEPTH:
            raise CheckValidationError(f"{label} exceeds the cleanup depth bound")
        before = os.fstat(current_fd)
        if not stat.S_ISDIR(before.st_mode):
            raise CheckValidationError(f"{label} root is not a directory")
        try:
            with os.scandir(current_fd) as iterator:
                for item in iterator:
                    _check_control(
                        deadline=deadline,
                        cancel_event=cancel_event,
                        label=label,
                    )
                    name = os.fsencode(item.name)
                    if name in {b"", b".", b".."} or b"/" in name:
                        raise CheckValidationError(
                            f"{label} contains an unsafe path"
                        )
                    counters["entries"] += 1
                    counters["path_bytes"] += prefix_bytes + len(name)
                    if (
                        counters["entries"] > MAX_CHECK_CLEANUP_ENTRIES
                        or counters["path_bytes"] > MAX_CHECK_CLEANUP_PATH_BYTES
                    ):
                        raise CheckValidationError(
                            f"{label} exceeds the cleanup metadata bound"
                        )
                    info = os.stat(
                        name,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        child_fd = os.open(
                            name,
                            _directory_open_flags(),
                            dir_fd=current_fd,
                        )
                        try:
                            opened = os.fstat(child_fd)
                            if (
                                not stat.S_ISDIR(opened.st_mode)
                                or (opened.st_dev, opened.st_ino)
                                != (info.st_dev, info.st_ino)
                            ):
                                raise CheckValidationError(
                                    f"{label} directory was substituted"
                                )
                            walk(
                                child_fd,
                                depth=depth + 1,
                                prefix_bytes=prefix_bytes + len(name) + 1,
                            )
                            _check_control(
                                deadline=deadline,
                                cancel_event=cancel_event,
                                label=label,
                            )
                            after = os.stat(
                                name,
                                dir_fd=current_fd,
                                follow_symlinks=False,
                            )
                            if (
                                not stat.S_ISDIR(after.st_mode)
                                or (after.st_dev, after.st_ino)
                                != (opened.st_dev, opened.st_ino)
                            ):
                                raise CheckValidationError(
                                    f"{label} directory changed during cleanup"
                                )
                        finally:
                            os.close(child_fd)
                        os.rmdir(name, dir_fd=current_fd)
                    elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                        os.unlink(name, dir_fd=current_fd)
                    else:
                        raise CheckValidationError(
                            f"{label} contains an unsupported file type"
                        )
        except OSError:
            raise CheckValidationError(f"{label} cleanup failed") from None
        after = os.fstat(current_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
        ):
            raise CheckValidationError(f"{label} root changed during cleanup")

    root_fd = os.dup(directory_fd)
    try:
        walk(root_fd, depth=0, prefix_bytes=0)
    finally:
        os.close(root_fd)


def _capture_owned_check_tree(
    descriptor: int,
    identity: tuple[int, int, int, int],
    *,
    deadline: float,
    label: str,
) -> CheckTree:
    path = _verified_directory_path(
        descriptor,
        identity=identity,
        label=label,
    )
    captured = _capture_check_tree(path, deadline=deadline)
    if _verified_directory_path(
        descriptor,
        identity=identity,
        label=label,
    ) != path:
        raise CheckValidationError(f"{label} path changed during capture")
    return captured


def _command_digest(command: BoundCommand) -> str:
    body = {
        "identifier": command.identifier,
        "executable": command.executable,
        "executable_sha256": command.executable_sha256,
        "argv": list(command.argv),
        "logical_cwd": command.logical_cwd,
        "env": [[key, value] for key, value in command.env],
        "inputs": [[item.path, item.sha256] for item in command.inputs],
        "cache": [[item.path, item.sha256] for item in command.cache],
        "timeout_seconds": command.timeout_seconds,
        "network_allowlist": list(command.network_allowlist),
    }
    return _domain_digest(b"hermes.bestplan.bound-command.v1", body)


def _cache_specs(commands: Sequence[BoundCommand]) -> tuple[PinnedInput, ...]:
    by_path: dict[str, PinnedInput] = {}
    aliases: dict[str, str] = {}
    for command in commands:
        for item in command.cache:
            _logical_path(item.path, "check cache path")
            alias = _path_alias(item.path.encode("utf-8"))
            prior_alias = aliases.get(alias)
            if prior_alias is not None and prior_alias != item.path:
                raise CheckValidationError("check cache paths contain an alias")
            aliases[alias] = item.path
            prior = by_path.get(item.path)
            if prior is not None and prior.sha256 != item.sha256:
                raise CheckValidationError("check cache pin differs between commands")
            by_path[item.path] = item
    ordered = tuple(sorted(by_path.values(), key=lambda item: item.path))
    for index, left in enumerate(ordered):
        left_raw = left.path.encode("utf-8")
        for right in ordered[index + 1:]:
            right_raw = right.path.encode("utf-8")
            if _under(left_raw, right_raw) or _under(right_raw, left_raw):
                raise CheckValidationError("check cache roots overlap")
    return ordered


def _raw_path_contains_git_metadata(path: bytes) -> bool:
    try:
        parts = path.decode("utf-8", "strict").split("/")
    except UnicodeError:
        raise CheckValidationError("check cache path is not valid UTF-8") from None
    return any(
        unicodedata.normalize("NFC", part).casefold() == ".git"
        for part in parts
    )


def _write_all(
    descriptor: int,
    data: bytes,
    *,
    deadline: float,
    cancel_event: threading.Event | None = None,
) -> None:
    offset = 0
    while offset < len(data):
        _check_control(
            deadline=deadline,
            cancel_event=cancel_event,
            label="check cache copy",
        )
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise CheckProofStale("check cache copy write failed")
        offset += written


def _materialize_captured_cache_tree(
    target: Path,
    tree: CheckTree,
    *,
    deadline: float,
    cancel_event: threading.Event | None = None,
) -> None:
    _check_control(
        deadline=deadline,
        cancel_event=cancel_event,
        label="check cache copy",
    )
    target.mkdir(mode=0o755)
    target_raw = os.fsencode(target)
    for record in tree.records:
        _check_control(
            deadline=deadline,
            cancel_event=cancel_event,
            label="check cache copy",
        )
        if record.kind == "symlink":
            raise CheckProofStale("check cache seed contains a symlink")
        if _raw_path_contains_git_metadata(record.path):
            raise CheckValidationError("check cache seed contains Git metadata")
        destination = os.path.join(target_raw, record.path)
        if record.kind == "directory":
            os.mkdir(destination, record.mode)
            continue
        if record.kind != "regular":
            raise CheckProofStale("check cache seed shape is unsupported")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination, flags, record.mode)
        try:
            _write_all(
                descriptor,
                record.data,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            os.fchmod(descriptor, record.mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _copy_cache_seed(
    seed_root: Path,
    target_root: Path,
    spec: PinnedInput,
    *,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
    target_root_fd: int | None = None,
) -> Path:
    absolute_deadline = (
        time.monotonic() + 20.0 if deadline is None else float(deadline)
    )
    _check_control(
        deadline=absolute_deadline,
        cancel_event=cancel_event,
        label="check cache",
    )
    target_identity: tuple[int, int, int, int] | None = None
    if target_root_fd is not None:
        target_info = os.fstat(target_root_fd)
        target_identity = (
            target_info.st_dev,
            target_info.st_ino,
            target_info.st_mode,
            target_info.st_uid,
        )
        rebound = _verified_directory_path(
            target_root_fd,
            identity=target_identity,
            label="check cache target root",
        )
        if rebound != target_root:
            raise CheckValidationError("check cache target root path changed")
    relative = _logical_path(spec.path, "check cache path")
    source = seed_root.joinpath(*relative.parts)
    target = target_root.joinpath(*relative.parts)
    try:
        source_info = source.lstat()
    except FileNotFoundError:
        if spec.sha256 != EMPTY_CACHE_SHA256:
            raise CheckProofStale("check cache seed is missing")
        target.mkdir(parents=True, mode=0o755)
        if target_root_fd is not None and _verified_directory_path(
            target_root_fd,
            identity=target_identity,
            label="check cache target root",
        ) != target_root:
            raise CheckValidationError("check cache target root path changed")
        return target
    except OSError:
        raise CheckProofStale("check cache seed is unavailable") from None

    source_tree: CheckTree | None = None
    source_bytes: bytes | None = None
    source_mode = stat.S_IMODE(source_info.st_mode)
    if stat.S_ISDIR(source_info.st_mode) and not stat.S_ISLNK(source_info.st_mode):
        source_tree = _capture_check_tree(source, deadline=absolute_deadline)
        _check_control(
            deadline=absolute_deadline,
            cancel_event=cancel_event,
            label="check cache",
        )
        if target_root_fd is not None and _verified_directory_path(
            target_root_fd,
            identity=target_identity,
            label="check cache target root",
        ) != target_root:
            raise CheckValidationError("check cache target root path changed")
        if any(item.kind == "symlink" for item in source_tree.records):
            raise CheckProofStale("check cache seed contains a symlink")
        actual_digest = (
            EMPTY_CACHE_SHA256 if not source_tree.records else source_tree.digest
        )
    elif stat.S_ISREG(source_info.st_mode) and not stat.S_ISLNK(source_info.st_mode):
        source_bytes = _stable_regular_bytes(
            source,
            deadline=absolute_deadline,
        )
        actual_digest = hashlib.sha256(source_bytes).hexdigest()
    else:
        raise CheckProofStale("check cache seed shape is unsupported")
    if actual_digest != spec.sha256:
        raise CheckProofStale("check cache seed digest changed")

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if source_tree is not None:
            _materialize_captured_cache_tree(
                target,
                source_tree,
                deadline=absolute_deadline,
                cancel_event=cancel_event,
            )
        else:
            assert source_bytes is not None
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(target, flags, source_mode)
            try:
                _write_all(
                    descriptor,
                    source_bytes,
                    deadline=absolute_deadline,
                    cancel_event=cancel_event,
                )
                os.fchmod(descriptor, source_mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if pinned_path_sha256(target, deadline=absolute_deadline) != spec.sha256:
            raise CheckProofStale("check cache copy digest differs")
        _check_control(
            deadline=absolute_deadline,
            cancel_event=cancel_event,
            label="check cache",
        )
        if target_root_fd is not None and _verified_directory_path(
            target_root_fd,
            identity=target_identity,
            label="check cache target root",
        ) != target_root:
            raise CheckValidationError("check cache target root path changed")
    except BaseException:
        # The surrounding owned attempt is cleaned by the same bounded,
        # descriptor-relative controller.  Do not start a fresh recursive
        # cleanup budget from this inner failure path.
        raise
    return target


def _validated_deadline(deadline: float) -> float:
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(float(deadline))
    ):
        raise CheckValidationError("check deadline is invalid")
    value = float(deadline)
    remaining = value - time.monotonic()
    if remaining <= 0:
        raise CheckExecutionError("check deadline expired")
    if remaining > MAX_CHECK_TIMEOUT_SECONDS:
        raise CheckValidationError("check deadline exceeds the host bound")
    return value


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        common = Path(os.path.commonpath((str(left), str(right))))
    except ValueError:
        raise CheckValidationError("check root identity is invalid") from None
    return common == left or common == right


def _prepare_checks_root(
    checks_root: str | Path,
    *,
    forbidden_roots: Sequence[Path],
) -> _PreparedChecksRoot:
    requested = Path(checks_root)
    if not requested.is_absolute():
        raise CheckValidationError("check root must be absolute")
    try:
        before = requested.lstat()
        resolved = requested.resolve(strict=True)
    except OSError:
        raise CheckValidationError("check root must already exist") from None
    if (
        resolved != requested
        or not stat.S_ISDIR(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o700
        or before.st_uid != os.geteuid()
    ):
        raise CheckValidationError("check root is not a private host-owned directory")
    resolved_forbidden: list[Path] = []
    for forbidden in forbidden_roots:
        try:
            resolved_forbidden.append(Path(forbidden).resolve(strict=True))
        except OSError:
            raise CheckValidationError("trusted check root is unavailable") from None
    if any(_paths_overlap(resolved, forbidden) for forbidden in resolved_forbidden):
        raise CheckValidationError("check root overlaps trusted repository state")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(requested, flags)
    except OSError:
        raise CheckValidationError("check root could not be opened safely") from None
    identity = (before.st_dev, before.st_ino, before.st_mode, before.st_uid)
    prepared = _PreparedChecksRoot(
        path=resolved,
        descriptor=descriptor,
        identity=identity,
    )
    try:
        prepared.verify()
    except BaseException:
        prepared.close()
        raise
    return prepared


def _create_owned_attempt(
    root: _PreparedChecksRoot,
    *,
    deadline: float,
) -> _OwnedCheckAttempt:
    for _attempt in range(32):
        _check_absolute_deadline(deadline, "check attempt")
        leaf = f"bestplan-check-{secrets.token_hex(12)}"
        descriptor = -1
        try:
            os.mkdir(leaf, 0o700, dir_fd=root.descriptor)
        except FileExistsError:
            continue
        try:
            root.verify()
            created = os.stat(
                leaf,
                dir_fd=root.descriptor,
                follow_symlinks=False,
            )
            descriptor = os.open(
                leaf,
                _directory_open_flags(),
                dir_fd=root.descriptor,
            )
            opened = os.fstat(descriptor)
            identity = (
                created.st_dev,
                created.st_ino,
                created.st_mode,
                created.st_uid,
            )
            if (
                not stat.S_ISDIR(created.st_mode)
                or stat.S_ISLNK(created.st_mode)
                or identity
                != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                    opened.st_uid,
                )
                or stat.S_IMODE(opened.st_mode) != 0o700
                or opened.st_uid != os.geteuid()
            ):
                raise CheckValidationError("check attempt root is unstable")
            attempt_path = _verified_directory_path(
                descriptor,
                identity=identity,
                label="check attempt",
            )
        except BaseException:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                os.rmdir(leaf, dir_fd=root.descriptor)
            except OSError:
                pass
            raise
        return _OwnedCheckAttempt(
            root=root,
            leaf=leaf,
            path=attempt_path,
            descriptor=descriptor,
            identity=identity,
        )
    raise CheckValidationError("check attempt identity could not be allocated")


def _assert_host_runtime(
    runtime: CheckHostRuntime,
    *,
    deadline: float | None = None,
) -> None:
    absolute_deadline = (
        time.monotonic() + 20.0 if deadline is None else float(deadline)
    )
    _check_absolute_deadline(absolute_deadline, "check runtime")
    _require_pinned_regular_file(
        runtime.sandbox_executable,
        runtime.sandbox_executable_sha256,
        "check sandbox executable",
        deadline=absolute_deadline,
    )
    _check_absolute_deadline(absolute_deadline, "check runtime")
    actual_controller = candidate_controller_artifact_sha256(
        runtime.controller_source,
        deadline=absolute_deadline,
    )
    if actual_controller != runtime.controller.artifact_sha256:
        raise CheckProofStale("check controller artifact changed")
    local_budget = (
        _new_artifact_budget(absolute_deadline)
        if runtime.controller_python_launcher is not None
        else None
    )
    for item in runtime.runtime_read_paths:
        _check_absolute_deadline(absolute_deadline, "check runtime")
        if local_budget is None:
            actual_digest = pinned_path_sha256(
                item.path, deadline=absolute_deadline,
            )
        else:
            try:
                identity = _stable_artifact_tree_identity(item.path, local_budget)
            except (OSError, RuntimeError, ValueError):
                raise CheckProofStale(
                    "check runtime dependency changed"
                ) from None
            actual_digest = identity.get("sha256")
        if actual_digest != item.sha256:
            raise CheckProofStale("check runtime dependency changed")


def _safe_environment(
    command: BoundCommand, *, runtime_root: Path, scratch_root: Path,
) -> dict[str, str]:
    fixed = {
        **_FIXED_ENVIRONMENT,
        "HOME": str(runtime_root),
        "TMPDIR": str(scratch_root),
    }
    for key, value in command.env:
        if key in fixed:
            raise CheckValidationError("check environment overrides a host-owned key")
        fixed[key] = value
    return fixed


def _tracked_paths(tree: CheckTree) -> tuple[bytes, ...]:
    return tuple(item.path for item in tree.records if item.kind != "directory")


def _assert_cache_disjoint_from_tracked(
    cache_paths: Sequence[bytes], tracked_paths: Sequence[bytes],
) -> None:
    for cache in cache_paths:
        for tracked in tracked_paths:
            if _under(tracked, cache) or _under(cache, tracked):
                raise CheckValidationError("check cache overlaps an immutable tracked path")


def _remove_command_cache_roots(
    integration_root: Path,
    specs: Sequence[PinnedInput],
    *,
    deadline: float,
    cancel_event: threading.Event | None = None,
    integration_root_fd: int | None = None,
) -> None:
    _check_control(
        deadline=deadline,
        cancel_event=cancel_event,
        label="check cache cleanup",
    )
    integration_identity: tuple[int, int, int, int] | None = None
    if integration_root_fd is None:
        try:
            integration_fd = os.open(integration_root, _directory_open_flags())
        except OSError:
            raise CheckMutationError(
                "check integration root became unavailable"
            ) from None
    else:
        integration_fd = os.dup(integration_root_fd)
        info = os.fstat(integration_fd)
        integration_identity = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
        )
        if _verified_directory_path(
            integration_fd,
            identity=integration_identity,
            label="check integration root",
        ) != integration_root:
            os.close(integration_fd)
            raise CheckMutationError("check integration root path changed")
    try:
        for spec in sorted(specs, key=lambda item: item.path, reverse=True):
            _check_control(
                deadline=deadline,
                cancel_event=cancel_event,
                label="check cache cleanup",
            )
            relative = _logical_path(spec.path, "check cache path")
            parts = tuple(os.fsencode(part) for part in relative.parts)
            parent_fd, _parent_identity = _open_relative_directory(
                integration_fd,
                parts[:-1],
                label="check cache cleanup parent",
            )
            try:
                try:
                    info = os.stat(
                        parts[-1],
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    target_fd = os.open(
                        parts[-1],
                        _directory_open_flags(),
                        dir_fd=parent_fd,
                    )
                    try:
                        opened = os.fstat(target_fd)
                        if (opened.st_dev, opened.st_ino) != (
                            info.st_dev,
                            info.st_ino,
                        ):
                            raise CheckMutationError(
                                "check cache root was substituted"
                            )
                        _remove_owned_tree_contents(
                            target_fd,
                            deadline=deadline,
                            cancel_event=cancel_event,
                            label="check cache cleanup",
                        )
                    finally:
                        os.close(target_fd)
                    _check_control(
                        deadline=deadline,
                        cancel_event=cancel_event,
                        label="check cache cleanup",
                    )
                    current = os.stat(
                        parts[-1],
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) != (
                        info.st_dev,
                        info.st_ino,
                    ):
                        raise CheckMutationError(
                            "check cache root changed during cleanup"
                        )
                    os.rmdir(parts[-1], dir_fd=parent_fd)
                elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    os.unlink(parts[-1], dir_fd=parent_fd)
                else:
                    raise CheckMutationError(
                        "check cache root has an unsupported file type"
                    )
            except OSError:
                raise CheckMutationError(
                    "check cache root became unavailable"
                ) from None
            finally:
                os.close(parent_fd)

            for depth in range(len(parts) - 1, 0, -1):
                _check_control(
                    deadline=deadline,
                    cancel_event=cancel_event,
                    label="check cache cleanup",
                )
                ancestor_parent, _identity = _open_relative_directory(
                    integration_fd,
                    parts[: depth - 1],
                    label="check cache cleanup parent",
                )
                try:
                    try:
                        os.rmdir(parts[depth - 1], dir_fd=ancestor_parent)
                    except OSError as error:
                        if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                            break
                        raise
                finally:
                    os.close(ancestor_parent)
        if integration_identity is not None and _verified_directory_path(
            integration_fd,
            identity=integration_identity,
            label="check integration root",
        ) != integration_root:
            raise CheckMutationError("check integration root path changed")
    finally:
        os.close(integration_fd)


def _logical_profile_sha256(
    profile: str,
    *,
    integration_root: Path,
    runtime_root: Path,
    scratch_root: Path,
    cache_roots: Sequence[Path],
) -> str:
    replacements: list[tuple[Path, str]] = []
    for cache in cache_roots:
        relative = cache.relative_to(integration_root).as_posix()
        replacements.append((cache, f"/__hermes_integration__/{relative}"))
    replacements.extend((
        (integration_root, "/__hermes_integration__"),
        (runtime_root, "/__hermes_runtime__"),
        (scratch_root, "/__hermes_scratch__"),
    ))
    normalized = profile
    for actual, logical in sorted(
        replacements,
        key=lambda item: len(str(item[0])),
        reverse=True,
    ):
        normalized = normalized.replace(
            _sbpl_quote(actual.absolute()),
            json.dumps(logical),
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _cleanup_owned_attempt(
    path: Path | _OwnedCheckAttempt,
    *,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    absolute_deadline = (
        time.monotonic() + 20.0 if deadline is None else float(deadline)
    )
    if isinstance(path, _OwnedCheckAttempt):
        try:
            _check_control(
                deadline=absolute_deadline,
                cancel_event=cancel_event,
                label="check cleanup",
            )
            path.verify()
            _remove_owned_tree_contents(
                path.descriptor,
                deadline=absolute_deadline,
                cancel_event=cancel_event,
                label="check cleanup",
            )
            _check_control(
                deadline=absolute_deadline,
                cancel_event=cancel_event,
                label="check cleanup",
            )
            current = os.stat(
                path.leaf,
                dir_fd=path.root.descriptor,
                follow_symlinks=False,
            )
            if (
                current.st_dev,
                current.st_ino,
                current.st_mode,
                current.st_uid,
            ) != path.identity:
                raise CheckValidationError("check attempt identity changed")
            os.rmdir(path.leaf, dir_fd=path.root.descriptor)
        finally:
            path.close()
        return
    _check_control(
        deadline=absolute_deadline,
        cancel_event=cancel_event,
        label="check cleanup",
    )
    candidate = Path(path)
    if not candidate.name.startswith("bestplan-check-"):
        raise CheckValidationError("check cleanup target is not owned")
    parent_fd = -1
    target_fd = -1
    try:
        parent_fd = os.open(candidate.parent, _directory_open_flags())
        target_fd = os.open(
            os.fsencode(candidate.name),
            _directory_open_flags(),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        if parent_fd >= 0:
            os.close(parent_fd)
        return
    except OSError:
        if target_fd >= 0:
            os.close(target_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        raise CheckValidationError("check cleanup target is unavailable") from None
    try:
        _remove_owned_tree_contents(
            target_fd,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
            label="check cleanup",
        )
        _check_control(
            deadline=absolute_deadline,
            cancel_event=cancel_event,
            label="check cleanup",
        )
        os.rmdir(os.fsencode(candidate.name), dir_fd=parent_fd)
    finally:
        os.close(target_fd)
        os.close(parent_fd)


def _validate_command_paths(command: BoundCommand) -> None:
    if command.logical_cwd != "integration":
        prefix = "integration/"
        if not command.logical_cwd.startswith(prefix):
            raise CheckValidationError("check logical cwd is outside the integration")
        _logical_path(
            command.logical_cwd[len(prefix):],
            "check logical cwd",
        )
    for item in command.inputs:
        _logical_path(item.path, "check input path")
    for item in command.cache:
        _logical_path(item.path, "check cache path")
    parse_network_allowlist(command.network_allowlist)


def _local_check_runtime_digest(
    runtime: CheckHostRuntime,
    commands: Sequence[BoundCommand],
    *,
    deadline: float,
) -> str:
    """Rebuild the approved local runtime identity from live pinned inputs."""

    launcher = runtime.controller_python_launcher
    pytest_module = runtime.pytest_module_path
    if launcher is None or pytest_module is None:
        raise CheckValidationError("local check runtime proof is required")
    if not commands:
        raise CheckValidationError("local check command set is empty")

    executable = Path(commands[0].executable)
    executable_sha256 = commands[0].executable_sha256
    if not executable.is_absolute() or any(
        Path(command.executable) != executable
        or command.executable_sha256 != executable_sha256
        for command in commands
    ):
        raise CheckValidationError(
            "local check commands must use one exact resolved executable"
        )
    for command in commands:
        environment = dict(command.env)
        bound_launcher = environment.get("__PYVENV_LAUNCHER__")
        if bound_launcher is not None and bound_launcher != str(launcher):
            raise CheckValidationError(
                "local check command launcher differs from the runtime proof"
            )
    if runtime.policy_version != CHECK_SANDBOX_POLICY_VERSION:
        raise CheckValidationError("local check runtime policy differs")

    try:
        resolved_executable = executable.resolve(strict=True)
        resolved_launcher = launcher.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CheckProofStale("local check runtime launcher changed") from None
    if resolved_executable != executable:
        raise CheckValidationError(
            "local check command executable must be a resolved path"
        )
    if resolved_launcher != executable:
        raise CheckProofStale("local check runtime launcher changed")

    budget = _new_artifact_budget(deadline)
    try:
        launcher_identity = _launcher_identity(launcher, executable, budget)
        runtime_identities = tuple(
            _stable_artifact_tree_identity(item.path, budget)
            for item in runtime.runtime_read_paths
        )
        sandbox_identity = _stable_artifact_tree_identity(
            runtime.sandbox_executable, budget,
        )
        module_info = pytest_module.lstat()
        resolved_module = pytest_module.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise CheckProofStale("local check runtime dependency changed") from None

    resolved_identity = launcher_identity.get("resolved_identity")
    if (
        not isinstance(resolved_identity, Mapping)
        or resolved_identity.get("sha256") != executable_sha256
    ):
        raise CheckProofStale("local check executable digest changed")
    if any(
        identity.get("sha256") != pin.sha256
        for pin, identity in zip(
            runtime.runtime_read_paths, runtime_identities, strict=True,
        )
    ):
        raise CheckProofStale("local check runtime dependency changed")
    if sandbox_identity.get("sha256") != runtime.sandbox_executable_sha256:
        raise CheckProofStale("local check sandbox digest changed")
    if (
        resolved_module != pytest_module
        or not stat.S_ISREG(module_info.st_mode)
        or stat.S_ISLNK(module_info.st_mode)
    ):
        raise CheckValidationError("local pytest module proof is invalid")
    if not any(
        pytest_module == Path(identity["path"])
        or Path(identity["path"]) in pytest_module.parents
        for identity in runtime_identities
    ):
        raise CheckValidationError(
            "local pytest module is outside the pinned runtime"
        )

    body = {
        "schema": "hermes.bestplan.local-check-runtime.v1",
        "launcher": launcher_identity,
        "runtime_read_paths": list(runtime_identities),
        "sandbox": sandbox_identity,
        "policy_version": runtime.policy_version,
        "pytest_module_path": str(pytest_module),
    }
    return hashlib.sha256(
        b"hermes.bestplan.local-check-runtime.v1\0"
        + canonical_json(body).encode("utf-8")
    ).hexdigest()


def _read_current_target_oid(
    snapshot: object,
    integration: object,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> str | None:
    from agent.bestplan_promotion import _read_ref

    return _read_ref(
        snapshot.repo,
        integration.target_ref,
        deadline=deadline,
        cancel_event=cancel_event,
    )


def _validated_contract_commands(
    *,
    snapshot: object,
    integration: object,
    contract: Mapping[str, object],
    commands: Sequence[BoundCommand],
    runtime: CheckHostRuntime,
    deadline: float,
) -> tuple[tuple[BoundCommand, ...], str]:
    from agent.bestplan_contract import ContractValidationError
    from agent.bestplan_promotion import _validate_task6_contract

    try:
        validated = _validate_task6_contract(contract)
    except (ContractValidationError, KeyError, TypeError, ValueError):
        raise CheckValidationError("check promotion contract is invalid") from None
    digest = validated.contract_digest
    required = validated.commands
    repository = validated.repository
    controller = validated.controller
    if integration.contract_digest != digest:
        raise CheckProofStale("check integration contract digest differs")
    snapshot_digest = source_snapshot_digest(snapshot)
    if (
        validated.source["snapshot_digest"] != snapshot_digest
        or integration.source_snapshot_digest != snapshot_digest
    ):
        raise CheckProofStale("check source snapshot digest differs")
    if not repository.matches(snapshot.repo):
        raise CheckValidationError("check contract repository identity differs")
    source = validated.source
    if source["base_oid"] != snapshot.head_oid:
        raise CheckProofStale("check contract source base differs")
    if (
        validated.check_runtime_digest is None
        and source["local_main_oid"] != snapshot.head_oid
    ):
        raise CheckProofStale("check contract source base differs")
    if source["tree_oid"] != snapshot.tree_oid:
        raise CheckProofStale("check contract source tree differs")
    if source["source_digest"] != snapshot.fingerprint:
        raise CheckProofStale("check contract source digest differs")
    if source["protected_digest"] != snapshot.protected_manifest.digest:
        raise CheckProofStale("check contract protected digest differs")
    if source["local_ref"] != integration.target_ref:
        raise CheckValidationError("check integration target ref differs from contract")
    if controller != runtime.controller:
        raise CheckValidationError("check controller differs from contract")
    identifiers = tuple(item.identifier for item in required)
    if len(set(identifiers)) != len(identifiers):
        raise CheckValidationError("check command identifier is duplicated")
    if any(item.timeout_seconds > MAX_CHECK_TIMEOUT_SECONDS for item in required):
        raise CheckValidationError("check command timeout exceeds the host bound")
    for command in required:
        _validate_command_paths(command)
    supplied = tuple(commands)
    if supplied != required:
        raise CheckValidationError("check command set differs from contract")
    if validated.check_runtime_digest is None:
        if (
            runtime.controller_python_launcher is not None
            or runtime.pytest_module_path is not None
        ):
            raise CheckValidationError(
                "legacy check contract cannot use local runtime proof"
            )
    else:
        actual_runtime_digest = _local_check_runtime_digest(
            runtime, required, deadline=deadline,
        )
        if actual_runtime_digest != validated.check_runtime_digest:
            raise CheckProofStale("local check runtime digest differs")
    return required, digest


def run_integration_checks(
    *,
    snapshot: object,
    integration: object,
    contract: Mapping[str, object],
    commands: Sequence[BoundCommand],
    runtime: CheckHostRuntime,
    checks_root: str | Path,
    deadline: float,
    cancel_event: threading.Event | None = None,
) -> CheckSetReceipt:
    """Run the exact enrollment-bound checks against one frozen commit."""

    from agent.bestplan_promotion import (
        FrozenIntegration,
        _materialize_integration_tree_at_owned_parent,
    )
    from agent.bestplan_source import SourceSnapshot

    if not isinstance(snapshot, SourceSnapshot) or not isinstance(integration, FrozenIntegration):
        raise CheckValidationError("check integration input is invalid")
    if not isinstance(runtime, CheckHostRuntime):
        raise CheckValidationError("check host runtime is invalid")
    if cancel_event is not None and not isinstance(cancel_event, threading.Event):
        raise CheckValidationError("check cancellation control is invalid")
    if cancel_event is not None and cancel_event.is_set():
        raise CheckExecutionError("check cancelled")
    absolute_deadline = _validated_deadline(deadline)
    ordered_commands = tuple(commands)
    if not ordered_commands or any(
        not isinstance(item, BoundCommand) for item in ordered_commands
    ):
        raise CheckValidationError("check command set is invalid")
    ordered_commands, exact_contract_digest = _validated_contract_commands(
        snapshot=snapshot,
        integration=integration,
        contract=contract,
        commands=ordered_commands,
        runtime=runtime,
        deadline=absolute_deadline,
    )
    if _read_current_target_oid(
        snapshot,
        integration,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    ) != integration.target_oid:
        raise CheckProofStale("check integration target ref changed")
    if sys.platform != "darwin" or not runtime.sandbox_executable.is_file():
        raise CheckExecutionError("check sandbox backend is unavailable")
    _assert_host_runtime(runtime, deadline=absolute_deadline)
    root: _PreparedChecksRoot | None = None
    attempt: _OwnedCheckAttempt | None = None
    integration_fd: int | None = None
    runtime_parent_fd: int | None = None
    scratch_parent_fd: int | None = None
    control_fd: int | None = None
    command_directory_fds: list[int] = []
    retain = False
    try:
        root = _prepare_checks_root(
            checks_root,
            forbidden_roots=(
                Path(snapshot.repo.worktree),
                Path(snapshot.repo.git_dir),
                Path(snapshot.repo.common_dir),
                runtime.controller_source,
                runtime.cache_seed_root,
                *(item.path for item in runtime.runtime_read_paths),
            ),
        )
        attempt = _create_owned_attempt(root, deadline=absolute_deadline)
        attempt.verify()
        runtime_parent_fd, runtime_parent, _runtime_identity = (
            _create_owned_directory(
                attempt.descriptor,
                b"runtime",
                deadline=absolute_deadline,
                cancel_event=cancel_event,
                label="check runtime root",
            )
        )
        scratch_parent_fd, scratch_parent, _scratch_identity = (
            _create_owned_directory(
                attempt.descriptor,
                b"scratch",
                deadline=absolute_deadline,
                cancel_event=cancel_event,
                label="check scratch root",
            )
        )
        control_fd, _control_root, control_identity = _create_owned_directory(
            attempt.descriptor,
            b"control",
            deadline=absolute_deadline,
            cancel_event=cancel_event,
            label="check control root",
        )
        attempt.verify()
        _materialize_integration_tree_at_owned_parent(
            snapshot=snapshot,
            integration=integration,
            parent_fd=attempt.descriptor,
            parent_identity=attempt.identity,
            destination_leaf=b"integration",
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        attempt.verify()
        integration_fd, integration_identity = _open_relative_directory(
            attempt.descriptor,
            (b"integration",),
            label="check integration root",
        )
        integration_root = _verified_directory_path(
            integration_fd,
            identity=integration_identity,
            label="check integration root",
        )
        immutable_tree = _capture_owned_check_tree(
            integration_fd,
            integration_identity,
            deadline=absolute_deadline,
            label="check integration capture",
        )
        tracked = _tracked_paths(immutable_tree)
        cache_specs = _cache_specs(ordered_commands)
        cache_raw = tuple(item.path.encode("utf-8") for item in cache_specs)
        _assert_cache_disjoint_from_tracked(cache_raw, tracked)
        receipts: list[CheckReceipt] = []
        for index, command in enumerate(ordered_commands):
            if cancel_event is not None and cancel_event.is_set():
                raise CheckExecutionError("check cancelled")
            _check_absolute_deadline(absolute_deadline)
            assert runtime_parent_fd is not None
            assert scratch_parent_fd is not None
            runtime_fd, runtime_root, _runtime_command_identity = (
                _create_owned_directory(
                    runtime_parent_fd,
                    f"{index:04d}".encode("ascii"),
                    deadline=absolute_deadline,
                    cancel_event=cancel_event,
                    label="check command runtime",
                )
            )
            command_directory_fds.append(runtime_fd)
            scratch_fd, scratch_root, _scratch_command_identity = (
                _create_owned_directory(
                    scratch_parent_fd,
                    f"{index:04d}".encode("ascii"),
                    deadline=absolute_deadline,
                    cancel_event=cancel_event,
                    label="check command scratch",
                )
            )
            command_directory_fds.append(scratch_fd)
            command_cache_specs = _cache_specs((command,))
            command_cache_raw = tuple(
                item.path.encode("utf-8") for item in command_cache_specs
            )
            cache_roots = tuple(
                _copy_cache_seed(
                    runtime.cache_seed_root,
                    integration_root,
                    item,
                    deadline=absolute_deadline,
                    cancel_event=cancel_event,
                    target_root_fd=integration_fd,
                )
                for item in command_cache_specs
            )
            executable = Path(command.executable)
            if not executable.is_absolute():
                raise CheckValidationError("check executable must be absolute")
            _require_pinned_regular_file(
                executable,
                command.executable_sha256,
                "check executable",
                deadline=absolute_deadline,
            )
            cwd = _command_cwd(
                integration_root,
                command.logical_cwd,
                root_fd=integration_fd,
            )
            for item in command.inputs:
                relative = _logical_path(item.path, "check input path")
                _require_pinned_regular_file(
                    integration_root.joinpath(*relative.parts),
                    item.sha256,
                    "check input",
                    deadline=absolute_deadline,
                )
            environment = _safe_environment(
                command, runtime_root=runtime_root, scratch_root=scratch_root,
            )
            endpoints = parse_network_allowlist(command.network_allowlist)
            profile = _check_profile_text(
                integration_root=integration_root,
                runtime_root=runtime_root,
                scratch_root=scratch_root,
                cache_roots=cache_roots,
                executable=executable,
                runtime_read_paths=tuple(item.path for item in runtime.runtime_read_paths),
                network_allowlist=endpoints,
            )
            assert control_fd is not None
            profile_path = _write_owned_regular_file(
                control_fd,
                control_identity,
                f"{len(receipts):04d}.sb".encode("ascii"),
                profile.encode("utf-8"),
                mode=0o600,
                deadline=absolute_deadline,
                cancel_event=cancel_event,
                label="check sandbox profile",
            )
            policy_digest = _domain_digest(
                b"hermes.bestplan.check-policy.v1",
                {
                    "version": runtime.policy_version,
                    "profile_template_sha256": _logical_profile_sha256(
                        profile,
                        integration_root=integration_root,
                        runtime_root=runtime_root,
                        scratch_root=scratch_root,
                        cache_roots=cache_roots,
                    ),
                    "command_digest": _command_digest(command),
                    "sandbox_executable_sha256": (
                        runtime.sandbox_executable_sha256
                    ),
                    "controller_artifact_sha256": (
                        runtime.controller.artifact_sha256
                    ),
                    "runtime_pins": [
                        [str(item.path), item.sha256]
                        for item in runtime.runtime_read_paths
                    ],
                },
            )
            pre_tree = _capture_owned_check_tree(
                integration_fd,
                integration_identity,
                deadline=absolute_deadline,
                label="check preflight tree",
            )
            run_deadline = min(
                absolute_deadline - (2 * float(runtime.reap_grace_seconds)),
                time.monotonic() + command.timeout_seconds,
            )
            if run_deadline <= time.monotonic():
                raise CheckExecutionError(
                    "check deadline has no process cleanup reserve"
                )
            process = _launch_check_process(
                executable=executable,
                executable_sha256=command.executable_sha256,
                argv=command.argv,
                cwd=cwd,
                environment=environment,
                profile_path=profile_path,
                deadline=absolute_deadline,
            )
            try:
                captured = _supervise_check_process(
                    process,
                    deadline=run_deadline,
                    cancel_event=cancel_event,
                    max_output_bytes=runtime.max_output_bytes,
                    reap_grace_seconds=runtime.reap_grace_seconds,
                    cleanup_deadline=absolute_deadline,
                )
            except CheckContainmentError:
                retain = True
                raise
            post_tree = _capture_owned_check_tree(
                integration_fd,
                integration_identity,
                deadline=absolute_deadline,
                label="check postflight tree",
            )
            _validate_overlay_mutations(
                baseline=immutable_tree,
                current=post_tree,
                tracked_paths=tracked,
                cache_paths=command_cache_raw,
            )
            _require_pinned_regular_file(
                executable,
                command.executable_sha256,
                "check executable",
                deadline=absolute_deadline,
            )
            _assert_host_runtime(runtime, deadline=absolute_deadline)
            receipt = _build_check_receipt(
                integration_oid=integration.integration_oid,
                command_id=command.identifier,
                command_digest=_command_digest(command),
                policy_digest=policy_digest,
                exit_code=captured.returncode,
                stdout=captured.stdout,
                stderr=captured.stderr,
                pre_tree_digest=pre_tree.digest,
                post_tree_digest=post_tree.digest,
            )
            if captured.returncode != 0:
                raise CheckExecutionError("enrollment-bound check returned nonzero")
            receipts.append(receipt)
            _remove_command_cache_roots(
                integration_root,
                command_cache_specs,
                deadline=absolute_deadline,
                cancel_event=cancel_event,
                integration_root_fd=integration_fd,
            )
            restored = _capture_owned_check_tree(
                integration_fd,
                integration_identity,
                deadline=absolute_deadline,
                label="check restored tree",
            )
            if restored != immutable_tree:
                raise CheckMutationError(
                    "check cache cleanup did not restore the immutable integration"
                )
        set_body = {
            "integration_oid": integration.integration_oid,
            "contract_digest": exact_contract_digest,
            "ordered_receipts": [item.receipt_digest for item in receipts],
        }
        return CheckSetReceipt(
            integration_oid=integration.integration_oid,
            contract_digest=exact_contract_digest,
            ordered_receipts=tuple(receipts),
            receipt_digest=_domain_digest(
                b"hermes.bestplan.check-set.v1", set_body,
            ),
        )
    finally:
        try:
            for descriptor in reversed(command_directory_fds):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            for descriptor in (
                integration_fd,
                runtime_parent_fd,
                scratch_parent_fd,
                control_fd,
            ):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if attempt is not None and not retain:
                _cleanup_owned_attempt(
                    attempt,
                    deadline=absolute_deadline,
                    cancel_event=cancel_event,
                )
            elif attempt is not None:
                attempt.close()
        finally:
            if root is not None:
                root.close()


__all__ = [
    "CHECK_SANDBOX_POLICY_VERSION",
    "EMPTY_CACHE_SHA256",
    "CheckContainmentError",
    "CheckError",
    "CheckExecutionError",
    "CheckHostRuntime",
    "CheckMutationError",
    "CheckProofStale",
    "CheckReceipt",
    "CheckSetReceipt",
    "CheckValidationError",
    "PinnedRuntimePath",
    "parse_network_allowlist",
    "pinned_path_sha256",
    "run_integration_checks",
]
