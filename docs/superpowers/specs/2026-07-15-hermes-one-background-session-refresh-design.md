# Hermes One Background Session Refresh Design

Date: 2026-07-15
Status: Approved for implementation planning

## Problem

The installed Hermes One 0.7.3 app refreshes its session list every 30 seconds,
and only while the Sessions view is visible. Conversations created or continued
through Hermes WebUI, the CLI, the TUI, or another Hermes surface can therefore
remain stale when Hermes One is minimized, unfocused, or showing another
screen.

Hermes One and Hermes WebUI already share conversation state through the
active profile's `state.db`. The missing behavior is a cheap, app-wide way to
notice commits from another process and update Hermes One's cached and visible
session list while the app remains running.

## Goal

While Hermes One is running, externally committed session-list changes should
be detected by the next five-second probe under normal macOS scheduling,
regardless of window focus, minimization, or active screen. The refreshed list
is published after the canonical refresh completes, so end-to-end visibility is
one probe interval plus backend and refresh time rather than a strict
five-second wall-clock SLA. Returning from system sleep should trigger an immediate
catch-up check.

## Non-goals

- Do not run a launch agent or background service after Hermes One quits.
- Do not copy, migrate, or make a second canonical conversation store.
- Do not poll or rebuild full transcripts.
- Do not require a new user-facing setting for the five-second cadence.
- Do not patch the installed `app.asar` directly. The change must land in
  source, be tested, and be delivered through a rebuilt Hermes One app.
- Do not replace existing immediate refreshes after local mutations, turn
  completion, profile changes, or gateway reconnects.
- Do not add remote-profile change detection in this change. The detector
  covers local profile databases shared with Hermes WebUI, CLI, and TUI;
  remote desktop profiles keep their existing refresh behavior.
- Do not keep polling after the primary window is closed on macOS. Reopening
  the window recreates the renderer, reconnects the gateway, and performs an
  immediate check before the five-second interval begins.

## Current behavior and constraint

The installed build's refresh path calls `syncSessionCache()`. That operation
is not a cheap heartbeat: it reads and projects the sessions table, resolves
missing titles, attaches lineage/context metadata, and rewrites the JSON cache.
Calling the full path unconditionally every five seconds would add needless
SQLite and filesystem work when the database is idle.

The current Hermes Agent checkout has evolved toward event-driven local-session
refresh and separate bounded polling for cron and messaging sessions. Its
Electron shell no longer ships a SQLite driver, while the Python desktop
backend already owns all `state.db` access. The shell also explicitly disables
renderer backgrounding and timer throttling for Hermes windows. The approved
behavior therefore maps to a stable read-only SQLite revision tracker in the
existing Python backend and one app-wide five-second poller in the main
renderer. The design does not retain the installed build's old component
layout.

## Considered approaches

### 1. Backend SQLite change detector with app-wide poller (selected)

Keep stable, read-only SQLite connections in the existing Python desktop
backend and expose a lightweight aggregate revision endpoint. Run one
five-second poller in the main Hermes renderer, whose Electron window is
explicitly configured not to throttle in the background. A changed revision
triggers the renderer's existing canonical session-list refresh.

This keeps the idle tick cheap, preserves SQLite ownership in the backend,
works while Hermes One is hidden, minimized, unfocused, or on another screen,
and uses SQLite's committed-change signal rather than inferring state from UI
activity.

### 2. Unconditional five-second full refresh

Move the existing full refresh timer to an app-wide owner and shorten it to five
seconds. This is easy to implement but repeatedly scans and projects all
sessions and rewrites the cache even when nothing changed. It is rejected for
avoidable database, CPU, and disk churn.

### 3. Filesystem watch on `state.db` and `state.db-wal`

Watch SQLite files and debounce change events. This can react faster than five
seconds, but WAL creation, growth, checkpointing, replacement, profile changes,
and sleep/wake make filesystem notifications noisy and less reliable. It is
rejected as the primary mechanism.

## Architecture

### Backend session revision tracker

Introduce a narrowly scoped Python component, referred to here as
`SessionRevisionTracker`. It owns:

- one stable, read-only SQLite connection per locally requested profile
  database;
- the last observed `PRAGMA data_version` for each connection;
- each database path and filesystem identity; and
- deterministic per-profile and aggregate revision tokens returned by a
  lightweight HTTP endpoint.

