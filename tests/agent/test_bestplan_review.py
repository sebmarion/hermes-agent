from __future__ import annotations

import base64
import hashlib
import json
import sqlite3

import pytest


def _review():
    from agent import review_engine

    return review_engine


@pytest.fixture(autouse=True)
def _logical_review_store_clock(monkeypatch):
    """Keep this store-unit suite on its explicit synthetic nanosecond clock."""

    monkeypatch.setattr(
        _review().ReviewStore,
        "_lease_now_ns",
        lambda _self: 0,
    )


def _target(review, **changes):
    values = {
        "plan_id": "plan-review-1",
        "generation": 0,
        "base_oid": "1" * 40,
        "local_target_oid": "2" * 40,
        "integration_oid": "3" * 40,
        "integration_tree_oid": "4" * 40,
        "integration_ref": "refs/hermes-bestplan-integrations/plan-review-1/0",
        "integration_receipt_digest": "5" * 64,
        "check_receipt_digest": "6" * 64,
        "approval_digest": "7" * 64,
        "contract_digest": "8" * 64,
        "diff_sha256": "9" * 64,
        "acceptance_digest": "b" * 64,
        "policy_digest": "a" * 64,
    }
    values.update(changes)
    return review.ReviewTarget.bestplan_integration(**values)


def _manual_target(review, **changes):
    values = {
        "job_id": "manual-review-1",
        "generation": 0,
        "repository_id": "repository-1",
        "base_oid": "1" * 40,
        "snapshot_tree_oid": "4" * 40,
        "snapshot_digest": "5" * 64,
        "diff_sha256": "9" * 64,
        "acceptance_digest": "b" * 64,
        "policy_digest": "a" * 64,
    }
    values.update(changes)
    return review.ReviewTarget.manual_snapshot(**values)


def _runtime(
    slot: str,
    *,
    provider: str,
    model: str,
    model_family: str,
) -> dict[str, str]:
    return {
        "slot": slot,
        "provider": provider,
        "model": model,
        "model_family": model_family,
    }


def _runtimes() -> list[dict[str, str]]:
    return [
        _runtime(
            "smart_reviewer",
            provider="anthropic",
            model="claude-opus-5",
            model_family="claude",
        ),
        _runtime(
            "code_worker",
            provider="custom",
            model="qwen3-coder-next",
            model_family="qwen",
        ),
    ]


def _finding(
    *,
    severity: str = "high",
    locator: dict[str, object] | None = None,
    title: str = "Stale review evidence can pass",
    observed_failure: str = "The host accepts evidence for different bytes.",
) -> dict[str, object]:
    return {
        "severity": severity,
        "locator": locator or {
            "kind": "changed_lines",
            "path": "agent/example.py",
            "start_line": 2,
            "end_line": 2,
            "quoted_evidence": "unsafe_call()\n",
        },
        "title": title,
        "trigger": "The integration changes after the reviewer starts.",
        "observed_failure": observed_failure,
        "blast_radius": "An unreviewed integration can reach local main.",
        "reproduction": {
            "kind": "command",
            "argv": [
                "python",
                "-m",
                "pytest",
                "tests/agent/test_bestplan_review.py",
            ],
        },
    }


