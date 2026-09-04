#!/usr/bin/env python3
"""Daily safe upstream-delta sync wrapper.

The live Hermes checkout is deployment state, never a scratch workspace:
- flock serializes daily/manual updates;
- every run creates a separate disposable clone with independent refs/index;
- delta application and tests run only there; semantic conflicts halt for
  intent-based resolution instead of accepting an AI-authored core patch;
- a tested candidate is published by normal fast-forward push;
- canonical main advances only via guarded ``git merge --ff-only``;
- Telegram receives a redacted success or halt rundown.

A dirty or moved canonical checkout is always a halt. No reset, stash, force
checkout, or force push is permitted by this wrapper.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path("/home/seb/projects/hermes-agent")
PYTHON = REPO / ".venv" / "bin" / "python"
UV = Path("/home/seb/.local/bin/uv")
SYSTEMD_TIMEOUT_SECONDS = 6000
ACTIVATION_HEADROOM_SECONDS = 1500
MERGER_TIMEOUT_SECONDS = 3300
INSTALL_SYNC_TIMEOUT_SECONDS = 300
NOTIFY_TIMEOUT_SECONDS = 90
INSTALLED_IMPORT_PROBE = """\
import importlib
from pathlib import Path

repo = Path('/home/seb/projects/hermes-agent').resolve()
for name in ('hermes_startup_watchdog', 'hermes_state', 'hermes_cli.main', 'cron.scheduler'):
    module = importlib.import_module(name)
    origin = Path(module.__file__).resolve()
    if not origin.is_relative_to(repo):
        raise RuntimeError(f'{name} imported outside canonical repository: {origin}')
