#!/usr/bin/env python3
"""Conservative daily upstream sync for the Hermes fork.

This is deliberately NOT a blind ``git pull``. It builds a clean preview from
``origin/main`` and overlays only the edge paths owned by this system:
``optional-skills/``, ``tests/skills/``, and the explicitly owned scheduler
paths ``cron/scheduler.py`` and ``tests/cron/test_cron_script.py``. Other core
Hermes code is never copied from the local fork into the preview; therefore the
resulting core is exactly upstream's core, even when the fork histories have no
merge-base.

State records the owned edge paths and their last applied hashes. On bootstrap,
paths that are absent from upstream or differ from upstream are conservatively
classified as local-owned. On later runs, edits to any tracked edge path are
added to the owned set; upstream-only edge changes win for paths not owned.

Default mode is preview-only. ``--apply`` requires a clean live checkout and
moves it to the tested preview commit. ``--publish`` additionally updates the
fork remote using ``--force-with-lease`` when histories are unrelated. The
push remote must resolve to sebmarion; origin is fetch-only by policy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

EDGE_PREFIXES = ("optional-skills/", "tests/skills/")
OWNED_PATHS = ("cron/scheduler.py", "tests/cron/test_cron_script.py")
SNAPSHOT_PATHS = (*EDGE_PREFIXES, *OWNED_PATHS)
STATE_SCHEMA = 1


class MergeUpstreamError(RuntimeError):
    """Base class for fail-closed updater errors."""


class ScopeViolation(MergeUpstreamError):
    """A local change targets outside the permitted edge paths."""


class ConservationError(MergeUpstreamError):
    """An owned edge path was not preserved in the preview."""


class RemoteGuardError(MergeUpstreamError):
    """The requested push remote is not Seb's fork."""


def is_edge_path(path: str) -> bool:
    """Return whether a repo-relative path is within the updater allowlist."""
    if not isinstance(path, str) or path.startswith("/") or "\\" in path:
        return False
    parts = Path(path).parts
    if ".." in parts:
        return False
    return path.startswith(EDGE_PREFIXES) or path in OWNED_PATHS


def assert_edge_only(paths) -> None:
    for path in paths:
        if not is_edge_path(path):
            raise ScopeViolation(f"local divergence outside edge allowlist: {path}")


def parse_name_status(raw: str) -> list[tuple[str, str]]:
    """Parse ``git diff --name-status`` output for ordinary file operations."""
    result = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0].strip()
        if status.startswith(("R", "C")) and len(fields) >= 3:
            # A rename/copy is represented as delete old + add new so the
            # overlay cannot leave a stale source file behind.
            result.extend([("D", fields[1]), ("A", fields[2])])
        elif len(fields) >= 2 and status[0] in "AMD":
            result.append((status[0], fields[1]))
        else:
            raise MergeUpstreamError(f"unsupported git diff status line: {line!r}")
    return result


def overlay_operations(statuses) -> list[tuple[str, str]]:
    assert_edge_only(path for _, path in statuses)
    return [("delete" if status == "D" else "copy", path) for status, path in statuses]


def assert_conserved(*, expected: dict[str, str | None], actual: dict[str, str]) -> None:
    """Require every owned path's expected hash/presence to survive."""
    for path, digest in expected.items():
        if digest is None:
            if path in actual:
                raise ConservationError(f"owned edge deletion was not conserved: {path}")
            continue
        if actual.get(path) != digest:
            raise ConservationError(
                f"owned edge path was not conserved: {path} "
                f"(expected {digest}, got {actual.get(path)})"
            )


def assert_fork_remote(url: str) -> None:
    """Require an exact GitHub host/path for the protected fork remote."""
    raw = (url or "").strip()
    try:
        if "://" not in raw and ":" in raw:
            # SCP-style Git URL, e.g. git@github.com:sebmarion/hermes-agent.git.
            host_part, path = raw.split(":", 1)
            host = host_part.rsplit("@", 1)[-1]
            query = fragment = ""
        else:
            parsed = urlsplit(raw)
            if parsed.scheme.lower() not in {"https", "ssh"}:
                raise ValueError("unsupported remote scheme")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("credentials or URL suffix are not allowed")
            host = parsed.hostname or ""
            path = parsed.path
            query = parsed.query
            fragment = parsed.fragment
    except (ValueError, IndexError):
        host = ""
        path = ""
        query = fragment = ""

    identity = path.strip("/").removesuffix(".git").lower()
    if host.lower() != "github.com" or query or fragment or identity != "sebmarion/hermes-agent":
        raise RemoteGuardError(
            f"refusing push remote outside sebmarion/hermes-agent: {url!r}"
        )


