# Hermes Health Sentinel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the smallest independent, read-only Hermes health loop that
persists a finite next check and repeats local alerts until fresh observation
proves recovery.

**Architecture:** Extend the existing user-runtime watchdog with a third,
strictly isolated `sentinel` mode. The mode reads existing launchd, cron, gate,
and loopback health state; writes separate atomic report/reminder files; and is
invoked every five minutes by one Aqua LaunchAgent. Existing `quick` and
`deep` behavior stays unchanged, and no path in sentinel mode can perform
service lifecycle or recovery actions.

**Tech Stack:** Python 3.14 standard library (`fcntl`, `hashlib`, `ipaddress`,
`json`, `plistlib`, `signal`, `subprocess`, `urllib`), PyYAML already present
in the selected interpreter, pytest, macOS `launchctl`, `plutil`, and
`osascript`.

**Execution ownership:** Local Ornith performs Tasks 1–4 in bounded passes.
Codex reviews every diff, runs acceptance, performs Task 5 integration, and
performs Task 6 activation. Ornith must not bootstrap/unload/kickstart a
LaunchAgent, send a notification, or modify any service lifecycle state.
Orchestrero has no role.

**Non-git runtime note:** The implementation targets are user runtime files
outside this repository. Before any target is replaced, Codex creates an exact
timestamped backup with a SHA-256 manifest. The implementation plan and review
receipts are committed in this worktree; runtime integrity is proven by
backup/current hashes and focused tests rather than pretending the runtime
directory is a Git checkout.

---

## File Map

- Modify: `/Users/seb/.hermes/scripts/hermes_health_watchdog.py`
  - Retains quick/deep behavior.
  - Owns sentinel constants, pure checks, state transition logic, deadline,
    lock, and CLI dispatch.
- Create:
  `/Users/seb/.hermes/scripts/tests/test_hermes_health_watchdog_sentinel.py`
  - Hermetic sentinel unit/integration tests using temporary Hermes homes and
    stubbed HTTP/subprocess boundaries.
- Modify: `/Users/seb/.hermes/config.yaml`
  - Adds only `watchdog.sentinel` thresholds and the six exact required jobs.
- Create:
  `/Users/seb/Library/LaunchAgents/com.seb.hermes-health-sentinel.plist`
  - Runs the fixed script/interpreter every 300 seconds in the Aqua domain.
- Runtime outputs created by the script:
  - `/Users/seb/.hermes/state/sentinel_last_report.json`
  - `/Users/seb/.hermes/state/sentinel_alert_state.json`
  - `/Users/seb/.hermes/state/sentinel.lock`
- Create during integration:
  `/Users/seb/.hermes/backups/hermes-health-sentinel-<UTC timestamp>/`
  - Exact pre-change script/config/plist copies plus `SHA256SUMS`.

## Fixed v1 Contract

Implement these as module constants, not environment variables:

```python
SENTINEL_SCHEMA = "hermes.health_sentinel.v1"
SENTINEL_LABEL = "com.seb.hermes-health-sentinel"
SENTINEL_GATEWAY_LABEL = "ai.hermes.gateway"
SENTINEL_INTERVAL_SECONDS = 300
SENTINEL_GLOBAL_TIMEOUT_SECONDS = 45
SENTINEL_REMINDER_SECONDS = 3600
SENTINEL_CLOCK_SKEW_SECONDS = 300
SENTINEL_NOTIFICATION_TIMEOUT_SECONDS = 5
SENTINEL_MAX_EVIDENCE_CHARS = 800
SENTINEL_MAX_NOTIFICATION_CHARS = 400

SENTINEL_REPORT_PATH = STATE_DIR / "sentinel_last_report.json"
SENTINEL_ALERT_STATE_PATH = STATE_DIR / "sentinel_alert_state.json"
SENTINEL_LOCK_PATH = STATE_DIR / "sentinel.lock"
SENTINEL_PLIST_PATH = (
    Path.home()
    / "Library"
    / "LaunchAgents"
    / "com.seb.hermes-health-sentinel.plist"
)
```

