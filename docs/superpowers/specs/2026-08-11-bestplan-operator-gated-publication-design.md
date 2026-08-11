# BestPlan Operator-Gated Publication Design

## Decision

Replace the remaining unattended live-promotion roadmap with a smaller,
operator-gated publication flow.

BestPlan will:

1. produce isolated candidate commits with the existing Tasks 1-5 pipeline;
2. integrate and run the existing Task 6 checks;
3. obtain a strict review bound to the exact integration commit;
4. freeze a content-addressed publication manifest;
5. show the exact local and remote publication target to the operator;
6. record a short-lived approval for that exact manifest;
7. publish only when a foreground command consumes that approval;
8. stop after it independently verifies the authorized remote ref.

BestPlan will not activate a service, restart the gateway, claim that published
code is live, or mark the plan `completed_verified`.

This design supersedes Tasks 7-13 of the earlier live-promotion plan. It keeps
the existing Tasks 1-6 guarantees. It deliberately defers the root authority,
independent daemon, trusted launcher, live attestation, automatic activation,
automatic rollback, and exhaustive crash matrix.

## Product claim

The reduced release proves this statement:

> Publication occurred only after an explicit operator approval, and it moved
> the approved local and remote refs to the exact checked and reviewed
> integration commit without force or loss of protected working-tree changes.

It does not prove that a service runs that commit.

## Trust model

The reduced release trusts:

- the signed-in macOS user and the foreground Hermes CLI process;
- the local operating system and Git executable;
- the configured model providers used by the foreground host relay;
- the authorized Git remote and its normal non-force receive rules;
- the operator who confirms the exact publication packet.

It distrusts candidate and reviewer processes, candidate code, model prose,
mutable worktrees, remote aliases, and historical success receipts.

A process that already controls the same macOS user can change local state,
use that user's Git credentials, or replace the foreground process. Protection
from that attacker requires the deferred root-owned authority. Root compromise
is also outside this reduced claim.

Candidate and reviewer subprocesses still receive no provider, Git, SSH,
deployment, or cloud credentials. A same-user foreground host relay owns model
credentials and forwards only bounded, capability-bound model requests. The
foreground authority may use the operator's Git authentication for read-only
remote observation during capture and approval preparation. It may request a
remote write only after the operator approves the frozen publication packet.

## Supported scope

The first reduced release supports:

- interactive Hermes CLI sessions only;
- one repository per plan;
- local and remote targets fixed to `refs/heads/main`;
- the existing maximum of two independent candidate slices;
- current Task 4 macOS candidate containment;
- current Task 6 integration and sandboxed checks;
- candidate and review lanes supported by the existing bounded brokered-chat
  transport;
- explicit non-force publication;
- foreground retry and reconciliation;
- one local, same-user enrollment per repository.

The gateway and messaging surfaces cannot approve or start publication. They
remain read-only or candidate-only. `auto_live` is disabled for this release.

The release fails closed for unsupported repositories already rejected by
Tasks 1-6, dependency graphs, ambiguous model routes, unavailable reviewers,
non-interactive approval without an exact digest, observed pre-effect remote
drift, dirty-path overlap, and any identity mismatch.

## Approaches considered

### Reviewed bundle only

This would stop after checks and review. It is the smallest option, but it does
not deliver the publication value that the operator selected.

### Operator-gated publication

This is the selected option. It keeps the high-value review and publication
steps while trusting the same user account and requiring an explicit terminal
approval.

### Root-owned unattended promotion

This preserves the strongest original security claim, but it requires the
deferred authority, IPC, credential broker, durable controller succession,
activation, rollback, and live proof. It is outside this release.

## Components

### Local operator authority

Add a same-user, foreground authority adapter for CLI use. It supplies the two
production dependencies that Tasks 4-5 currently accept only through injected
test seams:

- the retained controller artifact and `BestplanHostRuntime`;
- a capability-bound model relay for candidate and reviewer subprocesses.

The adapter is not a daemon. It has no listening network service. It keeps
model capabilities in memory and revokes them when each child exits. It binds
each capability to one canonical route record: lane, provider, model,
normalized endpoint, API mode, route fingerprint, process identity, request
budget, token budget, and expiry. Credentials are held only by the foreground
host and are not part of that record. Ambiguous or unsupported provider routes
fail before launch.

