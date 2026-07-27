# BestPlan Synthesis Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** Allow BestPlan to recover once from a non-empty malformed synthesis response by asking the same synthesis lane for a bounded, no-tools V1 envelope repair, while preserving every existing fail-closed validation and baseline gate.

**Architecture:** `run_bestplan` continues to own explorer quorum, ordinary synthesis, validation, and teardown. It records only the last non-empty invalid ordinary synthesis result and its exact lane/runtime. If at least one second remains, a new no-tools child receives bounded untrusted inputs and gets at most 45 seconds for one representation-only repair; its response passes through `_validated_plan_envelope` and the existing downstream capture path unchanged.

**Tech Stack:** Python, existing `AIAgent`, `DaemonThreadPoolExecutor`, pytest through `scripts/run_tests.sh`

---

### Task 1: Lock the repair contract with failing tests

**Files:**
- Modify: `tests/agent/test_bestplan_orchestrator.py`

**Step 1: Add a successful-repair regression test**

Create a fake-agent test in which:

- both explorers return valid candidate packets;
- the ordinary SOL synthesizer returns a non-empty invalid response;
- the ordinary GLM fallback synthesizer returns a different non-empty invalid response;
- the single repair call returns `_synth_plan_envelope()`.

Record constructor arguments and prompts. Assert the result completes, the repair child uses the GLM lane/runtime that produced the last invalid output, `enabled_toolsets == []`, and the repair prompt contains the exact workspace, authoritative request, validated candidates, last GLM invalid output, and validation error. Assert it excludes the superseded SOL invalid output and instructs the model not to inspect files or use tools.

Override `_runtime_config(overall_timeout=2.0)` in this test so the repair is
eligible under the one-second minimum-remaining contract.

**Step 2: Add an invalid-repair fail-closed regression test**

Create a second fake-agent test in which the one repair call also returns invalid output. Assert the result remains failed with a synthesis/envelope error and exactly one repair child was constructed; no accepted plan or receipt is returned.

Override `_runtime_config(overall_timeout=2.0)` in this test as well.

**Step 3: Run the two focused tests and observe failure**

Run:

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py -k "repairs_last_nonempty_invalid_synthesis_without_tools or invalid_synthesis_repair_fails_closed_after_one_attempt"
```

Expected: both tests fail because current production code never creates the repair child.

### Task 2: Implement bounded no-tools synthesis repair

**Files:**
- Modify: `agent/bestplan_orchestrator.py`

**Step 1: Add bounded repair constants and prompt construction**

Add constants for:

- 45-second repair maximum;
- one-second minimum remaining deadline;
- bounded current request, candidate packet, and invalid output lengths.

Add a helper that builds the representation-only repair prompt from bounded JSON-encoded untrusted data. Require exactly one executable V1 envelope and explicitly prohibit tools, file/web inspection, scope expansion, invented authority, and prose outside the envelope.

Use the existing generic host validation error,
`BestPlan synthesizer returned no valid executable V1 envelope`, as the repair
prompt's validation error. Do not broaden `_validated_plan_envelope` or expose
parser internals in this change.

**Step 2: Add a no-tools repair child constructor**

Add a dedicated helper that constructs `AIAgent` from the selected ordinary synthesis lane/runtime with `enabled_toolsets=[]`, no persistence, no memory, no compression, quiet output, and a small bounded iteration count. Do not change `_build_child_agent`, because explorer and ordinary synthesizer behavior must remain unchanged.

Before editing any existing helper or `run_bestplan`, run GitNexus impact analysis for that symbol and report any HIGH or CRITICAL blast radius before proceeding.

**Step 3: Record only the last eligible invalid ordinary result**

In `run_bestplan`, retain the exact lane/runtime, bounded response body, and validation error only when an ordinary synthesizer returns non-empty output that fails `_validated_plan_envelope`. Empty output, construction errors, and timeouts must not create repair eligibility.

**Step 4: Run one bounded repair attempt**

After ordinary lanes fail validation:

- require an eligible last invalid result;
- require at least one second before the existing overall deadline;
- use an effective deadline of `min(45 seconds, remaining overall deadline)`;
- create exactly one no-tools child from the recorded lane/runtime;
- use the existing daemon pool and hard-bounded teardown pattern;
- validate the returned body through `_validated_plan_envelope`;
- continue through the existing receipt, baseline, persistence, and execution path only on validation success;
- otherwise preserve the existing fail-closed result.

Do not merge invalid outputs, retry repair, bypass validation, or alter baseline semantics.

**Step 5: Run the focused repair tests**

Run:

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py -k "repairs_last_nonempty_invalid_synthesis_without_tools or invalid_synthesis_repair_fails_closed_after_one_attempt"
```

Expected: both tests pass.

**Step 6: Run the complete orchestrator test file**

Run:

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py
```

Expected: all tests pass.

### Task 3: Verify adjacent contracts and changed scope

**Files:**
- Verify: `agent/bestplan_orchestrator.py`
- Verify: `tests/agent/test_bestplan_orchestrator.py`

**Step 1: Run adjacent BestPlan, conversation, and envelope suites**

Run the existing focused collection covering the BestPlan orchestrator, conversation ingress/context, capture/compiler, envelope leak, and baseline guards.

Expected: all tests pass.

**Step 2: Run formatting and whitespace checks**

Run:

```bash
git diff --check
```

Expected: no output.

**Step 3: Run the full Hermes Agent suite**

Run:

```bash
scripts/run_tests.sh
```

Expected: no new failures relative to the recorded platform/baseline failures.

**Step 4: Inspect the graph delta**

Run GitNexus `detect_changes` against the repository default branch and confirm only the intended BestPlan orchestration/test symbols and flows changed. Warn before proceeding if the graph reports unexpected HIGH or CRITICAL impact.

### Task 4: Review and commit the implementation

**Files:**
- Modify: `agent/bestplan_orchestrator.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`

**Step 1: Request independent spec and code-quality review**

Use `superpowers:requesting-code-review` with the reviewed design and this plan. Resolve only evidence-backed blocking issues, then rerun affected tests.

**Step 2: Commit the verified change**

Stage only the orchestrator, its tests, and this plan document. Commit with:

```bash
git commit -m "fix(bestplan): repair malformed synthesis envelope once"
```

### Task 5: Release and prove r61

**Files:**
- Release inputs only; do not change product source

**Step 1: Build immutable r61 inputs**

Create an Agent snapshot from the verified r61 commit and pair it with the already released WebUI source commit. Record both commit/tree identities and content manifests.

**Step 2: Run the managed release transaction**

Stage and release `hermes-candidate-20260727-r61` through the existing managed transaction engine with r60 as the expected current pair. Preserve watchdog and rollback behavior and record the completed journal, selector generation, live PIDs, and exact Agent/WebUI/runtime manifests.

**Step 3: Run live WebUI acceptance**

In a fresh signed-in WebUI conversation bound to a clean reproducible Git worktree:

1. seed bounded conversation context for a concrete repository task;
2. invoke shorthand BestPlan;
3. verify explorer quorum and synthesis complete;
4. verify the host accepts and displays a valid executable plan without timeout, quorum, provider-empty, or malformed-envelope errors.

Keep the proof gate unchanged: no synthetic proof-v1 evidence and no bypass of the 1000-sample/seven-day/zero-diff requirement.
