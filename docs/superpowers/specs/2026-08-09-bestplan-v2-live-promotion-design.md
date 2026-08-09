# BestPlan V2 Live Promotion Design

## Decision

Repair BestPlan as two separately trusted planes:

1. model workers produce isolated, host-frozen candidate commits in parallel;
2. one Hermes-owned promoter serializes integration, review, tests, local-main
   advancement, remote publication, live activation, and rollback per Git
   repository.

`completed_verified` means the enrolled live target passed every proof gate for
the frozen integration commit. Worker completion, a successful push, green
tests, or a reviewer verdict alone can never produce that state.

Legacy BestPlan envelopes remain candidate-only. A newly captured plan may be
upgraded to a host-attached V2 approval contract only when it matches a trusted
promotion enrollment in the separate top-level `bestplan_promotion` block in
`config.yaml`. The rendered plan must show the exact
publication and live target before bare `go` approves its combined digest.
Changing the enrollment requires operator action; routine promotions do not
require repeated approval after the contract and gates are unchanged.

## Scope

The first production version supports:

- one Git repository per plan;
- one local target branch and one authorized remote ref;
- up to the existing V1 maximum of two independent implementation slices;
- macOS `sandbox-exec` workers;
- the current Hermes Agent Mac gateway as the first live adapter;
- non-force fast-forward publication;
- one immutable source artifact per integration commit;
- automatic deployment-failure rollback and explicit complaint rollback;
- append-only proof events and idempotent retry after interruption.

The first version fails closed for submodules, Git LFS paths, sparse or shallow
checkouts, replacement refs, custom clean/smudge filters, dependency-manifest
changes that require rebuilding the Python environment, cross-repository
transactions, and more than one live target. These cases receive an explicit
unsupported or blocked state rather than a partial release.

ProofAgent Harness and Hardproof are not dependencies. A future adapter may
attach either report as `authoritative: false`; neither may mark a plan
verified.

## Trust model

V2 trusts the local operating system and administrator, the already-running
Hermes promoter code, the configured Git executable, the enrolled remote
service, and the enrolled live-target adapter. It distrusts:

- planner and worker prose;
- model worker processes;
- candidate code, tests, health handlers, and build scripts;
- mutable working trees and remote aliases;
- runtime self-report without host corroboration;
- locally mutable advisory-review reports.

Malicious local root, external non-repudiation, and mutually distrusting build
and deployment hosts are outside V2. Local hashes and the event chain provide
corruption detection and crash reconciliation, not protection from root.
Signing, Sigstore, in-toto, or CI OIDC belong to a later trust-domain upgrade.

The already-running trusted controller for release N-1 promotes release N.
Candidate N is never imported into, nor allowed to replace, the controller that
validates and activates it. A controller upgrade takes effect only after the
previous controller has completed that release and its live proof.

Promotion therefore does not run inside the gateway process it will restart.
The gateway queues a durable operation, and an independent promoter job starts
from the retained immutable controller artifact for N-1. On macOS the launcher
uses a supervised independent launchd job; tests use an injected launcher. The
promoter records its own PID, process-start identity, controller artifact, and
operation UUID before changing Git, remote, selector, or gateway state. A
gateway restart cannot terminate or replace the authority completing its proof.

## Host-attached approval contract

The model continues to emit the existing strict V1 execution envelope. During
capture, Hermes resolves the canonical Git repository and looks for exactly one
trusted enrollment under `bestplan_promotion.repositories` in `config.yaml`.
If no enrollment matches, the plan remains contract version 1 and can stop only
at `candidate_ready`.

For a matching enrollment, Hermes constructs an immutable contract version 2
containing:

- canonical repository root and Git common-directory identity;
- exact base commit and local target ref;
- source policy `head_only`;
- protected ambient-state manifest digest;
- remote name, normalized push URL fingerprint, remote ref, and observed ref;
- mandatory check identifiers and structured argument vectors;
- mandatory adversarial-review lane and blocking severity policy;
- live adapter, target/service identity, health probe, behavior canary, and
  rollback selector;
- promoter/controller identity and contract schema version;
- promotion mode `candidate_only` or `auto_live`.

The approval digest covers both the validated execution manifest and this
host-generated contract. The visible response renders all irreversible targets
and states that `go` authorizes automatic promotion only for that digest.
Existing rows without a V2 contract can never inherit auto-promotion through a
schema migration or configuration change.

Check vectors, timeouts, review policy, remote identity, and live probes are
host-owned enrollment inputs. Candidate code may add project tests, but it can
never delete, replace, relax, or otherwise weaken an enrolled gate for its own
promotion.

## Source and ambient-state contract

The committed base tree is the sole worker and release input. Existing staged,
unstaged, and untracked paths are protected ambient work; they are not copied
into worker sandboxes or artifacts.

Baseline capture records twice, until two consecutive reads agree:

- canonical root and common Git directory;
- symbolic HEAD, full base object ID, target ref and target object ID;
- separate binary staged and unstaged patches;
- index entries and relevant flags;
- every non-ignored untracked path with raw path, file type, mode, and content
  or symlink-target digest;