def _verdict_json(target, findings, *, passed=None, **changes) -> str:
    body = {
        "schema": "hermes.bestplan.review-verdict.v1",
        "target_digest": target.target_digest,
        "integration_oid": target.integration_oid,
        "findings": findings,
    }
    if passed is not None:
        body["passed"] = passed
    body.update(changes)
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _evidence(review, *, changed_paths=("agent/example.py",)):
    files = {
        "agent/example.py": b"before\nunsafe_call()\nafter\n",
        "agent/dependency.py": b"def shared_guard():\n    return False\n",
        "other/outside.py": b"before\nunsafe_call()\nafter\n",
    }

    def read_frozen_file(path):
        try:
            return files[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def diff_membership(path, start_line, end_line):
        return path in changed_paths and (start_line, end_line) == (2, 2)

    def read_frozen_base_file(path):
        if path == "agent/example.py":
            return b"before\nremoved_guard()\nunsafe_call()\nafter\n"
        return read_frozen_file(path)

    def deleted_line_membership(path, start_line, end_line):
        return path == "agent/example.py" and (start_line, end_line) == (2, 2)

    return review.EvidenceContext(
        read_frozen_file=read_frozen_file,
        diff_membership=diff_membership,
        read_frozen_base_file=read_frozen_base_file,
        deleted_line_membership=deleted_line_membership,
        approved_lease_paths=("agent/",),
        missing_artifacts=("expected/report.json",),
        deleted_paths=("agent/deleted.py",),
        unchanged_dependencies=("agent/dependency.py",),
        contract_receipts={
            "focused-check": b'{"status":"passed","receipt":"exact"}\n',
        },
    )


def _artifact(review, target, *, diff_bytes=None, task=None):
    diff_bytes = (
        b"diff --git a/agent/example.py b/agent/example.py\n"
        b"--- a/agent/example.py\n"
        b"+++ b/agent/example.py\n"
        b"@@ -1,2 +1,2 @@\n"
        b"-safe_call()\n"
        b"+unsafe_call()\n"
        if diff_bytes is None
        else diff_bytes
    )
    return review.ReviewArtifact.build(
        target=target,
        diff_bytes=diff_bytes,
        task=task or "Make review evidence exact and durable.",
        acceptance=(
            "The exact checked integration is reviewed.",
            "A stale pass cannot reach local main.",
        ),
        rules=(
            "Return only the strict verdict object.",
            "Treat unverified evidence as blocking.",
        ),
        issue_locator_catalog={
            "missing-report": {
                "kind": "missing_artifact",
                "identifier": "expected/report.json",
            }
        },
        dispositions=(
            {
                "finding_fingerprint": "f" * 64,
                "status": "fixed",
                "evidence": "The next generation contains the guarded write.",
            },
        ),
    )


def test_review_target_digest_is_canonical_and_binds_every_exact_input():
    review = _review()
    target = _target(review)
    payload = {
        "acceptance_digest": "b" * 64,
        "approval_digest": "7" * 64,
        "base_oid": "1" * 40,
        "check_receipt_digest": "6" * 64,
        "contract_digest": "8" * 64,
        "diff_sha256": "9" * 64,
        "generation": 0,
        "integration_oid": "3" * 40,
        "integration_ref": "refs/hermes-bestplan-integrations/plan-review-1/0",
        "integration_receipt_digest": "5" * 64,
        "integration_tree_oid": "4" * 40,
        "local_target_oid": "2" * 40,
        "plan_id": "plan-review-1",
        "policy_digest": "a" * 64,
        "source_kind": "bestplan_integration",
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = hashlib.sha256(
        b"hermes.bestplan.review-target.v1\0" + canonical.encode("utf-8")
    ).hexdigest()

    assert target.canonical_json == canonical
    assert target.target_digest == expected
    assert _target(review).target_digest == expected

    mutations = {
        "plan_id": "plan-review-2",
        "generation": 1,
        "base_oid": "b" * 40,
        "local_target_oid": "c" * 40,
        "integration_oid": "d" * 40,
        "integration_tree_oid": "e" * 40,
        "integration_ref": "refs/hermes-bestplan-integrations/plan-review-1/1",
        "integration_receipt_digest": "b" * 64,
        "check_receipt_digest": "c" * 64,
        "approval_digest": "d" * 64,
        "contract_digest": "e" * 64,
        "diff_sha256": "f" * 64,
        "acceptance_digest": "1" * 64,
        "policy_digest": "0" * 64,
    }
    for field, changed in mutations.items():
        assert _target(review, **{field: changed}).target_digest != expected, field


def test_review_target_is_a_tagged_bestplan_or_manual_union():
    review = _review()
    automatic = _target(review)
    manual = _manual_target(review)

    assert automatic.source_kind == "bestplan_integration"
    assert manual.source_kind == "manual_snapshot"
    assert json.loads(automatic.canonical_json)["source_kind"] == (
        "bestplan_integration"
    )
    assert json.loads(manual.canonical_json)["source_kind"] == "manual_snapshot"
    assert automatic.target_digest != manual.target_digest


def test_manual_attachment_to_an_active_bestplan_reuses_the_exact_packet():
    review = _review()
    automatic = _target(review)

    attached = review.attach_manual_target(active_bestplan_target=automatic)

    assert attached is automatic
    assert review.build_review_packet(attached) == review.build_review_packet(automatic)


def test_independent_manual_snapshot_never_aliases_a_bestplan_packet():
    review = _review()

    assert review.build_review_packet(_manual_target(review)) != (
        review.build_review_packet(_target(review))
    )


def test_review_packet_exposes_the_exact_immutable_artifact_to_both_reviewers():
    review = _review()
    diff_bytes = (
        b"diff --git a/agent/example.py b/agent/example.py\n"
        b"--- a/agent/example.py\n"
        b"+++ b/agent/example.py\n"
        b"@@ -1 +1 @@\n-safe_call()\n+unsafe_call()\n"
    )
    target = _target(
        review,
        diff_sha256=hashlib.sha256(diff_bytes).hexdigest(),
    )
    locator_catalog = {
        "missing-report": {
            "kind": "missing_artifact",
            "identifier": "expected/report.json",
        }
    }
    dispositions = [
        {
            "finding_fingerprint": "f" * 64,
            "status": "fixed",
            "evidence": "The exact next-generation bytes contain the fix.",
        }
    ]
    artifact = review.ReviewArtifact.build(
        target=target,
        diff_bytes=diff_bytes,
        task="Fix the exact stale-pass bug.",
        acceptance=("The focused check passes.",),
        rules=("Fail closed on stale evidence.",),
        issue_locator_catalog=locator_catalog,
        dispositions=dispositions,
    )
    packet_before_mutation = review.build_review_packet(target, artifact=artifact)

    locator_catalog["missing-report"]["identifier"] = "tampered/path"
    dispositions[0]["status"] = "disputed"
    packet = review.build_review_packet(target, artifact=artifact)
    decoded = json.loads(packet)
    git_diff = decoded["artifact"]["git_diff"]

    assert packet == packet_before_mutation
    assert decoded["target_digest"] == target.target_digest
    assert decoded["artifact"]["task"] == "Fix the exact stale-pass bug."
    assert decoded["artifact"]["acceptance"] == ["The focused check passes."]
    assert decoded["artifact"]["rules"] == ["Fail closed on stale evidence."]
    assert git_diff["text"] == diff_bytes.decode("utf-8")
    assert base64.b64decode(git_diff["content_base64"], validate=True) == diff_bytes
    assert git_diff["sha256"] == target.diff_sha256
    assert decoded["artifact"]["issue_locator_catalog"]["missing-report"] == {
        "identifier": "expected/report.json",
        "kind": "missing_artifact",
    }
    assert decoded["artifact"]["dispositions"][0]["status"] == "fixed"


def test_review_packet_keeps_non_utf8_diff_bytes_exact_and_bounded():
    review = _review()
    diff_bytes = b"diff --git a/blob b/blob\nBinary bytes: \xff\x00\n"
    target = _target(
        review,
        diff_sha256=hashlib.sha256(diff_bytes).hexdigest(),
    )

    artifact = _artifact(review, target, diff_bytes=diff_bytes)
    encoded = json.loads(
        review.build_review_packet(target, artifact=artifact)
    )["artifact"]["git_diff"]

    assert base64.b64decode(encoded["content_base64"], validate=True) == diff_bytes
    assert encoded["text"] is None


def test_oversized_final_review_packet_fails_before_either_reviewer_runs():
    review = _review()
    diff_bytes = b"diff --git a/a b/a\n+bounded\n"
    target = _target(
        review,
        diff_sha256=hashlib.sha256(diff_bytes).hexdigest(),
    )
    artifact = _artifact(
        review,
        target,
        diff_bytes=diff_bytes,
        task="x" * review.REVIEW_PACKET_MAX_BYTES,
    )
    calls = []

    with pytest.raises(review.ReviewValidationError, match="packet|size|large"):
        review.run_review_generation(
            target,
            _runtimes(),
            artifact=artifact,
            evidence=_evidence(review),
            reviewer_call=lambda binding, request: calls.append(
                (binding, request)
            ),
        )

    assert calls == []


@pytest.mark.parametrize(
    "runtimes",
    (
        [],
        [_runtimes()[0]],
        [
            *_runtimes(),
            _runtime(
                "extra_reviewer",
                provider="openai-codex",
                model="gpt-5.6-sol",
                model_family="gpt",
            ),
        ],
        [_runtimes()[0], dict(_runtimes()[1], slot="smart_reviewer")],
    ),
)
def test_reviewer_runtime_validation_requires_exactly_the_two_required_slots(runtimes):
    review = _review()

    with pytest.raises(review.ReviewValidationError):
        review.validate_reviewer_runtimes(runtimes)


def test_reviewer_runtime_validation_requires_distinct_model_families():
    review = _review()
    same_family = [
        _runtime(
            "smart_reviewer",
            provider="anthropic",
            model="claude-opus-5",
            model_family="claude",
        ),
        _runtime(
            "code_worker",
            provider="bedrock",
            model="anthropic.claude-fable-5-v1",
            model_family="claude",
        ),
    ]

    with pytest.raises(review.ReviewValidationError, match="famil"):
        review.validate_reviewer_runtimes(same_family)

    bindings = review.validate_reviewer_runtimes(_runtimes())
    assert tuple(binding.slot for binding in bindings) == (
        "smart_reviewer",
        "code_worker",
    )
    assert {binding.model_family for binding in bindings} == {"claude", "qwen"}


def test_strict_verdict_parser_derives_pass_and_blockers_from_findings():
    review = _review()
    target = _target(review)
    evidence = _evidence(review)

    clean = review.parse_review_verdict(
        _verdict_json(target, [_finding(severity="medium")]),
        target=target,
        evidence=evidence,
    )
    blocked = review.parse_review_verdict(
        _verdict_json(
            target,
            [
                _finding(severity="critical"),
                _finding(
                    severity="high",
                    locator={
                        "kind": "missing_artifact",
                        "locator_id": evidence.issue_locator(
                            "missing_artifact", "expected/report.json"
                        ),
                    },
                    title="Required report is missing",
                    observed_failure="The frozen target has no required report.",
                ),
            ],
        ),
        target=target,
        evidence=evidence,
    )

    assert clean.passed is True
    assert clean.blocking_findings == ()
    assert blocked.passed is False
    assert tuple(item.severity for item in blocked.blocking_findings) == (
        "critical",
        "high",
    )


@pytest.mark.parametrize(
    "raw_factory",
    (
        lambda target: "not json",
        lambda target: _verdict_json(target, [], target_digest="f" * 64),
        lambda target: _verdict_json(target, [], integration_oid="e" * 40),
        lambda target: _verdict_json(target, [_finding(severity="urgent")]),
        lambda target: _verdict_json(target, [_finding()], passed=True),
        lambda target: _verdict_json(target, [], passed=False),
        lambda target: _verdict_json(target, [], passed=True),
        lambda target: _verdict_json(target, [], unexpected="field"),
    ),
)
def test_strict_verdict_parser_rejects_malformed_stale_or_inconsistent_output(
    raw_factory,
):
    review = _review()
    target = _target(review)

    with pytest.raises(review.ReviewValidationError):
        review.parse_review_verdict(
            raw_factory(target),
            target=target,
            evidence=_evidence(review),
        )


def test_changed_line_evidence_is_verified_and_host_derives_digest_and_fingerprint():
    review = _review()
    target = _target(review)
    raw_finding = _finding()

    assert "cited_bytes_sha256" not in raw_finding
    assert "fingerprint" not in raw_finding
    first = review.parse_review_verdict(
        _verdict_json(target, [raw_finding]),
        target=target,
        evidence=_evidence(review),
    ).findings[0]
    retry = review.parse_review_verdict(
        _verdict_json(target, [raw_finding]),
        target=target,
        evidence=_evidence(review),
    ).findings[0]

    assert first.path == "agent/example.py"
    assert first.start_line == 2
    assert first.end_line == 2
    assert first.cited_bytes_sha256 == hashlib.sha256(b"unsafe_call()\n").hexdigest()
    assert len(first.fingerprint) == 64
    assert first.fingerprint == retry.fingerprint
    assert first.observed_failure == "The host accepts evidence for different bytes."


@pytest.mark.parametrize(
    "missing_field",
    (
        "severity",
        "locator",
        "title",
        "trigger",
        "observed_failure",
        "blast_radius",
        "reproduction",
    ),
)
def test_finding_requires_every_strict_evidence_field(missing_field):
    review = _review()
    target = _target(review)
    finding = _finding()
    del finding[missing_field]

    with pytest.raises(review.ReviewValidationError):
        review.parse_review_verdict(
            _verdict_json(target, [finding]),
            target=target,
            evidence=_evidence(review),
        )


@pytest.mark.parametrize(
    "missing_field",
    ("kind", "path", "start_line", "end_line", "quoted_evidence"),
)
def test_changed_line_locator_requires_exact_path_range_and_quote(missing_field):
    review = _review()
    target = _target(review)
    finding = _finding()
    locator = dict(finding["locator"])
    del locator[missing_field]
    finding["locator"] = locator

    with pytest.raises(review.ReviewValidationError):
        review.parse_review_verdict(
            _verdict_json(target, [finding]),
            target=target,
            evidence=_evidence(review),
        )


@pytest.mark.parametrize(
    "reproduction",
    (
        {"kind": "command"},
        {"kind": "command", "argv": []},
        {"kind": "not_applicable"},
        {"kind": "not_applicable", "reason": ""},
        {"kind": "guess", "reason": "not objective"},
    ),
)
def test_finding_requires_an_objective_reproduction_or_bounded_na_reason(
    reproduction,
):
    review = _review()
    target = _target(review)
    finding = _finding()
    finding["reproduction"] = reproduction

    with pytest.raises(review.ReviewValidationError):
        review.parse_review_verdict(
            _verdict_json(target, [finding]),
            target=target,
            evidence=_evidence(review),
        )


def test_not_applicable_reproduction_with_a_reason_is_valid():
    review = _review()
    target = _target(review)
    finding = _finding(severity="low")
    finding["reproduction"] = {
        "kind": "not_applicable",
        "reason": "The invariant is proved from the immutable receipt bytes.",
    }

    parsed = review.parse_review_verdict(
        _verdict_json(target, [finding]),
        target=target,
        evidence=_evidence(review),
    )

    assert parsed.findings[0].reproduction.kind == "not_applicable"


def test_changed_line_finding_rejects_stale_quoted_bytes():
    review = _review()
    target = _target(review)
    finding = _finding()
    finding["locator"] = {
        **finding["locator"],
        "quoted_evidence": "safe_call()\n",
    }

    with pytest.raises(review.ReviewValidationError, match="stale|bytes|quote"):
        review.parse_review_verdict(
            _verdict_json(target, [finding]),
            target=target,
            evidence=_evidence(review),
        )


def test_changed_line_finding_rejects_a_range_outside_the_exact_diff():
    review = _review()
    target = _target(review)
    finding = _finding()
    finding["locator"] = {
        "kind": "changed_lines",
        "path": "agent/example.py",
        "start_line": 1,
        "end_line": 1,
        "quoted_evidence": "before\n",
    }

    with pytest.raises(review.ReviewValidationError, match="diff|changed"):
        review.parse_review_verdict(
            _verdict_json(target, [finding]),
            target=target,
            evidence=_evidence(review),
        )


def test_deleted_line_locator_verifies_exact_preimage_bytes_and_membership():
    review = _review()
    target = _target(review)
    finding = _finding(
        locator={
            "kind": "deleted_lines",
            "path": "agent/example.py",
            "before_start_line": 2,
            "before_end_line": 2,
            "quoted_evidence": "removed_guard()\n",
        },
        title="A required guard was deleted",
        observed_failure="The surviving file calls the unsafe path without its guard.",
    )

    parsed = review.parse_review_verdict(
        _verdict_json(target, [finding]),
        target=target,
        evidence=_evidence(review),
    ).findings[0]

    assert parsed.locator.kind == "deleted_lines"
    assert parsed.path == "agent/example.py"
    assert parsed.start_line == 2
    assert parsed.end_line == 2
    assert parsed.cited_bytes_sha256 == hashlib.sha256(
        b"removed_guard()\n"
    ).hexdigest()


@pytest.mark.parametrize(
    "locator",
    (
        {
            "kind": "deleted_lines",
            "path": "agent/example.py",
            "before_start_line": 2,
            "before_end_line": 2,
            "quoted_evidence": "different_guard()\n",
        },
        {
            "kind": "deleted_lines",
            "path": "agent/example.py",
            "before_start_line": 1,
            "before_end_line": 1,
            "quoted_evidence": "before\n",
        },
    ),
)
def test_deleted_line_locator_rejects_stale_or_non_deleted_preimage(locator):
    review = _review()
    target = _target(review)

    with pytest.raises(review.ReviewValidationError, match="stale|deleted|bytes"):
        review.parse_review_verdict(
            _verdict_json(target, [_finding(locator=locator)]),
            target=target,
            evidence=_evidence(review),
        )


def test_changed_line_finding_rejects_a_path_outside_approved_leases():
    review = _review()
    target = _target(review)
    finding = _finding()
    finding["locator"] = {
        "kind": "changed_lines",
        "path": "other/outside.py",
        "start_line": 2,
        "end_line": 2,
        "quoted_evidence": "unsafe_call()\n",
    }

    with pytest.raises(review.ReviewRequiresAuthority):
        review.parse_review_verdict(
            _verdict_json(target, [finding]),
            target=target,
            evidence=_evidence(review, changed_paths=("other/outside.py",)),
        )


def test_reviewer_cannot_supply_host_derived_digest_or_fingerprint():
    review = _review()
    target = _target(review)
    finding = _finding()
    finding["cited_bytes_sha256"] = "f" * 64
    finding["fingerprint"] = "e" * 64

    with pytest.raises(review.ReviewValidationError):
        review.parse_review_verdict(
            _verdict_json(target, [finding]),
            target=target,
            evidence=_evidence(review),
        )


def test_duplicate_host_derived_finding_fingerprints_are_rejected():
    review = _review()
    target = _target(review)
    finding = _finding()

    with pytest.raises(review.ReviewValidationError, match="duplicate|fingerprint"):
        review.parse_review_verdict(
            _verdict_json(target, [finding, dict(finding)]),
            target=target,
            evidence=_evidence(review),
        )


def test_all_tagged_locator_kinds_are_validated_from_frozen_host_evidence():
    review = _review()
    target = _target(review)
    evidence = _evidence(review)
    locators = (
        {
            "kind": "changed_lines",
            "path": "agent/example.py",
            "start_line": 2,
            "end_line": 2,
            "quoted_evidence": "unsafe_call()\n",
        },
        {
            "kind": "missing_artifact",
            "locator_id": evidence.issue_locator(
                "missing_artifact", "expected/report.json"
            ),
        },
        {
            "kind": "deleted_path",
            "locator_id": evidence.issue_locator(
                "deleted_path", "agent/deleted.py"
            ),
        },
        {
            "kind": "unchanged_dependency",
            "path": "agent/dependency.py",
            "start_line": 1,
            "end_line": 1,
            "quoted_evidence": "def shared_guard():\n",
        },
        {
            "kind": "contract_or_receipt",
            "locator_id": evidence.issue_locator(
                "contract_or_receipt", "focused-check"
            ),
            "quoted_evidence": '{"status":"passed","receipt":"exact"}\n',
        },
    )

    for index, locator in enumerate(locators):
        finding = _finding(
            severity="low",
            locator=locator,
            title=f"Concrete finding {index}",
            observed_failure=f"Observed failure {index}",
        )
        finding["reproduction"] = {
            "kind": "not_applicable",
            "reason": "The frozen host evidence is the objective reproduction.",
        }
        parsed = review.parse_review_verdict(
            _verdict_json(target, [finding]),
            target=target,
            evidence=evidence,
        )
        assert parsed.findings[0].locator.kind == locator["kind"]


def test_evidence_validity_does_not_grant_repair_authority():
    review = _review()
    target = _target(review)
    evidence = _evidence(review)
    finding = _finding(
        locator={
            "kind": "missing_artifact",
            "locator_id": evidence.issue_locator(
                "missing_artifact", "expected/report.json"
            ),
        },
        title="Required report is missing",
        observed_failure="The frozen artifact set has no report.",
    )

    parsed = review.parse_review_verdict(
        _verdict_json(target, [finding]),
        target=target,
        evidence=evidence,
    )

    assert parsed.blocking_findings
    assert evidence.approved_lease_paths == ("agent/",)
    assert evidence.repair_authorized_path("expected/report.json") is False


def test_run_review_generation_calls_both_slots_with_identical_no_tool_packet():
    review = _review()
    diff_bytes = (
        b"diff --git a/agent/example.py b/agent/example.py\n"
        b"+unsafe_call()\n"
    )
    target = _target(
        review,
        diff_sha256=hashlib.sha256(diff_bytes).hexdigest(),
    )
    artifact = _artifact(review, target, diff_bytes=diff_bytes)
    bindings = review.validate_reviewer_runtimes(_runtimes())
    calls: list[tuple[object, dict[str, object]]] = []

    def reviewer_call(binding, request):
        calls.append((binding, request))
        findings = [_finding()] if binding.slot == "smart_reviewer" else []
        return _verdict_json(target, findings)

    receipt = review.run_review_generation(
        target,
        bindings,
        artifact=artifact,
        evidence=_evidence(review),
        reviewer_call=reviewer_call,
    )

    assert {binding.slot for binding, _request in calls} == {
        "smart_reviewer",
        "code_worker",
    }
    assert all(request["tools"] == [] for _binding, request in calls)
    assert all("tool_choice" not in request for _binding, request in calls)
    assert calls[0][1]["messages"] == calls[1][1]["messages"]
    assert calls[0][1]["messages"][1]["content"].encode("utf-8") == (
        calls[1][1]["messages"][1]["content"].encode("utf-8")
    )
    assert json.loads(calls[0][1]["messages"][1]["content"])["artifact"][
        "git_diff"
    ]["text"] == diff_bytes.decode("utf-8")
    assert target.target_digest in json.dumps(calls[0][1]["messages"])
    assert receipt.target_digest == target.target_digest
    assert receipt.integration_oid == target.integration_oid
    assert receipt.passed is False
    assert tuple(item.severity for item in receipt.blocking_findings) == ("high",)
    assert {item.slot for item in receipt.reviewer_receipts} == {
        "smart_reviewer",
        "code_worker",
    }


def test_run_review_generation_rejects_a_missing_slot_before_model_dispatch():
    review = _review()
    diff_bytes = b"diff --git a/a b/a\n+exact\n"
    target = _target(
        review,
        diff_sha256=hashlib.sha256(diff_bytes).hexdigest(),
    )
    calls = []

    def reviewer_call(binding, request):
        calls.append((binding, request))
        return _verdict_json(target, [])

    with pytest.raises(review.ReviewValidationError):
        review.run_review_generation(
            target,
            review.validate_reviewer_runtimes(_runtimes())[:1],
            artifact=_artifact(review, target, diff_bytes=diff_bytes),
            evidence=_evidence(review),
            reviewer_call=reviewer_call,
        )

    assert calls == []


def test_review_journal_is_append_only_and_exact_retries_are_idempotent(tmp_path):
    review = _review()
    target = _target(review)
    journal = review.ReviewJournal(tmp_path / "state.db")
    kwargs = {
        "plan_id": target.plan_id,
        "generation": target.generation,
        "operation_id": "generation-0-start",
        "kind": "generation_started",
        "target_digest": target.target_digest,
        "integration_oid": target.integration_oid,
        "payload": {"target": json.loads(target.canonical_json)},
    }

    first = journal.append(**kwargs)
    retry = journal.append(**kwargs)
    second = journal.append(
        plan_id=target.plan_id,
        generation=target.generation,
        operation_id="generation-0-reviewer-smart",
        kind="reviewer_receipt",
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        payload={"slot": "smart_reviewer", "passed": True},
    )

    assert retry == first
    assert first.event_seq == 1
    assert second.event_seq == 2
    assert second.previous_event_digest == first.event_digest
    assert first.payload_json == json.dumps(
        kwargs["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert first.payload_digest == hashlib.sha256(
        b"hermes.bestplan.review-event-payload.v1\0"
        + first.payload_json.encode("utf-8")
    ).hexdigest()

    with pytest.raises(review.ReviewJournalConflict):
        journal.append(**{**kwargs, "payload": {"target": "changed"}})


def test_review_journal_latest_pass_is_bound_to_the_exact_target(tmp_path):
    review = _review()
    first_target = _target(review)
    next_target = _target(
        review,
        generation=1,
        integration_oid="b" * 40,
        integration_tree_oid="c" * 40,
        integration_receipt_digest="d" * 64,
        check_receipt_digest="e" * 64,
        diff_sha256="f" * 64,
    )
    journal = review.ReviewJournal(tmp_path / "state.db")
    first_pass = journal.append(
        plan_id=first_target.plan_id,
        generation=first_target.generation,
        operation_id="generation-0-pass",
        kind="review_pass",
        target_digest=first_target.target_digest,
        integration_oid=first_target.integration_oid,
        payload={"review_receipt_digest": "b" * 64},
    )

    assert journal.latest_pass(first_target.plan_id, first_target.target_digest) == first_pass
    assert journal.latest_pass(next_target.plan_id, next_target.target_digest) is None

    next_pass = journal.append(
        plan_id=next_target.plan_id,
        generation=next_target.generation,
        operation_id="generation-1-pass",
        kind="review_pass",
        target_digest=next_target.target_digest,
        integration_oid=next_target.integration_oid,
        payload={"review_receipt_digest": "c" * 64},
    )

    reopened = review.ReviewJournal(tmp_path / "state.db")
    assert reopened.latest_pass(first_target.plan_id, first_target.target_digest) == first_pass
    assert reopened.latest_pass(next_target.plan_id, next_target.target_digest) == next_pass


def _create_review_job(store, target):
    return store.create_job(
        job_id="review-job-1",
        source_kind=target.source_kind,
        source_id=target.plan_id,
        target_digest=target.target_digest,
        policy_digest=target.policy_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
    )


def _record_slot(
    store,
    target,
    claim,
    slot,
    *,
    passed=True,
    suffix="0",
):
    return store.record_reviewer_receipt(
        job_id="review-job-1",
        generation=target.generation,
        slot=slot,
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        output_digest=hashlib.sha256(f"output-{slot}-{suffix}".encode()).hexdigest(),
        verdict_digest=hashlib.sha256(
            f"verdict-{slot}-{suffix}".encode()
        ).hexdigest(),
        passed=passed,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id=f"receipt-{slot}-{suffix}",
    )


def _record_exact_pass(store, target, claim, *, suffix="0"):
    for slot in ("smart_reviewer", "code_worker"):
        _record_slot(store, target, claim, slot, suffix=suffix)
    receipt_digest = hashlib.sha256(
        f"review-generation-{suffix}".encode()
    ).hexdigest()
    stored = store.record_generation_pass(
        job_id="review-job-1",
        generation=target.generation,
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        check_receipt_digest=target.check_receipt_digest,
        review_receipt_digest=receipt_digest,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id=f"generation-pass-{suffix}",
    )
    return stored, receipt_digest


def test_review_store_claim_is_cas_and_expired_lease_reclaim_increments_fence(
    tmp_path,
):
    review = _review()
    store = review.ReviewStore(tmp_path / "state.db")
    target = _target(review)
    _create_review_job(store, target)

    first = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-a",
        now_ns=1_000,
        lease_duration_ns=100,
        expected_fencing_token=0,
    )
    assert first.owner_id == "worker-a"
    assert first.fencing_token == 1
    assert first.lease_expires_at_ns == 1_100

    with pytest.raises(review.ReviewLeaseConflict):
        store.claim_job(
            job_id="review-job-1",
            owner_id="worker-b",
            now_ns=1_050,
            lease_duration_ns=100,
            expected_fencing_token=1,
        )

    reclaimed = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-b",
        now_ns=1_101,
        lease_duration_ns=100,
        expected_fencing_token=1,
    )
    assert reclaimed.owner_id == "worker-b"
    assert reclaimed.fencing_token == 2
    assert reclaimed.lease_expires_at_ns == 1_201

    with pytest.raises(review.ReviewLeaseConflict):
        store.begin_generation(
            job_id="review-job-1",
            generation=0,
            target=target,
            owner_id="worker-a",
            fencing_token=1,
            operation_id="generation-0",
        )


def test_review_store_lease_renewal_keeps_fence_and_rejects_stale_token(tmp_path):
    review = _review()
    store = review.ReviewStore(tmp_path / "state.db")
    target = _target(review)
    _create_review_job(store, target)
    claim = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-a",
        now_ns=2_000,
        lease_duration_ns=100,
        expected_fencing_token=0,
    )

    renewed = store.renew_lease(
        job_id="review-job-1",
        owner_id="worker-a",
        fencing_token=claim.fencing_token,
        now_ns=2_050,
        lease_duration_ns=200,
    )
    assert renewed.fencing_token == claim.fencing_token
    assert renewed.lease_expires_at_ns == 2_250

    with pytest.raises(review.ReviewLeaseConflict):
        store.renew_lease(
            job_id="review-job-1",
            owner_id="worker-a",
            fencing_token=claim.fencing_token - 1,
            now_ns=2_060,
            lease_duration_ns=200,
        )


