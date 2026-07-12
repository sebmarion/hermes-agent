"""OS-enforced filesystem sandbox for strict BestPlan workers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


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
