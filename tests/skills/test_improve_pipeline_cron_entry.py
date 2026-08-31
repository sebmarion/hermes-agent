"""Tests for the scheduler entry's fail-closed behavior."""
from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "optional-skills/research/darwinian-evolver/labs/scripts"
sys.path.insert(0, str(SCRIPTS))

import harvest_x_bookmarks as hx  # noqa: E402
import improve_cron_entry as entry  # noqa: E402
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def _configured_auxiliary_judge(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "auxiliary": {
                "moa_reference": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash-0731",
                }
            }
        },
    )


def test_merge_failures_deduplicates_exact_failure_class_but_preserves_distinct_titles() -> None:
    first = {"task_id": "a", "failure_signature": "error", "task_title": "Timeout", "task_instructions": "gateway failed"}
    duplicate = {"task_id": "b", "failure_signature": "error", "task_title": "Timeout", "task_instructions": "gateway failed"}
    distinct = {"task_id": "c", "failure_signature": "error", "task_title": "Parser", "task_instructions": "invalid JSON"}

    merged = entry._merge_failures([first, duplicate], [distinct])

    assert [row["task_id"] for row in merged] == ["a", "c"]


def test_merge_failures_does_not_collapse_distinct_long_instructions() -> None:
    prefix = "same " * 250
    first = {"task_id": "a", "failure_signature": "error", "task_title": "Same", "task_instructions": prefix + " first"}
    second = {"task_id": "b", "failure_signature": "error", "task_title": "Same", "task_instructions": prefix + " second"}

    merged = entry._merge_failures([], [first, second])

    assert [row["task_id"] for row in merged] == ["a", "b"]


def test_merge_failures_skips_empty_objects_but_keeps_valid_rows() -> None:
    valid = {
        "task_id": "task_valid",
        "failure_signature": "error",
        "task_title": "Timeout",
        "task_instructions": "gateway failed",
    }

    merged = entry._merge_failures([{}, {"task_id": "empty"}], [valid])

    assert [row["task_id"] for row in merged] == [valid["task_id"]]