The config block is:

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

## Issue and Status Contract

Use one constructor so every issue has the same stable shape:

```python
def sentinel_issue(
    code: str,
    subject: str,
    severity: str,
    summary: str,
    evidence: dict | None = None,
) -> dict:
    return {
        "code": code,
        "subject": subject,
        "severity": severity,
        "summary": summary[:SENTINEL_MAX_EVIDENCE_CHARS],
        "evidence": bound_evidence(evidence or {}),
    }
```

Severity ranking is `GREEN < WARNING < ALERT < ERROR`.
`MAINTENANCE_ACTIVE` is `WARNING` and is excluded from alert fingerprinting.
All other operational failures are `ALERT`; malformed checker/config/state
inputs use `CHECKER_BROKEN` at `ERROR`.

The active fingerprint is:

```python
active = {
    (issue["code"], issue["subject"], issue["severity"])
    for issue in issues
    if issue["severity"] in {"ALERT", "ERROR"}
}
fingerprint = hashlib.sha256(
    json.dumps(sorted(active), separators=(",", ":")).encode()
).hexdigest() if active else ""
```

Volatile evidence, age, PID, output, timestamps, and summaries never enter the
fingerprint.

---

### Task 1: Establish the Hermetic Sentinel Test Harness

**Owner:** Ornith

**Files:**

- Create:
  `/Users/seb/.hermes/scripts/tests/test_hermes_health_watchdog_sentinel.py`
- Modify: `/Users/seb/.hermes/scripts/hermes_health_watchdog.py`

- [ ] **Step 1: Create a test loader that isolates module globals**

Set `WATCHDOG_HERMES_DIR` to a pytest `tmp_path`, load the watchdog under a
unique module name with `importlib.util.spec_from_file_location`, and restore
the environment afterward. Never import the live module once at collection
time because all path constants are computed during import.

```python
def load_watchdog(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCHDOG_HERMES_DIR", str(tmp_path))
    name = f"hermes_health_watchdog_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, WATCHDOG_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module
```

- [ ] **Step 2: Write failing contract tests for constants and mode dispatch**

Cover:

- parser accepts exactly `quick`, `deep`, and `sentinel`;
- sentinel paths are separate from quick/deep paths;
- fixed cadence/deadline/reminder/skew constants match the spec;
- `next_check_at` is finite and exactly 300 seconds after `observed_at`;
- quick/deep report and heartbeat path constants remain unchanged.

- [ ] **Step 3: Run the new tests and prove RED**

Run:

```bash
/opt/homebrew/bin/python3.14 -m pytest -q \
  /Users/seb/.hermes/scripts/tests/test_hermes_health_watchdog_sentinel.py
```

Expected: failures because sentinel mode and constants do not exist.

- [ ] **Step 4: Add only the sentinel constants, path constants, and parser
      branch needed to collect the tests**

Extract parser construction into a small `build_parser()` function so tests do
not call `main()`. Preserve `quick` as the default and retain the existing
`WATCHDOG_MODE` compatibility for quick/deep callers. An invalid environment
default must still be rejected by argparse rather than silently choosing
sentinel.

- [ ] **Step 5: Re-run the focused tests**

Expected: the Task 1 contract tests pass; later sentinel behavior tests may
still be absent.

- [ ] **Step 6: Prove quick/deep have not moved**

Run:

```bash
/opt/homebrew/bin/python3.14 \
  /Users/seb/.hermes/scripts/hermes_health_watchdog.py --help
/opt/homebrew/bin/python3.14 -m py_compile \
  /Users/seb/.hermes/scripts/hermes_health_watchdog.py
```

Expected: help lists `quick`, `deep`, `sentinel`; compilation succeeds.

- [ ] **Step 7: Return a receipt to Codex**

Report exact modified paths, test output, and
`shasum -a 256` for both files. Do not touch config, plist, launchd, or state.

