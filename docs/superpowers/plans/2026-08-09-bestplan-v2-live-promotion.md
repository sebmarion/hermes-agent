# BestPlan V2 Live Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make BestPlan produce parallel, isolated candidates and automatically promote an enrolled V2 plan through review, local main, the authorized remote, and verified live gateway activation.

**Architecture:** Keep the model envelope at V1 and attach an immutable host-owned `execution_protocol=2` contract only for enrolled repositories. Candidate workers remain untrusted and parallel; a root-owned promoter authority retained from controller release N-1 serializes proof, integration, review, publication, activation, and rollback through narrow peer-validated IPC.

**Tech Stack:** Python, SQLite, Git plumbing, macOS `sandbox-exec`, launchd, authenticated gateway health, and pytest through `scripts/run_tests.sh`.

---

## File structure

Create focused modules rather than expanding the existing BestPlan state and
delegation chokepoints further:

- `agent/bestplan_source.py` — repository support checks, stable HEAD-only
  snapshots, protected ambient manifests, and exact-tree exports.
- `agent/bestplan_contract.py` — enrollment parsing/matching, normalized
  destinations, V2 contract construction, and combined approval digests.
- `agent/bestplan_authority_client.py` — narrow client protocol for authority
  enrollment/status, projections, and model-broker attempts.
- `agent/bestplan_proof.py` — additive migrations, immutable candidates,
  append-only proof events, and verified-state validation.
- `agent/bestplan_redaction.py` — single bounded secret-safe serialization and
  raw-output digest boundary for authority and client projections.
- `agent/bestplan_candidates.py` — unique no-`.git` attempts, process lifecycle,
  delta validation, and host-created candidate commits/refs.
- `agent/bestplan_checks.py` — exact-argv deterministic checks and mutation
  detection at one integration commit.
- `agent/bestplan_review.py` — strict commit-bound adversarial-review schema and
  blocking policy.
- `agent/bestplan_promotion.py` — FIFO repository queue, integration,
  local/remote CAS, intent/receipt orchestration, and recovery.
- `agent/bestplan_promotion_worker.py` — independent retained-controller
  process entrypoint.
- `agent/bestplan_authority.py` — authenticated client protocol, authority-side
  operation validation, controller-selector CAS, and root-owned storage layout.
- `hermes_cli/bestplan_authority_install.py` — explicit privileged bootstrap and
  LaunchDaemon installation/removal primitives.
- `agent/bestplan_live.py` — immutable artifacts, selector/launcher activation,
  live attestation, automatic rollback, and complaint rollback.
- `agent/bestplan_canary.py` — deterministic provider-free capture/dispatch
  canary.
- `gateway/release_attestation.py` — booted release and process identity.

Modify only the integration surfaces:

- `agent/bestplan_state.py:237-318,480-1090,1329-1530`
- `agent/bestplan_sandbox.py:29-156`
- `agent/bestplan_worker.py:12-80`
- `tools/delegate_tool.py:5248-5506`
- `tools/async_delegation.py:2580-2650`
- `hermes_cli/subcommands/bestplan.py`
- `hermes_cli/config_defaults.py`
- `hermes_cli/gateway.py:2568-2700,4086-4400,4611-4700`
- `gateway/code_skew.py`
- `gateway/status.py:992-1200`
- `gateway/platforms/api_server.py:2919-2981`
- `gateway/run.py:26440-26475`
- `website/docs/reference/slash-commands.md`

The user requires direct work on `main`; do not create a feature branch or
worktree for this implementation. Before edits, capture a structured inventory
of every pre-existing staged, unstaged, and non-ignored untracked path,
including index state, type, mode, symlink target, and content digest. Before
every task, stop on path/prefix/case/Unicode overlap; stage only the exact task
files. At completion, reproduce the full inventory byte-for-byte and index-for-
index. Never stash, reset, clean, auto-commit, or overwrite ambient work.
Every task commit uses `git commit --only <exact-task-paths>` after staging, then
recaptures the ambient index manifest and requires it to be unchanged.

### Task 1: Stable HEAD-only source boundary

**Files:**

- Create: `agent/bestplan_source.py`
- Create: `tests/agent/test_bestplan_source.py`
- Modify: `agent/bestplan_state.py:237-318,692-744`
- Modify: `tests/agent/test_bestplan_final_hardening.py:129-136`

- [ ] **Step 1: Write failing source-snapshot tests.** Cover ignored
  `.bytecode-fingerprint`, a large ignored tree, distinct staged/unstaged binary
  patches, untracked file/symlink type and mode, index flags, raw paths,
  symbolic HEAD/full OID/ref/common-dir identity, special-file rejection,
  stable `A,B,B` double capture, restricted repository features, and populated
  `baseline_revision`. Add an actively churning worktree test proving a bounded
  deadline yields `proof_stale` before plan persistence or dispatch.