def test_review_store_generation_state_is_durable_and_every_event_has_fence(
    tmp_path,
):
    review = _review()
    store = review.ReviewStore(tmp_path / "state.db")
    target = _target(review)
    _create_review_job(store, target)
    claim = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-a",
        now_ns=3_000,
        lease_duration_ns=1_000,
        expected_fencing_token=0,
    )

    generation = store.begin_generation(
        job_id="review-job-1",
        generation=0,
        target=target,
        owner_id="worker-a",
        fencing_token=claim.fencing_token,
        operation_id="generation-0",
    )

    reopened = review.ReviewStore(tmp_path / "state.db")
    job = reopened.get_job("review-job-1")
    events = reopened.list_events("review-job-1")
    assert generation.generation == 0
    assert generation.state == "reviewing"
    assert generation.target_digest == target.target_digest
    assert job.current_generation == 0
    assert job.state == "reviewing"
    assert events[-1].kind == "generation_started"
    assert all(event.fencing_token == claim.fencing_token for event in events)


def test_review_store_cancellation_is_persisted_before_children_are_signalled(
    tmp_path,
):
    review = _review()
    store = review.ReviewStore(tmp_path / "state.db")
    target = _target(review)
    _create_review_job(store, target)
    claim = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-a",
        now_ns=4_000,
        lease_duration_ns=1_000,
        expected_fencing_token=0,
    )
    observed_states = []

    def signal_children():
        observed_states.append(
            review.ReviewStore(tmp_path / "state.db")
            .get_job("review-job-1")
            .state
        )

    cancelled = store.request_cancel(
        job_id="review-job-1",
        owner_id="worker-a",
        fencing_token=claim.fencing_token,
        operation_id="cancel-request-1",
        signal_children=signal_children,
    )

    assert observed_states == ["cancel_requested"]
    assert cancelled.state == "cancel_requested"
    assert review.ReviewStore(tmp_path / "state.db").get_job(
        "review-job-1"
    ).cancel_requested is True