def test_bookmark_failure_is_skipped_without_failing_cron(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)

    def unavailable(_n):
        raise RuntimeError("xurl unavailable")

    monkeypatch.setattr(hx, "fetch_bookmarks", unavailable)
    monkeypatch.setattr(entry.hf, "load_hermes_sessions", lambda _db_path=None: [])
    assert entry.main(["entry"]) == 0
    reports = list(tmp_path.glob("report-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["ok"] is True
    assert report["halted"] is False
    assert "harvest_x: skipped" in report["steps"]


def test_unreadable_pending_queue_does_not_advance_session_watermark(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(hx, "fetch_bookmarks", lambda _n: [])
    (tmp_path / "pending_failures.jsonl").write_text("not-json\n")
    writes = []
    monkeypatch.setattr(entry.ps, "write_watermark", lambda *args: writes.append(args))

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("unreadable-queue-session", source="cli", model="test")
    db.append_message("unreadable-queue-session", role="user", content="repair")
    db.append_message("unreadable-queue-session", role="assistant", content="ERROR: failed")
    db.end_session("unreadable-queue-session", "done")
    db.close()

    assert entry.main(["entry", "--db-path", str(db_path)]) != 0
    report = json.loads(next(tmp_path.glob("report-*.json")).read_text())
    assert report["watermark_sessions"] == 0
    assert report["n_failures_new"] == 0
    assert writes == []


def test_cron_harvests_real_session_rows_before_halting_live_chain(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(hx, "fetch_bookmarks", lambda _n: [])

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("real-session-0001", source="cli", model="test")
    db.append_message("real-session-0001", role="user", content="repair the skill")
    db.append_message("real-session-0001", role="assistant", content="ERROR: the check failed")
    db.end_session("real-session-0001", "done")
    db.close()

    monkeypatch.setattr(entry, "run_live_chain", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("live chain unavailable")))
    assert entry.main(["entry", "--db-path", str(db_path)]) != 0
    reports = list(tmp_path.glob("report-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["ok"] is False
    assert report["halted"] is True
    assert report["n_failures_new"] == 1
    assert isinstance(report["watermark_sessions"], int)
    assert report["watermark_sessions"] > 0
    assert (tmp_path / "failures.jsonl").is_file()
    failure = json.loads((tmp_path / "failures.jsonl").read_text())
    assert failure["before_session_ids"] == ["real-session-0001"]
    assert "session data source is not wired" not in " ".join(report["notes"])


def test_cron_hands_harvested_failures_to_live_chain(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(hx, "fetch_bookmarks", lambda _n: [])

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("real-session-0002", source="cli", model="test")
    db.append_message("real-session-0002", role="user", content="repair the skill")
    db.append_message("real-session-0002", role="assistant", content="ERROR: the check failed")
    db.end_session("real-session-0002", "done")
    db.close()

    seen = {}
    luna_prompts = []

    monkeypatch.setattr(
        entry.propose_zeus_candidate,
        "call_luna",
        lambda prompt: luna_prompts.append(prompt) or "proposal",
    )

    def fake_chain(**kwargs):
        seen.update(kwargs)
        assert kwargs["proposer"]("failure prompt") == "proposal"
        return {"ok": True, "halted": False, "applied": ["task_x"], "blocked": [], "notes": []}

    monkeypatch.setattr(entry, "run_live_chain", fake_chain)
    monkeypatch.setattr(
        entry.promote_skill,
        "promote",
        lambda **_kwargs: {"status": "pushed", "commit": "test", "remote_head": "test", "remote": "origin", "branch": "main", "verification": {"status": "passed"}},
    )
    monkeypatch.setattr(entry, "_request_live_activation", lambda *_args: "request.json")
    assert entry.main(["entry", "--db-path", str(db_path)]) == 0
    assert len(seen["failures"]) == 1
    assert seen["failures"][0]["before_session_ids"] == ["real-session-0002"]
    assert luna_prompts == ["failure prompt"]


def test_cron_includes_actionable_bookmarks_in_luna_research_context(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(
        hx,
        "fetch_bookmarks",
        lambda _n: [
            {
                "id": "22",
                "full_text": "Hermes hook idea api_key=abcdefghijklmnop",
                "url": "https://x.com/u/status/22",
            },
            {
                "id": "23",
                "full_text": "football result",
                "url": "https://x.com/u/status/23",
            },
        ],
    )
    monkeypatch.setattr(
        entry.hf,
        "load_hermes_sessions",
        lambda _db_path=None: [
            {
                "id": "real-session-bookmark-context",
                "seq": 1,
                "title": "repair",
                "body": "ERROR: the check failed",
            }
        ],
    )
    luna_prompts = []
    monkeypatch.setattr(
        entry.propose_zeus_candidate,
        "call_luna",
        lambda prompt: luna_prompts.append(prompt) or "proposal",
    )

    def fake_chain(**kwargs):
        assert kwargs["proposer"]("failure prompt") == "proposal"
        return {
            "ok": True,
            "halted": False,
            "applied": [],
            "blocked": [kwargs["failures"][0]["task_id"]],
            "notes": [],
        }

    monkeypatch.setattr(entry, "run_live_chain", fake_chain)

    assert entry.main(["entry"]) == 0
    assert len(luna_prompts) == 1
    assert "X BOOKMARK RESEARCH CONTEXT" in luna_prompts[0]
    assert "Hermes hook idea" in luna_prompts[0]
    assert "https://x.com/u/status/22" in luna_prompts[0]
    assert "football result" not in luna_prompts[0]
    assert "abcdefghijklmnop" not in luna_prompts[0]


def test_configured_judge_route_is_explicit_and_independent_from_luna() -> None:
    route = entry._configured_judge_route(
        {
            "auxiliary": {
                "moa_reference": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash-0731",
                }
            }
        }
    )

    assert route == ("openrouter", "deepseek/deepseek-v4-flash-0731")


@pytest.mark.parametrize(
    "task_config",
    [
        {"provider": "auto", "model": ""},
        {"provider": "openrouter", "model": ""},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "openrouter", "model": "openrouter/gpt-5.6-luna"},
        {"provider": ["openrouter"], "model": "deepseek/deepseek-v4-flash-0731"},
        {"provider": "openrouter", "model": ["deepseek/deepseek-v4-flash-0731"]},
        {"provider": "openrouter", "model": {"name": "deepseek/deepseek-v4-flash-0731"}},
    ],
)
def test_configured_judge_route_fails_closed_when_not_explicitly_independent(task_config) -> None:
    with pytest.raises(RuntimeError, match="independent judge"):
        entry._configured_judge_route({"auxiliary": {"moa_reference": task_config}})


def test_independent_judge_uses_auxiliary_moa_reference_and_returns_actual_model(monkeypatch) -> None:
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            model="deepseek/deepseek-v4-flash-0731",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"verdict":"equal","score":0.8,"rationale":"steady"}'
                    )
                )
            ],
        )

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    raw, actual_model = entry._call_independent_judge(
        "judge this", "openrouter", "deepseek/deepseek-v4-flash-0731"
    )

    assert raw.startswith("{")
    assert actual_model == "deepseek/deepseek-v4-flash-0731"
    assert calls[0]["task"] == "moa_reference"
    assert calls[0]["provider"] == "openrouter"
    assert calls[0]["model"] == "deepseek/deepseek-v4-flash-0731"
    response_format = calls[0]["extra_body"]["response_format"]
    assert response_format["type"] == "json_schema"
    schema = response_format["json_schema"]["schema"]
    assert schema["required"] == ["verdict", "score", "rationale"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["rationale"]["minLength"] == 10


def test_independent_judge_rejects_luna_fallback_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **_kwargs: SimpleNamespace(
            model="openai/gpt-5.6-luna",
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict":"equal"}'))],
        ),
    )

    with pytest.raises(RuntimeError, match="independent judge"):
        entry._call_independent_judge(
            "judge this", "openrouter", "deepseek/deepseek-v4-flash-0731"
        )