---

### Task 2: Implement Pure Read-Only Sentinel Checks

**Owner:** Ornith

**Files:**

- Modify: `/Users/seb/.hermes/scripts/hermes_health_watchdog.py`
- Modify:
  `/Users/seb/.hermes/scripts/tests/test_hermes_health_watchdog_sentinel.py`

- [ ] **Step 1: Write failing config-validation tests**

`load_sentinel_config(cfg)` must accept only a mapping at
`watchdog.sentinel`, require positive bounded
`maintenance_grace_seconds`/`ticker_max_age_seconds`, require a non-empty list
of exact job records, reject duplicate IDs, reject unknown/missing keys, and
return `CHECKER_BROKEN` evidence rather than a healthy empty inventory.

- [ ] **Step 2: Implement minimal config validation**

Do not add fallback thresholds. Invalid config is an error issue, but
`next_check_at` remains computable from the fixed cadence.

- [ ] **Step 3: Write failing launchd identity tests**

Use captured `launchctl print` text fixtures and temporary plist files. Cover:

- healthy `ai.hermes.gateway` loaded/running/absolute program;
- missing, stopped, malformed gateway identity;
- canonical sentinel label/path/interpreter/script/arguments/run interval;
- exact `RunAtLoad`, `ProcessType`, `Umask`, and Aqua session;
- absence of `KeepAlive`, `StartCalendarInterval`, and `WorkingDirectory`;
- mismatched source plist or loaded output produces
  `SENTINEL_DEFINITION_DRIFT`.

- [ ] **Step 4: Implement launchd checks using fixed argv**

Only these subprocess calls are permitted in observation:

```python
["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"]
```

Parse the source plist with `plistlib`, not shell tools. Do not call
`bootout`, `bootstrap`, `kickstart`, `enable`, or `disable`.

- [ ] **Step 5: Write failing ticker and gate tests**

Cover:

- missing/stale/fresh `ticker_heartbeat` and `ticker_last_success`;
- mtimes more than 300 seconds in the future are not fresh;
- public symlink target basename beginning `hermes-maintenance-`;
- regular-file fallback whose first 4 KiB includes the exact maintenance
  sentence and `exit 75`;
- symlink `lstat` mtime controls maintenance age;
- active grace is warning, stale is alert;
- closed admission is allowed only during active non-stale maintenance;
- missing/malformed admission JSON is `CHECKER_BROKEN`.

- [ ] **Step 6: Implement ticker and gate checks with filesystem reads only**

Never execute `/Users/seb/.local/bin/hermes` or a maintenance target. Bound
wrapper reads to 4 KiB.

- [ ] **Step 7: Write failing exact-job tests**

Create canonical `cron/jobs.json` fixtures. Cover healthy, missing, duplicate
ID, wrong name, disabled, paused, failed, missing status, missing/naive/bad/
stale/future `last_run_at`, legacy top-level list, and unreadable/malformed
store. IDs are authoritative; names never become fallback selectors.

- [ ] **Step 8: Implement exact-job checks**

Read only `<WATCHDOG_HERMES_DIR>/cron/jobs.json`. Parse timezone-aware ISO-8601
and compare in UTC.

- [ ] **Step 9: Write failing direct OpenViking tests**

Fixtures must cover:

- valid `~/.openviking/ov.conf` equivalent under a test seam;
- healthy `/health`, `/models`, and `/embeddings` responses;
- refused connection, timeout, unhealthy/HTTP/malformed responses;
- missing model/dimension/API base;
- non-loopback host;
- a loopback URL returning a redirect, proving the redirect is not followed;
- wrong or empty embedding vector.

Monkeypatch the Python HTTP boundary. Assert that no shell checker and no
lifecycle command is invoked.

- [ ] **Step 10: Implement bounded loopback-only OpenViking probes**

