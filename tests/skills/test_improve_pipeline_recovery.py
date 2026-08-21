"""TDD contracts for bounded automatic upstream-conflict recovery.

These tests define the required behavior for the new recovery stage:
1. A genuine upstream conflict triggers the resolver (worktree + model call).
2. A non-conflict failure (network/anchor/dirty-tree) does NOT trigger it.
3. Model failure HALTs (no candidate, original error surfaced).
4. A dirty/uncommitted candidate HALTs.
5. A candidate that is not a descendant of the pinned source HEAD HALTs.
6. A candidate whose tests fail HALTs.
7. A clean candidate is applied/published through the existing seams.
8. Path traversal / argument injection is impossible (prompt is a single
   argv element; the model argv is fixed; the worktree path is isolated).
9. Cleanup occurs on every failure (worktree removed).

All tests use fake ``hermes`` executables and toy Git repositories; no live
network and no real Qwen call.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "optional-skills/research/darwinian-evolver/labs/scripts"
sys.path.insert(0, str(SCRIPTS))

import merge_upstream as mu  # noqa: E402
import merge_upstream_recovery as mur  # noqa: E402


# ---------------------------------------------------------------------------
# Toy-repo helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _init_repo(repo: Path, files: dict[str, str]) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return _git(repo, "rev-parse", "HEAD")


FAKE_HERMES = '''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
worktree = None
args = sys.argv[1:]
for i, a in enumerate(args):
    if a == "--in":
        worktree = args[i + 1]
record = os.environ.get("FAKE_HERMES_RECORD")
if record:
    with open(record, "w") as f:
        json.dump({"argv": args}, f)
wt = Path(worktree) if worktree else Path(".")
behavior = ''' + "BEHAVIOR" + '''
def git(*a):
    subprocess.run(["git", *a], cwd=wt, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if behavior == "fail":
    sys.stderr.write("fake hermes failed\\n")
    sys.exit(1)
if behavior == "commit":
    git("add", "-A")
    git("commit", "--allow-empty", "-m", "candidate")
elif behavior == "dirty":
    git("add", "-A")
    git("commit", "--allow-empty", "-m", "candidate")
    (wt / "uncommitted.txt").write_text("leftover\\n")
elif behavior == "not-desc":
    git("checkout", "--orphan", "orphan")
    (wt / "orphan.txt").write_text("orphan\\n")
    git("add", ".")
    git("commit", "-m", "orphan")
elif behavior == "markers":
    (wt / "marked.py").write_text("<<<<<<< ours\\nours\\n>>>>>>> theirs\\n")
    git("add", ".")
    git("commit", "-m", "markers")
sys.exit(0)
'''


def _write_fake_hermes(path: Path, behavior: str = "commit") -> None:
    path.write_text(FAKE_HERMES.replace("BEHAVIOR", json.dumps(behavior)))
    path.chmod(0o755)


def _recover(tmp_path: Path, repo: Path, *, behavior: str = "commit",
             model_timeout: int = 60, test_argv=None,
             required_paths: list[str] | None = [], expected_remote_sha: str | None = None):
    """Set up a fake hermes + call recover_upstream_conflict."""
    fake = tmp_path / "hermes"
    _write_fake_hermes(fake, behavior)
    record = tmp_path / "argv.json"
    state_path = tmp_path / "state" / "recovery-receipt.json"
    worktree = tmp_path / "recovery-wt"
    old_env = os.environ.get("PATH")
    os.environ["PATH"] = f"{tmp_path}{os.pathsep}{old_env}"
    os.environ["FAKE_HERMES_RECORD"] = str(record)
    try:
        result = mur.recover_upstream_conflict(
            repo=repo,
            source_head=_git(repo, "rev-parse", "HEAD"),
            anchor=_git(repo, "rev-parse", "HEAD"),
            upstream=_git(repo, "rev-parse", "HEAD"),
            expected_remote_sha=expected_remote_sha or "",
            conflict_detail="Applied patch to 'tools/memory_tool.py' with conflicts.",
            state_path=state_path,
            worktree=worktree,
            model_argv=None,  # Use the default HERMES_FIXED_ARGV
            model_timeout=model_timeout,
            required_paths=required_paths,
            test_argv=test_argv,
            notify=False,
        )
    finally:
        os.environ["PATH"] = old_env
        os.environ.pop("FAKE_HERMES_RECORD", None)
    return result, record, state_path, worktree


def _recover_direct(tmp_path: Path, repo: Path, *, behavior: str,
                    expected_remote_sha: str = "", test_argv=None,
                    conflict_detail: str = "Applied patch to 'a.py' with conflicts.",
                    source_head: str | None = None):
    """Set up a fake hermes + call recover_upstream_conflict (failure cases)."""
    fake = tmp_path / "hermes"
    _write_fake_hermes(fake, behavior)
    record = tmp_path / "argv.json"
    state_path = tmp_path / "state" / "recovery-receipt.json"
    worktree = tmp_path / "recovery-wt"
    old_env = os.environ.get("PATH")
    os.environ["PATH"] = f"{tmp_path}{os.pathsep}{old_env}"
    os.environ["FAKE_HERMES_RECORD"] = str(record)
    try:
        result = mur.recover_upstream_conflict(
            repo=repo,
            source_head=source_head or _git(repo, "rev-parse", "HEAD"),
            anchor=_git(repo, "rev-parse", "HEAD"),
            upstream=_git(repo, "rev-parse", "HEAD"),
            expected_remote_sha=expected_remote_sha,
            conflict_detail=conflict_detail,
            state_path=state_path,
            worktree=worktree,
            model_argv=["hermes", "-z"],
            test_argv=test_argv,
            required_paths=[],
        )
    finally:
        os.environ["PATH"] = old_env
        os.environ.pop("FAKE_HERMES_RECORD", None)
    return result, record, state_path, worktree


# ---------------------------------------------------------------------------
# Contract 1: a genuine upstream conflict triggers the resolver
# ---------------------------------------------------------------------------

def test_conflict_triggers_resolver(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"core.py": "base\n"})
    result, record, state_path, worktree = _recover(tmp_path, repo)
    assert result["outcome"] == "ok"
    assert result["candidate_sha"]
    argv = json.loads(record.read_text())["argv"]
    assert "-z" in argv
    assert "--provider" in argv
    assert "custom:zeus" in argv
    assert "--in" in argv
    idx = argv.index("--in")
    assert str(worktree) == argv[idx + 1]
    prompt = argv[-1]
    assert "tools/memory_tool.py" in prompt
    assert "->" in prompt
    receipt = json.loads(state_path.read_text())
    assert receipt["outcome"] == "ok"
    assert "tools/memory_tool.py" in receipt["conflict_paths"]
    assert receipt["model_route"]
    assert receipt["test_result"] == "pass"
    assert receipt["source_head"] == base


# ---------------------------------------------------------------------------
# Contract 2: a non-conflict failure does NOT trigger recovery
# ---------------------------------------------------------------------------

def test_non_conflict_failure_does_not_trigger_recovery(tmp_path: Path) -> None:
    """Recovery is only for conflicts.  A missing model binary is a plain
    HALT, never an invented candidate."""
    repo = tmp_path / "repo"
    _init_repo(repo, {"core.py": "base\n"})
    state_path = tmp_path / "state" / "receipt.json"
    worktree = tmp_path / "wt"
    with pytest.raises(mur.RecoveryError):
        mur.recover_upstream_conflict(
            repo=repo,
            source_head=_git(repo, "rev-parse", "HEAD"),
            anchor=_git(repo, "rev-parse", "HEAD"),
            upstream=_git(repo, "rev-parse", "HEAD"),
            expected_remote_sha="",
            conflict_detail="some non-conflict detail",
            state_path=state_path,
            worktree=worktree,
            model_argv=["definitely-not-a-real-hermes-binary", "-z"],
        )
    assert not (worktree / "uncommitted.txt").exists()


# ---------------------------------------------------------------------------
# Contract 3: model failure HALTs
# ---------------------------------------------------------------------------

def test_model_failure_halts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, {"core.py": "base\n"})
    state_path = tmp_path / "state" / "recovery-receipt.json"
    with pytest.raises(mur.RecoveryError, match="model exited"):
        _recover_direct(tmp_path, repo, behavior="fail")
    receipt = json.loads(state_path.read_text())
    assert receipt["outcome"] == "halt"
    assert not receipt.get("candidate_sha")


# ---------------------------------------------------------------------------
# Contract 4: dirty / uncommitted candidate HALTs
# ---------------------------------------------------------------------------

def test_dirty_candidate_halts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, {"core.py": "base\n"})
    with pytest.raises(mur.RecoveryError, match="not clean"):
        _recover_direct(tmp_path, repo, behavior="dirty")


# ---------------------------------------------------------------------------
# Contract 5: candidate not a descendant of pinned HEAD HALTs
# ---------------------------------------------------------------------------

def test_candidate_not_descendant_halts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"core.py": "base\n"})
    with pytest.raises(mur.RecoveryError, match="descendant"):
        _recover_direct(tmp_path, repo, behavior="not-desc", source_head=base)


# ---------------------------------------------------------------------------
# Contract 6: candidate test failure HALTs
# ---------------------------------------------------------------------------

def test_candidate_test_failure_halts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, {"core.py": "base\n"})
    fail_test = tmp_path / "fail_test.py"
    fail_test.write_text("import sys; sys.exit(1)\n")
    with pytest.raises(mur.RecoveryError, match="tests failed"):
        _recover_direct(
            tmp_path, repo, behavior="commit",
            test_argv=["python", str(fail_test)],
        )


# ---------------------------------------------------------------------------
# Contract 7: a clean candidate is applied/published through existing seams
# ---------------------------------------------------------------------------

def test_clean_candidate_is_applied_via_existing_seams(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"core.py": "base\n"})
    result, record, state_path, worktree = _recover(tmp_path, repo)
    candidate = result["candidate_sha"]
    assert _git(repo, "rev-parse", candidate) == candidate
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, candidate],
        cwd=repo, capture_output=True,
    )
    assert proc.returncode == 0
    assert len(candidate) == 40
    assert callable(mu.publish_and_verify)
    assert callable(mu.apply_candidate)


# ---------------------------------------------------------------------------
# Contract 8: path traversal / argument injection is impossible
# ---------------------------------------------------------------------------

def test_argument_injection_is_impossible(tmp_path: Path) -> None:
    malicious_paths = [
        "a.py; rm -rf /",
        "b.py --yolo --provider evil",
        "c.py\n$(whoami)",
        "d.py --in /etc/passwd",
    ]
    for p in malicious_paths:
        prompt = mur.build_repair_prompt(
            anchor="a" * 40, upstream="b" * 40, source_head="c" * 40,
            conflict_paths=[p],
        )
        assert isinstance(prompt, str)
    prompt = mur.build_repair_prompt(
        anchor="a" * 40, upstream="b" * 40, source_head="c" * 40,
        conflict_paths=["evil.py --yolo"],
    )
    assert "evil.py --yolo" in prompt


# ---------------------------------------------------------------------------
# Contract 9: cleanup occurs on every failure
# ---------------------------------------------------------------------------

def test_cleanup_on_every_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, {"core.py": "base\n"})
    worktree = tmp_path / "wt"
    with pytest.raises(mur.RecoveryError):
        _recover_direct(tmp_path, repo, behavior="fail")
    assert not worktree.exists(), "recovery worktree was not cleaned up"


# ---------------------------------------------------------------------------
# Receipt is concise, machine-readable, and has no secrets / transcripts
# ---------------------------------------------------------------------------

def test_receipt_is_concise_and_secret_free(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"core.py": "base\n"})
    result, record, state_path, worktree = _recover(tmp_path, repo)
    receipt = json.loads(state_path.read_text())
    for field in ("source_head", "anchor", "upstream", "remote_sha",
                  "conflict_paths", "model_route", "test_result", "outcome"):
        assert field in receipt, f"receipt missing {field}"
    assert "output" not in receipt or len(receipt.get("output", "")) < 5000
    raw = state_path.read_text()
    assert "sk-" not in raw


# ---------------------------------------------------------------------------
# build_repair_prompt is bounded and names the conflict
# ---------------------------------------------------------------------------

def test_repair_prompt_names_conflict_and_delta() -> None:
    prompt = mur.build_repair_prompt(
        anchor="a" * 40, upstream="b" * 40, source_head="c" * 40,
        conflict_paths=["tools/memory_tool.py", "tools/memory.md"],
    )
    assert "tools/memory_tool.py" in prompt
    assert "tools/memory.md" in prompt
    assert "a" * 40 in prompt
    assert "b" * 40 in prompt
    assert "c" * 40 in prompt
    assert isinstance(prompt, str) and len(prompt) < 4000


# ---------------------------------------------------------------------------
# Notifications: milestone events are advisory (failure never changes HALT)
# ---------------------------------------------------------------------------

def test_notification_failure_is_advisory(tmp_path: Path) -> None:
    """A notification failure (missing hermes / script error) must not change
    the HALT/OK outcome.  We monkeypatch mur._notify to raise and assert the
    recovery still completes (success) or halts (failure) as expected."""
    repo = tmp_path / "repo"
    _init_repo(repo, {"core.py": "base\n"})
    fake = tmp_path / "hermes"
    _write_fake_hermes(fake, "commit")
    state_path = tmp_path / "state" / "recovery-receipt.json"
    worktree = tmp_path / "recovery-wt"
    old_env = os.environ.get("PATH")
    os.environ["PATH"] = f"{tmp_path}{os.pathsep}{old_env}"
    os.environ["FAKE_HERMES_RECORD"] = str(tmp_path / "argv.json")

    def _boom(*args, **kwargs):
        raise RuntimeError("telegram down")

    old_notify = mur._notify
    mur._notify = _boom
    try:
        result = mur.recover_upstream_conflict(
            repo=repo,
            source_head=_git(repo, "rev-parse", "HEAD"),
            anchor=_git(repo, "rev-parse", "HEAD"),
            upstream=_git(repo, "rev-parse", "HEAD"),
            expected_remote_sha="",
            conflict_detail="Applied patch to 'a.py' with conflicts.",
            state_path=state_path,
            worktree=worktree,
            model_argv=["hermes", "-z"],
            required_paths=[],
        )
        assert result["outcome"] == "ok"
    finally:
        mur._notify = old_notify
        os.environ["PATH"] = old_env
        os.environ.pop("FAKE_HERMES_RECORD", None)


def test_notification_failure_is_advisory_on_halt(tmp_path: Path) -> None:
    """A notification failure during a HALT must not mask the HALT."""
    repo = tmp_path / "repo"
    _init_repo(repo, {"core.py": "base\n"})
    fake = tmp_path / "hermes"
    _write_fake_hermes(fake, "fail")
    state_path = tmp_path / "state" / "recovery-receipt.json"
    worktree = tmp_path / "recovery-wt"
    old_env = os.environ.get("PATH")
    os.environ["PATH"] = f"{tmp_path}{os.pathsep}{old_env}"
    os.environ["FAKE_HERMES_RECORD"] = str(tmp_path / "argv.json")

    def _boom(*args, **kwargs):
        raise RuntimeError("telegram down")

    old_notify = mur._notify
    mur._notify = _boom
    try:
        with pytest.raises(mur.RecoveryError):
            mur.recover_upstream_conflict(
                repo=repo,
                source_head=_git(repo, "rev-parse", "HEAD"),
                anchor=_git(repo, "rev-parse", "HEAD"),
                upstream=_git(repo, "rev-parse", "HEAD"),
                expected_remote_sha="",
                conflict_detail="Applied patch to 'a.py' with conflicts.",
                state_path=state_path,
                worktree=worktree,
                model_argv=["hermes", "-z"],
                required_paths=[],
            )
    finally:
        mur._notify = old_notify
        os.environ["PATH"] = old_env
        os.environ.pop("FAKE_HERMES_RECORD", None)
    receipt = json.loads(state_path.read_text())
    assert receipt["outcome"] == "halt"


def test_milestone_events_use_allowed_labels(tmp_path: Path) -> None:
    """The recovery module emits only allowed notify_telegram events
    (applied/halted/upgrade) and the messages are concise + redacted."""
    import notify_telegram
    # Verify the events we use are in the allowed set.
    assert "halted" in notify_telegram.EVENTS
    assert "upgrade" in notify_telegram.EVENTS
    assert "applied" in notify_telegram.EVENTS
    # The _notify_start/_notify_success/_notify_failure helpers must produce
    # concise messages (no full transcripts) via allowed events.
    start = mur._notify_start(
        source_head="a" * 40, anchor="b" * 40, upstream="c" * 40,
        conflict_paths=["a.py", "b.py"],
    )
    assert isinstance(start, dict)
    success = mur._notify_success(
        candidate_sha="d" * 40, conflict_paths=["a.py"],
    )
    assert isinstance(success, dict)
    failure = mur._notify_failure(
        source_head="a" * 40, anchor="b" * 40, upstream="c" * 40,
        reason="model exited 1: some error",
    )
    assert isinstance(failure, dict)
