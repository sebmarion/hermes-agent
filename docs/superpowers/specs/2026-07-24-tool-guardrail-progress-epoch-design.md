# Tool Guardrail Verified-Progress Epoch Design

## Problem

Hermes' hard-stop tool-loop guardrails count repeated failures for an entire
turn. A successful file edit only clears the failure counter for that edit
tool's own signature and tool name. It does not clear an earlier `terminal`
failure streak.

That makes the guardrail progress-blind. A turn can fail a command, inspect the
cause, successfully patch the code, and then be blocked when it reruns the
original command even though the repository state has materially changed.

## Goal

Keep the existing fail-closed hard stops for true repeated failures while
starting a new transient-failure epoch after Hermes has proof that a file
mutation changed content.

## Non-goals

- Do not raise or disable any warning or hard-stop threshold.
- Do not reset typed auth, capability, permission, permanent, or
  schema-correctable retry policy.
- Do not reset repeated read-only/no-progress tracking.
- Do not infer progress from model prose, a successful shell exit, or an
  unverified write result.
- Do not restart Hermes WebUI or disturb active WebUI threads during delivery.

## Selected approach

Add a narrow result-classification helper for mutation evidence. In the first
version, only a successful `patch` result with a non-empty textual `diff`
proves material progress:

```python
tool_name == "patch"
and result["success"] is True
and isinstance(result["diff"], str)
and bool(result["diff"].strip())
and not result.get("error")
```

When `ToolCallGuardrailController.after_call()` observes that evidence on a
successful call, it advances the transient failure epoch by clearing:

- `_exact_failure_counts`
- `_same_tool_failure_counts`

It deliberately preserves:

- `_typed_permanent_by_tool`
- `_typed_permanent_by_signature`
- `_schema_retry_policies`
- `_no_progress`
- `_halt_decision`
- all configured thresholds

The existing per-signature and per-tool success cleanup remains in place.

## Why `write_file` does not prove progress yet

The current `write_file` result exposes fields such as `bytes_written` and
`files_modified`, but those fields do not prove that pre- and post-write
content differed. Treating them as mutation evidence would let repeated no-op
writes defeat the hard stop. `write_file` can join the progress contract later
if it reports an explicit `changed` flag backed by pre/post hashes.

## Considered alternatives

### Raise or disable hard-stop thresholds

Rejected. It delays true runaway detection and still fails to distinguish
repetition from recovery.

### Clear every guardrail state bucket after any successful mutating tool

Rejected. A successful unrelated call must not erase typed permanent failures,
schema retry limits, or read-only no-progress evidence.

### Track a numeric epoch on every failure record

This can encode the same semantics, but it adds state and migration complexity
without improving the first implementation. Clearing the two transient maps is
equivalent because the controller is already scoped to one turn.

## Safety invariants

1. Two identical failures still block at the configured threshold when no
   verified content-changing patch occurs.
2. An empty or whitespace-only patch diff does not reset a failure streak.
3. A `write_file` result containing only current success metadata does not
   reset a failure streak.
4. Typed permanent and schema-correctable failures survive verified progress.
5. Repeated identical read-only results remain blocked independently.
6. A verified patch resets both exact-signature and varying-argument
   same-tool failure streaks.

## Verification and delivery

- Add unit tests for the mutation-evidence classifier.
- Add controller regressions for verified reset and no-op/non-proof cases.
- Add a runtime regression through Hermes' sequential tool-dispatch path.
- Run the focused guardrail/runtime gate through `scripts/run_tests.sh`, then
  the broader affected Agent suite.
- Run GitNexus change detection against `main`.
- Obtain an adversarial read-only review from local Ornith through Hermes.
- Build a new immutable Agent source/release and activate only the gateway
  target required for Agent code. Do not restart the WebUI process.
- Prove the live health endpoint, immutable source identity, persisted
  provider/model receipt, and a real fail-patch-retry Hermes tool loop.
