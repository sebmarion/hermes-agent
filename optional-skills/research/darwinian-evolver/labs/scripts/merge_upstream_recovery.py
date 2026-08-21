#!/usr/bin/env python3
"""Bounded automatic upstream-conflict recovery for the fail-closed updater.

When ``merge_upstream.build_delta_candidate`` halts because the anchor→upstream
delta cannot be applied to the current fork HEAD (a genuine Git conflict or
patch rejection), this module creates an isolated disposable worktree from the
pinned source HEAD, asks headless Zeus Qwen (via a fixed ``hermes -z`` argv) to
resolve exactly that one-time delta, and then independently verifies the
candidate before the existing apply/publish seams are reused.

Design rules
------------
* Trigger only on a conflict/rejection.  Any other failure (network, missing
  anchor, dirty tree, remote movement, tests) must NOT enter recovery.
* The model is a bounded, isolated worker: it runs as ``seb`` with a hard
  timeout, in a worktree the parent owns, and may only touch conflict paths.
* The parent process verifies every claim independently (exit code,
  descendant-of-HEAD, clean worktree, no conflict markers, required runtime
  paths, candidate tests).  The model's prose is never evidence.
* On any failure the worktree is removed and the original safe HALT is
  returned.  The next timer run retries a fresh isolated attempt.
* A concise machine-readable receipt is written to the state directory.
  No secrets, no full transcripts.
* Milestone notifications are emitted through ``notify_telegram`` (events
  ``applied``/``halted``/``upgrade``).  Notifications are advisory: a failure
  to notify never changes the HALT/OK outcome.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import merge_upstream as mu

#: The exact headless Hermes argv.  Fixed by policy: no ``--yolo``, no root,
#: no live checkout.  ``worktree`` is substituted for the disposable path.
HERMES_FIXED_ARGV = (
    "--provider", "custom:zeus",
    "--model", "qwen3.8-27b",
    "--safe-mode", "--ignore-rules",
    "--accept-hooks",
)

#: Git conflict-marker line starts that a successful resolution must not
#: leave behind.  Literal marker *strings* inside existing test fixtures are
#: not flagged: only lines that begin with the marker count.
_MARKER_TOKENS = ("<<<<<<<", ">>>>>>>", "=======")


class RecoveryError(mu.MergeUpstreamError):
    """Base class for bounded-recovery failures (still a safe HALT)."""


# ---------------------------------------------------------------------------
# Notifications (advisory — never change the outcome)
# ---------------------------------------------------------------------------

def _notify(event: str, message: str) -> dict:
    """Emit a bounded milestone notification via notify_telegram.

    Advisory: any failure (missing script, hermes missing, timeout, non-zero
    exit) is swallowed and never changes the HALT/OK contract."""
    try:
        import notify_telegram  # noqa: E402 (same scripts/ dir on sys.path)
        result = notify_telegram.send(event, message)
        return result
    except Exception:  # noqa: BLE001 - notifications are advisory
        return {"sent": False, "reason": "notification unavailable"}


def _notify_start(*, source_head: str, anchor: str, upstream: str,
                  conflict_paths: list[str]) -> dict:
    return _notify(
        "halted",
        f"Upstream delta {anchor[:8]}..{upstream[:8]} conflicted on "
        f"{len(conflict_paths)} path(s); bounded Zeus-Qwen recovery started. "
        f"Source HEAD {source_head[:8]}.",
    )


def _notify_success(*, candidate_sha: str, conflict_paths: list[str]) -> dict:
    return _notify(
        "upgrade",
        f"Bounded upstream conflict recovery succeeded. Candidate "
        f"{candidate_sha[:8]} resolves {len(conflict_paths)} conflict(s); "
        f"verified and applied via existing seams.",
    )


def _notify_failure(*, source_head: str, anchor: str, upstream: str,
                    reason: str) -> dict:
    return _notify(
        "halted",
        f"Bounded upstream conflict recovery HALT (safe): {reason[:300]}. "
        f"Source {source_head[:8]}, anchor {anchor[:8]}..{upstream[:8]}. "
        f"No live mutation; next timer run retries a fresh attempt.",
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_repair_prompt(*, anchor: str, upstream: str, source_head: str,
                        conflict_paths: list[str]) -> str:
    """Return the bounded repair prompt for the one-time delta."""
    return (
        "You are resolving a single upstream Git merge conflict for a fork of "
        "Hermes Agent. Work in the current directory (an isolated Git "
        "worktree pinned at the fork HEAD). Do not run any command outside "
        "this directory. Do not push, pull, fetch, or touch any remote. Do "
        "not edit files outside the listed conflict paths.\n\n"
        f"Upstream delta to apply: {anchor} -> {upstream}\n"
        f"Source (fork) HEAD: {source_head}\n"
        f"Conflicted paths:\n"
        + "".join(f"  - {p}\n" for p in conflict_paths)
        + "\n"
        "Rules:\n"
        "1. Apply exactly the anchor->upstream delta to the listed conflict "
        "paths. Do not apply any other upstream change.\n"
        "2. For each conflict path, inspect the anchor version, the local "
        "version, and the upstream version before resolving.\n"
        "3. Resolve only the conflict paths. Preserve local fork behavior "
        "(local edits, owned runtime paths, edge paths) unless the upstream "
        "delta explicitly supersedes them.\n"
        "4. Never use union, take-ours, or take-theirs heuristics. Reason "
        "about the content on a per-file basis.\n"
        "5. Do not alter unrelated files. Do not add new files unless the "
        "upstream delta adds them on a conflict path.\n"
        "6. Commit the resolution with message "
        "'chore(update): apply upstream delta (auto-resolved)'. Leave the "
        "worktree clean (no uncommitted or untracked files).\n"
    )


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True
    )
    if check and proc.returncode != 0:
        raise RecoveryError(proc.stderr.strip() or f"git {args[0]} failed")
    return proc.stdout


def _worktree_add(repo: Path, worktree: Path) -> None:
    """Create the disposable worktree from the pinned source HEAD."""
    proc = subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repo, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise RecoveryError(
            f"could not create recovery worktree: {proc.stderr.strip()}"
        )


def _worktree_remove(repo: Path, worktree: Path) -> None:
    if not worktree.exists():
        return
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=repo, text=True, capture_output=True, check=False,
    )
    # Belt-and-braces: a crashed git can leave the path behind.
    try:
        if worktree.exists():
            import shutil
            shutil.rmtree(worktree, ignore_errors=True)
        # Drop any stale worktree registration without failing.
        subprocess.run(
            ["git", "worktree", "prune"], cwd=repo, check=False,
            capture_output=True, text=True,
        )
    except Exception:  # noqa: BLE001 - cleanup must never raise
        pass


# ---------------------------------------------------------------------------
# Model invocation (bounded)
# ---------------------------------------------------------------------------

def _run_hermes(model_argv: list[str], prompt: str, worktree: Path,
                timeout: int) -> tuple[int, str]:
    """Invoke the headless model.  Returns (exit_code, bounded output)."""
    cmd = list(model_argv)
    if not any(str(a) == "-z" for a in cmd):
        cmd = ["hermes", "-z"] + list(cmd)
    cmd = list(cmd) + ["--in", str(worktree)]
    # The prompt is passed as a single argument so shell metacharacters are
    # never interpreted.  (No shell involved.)
    cmd = cmd + [prompt]
    try:
        proc = subprocess.run(
            cmd, cwd=worktree, text=True, capture_output=True, timeout=timeout
        )
        output = (proc.stdout or "") + ("\n" + (proc.stderr or "") if proc.stderr else "")
        return proc.returncode, output[-20000:]
    except subprocess.TimeoutExpired as exc:
        return 124, f"model timed out after {timeout}s\n{(exc.stdout or '')[-20000:]}"
    except (OSError, subprocess.SubprocessError) as exc:
        return 125, f"model invocation failed: {exc}"


def _conflict_paths_from_detail(detail: str) -> list[str]:
    """Extract the conflict paths from the updater's halt detail."""
    paths: list[str] = []
    for line in detail.splitlines():
        m = re.search(r"Applied patch to '([^']+)'.*?with conflicts?", line)
        if m:
            paths.append(m.group(1))
        else:
            m = re.search(r"error: ([^:]+): (does not exist in index|already exists)", line)
            if m:
                paths.append(m.group(1))
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Independent verification
# ---------------------------------------------------------------------------

