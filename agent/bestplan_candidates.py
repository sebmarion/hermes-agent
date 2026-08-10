"""Single-attempt isolation and host-owned freezing for BestPlan candidates.

This module intentionally owns one candidate lifecycle.  Batch scheduling,
integration, publication, and live activation remain outside this boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from agent.bestplan_authority_client import (
    BrokerTurnRequest,
    BrokerTurnResponse,
    WorkerIdentity,
)
from agent.bestplan_sandbox import CANDIDATE_BOOTSTRAP
from agent.bestplan_source import (
    RepoIdentity,
    SourceSnapshot,
    export_captured_commit_tree,
    resolve_repo_identity,
)


CANDIDATE_ENVIRONMENT_KEYS = frozenset({
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
MAX_CANDIDATE_ENTRIES = 100_000
MAX_CANDIDATE_FILE_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_CANDIDATE_PATH_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_DEPTH = 128
DEFAULT_CANDIDATE_SCAN_SECONDS = 10.0
DEFAULT_CANDIDATE_GIT_SECONDS = 15.0
MAX_CANDIDATE_GIT_OUTPUT_BYTES = 1024 * 1024
MAX_CANDIDATE_GIT_INPUT_BYTES = 128 * 1024 * 1024
MAX_WORKER_STDOUT_BYTES = 2 * 1024 * 1024
MAX_WORKER_STDERR_BYTES = 512 * 1024
MAX_BROKER_FRAME_BYTES = 4 * 1024 * 1024
MAX_BROKER_TOOL_CALLS_PER_RESPONSE = 8
MAX_BROKER_TOOL_CALLS_PER_ATTEMPT = 64
MAX_BROKER_TOOL_ARGUMENT_BYTES_PER_ATTEMPT = 2 * 1024 * 1024
MAX_CANDIDATE_TIMEOUT_SECONDS = 86_400.0
MAX_PROCESS_OBSERVATION_BYTES = 4 * 1024 * 1024
_RESULT_MARKER = "HERMES_BESTPLAN_RESULT="
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ALLOWED_TOOL_NAMES = {
    "file": frozenset({"read_file", "write_file", "patch", "search_files"}),
    "read_only_files": frozenset({"read_file", "search_files"}),
}
_HOST_CANDIDATE_TOOL_SCHEMAS = {
    "read_file": {
        "name": "read_file",
        "description": "Read bounded UTF-8 text inside the candidate source tree.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "write_file": {
        "name": "write_file",
        "description": "Write bounded UTF-8 text inside an approved candidate lease.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "patch": {
        "name": "patch",
        "description": "Replace exact text inside one approved candidate file.",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["replace"]},
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    },
    "search_files": {
        "name": "search_files",
        "description": "Search bounded candidate paths or literal UTF-8 text.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "target": {"type": "string", "enum": ["content", "files"]},
                "path": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "offset": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
}
_HOST_ALLOWED_MODEL_REQUEST_KEYS = frozenset({
    "frequency_penalty",
    "max_completion_tokens",
    "max_tokens",
    "messages",
    "model",
    "n",
    "parallel_tool_calls",
    "presence_penalty",
    "reasoning_effort",
    "response_format",
    "seed",
    "stop",
    "stream",
    "temperature",
    "tool_choice",
    "tools",
    "top_p",
})
_HOST_FORBIDDEN_REQUEST_KEYS = frozenset({
    "api_key",
    "api_mode",
    "base_url",
    "command",
    "endpoint",
    "extra_body",
    "extra_headers",
    "headers",
    "organization",
    "project",
    "provider",
})


class CandidateError(RuntimeError):
    """Base error for the single-candidate boundary."""


class CandidateValidationError(CandidateError):
    """Candidate input or filesystem state is outside the approved boundary."""


class CandidateProofStale(CandidateError):
    """A two-observation candidate proof did not remain stable."""


class CandidateRefConflict(CandidateError):
    """A host-owned reference already names a different object."""


class CandidateExecutionError(CandidateError):
    """The isolated worker failed without exposing raw provider/host details."""


@dataclass(frozen=True)
class CandidateSpec:
    plan_id: str
    candidate_id: str
    slice_id: str
    goal: str
    allowed_paths: tuple[str, ...]
    read_only: bool
    expected_artifacts: tuple[str, ...]
    model: str
    request_budget: int
    token_budget: int
    expires_at: int
    max_iterations: int
    max_output_tokens: int
    toolsets: tuple[str, ...]

    def __post_init__(self) -> None:
        for value, name in (
            (self.plan_id, "plan"),
            (self.candidate_id, "candidate"),
            (self.slice_id, "slice"),
        ):
            _validated_identifier(value, name)
        if not isinstance(self.goal, str) or not self.goal or len(self.goal) > 256_000:
            raise CandidateValidationError("candidate goal is invalid")
        if (
            not isinstance(self.model, str)
            or not self.model.strip()
            or "\x00" in self.model
            or len(self.model.encode("utf-8")) > 1024
        ):
            raise CandidateValidationError("candidate model is invalid")
        if not isinstance(self.read_only, bool):
            raise CandidateValidationError("candidate read-only flag is invalid")
        toolsets = tuple(self.toolsets)
        if toolsets not in (("file",), ("read_only_files",)):
            raise CandidateValidationError("candidate toolset is not process-free")
        if self.read_only and toolsets != ("read_only_files",):
            raise CandidateValidationError("read-only candidate requires read_only_files")
        if not self.read_only and toolsets != ("file",):
            raise CandidateValidationError("writable candidate requires file toolset")
        for value, name, maximum in (
            (self.request_budget, "request budget", 10_000),
            (self.token_budget, "token budget", 100_000_000),
            (self.max_iterations, "iteration budget", 500),
            (self.max_output_tokens, "output token budget", 32_768),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise CandidateValidationError(f"candidate {name} is invalid")
        if (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, int)
            or self.expires_at <= int(time.time())
            or self.expires_at > (1 << 63) - 1
        ):
            raise CandidateValidationError("candidate expiry is invalid")
        normalized_leases = tuple(_validated_relative_path(item, "write lease") for item in self.allowed_paths)
        normalized_artifacts = tuple(
            _validated_relative_path(item, "expected artifact")
            for item in self.expected_artifacts
        )
        if len(set(normalized_leases)) != len(normalized_leases):
            raise CandidateValidationError("candidate write lease is duplicated")
        if len(set(normalized_artifacts)) != len(normalized_artifacts):
            raise CandidateValidationError("candidate expected artifact is duplicated")
        if self.read_only and normalized_leases:
            raise CandidateValidationError("read-only candidate cannot have a write lease")
        if not self.read_only and not normalized_leases:
            raise CandidateValidationError("candidate write lease is required")
        if not normalized_artifacts:
            raise CandidateValidationError("candidate expected artifact is required")
        object.__setattr__(self, "toolsets", toolsets)
        object.__setattr__(self, "allowed_paths", normalized_leases)
        object.__setattr__(self, "expected_artifacts", normalized_artifacts)


@dataclass(frozen=True)
class _TreeRecord:
    path: bytes
    kind: str
    mode: int
    data: bytes


@dataclass(frozen=True)
class CandidateAttempt:
    attempt_id: str
    root: Path
    source_dir: Path
    runtime_dir: Path
    scratch_dir: Path
    control_dir: Path
    ref_name: str
    base_ref_name: str
    _base_records: tuple[_TreeRecord, ...] = field(repr=False)


@dataclass(frozen=True)
class SealedCandidate:
    attempt: CandidateAttempt
    records: tuple[_TreeRecord, ...]
    witness_sha256: str


@dataclass(frozen=True)
class FrozenCandidateArtifact:
    candidate_id: str
    slice_id: str
    attempt_id: str
    commit_oid: str
    tree_oid: str
    ref_name: str
    changed_paths: tuple[bytes, ...]
    raw_receipt: Mapping[str, object]
    raw_receipt_sha256: str


@dataclass(frozen=True)
class FrozenCandidate(FrozenCandidateArtifact):
    """Broker- and sandbox-attested result produced only by the public runner."""

    policy_digest: str
    controller_id: str
    controller_repository_id: str
    controller_release_oid: str
    controller_artifact_sha256: str
    admitted_requests: int
    admitted_input_tokens: int
    admitted_output_tokens: int


def _validated_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise CandidateValidationError(f"candidate {name} identifier is malformed")
    return value


def _immutable_json_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({
            str(key): _immutable_json_value(item) for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_immutable_json_value(item) for item in value)
    return value


def _validated_relative_path(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise CandidateValidationError(f"candidate {name} is invalid")
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise CandidateValidationError(f"candidate {name} is invalid")
    return path.as_posix()


def candidate_ref_name(plan_id: str, slice_id: str, attempt_id: str) -> str:
    parts = tuple(
        _validated_identifier(value, name)
        for value, name in (
            (plan_id, "plan"),
            (slice_id, "slice"),
            (attempt_id, "attempt"),
        )
    )
    return "refs/hermes-bestplan/" + "/".join(parts)


def _base_ref_name(plan_id: str, slice_id: str, attempt_id: str) -> str:
    candidate_ref_name(plan_id, slice_id, attempt_id)
    return f"refs/hermes-bestplan-bases/{plan_id}/{slice_id}/{attempt_id}"


def _repo_path(repo: RepoIdentity) -> Path:
    path = Path(os.fsdecode(repo.worktree_raw))
    if os.fsencode(path) != repo.worktree_raw:
        raise CandidateValidationError("repository raw path identity is not lossless")
    return path


def _lossless_repo_path(raw: bytes, name: str) -> str:
    value = os.fsdecode(raw)
    if os.fsencode(value) != raw or not os.path.isabs(value):
        raise CandidateValidationError(f"repository {name} path identity is invalid")
    return value


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


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _assert_controller_source_disjoint(
    controller_source: str | Path, repo: RepoIdentity,
) -> Path:
    try:
        controller = Path(controller_source).resolve(strict=True)
        protected = (
            Path(_lossless_repo_path(repo.worktree_raw, "worktree")).resolve(strict=True),
            Path(_lossless_repo_path(repo.git_dir_raw, "Git directory")).resolve(strict=True),
            Path(_lossless_repo_path(repo.common_dir_raw, "common directory")).resolve(strict=True),
        )
    except (OSError, RuntimeError):
        raise CandidateValidationError("candidate controller source identity is unavailable") from None
    if any(_paths_overlap(controller, path) for path in protected):
        raise CandidateValidationError(
            "candidate controller source overlaps the primary repository"
        )
    return controller


def _assert_attempts_root_disjoint(
    attempts_root: str | Path,
    repo: RepoIdentity,
    *,
    controller_source: Path | None = None,
) -> Path:
    try:
        root = Path(attempts_root).expanduser().resolve(strict=False)
        protected = [
            Path(_lossless_repo_path(repo.worktree_raw, "worktree")).resolve(strict=True),
            Path(_lossless_repo_path(repo.git_dir_raw, "Git directory")).resolve(strict=True),
            Path(_lossless_repo_path(repo.common_dir_raw, "common directory")).resolve(strict=True),
        ]
        if controller_source is not None:
            protected.append(controller_source.resolve(strict=True))
    except (OSError, RuntimeError):
        raise CandidateValidationError("candidate attempt root identity is unavailable") from None
    if any(_paths_overlap(root, path) for path in protected):
        raise CandidateValidationError("candidate attempt root overlaps a protected root")
    return root


def _assert_repository_identity(
    expected: RepoIdentity, *, deadline: float | None = None,
) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise CandidateExecutionError("candidate freeze deadline expired")
    workspace = _lossless_repo_path(expected.worktree_raw, "worktree")
    try:
        actual = resolve_repo_identity(workspace, deadline=deadline)
    except BaseException:
        raise CandidateValidationError("candidate repository identity is unavailable") from None
    if deadline is not None and time.monotonic() >= deadline:
        raise CandidateExecutionError("candidate freeze deadline expired")
    if not _same_repository(expected, actual):
        raise CandidateValidationError("candidate repository identity changed")


def _git_environment() -> dict[str, str]:
    return {
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


def _git(
    repo: RepoIdentity,
    *args: str,
    input_bytes: bytes | None = None,
    check: bool = True,
    extra_environment: Mapping[str, str] | None = None,
    deadline: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    deadline = (
        time.monotonic() + DEFAULT_CANDIDATE_GIT_SECONDS
        if deadline is None
        else float(deadline)
    )
    if time.monotonic() >= deadline:
        raise CandidateExecutionError("candidate Git deadline expired")
    if input_bytes is not None and (
        not isinstance(input_bytes, bytes)
        or len(input_bytes) > MAX_CANDIDATE_GIT_INPUT_BYTES
    ):
        raise CandidateExecutionError("candidate Git input limit exceeded")
    git_dir = _lossless_repo_path(repo.git_dir_raw, "Git directory")
    worktree = _lossless_repo_path(repo.worktree_raw, "worktree")
    command = [
        "git",
        f"--git-dir={git_dir}",
        f"--work-tree={worktree}",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "filter.lfs.required=false",
        *args,
    ]
    environment = _git_environment()
    if extra_environment:
        environment.update(extra_environment)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
    except BaseException:
        raise CandidateExecutionError("candidate Git operation failed") from None
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    writer_error = threading.Event()

    def drain(name: str, stream: object) -> None:
        try:
            while True:
                read = getattr(stream, "read1", stream.read)
                chunk = read(64 * 1024)
                if not chunk:
                    return
                target = outputs[name]
                remaining = max(0, MAX_CANDIDATE_GIT_OUTPUT_BYTES + 1 - len(target))
                if remaining:
                    target.extend(chunk[:remaining])
                if len(target) > MAX_CANDIDATE_GIT_OUTPUT_BYTES or len(chunk) > remaining:
                    overflow.set()
        except (OSError, ValueError):
            return

    def write_input() -> None:
        assert process.stdin is not None and input_bytes is not None
        try:
            process.stdin.write(input_bytes)
            process.stdin.flush()
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            writer_error.set()

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    if input_bytes is not None:
        assert process.stdin is not None
        threads.append(threading.Thread(target=write_input, daemon=True))
    for thread in threads:
        thread.start()

    forced_failure: str | None = None
    while True:
        if overflow.is_set():
            forced_failure = "candidate Git output limit exceeded"
            break
        if time.monotonic() >= deadline:
            forced_failure = "candidate Git deadline expired"
            break
        returncode = process.poll()
        if returncode is not None:
            break
        time.sleep(0.005)
    if forced_failure is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except BaseException:
            raise CandidateExecutionError("candidate Git process did not stop") from None
    else:
        returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    for thread in threads:
        thread.join(timeout=1)
    if any(thread.is_alive() for thread in threads):
        raise CandidateExecutionError("candidate Git pipe did not close")
    if forced_failure is not None:
        raise CandidateExecutionError(forced_failure)
    if overflow.is_set():
        raise CandidateExecutionError("candidate Git output limit exceeded")
    if writer_error.is_set() and returncode == 0:
        raise CandidateExecutionError("candidate Git input failed")
    result = subprocess.CompletedProcess(
        command, returncode, bytes(outputs["stdout"]), bytes(outputs["stderr"]),
    )
    if check and result.returncode != 0:
        raise CandidateExecutionError("candidate Git operation failed")
    return result


def _read_ref(
    repo: RepoIdentity, ref_name: str, *, deadline: float | None = None,
) -> str | None:
    result = _git(
        repo, "rev-parse", "--verify", "--quiet", ref_name,
        check=False, deadline=deadline,
    )
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise CandidateExecutionError("candidate reference lookup failed")
    return result.stdout.decode("ascii").strip()


def _anchor_candidate_ref(
    repo: RepoIdentity,
    ref_name: str,
    commit_oid: str,
    *,
    deadline: float | None = None,
) -> str:
    if not ref_name.startswith(("refs/hermes-bestplan/", "refs/hermes-bestplan-bases/")):
        raise CandidateValidationError("candidate reference namespace is invalid")
    existing = _read_ref(repo, ref_name, deadline=deadline)
    if existing is not None:
        if existing == commit_oid:
            return commit_oid
        raise CandidateRefConflict("candidate reference already names a different commit")
    zero = "0" * len(commit_oid)
    result = _git(
        repo, "update-ref", "--no-deref", ref_name, commit_oid, zero,
        check=False, deadline=deadline,
    )
    if result.returncode != 0:
        existing = _read_ref(repo, ref_name, deadline=deadline)
        if existing == commit_oid:
            return commit_oid
        raise CandidateRefConflict("candidate reference already exists")
    return commit_oid


def _delete_ref(
    repo: RepoIdentity,
    ref_name: str,
    expected_oid: str,
    *,
    deadline: float | None = None,
) -> None:
    existing = _read_ref(repo, ref_name, deadline=deadline)
    if existing is None:
        return
    if existing != expected_oid:
        raise CandidateRefConflict("candidate reference deletion identity differs")
    result = _git(
        repo, "update-ref", "--no-deref", "-d", ref_name, expected_oid,
        check=False, deadline=deadline,
    )
    if result.returncode != 0 or _read_ref(repo, ref_name, deadline=deadline) is not None:
        raise CandidateRefConflict("candidate reference deletion failed")


def _delete_refs_transactionally(
    repo: RepoIdentity,
    refs: tuple[tuple[str, str], ...],
    *,
    deadline: float,
) -> None:
    """CAS-delete an owned ref set as one all-or-nothing Git transaction."""

    pending: list[tuple[str, str]] = []
    for ref_name, expected_oid in refs:
        if not ref_name.startswith((
            "refs/hermes-bestplan/",
            "refs/hermes-bestplan-bases/",
        )):
            raise CandidateValidationError("candidate reference namespace is invalid")
        existing = _read_ref(repo, ref_name, deadline=deadline)
        if existing is None:
            continue
        if existing != expected_oid:
            raise CandidateRefConflict(
                "candidate reference deletion identity differs"
            )
        pending.append((ref_name, expected_oid))
    if not pending:
        return
    commands = ["start"]
    commands.extend(
        f"delete {ref_name} {expected_oid}" for ref_name, expected_oid in pending
    )
    commands.extend(("prepare", "commit", ""))
    result = _git(
        repo,
        "update-ref",
        "--no-deref",
        "--stdin",
        input_bytes="\n".join(commands).encode("ascii"),
        check=False,
        deadline=deadline,
    )
    if result.returncode != 0 or any(
        _read_ref(repo, ref_name, deadline=deadline) is not None
        for ref_name, _expected_oid in pending
    ):
        raise CandidateRefConflict("candidate reference transaction failed")


def create_candidate_attempt(
    snapshot: SourceSnapshot,
    *,
    plan_id: str,
    slice_id: str,
    attempts_root: str | Path,
    attempt_id: str | None = None,
) -> CandidateAttempt:
    if not isinstance(snapshot, SourceSnapshot):
        raise CandidateValidationError("candidate source snapshot is invalid")
    _validated_identifier(plan_id, "plan")
    _validated_identifier(slice_id, "slice")
    attempt_id = (
        f"attempt-{uuid.uuid4().hex[:16]}" if attempt_id is None else attempt_id
    )
    _validated_identifier(attempt_id, "attempt")
    root_parent = _assert_attempts_root_disjoint(attempts_root, snapshot.repo)
    root_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = root_parent / attempt_id
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise CandidateValidationError("candidate attempt already exists") from exc
    source_dir = root / "source"
    runtime_dir = root / "runtime"
    scratch_dir = root / "scratch"
    control_dir = root / "control"
    ref_name = candidate_ref_name(plan_id, slice_id, attempt_id)
    base_ref_name = _base_ref_name(plan_id, slice_id, attempt_id)
    anchored = False
    try:
        _anchor_candidate_ref(snapshot.repo, base_ref_name, snapshot.head_oid)
        anchored = True
        for directory in (runtime_dir, scratch_dir, control_dir):
            directory.mkdir(mode=0o700)
        export_captured_commit_tree(snapshot, source_dir)
        records, _paths, _witness = _scan_candidate_tree(source_dir)
        return CandidateAttempt(
            attempt_id=attempt_id,
            root=root,
            source_dir=source_dir,
            runtime_dir=runtime_dir,
            scratch_dir=scratch_dir,
            control_dir=control_dir,
            ref_name=ref_name,
            base_ref_name=base_ref_name,
            _base_records=records,
        )
    except BaseException:
        if anchored:
            _delete_ref(snapshot.repo, base_ref_name, snapshot.head_oid)
        raise


def validate_raw_candidate_paths(paths: Iterable[bytes]) -> tuple[bytes, ...]:
    output: list[bytes] = []
    aliases: dict[str, bytes] = {}
    for raw in paths:
        if not isinstance(raw, bytes) or not raw or b"\x00" in raw or raw.startswith(b"/"):
            raise CandidateValidationError("candidate raw path is invalid")
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise CandidateValidationError("candidate raw path is not valid UTF-8") from exc
        parts = text.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise CandidateValidationError("candidate raw path is invalid")
        if any(unicodedata.normalize("NFC", part).casefold() == ".git" for part in parts):
            raise CandidateValidationError("candidate path aliases Git metadata")
        alias = unicodedata.normalize("NFC", text).casefold()
        previous = aliases.get(alias)
        if previous is not None and previous != raw:
            raise CandidateValidationError("candidate raw paths contain an alias")
        aliases[alias] = raw
        output.append(raw)
    return tuple(output)


def _check_scan_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise CandidateValidationError("candidate scan deadline expired")


def _stable_file_bytes_at(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
    deadline: float,
) -> bytes:
    _check_scan_deadline(deadline)
    if expected.st_size > MAX_CANDIDATE_FILE_BYTES:
        raise CandidateValidationError("candidate file exceeds the bounded size limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        data = bytearray()
        while len(data) <= MAX_CANDIDATE_FILE_BYTES:
            _check_scan_deadline(deadline)
            chunk = os.read(descriptor, min(1024 * 1024, MAX_CANDIDATE_FILE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if len(data) > MAX_CANDIDATE_FILE_BYTES or not (
        identity(expected) == identity(opened) == identity(after_open) == identity(after_path)
    ):
        raise CandidateProofStale("candidate file did not remain stable")
    return bytes(data)


def _scan_candidate_tree(
    root: str | Path,
    *,
    deadline: float | None = None,
) -> tuple[tuple[_TreeRecord, ...], tuple[bytes, ...], bytes]:
    deadline = (
        time.monotonic() + DEFAULT_CANDIDATE_SCAN_SECONDS
        if deadline is None
        else deadline
    )
    _check_scan_deadline(deadline)
    root_path = Path(root)
    root_info = root_path.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise CandidateValidationError("candidate tree root is not a stable directory")
    root_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    root_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root_path, root_flags)
    opened_root = os.fstat(root_fd)
    if (opened_root.st_dev, opened_root.st_ino, opened_root.st_mode) != (
        root_info.st_dev,
        root_info.st_ino,
        root_info.st_mode,
    ):
        os.close(root_fd)
        raise CandidateProofStale("candidate tree root identity changed")
    records: list[_TreeRecord] = []
    paths: list[bytes] = []
    total_bytes = 0
    total_path_bytes = 0

    def walk(directory_fd: int, prefix: bytes, depth: int) -> None:
        nonlocal total_bytes, total_path_bytes
        _check_scan_deadline(deadline)
        if depth > MAX_CANDIDATE_DEPTH:
            raise CandidateValidationError("candidate tree exceeds the depth limit")
        before = os.fstat(directory_fd)
        children: list[tuple[str, bytes, os.stat_result]] = []
        with os.scandir(directory_fd) as iterator:
            for child in iterator:
                _check_scan_deadline(deadline)
                name = child.name
                raw_name = os.fsencode(name)
                relative = raw_name if not prefix else prefix + b"/" + raw_name
                paths.append(relative)
                total_path_bytes += len(relative)
                if len(paths) > MAX_CANDIDATE_ENTRIES:
                    raise CandidateValidationError("candidate tree exceeds the entry limit")
                if total_path_bytes > MAX_CANDIDATE_PATH_BYTES:
                    raise CandidateValidationError("candidate tree exceeds the path byte limit")
                children.append((
                    name,
                    relative,
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False),
                ))
        children.sort(key=lambda item: item[1])
        for name, relative, info in children:
            _check_scan_deadline(deadline)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                if mode != 0o755:
                    raise CandidateValidationError("candidate directory mode is not Git-representable")
                child_fd = os.open(name, root_flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino, opened.st_mode) != (
                        info.st_dev,
                        info.st_ino,
                        info.st_mode,
                    ):
                        raise CandidateProofStale("candidate directory identity changed")
                    records.append(_TreeRecord(relative, "directory", mode, b""))
                    walk(child_fd, relative, depth + 1)
                finally:
                    os.close(child_fd)
                rebound = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (rebound.st_dev, rebound.st_ino, rebound.st_mode) != (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                ):
                    raise CandidateProofStale("candidate directory path was rebound")
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise CandidateValidationError("candidate tree contains a hardlink")
                if mode not in (0o644, 0o755):
                    raise CandidateValidationError("candidate file mode is not Git-representable")
                data = _stable_file_bytes_at(directory_fd, name, info, deadline)
                total_bytes += len(data)
                if total_bytes > MAX_CANDIDATE_TOTAL_BYTES:
                    raise CandidateValidationError("candidate tree exceeds the total byte limit")
                records.append(_TreeRecord(relative, "regular", mode, data))
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=directory_fd)
                if isinstance(target, str):
                    target = os.fsencode(target)
                rebound = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (rebound.st_dev, rebound.st_ino, rebound.st_mode) != (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                ):
                    raise CandidateProofStale("candidate symlink path was rebound")
                records.append(_TreeRecord(relative, "symlink", mode, target))
            else:
                raise CandidateValidationError("candidate tree contains a special file")
        after = os.fstat(directory_fd)
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
            raise CandidateProofStale("candidate directory did not remain stable")

    try:
        walk(root_fd, b"", 0)
    finally:
        os.close(root_fd)
    validated = validate_raw_candidate_paths(paths)
    records.sort(key=lambda item: item.path)
    witness = hashlib.sha256()
    for record in records:
        witness.update(len(record.path).to_bytes(8, "big") + record.path)
        witness.update(record.kind.encode("ascii") + b"\0")
        witness.update(record.mode.to_bytes(4, "big") + record.data)
    return tuple(records), validated, witness.digest()


def seal_candidate_attempt(
    attempt: CandidateAttempt, *, deadline: float | None = None,
) -> SealedCandidate:
    if not isinstance(attempt, CandidateAttempt):
        raise CandidateValidationError("candidate attempt is invalid")
    deadline = (
        time.monotonic() + DEFAULT_CANDIDATE_SCAN_SECONDS
        if deadline is None
        else deadline
    )
    first = _scan_candidate_tree(attempt.source_dir, deadline=deadline)
    second = _scan_candidate_tree(attempt.source_dir, deadline=deadline)
    if first != second:
        raise CandidateProofStale("candidate tree did not remain stable")
    return SealedCandidate(attempt, first[0], first[2].hex())


def _path_is_leased(path: bytes, leases: tuple[str, ...]) -> bool:
    text = path.decode("utf-8")
    return any(
        lease.rstrip("/") in ("", ".")
        or text == lease.rstrip("/")
        or text.startswith(lease.rstrip("/") + "/")
        for lease in leases
    )


def _validated_delta(
    sealed: SealedCandidate,
    spec: CandidateSpec,
    *,
    deadline: float | None = None,
) -> tuple[bytes, ...]:
    if deadline is not None and time.monotonic() >= deadline:
        raise CandidateExecutionError("candidate freeze deadline expired")
    base = {item.path: item for item in sealed.attempt._base_records}
    current = {item.path: item for item in sealed.records}
    populated_directories: set[bytes] = set()
    for path, record in current.items():
        if record.kind == "directory":
            continue
        parts = path.split(b"/")
        for index in range(1, len(parts)):
            populated_directories.add(b"/".join(parts[:index]))
    for path, record in current.items():
        if deadline is not None and time.monotonic() >= deadline:
            raise CandidateExecutionError("candidate freeze deadline expired")
        if record.kind == "directory" and path not in populated_directories:
            raise CandidateValidationError(
                "candidate empty directory is not Git-representable"
            )
    changed_all = sorted(
        path for path in set(base) | set(current) if base.get(path) != current.get(path)
    )
    if spec.read_only and changed_all:
        raise CandidateValidationError("read-only candidate changed the source tree")
    for path in changed_all:
        if not _path_is_leased(path, spec.allowed_paths):
            raise CandidateValidationError("candidate change is outside the write lease")
        before = base.get(path)
        after = current.get(path)
        if (before and before.kind == "symlink") or (after and after.kind == "symlink"):
            raise CandidateValidationError("candidate changed an unsupported symlink")
        if before is not None and after is not None and before.kind != after.kind:
            raise CandidateValidationError("candidate changed an unsupported path type")
    for expected in spec.expected_artifacts:
        record = current.get(expected.encode("utf-8"))
        if record is None or record.kind != "regular":
            raise CandidateValidationError("candidate expected artifact is missing")
    return tuple(
        path for path in changed_all
        if (current.get(path) or base.get(path)).kind != "directory"
    )


def _write_blob(
    repo: RepoIdentity,
    data: bytes,
    *,
    deadline: float,
    environment: Mapping[str, str],
) -> str:
    result = _git(
        repo,
        "hash-object",
        "-w",
        "--stdin",
        input_bytes=data,
        extra_environment=environment,
        deadline=deadline,
    )
    return result.stdout.decode("ascii").strip()


def _write_tree(
    repo: RepoIdentity,
    snapshot: SourceSnapshot,
    sealed: SealedCandidate,
    changed: tuple[bytes, ...],
    *,
    deadline: float,
) -> str:
    if not changed:
        return snapshot.tree_oid
    current = {item.path: item for item in sealed.records}
    zero_oid = b"0" * len(snapshot.tree_oid)
    with tempfile.TemporaryDirectory(
        prefix="bestplan-index-", dir=sealed.attempt.control_dir,
    ) as index_root:
        index_path = Path(index_root) / "index"
        environment = {"GIT_INDEX_FILE": str(index_path)}
        _git(
            repo,
            "read-tree",
            snapshot.tree_oid,
            extra_environment=environment,
            deadline=deadline,
        )
        index_lines: list[bytes] = []
        for path in changed:
            if time.monotonic() >= deadline:
                raise CandidateExecutionError("candidate freeze deadline expired")
            record = current.get(path)
            if record is None:
                index_lines.append(b"0 " + zero_oid + b"\t" + path + b"\0")
                continue
            if record.kind == "regular":
                mode = b"100755" if record.mode == 0o755 else b"100644"
            elif record.kind == "symlink":
                mode = b"120000"
            else:
                raise CandidateValidationError("candidate tree record kind is invalid")
            oid = _write_blob(
                repo, record.data, deadline=deadline, environment=environment,
            ).encode("ascii")
            index_lines.append(mode + b" " + oid + b"\t" + path + b"\0")
        _git(
            repo,
            "update-index",
            "-z",
            "--index-info",
            input_bytes=b"".join(index_lines),
            extra_environment=environment,
            deadline=deadline,
        )
        result = _git(
            repo,
            "write-tree",
            extra_environment=environment,
            deadline=deadline,
        )
        return result.stdout.decode("ascii").strip()


def _write_commit(
    repo: RepoIdentity,
    tree_oid: str,
    parent_oid: str,
    spec: CandidateSpec,
    *,
    deadline: float,
) -> str:
    message = (
        "Hermes BestPlan candidate\n\n"
        f"plan={spec.plan_id}\n"
        f"candidate={spec.candidate_id}\n"
        f"slice={spec.slice_id}\n"
    ).encode("utf-8")
    environment = {
        "GIT_AUTHOR_NAME": "Hermes BestPlan",
        "GIT_AUTHOR_EMAIL": "bestplan@localhost",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_NAME": "Hermes BestPlan",
        "GIT_COMMITTER_EMAIL": "bestplan@localhost",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    result = _git(
        repo,
        "commit-tree",
        tree_oid,
        "-p",
        parent_oid,
        input_bytes=message,
        extra_environment=environment,
        deadline=deadline,
    )
    return result.stdout.decode("ascii").strip()


def _verify_frozen_git_objects(
    repo: RepoIdentity,
    *,
    ref_name: str,
    commit_oid: str,
    tree_oid: str,
    parent_oid: str,
    deadline: float,
) -> None:
    object_type = _git(
        repo, "cat-file", "-t", commit_oid, deadline=deadline,
    ).stdout.strip()
    actual_tree = _git(
        repo, "rev-parse", f"{commit_oid}^{{tree}}", deadline=deadline,
    ).stdout.decode("ascii").strip()
    parents = _git(
        repo, "rev-list", "--parents", "-n", "1", commit_oid,
        deadline=deadline,
    ).stdout.decode("ascii").split()
    if (
        object_type != b"commit"
        or actual_tree != tree_oid
        or parents != [commit_oid, parent_oid]
        or _read_ref(repo, ref_name, deadline=deadline) != commit_oid
    ):
        raise CandidateExecutionError("candidate frozen Git proof differs")


def _freeze_sealed_candidate(
    snapshot: SourceSnapshot,
    sealed: SealedCandidate,
    spec: CandidateSpec,
    *,
    raw_receipt: Mapping[str, object],
    deadline: float | None = None,
) -> FrozenCandidateArtifact:
    if not isinstance(snapshot, SourceSnapshot) or not isinstance(sealed, SealedCandidate):
        raise CandidateValidationError("candidate freeze inputs are invalid")
    if sealed.attempt.ref_name != candidate_ref_name(spec.plan_id, spec.slice_id, sealed.attempt.attempt_id):
        raise CandidateValidationError("candidate attempt differs from the approved spec")
    deadline = (
        time.monotonic() + DEFAULT_CANDIDATE_GIT_SECONDS
        if deadline is None
        else float(deadline)
    )
    if time.monotonic() >= deadline:
        raise CandidateExecutionError("candidate freeze deadline expired")
    _assert_repository_identity(snapshot.repo, deadline=deadline)
    try:
        receipt_json = json.dumps(
            raw_receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise CandidateValidationError("candidate receipt is invalid") from None
    if len(receipt_json.encode("utf-8")) > 256 * 1024:
        raise CandidateValidationError("candidate receipt exceeds the bounded limit")
    changed = _validated_delta(sealed, spec, deadline=deadline)
    tree_oid = _write_tree(
        snapshot.repo, snapshot, sealed, changed, deadline=deadline,
    )
    commit_oid = _write_commit(
        snapshot.repo, tree_oid, snapshot.head_oid, spec, deadline=deadline,
    )
    anchored = False
    try:
        _anchor_candidate_ref(
            snapshot.repo, sealed.attempt.ref_name, commit_oid, deadline=deadline,
        )
        anchored = True
        _verify_frozen_git_objects(
            snapshot.repo,
            ref_name=sealed.attempt.ref_name,
            commit_oid=commit_oid,
            tree_oid=tree_oid,
            parent_oid=snapshot.head_oid,
            deadline=deadline,
        )
        _assert_repository_identity(snapshot.repo, deadline=deadline)
        _delete_ref(
            snapshot.repo,
            sealed.attempt.base_ref_name,
            snapshot.head_oid,
            deadline=deadline,
        )
    except BaseException:
        if anchored:
            cleanup_deadline = max(deadline, time.monotonic() + 2.0)
            try:
                _delete_refs_transactionally(
                    snapshot.repo,
                    (
                        (sealed.attempt.ref_name, commit_oid),
                        (sealed.attempt.base_ref_name, snapshot.head_oid),
                    ),
                    deadline=cleanup_deadline,
                )
            except BaseException:
                raise CandidateExecutionError(
                    "candidate reference cleanup failed; reconciliation retained"
                ) from None
        raise
    receipt_bytes = receipt_json.encode("utf-8")
    immutable_receipt = _immutable_json_value(json.loads(receipt_json))
    if not isinstance(immutable_receipt, Mapping):
        raise CandidateValidationError("candidate receipt is invalid")
    return FrozenCandidateArtifact(
        candidate_id=spec.candidate_id,
        slice_id=spec.slice_id,
        attempt_id=sealed.attempt.attempt_id,
        commit_oid=commit_oid,
        tree_oid=tree_oid,
        ref_name=sealed.attempt.ref_name,
        changed_paths=changed,
        raw_receipt=immutable_receipt,
        raw_receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
    )


def _freeze_sealed_candidate_for_test(
    snapshot: SourceSnapshot,
    sealed: SealedCandidate,
    spec: CandidateSpec,
    *,
    raw_receipt: Mapping[str, object],
    deadline: float | None = None,
) -> FrozenCandidateArtifact:
    """Private artifact-freeze seam for focused host-plumbing tests."""

    return _freeze_sealed_candidate(
        snapshot, sealed, spec, raw_receipt=raw_receipt, deadline=deadline,
    )


def build_candidate_environment(
    attempt: CandidateAttempt,
    *,
    controller_source: str | Path,
    broker_fd: int,
) -> dict[str, str]:
    del controller_source
    if isinstance(broker_fd, bool) or not isinstance(broker_fd, int) or broker_fd < 0:
        raise CandidateValidationError("candidate broker descriptor is invalid")
    return {
        "HOME": str(attempt.runtime_dir),
        "HERMES_BESTPLAN_BROKER_FD": str(broker_fd),
        "HERMES_BESTPLAN_CHILD": "1",
        "HERMES_HOME": str(attempt.runtime_dir),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "TERMINAL_CWD": str(attempt.source_dir),
        "TMPDIR": str(attempt.scratch_dir),
    }


def parse_bounded_worker_output(stdout: str, stderr: str) -> dict[str, object]:
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise CandidateExecutionError("candidate worker output is invalid")
    if len(stdout.encode("utf-8", "replace")) > MAX_WORKER_STDOUT_BYTES or len(
        stderr.encode("utf-8", "replace")
    ) > MAX_WORKER_STDERR_BYTES:
        raise CandidateExecutionError("candidate worker output limit exceeded")
    if stdout.count(_RESULT_MARKER) != 1 or not stdout.startswith(_RESULT_MARKER):
        raise CandidateExecutionError("candidate worker result marker is invalid")
    raw = stdout[len(_RESULT_MARKER):]
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        raise CandidateExecutionError("candidate worker result is invalid") from None
    expected = {"api_calls", "duration_seconds", "error", "model", "status", "summary"}
    legacy_expected = {"api_calls", "error", "status", "summary"}
    if not isinstance(value, dict) or set(value) not in (expected, legacy_expected):
        raise CandidateExecutionError("candidate worker result fields are invalid")
    if value.get("status") != "completed" or value.get("error") is not None:
        raise CandidateExecutionError("candidate worker reported a fixed failure")
    summary = value.get("summary")
    if not isinstance(summary, str) or len(summary) > 16_000:
        raise CandidateExecutionError("candidate worker result summary is invalid")
    return value


def _observe_process_group_members(pgid: int, *, deadline: float) -> tuple[int, ...]:
    """Return live same-uid members using one bounded, deadline-aware ps read."""

    if time.monotonic() >= deadline:
        raise CandidateExecutionError("candidate worker group observation failed")
    try:
        observer = subprocess.Popen(
            ["/bin/ps", "-axo", "pid=,pgid=,uid=,state="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except BaseException:
        raise CandidateExecutionError(
            "candidate worker group observation failed"
        ) from None
    output = bytearray()
    overflow = threading.Event()
    read_error = threading.Event()
    eof = threading.Event()

    def drain() -> None:
        try:
            source = observer.stdout
            if source is None:
                read_error.set()
                return
            read = getattr(source, "read1", None)
            if read is None:
                read = source.read
            while True:
                chunk = read(64 * 1024)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "strict")
                remaining = max(0, MAX_PROCESS_OBSERVATION_BYTES + 1 - len(output))
                if remaining:
                    output.extend(chunk[:remaining])
                if len(output) > MAX_PROCESS_OBSERVATION_BYTES or len(chunk) > remaining:
                    overflow.set()
                    return
        except BaseException:
            read_error.set()
        finally:
            eof.set()

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    failure = False
    returncode: int | None = None
    while True:
        if overflow.is_set() or read_error.is_set() or time.monotonic() >= deadline:
            failure = True
            break
        returncode = observer.poll()
        if returncode is not None and eof.is_set():
            break
        time.sleep(0.005)
    if failure:
        try:
            observer.kill()
        except (OSError, ProcessLookupError):
            pass
    try:
        returncode = observer.wait(timeout=max(0.001, deadline - time.monotonic()))
    except BaseException:
        failure = True
        try:
            observer.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            observer.wait(timeout=0.1)
        except BaseException:
            pass
    reader.join(timeout=max(0.0, deadline - time.monotonic()))
    if reader.is_alive() or failure or overflow.is_set() or read_error.is_set():
        raise CandidateExecutionError("candidate worker group observation failed")
    if returncode != 0:
        raise CandidateExecutionError("candidate worker group observation failed")
    try:
        text = bytes(output).decode("ascii", "strict")
    except UnicodeError:
        raise CandidateExecutionError(
            "candidate worker group observation failed"
        ) from None
    members: list[int] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise CandidateExecutionError("candidate worker group observation failed")
        try:
            member_pid, member_pgid, uid = map(int, fields[:3])
        except ValueError:
            raise CandidateExecutionError(
                "candidate worker group observation failed"
            ) from None
        state = fields[3]
        if member_pgid == pgid and uid == os.getuid() and not state.startswith("Z"):
            members.append(member_pid)
    return tuple(members)


def terminate_process_group(process: object, *, grace_seconds: float = 1.0) -> None:
    pid = int(getattr(process, "pid"))
    grace = max(0.0, float(grace_seconds))
    observation_deadline = time.monotonic() + max(1.0, 2.0 * grace + 1.0)

    def members() -> tuple[int, ...]:
        return _observe_process_group_members(pid, deadline=observation_deadline)

    def signal_group(sig: signal.Signals) -> None:
        try:
            os.killpg(pid, sig)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        for member_pid in members():
            try:
                os.kill(member_pid, sig)
            except ProcessLookupError:
                pass

    signal_group(signal.SIGTERM)
    deadline = min(observation_deadline, time.monotonic() + grace)
    while members() and time.monotonic() < deadline:
        getattr(process, "poll")()
        time.sleep(0.01)
    if members():
        signal_group(signal.SIGKILL)
    wait = getattr(process, "wait", None)
    if callable(wait):
        try:
            wait(timeout=max(0.001, observation_deadline - time.monotonic()))
        except (subprocess.TimeoutExpired, ProcessLookupError):
            raise CandidateExecutionError("candidate worker group did not become extinct") from None
    while members() and time.monotonic() < observation_deadline:
        time.sleep(0.01)
    if members():
        raise CandidateExecutionError("candidate worker group did not become extinct")


def _receive_exact(channel: socket.socket, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        chunk = channel.recv(size - len(output))
        if not chunk:
            raise EOFError
        output.extend(chunk)
    return bytes(output)


def _receive_frame(channel: socket.socket) -> dict[str, object]:
    size = struct.unpack("!I", _receive_exact(channel, 4))[0]
    if size <= 0 or size > MAX_BROKER_FRAME_BYTES:
        raise CandidateExecutionError("candidate broker frame limit exceeded")
    raw = _receive_exact(channel, size)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise CandidateExecutionError("candidate broker request is invalid") from None
    if not isinstance(value, dict):
        raise CandidateExecutionError("candidate broker request is invalid")
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        raise CandidateExecutionError("candidate broker request is invalid") from None
    if canonical.encode("utf-8") != raw:
        raise CandidateExecutionError("candidate broker request is not canonical")
    return value


def _send_frame(channel: socket.socket, value: dict[str, object]) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_BROKER_FRAME_BYTES:
        raise CandidateExecutionError("candidate broker response limit exceeded")
    channel.sendall(struct.pack("!I", len(raw)) + raw)


@dataclass
class _BrokerAccounting:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    tool_argument_bytes: int = 0
    error: CandidateExecutionError | None = None
    error_event: threading.Event = field(default_factory=threading.Event, repr=False)


def _validate_host_tool_choice(value: object, advertised: frozenset[str]) -> None:
    if value is None or value in ("auto", "none"):
        return
    if value == "required":
        if advertised:
            return
        raise CandidateExecutionError("candidate broker tool choice is invalid")
    if not isinstance(value, dict) or set(value) != {"type", "function"}:
        raise CandidateExecutionError("candidate broker tool choice is invalid")
    function = value.get("function")
    if (
        value.get("type") != "function"
        or not isinstance(function, dict)
        or set(function) != {"name"}
        or function.get("name") not in advertised
    ):
        raise CandidateExecutionError("candidate broker tool choice is invalid")


def _validate_host_tool_arguments(name: str, value: object) -> None:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CandidateExecutionError("candidate authority tool arguments are invalid")
    required: dict[str, tuple[str, ...]] = {
        "read_file": ("path",),
        "write_file": ("path", "content"),
        "patch": ("path", "old_string", "new_string"),
        "search_files": ("pattern",),
    }
    optional: dict[str, frozenset[str]] = {
        "read_file": frozenset({"offset", "limit"}),
        "write_file": frozenset(),
        "patch": frozenset({"mode", "replace_all"}),
        "search_files": frozenset({"target", "path", "limit", "offset"}),
    }
    required_names = required.get(name)
    if required_names is None:
        raise CandidateExecutionError("candidate authority tool arguments are invalid")
    allowed = frozenset(required_names) | optional[name]
    if set(value) - allowed or any(field not in value for field in required_names):
        raise CandidateExecutionError("candidate authority tool arguments are invalid")
    for field in required_names:
        if not isinstance(value[field], str):
            raise CandidateExecutionError("candidate authority tool arguments are invalid")
    if name == "patch":
        if not value["old_string"] or value.get("mode", "replace") != "replace":
            raise CandidateExecutionError("candidate authority tool arguments are invalid")
        if "replace_all" in value and not isinstance(value["replace_all"], bool):
            raise CandidateExecutionError("candidate authority tool arguments are invalid")
    if name == "search_files":
        if not value["pattern"] or value.get("target", "content") not in ("content", "files"):
            raise CandidateExecutionError("candidate authority tool arguments are invalid")
        if "path" in value and not isinstance(value["path"], str):
            raise CandidateExecutionError("candidate authority tool arguments are invalid")
    bounds = {
        "offset": (0 if name == "search_files" else 1, 1_000_000 if name == "search_files" else 10_000_000),
        "limit": (1, 200 if name == "search_files" else 2000),
    }
    for field, (minimum, maximum) in bounds.items():
        if field in value:
            number = value[field]
            if isinstance(number, bool) or not isinstance(number, int) or not minimum <= number <= maximum:
                raise CandidateExecutionError("candidate authority tool arguments are invalid")


def _validate_host_request(
    envelope: dict[str, object], spec: CandidateSpec, accounting: _BrokerAccounting,
) -> BrokerTurnRequest:
    if set(envelope) != {"max_output_tokens", "request", "request_id"}:
        raise CandidateExecutionError("candidate broker request fields are invalid")
    expected_id = f"turn-{accounting.requests + 1:08d}"
    if envelope.get("request_id") != expected_id:
        raise CandidateExecutionError("candidate broker request identity is invalid")
    maximum = envelope.get("max_output_tokens")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= spec.max_output_tokens:
        raise CandidateExecutionError("candidate broker output budget is invalid")
    request = envelope.get("request")
    if not isinstance(request, dict):
        raise CandidateExecutionError("candidate broker request body is invalid")

    def contains_forbidden(value: object) -> bool:
        if isinstance(value, dict):
            return any(
                key in _HOST_FORBIDDEN_REQUEST_KEYS or contains_forbidden(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(contains_forbidden(item) for item in value)
        return False

    if (
        set(request) - _HOST_ALLOWED_MODEL_REQUEST_KEYS
        or not {"model", "messages", "stream"}.issubset(request)
        or contains_forbidden(request)
        or request.get("model") != spec.model
        or request.get("stream") is not False
    ):
        raise CandidateExecutionError("candidate broker routing boundary differs")
    if not isinstance(request.get("messages"), list):
        raise CandidateExecutionError("candidate broker messages are invalid")
    if ("max_completion_tokens" in request) == ("max_tokens" in request):
        raise CandidateExecutionError("candidate broker token request is invalid")
    requested_tokens = request.get("max_completion_tokens", request.get("max_tokens"))
    if isinstance(requested_tokens, bool) or not isinstance(requested_tokens, int) or not 1 <= requested_tokens <= maximum:
        raise CandidateExecutionError("candidate broker token request is invalid")
    tools = request.get("tools", [])
    if tools is None:
        tools = []
    if not isinstance(tools, list):
        raise CandidateExecutionError("candidate broker tool schema is invalid")
    allowed_names = _ALLOWED_TOOL_NAMES[spec.toolsets[0]]
    seen: set[str] = set()
    for schema in tools:
        function = schema.get("function") if isinstance(schema, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if (
            not isinstance(schema, dict)
            or schema.get("type") != "function"
            or not isinstance(function, dict)
            or name not in allowed_names
            or name in seen
            or schema != {
                "type": "function",
                "function": _HOST_CANDIDATE_TOOL_SCHEMAS.get(name),
            }
        ):
            raise CandidateExecutionError("candidate broker tool schema is invalid")
        seen.add(name)
    _validate_host_tool_choice(request.get("tool_choice"), frozenset(seen))
    accounting.requests += 1
    if accounting.requests > spec.request_budget:
        raise CandidateExecutionError("candidate broker request budget exhausted")
    try:
        request_json = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise CandidateExecutionError("candidate broker request body is invalid") from None
    return BrokerTurnRequest(expected_id, request_json, maximum)


def _validate_host_response(
    response: BrokerTurnResponse,
    request: BrokerTurnRequest,
    spec: CandidateSpec,
    accounting: _BrokerAccounting,
) -> None:
    if not isinstance(response, BrokerTurnResponse):
        raise CandidateExecutionError("candidate authority response type is invalid")
    try:
        response.validate_for_request(request.request_id)
        body = json.loads(response.response_json)
    except (ValueError, json.JSONDecodeError, RecursionError):
        raise CandidateExecutionError("candidate authority response is invalid") from None
    if not isinstance(body, dict) or body.get("model") != spec.model:
        raise CandidateExecutionError("candidate authority response model differs")
    if set(body) != {"id", "object", "created", "model", "choices", "usage"}:
        raise CandidateExecutionError("candidate authority response fields are invalid")
    usage = body.get("usage")
    if not isinstance(usage, dict) or set(usage) != {
        "prompt_tokens", "completion_tokens", "total_tokens",
    }:
        raise CandidateExecutionError("candidate authority response usage is invalid")
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    if (
        any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 100_000_000
            for value in (prompt_tokens, completion_tokens, total_tokens)
        )
        or prompt_tokens != response.input_tokens
        or completion_tokens != response.output_tokens
        or total_tokens != prompt_tokens + completion_tokens
    ):
        raise CandidateExecutionError("candidate authority response usage differs")
    try:
        request_body = json.loads(request.request_json)
    except (json.JSONDecodeError, RecursionError):
        raise CandidateExecutionError("candidate broker request proof is invalid") from None
    advertised = frozenset(
        schema["function"]["name"] for schema in request_body.get("tools", [])
    )
    requested_tokens = request_body.get(
        "max_completion_tokens", request_body.get("max_tokens"),
    )
    if response.output_tokens > requested_tokens:
        raise CandidateExecutionError("candidate authority output budget differs")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise CandidateExecutionError("candidate authority response choices are invalid")
    call_count = 0
    argument_bytes = 0
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        calls = message.get("tool_calls", []) if isinstance(message, dict) else None
        if calls is None:
            calls = []
        if not isinstance(calls, list):
            raise CandidateExecutionError("candidate authority tool response is invalid")
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            arguments = function.get("arguments") if isinstance(function, dict) else None
            call_count += 1
            if (
                call_count > MAX_BROKER_TOOL_CALLS_PER_RESPONSE
                or not isinstance(call, dict)
                or set(call) != {"id", "type", "function"}
                or call.get("type") != "function"
                or not isinstance(call.get("id"), str)
                or not call["id"]
                or len(call["id"].encode("utf-8")) > 128
                or not isinstance(function, dict)
                or set(function) != {"name", "arguments"}
                or name not in advertised
                or not isinstance(arguments, str)
            ):
                raise CandidateExecutionError("candidate authority tool response is invalid")
            try:
                parsed = json.loads(arguments)
            except (json.JSONDecodeError, RecursionError):
                raise CandidateExecutionError("candidate authority tool arguments are invalid") from None
            if not isinstance(parsed, dict) or len(arguments.encode("utf-8")) > 256 * 1024:
                raise CandidateExecutionError("candidate authority tool arguments are invalid")
            _validate_host_tool_arguments(name, parsed)
            argument_bytes += len(arguments.encode("utf-8"))
    if (
        accounting.tool_calls + call_count > MAX_BROKER_TOOL_CALLS_PER_ATTEMPT
        or accounting.tool_argument_bytes + argument_bytes
        > MAX_BROKER_TOOL_ARGUMENT_BYTES_PER_ATTEMPT
    ):
        raise CandidateExecutionError(
            "candidate authority aggregate tool budget exhausted"
        )
    accounting.tool_calls += call_count
    accounting.tool_argument_bytes += argument_bytes
    accounting.input_tokens += response.input_tokens
    accounting.output_tokens += response.output_tokens
    if accounting.input_tokens + accounting.output_tokens > spec.token_budget:
        raise CandidateExecutionError("candidate broker token budget exhausted")


def _pump_broker(
    channel: socket.socket,
    authority_client: object,
    capability: object,
    spec: CandidateSpec,
    accounting: _BrokerAccounting,
) -> None:
    try:
        while True:
            envelope = _receive_frame(channel)
            request = _validate_host_request(envelope, spec, accounting)
            response = authority_client.model_request(capability, request)
            _validate_host_response(response, request, spec, accounting)
            _send_frame(channel, {
                "ok": True,
                "request_id": request.request_id,
                "response_json": response.response_json,
            })
    except (EOFError, OSError):
        return
    except BaseException:
        accounting.error = CandidateExecutionError("candidate broker validation failed")
        accounting.error_event.set()
        try:
            channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _default_process_identity(pid: int, expected_executable: str | Path) -> WorkerIdentity:
    expected = Path(expected_executable).resolve(strict=True)
    if sys_platform_is_macos():
        import ctypes

        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        if ctypes.sizeof(ProcBsdInfo) != 136:
            raise CandidateExecutionError("candidate worker identity is unavailable")

        def process_info() -> ProcBsdInfo:
            info = ProcBsdInfo()
            size = library.proc_pidinfo(
                pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info),
            )
            if (
                size != ctypes.sizeof(info)
                or info.pbi_pid != pid
                or info.pbi_uid != os.getuid()
                or info.pbi_ruid != os.getuid()
                or info.pbi_pgid != pid
                or info.pbi_start_tvsec <= 0
                or info.pbi_start_tvusec >= 1_000_000
            ):
                raise CandidateExecutionError("candidate worker identity is unavailable")
            return info

        before = process_info()
        buffer = ctypes.create_string_buffer(4096)
        if library.proc_pidpath(pid, buffer, len(buffer)) <= 0:
            raise CandidateExecutionError("candidate worker identity is unavailable")
        actual = Path(os.fsdecode(buffer.value)).resolve(strict=True)
        file_before = actual.stat()
        executable_bytes = actual.read_bytes()
        file_after = actual.stat()
        after = process_info()
        process_key = lambda info: (
            info.pbi_pid,
            info.pbi_uid,
            info.pbi_ruid,
            info.pbi_pgid,
            info.pbi_start_tvsec,
            info.pbi_start_tvusec,
        )
        file_key = lambda info: (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        if process_key(before) != process_key(after) or file_key(file_before) != file_key(file_after):
            raise CandidateExecutionError("candidate worker identity changed")
        uid = int(before.pbi_ruid)
        start_id = (
            f"darwin-start:{before.pbi_start_tvsec}:"
            f"{before.pbi_start_tvusec}:{pid}"
        )
    else:
        try:
            import psutil

            process = psutil.Process(pid)
            before = (process.create_time(), process.uids(), process.exe())
            actual = Path(before[2]).resolve(strict=True)
            executable_bytes = actual.read_bytes()
            after = (process.create_time(), process.uids(), process.exe())
            if before != after or os.getpgid(pid) != pid:
                raise CandidateExecutionError("candidate worker identity changed")
            uid = int(before[1].real)
            start_id = f"process-start:{pid}:{int(before[0] * 1_000_000_000)}"
        except CandidateError:
            raise
        except BaseException:
            raise CandidateExecutionError("candidate worker identity is unavailable") from None
    if expected != expected.resolve(strict=True):
        raise CandidateExecutionError("candidate expected executable is invalid")
    digest = hashlib.sha256(executable_bytes).hexdigest()
    return WorkerIdentity(pid, uid, start_id, digest)


def sys_platform_is_macos() -> bool:
    import sys

    return sys.platform == "darwin"


def _wait_for_worker_identity(
    process: object,
    expected_executable: Path,
    resolver: Callable[[int, Path], WorkerIdentity],
    deadline: float,
    cancel_event: object | None = None,
) -> WorkerIdentity:
    expected_digest = hashlib.sha256(expected_executable.read_bytes()).hexdigest()
    start_id: str | None = None
    while time.monotonic() < deadline:
        if cancel_event is not None and bool(cancel_event.is_set()):
            raise CandidateExecutionError("candidate cancelled")
        if getattr(process, "poll")() is not None:
            raise CandidateExecutionError("candidate worker exited before admission")
        try:
            identity = resolver(int(getattr(process, "pid")), expected_executable)
        except BaseException:
            time.sleep(0.01)
            continue
        if identity.pid != int(getattr(process, "pid")) or identity.uid != os.getuid():
            raise CandidateExecutionError("candidate worker identity differs")
        if start_id is None:
            start_id = identity.process_start_id
        elif identity.process_start_id != start_id:
            raise CandidateExecutionError("candidate worker process identity changed")
        if identity.executable_sha256 == expected_digest:
            return identity
        time.sleep(0.01)
    raise CandidateExecutionError("candidate worker did not reach its pinned executable")


def _validated_cancel_event(cancel_event: object | None) -> threading.Event | None:
    if cancel_event is not None and not isinstance(cancel_event, threading.Event):
        raise CandidateValidationError("candidate cancellation signal is invalid")
    return cancel_event


class _BoundedProcessCapture:
    """Drain worker pipes without waiting for process exit or unbounded writes."""

    def __init__(self, process: object, payload: str):
        encoded = payload.encode("utf-8")
        if len(encoded) > 2 * 1024 * 1024:
            raise CandidateExecutionError("candidate worker input limit exceeded")
        self.process = process
        self.payload = payload
        self.outputs: dict[str, bytearray] = {
            "stdout": bytearray(),
            "stderr": bytearray(),
        }
        self.lock = threading.Lock()
        self.overflow = threading.Event()
        self.result_ready = threading.Event()
        self.stdout_eof = threading.Event()
        self.stderr_eof = threading.Event()
        self.input_done = threading.Event()
        self.input_error = threading.Event()
        self.threads: list[threading.Thread] = []

    def _inspect_stdout(self) -> None:
        raw = bytes(self.outputs["stdout"])
        marker = _RESULT_MARKER.encode("ascii")
        if len(raw) < len(marker):
            if not marker.startswith(raw):
                self.result_ready.set()
            return
        if not raw.startswith(marker) or raw.count(marker) != 1:
            self.result_ready.set()
            return
        payload = raw[len(marker):]
        if b"\n" in payload:
            # Candidate workers emit one newline-terminated result frame.  A
            # complete malformed line is still terminal and is rejected after
            # admission closes; it must not consume the remaining deadline.
            self.result_ready.set()
            return
        try:
            text = payload.decode("utf-8", "strict")
            _value, end = json.JSONDecoder().raw_decode(text)
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            return
        if not text[end:].strip():
            self.result_ready.set()

    def _drain(self, name: str, stream: object, limit: int) -> None:
        try:
            while True:
                source = stream.buffer if hasattr(stream, "buffer") else stream
                read = getattr(source, "read1", source.read)
                chunk = read(64 * 1024)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                with self.lock:
                    target = self.outputs[name]
                    remaining = max(0, limit + 1 - len(target))
                    if remaining:
                        target.extend(chunk[:remaining])
                    if len(target) > limit or len(chunk) > remaining:
                        self.overflow.set()
                    if name == "stdout":
                        self._inspect_stdout()
        except (OSError, ValueError):
            return
        finally:
            if name == "stdout":
                with self.lock:
                    self._inspect_stdout()
                self.stdout_eof.set()
            else:
                self.stderr_eof.set()

    def _write_input(self) -> None:
        try:
            self.process.stdin.write(self.payload)
            self.process.stdin.flush()
            self.process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            self.input_error.set()
        finally:
            self.input_done.set()

    def start(self) -> None:
        self.threads = [
            threading.Thread(
                target=self._drain,
                args=("stdout", self.process.stdout, MAX_WORKER_STDOUT_BYTES),
                daemon=True,
            ),
            threading.Thread(
                target=self._drain,
                args=("stderr", self.process.stderr, MAX_WORKER_STDERR_BYTES),
                daemon=True,
            ),
            threading.Thread(target=self._write_input, daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def close_input(self) -> None:
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass

    def finish(self) -> tuple[str, str]:
        for thread in self.threads:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in self.threads):
            raise CandidateExecutionError("candidate worker pipe did not close")
        self.close_input()
        with self.lock:
            stdout = bytes(self.outputs["stdout"])
            stderr = bytes(self.outputs["stderr"])
        if self.overflow.is_set():
            raise CandidateExecutionError("candidate worker output limit exceeded")
        return stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def _close_broker_channel(channel: socket.socket) -> None:
    try:
        channel.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        channel.close()
    except OSError:
        pass


def _run_and_freeze_candidate(
    *,
    snapshot: SourceSnapshot,
    spec: CandidateSpec,
    attempts_root: str | Path,
    controller_source: str | Path,
    controller_python: str | Path,
    runtime_read_paths: Iterable[str | Path],
    expected_controller: object,
    authority_client: object,
    timeout_seconds: float,
    attempt_id: str | None = None,
    cancel_event: object | None = None,
    sandbox_factory: Callable[..., object] | None = None,
    process_identity_resolver: Callable[[int, Path], WorkerIdentity] | None = None,
    process_group_reaper: Callable[..., None] = terminate_process_group,
) -> FrozenCandidate:
    from agent.bestplan_sandbox import create_bestplan_candidate_sandbox_launch

    cancel_event = _validated_cancel_event(cancel_event)
    if cancel_event is not None and bool(cancel_event.is_set()):
        raise CandidateExecutionError("candidate cancelled")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or not 0 < float(timeout_seconds) <= MAX_CANDIDATE_TIMEOUT_SECONDS
    ):
        raise CandidateValidationError("candidate timeout is invalid")
    controller_repository_id = getattr(expected_controller, "repository_id", None)
    if controller_repository_id != snapshot.repo.repository_id:
        raise CandidateValidationError(
            "candidate controller repository identity differs"
        )
    controller_source_path = _assert_controller_source_disjoint(
        controller_source, snapshot.repo,
    )
    _assert_attempts_root_disjoint(
        attempts_root, snapshot.repo, controller_source=controller_source_path,
    )
    if spec.expires_at <= time.time():
        raise CandidateValidationError("candidate spec expired before execution")
    deadline = time.monotonic() + float(timeout_seconds)
    try:
        parent_channel, child_channel = socket.socketpair()
    except OSError:
        raise CandidateExecutionError(
            "candidate broker channel allocation failed"
        ) from None
    try:
        if cancel_event is not None and bool(cancel_event.is_set()):
            raise CandidateExecutionError("candidate cancelled")
        attempt = create_candidate_attempt(
            snapshot,
            plan_id=spec.plan_id,
            slice_id=spec.slice_id,
            attempts_root=attempts_root,
            attempt_id=attempt_id,
        )
    except BaseException:
        _close_broker_channel(parent_channel)
        _close_broker_channel(child_channel)
        raise
    launch = None
    process = None
    capability = None
    pump_thread = None
    capture: _BoundedProcessCapture | None = None
    capture_finished = False
    accounting = _BrokerAccounting()
    ordinary_failure = True
    group_extinct = True
    factory = sandbox_factory or create_bestplan_candidate_sandbox_launch
    resolver = process_identity_resolver or _default_process_identity
    try:
        if cancel_event is not None and bool(cancel_event.is_set()):
            raise CandidateExecutionError("candidate cancelled")
        environment = build_candidate_environment(
            attempt,
            controller_source=controller_source_path,
            broker_fd=child_channel.fileno(),
        )
        launch = factory(
            workspace=attempt.source_dir,
            allowed_paths=spec.allowed_paths,
            read_only=spec.read_only,
            runtime_dir=attempt.runtime_dir,
            scratch_dir=attempt.scratch_dir,
            control_dir=attempt.control_dir,
            controller_source=controller_source_path,
            controller_python=controller_python,
            runtime_read_paths=tuple(runtime_read_paths),
            enabled_toolsets=spec.toolsets,
            expected_controller=expected_controller,
            worker_environment=environment,
            broker_fd=child_channel.fileno(),
        )
        if cancel_event is not None and bool(cancel_event.is_set()):
            raise CandidateExecutionError("candidate cancelled")
        process = launch.launch_worker()
        group_extinct = False
        child_channel.close()
        expected_executable = Path(controller_python).resolve(strict=True)
        identity = _wait_for_worker_identity(
            process,
            expected_executable,
            resolver,
            deadline,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and bool(cancel_event.is_set()):
            raise CandidateExecutionError("candidate cancelled")
        capability = authority_client.register_model_attempt(
            attempt.attempt_id,
            identity,
            spec.model,
            spec.request_budget,
            spec.token_budget,
            spec.expires_at,
        )
        pump_thread = threading.Thread(
            target=_pump_broker,
            args=(parent_channel, authority_client, capability, spec, accounting),
            daemon=True,
        )
        pump_thread.start()
        payload = {
            "allowed_paths": list(spec.allowed_paths),
            "goal": spec.goal,
            "max_iterations": spec.max_iterations,
            "read_only": spec.read_only,
            "runtime": {
                "bestplan_toolsets": list(spec.toolsets),
                "max_output_tokens": spec.max_output_tokens,
                "model": spec.model,
                "request_overrides": {},
            },
            "runtime_home": str(attempt.runtime_dir),
            "system_prompt": "Complete only the declared candidate goal using the approved file operations.",
            "task_id": attempt.attempt_id,
            "workspace": str(attempt.source_dir),
        }
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        stdout = ""
        stderr = ""
        execution_error: CandidateExecutionError | None = None
        has_pipes = all(
            getattr(process, name, None) is not None
            for name in ("stdin", "stdout", "stderr")
        )
        if has_pipes:
            capture = _BoundedProcessCapture(process, payload_json)
            capture.start()
            while True:
                if cancel_event is not None and bool(cancel_event.is_set()):
                    execution_error = CandidateExecutionError(
                        "candidate cancelled"
                    )
                    break
                if capture.overflow.is_set():
                    execution_error = CandidateExecutionError(
                        "candidate worker output limit exceeded"
                    )
                    break
                if capture.input_error.is_set():
                    execution_error = CandidateExecutionError(
                        "candidate worker input failed"
                    )
                    break
                if accounting.error_event.is_set():
                    execution_error = accounting.error or CandidateExecutionError(
                        "candidate broker validation failed"
                    )
                    break
                if capture.result_ready.is_set():
                    break
                if capture.stdout_eof.is_set():
                    execution_error = CandidateExecutionError(
                        "candidate worker exited before its final result"
                    )
                    break
                if time.monotonic() >= deadline:
                    execution_error = CandidateExecutionError("candidate worker timeout")
                    break
                time.sleep(0.01)
            if execution_error is None:
                try:
                    live_identity = resolver(
                        int(getattr(process, "pid")), expected_executable,
                    )
                except BaseException:
                    execution_error = CandidateExecutionError(
                        "candidate worker final identity is unavailable"
                    )
                else:
                    if live_identity != identity:
                        execution_error = CandidateExecutionError(
                            "candidate worker final identity differs"
                        )
        else:
            # Private dependency-injected tests may use a structural process fake.
            try:
                stdout, stderr = process.communicate(
                    payload_json, timeout=max(0.001, deadline - time.monotonic()),
                )
            except subprocess.TimeoutExpired:
                execution_error = CandidateExecutionError("candidate worker timeout")

        # Admission is closed before any wait/reap operation, including errors.
        _close_broker_channel(parent_channel)
        try:
            process_group_reaper(
                process, grace_seconds=min(1.0, float(timeout_seconds)),
            )
            group_extinct = True
        except BaseException:
            raise CandidateExecutionError(
                "candidate worker extinction proof failed; reconciliation retained"
            ) from None
        if capability is not None:
            authority_client.revoke_model_attempt(capability)
            capability = None
        if pump_thread is not None:
            pump_thread.join(timeout=1)
            if pump_thread.is_alive():
                execution_error = execution_error or CandidateExecutionError(
                    "candidate broker did not stop"
                )
        if capture is not None:
            try:
                stdout, stderr = capture.finish()
                capture_finished = True
            except CandidateExecutionError as exc:
                execution_error = exc
        if execution_error is not None:
            raise execution_error
        if accounting.error is not None or accounting.requests < 1:
            raise CandidateExecutionError("candidate broker proof is incomplete")
        if not has_pipes and getattr(process, "returncode", None) != 0:
            raise CandidateExecutionError("candidate worker exited with a fixed failure")
        result = parse_bounded_worker_output(stdout, stderr)
        if cancel_event is not None and bool(cancel_event.is_set()):
            raise CandidateExecutionError("candidate cancelled")
        if time.monotonic() >= deadline:
            raise CandidateExecutionError("candidate worker timeout")
        launch.verify_identity(deadline=deadline)
        if cancel_event is not None and bool(cancel_event.is_set()):
            raise CandidateExecutionError("candidate cancelled")
        if time.monotonic() >= deadline:
            raise CandidateExecutionError("candidate worker timeout")
        sealed = seal_candidate_attempt(attempt, deadline=deadline)
        if cancel_event is not None and bool(cancel_event.is_set()):
            raise CandidateExecutionError("candidate cancelled")
        artifact = _freeze_sealed_candidate(
            snapshot,
            sealed,
            spec,
            raw_receipt={
                "status": result["status"],
                "summary": result["summary"],
                "request_count": accounting.requests,
                "input_tokens": accounting.input_tokens,
                "output_tokens": accounting.output_tokens,
            },
            deadline=deadline,
        )
        policy_digest = getattr(launch, "policy_digest", None)
        controller_id = getattr(expected_controller, "controller_id", None)
        release_oid = getattr(expected_controller, "release_oid", None)
        artifact_sha256 = getattr(expected_controller, "artifact_sha256", None)
        if not all(isinstance(item, str) and item for item in (
            policy_digest, controller_id, release_oid, artifact_sha256,
        )):
            raise CandidateExecutionError("candidate attestation identity is incomplete")
        frozen = FrozenCandidate(
            candidate_id=artifact.candidate_id,
            slice_id=artifact.slice_id,
            attempt_id=artifact.attempt_id,
            commit_oid=artifact.commit_oid,
            tree_oid=artifact.tree_oid,
            ref_name=artifact.ref_name,
            changed_paths=artifact.changed_paths,
            raw_receipt=artifact.raw_receipt,
            raw_receipt_sha256=artifact.raw_receipt_sha256,
            policy_digest=policy_digest,
            controller_id=controller_id,
            controller_repository_id=controller_repository_id,
            controller_release_oid=release_oid,
            controller_artifact_sha256=artifact_sha256,
            admitted_requests=accounting.requests,
            admitted_input_tokens=accounting.input_tokens,
            admitted_output_tokens=accounting.output_tokens,
        )
        ordinary_failure = False
        return frozen
    except CandidateError:
        raise
    except BaseException:
        raise CandidateExecutionError("candidate execution failed") from None
    finally:
        _close_broker_channel(parent_channel)
        try:
            child_channel.close()
        except OSError:
            pass
        extinction_failure = False
        if process is not None and not group_extinct:
            try:
                process_group_reaper(
                    process, grace_seconds=min(1.0, float(timeout_seconds)),
                )
                group_extinct = True
            except BaseException:
                extinction_failure = True
        if capability is not None:
            try:
                authority_client.revoke_model_attempt(capability)
            except BaseException:
                pass
        if capture is not None and group_extinct and not capture_finished:
            try:
                capture.finish()
                capture_finished = True
            except CandidateError:
                pass
        if launch is not None:
            try:
                launch.close()
            except BaseException:
                pass
        if ordinary_failure and group_extinct:
            _delete_ref(snapshot.repo, attempt.base_ref_name, snapshot.head_oid)
        if extinction_failure:
            raise CandidateExecutionError(
                "candidate worker extinction proof failed; reconciliation retained"
            ) from None


def run_and_freeze_candidate(
    *,
    snapshot: SourceSnapshot,
    spec: CandidateSpec,
    attempts_root: str | Path,
    controller_source: str | Path,
    controller_python: str | Path,
    runtime_read_paths: Iterable[str | Path],
    expected_controller: object,
    authority_client: object,
    timeout_seconds: float,
    attempt_id: str | None = None,
    cancel_event: object | None = None,
) -> FrozenCandidate:
    """Run one candidate using only the production sandbox/identity/reaper."""

    from agent.bestplan_sandbox import create_bestplan_candidate_sandbox_launch

    return _run_and_freeze_candidate(
        snapshot=snapshot,
        spec=spec,
        attempts_root=attempts_root,
        controller_source=controller_source,
        controller_python=controller_python,
        runtime_read_paths=runtime_read_paths,
        expected_controller=expected_controller,
        authority_client=authority_client,
        timeout_seconds=timeout_seconds,
        attempt_id=attempt_id,
        cancel_event=cancel_event,
        sandbox_factory=create_bestplan_candidate_sandbox_launch,
        process_identity_resolver=_default_process_identity,
        process_group_reaper=terminate_process_group,
    )


def _run_and_freeze_candidate_for_test(**kwargs) -> FrozenCandidate:
    """Private dependency-injected seam for lifecycle characterization tests."""

    return _run_and_freeze_candidate(**kwargs)
