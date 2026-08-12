"""Small same-user execution primitives for BestPlan ``go``.

This module deliberately contains no publication, review, deployment, or live
authority.  It binds the captured local source, exact checks, and controller;
it also keeps provider credentials in the foreground model relay.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from agent.auxiliary_client import resolve_provider_client
from agent.bestplan_authority_client import (
    AuthorityProtocolError,
    AuthorityStatus,
    AuthorityUnavailable,
    BrokerCapability,
    BrokerTurnRequest,
    BrokerTurnResponse,
    ModelRequest,
    ModelResponse,
    WorkerIdentity,
)
from agent.bestplan_contract import (
    LOCAL_MAIN_REF,
    BoundCommand,
    ContractValidationError,
    ControllerIdentity,
    EnrolledRepository,
    _command_from_dict,
    _command_to_dict,
    _controller_from_dict,
    _controller_to_dict,
    _exact_mapping,
    _git_oid,
    _repository_from_dict,
    _repository_to_dict,
    _sha256,
    canonical_json,
    source_snapshot_digest,
)
from agent.bestplan_source import SourceSnapshot

if TYPE_CHECKING:
    from agent.bestplan_checks import CheckHostRuntime, PinnedRuntimePath
    from tools.delegate_tool import BestplanHostRuntime


LOCAL_GO_CONTRACT_SCHEMA = "hermes.bestplan.local-go.v1"
LOCAL_GO_CONTRACT_VERSION = 1
_LOCAL_GO_DIGEST_DOMAIN = b"hermes.bestplan.local-go.v1\0"
_LOCAL_GO_APPROVAL_DIGEST_DOMAIN = b"hermes.bestplan.local-go-approval.v1\0"
_LOCAL_CHECK_RUNTIME_DIGEST_DOMAIN = b"hermes.bestplan.local-check-runtime.v1\0"
_DEFAULT_CHECK_TIMEOUT_SECONDS = 600
_LOCAL_RUNTIME_CAPTURE_SECONDS = 60.0
_LOCAL_CAPTURE_SECONDS = _LOCAL_RUNTIME_CAPTURE_SECONDS
_LOCAL_PROBE_REAP_RESERVE_SECONDS = 0.5
_MAX_CHECK_MARKER_BYTES = 1024 * 1024
_MAX_PROBE_OUTPUT_BYTES = 64 * 1024
_MAX_LOCAL_PYTEST_NODES = 64
_MAX_LOCAL_PYTEST_NODE_BYTES = 16 * 1024
_MAX_BROKER_RESPONSE_BYTES = 2 * 1024 * 1024
_BROKER_INPUT_TOKEN_OVERHEAD = 4096
_LOCAL_NO_AUTH_API_KEY = "hermes-bestplan-no-auth"
_LOCAL_CONTROLLER_ID = "local-controller-v1"
_LOCAL_RUNTIME_ROOT_NAME = "bestplan-local-go"
_LOCAL_PLAN_ROOT_SCHEMA = "hermes.bestplan.local-plan-root.v1"
_LOCAL_PLAN_ROOT_DOMAIN = b"hermes.bestplan.local-plan-root.v1\0"
_LOCAL_PLAN_IDENTITY_LEAF = b".identity.json"
_LOCAL_OPERATION_TIMEOUT_SECONDS = 3600.0
_LOCAL_CANDIDATE_POLICY_VERSION = 1
_LOCAL_CANDIDATE_REQUEST_BUDGET = 64
_LOCAL_CANDIDATE_TOKEN_BUDGET = 262_144
_LOCAL_CANDIDATE_MAX_ITERATIONS = 64
_LOCAL_CANDIDATE_MAX_OUTPUT_TOKENS = 8192
_LOCAL_CANDIDATE_TIMEOUT_SECONDS = 900.0
_LOCAL_CANDIDATE_CAPABILITY_TTL_SECONDS = 1200.0
_RESPONSE_FIELDS = {"id", "object", "created", "model", "choices", "usage"}
_ALLOWED_REQUEST_OVERRIDE_FIELDS = frozenset({
    "frequency_penalty",
    "presence_penalty",
    "reasoning_effort",
    "seed",
    "stop",
    "temperature",
    "top_p",
})
_LOCAL_PYTEST_ARGV_PREFIX = ("-I", "-B", "-m", "pytest", "-q", "--")
_LOCAL_PYTEST_PATH_PART_RE = re.compile(r"[A-Za-z0-9_.-]+")
_LOCAL_PYTEST_SELECTOR_PART_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_.\[\]=,+@%-]*"
)


class LocalGoValidationError(ContractValidationError):
    """A local ``go`` input is incomplete, unsafe, or noncanonical."""


def _as_local_error(exc: BaseException) -> LocalGoValidationError:
    return LocalGoValidationError(str(exc))


def validate_local_go_contract(value: Any) -> dict[str, Any]:
    """Validate and normalize the narrow local-main execution contract."""

    keys = {
        "schema",
        "version",
        "mode",
        "repository",
        "source",
        "manifest_digest",
        "check_runtime_digest",
        "commands",
        "controller",
    }
    try:
        value = _exact_mapping(value, keys, "local-go contract")
        if value["schema"] != LOCAL_GO_CONTRACT_SCHEMA:
            raise ContractValidationError("local-go contract schema is unsupported")
        if (
            value["version"] != LOCAL_GO_CONTRACT_VERSION
            or isinstance(value["version"], bool)
        ):
            raise ContractValidationError("local-go contract version must be integer 1")
        if value["mode"] != "local_main":
            raise ContractValidationError("local-go contract mode must be local_main")

        repository = _repository_from_dict(
            value["repository"], "local-go contract.repository"
        )
        source = _exact_mapping(
            value["source"],
            {
                "base_oid",
                "tree_oid",
                "local_ref",
                "snapshot_digest",
                "source_digest",
                "protected_digest",
            },
            "local-go contract.source",
        )
        if source["local_ref"] != LOCAL_MAIN_REF:
            raise ContractValidationError(
                "local-go contract source must use refs/heads/main"
            )
        _git_oid(
            source["base_oid"],
            repository.object_format,
            "local-go contract source base_oid",
        )
        _git_oid(
            source["tree_oid"],
            repository.object_format,
            "local-go contract source tree_oid",
        )
        for name in ("snapshot_digest", "source_digest", "protected_digest"):
            _sha256(source[name], f"local-go contract source {name}")
        _sha256(value["manifest_digest"], "local-go contract manifest_digest")
        _sha256(
            value["check_runtime_digest"],
            "local-go contract check_runtime_digest",
        )

        if not isinstance(value["commands"], list) or not value["commands"]:
            raise ContractValidationError(
                "local-go contract requires at least one exact check"
            )
        commands = [
            _command_from_dict(item, f"local-go contract.commands[{index}]")
            for index, item in enumerate(value["commands"])
        ]
        if len({command.identifier for command in commands}) != len(commands):
            raise ContractValidationError(
                "local-go contract check identifiers must be unique"
            )

        controller = _controller_from_dict(
            value["controller"], "local-go contract.controller"
        )
        if controller.repository_id != repository.repository_id:
            raise ContractValidationError(
                "local-go contract controller crosses repository identity"
            )
        _git_oid(
            controller.release_oid,
            repository.object_format,
            "local-go contract controller release_oid",
        )

        normalized = {
            "schema": LOCAL_GO_CONTRACT_SCHEMA,
            "version": LOCAL_GO_CONTRACT_VERSION,
            "mode": "local_main",
            "repository": _repository_to_dict(repository),
            "source": dict(source),
            "manifest_digest": value["manifest_digest"],
            "check_runtime_digest": value["check_runtime_digest"],
            "commands": [_command_to_dict(command) for command in commands],
            "controller": _controller_to_dict(controller),
        }
        if canonical_json(normalized) != canonical_json(value):
            raise ContractValidationError("local-go contract is not canonical")
        return normalized
    except LocalGoValidationError:
        raise
    except ContractValidationError as exc:
        raise _as_local_error(exc) from exc


def build_local_go_contract(
    *,
    snapshot: SourceSnapshot,
    controller: ControllerIdentity,
    commands: tuple[BoundCommand, ...],
    manifest_digest: str,
    check_runtime_digest: str,
) -> dict[str, Any]:
    """Build one canonical local-main contract from trusted host inputs."""

    if not isinstance(snapshot, SourceSnapshot):
        raise LocalGoValidationError("snapshot must be a SourceSnapshot")
    if not snapshot.head_symbolic or snapshot.head_ref != LOCAL_MAIN_REF.encode("ascii"):
        raise LocalGoValidationError(
            "local go requires a source attached to refs/heads/main"
        )
    if not isinstance(controller, ControllerIdentity):
        raise LocalGoValidationError("controller identity is required")
    if not isinstance(commands, tuple) or not commands or any(
        not isinstance(command, BoundCommand) for command in commands
    ):
        raise LocalGoValidationError("at least one exact check command is required")
    try:
        repository = EnrolledRepository.from_repo_identity(snapshot.repo)
        contract = {
            "schema": LOCAL_GO_CONTRACT_SCHEMA,
            "version": LOCAL_GO_CONTRACT_VERSION,
            "mode": "local_main",
            "repository": _repository_to_dict(repository),
            "source": {
                "base_oid": snapshot.head_oid,
                "tree_oid": snapshot.tree_oid,
                "local_ref": LOCAL_MAIN_REF,
                "snapshot_digest": source_snapshot_digest(snapshot),
                "source_digest": snapshot.fingerprint,
                "protected_digest": snapshot.protected_manifest.digest,
            },
            "manifest_digest": manifest_digest,
            "check_runtime_digest": check_runtime_digest,
            "commands": [_command_to_dict(command) for command in commands],
            "controller": _controller_to_dict(controller),
        }
        return validate_local_go_contract(contract)
    except LocalGoValidationError:
        raise
    except ContractValidationError as exc:
        raise _as_local_error(exc) from exc


def local_go_contract_json(contract: Mapping[str, Any]) -> str:
    return canonical_json(validate_local_go_contract(contract))


def local_go_contract_digest(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _LOCAL_GO_DIGEST_DOMAIN
        + local_go_contract_json(contract).encode("utf-8")
    ).hexdigest()


def local_go_manifest_digest(manifest: Mapping[str, Any]) -> str:
    """Digest the exact canonical local-go manifest bytes."""

    if not isinstance(manifest, Mapping):
        raise LocalGoValidationError("local-go manifest must be an object")
    try:
        manifest_json = canonical_json(manifest)
    except ContractValidationError as exc:
        raise _as_local_error(exc) from exc
    return hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()


def local_go_approval_digest(
    manifest: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> str:
    """Bind one canonical manifest to one exact canonical local contract."""

    manifest_digest = local_go_manifest_digest(manifest)
    approved = validate_local_go_contract(contract)
    if approved["manifest_digest"] != manifest_digest:
        raise LocalGoValidationError(
            "local-go contract manifest digest differs from the manifest"
        )
    manifest_json = canonical_json(manifest)
    return hashlib.sha256(
        _LOCAL_GO_APPROVAL_DIGEST_DOMAIN
        + manifest_json.encode("utf-8")
        + b"\0"
        + local_go_contract_json(approved).encode("utf-8")
    ).hexdigest()


def render_local_go_contract(contract: Mapping[str, Any]) -> str:
    """Render the exact local consequence and separate remote boundary."""

    approved = validate_local_go_contract(contract)
    check_lines = tuple(
        "- approved check "
        + command["identifier"]
        + ": "
        + shlex.join((command["executable"], *command["argv"]))
        for command in approved["commands"]
    )
    source = approved["source"]
    return "\n".join(
        (
            "Approved BestPlan local execution:",
            f"- source: {source['local_ref']} at {source['base_oid']}",
            *check_lines,
            f"- check runtime: {approved['check_runtime_digest']}",
            "- go runs the approved slices, combines them, and runs these "
            "approved checks",
            "- after all checks pass, Hermes will fast-forward local `main` "
            "to the exact checked integration commit",
            "- go does not authorize a remote push; Hermes asks before that write",
        )
    )


def _bounded_local_capture_deadline(deadline: float | None) -> float:
    """Return one finite deadline within the shared local capture window."""

    now = time.monotonic()
    if deadline is None:
        requested = now + _LOCAL_CAPTURE_SECONDS
    elif (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(float(deadline))
    ):
        raise LocalGoValidationError("the local check runtime deadline is invalid")
    else:
        requested = float(deadline)
    bounded = min(requested, now + _LOCAL_CAPTURE_SECONDS)
    if bounded <= now:
        raise LocalGoValidationError("the local check runtime deadline expired")
    return bounded


def _local_capture_checkpoint(
    deadline: float,
    cancel_event: threading.Event | None = None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise LocalGoValidationError("the local check runtime capture was cancelled")
    if time.monotonic() >= deadline:
        raise LocalGoValidationError("the local check runtime deadline expired")


def _local_python_launch(
    controller_python: Path,
) -> tuple[Path, Path, Path | None]:
    launcher = Path(controller_python).expanduser().absolute()
    try:
        resolved = launcher.resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
            raise OSError("controller Python is not an executable regular file")
    except OSError as exc:
        raise LocalGoValidationError(
            "the pinned controller Python executable is unavailable"
        ) from exc
    pyvenv = launcher.parent.parent / "pyvenv.cfg"
    try:
        venv_info = pyvenv.lstat()
    except FileNotFoundError:
        pyvenv = None
    except OSError as exc:
        raise LocalGoValidationError(
            "the pinned controller Python environment is unavailable"
        ) from exc
    else:
        if not stat.S_ISREG(venv_info.st_mode) or stat.S_ISLNK(venv_info.st_mode):
            raise LocalGoValidationError(
                "the pinned controller Python environment is unsupported"
            )
    return launcher, resolved, pyvenv


def _capture_runtime_read_paths(
    launcher: Path,
    pyvenv: Path | None,
    *,
    budget: Any,
    deadline: float,
    cancel_event: threading.Event | None,
) -> tuple[tuple[PinnedRuntimePath, ...], tuple[dict[str, Any], ...]]:
    from agent.bestplan_checks import PinnedRuntimePath
    from agent.bestplan_sandbox import (
        _stable_artifact_tree_identity,
        pinned_candidate_runtime_paths,
    )

    paths = list(pinned_candidate_runtime_paths(launcher))
    if pyvenv is not None:
        paths.append(pyvenv)
    unique = sorted({Path(path).absolute() for path in paths}, key=str)
    pins: list[PinnedRuntimePath] = []
    identities: list[dict[str, Any]] = []
    for path in unique:
        _local_capture_checkpoint(deadline, cancel_event)
        identity = _stable_artifact_tree_identity(path, budget)
        digest = identity.get("sha256")
        if not isinstance(digest, str):
            raise LocalGoValidationError(
                "the pinned controller Python runtime identity is invalid"
            )
        pins.append(PinnedRuntimePath(path=path, sha256=digest))
        identities.append(dict(identity))
    return tuple(pins), tuple(identities)


def _captured_pytest_marker(
    snapshot: SourceSnapshot,
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> None:
    """Prove pytest support from bounded blobs in the admitted Git tree."""

    from agent.bestplan_source import _run_git_output, _tree_entries

    _local_capture_checkpoint(deadline, cancel_event)
    wanted = {b"pytest.ini", b"pyproject.toml", b"setup.cfg"}
    try:
        entries = {
            entry.path: entry
            for entry in _tree_entries(
                snapshot.repo, snapshot.tree_oid, deadline=deadline,
            )
            if entry.path in wanted
        }
        for path in sorted(entries):
            _local_capture_checkpoint(deadline, cancel_event)
            entry = entries[path]
            if entry.object_type != b"blob" or entry.mode not in {0o100644, 0o100755}:
                raise LocalGoValidationError(
                    "captured project check markers must be regular Git blobs"
                )
            _code, raw = _run_git_output(
                snapshot.repo.worktree_raw,
                "cat-file",
                "blob",
                entry.oid,
                deadline=deadline,
                max_output_bytes=_MAX_CHECK_MARKER_BYTES,
                digest_only=False,
            )
            if not isinstance(raw, bytes):
                raise LocalGoValidationError(
                    "captured project check marker is invalid"
                )
            if path == b"pytest.ini" and re.search(
                rb"(?m)^\s*\[\s*pytest\s*\]\s*(?:[#;].*)?$", raw,
            ):
                return
            if path == b"pyproject.toml" and re.search(
                rb"(?m)^\s*\[\s*tool\.pytest\.ini_options\s*\]\s*(?:#.*)?$",
                raw,
            ):
                return
            if path == b"setup.cfg" and re.search(
                rb"(?m)^\s*\[\s*tool:pytest\s*\]\s*(?:[#;].*)?$", raw,
            ):
                return
    except LocalGoValidationError:
        raise
    except Exception as exc:
        raise LocalGoValidationError(
            "the admitted source tree could not be inspected for checks"
        ) from exc
    raise LocalGoValidationError(
        "the admitted source tree has no supported exact check; "
        "the initial local-go policy supports pytest only"
    )


def _terminate_local_probe(process: subprocess.Popen[Any], deadline: float) -> bool:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
    return process.poll() is not None


def _read_local_probe_output(stream: Any) -> bytes:
    stream.seek(0)
    data = stream.read(_MAX_PROBE_OUTPUT_BYTES + 1)
    if len(data) > _MAX_PROBE_OUTPUT_BYTES:
        raise LocalGoValidationError("the local pytest probe output is oversized")
    return data


def _probe_local_pytest_import(
    *,
    launcher: Path,
    executable: Path,
    pyvenv: Path | None,
    runtime_read_paths: tuple[PinnedRuntimePath, ...],
    sandbox_executable: Path,
    deadline: float,
    cancel_event: threading.Event | None,
) -> Path:
    """Import pytest in the exact no-fork Task 6 sandbox before approval."""

    from agent.bestplan_checks import _check_profile_text

    _local_capture_checkpoint(deadline, cancel_event)
    if sys.platform != "darwin" or sandbox_executable != Path(
        "/usr/bin/sandbox-exec"
    ):
        raise LocalGoValidationError(
            "the initial local-go check requires macOS sandbox-exec"
        )
    operation_deadline = deadline - _LOCAL_PROBE_REAP_RESERVE_SECONDS
    if operation_deadline <= time.monotonic():
        raise LocalGoValidationError("the local check runtime deadline expired")

    process: subprocess.Popen[Any] | None = None
    with tempfile.TemporaryDirectory(prefix="hermes-bestplan-pytest-probe-") as raw_root:
        root = Path(raw_root).resolve(strict=True)
        integration = root / "integration"
        runtime = root / "runtime"
        scratch = root / "scratch"
        for path in (integration, runtime, scratch):
            path.mkdir(mode=0o700)
        profile_path = root / "profile.sb"
        profile_path.write_text(
            _check_profile_text(
                integration_root=integration,
                runtime_root=runtime,
                scratch_root=scratch,
                cache_roots=(),
                executable=executable,
                runtime_read_paths=tuple(item.path for item in runtime_read_paths),
                network_allowlist=(),
            ),
            encoding="utf-8",
        )
        environment = {
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        if pyvenv is not None:
            environment["__PYVENV_LAUNCHER__"] = str(launcher)
        probe = (
            "import json,pathlib,pytest,sys;"
            "sys.stdout.write(json.dumps(str(pathlib.Path(pytest.__file__).resolve())))"
        )
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    [
                        str(sandbox_executable),
                        "-f",
                        str(profile_path),
                        str(executable),
                        "-I",
                        "-B",
                        "-c",
                        probe,
                    ],
                    cwd=str(integration),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    close_fds=True,
                    start_new_session=True,
                )
            except OSError as exc:
                raise LocalGoValidationError(
                    "the local pytest sandbox probe could not start"
                ) from exc
            cancelled = False
            while process.poll() is None:
                cancelled = cancel_event is not None and cancel_event.is_set()
                if cancelled or time.monotonic() >= operation_deadline:
                    if not _terminate_local_probe(process, deadline):
                        raise LocalGoValidationError(
                            "the local pytest sandbox probe could not be reaped"
                        )
                    if cancelled:
                        raise LocalGoValidationError(
                            "the local check runtime capture was cancelled"
                        )
                    raise LocalGoValidationError(
                        "the local pytest sandbox probe deadline expired"
                    )
                time.sleep(
                    min(0.005, max(0.0, operation_deadline - time.monotonic()))
                )
            stdout = _read_local_probe_output(stdout_file)
            _stderr = _read_local_probe_output(stderr_file)
            if process.returncode != 0:
                raise LocalGoValidationError(
                    "the pinned local check runtime cannot import pytest "
                    "inside the Task 6 sandbox"
                )
    try:
        module_value = json.loads(stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise LocalGoValidationError(
            "the local pytest sandbox probe returned invalid evidence"
        ) from exc
    if not isinstance(module_value, str) or not module_value:
        raise LocalGoValidationError(
            "the local pytest sandbox probe returned invalid evidence"
        )
    module_path = Path(module_value)
    if not module_path.is_absolute() or not any(
        module_path == item.path or item.path in module_path.parents
        for item in runtime_read_paths
    ):
        raise LocalGoValidationError(
            "pytest resolved outside the pinned local check runtime"
        )
    return module_path


@dataclass(frozen=True)
class LocalCheckPlan:
    commands: tuple[BoundCommand, ...]
    runtime_read_paths: tuple[PinnedRuntimePath, ...]
    sandbox_executable: Path
    sandbox_executable_sha256: str
    policy_version: str
    check_runtime_digest: str
    pytest_module_path: Path


def _local_pytest_nodes(config: Mapping[str, Any] | None) -> tuple[str, ...]:
    try:
        exact = _exact_mapping(config, {"pytest_nodes"}, "local check config")
    except ContractValidationError as exc:
        raise _as_local_error(exc) from exc
    nodes = exact["pytest_nodes"]
    if not isinstance(nodes, list) or not nodes:
        raise LocalGoValidationError(
            "local check config pytest_nodes must be a nonempty list"
        )
    if len(nodes) > _MAX_LOCAL_PYTEST_NODES:
        raise LocalGoValidationError("local pytest nodes are oversized")

    total_bytes = 0
    validated: list[str] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, str) or not node:
            raise LocalGoValidationError(
                f"local pytest node {index} is invalid"
            )
        try:
            total_bytes += len(node.encode("utf-8", "strict"))
        except UnicodeError as exc:
            raise LocalGoValidationError(
                f"local pytest node {index} is invalid"
            ) from exc
        if total_bytes > _MAX_LOCAL_PYTEST_NODE_BYTES:
            raise LocalGoValidationError("local pytest nodes are oversized")

        parts = node.split("::")
        path_text, selectors = parts[0], parts[1:]
        path = PurePosixPath(path_text)
        path_parts = path.parts
        if (
            path.is_absolute()
            or path.as_posix() != path_text
            or len(path_parts) < 2
            or path_parts[0] != "tests"
            or ".." in path_parts
            or path.suffix != ".py"
            or any(
                _LOCAL_PYTEST_PATH_PART_RE.fullmatch(part) is None
                for part in path_parts
            )
            or any(
                _LOCAL_PYTEST_SELECTOR_PART_RE.fullmatch(selector) is None
                for selector in selectors
            )
        ):
            raise LocalGoValidationError(
                f"local pytest node {index} is invalid"
            )
        validated.append(node)
    return tuple(validated)


def _local_check_config_from_manifest(
    manifest: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Extract exact, non-shell pytest nodes from each writable slice."""

    if not isinstance(manifest, Mapping):
        raise LocalGoValidationError("local-go manifest must be an object")
    slices = manifest.get("slices")
    if not isinstance(slices, list) or not slices:
        raise LocalGoValidationError("local-go manifest slices are invalid")

    ordered_nodes: list[str] = []
    seen: set[str] = set()
    writable_slices = 0
    for index, item in enumerate(slices):
        if not isinstance(item, Mapping) or not isinstance(
            item.get("read_only"), bool,
        ):
            raise LocalGoValidationError(
                f"local-go manifest slice {index} is invalid"
            )
        if item["read_only"]:
            continue
        writable_slices += 1
        acceptance = item.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            raise LocalGoValidationError(
                f"local-go manifest slice {index} acceptance is invalid"
            )
        slice_nodes: list[str] = []
        for entry in acceptance:
            if not isinstance(entry, str) or not entry:
                raise LocalGoValidationError(
                    f"local-go manifest slice {index} acceptance is invalid"
                )
            text = entry.strip()
            if not text.startswith("pytest"):
                continue
            tokens = text.split(" ")
            if (
                len(tokens) < 4
                or tokens[:3] != ["pytest", "-q", "--"]
                or any(not token for token in tokens)
            ):
                raise LocalGoValidationError(
                    f"local-go manifest slice {index} pytest acceptance is invalid"
                )
            slice_nodes.extend(tokens[3:])
        if not slice_nodes:
            raise LocalGoValidationError(
                f"local-go manifest slice {index} requires exact pytest acceptance"
            )
        for node in _local_pytest_nodes({"pytest_nodes": slice_nodes}):
            if node not in seen:
                seen.add(node)
                ordered_nodes.append(node)

    if not writable_slices:
        raise LocalGoValidationError(
            "local-go manifest requires a writable slice"
        )
    return {
        "pytest_nodes": list(
            _local_pytest_nodes({"pytest_nodes": ordered_nodes}),
        ),
    }


