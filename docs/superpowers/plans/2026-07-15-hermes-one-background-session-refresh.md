# Hermes One Background Session Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh Hermes One's local session list after external `state.db` commits by the next five-second probe while the primary window is open, including when it is minimized, unfocused, occluded, or showing another route.

**Architecture:** The Python desktop backend retains stable, read-only SQLite connections and exposes one cheap aggregate revision endpoint based on `PRAGMA data_version` plus database identity. One main-renderer hook polls that endpoint every five seconds using Hermes One's existing no-background-throttling contract, then invokes the existing canonical `refreshSessions()` action only when required. The poller acknowledges a revision only after refresh and a confirming probe both succeed.

**Tech Stack:** Python 3.11-3.13, FastAPI, stdlib `sqlite3`, React 19, TypeScript, Vitest, Testing Library, Electron 40, nanostores.

---

## Execution prerequisites

- Preserve the dirty checkout at `/Users/seb/.hermes/hermes-agent`; none of its unrelated user changes belong to this feature.
- This ignored plan must be force-added and committed on `main` before creating
  the worktree. The worktree starts from that exact plan commit, so the final
  feature diff and build stamp include both the approved spec and this plan.
- Use `superpowers:using-git-worktrees` to create an isolated worktree from the commit containing this plan, on a `codex/` branch such as `codex/hermes-one-background-refresh`.
- Read `README.md`, `CONTRIBUTING.md`, `docs/CONTRACTS.md`, `CHANGELOG.md`, `ARCHITECTURE.md`, and `TESTING.md` in the worktree before editing.
- Use GitNexus `impact(..., direction: "upstream")` for `_lifespan`, `get_profiles_sessions`, `listAllProfileSessions`, and `DesktopController` before touching those symbols. Report HIGH or CRITICAL findings before proceeding.
- Before every commit below, run GitNexus `detect_changes({ scope: "compare", base_ref: "main" })` and confirm only the expected symbols and flows changed.
- Use `scripts/run_tests.sh` for every Python test command. Do not invoke bare `pytest`.

## Task 1: Build the stable read-only revision tracker

**Files:**

- Create: `hermes_cli/session_revision.py`
- Create: `tests/hermes_cli/test_session_revision.py`

- [ ] **Step 1: Write failing real-SQLite tests**

Create tests with temporary directories and independent `sqlite3` writer connections:

```python
def test_revision_stays_stable_until_external_commit(tmp_path): ...
def test_never_created_database_is_a_stable_absent_marker(tmp_path): ...
def test_previously_observed_missing_database_fails_probe(tmp_path): ...
def test_atomic_replacement_reopens_and_changes_revision(tmp_path): ...
def test_scope_pruning_and_close_are_idempotent(tmp_path): ...
def test_probe_and_close_are_serialized(tmp_path): ...
```

The first test must prove the actual SQLite invariant, not a mock:

```python
tracker = SessionRevisionTracker()
first = tracker.revision([("default", db_path)])
assert tracker.revision([("default", db_path)]) == first

writer.execute("INSERT INTO sessions(id) VALUES (?)", ("external",))
writer.commit()

assert tracker.revision([("default", db_path)]) != first
```

The missing-file test must also assert `not db_path.exists()` after probing. The replacement test must use `os.replace()` so it exercises a new file identity. The serialization test must coordinate a probe and `close()` from different threads without relying on timing sleeps.

- [ ] **Step 2: Run the test and confirm the expected failure**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_session_revision.py -q
```

Expected: FAIL because `hermes_cli.session_revision` does not exist.

- [ ] **Step 3: Implement `SessionRevisionTracker` minimally**

Implement this public surface:

```python
class SessionRevisionProbeError(RuntimeError):
    pass


class SessionRevisionTracker:
    def revision(self, targets: Iterable[tuple[str, Path]]) -> str:
        """Return an opaque token for the sorted local profile/database set."""

    def close(self) -> None:
        """Close every retained connection; safe to call repeatedly."""