from cron.scheduler import CronTickYielded
"""
NOTIFY = REPO / "optional-skills/research/darwinian-evolver/labs/scripts/notify_telegram.py"
STATE = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "labs/bestplan-research/state"
LOCK = STATE / "upstream-merge.lock"
RUN_ROOT = STATE / "updater-runs"
UPSTREAM_URL = "https://github.com/NousResearch/hermes-agent.git"
FORK_URL = "git@github.com:sebmarion/hermes-agent.git"
DEPLOYMENT_ONLY_UNTRACKED_PREFIX = "plugins/hermes-bestplan/"
RELEVANT_TEST_PATHS = (
    "tests/skills",
    "tests/gateway/test_scale_to_zero_watcher.py",
    "tests/plugins/test_teams_pipeline_plugin.py",
    "tests/tools/test_memory_tool.py",
)


class WrapperError(RuntimeError):
    """Fail-closed updater wrapper error."""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()
        raise WrapperError(f"git {args[0]} failed: {detail[-2000:]}")
    return proc.stdout.strip()


def _is_clean(repo: Path) -> bool:
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    for line in status.splitlines():
        if not line:
            continue
        if line.startswith("?? ") and line[3:].startswith(DEPLOYMENT_ONLY_UNTRACKED_PREFIX):
            continue
        return False
    return True


def _remote_main_sha(repo: Path) -> str:
    output = _git(repo, "ls-remote", "sebmarion-fork", "refs/heads/main")
    return output.split()[0] if output.split() else ""


def _load_sync_state() -> dict:
    path = STATE / "upstream-sync.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _assert_canonical(expected_head: str | None = None) -> str:
    if not _is_clean(REPO):
        raise WrapperError("canonical Hermes checkout is dirty; refusing updater run")
    head = _git(REPO, "rev-parse", "HEAD")
    if expected_head and head != expected_head:
        raise WrapperError(
            f"canonical HEAD moved: expected {expected_head[:12]}, got {head[:12]}"
        )
    return head


def _recover_published_activation() -> str:
    """Fast-forward a clean canonical checkout only from a matching receipt.

    This covers a crash after publish but before canonical promotion/reload.
    Unknown remote movement remains a hard halt.
    """
    head = _assert_canonical()
    _git(REPO, "fetch", "sebmarion-fork", "main")
    remote = _remote_main_sha(REPO)
    if not remote:
        raise WrapperError("fork remote main is unavailable")
    if head == remote:
        return head

    state = _load_sync_state()
    if state.get("published_sha") != remote:
        raise WrapperError(
            f"canonical/remote mismatch has no matching updater receipt: "
            f"local {head[:12]} remote {remote[:12]}"
        )
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", head, remote],
        cwd=REPO,
        capture_output=True,
    )
    if ancestor.returncode:
        raise WrapperError("receipt remote is not a fast-forward of canonical HEAD")
    _git(REPO, "merge", "--ff-only", remote)
    return _assert_canonical(remote)


def _create_isolated_repo(
    source_head: str,
    *,
    source_repo: Path = REPO,
    run_root: Path = RUN_ROOT,
    upstream_url: str = UPSTREAM_URL,
    fork_url: str = FORK_URL,
) -> tuple[Path, Path]:
    """Create a disposable clone with refs/index independent from canonical."""
    run_root.mkdir(parents=True, exist_ok=True)
    run_parent = Path(tempfile.mkdtemp(prefix="run-", dir=run_root))
    run_repo = run_parent / "repo"
    try:
        proc = subprocess.run(
            ["git", "clone", "--no-checkout", str(source_repo), str(run_repo)],
            capture_output=True,
            text=True,
        )
        if proc.returncode:
            raise WrapperError(f"isolated clone failed: {proc.stderr[-2000:]}")
        _git(run_repo, "remote", "set-url", "origin", upstream_url)
        _git(run_repo, "remote", "remove", "sebmarion-fork", check=False)
        _git(run_repo, "remote", "add", "sebmarion-fork", fork_url)
        _git(run_repo, "fetch", "origin", "main")
        _git(run_repo, "fetch", "sebmarion-fork", "main")
        fork_head = _git(run_repo, "rev-parse", "sebmarion-fork/main")
        if fork_head != source_head:
            raise WrapperError(
                f"fork remote moved before isolated run: "
                f"expected {source_head[:12]}, got {fork_head[:12]}"
            )
        _git(run_repo, "switch", "--detach", fork_head)
        if not _is_clean(run_repo):
            raise WrapperError("isolated updater clone is unexpectedly dirty")
        return run_parent, run_repo
    except BaseException:
        shutil.rmtree(run_parent, ignore_errors=True)
        raise


def _prune_stale_runs(max_age_seconds: int = 86_400) -> None:
    if not RUN_ROOT.is_dir():
        return
    cutoff = time.time() - max_age_seconds
    for child in RUN_ROOT.iterdir():
        try:
            if child.is_dir() and not child.is_symlink() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _build_command(repo: Path = REPO) -> list[str]:
    merger = repo / "optional-skills/research/darwinian-evolver/labs/scripts/merge_upstream.py"
    command = [
        str(PYTHON), str(merger),
        "--repo", str(repo),
        "--state-dir", str(STATE),
        "--remote", "sebmarion-fork",
        "--apply", "--publish",
        "--test", str(PYTHON),
        "--test=-m",
        "--test", "pytest",
    ]
    for path in RELEVANT_TEST_PATHS:
        command.extend(["--test", path])
    command.append("--test=-q")
    return command


def _promote_canonical(expected_head: str, published_sha: str) -> str:
    _assert_canonical(expected_head)
    _git(REPO, "fetch", "sebmarion-fork", "main")
    remote = _remote_main_sha(REPO)
    if remote != published_sha:
        raise WrapperError(
            f"published SHA moved before canonical promotion: "
            f"expected {published_sha[:12]}, got {remote[:12]}"
        )
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", expected_head, published_sha],
        cwd=REPO,
        capture_output=True,
    )
    if ancestor.returncode:
        raise WrapperError("tested candidate is not a fast-forward of canonical HEAD")
    _git(REPO, "merge", "--ff-only", published_sha)
    return _assert_canonical(published_sha)


def _sync_canonical_install(repo: Path = REPO) -> None:
    """Refresh the live venv and prove top-level modules import off-checkout."""
    if not UV.is_file():
        raise WrapperError(f"uv executable is unavailable: {UV}")
    completed = subprocess.run(
        [
            str(UV),
            "sync",
            "--locked",
            "--extra",
            "dev",
            "--extra",
            "messaging",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=INSTALL_SYNC_TIMEOUT_SECONDS,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise WrapperError(f"canonical install sync failed: {detail[-2000:]}")
    probe_env = {
        key: value
        for key in ("HOME", "HERMES_HOME", "PATH", "LANG", "LC_ALL", "TZ")
        if (value := os.environ.get(key)) is not None
    }
    with tempfile.TemporaryDirectory(prefix="hermes-import-probe-") as probe_dir_raw:
        probe_dir = Path(probe_dir_raw)
        probe_dir.chmod(0o700)
        probe = subprocess.run(
            [str(PYTHON), "-I", "-c", INSTALLED_IMPORT_PROBE],
            cwd=probe_dir,
            capture_output=True,
            env=probe_env,
            text=True,
            timeout=60,
        )
    if probe.returncode:
        detail = (probe.stderr or probe.stdout).strip()
        raise WrapperError(f"installed import probe failed: {detail[-2000:]}")


def _promote_and_sync_canonical(expected_head: str, published_sha: str) -> str:
    promoted = _promote_canonical(expected_head, published_sha)
    _sync_canonical_install(REPO)
    return promoted


def _notify(event: str, message: str) -> None:
    if not NOTIFY.is_file():
        return
    try:
        subprocess.run(
            [str(PYTHON), str(NOTIFY), "--event", event, "--message", message],
            capture_output=True,
            text=True,
            timeout=NOTIFY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("skipped: upstream merge already running")
            return 0

        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        run_parent: Path | None = None
        canonical_before: str | None = None
        canonical_advanced_to: str | None = None
        canonical_mutation_unknown = False
        try:
            _prune_stale_runs()
            canonical_before = _assert_canonical()
            source_head = _recover_published_activation()
            if source_head != canonical_before:
                canonical_advanced_to = source_head
            # Recovery includes the case where the prior run published and
            # promoted source but died before synchronizing the live venv.
            # Prove the current canonical install before starting new work.
            _sync_canonical_install(REPO)
            run_parent, run_repo = _create_isolated_repo(source_head)
            proc = subprocess.run(
                _build_command(run_repo),
                capture_output=True,
                text=True,
                timeout=MERGER_TIMEOUT_SECONDS,
            )
            output = (proc.stdout + "\n" + proc.stderr).strip()
            if proc.returncode:
                raise WrapperError(output[-12_000:] or f"merger exited {proc.returncode}")

            published_sha = str(_load_sync_state().get("published_sha") or "")
            if len(published_sha) != 40:
                raise WrapperError("successful merger did not persist a published SHA receipt")
            canonical_advanced_to = _promote_canonical(source_head, published_sha)
            _sync_canonical_install(REPO)
        except (WrapperError, OSError, subprocess.SubprocessError) as exc:
            output = f"RESULT: HALT — {exc}"
            if canonical_before is not None and canonical_advanced_to is None:
                try:
                    actual_head = _git(REPO, "rev-parse", "HEAD", check=False)
                except OSError:
                    actual_head = ""
                actual_head_is_oid = re.fullmatch(r"[0-9a-f]{40}", actual_head) is not None
                if actual_head_is_oid and actual_head != canonical_before:
                    canonical_advanced_to = actual_head
                elif not actual_head_is_oid:
                    canonical_mutation_unknown = True
            report = STATE / "last-upstream-merge.txt"
            if canonical_advanced_to:
                mutation_status = (
                    f"canonical checkout advanced to {canonical_advanced_to}; "
                    "activation was withheld"
                )
            elif canonical_mutation_unknown:
                mutation_status = (
                    "canonical checkout mutation status is unknown; activation was withheld"
                )
            else:
                mutation_status = "canonical checkout was not mutated"
            report.write_text(
                f"started={started}\nexit_code=1\n{mutation_status}\n{output[-12000:]}\n"
            )
            _notify(
                "halted",
                f"Daily Hermes upstream sync halted safely; {mutation_status}. "
                f"Run started {started}. Reason: {output[-2500:]}",
            )
            print(output, file=sys.stderr)
            return 1
        finally:
            if run_parent is not None:
                shutil.rmtree(run_parent, ignore_errors=True)

        output = (
            "RESULT: OK — tested candidate published and canonical main advanced "
            f"via fast-forward to {published_sha}"
        )
        report = STATE / "last-upstream-merge.txt"
        report.write_text(
            f"started={started}\nexit_code=0\n{output}\n",
            encoding="utf-8",
        )
        _notify(
            "upgrade",
            "Daily Hermes upstream sync succeeded. The isolated tested candidate was "
            f"published and canonical main fast-forwarded to {published_sha}. "
            f"Run started {started}.",
        )
        print(output)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