- [ ] **Step 2: Run the red tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_source.py tests/agent/test_bestplan_final_hardening.py -q
  ```

  Expected: failures because ignored regular files are still rejected and the
  source snapshot API does not exist.
- [ ] **Step 3: Implement the minimum source API.** Add immutable
  `RepoIdentity`, `ProtectedEntry`, and `SourceSnapshot` dataclasses plus:

  ```python
  resolve_repo_identity(workspace: str) -> RepoIdentity
  capture_source_snapshot(repo: RepoIdentity, deadline: float) -> SourceSnapshot
  capture_protected_manifest(repo: RepoIdentity) -> ProtectedManifest
  assert_supported_repository(repo: RepoIdentity) -> None
  recapture_matches(expected: SourceSnapshot) -> bool
  export_exact_tree(snapshot: SourceSnapshot, destination: Path) -> None
  ```

  Resolve repository identity first so Task 2 can query authority enrollment;
  then capture with its deadline, or a bounded legacy default when unmatched.
  Encode paths losslessly, hash staged and unstaged state separately, and never
  enumerate ignored paths. Require two consecutive identical observations;
  otherwise emit `proof_stale` before persistence/dispatch. Make
  `compute_baseline_fingerprint()` a compatibility wrapper over the new digest.
- [ ] **Step 4: Run focused green tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_source.py tests/agent/test_bestplan_final_hardening.py tests/agent/test_bestplan_host_ingress.py -q
  ```

- [ ] **Step 5: Inspect the diff, stage only Task 1 files, and commit.**

  ```bash
  git add agent/bestplan_source.py agent/bestplan_state.py tests/agent/test_bestplan_source.py tests/agent/test_bestplan_final_hardening.py
  git commit --only agent/bestplan_source.py agent/bestplan_state.py tests/agent/test_bestplan_source.py tests/agent/test_bestplan_final_hardening.py -m "fix(bestplan): capture stable head-only source state"
  ```

### Task 2: Enrollment and host-attached execution contract

**Files:**

- Create: `agent/bestplan_contract.py`
- Create: `agent/bestplan_authority_client.py`
- Create: `tests/agent/test_bestplan_contract.py`
- Create: `tests/agent/test_bestplan_authority_client.py`
- Modify: `agent/bestplan_state.py:480-1090`
- Modify: `hermes_cli/config_defaults.py`

- [ ] **Step 1: Write failing contract/migration tests.** Prove unavailable or
  unmatched authority enrollment creates protocol 1, exactly one canonical match creates protocol
  2, `review_only` or descriptive artifact declarations cannot become
  `auto_live`, every irreversible field changes the digest, unrelated config
  does not, legacy rows default to protocol 1, and the host render exposes the
  exact local ref, remote ref, service, checks, rollback target, and auto-live
  consequence before `go`. Reject any target other than `refs/heads/main`, more
  than one live target, or any cross-repository contract before protocol 2 can
  be issued.
- [ ] **Step 2: Run the red tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_contract.py tests/agent/test_bestplan_authority_client.py tests/agent/test_bestplan_state.py -q
  ```

- [ ] **Step 3: Implement strict enrollment and contract APIs.** Use a separate
  top-level `bestplan_promotion` config block only for the non-authoritative
  authority endpoint/enrollment reference, query the injected authority client,
  and use credential-free URL normalization:

  ```python
  resolve_matching_enrollment(config, repo_identity, authority_client) -> Enrollment | None
  build_execution_contract(plan, snapshot, enrollment, controller) -> dict
  approval_digest(manifest, contract_or_none) -> str
  ```

  Wire `repo identity -> authority enrollment/deadline -> source snapshot`
  explicitly. Define the injected client protocol before the daemon exists:

  ```python
  lookup_enrollment(repo_identity) -> Enrollment | None
  register_model_attempt(attempt_id, worker_identity, model, request_budget, token_budget, expires_at) -> BrokerCapability
  model_request(capability, request) -> ModelResponse
  revoke_model_attempt(capability) -> None
  read_authoritative_status(plan_id) -> AuthorityStatus
  ```

  The broker capability is not a provider credential and is bound to the
  registered worker PID/start identity. Task 4 uses a fake injected client;
  Task 8 implements the protected server. Add only nullable/defaulted columns,
  including a distinct nullable
  `promotion_contract_version`, for execution protocol, immutable contract
  and source JSON/digests, current phase, identities, proof pointer, and
  verification timestamps. Preserve the literal V1 model envelope.
- [ ] **Step 4: Run contract and ingress tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_contract.py tests/agent/test_bestplan_authority_client.py tests/agent/test_bestplan_state.py tests/agent/test_bestplan_host_ingress.py -q
  ```

- [ ] **Step 5: Stage and commit Task 2.**

  ```bash
  git add agent/bestplan_contract.py agent/bestplan_authority_client.py agent/bestplan_state.py hermes_cli/config_defaults.py tests/agent/test_bestplan_contract.py tests/agent/test_bestplan_authority_client.py
  git commit --only agent/bestplan_contract.py agent/bestplan_authority_client.py agent/bestplan_state.py hermes_cli/config_defaults.py tests/agent/test_bestplan_contract.py tests/agent/test_bestplan_authority_client.py -m "feat(bestplan): bind enrolled live targets to approval"
  ```

