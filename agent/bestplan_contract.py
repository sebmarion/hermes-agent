"""Pure enrollment and execution-contract primitives for BestPlan V2.

The model still emits the literal V1 envelope.  A trusted host may attach one
canonical V2 contract only when an injected authority returns one exact active
enrollment for the repository identity.  This module performs no I/O.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping
from urllib.parse import parse_qsl, quote_from_bytes, unquote_to_bytes, urlsplit, urlunsplit

from agent.bestplan_authority_client import AuthorityUnavailable, BestplanAuthorityClient
from agent.bestplan_source import (
    IndexEntry,
    IndexFlags,
    ProtectedManifest,
    ProtectedPath,
    RepoIdentity,
    SourceSnapshot,
)


CONTRACT_SCHEMA = "hermes.bestplan.promotion_contract.v2"
SOURCE_SNAPSHOT_SCHEMA = "hermes.bestplan.source_snapshot.v1"
ENROLLMENT_SCHEMA = "hermes.bestplan.enrollment.v1"
EXECUTION_PROTOCOL = 2
PROMOTION_CONTRACT_VERSION = 2
LOCAL_MAIN_REF = "refs/heads/main"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ENV_PARTS = {
    "api", "auth", "authorization", "bearer", "cookie", "credential",
    "key", "password", "secret", "token",
}


class ContractValidationError(ValueError):
    """A contract or enrollment violates the frozen V2 schema."""


class MalformedEnrollmentError(ContractValidationError):
    """A matching authority enrollment is malformed or unsupported."""


class AmbiguousEnrollmentError(ContractValidationError):
    """More than one enrollment matches one configured repository reference."""


def _strict_string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        suffix = "a string" if allow_empty else "a nonempty string"
        raise ContractValidationError(f"{name} must be {suffix}")
    if "\x00" in value:
        raise ContractValidationError(f"{name} contains NUL")
    return value


def _strict_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{name} must be a bool")
    return value


def _sha256(value: Any, name: str) -> str:
    text = _strict_string(value, name)
    if not _SHA256_RE.fullmatch(text):
        raise ContractValidationError(f"{name} must be a lowercase sha256 digest")
    return text


def _git_oid(value: Any, object_format: str, name: str) -> str:
    text = _strict_string(value, name)
    matcher = _SHA1_RE if object_format == "sha1" else _SHA256_RE
    if object_format not in {"sha1", "sha256"} or not matcher.fullmatch(text):
        raise ContractValidationError(f"{name} is not a valid {object_format} object id")
    return text


def _exact_mapping(value: Any, keys: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ContractValidationError(f"{name} must use string keys")
    actual = set(value)
    missing = sorted(keys - actual)
    unknown = sorted(actual - keys)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing={missing}")
        if unknown:
            details.append(f"unknown={unknown}")
        raise ContractValidationError(f"{name} has invalid fields ({', '.join(details)})")
    return value


def _canonical_value(value: Any, path: str = "$") -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        raise ContractValidationError(f"float values are forbidden at {path}")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractValidationError(f"canonical objects require string keys at {path}")
        return {
            key: _canonical_value(value[key], f"{path}.{key}")
            for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise ContractValidationError(
        f"unsupported canonical value {type(value).__name__} at {path}"
    )


def canonical_json(value: Any) -> str:
    """Return strict, deterministic JSON without accepting lossy number types."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _b64(raw: bytes, name: str = "bytes") -> str:
    if not isinstance(raw, bytes):
        raise ContractValidationError(f"{name} must be bytes")
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: Any, name: str) -> bytes:
    text = _strict_string(value, name, allow_empty=True)
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ContractValidationError(f"{name} must be canonical base64") from exc
    if _b64(raw) != text:
        raise ContractValidationError(f"{name} must be canonical base64")
    return raw


def _validate_display_raw(display: str, raw: bytes, name: str) -> None:
    try:
        encoded = os.fsencode(display)
    except (UnicodeError, ValueError) as exc:
        raise ContractValidationError(f"{name} display path is not lossless") from exc
    if encoded != raw:
        raise ContractValidationError(f"{name} raw/display path identity differs")


@dataclass(frozen=True)
class PinnedInput:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        path = _strict_string(self.path, "pinned input path")
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise ContractValidationError("pinned input path must be logical and relative")
        _sha256(self.sha256, "pinned input sha256")


@dataclass(frozen=True)
class BoundCommand:
    identifier: str
    executable: str
    executable_sha256: str
    argv: tuple[str, ...]
    logical_cwd: str
    env: tuple[tuple[str, str], ...]
    inputs: tuple[PinnedInput, ...]
    cache: tuple[PinnedInput, ...]
    timeout_seconds: int
    network_allowlist: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier = _strict_string(self.identifier, "command identifier")
        if not _IDENTIFIER_RE.fullmatch(identifier):
            raise ContractValidationError("command identifier is malformed")
        executable = _strict_string(self.executable, "command executable")
        if not PurePosixPath(executable).is_absolute():
            raise ContractValidationError("command executable must be absolute")
        _sha256(self.executable_sha256, "command executable_sha256")
        if not isinstance(self.argv, tuple) or any(
            not isinstance(arg, str) or "\x00" in arg for arg in self.argv
        ):
            raise ContractValidationError("command argv must be a tuple of strings")
        credential_flags = {
            "--api-key", "--api_key", "--authorization", "--bearer",
            "--credential", "--password", "--secret", "--token",
        }
        credential_assignment = re.compile(
            r"(?i)^(?:--)?(?:api[-_]?key|authorization|bearer|credential|password|secret|token)="
        )
        for arg in self.argv:
            if arg.casefold() in credential_flags or credential_assignment.match(arg):
                raise ContractValidationError("command argv cannot contain credential arguments")
            if "://" in arg:
                try:
                    parsed_arg = urlsplit(arg)
                except ValueError as exc:
                    raise ContractValidationError("command argv URL is malformed") from exc
                if parsed_arg.username is not None or parsed_arg.password is not None:
                    raise ContractValidationError("command argv cannot contain credential URLs")
        cwd = _strict_string(self.logical_cwd, "command logical_cwd")
        if PurePosixPath(cwd).is_absolute() or ".." in PurePosixPath(cwd).parts:
            raise ContractValidationError("command logical_cwd must be a logical relative name")
        if not isinstance(self.env, tuple) or any(
            not isinstance(item, tuple) or len(item) != 2 for item in self.env
        ):
            raise ContractValidationError("command env must be a tuple of key/value pairs")
        if tuple(sorted(self.env)) != self.env or len({key for key, _ in self.env}) != len(self.env):
            raise ContractValidationError("command env must be sorted with unique keys")
        for key, value in self.env:
            if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
                raise ContractValidationError("command env key is malformed")
            if not isinstance(value, str) or "\x00" in value:
                raise ContractValidationError("command env value must be a string")
            normalized_parts = set(re.split(r"[^a-z0-9]+", key.casefold()))
            if normalized_parts & _SECRET_ENV_PARTS:
                raise ContractValidationError(f"command env contains secret-like key {key}")
        for name, items in (("inputs", self.inputs), ("cache", self.cache)):
            if not isinstance(items, tuple) or any(
                not isinstance(item, PinnedInput) for item in items
            ):
                raise ContractValidationError(f"command {name} must contain PinnedInput values")
            if tuple(sorted(items, key=lambda item: item.path)) != items:
                raise ContractValidationError(f"command {name} must be sorted by path")
            if len({item.path for item in items}) != len(items):
                raise ContractValidationError(
                    f"command {name} paths must be unique"
                )
        _strict_int(self.timeout_seconds, "command timeout_seconds", minimum=1)
        if not isinstance(self.network_allowlist, tuple) or any(
            not isinstance(item, str) or not item for item in self.network_allowlist
        ):
            raise ContractValidationError("command network_allowlist must be a tuple of strings")
        if tuple(sorted(set(self.network_allowlist))) != self.network_allowlist:
            raise ContractValidationError("command network_allowlist must be sorted and unique")
        if any("@" in item or "://" in item and urlsplit(item).username for item in self.network_allowlist):
            raise ContractValidationError("command network_allowlist cannot contain credentials")


