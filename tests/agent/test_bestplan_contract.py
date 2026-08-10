from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.bestplan_authority_client import AuthorityUnavailable
from agent.bestplan_contract import (
    AmbiguousEnrollmentError,
    BlockingReview,
    BoundCommand,
    ContractValidationError,
    ControllerIdentity,
    EnrolledRepository,
    Enrollment,
    LiveTarget,
    MalformedEnrollmentError,
    PinnedInput,
    Publication,
    RollbackTarget,
    approval_digest,
    build_execution_contract,
    canonical_json,
    contract_digest,
    enrollment_from_dict,
    enrollment_to_dict,
    normalize_git_push_identity,
    render_execution_contract,
    resolve_matching_enrollment,
    source_snapshot_digest,
    source_snapshot_from_json,
    source_snapshot_json,
    validate_execution_contract,
)
from agent.bestplan_source import (
    IndexEntry,
    IndexFlags,
    ProtectedManifest,
    ProtectedPath,
    RepoIdentity,
    SourceSnapshot,
)
from agent.bestplan_state import (
    BESTPLAN_ENVELOPE_END,
    BESTPLAN_ENVELOPE_START,
    BestplanStore,
    PlanState,
    _render_authoritative_manifest,
    capture_bestplan_response,
    try_resolve_go,
)
from agent.execution_plan import compile_execution_plan
from hermes_cli.config_defaults import DEFAULT_CONFIG


def _repo(*, suffix: str = "") -> RepoIdentity:
    worktree = str(Path(f"/tmp/work{suffix}").resolve())
    common = str(Path(f"/tmp/repo{suffix}/.git").resolve())
    return RepoIdentity(
        workspace=worktree,
        workspace_raw=worktree.encode(),
        worktree=worktree,
        worktree_raw=worktree.encode(),
        git_dir=common,
        git_dir_raw=common.encode(),
        common_dir=common,
        common_dir_raw=common.encode(),
        common_dir_device=11,
        common_dir_inode=22,
        object_format="sha1",
        repository_id=f"repo-id{suffix}",
    )


def _snapshot(repo: RepoIdentity | None = None) -> SourceSnapshot:
    repo = repo or _repo()
    protected = ProtectedManifest(
        index_entries=(IndexEntry(b"tracked.bin", 0o100644, "1" * 40, 0),),
        index_flags=(
            IndexFlags(b"tracked.bin", b"H ", b"", False, False, False, False),
        ),
        worktree_entries=(
            ProtectedPath(
                path=b"raw-\xff-link",
                tracked=False,
                kind="symlink",
                mode=0o120777,
                size=None,
                content_sha256=None,
                symlink_target=b"target-\xfe",
                git_oid=None,
            ),
        ),
        protected_paths=(b"raw-\xff-link", b"tracked.bin"),
        staged_diff_sha256="2" * 64,
        unstaged_diff_sha256="3" * 64,
        digest="4" * 64,
    )
    return SourceSnapshot(
        repo=repo,
        head_symbolic=True,
        head_ref=b"refs/heads/main",
        head_raw=b"ref: refs/heads/main\n",
        head_oid="5" * 40,
        tree_oid="6" * 40,
        protected_manifest=protected,
        capture_implementation_sha256="7" * 64,
        fingerprint="8" * 64,
    )


def _command(identifier: str = "focused-tests") -> BoundCommand:
    return BoundCommand(
        identifier=identifier,
        executable="/usr/bin/python3",
        executable_sha256="9" * 64,
        argv=("-m", "pytest", "-q"),
        logical_cwd="integration",
        env=(("PYTHONHASHSEED", "0"),),
        inputs=(PinnedInput("pyproject.toml", "a" * 64),),
        cache=(PinnedInput(".cache/pytest", "b" * 64),),
        timeout_seconds=600,
        network_allowlist=(),
    )


def _enrollment(
    repo: RepoIdentity | None = None,
    *,
    promotion_mode: str = "auto_live",
    push_url: str = "https://publisher:secret@GitHub.COM:443/org/repo.git?token=x",
) -> Enrollment:
    repo = repo or _repo()
    enrolled_repo = EnrolledRepository.from_repo_identity(repo)
    health = _command("health")
    canary = _command("canary")
    rollback_command = _command("rollback")
    publication = Publication(
        repository_id=repo.repository_id,
        remote_name="origin",
        push_url=push_url,
        remote_ref="refs/heads/main",
        observed_oid="c" * (40 if repo.object_format == "sha1" else 64),
    )
    rollback = RollbackTarget(
        repository_id=repo.repository_id,
        selector="/var/db/hermes/releases/current",
        service="com.nous.hermes.gateway",
        command=rollback_command,
    )
    live = LiveTarget(
        repository_id=repo.repository_id,
        adapter="launchd",
        target_id="gateway-primary",
        service="com.nous.hermes.gateway",
        activation=_command("activate"),
        health=health,
        canary=canary,
        rollback=rollback,
    )
    controller = ControllerIdentity(
        repository_id=repo.repository_id,
        controller_id="controller-c0",
        release_oid="d" * (40 if repo.object_format == "sha1" else 64),
        artifact_sha256="e" * 64,
    )
    return Enrollment(
        reference="prod-gateway",
        enrollment_id="enrollment-1",
        revision=7,
        epoch="epoch-3",
        repository=enrolled_repo,
        source_policy="head_only",
        capture_budget_seconds=30,
        local_ref="refs/heads/main",
        publication=publication,
        commands=(_command("focused-tests"), _command("full-tests")),
        review=BlockingReview(
            lane="smart_reviewer",
            command=_command("review"),
            blocking_severities=("critical", "high"),
        ),
        live_targets=(live,),
        controller=controller,
        promotion_mode=promotion_mode,
    )