`PRAGMA data_version` values are meaningful only when compared on the same
connection. The tracker must not open a fresh connection for every request.
When the requested profile set changes, it opens missing connections and
closes connections that no longer belong to the requested local scope.

Before reading `data_version`, each request also validates that every database
path still resolves to the same file identity (device/inode, with a portable
fallback where necessary). If a database disappears or the identity changes
because `state.db` was atomically replaced,
the tracker closes the stale connection. When the path becomes available, it
opens a new read-only connection and returns a different revision token. It
must never continue polling an unlinked old database inode.

The tracker is created during the FastAPI lifespan and closes every retained
connection during shutdown. It uses Python's existing `sqlite3` dependency and
the same read-only URI contract as `SessionDB(read_only=True)`. It never creates
an absent database and never writes schema or session state. Connections use
`check_same_thread=False`, and every open, probe, prune, and close operation is
serialized behind one `threading.RLock` so FastAPI worker threads and lifespan
shutdown cannot touch the same connection concurrently.

The revision endpoint returns a deterministic token for the requested local
profile scope. The token changes when any included database's `data_version`,
file identity, presence, or profile membership changes. It does not project or
return session rows.

### App-wide revision poller

Introduce one main-window React hook that polls the lightweight revision
endpoint every five seconds while the desktop gateway is open. It is mounted
at the desktop-controller level rather than inside the Sessions screen, so
route changes do not stop it. Secondary Hermes windows do not start a duplicate
poller and do not own the sidebar session list.

This relies on an existing, tested desktop-shell contract:
`backgroundThrottling: false` on Hermes BrowserWindows plus the Chromium
switches that disable renderer backgrounding and background timer throttling.
No new native SQLite dependency or Electron IPC bridge is introduced.

### Refresh coordinator

On each probe:

1. Request the aggregate revision token for the current local profile scope.
2. The backend validates file identities and reads `PRAGMA data_version` from
   its stable connections.
3. If the token matches the last successfully acknowledged revision and no
   dirty refresh is pending, do nothing else.
4. On the first successful probe or when the token differs, capture the
   candidate revision and current poller generation, mark the poller dirty, and
   request one refresh through the existing canonical session-list function.
5. If another tick, profile-scope change, or lifecycle event arrives while a
   refresh is running, remember one pending check instead of starting a
   concurrent refresh.
6. A candidate revision is acknowledged only after the canonical refresh
   succeeds for the same poller generation. A failed refresh leaves
   the poller dirty so the next tick retries even if the revision token has not
   changed again.
7. Immediately after a successful refresh, request the revision token again.
   If it no longer matches the candidate revision, keep the poller dirty and
   run one follow-up refresh/check so a commit that landed during the refresh
   cannot be lost. If this mandatory confirmation probe fails, do not
   acknowledge the candidate: keep the poller dirty and retry from a fresh
   probe on the next tick.
8. If the poller generation changed because the profile scope or gateway
   lifecycle changed while work was in flight, do not acknowledge the old
   result. The canonical refresh retains its existing store-generation guards;
   the poller must not publish an old revision as current for the new scope.

Refresh work is scoped through the existing `profileScope` argument and
session-store generation guards. The poller captures the scope for each run and
must not re-resolve a mutable current scope halfway through asynchronous work.

The tracker and poller must not introduce a second session-list projection or
duplicate the filtering, paging, lineage, archive, pin, or profile rules
already owned by the canonical refresh path.

### Renderer reconciliation

When the revision changes, the app-wide hook calls the existing
`refreshSessions()` action. That action reads through the canonical profile
session API and applies the existing store merge and generation rules. The
revision endpoint never returns a copied session-list payload. This keeps
filtering, paging, lineage, archive, pin, and merge semantics in their current
owner.

Renderer behavior:

- The main session store reconciles the fresh list immediately, even when its
  window is unfocused or minimized.
- A screen that does not currently render sessions does not need to rebuild
  hidden DOM. The store is already current, and opening the sidebar/sessions
  screen reads that fresh data immediately.
- Existing optimistic rows for in-flight first turns, pinned sessions, active
  sessions, and recently settled sessions retain their current merge rules.
- Secondary windows do not start their own background revision poll.

### Lifecycle

- Start the backend tracker with the FastAPI app lifespan.
- Start the poller after the desktop gateway is open.
- Perform an immediate baseline/check rather than waiting five seconds.
- Keep it active while Hermes One is open, independent of focus, visibility,
  minimization, or route.
