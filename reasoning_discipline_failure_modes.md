# REASONING_DISCIPLINE_GUIDANCE Failure Mode Analysis

## Failure Mode 1: Gateway Config Propagation Gap

**Trigger:** The `reasoning_discipline_guidance` config key is added to `hermes_cli/config.py` DEFAULT_CONFIG but never read by `agent_init.py` into an agent instance attribute like `_task_completion_guidance` and `_parallel_tool_call_guidance`.

**Blast Radius:**
- CLI sessions work correctly (load_config() reads the key)
- Gateway sessions **silently ignore** the config — the gateway bypasses load_config() and reads raw YAML directly (gateway/run.py:2239 comment explicitly states this)
- The guidance block in system_prompt.py checks `getattr(agent, "_reasoning_discipline_guidance", True)` which returns the default True, so the block is **always injected** regardless of user config intent
- Users cannot disable the guidance via config.yaml in gateway mode

**Evidence:**
- No `_reasoning_discipline_guidance` assignment exists in `agent/agent_init.py` (lines 1308-1337 show where `_task_completion_guidance` and `_parallel_tool_call_guidance` are read from `_agent_cfg.get("agent", {})`)
- gateway/run.py `_load_gateway_config()` at line 2220-2248 reads raw YAML and applies overlays, but does not normalize or validate against DEFAULT_CONFIG schema
- No test in tests/agent/test_system_prompt.py validates config gating behavior

---

## Failure Mode 2: Prompt Cache Invalidation Without Agent Cache Invalidation

**Trigger:** User adds `reasoning_discipline_guidance: false` to config.yaml during an active gateway session.

**Blast Radius:**
- The cached AIAgent instance is reused because `_agent_config_signature()` (gateway/run.py:14940-15005) does NOT include the new config key in its cache-busting hash
- The signature includes: model, api_key_fingerprint, base_url, provider, api_mode, enabled_toolsets, ephemeral_prompt, cache_keys (from _extract_cache_busting_config), user_id
- `_extract_cache_busting_config()` (gateway/run.py:14847-14937) extracts specific keys: `model.context_length`, `model.context_window`, `model.max_tokens`, `model.thinking_budget`, `model.reasoning_effort`, `compression.*` keys
- **agent.* keys are NOT in the cache-busting list** — only model.* and compression.* are
- Result: The agent instance is reused with a STALE system prompt that still contains the reasoning guidance block, even though config.yaml changed

**Evidence:**
- gateway/run.py:14956-14960 explicitly documents that cache_keys comes from `_extract_cache_busting_config(user_config)`
- Line 14995-14996 comment: "reasoning_config excluded — it's set per-message on the cached agent and doesn't affect system prompt or tools" — this is the WRONG assumption; reasoning_discipline_guidance DOES affect the system prompt
- Line 14860-14870 shows cache_keys only captures model.* fields, not agent.* guidance toggles

---

## Failure Mode 3: Token Dilution + Conflict with TASK_COMPLETION_GUIDANCE

**Trigger:** Model receives both TASK_COMPLETION_GUIDANCE ("keep working until done") and REASONING_DISCIPLINE_GUIDANCE ("stop reasoning after 2 iterations and gather data") in the same cached system prompt.

**Blast Radius:**
- ~80 tokens of reasoning guidance competes for attention with the existing ~80 tokens of task completion guidance + ~70 tokens parallel tool guidance + ~200 tokens of other guidance blocks
- **Contradiction:** TASK_COMPLETION_GUIDANCE says "keep working until you have actually exercised the code" while REASONING_DISCIPLINE_GUIDANCE says "stop reasoning and gather empirical data" after 2 iterations
- Models prone to tool over-reliance (explicitly called out in OPENAI_MODEL_EXECUTION_GUIDANCE for "skip prerequisite lookups") may interpret "gather data" as "call more tools" and spiral into tool-churn
- The guidance is metacognitive — it requires the model to monitor its own reasoning state ("2+ iterations of the same argument shape") — which is itself a reasoning task that adds cognitive overhead
- **Model-specific harm:** Models that already under-reason (rush to tools) get worse; models that over-reason may find this helpful, but the 80-token cost is paid by ALL models

**Evidence:**
- prompt_builder.py:316-329 TASK_COMPLETION_GUIDANCE (80 tokens)
- prompt_builder.py:331-370 PARALLEL_TOOL_CALL_GUIDANCE (70 tokens)
- prompt_builder.py:380-438 OPENAI_MODEL_EXECUTION_GUIDANCE (200+ tokens) with explicit "<prerequisite_checks>" section that already tells models to gather context
- Line 313-315 comment: "Short on purpose. This block is shipped to every user, every session, in the cached system prompt — token cost is paid once at install and then amortised across all sessions via prefix caching. Keep it tight." — the proposed 80-token block violates this principle without evidence of effectiveness

---

## Failure Mode 4: Static Guidance Cannot Adapt to Task Type

**Trigger:** Model receives reasoning discipline guidance permanently embedded in the stable tier, regardless of whether the task requires deep theoretical reasoning (e.g., proving a theorem, debugging a race condition) vs. straightforward implementation (e.g., writing a CRUD endpoint).

**Blast Radius:**
- For tasks requiring deep reasoning, "stop after 2 iterations" causes premature abandonment of legitimate multi-step theoretical work
- For tasks requiring tool use, the guidance is redundant with OPENAI_MODEL_EXECUTION_GUIDANCE (which already mandates tool use for specific operations)
- The guidance is injected into the STABLE tier (system_prompt.py:173-186) — built once per session, cached for all turns — meaning a model working on a mix of task types cannot adapt
- Contrast with ephemeral guidance: TOOL_USE_ENFORCEMENT_GUIDANCE is model-gated (only for "gpt", "codex", "gemini", "gemma", "grok", "glm", "qwen", "deepseek" per line 232-258), but REASONING_DISCIPLINE_GUIDANCE has no such gating

**Evidence:**
- system_prompt.py:147-162 stable tier description: "identity (SOUL.md or DEFAULT_AGENT_IDENTITY), tool guidance, computer-use guidance... Built once per session and reused across all turns"
- Line 232-246 shows model-gated injection for TOOL_USE_ENFORCEMENT_GUIDANCE using `TOOL_USE_ENFORCEMENT_MODELS` tuple
- Line 173-186 shows the guidance blocks are appended to `stable_parts` unconditionally (only gated by config flags, not by task type or model family)

---

## Summary Table

| Failure Mode | Root Cause | Detection Method |
|--------------|------------|------------------|
| Gateway config gap | No `_reasoning_discipline_guidance` read in agent_init.py; gateway bypasses load_config() | `git grep "_reasoning_discipline_guidance"` returns 0 results in agent_init.py |
| Cache invalidation gap | `_extract_cache_busting_config()` excludes agent.* keys; `_agent_config_signature()` doesn't hash them | Add `reasoning_discipline_guidance: false` mid-session; observe no agent rebuild |
| Token dilution + conflict | 80 tokens of metacognitive guidance competes with existing guidance; partial contradiction with "keep working" directive | Measure completion rates on tasks before/after adding guidance; compare error modes |
| Static vs. dynamic task needs | Guidance injected into stable tier, not task-gated; no model-family gating | Compare performance on deep-reasoning vs. shallow-implementation tasks |