def _manifest(
    *,
    review_only: bool = False,
    text: str = "ordinary",
    workspace: str = "/tmp/work",
) -> dict:
    return {
        "version": 1,
        "mode": "sota" if review_only else "delegate",
        "risk": "high" if review_only else "low",
        "slices": [
            {
                "id": "work",
                "kind": "review" if review_only else "implement",
                "goal": text,
                "depends_on": [],
                "capability": "frontier_review" if review_only else "fast_fallback",
                "workspace": workspace,
                "allowed_paths": [] if review_only else ["agent/"],
                "read_only": review_only,
                "expected_artifacts": ["review.md" if review_only else "agent/change.py"],
                "acceptance": ["checks pass"],
            }
        ],
        "merge_policy": text,
        "stop_condition": "acceptance passes",
        "escalation_predicates": ["review_required"],
    }


def _plan(
    *,
    review_only: bool = False,
    text: str = "ordinary",
    workspace: str = "/tmp/work",
):
    return compile_execution_plan(
        _manifest(review_only=review_only, text=text, workspace=workspace)
    )


def _config(reference: str = "prod-gateway", endpoint: str = "unix:///ignored"):
    return {
        "bestplan_promotion": {
            "authority_endpoint": endpoint,
            "enrollment_ref": reference,
        }
    }


class _Authority:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def lookup_enrollment(self, repo_identity):
        self.calls.append(repo_identity)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def _envelope(manifest: dict | None = None) -> str:
    payload = {"version": 1, "manifest": manifest or _manifest()}
    return (
        f"{BESTPLAN_ENVELOPE_START}\n"
        f"{json.dumps(payload, sort_keys=True)}\n"
        f"{BESTPLAN_ENVELOPE_END}"
    )


def test_source_snapshot_json_round_trips_every_raw_path_and_target_byte():
    snapshot = _snapshot()
    encoded = source_snapshot_json(snapshot)
    decoded = source_snapshot_from_json(encoded)

    assert decoded == snapshot
    assert decoded.repo.worktree_raw == snapshot.repo.worktree_raw
    assert decoded.protected_manifest.protected_paths[0] == b"raw-\xff-link"
    assert decoded.protected_manifest.worktree_entries[0].symlink_target == b"target-\xfe"
    assert source_snapshot_digest(decoded) == source_snapshot_digest(snapshot)


def test_canonical_json_rejects_floats_and_non_string_keys():
    with pytest.raises(ContractValidationError, match="float"):
        canonical_json({"timeout": 1.5})
    with pytest.raises(ContractValidationError, match="string keys"):
        canonical_json({1: "value"})


def test_enrollment_parser_rejects_unknown_and_malformed_fields():
    raw = enrollment_to_dict(_enrollment())
    raw["unexpected"] = "field"
    with pytest.raises(MalformedEnrollmentError, match="unknown"):
        enrollment_from_dict(raw)

    raw = enrollment_to_dict(_enrollment())
    raw["commands"][0]["timeout_seconds"] = 1.25
    with pytest.raises(MalformedEnrollmentError, match="timeout"):
        enrollment_from_dict(raw)


def test_exact_unmatched_unavailable_ambiguous_and_malformed_resolution():
    repo = _repo()
    enrollment = _enrollment(repo)
    exact = _Authority(enrollment)
    assert resolve_matching_enrollment(_config(), repo, exact) == enrollment
    assert exact.calls == [repo]

    mismatch = replace(
        enrollment,
        repository=replace(enrollment.repository, common_dir_inode=999),
    )
    assert resolve_matching_enrollment(_config(), repo, _Authority(mismatch)) is None
    assert resolve_matching_enrollment(_config("another"), repo, exact) is None
    assert resolve_matching_enrollment({}, repo, exact) is None

    assert resolve_matching_enrollment(
        _config(), repo, _Authority(AuthorityUnavailable("offline"))
    ) is None

    with pytest.raises(AmbiguousEnrollmentError, match="ambiguous"):
        resolve_matching_enrollment(
            _config(), repo, _Authority(AmbiguousEnrollmentError("ambiguous enrollment"))
        )
    with pytest.raises(MalformedEnrollmentError, match="unsupported"):
        resolve_matching_enrollment(
            _config(), repo, _Authority((enrollment, enrollment))
        )

    malformed = enrollment_to_dict(enrollment)
    malformed["unexpected"] = True
    with pytest.raises(MalformedEnrollmentError, match="unsupported type dict"):
        resolve_matching_enrollment(_config(), repo, _Authority(malformed))


