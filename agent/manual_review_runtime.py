"""Concrete scoped repair host for standalone manual ``/review`` jobs.

The review engine owns the live target and the durable state machine.  This
module adapts that immutable target to the existing BestPlan candidate,
integration, and check machinery.  It never grants a broader write lease than
the exact manual objective paths.
"""

from __future__ import annotations

from contextlib import nullcontext
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Mapping, Sequence


logger = logging.getLogger(__name__)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MAX_MANUAL_FILE_BYTES = 8 * 1024 * 1024
_HOST_OPERATION_SECONDS = 3_600.0


class _ManualRuntimeWaiting(RuntimeError):
    """The exact target cannot safely continue in this host invocation."""

    def __init__(self, message: str, *, recovery_paths: Sequence[str] = ()):
        super().__init__(message)
        self.recovery_paths = tuple(recovery_paths)


class _ManualRuntimeRequiresAuthority(RuntimeError):
    """No valid candidate authority exists for the required write set."""

    def __init__(self, paths: Sequence[str] = ()):
        super().__init__("manual repair requires different write authority")
        self.paths = tuple(paths)


def _check_cancelled(cancel_event: object | None) -> None:
    if cancel_event is None:
        return
    if not isinstance(cancel_event, threading.Event):
        raise _ManualRuntimeWaiting("manual repair cancellation control is invalid")
    if cancel_event.is_set():
        raise _ManualRuntimeWaiting("manual repair was cancelled")


@dataclass(frozen=True)
class _LiveEntry:
    exists: bool
    data: bytes = b""
    mode: int = 0
    identity: tuple[int, int] | None = None


