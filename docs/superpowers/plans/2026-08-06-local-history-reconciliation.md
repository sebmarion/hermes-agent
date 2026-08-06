# Local History Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Account for every local Agent branch and worktree, integrate all valid intentional changes into current `main`, and prove the final graph, runtime, and remote state.

**Architecture:** Work in an isolated Agent integration worktree. First rescue and classify every source, commit, and dirty path; then replay valid behavior onto current `main`, record explicit ancestry for already-present or superseded histories, and promote only after Agent and WebUI verification.

**Tech Stack:** Git worktrees and patch IDs, Hermes Agent Python test runner (`scripts/run_tests.sh`), Hermes WebUI test runner (`./scripts/test.sh`), LaunchAgent health endpoints, SHA-256 manifests.

---

### Task 1: Capture immutable baseline and recovery envelope

**Files:**
- Create: timestamped local checkpoint refs and evidence manifests outside the product tree

- [ ] **Step 1: Fetch current main refs without pruning or rebasing**

Run:

```bash
git fetch sebmarion main
git fetch origin main
```

Expected: fetch succeeds; no working-tree files change.

- [ ] **Step 2: Record live main, remote, branch, and worktree state**

Record `git status --short --branch`, exact `HEAD`/remote SHAs, `git branch -vv`,
`git worktree list --porcelain`, and merge-conflict paths for both repositories.

- [ ] **Step 3: Create recoverable checkpoint refs**

Create uniquely named local refs for current Agent `main`, every branch tip, and
each detached worktree HEAD. Do not delete or rewrite existing refs.

- [ ] **Step 4: Verify the checkpoint envelope**

Re-read every checkpoint SHA and confirm live Agent and WebUI worktrees remain
clean and unchanged.

### Task 2: Rescue and inventory dirty or detached worktrees

**Files:**
- Create: evidence manifests and binary diffs outside the product tree
- Create: named rescue branches/commits for intentional dirty repository content

- [ ] **Step 1: Capture tracked, staged, untracked, and ignored-file evidence**

For each dirty worktree, record status, file type, size, mtime, SHA-256, and
binary diffs. Do not print secrets or full credential files.

- [ ] **Step 2: Classify files before staging**

Classify each path as intentional source/test/docs/config, generated/build
output, runtime state/cache/database, secret-like, or unknown. Unknown stops
that worktree until resolved from evidence.

- [ ] **Step 3: Create rescue refs and commits**

Create a branch for detached intentional work. Commit only safe intentional
content with an audit message naming the source worktree and original HEAD.

- [ ] **Step 4: Verify rescue reachability**

Confirm every intentional rescued path is present in its rescue commit and every
excluded path has an external hash/manifest; no original worktree is cleaned.

### Task 3: Classify all branches and commit families

**Files:**
- Modify: integration ledger in the isolated worktree

- [ ] **Step 1: Measure graph and patch equivalence**

For each local branch, run merge-base, `git cherry`, `git range-diff`, changed
paths, and commit-author/subject inspection against current `main`.

- [ ] **Step 2: Group related histories**

Review Apple design, BestPlan, context-budget/reconciliation, platform/release,
guardrail/recovery, radar, CI repair, safety snapshots, and detached experimental
families as units to avoid replaying conflicting snapshots in arbitrary order.

- [ ] **Step 3: Assign a disposition to every commit and worktree**

Use only `already_present`, `replay`, `partial`, `superseded`, `artifact_only`,
or `blocked`. A `blocked` row stops execution.

- [ ] **Step 4: Review the ledger before replay**

Check that every original branch and worktree appears exactly once and that the
classification explains all dirty paths and unique commits.

### Task 4: Establish a passing integration baseline

**Files:**
- Test: existing Agent test suites

- [ ] **Step 1: Run focused context/recovery regression tests**

Use `scripts/run_tests.sh` with the tests covering provider payload admission,
compression exhaustion, session replay, and tool-effect metadata.

- [ ] **Step 2: Run the full Agent baseline**

Run `scripts/run_tests.sh`; record failures before replay so pre-existing failures
cannot be misattributed to integration.

### Task 5: Replay valid behavior family by family

**Files:**
- Modify: only files selected by the ledger through cherry-pick or focused conflict repair
- Test: tests selected by each family

- [ ] **Step 1: Replay the oldest valid foundational family**

Cherry-pick clean commits preserving authorship; stop on conflicts and inspect
the current contract before resolving.

- [ ] **Step 2: Run focused tests and commit conflict repairs**

Every manual repair is a separate commit naming source SHAs, affected invariants,
and tests.

- [ ] **Step 3: Repeat for each remaining family in dependency order**

Do not replay patch-equivalent or superseded snapshots as source changes.

- [ ] **Step 4: Checkpoint after each family**

Record the integration SHA, focused test output, diff stat, and ledger updates.

### Task 6: Close graph ancestry after content is proven

**Files:**
- Modify: Git history only in the integration worktree

- [ ] **Step 1: Verify no ledger row is unresolved**

There must be no `blocked` or unexplained dirty-content row.

- [ ] **Step 2: Add ancestry-only merge records where appropriate**

Use `git merge --no-ff -s ours` for already-present or superseded branch tips,
with commit messages naming the disposition and evidence ledger.

- [ ] **Step 3: Verify graph closure**

`git branch --no-merged main` must be empty for local branches selected for
closure, and every rescued intentional commit must be reachable from the
integration head.

### Task 7: Verify Agent, WebUI, and live runtime

**Files:**
- Test: Agent and WebUI existing test suites

- [ ] **Step 1: Run Agent focused and full suites**

Use `scripts/run_tests.sh` and record exact pass/fail/flaky counts.

- [ ] **Step 2: Run WebUI focused and full suites**

Use `./scripts/test.sh` from `/Users/seb/hermes-webui` against the integrated
Agent source; report any known baseline failures separately.

- [ ] **Step 3: Restart and smoke-test the live WebUI**

Restart the managed LaunchAgent, then prove `/health`, deep health, admission,
startup error, source path, and a real context-exhaustion recovery turn.

- [ ] **Step 4: Re-read all Git invariants**

Confirm clean worktrees, exact expected branch graph, no unmerged index entries,
and no secret/runtime files in commits.

### Task 8: Promote and push

**Files:**
- Modify: live Agent `main` history only after all prior gates pass

- [ ] **Step 1: Fast-forward or merge the verified integration head into live Agent main**

Preserve the pre-integration checkpoint and do not force-update refs.

- [ ] **Step 2: Push to the personal `main` remote**

Run `git push sebmarion main`; do not push upstream `origin` or force-push.

- [ ] **Step 3: Verify remote parity and runtime source**

Fetch `sebmarion/main`, compare exact SHAs, and re-check WebUI health/source path.

- [ ] **Step 4: Produce the final ledger report**

Report every branch/worktree, rescue SHA, disposition, integrated SHA, tests,
remote SHA, and any preserved artifact that was intentionally not product code.
