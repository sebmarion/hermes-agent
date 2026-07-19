# Required Policy Runtime Recovery Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task with spec review, then code-quality review, after every implementation task.

**Goal:** Make a long-lived Hermes process notice a newly installed/enabled required policy plugin without a restart, and deterministically stop a tool turn after a host policy-infrastructure block instead of asking the model to retry.

**Architecture:** Add a restricted, monotonic hooks+policy recovery path alongside the existing global `PluginManager`; it stages a standalone directory plugin with no declared tools and atomically publishes one immutable, home-scoped hook/policy snapshot without refreshing the global tool/provider/platform universe. WebUI binds its resolved profile home through the Agent's ContextVar for the whole turn. Separately, carry the concrete `ToolPolicyBlock` from both host producers through a ContextVar collector into the executor, then halt the conversation loop on every code except the explicit recoverable `policy_blocked` decision. Preserve one tool-result row per tool-call ID and bypass response rewriting/model review on the deterministic halt path.

**Tech Stack:** Python 3.11-3.13, pytest through `./scripts/run_tests.sh`, ContextVars, thread-safe collectors, existing Hermes plugin and agent-loop contracts.

**Design:** `docs/superpowers/specs/2026-07-19-required-policy-runtime-recovery-design.md`

**Risk receipts:** GitNexus reports `ToolRegistry.dispatch` CRITICAL (21 direct callers, 29 impacted symbols), `PluginManager._load_plugin` HIGH (not edited by this plan), `authorize_required_tool_policies` MEDIUM, and the executor/finalizer seams LOW. Keep the CRITICAL dispatch change to one typed-observation call before its existing block return and run its direct-call suites.

---

## Task 1: Add trusted required-policy block provenance

**Files:**

- Modify: `hermes_cli/middleware.py`
- Modify: `tools/registry.py`
- Test: `tests/run_agent/test_required_policy_dispatch.py`
- Test: `tests/run_agent/test_required_policy_bypass_inventory.py`

### Step 1: Write failing collector tests

Add tests proving:

- a collector bound around `authorize_and_dispatch_tool()` receives the exact frozen `ToolPolicyBlock` and tool-call ID before the JSON result is returned;
- a one-use registry binding failure records the concrete block too;
- without a bound collector, both paths retain their current result and dispatch behavior;
- plain tool text shaped exactly like a `required_policy_block` does not create a collector event;
- terminal classification is exactly `policy_code != PolicyDecisionCode.BLOCKED`, so unknown future codes fail closed.

Run:

```bash
./scripts/run_tests.sh tests/run_agent/test_required_policy_dispatch.py tests/run_agent/test_required_policy_bypass_inventory.py -q
```

Expected: FAIL because the collector API and typed observations do not exist.

### Step 2: Implement the typed collector in middleware

In `hermes_cli/middleware.py`:

- add an immutable record containing `tool_call_id` and `ToolPolicyBlock`;
- add a thread-safe batch collector that keeps the first record per call ID and can choose the first terminal record from an explicit original-order ID list;
- bind it with a ContextVar context manager so copied worker contexts share the same collector object;
- expose narrow helpers to record and retrieve a block for one call ID;
- record the outer authorization block in `_emit_required_policy_block()` before any observer hook;
- keep `is_required_policy_block_result()` for compatibility, but do not use it as a new control signal.

### Step 3: Record registry binding failures

In `ToolRegistry.dispatch()`, immediately before the existing JSON return for a non-null `registry_dispatch_policy_block()`, call the middleware recorder with the current `tool_call_id` and concrete block. Do not alter handler selection, one-use authorization, exception handling, or serialized output.

### Step 4: Run focused tests

Run the Step 1 command. Expected: PASS.

### Step 5: Commit

Run `detect_changes()` for the staged diff, verify only middleware/registry policy flows changed, then commit:

```bash
git add hermes_cli/middleware.py tools/registry.py tests/run_agent/test_required_policy_dispatch.py tests/run_agent/test_required_policy_bypass_inventory.py
git commit -m "fix: preserve required policy block provenance"
```

---

## Task 2: Halt policy-infrastructure failures deterministically

**Files:**

- Modify: `agent/agent_init.py`
- Modify: `agent/turn_context.py`
- Modify: `agent/tool_executor.py`
- Modify: `run_agent.py`
- Modify: `agent/conversation_loop.py`
- Modify: `agent/turn_finalizer.py`
- Test: `tests/run_agent/test_run_agent.py`
- Test: `tests/run_agent/test_tool_call_guardrail_runtime.py`
- Create: `tests/run_agent/test_required_policy_halt_runtime.py`

### Step 1: Write failing end-to-end runtime tests

