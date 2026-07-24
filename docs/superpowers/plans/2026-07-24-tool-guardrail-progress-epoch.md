# Hermes Tool Guardrail Verified-Progress Epoch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:test-driven-development while implementing each behavior change
> and superpowers:verification-before-completion before claiming success.

**Goal:** Prevent Hermes from progress-blindly blocking a previously failing
tool call after a verified content-changing patch, without weakening any true
runaway, typed-error, schema-retry, or read-only no-progress guardrail.

**Architecture:** Add a conservative mutation-evidence classifier beside the
existing result classifiers. A successful non-empty `patch.diff` advances the
controller's transient failure epoch by clearing exact-signature and same-tool
failure counters only. Existing hard-stop thresholds and all durable
per-turn policy buckets remain unchanged.

**Tech Stack:** Python 3.11-3.13, pytest through `scripts/run_tests.sh`,
GitNexus, Hermes CLI, local Ornith.

---

## Task 1: Lock the result-evidence contract with failing tests

**Files:**

- Modify: `tests/agent/test_tool_result_classification.py`

- [ ] Add tests proving that a successful patch with a non-empty diff is
  verified progress.
- [ ] Add tests proving that empty/whitespace diffs, top-level errors, malformed
  payloads, and current `write_file` metadata are not verified progress.
- [ ] Run:

  ```bash
  scripts/run_tests.sh tests/agent/test_tool_result_classification.py -q
  ```

  Expected: FAIL because the new classifier does not exist.

## Task 2: Lock the controller and runtime regressions with failing tests

**Files:**

- Modify: `tests/agent/test_tool_guardrails.py`
- Modify: `tests/run_agent/test_tool_call_guardrail_runtime.py`

- [ ] Add an exact-signature regression: seed failures to the threshold, record
  a verified patch, and prove the original call is allowed again.
- [ ] Add a same-tool varying-arguments regression with the same verified reset.
- [ ] Prove an empty patch diff and current `write_file` metadata do not reset
  either failure streak.
- [ ] Prove typed/schema and read-only no-progress state survive a verified
  patch.
- [ ] Add a runtime test through `_execute_tool_calls_sequential` proving the
  post-patch retry reaches `handle_function_call`.
- [ ] Run:

  ```bash
  scripts/run_tests.sh \
    tests/agent/test_tool_result_classification.py \
    tests/agent/test_tool_guardrails.py \
    tests/run_agent/test_tool_call_guardrail_runtime.py -q
  ```

  Expected: FAIL only at the newly specified progress-epoch behaviors.

## Task 3: Implement the minimal progress epoch

**Files:**

- Modify: `agent/tool_result_classification.py`
- Modify: `agent/tool_guardrails.py`

- [ ] Add `file_mutation_result_proves_change(tool_name, result)` with the
  conservative non-empty `patch.diff` contract.
- [ ] In the successful-call path of
  `ToolCallGuardrailController.after_call()`, clear only
  `_exact_failure_counts` and `_same_tool_failure_counts` when the helper
  returns true.
- [ ] Keep typed permanent, schema retry, no-progress, halt, and configuration
  state untouched.
- [ ] Rerun the focused command from Task 2 and require PASS.

## Task 4: Verify scope and regressions

- [ ] Run the complete focused guardrail gate:

  ```bash
  scripts/run_tests.sh \
    tests/agent/test_tool_result_classification.py \
    tests/agent/test_tool_guardrails.py \
    tests/run_agent/test_tool_call_guardrail_runtime.py -q
  ```

- [ ] Run the broader affected Agent test selection discovered from direct
  imports/callers.
- [ ] Run compile/static checks required by repository guidance.
- [ ] Run GitNexus
  `detect_changes(scope="compare", base_ref="main")` and inspect every affected
  symbol and flow.
- [ ] Request an adversarial read-only review from local Ornith through the
  canonical Hermes CLI and resolve all real findings.

## Task 5: Commit and build an immutable Agent release

- [ ] Confirm the diff contains only the design, implementation, focused tests,
  and analyzer-generated GitNexus metadata.
- [ ] Commit with an intentional bug-fix message.
- [ ] Build the immutable Agent source snapshot from the committed tree.
- [ ] Build/verify the paired immutable runtime/release manifest using the
  existing WebUI cutover tooling and explicit test receipts.
- [ ] Inspect the activation plan and assert that WebUI restart/termination is
  not part of the transition.

## Task 6: Activate without restarting WebUI and run live acceptance

- [ ] Drain only the Agent gateway's in-flight work if required by the managed
  cutover contract; do not stop or restart the WebUI process.
- [ ] Activate the committed immutable Agent source and verify selector,
  manifest, interpreter, listener, and health identities.
- [ ] Run a real Hermes turn via local Ornith that deliberately performs:
  failing command -> successful non-empty patch -> same command retry.
- [ ] Require the retry to execute, the turn to finish, and no
  `repeated_exact_failure_block` to appear.
- [ ] Run a control turn with repeated identical failures and no patch; require
  the configured hard stop to remain active.
- [ ] Capture live health, release identity, source commit/tree, and
  provider/model receipts.
