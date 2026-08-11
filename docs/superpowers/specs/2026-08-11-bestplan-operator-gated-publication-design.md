# BestPlan Operator-Gated Publication Design

## Decision

Replace the remaining unattended live-promotion roadmap with a smaller,
operator-gated publication flow.

BestPlan will:

1. produce isolated candidate commits with the existing Tasks 1-5 pipeline;
2. integrate and run the existing Task 6 checks;
3. obtain a strict review bound to the exact integration commit;
4. freeze a content-addressed publication manifest and receipt;
5. show the exact local and remote publication target to the operator;
6. publish only after an explicit terminal approval;
7. stop after it independently verifies the authorized remote ref.

BestPlan will not activate a service, restart the gateway, claim that published
code is live, or mark the plan `completed_verified`.

This design supersedes Tasks 7-13 of the earlier live-promotion plan. It keeps
the existing Tasks 1-6 guarantees. It deliberately defers the root authority,
independent daemon, trusted launcher, live attestation, automatic activation,
automatic rollback, and exhaustive crash matrix.

## Product claim

The reduced release proves this statement:

> One explicit operator action published the exact checked and reviewed
> integration commit to the approved local and remote refs without force and
> without discarding protected working-tree changes.

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
publisher may use the operator's existing Git authentication only after the
operator approves the frozen publication packet.

## Supported scope

The first reduced release supports:

- interactive Hermes CLI sessions only;
- one repository per plan;
- local and remote targets fixed to `refs/heads/main`;
- the existing maximum of two independent candidate slices;
- current Task 4 macOS candidate containment;
- current Task 6 integration and sandboxed checks;
- OpenAI-compatible chat-completions candidate and review lanes;
- explicit non-force publication;
- foreground retry and reconciliation;
- one local, same-user enrollment per repository.

The gateway and messaging surfaces cannot approve or start publication. They
remain read-only or candidate-only. `auto_live` is disabled for this release.

The release fails closed for unsupported repositories already rejected by
Tasks 1-6, dependency graphs, ambiguous model routes, unavailable reviewers,
non-interactive publication without an exact digest, remote drift, dirty-path
overlap, and any identity mismatch.

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
each capability to one resolved route, provider, model, process identity,
request budget, token budget, and expiry. Ambiguous or unsupported provider
routes fail before launch.

One explicit local enrollment command creates a content-addressed controller
export outside the primary repository, pins the interpreter and runtime read
paths, validates the check and review policy, records the normalized
credential-free remote identity, and writes a non-secret local enrollment. It
then writes only the local authority pointer and enrollment reference to
`config.yaml`.

Every use rehashes the retained controller and pinned runtime. A mismatch
blocks before a worker starts. Refreshing that controller is a separate
explicit operator action; publication does not advance it automatically.

### Commit-bound review

Add a strict review packet and receipt. The host constructs the packet from:

- the exact integration commit and tree;
- the exact target-to-integration diff;
- the approved execution contract and source snapshot digest;
- candidate and check receipt digests;
- project contribution rules;
- the requested acceptance criteria.

The isolated reviewer receives no file or terminal tools. It receives the
bounded packet through the same-user model relay and must return one canonical
JSON object. The response names the exact integration object ID and packet
digest. Findings use only `critical`, `high`, `medium`, or `low`.

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
- remote name, normalized push-identity fingerprint, remote ref, and expected
  old object ID;
- publication policy and schema versions.

The SHA-256 digest of the canonical bytes is the `artifact_digest`. The host
stores those bytes at a content-addressed, mode-0600 path under the Hermes home
and rereads them before each mutation. Existing bytes at that digest must be
identical. This is an audit manifest, not a deployable source bundle.

### Operator approval

`hermes bestplan publish <plan-id>` runs integration, checks, review, and
manifest preparation in the foreground. It then prints a compact summary with
the exact integration object ID, artifact digest, changed paths, check result,
review findings, local target, and remote target.

In a terminal, the operator confirms a prompt bound to the full artifact
digest. In a non-interactive shell, preparation stops without mutation and
prints the exact follow-up form:

```text
hermes bestplan publish <plan-id> --approve <full-artifact-digest>
```

There is no unbound `--yes` option. Approval expires before the first external
effect. Once a matching effect intent is durable, retry may reconcile that
same exact operation after expiry. A different integration, artifact, local
tip, remote tip, ref, or remote identity requires a new manifest and approval.

### Publication coordinator

The foreground coordinator uses a same-user lock keyed by canonical Git common
directory identity. The lock reduces accidental concurrent work; the local
and remote compare-and-swap checks remain the authority.

The order is:

1. revalidate the stored plan, contract, source, candidate set, controller,
   integration ref, checks, review receipt, manifest bytes, local ref, remote
   identity, and protected ambient state;
2. record a durable local-publication intent;
3. advance local `main` only from the approved old object ID to the integration
   object ID;
4. reread local `main`, the integration commit, and protected ambient state;
5. record the local receipt and `main_fast_forwarded` proof event;
6. reread the remote and require the approved old object ID;
7. record a durable remote-publication intent;
8. push the explicit `<integration-oid>:refs/heads/main` refspec without force;
9. fetch the authorized remote ref into a private observation ref and require
   the exact integration object ID;
10. record the remote receipt and `remote_verified` proof event.

For a checked-out local `main`, publication uses a fast-forward-only Git update
with hooks and autostash disabled, but only after the existing source boundary
proves every incoming path is disjoint from staged, unstaged, and untracked
protected work. It then requires the protected files, modes, links, and index
state to remain exact. For a target ref that is not checked out, it uses
`update-ref <new> <expected-old>`.