def derive_local_check_plan(
    *,
    snapshot: SourceSnapshot,
    controller_python: Path,
    config: Mapping[str, Any] | None,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
) -> LocalCheckPlan:
    """Derive the command and exact host runtime in one bounded capture."""

    from agent.bestplan_checks import CHECK_SANDBOX_POLICY_VERSION
    from agent.bestplan_sandbox import (
        _ArtifactBudget,
        _launcher_identity,
        _stable_artifact_tree_identity,
    )

    if not isinstance(snapshot, SourceSnapshot):
        raise LocalGoValidationError("snapshot must be a SourceSnapshot")
    pytest_nodes = _local_pytest_nodes(config)
    absolute_deadline = _bounded_local_capture_deadline(deadline)
    _captured_pytest_marker(
        snapshot, deadline=absolute_deadline, cancel_event=cancel_event,
    )
    launcher, executable, pyvenv = _local_python_launch(controller_python)
    sandbox_executable = Path("/usr/bin/sandbox-exec")
    try:
        budget = _ArtifactBudget(absolute_deadline)
        _local_capture_checkpoint(absolute_deadline, cancel_event)
        launcher_identity = _launcher_identity(launcher, executable, budget)
        runtime_read_paths, runtime_identities = _capture_runtime_read_paths(
            launcher,
            pyvenv,
            budget=budget,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
        _local_capture_checkpoint(absolute_deadline, cancel_event)
        sandbox_identity = _stable_artifact_tree_identity(
            sandbox_executable, budget,
        )
    except LocalGoValidationError:
        raise
    except Exception as exc:
        raise LocalGoValidationError(
            "the pinned local check runtime is unavailable"
        ) from exc

    resolved_identity = launcher_identity.get("resolved_identity")
    executable_sha256 = (
        resolved_identity.get("sha256")
        if isinstance(resolved_identity, Mapping)
        else None
    )
    sandbox_sha256 = sandbox_identity.get("sha256")
    if not isinstance(executable_sha256, str) or not isinstance(
        sandbox_sha256, str,
    ):
        raise LocalGoValidationError("the local check runtime identity is invalid")
    environment = [
        ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"),
        ("PYTHONHASHSEED", "0"),
    ]
    if pyvenv is not None:
        environment.append(("__PYVENV_LAUNCHER__", str(launcher)))
    commands = (
        BoundCommand(
            identifier="pytest",
            executable=str(executable),
            executable_sha256=executable_sha256,
            argv=(*_LOCAL_PYTEST_ARGV_PREFIX, *pytest_nodes),
            logical_cwd="integration",
            env=tuple(environment),
            inputs=(),
            cache=(),
            timeout_seconds=_DEFAULT_CHECK_TIMEOUT_SECONDS,
            network_allowlist=(),
        ),
    )
    pytest_module_path = _probe_local_pytest_import(
        launcher=launcher,
        executable=executable,
        pyvenv=pyvenv,
        runtime_read_paths=runtime_read_paths,
        sandbox_executable=sandbox_executable,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    runtime_body = {
        "schema": "hermes.bestplan.local-check-runtime.v1",
        "launcher": launcher_identity,
        "runtime_read_paths": list(runtime_identities),
        "sandbox": sandbox_identity,
        "policy_version": CHECK_SANDBOX_POLICY_VERSION,
        "pytest_module_path": str(pytest_module_path),
    }
    check_runtime_digest = hashlib.sha256(
        _LOCAL_CHECK_RUNTIME_DIGEST_DOMAIN
        + canonical_json(runtime_body).encode("utf-8")
    ).hexdigest()
    return LocalCheckPlan(
        commands=commands,
        runtime_read_paths=runtime_read_paths,
        sandbox_executable=sandbox_executable,
        sandbox_executable_sha256=sandbox_sha256,
        policy_version=CHECK_SANDBOX_POLICY_VERSION,
        check_runtime_digest=check_runtime_digest,
        pytest_module_path=pytest_module_path,
    )


@dataclass(frozen=True)
class LocalExecutionInputs:
    """Approval-time controller and check identities retained for local go."""

    controller_source: Path
    controller: ControllerIdentity
    check_plan: LocalCheckPlan

    def __post_init__(self) -> None:
        source = Path(self.controller_source)
        if (
            not source.is_absolute()
            or not isinstance(self.controller, ControllerIdentity)
            or not isinstance(self.check_plan, LocalCheckPlan)
        ):
            raise LocalGoValidationError("local execution inputs are invalid")
        object.__setattr__(self, "controller_source", source)


def _bounded_local_runtime_capture_deadline(deadline: float | None) -> float:
    now = time.monotonic()
    if deadline is None:
        requested = now + _LOCAL_RUNTIME_CAPTURE_SECONDS
    elif (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(float(deadline))
    ):
        raise LocalGoValidationError("the local runtime capture deadline is invalid")
    else:
        requested = float(deadline)
    bounded = min(requested, now + _LOCAL_RUNTIME_CAPTURE_SECONDS)
    if bounded <= now:
        raise LocalGoValidationError("the local runtime capture deadline expired")
    return bounded


def _local_runtime_checkpoint(
    deadline: float,
    cancel_event: threading.Event | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise LocalGoValidationError("the local runtime capture was cancelled")
    if time.monotonic() >= deadline:
        raise LocalGoValidationError("the local runtime capture deadline expired")


def _local_paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _private_local_directory(path: Path) -> Path:
    requested = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    try:
        requested.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise LocalGoValidationError(
            "the private local runtime directory could not be created"
        ) from exc
    try:
        before = requested.lstat()
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise LocalGoValidationError(
            "the private local runtime directory is unavailable"
        ) from exc
    if (
        resolved != requested
        or not stat.S_ISDIR(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o700
        or before.st_uid != os.geteuid()
    ):
        raise LocalGoValidationError(
            "the private local runtime directory is unsafe"
        )
    return resolved


def _local_runtime_root(snapshot: SourceSnapshot) -> Path:
    from hermes_constants import get_hermes_home

    try:
        home = Path(get_hermes_home()).expanduser().resolve(strict=True)
        home_info = home.stat(follow_symlinks=False)
    except OSError as exc:
        raise LocalGoValidationError("the active Hermes home is unavailable") from exc
    if not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid != os.geteuid():
        raise LocalGoValidationError("the active Hermes home is unsafe")
    root = _private_local_directory(home / _LOCAL_RUNTIME_ROOT_NAME)
    try:
        protected = tuple(
            Path(os.fsdecode(raw)).resolve(strict=True)
            for raw in (
                snapshot.repo.worktree_raw,
                snapshot.repo.git_dir_raw,
                snapshot.repo.common_dir_raw,
            )
        )
    except OSError as exc:
        raise LocalGoValidationError(
            "the target repository identity is unavailable"
        ) from exc
    if any(_local_paths_overlap(root, item) for item in protected):
        raise LocalGoValidationError(
            "the local runtime root overlaps target repository state"
        )
    return root


def _controller_path_is_secret_bearing(raw_path: bytes) -> bool:
    for raw_component in raw_path.split(b"/"):
        try:
            component = unicodedata.normalize(
                "NFC", raw_component.decode("utf-8", "strict"),
            ).casefold()
        except UnicodeError:
            return True
        if component == ".git" or component == ".env":
            return True
        if component.startswith(".env.") and component not in {
            ".env.example",
            ".env.sample",
            ".env.template",
        }:
            return True
        if component in {
            ".credentials",
            ".secrets",
            "credentials.json",
            "secrets.json",
        }:
            return True
    return False


def _validate_controller_entries(entries: Sequence[Any], *, deadline: float) -> None:
    from agent.bestplan_source import _assert_tree_path_aliases

    if not entries:
        raise LocalGoValidationError("the controller Git tree is empty")
    for entry in entries:
        if time.monotonic() >= deadline:
            raise LocalGoValidationError(
                "the local runtime capture deadline expired"
            )
        if entry.object_type != b"blob" or entry.mode not in {0o100644, 0o100755}:
            raise LocalGoValidationError(
                "the controller Git tree contains an unsupported entry"
            )
        if _controller_path_is_secret_bearing(entry.path):
            raise LocalGoValidationError(
                "the controller Git tree contains a secret or environment file"
            )
    try:
        _assert_tree_path_aliases(tuple(entries), deadline=deadline)
    except Exception as exc:
        raise LocalGoValidationError(
            "the controller Git tree contains unsafe path aliases"
        ) from exc
    if b"agent/bestplan_worker.py" not in {entry.path for entry in entries}:
        raise LocalGoValidationError(
            "the controller Git tree has no BestPlan worker"
        )


def _seal_controller_tree(directory_fd: int, *, deadline: float) -> None:
    def seal(current_fd: int) -> None:
        if time.monotonic() >= deadline:
            raise LocalGoValidationError(
                "the local runtime capture deadline expired"
            )
        with os.scandir(current_fd) as iterator:
            names = sorted(os.fsencode(item.name) for item in iterator)
        for name in names:
            if time.monotonic() >= deadline:
                raise LocalGoValidationError(
                    "the local runtime capture deadline expired"
                )
            info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                child_fd = os.open(name, flags, dir_fd=current_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise LocalGoValidationError(
                            "the retained controller tree changed during sealing"
                        )
                    seal(child_fd)
                    os.fchmod(child_fd, 0o500)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                file_fd = os.open(name, flags, dir_fd=current_fd)
                try:
                    opened = os.fstat(file_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise LocalGoValidationError(
                            "the retained controller tree changed during sealing"
                        )
                    os.fchmod(file_fd, 0o500 if info.st_mode & 0o111 else 0o400)
                finally:
                    os.close(file_fd)
            else:
                raise LocalGoValidationError(
                    "the retained controller tree contains a special file"
                )
        os.fchmod(current_fd, 0o500)

    seal(directory_fd)


def _make_controller_tree_deletable(directory_fd: int, *, deadline: float) -> None:
    if time.monotonic() >= deadline:
        return
    try:
        os.fchmod(directory_fd, 0o700)
        with os.scandir(directory_fd) as iterator:
            names = [os.fsencode(item.name) for item in iterator]
        for name in names:
            if time.monotonic() >= deadline:
                return
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    _make_controller_tree_deletable(child_fd, deadline=deadline)
                finally:
                    os.close(child_fd)
    except OSError:
        return


def _discard_controller_stage(
    parent_fd: int,
    leaf: bytes,
    directory_fd: int,
    identity: tuple[int, int],
    *,
    deadline: float,
) -> None:
    from agent.bestplan_source import _remove_owned_tree_contents

    if time.monotonic() >= deadline:
        return
    _make_controller_tree_deletable(directory_fd, deadline=deadline)
    _remove_owned_tree_contents(directory_fd, deadline=deadline)
    current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
        or (opened.st_dev, opened.st_ino) != identity
    ):
        raise LocalGoValidationError(
            "the retained controller staging identity changed"
        )
    os.rmdir(leaf, dir_fd=parent_fd)


def _verify_retained_controller(
    path: Path,
    expected_sha256: str,
    *,
    deadline: float,
) -> None:
    from agent.bestplan_sandbox import (
        _new_artifact_budget,
        _stable_artifact_tree_identity,
    )

    try:
        root_info = path.lstat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or stat.S_IMODE(root_info.st_mode) != 0o500
            or root_info.st_uid != os.geteuid()
        ):
            raise LocalGoValidationError(
                "the retained controller root changed"
            )
        identity = _stable_artifact_tree_identity(
            path, _new_artifact_budget(deadline),
        )
    except LocalGoValidationError:
        raise
    except Exception as exc:
        raise LocalGoValidationError(
            "the retained controller artifact changed"
        ) from exc
    if identity.get("sha256") != expected_sha256:
        raise LocalGoValidationError("the retained controller artifact changed")
    worker = path / "agent" / "bestplan_worker.py"
    try:
        worker_info = worker.lstat()
    except OSError as exc:
        raise LocalGoValidationError(
            "the retained controller worker changed"
        ) from exc
    if (
        not stat.S_ISREG(worker_info.st_mode)
        or stat.S_ISLNK(worker_info.st_mode)
        or worker_info.st_mode & 0o222
    ):
        raise LocalGoValidationError("the retained controller worker changed")


def _controller_commit_tree(
    checkout: Path,
    *,
    release_oid: str | None,
    deadline: float,
) -> tuple[Any, str, tuple[Any, ...]]:
    from agent.bestplan_source import (
        _assert_supported_repository,
        _run_git_output,
        _tree_entries,
        resolve_repo_identity,
    )

    try:
        requested = Path(checkout).expanduser().resolve(strict=True)
        repo = resolve_repo_identity(requested, deadline=deadline)
        if Path(repo.worktree).resolve(strict=True) != requested:
            raise LocalGoValidationError(
                "the Hermes controller must be a Git checkout root"
            )
        current_oid, _current_tree, current_entries = _assert_supported_repository(
            repo, deadline=deadline, scan_specials=False,
        )
        selected_oid = current_oid if release_oid is None else release_oid
        if release_oid is None:
            entries = current_entries
        else:
            _code, commit_raw = _run_git_output(
                repo.worktree_raw,
                "rev-parse",
                "--verify",
                f"{selected_oid}^{{commit}}",
                deadline=deadline,
                max_output_bytes=4096,
                digest_only=False,
            )
            if not isinstance(commit_raw, bytes) or commit_raw.strip().decode(
                "ascii",
            ) != selected_oid:
                raise LocalGoValidationError(
                    "the retained controller release is unavailable"
                )
            _code, tree_raw = _run_git_output(
                repo.worktree_raw,
                "rev-parse",
                "--verify",
                f"{selected_oid}^{{tree}}",
                deadline=deadline,
                max_output_bytes=4096,
                digest_only=False,
            )
            if not isinstance(tree_raw, bytes):
                raise LocalGoValidationError(
                    "the retained controller release is unavailable"
                )
            entries = _tree_entries(
                repo, tree_raw.strip().decode("ascii"), deadline=deadline,
            )
        _validate_controller_entries(entries, deadline=deadline)
        return repo, selected_oid, tuple(entries)
    except LocalGoValidationError:
        raise
    except Exception as exc:
        raise LocalGoValidationError(
            "the Hermes controller must be a resolvable Git checkout"
        ) from exc


def _retain_local_controller(
    *,
    snapshot: SourceSnapshot,
    controller_checkout: Path,
    expected: ControllerIdentity | None,
    deadline: float,
    cancel_event: threading.Event | None,
    runtime_root: Path | None = None,
) -> tuple[Path, ControllerIdentity]:
    from agent.bestplan_sandbox import (
        _new_artifact_budget,
        _stable_artifact_tree_identity,
    )
    from agent.bestplan_source import (
        _atomic_publish_backend,
        _materialize_blobs,
        _rename_leaf_no_replace,
    )

    _local_runtime_checkpoint(deadline, cancel_event)
    if expected is not None:
        if (
            not isinstance(expected, ControllerIdentity)
            or expected.repository_id != snapshot.repo.repository_id
            or expected.controller_id != _LOCAL_CONTROLLER_ID
        ):
            raise LocalGoValidationError(
                "the stored local controller identity differs"
            )
    owned_runtime_root = (
        _local_runtime_root(snapshot)
        if runtime_root is None
        else Path(runtime_root)
    )
    controllers_root = _private_local_directory(
        owned_runtime_root / "controllers",
    )
    repo, release_oid, entries = _controller_commit_tree(
        controller_checkout,
        release_oid=None if expected is None else expected.release_oid,
        deadline=deadline,
    )
    if expected is not None:
        retained = controllers_root / expected.artifact_sha256
        if retained.exists():
            _verify_retained_controller(
                retained, expected.artifact_sha256, deadline=deadline,
            )
            return retained, expected

    parent_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    parent_flags |= getattr(os, "O_DIRECTORY", 0)
    parent_flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(controllers_root, parent_flags)
    stage_leaf = f".capture-{secrets.token_hex(16)}".encode("ascii")
    stage_fd = -1
    stage_identity: tuple[int, int] | None = None
    published = False
    try:
        os.mkdir(stage_leaf, 0o700, dir_fd=parent_fd)
        stage_fd = os.open(stage_leaf, parent_flags, dir_fd=parent_fd)
        stage_info = os.fstat(stage_fd)
        stage_identity = (stage_info.st_dev, stage_info.st_ino)
        _materialize_blobs(repo, entries, stage_fd, deadline=deadline)
        _local_runtime_checkpoint(deadline, cancel_event)
        _seal_controller_tree(stage_fd, deadline=deadline)
        identity = _stable_artifact_tree_identity(
            controllers_root / os.fsdecode(stage_leaf),
            _new_artifact_budget(deadline),
        )
        artifact_sha256 = identity.get("sha256")
        if not isinstance(artifact_sha256, str):
            raise LocalGoValidationError(
                "the retained controller artifact identity is invalid"
            )
        if expected is not None and artifact_sha256 != expected.artifact_sha256:
            raise LocalGoValidationError(
                "the retained controller artifact differs from the stored contract"
            )
        final_leaf = artifact_sha256.encode("ascii")
        retained = controllers_root / artifact_sha256
        try:
            os.stat(final_leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                _rename_leaf_no_replace(
                    parent_fd,
                    stage_leaf,
                    final_leaf,
                    backend=_atomic_publish_backend(),
                )
                published = True
            except FileExistsError:
                pass
        _verify_retained_controller(
            retained, artifact_sha256, deadline=deadline,
        )
        controller = ControllerIdentity(
            repository_id=snapshot.repo.repository_id,
            controller_id=_LOCAL_CONTROLLER_ID,
            release_oid=release_oid,
            artifact_sha256=artifact_sha256,
        )
        if expected is not None and controller != expected:
            raise LocalGoValidationError(
                "the retained controller identity differs from the stored contract"
            )
        return retained, controller
    except LocalGoValidationError:
        raise
    except Exception as exc:
        raise LocalGoValidationError(
            "the retained controller export failed"
        ) from exc
    finally:
        if stage_fd >= 0:
            if not published and stage_identity is not None:
                try:
                    os.stat(stage_leaf, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    try:
                        _discard_controller_stage(
                            parent_fd,
                            stage_leaf,
                            stage_fd,
                            stage_identity,
                            deadline=deadline,
                        )
                    except Exception:
                        pass
            os.close(stage_fd)
        os.close(parent_fd)


def capture_local_execution_inputs(
    *,
    snapshot: SourceSnapshot,
    controller_python: Path,
    manifest: Mapping[str, Any],
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
    _controller_checkout: Path | None = None,
) -> LocalExecutionInputs:
    """Retain exact approval-time controller and local check identities."""

    if not isinstance(snapshot, SourceSnapshot):
        raise LocalGoValidationError("snapshot must be a SourceSnapshot")
    absolute_deadline = _bounded_local_runtime_capture_deadline(deadline)
    check_config = _local_check_config_from_manifest(manifest)
    checkout = (
        Path(__file__).resolve().parent.parent
        if _controller_checkout is None
        else Path(_controller_checkout)
    )
    controller_source, controller = _retain_local_controller(
        snapshot=snapshot,
        controller_checkout=checkout,
        expected=None,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    launcher = Path(
        os.path.abspath(os.path.expanduser(os.fspath(controller_python)))
    )
    check_plan = derive_local_check_plan(
        snapshot=snapshot,
        controller_python=launcher,
        config=check_config,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    return LocalExecutionInputs(
        controller_source=controller_source,
        controller=controller,
        check_plan=check_plan,
    )


@dataclass(frozen=True)
class LocalExecutionRuntime:
    """Exact host runtimes and private roots for one approved local plan."""

    candidate_runtime: BestplanHostRuntime
    check_runtime: CheckHostRuntime
    check_plan: LocalCheckPlan
    integration_root: Path
    checks_root: Path
    operation_timeout_seconds: float

    def __post_init__(self) -> None:
        from agent.bestplan_checks import CheckHostRuntime
        from tools.delegate_tool import BestplanHostRuntime

        integration_root = Path(self.integration_root)
        checks_root = Path(self.checks_root)
        timeout = self.operation_timeout_seconds
        if (
            not isinstance(self.candidate_runtime, BestplanHostRuntime)
            or not isinstance(self.check_runtime, CheckHostRuntime)
            or not isinstance(self.check_plan, LocalCheckPlan)
            or not integration_root.is_absolute()
            or not checks_root.is_absolute()
        ):
            raise LocalGoValidationError("the local execution runtime is invalid")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 86_400.0
        ):
            raise LocalGoValidationError(
                "the local execution operation timeout is invalid"
            )
        if (
            self.candidate_runtime.controller != self.check_runtime.controller
            or self.candidate_runtime.controller_source
            != self.check_runtime.controller_source
            or self.check_runtime.controller_python_launcher
            != self.candidate_runtime.controller_python
            or self.check_runtime.pytest_module_path
            != self.check_plan.pytest_module_path
            or self.check_runtime.runtime_read_paths
            != self.check_plan.runtime_read_paths
        ):
            raise LocalGoValidationError(
                "the local execution runtime identities differ"
            )
        object.__setattr__(self, "integration_root", integration_root)
        object.__setattr__(self, "checks_root", checks_root)
        object.__setattr__(self, "operation_timeout_seconds", float(timeout))


def _validated_local_execution_contract(
    *,
    snapshot: SourceSnapshot,
    manifest: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], ControllerIdentity, str]:
    if not isinstance(snapshot, SourceSnapshot):
        raise LocalGoValidationError("snapshot must be a SourceSnapshot")
    approved = validate_local_go_contract(contract)
    approval_digest = local_go_approval_digest(manifest, approved)
    if _approved_local_check_config(
        approved,
    ) != _local_check_config_from_manifest(manifest):
        raise LocalGoValidationError(
            "the stored local checks differ from manifest acceptance"
        )
    if (
        not snapshot.head_symbolic
        or snapshot.head_ref != LOCAL_MAIN_REF.encode("ascii")
    ):
        raise LocalGoValidationError(
            "local go requires a source attached to refs/heads/main"
        )
    try:
        repository = _repository_to_dict(
            EnrolledRepository.from_repo_identity(snapshot.repo)
        )
        source = {
            "base_oid": snapshot.head_oid,
            "tree_oid": snapshot.tree_oid,
            "local_ref": LOCAL_MAIN_REF,
            "snapshot_digest": source_snapshot_digest(snapshot),
            "source_digest": snapshot.fingerprint,
            "protected_digest": snapshot.protected_manifest.digest,
        }
        controller = _controller_from_dict(
            approved["controller"], "local-go contract.controller"
        )
    except ContractValidationError as exc:
        raise _as_local_error(exc) from exc
    if approved["repository"] != repository:
        raise LocalGoValidationError(
            "the stored local repository identity differs from the source"
        )
    if approved["source"] != source:
        raise LocalGoValidationError(
            "the stored local source identity differs from the source"
        )
    if controller.repository_id != snapshot.repo.repository_id:
        raise LocalGoValidationError(
            "the stored local controller crosses repository identity"
        )
    return approved, controller, approval_digest


def _assert_local_check_plan_matches_contract(
    check_plan: LocalCheckPlan,
    approved: Mapping[str, Any],
) -> None:
    if not isinstance(check_plan, LocalCheckPlan):
        raise LocalGoValidationError("the local check plan is invalid")
    commands = [_command_to_dict(command) for command in check_plan.commands]
    if commands != approved["commands"]:
        raise LocalGoValidationError(
            "the rederived local check commands differ from the stored contract"
        )
    if check_plan.check_runtime_digest != approved["check_runtime_digest"]:
        raise LocalGoValidationError(
            "the rederived local check runtime differs from the stored contract"
        )


def _approved_local_check_config(
    approved: Mapping[str, Any],
) -> dict[str, list[str]]:
    commands = approved.get("commands")
    if not isinstance(commands, list) or len(commands) != 1:
        raise LocalGoValidationError(
            "the stored local contract has no supported exact check"
        )
    command = commands[0]
    if not isinstance(command, Mapping):
        raise LocalGoValidationError(
            "the stored local contract has no supported exact check"
        )
    argv = command.get("argv")
    if (
        command.get("identifier") != "pytest"
        or not isinstance(argv, list)
        or tuple(argv[:len(_LOCAL_PYTEST_ARGV_PREFIX)])
        != _LOCAL_PYTEST_ARGV_PREFIX
    ):
        raise LocalGoValidationError(
            "the stored local contract has no supported exact check"
        )
    config = {"pytest_nodes": list(argv[len(_LOCAL_PYTEST_ARGV_PREFIX):])}
    _local_pytest_nodes(config)
    return config


def _local_plan_id_bytes(plan_id: str) -> bytes:
    if not isinstance(plan_id, str) or not plan_id or "\x00" in plan_id:
        raise LocalGoValidationError("the local plan identifier is invalid")
    try:
        encoded = plan_id.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise LocalGoValidationError(
            "the local plan identifier is invalid"
        ) from exc
    if len(encoded) > 1024:
        raise LocalGoValidationError("the local plan identifier is oversized")
    return encoded


_LOCAL_PLAN_CHILDREN = (b"candidates", b"integration", b"checks", b"cache")


def _local_plan_identity_body(
    *,
    leaf: str,
    approval_digest: str,
    identities: Mapping[bytes, tuple[int, int]],
) -> dict[str, Any]:
    return {
        "schema": _LOCAL_PLAN_ROOT_SCHEMA,
        "plan_leaf": leaf,
        "approval_digest": approval_digest,
        "children": [
            {
                "name": os.fsdecode(name),
                "device": identities[name][0],
                "inode": identities[name][1],
            }
            for name in sorted(_LOCAL_PLAN_CHILDREN)
        ],
    }


def _write_local_plan_identity(
    plan_fd: int,
    body: Mapping[str, Any],
) -> None:
    raw = canonical_json(body).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    file_fd = os.open(
        _LOCAL_PLAN_IDENTITY_LEAF, flags, 0o400, dir_fd=plan_fd,
    )
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(file_fd, raw[offset:])
            if written <= 0:
                raise OSError("short local plan identity write")
            offset += written
        os.fchmod(file_fd, 0o400)
        os.fsync(file_fd)
    finally:
        os.close(file_fd)


def _read_local_plan_identity(
    plan_fd: int,
    *,
    leaf: str,
    approval_digest: str,
) -> dict[bytes, tuple[int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        entry_info = os.stat(
            _LOCAL_PLAN_IDENTITY_LEAF,
            dir_fd=plan_fd,
            follow_symlinks=False,
        )
        file_fd = os.open(_LOCAL_PLAN_IDENTITY_LEAF, flags, dir_fd=plan_fd)
    except OSError as exc:
        raise LocalGoValidationError(
            "the private local plan runtime identity is unsafe"
        ) from exc
    try:
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(entry_info.st_mode)
            or (entry_info.st_dev, entry_info.st_ino)
            != (before.st_dev, before.st_ino)
            or stat.S_IMODE(before.st_mode) != 0o400
            or before.st_uid != os.geteuid()
            or not 0 < before.st_size <= 4096
        ):
            raise LocalGoValidationError(
                "the private local plan runtime identity is unsafe"
            )
        chunks: list[bytes] = []
        remaining = 4097
        while remaining:
            chunk = os.read(file_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(file_fd)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if not stable or len(raw) != before.st_size:
            raise LocalGoValidationError(
                "the private local plan runtime identity changed"
            )
    finally:
        os.close(file_fd)
    try:
        value = json.loads(raw.decode("utf-8"))
        value = _exact_mapping(
            value,
            {"schema", "plan_leaf", "approval_digest", "children"},
            "local plan runtime identity",
        )
        if (
            value["schema"] != _LOCAL_PLAN_ROOT_SCHEMA
            or value["plan_leaf"] != leaf
            or value["approval_digest"] != approval_digest
            or canonical_json(value).encode("utf-8") != raw
        ):
            raise ContractValidationError(
                "local plan runtime identity differs"
            )
        _sha256(value["approval_digest"], "local plan approval digest")
        if not isinstance(value["children"], list) or len(
            value["children"]
        ) != len(_LOCAL_PLAN_CHILDREN):
            raise ContractValidationError(
                "local plan runtime children are invalid"
            )
        identities: dict[bytes, tuple[int, int]] = {}
        for index, item in enumerate(value["children"]):
            item = _exact_mapping(
                item,
                {"name", "device", "inode"},
                f"local plan runtime children[{index}]",
            )
            name = item["name"]
            device = item["device"]
            inode = item["inode"]
            if (
                not isinstance(name, str)
                or isinstance(device, bool)
                or not isinstance(device, int)
                or device < 1
                or isinstance(inode, bool)
                or not isinstance(inode, int)
                or inode < 1
            ):
                raise ContractValidationError(
                    "local plan runtime child identity is invalid"
                )
            identities[os.fsencode(name)] = (device, inode)
        if tuple(sorted(identities)) != tuple(sorted(_LOCAL_PLAN_CHILDREN)):
            raise ContractValidationError(
                "local plan runtime children differ"
            )
        return identities
    except (
        ContractValidationError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
    ) as exc:
        raise LocalGoValidationError(
            "the private local plan runtime identity is unsafe"
        ) from exc


def _inspect_empty_local_plan_children(
    plan_fd: int,
    *,
    expected_identities: Mapping[bytes, tuple[int, int]] | None,
    deadline: float,
    cancel_event: threading.Event | None,
) -> dict[bytes, tuple[int, int]]:
    expected_names = set(_LOCAL_PLAN_CHILDREN)
    if expected_identities is not None:
        expected_names.add(_LOCAL_PLAN_IDENTITY_LEAF)
    with os.scandir(plan_fd) as iterator:
        observed_names = {os.fsencode(item.name) for item in iterator}
    if observed_names != expected_names:
        raise LocalGoValidationError(
            "the private local plan runtime layout is unsafe"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    identities: dict[bytes, tuple[int, int]] = {}
    for name in _LOCAL_PLAN_CHILDREN:
        _local_runtime_checkpoint(deadline, cancel_event)
        try:
            before = os.stat(name, dir_fd=plan_fd, follow_symlinks=False)
            child_fd = os.open(name, flags, dir_fd=plan_fd)
        except OSError as exc:
            raise LocalGoValidationError(
                "the private local plan child is unsafe"
            ) from exc
        try:
            opened = os.fstat(child_fd)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISDIR(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or (before.st_dev, before.st_ino) != identity
                or stat.S_IMODE(opened.st_mode) != 0o700
                or opened.st_uid != os.geteuid()
                or (
                    expected_identities is not None
                    and expected_identities.get(name) != identity
                )
            ):
                raise LocalGoValidationError(
                    "the private local plan child is unsafe"
                )
            with os.scandir(child_fd) as iterator:
                if next(iterator, None) is not None:
                    raise LocalGoValidationError(
                        "the private local plan child is not empty"
                    )
            identities[name] = identity
        finally:
            os.close(child_fd)
    return identities


def _create_local_plan_roots(
    *,
    runtime_root: Path,
    plan_id: str,
    approval_digest: str,
    snapshot: SourceSnapshot,
    controller_source: Path,
    deadline: float,
    cancel_event: threading.Event | None,
) -> tuple[Path, Path, Path, Path]:
    _local_runtime_checkpoint(deadline, cancel_event)
    leaf = hashlib.sha256(
        _LOCAL_PLAN_ROOT_DOMAIN + _local_plan_id_bytes(plan_id)
    ).hexdigest()
    plans_root = _private_local_directory(runtime_root / "plans")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(plans_root, flags)
    plan_fd = -1
    identities: dict[bytes, tuple[int, int]]
    try:
        _local_runtime_checkpoint(deadline, cancel_event)
        created = False
        try:
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        try:
            before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            plan_fd = os.open(leaf, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise LocalGoValidationError(
                "the private local plan runtime is unsafe"
            ) from exc
        plan_info = os.fstat(plan_fd)
        if (
            not stat.S_ISDIR(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or (before.st_dev, before.st_ino)
            != (plan_info.st_dev, plan_info.st_ino)
            or stat.S_IMODE(plan_info.st_mode) != 0o700
            or plan_info.st_uid != os.geteuid()
        ):
            raise LocalGoValidationError(
                "the private local plan runtime is unsafe"
            )
        if created:
            for name in _LOCAL_PLAN_CHILDREN:
                _local_runtime_checkpoint(deadline, cancel_event)
                os.mkdir(name, 0o700, dir_fd=plan_fd)
            identities = _inspect_empty_local_plan_children(
                plan_fd,
                expected_identities=None,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            _write_local_plan_identity(
                plan_fd,
                _local_plan_identity_body(
                    leaf=leaf,
                    approval_digest=approval_digest,
                    identities=identities,
                ),
            )
            os.fsync(plan_fd)
        else:
            expected_identities = _read_local_plan_identity(
                plan_fd, leaf=leaf, approval_digest=approval_digest,
            )
            identities = _inspect_empty_local_plan_children(
                plan_fd,
                expected_identities=expected_identities,
                deadline=deadline,
                cancel_event=cancel_event,
            )
    except LocalGoValidationError:
        raise
    except OSError as exc:
        raise LocalGoValidationError(
            "the private local plan runtime could not be created"
        ) from exc
    finally:
        if plan_fd >= 0:
            os.close(plan_fd)
        os.close(parent_fd)

    plan_root = plans_root / leaf
    roots = tuple(
        plan_root / os.fsdecode(name) for name in _LOCAL_PLAN_CHILDREN
    )
    try:
        target_roots = tuple(
            Path(os.fsdecode(raw)).resolve(strict=True)
            for raw in (
                snapshot.repo.worktree_raw,
                snapshot.repo.git_dir_raw,
                snapshot.repo.common_dir_raw,
            )
        )
        retained = Path(controller_source).resolve(strict=True)
        for name, root in zip(_LOCAL_PLAN_CHILDREN, roots, strict=True):
            info = root.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.geteuid()
                or (info.st_dev, info.st_ino) != identities[name]
                or any(_local_paths_overlap(root, item) for item in target_roots)
                or _local_paths_overlap(root, retained)
            ):
                raise LocalGoValidationError(
                    "the private local plan runtime is unsafe"
                )
    except LocalGoValidationError:
        raise
    except OSError as exc:
        raise LocalGoValidationError(
            "the private local plan runtime is unavailable"
        ) from exc
    return roots


def build_local_execution_runtime(
    *,
    plan_id: str,
    snapshot: SourceSnapshot,
    manifest: Mapping[str, Any],
    contract: Mapping[str, Any],
    controller_python: Path,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
    _controller_checkout: Path | None = None,
) -> LocalExecutionRuntime:
    """Rebuild and exact-match one approved local execution runtime."""

    from agent.bestplan_checks import CheckHostRuntime
    from agent.bestplan_sandbox import pinned_candidate_runtime_paths
    from tools.delegate_tool import BestplanHostRuntime

    absolute_deadline = _bounded_local_runtime_capture_deadline(deadline)
    _local_runtime_checkpoint(absolute_deadline, cancel_event)
    _local_plan_id_bytes(plan_id)
    approved, expected_controller, approval_digest = (
        _validated_local_execution_contract(
            snapshot=snapshot,
            manifest=manifest,
            contract=contract,
        )
    )
    check_config = _approved_local_check_config(approved)
    launcher = Path(
        os.path.abspath(os.path.expanduser(os.fspath(controller_python)))
    )
    checkout = (
        Path(__file__).resolve().parent.parent
        if _controller_checkout is None
        else Path(_controller_checkout)
    )
    runtime_root = _local_runtime_root(snapshot)
    controller_source, controller = _retain_local_controller(
        snapshot=snapshot,
        controller_checkout=checkout,
        expected=expected_controller,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
        runtime_root=runtime_root,
    )
    _local_runtime_checkpoint(absolute_deadline, cancel_event)
    check_plan = derive_local_check_plan(
        snapshot=snapshot,
        controller_python=launcher,
        config=check_config,
        deadline=absolute_deadline,
        cancel_event=cancel_event,
    )
    _assert_local_check_plan_matches_contract(check_plan, approved)
    try:
        candidate_runtime_paths = pinned_candidate_runtime_paths(launcher)
    except Exception as exc:
        raise LocalGoValidationError(
            "the candidate Python runtime is unavailable"
        ) from exc
    attempts_root, integration_root, checks_root, cache_root = (
        _create_local_plan_roots(
            runtime_root=runtime_root,
            plan_id=plan_id,
            approval_digest=approval_digest,
            snapshot=snapshot,
            controller_source=controller_source,
            deadline=absolute_deadline,
            cancel_event=cancel_event,
        )
    )
    _local_runtime_checkpoint(absolute_deadline, cancel_event)
    try:
        candidate_runtime = BestplanHostRuntime(
            controller=controller,
            controller_source=controller_source,
            controller_python=launcher,
            runtime_read_paths=candidate_runtime_paths,
            attempts_root=attempts_root,
            policy_version=_LOCAL_CANDIDATE_POLICY_VERSION,
            request_budget=_LOCAL_CANDIDATE_REQUEST_BUDGET,
            token_budget=_LOCAL_CANDIDATE_TOKEN_BUDGET,
            max_iterations=_LOCAL_CANDIDATE_MAX_ITERATIONS,
            max_output_tokens=_LOCAL_CANDIDATE_MAX_OUTPUT_TOKENS,
            timeout_seconds=_LOCAL_CANDIDATE_TIMEOUT_SECONDS,
            capability_ttl_seconds=_LOCAL_CANDIDATE_CAPABILITY_TTL_SECONDS,
        )
        check_runtime = CheckHostRuntime(
            controller_source=controller_source,
            controller=controller,
            sandbox_executable=check_plan.sandbox_executable,
            sandbox_executable_sha256=(
                check_plan.sandbox_executable_sha256
            ),
            runtime_read_paths=check_plan.runtime_read_paths,
            cache_seed_root=cache_root,
            policy_version=check_plan.policy_version,
            controller_python_launcher=launcher,
            pytest_module_path=check_plan.pytest_module_path,
        )
        runtime = LocalExecutionRuntime(
            candidate_runtime=candidate_runtime,
            check_runtime=check_runtime,
            check_plan=check_plan,
            integration_root=integration_root,
            checks_root=checks_root,
            operation_timeout_seconds=_LOCAL_OPERATION_TIMEOUT_SECONDS,
        )
        _local_runtime_checkpoint(absolute_deadline, cancel_event)
        return runtime
    except LocalGoValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise LocalGoValidationError(
            "the approved local host runtime is invalid"
        ) from exc


@dataclass
class _AttemptAuthority:
    capability: BrokerCapability
    request_budget: int
    token_budget: int
    expires_at: int
    requests: int = 0
    tokens: int = 0
    reserved_tokens: int = 0
    revoked: bool = False
    in_flight: bool = False


class LocalBestplanAuthority:
    """Foreground, credential-holding model relay for one exact runtime."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_mode: str,
        api_key: str,
        no_auth: bool,
        request_overrides: Mapping[str, Any],
    ) -> None:
        self._provider = provider
        self._model = model
        self._base_url = base_url
        self._api_mode = api_mode
        self._api_key = api_key
        self._no_auth = no_auth
        self._request_overrides = dict(request_overrides)
        self._attempts: dict[str, _AttemptAuthority] = {}
        self._attempt_ids: set[str] = set()
        self._client: Any | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_runtime(cls, runtime: Mapping[str, Any]) -> "LocalBestplanAuthority":
        if not isinstance(runtime, Mapping):
            raise LocalGoValidationError("candidate runtime must be an object")

        def text(name: str, *, required: bool = False) -> str:
            value = runtime.get(name, "")
            if not isinstance(value, str) or "\x00" in value or (required and not value):
                requirement = "nonempty " if required else ""
                raise LocalGoValidationError(
                    f"candidate runtime {name} must be a {requirement}string"
                )
            return value

        provider = text("provider", required=True)
        model = text("model", required=True)
        base_url = text("base_url")
        api_key = text("api_key")
        no_auth = runtime.get("no_auth", False)
        if type(no_auth) is not bool:
            raise LocalGoValidationError(
                "candidate runtime no_auth must be true or false"
            )
        if base_url:
            if not api_key and not no_auth:
                raise LocalGoValidationError(
                    "candidate direct endpoint requires api_key or explicit no_auth"
                )
            if api_key and no_auth:
                raise LocalGoValidationError(
                    "candidate direct endpoint cannot combine api_key and no_auth"
                )
            if no_auth and provider != "custom":
                raise LocalGoValidationError(
                    "candidate runtime no_auth supports custom endpoints only"
                )
        elif no_auth:
            raise LocalGoValidationError(
                "candidate runtime no_auth requires a direct endpoint"
            )
        overrides = runtime.get("request_overrides") or {}
        if not isinstance(overrides, Mapping) or any(
            not isinstance(key, str) for key in overrides
        ):
            raise LocalGoValidationError(
                "candidate runtime request_overrides must be an object"
            )
        if set(overrides) - _ALLOWED_REQUEST_OVERRIDE_FIELDS:
            raise LocalGoValidationError(
                "candidate runtime request override is unsupported"
            )
        try:
            overrides_json = json.dumps(
                overrides,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            copied_overrides = json.loads(overrides_json)
        except (TypeError, ValueError, RecursionError) as exc:
            raise LocalGoValidationError(
                "candidate runtime request_overrides are not bounded JSON"
            ) from exc
        if len(overrides_json.encode("utf-8")) > 32 * 1024:
            raise LocalGoValidationError(
                "candidate runtime request_overrides are oversized"
            )
        return cls(
            provider=provider,
            model=model,
            base_url=base_url,
            api_mode=text("api_mode"),
            api_key=api_key,
            no_auth=no_auth,
            request_overrides=copied_overrides,
        )

    def lookup_enrollment(self, repo_identity: Any) -> None:
        del repo_identity
        return None

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise AuthorityProtocolError(f"{name} must be a positive integer")
        return value

    def register_model_attempt(
        self,
        attempt_id: str,
        worker_identity: WorkerIdentity,
        model: str,
        request_budget: int,
        token_budget: int,
        expires_at: int,
    ) -> BrokerCapability:
        if not isinstance(attempt_id, str) or not attempt_id or "\x00" in attempt_id:
            raise AuthorityProtocolError("attempt identity is invalid")
        if not isinstance(worker_identity, WorkerIdentity):
            raise AuthorityProtocolError("worker identity is invalid")
        if model != self._model:
            raise AuthorityProtocolError("candidate model differs from the bound model")
        request_limit = self._positive_int(request_budget, "request budget")
        token_limit = self._positive_int(token_budget, "token budget")
        expiry = self._positive_int(expires_at, "capability expiry")
        with self._lock:
            if attempt_id in self._attempt_ids:
                raise AuthorityProtocolError("candidate attempt is already registered")
            capability = BrokerCapability(
                attempt_id=attempt_id,
                worker_identity=worker_identity,
                opaque_handle=secrets.token_urlsafe(32),
            )
            self._attempt_ids.add(attempt_id)
            self._attempts[capability.opaque_handle] = _AttemptAuthority(
                capability=capability,
                request_budget=request_limit,
                token_budget=token_limit,
                expires_at=expiry,
            )
            return capability

    def _state_for(
        self,
        capability: BrokerCapability,
        *,
        start_request: bool = False,
        token_reservation: int = 0,
    ) -> _AttemptAuthority:
        if not isinstance(capability, BrokerCapability):
            raise AuthorityProtocolError("broker capability is invalid")
        state = self._attempts.get(capability.opaque_handle)
        if state is None or state.capability != capability:
            raise AuthorityProtocolError("broker capability is unknown")
        if state.revoked:
            raise AuthorityProtocolError("broker capability is revoked")
        if int(time.time()) >= state.expires_at:
            raise AuthorityProtocolError("broker capability is expired")
        if start_request:
            if state.in_flight:
                raise AuthorityProtocolError("broker capability already has a request")
            if state.requests >= state.request_budget:
                raise AuthorityProtocolError("broker request budget is exhausted")
            if (
                isinstance(token_reservation, bool)
                or not isinstance(token_reservation, int)
                or token_reservation < 1
            ):
                raise AuthorityProtocolError("broker token reservation is invalid")
            if state.tokens + token_reservation > state.token_budget:
                raise AuthorityProtocolError("broker token budget is exhausted")
            state.requests += 1
            state.reserved_tokens = token_reservation
            state.in_flight = True
        return state

    def _resolve_client(self) -> Any:
        with self._lock:
            if self._client is not None:
                return self._client
        try:
            client, resolved_model = resolve_provider_client(
                self._provider,
                self._model,
                explicit_base_url=self._base_url or None,
                explicit_api_key=(
                    _LOCAL_NO_AUTH_API_KEY if self._no_auth else self._api_key or None
                ),
                api_mode=self._api_mode or None,
            )
        except BaseException as exc:
            raise AuthorityUnavailable("the bound candidate model route is unavailable") from exc
        if client is None or resolved_model != self._model:
            raise AuthorityUnavailable("the bound candidate model route is unavailable")
        with self._lock:
            if self._client is None:
                self._client = client
            return self._client

    @staticmethod
    def _response_mapping(response: Any) -> Mapping[str, Any]:
        if isinstance(response, Mapping):
            value = response
        else:
            dump = getattr(response, "model_dump", None)
            if callable(dump):
                value = dump(mode="json")
            else:
                dump = getattr(response, "dict", None)
                if not callable(dump):
                    raise AuthorityProtocolError(
                        "candidate model response is not serializable"
                    )
                value = dump()
        if not isinstance(value, Mapping):
            raise AuthorityProtocolError("candidate model response is invalid")
        return value

    def _broker_response(
        self, request: BrokerTurnRequest, response: Any
    ) -> BrokerTurnResponse:
        raw = self._response_mapping(response)
        missing = _RESPONSE_FIELDS - set(raw)
        if missing:
            raise AuthorityProtocolError("candidate model response is incomplete")
        if raw.get("model") != self._model:
            raise AuthorityProtocolError("candidate model response model differs")
        usage = raw.get("usage")
        if not isinstance(usage, Mapping):
            raise AuthorityProtocolError("candidate model response usage is invalid")
        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion_tokens = usage.get(
            "completion_tokens", usage.get("output_tokens")
        )
        total_tokens = usage.get("total_tokens")
        for value in (prompt_tokens, completion_tokens, total_tokens):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AuthorityProtocolError("candidate model response usage is invalid")
        if total_tokens != prompt_tokens + completion_tokens:
            raise AuthorityProtocolError("candidate model response usage differs")
        if completion_tokens > request.max_output_tokens:
            raise AuthorityProtocolError("candidate model response exceeds its output budget")
        projected_choices = self._project_choices(raw.get("choices"))
        response_id = self._bounded_response_text(
            raw.get("id"), "id", maximum=256,
        )
        response_object = self._bounded_response_text(
            raw.get("object"), "object", maximum=128,
        )
        created = raw.get("created")
        if isinstance(created, bool) or not isinstance(created, int) or created < 0:
            raise AuthorityProtocolError(
                "candidate model response created value is invalid"
            )
        body = {
            "id": response_id,
            "object": response_object,
            "created": created,
            "model": raw["model"],
            "choices": projected_choices,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
        }
        try:
            response_json = json.dumps(
                body,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise AuthorityProtocolError(
                "candidate model response is not canonical JSON"
            ) from exc
        if len(response_json.encode("utf-8")) > _MAX_BROKER_RESPONSE_BYTES:
            raise AuthorityProtocolError(
                "candidate model response exceeds the broker frame bound"
            )
        return BrokerTurnResponse(
            request_id=request.request_id,
            response_json=response_json,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        )

    @staticmethod
    def _bounded_response_text(
        value: Any,
        label: str,
        *,
        maximum: int,
        allow_none: bool = False,
    ) -> str | None:
        if value is None and allow_none:
            return None
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value.encode("utf-8")) > maximum
        ):
            raise AuthorityProtocolError(
                f"candidate model response {label} is invalid"
            )
        return value

    @classmethod
    def _project_tool_calls(cls, value: Any) -> list[dict[str, Any]] | None:
        if value is None:
            return None
        if not isinstance(value, list) or len(value) > 64:
            raise AuthorityProtocolError(
                "candidate model response tool calls are invalid"
            )
        projected: list[dict[str, Any]] = []
        for raw_call in value:
            if not isinstance(raw_call, Mapping) or raw_call.get("type") != "function":
                raise AuthorityProtocolError(
                    "candidate model response tool call is invalid"
                )
            function = raw_call.get("function")
            if not isinstance(function, Mapping):
                raise AuthorityProtocolError(
                    "candidate model response tool call is invalid"
                )
            arguments = function.get("arguments")
            if (
                not isinstance(arguments, str)
                or len(arguments.encode("utf-8")) > 256 * 1024
            ):
                raise AuthorityProtocolError(
                    "candidate model response tool arguments are invalid"
                )
            projected.append({
                "id": cls._bounded_response_text(
                    raw_call.get("id"), "tool call id", maximum=128,
                ),
                "type": "function",
                "function": {
                    "name": cls._bounded_response_text(
                        function.get("name"), "tool name", maximum=128,
                    ),
                    "arguments": arguments,
                },
            })
        return projected

    @classmethod
    def _project_choices(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not 1 <= len(value) <= 16:
            raise AuthorityProtocolError(
                "candidate model response choices are invalid"
            )
        projected: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw_choice in value:
            if not isinstance(raw_choice, Mapping):
                raise AuthorityProtocolError(
                    "candidate model response choice is invalid"
                )
            index = raw_choice.get("index")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < 16
                or index in seen
            ):
                raise AuthorityProtocolError(
                    "candidate model response choice index is invalid"
                )
            seen.add(index)
            finish_reason = raw_choice.get("finish_reason")
            if finish_reason is not None:
                finish_reason = cls._bounded_response_text(
                    finish_reason, "finish reason", maximum=128,
                )
            message = raw_choice.get("message")
            if not isinstance(message, Mapping) or message.get("role") != "assistant":
                raise AuthorityProtocolError(
                    "candidate model response message is invalid"
                )
            content = message.get("content")
            if content is not None and (
                not isinstance(content, str)
                or len(content.encode("utf-8")) > _MAX_BROKER_RESPONSE_BYTES
            ):
                raise AuthorityProtocolError(
                    "candidate model response content is invalid"
                )
            projected.append({
                "index": index,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": cls._project_tool_calls(
                        message.get("tool_calls")
                    ),
                },
            })
        return projected

    def model_request(
        self,
        capability: BrokerCapability,
        request: ModelRequest | BrokerTurnRequest,
    ) -> ModelResponse | BrokerTurnResponse:
        if not isinstance(request, BrokerTurnRequest):
            raise AuthorityProtocolError("local candidate relay requires a broker turn")
        try:
            body = json.loads(request.request_json)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise AuthorityProtocolError("candidate model request is invalid") from exc
        if body.get("model") != self._model:
            raise AuthorityProtocolError("candidate model request model differs")
        requested_tokens = body.get(
            "max_completion_tokens", body.get("max_tokens")
        )
        if (
            isinstance(requested_tokens, bool)
            or not isinstance(requested_tokens, int)
            or not 1 <= requested_tokens <= request.max_output_tokens
        ):
            raise AuthorityProtocolError("candidate model request output budget differs")

        token_reservation = (
            len(request.request_json.encode("utf-8"))
            + _BROKER_INPUT_TOKEN_OVERHEAD
            + requested_tokens
        )

        with self._lock:
            self._state_for(
                capability,
                start_request=True,
                token_reservation=token_reservation,
            )
        provider_started = False
        try:
            kwargs = dict(body)
            kwargs.update(self._request_overrides)
            client = self._resolve_client()
            provider_started = True
            response = client.chat.completions.create(**kwargs)
            broker_response = self._broker_response(request, response)
            with self._lock:
                state = self._state_for(capability)
                actual = broker_response.input_tokens + broker_response.output_tokens
                reserved = state.reserved_tokens
                state.reserved_tokens = 0
                state.in_flight = False
                total = state.tokens + actual
                if actual > reserved or total > state.token_budget:
                    state.tokens += max(actual, reserved)
                    state.revoked = True
                    raise AuthorityProtocolError("broker token budget is exhausted")
                state.tokens = total
            return broker_response
        except BaseException:
            with self._lock:
                current = self._attempts.get(capability.opaque_handle)
                if current is not None and current.in_flight:
                    if provider_started:
                        current.tokens += current.reserved_tokens
                    current.reserved_tokens = 0
                    current.in_flight = False
            raise

    def revoke_model_attempt(self, capability: BrokerCapability) -> None:
        with self._lock:
            if not isinstance(capability, BrokerCapability):
                raise AuthorityProtocolError("broker capability is invalid")
            state = self._attempts.get(capability.opaque_handle)
            if state is None or state.capability != capability:
                raise AuthorityProtocolError("broker capability is unknown")
            state.revoked = True

    def read_authoritative_status(self, plan_id: str) -> AuthorityStatus:
        del plan_id
        raise AuthorityUnavailable("local BestPlan has no publication authority status")


@dataclass(frozen=True)
class LocalAuthorityBinding:
    """One manifest-position authority bound to one runtime fingerprint."""

    position: int
    runtime_fingerprint: str
    authority: LocalBestplanAuthority


def build_local_authority_bindings(
    resolved_runtimes: Sequence[Mapping[str, Any]],
) -> tuple[LocalAuthorityBinding, ...]:
    """Create a distinct credential holder for every ordered runtime."""

    if not isinstance(resolved_runtimes, (list, tuple)) or not resolved_runtimes:
        raise LocalGoValidationError(
            "candidate runtimes must be a nonempty ordered sequence"
        )
    bindings: list[LocalAuthorityBinding] = []
    for position, runtime in enumerate(resolved_runtimes):
        if not isinstance(runtime, Mapping):
            raise LocalGoValidationError("candidate runtime must be an object")
        fingerprint = runtime.get("runtime_fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", fingerprint,
        ):
            raise LocalGoValidationError(
                "candidate runtime fingerprint is invalid"
            )
        bindings.append(LocalAuthorityBinding(
            position=position,
            runtime_fingerprint=fingerprint,
            authority=LocalBestplanAuthority.from_runtime(runtime),
        ))
    return tuple(bindings)
