# Local History Reconciliation Design

## Decision

Reconcile every local Hermes Agent branch and linked worktree into the current
Agent `main` history through evidence-based replay. Do not merge stale branch
snapshots directly. Every commit receives a disposition, every dirty worktree
is rescued before inspection or mutation, and every original branch tip is
made reachable from the final `main` only after its surviving behavior has
been accounted for.

The product tree receives intentional source, test, documentation, and
configuration changes that remain compatible with the current contracts.
Secrets, runtime state, caches, generated artifacts, and superseded code are
preserved in recoverable evidence but are not injected into the product tree.

## Why

The current Agent checkout has 33 local branches whose tips are not ancestors
of `main`, 27 valid linked worktrees, 11 dirty worktrees, and 3 stale
worktree records. The branches are highly divergent and include patch-
equivalent work, merge-only snapshots, context/recovery experiments, and
uncommitted trees. A raw merge can reintroduce obsolete files; a blind
cherry-pick can duplicate or resurrect superseded behavior.

## Integration boundaries

- Hermes Agent is the only repository being consolidated. Hermes WebUI has no
  unmerged local branch or extra worktree and is verified afterward because it
  imports Agent internals.
- The live Agent checkout and live WebUI checkout remain untouched until the
  isolated integration tree passes its gates.
- No reset, restore, clean, rebase, force-push, or destructive worktree
  removal is allowed.
- Existing authorship is preserved by cherry-picking valid commits where
  possible. Conflict repairs are separate, focused commits that name their
  source SHAs.

## State and evidence model

Each branch/worktree receives a ledger row containing its path, branch or
detached HEAD, original SHA, dirty-file manifest, patch-equivalence result,
commit-family classification, disposition, rescue commit (if any), tests, and
final reachability. Before any dirty tree is staged, tracked and untracked
content is captured as a binary diff/hash evidence bundle. Secret-like files,
databases, caches, and build output are never committed.

Commit dispositions are:

1. `already_present` — patch-equivalent behavior is already in current main;
2. `replay` — valid missing behavior is cherry-picked or manually reconciled;
3. `partial` — only the contract-compatible portion is replayed;
4. `superseded` — preserved and ancestry-closed after evidence shows current
   main intentionally replaces it;
5. `artifact_only` — preserved outside Git with hashes, never product code;
6. `blocked` — insufficient evidence or unresolved conflict, which stops the
   integration rather than being guessed through.

After all non-artifact dispositions are verified, ancestry-only merge records
(`-s ours`) may close patch-equivalent or superseded branch tips without
replacing the current tree. This makes graph state honest while keeping the
product tree controlled.

## Verification gates

The integration cannot enter live Agent `main` unless all of these are true:

- every original branch/worktree has a ledger disposition;
- every intentional rescue commit is reachable from the integration head;
- no secrets or runtime state entered Git;
- targeted tests for every replayed family pass;
- Agent `scripts/run_tests.sh` passes, with known pre-existing failures
  separately identified if the repository baseline already has them;
- WebUI `./scripts/test.sh` passes against the integrated Agent source;
- managed WebUI restart, `/health`, deep health, source-path, admission, and
  context-compression smoke checks pass;
- the final local and `sebmarion/main` hashes match exactly;
- both live main worktrees are clean.

If a gate fails, preserve the integration tree and evidence, fix forward in
the integration worktree, and do not mutate live `main`.