def test_independent_judge_requires_actual_model_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **_kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict":"equal"}'))],
        ),
    )

    with pytest.raises(RuntimeError, match="model metadata"):
        entry._call_independent_judge(
            "judge this", "openrouter", "deepseek/deepseek-v4-flash-0731"
        )


@pytest.mark.parametrize("bad_model", [["gpt-5.6-luna"], {"name": "gpt-5.6-luna"}])
def test_independent_judge_rejects_non_string_model_metadata(monkeypatch, bad_model) -> None:
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **_kwargs: SimpleNamespace(
            model=bad_model,
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict":"equal"}'))],
        ),
    )

    with pytest.raises(RuntimeError, match="model metadata"):
        entry._call_independent_judge(
            "judge this", "openrouter", "deepseek/deepseek-v4-flash-0731"
        )


def test_cron_caps_live_candidates_and_persists_remaining_queue(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(entry, "MAX_CANDIDATES_PER_RUN", 1)
    monkeypatch.setattr(hx, "fetch_bookmarks", lambda _n: [])

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    for index in (1, 2):
        sid = f"real-session-00{index:02d}"
        db.create_session(sid, source="cli", model="test")
        db.append_message(sid, role="user", content="repair the skill")
        db.append_message(sid, role="assistant", content=f"ERROR: check {index} failed")
        db.end_session(sid, "done")
    db.close()

    seen = {}

    def fake_chain(**kwargs):
        seen.update(kwargs)
        return {
            "ok": True,
            "halted": False,
            "applied": [kwargs["failures"][0]["task_id"]],
            "blocked": [],
            "notes": [],
        }

    monkeypatch.setattr(entry, "run_live_chain", fake_chain)
    monkeypatch.setattr(
        entry.promote_skill,
        "promote",
        lambda **_kwargs: {"status": "pushed", "commit": "test", "remote_head": "test", "remote": "origin", "branch": "main", "verification": {"status": "passed"}},
    )
    monkeypatch.setattr(entry, "_request_live_activation", lambda *_args: "request.json")
    assert entry.main(["entry", "--db-path", str(db_path)]) == 0
    assert len(seen["failures"]) == 1
    pending = [json.loads(line) for line in (tmp_path / "pending_failures.jsonl").read_text().splitlines()]
    assert len(pending) == 1


def test_cron_promotes_accepted_skill_changes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(hx, "fetch_bookmarks", lambda _n: [])

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("real-session-0003", source="cli", model="test")
    db.append_message("real-session-0003", role="user", content="repair the skill")
    db.append_message("real-session-0003", role="assistant", content="ERROR: check failed")
    db.end_session("real-session-0003", "done")
    db.close()

    monkeypatch.setattr(
        entry,
        "run_live_chain",
        lambda **_kwargs: {
            "ok": True,
            "halted": False,
            "applied": ["task_accepted"],
            "blocked": [],
            "notes": [],
            "summary": "1 applied, 0 blocked",
        },
    )
    seen = {}

    def fake_promote(**kwargs):
        seen.update(kwargs)
        return {
            "status": "pushed",
            "commit": "abc123",
            "remote_head": "abc123",
            "remote": "origin",
            "branch": "main",
            "changed_paths": ["software-development/bestplan/SKILL.md"],
            "verification": {"status": "passed"},
        }

    monkeypatch.setattr(entry.promote_skill, "promote", fake_promote)
    monkeypatch.setattr(entry, "_request_live_activation", lambda *_args: "request.json")
    assert entry.main(["entry", "--db-path", str(db_path)]) == 0
    assert seen["changed_paths"] == ["software-development/bestplan/SKILL.md"]
    report = json.loads(next(tmp_path.glob("report-*.json")).read_text())
    assert report["promotion"]["status"] == "pushed"
    assert report["promotion"]["commit"] == "abc123"


def test_failed_promotion_restores_skill_and_keeps_task_queued(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(hx, "fetch_bookmarks", lambda _n: [])
    state_dir = tmp_path / "state"
    repo = tmp_path / "skills-repo"
    skill = repo / "software-development" / "bestplan" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    baseline = "---\nname: bestplan\ndescription: test\n---\n# BestPlan\n"
    skill.write_text(baseline)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("real-session-0004", source="cli", model="test")
    db.append_message("real-session-0004", role="user", content="repair the skill")
    db.append_message("real-session-0004", role="assistant", content="ERROR: check failed")
    db.end_session("real-session-0004", "done")
    db.close()

    seen = {}

    def fake_chain(**kwargs):
        task_id = kwargs["failures"][0]["task_id"]
        seen["task_id"] = task_id
        skill.write_text(baseline + "\n## Bad Candidate\n")
        return {
            "ok": True,
            "halted": False,
            "applied": [task_id],
            "blocked": [],
            "notes": [],
            "summary": "1 applied, 0 blocked",
        }

    monkeypatch.setattr(entry, "run_live_chain", fake_chain)
    monkeypatch.setattr(
        entry.promote_skill,
        "promote",
        lambda **_kwargs: (_ for _ in ()).throw(entry.promote_skill.PromotionError("OCR failed")),
    )

    assert entry.main([
        "entry",
        "--state-dir", str(state_dir),
        "--db-path", str(db_path),
        "--live-skills", str(repo),
        "--skill-path", str(skill),
    ]) == 1

    assert skill.read_text() == baseline
    assert subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repo, text=True
    ).strip() == ""
    pending = [
        json.loads(line)
        for line in (state_dir / "pending_failures.jsonl").read_text().splitlines()
    ]
    assert [row["task_id"] for row in pending] == [seen["task_id"]]


def test_activation_failure_does_not_requeue_successfully_promoted_task(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(hx, "fetch_bookmarks", lambda _n: [])
    state_dir = tmp_path / "state"
    repo = tmp_path / "skills-repo"
    skill = repo / "software-development" / "bestplan" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: bestplan\ndescription: test\n---\n# BestPlan\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("real-session-0005", source="cli", model="test")
    db.append_message("real-session-0005", role="user", content="repair the skill")
    db.append_message("real-session-0005", role="assistant", content="ERROR: check failed")
    db.end_session("real-session-0005", "done")
    db.close()

    def fake_chain(**kwargs):
        task_id = kwargs["failures"][0]["task_id"]
        return {"ok": True, "halted": False, "applied": [task_id], "blocked": [], "notes": []}

    monkeypatch.setattr(entry, "run_live_chain", fake_chain)
    monkeypatch.setattr(
        entry.promote_skill,
        "promote",
        lambda **_kwargs: {"status": "pushed", "commit": "abc123", "remote": "origin", "branch": "main", "verification": {"status": "passed"}},
    )
    monkeypatch.setattr(entry, "_request_live_activation", lambda *_args: (_ for _ in ()).throw(RuntimeError("activation unavailable")))

    assert entry.main([
        "entry", "--state-dir", str(state_dir), "--db-path", str(db_path),
        "--live-skills", str(repo), "--skill-path", str(skill),
    ]) == 1
    pending = [json.loads(line) for line in (state_dir / "pending_failures.jsonl").read_text().splitlines()]
    assert pending == []
    report = json.loads(next(state_dir.glob("report-*.json")).read_text())
    assert report["promotion"]["status"] == "pushed"


def test_ocr_gate_path_follows_resolved_skills_repository(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "skills-repo"
    skill = repo / "software-development" / "bestplan" / "SKILL.md"
    ocr = repo / "plugins" / "hermes-bestplan" / "bestplan_ocr.py"
    skill.parent.mkdir(parents=True)
    ocr.parent.mkdir(parents=True)
    skill.write_text("---\nname: bestplan\ndescription: test\n---\n")
    ocr.write_text("# canonical skills-repo OCR\n")
    monkeypatch.setattr(entry.promote_skill, "repository_root", lambda _path: repo)

    assert entry._bestplan_ocr_path(skill) == ocr
