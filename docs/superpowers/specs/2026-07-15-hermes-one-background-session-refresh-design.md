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
one probe interval plus refresh and IPC time rather than a strict five-second
wall-clock SLA. Returning from system sleep should trigger an immediate
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

## Current behavior and constraint

The installed build's refresh path calls `syncSessionCache()`. That operation
is not a cheap heartbeat: it reads and projects the sessions table, resolves
missing titles, attaches lineage/context metadata, and rewrites the JSON cache.
Calling the full path unconditionally every five seconds would add needless
SQLite and filesystem work when the database is idle.

The current Hermes Agent checkout has evolved toward event-driven local-session
refresh and separate bounded polling for cron and messaging sessions. The
implementation plan must first identify the source revision used to build the
installed Hermes One app, then carry the same behavioral contract into the
current source architecture. The design does not depend on retaining the old
component layout.

## Considered approaches

### 1. Main-process SQLite change detector (selected)

Run a single five-second monitor in Electron's main process. Use a stable,
read-only SQLite connection to compare `PRAGMA data_version`. A changed revision
triggers the existing full session-cache/list refresh and an IPC notification
to renderer windows.

This keeps the idle tick cheap, works when renderers are hidden or throttled,
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

### Session cache monitor

Introduce a narrowly scoped main-process component, referred to here as
`SessionCacheMonitor`. It owns:

- one stable, read-only SQLite connection for the active profile database;
- the last observed `PRAGMA data_version` value;
- the database path and filesystem identity used by that connection;
- a monotonically increasing profile/database generation token;
- a five-second timer;
- single-flight refresh state; and
- start, stop, profile-change, and system-resume lifecycle methods.

`PRAGMA data_version` values are meaningful only when compared on the same
connection. The monitor must not open a fresh connection for every tick. When
the active profile or database path changes, it closes the old connection,
opens the new one, establishes a new baseline, and requests an immediate full
refresh through the existing profile-switch path.

Before comparing `data_version`, each tick also validates that the database
path still resolves to the same file identity (device/inode or the closest
portable equivalent available to the Electron main process). If the database
disappears or the identity changes because `state.db` was atomically replaced,
the monitor closes the stale connection. When the path becomes available, it
opens a new read-only connection, increments the generation token, establishes
a baseline, and requests a canonical full refresh. It must never continue
polling an unlinked old database inode.

The timer belongs to Electron's main process rather than a React component.
Chromium may throttle renderer timers when a window is hidden, minimized, or
unfocused; the main process must continue monitoring for as long as the app is
running.

### Refresh coordinator

On each tick:

1. Validate the connection's database path and file identity.
2. Read `PRAGMA data_version` from the monitor connection.
3. If the value matches the last successfully acknowledged revision and no
   dirty refresh is pending, do nothing else.
4. If it differs, capture the candidate revision and current generation, mark
   the monitor dirty, and request one refresh through the existing canonical
   session list/cache function.
5. If another tick or lifecycle event arrives while a refresh is running,
   remember one pending check instead of starting a concurrent refresh.
6. A candidate revision is acknowledged only after the canonical refresh
   succeeds for the same profile/database generation. A failed refresh leaves
   the monitor dirty so the next tick retries even if `data_version` has not
   changed again.
7. Immediately after a successful refresh, re-read `data_version`. If it no
   longer matches the candidate revision, keep the monitor dirty and run one
   follow-up refresh/check so a commit that landed during the refresh cannot be
   lost.
8. If the generation changed while the refresh was in flight, discard its
   result and notification. It must not update or publish state for the newly
   active profile.

Refresh work must be scoped to the captured profile/database identity. It may
either write only that profile's own cache or return a staged result that is
committed after the generation guard passes; it must not re-resolve a mutable
"active profile" halfway through asynchronous work.

The monitor must not introduce a second session-list projection or duplicate
the filtering, paging, lineage, archive, pin, or profile rules already owned by
the canonical refresh path.

### Renderer notification

After a successful refresh, the main process emits one session-list-changed
invalidation to every live Hermes One renderer window through the existing
preload/IPC boundary. The event carries the profile key and generation, not a
copied session-list payload. A renderer whose current scope includes that
profile reads back through the existing canonical cache/list API; a stale event
for a prior generation is ignored. This keeps filtering, paging, lineage,
archive, pin, and merge semantics in their current owner.

Renderer behavior:

- A mounted sidebar/session store reconciles the fresh list immediately, even
  when its window is unfocused or minimized.
- A screen that does not currently render sessions does not need to rebuild
  hidden DOM. The main-process cache is already current, and opening the
  sidebar/sessions screen reads that fresh data immediately.
- Existing optimistic rows for in-flight first turns, pinned sessions, active
  sessions, and recently settled sessions retain their current merge rules.
- Multiple windows receive the same notification; no window starts its own
  background database poll.

### Lifecycle

- Start the monitor after the desktop backend/profile is ready.
- Perform an immediate baseline/check rather than waiting five seconds.
- Keep it active while Hermes One is open, independent of focus, visibility,
  minimization, or route.
- On macOS system resume, request an immediate check.
- On profile switch, replace the monitored connection and baseline.
- Stop the timer and close the connection during app shutdown.
- Do not leave a helper, daemon, or launch agent running after quit.

## Failure handling

- If `state.db` does not exist yet, keep the last good list, retry on the next
  tick, and establish a baseline when the database appears.
- If the database is temporarily locked, replaced, or unavailable, keep the
  last good list and retry. Do not clear the sidebar.
- If the change probe succeeds but the full refresh fails, do not acknowledge
  the candidate revision or publish a success notification. Preserve the last
  good cache, keep the monitor dirty, and retry on the next tick even if no
  later commit occurs.
- If a database or profile generation changes during an in-flight refresh,
  discard that refresh's result and notification before it can affect the new
  generation.
- Log repeated failures with rate limiting or state-transition logging; do not
  show a toast every five seconds.
- A failure in one tick must not stop the monitor permanently.

## Performance invariants

- An unchanged `data_version` must not run the full session projection or write
  the JSON cache.
- At most one full session refresh may run at a time.
- The monitor uses a read-only connection and must not create an empty
  `state.db` as a side effect.
- No renderer owns a duplicate background interval.
- Existing event-driven refreshes remain in place so local changes do not wait
  for the five-second fallback.

## Verification

### Automated tests

1. With a fake clock and unchanged revision, multiple ticks perform only the
   lightweight probe and never call the full refresh.
2. With two real SQLite connections against a temporary profile database, a
   commit on the writer connection changes the monitor connection's
   `data_version` and triggers one refresh within the next tick.
3. A failed refresh does not acknowledge the candidate revision, remains dirty,
   and retries on the next tick without requiring another database commit.
4. A commit that lands during a successful refresh is found by the post-refresh
   probe and produces one coalesced follow-up refresh.
5. Multiple ticks or commits while a refresh is in flight never run concurrent
   refreshes.
6. A missing, locked, or atomically replaced database preserves the last good
   list; replacement changes file identity, reopens the connection, and
   recovers on a later tick.
7. Switching profiles closes the previous connection, increments the
   generation, resets the revision baseline, and cannot publish rows or an IPC
   invalidation from the old profile.
8. Successful refresh emits one profile/generation-scoped invalidation to all
   live windows; renderer read-back uses the canonical list API.
9. Renderer reconciliation preserves optimistic, pinned, active, and recently
   settled rows under the existing merge contract.
10. System resume invokes an immediate check without creating a second timer.
11. Shutdown clears the timer and closes the monitor connection.

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