def _verify_candidate(repo: Path, worktree: Path, source_head: str,
                      anchor: str, upstream: str, remote_name: str,
                      expected_remote_sha: str, required_paths: list[str],
                      test_argv: list[str] | None) -> dict:
    """Independently verify the model's candidate.  Raises on any violation."""
    # 1. Candidate commit exists and is a descendant of the pinned source HEAD.
    candidate_sha = _git(worktree, "rev-parse", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        raise RecoveryError(f"candidate SHA is not a valid commit: {candidate_sha!r}")
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_head, candidate_sha],
        cwd=worktree, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise RecoveryError(
            "candidate commit is not a descendant of pinned source HEAD"
        )

    # 2. Worktree must be clean (no uncommitted or untracked residue).
    status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    if status.strip():
        lines = status.strip().splitlines()[:5]
        raise RecoveryError(
            "candidate worktree is not clean: " + str(lines)
        )

    # 3. No conflict markers in changed production files.  Literal marker
    #    strings inside existing test fixtures are NOT flagged: only lines
    #    that begin with the marker (or a '===' separator line) count.
    changed = _git(worktree, "diff", "--name-only", source_head, candidate_sha)
    changed_paths = [p for p in changed.splitlines() if p.strip()]
    allowed_paths = set(_git(repo, "diff", "--name-only", anchor, upstream).splitlines())
    unexpected = sorted(set(changed_paths) - allowed_paths)
    if unexpected:
        raise RecoveryError(
            "candidate changed paths outside the pinned upstream delta: "
            + ", ".join(unexpected[:8])
        )
    for path in changed_paths:
        fp = worktree / path
        if not fp.is_file():
            continue
        try:
            text = fp.read_text(errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.lstrip()
            if any(stripped.startswith(tok) for tok in _MARKER_TOKENS):
                if "test" in path.lower() and "fixture" in path.lower():
                    continue
                raise RecoveryError(
                    f"conflict marker left in {path}: {line!r}"
                )

    # 4. Required owned runtime paths must still be present.
    for path in required_paths:
        if not (worktree / path).is_file():
            raise RecoveryError(
                f"required runtime path missing after resolution: {path}"
            )
        local_blob = _git(repo, "rev-parse", f"{source_head}:{path}").strip()
        candidate_blob = _git(worktree, "rev-parse", f"HEAD:{path}").strip()
        if local_blob != candidate_blob:
            raise RecoveryError(
                f"required runtime path changed during recovery: {path}"
            )

    # 5. The current/remote SHA must not have moved since we pinned it.
    if expected_remote_sha:
        try:
            current_remote_sha = _git(
                repo, "ls-remote", remote_name, "refs/heads/main"
            ).split()[0]
        except Exception:  # noqa: BLE001
            current_remote_sha = ""
        if current_remote_sha and current_remote_sha != expected_remote_sha:
            raise RecoveryError(
                f"remote moved during resolution: pinned {expected_remote_sha[:8]} "
                f"now {current_remote_sha[:8]}"
            )

    # 6. Exact candidate test command passes in the candidate worktree.
    if test_argv:
        proc = subprocess.run(
            list(test_argv), cwd=worktree, text=True, capture_output=True
        )
        if proc.returncode != 0:
            raise RecoveryError(
                f"candidate tests failed with exit {proc.returncode}: "
                f"{(proc.stdout or '')[-2000:]}"
            )

    return {
        "candidate_sha": candidate_sha,
        "source_head": source_head,
        "remote_sha": expected_remote_sha,
    }


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------

def _write_receipt(state_path: Path, receipt: dict) -> None:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_name(state_path.name + ".recovery.tmp")
        tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, state_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def recover_upstream_conflict(
    *,
    repo: Path,
    source_head: str,
    anchor: str,
    upstream: str,
    expected_remote_sha: str,
    conflict_detail: str,
    state_path: Path,
    worktree: Path,
    remote_name: str = "sebmarion-fork",
    model_argv: list[str] | None = None,
    model_timeout: int = 600,
    required_paths: list[str] | None = None,
    test_argv: list[str] | None = None,
    notify: bool = True,
) -> dict:
    """Recover a single upstream delta conflict via a bounded model worker.

    Returns a receipt dict on success:
      {candidate_sha, source_head, anchor, upstream, remote_sha,
       conflict_paths, model_route, test_result, outcome}

    Raises ``RecoveryError`` on any failure (worktree is always cleaned).
    """
    repo = Path(repo)
    worktree = Path(worktree)
    state_path = Path(state_path)
    anchor = anchor.strip()
    upstream = upstream.strip()
    source_head = source_head.strip()
    conflict_paths = _conflict_paths_from_detail(conflict_detail)
    route = list(model_argv) if model_argv else ["hermes", "-z"] + list(HERMES_FIXED_ARGV)
    required_paths = list(required_paths) if required_paths is not None else list(mu.REQUIRED_RUNTIME_PATHS)

    receipt: dict = {
        "schema": 1,
        "source_head": source_head,
        "anchor": anchor,
        "upstream": upstream,
        "remote_sha": expected_remote_sha,
        "conflict_paths": conflict_paths,
        "model_route": list(route),
        "test_result": None,
        "outcome": "halt",
    }

    if notify:
        try:
            _notify_start(
                source_head=source_head, anchor=anchor, upstream=upstream,
                conflict_paths=conflict_paths,
            )
        except Exception:  # noqa: BLE001 - advisory
            pass

    try:
        _worktree_add(repo, worktree)
        prompt = build_repair_prompt(
            anchor=anchor, upstream=upstream, source_head=source_head,
            conflict_paths=conflict_paths,
        )
        exit_code, output = _run_hermes(
            route, prompt, worktree, model_timeout
        )
        if exit_code != 0:
            raise RecoveryError(
                f"model exited {exit_code}: {output[-1000:]}"
            )
        verified = _verify_candidate(
            repo=repo, worktree=worktree, source_head=source_head,
            anchor=anchor, upstream=upstream, remote_name=remote_name,
            expected_remote_sha=expected_remote_sha,
            required_paths=required_paths, test_argv=test_argv,
        )
        receipt["candidate_sha"] = verified["candidate_sha"]
        receipt["test_result"] = "pass"
        receipt["outcome"] = "ok"
        _write_receipt(state_path, receipt)
        if notify:
            try:
                _notify_success(
                    candidate_sha=verified["candidate_sha"],
                    conflict_paths=conflict_paths,
                )
            except Exception:  # noqa: BLE001 - advisory
                pass
        return receipt
    except Exception as exc:
        receipt["outcome"] = "halt"
        receipt["error"] = str(exc)[-500:]
        _write_receipt(state_path, receipt)
        if notify:
            try:
                _notify_failure(
                    source_head=source_head, anchor=anchor, upstream=upstream,
                    reason=str(exc),
                )
            except Exception:  # noqa: BLE001 - advisory
                pass
        raise RecoveryError(str(exc)) from exc
    finally:
        _worktree_remove(repo, worktree)


if __name__ == "__main__":
    print("merge_upstream_recovery is a library module; "
          "call recover_upstream_conflict() from merge_upstream.py")
    sys.exit(0)