- the normalized set of protected paths and an aggregate digest.

Ignored files are outside the source boundary unless a later contract declares
an explicit read-only overlay. They are neither recursively scanned nor treated
as missing detached-worktree inputs. This removes the current collision with
runtime caches such as `.bytecode-fingerprint` and large ignored dependency
trees.

A write lease overlapping a protected path blocks dispatch. Before and after
local-main advancement, the promoter recaptures ambient state and requires the
protected manifest to be byte-identical. Any change or path overlap produces
`dirty_overlap` or `proof_stale` without stashing, resetting, cleaning, or
committing the user's work.

## Candidate production

Each slice receives a unique attempt directory exported from the exact base
commit. The model-visible directory contains no `.git` metadata. The worker:

- receives an allowlisted environment plus only its model credential;
- has no SSH agent, Git/GitHub/cloud/deployment credentials, inherited
  `PYTHONPATH`, or primary Hermes home;
- cannot read repository Git metadata or enrolled promoter state;
- can write only its normalized lease and private runtime directory;
- is terminated and reaped as a process group on timeout, cancellation, or
  malformed protocol output.

The host, not the worker, freezes a candidate. After all writers have stopped,
the host compares the exported filesystem against the base, validates raw
NUL-delimited changed paths (including both sides of renames), rejects symlink
escapes, hardlinks, `.git`, case/Unicode aliases, unsupported modes, and changes
outside the lease, then copies the accepted delta into a host-controlled
worktree at the base commit. It verifies expected artifacts, creates a
deterministic candidate commit, and anchors it using a compare-and-swap ref under
`refs/hermes-bestplan/<plan>/<slice>/<attempt>`.

Independent slices execute concurrently. The combined async unit remains
running until every process has terminated and every successful candidate has
been host-frozen. A worker summary is stored as advisory evidence only.

## Serialized promotion

Promotion is protected by a FIFO lease keyed by the canonical Git common
directory. Other plans may continue planning and producing candidates while a
promotion waits or runs.

At the head of the queue, the promoter:

1. revalidates the approved contract, controller identity, repository,
   enrollment, base, local target, remote URL/ref, and ambient manifest;
2. creates a clean temporary integration worktree from current local `main`;
3. applies every candidate in deterministic manifest order without automatic
   model conflict resolution;
4. creates one immutable integration commit `I` whose first parent is the
   current target tip;
5. verifies candidate ancestry and accepted leased-path/artifact digests;
6. runs the required host-owned checks at exactly `I` and rejects any tracked or
   non-ignored untracked test mutation (ignored runtime caches remain ambient);
7. runs the mandatory independent adversarial review against exactly `I`;
8. rechecks local/remote preconditions and protected ambient state;
9. advances local `main` by compare-and-swap fast-forward;
10. pushes explicit refspec `I:<remote-ref>` without force and fetches/queries
    the server to verify the resulting object ID;
11. activates and verifies the live artifact;
12. rereads local, remote, and live identity before terminal verification.

The terminal reread fetches the authorized remote ref again and proves it still
equals `I`, or is an explicitly permitted fast-forward descendant containing
`I`; a historical push receipt is not treated as current remote state.

If current `main` moved before step 9, the integration proof is stale. The
promoter rebuilds from the new tip and reruns checks and review. Merge conflicts
block for a new candidate; the promoter does not ask a model to resolve them
inside the trusted lane.

When the target branch is checked out in a dirty worktree, the promoter may
fast-forward it only after proving that incoming paths are disjoint from the
protected staged/unstaged/untracked set. It uses `--ff-only`, disables hooks and
autostash, and verifies the complete protected manifest afterward. If the
target branch is not checked out, it uses `update-ref <new> <expected-old>`.

## Mandatory adversarial review

Review occurs once, after the final integration commit and deterministic checks
are frozen, and before local-main advancement. The independent read-only
reviewer receives:

- exact integration commit and base-to-integration diff;
- approved execution and promotion contract;
- candidate and deterministic-check receipts;
- project contribution rules and requested acceptance criteria.

Its output is strict structured data naming `I` and categorizing findings as
critical, high, medium, or low. Critical and high findings block. Unavailable,
timed-out, malformed, or commit-mismatched output becomes `review_blocked`.
Any change to `I` invalidates review. A repair is a new candidate followed by
fresh integration, checks, and review. The review report hash is evidence, not
attestation, and the reviewer cannot mutate the integration tree or state.

## Live adapter and rollback

The Mac gateway adapter builds a content-addressed source artifact from the
clean integration tree and stores its manifest/digest before publication. It
uses the existing trusted Python environment only when dependency and installer
manifests are unchanged; otherwise it blocks as unsupported.

Enrollment creates a stable trusted launcher and selector outside the Git
checkout. The launcher clears inherited Python path overrides and starts Hermes
with the selected release source first on `sys.path`. The gateway LaunchAgent
points at that launcher. Each release changes only the atomic selector and then
performs a bounded drain/restart.