```

Implementation requirements:

- Normalize and sort targets by profile name and path before hashing their descriptors.
- Keep one entry per requested `(profile, db_path)` and prune entries outside the current target set.
- Open with `mode=ro`, `check_same_thread=False`, `timeout=1.0`, and `isolation_level=None`; never create a file or run schema setup.
- Serialize probe, prune, replacement, and shutdown with one `threading.RLock`.
- Compare a stable connection's `PRAGMA data_version`; never reopen on an unchanged probe.
- Include profile membership, normalized path, present/absent state, file identity, connection epoch, and `data_version` in a SHA-256 token. The token is opaque to clients.
- Treat a never-seen missing database as a stable absent descriptor.
- If a previously present database disappears or cannot be read, close any stale connection and raise `SessionRevisionProbeError` without producing a new token.
- If identity changes, close the old connection, open the replacement, increment that entry's epoch, and return a changed token.
- Avoid logging full private filesystem paths in the normal HTTP-facing error.

- [ ] **Step 4: Run the tracker tests**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_session_revision.py -q
```

Expected: PASS.

- [ ] **Step 5: Inspect the blast radius and commit**

Run GitNexus `detect_changes({ scope: "compare", base_ref: "main" })`, then:

```bash
git add hermes_cli/session_revision.py tests/hermes_cli/test_session_revision.py
git diff --cached --check
git commit -m "feat(desktop): track session database revisions"
```

## Task 2: Expose the aggregate revision through the desktop backend

**Files:**

- Modify: `hermes_cli/web_server.py`
- Modify: `tests/hermes_cli/test_web_server.py`

- [ ] **Step 1: Write failing endpoint and lifecycle tests**

Add focused tests covering:

```python
def test_profiles_sessions_revision_changes_after_external_commit(self): ...
def test_profiles_sessions_revision_scopes_to_requested_profile(self): ...
def test_profiles_sessions_revision_returns_503_for_previously_observed_missing_db(self): ...
def test_session_revision_tracker_closes_at_lifespan_shutdown(...): ...
```

The route contract is:

```json
{
  "revision": "opaque-sha256-token",
  "profiles": ["default", "worker"]
}
```

and the path is `GET /api/profiles/sessions/revision?profile=all` or a URL-encoded local profile name.

- [ ] **Step 2: Run the endpoint tests and confirm the expected failure**

Run:

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_web_server.py::TestWebServerEndpoints::test_profiles_sessions_revision_changes_after_external_commit \
  tests/hermes_cli/test_web_server.py::TestWebServerEndpoints::test_profiles_sessions_revision_scopes_to_requested_profile \
  tests/hermes_cli/test_web_server.py::TestWebServerEndpoints::test_profiles_sessions_revision_returns_503_for_previously_observed_missing_db \
  -q
```

Expected: FAIL with 404 until the route exists.

- [ ] **Step 3: Share profile-target resolution with the existing list route**

Extract the existing target-selection block from `get_profiles_sessions()` into:

```python
def _profile_session_targets(profile: str = "all") -> List[Tuple[str, Path]]:
    """Resolve local profile names and homes in deterministic order."""
```

Keep current fallback semantics for the default profile. Sort by normalized profile name so the revision token does not depend on filesystem/listing order. Use this helper from both `/api/profiles/sessions` and the new revision route; do not duplicate profile discovery.

- [ ] **Step 4: Own the tracker through FastAPI lifespan**

Create the tracker during `_lifespan`, store it on `app.state`, and close it in `finally` after other shutdown signals are issued. Add a lazy `_get_session_revision_tracker(app)` path for existing tests that construct `TestClient(app)` without entering a context manager. The lazy helper and lifespan path must replace/close stale test instances safely.

- [ ] **Step 5: Implement the lightweight route**

Add the route immediately before `/api/profiles/sessions`:

```python
@app.get("/api/profiles/sessions/revision")
def get_profiles_sessions_revision(profile: str = "all"):
    targets = _profile_session_targets(profile)
    db_targets = [(name, Path(home) / "state.db") for name, home in targets]
    try:
        token = _get_session_revision_tracker(app).revision(db_targets)
    except SessionRevisionProbeError:
        raise HTTPException(status_code=503, detail="Session revision probe unavailable")
    return {"revision": token, "profiles": [name for name, _ in targets]}
