# Hermes Health Sentinel Design

**Date:** 2026-07-24

**Status:** Approved design under spec review

## Goal

Make Hermes' existing canaries and health checks follow through with the
smallest safe change: run a bounded, read-only subset of the existing health
watchdog outside Hermes cron, persist every result with its next scheduled
check, and repeat local alerts until a fresh probe proves recovery.

The sentinel must still work when Hermes cron or the public `hermes` CLI is
unavailable. It must not restart, resume, deploy, delete, or otherwise mutate
Hermes.

## Scope

The implementation changes only three runtime surfaces:

1. Extend `~/.hermes/scripts/hermes_health_watchdog.py` with a lightweight
   `--mode sentinel`.
2. Add the sentinel's required-job inventory and thresholds under `watchdog:`
   in `~/.hermes/config.yaml`.
3. Install one per-user Aqua LaunchAgent at
   `~/Library/LaunchAgents/com.seb.hermes-health-sentinel.plist`.

Focused tests may be added under `~/.hermes/scripts/tests/`. No Hermes core
module, model tool, plugin, cron schema, conversation, WebUI, gateway, or
Orchestrero component is added or changed.

## Explicit Non-Goals

- No automatic restart, `kickstart`, cron resume, manual job run, release
  completion, rollback, deployment, deletion, or arbitrary shell recovery.
- No SQLite database, action registry, generic campaign engine, outbox, Radar
  integration, new model tool, or new `HERMES_*` behavioral environment
  variable.
- No off-host delivery, boot-time coverage, logged-out coverage, or guarantee
  that a macOS notification was seen.
- No attempt to make same-UID files tamper-resistant.
- No change to the existing in-cron `hermes-health-watchdog` job. The same
  script continues to support its current `quick` and `deep` modes.
- No acknowledgment or snooze workflow in v1. An unresolved incident keeps
  reminding until verification clears it.

## Existing Seam

`~/.hermes/scripts/hermes_health_watchdog.py` already provides:

- bounded process and HTTP probes;
- atomic JSON writes;
- a full health report and heartbeat;
- alert fingerprinting and transition-only output;
- crash containment around individual checks;
- the Python 3.14 and PyYAML runtime needed to read `config.yaml`.

The sentinel extends that script rather than creating another controller. Its
state is projection and reminder state only; it never authorizes an effect.

## Configuration

All behavioral settings live in `~/.hermes/config.yaml`:

```yaml
watchdog:
  sentinel:
    maintenance_grace_seconds: 900
    ticker_max_age_seconds: 600
    required_jobs:
      - id: cbfd1c67dc66
        name: openviking-health-watchdog
        max_lateness_seconds: 1800
      - id: ca42de8f723a
        name: verified-state-guard-auto-promotion
        max_lateness_seconds: 129600
      - id: 761cf565a8ef
        name: Local First Controller
        max_lateness_seconds: 2700
      - id: 0a78af9e9c9e
        name: Local First Ornith Canary
        max_lateness_seconds: 10800
      - id: a8a79233c35a
        name: Local First Conditional Frontier Audit
        max_lateness_seconds: 129600
      - id: ded70cfc6cd6
        name: Local First Ledger Maintenance
        max_lateness_seconds: 129600
```

The sentinel cadence, process deadline, reminder period, and clock-skew
tolerance are deliberately not configurable in v1.
`SENTINEL_INTERVAL_SECONDS = 300` and
`SENTINEL_GLOBAL_TIMEOUT_SECONDS = 45`,
`SENTINEL_REMINDER_SECONDS = 3600`,
`SENTINEL_CLOCK_SKEW_SECONDS = 300`, and
`SENTINEL_GATEWAY_LABEL = "ai.hermes.gateway"` are fixed script constants.
The installed plist must carry the same 300-second interval, and sentinel mode
checks its own loaded launchd definition. A mismatch reports
`SENTINEL_DEFINITION_DRIFT`; it never changes `next_check_at` or silently
trusts the mismatched definition.

The `id` is authoritative. `name` is evidence for humans and a drift check,
not a fallback selector. A missing or renamed ID is an alert rather than an
instruction to guess another job.