def _relative_path(value: object, *, field: str = "path") -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _ManualRuntimeWaiting(f"manual repair {field} is invalid")
    if "\\" in value or value.endswith("/"):
        raise _ManualRuntimeWaiting(f"manual repair {field} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or ".." in path.parts:
        raise _ManualRuntimeWaiting(f"manual repair {field} is invalid")
    if any(part in {"", "."} for part in path.parts):
        raise _ManualRuntimeWaiting(f"manual repair {field} is invalid")
    return value


def _ordered_paths(value: object, *, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not value
    ):
        raise _ManualRuntimeWaiting(f"manual repair {field} is invalid")
    paths = tuple(_relative_path(item, field=field) for item in value)
    if len(set(paths)) != len(paths):
        raise _ManualRuntimeWaiting(f"manual repair {field} is ambiguous")
    return tuple(sorted(paths))


def _git(
    workspace: Path,
    *args: str,
    input_bytes: bytes | None = None,
    extra_environment: Mapping[str, str] | None = None,
    check: bool = True,
) -> bytes:
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    if extra_environment:
        environment.update(extra_environment)
    result = subprocess.run(
        ["/usr/bin/git", *args],
        cwd=workspace,
        env=environment,
        input=input_bytes,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise _ManualRuntimeWaiting("manual repair Git evidence is unavailable")
    if len(result.stdout) > 16 * 1024 * 1024:
        raise _ManualRuntimeWaiting("manual repair Git evidence is oversized")
    return result.stdout


def _tree_blob_mode(workspace: Path, tree_oid: str, path: str) -> int | None:
    output = _git(
        workspace,
        "ls-tree",
        "-z",
        "--full-tree",
        tree_oid,
        "--",
        path,
    )
    expected = path.encode("utf-8")
    for record in output.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        header, actual_path = record.split(b"\t", 1)
        if actual_path != expected:
            continue
        fields = header.split()
        if len(fields) != 3 or fields[1] != b"blob":
            raise _ManualRuntimeWaiting(
                "manual repair target contains an unsupported artifact"
            )
        try:
            mode = int(fields[0], 8)
        except ValueError as exc:
            raise _ManualRuntimeWaiting(
                "manual repair target contains invalid Git evidence"
            ) from exc
        if mode not in {0o100644, 0o100755}:
            raise _ManualRuntimeWaiting(
                "manual repair target contains an unsupported artifact"
            )
        return mode
    return None


def _workspace_prefix(workspace: Path, worktree: Path) -> str:
    try:
        relative = workspace.relative_to(worktree)
    except ValueError as exc:
        raise _ManualRuntimeWaiting(
            "manual repair workspace differs from the Git worktree"
        ) from exc
    return "" if relative == Path(".") else relative.as_posix()


def _rebase(path: str, prefix: str) -> str:
    return path if not prefix else f"{prefix}/{path}"


def _unbase(path: str, prefix: str) -> str:
    if not prefix:
        return _relative_path(path)
    marker = prefix + "/"
    if not path.startswith(marker):
        raise _ManualRuntimeRequiresAuthority((path,))
    return _relative_path(path[len(marker) :])


def _evidence_test_paths(agent: object, workspace: Path) -> tuple[str, ...]:
    try:
        from agent.verification_evidence import verification_status

        evidence_status = verification_status(
            session_id=str(getattr(agent, "session_id", "") or "default"),
            cwd=workspace,
        )
    except Exception:
        return ()
    evidence = (
        evidence_status.get("evidence")
        if isinstance(evidence_status, Mapping)
        else None
    )
    if not isinstance(evidence, Mapping):
        return ()
    values: list[str] = []
    for field in ("command", "canonical_command"):
        command = evidence.get(field)
        if not isinstance(command, str) or not command.strip():
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        for token in tokens:
            raw_path = token.split("::", 1)[0]
            if not raw_path.endswith(".py"):
                continue
            candidate = Path(raw_path).expanduser()
            if candidate.is_absolute():
                try:
                    raw_path = candidate.resolve(strict=False).relative_to(
                        workspace
                    ).as_posix()
                except (OSError, RuntimeError, ValueError):
                    continue
            else:
                raw_path = raw_path.removeprefix("./")
            try:
                normalized = _relative_path(raw_path, field="check path")
            except _ManualRuntimeWaiting:
                continue
            selector = token[len(token.split("::", 1)[0]) :]
            values.append(normalized + selector)
    return tuple(values)


def _candidate_test_paths(
    *,
    agent: object,
    workspace: Path,
    git_root: Path,
    allowed_paths: Sequence[str],
    prefix: str,
    tree_oid: str,
) -> tuple[str, ...]:
    candidates = list(_evidence_test_paths(agent, workspace))
    for value in allowed_paths:
        path = PurePosixPath(value)
        if path.suffix != ".py":
            continue
        if path.parts and path.parts[0] in {"test", "tests"}:
            candidates.append(value)
        test_name = (
            path.name if path.name.startswith("test_") else f"test_{path.name}"
        )
        parent_parts = path.parts[:-1]
        if parent_parts and parent_parts[0] in {"src", "lib"}:
            parent_parts = parent_parts[1:]
        candidates.extend(
            (
                str(PurePosixPath("tests", *parent_parts, test_name)),
                str(PurePosixPath("tests", test_name)),
            )
        )

    selected: list[str] = []
    seen: set[str] = set()
    for node in candidates:
        path_text, separator, selector = node.partition("::")
        try:
            path_text = _relative_path(path_text, field="check path")
        except _ManualRuntimeWaiting:
            continue
        root_path = _rebase(path_text, prefix)
        if _tree_blob_mode(git_root, tree_oid, root_path) is None:
            continue
        normalized = path_text + (separator + selector if separator else "")
        if normalized not in seen:
            seen.add(normalized)
            selected.append(normalized)
    if not selected:
        raise _ManualRuntimeWaiting(
            "manual repair has no exact pytest node for the selected objective"
        )
    return tuple(selected)


def _blocker_payload(blocker: object) -> dict[str, object]:
    locator = getattr(blocker, "locator", None)
    reproduction = getattr(blocker, "reproduction", None)
    return {
        "blast_radius": str(getattr(blocker, "blast_radius", "")),
        "fingerprint": str(getattr(blocker, "fingerprint", "")),
        "locator": {
            "end_line": getattr(locator, "end_line", None),
            "kind": str(getattr(locator, "kind", "")),
            "locator_id": str(getattr(locator, "locator_id", "")),
            "path": str(getattr(locator, "path", "")),
            "start_line": getattr(locator, "start_line", None),
        },
        "observed_failure": str(getattr(blocker, "observed_failure", "")),
        "reproduction": {
            "argv": [str(item) for item in getattr(reproduction, "argv", ())],
            "kind": str(getattr(reproduction, "kind", "")),
            "reason": str(getattr(reproduction, "reason", "")),
        },
        "severity": str(getattr(blocker, "severity", "")),
        "title": str(getattr(blocker, "title", "")),
        "trigger": str(getattr(blocker, "trigger", "")),
    }


def _validate_blocker_authority(
    blockers: object, allowed_paths: Sequence[str]
) -> tuple[object, ...]:
    if (
        not isinstance(blockers, Sequence)
        or isinstance(blockers, (str, bytes, bytearray))
        or not blockers
    ):
        raise _ManualRuntimeWaiting("manual repair blocker evidence is invalid")
    values = tuple(blockers)
    allowed = set(allowed_paths)
    outside: list[str] = []
    for blocker in values:
        locator = getattr(blocker, "locator", None)
        path = str(getattr(locator, "path", "") or "")
        if path and path not in allowed:
            outside.append(path)
    if outside:
        raise _ManualRuntimeRequiresAuthority(tuple(sorted(set(outside))))
    return values


def _build_plan(
    *,
    workspace: Path,
    task: str,
    allowed_paths: Sequence[str],
    expected_artifacts: Sequence[str],
    root_test_nodes: Sequence[str],
) -> object:
    from agent.execution_plan import compile_execution_plan

    task_text = task.strip() if isinstance(task, str) else ""
    if not task_text or "\x00" in task_text:
        task_text = "Repair the selected manual review objective."
    goal = "Repair all blocking findings in the selected manual objective."
    if task_text:
        goal += " User objective: " + task_text[:1500]
    return compile_execution_plan(
        {
            "version": 1,
            "mode": "direct",
            "risk": "high",
            "slices": [
                {
                    "id": "manual-objective",
                    "kind": "implement",
                    "goal": goal,
                    "depends_on": [],
                    "capability": "local_execution",
                    "workspace": str(workspace),
                    "allowed_paths": list(allowed_paths),
                    "read_only": False,
                    "expected_artifacts": list(expected_artifacts),
                    "acceptance": [
                        "pytest -q -- " + " ".join(root_test_nodes)
                    ],
                }
            ],
            "merge_policy": "apply only the exact manual objective lease",
            "stop_condition": "the exact checks and fresh two-lane review pass",
            "escalation_predicates": ["review_blocker"],
        }
    )


def _resolve_write_authority(
    *, agent: object, workspace: Path, goal: str
) -> tuple[Mapping[str, object], object]:
    from agent.bestplan_local import build_local_authority_bindings
    from tools.delegate_tool import resolve_bestplan_runtime_specs

    task = {
        "route": "code_worker",
        "goal": goal,
        "_bestplan_read_only": False,
        "_bestplan_workspace": str(workspace),
    }
    try:
        runtimes = resolve_bestplan_runtime_specs(
            [task], agent, execution_protocol=2
        )
        bindings = build_local_authority_bindings(runtimes)
    except Exception as exc:
        raise _ManualRuntimeRequiresAuthority() from exc
    if len(runtimes) != 1 or len(bindings) != 1:
        raise _ManualRuntimeRequiresAuthority()
    return runtimes[0], bindings[0].authority


def _safe_identifier(prefix: str, *values: object) -> str:
    payload = json.dumps(
        [str(value) for value in values],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _prior_integration(
    *,
    workspace: Path,
    snapshot: object,
    target: object,
    plan_id: str,
    approval_digest: str,
    contract_digest: str,
) -> object:
    from agent.bestplan_contract import source_snapshot_digest
    from agent.bestplan_promotion import FrozenIntegration, _receipt_digest

    target_ref_raw = getattr(snapshot, "head_ref", None)
    if target_ref_raw != b"refs/heads/main":
        raise _ManualRuntimeWaiting(
            "manual repair requires the admitted local main target"
        )
    target_oid = str(getattr(target, "base_oid", ""))
    tree_oid = str(getattr(target, "snapshot_tree_oid", ""))
    if _git(workspace, "rev-parse", "refs/heads/main^{commit}").strip().decode(
        "ascii"
    ) != target_oid:
        raise _ManualRuntimeWaiting("manual repair target changed")
    if not _OID_RE.fullmatch(tree_oid):
        raise _ManualRuntimeWaiting("manual repair snapshot tree is invalid")
    _git(workspace, "cat-file", "-e", f"{tree_oid}^{{tree}}")
    message = (
        "Hermes manual review snapshot\n\n"
        f"plan={plan_id}\n"
        f"target={getattr(target, 'target_digest', '')}\n"
    ).encode("utf-8")
    identity = {
        "GIT_AUTHOR_NAME": "Hermes Manual Review",
        "GIT_AUTHOR_EMAIL": "manual-review@localhost",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_NAME": "Hermes Manual Review",
        "GIT_COMMITTER_EMAIL": "manual-review@localhost",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    integration_oid = _git(
        workspace,
        "commit-tree",
        tree_oid,
        "-p",
        target_oid,
        input_bytes=message,
        extra_environment=identity,
    ).strip().decode("ascii")
    ref_name = (
        "refs/hermes-bestplan-integrations/"
        f"{plan_id}/manual-{str(getattr(target, 'target_digest', ''))[:24]}"
    )
    existing = _git(
        workspace, "rev-parse", "--verify", "--quiet", ref_name, check=False
    ).strip()
    if existing:
        if existing.decode("ascii") != integration_oid:
            raise _ManualRuntimeWaiting(
                "manual repair integration evidence conflicts"
            )
    else:
        _git(
            workspace,
            "update-ref",
            ref_name,
            integration_oid,
            "0" * len(target_oid),
        )
    value = FrozenIntegration(
        plan_id=plan_id,
        approval_digest=approval_digest,
        contract_digest=contract_digest,
        source_snapshot_digest=source_snapshot_digest(snapshot),
        target_ref="refs/heads/main",
        target_oid=target_oid,
        integration_oid=integration_oid,
        tree_oid=tree_oid,
        ref_name=ref_name,
        candidates=(),
        receipt_digest="",
    )
    return FrozenIntegration(
        **{**value.__dict__, "receipt_digest": _receipt_digest(value)}
    )


def _read_live_entry(parent_fd: int, name: str) -> _LiveEntry:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _LiveEntry(False)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise _ManualRuntimeWaiting(
            "manual repair live target is not a regular file"
        )
    if info.st_size > _MAX_MANUAL_FILE_BYTES:
        raise _ManualRuntimeWaiting("manual repair live target is oversized")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise _ManualRuntimeWaiting("manual repair live target changed")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, _MAX_MANUAL_FILE_BYTES + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_MANUAL_FILE_BYTES:
                raise _ManualRuntimeWaiting(
                    "manual repair live target is oversized"
                )
            chunks.append(chunk)
        if size != opened.st_size:
            raise _ManualRuntimeWaiting("manual repair live target changed")
        mode = 0o755 if opened.st_mode & stat.S_IXUSR else 0o644
        return _LiveEntry(
            True,
            b"".join(chunks),
            mode,
            (opened.st_dev, opened.st_ino),
        )
    finally:
        os.close(descriptor)


def _source_entry(parent_fd: int, name: str) -> _LiveEntry:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _LiveEntry(False)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise _ManualRuntimeWaiting(
            "manual repair candidate produced an unsupported artifact"
        )
    if info.st_size > _MAX_MANUAL_FILE_BYTES:
        raise _ManualRuntimeWaiting(
            "manual repair candidate artifact is oversized"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _ManualRuntimeWaiting(
            "manual repair candidate artifact changed"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        initial_identity = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if not stat.S_ISREG(opened.st_mode) or opened_identity != initial_identity:
            raise _ManualRuntimeWaiting(
                "manual repair candidate artifact changed"
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, _MAX_MANUAL_FILE_BYTES + 1),
            )
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_MANUAL_FILE_BYTES:
                raise _ManualRuntimeWaiting(
                    "manual repair candidate artifact is oversized"
                )
            chunks.append(chunk)
        final_opened = os.fstat(descriptor)
        final_identity = (
            final_opened.st_dev,
            final_opened.st_ino,
            final_opened.st_mode,
            final_opened.st_size,
            final_opened.st_mtime_ns,
            final_opened.st_ctime_ns,
        )
        try:
            final_path = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise _ManualRuntimeWaiting(
                "manual repair candidate artifact changed"
            ) from exc
        final_path_identity = (
            final_path.st_dev,
            final_path.st_ino,
            final_path.st_mode,
            final_path.st_size,
            final_path.st_mtime_ns,
            final_path.st_ctime_ns,
        )
        if (
            size != opened.st_size
            or final_identity != initial_identity
            or final_path_identity != initial_identity
        ):
            raise _ManualRuntimeWaiting(
                "manual repair candidate artifact changed"
            )
        mode = 0o755 if opened.st_mode & stat.S_IXUSR else 0o644
        return _LiveEntry(True, b"".join(chunks), mode)
    finally:
        os.close(descriptor)


def _rename_leaf_no_replace(
    parent_fd: int,
    source: str,
    destination: str,
) -> None:
    """Atomically move one sibling without replacing another entry."""

    if os.name != "posix":
        raise _ManualRuntimeWaiting(
            "manual repair host lacks fd-relative no-replace rename"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
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
            source_bytes,
            parent_fd,
            destination_bytes,
            0x00000004,  # RENAME_EXCL
        )
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
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
            source_bytes,
            parent_fd,
            destination_bytes,
            1,  # RENAME_NOREPLACE
        )
    else:
        raise _ManualRuntimeWaiting(
            "manual repair host lacks fd-relative no-replace rename"
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number))
    if error_number == errno.ENOENT:
        raise FileNotFoundError(error_number, os.strerror(error_number))
    if error_number in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
        raise _ManualRuntimeWaiting(
            "manual repair host rejected fd-relative no-replace rename"
        )
    raise _ManualRuntimeWaiting(
        "manual repair fd-relative no-replace rename failed"
    )


def _atomic_write(parent_fd: int, name: str, entry: _LiveEntry) -> None:
    leaf = f".hermes-manual-{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(leaf, flags, 0o600, dir_fd=parent_fd)
    try:
        offset = 0
        while offset < len(entry.data):
            offset += os.write(descriptor, entry.data[offset:])
        os.fchmod(descriptor, entry.mode)
        os.fsync(descriptor)
    except BaseException:
        try:
            os.unlink(leaf, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    try:
        _rename_leaf_no_replace(
            parent_fd,
            leaf,
            name,
        )
        os.fsync(parent_fd)
    except BaseException:
        try:
            os.unlink(leaf, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _discard_leaf(parent_fd: int, leaf: str | None) -> None:
    if leaf is None:
        return
    try:
        os.unlink(leaf, dir_fd=parent_fd)
    except FileNotFoundError:
        return


def _make_leaf_owner_only(parent_fd: int, leaf: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(leaf, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _ManualRuntimeWaiting(
            "manual repair recovery artifact is unavailable"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _ManualRuntimeWaiting(
                "manual repair recovery artifact is invalid"
            )
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent_fd)


def _preserve_recovery_leaf(
    parent_fd: int,
    leaf: str,
    name: str,
    expected: _LiveEntry,
) -> str:
    """Keep exact recovery bytes under one deterministic private sibling."""

    captured = _read_live_entry(parent_fd, leaf)
    if (
        not captured.exists
        or not expected.exists
        or captured.data != expected.data
        or captured.mode != expected.mode
    ):
        _make_leaf_owner_only(parent_fd, leaf)
        return leaf
    digest = hashlib.sha256(
        b"hermes.manual-recovery-artifact.v1\0"
        + os.fsencode(name)
        + expected.mode.to_bytes(4, "big")
        + expected.data
    ).hexdigest()[:32]
    _make_leaf_owner_only(parent_fd, leaf)
    for index in range(8):
        suffix = "" if index == 0 else f"-{index}"
        recovery_leaf = f".hermes-manual-recovery-{digest}{suffix}"
        try:
            _rename_leaf_no_replace(
                parent_fd,
                leaf,
                recovery_leaf,
            )
            os.fsync(parent_fd)
            return recovery_leaf
        except FileExistsError:
            try:
                existing = _read_live_entry(parent_fd, recovery_leaf)
            except _ManualRuntimeWaiting:
                continue
            if existing.exists and existing.data == expected.data:
                _make_leaf_owner_only(parent_fd, recovery_leaf)
                _discard_leaf(parent_fd, leaf)
                os.fsync(parent_fd)
                return recovery_leaf
    return leaf


def _directory_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _open_bound_directory(path: Path, *, error: str) -> tuple[int, tuple[int, int]]:
    if (
        not path.is_absolute()
        or not path.anchor
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise _ManualRuntimeWaiting(error)
    try:
        root_fd = os.open(path.anchor, _directory_flags())
    except OSError as exc:
        raise _ManualRuntimeWaiting(error) from exc
    try:
        descriptor = _open_directory_chain(
            root_fd,
            path.parts[1:],
            error=error,
        )
    finally:
        os.close(root_fd)
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode):
        os.close(descriptor)
        raise _ManualRuntimeWaiting(error)
    return descriptor, (opened.st_dev, opened.st_ino)


def _open_directory_chain(
    root_fd: int,
    parts: Sequence[str],
    *,
    error: str = "manual repair target parent changed",
) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            try:
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except OSError as exc:
                raise _ManualRuntimeWaiting(error) from exc
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise _ManualRuntimeWaiting(error)
            try:
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            except OSError as exc:
                raise _ManualRuntimeWaiting(error) from exc
            opened = os.fstat(child)
            try:
                current = os.stat(
                    part,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                os.close(child)
                raise _ManualRuntimeWaiting(error)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
            ):
                os.close(child)
                raise _ManualRuntimeWaiting(error)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _entry_matches(observed: _LiveEntry, expected: _LiveEntry) -> bool:
    if (
        observed.exists != expected.exists
        or observed.data != expected.data
        or observed.mode != expected.mode
    ):
        return False
    return expected.identity is None or observed.identity == expected.identity


def _compare_and_swap_entry(
    parent_fd: int,
    name: str,
    expected: _LiveEntry,
    replacement: _LiveEntry,
    *,
    _rollback_on_failure: bool = True,
) -> _LiveEntry:
    """Install ``replacement`` only while ``expected`` owns ``name``.

    An existing entry is first moved to a private sibling with a kernel
    no-replace operation.  Hermes compares that captured entry before it
    publishes anything at ``name``.  The same primitive runs in reverse for
    rollback, so a newer external edit is never replaced by stale bytes.
    """

    staged_leaf: str | None = None
    captured_leaf: str | None = None
    installed = False
    installed_entry = _LiveEntry(False)
    try:
        if replacement.exists:
            staged_leaf = f".hermes-manual-stage-{secrets.token_hex(16)}"
            _atomic_write(parent_fd, staged_leaf, replacement)
            installed_entry = _read_live_entry(parent_fd, staged_leaf)

        if expected.exists:
            captured_leaf = f".hermes-manual-before-{secrets.token_hex(16)}"
            try:
                _rename_leaf_no_replace(
                    parent_fd,
                    name,
                    captured_leaf,
                )
            except FileNotFoundError as exc:
                raise _ManualRuntimeWaiting(
                    "manual repair target changed during scoped apply"
                ) from exc
            observed = _read_live_entry(parent_fd, captured_leaf)
        else:
            observed = _read_live_entry(parent_fd, name)

        if not _entry_matches(observed, expected):
            raise _ManualRuntimeWaiting(
                "manual repair target changed during scoped apply"
            )

        if replacement.exists:
            try:
                _rename_leaf_no_replace(parent_fd, staged_leaf, name)
            except FileExistsError as exc:
                raise _ManualRuntimeWaiting(
                    "manual repair target changed during scoped apply"
                ) from exc
            staged_leaf = None
        installed = True
        os.fsync(parent_fd)
        _discard_leaf(parent_fd, captured_leaf)
        captured_leaf = None
        os.fsync(parent_fd)
        return installed_entry
    except BaseException as apply_error:
        rollback_error: BaseException | None = None
        if installed and _rollback_on_failure:
            try:
                _compare_and_swap_entry(
                    parent_fd,
                    name,
                    installed_entry,
                    expected,
                    _rollback_on_failure=False,
                )
            except BaseException as exc:
                rollback_error = exc
        elif captured_leaf is not None:
            try:
                _rename_leaf_no_replace(parent_fd, captured_leaf, name)
                captured_leaf = None
                os.fsync(parent_fd)
            except FileExistsError as exc:
                recovery_leaf = _preserve_recovery_leaf(
                    parent_fd,
                    captured_leaf,
                    name,
                    expected,
                )
                captured_leaf = None
                rollback_error = _ManualRuntimeWaiting(
                    "manual repair rollback found a newer external entry; "
                    f"the captured original is preserved at sibling {recovery_leaf}",
                    recovery_paths=(recovery_leaf,),
                )
            except BaseException as exc:
                rollback_error = exc
        recovery_paths: list[str] = []
        if (
            staged_leaf is not None
            and not _rollback_on_failure
            and replacement.exists
        ):
            try:
                recovery_leaf = _preserve_recovery_leaf(
                    parent_fd,
                    staged_leaf,
                    name,
                    replacement,
                )
                staged_leaf = None
                recovery_paths.append(recovery_leaf)
            except BaseException as exc:
                if rollback_error is None:
                    rollback_error = exc
        _discard_leaf(parent_fd, staged_leaf)
        _discard_leaf(parent_fd, captured_leaf)
        if recovery_paths:
            detail = ", ".join(recovery_paths)
            raise _ManualRuntimeWaiting(
                "manual repair rollback found a newer external entry; "
                f"the original is preserved at sibling {detail}",
                recovery_paths=tuple(recovery_paths),
            ) from apply_error
        if rollback_error is not None:
            if isinstance(rollback_error, _ManualRuntimeWaiting) and (
                rollback_error.recovery_paths
            ):
                raise rollback_error from apply_error
            raise _ManualRuntimeWaiting(
                "manual repair compare-and-swap rollback failed closed"
            ) from rollback_error
        raise apply_error


def _apply_repaired_paths(
    *,
    workspace: Path,
    materialized_root: Path,
    prefix: str,
    root_paths: Sequence[str],
    cancel_event: object | None = None,
    adoption_lock: object | None = None,
) -> tuple[str, ...]:
    prepared: list[
        tuple[int, tuple[str, ...], tuple[int, int], str, _LiveEntry, _LiveEntry]
    ] = []
    descriptors: list[int] = []
    workspace_paths: list[str] = []
    _check_cancelled(cancel_event)
    if adoption_lock is not None and not all(
        callable(getattr(adoption_lock, name, None))
        for name in ("acquire", "release")
    ):
        raise _ManualRuntimeWaiting("manual repair adoption control is invalid")
    try:
        workspace_fd, workspace_identity = _open_bound_directory(
            workspace,
            error="manual repair workspace changed",
        )
        descriptors.append(workspace_fd)
        materialized_fd, _materialized_identity = _open_bound_directory(
            materialized_root,
            error="manual repair candidate root changed",
        )
        descriptors.append(materialized_fd)
        for root_path in root_paths:
            relative = _unbase(root_path, prefix)
            relative_path = PurePosixPath(relative)
            parent_parts = tuple(relative_path.parts[:-1])
            parent_fd = _open_directory_chain(workspace_fd, parent_parts)
            descriptors.append(parent_fd)
            opened_parent = os.fstat(parent_fd)
            if not stat.S_ISDIR(opened_parent.st_mode):
                raise _ManualRuntimeWaiting(
                    "manual repair target parent changed"
                )
            parent_identity = (opened_parent.st_dev, opened_parent.st_ino)
            name = relative_path.name
            before = _read_live_entry(parent_fd, name)
            source_path = PurePosixPath(_relative_path(root_path))
            source_parent_fd = _open_directory_chain(
                materialized_fd,
                tuple(source_path.parts[:-1]),
                error="manual repair candidate parent changed",
            )
            descriptors.append(source_parent_fd)
            after = _source_entry(source_parent_fd, source_path.name)
            prepared.append(
                (
                    parent_fd,
                    parent_parts,
                    parent_identity,
                    name,
                    before,
                    after,
                )
            )
            workspace_paths.append(relative)

        with adoption_lock if adoption_lock is not None else nullcontext():
            _check_cancelled(cancel_event)
            applied: list[tuple[int, str, _LiveEntry, _LiveEntry]] = []
            try:
                try:
                    current_workspace = workspace.lstat()
                except OSError as exc:
                    raise _ManualRuntimeWaiting(
                        "manual repair workspace changed"
                    ) from exc
                if (
                    not stat.S_ISDIR(current_workspace.st_mode)
                    or (current_workspace.st_dev, current_workspace.st_ino)
                    != workspace_identity
                ):
                    raise _ManualRuntimeWaiting(
                        "manual repair workspace changed"
                    )
                for (
                    parent_fd,
                    parent_parts,
                    parent_identity,
                    name,
                    before,
                    after,
                ) in prepared:
                    _check_cancelled(cancel_event)
                    probe_fd = _open_directory_chain(workspace_fd, parent_parts)
                    try:
                        probe = os.fstat(probe_fd)
                        if (probe.st_dev, probe.st_ino) != parent_identity:
                            raise _ManualRuntimeWaiting(
                                "manual repair target parent changed"
                            )
                    finally:
                        os.close(probe_fd)
                    installed = _compare_and_swap_entry(
                        parent_fd,
                        name,
                        before,
                        after,
                    )
                    applied.append((parent_fd, name, before, installed))
                    _check_cancelled(cancel_event)
                for parent_fd, name, _before, installed in applied:
                    _check_cancelled(cancel_event)
                    if not _entry_matches(
                        _read_live_entry(parent_fd, name), installed
                    ):
                        raise _ManualRuntimeWaiting(
                            "manual repair scoped apply differs from checked evidence"
                        )
                for (
                    _parent_fd,
                    parent_parts,
                    parent_identity,
                    _name,
                    _before,
                    _after,
                ) in prepared:
                    probe_fd = _open_directory_chain(workspace_fd, parent_parts)
                    try:
                        probe = os.fstat(probe_fd)
                        if (probe.st_dev, probe.st_ino) != parent_identity:
                            raise _ManualRuntimeWaiting(
                                "manual repair target parent changed"
                            )
                    finally:
                        os.close(probe_fd)
                _check_cancelled(cancel_event)
            except BaseException as apply_error:
                rollback_error: BaseException | None = None
                for parent_fd, name, before, installed in reversed(applied):
                    try:
                        _compare_and_swap_entry(
                            parent_fd,
                            name,
                            installed,
                            before,
                            _rollback_on_failure=False,
                        )
                    except BaseException as exc:
                        rollback_error = exc
                if rollback_error is not None:
                    recovery_paths = tuple(
                        getattr(rollback_error, "recovery_paths", ())
                    )
                    suffix = (
                        "; captured original preserved at sibling "
                        + ", ".join(recovery_paths)
                        if recovery_paths
                        else ""
                    )
                    raise _ManualRuntimeWaiting(
                        "manual repair rollback could not restore the exact target"
                        + suffix,
                        recovery_paths=recovery_paths,
                    ) from rollback_error
                raise apply_error
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return tuple(sorted(workspace_paths))


def _execute_manual_bestplan_repair(
    *,
    agent: object,
    blockers: object,
    allowed_paths: object,
    generation: object,
    repair_attempt: object = 1,
    target: object,
    workspace: object,
    expected_live_state_digest: object,
    task: object = "",
    cancel_event: object | None = None,
    adoption_lock: object | None = None,
    checkpoint_callback: object | None = None,
) -> dict[str, object]:
    from agent import bestplan_candidates, bestplan_checks, bestplan_local
    from agent import bestplan_promotion, bestplan_source
    from agent.bestplan_candidates import CandidateSpec
    from agent.bestplan_contract import source_snapshot_digest
    from tools.delegate_tool import (
        _bestplan_safe_identifier,
        _build_local_candidate_binding,
        _validate_bestplan_frozen_candidate,
    )

    _check_cancelled(cancel_event)
    try:
        owned_workspace = Path(workspace).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise _ManualRuntimeWaiting("manual repair workspace is invalid") from exc
    paths = _ordered_paths(allowed_paths, field="allowed paths")
    blockers_tuple = _validate_blocker_authority(blockers, paths)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise _ManualRuntimeWaiting("manual repair generation is invalid")
    if (
        isinstance(repair_attempt, bool)
        or not isinstance(repair_attempt, int)
        or repair_attempt < 1
    ):
        raise _ManualRuntimeWaiting("manual repair attempt is invalid")
    if (
        getattr(target, "source_kind", None) != "manual_snapshot"
        or getattr(target, "generation", None) != generation - 1
        or not _OID_RE.fullmatch(str(getattr(target, "base_oid", "")))
        or not _OID_RE.fullmatch(str(getattr(target, "snapshot_tree_oid", "")))
        or not _DIGEST_RE.fullmatch(str(getattr(target, "target_digest", "")))
        or not _DIGEST_RE.fullmatch(str(expected_live_state_digest or ""))
    ):
        raise _ManualRuntimeWaiting("manual repair target evidence is invalid")

    from agent.review_engine import _manual_live_state_digest

    if _manual_live_state_digest(owned_workspace, paths) != expected_live_state_digest:
        raise _ManualRuntimeWaiting("manual repair target changed")

    _check_cancelled(cancel_event)
    deadline = time.monotonic() + _HOST_OPERATION_SECONDS
    repo = bestplan_source.resolve_repo_identity(
        str(owned_workspace), deadline=deadline
    )
    snapshot = bestplan_source.capture_source_snapshot(repo, deadline)
    if snapshot.head_oid != target.base_oid:
        raise _ManualRuntimeWaiting("manual repair Git target changed")
    worktree = Path(repo.worktree).resolve(strict=True)
    prefix = _workspace_prefix(owned_workspace, worktree)
    root_allowed = tuple(_rebase(path, prefix) for path in paths)
    test_nodes = _candidate_test_paths(
        agent=agent,
        workspace=owned_workspace,
        git_root=worktree,
        allowed_paths=paths,
        prefix=prefix,
        tree_oid=target.snapshot_tree_oid,
    )
    root_test_nodes = tuple(
        _rebase(node, prefix) for node in test_nodes
    )
    expected_artifacts = [
        path
        for path in paths
        if _tree_blob_mode(
            worktree, target.snapshot_tree_oid, _rebase(path, prefix)
        )
        is not None
    ]
    for node in test_nodes:
        path = node.split("::", 1)[0]
        if path not in expected_artifacts:
            expected_artifacts.append(path)
    if not expected_artifacts:
        raise _ManualRuntimeWaiting(
            "manual repair has no immutable expected artifact"
        )

    plan = _build_plan(
        workspace=owned_workspace,
        task=str(task or ""),
        allowed_paths=paths,
        expected_artifacts=tuple(expected_artifacts),
        root_test_nodes=root_test_nodes,
    )
    manifest = plan.to_manifest()
    runtime_spec, authority = _resolve_write_authority(
        agent=agent,
        workspace=owned_workspace,
        goal=plan.slices[0].goal,
    )
    plan_id = _safe_identifier(
        "manual-review",
        getattr(target, "job_id", ""),
        generation,
        repair_attempt,
        target.target_digest,
    )
    inputs = bestplan_local.capture_local_execution_inputs(
        snapshot=snapshot,
        controller_python=Path(sys.executable),
        manifest=manifest,
        deadline=deadline,
    )
    contract = bestplan_local.build_local_go_contract(
        snapshot=snapshot,
        controller=inputs.controller,
        commands=inputs.check_plan.commands,
        manifest_digest=bestplan_local.local_go_manifest_digest(manifest),
        check_runtime_digest=inputs.check_plan.check_runtime_digest,
    )
    contract_digest = bestplan_local.local_go_contract_digest(contract)
    approval_digest = bestplan_local.local_go_approval_digest(manifest, contract)
    runtime = bestplan_local.build_local_execution_runtime(
        plan_id=plan_id,
        snapshot=snapshot,
        manifest=manifest,
        contract=contract,
        controller_python=Path(sys.executable),
        deadline=deadline,
    )
    deadline = min(
        deadline,
        time.monotonic() + float(runtime.operation_timeout_seconds),
    )
    prior = _prior_integration(
        workspace=owned_workspace,
        snapshot=snapshot,
        target=target,
        plan_id=plan_id,
        approval_digest=approval_digest,
        contract_digest=contract_digest,
    )

    generation_plan_id = _bestplan_safe_identifier(
        "repair-plan", plan_id, generation
    )
    slice_id = "manual-objective"
    candidate_id = _bestplan_safe_identifier(
        "candidate", generation_plan_id, 0, slice_id
    )
    frozen_slice_id = _bestplan_safe_identifier(
        "slice", generation_plan_id, 0, slice_id
    )
    attempt_id = _bestplan_safe_identifier(
        "attempt", generation_plan_id, 0, slice_id
    )
    repair_evidence = json.dumps(
        {
            "blockers": [_blocker_payload(item) for item in blockers_tuple],
            "generation": generation,
            "repair_attempt": repair_attempt,
            "prior_target_digest": target.target_digest,
            "rules": [
                "Change only the exact approved manual objective paths.",
                "Preserve every expected artifact.",
                "Do not claim pass; the host reruns checks and review.",
            ],
            "schema": "hermes.manual-review-repair-instruction.v1",
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    repair_spec = CandidateSpec(
        plan_id=generation_plan_id,
        candidate_id=candidate_id,
        slice_id=frozen_slice_id,
        goal=(
            f"{plan.slices[0].goal}\n\n"
            f"Automatic manual review repair evidence:\n{repair_evidence}"
        ),
        allowed_paths=root_allowed,
        read_only=False,
        expected_artifacts=tuple(
            _rebase(path, prefix) for path in expected_artifacts
        ),
        model=str(runtime_spec.get("model") or ""),
        request_budget=runtime.candidate_runtime.request_budget,
        token_budget=runtime.candidate_runtime.token_budget,
        expires_at=math.ceil(
            time.time() + runtime.candidate_runtime.capability_ttl_seconds
        ),
        max_iterations=runtime.candidate_runtime.max_iterations,
        max_output_tokens=runtime.candidate_runtime.max_output_tokens,
        toolsets=("file",),
    )
    frozen = bestplan_candidates.run_and_freeze_repair_candidate(
        snapshot=snapshot,
        candidate_base=prior,
        spec=repair_spec,
        attempts_root=runtime.candidate_runtime.attempts_root,
        controller_source=runtime.candidate_runtime.controller_source,
        controller_python=runtime.candidate_runtime.controller_python,
        runtime_read_paths=runtime.candidate_runtime.runtime_read_paths,
        expected_controller=runtime.candidate_runtime.controller,
        authority_client=authority,
        timeout_seconds=runtime.candidate_runtime.timeout_seconds,
        attempt_id=attempt_id,
        cancel_event=cancel_event,
    )
    _check_cancelled(cancel_event)
    _validate_bestplan_frozen_candidate(
        frozen,
        spec=repair_spec,
        attempt_id=attempt_id,
        runtime=runtime.candidate_runtime,
    )
    root_changed = tuple(
        sorted(path.decode("utf-8", "strict") for path in frozen.changed_paths)
    )
    if not root_changed:
        raise _ManualRuntimeWaiting("manual repair candidate made no progress")
    outside = tuple(path for path in root_changed if path not in root_allowed)
    if outside:
        requested: list[str] = []
        for path in outside:
            try:
                requested.append(_unbase(path, prefix))
            except _ManualRuntimeRequiresAuthority:
                requested.append(path)
        raise _ManualRuntimeRequiresAuthority(tuple(requested))

    binding = _build_local_candidate_binding(
        frozen=frozen,
        spec=repair_spec,
        manifest_slice_id=slice_id,
        snapshot=snapshot,
        approval_digest=approval_digest,
        contract_digest=contract_digest,
        candidate_base_oid=prior.integration_oid,
    )
    repaired = bestplan_promotion.freeze_repair_integration(
        plan_id=plan_id,
        generation=generation,
        prior=prior,
        plan=plan,
        snapshot=snapshot,
        contract=contract,
        approval_digest=approval_digest,
        candidates=(binding,),
        temp_root=runtime.integration_root,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    _check_cancelled(cancel_event)
    checks = bestplan_checks.run_integration_checks(
        snapshot=snapshot,
        integration=repaired,
        contract=contract,
        commands=runtime.check_plan.commands,
        runtime=runtime.check_runtime,
        checks_root=runtime.checks_root,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    _check_cancelled(cancel_event)
    if not _DIGEST_RE.fullmatch(str(checks.receipt_digest)):
        raise _ManualRuntimeWaiting("manual repair check receipt is invalid")
    if source_snapshot_digest(snapshot) != repaired.source_snapshot_digest:
        raise _ManualRuntimeWaiting("manual repair source proof changed")
    if _manual_live_state_digest(owned_workspace, paths) != expected_live_state_digest:
        raise _ManualRuntimeWaiting("manual repair target changed before apply")

    with tempfile.TemporaryDirectory(
        prefix="manual-apply-", dir=runtime.integration_root
    ) as raw_parent:
        materialized = Path(raw_parent) / "integration"
        bestplan_promotion.materialize_integration_tree(
            snapshot=snapshot,
            integration=repaired,
            destination=materialized,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        _check_cancelled(cancel_event)
        if checkpoint_callback is not None:
            if not callable(checkpoint_callback):
                raise _ManualRuntimeWaiting(
                    "manual repair checkpoint callback is invalid"
                )
            checkpoint_callback(
                {
                    "changed_paths": [
                        _unbase(path, prefix) for path in root_changed
                    ],
                    "check_receipt_digest": checks.receipt_digest,
                    "integration_oid": repaired.integration_oid,
                    "integration_receipt_digest": repaired.receipt_digest,
                    "integration_ref": repaired.ref_name,
                    "snapshot_tree_oid": repaired.tree_oid,
                    "status": "prepared",
                }
            )
            _check_cancelled(cancel_event)
        changed_paths = _apply_repaired_paths(
            workspace=owned_workspace,
            materialized_root=materialized,
            prefix=prefix,
            root_paths=root_changed,
            cancel_event=cancel_event,
            adoption_lock=adoption_lock,
        )
    return {
        "status": "applied",
        "changed_paths": list(changed_paths),
        "check_receipt_digest": checks.receipt_digest,
    }


def execute_manual_bestplan_repair(**kwargs: object) -> Mapping[str, object]:
    """Run one canonical isolated repair and apply only its checked file delta."""

    try:
        return _execute_manual_bestplan_repair(**kwargs)
    except _ManualRuntimeRequiresAuthority as exc:
        result: dict[str, object] = {"status": "requires_authority"}
        if exc.paths:
            result["requested_paths"] = list(exc.paths)
        return result
    except Exception as exc:
        logger.warning(
            "manual review repair is waiting after %s", type(exc).__name__
        )
        return {
            "status": "waiting",
            "reason": "manual review repair or exact checks are unavailable",
        }