One CLI-owned factory in `agent/bestplan_local_authority.py` is the sole
production constructor. It loads the local enrollment, rebuilds and verifies
the retained `BestplanHostRuntime`, constructs the relay, and attaches
`candidate_host_runtime` and `bestplan_authority_client` to the CLI agent
before BestPlan capture or `/go`. The execution, approval, publication, and
retry commands use the same factory. Read-only `status` uses a separate pure
reader and does not construct the relay. CLI shutdown revokes every capability
and closes the relay. Gateway, messaging, background, and scheduler
construction paths never call this factory and never receive publication
authority.

One explicit local enrollment command creates a content-addressed controller
export outside the primary repository, pins the interpreter and runtime read
paths, binds the exact check runtime, validates the check and review policy,
records the normalized credential-free remote identity and refs, binds finite
preparation and publication timeout policies, and writes a non-secret local
enrollment under the Hermes home. It then writes only a versioned
local-authority pointer and enrollment reference to `config.yaml`. The pointer
cannot name a network endpoint.

The local enrollment stores stable repository, remote, controller, command,
and runtime policy. It does not freeze a mutable remote tip for all future
plans. At each plan capture, the local authority observes the approved remote
ref and binds that object ID into the new contract and digest. Missing,
ambiguous, or changed remote identity blocks capture.

Every use rehashes the retained controller and pinned runtime. A mismatch
blocks before a worker starts. Refreshing that controller is a separate
explicit operator action; publication does not advance it automatically.

### Candidate binding persistence

Publication must work after the original candidate process and CLI have
exited. Protocol 2 therefore gains an append-only candidate-integration
binding record for each manifest slice. Its canonical non-secret fields are:

- manifest index and original manifest slice ID;
- candidate, slice, and attempt IDs;
- exact private candidate ref, commit, tree, and base object IDs;
- sorted lossless `changed_paths_hex` values;
- approval, contract, source, sandbox-policy, and candidate-receipt digests;
- controller ID, repository ID, release object ID, and artifact digest;
- the recomputed candidate-integration binding digest.

A single proof-ledger transaction stores the complete manifest-ordered set of
candidate receipts and binding records, verifies that no extra or conflicting
row exists, and then appends the one `candidate_ready` authority event. A crash
cannot expose `candidate_ready` with a partial binding set. Publication reloads
these rows instead of live `FrozenCandidate` objects or redacted prose. It
recomputes every digest, reconstructs `CandidateIntegrationBinding`, and
rereads the exact private ref, commit, sole parent, tree, and changed paths
before integration.

### Commit-bound review

Add a strict review packet and receipt. The host constructs the packet from:

- the exact integration commit and tree;
- the exact target-to-integration diff;
- the approved execution contract and source snapshot digest;
- candidate and check receipt digests;
- project contribution rules;
- the requested acceptance criteria.

The retained controller launches the exact pinned
`contract.review.command`. That static worker imports no code from the
integration tree. The host passes the dynamic packet through a bounded
inherited channel rather than changing the approved command. The worker uses
the exact bound review lane through the same-user relay. It receives no file
or terminal tools, provider credentials, or publication credentials, and it
must return one canonical JSON object. The response names the exact integration
object ID and packet digest. Findings use only `critical`, `high`, `medium`, or
`low`.

Critical or high findings block. Timeout, unavailable routing, malformed JSON,
unknown severity, missing fields, output overflow, or an identity mismatch
also blocks. Medium and low findings remain visible evidence but do not block.
Any change to the integration commit invalidates the review.

### Publication manifest

After checks and review pass, the host creates one canonical publication
manifest containing only non-secret identities and digests:

- plan, approval, contract, source, and candidate-set digests;
- integration commit, tree, first parent, and integration ref;
- ordered candidate, check, and review receipt digests;
- changed-path digest and expected-artifact digests;
- local target ref and expected old object ID;
- display-only remote name, exact normalized credential-free push URL, its
  fingerprint, remote ref, and expected old object ID;
- publication policy, timeout-policy, and schema versions.

The SHA-256 digest of the canonical bytes is the `artifact_digest`. The host
stores those bytes at a content-addressed, mode-0600 path under the Hermes home
and rereads them before each mutation. Existing bytes at that digest must be
identical. This is an audit manifest, not a deployable source bundle.

### Operator approval

`hermes bestplan approve <plan-id>` runs or reuses integration, checks, review,
and manifest preparation in the foreground. It then prints a compact summary
with the exact integration object ID, artifact digest, changed paths, check
result, review findings, local target, and remote target.