The sentinel validates that the remaining configured positive numeric
thresholds are within hardcoded safe bounds. The fixed process-wide deadline
is armed before configuration is read. Invalid sentinel configuration
produces a `CHECKER_BROKEN` report using the fixed five-minute cadence, so
mandatory `next_check_at` remains computable; it never silently falls back to
green.

## Sentinel Checks

`--mode sentinel` runs only the following checks. It does not perform provider
API calls outside loopback, deep database checks, broad log scans, or any
repair.

### 1. Managed gateway ownership and health

- Run `/bin/launchctl print gui/<uid>/ai.hermes.gateway` using the fixed
  `SENTINEL_GATEWAY_LABEL` with a bounded
  timeout.
- Require the job to be loaded and running with an absolute program path.
- Probe the existing loopback gateway health endpoint with the existing
  bounded helper.
- A missing launchd job, non-running state, malformed identity, or failed
  health probe is an alert.

The sentinel records the loaded path, program, PID, and reported health as
evidence. It does not reload or restart the job.

### 2. Sentinel schedule identity

- Run `/bin/launchctl print
  gui/<uid>/com.seb.hermes-health-sentinel`.
- Require the loaded label, canonical plist path, Python executable, script
  path, Aqua session type, and 300-second run interval to match this contract.
- Any mismatch reports `SENTINEL_DEFINITION_DRIFT`.

This check is evidence only. It never bootstraps, reloads, or kickstarts
itself.

### 3. Cron ticker liveness

- Read the mtimes of `~/.hermes/cron/ticker_heartbeat` and
  `~/.hermes/cron/ticker_last_success`.
- Alert if either file is missing or older than
  `ticker_max_age_seconds`.
- A timestamp more than `SENTINEL_CLOCK_SKEW_SECONDS` in the future reports
  `CHECKER_BROKEN`; negative age is never accepted as fresh.

The ten-minute default absorbs ordinary wake/login races while still detecting
a scheduler that is registered but no longer completing ticks.

### 4. Release and cron-admission gates

- Inspect the public `~/.local/bin/hermes` symlink without executing it.
- Classify it as a maintenance-deny shim only when either the resolved target
  basename starts with `hermes-maintenance-`, or the first 4 KiB of the
  resolved regular file contains both
  `Hermes is temporarily unavailable while release transaction` and
  `exit 75`.
- Measure maintenance age from the public symlink's `lstat` mtime. If the
  public path is a matching regular file rather than a symlink, use that
  file's mtime. A matching gate with no trustworthy mtime is
  `CHECKER_BROKEN`. An mtime more than `SENTINEL_CLOCK_SKEW_SECONDS` in the
  future is also untrustworthy.
- If the recognized gate is present, report
  `MAINTENANCE_ACTIVE` during the configured grace period and
  `MAINTENANCE_STALE` after it.
- Read `~/.hermes/cron/.admission.json`.
- Closed admission is expected only while a non-stale maintenance gate is
  active. Otherwise report `CRON_ADMISSION_STUCK`.
- Missing, malformed, or unreadable gate state reports `CHECKER_BROKEN`, never
  healthy.

This is observation only. The sentinel does not complete a release, repoint
the CLI, or open admission.

### 5. Exact required jobs

The authoritative store is
`<HERMES_DIR>/cron/jobs.json`, where the installed LaunchAgent's
`HERMES_DIR` is the resolved `/Users/seb/.hermes`. The canonical schema is a
JSON object with a `jobs` array and optional `updated_at`. A legacy top-level
array, malformed object, duplicate job ID, or unreadable file reports
`CHECKER_BROKEN`; sentinel mode does not guess or migrate it.

Tests may override `HERMES_DIR` with the watchdog's existing test seam, but the
installed plist supplies no override.

For every configured job:

- require the exact ID to exist;
- require its stored name to match the configured evidence name;
- require `enabled: true` and `state: scheduled`;
- require `last_status` not to be `error`;
- require `last_run_at` to be present, parseable, and no older than the
  configured maximum lateness.