@dataclass(frozen=True)
class EnrolledRepository:
    repository_id: str
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

    def __post_init__(self) -> None:
        _strict_string(self.repository_id, "repository_id")
        for display_name, raw_name in (
            ("workspace", "workspace_raw"),
            ("worktree", "worktree_raw"),
            ("git_dir", "git_dir_raw"),
            ("common_dir", "common_dir_raw"),
        ):
            display = _strict_string(getattr(self, display_name), display_name)
            raw = getattr(self, raw_name)
            if not isinstance(raw, bytes):
                raise ContractValidationError(f"{raw_name} must be bytes")
            _validate_display_raw(display, raw, display_name)
        _strict_int(self.common_dir_device, "common_dir_device")
        _strict_int(self.common_dir_inode, "common_dir_inode", minimum=1)
        if self.object_format not in {"sha1", "sha256"}:
            raise ContractValidationError("repository object_format must be sha1 or sha256")

    @classmethod
    def from_repo_identity(cls, repo: RepoIdentity) -> "EnrolledRepository":
        if not isinstance(repo, RepoIdentity):
            raise ContractValidationError("repo must be a RepoIdentity")
        return cls(
            repository_id=repo.repository_id,
            workspace=repo.workspace,
            workspace_raw=repo.workspace_raw,
            worktree=repo.worktree,
            worktree_raw=repo.worktree_raw,
            git_dir=repo.git_dir,
            git_dir_raw=repo.git_dir_raw,
            common_dir=repo.common_dir,
            common_dir_raw=repo.common_dir_raw,
            common_dir_device=repo.common_dir_device,
            common_dir_inode=repo.common_dir_inode,
            object_format=repo.object_format,
        )

    def matches(self, repo: RepoIdentity) -> bool:
        try:
            return self == EnrolledRepository.from_repo_identity(repo)
        except ContractValidationError:
            return False


def normalize_git_push_identity(url: str) -> str:
    """Normalize a push destination without retaining provider credentials."""

    raw = _strict_string(url, "push_url").strip()
    if "\x00" in raw:
        raise ContractValidationError("push_url contains NUL")

    # An absolute local Git destination is a filesystem byte identity, not a
    # URL.  Quote URL metacharacters before parsing so '?' and '#' in valid
    # filenames cannot alias another destination.
    if raw.startswith("/"):
        # Do not collapse dot segments without consulting the filesystem:
        # ``symlink/../target`` need not resolve like a lexical normalizer says.
        path = os.fsencode(raw)
        return "file://" + quote_from_bytes(path, safe="/")
    if raw.startswith(("./", "../")):
        raise ContractValidationError("file push identity must use an absolute path")

    # Git's scp-like syntax: [user@]host:path.  Its path treats URL query and
    # fragment metacharacters literally, and its SSH username is part of the
    # server-side repository identity rather than a provider password.
    if "://" not in raw and not raw.startswith(("/", "./", "../")):
        match = re.fullmatch(
            r"(?:(?P<user>[^@/:]+)@)?(?P<host>[^/:]+):(?P<path>.+)", raw
        )
        if match:
            username = match.group("user")
            userinfo = ""
            if username is not None:
                quoted_user = quote_from_bytes(
                    os.fsencode(username), safe="!$&'()*+,;=-._~"
                )
                userinfo = f"{quoted_user}@"
            host = match.group("host").casefold()
            scp_path = match.group("path")
            identity_path = scp_path if scp_path.startswith("/") else "/~/" + scp_path
            path = quote_from_bytes(
                os.fsencode(identity_path),
                safe="/:@!$&'()*+,;=-._~",
            )
            return urlunsplit(("ssh", f"{userinfo}{host}", path, "", ""))

    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.casefold()
        if not scheme:
            raise ContractValidationError("file push identity must use an absolute path")
        if scheme == "file":
            if parsed.hostname not in (None, "", "localhost"):
                raise ContractValidationError("file push identity must be local")
            if parsed.query:
                raise ContractValidationError("file push identity cannot contain a query")
            path = unquote_to_bytes(parsed.path)
            if not path.startswith(b"/"):
                raise ContractValidationError("file push identity must use an absolute path")
            return "file://" + quote_from_bytes(path, safe="/")
        host = parsed.hostname
        if not host:
            raise ContractValidationError("push_url must include a host")
        if parsed.query:
            try:
                query = parse_qsl(
                    parsed.query, keep_blank_values=True, strict_parsing=True
                )
            except ValueError as exc:
                raise ContractValidationError("push_url query is malformed") from exc
            for key, _value in query:
                key_parts = {
                    part
                    for part in re.split(r"[^a-z0-9]+", key.casefold())
                    if part
                }
                if not key_parts.intersection(_SECRET_ENV_PARTS):
                    raise ContractValidationError(
                        "push_url query may contain only credential parameters"
                    )
        host = host.casefold()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        if (scheme, port) in {("https", 443), ("http", 80), ("ssh", 22)}:
            port = None
        userinfo = ""
        if "ssh" in scheme and parsed.username is not None:
            if not parsed.username:
                raise ContractValidationError("SSH push identity has an empty username")
            username = quote_from_bytes(
                unquote_to_bytes(parsed.username), safe="!$&'()*+,;=-._~"
            )
            userinfo = f"{username}@"
        netloc = userinfo + host + (f":{port}" if port is not None else "")
        path = parsed.path or "/"
        if not path.startswith("/"):
            path = "/" + path
        return urlunsplit((scheme, netloc, path, "", ""))
    except (TypeError, UnicodeError, ValueError) as exc:
        if isinstance(exc, ContractValidationError):
            raise
        raise ContractValidationError("push_url is malformed") from exc