@pytest.mark.parametrize(
    "config",
    [
        {"bestplan_promotion": {"authority_endpoint": "", "enrollment_ref": "prod-gateway"}},
        {
            "bestplan_promotion": {
                "authority_endpoint": "unix:///authority.sock",
                "enrollment_ref": "",
            }
        },
    ],
)
def test_one_sided_authority_config_stays_candidate_only_without_lookup(config):
    authority = _Authority(_enrollment())
    assert resolve_matching_enrollment(config, _repo(), authority) is None
    assert authority.calls == []


def test_invalid_nonempty_endpoint_is_malformed_even_without_enrollment_reference():
    authority = _Authority(_enrollment())
    with pytest.raises(MalformedEnrollmentError, match="https or unix"):
        resolve_matching_enrollment(
            {
                "bestplan_promotion": {
                    "authority_endpoint": "http://authority.example.test/v1",
                    "enrollment_ref": "",
                }
            },
            _repo(),
            authority,
        )
    assert authority.calls == []


@pytest.mark.parametrize("field", ["worktree", "common_dir", "device", "inode", "format", "id"])
def test_repository_match_binds_raw_identity_and_device_inode(field):
    repo = _repo()
    if field == "worktree":
        changed_repo = replace(repo, worktree="/tmp/other", worktree_raw=b"/tmp/other")
    elif field == "common_dir":
        changed_repo = replace(
            repo, common_dir="/tmp/other/.git", common_dir_raw=b"/tmp/other/.git"
        )
    elif field == "device":
        changed_repo = replace(repo, common_dir_device=999)
    elif field == "inode":
        changed_repo = replace(repo, common_dir_inode=999)
    elif field == "format":
        changed_repo = replace(repo, object_format="sha256")
    else:
        changed_repo = replace(repo, repository_id="different-repo")
    assert resolve_matching_enrollment(
        _config(), repo, _Authority(_enrollment(changed_repo))
    ) is None


def test_enrollment_rejects_non_main_multiple_live_cross_repo_and_missing_gates():
    enrollment = _enrollment()
    with pytest.raises(ContractValidationError, match="refs/heads/main"):
        replace(enrollment, local_ref="refs/heads/release")
    with pytest.raises(ContractValidationError, match="exactly one live target"):
        replace(enrollment, live_targets=(enrollment.live_targets[0],) * 2)
    with pytest.raises(ContractValidationError, match="check"):
        replace(enrollment, commands=())
    with pytest.raises(ContractValidationError, match="repository"):
        replace(
            enrollment,
            publication=replace(enrollment.publication, repository_id="other"),
        )
    with pytest.raises(ContractValidationError, match="controller"):
        replace(
            enrollment,
            controller=replace(enrollment.controller, repository_id="other"),
        )


def test_bound_commands_reject_secret_env_unsorted_env_and_relative_executable():
    command = _command()
    with pytest.raises(ContractValidationError, match="absolute"):
        replace(command, executable="python3")
    with pytest.raises(ContractValidationError, match="sorted"):
        replace(command, env=(("Z", "1"), ("A", "2")))
    with pytest.raises(ContractValidationError, match="secret"):
        replace(command, env=(("API_TOKEN", "not-allowed"),))
    with pytest.raises(ContractValidationError, match="credential"):
        replace(command, argv=("--api-key", "not-allowed"))
    with pytest.raises(ContractValidationError, match="unique"):
        replace(
            command,
            inputs=(
                PinnedInput("same", "a" * 64),
                PinnedInput("same", "b" * 64),
            ),
        )