In a terminal, the operator confirms a prompt bound to the full artifact
digest. Confirmation writes a short-lived durable approval receipt containing
the artifact and integration digests, local and remote refs and approved old
object IDs, remote identity fingerprint, issue and expiry times, and a receipt
digest. It also binds the exact normalized push URL and timeout-policy digest.
The receipt is single-use except for reconciliation of its already recorded
effect intent. In a non-interactive shell, preparation stops without approval
and prints the exact follow-up form:

```text
hermes bestplan approve <plan-id> --digest <full-artifact-digest>
```

There is no unbound `--yes` option. `hermes bestplan publish <plan-id>` never
prompts and consumes only that exact stored receipt. Approval expires before
the first external effect. Once a matching effect intent is durable, retry may
reconcile that same exact operation after expiry. A different integration,
artifact, local tip, pre-effect remote tip, ref, or remote identity requires a
new manifest and approval.

### Publication coordinator

The foreground coordinator uses a same-user lock keyed by canonical Git common
directory identity. The lock reduces accidental concurrent work; the local
object-ID compare-and-swap and remote fast-forward plus final observation remain
the authority.

The order is:

1. revalidate the stored plan, contract, source, candidate set, controller,
   integration ref, checks, review receipt, manifest bytes, local ref, exact
   remote URL and fingerprint, approved remote old object ID, and protected
   ambient state;
2. record a durable local-publication intent;
3. advance local `main` only from the approved old object ID to the integration
   object ID;
4. reread local `main`, the integration commit, and protected ambient state;
5. record the local receipt and `main_fast_forwarded` proof event;
6. reread the remote and require the approved old object ID;
7. record a durable remote-publication intent;
8. push the explicit `<integration-oid>:refs/heads/main` refspec to the frozen
   URL without force;
9. fetch the authorized remote ref from the frozen URL into a private
   observation ref and require the exact integration object ID;
10. record the remote receipt and `remote_verified` proof event.

For a checked-out local `main`, publication uses `git merge --ff-only` with an
empty host-owned hooks directory and autostash disabled. It does so only after
the source boundary proves every incoming path is disjoint from staged,
unstaged, and untracked protected work. It rejects checkout filters and
`working-tree-encoding` on incoming paths. After the update it requires the
logical staged, unstaged, untracked, mode, symlink, and byte state of every
protected path to match the pre-effect snapshot. It does not require unrelated
index entries changed by the fast-forward to remain byte-identical. For a
target ref that is not checked out, it uses
`update-ref <new> <expected-old>`.

An interrupted checked-out fast-forward is not repaired automatically. Retry
may adopt it only if `HEAD` equals the integration commit, incoming index and
worktree state match that commit, and protected state is exact. It may retry
from the old object ID only if the full captured pre-effect state is exact.
Every partial or ambiguous state is quarantined for manual reconciliation.

The remote guarantee is precise: immediately before push, the publisher
observes and requires the approved old remote object ID. It then requests a
normal fast-forward, non-force update and independently fetches the ref and
requires the exact integration object ID. Under correct Git receive rules it
cannot overwrite a remote tip that is not an ancestor of the integration
commit. It does not prove that the approved old object ID was still present at
the instant of the server update: a concurrent move to another ancestor of the
integration commit can be accepted. Exact server-side old-object
compare-and-swap would require a lease or server API and is outside this
literal non-force design.

Every remote observation, push, and fetch uses the exact normalized,
credential-free URL stored in the contract, manifest, and approval receipt.
The configured remote name is display-only and is never a Git operand.
Fingerprint comparison brackets each operation. Enrollment accepts only the
explicit `file`, `ssh`, and `https` transports; helper-style and unknown
transport schemes fail before approval.

### Bounded execution and cancellation

Enrollment binds finite preparation and publication durations and a cleanup
reserve into the contract. Their policy digest is also bound into the manifest
and approval receipt. Each `enroll`, `approve`, `publish`, or `retry` command
derives one monotonic absolute deadline and one cancellation event from that
policy. It passes those exact control objects through controller and runtime
hashing, source scans, model relay calls, review, Git operations, credential
helpers, observation, and cleanup. No nested operation creates a fresh budget.

Every spawned process starts in an owned process group. Timeout, Ctrl-C, or CLI
shutdown closes admission, terminates and reaps the group within the reserved
part of the same deadline, and then observes the affected state. Cancellation
before an effect intent has no target-ref effect. Cancellation after an intent
adopts only an exact completed effect; otherwise it retains evidence and
quarantines the operation. Failure to prove process extinction is itself a
quarantined failure and never produces a success receipt.

### Durable state and retry

