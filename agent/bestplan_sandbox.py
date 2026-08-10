"""OS-enforced filesystem sandbox for strict BestPlan workers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import MappingProxyType
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


CANDIDATE_SANDBOX_POLICY_VERSION = "bestplan-candidate-sandbox-v1"
CANDIDATE_BOOTSTRAP = (
    "import os,runpy,sys;"
    "controller=os.path.realpath(sys.argv[1]);"
    "worker=os.path.realpath(sys.argv[2]);"
    "runtime=[os.path.realpath(p) for p in sys.argv[3:]];"
    "sys.path[:]=[controller,*runtime];"
    "sys.argv[:]=[worker];"
    "runpy.run_path(worker,run_name='__main__')"
)
_MAX_ARTIFACT_ENTRIES = 250_000
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARTIFACT_PATH_BYTES = 64 * 1024 * 1024
_MAX_ARTIFACT_DEPTH = 128
_MAX_ARTIFACT_ROOTS = 32
_ARTIFACT_IDENTITY_SECONDS = 20.0
_EXACT_LEASE_STAGE_PREFIX = ".hermes-bestplan-stage-"
_CANDIDATE_ENVIRONMENT_KEYS = frozenset({
    "HOME",
    "HERMES_BESTPLAN_BROKER_FD",
    "HERMES_BESTPLAN_CHILD",
    "HERMES_HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONNOUSERSITE",
    "PYTHONSAFEPATH",
    "TERMINAL_CWD",
    "TMPDIR",
})
_CANDIDATE_SYSTEM_READ_ROOTS = (
    Path("/System/Library/Frameworks"),
    Path("/System/Library/PrivateFrameworks"),
    Path("/usr/lib"),
    Path("/usr/share/zoneinfo"),
)


class BestplanSandboxUnavailable(RuntimeError):
    """Raised when this host has no enforceable BestPlan sandbox backend."""


@lru_cache(maxsize=1)
def _macos_sandbox_available() -> bool:
    executable = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not executable.is_file():
        return False
    for _attempt in range(3):
        try:
            probe = subprocess.run(
                [str(executable), "-p", "(version 1)(allow default)", "/usr/bin/true"],
                capture_output=True, timeout=5,
            )
            if probe.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    return False


def _canonical_lease_paths(workspace: Path, allowed_paths: Iterable[str]) -> list[Path]:
    root = workspace.expanduser().resolve()
    leases: list[Path] = []
    for raw in allowed_paths:
        candidate = Path(str(raw or "").strip())
        if not str(candidate) or candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"unsafe BestPlan write lease: {raw!r}")
        resolved = (root / candidate).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"BestPlan write lease escapes workspace: {raw!r}")
        leases.append(resolved)
    return sorted(set(leases), key=lambda item: str(item))


def sandbox_backend_identity(
    *, workspace: str | Path, allowed_paths: Iterable[str], read_only: bool,
) -> dict[str, str]:
    """Return the non-secret backend/policy identity shown at approval time."""
    root = Path(workspace).expanduser().resolve()
    leases = _canonical_lease_paths(root, allowed_paths)
    if _macos_sandbox_available():
        backend = "macos-sandbox-exec-v1"
    else:
        # V1 deliberately has no advisory fallback.  A future Linux backend
        # must be added here and in ``create_bestplan_sandbox_launch`` together.
        backend = "unavailable"
    canonical = {
        "backend": backend,
        "read_only": bool(read_only),
        "write_leases": [] if read_only else [str(path.relative_to(root)) for path in leases],
        "policy": "deny-file-write-by-default;allow-normalized-leases;symlink-target-enforced",
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"backend": backend, "policy_digest": digest}


def _sbpl_quote(path: Path) -> str:
    return json.dumps(str(path), ensure_ascii=False)


def _exact_lease_stage_path(path: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(str(path))).hexdigest()[:24]
    return path.parent / f"{_EXACT_LEASE_STAGE_PREFIX}{digest}"


@dataclass
class _ArtifactBudget:
    deadline: float
    entries: int = 0
    path_bytes: int = 0
    content_bytes: int = 0

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ValueError("candidate runtime artifact identity deadline expired")


def _new_artifact_budget(deadline: float | None = None) -> _ArtifactBudget:
    bounded = time.monotonic() + _ARTIFACT_IDENTITY_SECONDS
    if deadline is not None:
        bounded = min(bounded, float(deadline))
    return _ArtifactBudget(bounded)


def _stable_regular_file_digest(
    path: Path, budget: _ArtifactBudget | None = None,
) -> tuple[str, int, int, int]:
    budget = _new_artifact_budget() if budget is None else budget
    budget.check()
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"candidate runtime artifact is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        digest = hashlib.sha256()
        total = 0
        while True:
            budget.check()
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            budget.content_bytes += len(chunk)
            if budget.content_bytes > _MAX_ARTIFACT_BYTES:
                raise ValueError("candidate runtime artifact exceeds the bounded limit")
            digest.update(chunk)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = path.stat(follow_symlinks=False)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if not (
        identity(before)
        == identity(opened)
        == identity(after_open)
        == identity(after_path)
    ):
        raise ValueError("candidate runtime artifact changed during identity capture")
    return digest.hexdigest(), before.st_dev, before.st_ino, before.st_mode


def _artifact_tree_identity(
    path: Path, budget: _ArtifactBudget | None = None,
) -> dict[str, object]:
    budget = _new_artifact_budget() if budget is None else budget
    budget.check()
    root = path.expanduser().resolve(strict=True)
    if root.is_file():
        digest, device, inode, mode = _stable_regular_file_digest(root, budget)
        return {
            "path": str(root),
            "kind": "file",
            "sha256": digest,
            "device": device,
            "inode": inode,
            "mode": stat.S_IMODE(mode),
        }
    if not root.is_dir():
        raise ValueError(f"candidate runtime artifact root is unsupported: {root}")

    digest = hashlib.sha256(b"bestplan-candidate-artifact-tree-v1\0")
    entries = 0
    total_bytes = 0
    stack = [(root, b"", 0)]
    while stack:
        budget.check()
        directory, prefix, depth = stack.pop()
        if depth > _MAX_ARTIFACT_DEPTH:
            raise ValueError("candidate runtime artifact exceeds the depth limit")
        before = directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise ValueError("candidate runtime artifact directory changed")
        children: list[tuple[bytes, str]] = []
        with os.scandir(directory) as iterator:
            for child in iterator:
                budget.check()
                name = os.fsencode(child.name)
                entries += 1
                budget.entries += 1
                budget.path_bytes += len(prefix) + (1 if prefix else 0) + len(name)
                if budget.entries > _MAX_ARTIFACT_ENTRIES:
                    raise ValueError(
                        "candidate runtime artifact exceeds the entry limit"
                    )
                if budget.path_bytes > _MAX_ARTIFACT_PATH_BYTES:
                    raise ValueError(
                        "candidate runtime artifact exceeds the path limit"
                    )
                children.append((name, child.path))
        children.sort(key=lambda item: item[0])
        for name, child_name in children:
            relative = name if not prefix else prefix + b"/" + name
            child_path = Path(child_name)
            info = child_path.stat(follow_symlinks=False)
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(str(stat.S_IMODE(info.st_mode)).encode("ascii") + b"\0")
            if stat.S_ISDIR(info.st_mode):
                digest.update(b"directory\0")
                stack.append((child_path, relative, depth + 1))
            elif stat.S_ISREG(info.st_mode):
                content, _device, _inode, _mode = _stable_regular_file_digest(
                    child_path, budget,
                )
                total_bytes += info.st_size
                digest.update(b"regular\0" + content.encode("ascii"))
            elif stat.S_ISLNK(info.st_mode):
                target = os.fsencode(os.readlink(child_path))
                digest.update(b"symlink\0" + target)
            else:
                raise ValueError(
                    "candidate runtime artifact contains a special file"
                )
        after = directory.stat(follow_symlinks=False)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("candidate runtime artifact changed during identity capture")
    root_info = root.stat(follow_symlinks=False)
    return {
        "path": str(root),
        "kind": "directory",
        "sha256": digest.hexdigest(),
        "device": root_info.st_dev,
        "inode": root_info.st_ino,
        "mode": stat.S_IMODE(root_info.st_mode),
        "entries": entries,
        "bytes": total_bytes,
    }


def _stable_artifact_tree_identity(
    path: Path, budget: _ArtifactBudget,
) -> dict[str, object]:
    first = _artifact_tree_identity(path, budget)
    second = _artifact_tree_identity(path, budget)
    if first != second:
        raise ValueError("candidate runtime artifact did not remain stable")
    return first


def _lexical_absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _launcher_identity(
    launcher: Path, resolved: Path, budget: _ArtifactBudget,
) -> dict[str, object]:
    budget.check()
    before = launcher.lstat()
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(launcher)
        kind = "symlink"
    elif stat.S_ISREG(before.st_mode):
        target = None
        kind = "regular"
    else:
        raise ValueError("candidate controller launcher is not a file or symlink")
    resolved_identity = _stable_artifact_tree_identity(resolved, budget)
    after = launcher.lstat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("candidate controller launcher changed during capture")
    if kind == "symlink" and os.readlink(launcher) != target:
        raise ValueError("candidate controller launcher target changed")
    return {
        "path": str(launcher),
        "kind": kind,
        "target": target,
        "resolved": str(resolved),
        "resolved_identity": resolved_identity,
    }


def pinned_candidate_runtime_paths(
    controller_python: str | Path,
) -> tuple[Path, ...]:
    """Derive exact stdlib/dynload/site roots from a pinned launcher layout."""

    launcher = _lexical_absolute_path(controller_python)
    resolved = launcher.resolve(strict=True)
    match = re.search(r"python(?P<version>\d+\.\d+)$", resolved.name)
    if match is None:
        raise ValueError("candidate controller interpreter version is ambiguous")
    version = match.group("version")
    installation = resolved.parent.parent
    stdlib = installation / "lib" / f"python{version}"
    dynload = stdlib / "lib-dynload"
    launcher_root = launcher.parent.parent
    site_candidates = (
        launcher_root / "lib" / f"python{version}" / "site-packages",
        launcher_root / "lib" / f"python{version}" / "dist-packages",
        installation / "lib" / f"python{version}" / "site-packages",
        installation / "lib" / f"python{version}" / "dist-packages",
    )
    site = next((path for path in site_candidates if path.is_dir()), None)
    if not stdlib.is_dir() or not dynload.is_dir() or site is None:
        raise ValueError("candidate controller runtime layout is incomplete")
    native_candidates = (
        installation / "lib" / f"libpython{version}.dylib",
        installation / "lib" / f"libpython{version}.so",
        installation / "lib" / f"libpython{version}.so.1.0",
    )
    native_library = next((path for path in native_candidates if path.is_file()), None)
    return tuple(
        path.resolve(strict=True)
        for path in (
            stdlib,
            dynload,
            site,
            *((native_library,) if native_library is not None else ()),
        )
    )


def candidate_controller_artifact_sha256(controller_source: str | Path) -> str:
    """Return the stable content digest bound by ``ControllerIdentity``."""

    identity = _stable_artifact_tree_identity(
        Path(controller_source), _new_artifact_budget(),
    )
    return str(identity["sha256"])


def _candidate_policy_inputs(
    *,
    workspace: str | Path,
    allowed_paths: Iterable[str],
    read_only: bool,
    runtime_dir: str | Path,
    scratch_dir: str | Path,
    control_dir: str | Path,
    controller_source: str | Path,
    controller_python: str | Path,
    runtime_read_paths: Iterable[str | Path],
    enabled_toolsets: Iterable[str],
    expected_controller: object,
) -> tuple[dict[str, object], dict[str, object]]:
    from agent.bestplan_contract import ControllerIdentity

    if not isinstance(expected_controller, ControllerIdentity):
        raise ValueError("candidate sandbox requires an approved ControllerIdentity")
    root = Path(workspace).expanduser().resolve(strict=True)
    runtime = Path(runtime_dir).expanduser().resolve(strict=True)
    scratch = Path(scratch_dir).expanduser().resolve(strict=True)
    control = Path(control_dir).expanduser().resolve(strict=False)
    controller = Path(controller_source).expanduser().resolve(strict=True)
    interpreter_launcher = _lexical_absolute_path(controller_python)
    interpreter = interpreter_launcher.resolve(strict=True)
    dependencies = tuple(dict.fromkeys(
        Path(path).expanduser().resolve(strict=True)
        for path in runtime_read_paths
    ))
    if not root.is_dir() or not runtime.is_dir() or not scratch.is_dir():
        raise ValueError("candidate source/runtime/scratch roots must be directories")
    if not controller.is_dir():
        raise ValueError("candidate controller source must be a directory")
    if not interpreter.is_file():
        raise ValueError("candidate controller interpreter must be a file")
    expected_dependencies = pinned_candidate_runtime_paths(interpreter_launcher)
    if dependencies != expected_dependencies:
        raise ValueError(
            "candidate runtime paths differ from the pinned interpreter layout"
        )
    if len({root, runtime, scratch, control}) != 4:
        raise ValueError("candidate source/runtime/scratch/control roots must be distinct")
    if runtime.parent != root.parent or scratch.parent != root.parent or control.parent != root.parent:
        raise ValueError("candidate source/runtime/scratch/control roots must be siblings")
    candidate_roots = (root, runtime, scratch, control)
    broad_roots = {
        Path("/"),
        Path("/Users"),
        Path("/Library"),
        Path("/private"),
        Path("/private/tmp"),
        Path("/tmp").resolve(strict=False),
        Path("/usr"),
        Path("/opt"),
        Path.home().resolve(strict=False),
    }
    interpreter_install = interpreter.parent.parent
    launcher_root = interpreter_launcher.parent.parent
    for dependency in dependencies:
        if not (
            dependency == interpreter_install
            or interpreter_install in dependency.parents
            or dependency == controller
            or controller in dependency.parents
            or dependency == launcher_root
            or launcher_root in dependency.parents
        ):
            raise ValueError(
                "candidate runtime dependency is outside the pinned runtime artifacts"
            )
    for trusted in (controller, interpreter_launcher, interpreter, *dependencies):
        if trusted in broad_roots:
            raise ValueError("trusted candidate runtime path is too broad")
        if any(
            trusted == candidate
            or candidate in trusted.parents
            or trusted in candidate.parents
            for candidate in candidate_roots
        ):
            raise ValueError("trusted candidate runtime paths must be outside candidate roots")

    toolsets = tuple(str(item) for item in enabled_toolsets)
    allowed_toolsets = {("file",), ("read_only_files",)}
    if toolsets not in allowed_toolsets:
        raise ValueError("candidate sandbox requires one process-free file toolset")
    if read_only and toolsets != ("read_only_files",):
        raise ValueError("read-only candidate sandbox requires read_only_files")
    if not read_only and toolsets != ("file",):
        raise ValueError("writable candidate sandbox requires file")
    leases = [] if read_only else _canonical_lease_paths(root, allowed_paths)
    if 2 + len(dependencies) > _MAX_ARTIFACT_ROOTS:
        raise ValueError("candidate runtime artifact root count exceeds the limit")
    budget = _new_artifact_budget()
    controller_identity = _stable_artifact_tree_identity(controller, budget)
    if controller_identity.get("sha256") != expected_controller.artifact_sha256:
        raise ValueError("candidate controller artifact differs from approval")
    interpreter_identity = _launcher_identity(
        interpreter_launcher, interpreter, budget,
    )
    worker = (controller / "agent" / "bestplan_worker.py").resolve(strict=True)
    if controller != worker and controller not in worker.parents:
        raise ValueError("candidate worker path escapes the approved controller")
    worker_info = worker.stat(follow_symlinks=False)
    if not stat.S_ISREG(worker_info.st_mode):
        raise ValueError("candidate worker path is not a regular file")
    bootstrap_sha256 = hashlib.sha256(CANDIDATE_BOOTSTRAP.encode("utf-8")).hexdigest()
    worker_command = (
        str(interpreter),
        "-I",
        "-S",
        "-B",
        "-c",
        CANDIDATE_BOOTSTRAP,
        str(controller),
        str(worker),
        *(str(path) for path in dependencies),
    )
    artifact_identity = {
        "controller": controller_identity,
        "interpreter": interpreter_identity,
        "runtime_dependencies": [
            _stable_artifact_tree_identity(path, budget) for path in dependencies
        ],
    }
    canonical = {
        "policy_version": CANDIDATE_SANDBOX_POLICY_VERSION,
        "backend": (
            "macos-sandbox-exec-candidate-v1"
            if _macos_sandbox_available()
            else "unavailable"
        ),
        "workspace": str(root),
        "runtime_dir": str(runtime),
        "scratch_dir": str(scratch),
        "control_dir": str(control),
        "controller_source": str(controller),
        "controller_python_launcher": str(interpreter_launcher),
        "controller_python_resolved": str(interpreter),
        "controller_identity": {
            "repository_id": expected_controller.repository_id,
            "controller_id": expected_controller.controller_id,
            "release_oid": expected_controller.release_oid,
            "artifact_sha256": expected_controller.artifact_sha256,
        },
        "runtime_read_paths": [str(path) for path in dependencies],
        "system_read_roots": [
            str(path) for path in _CANDIDATE_SYSTEM_READ_ROOTS if path.exists()
        ],
        "write_leases": [str(path) for path in leases],
        "read_only": bool(read_only),
        "enabled_toolsets": list(toolsets),
        "broker_channel": "inherited-af-unix-stream-v1",
        "process_policy": "no-fork",
        "bootstrap_sha256": bootstrap_sha256,
        "worker_relative_path": "agent/bestplan_worker.py",
        "worker_command": list(worker_command),
        "artifacts": artifact_identity,
    }
    resolved = {
        "root": root,
        "runtime": runtime,
        "scratch": scratch,
        "control": control,
        "controller": controller,
        "interpreter_launcher": interpreter_launcher,
        "interpreter": interpreter,
        "dependencies": dependencies,
        "leases": tuple(leases),
        "toolsets": toolsets,
        "bootstrap_sha256": bootstrap_sha256,
        "worker_command": worker_command,
        "artifact_identity": artifact_identity,
    }
    return canonical, resolved


def _candidate_profile_text(resolved: dict[str, object]) -> str:
    root = resolved["root"]
    runtime = resolved["runtime"]
    scratch = resolved["scratch"]
    controller = resolved["controller"]
    interpreter_launcher = resolved["interpreter_launcher"]
    interpreter = resolved["interpreter"]
    dependencies = resolved["dependencies"]
    leases = resolved["leases"]
    assert isinstance(root, Path)
    assert isinstance(runtime, Path)
    assert isinstance(scratch, Path)
    assert isinstance(controller, Path)
    assert isinstance(interpreter_launcher, Path)
    assert isinstance(interpreter, Path)
    assert isinstance(dependencies, tuple)
    assert isinstance(leases, tuple)
    read_roots = [
        root,
        runtime,
        scratch,
        controller,
        *dependencies,
        *(path for path in _CANDIDATE_SYSTEM_READ_ROOTS if path.exists()),
    ]
    rules = [
        "(version 1)",
        "(deny default)",
        "(deny network*)",
        "(deny mach-lookup)",
        "(deny signal)",
        "(deny process-info*)",
        "(deny process-fork)",
        f"(allow process-exec (literal {_sbpl_quote(interpreter_launcher)}))",
        f"(allow process-exec (literal {_sbpl_quote(interpreter)}))",
        "(allow sysctl-read)",
        "(allow file-read* (literal \"/\"))",
        "(allow file-read* (literal \"/dev/null\"))",
        "(allow file-read* (literal \"/dev/urandom\"))",
        "(allow file-write* (literal \"/dev/null\"))",
    ]
    for path in read_roots:
        operation = "literal" if path.is_file() else "subpath"
        rules.append(f"(allow file-read* ({operation} {_sbpl_quote(path)}))")
    rules.extend((
        f"(allow file-read* (literal {_sbpl_quote(interpreter_launcher)}))",
        f"(allow file-read* (literal {_sbpl_quote(interpreter)}))",
        f"(allow file-write* (subpath {_sbpl_quote(runtime)}))",
        f"(allow file-write* (subpath {_sbpl_quote(scratch)}))",
    ))
    for lease in leases:
        if lease.is_dir():
            rules.append(f"(allow file-write* (subpath {_sbpl_quote(lease)}))")
        else:
            rules.append(f"(allow file-write* (literal {_sbpl_quote(lease)}))")
            rules.append(
                f"(allow file-write* (literal "
                f"{_sbpl_quote(_exact_lease_stage_path(lease))}))"
            )
    return "\n".join(rules) + "\n"


def _bind_profile_identity(
    canonical: dict[str, object], resolved: dict[str, object],
) -> tuple[dict[str, object], str, str]:
    profile_text = _candidate_profile_text(resolved)
    profile_sha256 = hashlib.sha256(profile_text.encode("utf-8")).hexdigest()
    bound = dict(canonical)
    bound["profile_sha256"] = profile_sha256
    policy_digest = hashlib.sha256(
        json.dumps(
            bound, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return bound, profile_text, policy_digest


def candidate_sandbox_backend_identity(
    *,
    workspace: str | Path,
    allowed_paths: Iterable[str],
    read_only: bool,
    runtime_dir: str | Path,
    scratch_dir: str | Path,
    control_dir: str | Path,
    controller_source: str | Path,
    controller_python: str | Path,
    runtime_read_paths: Iterable[str | Path],
    enabled_toolsets: Iterable[str],
    expected_controller: object,
) -> dict[str, str]:
    """Return the complete non-secret identity of the no-fork candidate policy."""

    canonical, _resolved = _candidate_policy_inputs(
        workspace=workspace,
        allowed_paths=allowed_paths,
        read_only=read_only,
        runtime_dir=runtime_dir,
        scratch_dir=scratch_dir,
        control_dir=control_dir,
        controller_source=controller_source,
        controller_python=controller_python,
        runtime_read_paths=runtime_read_paths,
        enabled_toolsets=enabled_toolsets,
        expected_controller=expected_controller,
    )
    bound, _profile_text, digest = _bind_profile_identity(canonical, _resolved)
    return {
        "backend": str(bound["backend"]),
        "policy_digest": digest,
        "profile_sha256": str(bound["profile_sha256"]),
    }


@dataclass
class BestplanSandboxLaunch:
    workspace: Path
    runtime_dir: Path
    profile_path: Path
    backend: str
    policy_digest: str

    def command(self, argv: Sequence[str]) -> list[str]:
        if self.backend != "macos-sandbox-exec-v1":
            raise BestplanSandboxUnavailable(f"unsupported BestPlan sandbox backend: {self.backend}")
        return ["/usr/bin/sandbox-exec", "-f", str(self.profile_path), *map(str, argv)]

    def popen(self, argv: Sequence[str], **kwargs) -> subprocess.Popen:
        env = dict(kwargs.pop("env", None) or os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.Popen(
            self.command(argv), cwd=str(self.workspace), env=env, **kwargs,
        )

    def run(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        env = dict(kwargs.pop("env", None) or os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            self.command(argv), cwd=str(self.workspace), env=env,
            capture_output=True, text=True, **kwargs,
        )

    def close(self) -> None:
        try:
            self.profile_path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "BestplanSandboxLaunch":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def create_bestplan_sandbox_launch(
    *,
    workspace: str | Path,
    allowed_paths: Iterable[str],
    read_only: bool,
    runtime_dir: str | Path,
) -> BestplanSandboxLaunch:
    """Create a fail-closed launch descriptor for a strict child process."""
    root = Path(workspace).expanduser().resolve()
    runtime = Path(runtime_dir).expanduser().resolve()
    if root != runtime and root not in runtime.parents:
        raise ValueError("BestPlan runtime directory must be inside the isolated worktree")
    runtime.mkdir(parents=True, exist_ok=True)
    identity = sandbox_backend_identity(
        workspace=root, allowed_paths=allowed_paths, read_only=read_only,
    )
    if identity["backend"] != "macos-sandbox-exec-v1":
        raise BestplanSandboxUnavailable(
            "strict BestPlan execution requires an enforceable OS sandbox; "
            "macOS sandbox-exec is the only V1 backend"
        )
    leases = [] if read_only else _canonical_lease_paths(root, allowed_paths)
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* (subpath {_sbpl_quote(runtime)}))",
    ]
    for lease in leases:
        rules.append(f"(allow file-write* (subpath {_sbpl_quote(lease)}))")
    fd, raw_profile = tempfile.mkstemp(prefix="policy-", suffix=".sb", dir=runtime)
    profile = Path(raw_profile)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(rules) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return BestplanSandboxLaunch(
        workspace=root,
        runtime_dir=runtime,
        profile_path=profile,
        backend=identity["backend"],
        policy_digest=identity["policy_digest"],
    )


@dataclass(frozen=True)
class BestplanCandidateSandboxLaunch:
    """Opinionated launch descriptor for one credential-free candidate worker."""

    workspace: Path
    runtime_dir: Path
    scratch_dir: Path
    control_dir: Path
    controller_source: Path
    controller_python: Path
    controller_python_resolved: Path
    runtime_read_paths: tuple[Path, ...]
    profile_path: Path
    backend: str
    policy_digest: str
    profile_sha256: str
    worker_environment_items: tuple[tuple[str, str], ...]
    broker_fd: int
    artifact_identity_json: str
    worker_command: tuple[str, ...]
    bootstrap_sha256: str
    _launch_lock: threading.Lock
    _launched: bool = False
    _broker_fd_closed: bool = False

    @property
    def worker_environment(self):
        return MappingProxyType(dict(self.worker_environment_items))

    def command(self) -> list[str]:
        if self.backend != "macos-sandbox-exec-candidate-v1":
            raise BestplanSandboxUnavailable(
                f"unsupported BestPlan candidate sandbox backend: {self.backend}"
            )
        return [
            "/usr/bin/sandbox-exec",
            "-f",
            str(self.profile_path),
            *self.worker_command,
        ]

    def verify_identity(self, *, deadline: float | None = None) -> None:
        budget = _new_artifact_budget(deadline)
        profile_digest, _device, _inode, _mode = _stable_regular_file_digest(
            self.profile_path, budget,
        )
        if profile_digest != self.profile_sha256:
            raise BestplanSandboxUnavailable(
                "candidate sandbox profile identity changed"
            )
        current = {
            "controller": _stable_artifact_tree_identity(
                self.controller_source, budget,
            ),
            "interpreter": _launcher_identity(
                self.controller_python, self.controller_python_resolved, budget,
            ),
            "runtime_dependencies": [
                _stable_artifact_tree_identity(path, budget)
                for path in self.runtime_read_paths
            ],
        }
        if _canonical_identity_json(current) != self.artifact_identity_json:
            raise BestplanSandboxUnavailable(
                "candidate controller/runtime artifact identity changed"
            )

    def launch_worker(self) -> subprocess.Popen:
        """Launch with a fixed cwd/env/fd/session contract and no caller overrides."""

        with self._launch_lock:
            if self._launched:
                raise RuntimeError("candidate sandbox worker was already launched")
            object.__setattr__(self, "_launched", True)
            self.verify_identity()
            environment = _validate_candidate_environment(
                dict(self.worker_environment_items),
                workspace=self.workspace,
                runtime=self.runtime_dir,
                scratch=self.scratch_dir,
                broker_fd=self.broker_fd,
            )
            _validate_candidate_broker_fd(self.broker_fd)
            try:
                process = subprocess.Popen(
                    self.command(),
                    cwd=str(self.workspace),
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    close_fds=True,
                    pass_fds=(self.broker_fd,),
                    start_new_session=True,
                )
            finally:
                self._close_broker_fd()
            return process

    def _close_broker_fd(self) -> None:
        if self._broker_fd_closed:
            return
        try:
            os.close(self.broker_fd)
        except OSError:
            pass
        object.__setattr__(self, "_broker_fd_closed", True)

    def close(self) -> None:
        self._close_broker_fd()
        try:
            self.profile_path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "BestplanCandidateSandboxLaunch":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def _validate_candidate_environment(
    environment: object,
    *,
    workspace: Path,
    runtime: Path,
    scratch: Path,
    broker_fd: int,
) -> dict[str, str]:
    if not isinstance(environment, dict) or set(environment) != _CANDIDATE_ENVIRONMENT_KEYS:
        raise ValueError("candidate worker environment keys do not match the allowlist")
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or "\x00" in value
        or "\n" in value
        for key, value in environment.items()
    ):
        raise ValueError("candidate worker environment contains an invalid value")
    expected = {
        "HOME": str(runtime),
        "HERMES_BESTPLAN_BROKER_FD": str(broker_fd),
        "HERMES_BESTPLAN_CHILD": "1",
        "HERMES_HOME": str(runtime),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "TERMINAL_CWD": str(workspace),
        "TMPDIR": str(scratch),
    }
    if environment != expected:
        raise ValueError("candidate worker environment values do not match the policy")
    return dict(environment)


def _validate_candidate_broker_fd(broker_fd: int) -> None:
    if isinstance(broker_fd, bool) or not isinstance(broker_fd, int) or broker_fd < 0:
        raise ValueError("candidate broker_fd must be an inherited descriptor")
    try:
        probe_socket = socket.socket(fileno=os.dup(broker_fd))
        try:
            if probe_socket.family != socket.AF_UNIX or (
                probe_socket.type & socket.SOCK_STREAM
            ) != socket.SOCK_STREAM:
                raise ValueError("candidate broker_fd must be an AF_UNIX stream socket")
            probe_socket.getpeername()
        finally:
            probe_socket.close()
    except OSError as exc:
        raise ValueError("candidate broker_fd is not a connected socket") from exc


def _canonical_identity_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def create_bestplan_candidate_sandbox_launch(
    *,
    workspace: str | Path,
    allowed_paths: Iterable[str],
    read_only: bool,
    runtime_dir: str | Path,
    scratch_dir: str | Path,
    control_dir: str | Path,
    controller_source: str | Path,
    controller_python: str | Path,
    runtime_read_paths: Iterable[str | Path],
    enabled_toolsets: Iterable[str],
    expected_controller: object,
    worker_environment: dict[str, str] | None = None,
    broker_fd: int = -1,
) -> BestplanCandidateSandboxLaunch:
    """Create a host-owned, default-deny, no-fork candidate launch."""

    canonical, resolved = _candidate_policy_inputs(
        workspace=workspace,
        allowed_paths=allowed_paths,
        read_only=read_only,
        runtime_dir=runtime_dir,
        scratch_dir=scratch_dir,
        control_dir=control_dir,
        controller_source=controller_source,
        controller_python=controller_python,
        runtime_read_paths=runtime_read_paths,
        enabled_toolsets=enabled_toolsets,
        expected_controller=expected_controller,
    )
    bound, profile_text, identity_digest = _bind_profile_identity(
        canonical, resolved,
    )
    backend = str(bound["backend"])
    if backend != "macos-sandbox-exec-candidate-v1":
        raise BestplanSandboxUnavailable(
            "strict candidate execution requires the macOS sandbox backend"
        )
    _validate_candidate_broker_fd(broker_fd)

    root = resolved["root"]
    runtime = resolved["runtime"]
    scratch = resolved["scratch"]
    control = resolved["control"]
    controller = resolved["controller"]
    interpreter_launcher = resolved["interpreter_launcher"]
    interpreter = resolved["interpreter"]
    dependencies = resolved["dependencies"]
    leases = resolved["leases"]
    assert isinstance(root, Path)
    assert isinstance(runtime, Path)
    assert isinstance(scratch, Path)
    assert isinstance(control, Path)
    assert isinstance(controller, Path)
    assert isinstance(interpreter_launcher, Path)
    assert isinstance(interpreter, Path)
    assert isinstance(dependencies, tuple)
    assert isinstance(leases, tuple)
    caller_environment = _validate_candidate_environment(
        worker_environment,
        workspace=root,
        runtime=runtime,
        scratch=scratch,
        broker_fd=broker_fd,
    )
    control.mkdir(mode=0o700, parents=True, exist_ok=True)
    if control.is_symlink() or not control.is_dir():
        raise ValueError("candidate control root must be a host-owned directory")
    os.chmod(control, 0o700)

    descriptor, raw_profile = tempfile.mkstemp(
        prefix="candidate-policy-", suffix=".sb", dir=control,
    )
    profile = Path(raw_profile)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(profile_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(profile, 0o600)
        profile_sha256, _device, _inode, _mode = _stable_regular_file_digest(
            profile,
        )
        if profile_sha256 != bound["profile_sha256"]:
            raise BestplanSandboxUnavailable(
                "candidate sandbox profile does not match its policy identity"
            )
    except BaseException:
        try:
            profile.unlink()
        except FileNotFoundError:
            pass
        raise
    try:
        owned_broker_fd = os.dup(broker_fd)
        os.set_inheritable(owned_broker_fd, False)
        environment = dict(caller_environment)
        environment["HERMES_BESTPLAN_BROKER_FD"] = str(owned_broker_fd)
        return BestplanCandidateSandboxLaunch(
        workspace=root,
        runtime_dir=runtime,
        scratch_dir=scratch,
        control_dir=control,
        controller_source=controller,
        controller_python=interpreter_launcher,
        controller_python_resolved=interpreter,
        runtime_read_paths=dependencies,
        profile_path=profile,
        backend=backend,
        policy_digest=identity_digest,
        profile_sha256=profile_sha256,
        worker_environment_items=tuple(sorted(environment.items())),
        broker_fd=owned_broker_fd,
        artifact_identity_json=_canonical_identity_json(resolved["artifact_identity"]),
        worker_command=resolved["worker_command"],
        bootstrap_sha256=resolved["bootstrap_sha256"],
            _launch_lock=threading.Lock(),
        )
    except BaseException:
        try:
            profile.unlink()
        except FileNotFoundError:
            pass
        raise