def _run(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise MergeUpstreamError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def changed_name_status(repo: Path, old_ref: str, new_ref: str) -> list[tuple[str, str]]:
    return parse_name_status(_run(repo, "diff", "--name-status", old_ref, new_ref, "--"))


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _blob(repo: Path, ref: str, path: str) -> bytes | None:
    proc = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout


def snapshot_worktree(root: Path) -> dict[str, str]:
    result = {}
    root = Path(root)
    for prefix in EDGE_PREFIXES:
        directory = root / prefix
        if not directory.is_dir():
            continue
        for file_path in directory.rglob("*"):
            if file_path.is_file() and ".git" not in file_path.parts:
                result[file_path.relative_to(root).as_posix()] = _sha(file_path.read_bytes())
    for path in OWNED_PATHS:
        file_path = root / path
        if file_path.is_file():
            result[path] = _sha(file_path.read_bytes())
    return result


def snapshot(repo: Path, ref: str | None = None) -> dict[str, str]:
    """Hash tracked edge files at a ref, or files in a worktree directory."""
    if ref is not None and Path(ref).is_dir():
        return snapshot_worktree(Path(ref))
    if ref is None:
        raw = _run(repo, "ls-files", "--", *SNAPSHOT_PATHS)
        paths = [p for p in raw.splitlines() if p]
        result = {}
        for path in paths:
            file_path = repo / path
            if file_path.is_file():
                result[path] = _sha(file_path.read_bytes())
        return result
    raw = _run(repo, "ls-tree", "-r", "--name-only", ref, "--", *SNAPSHOT_PATHS)
    result = {}
    for path in raw.splitlines():
        data = _blob(repo, ref, path)
        if data is not None:
            result[path] = _sha(data)
    return result


def _load_state(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MergeUpstreamError(f"corrupt updater state: {path}") from exc
    if data.get("schema") != STATE_SCHEMA:
        raise MergeUpstreamError(f"unsupported updater state schema: {data.get('schema')!r}")
    return data


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def discover_owned_paths(current: dict[str, str], upstream: dict[str, str], state: dict | None) -> set[str]:
    if state is None:
        # Safe bootstrap: preserve every local edge difference and every local
        # edge addition. The next run becomes precise via the state manifest.
        return {path for path, digest in current.items() if upstream.get(path) != digest}
    owned = set(state.get("owned_paths", []))
    previous = state.get("edge_manifest", {})
    for path in set(current) | set(previous):
        if current.get(path) != previous.get(path):
            owned.add(path)
    return owned


def _write_overlay(repo: Path, preview: Path, current_ref: str, path: str) -> None:
    data = _blob(repo, current_ref, path)
    destination = preview / path
    if data is None:
        if destination.exists():
            destination.unlink()
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, destination)


def _clean_checkout(repo: Path, expected_head: str | None = None) -> None:
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo, text=True
    ).strip()
    if status:
        raise MergeUpstreamError("live checkout is dirty; refusing autonomous upstream apply")
    if expected_head is not None:
        actual_head = _run(repo, "rev-parse", "HEAD").strip()
        if actual_head != expected_head:
            raise MergeUpstreamError(
                "live checkout changed after preview; refusing autonomous upstream apply"
            )