def _push_fingerprint(identity: str) -> str:
    return hashlib.sha256(
        b"hermes.bestplan.push-identity.v1\0" + identity.encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class Publication:
    repository_id: str
    remote_name: str
    push_url: str
    remote_ref: str
    observed_oid: str

    def __post_init__(self) -> None:
        _strict_string(self.repository_id, "publication repository_id")
        remote = _strict_string(self.remote_name, "publication remote_name")
        if not _IDENTIFIER_RE.fullmatch(remote):
            raise ContractValidationError("publication remote_name is malformed")
        object.__setattr__(self, "push_url", normalize_git_push_identity(self.push_url))
        if self.remote_ref != LOCAL_MAIN_REF:
            raise ContractValidationError("publication remote_ref must be refs/heads/main")
        if not (_SHA1_RE.fullmatch(self.observed_oid) or _SHA256_RE.fullmatch(self.observed_oid)):
            raise ContractValidationError("publication observed_oid is malformed")

    @property
    def remote_identity(self) -> str:
        return self.push_url

    @property
    def remote_identity_fingerprint(self) -> str:
        return _push_fingerprint(self.push_url)


@dataclass(frozen=True)
class BlockingReview:
    lane: str
    command: BoundCommand
    blocking_severities: tuple[str, ...]

    def __post_init__(self) -> None:
        _strict_string(self.lane, "review lane")
        if not isinstance(self.command, BoundCommand):
            raise ContractValidationError("review command gate is required")
        if not isinstance(self.blocking_severities, tuple) or not self.blocking_severities:
            raise ContractValidationError("review blocking severity gate is required")
        if tuple(sorted(set(self.blocking_severities))) != self.blocking_severities:
            raise ContractValidationError("review blocking severities must be sorted and unique")
        if any(not isinstance(item, str) or not item for item in self.blocking_severities):
            raise ContractValidationError("review blocking severities are malformed")


@dataclass(frozen=True)
class RollbackTarget:
    repository_id: str
    selector: str
    service: str
    command: BoundCommand

    def __post_init__(self) -> None:
        _strict_string(self.repository_id, "rollback repository_id")
        selector = _strict_string(self.selector, "rollback selector")
        if not PurePosixPath(selector).is_absolute():
            raise ContractValidationError("rollback selector must be absolute")
        _strict_string(self.service, "rollback service")
        if not isinstance(self.command, BoundCommand):
            raise ContractValidationError("rollback command gate is required")


@dataclass(frozen=True)
class LiveTarget:
    repository_id: str
    adapter: str
    target_id: str
    service: str
    activation: BoundCommand
    health: BoundCommand
    canary: BoundCommand
    rollback: RollbackTarget

    def __post_init__(self) -> None:
        _strict_string(self.repository_id, "live repository_id")
        _strict_string(self.adapter, "live adapter")
        _strict_string(self.target_id, "live target_id")
        _strict_string(self.service, "live service")
        if not isinstance(self.activation, BoundCommand):
            raise ContractValidationError("live activation command gate is required")
        if not isinstance(self.health, BoundCommand):
            raise ContractValidationError("live health gate is required")
        if not isinstance(self.canary, BoundCommand):
            raise ContractValidationError("live canary gate is required")
        if not isinstance(self.rollback, RollbackTarget):
            raise ContractValidationError("live rollback target is required")
        if self.rollback.repository_id != self.repository_id:
            raise ContractValidationError("rollback target crosses repository identity")
        if self.rollback.service != self.service:
            raise ContractValidationError("rollback service differs from live service")


@dataclass(frozen=True)
class ControllerIdentity:
    repository_id: str
    controller_id: str
    release_oid: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        _strict_string(self.repository_id, "controller repository_id")
        _strict_string(self.controller_id, "controller identity")
        if not (_SHA1_RE.fullmatch(self.release_oid) or _SHA256_RE.fullmatch(self.release_oid)):
            raise ContractValidationError("controller release_oid is malformed")
        _sha256(self.artifact_sha256, "controller artifact_sha256")


@dataclass(frozen=True)
class Enrollment:
    reference: str
    enrollment_id: str
    revision: int
    epoch: str
    repository: EnrolledRepository
    source_policy: str
    capture_budget_seconds: int
    local_ref: str
    publication: Publication
    commands: tuple[BoundCommand, ...]
    review: BlockingReview
    live_targets: tuple[LiveTarget, ...]
    controller: ControllerIdentity
    promotion_mode: str

    def __post_init__(self) -> None:
        _strict_string(self.reference, "enrollment reference")
        _strict_string(self.enrollment_id, "enrollment id")
        _strict_int(self.revision, "enrollment revision", minimum=1)
        _strict_string(self.epoch, "enrollment epoch")
        if not isinstance(self.repository, EnrolledRepository):
            raise ContractValidationError("enrollment repository identity is required")
        if self.source_policy != "head_only":
            raise ContractValidationError("enrollment source_policy must be head_only")
        _strict_int(self.capture_budget_seconds, "capture_budget_seconds", minimum=1)
        if self.local_ref != LOCAL_MAIN_REF:
            raise ContractValidationError("enrollment local_ref must be refs/heads/main")
        if not isinstance(self.publication, Publication):
            raise ContractValidationError("enrollment publication gate is required")
        if not isinstance(self.commands, tuple) or not self.commands or any(
            not isinstance(command, BoundCommand) for command in self.commands
        ):
            raise ContractValidationError("enrollment requires at least one bound check command")
        identifiers = [command.identifier for command in self.commands]
        if len(set(identifiers)) != len(identifiers):
            raise ContractValidationError("enrollment check identifiers must be unique")
        if not isinstance(self.review, BlockingReview):
            raise ContractValidationError("enrollment blocking review gate is required")
        if not isinstance(self.live_targets, tuple) or len(self.live_targets) != 1:
            raise ContractValidationError("enrollment requires exactly one live target")
        if not isinstance(self.live_targets[0], LiveTarget):
            raise ContractValidationError("enrollment live target is malformed")
        if not isinstance(self.controller, ControllerIdentity):
            raise ContractValidationError("enrollment controller identity is required")
        if self.promotion_mode not in {"candidate_only", "auto_live"}:
            raise ContractValidationError(
                "enrollment promotion_mode must be candidate_only or auto_live"
            )
        repository_id = self.repository.repository_id
        if self.publication.repository_id != repository_id:
            raise ContractValidationError("publication crosses repository identity")
        _git_oid(
            self.publication.observed_oid,
            self.repository.object_format,
            "publication observed_oid",
        )
        if self.live_targets[0].repository_id != repository_id:
            raise ContractValidationError("live target crosses repository identity")
        if self.controller.repository_id != repository_id:
            raise ContractValidationError("controller crosses repository identity")
        _git_oid(
            self.controller.release_oid,
            self.repository.object_format,
            "controller release_oid",
        )


def _pinned_to_dict(item: PinnedInput) -> dict[str, Any]:
    return {"path": item.path, "sha256": item.sha256}


def _pinned_from_dict(value: Any, name: str) -> PinnedInput:
    value = _exact_mapping(value, {"path", "sha256"}, name)
    return PinnedInput(value["path"], value["sha256"])


def _command_to_dict(command: BoundCommand) -> dict[str, Any]:
    return {
        "identifier": command.identifier,
        "executable": command.executable,
        "executable_sha256": command.executable_sha256,
        "argv": list(command.argv),
        "logical_cwd": command.logical_cwd,
        "env": [{"name": key, "value": value} for key, value in command.env],
        "inputs": [_pinned_to_dict(item) for item in command.inputs],
        "cache": [_pinned_to_dict(item) for item in command.cache],
        "timeout_seconds": command.timeout_seconds,
        "network_allowlist": list(command.network_allowlist),
    }


def _command_from_dict(value: Any, name: str) -> BoundCommand:
    keys = {
        "identifier", "executable", "executable_sha256", "argv", "logical_cwd",
        "env", "inputs", "cache", "timeout_seconds", "network_allowlist",
    }
    value = _exact_mapping(value, keys, name)
    if not isinstance(value["argv"], list):
        raise ContractValidationError(f"{name}.argv must be a list")
    if not isinstance(value["env"], list):
        raise ContractValidationError(f"{name}.env must be a list")
    env: list[tuple[str, str]] = []
    for index, item in enumerate(value["env"]):
        pair = _exact_mapping(item, {"name", "value"}, f"{name}.env[{index}]")
        env.append((pair["name"], pair["value"]))
    for field in ("inputs", "cache", "network_allowlist"):
        if not isinstance(value[field], list):
            raise ContractValidationError(f"{name}.{field} must be a list")
    return BoundCommand(
        identifier=value["identifier"],
        executable=value["executable"],
        executable_sha256=value["executable_sha256"],
        argv=tuple(value["argv"]),
        logical_cwd=value["logical_cwd"],
        env=tuple(env),
        inputs=tuple(
            _pinned_from_dict(item, f"{name}.inputs[{index}]")
            for index, item in enumerate(value["inputs"])
        ),
        cache=tuple(
            _pinned_from_dict(item, f"{name}.cache[{index}]")
            for index, item in enumerate(value["cache"])
        ),
        timeout_seconds=value["timeout_seconds"],
        network_allowlist=tuple(value["network_allowlist"]),
    )


def _repository_to_dict(repository: EnrolledRepository) -> dict[str, Any]:
    return {
        "repository_id": repository.repository_id,
        "workspace": repository.workspace,
        "workspace_raw_b64": _b64(repository.workspace_raw),
        "worktree": repository.worktree,
        "worktree_raw_b64": _b64(repository.worktree_raw),
        "git_dir": repository.git_dir,
        "git_dir_raw_b64": _b64(repository.git_dir_raw),
        "common_dir": repository.common_dir,
        "common_dir_raw_b64": _b64(repository.common_dir_raw),
        "common_dir_device": repository.common_dir_device,
        "common_dir_inode": repository.common_dir_inode,
        "object_format": repository.object_format,
    }


def _repository_from_dict(value: Any, name: str) -> EnrolledRepository:
    keys = {
        "repository_id", "workspace", "workspace_raw_b64", "worktree",
        "worktree_raw_b64", "git_dir", "git_dir_raw_b64", "common_dir",
        "common_dir_raw_b64", "common_dir_device", "common_dir_inode", "object_format",
    }
    value = _exact_mapping(value, keys, name)
    return EnrolledRepository(
        repository_id=value["repository_id"],
        workspace=value["workspace"],
        workspace_raw=_unb64(value["workspace_raw_b64"], f"{name}.workspace_raw_b64"),
        worktree=value["worktree"],
        worktree_raw=_unb64(value["worktree_raw_b64"], f"{name}.worktree_raw_b64"),
        git_dir=value["git_dir"],
        git_dir_raw=_unb64(value["git_dir_raw_b64"], f"{name}.git_dir_raw_b64"),
        common_dir=value["common_dir"],
        common_dir_raw=_unb64(value["common_dir_raw_b64"], f"{name}.common_dir_raw_b64"),
        common_dir_device=value["common_dir_device"],
        common_dir_inode=value["common_dir_inode"],
        object_format=value["object_format"],
    )


def _publication_to_dict(publication: Publication) -> dict[str, Any]:
    return {
        "repository_id": publication.repository_id,
        "remote_name": publication.remote_name,
        "remote_identity": publication.remote_identity,
        "remote_identity_fingerprint": publication.remote_identity_fingerprint,
        "remote_ref": publication.remote_ref,
        "observed_oid": publication.observed_oid,
    }


def _publication_from_dict(value: Any, name: str) -> Publication:
    keys = {
        "repository_id", "remote_name", "remote_identity",
        "remote_identity_fingerprint", "remote_ref", "observed_oid",
    }
    value = _exact_mapping(value, keys, name)
    publication = Publication(
        repository_id=value["repository_id"],
        remote_name=value["remote_name"],
        push_url=value["remote_identity"],
        remote_ref=value["remote_ref"],
        observed_oid=value["observed_oid"],
    )
    if value["remote_identity"] != publication.remote_identity:
        raise ContractValidationError(f"{name}.remote_identity is not normalized")
    if value["remote_identity_fingerprint"] != publication.remote_identity_fingerprint:
        raise ContractValidationError(f"{name}.remote_identity_fingerprint differs")
    return publication


def _review_to_dict(review: BlockingReview) -> dict[str, Any]:
    return {
        "lane": review.lane,
        "command": _command_to_dict(review.command),
        "blocking_severities": list(review.blocking_severities),
    }


def _review_from_dict(value: Any, name: str) -> BlockingReview:
    value = _exact_mapping(value, {"lane", "command", "blocking_severities"}, name)
    if not isinstance(value["blocking_severities"], list):
        raise ContractValidationError(f"{name}.blocking_severities must be a list")
    return BlockingReview(
        lane=value["lane"],
        command=_command_from_dict(value["command"], f"{name}.command"),
        blocking_severities=tuple(value["blocking_severities"]),
    )


def _rollback_to_dict(rollback: RollbackTarget) -> dict[str, Any]:
    return {
        "repository_id": rollback.repository_id,
        "selector": rollback.selector,
        "service": rollback.service,
        "command": _command_to_dict(rollback.command),
    }


def _rollback_from_dict(value: Any, name: str) -> RollbackTarget:
    value = _exact_mapping(value, {"repository_id", "selector", "service", "command"}, name)
    return RollbackTarget(
        repository_id=value["repository_id"],
        selector=value["selector"],
        service=value["service"],
        command=_command_from_dict(value["command"], f"{name}.command"),
    )


def _live_to_dict(live: LiveTarget) -> dict[str, Any]:
    return {
        "repository_id": live.repository_id,
        "adapter": live.adapter,
        "target_id": live.target_id,
        "service": live.service,
        "activation": _command_to_dict(live.activation),
        "health": _command_to_dict(live.health),
        "canary": _command_to_dict(live.canary),
        "rollback": _rollback_to_dict(live.rollback),
    }


def _live_from_dict(value: Any, name: str) -> LiveTarget:
    keys = {
        "repository_id", "adapter", "target_id", "service", "activation",
        "health", "canary", "rollback",
    }
    value = _exact_mapping(value, keys, name)
    return LiveTarget(
        repository_id=value["repository_id"],
        adapter=value["adapter"],
        target_id=value["target_id"],
        service=value["service"],
        activation=_command_from_dict(value["activation"], f"{name}.activation"),
        health=_command_from_dict(value["health"], f"{name}.health"),
        canary=_command_from_dict(value["canary"], f"{name}.canary"),
        rollback=_rollback_from_dict(value["rollback"], f"{name}.rollback"),
    )


def _controller_to_dict(controller: ControllerIdentity) -> dict[str, Any]:
    return {
        "repository_id": controller.repository_id,
        "controller_id": controller.controller_id,
        "release_oid": controller.release_oid,
        "artifact_sha256": controller.artifact_sha256,
    }


def _controller_from_dict(value: Any, name: str) -> ControllerIdentity:
    value = _exact_mapping(
        value, {"repository_id", "controller_id", "release_oid", "artifact_sha256"}, name
    )
    return ControllerIdentity(
        repository_id=value["repository_id"],
        controller_id=value["controller_id"],
        release_oid=value["release_oid"],
        artifact_sha256=value["artifact_sha256"],
    )


def enrollment_to_dict(enrollment: Enrollment) -> dict[str, Any]:
    if not isinstance(enrollment, Enrollment):
        raise ContractValidationError("enrollment must be an Enrollment")
    return {
        "schema": ENROLLMENT_SCHEMA,
        "version": 1,
        "reference": enrollment.reference,
        "enrollment_id": enrollment.enrollment_id,
        "revision": enrollment.revision,
        "epoch": enrollment.epoch,
        "repository": _repository_to_dict(enrollment.repository),
        "source_policy": enrollment.source_policy,
        "capture_budget_seconds": enrollment.capture_budget_seconds,
        "local_ref": enrollment.local_ref,
        "publication": _publication_to_dict(enrollment.publication),
        "commands": [_command_to_dict(command) for command in enrollment.commands],
        "review": _review_to_dict(enrollment.review),
        "live_targets": [_live_to_dict(target) for target in enrollment.live_targets],
        "controller": _controller_to_dict(enrollment.controller),
        "promotion_mode": enrollment.promotion_mode,
    }


def enrollment_from_dict(value: Any) -> Enrollment:
    keys = {
        "schema", "version", "reference", "enrollment_id", "revision", "epoch",
        "repository", "source_policy", "capture_budget_seconds", "local_ref",
        "publication", "commands", "review", "live_targets", "controller",
        "promotion_mode",
    }
    try:
        value = _exact_mapping(value, keys, "enrollment")
        if value["schema"] != ENROLLMENT_SCHEMA or value["version"] != 1 or isinstance(
            value["version"], bool
        ):
            raise ContractValidationError("enrollment schema/version is unsupported")
        if not isinstance(value["commands"], list):
            raise ContractValidationError("enrollment commands must be a list")
        if not isinstance(value["live_targets"], list):
            raise ContractValidationError("enrollment live_targets must be a list")
        enrollment = Enrollment(
            reference=value["reference"],
            enrollment_id=value["enrollment_id"],
            revision=value["revision"],
            epoch=value["epoch"],
            repository=_repository_from_dict(value["repository"], "enrollment.repository"),
            source_policy=value["source_policy"],
            capture_budget_seconds=value["capture_budget_seconds"],
            local_ref=value["local_ref"],
            publication=_publication_from_dict(value["publication"], "enrollment.publication"),
            commands=tuple(
                _command_from_dict(item, f"enrollment.commands[{index}]")
                for index, item in enumerate(value["commands"])
            ),
            review=_review_from_dict(value["review"], "enrollment.review"),
            live_targets=tuple(
                _live_from_dict(item, f"enrollment.live_targets[{index}]")
                for index, item in enumerate(value["live_targets"])
            ),
            controller=_controller_from_dict(value["controller"], "enrollment.controller"),
            promotion_mode=value["promotion_mode"],
        )
        # Re-serialization equality rejects alternate/noncanonical encodings.
        if canonical_json(enrollment_to_dict(enrollment)) != canonical_json(value):
            raise ContractValidationError("enrollment is not canonical")
        return enrollment
    except MalformedEnrollmentError:
        raise
    except ContractValidationError as exc:
        raise MalformedEnrollmentError(str(exc)) from exc


def resolve_matching_enrollment(
    config: Mapping[str, Any] | None,
    repo_identity: RepoIdentity,
    authority_client: BestplanAuthorityClient | None,
) -> Enrollment | None:
    """Resolve exactly one active enrollment; absence/unavailability is V1."""

    if not isinstance(repo_identity, RepoIdentity):
        raise ContractValidationError("repo_identity must be a RepoIdentity")
    if not config or "bestplan_promotion" not in config:
        return None
    promotion = config.get("bestplan_promotion")
    if promotion is None:
        return None
    if not isinstance(promotion, Mapping):
        raise MalformedEnrollmentError("bestplan_promotion must be an object")
    unknown = set(promotion) - {"authority_endpoint", "enrollment_ref"}
    if unknown:
        raise MalformedEnrollmentError(
            f"bestplan_promotion has unknown fields {sorted(unknown)}"
        )
    endpoint = promotion.get("authority_endpoint", "")
    reference = promotion.get("enrollment_ref", "")
    if not isinstance(endpoint, str) or not isinstance(reference, str):
        raise MalformedEnrollmentError(
            "bestplan_promotion authority_endpoint/enrollment_ref must be strings"
        )
    if endpoint:
        try:
            parsed_endpoint = urlsplit(endpoint)
            endpoint_port = parsed_endpoint.port
        except ValueError as exc:
            raise MalformedEnrollmentError("authority_endpoint is malformed") from exc
        if endpoint_port == 0:
            raise MalformedEnrollmentError("authority_endpoint is malformed")
        if (
            parsed_endpoint.username is not None
            or parsed_endpoint.password is not None
            or parsed_endpoint.query
            or parsed_endpoint.fragment
        ):
            raise MalformedEnrollmentError(
                "authority_endpoint cannot contain credentials, query, or fragment"
            )
        if parsed_endpoint.scheme not in {"https", "unix"}:
            raise MalformedEnrollmentError(
                "authority_endpoint must use https or unix"
            )
        if parsed_endpoint.scheme == "https" and not parsed_endpoint.hostname:
            raise MalformedEnrollmentError("https authority_endpoint requires a host")
        if parsed_endpoint.scheme == "unix" and (
            parsed_endpoint.netloc or not parsed_endpoint.path.startswith("/")
        ):
            raise MalformedEnrollmentError(
                "unix authority_endpoint requires an absolute socket path"
            )
    if not endpoint or not reference:
        return None
    if authority_client is None:
        return None
    try:
        result = authority_client.lookup_enrollment(repo_identity)
    except AuthorityUnavailable:
        return None

    if result is None:
        return None
    if not isinstance(result, Enrollment):
        raise MalformedEnrollmentError(
            f"authority enrollment has unsupported type {type(result).__name__}"
        )
    enrollment = result
    if enrollment.reference != reference:
        return None
    return enrollment if enrollment.repository.matches(repo_identity) else None


def _repo_to_source_dict(repo: RepoIdentity) -> dict[str, Any]:
    return _repository_to_dict(EnrolledRepository.from_repo_identity(repo))


def _source_repo_from_dict(value: Any) -> RepoIdentity:
    repo = _repository_from_dict(value, "source_snapshot.repository")
    return RepoIdentity(
        workspace=repo.workspace,
        workspace_raw=repo.workspace_raw,
        worktree=repo.worktree,
        worktree_raw=repo.worktree_raw,
        git_dir=repo.git_dir,
        git_dir_raw=repo.git_dir_raw,
        common_dir=repo.common_dir,
        common_dir_raw=repo.common_dir_raw,
        common_dir_device=repo.common_dir_device,
        common_dir_inode=repo.common_dir_inode,
        object_format=repo.object_format,
        repository_id=repo.repository_id,
    )


def source_snapshot_to_dict(snapshot: SourceSnapshot) -> dict[str, Any]:
    if not isinstance(snapshot, SourceSnapshot):
        raise ContractValidationError("snapshot must be a SourceSnapshot")
    manifest = snapshot.protected_manifest
    return {
        "schema": SOURCE_SNAPSHOT_SCHEMA,
        "version": 1,
        "repository": _repo_to_source_dict(snapshot.repo),
        "head": {
            "symbolic": snapshot.head_symbolic,
            "ref_b64": None if snapshot.head_ref is None else _b64(snapshot.head_ref),
            "raw_b64": _b64(snapshot.head_raw),
            "oid": snapshot.head_oid,
            "tree_oid": snapshot.tree_oid,
        },
        "protected_manifest": {
            "index_entries": [
                {"path_b64": _b64(item.path), "mode": item.mode, "oid": item.oid, "stage": item.stage}
                for item in manifest.index_entries
            ],
            "index_flags": [
                {
                    "path_b64": _b64(item.path),
                    "tag_b64": _b64(item.tag),
                    "fsmonitor_tag_b64": _b64(item.fsmonitor_tag),
                    "assume_unchanged": item.assume_unchanged,
                    "skip_worktree": item.skip_worktree,
                    "fsmonitor_valid": item.fsmonitor_valid,
                    "intent_to_add": item.intent_to_add,
                }
                for item in manifest.index_flags
            ],
            "worktree_entries": [
                {
                    "path_b64": _b64(item.path),
                    "tracked": item.tracked,
                    "kind": item.kind,
                    "mode": item.mode,
                    "size": item.size,
                    "content_sha256": item.content_sha256,
                    "symlink_target_b64": (
                        None if item.symlink_target is None else _b64(item.symlink_target)
                    ),
                    "git_oid": item.git_oid,
                }
                for item in manifest.worktree_entries
            ],
            "protected_paths_b64": [_b64(path) for path in manifest.protected_paths],
            "staged_diff_sha256": manifest.staged_diff_sha256,
            "unstaged_diff_sha256": manifest.unstaged_diff_sha256,
            "digest": manifest.digest,
        },
        "capture_implementation_sha256": snapshot.capture_implementation_sha256,
        "fingerprint": snapshot.fingerprint,
    }


def source_snapshot_from_dict(value: Any) -> SourceSnapshot:
    value = _exact_mapping(
        value,
        {
            "schema", "version", "repository", "head", "protected_manifest",
            "capture_implementation_sha256", "fingerprint",
        },
        "source_snapshot",
    )
    if value["schema"] != SOURCE_SNAPSHOT_SCHEMA or value["version"] != 1 or isinstance(
        value["version"], bool
    ):
        raise ContractValidationError("source_snapshot schema/version is unsupported")
    repo = _source_repo_from_dict(value["repository"])
    head = _exact_mapping(value["head"], {"symbolic", "ref_b64", "raw_b64", "oid", "tree_oid"}, "source_snapshot.head")
    symbolic = _strict_bool(head["symbolic"], "source_snapshot.head.symbolic")
    head_ref = None if head["ref_b64"] is None else _unb64(head["ref_b64"], "source_snapshot.head.ref_b64")
    if symbolic != (head_ref is not None):
        raise ContractValidationError("source_snapshot symbolic/ref identity differs")
    _git_oid(head["oid"], repo.object_format, "source_snapshot.head.oid")
    _git_oid(head["tree_oid"], repo.object_format, "source_snapshot.head.tree_oid")
    protected = _exact_mapping(
        value["protected_manifest"],
        {
            "index_entries", "index_flags", "worktree_entries", "protected_paths_b64",
            "staged_diff_sha256", "unstaged_diff_sha256", "digest",
        },
        "source_snapshot.protected_manifest",
    )
    for field in ("index_entries", "index_flags", "worktree_entries", "protected_paths_b64"):
        if not isinstance(protected[field], list):
            raise ContractValidationError(f"source_snapshot.protected_manifest.{field} must be a list")
    index_entries: list[IndexEntry] = []
    for index, item in enumerate(protected["index_entries"]):
        item = _exact_mapping(item, {"path_b64", "mode", "oid", "stage"}, f"index_entries[{index}]")
        _strict_int(item["mode"], "index mode")
        _strict_int(item["stage"], "index stage")
        _git_oid(item["oid"], repo.object_format, "index oid")
        index_entries.append(IndexEntry(_unb64(item["path_b64"], "index path"), item["mode"], item["oid"], item["stage"]))
    index_flags: list[IndexFlags] = []
    for index, item in enumerate(protected["index_flags"]):
        item = _exact_mapping(
            item,
            {
                "path_b64", "tag_b64", "fsmonitor_tag_b64", "assume_unchanged",
                "skip_worktree", "fsmonitor_valid", "intent_to_add",
            },
            f"index_flags[{index}]",
        )
        index_flags.append(
            IndexFlags(
                _unb64(item["path_b64"], "flag path"),
                _unb64(item["tag_b64"], "flag tag"),
                _unb64(item["fsmonitor_tag_b64"], "flag fsmonitor tag"),
                _strict_bool(item["assume_unchanged"], "assume_unchanged"),
                _strict_bool(item["skip_worktree"], "skip_worktree"),
                _strict_bool(item["fsmonitor_valid"], "fsmonitor_valid"),
                _strict_bool(item["intent_to_add"], "intent_to_add"),
            )
        )
    worktree_entries: list[ProtectedPath] = []
    for index, item in enumerate(protected["worktree_entries"]):
        item = _exact_mapping(
            item,
            {
                "path_b64", "tracked", "kind", "mode", "size", "content_sha256",
                "symlink_target_b64", "git_oid",
            },
            f"worktree_entries[{index}]",
        )
        mode = item["mode"]
        size = item["size"]
        if mode is not None:
            _strict_int(mode, "worktree mode")
        if size is not None:
            _strict_int(size, "worktree size")
        if item["content_sha256"] is not None:
            _sha256(item["content_sha256"], "worktree content_sha256")
        if item["git_oid"] is not None:
            _git_oid(item["git_oid"], repo.object_format, "worktree git_oid")
        target = (
            None
            if item["symlink_target_b64"] is None
            else _unb64(item["symlink_target_b64"], "worktree symlink target")
        )
        worktree_entries.append(
            ProtectedPath(
                path=_unb64(item["path_b64"], "worktree path"),
                tracked=_strict_bool(item["tracked"], "worktree tracked"),
                kind=_strict_string(item["kind"], "worktree kind"),
                mode=mode,
                size=size,
                content_sha256=item["content_sha256"],
                symlink_target=target,
                git_oid=item["git_oid"],
            )
        )
    for digest_name in ("staged_diff_sha256", "unstaged_diff_sha256", "digest"):
        _sha256(protected[digest_name], f"protected_manifest.{digest_name}")
    _sha256(value["capture_implementation_sha256"], "capture_implementation_sha256")
    _sha256(value["fingerprint"], "fingerprint")
    snapshot = SourceSnapshot(
        repo=repo,
        head_symbolic=symbolic,
        head_ref=head_ref,
        head_raw=_unb64(head["raw_b64"], "source_snapshot.head.raw_b64"),
        head_oid=head["oid"],
        tree_oid=head["tree_oid"],
        protected_manifest=ProtectedManifest(
            index_entries=tuple(index_entries),
            index_flags=tuple(index_flags),
            worktree_entries=tuple(worktree_entries),
            protected_paths=tuple(_unb64(item, "protected path") for item in protected["protected_paths_b64"]),
            staged_diff_sha256=protected["staged_diff_sha256"],
            unstaged_diff_sha256=protected["unstaged_diff_sha256"],
            digest=protected["digest"],
        ),
        capture_implementation_sha256=value["capture_implementation_sha256"],
        fingerprint=value["fingerprint"],
    )
    if canonical_json(source_snapshot_to_dict(snapshot)) != canonical_json(value):
        raise ContractValidationError("source_snapshot is not canonical")
    return snapshot


def source_snapshot_json(snapshot: SourceSnapshot) -> str:
    return canonical_json(source_snapshot_to_dict(snapshot))


def source_snapshot_from_json(value: str) -> SourceSnapshot:
    if not isinstance(value, str):
        raise ContractValidationError("source snapshot JSON must be a string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ContractValidationError("source snapshot JSON is malformed") from exc
    snapshot = source_snapshot_from_dict(decoded)
    if source_snapshot_json(snapshot) != value:
        raise ContractValidationError("source snapshot JSON is not canonical")
    return snapshot


def source_snapshot_digest(snapshot: SourceSnapshot) -> str:
    return hashlib.sha256(
        b"hermes.bestplan.source-snapshot.v1\0" + source_snapshot_json(snapshot).encode("utf-8")
    ).hexdigest()


def build_execution_contract(
    plan: Any,
    snapshot: SourceSnapshot,
    enrollment: Enrollment,
    controller: ControllerIdentity | None = None,
) -> dict[str, Any]:
    """Build one canonical V2 contract from immutable trusted inputs."""

    if not isinstance(snapshot, SourceSnapshot):
        raise ContractValidationError("snapshot must be a SourceSnapshot")
    if not isinstance(enrollment, Enrollment):
        raise ContractValidationError("enrollment must be an Enrollment")
    if not enrollment.repository.matches(snapshot.repo):
        raise ContractValidationError("snapshot and enrollment repository identities differ")
    if not snapshot.head_symbolic or snapshot.head_ref != LOCAL_MAIN_REF.encode("ascii"):
        raise ContractValidationError("protocol 2 source must be attached to refs/heads/main")
    selected_controller = enrollment.controller if controller is None else controller
    if selected_controller != enrollment.controller:
        raise ContractValidationError("controller identity differs from enrollment")
    _git_oid(snapshot.head_oid, snapshot.repo.object_format, "snapshot head_oid")
    _git_oid(snapshot.tree_oid, snapshot.repo.object_format, "snapshot tree_oid")

    slices = tuple(getattr(plan, "slices", ()) or ())
    review_only = bool(slices) and all(getattr(item, "kind", None) == "review" for item in slices)
    promotion_mode = "candidate_only" if review_only else enrollment.promotion_mode
    contract = {
        "schema": CONTRACT_SCHEMA,
        "version": PROMOTION_CONTRACT_VERSION,
        "execution_protocol": EXECUTION_PROTOCOL,
        "enrollment": {
            "reference": enrollment.reference,
            "id": enrollment.enrollment_id,
            "revision": enrollment.revision,
            "epoch": enrollment.epoch,
        },
        "repository": _repository_to_dict(enrollment.repository),
        "source": {
            "policy": enrollment.source_policy,
            "base_oid": snapshot.head_oid,
            "tree_oid": snapshot.tree_oid,
            "local_ref": enrollment.local_ref,
            "local_main_oid": snapshot.head_oid,
            "snapshot_digest": source_snapshot_digest(snapshot),
            "source_digest": snapshot.fingerprint,
            "protected_digest": snapshot.protected_manifest.digest,
        },
        "publication": _publication_to_dict(enrollment.publication),
        "commands": [_command_to_dict(command) for command in enrollment.commands],
        "review": _review_to_dict(enrollment.review),
        "live_target": _live_to_dict(enrollment.live_targets[0]),
        "controller": _controller_to_dict(selected_controller),
        "promotion_mode": promotion_mode,
    }
    return validate_execution_contract(contract)


def validate_execution_contract(value: Any) -> dict[str, Any]:
    keys = {
        "schema", "version", "execution_protocol", "enrollment", "repository",
        "source", "publication", "commands", "review", "live_target", "controller",
        "promotion_mode",
    }
    value = _exact_mapping(value, keys, "promotion contract")
    if value["schema"] != CONTRACT_SCHEMA:
        raise ContractValidationError("promotion contract schema is unsupported")
    if value["version"] != 2 or isinstance(value["version"], bool):
        raise ContractValidationError("promotion contract version must be integer 2")
    if value["execution_protocol"] != 2 or isinstance(value["execution_protocol"], bool):
        raise ContractValidationError("execution_protocol must be integer 2")
    enrollment = _exact_mapping(value["enrollment"], {"reference", "id", "revision", "epoch"}, "contract.enrollment")
    _strict_string(enrollment["reference"], "contract enrollment reference")
    _strict_string(enrollment["id"], "contract enrollment id")
    _strict_int(enrollment["revision"], "contract enrollment revision", minimum=1)
    _strict_string(enrollment["epoch"], "contract enrollment epoch")
    repository = _repository_from_dict(value["repository"], "contract.repository")
    source = _exact_mapping(
        value["source"],
        {"policy", "base_oid", "tree_oid", "local_ref", "local_main_oid", "snapshot_digest", "source_digest", "protected_digest"},
        "contract.source",
    )
    if source["policy"] != "head_only":
        raise ContractValidationError("contract source policy must be head_only")
    if source["local_ref"] != LOCAL_MAIN_REF:
        raise ContractValidationError("contract local_ref must be refs/heads/main")
    _git_oid(source["base_oid"], repository.object_format, "contract source base_oid")
    _git_oid(source["tree_oid"], repository.object_format, "contract source tree_oid")
    _git_oid(source["local_main_oid"], repository.object_format, "contract source local_main_oid")
    if source["base_oid"] != source["local_main_oid"]:
        raise ContractValidationError("contract base/local-main object ids differ")
    for name in ("snapshot_digest", "source_digest", "protected_digest"):
        _sha256(source[name], f"contract source {name}")
    publication = _publication_from_dict(value["publication"], "contract.publication")
    _git_oid(
        publication.observed_oid,
        repository.object_format,
        "contract publication observed_oid",
    )
    if not isinstance(value["commands"], list) or not value["commands"]:
        raise ContractValidationError("contract commands require at least one check")
    commands = [
        _command_from_dict(item, f"contract.commands[{index}]")
        for index, item in enumerate(value["commands"])
    ]
    if len({command.identifier for command in commands}) != len(commands):
        raise ContractValidationError("contract command identifiers must be unique")
    review = _review_from_dict(value["review"], "contract.review")
    live = _live_from_dict(value["live_target"], "contract.live_target")
    controller = _controller_from_dict(value["controller"], "contract.controller")
    _git_oid(
        controller.release_oid,
        repository.object_format,
        "contract controller release_oid",
    )
    if value["promotion_mode"] not in {"candidate_only", "auto_live"}:
        raise ContractValidationError("contract promotion_mode is unsupported")
    repository_id = repository.repository_id
    if publication.repository_id != repository_id:
        raise ContractValidationError("contract publication crosses repository identity")
    if live.repository_id != repository_id:
        raise ContractValidationError("contract live target crosses repository identity")
    if controller.repository_id != repository_id:
        raise ContractValidationError("contract controller crosses repository identity")

    normalized = {
        "schema": CONTRACT_SCHEMA,
        "version": 2,
        "execution_protocol": 2,
        "enrollment": dict(enrollment),
        "repository": _repository_to_dict(repository),
        "source": dict(source),
        "publication": _publication_to_dict(publication),
        "commands": [_command_to_dict(command) for command in commands],
        "review": _review_to_dict(review),
        "live_target": _live_to_dict(live),
        "controller": _controller_to_dict(controller),
        "promotion_mode": value["promotion_mode"],
    }
    if canonical_json(normalized) != canonical_json(value):
        raise ContractValidationError("promotion contract is not canonical")
    return normalized


def contract_json(contract: Mapping[str, Any]) -> str:
    return canonical_json(validate_execution_contract(contract))


def contract_digest(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        b"hermes.bestplan.promotion-contract.v2\0" + contract_json(contract).encode("utf-8")
    ).hexdigest()


def approval_digest(manifest: Mapping[str, Any], contract_or_none: Mapping[str, Any] | None) -> str:
    if contract_or_none is None:
        # Preserve the literal V1 byte semantics used by _manifest_digest.
        legacy_json = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(legacy_json.encode("utf-8")).hexdigest()
    manifest_json = canonical_json(manifest)
    return hashlib.sha256(
        b"hermes.bestplan.approval.v2\0"
        + manifest_json.encode("utf-8")
        + b"\0"
        + contract_json(contract_or_none).encode("utf-8")
    ).hexdigest()


def render_execution_contract(
    plan: Any,
    contract_or_none: Mapping[str, Any] | None,
    digest: str,
    workspace: str,
) -> str:
    """Render the exact approval consequences without trusting model prose."""

    _sha256(digest, "approval digest")

    def escaped(value: Any) -> str:
        encoded = json.dumps(str(value), ensure_ascii=True)
        return encoded[1:-1]

    lines = [
        "Authoritative executable manifest (host-rendered):",
        f"- approval digest: {digest}",
        f"- workspace: {escaped(workspace)}",
        f"- mode: {escaped(getattr(plan, 'mode', ''))}",
        f"- risk: {escaped(getattr(plan, 'risk', ''))}",
    ]
    for item in tuple(getattr(plan, "slices", ()) or ()):
        leases = ", ".join(
            escaped(value) for value in (getattr(item, "allowed_paths", ()) or ())
        ) or "none (read-only)"
        artifacts = ", ".join(
            escaped(value)
            for value in (getattr(item, "expected_artifacts", ()) or ())
        ) or "none"
        lines.extend(
            [
                f"- slice {escaped(getattr(item, 'id', ''))}:",
                f"  - kind/capability: {escaped(getattr(item, 'kind', ''))}/{escaped(getattr(item, 'capability', ''))}",
                f"  - write leases: {leases}",
                f"  - expected artifacts: {artifacts}",
            ]
        )
    if contract_or_none is None:
        lines.extend(
            [
                "- execution protocol: 1 (candidate-only)",
                "- consequence: candidate-only work; no local-main, remote, or live mutation",
            ]
        )
        return "\n".join(lines)

    contract = validate_execution_contract(contract_or_none)
    source = contract["source"]
    publication = contract["publication"]
    review = contract["review"]
    live = contract["live_target"]
    rollback = live["rollback"]
    controller = contract["controller"]
    lines.extend(
        [
            "- execution protocol: 2",
            f"- enrollment: {escaped(contract['enrollment']['reference'])} / {escaped(contract['enrollment']['id'])} revision {contract['enrollment']['revision']} epoch {escaped(contract['enrollment']['epoch'])}",
            f"- local source: {escaped(source['local_ref'])} at {source['local_main_oid']}",
            f"- source policy: {source['policy']}",
            f"- source snapshot digest: {source['snapshot_digest']}",
            f"- protected digest: {source['protected_digest']}",
            f"- remote: {escaped(publication['remote_name'])} {escaped(publication['remote_identity'])} {escaped(publication['remote_ref'])} observed {publication['observed_oid']}",
            "- ordered checks: " + ", ".join(escaped(command["identifier"]) for command in contract["commands"]),
            f"- blocking review: lane={escaped(review['lane'])} command={escaped(review['command']['identifier'])} severities={','.join(escaped(item) for item in review['blocking_severities'])}",
            f"- live target: {escaped(live['target_id'])} adapter={escaped(live['adapter'])} service={escaped(live['service'])}",
            f"- activation: {escaped(live['activation']['identifier'])}",
            f"- health: {escaped(live['health']['identifier'])}",
            f"- canary: {escaped(live['canary']['identifier'])}",
            f"- rollback: selector={escaped(rollback['selector'])} service={escaped(rollback['service'])} command={escaped(rollback['command']['identifier'])}",
            f"- controller: {escaped(controller['controller_id'])} release={controller['release_oid']} artifact={controller['artifact_sha256']}",
            f"- promotion mode: {contract['promotion_mode']}",
            "- canonical contract JSON: " + contract_json(contract),
        ]
    )
    if contract["promotion_mode"] == "auto_live":
        lines.append(
            "- consequence: bare go authorizes serialized local-main fast-forward, "
            "non-force publication, live activation and verification, and automatic "
            "deployment-failure rollback for this approval digest"
        )
    else:
        lines.append(
            "- consequence: candidate-only work; no local-main, remote, or live mutation"
        )
    return "\n".join(lines)