```

Do not list sessions, read transcripts, mutate `state.db`, or add an Electron dependency.

- [ ] **Step 6: Run backend regression tests**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_session_revision.py tests/hermes_cli/test_web_server.py -q
```

Expected: PASS.

- [ ] **Step 7: Inspect the blast radius and commit**

Run GitNexus `detect_changes({ scope: "compare", base_ref: "main" })`, then:

```bash
git add hermes_cli/web_server.py tests/hermes_cli/test_web_server.py
git diff --cached --check
git commit -m "feat(desktop): expose session revision endpoint"
```

## Task 3: Add the typed renderer revision client

**Files:**

- Modify: `apps/desktop/src/types/hermes.ts`
- Modify: `apps/desktop/src/hermes.ts`
- Modify: `apps/desktop/src/hermes.test.ts`

- [ ] **Step 1: Write the failing REST-helper test**

Import `getAllProfileSessionsRevision` and assert the exact API call:

```typescript
await getAllProfileSessionsRevision('worker one')

expect(api).toHaveBeenCalledWith({
  path: '/api/profiles/sessions/revision?profile=worker%20one',
  timeoutMs: 5_000
})
```

- [ ] **Step 2: Run the test and confirm the expected failure**

Run:

```bash
npx vitest run apps/desktop/src/hermes.test.ts --environment jsdom
```

Expected: FAIL because the helper is not exported.

- [ ] **Step 3: Implement the response type and helper**

Add:

```typescript
export interface SessionRevisionResponse {
  profiles: string[]
  revision: string
}
```

and:

```typescript
const SESSION_REVISION_REQUEST_TIMEOUT_MS = 5_000

export function getAllProfileSessionsRevision(
  profile: 'all' | (string & {}) = 'all'
): Promise<SessionRevisionResponse> {
  return window.hermesDesktop.api<SessionRevisionResponse>({
    path: `/api/profiles/sessions/revision?profile=${encodeURIComponent(profile)}`,
    timeoutMs: SESSION_REVISION_REQUEST_TIMEOUT_MS
  })
}
```

Keep the full list's existing 60-second timeout unchanged.

- [ ] **Step 4: Run the helper test and typecheck**

Run:

```bash
npx vitest run apps/desktop/src/hermes.test.ts --environment jsdom
npm --prefix apps/desktop run typecheck
```

Expected: PASS.

- [ ] **Step 5: Inspect the blast radius and commit**

Run GitNexus `detect_changes({ scope: "compare", base_ref: "main" })`, then:

```bash
git add apps/desktop/src/types/hermes.ts apps/desktop/src/hermes.ts apps/desktop/src/hermes.test.ts
git diff --cached --check
git commit -m "feat(desktop): request aggregate session revisions"
```

## Task 4: Implement the five-second single-flight poller

**Files:**

- Create: `apps/desktop/src/app/session/hooks/use-session-revision-poll.ts`
- Create: `apps/desktop/src/app/session/hooks/use-session-revision-poll.test.tsx`

- [ ] **Step 1: Write failing fake-clock hook tests**

Mock `getAllProfileSessionsRevision`, use `vi.useFakeTimers()`, and provide a controllable `refreshSessions` promise. Cover:

```typescript
it('refreshes once on the first probe and not again for an unchanged acknowledged revision', ...)
it('refreshes a changed revision by the next five-second tick', ...)
it('retries the same dirty revision after refresh failure', ...)
it('keeps the revision dirty when the confirmation probe fails', ...)
it('coalesces ticks and follows a commit that lands during refresh', ...)
it('does not overlap old and new profile generations', ...)
it('does not acknowledge an old profile generation', ...)
it('does not start when disabled', ...)
it('probes immediately on power resume without adding an interval', ...)
```