The remote guarantee is precise: the publisher first observes the approved old
remote object ID, performs a normal non-force push, and then independently
fetches the ref and requires the exact integration object ID. A concurrent
change causes rejection or a post-fetch mismatch. The design does not claim a
server-side old-object compare-and-swap beyond the remote's normal non-force
receive rule.

### Durable state and retry

Add a separate operator-publication ledger. It stores the immutable operation
fingerprint, approval issue and expiry times, expected and observed local and
remote object IDs, effect intents, receipts, status, and fixed error code. It
stores no credential or raw command output.

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

Add `operator_publish` as an explicit promotion mode. `candidate_only` still
stops at `candidate_ready`. Existing `auto_live` contracts are not silently
downgraded; they must be recaptured under the new mode.

The existing proof phases through `remote_verified` remain valid. For an
`operator_publish` contract only, `remote_verified` projects
`completed_unverified`. This is truthful: the approved publication completed,
but no live target was verified. The row keeps `current_phase=remote_verified`
and `remote_verified_at` for precise status.

The reduced flow never writes `live_verified`, never calls the verified setter,
and never produces `completed_verified` or a live verification receipt.

### CLI surface

The reduced CLI is:

- `hermes bestplan lanes` — existing lane validation;
- `hermes bestplan enroll --policy <path>` — create or refresh the same-user
  local enrollment and retained controller after rendered confirmation;
- `hermes bestplan status [plan-id]` — read-only local phase, exact identities,
  approval state, and blocker summary;
- `hermes bestplan publish <plan-id> [--approve <digest>]` — prepare, approve,
  and publish one exact packet;
- `hermes bestplan retry <plan-id>` — reconcile one already-approved partial
  publication.

`status` opens SQLite read-only and does not run migrations, tracker
reconciliation, remote requests, or config writes. It labels the final state
as `published; live deployment not performed`.

No slash command, gateway handler, background scheduler, enrollment daemon,
activation command, or rollback command is added.

## Failure behavior

Before operator approval, every failure has zero local-ref or remote-ref
effect. Check or review failures preserve their bounded evidence and stop.

Before the local effect, target, contract, candidate, review, manifest, dirty
state, or remote drift invalidates approval. After an effect intent, retry can
adopt only the exact intended object ID. A different object ID never inherits
approval.

The publisher never runs stash, reset, clean, rebase, force-push, or an ambient
auto-commit. It never deletes candidate, integration, manifest, or publication
evidence needed to explain an incomplete operation.

Errors shown to the operator use fixed codes and bounded redacted detail.
Full raw Git or model output is hashed in memory and is not persisted as a
fallback.

## Test strategy

Use test-driven implementation and keep the following release gates:

1. Review tests prove exact commit and packet binding, blocking severities,
   malformed/unavailable behavior, and credential-free no-tool isolation.
2. Local-authority tests prove retained controller/runtime pinning, route-bound
   capability accounting, revocation, and no worker/reviewer credentials.
3. Real-Git publication tests use temporary clones and a bare remote. They
   cover clean and dirty-disjoint fast-forward, staged/unstaged/untracked and
   case/Unicode overlap rejection, local compare-and-swap failure, non-force
   push, remote race/rejection, exact post-fetch, and protected-state equality.
4. Recovery tests cover a crash before local effect, after local effect before
   receipt, before push, and after push before receipt. They prove exact
   observe-before-retry and quarantine on mismatch.
5. CLI tests prove read-only status, exact-digest approval, non-interactive
   no-effect preparation, expired approval, and fixed redacted errors.
6. One end-to-end test drives candidates through integration, checks, review,
   manifest, approval, local publication, remote publication, and final
   `remote_verified` state. It asserts no live or activation call occurs.
7. Run the focused BestPlan suite, then the complete project suite before
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
- the CLI construction seam that attaches the local authority to the parent
  agent;
- focused contract, proof, candidate, promotion, host-ingress, and CLI tests;
- BestPlan CLI documentation.

Do not create or modify authority-daemon, gateway-live, launcher, activation,
rollback, or deployment-service modules.

## Delivery slices and estimate

1. Same-user local enrollment, retained controller, and model relay: 2-3 days.
2. Commit-bound strict review: 2-3 days.
3. Manifest, operator approval, and proof projection: 2-3 days.
4. Dirty-preserving local and non-force remote publication with retry: 4-6 days.
5. CLI, one end-to-end test, focused suite, and full-suite fixes: 2-3 days.

Expected total: 12-18 focused engineering days. The local authority is part of
this estimate. Root separation and live deployment remain deferred.

## Acceptance

The reduced release is complete only when:

- production CLI candidate execution has a real retained controller and model
  relay rather than a test-only injected host;
- candidate and reviewer subprocesses contain no provider or publication
  credential;
- checks and review bind the exact integration commit;
- critical/high or malformed review output blocks before publication;
- the content-addressed publication manifest rereads to its recorded digest;
- approval binds the exact manifest, integration, local target, and remote
  target and expires before an unstarted effect;
- protected dirty work remains exact and overlapping incoming paths block;
- local `main` advances only from the approved old object ID;
- the push is explicit and non-force;
- an independent fetch observes the authorized remote ref at the exact
  integration object ID;
- crash retry adopts only an exact intended effect and quarantines ambiguity;
- `status` is read-only and clearly says that publication is not live
  deployment;
- the terminal plan is `completed_unverified` at `remote_verified`;
- no code path writes `live_verified` or `completed_verified` for this mode;
- focused BestPlan tests and the complete project suite pass.