### Task 3: Append-only proof ledger

**Files:**

- Create: `agent/bestplan_proof.py`
- Create: `agent/bestplan_redaction.py`
- Create: `tests/agent/test_bestplan_proof.py`
- Create: `tests/agent/test_bestplan_redaction.py`
- Modify: `agent/bestplan_state.py:515-690,1027-1042`

- [ ] **Step 1: Write failing ledger tests.** Cover deterministic event hashes,
  idempotent operation receipts, concurrent writers, wrong expected head,
  SQLite `UPDATE`/`DELETE` rejection, legacy advisory evidence, direct SQL and
  legacy-setter bypass attempts, exact phase/OID chain validation,
  `verified_at` separation, and old/partial schema migration. Rows without a
  source snapshot/candidate receipt must remain legacy-terminal and require
  recapture; legacy async callbacks must be SQL-gated away from V2 rows.
  Also prove a gateway-owned ledger/projection cannot satisfy a protocol-2 gate
  without a fresh authority receipt.
  Inject sentinel secrets through fake Git, check, review, model-broker, health,
  canary, and process outputs; assert they never appear in plan/config fields,
  projections, journal-event payloads, receipts, status text/JSON, or logs,
  while full raw-output digests remain correct.
- [ ] **Step 2: Run the red ledger test.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_proof.py tests/agent/test_bestplan_redaction.py -q
  ```

- [ ] **Step 3: Implement additive projection tables and proof primitives.** Create
  `bestplan_candidates` and `bestplan_proof_events`, immutability triggers,
  append/read/verify APIs, and `complete_verified()`. A database trigger must
  prevent an older process from directly setting a protocol-2 row to
  `completed_verified` without the same-plan final event pointer. Retain
  `evidence_json` only as a compatibility projection, route versioned async
  terminal results into proof projections instead of legacy setters, and keep
  the authority-receipt verifier injectable. Task 8 moves the authoritative
  event chain and terminal CAS into the protected service; local tables alone
  can never complete V2. Centralize serialization so raw output is digested in
  memory and only bounded redacted summaries are persistable; unsafe payloads
  fail closed and are never logged as fallback.
- [ ] **Step 4: Run ledger and state tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_proof.py tests/agent/test_bestplan_redaction.py tests/agent/test_bestplan_state.py -q
  ```

- [ ] **Step 5: Stage and commit Task 3.**

  ```bash
  git add agent/bestplan_proof.py agent/bestplan_redaction.py agent/bestplan_state.py tests/agent/test_bestplan_proof.py tests/agent/test_bestplan_redaction.py
  git commit --only agent/bestplan_proof.py agent/bestplan_redaction.py agent/bestplan_state.py tests/agent/test_bestplan_proof.py tests/agent/test_bestplan_redaction.py -m "feat(bestplan): add append-only redacted promotion proofs"
  ```

### Task 4: Isolated host-frozen candidates

**Files:**

- Create: `agent/bestplan_candidates.py`
- Create: `tests/agent/test_bestplan_candidates.py`
- Modify: `agent/bestplan_sandbox.py`
- Modify: `agent/bestplan_worker.py`

- [ ] **Step 1: Write failing single-candidate tests.** Prove exact-base export
  after ambient HEAD moves, unique attempt layout, no `.git` or ambient ignored
  files, environment allowlisting, no provider credential, an N-1 authority
  model broker keyed by registered attempt/model/budget/expiry and worker
  process identity, capability revocation at process exit, denied primary
  checkout/common-dir/config/state/sibling reads, lease-only writes, timeout
  process-group reaping, denial of user-home/credential-store/authority-control
  IPC/unrelated socket or Mach-service reads, denial of arbitrary egress and
  unrelated-process signaling, continued access to pinned N-1/system runtime
  dependencies and the capability-bound broker channel, raw
  path/delta rejection, exact expected artifacts, deterministic host-created
  commit, and CAS anchoring under `refs/hermes-bestplan/...`.
- [ ] **Step 2: Run the red candidate test.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_candidates.py -q
  ```

- [ ] **Step 3: Implement exact-tree attempt creation and freezing.** Model
  code runs from the retained controller source with isolated Python startup;
  the candidate source never enters `PYTHONPATH`. Sanitize SSH/GitHub/cloud/
  deploy credentials, mediate every model call through the authority broker,
  supervise the entire process group, validate the raw
  filesystem delta after all writers stop, then create and ref-anchor the
  candidate from a host-controlled index/worktree.
- [ ] **Step 4: Run candidate and hardening tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_candidates.py tests/agent/test_bestplan_final_hardening.py -q
  ```