def test_push_identity_is_credential_free_and_credential_variation_is_irrelevant():
    first = normalize_git_push_identity(
        "https://publisher:one@GitHub.COM:443/org/repo.git?token=one"
    )
    second = normalize_git_push_identity(
        "https://another:two@github.com/org/repo.git#credential-two"
    )
    assert first == second == "https://github.com/org/repo.git"
    assert "publisher" not in first
    assert "token" not in first

    first_contract = build_execution_contract(_plan(), _snapshot(), _enrollment())
    second_contract = build_execution_contract(
        _plan(),
        _snapshot(),
        _enrollment(push_url="https://different:credential@github.com/org/repo.git"),
    )
    assert first_contract == second_contract
    assert contract_digest(first_contract) == contract_digest(second_contract)
    with pytest.raises(ContractValidationError, match="absolute"):
        normalize_git_push_identity("./relative-repository")
    local_one = normalize_git_push_identity("/tmp/repository?one")
    local_two = normalize_git_push_identity("/tmp/repository?two")
    assert local_one != local_two
    assert "%3Fone" in local_one
    assert normalize_git_push_identity("git@github.com:org/repo?one.git") != normalize_git_push_identity(
        "git@github.com:org/repo?two.git"
    )
    assert normalize_git_push_identity(
        "alice@example.test:org/repo.git"
    ) != normalize_git_push_identity("bob@example.test:org/repo.git")
    assert normalize_git_push_identity(
        "ssh://alice:one@example.test/org/repo.git"
    ) == normalize_git_push_identity("ssh://alice:two@example.test/org/repo.git")
    assert normalize_git_push_identity(
        "ssh://alice@example.test/org/repo.git"
    ) != normalize_git_push_identity("ssh://bob@example.test/org/repo.git")
    assert normalize_git_push_identity(
        "alice@example.test:org/repo.git"
    ) != normalize_git_push_identity("ssh://alice@example.test/org/repo.git")
    assert normalize_git_push_identity(
        "/tmp/repository with space"
    ) == normalize_git_push_identity("file:///tmp/repository with space")
    assert normalize_git_push_identity(
        "/tmp/symlink/../repository"
    ) != normalize_git_push_identity("/tmp/repository")
    with pytest.raises(ContractValidationError, match="query"):
        normalize_git_push_identity("https://github.com/org/repo.git?repository=other")


def test_authority_endpoint_rejects_embedded_credentials_but_does_not_bind_contract():
    repo = _repo()
    authority = _Authority(_enrollment(repo))
    first = resolve_matching_enrollment(
        _config(endpoint="unix:///var/run/hermes.sock"), repo, authority
    )
    second = resolve_matching_enrollment(
        _config(endpoint="https://authority.example.test/v1"), repo, authority
    )
    assert build_execution_contract(_plan(), _snapshot(repo), first) == build_execution_contract(
        _plan(), _snapshot(repo), second
    )
    with pytest.raises(MalformedEnrollmentError, match="credential"):
        resolve_matching_enrollment(
            _config(endpoint="https://user:secret@authority.example.test/v1"),
            repo,
            authority,
        )


@pytest.mark.parametrize(
    "endpoint",
    ["https://authority.example.test:bad/v1", "https://authority.example.test:70000/v1"],
)
def test_authority_endpoint_rejects_malformed_ports(endpoint):
    with pytest.raises(MalformedEnrollmentError, match="malformed"):
        resolve_matching_enrollment(
            _config(endpoint=endpoint), _repo(), _Authority(_enrollment())
        )


def test_contract_has_exact_schema_and_binds_every_irreversible_leaf():
    contract = build_execution_contract(_plan(), _snapshot(), _enrollment())
    assert set(contract) == {
        "schema",
        "version",
        "execution_protocol",
        "enrollment",
        "repository",
        "source",
        "publication",
        "commands",
        "review",
        "live_target",
        "controller",
        "promotion_mode",
    }
    assert contract["execution_protocol"] == 2
    assert contract["source"]["local_ref"] == "refs/heads/main"
    assert contract["source"]["local_main_oid"] == _snapshot().head_oid
    assert contract["publication"]["remote_ref"] == "refs/heads/main"
    assert contract["publication"]["remote_identity"] == "https://github.com/org/repo.git"
    assert contract["source"]["protected_digest"] == "4" * 64
    assert contract["promotion_mode"] == "auto_live"
    assert contract["live_target"]["activation"]["identifier"] == "activate"

    def raw_contract_digest(value):
        return hashlib.sha256(
            b"hermes.bestplan.promotion-contract.v2\0"
            + canonical_json(value).encode("utf-8")
        ).hexdigest()

    baseline = raw_contract_digest(contract)
    assert contract_digest(contract) == baseline

    def scalar_paths(value, prefix=()):
        if isinstance(value, dict):
            for key, item in value.items():
                yield from scalar_paths(item, prefix + (key,))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from scalar_paths(item, prefix + (index,))
        else:
            yield prefix

    def mutate(value, path):
        copied = json.loads(json.dumps(value))
        parent = copied
        for key in path[:-1]:
            parent = parent[key]
        leaf = parent[path[-1]]
        if isinstance(leaf, bool):
            parent[path[-1]] = not leaf
        elif isinstance(leaf, int):
            parent[path[-1]] = leaf + 1
        elif leaf is None:
            parent[path[-1]] = "changed"
        else:
            parent[path[-1]] = str(leaf) + "-changed"
        return copied

    for path in scalar_paths(contract):
        assert raw_contract_digest(mutate(contract, path)) != baseline, path

    unknown = dict(contract)
    unknown["extra"] = True
    with pytest.raises(ContractValidationError, match="unknown"):
        validate_execution_contract(unknown)


def test_contract_rejects_controller_mismatch():
    enrollment = _enrollment()
    with pytest.raises(ContractValidationError, match="controller"):
        build_execution_contract(
            _plan(),
            _snapshot(),
            enrollment,
            replace(enrollment.controller, controller_id="other-controller"),
        )