`last_run_at` must be timezone-aware ISO-8601. Comparisons are made in UTC;
naive, invalid, or more than `SENTINEL_CLOCK_SKEW_SECONDS` future-dated
timestamps report the job as malformed.

Failures are individually identified as missing, identity drift, inactive,
failed, or stale. Disabled and paused jobs are not skipped.

### 6. OpenViking semantic health

Sentinel mode does not execute
`~/.hermes/scripts/openviking-health-watchdog.sh`. Instead it implements the
same three semantic probes directly with the watchdog's existing bounded
Python HTTP helper:

- read at most 64 KiB from `~/.openviking/ov.conf`;
- require the configured server and embedding URLs to resolve to loopback
  hosts;
- GET the OpenViking health endpoint and require its healthy contract;
- GET the configured embedding models endpoint; and
- POST one fixed health-probe input to the configured embedding endpoint,
  requiring a non-empty vector of the configured dimension.

Missing or malformed configuration, a non-loopback URL, or an internal probe
exception reports `CHECKER_BROKEN`. A timeout, refused connection, non-success
HTTP response, malformed service response, unhealthy response, or invalid
embedding result from an otherwise valid local target reports
`SUBJECT_DEGRADED`. Evidence contains only bounded, redacted response excerpts
and digests.

The existing cron job and shell checker remain unchanged. Duplicating this
small read-only probe in sentinel mode removes a repeatedly executed mutable
shell dependency and makes the no-lifecycle/no-repair boundary enforceable:
the sentinel performs only configuration reads and bounded loopback HTTP
requests.

## State and Follow-Through

Sentinel mode uses separate files so it cannot overwrite the existing quick
watchdog report:

- `~/.hermes/state/sentinel_last_report.json`
- `~/.hermes/state/sentinel_alert_state.json`

Every completed run atomically writes a report containing:

```json
{
  "schema": "hermes.health_sentinel.v1",
  "mode": "sentinel",
  "status": "GREEN | WARNING | ALERT | ERROR",
  "observed_at": "ISO-8601 timestamp",
  "next_check_at": "ISO-8601 timestamp",
  "fingerprint": "sha256 of normalized active issues",
  "issues": [],
  "evidence": {}
}
```

Issue records have the fixed shape:

```json
{
  "code": "stable machine code",
  "subject": "stable job ID, launchd label, or path",
  "severity": "WARNING | ALERT | ERROR",
  "summary": "bounded human text",
  "evidence": {}
}
```

Overall report status is the highest issue severity, with no issues producing
`GREEN`. `MAINTENANCE_ACTIVE` is `WARNING`; it is reported but does not enter
the active alert fingerprint. `MAINTENANCE_STALE`,
`CRON_ADMISSION_STUCK`, `SENTINEL_DEFINITION_DRIFT`, required-job failures,
and `SUBJECT_DEGRADED` are `ALERT`. `CHECKER_BROKEN` is `ERROR`.

The fingerprint is SHA-256 over sorted unique
`(code, subject, severity)` tuples for `ALERT` and `ERROR` issues only.
Timestamps, summaries, command output, PIDs, ages, and other volatile evidence
are excluded, so an unchanged incident cannot evade reminder throttling.

The central invariant is:

> A completed sentinel observation cannot be persisted without a finite
> `next_check_at`.

`next_check_at` is
`observed_at + SENTINEL_INTERVAL_SECONDS`. It describes the next
expected launchd check; it is not a second scheduler.

The alert-state file contains the current fingerprint, `first_seen_at`,
`last_seen_at`, `last_notification_attempt_at`, and
`next_notification_at`. It has no action or resolution authority.

### Notification policy

- New or changed active fingerprint: attempt a local macOS notification
  immediately.
- Unchanged active fingerprint: attempt one reminder when
  `next_notification_at` is due, then schedule the next hourly reminder.
- Transition from active to healthy: attempt one recovery notification and
  clear the active fingerprint.
- Multiple missed launchd intervals coalesce into one current observation and
  at most one due reminder.
- Notification command success means `attempted`, not delivered or seen.
- Notification failure is recorded in the report and alert state. It does not
  erase or resolve the incident.