- [ ] **Step 5: Stage and commit Task 4.**

  ```bash
  git add agent/bestplan_candidates.py agent/bestplan_sandbox.py agent/bestplan_worker.py tests/agent/test_bestplan_candidates.py
  git commit --only agent/bestplan_candidates.py agent/bestplan_sandbox.py agent/bestplan_worker.py tests/agent/test_bestplan_candidates.py -m "feat(bestplan): freeze isolated host-owned candidates"
  ```

### Task 5: True parallel candidate batch

**Files:**

- Modify: `tools/delegate_tool.py:5248-5506`
- Modify: `tools/async_delegation.py:2580-2650`
- Modify: `agent/bestplan_state.py:1329-1530`
- Modify: `tests/agent/test_bestplan_candidates.py`
- Modify: `tests/agent/test_bestplan_host_ingress.py`

- [ ] **Step 1: Add barrier-based red tests.** Prove two slices overlap without
  timing guesses, use distinct source/runtime directories, return results in
  manifest order, terminate all writers on failure/cancel, close unused launch
  profiles after admission rejection, bind the real interrupt registry, and do
  not announce completion until every candidate is frozen. Protocol 1 and V2
  `candidate_only` must stop at candidate-ready/unverified. Before worker launch
  or model-broker capability issuance, reject every lease overlapping a
  protected staged, unstaged, or untracked path or ancestor, including
  case-folded and Unicode-normalized aliases.
- [ ] **Step 2: Run the red batch tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_candidates.py tests/agent/test_bestplan_host_ingress.py -q
  ```

- [ ] **Step 3: Replace the reused worktree/sequential loop.** Use bounded
  concurrent candidate execution inside the single async batch. Make the
  generic async finalizer append only advisory terminal evidence and prevent it
  from overwriting a later promotion phase. Run the protected-path/ancestor/
  case/Unicode admission gate before creating attempts or registering broker
  capabilities.
- [ ] **Step 4: Run batch, ingress, and state tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_candidates.py tests/agent/test_bestplan_host_ingress.py tests/agent/test_bestplan_state.py -q
  ```

- [ ] **Step 5: Stage and commit Task 5.**

  ```bash
  git add tools/delegate_tool.py tools/async_delegation.py agent/bestplan_state.py tests/agent/test_bestplan_candidates.py tests/agent/test_bestplan_host_ingress.py
  git commit --only tools/delegate_tool.py tools/async_delegation.py agent/bestplan_state.py tests/agent/test_bestplan_candidates.py tests/agent/test_bestplan_host_ingress.py -m "fix(bestplan): run independent slices concurrently"
  ```

### Task 6: Immutable integration and host-owned checks

**Files:**

- Create: `agent/bestplan_checks.py`
- Create: `agent/bestplan_promotion.py`
- Create: `tests/agent/test_bestplan_checks.py`
- Create: `tests/agent/test_bestplan_promotion.py`

- [ ] **Step 1: Write failing integration/check tests.** Prove manifest-order
  application, conflict blocking, current-target first parent, candidate
  ancestry/lease/tree/artifact revalidation, stale target invalidation,
  dependency-manifest rejection, absolute checker executable/config digests,
  exact `shell=False` argv/cwd/environment, restricted network, output hashes
  bound to integration OID, and rejection of tracked mutations or untracked
  mutations outside an enrollment-frozen cache allowlist. Ignore rules from the
  candidate integration cannot expand that allowlist. Exercise checks inside an
  N-1-created sandbox with read-only `I`, private scratch/cache, no Git/promoter/
  remote/live credentials, restricted network, and full process-tree reaping on
  success, failure, cancellation, and timeout. The default-deny profile must
  reject user-home/credential-store reads, authority/control IPC, unrelated
  sockets/Mach services/process signaling, and arbitrary egress while allowing
  only pinned N-1/system runtime dependencies and enrollment-pinned check
  endpoints.
- [ ] **Step 2: Run the red tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_checks.py tests/agent/test_bestplan_promotion.py -q
  ```

- [ ] **Step 3: Implement clean integration worktree and check runner.** Create
  one immutable integration commit whose first parent is the admitted target.
  Candidate checks run in an N-1-created disposable overlay with immutable `I`,
  only private scratch/frozen cache writes, restricted network, no publication
  credentials, and process-tree reap on every exit. Compare and discard the
  upper layer; promoter code never imports from the integration tree.
- [ ] **Step 4: Run green integration/check tests.** Use the same command.
- [ ] **Step 5: Stage and commit Task 6.**

  ```bash
  git add agent/bestplan_checks.py agent/bestplan_promotion.py tests/agent/test_bestplan_checks.py tests/agent/test_bestplan_promotion.py
  git commit --only agent/bestplan_checks.py agent/bestplan_promotion.py tests/agent/test_bestplan_checks.py tests/agent/test_bestplan_promotion.py -m "feat(bestplan): freeze and test integration commits"
  ```

### Task 7: Mandatory commit-bound adversarial review

**Files:**

- Create: `agent/bestplan_review.py`
- Create: `tests/agent/test_bestplan_review.py`
- Modify: `agent/bestplan_worker.py`
- Modify: `agent/bestplan_promotion.py`

- [ ] **Step 1: Write failing review tests.** Require exact integration OID;
  block critical/high findings; accept medium/low-only findings; turn timeout,
  unavailable lane, malformed JSON, unknown severity, or commit mismatch into
  `review_blocked`; prove read-only no-`.git` input; invalidate on integration
  change; and prove ProofAgent/Hardproof attachments cannot satisfy the gate.
- [ ] **Step 2: Run the red test.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_review.py -q
  ```