Assert that the poller never has two of its own `refreshSessions()` calls in flight.

- [ ] **Step 2: Run the hook tests and confirm the expected failure**

Run:

```bash
npx vitest run \
  apps/desktop/src/app/session/hooks/use-session-revision-poll.test.tsx \
  --environment jsdom
```

Expected: FAIL because the hook does not exist.

- [ ] **Step 3: Implement the hook state machine**

Export:

```typescript
interface UseSessionRevisionPollArgs {
  enabled: boolean
  profileScope: string
  refreshSessions: () => Promise<void>
}

export function useSessionRevisionPoll(args: UseSessionRevisionPollArgs): void
```

Keep the coordinator state in hook-level refs so it survives effect cleanup and
replacement. Within one effect per enabled scope:

- Run an immediate probe, then one `window.setInterval(probe, 5_000)`.
- Subscribe to the existing `window.hermesDesktop.onPowerResume` callback and invoke the same probe function.
- Keep hook-level `activeGeneration`, `acknowledgedRevision`, `dirty`
  (initially `true` per generation), `inFlightPromise`, `pendingGeneration`,
  latest-probe callback, and failure-log refs. Effect-local state is limited to
  the current generation id, timer/listener handles, and cancellation flag.
- If a probe arrives while work is running, set `pendingGeneration` to the
  latest active generation; never start concurrent poller refreshes.
- If clean and the candidate equals the acknowledged revision, stop after the cheap probe.
- Otherwise await `refreshSessions()`, then make a mandatory confirmation revision request.
- Acknowledge only when refresh succeeds, confirmation succeeds, the confirmed token equals the candidate, and the effect generation is still current.
- On a changed confirmation token, keep dirty and schedule one coalesced follow-up cycle.
- On any probe, refresh, or confirmation error, keep dirty and retry on the next tick. Log only the transition into a failing state; do not warn every five seconds.
- On profile/gateway generation change, the new effect must wait for the shared
  `inFlightPromise` to settle before starting its refresh. Queue only the latest
  active generation; do not overlap the old and new callbacks.
- On cleanup, mark that generation cancelled, clear its interval, and remove
  its power-resume listener. Late promises may settle but may not acknowledge
  the new scope. On final hook unmount, dispose the shared coordinator after
  the outstanding promise settles.

Do not check `document.visibilityState`; background operation relies on the existing tested Electron no-throttling contract.

- [ ] **Step 4: Run all poller tests**

Run:

```bash
npx vitest run \
  apps/desktop/src/app/session/hooks/use-session-revision-poll.test.tsx \
  --environment jsdom
```

Expected: PASS.

- [ ] **Step 5: Inspect the blast radius and commit**

Run GitNexus `detect_changes({ scope: "compare", base_ref: "main" })`, then:

```bash
git add \
  apps/desktop/src/app/session/hooks/use-session-revision-poll.ts \
  apps/desktop/src/app/session/hooks/use-session-revision-poll.test.tsx
git diff --cached --check
git commit -m "feat(desktop): poll session revisions in background"
```

## Task 5: Mount exactly one poller in the desktop controller

**Files:**

- Modify: `apps/desktop/src/app/desktop-controller.tsx`
- Verify: `apps/desktop/electron/session-windows.cjs`
- Verify: `apps/desktop/electron/session-windows.test.cjs`

- [ ] **Step 1: Mount the hook beside the canonical list actions**

Immediately after `useSessionListActions({ profileScope })`, call:

```typescript
useSessionRevisionPoll({
  enabled: gatewayState === 'open' && !isSecondaryWindow(),
  profileScope,
  refreshSessions
})
```