Sentinel runs are serialized with a nonblocking `fcntl` lock at
`~/.hermes/state/sentinel.lock`. A second manual or launchd invocation exits
without probing or notifying while the lock is held.

For every due alert or recovery notification, the runner:

1. atomically persists a unique attempt ID, `pending` state,
   `last_notification_attempt_at`, and the next notification time computed
   with `SENTINEL_REMINDER_SECONDS`;
2. invokes `/usr/bin/osascript` with a five-second timeout and `shell=False`.
   The AppleScript source and `Hermes Health Sentinel` title are fixed
   literals; the redacted body, capped at 400 characters, is supplied as a
   separate `run argv` value and is never interpolated into executable
   AppleScript;
3. atomically records `attempted` or `failed` for the same attempt ID.

Persist-before-attempt prevents a crash after notification submission from
causing a retry storm. A crash after step 1 but before step 2 may suppress that
hour's best-effort notification; the persistent red report remains canonical
and the next hourly reminder remains scheduled. The design does not claim
exactly-once delivery or human receipt.

Only a fresh sentinel run with no active issues resolves the local incident.
An exit code, PID change, notification attempt, or prior green report cannot.

## Runtime Boundaries

The LaunchAgent is per-user and Aqua-session scoped:

```text
Label: com.seb.hermes-health-sentinel
ProgramArguments:
  /opt/homebrew/bin/python3.14
  /Users/seb/.hermes/scripts/hermes_health_watchdog.py
  --mode
  sentinel
RunAtLoad: true
StartInterval: 300
ProcessType: Background
Umask: 077
LimitLoadToSessionType: Aqua
KeepAlive: absent
StartCalendarInterval: absent
WorkingDirectory: absent
```

The plist supplies no inherited shell path, virtual environment, provider
credential, or Hermes behavioral environment variable. The script uses
absolute paths for external commands.

Sentinel mode arms the fixed
`SENTINEL_GLOBAL_TIMEOUT_SECONDS` process-wide deadline before configuration
or prior state is read and before probes begin. A deadline produces an error
report when possible and exits nonzero. Because `KeepAlive` is absent, a
failure cannot create a rapid restart loop; launchd tries again on the next
interval.

Launchd provides logged-in per-user supervision only. Sleep, logout, host
power loss, launchd failure, local disk failure, and malicious same-UID
modification remain explicit blind spots.

“Rechecked every five minutes” throughout this design means while the Aqua
session is active, the host is awake, launchd is functioning, and no prior
sentinel process remains hung. Missed sleep or logout intervals are not
replayed.

## Installation and Update Safety

Installation is additive:

1. Back up the exact current watchdog script and the affected `watchdog:`
   configuration.
2. Run the modified script manually in sentinel mode before installing the
   plist and require a structurally valid report. Because the sentinel label
   is not loaded yet, `SENTINEL_DEFINITION_DRIFT` is the sole expected
   installation-state alert at this stage; it is not accepted as a healthy
   result.
3. Validate the plist with `plutil -lint`.
4. Bootstrap only `com.seb.hermes-health-sentinel`.
5. Verify `launchctl print` shows the canonical plist path, exact Python and
   script arguments, Aqua domain, 300-second interval, and no `KeepAlive`,
   `StartCalendarInterval`, or `WorkingDirectory`.
6. Wait for a fresh sentinel report, compare its timestamp and mode, and
   require the pre-install `SENTINEL_DEFINITION_DRIFT` issue to disappear.

The pre-bootstrap manual run intentionally uses the normal notification
policy, so it may attempt one real definition-drift alert. The first
post-bootstrap run attempts a recovery notification only if no other active
issues remain; otherwise the ordinary changed- or unchanged-fingerprint rule
applies. This one-time install transition is part of validating the alert
path, not a silent special case.

The installation must not boot out, kickstart, terminate, or restart the
gateway, WebUI, Hermes One, cron, OpenViking, or any existing service.

Payload updates modify the script or config only. A plist reload is required
only when the launch contract itself changes.

## Error Handling