- [ ] **Step 3: Implement a strict review packet and schema.** Reuse the
  configured `smart_reviewer` runtime through isolated read-only transport. The
  reviewer receives the exact diff, contract, deterministic receipts, project
  rules, and acceptance criteria, but cannot write or mutate trusted state.
- [ ] **Step 4: Run review and candidate tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_review.py tests/agent/test_bestplan_candidates.py -q
  ```

- [ ] **Step 5: Stage and commit Task 7.**

  ```bash
  git add agent/bestplan_review.py agent/bestplan_worker.py agent/bestplan_promotion.py tests/agent/test_bestplan_review.py
  git commit --only agent/bestplan_review.py agent/bestplan_worker.py agent/bestplan_promotion.py tests/agent/test_bestplan_review.py -m "feat(bestplan): require commit-bound adversarial review"
  ```

### Task 8: FIFO OS-protected promoter from controller N-1

**Files:**

- Create: `agent/bestplan_promotion_worker.py`
- Create: `agent/bestplan_authority.py`
- Create: `hermes_cli/bestplan_authority_install.py`
- Create: `tests/agent/test_bestplan_recovery.py`
- Modify: `agent/bestplan_promotion.py`
- Modify: `hermes_cli/gateway.py`

- [ ] **Step 1: Write failing queue/controller tests.** Prove FIFO by canonical
  common-dir identity, candidate work during another promotion, PID plus process
  start identity, retained controller source, N-1 promotes N, survival across
  gateway restart, recovery from the recorded controller artifact, root-owned
  enrollment/journal/job/controller storage, peer-validated authenticated IPC,
  denial of gateway/worker/check/reviewer filesystem or signal access, distinct
  gateway/controller selectors, controller-selector CAS only after
  `completed_verified`, rollback/failure leaving N-1 selected, the next
  operation using the newly selected controller only after success, and
  quarantine on controller mismatch. Prove Git/worktree commands run under the
  enrolled repository owner UID/GID, ownership mismatch blocks, and publication
  authentication comes only from the authority credential broker. Immediately
  at queue head, revalidate the
  repository/common-dir, base/target, enrollment, remote URL/ref, absolute
  check/timeouts, review policy, live/rollback targets, controller, and ambient
  digest; any drift must block before side effects.
  Feed secret sentinels through authority Git/check/review/health/broker output
  and require the central redaction boundary for journal, receipt, status, and
  logs. Add a crash after authority-journal commit but before projection
  acknowledgement, startup cursor backfill, and invalid/gapped/replayed/
  regressive/bad-MAC projection rejection without authoritative-state change.
- [ ] **Step 2: Run the red tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_promotion.py tests/agent/test_bestplan_recovery.py -q
  ```

- [ ] **Step 3: Implement the authority and independent promotion worker.** On
  macOS install a root-owned LaunchDaemon and state directory during explicit
  enrollment. Its narrow Unix-socket protocol validates peer/process/controller
  identity, accepts only a frozen approved operation pinned to immutable
  controller N-1 before any side effect, and owns enrollment,
  journal, controller artifacts/selectors, job definition, and publication/live
  credentials and authoritative append-only proof journal. Gateway `state.db`
  is only a signed projection and cannot satisfy a gate. Repository commands
  drop to the enrolled owner UID/GID with a sanitized environment, while remote
  authentication is brokered without environment variables. The ordinary
  gateway cannot signal or replace the authority. Expose a CAS
  controller-succession primitive that is callable only after the matching
  operation is both `live_verified` and `completed_verified`. Keep all
  platform and privilege effects behind injected runners for deterministic
  tests. Implement the Task 2 model-broker server calls with registered
  attempt/PID-start/model/request-budget/token-budget/expiry enforcement; keep
  provider credentials inside authority and revoke access on worker exit. After
  each journal commit, enqueue an idempotent projection keyed by
  authority epoch, plan, sequence, and event hash; persist each sink cursor,
  retry until acknowledged, replay gaps at startup, and validate the root-keyed
  MAC through authority IPC. Projection failure never changes promotion state.
- [ ] **Step 4: Run queue/recovery tests.** Use the same command.
- [ ] **Step 5: Stage and commit Task 8.**

  ```bash
  git add agent/bestplan_promotion.py agent/bestplan_promotion_worker.py agent/bestplan_authority.py hermes_cli/bestplan_authority_install.py hermes_cli/gateway.py tests/agent/test_bestplan_recovery.py tests/agent/test_bestplan_promotion.py
  git commit --only agent/bestplan_promotion.py agent/bestplan_promotion_worker.py agent/bestplan_authority.py hermes_cli/bestplan_authority_install.py hermes_cli/gateway.py tests/agent/test_bestplan_recovery.py tests/agent/test_bestplan_promotion.py -m "feat(bestplan): isolate promotion authority from candidates"
  ```