def test_review_store_reviewer_receipts_are_idempotent_per_exact_slot(tmp_path):
    review = _review()
    store = review.ReviewStore(tmp_path / "state.db")
    target = _target(review)
    _create_review_job(store, target)
    claim = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-a",
        now_ns=5_000,
        lease_duration_ns=1_000,
        expected_fencing_token=0,
    )
    store.begin_generation(
        job_id="review-job-1",
        generation=0,
        target=target,
        owner_id="worker-a",
        fencing_token=claim.fencing_token,
        operation_id="generation-0",
    )
    kwargs = {
        "job_id": "review-job-1",
        "generation": 0,
        "slot": "smart_reviewer",
        "target_digest": target.target_digest,
        "integration_oid": target.integration_oid,
        "output_digest": "c" * 64,
        "verdict_digest": "d" * 64,
        "passed": True,
        "owner_id": "worker-a",
        "fencing_token": claim.fencing_token,
        "operation_id": "receipt-smart-0",
    }

    first = store.record_reviewer_receipt(**kwargs)
    retry = store.record_reviewer_receipt(**kwargs)
    assert retry == first

    with pytest.raises(review.ReviewStoreConflict):
        store.record_reviewer_receipt(
            **{**kwargs, "verdict_digest": "e" * 64}
        )