Add a separate operator-publication ledger. It stores the immutable operation
fingerprint, approval receipt and expiry, expected and observed local and
remote object IDs, effect intents, receipts, status, and fixed error code. It
stores no credential or raw command output. Candidate-integration binding rows
remain in the proof ledger because they are prerequisites of the
`candidate_ready` authority event; publication intents remain in this separate
operator ledger.

Every effect follows:

```text
record intent -> perform effect -> observe exact result -> record receipt
```

`hermes bestplan retry <plan-id>` never repeats candidate generation or adopts
new evidence. For an incomplete local intent it observes local `main`: the old
object ID permits one retry, the integration object ID permits adoption, and
anything else becomes stale. For an incomplete remote intent it observes the
remote: the old object ID permits one non-force retry, the integration object
ID permits adoption, and anything else becomes a remote mismatch.

There is no automatic rollback. If local publication succeeds but remote
publication does not, status reports that split state and retry reconciles it.
Ambiguous state is quarantined for operator action.

### Proof and terminal state

Add `operator_publish` as an explicit promotion mode and bump the exact
enrollment and contract schema versions. An `operator_publish` enrollment
requires a controller and zero live targets; its contract serializes
`live_target: null`. `auto_live` retains exactly one fully bound live target.
`candidate_only` keeps its existing compatibility shape and still stops at
`candidate_ready`. Existing contracts are never silently reinterpreted or
downgraded; they must be recaptured under the new schema and mode.

Integration accepts `operator_publish` as well as the existing `auto_live`
mode, while binding the selected mode into its receipt. Candidate-only plans
cannot enter integration or publication.

The existing proof phases through `remote_verified` remain valid. One
mode-aware invariant applies in event append validation, SQLite triggers,
replay, rebuild, terminal verification, and status projection:

- `operator_publish + remote_verified` projects `completed_unverified`;
- `auto_live + remote_verified` remains running;
- `candidate_only` cannot publish.

The operator row keeps `current_phase=remote_verified` and
`remote_verified_at` for precise status. This is truthful: the approved
publication completed, but no live target was verified.

The reduced flow never writes `live_verified`, never calls the verified setter,
and never produces `completed_verified`, `AuthorityVerification`, or a live
verification receipt. Proof replay rejects any such relation for
`operator_publish` without weakening the existing `auto_live` rules.

### CLI surface

The reduced CLI is:

- `hermes bestplan lanes` — existing lane validation;
- `hermes bestplan enroll --policy <path>` — create or refresh the same-user
  local enrollment and retained controller after rendered confirmation;
- `hermes bestplan status [plan-id]` — read-only local phase, exact identities,
  approval state, and blocker summary;
- `hermes bestplan approve <plan-id> [--digest <digest>]` — prepare and approve
  one exact, short-lived packet;
- `hermes bestplan publish <plan-id>` — consume the stored approval and publish
  that packet in the foreground;
- `hermes bestplan retry <plan-id>` — reconcile one already-approved partial
  publication.

`status` opens SQLite read-only and does not run migrations, tracker
reconciliation, remote requests, or config writes. It labels the final state
as `published; live deployment not performed`.

No slash command, gateway handler, background scheduler, enrollment daemon,
activation command, or rollback command is added.

## Failure behavior

Before operator approval, every failure has zero local-target or remote-target
ref effect. Private candidate and integration refs and bounded evidence can be
retained. Check or review failures preserve their bounded evidence and stop.

Before the local effect, target, contract, candidate, review, manifest, dirty
state, or observed pre-effect remote drift invalidates approval. A remote move
that races after the final read follows the narrower non-force guarantee above.
After an effect intent, retry can adopt only the exact intended object ID. A
different object ID never inherits approval.

The publisher never runs stash, reset, clean, rebase, force-push, or an ambient
auto-commit. It never deletes candidate, integration, manifest, or publication
evidence needed to explain an incomplete operation.

Errors shown to the operator use fixed codes and bounded redacted detail.
Full raw Git or model output is hashed in memory and is not persisted as a
fallback.

## Test strategy

Use test-driven implementation and keep the following release gates:

1. Review tests prove the exact approved command, retained-controller origin,
   commit and packet binding, blocking severities, malformed/unavailable
   behavior, and credential-free no-tool isolation.
2. Local-authority tests prove retained controller/runtime pinning, route-bound
   capability accounting, production CLI construction and shutdown, revocation,
   and no worker/reviewer credentials.
