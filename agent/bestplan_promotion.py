"""Deterministic, host-owned integration for frozen BestPlan candidates.

This module does not advance ``main`` and never imports candidate code.  It
rederives each candidate's base-relative tree delta, composes nonoverlapping
deltas in memory, creates one deterministic integration commit whose sole
parent is the admitted current target, and anchors that commit under an owned
compare-and-swap ref.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import re
import secrets
import stat
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from agent import bestplan_source as source_boundary
from agent.bestplan_candidates import (
    candidate_ref_name,
    terminate_process_group,
    validate_raw_candidate_paths,
)
from agent.bestplan_contract import (
    CONTRACT_SCHEMA,
    BoundCommand,
    ContractValidationError,
    ControllerIdentity,
    EnrolledRepository,
    _command_from_dict,
    _controller_from_dict,
    _repository_from_dict,
    approval_digest as compute_approval_digest,
    canonical_json,
    contract_digest as compute_contract_digest,
    source_snapshot_digest,
    validate_execution_contract,
)
from agent.bestplan_source import (
    RepoIdentity,
    SourceSnapshot,
    assert_supported_repository,
    capture_source_snapshot,
    resolve_repo_identity,
)
from agent.execution_plan import ExecutionPlan, ExecutionSlice


MAX_PROMOTION_GIT_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_PROMOTION_GIT_INPUT_BYTES = 128 * 1024 * 1024
MAX_PROMOTION_BLOB_BYTES = 64 * 1024 * 1024
MAX_PROMOTION_DEADLINE_SECONDS = 86_400.0
_SHA256 = frozenset("0123456789abcdef")
_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class PromotionError(RuntimeError):
    """Base class for immutable integration failures."""


class IntegrationValidationError(PromotionError):
    """Approval, manifest, binding, or path evidence is malformed."""


class IntegrationConflictError(PromotionError):
    """A target or another candidate changed the same path relationship."""


class IntegrationProofStale(PromotionError):
    """A repository, target, candidate, or object differs from its binding."""


class IntegrationRefConflict(PromotionError):
    """An owned integration ref already names different evidence."""


class _GitFailure(PromotionError):
    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        super().__init__("integration Git operation failed")
        self.returncode = returncode
        self.stderr = stderr


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= _SHA256
    )


def _is_oid(value: object, object_format: str | None = None) -> bool:
    expected = 64 if object_format == "sha256" else 40 if object_format == "sha1" else None
    return (
        isinstance(value, str)
        and (len(value) == expected if expected is not None else len(value) in {40, 64})
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
        raise IntegrationValidationError(
            "integration evidence is not canonical JSON"
        ) from None


def _domain_digest(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + b"\0" + _canonical_json(value)).hexdigest()


def _validated_deadline(value: object) -> float:
    """Return one bounded monotonic deadline before any external side effect."""

    if type(value) not in {int, float}:
        raise IntegrationValidationError("integration deadline is invalid")
    deadline = float(value)
    now = time.monotonic()
    if (
        not math.isfinite(deadline)
        or deadline <= now
        or deadline - now > MAX_PROMOTION_DEADLINE_SECONDS
    ):
        raise IntegrationValidationError("integration deadline is invalid")
    return deadline


def _validated_cancel_event(value: object) -> threading.Event | None:
    if value is not None and not isinstance(value, threading.Event):
        raise IntegrationValidationError("integration cancel event is invalid")
    return value


def _check_control(
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PromotionError("integration cancelled")
    if time.monotonic() >= deadline:
        raise PromotionError("integration deadline expired")


def _safe_identifier(prefix: str, *values: object) -> str:
    """Mirror the Task 5 host identity derivation without importing its runner."""

    payload = json.dumps(
        [str(value) for value in values],
        ensure_ascii=True,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("ascii")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _candidate_plan_id(plan_id: str) -> str:
    return (
        plan_id
        if _CANDIDATE_ID.fullmatch(plan_id)
        else _safe_identifier("plan", plan_id)
    )


def _binding_slice_matches_manifest(
    *, plan_id: str, manifest_index: int, manifest_slice_id: str, slice_id: str,
) -> bool:
    return slice_id == _safe_identifier(
        "slice", plan_id, manifest_index, manifest_slice_id,
    )


@dataclass(frozen=True)
class CandidateIntegrationBinding:
    manifest_slice_id: str
    candidate_id: str
    slice_id: str
    attempt_id: str
    ref_name: str
    commit_oid: str
    tree_oid: str
    changed_paths: tuple[bytes, ...]
    base_oid: str
    approval_digest: str
    contract_digest: str
    source_snapshot_digest: str
    policy_digest: str
    controller_id: str
    controller_repository_id: str
    controller_release_oid: str
    controller_artifact_sha256: str
    candidate_receipt_digest: str
    binding_digest: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.manifest_slice_id, "manifest slice"),
            (self.candidate_id, "candidate"),
            (self.slice_id, "slice"),
            (self.attempt_id, "attempt"),
            (self.controller_id, "controller"),
            (self.controller_repository_id, "controller repository"),
        ):
            if (
                not isinstance(value, str)
                or not value
                or "\x00" in value
                or len(value.encode("utf-8")) > 1024
            ):
                raise IntegrationValidationError(
                    f"integration {label} identity is invalid"
                )
        if (
            not isinstance(self.ref_name, str)
            or not self.ref_name.startswith("refs/hermes-bestplan/")
            or "\x00" in self.ref_name
            or len(self.ref_name) > 1024
        ):
            raise IntegrationValidationError("integration candidate ref is invalid")
        for value, label in (
            (self.commit_oid, "candidate commit"),
            (self.tree_oid, "candidate tree"),
            (self.base_oid, "candidate base"),
            (self.controller_release_oid, "controller release"),
        ):
            if not _is_oid(value):
                raise IntegrationValidationError(f"integration {label} is invalid")
        for value, label in (
            (self.approval_digest, "approval"),
            (self.contract_digest, "contract"),
            (self.source_snapshot_digest, "source snapshot"),
            (self.policy_digest, "sandbox policy"),
            (self.controller_artifact_sha256, "controller artifact"),
            (self.candidate_receipt_digest, "candidate receipt"),
            (self.binding_digest, "candidate binding"),
        ):
            if not _is_sha256(value):
                raise IntegrationValidationError(f"integration {label} digest is invalid")
        changed = tuple(self.changed_paths)
        try:
            validated = validate_raw_candidate_paths(changed)
        except BaseException:
            raise IntegrationValidationError(
                "integration changed paths are invalid"
            ) from None
        if validated != tuple(sorted(set(changed))):
            raise IntegrationValidationError(
                "integration changed paths must be sorted and unique"
            )
        object.__setattr__(self, "changed_paths", changed)
        if candidate_integration_binding_digest(self) != self.binding_digest:
            raise IntegrationValidationError("integration candidate binding digest differs")


def candidate_integration_binding_digest(
    value: CandidateIntegrationBinding | Mapping[str, object],
) -> str:
    names = (
        "manifest_slice_id",
        "candidate_id",
        "slice_id",
        "attempt_id",
        "ref_name",
        "commit_oid",
        "tree_oid",
        "changed_paths",
        "base_oid",
        "approval_digest",
        "contract_digest",
        "source_snapshot_digest",
        "policy_digest",
        "controller_id",
        "controller_repository_id",
        "controller_release_oid",
        "controller_artifact_sha256",
        "candidate_receipt_digest",
    )
    try:
        if isinstance(value, Mapping):
            if set(value) != set(names):
                raise KeyError
            fields = {name: value[name] for name in names}
        else:
            fields = {name: getattr(value, name) for name in names}
        changed = fields.pop("changed_paths")
        if not isinstance(changed, tuple) or any(not isinstance(item, bytes) for item in changed):
            raise TypeError
        fields["changed_paths_hex"] = [item.hex() for item in changed]
    except (AttributeError, KeyError, TypeError):
        raise IntegrationValidationError("integration candidate binding is malformed") from None
    return _domain_digest(b"hermes.bestplan.candidate-integration-binding.v1", fields)


@dataclass(frozen=True)
class AppliedCandidate:
    manifest_index: int
    manifest_slice_id: str
    slice_id: str
    candidate_id: str
    attempt_id: str
    ref_name: str
    commit_oid: str
    tree_oid: str
    policy_digest: str
    candidate_receipt_digest: str
    binding_digest: str
    changed_paths_sha256: str
    artifact_digests: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class FrozenIntegration:
    plan_id: str
    approval_digest: str
    contract_digest: str
    source_snapshot_digest: str
    target_ref: str
    target_oid: str
    integration_oid: str
    tree_oid: str
    ref_name: str
    candidates: tuple[AppliedCandidate, ...]
    receipt_digest: str


@dataclass(frozen=True)
class _GitEntry:
    mode: int
    object_type: str
    oid: str


@dataclass(frozen=True)
class _PreparedTempRoot:
    path: Path
    raw_path: bytes
    descriptor: int
    identity: tuple[int, int]


@dataclass(frozen=True)
class _OwnedTempAttempt:
    root: _PreparedTempRoot
    leaf: str
    path: Path
    identity: tuple[int, int]


class _ControlledDeadline(float):
    """Expose one caller deadline while polling an older private API's cancel."""

    cancel_event: threading.Event | None

    def __new__(
        cls,
        deadline: float,
        cancel_event: threading.Event | None,
    ) -> _ControlledDeadline:
        value = super().__new__(cls, deadline)
        value.cancel_event = cancel_event
        return value

    def __float__(self) -> float:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise PromotionError("integration cancelled")
        return super().__float__()


def _git_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    if extra:
        environment.update(extra)
    return environment