Read at most 64 KiB. Accept only `localhost`, `127.0.0.0/8`, or `::1`; do not
perform DNS resolution for arbitrary hostnames. Use an opener whose redirect
handler refuses all redirects. Cap response bodies before JSON parsing. POST
only:

```json
{"model": "<configured model>", "input": "OpenViking health probe"}
```

Configuration/boundary/internal errors are `CHECKER_BROKEN`. A reachable
subject that times out, refuses, reports bad HTTP/JSON/health, or returns an
invalid embedding is `SUBJECT_DEGRADED`.

- [ ] **Step 11: Run the complete sentinel test file**

Expected: all Task 1–2 tests pass.

- [ ] **Step 12: Return a receipt to Codex**

Include test count/output, file hashes, and a grep receipt showing no lifecycle
argv was introduced:

```bash
rg -n 'bootout|bootstrap|kickstart|launchctl (enable|disable)|cron (run|resume)' \
  /Users/seb/.hermes/scripts/hermes_health_watchdog.py
```

Printed documentation strings are not execution; report any matches with
their call context.

---

### Task 3: Implement Follow-Through State, Locking, Deadline, and Notification

**Owner:** Ornith

**Files:**

- Modify: `/Users/seb/.hermes/scripts/hermes_health_watchdog.py`
- Modify:
  `/Users/seb/.hermes/scripts/tests/test_hermes_health_watchdog_sentinel.py`

- [ ] **Step 1: Write failing report/fingerprint tests**

Cover fixed issue shape, severity aggregation, warning-only state, sorted
deduplicated fingerprint tuples, volatile evidence stability, bounded
evidence, UTC timestamps, and the invariant that every completed report has
finite `next_check_at = observed_at + 300s`.

- [ ] **Step 2: Implement pure report helpers**

Keep sentinel report construction separate from existing quick/deep report
format. Do not change `emit_alert_transition`.

- [ ] **Step 3: Write failing alert-state transition tests**

Cover:

- first active fingerprint is immediately due;
- identical active fingerprint is not due until 3600 seconds;
- changed fingerprint is immediately due;
- missed intervals coalesce into one reminder;
- fresh green after active creates one recovery attempt;
- corrupt state cannot suppress a current alert;
- warning-only maintenance never enters the active fingerprint;
- a crash after pending intent persistence cannot create another attempt
  before the next hourly time.

- [ ] **Step 4: Implement persist-before-attempt state transitions**

Use one pure decision helper and atomic writes. Persist a unique attempt ID,
`pending`, `last_notification_attempt_at`, and the next reminder time before
calling the notifier. After the attempt, persist `attempted` or `failed`.
Command success means attempted, never delivered or acknowledged.

- [ ] **Step 5: Write failing notification-argv tests**

Issue codes/subjects containing quotes, newlines, dashes, or AppleScript text
must remain one data argument. The executable source must stay fixed.

- [ ] **Step 6: Implement the notifier with fixed AppleScript**

Use `subprocess.run(..., shell=False, timeout=5)` with fixed `-e` program
fragments and pass the body after `--` to `run argv`. Build the body only from
bounded issue code/subject pairs and next-check time; do not include raw
evidence, URLs, command output, or exception text.

The fixed argv prefix is:

```python
[
    "/usr/bin/osascript",
    "-e", "on run argv",
    "-e", (
        'display notification (item 1 of argv) '
        'with title "Hermes Health Sentinel"'
    ),
    "-e", "end run",
    "--",
]
```

- [ ] **Step 7: Write failing concurrency/deadline tests**

Cover:

- a second process cannot acquire `sentinel.lock` and performs no probes or
  notification;
- the fixed 45-second alarm is armed before config/state reads;
- deadline exceptions bypass per-check catch blocks;
- timeout still writes an error report when the state directory is writable;
- no repair/lifecycle command starts after deadline.

- [ ] **Step 8: Implement `fcntl` lock and `signal.setitimer` deadline**

Use a dedicated exception path that is re-raised by individual-check
containment. Always disarm the timer and release the lock in `finally`.

