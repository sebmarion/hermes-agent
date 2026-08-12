"""Safe local-main landing for one checked BestPlan integration commit."""

from __future__ import annotations

import hashlib
import math
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit, urlunsplit

from agent import bestplan_source as source_boundary
from agent.bestplan_checks import (
    CheckReceipt,
    CheckSetReceipt,
    _command_digest,
    _domain_digest,
)
from agent.bestplan_contract import BoundCommand
from agent.bestplan_promotion import (
    MAX_PROMOTION_GIT_OUTPUT_BYTES,
    FrozenIntegration,
    IntegrationValidationError,
    PromotionError,
    _changed_paths,
    _commit_proof,
    _git_environment,
    _path_alias,
    _path_related,
    _read_ref,
    _run_git,
    _tree_map,
)
from agent.bestplan_source import (
    ProtectedManifest,
    SourceBoundaryError,
    SourceSnapshot,
    capture_protected_manifest,
    resolve_repo_identity,
)


LOCAL_MAIN_REF = "refs/heads/main"
MAX_LOCAL_MAIN_DEADLINE_SECONDS = 86_400.0
LOCAL_MAIN_POSTFLIGHT_SECONDS = 10.0
LOCAL_PUSH_FINISH_RESERVE_SECONDS = 0.5
LOCAL_PUSH_TERM_GRACE_SECONDS = 0.1


class LocalMainError(RuntimeError):
    """Base error for a local-main landing failure."""


class LocalMainProofStale(LocalMainError):
    """The checked integration or admitted local target has changed."""


class LocalMainConflict(LocalMainError):
    """Incoming paths conflict with protected local work."""


class LocalMainEffectUnknown(LocalMainError):
    """The local-main effect started but its exact outcome needs reconciliation."""


class LocalPushStale(LocalMainError):
    """The approved local or remote push target changed."""


class LocalPushConflict(LocalMainError):
    """The exact normal push was rejected without changing the remote."""


class LocalPushEffectUnknown(LocalMainError):
    """The push started but remote read-back could not prove its outcome."""


class _PushProcessNotExtinct(LocalPushEffectUnknown):
    """The push process group might still be active; retain its control repo."""


@dataclass(frozen=True)
class LocalMainLandingReceipt:
    """Proof that local ``main`` now names the exact checked commit."""

    target_ref: str
    old_oid: str
    new_oid: str
    check_receipt_digest: str


@dataclass(frozen=True)
class LocalMainPushTarget:
    remote_name: str
    remote_ref: str
    display_url: str
    remote_identity_sha256: str
    observed_remote_oid: str
    integration_oid: str


@dataclass(frozen=True)
class LocalMainPushReceipt:
    remote_name: str
    remote_ref: str
    integration_oid: str
    remote_oid: str


@dataclass(frozen=True)
class _PushControlRepo:
    path: str
    root_fd: int
    parent_fd: int
    leaf: bytes
    identity: tuple[int, int]
    source_objects_path: str
    source_objects_fd: int


def _validated_deadline(value: object) -> float:
    if type(value) not in {int, float}:
        raise LocalMainProofStale("local main deadline is invalid")
    deadline = float(value)
    now = time.monotonic()
    if (
        not math.isfinite(deadline)
        or deadline <= now
        or deadline - now > MAX_LOCAL_MAIN_DEADLINE_SECONDS
    ):
        raise LocalMainProofStale("local main deadline is invalid")
    return deadline


def _run_local_git(
    snapshot: SourceSnapshot,
    *args: str,
    **kwargs: object,
):
    """Run Git with repository callbacks disabled for every local-main read/effect."""

    return _run_git(
        snapshot.repo,
        "-c",
        "core.fsmonitor=false",
        *args,
        **kwargs,
    )


