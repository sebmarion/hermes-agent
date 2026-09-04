#!/usr/bin/env python3
"""Conservative daily upstream sync for the Hermes fork.

This is deliberately NOT a blind ``git pull``. It applies the binary/full-index
diff from the last accepted upstream anchor to the exact fetched upstream tip
onto a clean worktree based on the fork. It then restores explicitly owned
paths and records the accepted tree as a two-parent merge commit: fork first,
tested upstream tip second. A sync must never silently discard local code or
claim untested upstream ancestry.

State records the owned edge paths, their last applied hashes, the exact
upstream tip, and the tested candidate SHA.

Default mode is preview-only. ``--apply`` requires a clean checkout whose HEAD
still matches the pinned source and advances it with ``git merge --ff-only``.
``--publish`` additionally performs a normal fast-forward push. The push remote
must resolve to sebmarion; origin is fetch-only by policy.
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
# BestPlan plugin moved out of core (canonical copy: sebmarion/hermes-skills);
# no longer snapshotted or required here.
PRESERVED_PREFIXES = ()
OWNED_PATHS = ("cron/scheduler.py", "tests/cron/test_cron_script.py")
OWNED_PATHS += ("scripts/improve_loop_wrapper.py",)
REQUIRED_RUNTIME_PATHS = (
    "scripts/improve_loop_wrapper.py",
)
SNAPSHOT_PATHS = (*EDGE_PREFIXES, *PRESERVED_PREFIXES, *OWNED_PATHS)
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
    return path.startswith(EDGE_PREFIXES + PRESERVED_PREFIXES) or path in OWNED_PATHS


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


def summarize_apply_failure(stdout: str, stderr: str) -> str:
    """Return the actionable reason from verbose ``git apply --3way`` output."""
    lines = [line.strip() for line in f"{stdout}\n{stderr}".splitlines() if line.strip()]
    conflicts = sorted({line[2:].strip() for line in lines if line.startswith("U ")})
    warnings = [line for line in lines if line.lower().startswith("warning:")]
    if conflicts:
        detail = "conflicting paths: " + ", ".join(conflicts)
        if warnings:
            detail += "; " + warnings[-1]
        return detail

    noise_prefixes = ("Applied patch to ", "Falling back to direct application")
    actionable = [line for line in lines if not line.startswith(noise_prefixes)]
    return "\n".join(actionable[-20:])[-2000:] or "git apply failed without diagnostics"


def snapshot_worktree(root: Path) -> dict[str, str]:
    result = {}
    root = Path(root)
    for prefix in (*EDGE_PREFIXES, *PRESERVED_PREFIXES):
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


def full_snapshot(repo: Path, ref: str) -> dict[str, str]:
    """Return tracked path identities, including mode, for a complete tree."""
    result = {}
    raw = _run(repo, "ls-tree", "-r", ref, "--")
    for line in raw.splitlines():
        metadata, path = line.split("\t", 1)
        mode, kind, object_id = metadata.split(" ", 2)
        result[path] = f"{mode} {kind} {object_id}"
    return result


def assert_core_conserved(
    current: dict[str, str],
    upstream: dict[str, str],
    *,
    detect_local_deletions: bool = False,
) -> None:
    """Refuse any preview that would replace or delete unowned local code."""
    paths = set(current)
    if detect_local_deletions:
        paths.update(upstream)
    replaced = sorted(
        path
        for path in paths
        if not is_edge_path(path) and upstream.get(path) != current.get(path)
    )
    if replaced:
        raise ScopeViolation(
            "upstream sync would replace unowned local Hermes paths: "
            + ", ".join(replaced[:8])
            + (f" (and {len(replaced) - 8} more)" if len(replaced) > 8 else "")
        )


def assert_required_runtime_paths(current: dict[str, str]) -> None:
    """Refuse to sync when the local BestPlan runtime has already disappeared."""
    missing = [path for path in REQUIRED_RUNTIME_PATHS if path not in current]
    if missing:
        raise ConservationError(
            "required BestPlan runtime path(s) missing from local checkout: "
            + ", ".join(missing)
        )


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
    """Build a guarded upstream-based preview and return its conservation report."""
    current_tree = full_snapshot(repo, current_ref)
    upstream_tree = full_snapshot(repo, upstream_ref)
    assert_core_conserved(current_tree, upstream_tree)
    current = snapshot(repo, current_ref)
    assert_required_runtime_paths(current)
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
    _run(repo, "merge", "--ff-only", candidate_sha)


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
        url,
        f"{candidate_sha}:main",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[5]))
    ap.add_argument("--upstream", default="origin/main")
    ap.add_argument("--remote", default="sebmarion-fork")
    ap.add_argument("--state-dir", default=str(Path.home() / ".hermes/labs/bestplan-research/state"))
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
        if args.publish and not args.apply:
            raise MergeUpstreamError("--publish requires --apply")
        _run(repo, "fetch", "origin", "main")
        source_head = _run(repo, "rev-parse", "HEAD").strip()
        state = _load_state(state_path)
        anchor = state.get("upstream_sha") if state else None
        if not anchor:
            raise MergeUpstreamError(
                f"upstream sync anchor missing from state: {state_path}"
            )
        assert_required_runtime_paths(full_snapshot(repo, source_head))
        expected_remote_sha = _run(
            repo, "ls-remote", args.remote, "refs/heads/main"
        ).split()[0]
        if not expected_remote_sha:
            raise MergeUpstreamError(f"remote branch not found: {args.remote}/main")

        report = None
        try:
            report = build_delta_candidate(
                repo, anchor, args.upstream, source_head, state_path, preview
            )
        except MergeUpstreamError:
            # Synthetic patch-replay recovery is retired: conflicts halt
            # hard and a human (or an agent session) resolves them by
            # intent in a normal git merge.
            raise
        candidate_sha = report["candidate_sha"]
        if subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_head, candidate_sha],
            cwd=repo,
            capture_output=True,
        ).returncode:
            raise MergeUpstreamError("candidate is not a descendant of current fork HEAD")

        if not report.get("noop"):
            # Real delta: test/apply/publish inside the candidate preview.
            # (No-op syncs skip all of it — the preview worktree was never
            # created because there is nothing to build.)
            if args.test_argv:
                proc = subprocess.run(args.test_argv, cwd=preview, text=True)
                if proc.returncode:
                    raise MergeUpstreamError(
                        f"candidate tests failed with exit {proc.returncode}"
                    )
            if args.apply:
                apply_candidate(repo, candidate_sha, source_head)
            published_sha = None
            if args.publish:
                published_sha = publish_and_verify(
                    repo, args.remote, candidate_sha, expected_remote_sha
                )
        else:
            # No-op: nothing new upstream. The receipt SHA is the unchanged
            # HEAD itself, but only when it truly is what the fork remote
            # serves — otherwise we'd be certifying an unpublished state.
            if candidate_sha != expected_remote_sha:
                raise MergeUpstreamError(
                    "no-op sync but fork remote differs from local HEAD: "
                    f"{expected_remote_sha[:12]} != {candidate_sha[:12]}"
                )
            published_sha = candidate_sha
        next_state = {
            "schema": STATE_SCHEMA,
            "upstream_ref": args.upstream,
            "upstream_sha": report["upstream_sha"],
            "candidate_sha": candidate_sha,
            "owned_paths": report["owned_paths"],
            "edge_manifest": snapshot(repo, candidate_sha),
            "source_head": source_head,
            "published_sha": published_sha,
        }
        if args.apply:
            _write_state(state_path, next_state)
        print(json.dumps({
            "result": "OK",
            "candidate_sha": candidate_sha,
            "applied": args.apply,
            "published": args.publish,
            "published_sha": published_sha,
            "anchor_sha": report["anchor_sha"],
            "upstream_sha": report["upstream_sha"],
            "owned_paths": report["owned_paths"],
        }, indent=2))
        return 0
    except (MergeUpstreamError, OSError, subprocess.SubprocessError) as exc:
        print(f"RESULT: HALT — {exc}", file=sys.stderr)
        return 1
    finally:
        if preview.exists():
            _run(repo, "worktree", "remove", "--force", str(preview), check=False)
        shutil.rmtree(preview_parent, ignore_errors=True)


def assert_candidate_lineage(
    repo: Path,
    *,
    candidate_sha: str,
    expected_tree_sha: str,
    local_sha: str,
    upstream_sha: str,
) -> None:
    """Require the candidate to record the exact tested tree and parents."""
    fields = _run(
        repo, "rev-list", "--parents", "-n", "1", candidate_sha
    ).split()
    actual_parents = fields[1:]
    expected_parents = [local_sha, upstream_sha]
    if actual_parents != expected_parents:
        raise MergeUpstreamError(
            "candidate parents do not match tested refs: "
            f"expected {expected_parents}, got {actual_parents}"
        )
    actual_tree_sha = _run(repo, "rev-parse", f"{candidate_sha}^{{tree}}").strip()
    if actual_tree_sha != expected_tree_sha:
        raise MergeUpstreamError(
            "candidate tree does not match tested tree: "
            f"expected {expected_tree_sha}, got {actual_tree_sha}"
        )


def build_delta_candidate(
    repo: Path,
    anchor: str,
    upstream_ref: str,
    local_ref: str,
    state_path: Path,
    preview: Path,
) -> dict:
    """Build a delta candidate from the previous upstream anchor to the pinned upstream ref.

    The candidate worktree is created from the current fork HEAD (local_ref).
    Only the binary/full-index delta from anchor..upstream_ref is applied with
    three-way semantics. Owned paths are restored from local_ref and asserted
    byte-identical. The accepted tree is committed with local_ref and the exact
    upstream ref as its ordered parents. Any conflict, rejected patch, missing
    path, or lineage mismatch halts.

    Returns a conservation report dict.
    """
    repo = Path(repo)
    preview = Path(preview)

    # Validate anchor exists and is a commit
    anchor_sha = _run(repo, "rev-parse", f"{anchor}^{{commit}}").strip()
    upstream_sha = _run(repo, "rev-parse", f"{upstream_ref}^{{commit}}").strip()
    local_sha = _run(repo, "rev-parse", f"{local_ref}^{{commit}}").strip()

    # No-op sync: already at the pinned upstream tip. Report without building
    # a candidate (git apply --3way rejects an empty patch stream).
    if anchor_sha == upstream_sha:
        lineage = subprocess.run(
            ["git", "merge-base", "--is-ancestor", upstream_sha, local_sha],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if lineage.returncode != 0:
            raise MergeUpstreamError(
                "recorded upstream anchor is not in local history; "
                "refusing an ancestry-free no-op"
            )
        assert_core_conserved(
            full_snapshot(repo, local_sha),
            full_snapshot(repo, anchor_sha),
            detect_local_deletions=True,
        )
        state = _load_state(state_path)
        return {
            "candidate_sha": local_sha,
            "candidate_tree_sha": _run(
                repo, "rev-parse", f"{local_sha}^{{tree}}"
            ).strip(),
            "merge_parents": [],
            "anchor_sha": anchor_sha,
            "upstream_sha": upstream_sha,
            "owned_paths": sorted(
                str(p) for p in (state or {}).get("owned_paths", [])
            ),
            "noop": True,
        }

    # The range delta is applied on top of local_ref, so reject any local core
    # divergence from the accepted anchor before it can be carried silently
    # into the candidate. Only explicitly owned edge paths may differ.
    assert_core_conserved(
        full_snapshot(repo, local_sha),
        full_snapshot(repo, anchor_sha),
        detect_local_deletions=True,
    )

    # Validate anchor is an ancestor of upstream (the delta must be forward)
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", anchor_sha, upstream_sha],
        cwd=repo, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise MergeUpstreamError(
            f"anchor {anchor_sha[:8]} is not an ancestor of upstream {upstream_sha[:8]}"
        )

    # Create isolated worktree from local HEAD
    preview.parent.mkdir(parents=True, exist_ok=True)
    _run(repo, "worktree", "add", "--detach", str(preview), local_sha)

    try:
        # Apply the upstream delta with standard Git three-way semantics.
        # Any conflict or rejected patch is a hard stop; the updater never
        # interprets conflict markers or invents a semantic merge.
        diff_output = _run(
            repo, "diff", "--binary", "--full-index", anchor_sha, upstream_sha, "--"
        )
        apply_proc = subprocess.run(
            ["git", "apply", "--3way", "--index", "-"],
            cwd=preview,
            input=diff_output,
            capture_output=True,
            text=True,
        )
        if apply_proc.returncode:
            detail = summarize_apply_failure(apply_proc.stdout, apply_proc.stderr)
            raise MergeUpstreamError(
                f"upstream delta conflict/rejection: {detail}"
            )

        # Restore every path recorded as fork-owned, plus the required runtime
        # paths. Upstream must never overwrite a locally owned edge file.
        state = _load_state(state_path)
        owned_paths = set(REQUIRED_RUNTIME_PATHS)
        if state:
            owned_paths.update(str(path) for path in state.get("owned_paths", []))
        current_tree = full_snapshot(repo, local_sha)
        for path in sorted(owned_paths):
            rel = Path(path)
            if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                raise ConservationError(f"unsafe owned path: {path!r}")
            if path in current_tree:
                _run(
                    preview,
                    "restore",
                    "--source",
                    local_sha,
                    "--staged",
                    "--worktree",
                    "--",
                    path,
                )
            else:
                _run(
                    preview,
                    "rm",
                    "-r",
                    "-f",
                    "--ignore-unmatch",
                    "--",
                    path,
                )

        # Verify both the index and worktree match local, including deletions and
        # file mode/type. Git performs the restore so an upstream symlink cannot
        # redirect a filesystem write outside the preview worktree.
        for path in sorted(owned_paths):
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", path],
                cwd=preview,
                capture_output=True,
                text=True,
            ).returncode == 0
            if path not in current_tree:
                if tracked or os.path.lexists(preview / path):
                    raise ConservationError(
                        f"owned path {path} should remain deleted"
                    )
                continue
            diff = subprocess.run(
                ["git", "diff", "--quiet", local_sha, "--", path],
                cwd=preview,
                capture_output=True,
                text=True,
            )
            if diff.returncode != 0:
                raise ConservationError(
                    f"owned path {path} was not conserved from local HEAD"
                )

        # Stage the tested tree and record both the fork and exact upstream
        # tip as parents. This preserves upstream ancestry without replaying
        # its individual commits onto the fork.
        _run(preview, "add", "-A")
        tree_sha = _run(preview, "write-tree").strip()
        message = (
            f"chore(update): merge upstream delta "
            f"{anchor_sha[:8]}..{upstream_sha[:8]}"
        )
        commit_proc = subprocess.run(
            [
                "git",
                "commit-tree",
                tree_sha,
                "-p",
                local_sha,
                "-p",
                upstream_sha,
            ],
            cwd=preview,
            input=message + "\n",
            capture_output=True,
            text=True,
        )
        if commit_proc.returncode:
            raise MergeUpstreamError(
                commit_proc.stderr.strip() or "git commit-tree failed"
            )
        candidate_sha = commit_proc.stdout.strip()
        assert_candidate_lineage(
            preview,
            candidate_sha=candidate_sha,
            expected_tree_sha=tree_sha,
            local_sha=local_sha,
            upstream_sha=upstream_sha,
        )
        _run(preview, "reset", "--hard", candidate_sha)

        return {
            "anchor_sha": anchor_sha,
            "upstream_sha": upstream_sha,
            "local_sha": local_sha,
            "candidate_sha": candidate_sha,
            "candidate_tree_sha": tree_sha,
            "merge_parents": [local_sha, upstream_sha],
            "owned_paths": sorted(owned_paths),
        }
    except BaseException:
        _run(repo, "worktree", "remove", "--force", str(preview), check=False)
        raise


def assert_remote_sha_unchanged(repo: Path, remote: str, expected_sha: str) -> None:
    """Assert the remote SHA has not changed since the preview was built."""
    remote_sha = _run(repo, "ls-remote", remote, "refs/heads/main").split()[0]
    if remote_sha != expected_sha:
        raise MergeUpstreamError(
            f"remote SHA changed between preview and publish: "
            f"expected {expected_sha[:8]}, got {remote_sha[:8]}"
        )


def publish_and_verify(
    repo: Path,
    remote: str,
    candidate_sha: str,
    expected_remote_sha: str,
) -> str:
    """Publish the candidate and verify the remote SHA matches.

    Returns the verified remote SHA.
    """
    url = _ssh_push_url(repo, remote)
    assert_fork_remote(url)

    # Check remote hasn't moved
    current_remote_sha = _run(repo, "ls-remote", remote, "refs/heads/main").split()[0]
    if current_remote_sha != expected_remote_sha:
        raise MergeUpstreamError(
            f"remote SHA changed before publish: "
            f"expected {expected_remote_sha[:8]}, got {current_remote_sha[:8]}"
        )

    _run(
        repo,
        "push",
        url,
        f"{candidate_sha}:main",
    )

    # Verify remote SHA matches candidate
    published_sha = _run(repo, "ls-remote", remote, "refs/heads/main").split()[0]
    if published_sha != candidate_sha:
        raise MergeUpstreamError(
            f"remote SHA readback mismatch: "
            f"expected {candidate_sha[:8]}, got {published_sha[:8]}"
        )

    return published_sha


if __name__ == "__main__":
    sys.exit(main())
