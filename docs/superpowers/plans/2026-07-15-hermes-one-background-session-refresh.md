# Hermes One Background Session Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh Hermes One's local session list after external `state.db` commits by the next five-second probe while the primary window is open, including when it is minimized, unfocused, occluded, or showing another route.

**Architecture:** The Python desktop backend retains stable, read-only SQLite connections and exposes one cheap aggregate revision endpoint based on `PRAGMA data_version` plus database identity. One main-renderer hook polls that endpoint every five seconds using Hermes One's existing no-background-throttling contract, then invokes a strict wrapper around the canonical session projections only when required. The poller acknowledges a revision only after the core, cron, and messaging projections apply for the same request generation and a confirming probe succeeds.

**Tech Stack:** Python 3.11-3.13, FastAPI, stdlib `sqlite3`, React 19, TypeScript, Vitest, Testing Library, Electron 40, nanostores.

---

## Execution prerequisites

- Preserve the runtime checkout at `/Users/seb/.hermes/hermes-agent`; it is outside this sandbox's writable roots and is not the delivery target for this task.
- Work directly on `main` in a standalone clone of Seb's fork. Configure only `origin = git@github.com:sebmarion/hermes-agent.git`; do not add or push an upstream remote, create a feature branch, or use a Git worktree.
- Read the repository and desktop contribution guidance before editing. Some documents named by the parent workspace guidance are not present in this fork; treat the files that exist here as authoritative.
- Use GitNexus `impact(..., direction: "upstream")` for `_lifespan`, `get_profiles_sessions`, `listAllProfileSessions`, and `DesktopController` before touching those symbols. Report HIGH or CRITICAL findings before proceeding.
- Before every commit below, run GitNexus `detect_changes({ scope: "compare", base_ref: "main" })`. If the local index is incompatible, record the error and verify scope from the direct diff and call sites.
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

From `apps/desktop`, run:

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
await getAllProfileSessionsRevision("worker one");

expect(api).toHaveBeenCalledWith({
  path: "/api/profiles/sessions/revision?profile=worker%20one",
  timeoutMs: 5_000,
});
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
  profiles: string[];
  revision: string;
}
```

and:

```typescript
const SESSION_REVISION_REQUEST_TIMEOUT_MS = 5_000;