3. Real-Git publication tests use temporary clones and a bare remote. They
   cover clean and dirty-disjoint fast-forward, staged/unstaged/untracked and
   case/Unicode overlap rejection, filter rejection, interrupted checked-out
   update quarantine, local compare-and-swap failure, non-force push, the
   permitted concurrent-ancestor race, non-ancestor rejection, exact
   post-fetch, frozen-URL use despite remote-name retargeting, and
   protected-state equality.
4. Recovery tests cover a crash before local effect, after local effect before
   receipt, during a checked-out update, before push, and after push before
   receipt. They prove exact observe-before-retry and quarantine on mismatch.
5. Persistence tests restart after candidate-ready and reconstruct the exact
   manifest-ordered `CandidateIntegrationBinding` set without model output or
   live worker objects.
6. CLI tests prove the production-shaped capture-to-`/go` authority factory,
   read-only status, separate durable exact-digest approval, non-interactive
   no-effect preparation, expired approval, one propagated absolute deadline
   and cancellation event, process-group extinction, and fixed redacted
   errors.
7. One end-to-end test drives candidates through integration, checks, review,
   manifest, approval, local publication, remote publication, and final
   `remote_verified` state. It asserts no live or activation call occurs.
8. Run the focused BestPlan suite, then the complete project suite before
   release.

This intentionally omits the original exhaustive test of every possible
intent/effect/receipt boundary and any real live rollout.

## Expected files

Create:

- `agent/bestplan_local_authority.py`
- `agent/bestplan_review.py`
- `agent/bestplan_publication.py`
- `tests/agent/test_bestplan_local_authority.py`
- `tests/agent/test_bestplan_review.py`
- `tests/agent/test_bestplan_publication.py`
- `tests/agent/test_bestplan_operator_end_to_end.py`

Modify:

- `agent/bestplan_authority_client.py`
- `agent/bestplan_contract.py`
- `agent/bestplan_proof.py`
- `agent/bestplan_state.py`
- `agent/bestplan_worker.py`
- `agent/bestplan_promotion.py`
- `tools/delegate_tool.py`
- `hermes_cli/subcommands/bestplan.py`
- `hermes_cli/cli_agent_setup_mixin.py` and `cli.py`, which attach and close the
  local authority for interactive CLI use only;
- BestPlan config defaults and validation for the versioned local pointer;
- focused contract, proof, candidate, promotion, host-ingress, and CLI tests;
- BestPlan CLI documentation.

Do not create or modify authority-daemon, gateway-live, launcher, activation,
rollback, or deployment-service modules.

## Delivery slices and estimate

1. Same-user local enrollment, retained controller, model relay, and CLI
   construction: 2-3 days.
2. Durable candidate bindings and commit-bound strict review: 2-3 days.
3. Manifest, separate operator approval, and proof projection: 2-3 days.
4. Dirty-preserving local and non-force remote publication with retry: 4-6 days.
5. CLI, one end-to-end test, focused suite, and full-suite fixes: 2-3 days.

Expected total: 12-18 focused engineering days. The local authority is part of
this estimate. Root separation and live deployment remain deferred.

## Acceptance

The reduced release is complete only when:

- production CLI candidate execution has a real retained controller and model
  relay rather than a test-only injected host;
- candidate-ready persists and reloads every exact integration binding without
  depending on redacted output or a live worker;
- candidate and reviewer subprocesses contain no provider or publication
  credential;
- the reviewer runs the exact approved command from the retained controller;
- checks and review bind the exact integration commit;
- critical/high or malformed review output blocks before publication;
- the content-addressed publication manifest rereads to its recorded digest;
- approval binds the exact manifest, integration, local target, and remote
  target in a durable short-lived receipt and expires before an unstarted
  effect;
- every preparation, publication, retry, and cleanup operation shares its
  bound absolute deadline and cancellation event and leaves no live child;
- protected dirty work remains exact and overlapping incoming paths block;
- checked-out local `main` advances by a hook-free, no-autostash fast-forward
  only from the approved old object ID, or ambiguous partial state is
  quarantined;
- the push is explicit and non-force;
- every remote operation uses the approved frozen URL, never the mutable remote
  alias;
- an independent fetch observes the authorized remote ref at the exact
  integration object ID;
- crash retry adopts only an exact intended effect and quarantines ambiguity;
- `status` is read-only and clearly says that publication is not live
  deployment;
- the terminal plan is `completed_unverified` at `remote_verified`;
- no code path writes `live_verified` or `completed_verified` for this mode;
- focused BestPlan tests and the complete project suite pass.