def test_review_only_or_descriptive_plan_text_cannot_enable_auto_live():
    review_contract = build_execution_contract(
        _plan(review_only=True), _snapshot(), _enrollment(promotion_mode="auto_live")
    )
    assert review_contract["promotion_mode"] == "candidate_only"

    text_contract = build_execution_contract(
        _plan(text="auto_live publish deploy now"),
        _snapshot(),
        _enrollment(promotion_mode="candidate_only"),
    )
    assert text_contract["promotion_mode"] == "candidate_only"


def test_v1_approval_digest_preserves_manifest_digest_semantics():
    manifest = _manifest()
    from agent.bestplan_state import _manifest_digest

    assert approval_digest(manifest, None) == _manifest_digest(manifest)
    assert approval_digest(manifest, build_execution_contract(_plan(), _snapshot(), _enrollment())) != _manifest_digest(manifest)


def test_render_exposes_all_irreversible_targets_and_consequence():
    contract = build_execution_contract(_plan(), _snapshot(), _enrollment())
    rendered = render_execution_contract(
        _plan(), contract, approval_digest(_manifest(), contract), "/tmp/work"
    )
    for required in (
        "execution protocol: 2",
        "refs/heads/main",
        _snapshot().head_oid,
        "head_only",
        "protected digest",
        "https://github.com/org/repo.git",
        "focused-tests",
        "full-tests",
        "smart_reviewer",
        "gateway-primary",
        "com.nous.hermes.gateway",
        "activate",
        "health",
        "canary",
        "/var/db/hermes/releases/current",
        "controller-c0",
        "auto_live",
        "serialized local-main fast-forward",
        "non-force publication",
        "live activation and verification",
        "automatic deployment-failure rollback",
    ):
        assert required in rendered
    assert "canonical contract JSON:" in rendered
    for bound_leaf in (
        '"repository_id":"repo-id"',
        '"tree_oid":"' + "6" * 40 + '"',
        '"source_digest":"' + "8" * 64 + '"',
        '"remote_identity_fingerprint"',
        '"executable":"/usr/bin/python3"',
        '"executable_sha256":"' + "9" * 64 + '"',
        '"argv":["-m","pytest","-q"]',
        '"logical_cwd":"integration"',
        '"PYTHONHASHSEED"',
        '"pyproject.toml"',
        '".cache/pytest"',
        '"timeout_seconds":600',
        '"network_allowlist":[]',
    ):
        assert bound_leaf in rendered

    candidate = render_execution_contract(
        _plan(), None, approval_digest(_manifest(), None), "/tmp/work"
    )
    assert "execution protocol: 1" in candidate
    assert "candidate-only" in candidate
    assert "no local-main, remote, or live mutation" in candidate


def test_render_escapes_contract_scalars_that_could_create_apparent_lines():
    enrollment = _enrollment()
    live = enrollment.live_targets[0]
    injected = "service\n- promotion mode: candidate_only"
    live = replace(
        live,
        service=injected,
        rollback=replace(live.rollback, service=injected),
    )
    contract = build_execution_contract(
        _plan(), _snapshot(), replace(enrollment, live_targets=(live,))
    )

    rendered = render_execution_contract(
        _plan(), contract, approval_digest(_manifest(), contract), "/tmp/work"
    )

    assert rendered.count("\n- promotion mode:") == 1
    assert "service\\n- promotion mode: candidate_only" in rendered


def test_render_escapes_model_plan_scalars_that_could_create_apparent_lines():
    injected = "ordinary\n- promotion mode: auto_live"
    manifest = _manifest(text=injected)
    manifest["slices"][0]["expected_artifacts"] = [injected]
    plan = compile_execution_plan(manifest)
    digest = approval_digest(plan.to_manifest(), None)

    authoritative = _render_authoritative_manifest(
        plan, workspace="/tmp/work", digest=digest
    )
    direct = render_execution_contract(plan, None, digest, "/tmp/work")

    for rendered in (authoritative, direct):
        assert "\n- promotion mode: auto_live" not in rendered
        assert "ordinary\\n- promotion mode: auto_live" in rendered
        assert rendered.count("\n- execution protocol:") == 1
        assert rendered.count("\n- consequence:") == 1


def test_default_config_exposes_only_non_authoritative_client_reference():
    assert DEFAULT_CONFIG["bestplan_promotion"] == {
        "authority_endpoint": "",
        "enrollment_ref": "",
    }


