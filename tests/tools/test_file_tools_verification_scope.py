from __future__ import annotations

from pathlib import Path

from agent import coding_context, verification_evidence
from tools import file_tools


def test_scratch_edit_outside_projects_is_not_attributed_to_session_workspace(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    scratch = tmp_path / "scratch" / "capture.py"
    scratch.parent.mkdir()
    calls: list[dict[str, object]] = []

    def project_facts_for(cwd):
        resolved = Path(cwd).resolve()
        if resolved == repo.resolve():
            return {"root": str(repo)}
        return None

    monkeypatch.setattr(coding_context, "project_facts_for", project_facts_for)
    monkeypatch.setattr(
        file_tools,
        "_authoritative_workspace_root",
        lambda _task_id="default": str(repo),
    )
    monkeypatch.setattr(
        verification_evidence,
        "mark_workspace_edited",
        lambda **kwargs: calls.append(kwargs),
    )

    file_tools._mark_verification_stale(
        "task-1", [str(scratch)], session_id="session-1"
    )

    assert calls == []


def test_repository_edit_is_still_attributed_to_its_project(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    source = repo / "src" / "module.py"
    source.parent.mkdir(parents=True)
    calls: list[dict[str, object]] = []

    def project_facts_for(cwd):
        candidate = Path(cwd).resolve()
        if candidate == source.parent.resolve():
            return {"root": str(repo)}
        return None

    monkeypatch.setattr(coding_context, "project_facts_for", project_facts_for)
    monkeypatch.setattr(
        verification_evidence,
        "mark_workspace_edited",
        lambda **kwargs: calls.append(kwargs),
    )

    file_tools._mark_verification_stale(
        "task-2", [str(source)], session_id="session-2"
    )

    assert calls == [
        {
            "session_id": "session-2",
            "cwd": str(source.parent),
            "paths": [str(source)],
        }
    ]


def test_workspace_lookup_failure_does_not_suppress_repository_edit(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    source = repo / "module.py"
    repo.mkdir()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        coding_context,
        "project_facts_for",
        lambda cwd: {"root": str(repo)}
        if Path(cwd).resolve() == repo.resolve()
        else None,
    )

    def fail_workspace(_task_id="default"):
        raise RuntimeError("workspace lookup failed")

    monkeypatch.setattr(file_tools, "_authoritative_workspace_root", fail_workspace)
    monkeypatch.setattr(
        verification_evidence,
        "mark_workspace_edited",
        lambda **kwargs: calls.append(kwargs),
    )

    file_tools._mark_verification_stale(
        "task-3", [str(source)], session_id="session-3"
    )

    assert len(calls) == 1
    assert calls[0]["paths"] == [str(source)]


def test_one_project_discovery_error_does_not_suppress_later_repository_edit(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bad = tmp_path / "bad" / "broken.py"
    good = repo / "good.py"
    calls: list[dict[str, object]] = []

    def project_facts_for(cwd):
        candidate = Path(cwd).resolve()
        if candidate == bad.parent.resolve():
            raise RuntimeError("discovery failed")
        if candidate == repo.resolve():
            return {"root": str(repo)}
        return None

    monkeypatch.setattr(coding_context, "project_facts_for", project_facts_for)
    monkeypatch.setattr(
        file_tools,
        "_authoritative_workspace_root",
        lambda _task_id="default": None,
    )
    monkeypatch.setattr(
        verification_evidence,
        "mark_workspace_edited",
        lambda **kwargs: calls.append(kwargs),
    )

    file_tools._mark_verification_stale(
        "task-4", [str(bad), str(good)], session_id="session-4"
    )

    assert len(calls) == 1
    assert calls[0]["paths"] == [str(good)]