Import the hook from the session hooks directory. Do not move or remove existing immediate refreshes for local mutations, gateway boot/reconnect, or profile changes.

- [ ] **Step 2: Verify secondary-window and background contracts**

Run:

```bash
npx vitest run \
  apps/desktop/src/hermes.test.ts \
  apps/desktop/src/app/session/hooks/use-session-revision-poll.test.tsx \
  --environment jsdom
node --test apps/desktop/electron/session-windows.test.cjs
npm --prefix apps/desktop run typecheck
```

Expected: PASS, including the existing assertion that Hermes BrowserWindows use `backgroundThrottling: false`.

- [ ] **Step 3: Inspect the blast radius and commit**

Run GitNexus `detect_changes({ scope: "compare", base_ref: "main" })`, then:

```bash
git add apps/desktop/src/app/desktop-controller.tsx
git diff --cached --check
git commit -m "feat(desktop): keep session refresh active offscreen"
```

## Task 6: Run focused and build-level verification

**Files:**

- Verify only; fix failures in the owning task's files and add regression coverage before continuing.

- [ ] **Step 1: Run the backend suite for the touched surface**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_session_revision.py tests/hermes_cli/test_web_server.py -q
```

- [ ] **Step 2: Run desktop UI, platform, type, and build checks**

Run:

```bash
npx vitest run \
  apps/desktop/src/hermes.test.ts \
  apps/desktop/src/app/session/hooks/use-session-revision-poll.test.tsx \
  --environment jsdom
node --test apps/desktop/electron/session-windows.test.cjs
npm --prefix apps/desktop run typecheck
npm --prefix apps/desktop run build
```

- [ ] **Step 3: Audit the final feature diff**

Run:

```bash
git diff --check main...HEAD
git status --short
git log --oneline main..HEAD
```

Run GitNexus `detect_changes({ scope: "compare", base_ref: "main" })` one final time. Confirm the diff is limited to the tracker, route, client helper, poll hook, controller integration, tests, spec, and plan.

## Task 7: Package, deploy both halves safely, and prove the live behavior

**Files:**

- Build artifact: `apps/desktop/release/mac-arm64/Hermes.app`
- Installed target: `/Applications/Hermes One.app`
- Runtime checkout: `/Users/seb/.hermes/hermes-agent`

- [ ] **Step 1: Build and validate the packaged app**

From the clean feature worktree, run:

```bash
npm --prefix apps/desktop run pack
```

Confirm the bundle exists and its install stamp names the clean feature-branch
HEAD. Do not launch it against the old runtime yet: the desktop package is a
thin shell and the revision endpoint lives in the Python source checkout.

- [ ] **Step 2: Deploy the Python runtime with a guarded fast-forward**

Record the original runtime commit as `BACKEND_ROLLBACK_HEAD`. In the original
dirty checkout, verify the feature-touched paths have no pre-existing local
changes:

```bash
git -C /Users/seb/.hermes/hermes-agent diff --name-only -- \
  hermes_cli/session_revision.py \
  hermes_cli/web_server.py \
  apps/desktop/src/types/hermes.ts \
  apps/desktop/src/hermes.ts \
  apps/desktop/src/hermes.test.ts \
  apps/desktop/src/app/desktop-controller.tsx \
  apps/desktop/src/app/session/hooks/use-session-revision-poll.ts \
  apps/desktop/src/app/session/hooks/use-session-revision-poll.test.tsx
