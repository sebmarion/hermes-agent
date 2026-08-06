# Bounded Delegation Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep delegated Ornith/IQ2_M child prompts below a 32K total context window, especially when `/bestplan` produces a large handoff, while preserving the complete source context and retrying once with a compact pointer-based brief.

**Architecture:** Add a pure, testable handoff-budget helper in `tools/delegate_tool.py`. It estimates the child prompt from goal, context, and fixed prompt overhead; reserves output/safety tokens; and spills oversized context to the delegation cache while replacing it with a concise head/tail plus a `read_file` pointer. Apply this before child construction, and retry a failed child once with the compacted handoff rather than creating an unrelated new session.

**Tech Stack:** Python, pytest, existing Hermes delegation cache and `read_file` tool.

---

### Task 1: Add failing handoff-budget tests

**Files:**
- Create: `tests/tools/test_delegate_handoff_budget.py`

- [x] **Step 1: Write tests for unknown, fitting, and oversized 32K handoffs.**
- [x] **Step 2: Run the focused tests and verify they fail because the helper does not exist.**

### Task 2: Implement bounded child handoff

**Files:**
- Modify: `tools/delegate_tool.py` near delegation constants and child construction.

- [x] **Step 1: Add conservative token/character budgeting constants and a pure budget calculation.**
- [x] **Step 2: Add lossless spill plus deterministic head/tail pointer formatting.**
- [x] **Step 3: Apply the helper to every task before `_build_child_agent`; preserve original task data for diagnostics.**
- [x] **Step 4: Run the focused tests and verify they pass.**

### Task 3: Preserve existing terminal recovery for residual context errors

**Files:**
- Modify: `tools/delegate_tool.py` in child execution aggregation.
- Modify: `tests/tools/test_delegate_handoff_budget.py`

- [x] **Step 1: Use pre-dispatch budgeting so the known oversized-handoff failure is prevented before the child session exists.**
- [x] **Step 2: Keep residual provider/compression errors on Hermes' existing explicit recovery path; do not create an unrelated fresh session.**
- [x] **Step 3: Run delegation regression tests.**

### Task 4: Verify scope and runtime safety

- [x] **Step 1: Run the relevant Hermes test files through the repository-supported test command.**
- [x] **Step 2: Run `git diff --check` and GitNexus `detect_changes()`; document the GitNexus index-version limitation if it persists.**
- [x] **Step 3: Inspect the final diff and report any unrelated pre-existing changes left untouched.**