def build_preview(repo: Path, upstream_ref: str, current_ref: str, state_path: Path, preview: Path) -> dict:
    """Build an upstream-based preview and return its conservation report."""
    current = snapshot(repo, current_ref)
    upstream = snapshot(repo, upstream_ref)
    state = _load_state(state_path)
    owned = discover_owned_paths(current, upstream, state)
    assert_edge_only(owned)

    _run(repo, "worktree", "add", "--detach", str(preview), upstream_ref)
    try:
        for path in sorted(owned):
            _write_overlay(repo, preview, current_ref, path)
        actual = snapshot_worktree(preview)
        expected = {path: current.get(path) for path in owned}
        assert_conserved(expected=expected, actual=actual)
        # The preview began at upstream and only edge paths were overlaid.
        unexpected = [path for path in actual if path not in upstream and not is_edge_path(path)]
        if unexpected:
            raise ScopeViolation(f"preview contains unexpected core additions: {unexpected[:3]}")
        return {"owned_paths": sorted(owned), "current": current, "upstream": upstream, "preview": actual}
    except BaseException:
        _run(repo, "worktree", "remove", "--force", str(preview), check=False)
        raise


def commit_preview(preview: Path, message: str) -> str:
    _run(preview, "add", "-A")
    _run(preview, "commit", "--allow-empty", "-m", message)
    return _run(preview, "rev-parse", "HEAD").strip()


def apply_candidate(repo: Path, candidate_sha: str, expected_head: str | None = None) -> None:
    _clean_checkout(repo, expected_head)
    _run(repo, "reset", "--hard", candidate_sha)


def _ssh_push_url(repo: Path, remote: str) -> str:
    push_url = _run(repo, "remote", "get-url", "--push", remote).strip()
    if push_url.startswith("disabled://") or not push_url:
        push_url = _run(repo, "remote", "get-url", remote).strip()
    if push_url.startswith("https://github.com/"):
        push_url = "git@github.com:" + push_url.removeprefix("https://github.com/")
    return push_url


def publish_candidate(repo: Path, remote: str, candidate_sha: str, expected_remote_sha: str) -> None:
    url = _ssh_push_url(repo, remote)
    assert_fork_remote(url)
    _run(
        repo,
        "push",
        f"--force-with-lease=main:{expected_remote_sha}",
        url,
        f"{candidate_sha}:main",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[5]))
    ap.add_argument("--upstream", default="origin/main")
    ap.add_argument("--remote", default="sebmarion-fork")
    ap.add_argument("--state-dir", default=str(Path.home() / ".hermes/labs/bestplan-research/state"))
    ap.add_argument("--base-ref", default=None, help="Only needed to bootstrap legacy state; kept for CLI compatibility")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--test", action="append", dest="test_argv", default=[])
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    state_dir = Path(args.state_dir).resolve()
    state_path = state_dir / "upstream-sync.json"
    preview_parent = Path(tempfile.mkdtemp(prefix="hermes-upstream-preview-parent-"))
    preview = preview_parent / "preview"
    try:
        _run(repo, "fetch", "origin", "main")
        if args.publish and not args.apply:
            raise MergeUpstreamError("--publish requires --apply")
        source_head = _run(repo, "rev-parse", "HEAD").strip()
        report = build_preview(repo, args.upstream, source_head, state_path, preview)
        if args.test_argv:
            proc = subprocess.run(args.test_argv, cwd=preview, text=True)
            if proc.returncode:
                raise MergeUpstreamError(f"preview tests failed with exit {proc.returncode}")
        sha = commit_preview(preview, "chore(update): sync upstream while preserving edge changes")
        if args.apply:
            apply_candidate(repo, sha, source_head)
        if args.publish:
            remote_sha = _run(repo, "ls-remote", args.remote, "refs/heads/main").split()[0]
            publish_candidate(repo, args.remote, sha, remote_sha)
        state = {
            "schema": STATE_SCHEMA,
            "upstream_ref": args.upstream,
            "upstream_sha": _run(repo, "rev-parse", args.upstream).strip(),
            "candidate_sha": sha,
            "owned_paths": report["owned_paths"],
            "edge_manifest": report["preview"],
        }
        if args.apply:
            _write_state(state_path, state)
        print(json.dumps({"result": "OK", "candidate_sha": sha, "applied": args.apply, "published": args.publish, "owned_paths": report["owned_paths"]}, indent=2))
        return 0
    except (MergeUpstreamError, OSError, subprocess.SubprocessError) as exc:
        print(f"RESULT: HALT — {exc}", file=sys.stderr)
        return 1
    finally:
        if preview.exists():
            _run(repo, "worktree", "remove", "--force", str(preview), check=False)
        shutil.rmtree(preview_parent, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