```

Expected: no output. If any touched path is dirty, stop; do not stash, reset,
or overwrite it.

Fast-forward `main` to the completed feature branch:

```bash
git -C /Users/seb/.hermes/hermes-agent merge --ff-only codex/hermes-one-background-refresh
```

This preserves every unrelated dirty file while making the new Python endpoint
available to the installed thin shell. Confirm `hermes_cli/session_revision.py`
exists and query the endpoint through a controlled local backend before
replacing the app. If the live deployment later fails, restore the old bundle
and revert only the feature range without resetting user work:

```bash
git -C /Users/seb/.hermes/hermes-agent revert --no-commit "${BACKEND_ROLLBACK_HEAD}..HEAD"
git -C /Users/seb/.hermes/hermes-agent commit -m "revert: disable background session refresh"
```

Use that rollback only after rechecking that feature-touched paths have not
acquired new user edits. Never use `git reset --hard` or discard unrelated
changes.

- [ ] **Step 3: Validate the packaged shell against the deployed runtime**

From the feature worktree, run:

```bash
HERMES_DESKTOP_SKIP_BUILD=1 npm --prefix apps/desktop run test:desktop:existing
```

Confirm the validator reports the packaged binary, renderer payload, native
dependencies, runtime root `/Users/seb/.hermes/hermes-agent`, and feature-branch
install-stamp commit. Confirm the launched shell can call the revision endpoint
without 404 or 503 errors.

- [ ] **Step 4: Preserve a rollback bundle and install the rebuilt app**

Quit Hermes One cleanly. Move `/Applications/Hermes One.app` to a timestamped sibling backup, then copy `apps/desktop/release/mac-arm64/Hermes.app` to `/Applications/Hermes One.app` with `ditto`. Do not delete the backup during verification.

Verify before launch:

```bash
/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' '/Applications/Hermes One.app/Contents/Info.plist'
/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' '/Applications/Hermes One.app/Contents/Info.plist'
python3 -m json.tool '/Applications/Hermes One.app/Contents/Resources/install-stamp.json'
```

Expected bundle identifier: `com.nousresearch.hermes`. Expected install stamp: the feature branch HEAD with `dirty: false`.

- [ ] **Step 5: Verify startup and backend health**

Launch `/Applications/Hermes One.app`. Confirm the expected Hermes process tree is running, the desktop backend port is listening, and its health/readiness endpoint succeeds. Inspect startup logs for revision-route or SQLite errors without printing tokens or credentials.

- [ ] **Step 6: Prove the five-second cross-surface refresh**

Use one uniquely named disposable session so cleanup cannot affect an existing conversation:

1. Leave Hermes One's primary window showing Sessions, then unfocus or minimize it.
2. Create the disposable session through the shared Hermes WebUI/CLI state path and record the commit time.
3. Without focusing Hermes One, confirm logs show the next revision probe and canonical session refresh starts within five seconds under normal scheduling.
4. Restore Hermes One and confirm the session row is already present without a focus-triggered refresh.
5. Repeat with Hermes One on a non-session route; open Sessions afterward and confirm the row is already in the store.
6. Delete only the uniquely named disposable session through the canonical API and confirm that deletion also propagates.

Do not manipulate arbitrary existing session rows directly.

- [ ] **Step 7: Prove idle efficiency and resume behavior**

Observe at least three unchanged five-second probes and confirm they do not call `/api/profiles/sessions` or run a full projection. Trigger the existing power-resume callback through a real sleep/wake cycle when practical, or through the tested Electron bridge in a controlled packaged run, and confirm it makes one immediate probe without adding an interval.

- [ ] **Step 8: Keep or roll back based on evidence**

Keep the new bundle and fast-forwarded runtime only if startup, backend health,
minimized-window propagation, deletion propagation, and idle-efficiency checks
all pass. Otherwise quit it, restore the timestamped app backup atomically,
relaunch, and use the guarded feature-range revert above if the backend source
also needs rollback. Report the failing evidence. Do not remove the rollback
bundle until the user confirms the new build is satisfactory.

## Final handoff

Report:

- focused Python, Vitest, Electron, typecheck, and build results;
- GitNexus final change scope;
- packaged install-stamp commit and bundle identifier;
- measured external-commit-to-probe and external-commit-to-visible-list latency;
- minimized/off-route and resume verification results;
- idle probes versus full refresh count; and
- the exact rollback-bundle path and `BACKEND_ROLLBACK_HEAD` retained.