def test_review_store_crash_resume_returns_only_the_missing_slot(tmp_path):
    review = _review()
    store = review.ReviewStore(tmp_path / "state.db")
    target = _target(review)
    _create_review_job(store, target)
    first_claim = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-a",
        now_ns=6_000,
        lease_duration_ns=100,
        expected_fencing_token=0,
    )
    store.begin_generation(
        job_id="review-job-1",
        generation=0,
        target=target,
        owner_id="worker-a",
        fencing_token=first_claim.fencing_token,
        operation_id="generation-0",
    )
    store.record_reviewer_receipt(
        job_id="review-job-1",
        generation=0,
        slot="smart_reviewer",
        target_digest=target.target_digest,
        integration_oid=target.integration_oid,
        output_digest="c" * 64,
        verdict_digest="d" * 64,
        passed=True,
        owner_id="worker-a",
        fencing_token=first_claim.fencing_token,
        operation_id="receipt-smart-0",
    )

    restarted = review.ReviewStore(tmp_path / "state.db")
    next_claim = restarted.claim_job(
        job_id="review-job-1",
        owner_id="worker-b",
        now_ns=6_101,
        lease_duration_ns=100,
        expected_fencing_token=first_claim.fencing_token,
    )
    resume = restarted.resume_job(
        job_id="review-job-1",
        owner_id="worker-b",
        fencing_token=next_claim.fencing_token,
    )

    assert resume.generation == 0
    assert resume.target_digest == target.target_digest
    assert tuple(item.slot for item in resume.adopted_reviewer_receipts) == (
        "smart_reviewer",
    )
    assert resume.missing_reviewer_slots == ("code_worker",)


