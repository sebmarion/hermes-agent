# Hermes Health Sentinel Design

**Date:** 2026-07-24

**Status:** Approved design awaiting implementation planning

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
    interval_seconds: 300
    global_timeout_seconds: 45
    reminder_seconds: 3600
    maintenance_grace_seconds: 900
    ticker_max_age_seconds: 600
    gateway_label: ai.hermes.gateway
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

The `id` is authoritative. `name` is evidence for humans and a drift check,
not a fallback selector. A missing or renamed ID is an alert rather than an
instruction to guess another job.

The sentinel validates that positive numeric thresholds are within hardcoded
safe bounds. Invalid sentinel configuration produces a `CHECKER_BROKEN`
report; it never silently falls back to green.

## Sentinel Checks

`--mode sentinel` runs only the following checks. It does not perform provider
API calls, deep database checks, broad log scans, or any repair.

### 1. Managed gateway ownership and health

- Run `/bin/launchctl print gui/<uid>/ai.hermes.gateway` with a bounded
  timeout.
- Require the job to be loaded and running with an absolute program path.
- Probe the existing loopback gateway health endpoint with the existing
  bounded helper.
- A missing launchd job, non-running state, malformed identity, or failed
  health probe is an alert.

The sentinel records the loaded path, program, PID, and reported health as
evidence. It does not reload or restart the job.

### 2. Cron ticker liveness

- Read the mtimes of `~/.hermes/cron/ticker_heartbeat` and
  `~/.hermes/cron/ticker_last_success`.
- Alert if either file is missing or older than
  `ticker_max_age_seconds`.

The ten-minute default absorbs ordinary wake/login races while still detecting
a scheduler that is registered but no longer completing ticks.

### 3. Release and cron-admission gates

- Inspect the public `~/.local/bin/hermes` symlink without executing it.
- If its resolved basename is a maintenance-deny shim, report
  `MAINTENANCE_ACTIVE` during the configured grace period and
  `MAINTENANCE_STALE` after it.
- Read `~/.hermes/cron/.admission.json`.
- Closed admission is expected only while a non-stale maintenance gate is
  active. Otherwise report `CRON_ADMISSION_STUCK`.
- Missing, malformed, or unreadable gate state reports `CHECKER_BROKEN`, never
  healthy.

This is observation only. The sentinel does not complete a release, repoint
the CLI, or open admission.

### 4. Exact required jobs

For every configured job:

- require the exact ID to exist;
- require its stored name to match the configured evidence name;
- require `enabled: true` and `state: scheduled`;
- require `last_status` not to be `error`;
- require `last_run_at` to be present, parseable, and no older than the
  configured maximum lateness.

Failures are individually identified as missing, identity drift, inactive,
failed, or stale. Disabled and paused jobs are not skipped.

### 5. OpenViking semantic health

Run the existing
`~/.hermes/scripts/openviking-health-watchdog.sh` with a bounded timeout:

- empty stdout and exit zero means healthy;
- non-empty stdout and exit zero means `SUBJECT_DEGRADED`;
- nonzero exit, timeout, or execution failure means `CHECKER_BROKEN`.

This preserves the script's established “non-empty output is an alert”
contract while preventing the cron job's green `last_status` from hiding a
failed embedding dependency. The sentinel records a bounded excerpt and
digest, not unbounded command output.

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

The central invariant is:

> A completed sentinel observation cannot be persisted without a finite
> `next_check_at`.

`next_check_at` is `observed_at + interval_seconds`. It describes the next
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

Sentinel mode arms a 45-second process-wide deadline before probes begin. A
deadline produces an error report when possible and exits nonzero. Because
`KeepAlive` is absent, a failure cannot create a rapid restart loop; launchd
tries again on the next interval.

Launchd provides logged-in per-user supervision only. Sleep, logout, host
power loss, launchd failure, local disk failure, and malicious same-UID
modification remain explicit blind spots.

## Installation and Update Safety

Installation is additive:

1. Back up the exact current watchdog script and the affected `watchdog:`
   configuration.
2. Validate the modified script manually in sentinel mode before installing
   the plist.
3. Validate the plist with `plutil -lint`.
4. Bootstrap only `com.seb.hermes-health-sentinel`.
5. Verify `launchctl print` shows the canonical plist path, exact Python and
   script arguments, Aqua domain, 300-second interval, and no `KeepAlive`.
6. Wait for a fresh sentinel report and compare its timestamp and mode.

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
   it reports `MAINTENANCE_STALE`.
3. Closed admission outside active maintenance reports
   `CRON_ADMISSION_STUCK`.
4. Missing, paused, failed, stale, and wrong-name required jobs are each
   reported and are never skipped.
5. Non-empty OpenViking stdout with exit zero reports
   `SUBJECT_DEGRADED`.
6. OpenViking timeout/nonzero reports `CHECKER_BROKEN`.
7. Two identical alert runs attempt one immediate notification; a reminder is
   attempted only after its due time.
8. A fingerprint change notifies immediately.
9. Recovery requires a new healthy probe and emits one recovery notification.
10. Corrupt prior alert state fails safely without suppressing the current
    alert.
11. A global timeout exits without starting any repair or lifecycle command.
12. Quick and deep modes retain their existing state paths and behavior.

Launchd acceptance uses the real disposable sentinel label and real script,
but no real-service failure injection:

- `plutil -lint` passes;
- loaded definition matches the canonical plist and exact arguments;
- two scheduled ticks produce fresh reports;
- `KeepAlive` is absent;
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
