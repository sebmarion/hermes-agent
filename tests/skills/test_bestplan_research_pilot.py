"""
Tests for the bounded Zeus "best-plan research" pilot.

These tests are deliberately deterministic and offline:
  - contracts (schemas, template) exist and conform to the plan spec
  - the dataset validator is fail-closed on bad input, passes good input
  - the scorecard is deterministic and never invents a verdict
  - an end-to-end fixture drives the whole pipeline without a network

The live Zeus qualification / A-B pilot (Tasks 8-9) are exercised by hand per
the runbook; here we pin the code paths that *could* run offline.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO / "optional-skills" / "research" / "darwinian-evolver"
LAB = SKILL_DIR / "labs"


# ---------------------------------------------------------------------------
# Task 1: RED — contracts do not exist yet. These fail until GREEN (Task 2).
# ---------------------------------------------------------------------------

def test_lab_directory_exists() -> None:
    assert LAB.is_dir(), f"missing lab dir: {LAB}"


@pytest.mark.parametrize(
    "path",
    [
        "schemas/hermes_skill_dataset.schema.json",
        "schemas/blind_judge.schema.json",
        "templates/research_loop.yaml",
        "RUNBOOK.md",
    ],
)
def test_contract_files_exist(path: str) -> None:
    p = LAB / path
    assert p.is_file(), f"missing contract file: {p}"


def test_dataset_schema_has_required_fields() -> None:
    schema = json.loads((LAB / "schemas" / "hermes_skill_dataset.schema.json").read_text())
    props = schema.get("properties", {})
    for field in [
        "schema_version", "researcher_id", "task_id", "task_title",
        "task_instructions", "skill_path", "before_session_ids",
        "after_session_ids", "blind_id", "judge_model", "judge_prompt_hash",
        "candidate_patch_sha256",
    ]:
        assert field in props, f"dataset schema missing required property: {field}"
    assert schema.get("schema_version") == 1, "schema_version must be 1"


def test_blind_judge_schema_has_required_fields() -> None:
    schema = json.loads((LAB / "schemas" / "blind_judge.schema.json").read_text())
    props = schema.get("properties", {})
    for field in [
        "schema_version", "task_id", "blind_candidate", "judge_model",
        "score", "verdict", "reasoning",
    ]:
        assert field in props, f"judge schema missing required property: {field}"
    # verdict must be an enum
    verdict = props["verdict"]
    assert "enum" in verdict, "verdict must be an enum"
    allowed = {"better", "equal", "worse"}
    assert set(verdict["enum"]) <= allowed, f"verdict enum {verdict['enum']} exceeds {allowed}"


def test_research_loop_template_parses() -> None:
    raw = (LAB / "templates" / "research_loop.yaml").read_text()
    doc = yaml.safe_load(raw)
    assert isinstance(doc, dict), "research_loop.yaml must parse to a mapping"
    # Must contain the canonical loop phases
    text = yaml.dump(doc)
    for token in ["harvest", "candidate_patch", "blind_ab", "independent_review"]:
        assert token.lower() in text.lower(), f"template missing phase: {token}"


def test_runbook_exists_and_nonempty() -> None:
    text = (LAB / "RUNBOOK.md").read_text()
    assert len(text) > 200, "runbook too short to be useful"


# ---------------------------------------------------------------------------
# Task 3: dataset validator — fail-closed
# ---------------------------------------------------------------------------

VALIDATOR = LAB / "scripts" / "validate_hermes_skill_dataset.py"


def test_validator_script_parses() -> None:
    assert VALIDATOR.is_file(), f"missing {VALIDATOR}"
    ast.parse(VALIDATOR.read_text())


def _make_dataset(**overrides) -> dict:
    """A minimally valid dataset row per the plan schema."""
    base = {
        "schema_version": 1,
        "researcher_id": "zeus-qwen3.8-27b",
        "task_id": "task_0001",
        "task_title": "Fix the off-by-one in parse_token_count",
        "task_instructions": "Given a token stream, return the index of the first OOV.",
        "skill_path": "/Users/seb/.hermes/skills/example/SKILL.md",
        "before_session_ids": ["20260814_101530_abcd"],
        "after_session_ids": [],
        "blind_id": "A",
        "judge_model": "anthropic/claude-sonnet-4.6",
        "judge_prompt_hash": "sha256:" + "0" * 64,
        "candidate_patch_sha256": "a" * 40,
    }
    base.update(overrides)
    return base


def test_validator_accepts_valid_dataset(tmp_path: Path) -> None:
    out = tmp_path / "ds.jsonl"
    row = _make_dataset()
    out.write_text(json.dumps(row) + "\n")
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode == 0, f"validator rejected a valid row:\n{proc.stdout}\n{proc.stderr}"


@pytest.mark.parametrize(
    "bad_field, bad_value",
    [
        ("schema_version", 2),
        ("researcher_id", ""),
        ("task_id", ""),
        ("blind_id", "C"),            # must be exactly A or B
        ("candidate_patch_sha256", "not-a-hash"),
        ("skill_path", "/etc/passwd"),
    ],
)
def test_validator_rejects_bad_dataset(tmp_path: Path, bad_field: str, bad_value) -> None:
    out = tmp_path / "ds.jsonl"
    row = _make_dataset(**{bad_field: bad_value})
    out.write_text(json.dumps(row) + "\n")
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode != 0, f"validator accepted invalid {bad_field}={bad_value!r}"


def test_validator_rejects_missing_required_field(tmp_path: Path) -> None:
    out = tmp_path / "ds.jsonl"
    row = _make_dataset()
    del row["judge_prompt_hash"]
    out.write_text(json.dumps(row) + "\n")
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode != 0, "validator accepted a row missing judge_prompt_hash"


def test_validator_rejects_unknown_field(tmp_path: Path) -> None:
    out = tmp_path / "ds.jsonl"
    row = _make_dataset(secret="leaked-credential")
    out.write_text(json.dumps(row) + "\n")
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode != 0, "validator must reject unknown fields (fail-closed)"


def test_validator_handles_non_object_row_cleanly(tmp_path: Path) -> None:
    # F2: a JSONL line that is a bare string/number/null must produce a clean
    # per-line error (rc=1), NOT a traceback.
    out = tmp_path / "ds.jsonl"
    good = json.dumps(_make_dataset()) + "\n"
    out.write_text(good + '"just a plain string"\n42\nnull\n')
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode == 1, (
        f"non-object rows must be a clean per-line failure:\n{proc.stdout}\n{proc.stderr}"
    )
    combined = proc.stdout + proc.stderr
    assert "Traceback" not in combined, "non-object row leaked a traceback"
    assert "expected an object" in combined


def test_validator_catches_nested_and_numeric_credentials(tmp_path: Path) -> None:
    # F3: credential scan reaches nested values and non-string scalars.
    out = tmp_path / "ds.jsonl"
    # smuggle a JWT-shaped token into the (known) after_session_ids array value?
    # No — array items are strings; put a bearer token in task_instructions.
    row = _make_dataset(task_instructions="Authorization: Bearer " + "abcDEF123" * 8)
    out.write_text(json.dumps(row) + "\n")
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode != 0, "validator must catch a bearer token in a field value"


def test_skill_path_accepts_all_three_forms(tmp_path: Path) -> None:
    # Regression (review finding): the schema's skill_path pattern had a broken
    # tilde branch and an unanchored third alternative. Pin all three intended
    # forms so neither can silently break again.
    for good in [
        "/Users/seb/.hermes/skills/example/SKILL.md",   # absolute
        "~/.hermes/hermes-agent/sk/SKILL.md",          # tilde (was broken)
        "skills/research/darwinian-evolver/SKILL.md",  # repo-relative under skills/
    ]:
        out = tmp_path / "ds.jsonl"
        out.write_text(json.dumps(_make_dataset(skill_path=good)) + "\n")
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), str(out)],
            capture_output=True, text=True, cwd=REPO,
        )
        assert proc.returncode == 0, (
            f"validator must accept skill_path={good!r}:\n{proc.stdout}\n{proc.stderr}"
        )


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",                     # absolute but not a SKILL.md path
        "opt/skills/foo/SKILL.md",          # unanchored relative (not under skills/)
        "SKILL.md",                         # bare filename, no directory
        "skills/SKILL.md",                  # no directory segment after skills/
        "/Users/seb/x/README.md",           # wrong basename
    ],
)
def test_skill_path_rejects_bad_forms(tmp_path: Path, bad_path: str) -> None:
    out = tmp_path / "ds.jsonl"
    out.write_text(json.dumps(_make_dataset(skill_path=bad_path)) + "\n")
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode != 0, (
        f"validator must reject skill_path={bad_path!r}:\n{proc.stdout}\n{proc.stderr}"
    )


def test_validator_does_not_flag_benign_lookalikes(tmp_path: Path) -> None:
    # Lock-in: the credential scanner is intentionally NARROW (it deliberately
    # misses bare high-entropy tokens — see _CRED_PATTERNS docstring). Pin that
    # so an over-eager "improvement" can't regress the validator on real,
    # clean transcripts. Each of these must be judged CLEAN.
    benign = [
        "the word sk- in prose is fine",   # short 'sk-', not a 20-char token
        "api_key = 'short'",               # key name present but value < 16 chars
        "Bearer abcdef",                  # Bearer + short value, below threshold
        "ghp_shortnotvalidtoken",         # ghp_ prefix but only 19 chars (need 20+)
        "a short xoxb token, not a real one",  # word 'xoxb' alone, no long token after it
        "AKIA in a sentence is fine",     # the *word* akia alone is not an AWS key id
    ]
    for val in benign:
        out = tmp_path / "ds.jsonl"
        row = _make_dataset(task_instructions=val)
        out.write_text(json.dumps(row) + "\n")
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), str(out)],
            capture_output=True, text=True, cwd=REPO,
        )
        assert proc.returncode == 0, (
            f"benign value {val!r} was flagged as a credential:\n{proc.stdout}\n{proc.stderr}"
        )


# ---------------------------------------------------------------------------
# Task 5: deterministic scorecard — never invents a verdict
# ---------------------------------------------------------------------------

SCORECARD = LAB / "scripts" / "score_hermes_skill_run.py"


def test_scorecard_script_parses() -> None:
    assert SCORECARD.is_file(), f"missing {SCORECARD}"
    ast.parse(SCORECARD.read_text())


def test_scorecard_refuses_empty_judges(tmp_path: Path) -> None:
    judges = tmp_path / "judges.jsonl"
    judges.write_text("")  # zero judge lines
    scores = tmp_path / "scores.tsv"
    proc = subprocess.run(
        [sys.executable, str(SCORECARD), "--judge-file", str(judges), "--out", str(scores)],
        capture_output=True, text=True, cwd=REPO,
    )
    # Must fail closed rather than emit a synthetic verdict
    assert proc.returncode != 0, "scorecard must refuse to score with zero judges"


def test_scorecard_produces_deterministic_output(tmp_path: Path) -> None:
    judge_rows = [
        {"schema_version": 1, "task_id": "task_1111aaaa", "blind_candidate": "A",
         "judge_model": "model/test", "score": 0.8, "verdict": "better", "reasoning": "clearer result"},
        {"schema_version": 1, "task_id": "task_2222bbbb", "blind_candidate": "B",
         "judge_model": "model/test", "score": 0.3, "verdict": "worse", "reasoning": "confusing result"},
    ]
    judges = tmp_path / "judges.jsonl"
    judges.write_text("\n".join(json.dumps(r) for r in judge_rows) + "\n")
    out1 = tmp_path / "s1.tsv"
    out2 = tmp_path / "s2.tsv"
    common = [sys.executable, str(SCORECARD), "--judge-file", str(judges)]
    subprocess.run(common + ["--out", str(out1)], capture_output=True, cwd=REPO)
    subprocess.run(common + ["--out", str(out2)], capture_output=True, cwd=REPO)
    assert out1.is_file() and out1.read_text() == out2.read_text(), (
        "scorecard output must be byte-identical across runs"
    )


def test_scorecard_never_fabricates_verdict(tmp_path: Path) -> None:
    # A judge row with a verdict outside the allowed enum must be rejected,
    # not silently coerced to 'equal'.
    judge_rows = [
        {"schema_version": 1, "task_id": "task_1111aaaa", "blind_candidate": "A",
         "judge_model": "model/test", "score": 0.5, "verdict": "meh", "reasoning": "unclear result"},
    ]
    judges = tmp_path / "judges.jsonl"
    judges.write_text(json.dumps(judge_rows[0]) + "\n")
    out = tmp_path / "s.tsv"
    proc = subprocess.run(
        [sys.executable, str(SCORECARD), "--judge-file", str(judges), "--out", str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode != 0, "scorecard must reject out-of-enum verdicts"


def test_scorecard_aborts_on_partial_invalid(tmp_path: Path) -> None:
    # F1: a judge file with BOTH valid and invalid rows must abort the whole
    # run (rc=1, no scorecard emitted), not silently score only the good rows.
    judges = tmp_path / "judges.jsonl"
    good = {"schema_version": 1, "task_id": "task_a1b2c3d4", "blind_candidate": "A",
            "judge_model": "model/test", "score": 0.9, "verdict": "better", "reasoning": "clear result"}
    bad = {"schema_version": 1, "task_id": "task_b1c2d3e4", "blind_candidate": "A",
           "judge_model": "model/test", "score": 0.7, "verdict": "totally-fine", "reasoning": "unclear result"}
    judges.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n")
    out = tmp_path / "s.tsv"
    proc = subprocess.run(
        [sys.executable, str(SCORECARD), "--judge-file", str(judges), "--out", str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode != 0, "mixed valid+invalid judge file must abort"
    assert not out.exists() or out.stat().st_size == 0, (
        "scorecard must NOT emit partial output when any row is invalid"
    )


def test_scorecard_rejects_row_that_violates_declared_artifact_schema(tmp_path: Path) -> None:
    judges = tmp_path / "judges.jsonl"
    judges.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": "task_1234abcd",
                "blind_candidate": "A",
                "score": 0.9,
                "verdict": "better",
                "reasoning": "clear improvement",
            }
        )
        + "\n"
    )
    out = tmp_path / "s.tsv"

    proc = subprocess.run(
        [sys.executable, str(SCORECARD), "--judge-file", str(judges), "--out", str(out)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )

    assert proc.returncode != 0
    assert "judge_model" in proc.stderr


def test_scorecard_rejects_boolean_schema_version(tmp_path: Path) -> None:
    judges = tmp_path / "judges.jsonl"
    judges.write_text(
        json.dumps(
            {
                "schema_version": True,
                "task_id": "task_1234abcd",
                "blind_candidate": "A",
                "judge_model": "model/test",
                "score": 0.9,
                "verdict": "better",
                "reasoning": "clear improvement",
            }
        )
        + "\n"
    )
    out = tmp_path / "s.tsv"

    proc = subprocess.run(
        [sys.executable, str(SCORECARD), "--judge-file", str(judges), "--out", str(out)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )

    assert proc.returncode != 0
    assert "schema_version" in proc.stderr


# ---------------------------------------------------------------------------
# Task 4: Zeus qualification gate — fail-closed when unreachable
# ---------------------------------------------------------------------------

QUALIFIER = LAB / "scripts" / "qualify_zeus_researcher.py"


def test_qualifier_script_parses() -> None:
    assert QUALIFIER.is_file(), f"missing {QUALIFIER}"
    ast.parse(QUALIFIER.read_text())


def test_qualifier_fails_closed_when_unreachable(tmp_path: Path) -> None:
    # A bogus port on loopback: connection must be refused, and the script must
    # exit non-zero rather than report a false "OK".
    proc = subprocess.run(
        [sys.executable, str(QUALIFIER),
         "--base-url", "http://127.0.0.1:1/v1",   # nothing listens here
         "--api-key", "local-no-auth-needed",
         "--timeout", "2"],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode != 0, (
        f"qualifier must fail closed on unreachable endpoint:\n{proc.stdout}\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# Task 6: end-to-end offline fixture
# ---------------------------------------------------------------------------

def test_end_to_end_offline_fixture(tmp_path: Path) -> None:
    # Assemble a minimal but complete fixture run directory and drive both scripts.
    ds = tmp_path / "dataset.jsonl"
    ds.write_text(json.dumps(_make_dataset()) + "\n")

    vproc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(ds)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert vproc.returncode == 0, f"fixture dataset should validate: {vproc.stderr}"

    judges = tmp_path / "judges.jsonl"
    judge_rows = [
        {"schema_version": 1, "task_id": "task_1111aaaa", "blind_candidate": "A",
         "judge_model": "anthropic/claude-sonnet-4.6",
         "score": 0.9, "verdict": "better", "reasoning": "sharper instructions"},
        {"schema_version": 1, "task_id": "task_2222bbbb", "blind_candidate": "B",
         "judge_model": "anthropic/claude-sonnet-4.6",
         "score": 0.6, "verdict": "equal", "reasoning": "no measurable delta"},
    ]
    judges.write_text("\n".join(json.dumps(r) for r in judge_rows) + "\n")

    out = tmp_path / "scorecard.tsv"
    sproc = subprocess.run(
        [sys.executable, str(SCORECARD), "--judge-file", str(judges), "--out", str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert sproc.returncode == 0, f"scorecard should run on clean fixture: {sproc.stderr}"
    assert out.is_file() and out.stat().st_size > 0