def test_review_store_rejects_an_event_without_the_current_fencing_token(tmp_path):
    review = _review()
    store = review.ReviewStore(tmp_path / "state.db")
    target = _target(review)
    _create_review_job(store, target)
    claim = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-a",
        now_ns=7_000,
        lease_duration_ns=100,
        expected_fencing_token=0,
    )

    with pytest.raises((TypeError, review.ReviewLeaseConflict)):
        store.append_event(
            job_id="review-job-1",
            generation=0,
            owner_id="worker-a",
            operation_id="missing-fence",
            kind="reviewer_attempt",
            target_digest=target.target_digest,
            payload={"slot": "smart_reviewer"},
        )

    with pytest.raises(review.ReviewLeaseConflict):
        store.append_event(
            job_id="review-job-1",
            generation=0,
            owner_id="worker-a",
            fencing_token=claim.fencing_token + 1,
            operation_id="wrong-fence",
            kind="reviewer_attempt",
            target_digest=target.target_digest,
            payload={"slot": "smart_reviewer"},
        )


def test_review_store_pass_requires_exactly_two_current_passing_slots(tmp_path):
    review = _review()
    store = review.ReviewStore(tmp_path / "state.db")
    target = _target(review)
    _create_review_job(store, target)
    claim = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-a",
        now_ns=8_000,
        lease_duration_ns=1_000,
        expected_fencing_token=0,
    )
    store.begin_generation(
        job_id="review-job-1",
        generation=0,
        target=target,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id="generation-0",
    )
    _record_slot(store, target, claim, "smart_reviewer")

    with pytest.raises(review.ReviewStoreConflict, match="slot|reviewer|pass"):
        store.record_generation_pass(
            job_id="review-job-1",
            generation=0,
            target_digest=target.target_digest,
            integration_oid=target.integration_oid,
            check_receipt_digest=target.check_receipt_digest,
            review_receipt_digest="e" * 64,
            owner_id=claim.owner_id,
            fencing_token=claim.fencing_token,
            operation_id="pass-one-slot",
        )

    _record_slot(
        store,
        target,
        claim,
        "code_worker",
        passed=False,
    )
    with pytest.raises(review.ReviewStoreConflict, match="slot|reviewer|pass"):
        store.record_generation_pass(
            job_id="review-job-1",
            generation=0,
            target_digest=target.target_digest,
            integration_oid=target.integration_oid,
            check_receipt_digest=target.check_receipt_digest,
            review_receipt_digest="e" * 64,
            owner_id=claim.owner_id,
            fencing_token=claim.fencing_token,
            operation_id="pass-blocked-slot",
        )