def _run_git(
    repo: RepoIdentity,
    *args: str,
    input_bytes: bytes | None = None,
    extra_environment: Mapping[str, str] | None = None,
    deadline: float,
    cancel_event: threading.Event | None = None,
    check: bool = True,
    output_limit: int = MAX_PROMOTION_GIT_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    if time.monotonic() >= deadline:
        raise PromotionError("integration Git deadline expired")
    if input_bytes is not None and len(input_bytes) > MAX_PROMOTION_GIT_INPUT_BYTES:
        raise IntegrationValidationError("integration Git input is too large")
    command = [
        "/usr/bin/git",
        f"--git-dir={repo.git_dir}",
        f"--work-tree={repo.worktree}",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "filter.lfs.required=false",
        *args,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(extra_environment),
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise PromotionError("integration Git operation could not start") from None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    read_error = threading.Event()
    writer_error = threading.Event()

    def drain(name: str, stream: object) -> None:
        try:
            read = getattr(stream, "read1", None) or getattr(stream, "read")
            while True:
                chunk = read(64 * 1024)
                if not chunk:
                    return
                remaining = max(0, output_limit + 1 - len(buffers[name]))
                if remaining:
                    buffers[name].extend(chunk[:remaining])
                if len(buffers[name]) > output_limit or len(chunk) > remaining:
                    overflow.set()
                    return
        except BaseException:
            read_error.set()

    def write_input() -> None:
        assert process.stdin is not None and input_bytes is not None
        try:
            process.stdin.write(input_bytes)
            process.stdin.flush()
            process.stdin.close()
        except BaseException:
            writer_error.set()

    readers: list[threading.Thread] = []
    for name in ("stdout", "stderr"):
        stream = getattr(process, name)
        thread = threading.Thread(target=drain, args=(name, stream), daemon=True)
        thread.start()
        readers.append(thread)
    writer: threading.Thread | None = None
    if input_bytes is not None:
        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
    terminal_error: str | None = None
    while True:
        if cancel_event is not None and cancel_event.is_set():
            terminal_error = "integration cancelled"
            break
        if time.monotonic() >= deadline:
            terminal_error = "integration Git deadline expired"
            break
        if overflow.is_set():
            terminal_error = "integration Git output limit exceeded"
            break
        if read_error.is_set() or writer_error.is_set():
            terminal_error = "integration Git pipe failed"
            break
        if process.poll() is not None:
            break
        time.sleep(0.005)
    group_extinct = False
    if terminal_error is None and process.returncode is not None:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            group_extinct = True
        except PermissionError:
            pass
    if not group_extinct:
        try:
            terminate_process_group(process, grace_seconds=0.5)
        except BaseException:
            raise PromotionError("integration Git process extinction failed") from None
    if writer is not None:
        writer.join(timeout=max(0.0, min(1.0, deadline - time.monotonic())))
    for reader in readers:
        reader.join(timeout=max(0.0, min(1.0, deadline - time.monotonic())))
    if (writer is not None and writer.is_alive()) or any(item.is_alive() for item in readers):
        raise PromotionError("integration Git pipes did not become extinct")
    if terminal_error is not None:
        raise PromotionError(terminal_error)
    result = subprocess.CompletedProcess(
        command,
        int(process.returncode),
        bytes(buffers["stdout"]),
        bytes(buffers["stderr"]),
    )
    if check and result.returncode != 0:
        raise _GitFailure(result.returncode, result.stderr)
    return result


def _assert_repo_identity(expected: RepoIdentity, *, deadline: float) -> None:
    try:
        actual = resolve_repo_identity(expected.workspace, deadline=deadline)
    except BaseException:
        raise IntegrationProofStale("integration repository identity is unavailable") from None
    if actual != expected:
        raise IntegrationProofStale("integration repository identity changed")


def _read_ref(
    repo: RepoIdentity,
    ref_name: str,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> str | None:
    existence = _run_git(
        repo,
        "show-ref",
        "--exists",
        ref_name,
        deadline=deadline,
        cancel_event=cancel_event,
        check=False,
    )
    if existence.returncode == 2:
        return None
    if existence.returncode != 0:
        raise IntegrationProofStale("integration ref existence could not be read")
    result = _run_git(
        repo,
        "show-ref",
        "--verify",
        "--hash",
        ref_name,
        deadline=deadline,
        cancel_event=cancel_event,
        check=False,
    )
    if result.returncode != 0:
        raise IntegrationProofStale("integration ref could not be read")
    try:
        oid = result.stdout.strip().decode("ascii")
    except UnicodeError:
        raise IntegrationProofStale("integration ref returned a malformed object id") from None
    if not _is_oid(oid, repo.object_format):
        raise IntegrationProofStale("integration ref returned a malformed object id")
    return oid


def _tree_map(
    repo: RepoIdentity,
    treeish: str,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> dict[bytes, _GitEntry]:
    result = _run_git(
        repo,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        treeish,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    records = result.stdout.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    output: dict[bytes, _GitEntry] = {}
    paths: list[bytes] = []
    for record in records:
        _check_control(deadline=deadline, cancel_event=cancel_event)
        try:
            metadata, path = record.split(b"\t", 1)
            mode_raw, object_type_raw, oid_raw = metadata.split(b" ", 2)
            mode = int(mode_raw, 8)
            object_type = object_type_raw.decode("ascii")
            oid = oid_raw.decode("ascii")
        except (ValueError, UnicodeError):
            raise IntegrationProofStale("integration Git tree record is malformed") from None
        if object_type != "blob" or mode not in {0o100644, 0o100755, 0o120000}:
            raise IntegrationValidationError("integration tree contains an unsupported object")
        if not _is_oid(oid, repo.object_format) or path in output:
            raise IntegrationProofStale("integration tree object identity is malformed")
        output[path] = _GitEntry(mode=mode, object_type=object_type, oid=oid)
        paths.append(path)
    try:
        validate_raw_candidate_paths(paths)
    except BaseException:
        raise IntegrationValidationError("integration tree contains an unsafe path") from None
    return output


def _changed_paths(
    before: Mapping[bytes, _GitEntry], after: Mapping[bytes, _GitEntry],
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> tuple[bytes, ...]:
    changed: list[bytes] = []
    for path in sorted(set(before) | set(after)):
        _check_control(deadline=deadline, cancel_event=cancel_event)
        if before.get(path) != after.get(path):
            changed.append(path)
    return tuple(changed)


def _read_blob_sha256(
    repo: RepoIdentity,
    oid: str,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> str:
    size_result = _run_git(
        repo,
        "cat-file",
        "-s",
        oid,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    try:
        size = int(size_result.stdout.strip())
    except ValueError:
        raise IntegrationProofStale("integration artifact size is malformed") from None
    if not 0 <= size <= MAX_PROMOTION_BLOB_BYTES:
        raise IntegrationValidationError("integration artifact exceeds the bounded size")
    data = _run_git(
        repo,
        "cat-file",
        "blob",
        oid,
        deadline=deadline,
        cancel_event=cancel_event,
        output_limit=MAX_PROMOTION_BLOB_BYTES,
    ).stdout
    if len(data) != size:
        raise IntegrationProofStale("integration artifact bytes are incomplete")
    return hashlib.sha256(data).hexdigest()


def _commit_proof(
    repo: RepoIdentity,
    commit_oid: str,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> tuple[str, tuple[str, ...]]:
    object_type = _run_git(
        repo,
        "cat-file",
        "-t",
        commit_oid,
        deadline=deadline,
        cancel_event=cancel_event,
    ).stdout.strip()
    tree = _run_git(
        repo,
        "rev-parse",
        f"{commit_oid}^{{tree}}",
        deadline=deadline,
        cancel_event=cancel_event,
    ).stdout.strip().decode("ascii")
    parents = _run_git(
        repo,
        "rev-list",
        "--parents",
        "-n",
        "1",
        commit_oid,
        deadline=deadline,
        cancel_event=cancel_event,
    ).stdout.decode("ascii").split()
    if object_type != b"commit" or not parents or parents[0] != commit_oid:
        raise IntegrationProofStale("integration commit proof is malformed")
    return tree, tuple(parents[1:])


def _path_alias(path: bytes) -> str:
    try:
        text = path.decode("utf-8", "strict")
    except UnicodeError:
        raise IntegrationValidationError("integration path is not valid UTF-8") from None
    return "/".join(
        unicodedata.normalize("NFC", part).casefold() for part in text.split("/")
    )


def _path_related(left: bytes, right: bytes) -> bool:
    left_alias = _path_alias(left)
    right_alias = _path_alias(right)
    return (
        left_alias == right_alias
        or left_alias.startswith(right_alias + "/")
        or right_alias.startswith(left_alias + "/")
    )


def _assert_composed_tree_has_no_component_aliases(
    tree: Mapping[bytes, _GitEntry],
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> None:
    """Reject sibling components that alias under NFC plus case folding."""

    siblings: dict[tuple[str, ...], dict[str, bytes]] = {}
    _check_control(deadline=deadline, cancel_event=cancel_event)
    for path in sorted(tree):
        _check_control(deadline=deadline, cancel_event=cancel_event)
        normalized_parent: list[str] = []
        for component in path.split(b"/"):
            _check_control(deadline=deadline, cancel_event=cancel_event)
            try:
                text = component.decode("utf-8", "strict")
            except UnicodeError:
                raise IntegrationValidationError(
                    "integration path is not valid UTF-8"
                ) from None
            normalized = unicodedata.normalize("NFC", text).casefold()
            parent = tuple(normalized_parent)
            previous = siblings.setdefault(parent, {}).get(normalized)
            if previous is not None and previous != component:
                raise IntegrationConflictError(
                    "integration composed tree contains a component alias conflict"
                )
            siblings[parent][normalized] = component
            normalized_parent.append(normalized)


def _paths_overlap(left: bytes, right: bytes) -> bool:
    try:
        common = os.path.commonpath((left, right))
    except ValueError:
        return False
    return common in {left, right}


def _prepare_temp_root(
    value: str | Path,
    *,
    repo: RepoIdentity,
    deadline: float,
    cancel_event: threading.Event | None,
) -> _PreparedTempRoot:
    """Open one pre-created, private, repository-disjoint host temp root."""

    _check_control(deadline=deadline, cancel_event=cancel_event)
    try:
        raw = os.fsencode(value)
    except (TypeError, ValueError):
        raise IntegrationValidationError("integration temp root is invalid") from None
    if not raw or b"\0" in raw:
        raise IntegrationValidationError("integration temp root is invalid")
    absolute = os.path.abspath(os.path.expanduser(raw))
    canonical = os.path.realpath(absolute)
    if raw != absolute or canonical != absolute:
        raise IntegrationValidationError(
            "integration temp root must be an absolute canonical path"
        )
    forbidden_roots = tuple(
        os.path.realpath(item)
        for item in (
            repo.worktree_raw,
            repo.git_dir_raw,
            repo.common_dir_raw,
        )
    )
    if any(_paths_overlap(canonical, forbidden) for forbidden in forbidden_roots):
        raise IntegrationValidationError(
            "integration temp root overlaps repository state"
        )
    descriptor = -1
    try:
        before = os.stat(absolute, follow_symlinks=False)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(absolute, flags)
        opened = os.fstat(descriptor)
        after = os.stat(absolute, follow_symlinks=False)
    except (OSError, RuntimeError, ValueError):
        if descriptor >= 0:
            os.close(descriptor)
        raise IntegrationValidationError(
            "integration temp root must already exist safely"
        ) from None
    identity = (opened.st_dev, opened.st_ino)
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(after.st_mode)
        or (before.st_dev, before.st_ino) != identity
        or (after.st_dev, after.st_ino) != identity
        or stat.S_IMODE(opened.st_mode) != 0o700
        or opened.st_uid != os.geteuid()
    ):
        os.close(descriptor)
        raise IntegrationValidationError(
            "integration temp root owner, mode, or identity is unsafe"
        )
    _check_control(deadline=deadline, cancel_event=cancel_event)
    return _PreparedTempRoot(
        path=Path(os.fsdecode(absolute)),
        raw_path=absolute,
        descriptor=descriptor,
        identity=identity,
    )


def _verify_temp_root(
    root: _PreparedTempRoot,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> None:
    _check_control(deadline=deadline, cancel_event=cancel_event)
    try:
        canonical = os.path.realpath(root.raw_path)
        current = os.stat(root.raw_path, follow_symlinks=False)
        opened = os.fstat(root.descriptor)
    except OSError:
        raise IntegrationValidationError(
            "integration temp root identity changed"
        ) from None
    if (
        canonical != root.raw_path
        or not stat.S_ISDIR(current.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (current.st_dev, current.st_ino) != root.identity
        or (opened.st_dev, opened.st_ino) != root.identity
        or stat.S_IMODE(opened.st_mode) != 0o700
        or opened.st_uid != os.geteuid()
    ):
        raise IntegrationValidationError(
            "integration temp root alias or identity changed"
        )


def _create_owned_temp_attempt(
    root: _PreparedTempRoot,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> _OwnedTempAttempt:
    _verify_temp_root(root, deadline=deadline, cancel_event=cancel_event)
    for _attempt in range(32):
        _check_control(deadline=deadline, cancel_event=cancel_event)
        leaf = f"bestplan-integration-{secrets.token_hex(16)}"
        try:
            os.mkdir(leaf, 0o700, dir_fd=root.descriptor)
        except FileExistsError:
            continue
        except OSError:
            raise IntegrationValidationError(
                "integration temp attempt could not be created safely"
            ) from None
        try:
            child = os.stat(
                leaf, dir_fd=root.descriptor, follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(child.st_mode)
                or stat.S_IMODE(child.st_mode) != 0o700
                or child.st_uid != os.geteuid()
            ):
                raise IntegrationValidationError(
                    "integration temp attempt identity is unsafe"
                )
            _verify_temp_root(
                root, deadline=deadline, cancel_event=cancel_event,
            )
        except BaseException:
            try:
                os.rmdir(leaf, dir_fd=root.descriptor)
            except BaseException:
                pass
            raise
        return _OwnedTempAttempt(
            root=root,
            leaf=leaf,
            path=root.path / leaf,
            identity=(child.st_dev, child.st_ino),
        )
    raise IntegrationValidationError(
        "integration temp attempt name could not be reserved"
    )


def _cleanup_owned_temp_attempt(
    attempt: _OwnedTempAttempt,
    *,
    deadline: float,
    cancel_event: threading.Event | None = None,
) -> None:
    descriptor = -1
    try:
        _check_control(deadline=deadline, cancel_event=cancel_event)
        current = os.stat(
            attempt.leaf,
            dir_fd=attempt.root.descriptor,
            follow_symlinks=False,
        )
        descriptor = os.open(
            attempt.leaf,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=attempt.root.descriptor,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(current.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (current.st_dev, current.st_ino) != attempt.identity
            or (opened.st_dev, opened.st_ino) != attempt.identity
        ):
            raise PromotionError(
                "integration temp attempt identity changed; evidence retained"
            )
        _call_source_with_control(
            source_boundary._remove_owned_tree_contents,
            descriptor,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        _check_control(deadline=deadline, cancel_event=cancel_event)
        current = os.stat(
            attempt.leaf,
            dir_fd=attempt.root.descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != attempt.identity
        ):
            raise PromotionError(
                "integration temp attempt identity changed; evidence retained"
            )
        os.rmdir(attempt.leaf, dir_fd=attempt.root.descriptor)
    except FileNotFoundError:
        return
    except PromotionError:
        raise
    except BaseException:
        raise PromotionError(
            "integration temp attempt cleanup failed; evidence retained"
        ) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _source_accepts_keyword(operation: object, name: str) -> bool:
    try:
        parameters = inspect.signature(operation).parameters
    except (TypeError, ValueError):
        return False
    parameter = parameters.get(name)
    if parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }:
        return True
    return any(
        item.kind is inspect.Parameter.VAR_KEYWORD
        for item in parameters.values()
    )


def _source_explicitly_accepts_keyword(operation: object, name: str) -> bool:
    try:
        parameter = inspect.signature(operation).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


def _call_source_with_control(
    operation: object,
    *args: object,
    deadline: float,
    cancel_event: threading.Event | None,
    **kwargs: object,
) -> object:
    accepts_cancel = _source_explicitly_accepts_keyword(
        operation, "cancel_event",
    )
    if _source_accepts_keyword(operation, "deadline"):
        kwargs["deadline"] = (
            deadline
            if accepts_cancel or cancel_event is None
            else _ControlledDeadline(deadline, cancel_event)
        )
    if accepts_cancel:
        kwargs["cancel_event"] = cancel_event
    return operation(*args, **kwargs)  # type: ignore[operator]


def _cleanup_materialization_staging(
    prepared: object,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> None:
    operation = source_boundary._cleanup_owned_staging
    if _source_explicitly_accepts_keyword(operation, "deadline"):
        _call_source_with_control(
            operation,
            prepared,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        return

    root_fd = prepared.root_fd
    parent_fd = prepared.parent_fds[-1]
    opened = os.fstat(root_fd)
    current = os.stat(
        prepared.staging_leaf,
        dir_fd=parent_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != prepared.root_identity
        or (current.st_dev, current.st_ino) != prepared.root_identity
    ):
        raise PromotionError(
            "integration staging identity changed; evidence retained"
        )
    _call_source_with_control(
        source_boundary._remove_owned_tree_contents,
        root_fd,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    _check_control(deadline=deadline, cancel_event=cancel_event)
    current = os.stat(
        prepared.staging_leaf,
        dir_fd=parent_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != prepared.root_identity
    ):
        raise PromotionError(
            "integration staging identity changed; evidence retained"
        )
    os.rmdir(prepared.staging_leaf, dir_fd=parent_fd)


def _quarantine_materialized_destination(
    prepared: object,
    entries: object,
    witnesses: object,
    *,
    backend: str,
    object_format: str,
    deadline: float,
    cancel_event: threading.Event | None,
) -> tuple[str, bool]:
    operation = source_boundary._quarantine_owned_published
    if _source_explicitly_accepts_keyword(operation, "deadline"):
        result = _call_source_with_control(
            operation,
            prepared,
            entries,
            witnesses,
            backend=backend,
            object_format=object_format,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        assert isinstance(result, tuple)
        return result

    parent_fd = prepared.parent_fds[-1]
    opened = os.fstat(prepared.root_fd)
    current = os.stat(
        prepared.final_leaf,
        dir_fd=parent_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != prepared.root_identity
        or (current.st_dev, current.st_ino) != prepared.root_identity
    ):
        raise PromotionError(
            "integration destination identity changed; cleanup quarantined"
        )
    for _attempt in range(16):
        quarantine_leaf = (
            b"."
            + prepared.final_leaf[:64]
            + b".bestplan-quarantine-"
            + secrets.token_hex(16).encode("ascii")
        )
        try:
            source_boundary._rename_leaf_no_replace(
                parent_fd,
                prepared.final_leaf,
                quarantine_leaf,
                backend=backend,
            )
        except FileExistsError:
            continue
        break
    else:
        raise PromotionError(
            "integration quarantine name could not be reserved"
        )
    quarantined = os.stat(
        quarantine_leaf,
        dir_fd=parent_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(quarantined.st_mode)
        or (quarantined.st_dev, quarantined.st_ino) != prepared.root_identity
    ):
        raise PromotionError("integration quarantine identity changed")
    quarantine_path = os.path.join(
        prepared.canonical_parent, quarantine_leaf,
    )
    try:
        _check_control(deadline=deadline, cancel_event=cancel_event)
        unchanged = _call_source_with_control(
            source_boundary._quarantined_tree_matches_export,
            prepared,
            entries,
            witnesses,
            quarantine_leaf=quarantine_leaf,
            object_format=object_format,
            deadline=deadline,
            cancel_event=cancel_event,
        )
    except PromotionError:
        unchanged = False
    return os.fsdecode(quarantine_path), bool(unchanged)


def _slice_prefix(item: ExecutionSlice, snapshot: SourceSnapshot) -> str:
    workspace = item.workspace
    if not workspace or workspace == ".":
        return ""
    path = Path(workspace)
    if path.is_absolute():
        try:
            relative = path.resolve(strict=False).relative_to(Path(snapshot.repo.worktree))
        except (OSError, ValueError):
            raise IntegrationValidationError("integration slice workspace escapes the repository") from None
        text = relative.as_posix()
        return "" if text == "." else text
    normalized = PurePosixPath(workspace)
    if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
        raise IntegrationValidationError("integration slice workspace is unsafe")
    return normalized.as_posix()


def _manifest_path(prefix: str, value: str, label: str) -> bytes:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise IntegrationValidationError(f"integration {label} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise IntegrationValidationError(f"integration {label} path is unsafe")
    joined = PurePosixPath(prefix).joinpath(path) if prefix else path
    raw = joined.as_posix().encode("utf-8")
    try:
        validate_raw_candidate_paths((raw,))
    except BaseException:
        raise IntegrationValidationError(f"integration {label} path is unsafe") from None
    return raw


def _leased(path: bytes, leases: Sequence[bytes]) -> bool:
    return any(path == lease or path.startswith(lease + b"/") for lease in leases)


def _pinned_contract_inputs(
    contract: Mapping[str, Any],
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> tuple[bytes, ...]:
    paths: set[bytes] = set()

    def visit(value: object) -> None:
        _check_control(deadline=deadline, cancel_event=cancel_event)
        if isinstance(value, Mapping):
            inputs = value.get("inputs")
            if isinstance(inputs, list):
                for item in inputs:
                    if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                        paths.add(_manifest_path("", item["path"], "pinned input"))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(contract)
    return tuple(sorted(paths))


@dataclass(frozen=True)
class _ValidatedTask6Contract:
    normalized: dict[str, Any]
    schema: str
    mode: str
    repository: EnrolledRepository
    source: dict[str, Any]
    commands: tuple[BoundCommand, ...]
    controller: ControllerIdentity
    contract_digest: str
    manifest_digest: str | None
    check_runtime_digest: str | None


def _validate_task6_contract(
    contract: Mapping[str, Any],
) -> _ValidatedTask6Contract:
    """Dispatch exact legacy/local schemas into one immutable Task 6 view."""

    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    if schema == CONTRACT_SCHEMA:
        normalized = validate_execution_contract(contract)
        if normalized["promotion_mode"] != "auto_live":
            raise ContractValidationError(
                "Task 6 legacy contract requires auto_live approval"
            )
        mode = normalized["promotion_mode"]
        digest = compute_contract_digest(normalized)
        manifest_digest = None
        check_runtime_digest = None
    else:
        from agent.bestplan_local import (
            LOCAL_GO_CONTRACT_SCHEMA,
            local_go_contract_digest,
            validate_local_go_contract,
        )

        if schema != LOCAL_GO_CONTRACT_SCHEMA:
            raise ContractValidationError("Task 6 contract schema is unsupported")
        normalized = validate_local_go_contract(contract)
        mode = normalized["mode"]
        digest = local_go_contract_digest(normalized)
        manifest_digest = normalized["manifest_digest"]
        check_runtime_digest = normalized["check_runtime_digest"]

    repository = _repository_from_dict(
        normalized["repository"], "Task 6 contract.repository",
    )
    controller = _controller_from_dict(
        normalized["controller"], "Task 6 contract.controller",
    )
    commands = tuple(
        _command_from_dict(item, f"Task 6 contract.commands[{index}]")
        for index, item in enumerate(normalized["commands"])
    )
    return _ValidatedTask6Contract(
        normalized=normalized,
        schema=schema,
        mode=mode,
        repository=repository,
        source=dict(normalized["source"]),
        commands=commands,
        controller=controller,
        contract_digest=digest,
        manifest_digest=manifest_digest,
        check_runtime_digest=check_runtime_digest,
    )


def _task6_approval_digest(
    manifest: Mapping[str, Any],
    contract: _ValidatedTask6Contract,
) -> str:
    if contract.schema == CONTRACT_SCHEMA:
        return compute_approval_digest(manifest, contract.normalized)

    from agent.bestplan_local import local_go_contract_json

    manifest_json = canonical_json(manifest).encode("utf-8")
    manifest_digest = hashlib.sha256(manifest_json).hexdigest()
    if contract.manifest_digest != manifest_digest:
        raise ContractValidationError(
            "local-go contract manifest digest differs from the approved plan"
        )
    return hashlib.sha256(
        b"hermes.bestplan.local-go-approval.v1\0"
        + manifest_json
        + b"\0"
        + local_go_contract_json(contract.normalized).encode("utf-8")
    ).hexdigest()


def _validate_contract_and_bindings(
    *,
    plan_id: str,
    plan: ExecutionPlan,
    snapshot: SourceSnapshot,
    contract: Mapping[str, Any],
    approved: str,
    candidates: Sequence[CandidateIntegrationBinding],
    candidate_base_oid: str | None = None,
    identity_plan_id: str | None = None,
    allow_partial: bool = False,
) -> tuple[dict[str, Any], str, str, dict[str, CandidateIntegrationBinding]]:
    if not isinstance(plan, ExecutionPlan) or not isinstance(snapshot, SourceSnapshot):
        raise IntegrationValidationError("integration plan/source input is invalid")
    if (
        not isinstance(plan_id, str)
        or not plan_id
        or "\x00" in plan_id
        or len(plan_id.encode("utf-8")) > 1024
    ):
        raise IntegrationValidationError("integration plan id is invalid")
    try:
        validated = _validate_task6_contract(contract)
        expected_approval = _task6_approval_digest(
            plan.to_manifest(), validated,
        )
    except ContractValidationError as exc:
        raise IntegrationValidationError(
            f"integration contract is invalid: {exc}"
        ) from None
    normalized = validated.normalized
    contract_sha = validated.contract_digest
    snapshot_sha = source_snapshot_digest(snapshot)
    if not _is_sha256(approved) or expected_approval != approved:
        raise IntegrationValidationError("integration approval digest differs")
    source = validated.source
    if (
        source["base_oid"] != snapshot.head_oid
        or source["tree_oid"] != snapshot.tree_oid
        or source["snapshot_digest"] != snapshot_sha
        or source["source_digest"] != snapshot.fingerprint
        or source["protected_digest"] != snapshot.protected_manifest.digest
    ):
        raise IntegrationValidationError("integration source contract differs")
    if not validated.repository.matches(snapshot.repo):
        raise IntegrationValidationError("integration repository contract differs")
    if any(item.depends_on for item in plan.slices):
        raise IntegrationValidationError("integration dependency manifests are unsupported")
    if not candidates or (
        not allow_partial and len(candidates) != len(plan.slices)
    ):
        raise IntegrationValidationError("integration candidate set is incomplete")
    by_manifest: dict[str, CandidateIntegrationBinding] = {}
    controller = validated.controller
    expected_candidate_base = (
        snapshot.head_oid
        if candidate_base_oid is None
        else candidate_base_oid
    )
    effective_identity_plan_id = identity_plan_id or plan_id
    for binding in candidates:
        if not isinstance(binding, CandidateIntegrationBinding):
            raise IntegrationValidationError("integration candidate binding is invalid")
        if binding.manifest_slice_id in by_manifest:
            raise IntegrationValidationError("integration candidate binding is duplicated")
        if (
            binding.base_oid != expected_candidate_base
            or binding.approval_digest != approved
            or binding.contract_digest != contract_sha
            or binding.source_snapshot_digest != snapshot_sha
            or binding.controller_id != controller.controller_id
            or binding.controller_repository_id != controller.repository_id
            or binding.controller_release_oid != controller.release_oid
            or binding.controller_artifact_sha256 != controller.artifact_sha256
        ):
            raise IntegrationValidationError("integration candidate binding differs")
        by_manifest[binding.manifest_slice_id] = binding
    manifest_ids = {item.id for item in plan.slices}
    if not set(by_manifest).issubset(manifest_ids) or (
        not allow_partial and set(by_manifest) != manifest_ids
    ):
        raise IntegrationValidationError("integration candidate set differs from the manifest")
    candidate_plan_id = _candidate_plan_id(effective_identity_plan_id)
    for index, manifest_slice in enumerate(plan.slices):
        if manifest_slice.id not in by_manifest:
            continue
        binding = by_manifest[manifest_slice.id]
        expected_slice_id = _safe_identifier(
            "slice", effective_identity_plan_id, index, manifest_slice.id,
        )
        expected_candidate_id = _safe_identifier(
            "candidate", effective_identity_plan_id, index, manifest_slice.id,
        )
        expected_attempt_id = _safe_identifier(
            "attempt", effective_identity_plan_id, index, manifest_slice.id,
        )
        if (
            not _binding_slice_matches_manifest(
            plan_id=effective_identity_plan_id,
            manifest_index=index,
            manifest_slice_id=manifest_slice.id,
            slice_id=binding.slice_id,
            )
            or binding.slice_id != expected_slice_id
            or binding.candidate_id != expected_candidate_id
            or binding.attempt_id != expected_attempt_id
        ):
            raise IntegrationValidationError(
                "integration Task 5 slice/candidate/attempt identity differs "
                "from the manifest"
            )
        try:
            expected_ref = candidate_ref_name(
                candidate_plan_id, binding.slice_id, binding.attempt_id,
            )
        except BaseException:
            raise IntegrationValidationError(
                "integration candidate ref identity is invalid"
            ) from None
        if binding.ref_name != expected_ref:
            raise IntegrationValidationError(
                "integration candidate ref identity differs"
            )
    return normalized, contract_sha, snapshot_sha, by_manifest


def _write_tree_from_map(
    repo: RepoIdentity,
    tree: Mapping[bytes, _GitEntry],
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> str:
    """Write a flat validated tree map without any pathname-addressed index."""

    root: dict[bytes, object] = {}
    for path, entry in sorted(tree.items()):
        _check_control(deadline=deadline, cancel_event=cancel_event)
        parts = path.split(b"/")
        if (
            not parts
            or any(part in {b"", b".", b".."} or b"\0" in part for part in parts)
            or entry.object_type != "blob"
            or entry.mode not in {0o100644, 0o100755, 0o120000}
            or not _is_oid(entry.oid, repo.object_format)
        ):
            raise IntegrationValidationError(
                "integration composed tree contains an unsafe entry"
            )
        node = root
        for component in parts[:-1]:
            existing = node.get(component)
            if isinstance(existing, _GitEntry):
                raise IntegrationValidationError(
                    "integration composed tree has a file/directory conflict"
                )
            if existing is None:
                existing = {}
                node[component] = existing
            if not isinstance(existing, dict):
                raise IntegrationValidationError(
                    "integration composed tree structure is malformed"
                )
            node = existing
        prior = node.get(parts[-1])
        if prior is not None:
            raise IntegrationValidationError(
                "integration composed tree has a path conflict"
            )
        node[parts[-1]] = entry

    written: dict[int, str] = {}
    stack: list[tuple[dict[bytes, object], bool]] = [(root, False)]
    while stack:
        _check_control(deadline=deadline, cancel_event=cancel_event)
        node, expanded = stack.pop()
        if not expanded:
            stack.append((node, True))
            children = [
                child for child in node.values() if isinstance(child, dict)
            ]
            stack.extend((child, False) for child in reversed(children))
            continue
        records: list[bytes] = []
        for name, value in sorted(node.items()):
            _check_control(deadline=deadline, cancel_event=cancel_event)
            if isinstance(value, dict):
                oid = written.get(id(value))
                if oid is None:
                    raise IntegrationProofStale(
                        "integration composed subtree was not written"
                    )
                records.append(b"040000 tree " + oid.encode("ascii") + b"\t" + name + b"\0")
            elif isinstance(value, _GitEntry):
                records.append(
                    f"{value.mode:06o} {value.object_type} {value.oid}".encode(
                        "ascii"
                    )
                    + b"\t"
                    + name
                    + b"\0"
                )
            else:
                raise IntegrationValidationError(
                    "integration composed tree structure is malformed"
                )
        result = _run_git(
            repo,
            "mktree",
            "-z",
            input_bytes=b"".join(records),
            deadline=deadline,
            cancel_event=cancel_event,
        )
        raw_oid = result.stdout
        if not raw_oid.endswith(b"\n") or raw_oid.count(b"\n") != 1:
            raise IntegrationProofStale(
                "integration written tree identity is malformed"
            )
        try:
            oid = raw_oid[:-1].decode("ascii")
        except UnicodeError:
            raise IntegrationProofStale(
                "integration written tree identity is malformed"
            ) from None
        if not _is_oid(oid, repo.object_format):
            raise IntegrationProofStale(
                "integration written tree identity is malformed"
            )
        written[id(node)] = oid
    return written[id(root)]


def _write_tree_from_delta(
    repo: RepoIdentity,
    *,
    base_tree: Mapping[bytes, _GitEntry],
    target_tree: Mapping[bytes, _GitEntry],
    changed: Sequence[bytes],
    deadline: float,
    cancel_event: threading.Event | None,
) -> str:
    reconstructed = dict(base_tree)
    for path in changed:
        _check_control(deadline=deadline, cancel_event=cancel_event)
        entry = target_tree.get(path)
        if entry is None:
            reconstructed.pop(path, None)
        else:
            reconstructed[path] = entry
    return _write_tree_from_map(
        repo,
        reconstructed,
        deadline=deadline,
        cancel_event=cancel_event,
    )


def _integration_ref(plan_id: str, approved: str) -> str:
    if not isinstance(plan_id, str) or not plan_id or "\x00" in plan_id or len(plan_id.encode("utf-8")) > 1024:
        raise IntegrationValidationError("integration plan id is invalid")
    plan_hash = hashlib.sha256(plan_id.encode("utf-8")).hexdigest()
    return f"refs/hermes-bestplan-integrations/{plan_hash}/{approved}"


def _integration_commit_message(
    *,
    plan_id: str,
    approved: str,
    contract_sha: str,
    snapshot_sha: str,
    candidates: Sequence[CandidateIntegrationBinding | AppliedCandidate],
) -> bytes:
    return (
        "Hermes BestPlan integration\n\n"
        f"plan_sha256={hashlib.sha256(plan_id.encode('utf-8')).hexdigest()}\n"
        f"approval={approved}\n"
        f"contract={contract_sha}\n"
        f"source={snapshot_sha}\n"
        + "".join(
            (
                f"candidate[{index}].commit={item.commit_oid}\n"
                f"candidate[{index}].binding={item.binding_digest}\n"
                f"candidate[{index}].policy={item.policy_digest}\n"
                f"candidate[{index}].receipt={item.candidate_receipt_digest}\n"
            )
            for index, item in enumerate(candidates)
        )
    ).encode("utf-8")


def _write_integration_commit(
    repo: RepoIdentity,
    *,
    tree_oid: str,
    parent_oid: str,
    plan_id: str,
    approved: str,
    contract_sha: str,
    snapshot_sha: str,
    candidates: Sequence[AppliedCandidate],
    deadline: float,
    cancel_event: threading.Event | None,
) -> str:
    message = _integration_commit_message(
        plan_id=plan_id,
        approved=approved,
        contract_sha=contract_sha,
        snapshot_sha=snapshot_sha,
        candidates=candidates,
    )
    environment = {
        "GIT_AUTHOR_NAME": "Hermes BestPlan",
        "GIT_AUTHOR_EMAIL": "bestplan@localhost",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_NAME": "Hermes BestPlan",
        "GIT_COMMITTER_EMAIL": "bestplan@localhost",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    return _run_git(
        repo,
        "commit-tree",
        tree_oid,
        "-p",
        parent_oid,
        input_bytes=message,
        extra_environment=environment,
        deadline=deadline,
        cancel_event=cancel_event,
    ).stdout.strip().decode("ascii")


def _is_owned_integration_commit(
    repo: RepoIdentity,
    commit_oid: str,
    *,
    expected_message: bytes,
    deadline: float,
    cancel_event: threading.Event | None,
) -> bool:
    raw = _run_git(
        repo,
        "cat-file",
        "commit",
        commit_oid,
        deadline=deadline,
        cancel_event=cancel_event,
    ).stdout
    try:
        _headers, message = raw.split(b"\n\n", 1)
    except ValueError:
        return False
    return message == expected_message


def _anchor_integration(
    repo: RepoIdentity,
    *,
    target_ref: str,
    target_oid: str,
    candidate_refs: Sequence[tuple[str, str]],
    integration_ref: str,
    integration_oid: str,
    existing_oid: str | None,
    deadline: float,
    cancel_event: threading.Event | None,
) -> None:
    records = [f"verify {target_ref}".encode() + b"\0" + target_oid.encode() + b"\0"]
    records.extend(
        f"verify {ref}".encode() + b"\0" + oid.encode() + b"\0"
        for ref, oid in candidate_refs
    )
    if existing_oid is None:
        records.append(
            f"create {integration_ref}".encode() + b"\0" + integration_oid.encode() + b"\0"
        )
    else:
        records.append(
            f"verify {integration_ref}".encode() + b"\0" + integration_oid.encode() + b"\0"
        )
    try:
        _run_git(
            repo,
            "update-ref",
            "--stdin",
            "-z",
            input_bytes=b"".join(records),
            deadline=deadline,
            cancel_event=cancel_event,
        )
    except _GitFailure:
        current_integration = _read_ref(
            repo, integration_ref, deadline=deadline, cancel_event=cancel_event,
        )
        if current_integration not in {None, integration_oid}:
            raise IntegrationRefConflict("integration reference already names different evidence") from None
        current_target = _read_ref(
            repo, target_ref, deadline=deadline, cancel_event=cancel_event,
        )
        if current_target != target_oid:
            raise IntegrationProofStale("integration target changed during anchoring") from None
        for ref_name, commit_oid in candidate_refs:
            if _read_ref(repo, ref_name, deadline=deadline, cancel_event=cancel_event) != commit_oid:
                raise IntegrationProofStale("integration candidate ref changed during anchoring") from None
        raise PromotionError("integration reference transaction failed") from None


def _receipt_digest(integration: FrozenIntegration) -> str:
    body = {
        "plan_id": integration.plan_id,
        "approval_digest": integration.approval_digest,
        "contract_digest": integration.contract_digest,
        "source_snapshot_digest": integration.source_snapshot_digest,
        "target_ref": integration.target_ref,
        "target_oid": integration.target_oid,
        "integration_oid": integration.integration_oid,
        "tree_oid": integration.tree_oid,
        "ref_name": integration.ref_name,
        "candidates": [
            {
                "manifest_index": item.manifest_index,
                "manifest_slice_id": item.manifest_slice_id,
                "slice_id": item.slice_id,
                "candidate_id": item.candidate_id,
                "attempt_id": item.attempt_id,
                "ref_name": item.ref_name,
                "commit_oid": item.commit_oid,
                "tree_oid": item.tree_oid,
                "policy_digest": item.policy_digest,
                "candidate_receipt_digest": item.candidate_receipt_digest,
                "binding_digest": item.binding_digest,
                "changed_paths_sha256": item.changed_paths_sha256,
                "artifact_digests": [list(pair) for pair in item.artifact_digests],
            }
            for item in integration.candidates
        ],
    }
    return _domain_digest(b"hermes.bestplan.frozen-integration.v1", body)


def freeze_integration(
    *,
    plan_id: str,
    plan: ExecutionPlan,
    snapshot: SourceSnapshot,
    contract: Mapping[str, Any],
    approval_digest: str,
    candidates: Sequence[CandidateIntegrationBinding],
    temp_root: str | Path,
    deadline: float,
    cancel_event: threading.Event | None = None,
    identity_plan_id: str | None = None,
    _generation: int = 0,
    _prior: FrozenIntegration | None = None,
) -> FrozenIntegration:
    """Freeze one exact, single-parent integration commit without moving main."""

    absolute_deadline = _validated_deadline(deadline)
    cancel_event = _validated_cancel_event(cancel_event)
    _check_control(
        deadline=absolute_deadline, cancel_event=cancel_event,
    )
    if (
        isinstance(_generation, bool)
        or not isinstance(_generation, int)
        or _generation < 0
        or (_generation == 0) != (_prior is None)
        or (_prior is not None and not isinstance(_prior, FrozenIntegration))
    ):
        raise IntegrationValidationError(
            "integration repair generation is invalid"
        )
    if identity_plan_id is not None and _prior is not None:
        raise IntegrationValidationError(
            "integration attempt identity is initial-generation only"
        )
    effective_identity_plan_id = (
        identity_plan_id or plan_id
        if _prior is None
        else _safe_identifier("repair-plan", plan_id, _generation)
    )
    candidate_base_oid = (
        snapshot.head_oid if _prior is None else _prior.integration_oid
    )
    normalized, contract_sha, snapshot_sha, by_manifest = _validate_contract_and_bindings(
        plan_id=plan_id,
        plan=plan,
        snapshot=snapshot,
        contract=contract,
        approved=approval_digest,
        candidates=tuple(candidates),
        candidate_base_oid=candidate_base_oid,
        identity_plan_id=effective_identity_plan_id,
        allow_partial=_prior is not None,
    )
    _assert_repo_identity(snapshot.repo, deadline=absolute_deadline)
    assert_supported_repository(snapshot.repo, deadline=absolute_deadline)
    try:
        current_capture = capture_source_snapshot(snapshot.repo, absolute_deadline)
    except BaseException:
        raise IntegrationProofStale("integration current target could not be captured") from None
    target_ref = normalized["source"]["local_ref"]
    target_oid = _read_ref(
        snapshot.repo, target_ref, deadline=absolute_deadline, cancel_event=cancel_event,
    )
    if target_oid is None:
        raise IntegrationProofStale("integration target ref is missing")
    if _prior is not None:
        if (
            not isinstance(_prior, FrozenIntegration)
            or _receipt_digest(FrozenIntegration(
                **{**_prior.__dict__, "receipt_digest": ""}
            )) != _prior.receipt_digest
            or _prior.plan_id != plan_id
            or _prior.approval_digest != approval_digest
            or _prior.contract_digest != contract_sha
            or _prior.source_snapshot_digest != snapshot_sha
            or _prior.target_ref != target_ref
            or _prior.target_oid != target_oid
        ):
            raise IntegrationValidationError(
                "integration repair base differs from the approved target"
            )
        if _read_ref(
            snapshot.repo,
            _prior.ref_name,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        ) != _prior.integration_oid:
            raise IntegrationProofStale("integration repair base ref changed")
        prior_tree_oid, prior_parents = _commit_proof(
            snapshot.repo,
            _prior.integration_oid,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        if (
            prior_tree_oid != _prior.tree_oid
            or prior_parents != (target_oid,)
        ):
            raise IntegrationProofStale("integration repair base commit differs")
    existing_ref = _integration_ref(effective_identity_plan_id, approval_digest)
    existing_oid = _read_ref(
        snapshot.repo, existing_ref, deadline=absolute_deadline, cancel_event=cancel_event,
    )
    if existing_oid is not None:
        _existing_tree, existing_parents = _commit_proof(
            snapshot.repo, existing_oid, deadline=absolute_deadline, cancel_event=cancel_event,
        )
        if existing_parents != (target_oid,):
            expected_message = _integration_commit_message(
                plan_id=effective_identity_plan_id,
                approved=approval_digest,
                contract_sha=contract_sha,
                snapshot_sha=snapshot_sha,
                candidates=tuple(
                    by_manifest[item.id]
                    for item in plan.slices
                    if item.id in by_manifest
                ),
            )
            if not _is_owned_integration_commit(
                snapshot.repo,
                existing_oid,
                expected_message=expected_message,
                deadline=absolute_deadline,
                cancel_event=cancel_event,
            ):
                raise IntegrationRefConflict(
                    "integration reference already names unrelated evidence"
                )
            raise IntegrationProofStale(
                "integration target changed after the frozen commit"
            )
    ancestry = _run_git(
        snapshot.repo,
        "merge-base",
        "--is-ancestor",
        snapshot.head_oid,
        target_oid,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
        check=False,
    )
    if ancestry.returncode != 0:
        raise IntegrationProofStale("integration target does not descend from the admitted base")
    candidate_base_tree_oid = (
        snapshot.tree_oid if _prior is None else _prior.tree_oid
    )
    base_tree = _tree_map(
        snapshot.repo,
        candidate_base_tree_oid,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    target_tree_oid, _target_parents = _commit_proof(
        snapshot.repo, target_oid, deadline=absolute_deadline, cancel_event=cancel_event,
    )
    target_tree = _tree_map(
        snapshot.repo, target_tree_oid, deadline=absolute_deadline, cancel_event=cancel_event,
    )
    target_change_base = (
        base_tree
        if _prior is None
        else _tree_map(
            snapshot.repo,
            snapshot.tree_oid,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
    )
    target_changes = _changed_paths(
        target_change_base,
        target_tree,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    pinned_inputs = _pinned_contract_inputs(
        normalized,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    prepared_root = _prepare_temp_root(
        temp_root,
        repo=snapshot.repo,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    owned: _OwnedTempAttempt | None = None
    applied_paths: list[bytes] = []
    current_tree = dict(target_tree if _prior is None else base_tree)
    applied: list[AppliedCandidate] = []
    candidate_refs: list[tuple[str, str]] = []
    expected_artifacts: list[tuple[bytes, str]] = []
    try:
        owned = _create_owned_temp_attempt(
            prepared_root,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        for index, manifest_slice in enumerate(plan.slices):
            if cancel_event is not None and cancel_event.is_set():
                raise PromotionError("integration cancelled")
            if manifest_slice.id not in by_manifest:
                continue
            binding = by_manifest[manifest_slice.id]
            if _read_ref(
                snapshot.repo,
                binding.ref_name,
                deadline=absolute_deadline,
                cancel_event=cancel_event,
            ) != binding.commit_oid:
                raise IntegrationProofStale("integration candidate ref changed")
            candidate_tree_oid, parents = _commit_proof(
                snapshot.repo,
                binding.commit_oid,
                deadline=absolute_deadline,
                cancel_event=cancel_event,
            )
            if parents != (candidate_base_oid,):
                raise IntegrationProofStale("integration candidate ancestry or parent differs")
            if candidate_tree_oid != binding.tree_oid:
                raise IntegrationProofStale("integration candidate tree differs")
            candidate_tree = _tree_map(
                snapshot.repo,
                candidate_tree_oid,
                deadline=absolute_deadline,
                cancel_event=cancel_event,
            )
            changed = _changed_paths(
                base_tree,
                candidate_tree,
                deadline=absolute_deadline,
                cancel_event=cancel_event,
            )
            if changed != binding.changed_paths:
                raise IntegrationProofStale("integration candidate changed paths differ")
            prefix = _slice_prefix(manifest_slice, snapshot)
            leases = tuple(
                _manifest_path(prefix, item, "lease")
                for item in manifest_slice.allowed_paths
            )
            if manifest_slice.read_only and changed:
                raise IntegrationValidationError("integration read-only candidate changed paths")
            if any(not _leased(path, leases) for path in changed):
                raise IntegrationValidationError("integration candidate changed a path outside its lease")
            for changed_path in changed:
                if any(_path_related(changed_path, input_path) for input_path in pinned_inputs):
                    raise IntegrationValidationError("integration candidate changed a pinned input")
                if any(_path_related(changed_path, target_path) for target_path in target_changes):
                    raise IntegrationConflictError("integration target/candidate path conflict")
                if any(_path_related(changed_path, prior) for prior in applied_paths):
                    raise IntegrationConflictError("integration candidate path conflict")
                if current_tree.get(changed_path) != base_tree.get(changed_path):
                    raise IntegrationConflictError("integration candidate content conflict")
            reconstructed = _write_tree_from_delta(
                snapshot.repo,
                base_tree=base_tree,
                target_tree=candidate_tree,
                changed=changed,
                deadline=absolute_deadline,
                cancel_event=cancel_event,
            )
            if reconstructed != candidate_tree_oid:
                raise IntegrationProofStale("integration candidate tree cannot be reconstructed")
            artifact_digests: list[tuple[str, str]] = []
            for artifact in manifest_slice.expected_artifacts:
                raw_artifact = _manifest_path(prefix, artifact, "artifact")
                entry = candidate_tree.get(raw_artifact)
                if entry is None or entry.mode not in {0o100644, 0o100755}:
                    raise IntegrationValidationError("integration expected artifact is missing or nonregular")
                artifact_digests.append((
                    raw_artifact.decode("utf-8"),
                    _read_blob_sha256(
                        snapshot.repo,
                        entry.oid,
                        deadline=absolute_deadline,
                        cancel_event=cancel_event,
                    ),
                ))
                expected_artifacts.append(
                    (raw_artifact, artifact_digests[-1][1])
                )
            for path in changed:
                entry = candidate_tree.get(path)
                if entry is None:
                    current_tree.pop(path, None)
                else:
                    current_tree[path] = entry
            changed_digest = _domain_digest(
                b"hermes.bestplan.integration-changed-paths.v1",
                [path.hex() for path in changed],
            )
            applied.append(AppliedCandidate(
                manifest_index=index,
                manifest_slice_id=manifest_slice.id,
                slice_id=binding.slice_id,
                candidate_id=binding.candidate_id,
                attempt_id=binding.attempt_id,
                ref_name=binding.ref_name,
                commit_oid=binding.commit_oid,
                tree_oid=binding.tree_oid,
                policy_digest=binding.policy_digest,
                candidate_receipt_digest=binding.candidate_receipt_digest,
                binding_digest=binding.binding_digest,
                changed_paths_sha256=changed_digest,
                artifact_digests=tuple(artifact_digests),
            ))
            applied_paths.extend(changed)
            candidate_refs.append((binding.ref_name, binding.commit_oid))
        _assert_composed_tree_has_no_component_aliases(
            current_tree,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        for artifact_path, expected_sha256 in expected_artifacts:
            final_entry = current_tree.get(artifact_path)
            if final_entry is None or final_entry.mode not in {0o100644, 0o100755}:
                raise IntegrationConflictError(
                    "integration expected artifact is missing from the final tree"
                )
            final_sha256 = _read_blob_sha256(
                snapshot.repo,
                final_entry.oid,
                deadline=absolute_deadline,
                cancel_event=cancel_event,
            )
            if final_sha256 != expected_sha256:
                raise IntegrationConflictError(
                    "integration expected artifact differs in the final tree"
                )
        integration_tree_oid = _write_tree_from_map(
            snapshot.repo,
            current_tree,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        written_tree = _tree_map(
            snapshot.repo,
            integration_tree_oid,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        if written_tree != current_tree:
            raise IntegrationProofStale(
                "integration written tree differs from the composed tree"
            )
        integration_oid = _write_integration_commit(
            snapshot.repo,
            tree_oid=integration_tree_oid,
            parent_oid=target_oid,
            plan_id=effective_identity_plan_id,
            approved=approval_digest,
            contract_sha=contract_sha,
            snapshot_sha=snapshot_sha,
            candidates=applied,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        if existing_oid is not None and existing_oid != integration_oid:
            raise IntegrationRefConflict("integration reference already names a different commit")
        _anchor_integration(
            snapshot.repo,
            target_ref=target_ref,
            target_oid=target_oid,
            candidate_refs=candidate_refs,
            integration_ref=existing_ref,
            integration_oid=integration_oid,
            existing_oid=existing_oid,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        final_tree, final_parents = _commit_proof(
            snapshot.repo,
            integration_oid,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        if final_tree != integration_tree_oid or final_parents != (target_oid,):
            raise IntegrationProofStale("integration frozen commit proof differs")
        if _read_ref(
            snapshot.repo, existing_ref, deadline=absolute_deadline, cancel_event=cancel_event,
        ) != integration_oid:
            raise IntegrationProofStale("integration frozen ref differs")
        if _read_ref(
            snapshot.repo, target_ref, deadline=absolute_deadline, cancel_event=cancel_event,
        ) != target_oid:
            raise IntegrationProofStale("integration target changed after anchoring")
        for ref_name, commit_oid in candidate_refs:
            if _read_ref(
                snapshot.repo, ref_name, deadline=absolute_deadline, cancel_event=cancel_event,
            ) != commit_oid:
                raise IntegrationProofStale("integration candidate ref changed after anchoring")
        _assert_repo_identity(snapshot.repo, deadline=absolute_deadline)
        if not source_boundary.recapture_matches(current_capture, deadline=absolute_deadline):
            raise IntegrationProofStale("integration target or protected state changed")
        result = FrozenIntegration(
            plan_id=plan_id,
            approval_digest=approval_digest,
            contract_digest=contract_sha,
            source_snapshot_digest=snapshot_sha,
            target_ref=target_ref,
            target_oid=target_oid,
            integration_oid=integration_oid,
            tree_oid=integration_tree_oid,
            ref_name=existing_ref,
            candidates=tuple(applied),
            receipt_digest="",
        )
        return FrozenIntegration(
            **{**result.__dict__, "receipt_digest": _receipt_digest(result)}
        )
    finally:
        try:
            if owned is not None:
                _cleanup_owned_temp_attempt(
                    owned,
                    deadline=absolute_deadline,
                    cancel_event=cancel_event,
                )
        finally:
            try:
                os.close(prepared_root.descriptor)
            except OSError:
                pass


def freeze_repair_integration(
    *,
    plan_id: str,
    generation: int,
    prior: FrozenIntegration,
    plan: ExecutionPlan,
    snapshot: SourceSnapshot,
    contract: Mapping[str, Any],
    approval_digest: str,
    candidates: Sequence[CandidateIntegrationBinding],
    temp_root: str | Path,
    deadline: float,
    cancel_event: threading.Event | None = None,
) -> FrozenIntegration:
    """Freeze a fresh immutable integration over the prior reviewed target."""

    return freeze_integration(
        plan_id=plan_id,
        plan=plan,
        snapshot=snapshot,
        contract=contract,
        approval_digest=approval_digest,
        candidates=candidates,
        temp_root=temp_root,
        deadline=deadline,
        cancel_event=cancel_event,
        _generation=generation,
        _prior=prior,
    )


def _owned_parent_state(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid)


def _validated_owned_parent(
    parent_fd: int,
    parent_identity: tuple[int, int, int, int],
) -> os.stat_result:
    if (
        isinstance(parent_fd, bool)
        or not isinstance(parent_fd, int)
        or parent_fd < 0
        or not isinstance(parent_identity, tuple)
        or len(parent_identity) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in parent_identity)
    ):
        raise IntegrationValidationError(
            "integration owned parent evidence is invalid"
        )
    try:
        opened = os.fstat(parent_fd)
    except OSError:
        raise IntegrationProofStale(
            "integration owned parent descriptor is unavailable"
        ) from None
    if (
        not stat.S_ISDIR(opened.st_mode)
        or _owned_parent_state(opened) != parent_identity
        or stat.S_IMODE(opened.st_mode) != 0o700
        or opened.st_uid != os.geteuid()
    ):
        raise IntegrationProofStale(
            "integration owned parent identity changed"
        )
    return opened


def _validate_destination_leaf(destination_leaf: bytes) -> None:
    if (
        not isinstance(destination_leaf, bytes)
        or destination_leaf != b"integration"
        or destination_leaf in {b"", b".", b".."}
        or b"/" in destination_leaf
        or b"\0" in destination_leaf
    ):
        raise IntegrationValidationError(
            "integration owned destination leaf is invalid"
        )


def _prepare_owned_parent_destination(
    *,
    parent_fd: int,
    parent_identity: tuple[int, int, int, int],
    destination_leaf: bytes,
    deadline: float,
    cancel_event: threading.Event | None,
) -> object:
    """Create staging below one retained host-owned directory descriptor."""

    _check_control(deadline=deadline, cancel_event=cancel_event)
    _validate_destination_leaf(destination_leaf)
    _validated_owned_parent(parent_fd, parent_identity)
    try:
        owned_parent_fd = os.dup(parent_fd)
        os.set_inheritable(owned_parent_fd, False)
    except OSError:
        raise IntegrationProofStale(
            "integration owned parent descriptor could not be retained"
        ) from None
    root_fd = -1
    staging_leaf: bytes | None = None
    root_identity: tuple[int, int] | None = None
    try:
        _validated_owned_parent(owned_parent_fd, parent_identity)
        try:
            os.stat(
                destination_leaf,
                dir_fd=owned_parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise IntegrationValidationError(
                "integration owned destination must not already exist"
            )
        for _attempt in range(16):
            _check_control(deadline=deadline, cancel_event=cancel_event)
            staging_leaf = (
                b".integration.bestplan-staging-"
                + secrets.token_hex(16).encode("ascii")
            )
            try:
                os.mkdir(staging_leaf, mode=0o700, dir_fd=owned_parent_fd)
            except FileExistsError:
                continue
            except OSError:
                raise IntegrationValidationError(
                    "integration owned staging could not be created"
                ) from None
            break
        else:
            raise IntegrationValidationError(
                "integration owned staging name could not be reserved"
            )
        created = os.stat(
            staging_leaf,
            dir_fd=owned_parent_fd,
            follow_symlinks=False,
        )
        root_fd = os.open(
            staging_leaf,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=owned_parent_fd,
        )
        opened = os.fstat(root_fd)
        root_identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISDIR(created.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (created.st_dev, created.st_ino) != root_identity
            or stat.S_IMODE(opened.st_mode) != 0o700
            or opened.st_uid != os.geteuid()
        ):
            raise IntegrationProofStale(
                "integration owned staging identity changed"
            )
        _validated_owned_parent(owned_parent_fd, parent_identity)
        return source_boundary._PreparedDestination(
            path=b"",
            final_leaf=destination_leaf,
            staging_leaf=staging_leaf,
            root_fd=root_fd,
            root_identity=root_identity,
            raw_parent=b"",
            canonical_parent=b"",
            parent_fds=(owned_parent_fd,),
            parent_identities=((parent_identity[0], parent_identity[1]),),
        )
    except BaseException:
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError:
                pass
        if staging_leaf is not None:
            try:
                named = os.stat(
                    staging_leaf,
                    dir_fd=owned_parent_fd,
                    follow_symlinks=False,
                )
                if (
                    root_identity is None
                    or (named.st_dev, named.st_ino) == root_identity
                ):
                    os.rmdir(staging_leaf, dir_fd=owned_parent_fd)
            except OSError:
                pass
        try:
            os.close(owned_parent_fd)
        except OSError:
            pass
        raise


def _verify_owned_parent_name(
    prepared: object,
    *,
    parent_identity: tuple[int, int, int, int],
    leaf: bytes,
    deadline: float,
    cancel_event: threading.Event | None,
) -> None:
    _check_control(deadline=deadline, cancel_event=cancel_event)
    parent_fd = prepared.parent_fds[-1]
    _validated_owned_parent(parent_fd, parent_identity)
    try:
        named = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(prepared.root_fd)
    except OSError:
        raise IntegrationProofStale(
            "integration owned destination identity changed"
        ) from None
    if (
        not stat.S_ISDIR(named.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (named.st_dev, named.st_ino) != prepared.root_identity
        or (opened.st_dev, opened.st_ino) != prepared.root_identity
    ):
        raise IntegrationProofStale(
            "integration owned destination identity changed"
        )


def _verify_owned_materialization(
    prepared: object,
    entries: object,
    witnesses: object,
    *,
    parent_identity: tuple[int, int, int, int],
    object_format: str,
    deadline: float,
    cancel_event: threading.Event | None,
) -> None:
    for _observation in range(source_boundary._STABLE_EXPORT_OBSERVATIONS):
        _verify_owned_parent_name(
            prepared,
            parent_identity=parent_identity,
            leaf=prepared.final_leaf,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        _call_source_with_control(
            source_boundary._verify_exported_tree,
            prepared,
            entries,
            witnesses,
            object_format=object_format,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        _verify_owned_parent_name(
            prepared,
            parent_identity=parent_identity,
            leaf=prepared.final_leaf,
            deadline=deadline,
            cancel_event=cancel_event,
        )


def _quarantine_owned_parent_destination(
    prepared: object,
    *,
    parent_identity: tuple[int, int, int, int],
    backend: str,
) -> None:
    """Atomically hide failed published output without reopening its parent."""

    parent_fd = prepared.parent_fds[-1]
    _validated_owned_parent(parent_fd, parent_identity)
    named = os.stat(
        prepared.final_leaf,
        dir_fd=parent_fd,
        follow_symlinks=False,
    )
    opened = os.fstat(prepared.root_fd)
    if (
        not stat.S_ISDIR(named.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (named.st_dev, named.st_ino) != prepared.root_identity
        or (opened.st_dev, opened.st_ino) != prepared.root_identity
    ):
        raise PromotionError(
            "integration destination identity changed; evidence retained"
        )
    quarantine_leaf = (
        b".integration.bestplan-quarantine-"
        + secrets.token_hex(16).encode("ascii")
    )
    try:
        source_boundary._rename_leaf_no_replace(
            parent_fd,
            prepared.final_leaf,
            quarantine_leaf,
            backend=backend,
        )
    except BaseException:
        raise PromotionError(
            "integration destination quarantine failed; evidence retained"
        ) from None
    quarantined = os.stat(
        quarantine_leaf,
        dir_fd=parent_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(quarantined.st_mode)
        or (quarantined.st_dev, quarantined.st_ino) != prepared.root_identity
    ):
        raise PromotionError(
            "integration destination quarantine identity changed"
        )


def _materialize_integration_tree_at_owned_parent(
    *,
    snapshot: SourceSnapshot,
    integration: FrozenIntegration,
    parent_fd: int,
    parent_identity: tuple[int, int, int, int],
    destination_leaf: bytes,
    deadline: float,
    cancel_event: threading.Event | None = None,
) -> None:
    """Export below a retained host-owned parent without reopening its path."""

    absolute_deadline = _validated_deadline(deadline)
    cancel_event = _validated_cancel_event(cancel_event)
    _check_control(deadline=absolute_deadline, cancel_event=cancel_event)
    if not isinstance(snapshot, SourceSnapshot) or not isinstance(
        integration, FrozenIntegration
    ):
        raise IntegrationValidationError(
            "integration materialization input is invalid"
        )
    if _receipt_digest(
        FrozenIntegration(**{**integration.__dict__, "receipt_digest": ""})
    ) != integration.receipt_digest:
        raise IntegrationValidationError("integration receipt digest differs")
    _validated_owned_parent(parent_fd, parent_identity)
    _validate_destination_leaf(destination_leaf)
    _assert_repo_identity(snapshot.repo, deadline=absolute_deadline)
    if _read_ref(
        snapshot.repo,
        integration.ref_name,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    ) != integration.integration_oid:
        raise IntegrationProofStale("integration materialization ref changed")
    if _read_ref(
        snapshot.repo,
        integration.target_ref,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    ) != integration.target_oid:
        raise IntegrationProofStale(
            "integration materialization target ref changed"
        )
    tree_oid, parents = _commit_proof(
        snapshot.repo,
        integration.integration_oid,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    if tree_oid != integration.tree_oid or parents != (integration.target_oid,):
        raise IntegrationProofStale("integration materialization commit differs")
    backend = source_boundary._assert_export_host_supported()
    entries = _call_source_with_control(
        source_boundary._tree_entries,
        snapshot.repo,
        integration.tree_oid,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    if any(
        entry.object_type != b"blob"
        or entry.mode not in {0o100644, 0o100755, 0o120000}
        for entry in entries
    ):
        raise IntegrationValidationError(
            "integration materialization tree is unsupported"
        )
    _call_source_with_control(
        source_boundary._assert_tree_path_aliases,
        entries,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    prepared = None
    witnesses = ()
    published = False
    try:
        prepared = _prepare_owned_parent_destination(
            parent_fd=parent_fd,
            parent_identity=parent_identity,
            destination_leaf=destination_leaf,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        witnesses = _call_source_with_control(
            source_boundary._materialize_blobs,
            snapshot.repo,
            entries,
            prepared.root_fd,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        _check_control(deadline=absolute_deadline, cancel_event=cancel_event)
        os.fchmod(prepared.root_fd, 0o755)
        _verify_owned_parent_name(
            prepared,
            parent_identity=parent_identity,
            leaf=prepared.staging_leaf,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        try:
            os.stat(
                destination_leaf,
                dir_fd=prepared.parent_fds[-1],
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise IntegrationProofStale(
                "integration owned destination appeared before publication"
            )
        source_boundary._rename_leaf_no_replace(
            prepared.parent_fds[-1],
            prepared.staging_leaf,
            prepared.final_leaf,
            backend=backend,
        )
        published = True
        _verify_owned_materialization(
            prepared,
            entries,
            witnesses,
            parent_identity=parent_identity,
            object_format=snapshot.repo.object_format,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        if _read_ref(
            snapshot.repo,
            integration.ref_name,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        ) != integration.integration_oid:
            raise IntegrationProofStale("integration materialization ref changed")
        if _read_ref(
            snapshot.repo,
            integration.target_ref,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        ) != integration.target_oid:
            raise IntegrationProofStale(
                "integration materialization target ref changed"
            )
    except BaseException as error:
        if prepared is not None:
            try:
                if published:
                    _quarantine_owned_parent_destination(
                        prepared,
                        parent_identity=parent_identity,
                        backend=backend,
                    )
                else:
                    _cleanup_materialization_staging(
                        prepared,
                        deadline=absolute_deadline,
                        cancel_event=cancel_event,
                    )
            except BaseException as cleanup_error:
                if (
                    not published
                    and (
                        (cancel_event is not None and cancel_event.is_set())
                        or time.monotonic() >= absolute_deadline
                    )
                ):
                    pass
                else:
                    raise PromotionError(
                        "integration materialization cleanup failed; evidence quarantined"
                    ) from cleanup_error
        if isinstance(error, PromotionError):
            raise
        raise PromotionError("integration materialization failed") from error
    finally:
        if prepared is not None:
            try:
                os.close(prepared.root_fd)
            except OSError:
                pass
            source_boundary._close_fds(prepared.parent_fds)


def materialize_integration_tree(
    *,
    snapshot: SourceSnapshot,
    integration: FrozenIntegration,
    destination: str | os.PathLike[str],
    deadline: float,
    cancel_event: threading.Event | None = None,
) -> None:
    """Export exact integration blobs into a new no-``.git`` directory."""

    absolute_deadline = _validated_deadline(deadline)
    cancel_event = _validated_cancel_event(cancel_event)
    _check_control(
        deadline=absolute_deadline, cancel_event=cancel_event,
    )
    if not isinstance(snapshot, SourceSnapshot) or not isinstance(integration, FrozenIntegration):
        raise IntegrationValidationError("integration materialization input is invalid")
    if _receipt_digest(FrozenIntegration(**{**integration.__dict__, "receipt_digest": ""})) != integration.receipt_digest:
        raise IntegrationValidationError("integration receipt digest differs")
    _assert_repo_identity(snapshot.repo, deadline=absolute_deadline)
    if _read_ref(
        snapshot.repo,
        integration.ref_name,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    ) != integration.integration_oid:
        raise IntegrationProofStale("integration materialization ref changed")
    if _read_ref(
        snapshot.repo,
        integration.target_ref,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    ) != integration.target_oid:
        raise IntegrationProofStale("integration materialization target ref changed")
    tree_oid, parents = _commit_proof(
        snapshot.repo,
        integration.integration_oid,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    if tree_oid != integration.tree_oid or parents != (integration.target_oid,):
        raise IntegrationProofStale("integration materialization commit differs")
    backend = source_boundary._assert_export_host_supported()
    entries = _call_source_with_control(
        source_boundary._tree_entries,
        snapshot.repo,
        integration.tree_oid,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    if any(
        entry.object_type != b"blob"
        or entry.mode not in {0o100644, 0o100755, 0o120000}
        for entry in entries
    ):
        raise IntegrationValidationError("integration materialization tree is unsupported")
    _call_source_with_control(
        source_boundary._assert_tree_path_aliases,
        entries,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    _check_control(
        deadline=absolute_deadline, cancel_event=cancel_event,
    )
    prepared = None
    witnesses = ()
    published = False
    try:
        prepared = _call_source_with_control(
            source_boundary._prepare_destination,
            snapshot.repo,
            destination,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        witnesses = _call_source_with_control(
            source_boundary._materialize_blobs,
            snapshot.repo,
            entries,
            prepared.root_fd,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        _check_control(
            deadline=absolute_deadline, cancel_event=cancel_event,
        )
        os.fchmod(prepared.root_fd, 0o755)
        staged = os.stat(
            prepared.staging_leaf,
            dir_fd=prepared.parent_fds[-1],
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(staged.st_mode)
            or (staged.st_dev, staged.st_ino) != prepared.root_identity
        ):
            raise IntegrationProofStale("integration staging directory changed")
        _call_source_with_control(
            source_boundary._verify_destination_parent,
            prepared,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        _check_control(
            deadline=absolute_deadline, cancel_event=cancel_event,
        )
        _call_source_with_control(
            source_boundary._publish_staging_no_replace,
            prepared,
            backend=backend,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        published = True
        _check_control(
            deadline=absolute_deadline, cancel_event=cancel_event,
        )
        _call_source_with_control(
            source_boundary._verify_published_exact_tree,
            prepared,
            entries,
            witnesses,
            object_format=snapshot.repo.object_format,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        if _read_ref(
            snapshot.repo,
            integration.ref_name,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        ) != integration.integration_oid:
            raise IntegrationProofStale("integration materialization ref changed")
        if _read_ref(
            snapshot.repo,
            integration.target_ref,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        ) != integration.target_oid:
            raise IntegrationProofStale(
                "integration materialization target ref changed"
            )
    except BaseException as error:
        if prepared is not None:
            try:
                if published:
                    _quarantine_materialized_destination(
                        prepared,
                        entries,
                        witnesses,
                        backend=backend,
                        object_format=snapshot.repo.object_format,
                        deadline=absolute_deadline,
                        cancel_event=cancel_event,
                    )
                else:
                    _cleanup_materialization_staging(
                        prepared,
                        deadline=absolute_deadline,
                        cancel_event=cancel_event,
                    )
            except BaseException as cleanup_error:
                if (
                    not published
                    and (
                        (cancel_event is not None and cancel_event.is_set())
                        or time.monotonic() >= absolute_deadline
                    )
                ):
                    pass
                else:
                    raise PromotionError(
                        "integration materialization cleanup failed; evidence quarantined"
                    ) from cleanup_error
        if isinstance(error, PromotionError):
            raise
        raise PromotionError("integration materialization failed") from error
    finally:
        if prepared is not None:
            try:
                os.close(prepared.root_fd)
            except OSError:
                pass
            source_boundary._close_fds(prepared.parent_fds)


__all__ = [
    "AppliedCandidate",
    "CandidateIntegrationBinding",
    "FrozenIntegration",
    "IntegrationConflictError",
    "IntegrationProofStale",
    "IntegrationRefConflict",
    "IntegrationValidationError",
    "PromotionError",
    "candidate_integration_binding_digest",
    "freeze_integration",
    "materialize_integration_tree",
]