### Task 9: Immutable gateway artifact and host-correlated identity

**Files:**

- Create: `agent/bestplan_live.py`
- Create: `agent/bestplan_canary.py`
- Create: `gateway/release_attestation.py`
- Create: `tests/agent/test_bestplan_live.py`
- Create: `tests/gateway/test_bestplan_runtime_identity.py`
- Modify: `gateway/code_skew.py`
- Modify: `gateway/status.py`
- Modify: `gateway/platforms/api_server.py`
- Modify: `hermes_cli/gateway.py`
- Modify: `agent/bestplan_promotion.py`

- [ ] **Step 1: Write failing artifact/identity tests.** Cover exact-tree
  artifact manifests, path/type/mode/content/link hashes, dependency-change
  rejection, external launcher/selector, atomic selector replacement, cleared
  path overrides, unchanged non-enrolled launch behavior, stable enrolled plist,
  authenticated detailed health fields, host detection of false runtime claims,
  and a model/provider-free capture/dispatch canary. Prove artifact construction
  and durable receipt occur after review but before local/remote mutation, and
  artifact failure leaves local main and the remote untouched.
- [ ] **Step 2: Run the red tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_live.py tests/gateway/test_bestplan_runtime_identity.py tests/hermes_cli/test_gateway_service.py tests/gateway/test_api_server.py tests/agent/test_bestplan_promotion.py -q
  ```

- [ ] **Step 3: Implement artifact, launcher, boot receipt, and canary.** Reuse
  gateway PID/start-time and authenticated health primitives. The retained
  trusted launcher hashes and records the selected release before `exec`; the
  promoter independently recomputes selector/artifact/process identity rather
  than trusting candidate health code. Wire the coordinator ordering as
  `review -> artifact receipt -> local CAS -> push -> activation`.
- [ ] **Step 4: Run the same tests green.**
- [ ] **Step 5: Stage and commit Task 9.**

  ```bash
  git add agent/bestplan_live.py agent/bestplan_canary.py gateway/release_attestation.py gateway/code_skew.py gateway/status.py gateway/platforms/api_server.py hermes_cli/gateway.py agent/bestplan_promotion.py tests/agent/test_bestplan_live.py tests/gateway/test_bestplan_runtime_identity.py tests/agent/test_bestplan_promotion.py
  git commit --only agent/bestplan_live.py agent/bestplan_canary.py gateway/release_attestation.py gateway/code_skew.py gateway/status.py gateway/platforms/api_server.py hermes_cli/gateway.py agent/bestplan_promotion.py tests/agent/test_bestplan_live.py tests/gateway/test_bestplan_runtime_identity.py tests/agent/test_bestplan_promotion.py -m "feat(bestplan): build attested gateway release artifacts"
  ```

### Task 10: Dirty-preserving local main and remote publication

**Files:**

- Modify: `agent/bestplan_promotion.py`
- Modify: `tests/agent/test_bestplan_promotion.py`
- Modify: `tests/agent/test_bestplan_recovery.py`

- [ ] **Step 1: Write failing real-Git tests.** Use temporary clones and a bare
  remote to cover dirty disjoint fast-forward, staged/unstaged/untracked/
  symlink/mode/prefix overlap, byte-identical ambient state, disabled hooks and
  autostash, targeted collisions where an ignored/untracked path becomes
  tracked, non-checked-out `update-ref`, target movement producing
  `proof_stale` and requiring a newly rendered/approved contract, explicit
  non-force refspec, remote race/rejection/mismatch, independent fetch, and
  crash before/after every intent/effect/receipt boundary. Revalidate the full
  approved contract and ambient digest again immediately before local CAS and
  before push; any drift must produce zero mutation.
- [ ] **Step 2: Run the red promotion/recovery tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_promotion.py tests/agent/test_bestplan_recovery.py -q
  ```

- [ ] **Step 3: Implement intent/observe/CAS/receipt publication.** Require the
  reviewed artifact receipt before the first mutation. The final private fetch
  must equal the integration OID exactly; even a descendant is a blocking
  remote mismatch. Never invoke `scripts/release.py`, stash, reset, clean,
  force-push, or auto-commit ambient work. Reconcile an exact observed effect;
  retry only a proven absent effect; quarantine ambiguity.
- [ ] **Step 4: Run green promotion/recovery tests.** Use the same command.
- [ ] **Step 5: Stage and commit Task 10.**

  ```bash
  git add agent/bestplan_promotion.py tests/agent/test_bestplan_promotion.py tests/agent/test_bestplan_recovery.py
  git commit --only agent/bestplan_promotion.py tests/agent/test_bestplan_promotion.py tests/agent/test_bestplan_recovery.py -m "feat(bestplan): publish reviewed artifacts without force"
  ```