def test_review_store_latest_exact_pass_is_invalidated_by_a_later_generation(
    tmp_path,
):
    review = _review()
    store = review.ReviewStore(tmp_path / "state.db")
    first_target = _target(review)
    _create_review_job(store, first_target)
    claim = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-a",
        now_ns=9_000,
        lease_duration_ns=10_000,
        expected_fencing_token=0,
    )
    store.begin_generation(
        job_id="review-job-1",
        generation=0,
        target=first_target,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id="generation-0",
    )
    stored_pass, receipt_digest = _record_exact_pass(
        store, first_target, claim
    )

    assert stored_pass.review_receipt_digest == receipt_digest
    assert store.latest_exact_pass(
        target=first_target,
        review_receipt_digest=receipt_digest,
    ) == stored_pass

    next_target = _target(
        review,
        generation=1,
        integration_oid="b" * 40,
        integration_tree_oid="c" * 40,
        integration_ref="refs/hermes-bestplan-integrations/plan-review-1/1",
        integration_receipt_digest="d" * 64,
        check_receipt_digest="e" * 64,
        diff_sha256="f" * 64,
    )
    store.begin_generation(
        job_id="review-job-1",
        generation=1,
        target=next_target,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id="generation-1",
    )

    assert store.latest_exact_pass(
        target=first_target,
        review_receipt_digest=receipt_digest,
    ) is None
    assert store.latest_exact_pass(
        target=next_target,
        review_receipt_digest=receipt_digest,
    ) is None