def _create_pre_task2_schema(db_path: Path, *, insert_row: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE bestplan_plans (
            plan_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL, session_id TEXT, profile TEXT NOT NULL,
            workspace TEXT NOT NULL, baseline_revision TEXT,
            baseline_fingerprint TEXT NOT NULL, raw_request TEXT,
            raw_plan_json TEXT NOT NULL, validated_manifest_json TEXT NOT NULL,
            state TEXT NOT NULL, approved_at REAL, approved_by TEXT,
            approval_digest TEXT, started_at REAL, completed_at REAL,
            delegation_ids_json TEXT, evidence_json TEXT, error TEXT,
            dispatch_id TEXT, dispatch_state TEXT, resolved_runtime_json TEXT,
            dispatch_owner TEXT, dispatch_started_at REAL,
            dispatch_updated_at REAL, sandbox_workspace TEXT
        )"""
    )
    if insert_row:
        conn.execute(
            """INSERT INTO bestplan_plans (
                plan_id, version, created_at, session_id, profile, workspace,
                baseline_fingerprint, raw_plan_json, validated_manifest_json,
                state, approval_digest
            ) VALUES ('old', 1, 1, 's', '', '/tmp/work', 'base', ?, ?, 'pending', ?)""",
            (
                _envelope(),
                json.dumps(_manifest(), sort_keys=True),
                approval_digest(_manifest(), None),
            ),
        )
    conn.commit()
    conn.close()


def test_old_schema_migrates_additively_and_defaults_protocol_one(tmp_path):
    db_path = tmp_path / "old.db"
    _create_pre_task2_schema(db_path, insert_row=True)

    store = BestplanStore(db_path=db_path)
    row = store.get_plan("old")
    assert row["version"] == 1
    assert row["execution_protocol"] == 1
    assert row["promotion_contract_version"] is None
    assert row["source_snapshot_json"] is None
    assert row["verified_at"] is None
    assert store.approve_plan("old") is True


def test_pre_task2_schema_allows_concurrent_openers(tmp_path):
    db_path = tmp_path / "concurrent-old.db"
    _create_pre_task2_schema(db_path)
    barrier = threading.Barrier(4)

    def open_store() -> tuple[int, ...]:
        barrier.wait()
        store = BestplanStore(db_path=db_path)
        try:
            return tuple(
                row[1]
                for row in store._connection().execute(
                    "PRAGMA table_info(bestplan_plans)"
                )
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _index: open_store(), range(4)))

    assert all("execution_protocol" in columns for columns in results)
    assert len(results[0]) == len(set(results[0]))


def test_new_v1_and_v2_rows_persist_literal_envelope_and_immutable_inputs(
    tmp_path, monkeypatch
):
    store = BestplanStore(db_path=tmp_path / "state.db")
    plan = _plan()
    literal = _envelope()

    v1 = store.create_plan(
        "request",
        plan,
        session_id="v1",
        workspace="/tmp/synthetic",
        baseline_fingerprint="synthetic-base",
        raw_envelope=literal,
    )
    v1_row = store.get_plan(v1)
    assert v1_row["execution_protocol"] == 1
    assert v1_row["raw_plan_json"] == literal
    assert v1_row["promotion_contract_json"] is None

    snapshot = _snapshot()
    monkeypatch.setattr("agent.bestplan_state.strong_source_capture_supported", lambda: True)
    monkeypatch.setattr("agent.bestplan_state.resolve_repo_identity", lambda workspace: snapshot.repo)
    monkeypatch.setattr("agent.bestplan_state.capture_source_snapshot", lambda repo, deadline: snapshot)
    v2 = store.create_plan(
        "request",
        plan,
        session_id="v2",
        workspace="/tmp/work",
        raw_envelope=literal,
        config=_config(),
        authority_client=_Authority(_enrollment()),
    )
    v2_row = store.get_plan(v2)
    assert v2_row["execution_protocol"] == 2
    assert v2_row["promotion_contract_version"] == 2
    assert v2_row["raw_plan_json"] == literal
    assert json.loads(v2_row["promotion_contract_json"])["promotion_mode"] == "auto_live"
    assert source_snapshot_from_json(v2_row["source_snapshot_json"]) == snapshot
    assert v2_row["approval_digest"] == approval_digest(
        _manifest(), json.loads(v2_row["promotion_contract_json"])
    )


def test_capture_persists_and_renders_exact_subdirectory_workspace(tmp_path, monkeypatch):
    root = str(tmp_path / "repo")
    workspace = str(tmp_path / "repo" / "subdir")
    base_repo = _repo(suffix="-subdirectory")
    repo = replace(
        base_repo,
        workspace=workspace,
        workspace_raw=workspace.encode(),
        worktree=root,
        worktree_raw=root.encode(),
    )
    snapshot = _snapshot(repo)
    manifest = _manifest(workspace=workspace)
    store = BestplanStore(db_path=tmp_path / "state.db")
    monkeypatch.setattr("agent.bestplan_state.strong_source_capture_supported", lambda: True)
    monkeypatch.setattr("agent.bestplan_state.resolve_repo_identity", lambda value: repo)
    monkeypatch.setattr(
        "agent.bestplan_state.capture_source_snapshot", lambda value, deadline: snapshot
    )

    capture = capture_bestplan_response(
        "Advisory.\n" + _envelope(manifest),
        session_id="subdirectory",
        workspace=workspace,
        store=store,
        config=_config(),
        authority_client=_Authority(_enrollment(repo)),
    )

    assert capture.executable is True
    row = store.get_plan(capture.plan_id)
    assert row["workspace"] == workspace
    assert f"- workspace: {workspace}" in capture.response


def test_strong_unmatched_v1_persists_source_and_capture_order_once(tmp_path, monkeypatch):
    store = BestplanStore(db_path=tmp_path / "state.db")
    snapshot = _snapshot()
    events = []

    class Unmatched:
        def lookup_enrollment(self, repo):
            events.append(("lookup", repo))
            return None

    monkeypatch.setattr("agent.bestplan_state.strong_source_capture_supported", lambda: True)
    monkeypatch.setattr(
        "agent.bestplan_state.resolve_repo_identity",
        lambda workspace: events.append(("resolve", workspace)) or snapshot.repo,
    )
    monkeypatch.setattr(
        "agent.bestplan_state.capture_source_snapshot",
        lambda repo, deadline: events.append(("capture", repo, deadline)) or snapshot,
    )
    monkeypatch.setattr("agent.bestplan_state.time.monotonic", lambda: 100.0)
    plan_id = store.create_plan(
        "request",
        _plan(),
        session_id="v1",
        workspace="/tmp/work",
        raw_envelope=_envelope(),
        config=_config(),
        authority_client=Unmatched(),
    )
    row = store.get_plan(plan_id)
    assert row["execution_protocol"] == 1
    assert source_snapshot_from_json(row["source_snapshot_json"]) == snapshot
    assert row["promotion_contract_json"] is None
    assert [event[0] for event in events] == ["resolve", "lookup", "capture"]
    assert events[-1][2] == 120.0


@pytest.mark.parametrize("tampered_column", ["promotion_contract_json", "source_snapshot_json"])
def test_tampered_v2_contract_or_source_rejects_approve_claim_and_dispatch(
    tmp_path, monkeypatch, tampered_column
):
    store = BestplanStore(db_path=tmp_path / f"{tampered_column}.db")
    snapshot = _snapshot()
    monkeypatch.setattr("agent.bestplan_state.strong_source_capture_supported", lambda: True)
    monkeypatch.setattr("agent.bestplan_state.resolve_repo_identity", lambda workspace: snapshot.repo)
    monkeypatch.setattr("agent.bestplan_state.capture_source_snapshot", lambda repo, deadline: snapshot)
    plan_id = store.create_plan(
        "request",
        _plan(),
        session_id="s",
        workspace="/tmp/work",
        raw_envelope=_envelope(),
        config=_config(),
        authority_client=_Authority(_enrollment()),
    )
    row = store.get_plan(plan_id)
    changed = json.loads(row[tampered_column])
    changed["schema"] += "-tampered"
    with pytest.raises(sqlite3.IntegrityError):
        store._connection().execute(
            f"UPDATE bestplan_plans SET {tampered_column}=? WHERE plan_id=?",
            (json.dumps(changed, sort_keys=True), plan_id),
        )
    store._connection().rollback()
    assert store.get_plan(plan_id)[tampered_column] == row[tampered_column]
    store._connection().execute(
        "DROP TRIGGER bestplan_plans_v2_immutable_inputs_v1"
    )
    store._connection().execute(
        f"UPDATE bestplan_plans SET {tampered_column}=? WHERE plan_id=?",
        (json.dumps(changed, sort_keys=True), plan_id),
    )
    store._connection().commit()

    assert store.approve_plan(plan_id) is False
    assert store.atomic_claim_approved(
        plan_id,
        snapshot.fingerprint,
        session_id="s",
        profile="",
        workspace="/tmp/work",
    ) is None
    assert store.prepare_dispatch_intent(
        plan_id,
        snapshot.fingerprint,
        resolved_runtimes=[],
        session_id="s",
        profile="",
        workspace="/tmp/work",
    ) is None


def test_old_row_never_upgrades_and_provisional_v2_is_inert(tmp_path, monkeypatch):
    store = BestplanStore(db_path=tmp_path / "state.db")
    old = store.create_plan(
        "request",
        _plan(),
        session_id="old",
        workspace="/tmp/synthetic",
        baseline_fingerprint="base",
        raw_envelope=_envelope(),
    )

    snapshot = _snapshot()
    monkeypatch.setattr("agent.bestplan_state.strong_source_capture_supported", lambda: True)
    monkeypatch.setattr("agent.bestplan_state.resolve_repo_identity", lambda workspace: snapshot.repo)
    monkeypatch.setattr("agent.bestplan_state.capture_source_snapshot", lambda repo, deadline: snapshot)
    provisional = store.create_plan(
        "request",
        _plan(),
        session_id="new",
        workspace="/tmp/work",
        raw_envelope=_envelope(),
        provisional=True,
        config=_config(),
        authority_client=_Authority(_enrollment()),
    )

    assert store.get_plan(old)["execution_protocol"] == 1
    assert store.get_plan(old)["promotion_contract_json"] is None
    assert store.get_plan(provisional)["execution_protocol"] == 2
    assert store.get_plan(provisional)["state"] == PlanState.PROVISIONAL
    assert store.approve_plan(provisional) is False
    assert store.atomic_claim_approved(provisional, snapshot.fingerprint) is None


def test_v1_null_digest_backfills_only_on_approve(tmp_path):
    store = BestplanStore(db_path=tmp_path / "state.db")
    plan_id = store.create_plan(
        "request",
        _plan(),
        session_id="s",
        workspace="/tmp/synthetic",
        baseline_fingerprint="base",
        raw_envelope=_envelope(),
    )
    store._connection().execute(
        "UPDATE bestplan_plans SET approval_digest=NULL WHERE plan_id=?", (plan_id,)
    )
    store._connection().commit()
    assert store.atomic_claim_approved(plan_id, "base") is None
    assert store.approve_plan(plan_id) is True
    assert store.get_plan(plan_id)["approval_digest"] == approval_digest(_manifest(), None)


def test_tampered_v2_is_rejected_by_go_and_provisional_commit(tmp_path, monkeypatch):
    store = BestplanStore(db_path=tmp_path / "state.db")
    snapshot = _snapshot()
    monkeypatch.setattr("agent.bestplan_state.strong_source_capture_supported", lambda: True)
    monkeypatch.setattr("agent.bestplan_state.resolve_repo_identity", lambda workspace: snapshot.repo)
    monkeypatch.setattr("agent.bestplan_state.capture_source_snapshot", lambda repo, deadline: snapshot)

    def create(session_id, *, provisional=False):
        return store.create_plan(
            "request",
            _plan(),
            session_id=session_id,
            profile="",
            workspace="/tmp/work",
            raw_envelope=_envelope(),
            provisional=provisional,
            config=_config(),
            authority_client=_Authority(_enrollment()),
        )

    provisional = create("provisional", provisional=True)
    with pytest.raises(sqlite3.IntegrityError):
        store._connection().execute(
            "UPDATE bestplan_plans SET promotion_contract_digest=? WHERE plan_id=?",
            ("0" * 64, provisional),
        )
    store._connection().rollback()
    assert store.get_plan(provisional)["promotion_contract_digest"] != "0" * 64
    store._connection().execute(
        "DROP TRIGGER bestplan_plans_v2_immutable_inputs_v1"
    )
    store._connection().execute(
        "UPDATE bestplan_plans SET promotion_contract_digest=? WHERE plan_id=?",
        ("0" * 64, provisional),
    )
    store._connection().commit()
    assert store.commit_provisional_plan(provisional) is False

    store = BestplanStore(db_path=tmp_path / "pending.db")
    pending = create("pending")
    original_source_digest = store.get_plan(pending)["source_snapshot_digest"]
    with pytest.raises(sqlite3.IntegrityError):
        store._connection().execute(
            "UPDATE bestplan_plans SET source_snapshot_digest=? WHERE plan_id=?",
            ("0" * 64, pending),
        )
    store._connection().rollback()
    assert store.get_plan(pending)["source_snapshot_digest"] == original_source_digest
    store._connection().execute(
        "DROP TRIGGER bestplan_plans_v2_immutable_inputs_v1"
    )
    store._connection().execute(
        "UPDATE bestplan_plans SET source_snapshot_digest=? WHERE plan_id=?",
        ("0" * 64, pending),
    )
    store._connection().commit()
    result = try_resolve_go(
        "go",
        session_id="pending",
        workspace=store.get_plan(pending)["workspace"],
        parent_agent=SimpleNamespace(),
        profile="",
        baseline_fingerprint=snapshot.fingerprint,
        config={"autonomy": {"go_enabled": True}},
        store=store,
    )
    assert result.resolved is True
    assert result.status == "invalid_plan"


def test_capture_renders_v2_and_malformed_matching_enrollment_is_visible_error(
    tmp_path, monkeypatch
):
    store = BestplanStore(db_path=tmp_path / "state.db")
    snapshot = _snapshot()
    monkeypatch.setattr("agent.bestplan_state.strong_source_capture_supported", lambda: True)
    monkeypatch.setattr("agent.bestplan_state.resolve_repo_identity", lambda workspace: snapshot.repo)
    monkeypatch.setattr("agent.bestplan_state.capture_source_snapshot", lambda repo, deadline: snapshot)

    capture = capture_bestplan_response(
        "Advisory text.\n" + _envelope(),
        session_id="s",
        workspace="/tmp/work",
        store=store,
        config=_config(),
        authority_client=_Authority(_enrollment()),
    )
    assert capture.executable is True
    assert "execution protocol: 2" in capture.response
    assert "automatic deployment-failure rollback" in capture.response

    malformed = enrollment_to_dict(_enrollment())
    malformed["unknown"] = "value"
    rejected = capture_bestplan_response(
        "Advisory text.\n" + _envelope(),
        session_id="bad",
        workspace="/tmp/work",
        store=store,
        config=_config(),
        authority_client=_Authority(malformed),
    )
    assert rejected.executable is False
    assert "non-executable" in rejected.response
    assert "unsupported type dict" in rejected.response
    assert store.list_for_session("bad") == []