Create focused agent-loop tests proving:

- sequential infrastructure block: one model API call, blocked handler never runs, `failed=true`, `completed=false`, `turn_exit_reason=required_policy_halt`, fixed streamed response, and structured metadata;
- sequential multi-call batch: the first terminal block stops execution and deterministic skipped results close every remaining tool-call ID;
- concurrent batch: all submitted calls receive results and the chosen terminal block follows original tool-call order even when workers finish out of order;
- explicit `policy_blocked` remains recoverable and permits the next model round;
- an ordinary handler returning spoofed block JSON does not halt;
- the file-mutation footer formatter, output transforms, `post_llm_call`, completion explanation, `_sync_external_memory_for_turn()`, external-memory prefetch, and background review are not called;
- the fixed host-authored response remains byte-identical from construction through persistence and streaming;
- existing tool-guardrail halt behavior remains unchanged.

Run:

```bash
./scripts/run_tests.sh tests/run_agent/test_required_policy_halt_runtime.py tests/run_agent/test_tool_call_guardrail_runtime.py -q
```

Expected: FAIL because agent halt state and deterministic handling do not exist.

### Step 2: Add/reset agent halt state

- Initialize `_required_policy_halt_block` in `agent/agent_init.py`.
- Reset it in `agent/turn_context.py` with other per-turn guardrail state.
- Add narrow `AIAgent` helpers in `run_agent.py` to record only the first terminal block and build the fixed safe response.
- Bind one Task 1 collector around each `_execute_tool_calls()` batch and reduce its events using assistant tool-call order after execution.

### Step 3: Replace JSON-derived executor control

In both paths in `agent/tool_executor.py`:

- retrieve the typed event for the current `tool_call_id` instead of parsing result text;
- continue suppressing ordinary progress/completion callbacks for genuine policy blocks;
- keep explicit `policy_blocked` transcript behavior recoverable;
- in sequential mode, after appending the terminal block result, append one deterministic skipped result for every unstarted sibling and break;
- in concurrent mode, retain original-order result append and let the batch collector choose the halt record after all submitted calls settle.

### Step 4: Break the conversation loop before another API call

Immediately after tool execution and before the existing guardrail check:

- when `_required_policy_halt_block` is set, mark `failed`, set `required_policy_halt`, append and stream the host-authored response, emit a safe status, and break;
- do not call the model again or suggest repair commands.

### Step 5: Preserve deterministic finalization

In `agent/turn_finalizer.py`:

- expose `required_policy` metadata from the frozen block;
- skip the file-mutation footer, completion explanation, `transform_llm_output`, `post_llm_call`, external-memory sync/prefetch, and background review for `required_policy_halt`;
- retain persistence, cleanup, session-end hooks, and non-model telemetry.

### Step 6: Run focused tests

Run:

```bash
./scripts/run_tests.sh tests/run_agent/test_required_policy_halt_runtime.py tests/run_agent/test_tool_call_guardrail_runtime.py tests/run_agent/test_run_agent.py -k 'required_policy or guardrail_halt' -q
```

Expected: PASS.

### Step 7: Commit

Run `detect_changes()` for the staged diff, verify only executor/conversation/finalizer flows changed, then commit:

```bash
git add agent/agent_init.py agent/turn_context.py agent/tool_executor.py run_agent.py agent/conversation_loop.py agent/turn_finalizer.py tests/run_agent/test_required_policy_halt_runtime.py tests/run_agent/test_tool_call_guardrail_runtime.py tests/run_agent/test_run_agent.py
git commit -m "fix: halt required policy infrastructure failures"
```

---

## Task 3: Recover a late hooks+policy standalone directory plugin safely

**Files:**

- Modify: `hermes_cli/plugins.py`
- Modify: `hermes_cli/tool_policy.py`
- Modify: `agent/turn_context.py`
- Modify in clean WebUI worktree: `api/streaming.py`
- Test: `tests/hermes_cli/test_required_tool_policy.py`
- Test: `tests/hermes_cli/test_plugins.py`
- Test in clean WebUI worktree: `tests/test_issue5567_profile_home_override.py`

### Step 1: Write failing same-PID recovery tests

Add tests proving:

- a manager first discovers an empty isolated `HERMES_HOME`; a plugin directory and enabled/required config are then created; reconciliation in the same Python PID publishes it and authorization calls its policy;
- a manifest discovered as not enabled can be enabled later and recovered once;
- simultaneous reconciliation invokes `register()` once and publishes one immutable home-scoped hook/policy generation;
- the resolver sees a new `plugin.yaml` inside an already-existing user plugin root and respects project-over-user precedence only when project plugins are enabled;
- safe mode, current disablement, undeclared policies, context drift, load failure, non-standalone kinds, declared tools, and non-directory entry points stay fail-closed before unsafe registration/import surfaces are reached;
- a candidate that accesses any registration API other than `register_hook`/`register_policy` publishes no hooks/policies/plugins and leaves `tools.registry.registry._generation` unchanged;
- a reader paused across publication observes a complete old or complete new generation, never mixed hooks/policies;
- a snapshot published for home A is invisible under home B, and a manager/discovery-home mismatch refuses recovery;
- an ordinary manager discovered under home A is never consulted while home B is active, even when both homes configure the same plugin key; authorization returns `required_policy_plugin_load_error` and A's callback is not invoked;
- direct `_plugin_manager` replacement and ordinary `discover_and_load(force=True)` behavior are unchanged;

Run:

```bash
./scripts/run_tests.sh tests/hermes_cli/test_required_tool_policy.py tests/hermes_cli/test_plugins.py -k 'required_policy_recovery or late_required_policy or force_rediscover' -q
```

Expected: FAIL because restricted reconciliation does not exist.

### Step 1b: Write and run failing WebUI home-binding tests

Before editing `api/streaming.py`, extend the clean WebUI worktree's
`tests/test_issue5567_profile_home_override.py` to prove:

- `_profile_home` is bound through the real `hermes_constants` ContextVar before
  Agent construction and remains bound through `run_conversation()`;
- a concurrent turn's process-global `HERMES_HOME` clobber cannot redirect the
  active turn's config reads;
- success, construction failure, and conversation failure all reset the exact
  ContextVar token without disturbing an outer override.

Run the WebUI wrapper against the Agent worktree:

```bash
env PYTHONPATH=/private/tmp/hermes-required-policy-runtime-recovery \
  ./scripts/test.sh tests/test_issue5567_profile_home_override.py -q
```

Expected: the existing three regressions pass and the new streaming tests FAIL
because `_run_agent_streaming()` does not bind the override yet.

Before editing the streaming symbol, run and report the required impact receipt:

```bash
node /Users/seb/hermes-webui/.gitnexus/run.cjs impact \
  'Function:api/streaming.py:_run_agent_streaming' \
  --direction upstream --repo /Users/seb/hermes-webui
```

If the current index again reports `UNKNOWN`/target-not-found for this oversized
worker, retain that receipt, identify its direct launch sites with a narrow source
search, and keep the change to one ContextVar set/reset envelope. Warn before
editing if a refreshed exact-symbol result is HIGH or CRITICAL.

### Step 2: Implement capture and candidate resolution

In `hermes_cli/plugins.py`:

- record the resolved discovery home only after a successful initial/forced manager sweep;
- add a process-wide recovery `RLock` and immutable capture of resolved home, manager discovery home, project root/enablement, safe mode, and normalized required mapping;
- select only absent or never-loaded standalone directory candidates with no declared tools from an existing disabled manifest or a fresh user/project directory scan;
- re-read enabled/disabled config, require every requested policy declaration, and reject already-loaded replacement/unload and entry-point recovery;
- reject manager/home mismatches and revalidate the full capture before publication.

### Step 3: Stage with a restricted context and publish monotonically

- create a sealed recovery-only context that exposes only `register_hook` and `register_policy`; every other `register_*` attribute fails closed;
- execute the plugin in a private staging module without installing its canonical `hermes_plugins.*` name, validate all required registrations, and discard all staged state on failure;
- build one frozen required-policy runtime snapshot containing immutable home-scoped plugin, hook, and policy tuples, then publish it with one reference swap under a short lock;
- make module-level hook accessors and policy resolution capture the same snapshot and select only entries matching the current home override;
- capture ordinary and recovered callback ownership atomically with forced discovery, preserve sorted first-block semantics, and copy the bound home ContextVar into policy executor workers;
- retain UUID-scoped private modules when explicit force discovery supersedes recovered ownership so already-captured callbacks can finish lazy relative imports;
- consult ordinary manager policy/plugin maps only when its recorded discovery home is known and equals the captured active home; otherwise return the stable `required_policy_plugin_load_error` block without touching those maps;
- never call or modify `PluginManager._load_plugin()`, never call `discover_and_load(force=True)`, and never mutate the tool/provider/platform registries;
- expose a best-effort `recover_required_policy_plugins()` for the turn boundary and invoke the same narrow recovery from `authorize_required_tool_policies()` before returning missing/disabled.

### Step 4: Bind the resolved WebUI home and reconcile before plugin hooks