Before activation, the adapter records and verifies the previous selector,
artifact digest, service definition, PID/start identity, and health result.
After activation it requires:

- a new process start identity;
- the enrolled service and profile;
- selector and launch configuration bound to artifact `A`;
- runtime-reported full commit and artifact digest;
- independently recomputed artifact and selector digest;
- authenticated detailed readiness;
- a deterministic, model-free BestPlan capture/dispatch canary;
- no new startup error in the bounded observation window.

Activation failure atomically restores the previous selector and service
definition, restarts, and verifies the old artifact and health. That terminal
state is `rolled_back`, never completed. Complaint rollback uses the same
verified last-good receipt. Source-history rollback is a new tested revert
commit through the normal promotion lane; it never resets or force-pushes.

## Durable state and recovery

`bestplan_plans` retains compatibility fields for existing readers but gains
contract version, promotion contract, source snapshot, integration, artifact,
and verification timestamps. New normalized tables store immutable candidates
and append-only proof events.

Each proof event contains plan ID, sequence, phase, operation UUID, expected old
identity, intended new identity, observed receipt, verifier/controller version,
timestamp, previous-event hash, and event hash. The hash chain detects accidental
rewrites but is not presented as malicious-admin protection.

Every external side effect follows intent then receipt:

```text
append intent -> perform idempotent/CAS side effect -> observe -> append receipt
```

Startup and `hermes bestplan retry` reconcile incomplete intents by rereading
Git, remote, selector, process, and health state. An exact observed effect is
adopted; an absent effect is retried only when safe; ambiguous or mismatched
state is quarantined. PID liveness includes process start identity so PID reuse
cannot recover a dead owner as live.

Visible success phases are:

```text
v2_approved
-> candidates_proven
-> integration_frozen
-> tests_proven
-> adversarial_review_passed
-> local_main_advanced
-> remote_verified
-> live_verified
```

Failure/block states include `dirty_overlap`, `candidate_failed`,
`integration_conflict`, `tests_failed`, `review_blocked`, `proof_stale`,
`push_rejected`, `remote_mismatch`, `pushed_not_live`, `live_failed`,
`rolled_back`, `unsupported_repository`, and `quarantined`.

There is no public unconditional verified setter. One validator performs a
compare-and-swap to `completed_verified` only from a fresh `live_verified`
event after recomputing every terminal invariant. Verification is point-in-time
and records `verified_at`; later monitoring may append `drifted` without
rewriting history.

## Operator surface

The minimal CLI is:

- `hermes bestplan lanes` — existing orchestration validation;
- `hermes bestplan enroll ...` — one-time trusted repository/remote/live target
  enrollment with a rendered confirmation;
- `hermes bestplan status [plan-id]` — phase, queue position, exact identities,
  and blockers;
- `hermes bestplan retry <plan-id>` — reconcile/retry an idempotent blocked or
  interrupted promotion;
- `hermes bestplan rollback <release-id>` — complaint rollback to a verified
  retained artifact.

Enrollment is also the bootstrap trust ceremony. It resolves and displays the
exact repository, target ref, normalized credential-free push identity, live
service, checks, and rollback target; snapshots the currently trusted
controller into a content-addressed retained artifact; installs and verifies
the independent promoter job plus stable gateway launcher; and only then writes
an active `auto_live` enrollment. A partial bootstrap remains disabled and is
safe to retry.

Secrets never enter enrollment, plan rows, proof events, logs, or receipts.
Commands and outputs are recorded as structured argument vectors, exit codes,
bounded redacted summaries, and full-output digests.

## Acceptance

Implementation is complete only when all of the following are demonstrated:

- the ignored `.bytecode-fingerprint` and large ignored dependency trees no
  longer make a HEAD-only plan non-executable;
- dirty staged, unstaged, untracked, symlink, mode, and path-overlap cases are
  characterized and protected;
- two slices run concurrently in distinct no-`.git` directories and produce
  host-created candidate commits/refs;
- worker environments contain no publication/deployment credentials and timeout
  cleanup leaves no writer process;
- legacy rows and V1 plans remain candidate-only;
- V2 approval changes when any source, remote, check, review, target, or rollback
  field changes;
- concurrent plans serialize only promotion and stale candidates are rebuilt or
  blocked;
- checks and review are commit-bound and invalidated by any integration change;
- dirty-overlap, conflict, test failure, review failure, push race/rejection,
  crash at every intent/receipt boundary, health failure, canary failure, and
  rollback failure are covered by deterministic tests;
- local main and protected dirty files are preserved exactly;
- the authorized remote ref is independently observed at the integration SHA;
- the gateway runs the selected immutable artifact, reports the expected full
  commit/artifact identity, and passes health plus the deterministic canary;
- the previous artifact can be restored and verified;
- only `live_verified` can transition to `completed_verified`;
- focused BestPlan tests and the project-native full suite pass;
- final local and authorized remote SHAs match, the reviewed commit is pushed,
  and the enrolled live target is verified before reporting done.