### Task 11: Activation and verified rollback

**Files:**

- Modify: `agent/bestplan_live.py`
- Modify: `agent/bestplan_promotion.py`
- Modify: `agent/bestplan_proof.py`
- Modify: `tests/agent/test_bestplan_live.py`
- Modify: `tests/agent/test_bestplan_proof.py`

- [ ] **Step 1: Write failing activation tests with injected launchctl, health,
  canary, clock, and log adapters.** Prove previous-state capture, new process
  start identity, exact selector/config/runtime/host agreement, readiness and
  log observation, service-definition drift blocking before activation,
  selector-only CAS restoration on activation failure, `rollback_failed` then
  quarantine on selector conflict/restart failure/unhealthy old artifact,
  verified-release-only complaint rollback, no source-history reset, and
  live-event-only terminal verification. Pin the operation to immutable
  controller N-1 before any side effect; prove every failure/rollback leaves
  N-1 selected, success CAS-selects N only after `completed_verified`, and the
  next operation resolves N as its controller. Immediately before terminal
  verification, freshly recapture local ref and ambient state, privately fetch
  the remote and require exact `I`, then reread selector, artifact, process start
  identity, readiness, canary, and bounded logs; any intervening drift blocks
  `live_verified` and `completed_verified`.
- [ ] **Step 2: Run the red tests.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_live.py tests/agent/test_bestplan_proof.py -q
  ```

- [ ] **Step 3: Implement activation and rollback in the independent worker.**
  Keep gateway admission closed until either the new release or the previous
  release has been positively verified.
- [ ] **Step 4: Run the same tests green.**
- [ ] **Step 5: Stage and commit Task 11.**

  ```bash
  git add agent/bestplan_live.py agent/bestplan_promotion.py agent/bestplan_proof.py tests/agent/test_bestplan_live.py tests/agent/test_bestplan_proof.py
  git commit --only agent/bestplan_live.py agent/bestplan_promotion.py agent/bestplan_proof.py tests/agent/test_bestplan_live.py tests/agent/test_bestplan_proof.py -m "feat(bestplan): verify live activation and rollback"
  ```

### Task 12: Capture wiring, recovery, CLI, and docs

**Files:**

- Modify: `agent/bestplan_state.py`
- Modify: `tools/async_delegation.py`
- Modify: `hermes_cli/subcommands/bestplan.py`
- Modify: `gateway/run.py`
- Modify: `website/docs/reference/slash-commands.md`
- Modify: `tests/hermes_cli/test_bestplan_cli.py`
- Modify: `tests/agent/test_bestplan_host_ingress.py`
- Modify: `tests/agent/test_bestplan_recovery.py`

- [ ] **Step 1: Write failing end-surface tests.** Prove `enroll` exact target
  rendering/confirmation/secret rejection/raw-config preservation/bootstrap,
  read-only `status`, idempotent `retry`, verified receipt-only `rollback`, no
  retroactive enrollment, V2 `go` candidate plus detached promotion scheduling,
  non-blocking startup reconciliation, and unchanged candidate-only legacy
  semantics. Keep `/bestplan` absent from the slash-command registry. Repeat
  sentinel-secret assertions for text and JSON `status` plus config persistence.
- [ ] **Step 2: Run the red CLI/ingress/recovery tests.**

  ```bash
  scripts/run_tests.sh tests/hermes_cli/test_bestplan_cli.py tests/agent/test_bestplan_host_ingress.py tests/agent/test_bestplan_recovery.py -q
  ```

- [ ] **Step 3: Implement `enroll`, `status`, `retry`, and `rollback`.** Reads
  must not mutate SQLite or config. Enrollment is the one-time bootstrap trust
  ceremony: render exact targets, obtain explicit privilege, and accept C0 only
  as an exact committed artifact explicitly attested by the operator. Install/
  verify the root-owned authority and stable gateway launcher, switch the
  launcher to C0, restart, verify process-start/controller/artifact/health, then
  atomically enable `auto_live` in authority state and write only its client
  reference to config. Dirty or unidentified runtime source is rejected;
  partial bootstrap remains disabled and retryable. `status` reads authoritative
  state from the protected service rather than trusting the local projection.
- [ ] **Step 4: Run the expanded compatibility set.**

  ```bash
  scripts/run_tests.sh tests/hermes_cli/test_bestplan_cli.py tests/agent/test_bestplan_host_ingress.py tests/agent/test_bestplan_recovery.py tests/cli/test_cli_bestplan_capture.py tests/gateway/test_bestplan_default_count.py -q
  ```

- [ ] **Step 5: Stage and commit Task 12.**

  ```bash
  git add agent/bestplan_state.py tools/async_delegation.py hermes_cli/subcommands/bestplan.py gateway/run.py website/docs/reference/slash-commands.md tests/hermes_cli/test_bestplan_cli.py tests/agent/test_bestplan_host_ingress.py tests/agent/test_bestplan_recovery.py
  git commit --only agent/bestplan_state.py tools/async_delegation.py hermes_cli/subcommands/bestplan.py gateway/run.py website/docs/reference/slash-commands.md tests/hermes_cli/test_bestplan_cli.py tests/agent/test_bestplan_host_ingress.py tests/agent/test_bestplan_recovery.py -m "feat(bestplan): expose enrollment status retry and rollback"
  ```

### Task 13: Adversarial acceptance and live rollout

**Files:**

- Create: `tests/agent/test_bestplan_end_to_end.py`
- Modify: relevant Task 1-12 tests for any review finding

- [ ] **Step 1: Add a deterministic end-to-end test.** Drive two concurrent
  candidates through exact integration, review, a temporary bare remote, and a
  fake live target. Add parameterized failure injection for every
  intent/effect/receipt boundary and each acceptance failure.
- [ ] **Step 2: Run focused BestPlan proof.**

  ```bash
  scripts/run_tests.sh tests/agent/test_bestplan_source.py tests/agent/test_bestplan_contract.py tests/agent/test_bestplan_authority_client.py tests/agent/test_bestplan_proof.py tests/agent/test_bestplan_redaction.py tests/agent/test_bestplan_candidates.py tests/agent/test_bestplan_checks.py tests/agent/test_bestplan_review.py tests/agent/test_bestplan_promotion.py tests/agent/test_bestplan_live.py tests/agent/test_bestplan_recovery.py tests/agent/test_bestplan_end_to_end.py tests/agent/test_bestplan_state.py tests/agent/test_bestplan_final_hardening.py tests/agent/test_bestplan_host_ingress.py tests/hermes_cli/test_bestplan_cli.py tests/hermes_cli/test_gateway_service.py tests/gateway/test_api_server.py tests/gateway/test_bestplan_runtime_identity.py -q
  ```

- [ ] **Step 3: Run the complete project suite.**

  ```bash
  scripts/run_tests.sh
  ```

- [ ] **Step 4: Request independent adversarial review of the exact final
  commit.** Critical/high findings block. Any behavior-changing fix creates a
  new commit and invalidates prior tests/review, so rerun Steps 2-4.
- [ ] **Step 5: Recompute the complete ambient-work inventory and inspect the
  final diff.** Compare every preflight staged, unstaged, and non-ignored
  untracked entry byte-for-byte, type/mode/link-for-link, and index-for-index;
  no extra ambient path may appear or disappear. The initial known entries
  include `AGENTS.md` digest
  `bd544953c70d078f46b0d625b02d7a95265d868b7c20db8070d8e3383308b107`
  and `CLAUDE.md` digest
  `8449f1136ef15d092c47eca391e6b2a40344a195e8cb9f99360ae1081dc36248`.
- [ ] **Step 6: Push the reviewed commit explicitly and non-force.**

  ```bash
  git push sebmarion HEAD:refs/heads/main
  git ls-remote --refs sebmarion refs/heads/main
  ```

  Confirm local `main`, reviewed SHA, and observed remote SHA are identical.
- [ ] **Step 7: Perform the one-time privileged bootstrap enrollment on the
  named Mac gateway.** Bind
  the verified `sebmarion` push identity/ref, service label, profile,
  authenticated detailed-health endpoint, `smart_reviewer`, focused BestPlan
  check, full-suite check, behavior canary, and rollback selector. Record the
  reviewed implementation release as distinct bootstrap controller C0 and live
  release R0; this explicit trust ceremony is not itself evidence that the
  automatic pipeline works.
- [ ] **Step 8: Promote a distinct canary release R1 through controller C0.**
  The sole candidate change is exact path
  `tests/fixtures/bestplan_live_canary.txt` with exact bytes
  `bestplan-live-canary-v1\n`; its lease and expected artifact name that path.
  Approval-bound checks assert those exact bytes and run the focused BestPlan
  suite. The promoter alone stages/commits that candidate and integration; no
  manual commit is permitted. Prove C0 is pinned before publication,
  the independent authority survives gateway restart, it performs artifact
  creation/local-main/push/activation, and only after R1 is live/completed does
  the controller selector CAS from C0 to R1. Verify new PID/start identity, full
  commit and artifact digest, selector/plist identity, authenticated readiness,
  canary, bounded clean logs, and a final exact local/remote reread.
- [ ] **Step 9: Prove rollback readiness and terminal state.** A retained prior
  verified selector must be restorable. Report done only when the final proof
  event is `live_verified` and the plan row is `completed_verified`.

Dependency order is strict: Tasks 1-3 establish state authority; Tasks 4-5
produce candidates; Tasks 6-7 create tested/reviewed integration; Task 8
establishes the protected promoter; Task 9 freezes the release artifact before
Task 10 performs any local/remote mutation; Task 11 establishes live proof,
rollback, and controller succession; Task 12 wires operator/runtime surfaces;
Task 13 is the release gate.