- In the clean WebUI worktree, bind `_profile_home` with `hermes_constants.set_hermes_home_override()` before Agent construction/turn execution and reset its token in the outer `finally`, with a compatibility no-op for older Agents.
- In `agent/turn_context.py`, call `recover_required_policy_plugins()` before first-turn lifecycle and `pre_llm_call` hooks. It must resolve the home from that ContextVar, not process-global env.
- Log a safe debug/warning on recovery failure and allow dispatch-time enforcement to produce the user-visible block only if a tool is attempted.
- Implement against the already-failing ordering, env-clobber, concurrent-turn,
  and reset tests from Step 1b; do not add the acceptance assertions after the
  production edit.

### Step 5: Run focused tests

Run the Step 1 command, then:

```bash
./scripts/run_tests.sh tests/hermes_cli/test_required_tool_policy.py tests/hermes_cli/test_plugins.py tests/run_agent/test_required_policy_halt_runtime.py -q
```

In the clean WebUI worktree, run:

```bash
env PYTHONPATH=/private/tmp/hermes-required-policy-runtime-recovery \
  ./scripts/test.sh tests/test_issue5567_profile_home_override.py -q
```

Expected: both commands PASS.

### Step 6: Commit

Run `detect_changes()` in both worktrees, verify the recovery does not affect global tool-schema flows, then commit each logical patch:

```bash
git add hermes_cli/plugins.py hermes_cli/tool_policy.py agent/turn_context.py tests/hermes_cli/test_required_tool_policy.py tests/hermes_cli/test_plugins.py
git commit -m "fix: recover late required policy plugins"
```

In the clean WebUI worktree:

```bash
git add api/streaming.py tests/test_issue5567_profile_home_override.py
git commit -m "fix: bind profile home across agent turns"
```

---

## Task 4: Document and verify the complete invariant

**Files:**

- Modify: `docs/middleware/README.md`
- Modify tests only if verification exposes a missing boundary.

### Step 1: Document behavior and limits

Add concise required-policy runtime documentation covering:

- monotonic same-home directory recovery;
- hooks+policy-only eligibility and restart-required cases;
- serialized transcript result versus typed host control provenance;
- only `policy_blocked` is recoverable;
- deterministic halt metadata/exit reason and multi-call transcript closure;
- explicit non-goal of full cross-profile plugin isolation.

### Step 2: Run regression suites

Run:

```bash
./scripts/run_tests.sh tests/hermes_cli/test_required_tool_policy.py tests/hermes_cli/test_plugins.py tests/run_agent/test_required_policy_dispatch.py tests/run_agent/test_required_policy_bypass_inventory.py tests/run_agent/test_required_policy_halt_runtime.py tests/run_agent/test_tool_call_guardrail_runtime.py -q
```

Then run the broader touched-area suites:

```bash
./scripts/run_tests.sh tests/hermes_cli tests/run_agent -q
```

In the clean WebUI worktree also run:

```bash
./scripts/test.sh tests/test_issue5567_profile_home_override.py tests/test_issue1968_mcp_profile_discovery.py
```

Expected: PASS with no unsupported-Python fallback and no bare-pytest invocation.

### Step 3: Same-PID isolated-home acceptance

Run a single Python process under isolated `HERMES_HOME` and `HERMES_WEBUI_STATE_DIR` that:

1. creates a manager and discovers before the plugin exists;
2. creates/enables a harmless hooks+`tool_dispatch` policy plugin and required config;
3. reconciles without replacing the manager or PID;
4. dispatches a harmless handler once and proves the policy callback ran;
5. prints no secrets and deletes no real state.

Expected receipt: one PID, one manager identity, policy callback count `1`, handler count `1`, no block.

### Step 4: Final graph review and commit

Run `detect_changes({scope: compare, base_ref: main})`, inspect all affected symbols/flows, then commit:

```bash
git add docs/middleware/README.md
git commit -m "docs: explain required policy runtime recovery"
```

### Step 5: Installed-runtime deployment and live proof

- Compare hashes/diffs for every touched installed-Agent file against the clean worktree base; preserve unrelated installed checkout changes.
- Apply only the reviewed Agent patch to `/Users/seb/.hermes/hermes-agent` and the reviewed home-binding hunk to the dirty `/Users/seb/hermes-webui` checkout after verifying it does not overwrite its unrelated local edits; do not touch Hermes One.
- Restart WebUI through its repository lifecycle script, wait for `/health`, and confirm the new PID is serving.
- Execute one harmless real tool call under the configured `gitnexus-governor` and verify the handler ran once with no policy block.
- Read back logs/API evidence for the restart, policy registration, call result, and absence of a retry loop.
- Do not archive the original session unless separately requested.

### Step 6: Final review

Dispatch an independent whole-change spec and code review. Resolve every issue, rerun the affected focused tests, and only then report completion.