- [ ] **Step 9: Integrate `run_sentinel()` into `main()`**

Branch immediately after parsing:

```python
if args.mode == "sentinel":
    return run_sentinel()
```

Do not load quick/deep credentials, DB-growth heartbeat, provider checks, logs,
or databases in sentinel mode.

- [ ] **Step 10: Run sentinel and legacy-focused tests**

Run:

```bash
/opt/homebrew/bin/python3.14 -m pytest -q \
  /Users/seb/.hermes/scripts/tests/test_hermes_health_watchdog_sentinel.py
/opt/homebrew/bin/python3.14 -m py_compile \
  /Users/seb/.hermes/scripts/hermes_health_watchdog.py
```

Then run quick/deep against a temporary Hermes home or monkeypatched fixtures;
never overwrite the live quick/deep report as part of a test.

- [ ] **Step 11: Return a receipt to Codex**

Include test results, hashes, and exact notifier argv from the unit-test
capture. Do not execute a real notification.

---

### Task 4: Prepare Configuration and LaunchAgent Artifacts

**Owner:** Ornith prepares; Codex reviews and installs.

**Files:**

- Propose patch for: `/Users/seb/.hermes/config.yaml`
- Create candidate:
  `/Users/seb/Library/LaunchAgents/com.seb.hermes-health-sentinel.plist`

- [ ] **Step 1: Produce the minimal YAML patch**

Add one top-level `watchdog.sentinel` block exactly as listed in the fixed
contract. Do not reformat, reorder, or rewrite unrelated config.

- [ ] **Step 2: Produce the exact plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.seb.hermes-health-sentinel</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3.14</string>
    <string>/Users/seb/.hermes/scripts/hermes_health_watchdog.py</string>
    <string>--mode</string>
    <string>sentinel</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>300</integer>
  <key>ProcessType</key>
  <string>Background</string>
  <key>Umask</key>
  <integer>63</integer>
  <key>LimitLoadToSessionType</key>
  <string>Aqua</string>
</dict>
</plist>
```

Do not add `KeepAlive`, `StartCalendarInterval`, `WorkingDirectory`,
environment variables, log paths, or shell wrappers.

- [ ] **Step 3: Validate artifacts without loading launchd**

Run `plutil -lint` and `plutil -p` on the candidate. Parse it with `plistlib`
in the focused test and verify the exact dictionary: canonical `Label`, exact
four `ProgramArguments`, `RunAtLoad is True`, `StartInterval == 300`,
`ProcessType == "Background"`, `Umask == 63`,
`LimitLoadToSessionType == "Aqua"`, and absence of `KeepAlive`,
`StartCalendarInterval`, `WorkingDirectory`, and `EnvironmentVariables`.

- [ ] **Step 4: Return artifacts and receipt**

Report plist/config candidate hashes and static validation. Do not call
`launchctl`.

---

### Task 5: Supervisor Review and Atomic Runtime Integration

**Owner:** Codex only

**Files:**

- Review all Task 1–4 targets.
- Create:
  `/Users/seb/.hermes/backups/hermes-health-sentinel-<UTC timestamp>/`

- [ ] **Step 1: Capture pre-change process and target identity**

Record:

- gateway PID, program, and health response;
- WebUI listener PID and health response;
- current script/config/plist existence, modes, owners, and SHA-256 hashes;
- current public Hermes symlink, admission state, ticker mtimes, and six job
  states.

- [ ] **Step 2: Create exact backups**

Copy script and config with metadata. Copy the plist only if it already exists.
Write and read back `SHA256SUMS`. Never overwrite an existing backup path.

- [ ] **Step 3: Review Ornith's diff against the spec**

Reject any:

- lifecycle/recovery command;
- remote network call;
- quick/deep format or state-path change;
- unbounded read/output;
- shell interpolation;
- new environment-variable behavior;
- unrelated config rewrite.

- [ ] **Step 4: Run the full focused suite from current files**

Require all sentinel tests and `py_compile` to pass. Run
`git diff --check` for the plan worktree and inspect external file diffs
against backups with `diff -u`.

- [ ] **Step 5: Install config/script/test/plist atomically**

Apply only the reviewed hunks. Preserve script owner and executable mode.
Re-read hashes and compare with the reviewed candidates. Do not touch any
existing LaunchAgent. “Install” in this step means writing the reviewed files
to disk only: do not call `launchctl` and do not load the new plist until
Task 6.

- [ ] **Step 6: Run the pre-bootstrap sentinel observation**

Run the exact Python/script arguments manually. Require a structurally valid
report with finite `next_check_at`. Before bootstrap,
`SENTINEL_DEFINITION_DRIFT` is expected. A real local alert attempt is also
expected by design. Do not interpret other live red issues as installation
failure unless the checker itself is broken.

- [ ] **Step 7: Re-check gateway/WebUI identity**

PIDs/program paths and health must be unchanged from Step 1. Stop before
activation if either changed unexpectedly.

---

### Task 6: Bootstrap Only the Sentinel and Prove Follow-Through

**Owner:** Codex only

**Files/state:**

- Load only:
  `/Users/seb/Library/LaunchAgents/com.seb.hermes-health-sentinel.plist`
- Observe:
  `/Users/seb/.hermes/state/sentinel_last_report.json`
  `/Users/seb/.hermes/state/sentinel_alert_state.json`

- [ ] **Step 1: Resolve exact activation target**

Confirm the canonical plist label/path and that no loaded job of that label
already points elsewhere. If the label already exists unexpectedly, stop; do
not boot it out or replace it.

- [ ] **Step 2: Bootstrap only the new label**

Use:

```bash
/bin/launchctl bootstrap gui/$(id -u) \
  /Users/seb/Library/LaunchAgents/com.seb.hermes-health-sentinel.plist