- On macOS system resume, the hook uses the existing renderer-accessible
  `window.hermesDesktop.onPowerResume` preload bridge to request an immediate
  check without creating another interval.
- On profile-scope change, invalidate the poller generation, request the new
  aggregate token, and prune backend connections outside that scope.
- Stop the renderer timer on unmount/gateway close and close backend tracker
  connections during app shutdown.
- Do not leave a helper, daemon, or launch agent running after quit.

## Failure handling

- A profile whose `state.db` has never existed contributes a stable absent
  marker. If that database later appears, its new presence/identity changes the
  aggregate token and triggers a refresh.
- If a previously observed database disappears, becomes unreadable, is
  temporarily locked, or is between the unlink and rename of an atomic
  replacement, fail the aggregate probe without returning a new token. Keep
  the last good list and retry; do not clear the sidebar. A profile removed
  from the requested scope is different: membership changes the token and the
  canonical refresh may remove its rows.
- If the change probe succeeds but the full refresh fails, do not acknowledge
  the candidate revision. Preserve the last good store, keep the poller dirty,
  and retry on the next tick even if no later commit occurs.
- If the profile scope or poller generation changes during an in-flight
  refresh, discard its acknowledgement; the existing session-store generation
  guard prevents stale rows from replacing the new scope.
- Log repeated failures with rate limiting or state-transition logging; do not
  show a toast every five seconds.
- A failure in one probe must not stop the poller permanently.

## Performance invariants

- An unchanged revision token while the poller is clean must not run the full
  session projection. A dirty poller intentionally retries an unacknowledged
  failed refresh without requiring a new revision.
- At most one poller-triggered full session refresh may run at a time. Existing
  event-driven refreshes retain their current request-generation guards and may
  overlap during gateway startup or a profile transition.
- The tracker uses read-only connections and must not create an empty
  `state.db` as a side effect.
- No renderer owns a duplicate background interval.
- Existing event-driven refreshes remain in place so local changes do not wait
  for the five-second fallback.

## Verification

### Automated tests

1. With a fake clock, unchanged revision, and a clean poller, multiple ticks
   perform only the lightweight probe and never call the full refresh.
2. With two real SQLite connections against a temporary profile database, a
   commit on the writer connection changes the tracker's aggregate revision
   token and triggers one refresh within the next renderer tick.
3. A failed refresh does not acknowledge the candidate revision, remains dirty,
   and retries on the next tick without requiring another database commit.
4. A commit that lands during a successful refresh is found by the post-refresh
   probe and produces one coalesced follow-up refresh.
5. Multiple ticks or commits while a refresh is in flight never run concurrent
   refreshes.
6. A never-created database produces a stable absent marker. A previously
   observed database that is missing or unreadable makes the aggregate probe
   fail and preserves the last good list; an atomic replacement later changes
   file identity, reopens the connection, and produces a different token.
7. Switching profile scopes prunes unused backend connections, increments the
   poller generation, resets the revision baseline, and cannot acknowledge an
   old-scope refresh as current.
8. A changed token causes renderer read-back through the canonical list API.
9. Renderer reconciliation preserves optimistic, pinned, active, and recently
   settled rows under the existing merge contract.
10. System resume invokes an immediate check without creating a second timer.
11. Unmount/gateway close clears the timer and backend shutdown closes every
    tracker connection without racing an in-flight probe.

Tests that exercise SQLite behavior must use a real temporary database rather
than mocking `PRAGMA data_version`. They must not read or write the user's real
`~/.hermes` state.

### Manual verification

1. Start Hermes One and leave its sessions visible in an unfocused or minimized
   window.
2. Create or continue a conversation in Hermes WebUI.
3. Confirm Hermes One starts the refresh by the next five-second probe and the
   session state appears after that refresh completes, without first focusing
   the app.
4. Repeat while Hermes One is showing a non-session screen; open the sessions
   view afterward and confirm the already-refreshed list appears immediately.
5. Put the Mac to sleep, mutate session state from another surface if possible,
   resume, and confirm immediate catch-up.
6. Observe idle logs/CPU/database access long enough to prove unchanged ticks do
   not run the full projection every five seconds.

## Delivery

The implementation should be one logical desktop change with focused tests.
The built application must be rebuilt and the installed Hermes One version must
be verified after replacement. Completion evidence includes the automated test
output, the installed bundle version/build identity, and the minimized-window
cross-surface manual test.