- A single check exception becomes `CHECKER_BROKEN`; remaining checks still
  run within the global deadline.
- A malformed jobs file, config, admission file, or timestamp cannot produce
  green.
- Atomic report failure exits nonzero and leaves the prior report intact. The
  prior report's age then exposes staleness.
- Probe output, exception text, and notification errors are length-bounded
  before persistence.
- The sentinel never interprets a missing state file as permission to act.

## Testing

Focused tests use temporary Hermes homes and stubbed subprocess/HTTP probes.
They must cover:

1. Healthy gateway, fresh ticker, open admission, healthy required jobs, and
   empty OpenViking output produce green with a finite `next_check_at`.
2. A maintenance shim within grace reports `MAINTENANCE_ACTIVE`; after grace
   it reports `MAINTENANCE_STALE`, using the defined public-link mtime.
3. Closed admission outside active maintenance reports
   `CRON_ADMISSION_STUCK`.
4. Missing, paused, failed, stale, and wrong-name required jobs are each
   reported and are never skipped.
5. A well-formed unhealthy OpenViking or embedding response reports
   `SUBJECT_DEGRADED`.
6. Invalid OpenViking config, non-loopback URLs, or an internal probe exception
   reports `CHECKER_BROKEN`; timeout, malformed target response, or transport
   failure reports `SUBJECT_DEGRADED`. No shell checker or lifecycle command is
   executed.
7. Two identical alert runs attempt one immediate notification; a reminder is
   attempted only after its due time.
8. A fingerprint change notifies immediately.
9. Recovery requires a new healthy probe and emits one recovery notification.
10. Corrupt prior alert state fails safely without suppressing the current
    alert.
11. A global timeout exits without starting any repair or lifecycle command.
12. Quick and deep modes retain their existing state paths and behavior.
13. Volatile evidence changes do not change the active fingerprint.
14. A crash after notification intent persistence does not create another
    attempt before the next hourly reminder.
15. A concurrent manual invocation loses the lock and cannot notify.
16. Loaded sentinel interval or identity drift reports
    `SENTINEL_DEFINITION_DRIFT`.
17. A pre-bootstrap manual run produces a valid report with the expected
    definition-drift issue; the first post-bootstrap report clears that issue.
18. Job and ticker timestamps more than the fixed skew tolerance in the
    future cannot produce green.
19. Notification bodies containing quotes, newlines, or AppleScript tokens
    remain a single data argument to fixed AppleScript source.

Launchd acceptance installs and uses the fixed canonical sentinel label and
real script, but performs no real-service failure injection. Definition-parser
unit tests use captured `launchctl print` fixtures for mismatched identities;
there is no alternate live label:

- `plutil -lint` passes;
- loaded definition matches the canonical plist and exact arguments;
- two scheduled ticks produce fresh reports;
- `KeepAlive`, `StartCalendarInterval`, and `WorkingDirectory` are absent;
- no gateway or WebUI PID changes during installation and observation;
- public maintenance shim, paused verified-state job, and current OpenViking
  embedding degradation are detected when those live conditions still exist.

## Rollback

Rollback is limited to the new sentinel:

1. Verify the exact loaded label and canonical plist path.
2. Boot out only `com.seb.hermes-health-sentinel`.
3. Remove only its plist.
4. Restore the backed-up script and `watchdog:` configuration.
5. Preserve sentinel report and alert-state files as diagnostic evidence
   unless the user explicitly requests their removal.

No Hermes service restart is part of rollback.

## Acceptance Criteria

The change is accepted only when:

- the sentinel runs from launchd independently of Hermes cron and the public
  CLI;
- every completed report contains a finite `next_check_at`;
- an unchanged live failure remains open and is rechecked every five minutes;
- reminders are bounded to at most one per hour per unchanged fingerprint;
- recovery is emitted only after a fresh healthy observation;
- the three known blind spots are visible when still present: stale public CLI
  maintenance, paused verified-state promotion, and OpenViking embedding
  degradation hidden behind cron `ok`;
- no Hermes/WebUI process identity, active session, cron job, release gate, or
  service lifecycle state is mutated.