```

Do not call kickstart. `RunAtLoad` supplies the first launch.

- [ ] **Step 3: Verify loaded identity**

`launchctl print` must show the canonical path, exact interpreter/script
arguments, `run interval = 300`, and no prohibited scheduling/lifecycle keys.
The fresh report must no longer contain `SENTINEL_DEFINITION_DRIFT`.

- [ ] **Step 4: Observe two scheduled ticks**

Without invoking the script manually, wait for two distinct fresh
`observed_at` values separated by launchd scheduling. For each report require:

- correct schema/mode;
- finite `next_check_at`;
- active issue fingerprint stable when the issue set is stable;
- alert-state reminder time finite and no duplicate immediate notification.

- [ ] **Step 5: Prove known current conditions are represented**

If still live, the paused verified-state job and OpenViking embedding failure
must appear. Public maintenance/admission issues are required only if still
present at observation time. Do not manufacture failures in real services.

- [ ] **Step 6: Prove no collateral lifecycle change**

Gateway and WebUI PID/program/health must match the pre-install receipt. Cron
jobs, admission, public CLI target, and service labels must not have been
mutated.

- [ ] **Step 7: Preserve final receipt**

Record:

- backup directory and hashes;
- script/config/plist hashes;
- focused test results;
- `launchctl print` identity;
- the two report timestamps/next-check values/status/fingerprint;
- notification attempt state;
- before/after gateway and WebUI identity;
- explicit blind spots: sleep, logout, host/launchd/disk failure, notification
  permission/Focus, and no guarantee of human receipt.

## Rollback Procedure

Rollback is limited to the newly installed sentinel:

1. Verify the exact loaded label and canonical plist path.
2. `launchctl bootout` only
   `gui/<uid>/com.seb.hermes-health-sentinel`.
3. Restore script/config from the exact backup and remove only the new plist.
4. Preserve sentinel report/alert files unless the user explicitly requests
   deletion.
5. Verify gateway/WebUI identity and health again.

No Hermes, WebUI, cron, OpenViking, or gateway restart is part of rollback.