export function getAllProfileSessionsRevision(
  profile: "all" | (string & {}) = "all",
): Promise<SessionRevisionResponse> {
  return window.hermesDesktop.api<SessionRevisionResponse>({
    path: `/api/profiles/sessions/revision?profile=${encodeURIComponent(profile)}`,
    timeoutMs: SESSION_REVISION_REQUEST_TIMEOUT_MS,
  });
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
it('retries when the requested refresh was superseded before it applied', ...)
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
  src/app/session/hooks/use-session-revision-poll.test.tsx \
  --environment jsdom
```

Expected: FAIL because the hook does not exist.

- [ ] **Step 3: Implement the hook state machine**

Export:

```typescript
type SessionRefreshResult = "applied" | "superseded";

interface UseSessionRevisionPollArgs {
  enabled: boolean;
  profileScope: string;
  refreshSessions: () => Promise<SessionRefreshResult>;
}

export function useSessionRevisionPoll(args: UseSessionRevisionPollArgs): void;
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
- Otherwise await the strict refresh callback. A `superseded` result stays dirty and skips confirmation; an `applied` result proceeds to the mandatory confirmation revision request.
- Acknowledge only when refresh returns `applied`, confirmation succeeds, the confirmed token equals the candidate, and the effect generation is still current.
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

From `apps/desktop`, run:

```bash
npx vitest run \
  src/app/session/hooks/use-session-revision-poll.test.tsx \
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
- Modify: `apps/desktop/src/app/session/hooks/use-session-list-actions.ts`
- Create: `apps/desktop/src/app/session/hooks/use-session-list-actions.test.tsx`
- Verify: `apps/desktop/electron/session-windows.ts`
- Verify: `apps/desktop/electron/session-windows.test.ts`

- [ ] **Step 1: Mount the hook beside the canonical list actions**

Immediately after `useSessionListActions({ profileScope })`, call:

```typescript
useSessionRevisionPoll({
  enabled: gatewayState === "open" && !isSecondaryWindow(),
  profileScope,
  refreshSessions: refreshSessionsForRevision,
});
```

Import the hook from the session hooks directory. The strict revision callback must report `applied` or `superseded`, await every revision-covered projection, and share generation guards with standalone refreshes and pagers. Preserve the existing `refreshSessions(): Promise<void>` semantics for local mutations, gateway boot/reconnect, and profile changes.

- [ ] **Step 2: Verify secondary-window and background contracts**

From `apps/desktop`, run:

```bash
npx vitest run \
  src/hermes.test.ts \
  src/app/session/hooks/use-session-list-actions.test.tsx \
  src/app/session/hooks/use-session-revision-poll.test.tsx \
  --environment jsdom
npx tsx --test electron/session-windows.test.ts
npm run typecheck
```

Expected: PASS, including the existing assertion that Hermes BrowserWindows use `backgroundThrottling: false`.

- [ ] **Step 3: Inspect the blast radius and commit**

Run GitNexus `detect_changes({ scope: "compare", base_ref: "main" })`, then:

```bash
git add \
  apps/desktop/src/app/desktop-controller.tsx \
  apps/desktop/src/app/session/hooks/use-session-list-actions.ts \
  apps/desktop/src/app/session/hooks/use-session-list-actions.test.tsx
git diff --cached --check
git commit -m "feat(desktop): keep session refresh active offscreen"
```

## Task 6: Run focused and build-level verification

**Files:**

- Verify only; fix failures in the owning task's files and add regression coverage before continuing.

- [ ] **Step 1: Run the backend suite for the touched surface**

From `apps/desktop`, run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_session_revision.py tests/hermes_cli/test_web_server.py -q
```

- [ ] **Step 2: Run desktop UI, platform, type, and build checks**

Run:

```bash
npx vitest run \
  src/hermes.test.ts \
  src/app/session/hooks/use-session-list-actions.test.tsx \
  src/app/session/hooks/use-session-revision-poll.test.tsx \
  --environment jsdom
npx tsx --test electron/session-windows.test.ts
npm run typecheck
npm run build
```

- [ ] **Step 3: Audit the final feature diff**

Run:

```bash
git diff --check origin/main...HEAD
git status --short
git log --oneline origin/main..HEAD
```

Run GitNexus `detect_changes({ scope: "compare", base_ref: "main" })` one final time. Confirm the diff is limited to the tracker, route, client helper, poll hook, controller integration, tests, spec, and plan.

## Task 7: Package what is buildable and push Seb's fork main

**Files:**

- Optional build artifact: `apps/desktop/release/mac-arm64/Hermes.app`
- Git destination: `git@github.com:sebmarion/hermes-agent.git`, branch `main`

- [ ] **Step 1: Validate repository ownership and remote state**

Confirm `origin` is Seb's fork and no upstream push target exists:

```bash
git remote -v
git fetch origin main
git rev-list --left-right --count origin/main...main
```

Do not force-push. If `origin/main` advanced, integrate it normally and repeat
the verification suite before pushing.

- [ ] **Step 2: Build and optionally package from a clean `main`**

Run the desktop build after every source and documentation change is committed:

```bash
npm --prefix apps/desktop run build
npm --prefix apps/desktop run pack
```

Packaging is best-effort within the sandbox. If it succeeds, verify the bundle
and install stamp name the clean `main` HEAD. Do not copy into `/Applications`
or modify `/Users/seb/.hermes/hermes-agent`; neither location is an authorized
writable delivery target for this task.

- [ ] **Step 3: Push directly to Seb's fork**

```bash
git push origin main
```

Confirm the pushed commit equals local `HEAD`. Do not create a PR or push any
remote other than Seb's `origin`.

## Final handoff

Report:

- focused Python, Vitest, Electron, typecheck, and build results;
- GitNexus final change scope;
- pushed fork branch and commit;
- packaged artifact/install-stamp details if packaging succeeded; and
- the explicit boundary that installed-app/runtime deployment and live minimized-window measurement remain pending when those targets are not writable.