def _remote_credential_environment() -> dict[str, str]:
    environment = {"GIT_TERMINAL_PROMPT": "0"}
    for name in ("HOME", "LOGNAME", "SSH_AUTH_SOCK", "USER"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def _run_uninterruptible_git_effect(
    snapshot: SourceSnapshot,
    arguments: Sequence[str],
    *,
    credential_environment: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Finish one approved Git effect without killing Git mid-write."""

    command = [
        "/usr/bin/git",
        f"--git-dir={snapshot.repo.git_dir}",
        f"--work-tree={snapshot.repo.worktree}",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "filter.lfs.required=false",
        *arguments,
    ]
    extra_environment = (
        _remote_credential_environment()
        if credential_environment
        else {"GIT_TERMINAL_PROMPT": "0"}
    )
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                env=_git_environment(extra_environment),
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            raise LocalMainError("local main fast-forward could not start") from None
        try:
            returncode = process.wait()
        except BaseException:
            # Do not signal a possibly mutating Git process.  The durable
            # prepared record and Git refs are the recovery authority.
            raise LocalMainEffectUnknown(
                "local main fast-forward outcome is unknown"
            ) from None

        streams: list[bytes] = []
        for stream in (stdout_file, stderr_file):
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_PROMOTION_GIT_OUTPUT_BYTES:
                raise LocalMainEffectUnknown(
                    "local main fast-forward output exceeded its proof bound"
                )
            stream.seek(0)
            data = stream.read(MAX_PROMOTION_GIT_OUTPUT_BYTES + 1)
            if len(data) != size:
                raise LocalMainEffectUnknown(
                    "local main fast-forward output could not be proved"
                )
            streams.append(data)
    return subprocess.CompletedProcess(
        command,
        int(returncode),
        streams[0],
        streams[1],
    )


def _run_local_git_effect(
    snapshot: SourceSnapshot,
    integration_oid: str,
) -> subprocess.CompletedProcess[bytes]:
    return _run_uninterruptible_git_effect(
        snapshot,
        (
            "merge",
            "--ff-only",
            "--no-autostash",
            "--no-edit",
            integration_oid,
        ),
    )


def _run_push_effect(
    snapshot: SourceSnapshot,
    arguments: Sequence[str],
    *,
    deadline: float,
) -> subprocess.CompletedProcess[bytes]:
    """Run one remote effect within the caller's hard deadline."""

    control = _create_push_control_repo(snapshot, deadline=deadline)
    cleanup_allowed = True
    try:
        named = os.stat(control.path, follow_symlinks=False)
        opened = os.fstat(control.root_fd)
        source_named = os.stat(
            control.source_objects_path, follow_symlinks=False,
        )
        source_opened = os.fstat(control.source_objects_fd)
        if (
            not stat.S_ISDIR(named.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (named.st_dev, named.st_ino) != control.identity
            or (opened.st_dev, opened.st_ino) != control.identity
            or not stat.S_ISDIR(source_named.st_mode)
            or not stat.S_ISDIR(source_opened.st_mode)
            or (source_named.st_dev, source_named.st_ino)
            != (source_opened.st_dev, source_opened.st_ino)
        ):
            raise LocalPushStale("local push control root identity changed")
        command = [
            "/usr/bin/git",
            f"--git-dir={control.path}",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "filter.lfs.required=false",
            *arguments,
        ]
        return _run_bounded_remote_command(
            command,
            deadline=deadline,
            pass_fds=(control.root_fd,),
            effect=True,
        )
    except _PushProcessNotExtinct:
        cleanup_allowed = False
        raise
    finally:
        try:
            if cleanup_allowed:
                _cleanup_push_control_repo(control, deadline=deadline)
        finally:
            for descriptor in (
                control.source_objects_fd,
                control.root_fd,
                control.parent_fd,
            ):
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _run_bounded_remote_command(
    command: Sequence[str],
    *,
    deadline: float,
    pass_fds: tuple[int, ...] = (),
    effect: bool,
) -> subprocess.CompletedProcess[bytes]:
    """Run one config-isolated transport command under an absolute deadline."""

    now = time.monotonic()
    effect_deadline = deadline - LOCAL_PUSH_FINISH_RESERVE_SECONDS
    if (
        not math.isfinite(deadline)
        or effect_deadline <= now
    ):
        raise LocalPushStale("local push deadline has no effect reserve")
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                env=_git_environment(_remote_credential_environment()),
                cwd="/",
                close_fds=True,
                pass_fds=pass_fds,
                start_new_session=True,
            )
        except OSError:
            if effect:
                raise LocalMainError("local push could not start") from None
            raise LocalPushStale("local push remote read could not start") from None
        timed_out = False
        try:
            while process.poll() is None:
                if time.monotonic() >= effect_deadline:
                    timed_out = True
                    break
                time.sleep(0.005)
        except BaseException:
            _extinguish_push_process(process, deadline=deadline)
            if effect:
                raise LocalPushEffectUnknown(
                    "local push outcome is unknown"
                ) from None
            raise LocalPushStale("local push remote read was interrupted") from None
        if timed_out:
            _extinguish_push_process(process, deadline=deadline)
            if effect:
                raise LocalPushEffectUnknown(
                    "local push deadline expired; outcome is unknown"
                )
            raise LocalPushStale("local push remote read deadline expired")
        try:
            _extinguish_push_process(process, deadline=deadline)
        except LocalPushEffectUnknown:
            if effect:
                raise
            raise LocalPushStale(
                "local push remote read extinction is unknown"
            ) from None
        returncode = process.returncode
        if returncode is None:
            if effect:
                raise LocalPushEffectUnknown(
                    "local push process outcome is unknown"
                )
            raise LocalPushStale("local push remote read outcome is unknown")
        streams: list[bytes] = []
        for stream in (stdout_file, stderr_file):
            if time.monotonic() >= deadline:
                if effect:
                    raise LocalPushEffectUnknown(
                        "local push output proof deadline expired"
                    )
                raise LocalPushStale(
                    "local push remote read proof deadline expired"
                )
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_PROMOTION_GIT_OUTPUT_BYTES:
                if effect:
                    raise LocalPushEffectUnknown(
                        "local push output exceeded its proof bound"
                    )
                raise LocalPushStale(
                    "local push remote read output exceeded its proof bound"
                )
            stream.seek(0)
            data = stream.read(MAX_PROMOTION_GIT_OUTPUT_BYTES + 1)
            if len(data) != size:
                if effect:
                    raise LocalPushEffectUnknown(
                        "local push output could not be proved"
                    )
                raise LocalPushStale(
                    "local push remote read output could not be proved"
                )
            streams.append(data)
    return subprocess.CompletedProcess(
        list(command),
        int(returncode),
        streams[0],
        streams[1],
    )


def _write_control_file(
    directory_fd: int,
    name: bytes,
    data: bytes,
    *,
    deadline: float,
) -> None:
    if time.monotonic() >= deadline:
        raise LocalPushStale("local push control deadline expired")
    descriptor = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(data):
            if time.monotonic() >= deadline:
                raise LocalPushStale("local push control deadline expired")
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("short control write")
            offset += written
    finally:
        os.close(descriptor)


def _open_directory_at(parent_fd: int, name: bytes) -> int:
    return os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )


def _create_push_control_repo(
    snapshot: SourceSnapshot,
    *,
    deadline: float,
) -> _PushControlRepo:
    if time.monotonic() >= deadline:
        raise LocalPushStale("local push control deadline expired")
    raw_root = tempfile.mkdtemp(prefix="hermes-bestplan-push-")
    root_path = Path(raw_root).resolve(strict=True)
    leaf = os.fsencode(root_path.name)
    parent_fd = -1
    root_fd = -1
    source_objects_fd = -1
    try:
        parent_fd = os.open(
            os.fspath(root_path.parent),
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        named = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        root_fd = _open_directory_at(parent_fd, leaf)
        opened = os.fstat(root_fd)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISDIR(named.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (named.st_dev, named.st_ino) != identity
            or stat.S_IMODE(opened.st_mode) != 0o700
            or opened.st_uid != os.geteuid()
        ):
            raise LocalPushStale("local push control root is not private")
        source_objects_path = os.fspath(
            Path(snapshot.repo.common_dir, "objects").resolve(strict=True)
        )
        source_objects_raw = os.fsencode(source_objects_path)
        if b"\0" in source_objects_raw or b"\n" in source_objects_raw:
            raise LocalPushStale("local push source objects path is unsupported")
        source_objects_fd = os.open(
            source_objects_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISDIR(os.fstat(source_objects_fd).st_mode):
            raise LocalPushStale("local push source objects are unavailable")

        for name in (b"objects", b"refs"):
            os.mkdir(name, 0o700, dir_fd=root_fd)
        objects_fd = _open_directory_at(root_fd, b"objects")
        refs_fd = _open_directory_at(root_fd, b"refs")
        try:
            for name in (b"info", b"pack"):
                os.mkdir(name, 0o700, dir_fd=objects_fd)
            os.mkdir(b"heads", 0o700, dir_fd=refs_fd)
            os.mkdir(b"tags", 0o700, dir_fd=refs_fd)
            info_fd = _open_directory_at(objects_fd, b"info")
            try:
                _write_control_file(
                    info_fd,
                    b"alternates",
                    source_objects_raw + b"\n",
                    deadline=deadline,
                )
            finally:
                os.close(info_fd)
        finally:
            os.close(objects_fd)
            os.close(refs_fd)
        config = (
            b"[core]\n"
            + (
                b"\trepositoryformatversion = 1\n"
                if snapshot.repo.object_format == "sha256"
                else b"\trepositoryformatversion = 0\n"
            )
            + b"\tbare = true\n"
            + (
                b"[extensions]\n\tobjectFormat = sha256\n"
                if snapshot.repo.object_format == "sha256"
                else b""
            )
        )
        _write_control_file(root_fd, b"config", config, deadline=deadline)
        _write_control_file(
            root_fd, b"HEAD", b"ref: refs/heads/main\n", deadline=deadline,
        )
        return _PushControlRepo(
            path=os.fspath(root_path),
            root_fd=root_fd,
            parent_fd=parent_fd,
            leaf=leaf,
            identity=identity,
            source_objects_path=source_objects_path,
            source_objects_fd=source_objects_fd,
        )
    except BaseException:
        if root_fd >= 0:
            try:
                source_boundary._remove_owned_tree_contents(
                    root_fd, deadline=deadline,
                )
                os.rmdir(leaf, dir_fd=parent_fd)
            except BaseException:
                pass
        for descriptor in (source_objects_fd, root_fd, parent_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise


def _cleanup_push_control_repo(
    control: _PushControlRepo,
    *,
    deadline: float,
) -> None:
    try:
        source_boundary._remove_owned_tree_contents(
            control.root_fd, deadline=deadline,
        )
        if time.monotonic() >= deadline:
            raise LocalPushEffectUnknown("local push control cleanup expired")
        named = os.stat(
            control.leaf,
            dir_fd=control.parent_fd,
            follow_symlinks=False,
        )
        opened = os.fstat(control.root_fd)
        if (
            not stat.S_ISDIR(named.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (named.st_dev, named.st_ino) != control.identity
            or (opened.st_dev, opened.st_ino) != control.identity
        ):
            raise LocalPushEffectUnknown(
                "local push control cleanup identity changed"
            )
        os.rmdir(control.leaf, dir_fd=control.parent_fd)
    except LocalPushEffectUnknown:
        raise
    except BaseException:
        raise LocalPushEffectUnknown(
            "local push control cleanup failed; evidence retained"
        ) from None


def _push_process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        raise _PushProcessNotExtinct(
            "local push process extinction is unknown"
        ) from None
    return True


def _signal_push_process_group(process_group: int, value: int) -> None:
    try:
        os.killpg(process_group, value)
    except ProcessLookupError:
        return
    except OSError:
        raise _PushProcessNotExtinct(
            "local push process extinction is unknown"
        ) from None


def _extinguish_push_process(
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
) -> None:
    process_group = int(process.pid)
    process.poll()
    if _push_process_group_exists(process_group):
        _signal_push_process_group(process_group, signal.SIGTERM)
        term_deadline = min(
            deadline - LOCAL_PUSH_TERM_GRACE_SECONDS,
            time.monotonic() + LOCAL_PUSH_TERM_GRACE_SECONDS,
        )
        while time.monotonic() < term_deadline:
            process.poll()
            if not _push_process_group_exists(process_group):
                break
            time.sleep(0.005)
    process.poll()
    if _push_process_group_exists(process_group):
        _signal_push_process_group(process_group, signal.SIGKILL)
    while time.monotonic() < deadline:
        process.poll()
        if not _push_process_group_exists(process_group):
            break
        time.sleep(0.005)
    if _push_process_group_exists(process_group):
        raise _PushProcessNotExtinct(
            "local push process extinction is unknown"
        )
    if process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LocalPushEffectUnknown(
                "local push process reap is unknown"
            )
        try:
            process.wait(timeout=remaining)
        except (subprocess.TimeoutExpired, OSError):
            raise LocalPushEffectUnknown(
                "local push process reap is unknown"
            ) from None


def _read_symbolic_head(snapshot: SourceSnapshot, *, deadline: float) -> str:
    try:
        result = _run_local_git(
            snapshot,
            "symbolic-ref",
            "-q",
            "HEAD",
            deadline=deadline,
            cancel_event=None,
            check=False,
        )
    except PromotionError:
        raise LocalMainProofStale("local main target could not be read") from None
    if result.returncode != 0:
        raise LocalMainProofStale("local main target is not checked out")
    try:
        return result.stdout.strip().decode("ascii")
    except UnicodeError:
        raise LocalMainProofStale("local main target is malformed") from None


def _assert_repository_identity(snapshot: SourceSnapshot, *, deadline: float) -> None:
    try:
        current = resolve_repo_identity(snapshot.repo.workspace, deadline=deadline)
    except SourceBoundaryError:
        raise LocalMainProofStale("local main repository identity is unavailable") from None
    if current != snapshot.repo:
        raise LocalMainProofStale("local main repository identity changed")


def _assert_target(
    snapshot: SourceSnapshot,
    *,
    expected_oid: str,
    deadline: float,
) -> None:
    if _read_symbolic_head(snapshot, deadline=deadline) != LOCAL_MAIN_REF:
        raise LocalMainProofStale("local main target is not checked out")
    try:
        current_oid = _read_ref(
            snapshot.repo,
            LOCAL_MAIN_REF,
            deadline=deadline,
            cancel_event=None,
        )
    except PromotionError:
        raise LocalMainProofStale("local main target could not be read") from None
    if current_oid != expected_oid:
        raise LocalMainProofStale("local main target changed")


def _assert_inputs(
    snapshot: SourceSnapshot,
    integration: FrozenIntegration,
    checks: CheckSetReceipt,
    commands: Sequence[BoundCommand],
) -> None:
    if not isinstance(snapshot, SourceSnapshot):
        raise LocalMainProofStale("local main source snapshot is invalid")
    if not isinstance(integration, FrozenIntegration):
        raise LocalMainProofStale("local main integration proof is invalid")
    if not isinstance(checks, CheckSetReceipt):
        raise LocalMainProofStale("local main check receipt is invalid")
    if (
        not snapshot.head_symbolic
        or snapshot.head_ref != LOCAL_MAIN_REF.encode("ascii")
        or integration.target_ref != LOCAL_MAIN_REF
        or integration.target_oid != snapshot.head_oid
    ):
        raise LocalMainProofStale("local main target proof changed")
    if (
        checks.integration_oid != integration.integration_oid
        or checks.contract_digest != integration.contract_digest
    ):
        raise LocalMainProofStale("local main check receipt is stale")
    ordered_commands = tuple(commands)
    receipts = tuple(checks.ordered_receipts)
    if (
        not ordered_commands
        or any(not isinstance(command, BoundCommand) for command in ordered_commands)
        or len(receipts) != len(ordered_commands)
    ):
        raise LocalMainProofStale("local main check receipt is incomplete")
    receipt_digests: list[str] = []
    for command, receipt in zip(ordered_commands, receipts):
        if not isinstance(receipt, CheckReceipt):
            raise LocalMainProofStale("local main check receipt is invalid")
        command_digest = _command_digest(command)
        if (
            receipt.integration_oid != integration.integration_oid
            or receipt.command_id != command.identifier
            or receipt.command_digest != command_digest
            or receipt.exit_code != 0
            or isinstance(receipt.stdout_size, bool)
            or isinstance(receipt.stderr_size, bool)
            or not isinstance(receipt.stdout_size, int)
            or not isinstance(receipt.stderr_size, int)
            or receipt.stdout_size < 0
            or receipt.stderr_size < 0
        ):
            raise LocalMainProofStale("local main check receipt is stale")
        output_body = {
            "integration_oid": receipt.integration_oid,
            "command_digest": receipt.command_digest,
            "exit_code": receipt.exit_code,
            "stdout_sha256": receipt.stdout_sha256,
            "stderr_sha256": receipt.stderr_sha256,
            "stdout_size": receipt.stdout_size,
            "stderr_size": receipt.stderr_size,
        }
        output_digest = _domain_digest(
            b"hermes.bestplan.check-output.v1", output_body,
        )
        receipt_body = {
            **output_body,
            "command_id": receipt.command_id,
            "policy_digest": receipt.policy_digest,
            "output_framed_sha256": output_digest,
            "pre_tree_digest": receipt.pre_tree_digest,
            "post_tree_digest": receipt.post_tree_digest,
        }
        expected_receipt = _domain_digest(
            b"hermes.bestplan.check-receipt.v1", receipt_body,
        )
        if (
            receipt.output_framed_sha256 != output_digest
            or receipt.receipt_digest != expected_receipt
        ):
            raise LocalMainProofStale("local main check receipt digest differs")
        receipt_digests.append(receipt.receipt_digest)
    set_body = {
        "integration_oid": checks.integration_oid,
        "contract_digest": checks.contract_digest,
        "ordered_receipts": receipt_digests,
    }
    if checks.receipt_digest != _domain_digest(
        b"hermes.bestplan.check-set.v1", set_body,
    ):
        raise LocalMainProofStale("local main check set digest differs")


def _assert_integration(
    snapshot: SourceSnapshot,
    integration: FrozenIntegration,
    *,
    deadline: float,
) -> tuple[dict[bytes, object], dict[bytes, object]]:
    try:
        if _read_ref(
            snapshot.repo,
            integration.ref_name,
            deadline=deadline,
            cancel_event=None,
        ) != integration.integration_oid:
            raise LocalMainProofStale("local main integration ref changed")
        tree_oid, parents = _commit_proof(
            snapshot.repo,
            integration.integration_oid,
            deadline=deadline,
            cancel_event=None,
        )
        if tree_oid != integration.tree_oid or parents != (snapshot.head_oid,):
            raise LocalMainProofStale("local main integration commit changed")
        target_tree = _tree_map(
            snapshot.repo,
            snapshot.head_oid,
            deadline=deadline,
            cancel_event=None,
        )
        integration_tree = _tree_map(
            snapshot.repo,
            integration.integration_oid,
            deadline=deadline,
            cancel_event=None,
        )
        return target_tree, integration_tree
    except LocalMainProofStale:
        raise
    except (PromotionError, IntegrationValidationError):
        raise LocalMainProofStale("local main integration proof is stale") from None


def _parent_paths(path: bytes) -> tuple[bytes, ...]:
    parts = path.split(b"/")
    return tuple(b"/".join(parts[:index]) for index in range(1, len(parts)))


def _target_directory_aliases(target_paths: tuple[bytes, ...]) -> frozenset[str]:
    aliases: set[str] = set()
    for path in target_paths:
        for parent in _parent_paths(path):
            aliases.add(_path_alias(parent))
    return frozenset(aliases)


def _assert_no_protected_overlap(
    manifest: ProtectedManifest,
    *,
    target_paths: tuple[bytes, ...],
    incoming_paths: tuple[bytes, ...],
) -> None:
    target_directories = _target_directory_aliases(target_paths)
    try:
        for protected in manifest.protected_paths:
            for incoming in incoming_paths:
                if _path_related(protected, incoming):
                    raise LocalMainConflict(
                        "local main incoming path overlaps protected work"
                    )
                for parent in _parent_paths(protected):
                    if (
                        _path_alias(parent) not in target_directories
                        and _path_related(parent, incoming)
                    ):
                        raise LocalMainConflict(
                            "local main incoming path overlaps protected work"
                        )
    except LocalMainConflict:
        raise
    except IntegrationValidationError:
        raise LocalMainConflict(
            "local main protected path overlap could not be proved absent"
        ) from None


def _assert_no_ignored_overlap(
    snapshot: SourceSnapshot,
    *,
    incoming_paths: tuple[bytes, ...],
    deadline: float,
) -> None:
    pathspecs: set[bytes] = set(incoming_paths)
    for path in incoming_paths:
        pathspecs.update(_parent_paths(path))
    ordered = tuple(sorted(pathspecs))
    for offset in range(0, len(ordered), 64):
        batch = ordered[offset : offset + 64]
        try:
            arguments = tuple(path.decode("utf-8", "strict") for path in batch)
            result = _run_local_git(
                snapshot,
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--",
                *arguments,
                extra_environment={"GIT_LITERAL_PATHSPECS": "1"},
                deadline=deadline,
                cancel_event=None,
            )
        except (PromotionError, UnicodeError):
            raise LocalMainProofStale(
                "local main ignored paths could not be proved absent"
            ) from None
        records = result.stdout.split(b"\0")
        if records and records[-1] == b"":
            records.pop()
        if any(not path or b"\0" in path for path in records):
            raise LocalMainProofStale(
                "local main ignored paths could not be proved absent"
            )
        try:
            if any(
                _path_related(ignored, incoming)
                for ignored in records
                for incoming in incoming_paths
            ):
                raise LocalMainConflict(
                    "local main incoming path overlaps ignored local work"
                )
        except IntegrationValidationError:
            raise LocalMainProofStale(
                "local main ignored path identity is invalid"
            ) from None


def _assert_no_active_filters(
    snapshot: SourceSnapshot,
    integration: FrozenIntegration,
    *,
    incoming_paths: tuple[bytes, ...],
    deadline: float,
) -> None:
    """Reject content-filter execution during the local fast-forward."""

    if not incoming_paths:
        return
    try:
        result = _run_local_git(
            snapshot,
            "check-attr",
            f"--source={integration.integration_oid}",
            "--stdin",
            "-z",
            "filter",
            input_bytes=b"\0".join(incoming_paths) + b"\0",
            deadline=deadline,
            cancel_event=None,
        )
    except PromotionError:
        raise LocalMainProofStale(
            "local main protected state could not prove Git filters absent"
        ) from None
    records = result.stdout.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    if len(records) != 3 * len(incoming_paths):
        raise LocalMainProofStale(
            "local main protected state returned malformed Git attributes"
        )
    for index in range(0, len(records), 3):
        path, attribute, value = records[index : index + 3]
        if (
            path != incoming_paths[index // 3]
            or attribute != b"filter"
            or not value
        ):
            raise LocalMainProofStale(
                "local main protected state returned malformed Git attributes"
            )
        if value not in {b"unspecified", b"unset"}:
            raise LocalMainProofStale(
                "local main protected state enables Git filters"
            )


def _protected_projection(manifest: ProtectedManifest) -> tuple[object, ...]:
    protected = frozenset(manifest.protected_paths)
    return (
        manifest.protected_paths,
        tuple(item for item in manifest.index_entries if item.path in protected),
        tuple(item for item in manifest.index_flags if item.path in protected),
        tuple(item for item in manifest.worktree_entries if item.path in protected),
        manifest.staged_diff_sha256,
        manifest.unstaged_diff_sha256,
    )


def _capture_manifest(snapshot: SourceSnapshot, *, deadline: float) -> ProtectedManifest:
    try:
        return capture_protected_manifest(snapshot.repo, deadline=deadline)
    except SourceBoundaryError:
        raise LocalMainProofStale("local main protected state is unavailable") from None


_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCP_PUSH_URL_RE = re.compile(
    r"^(?:[A-Za-z0-9._-]+@)?"
    r"(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\]):[^\s?#]+$"
)
_URL_PUSH_SCHEMES = frozenset({"file", "git", "http", "https", "ssh"})


def _decode_git_line(value: bytes, label: str) -> str:
    try:
        decoded = value.strip().decode("utf-8", "strict")
    except UnicodeError:
        raise LocalPushStale(f"local push {label} is malformed") from None
    if not decoded or len(decoded.encode("utf-8")) > 4096 or any(
        ord(character) < 32 or ord(character) == 127 for character in decoded
    ):
        raise LocalPushStale(f"local push {label} is malformed")
    return decoded


def _read_local_config(
    snapshot: SourceSnapshot,
    key: str,
    *,
    deadline: float,
) -> str:
    try:
        result = _run_local_git(
            snapshot,
            "config",
            "--local",
            "--get",
            key,
            deadline=deadline,
            cancel_event=None,
            check=False,
        )
    except PromotionError:
        raise LocalPushStale("local push configuration is unavailable") from None
    if result.returncode != 0:
        raise LocalPushStale("local push configuration is incomplete")
    return _decode_git_line(result.stdout, "configuration")


def _read_local_config_values(
    snapshot: SourceSnapshot,
    key: str,
    *,
    deadline: float,
) -> tuple[str, ...]:
    try:
        result = _run_local_git(
            snapshot,
            "config",
            "--local",
            "--get-all",
            key,
            deadline=deadline,
            cancel_event=None,
            check=False,
        )
    except PromotionError:
        raise LocalPushStale("local push configuration is unavailable") from None
    if result.returncode == 1 and not result.stdout:
        return ()
    if result.returncode != 0:
        raise LocalPushStale("local push configuration is unavailable")
    return tuple(
        _decode_git_line(line, "configuration")
        for line in result.stdout.splitlines()
    )


def _credential_free_remote_display(raw_url: str) -> str:
    if "://" in raw_url:
        parsed = urlsplit(raw_url)
        if not parsed.scheme or not parsed.hostname:
            raise LocalPushStale("local push remote URL is malformed")
        host = parsed.hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    if "@" in raw_url:
        _userinfo, separator, remainder = raw_url.partition("@")
        if separator and remainder:
            return remainder
    return raw_url


def _validated_exact_push_url(raw_url: str) -> str:
    """Accept only one non-secret endpoint that is safe as an argv value."""

    if raw_url.startswith("-"):
        raise LocalPushStale("local push remote URL is unsupported")
    if Path(raw_url).is_absolute():
        return raw_url
    if "://" in raw_url:
        try:
            parsed = urlsplit(raw_url)
            scheme = parsed.scheme.casefold()
            hostname = parsed.hostname
        except ValueError:
            raise LocalPushStale("local push remote URL is malformed") from None
        if (
            scheme not in _URL_PUSH_SCHEMES
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise LocalPushStale("local push remote URL is not credential-free")
        if scheme == "file":
            if parsed.netloc not in {"", "localhost"} or not parsed.path.startswith(
                "/"
            ):
                raise LocalPushStale("local push remote URL is malformed")
        elif hostname is None or not parsed.path:
            raise LocalPushStale("local push remote URL is malformed")
        return raw_url
    if _SCP_PUSH_URL_RE.fullmatch(raw_url) is not None:
        return raw_url
    raise LocalPushStale("local push remote URL is unsupported")


def _remote_target_config(
    snapshot: SourceSnapshot,
    *,
    deadline: float,
) -> tuple[str, str, str, str, str]:
    remote_name = _read_local_config(
        snapshot,
        "branch.main.remote",
        deadline=deadline,
    )
    if _REMOTE_NAME_RE.fullmatch(remote_name) is None:
        raise LocalPushStale("local push remote name is unsupported")
    remote_ref = _read_local_config(
        snapshot,
        "branch.main.merge",
        deadline=deadline,
    )
    if remote_ref != LOCAL_MAIN_REF:
        raise LocalPushStale("local push target is not remote main")
    raw_urls = _read_local_config_values(
        snapshot,
        f"remote.{remote_name}.pushurl",
        deadline=deadline,
    )
    if not raw_urls:
        raw_urls = _read_local_config_values(
            snapshot,
            f"remote.{remote_name}.url",
            deadline=deadline,
        )
    if len(raw_urls) != 1:
        raise LocalPushStale("local push remote must have exactly one URL")
    raw_url = _validated_exact_push_url(raw_urls[0])
    display_url = _credential_free_remote_display(raw_url)
    identity = hashlib.sha256(
        b"hermes.bestplan.local-push-remote.v1\0"
        + remote_name.encode("utf-8")
        + b"\0"
        + remote_ref.encode("ascii")
        + b"\0"
        + raw_url.encode("utf-8")
    ).hexdigest()
    return remote_name, remote_ref, display_url, identity, raw_url


def _valid_repo_oid(snapshot: SourceSnapshot, value: str) -> bool:
    width = 64 if snapshot.repo.object_format == "sha256" else 40
    return re.fullmatch(rf"[0-9a-f]{{{width}}}", value) is not None


def _read_remote_oid(
    snapshot: SourceSnapshot,
    exact_push_url: str,
    remote_ref: str,
    *,
    deadline: float,
) -> str:
    result = _run_bounded_remote_command(
        (
            "/usr/bin/git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.interactive=false",
            "ls-remote",
            "--exit-code",
            "--refs",
            exact_push_url,
            remote_ref,
        ),
        deadline=deadline,
        effect=False,
    )
    if result.returncode != 0:
        raise LocalPushStale("local push remote main is unavailable")
    lines = result.stdout.splitlines()
    if len(lines) != 1:
        raise LocalPushStale("local push remote ref is malformed")
    try:
        oid_raw, named_ref = lines[0].split(b"\t", 1)
        oid = oid_raw.decode("ascii")
        ref = named_ref.decode("ascii")
    except (ValueError, UnicodeError):
        raise LocalPushStale("local push remote ref is malformed") from None
    if ref != remote_ref or not _valid_repo_oid(snapshot, oid):
        raise LocalPushStale("local push remote ref is malformed")
    return oid


def classify_local_main_for_push(
    *,
    snapshot: SourceSnapshot,
    expected_target_oid: str,
    integration_oid: str,
    deadline: float,
) -> str:
    """Classify local ``main`` for crash recovery without changing Git."""

    absolute_deadline = _validated_deadline(deadline)
    if not _valid_repo_oid(snapshot, expected_target_oid) or not _valid_repo_oid(
        snapshot, integration_oid,
    ):
        raise LocalMainProofStale("local push object identity is invalid")
    _assert_repository_identity(snapshot, deadline=absolute_deadline)
    if _read_symbolic_head(snapshot, deadline=absolute_deadline) != LOCAL_MAIN_REF:
        raise LocalMainProofStale("local main target is not checked out")
    try:
        current_oid = _read_ref(
            snapshot.repo,
            LOCAL_MAIN_REF,
            deadline=absolute_deadline,
            cancel_event=None,
        )
    except PromotionError:
        raise LocalMainProofStale("local main target could not be read") from None
    if current_oid == expected_target_oid:
        return "expected"
    if current_oid == integration_oid:
        return "integration"
    return "other"


def classify_local_push_remote(
    *,
    snapshot: SourceSnapshot,
    target: LocalMainPushTarget,
    deadline: float,
) -> str:
    """Classify the bound remote ref for idempotent push recovery."""

    absolute_deadline = _validated_deadline(deadline)
    if not isinstance(target, LocalMainPushTarget) or not _valid_repo_oid(
        snapshot, target.integration_oid,
    ):
        raise LocalPushStale("local push target is invalid")
    _assert_repository_identity(snapshot, deadline=absolute_deadline)
    configured = _remote_target_config(snapshot, deadline=absolute_deadline)
    if configured[:4] != (
        target.remote_name,
        target.remote_ref,
        target.display_url,
        target.remote_identity_sha256,
    ):
        raise LocalPushStale("local push remote identity changed")
    exact_push_url = configured[4]
    current_oid = _read_remote_oid(
        snapshot,
        exact_push_url,
        target.remote_ref,
        deadline=absolute_deadline,
    )
    if current_oid == target.observed_remote_oid:
        return "observed"
    if current_oid == target.integration_oid:
        return "integration"
    return "other"


def observe_prelanding_local_main_push_target(
    *,
    snapshot: SourceSnapshot,
    expected_target_oid: str,
    integration_oid: str,
    deadline: float,
) -> LocalMainPushTarget:
    """Bind the remote target before the durable local-main effect starts."""

    absolute_deadline = _validated_deadline(deadline)
    if not _valid_repo_oid(snapshot, expected_target_oid) or not _valid_repo_oid(
        snapshot, integration_oid,
    ):
        raise LocalPushStale("local push object identity is invalid")
    _assert_repository_identity(snapshot, deadline=absolute_deadline)
    _assert_target(
        snapshot,
        expected_oid=expected_target_oid,
        deadline=absolute_deadline,
    )
    try:
        _tree_oid, parents = _commit_proof(
            snapshot.repo,
            integration_oid,
            deadline=absolute_deadline,
            cancel_event=None,
        )
    except PromotionError:
        raise LocalPushStale("local push integration proof is unavailable") from None
    if parents != (expected_target_oid,):
        raise LocalPushStale("local push integration parent differs")
    (
        remote_name,
        remote_ref,
        display_url,
        identity,
        exact_push_url,
    ) = _remote_target_config(
        snapshot,
        deadline=absolute_deadline,
    )
    observed_oid = _read_remote_oid(
        snapshot,
        exact_push_url,
        remote_ref,
        deadline=absolute_deadline,
    )
    return LocalMainPushTarget(
        remote_name=remote_name,
        remote_ref=remote_ref,
        display_url=display_url,
        remote_identity_sha256=identity,
        observed_remote_oid=observed_oid,
        integration_oid=integration_oid,
    )


def push_exact_local_main(
    *,
    snapshot: SourceSnapshot,
    target: LocalMainPushTarget,
    deadline: float,
) -> LocalMainPushReceipt:
    """Make one explicit normal push and verify the exact remote object."""

    absolute_deadline = _validated_deadline(deadline)
    if not isinstance(target, LocalMainPushTarget):
        raise LocalPushStale("local push target is invalid")
    _assert_repository_identity(snapshot, deadline=absolute_deadline)
    _assert_target(
        snapshot,
        expected_oid=target.integration_oid,
        deadline=absolute_deadline,
    )
    configured = _remote_target_config(snapshot, deadline=absolute_deadline)
    if configured[:4] != (
        target.remote_name,
        target.remote_ref,
        target.display_url,
        target.remote_identity_sha256,
    ):
        raise LocalPushStale("local push remote identity changed")
    exact_push_url = configured[4]
    current_remote = _read_remote_oid(
        snapshot,
        exact_push_url,
        target.remote_ref,
        deadline=absolute_deadline,
    )
    if current_remote == target.integration_oid:
        return LocalMainPushReceipt(
            remote_name=target.remote_name,
            remote_ref=target.remote_ref,
            integration_oid=target.integration_oid,
            remote_oid=current_remote,
        )
    if current_remote != target.observed_remote_oid:
        raise LocalPushStale("local push remote main changed")
    ancestry = _run_local_git(
        snapshot,
        "merge-base",
        "--is-ancestor",
        current_remote,
        target.integration_oid,
        deadline=absolute_deadline,
        cancel_event=None,
        check=False,
    )
    if ancestry.returncode != 0:
        raise LocalPushStale("local push is not a remote fast-forward")

    arguments = (
        "-c",
        "push.followTags=false",
        "-c",
        "push.recurseSubmodules=no",
        "-c",
        "push.gpgSign=false",
        "push",
        "--porcelain",
        "--no-follow-tags",
        "--recurse-submodules=no",
        "--no-signed",
        "--no-push-option",
        exact_push_url,
        f"{target.integration_oid}:{target.remote_ref}",
    )
    effect = _run_push_effect(
        snapshot, arguments, deadline=absolute_deadline,
    )
    try:
        remote_oid = _read_remote_oid(
            snapshot,
            exact_push_url,
            target.remote_ref,
            deadline=absolute_deadline,
        )
    except LocalPushStale:
        raise LocalPushEffectUnknown(
            "local push outcome could not be read back"
        ) from None
    if remote_oid != target.integration_oid:
        if effect.returncode != 0:
            raise LocalPushConflict("exact non-force push was rejected")
        raise LocalPushEffectUnknown("local push remote proof differs")
    return LocalMainPushReceipt(
        remote_name=target.remote_name,
        remote_ref=target.remote_ref,
        integration_oid=target.integration_oid,
        remote_oid=remote_oid,
    )


def land_checked_integration(
    *,
    snapshot: SourceSnapshot,
    integration: FrozenIntegration,
    checks: CheckSetReceipt,
    commands: Sequence[BoundCommand],
    deadline: float,
) -> LocalMainLandingReceipt:
    """Fast-forward checked-out local ``main`` to one checked commit."""

    absolute_deadline = _validated_deadline(deadline)
    _assert_inputs(snapshot, integration, checks, commands)
    _assert_repository_identity(snapshot, deadline=absolute_deadline)
    _assert_target(
        snapshot,
        expected_oid=snapshot.head_oid,
        deadline=absolute_deadline,
    )
    target_tree, integration_tree = _assert_integration(
        snapshot,
        integration,
        deadline=absolute_deadline,
    )
    try:
        incoming_paths = _changed_paths(
            target_tree,
            integration_tree,
            deadline=absolute_deadline,
            cancel_event=None,
        )
    except PromotionError:
        raise LocalMainProofStale("local main incoming paths are unavailable") from None

    before = _capture_manifest(snapshot, deadline=absolute_deadline)
    if before != snapshot.protected_manifest:
        raise LocalMainProofStale("local main protected state changed")
    _assert_no_ignored_overlap(
        snapshot,
        incoming_paths=incoming_paths,
        deadline=absolute_deadline,
    )
    _assert_no_active_filters(
        snapshot,
        integration,
        incoming_paths=incoming_paths,
        deadline=absolute_deadline,
    )
    if any(
        component.lower() == b".gitignore"
        for path in incoming_paths
        for component in path.split(b"/")
    ):
        raise LocalMainConflict(
            "local main incoming change modifies ignore rules"
        )
    _assert_no_protected_overlap(
        before,
        target_paths=tuple(target_tree),
        incoming_paths=incoming_paths,
    )

    # Re-read the only mutable target immediately before the effect.
    _assert_target(
        snapshot,
        expected_oid=snapshot.head_oid,
        deadline=absolute_deadline,
    )
    result = _run_local_git_effect(snapshot, integration.integration_oid)
    if result.returncode != 0:
        raise LocalMainConflict("local main fast-forward conflicts with local work") from None
    postflight_deadline = time.monotonic() + LOCAL_MAIN_POSTFLIGHT_SECONDS

    _assert_target(
        snapshot,
        expected_oid=integration.integration_oid,
        deadline=postflight_deadline,
    )
    after = _capture_manifest(snapshot, deadline=postflight_deadline)
    if _protected_projection(after) != _protected_projection(before):
        raise LocalMainConflict("local main fast-forward changed protected work")

    return LocalMainLandingReceipt(
        target_ref=LOCAL_MAIN_REF,
        old_oid=snapshot.head_oid,
        new_oid=integration.integration_oid,
        check_receipt_digest=checks.receipt_digest,
    )