def test_review_store_rejects_a_pass_after_its_fencing_token_is_reclaimed(
    tmp_path,
):
    review = _review()
    store = review.ReviewStore(tmp_path / "state.db")
    target = _target(review)
    _create_review_job(store, target)
    first_claim = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-a",
        now_ns=10_000,
        lease_duration_ns=100,
        expected_fencing_token=0,
    )
    store.begin_generation(
        job_id="review-job-1",
        generation=0,
        target=target,
        owner_id=first_claim.owner_id,
        fencing_token=first_claim.fencing_token,
        operation_id="generation-0",
    )
    _stored_pass, receipt_digest = _record_exact_pass(
        store, target, first_claim
    )

    reclaimed = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-b",
        now_ns=10_101,
        lease_duration_ns=100,
        expected_fencing_token=first_claim.fencing_token,
    )

    assert reclaimed.fencing_token > first_claim.fencing_token
    assert store.latest_exact_pass(
        target=target,
        review_receipt_digest=receipt_digest,
    ) is None


def test_review_store_sql_receipts_and_target_identity_are_immutable(tmp_path):
    review = _review()
    path = tmp_path / "state.db"
    store = review.ReviewStore(path)
    target = _target(review)
    _create_review_job(store, target)
    claim = store.claim_job(
        job_id="review-job-1",
        owner_id="worker-a",
        now_ns=10_000,
        lease_duration_ns=1_000,
        expected_fencing_token=0,
    )
    store.begin_generation(
        job_id="review-job-1",
        generation=0,
        target=target,
        owner_id=claim.owner_id,
        fencing_token=claim.fencing_token,
        operation_id="generation-0",
    )
    _record_exact_pass(store, target, claim)

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE review_reviewer_receipts SET verdict_digest=? "
                "WHERE job_id='review-job-1'",
                ("0" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE review_generations SET target_digest=? "
                "WHERE job_id='review-job-1' AND generation=0",
                ("0" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM review_pass_receipts "
                "WHERE job_id='review-job-1' AND generation=0"
            )
