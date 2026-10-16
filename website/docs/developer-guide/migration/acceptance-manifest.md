# Desktop-First Migration — Acceptance Manifest & Port Ledger

Base: upstream `origin/main` @ 4323c67dcc6048fc8e311cdff7600d3d6a17807f
Branch: `migration/desktop-upstream-first`
Last updated: 2026-08-17 (during execution)

Every row must resolve to exactly one decision:
- **RETAIN** — upstream behavior is equivalent; keep upstream, add regression tests only if meaningful.
- **PARITY** — upstream architecture differs; write behavior-level test first; port only on reproducible failure.
- **PORT** — genuinely fork-unique and actively required for Desktop/BestPlan/local-first routing.
- **DROP** — standalone WebUI, dashboard, CLI-only surface, dormant/experimental, or upstream-superseded.

## 1. Session durability & Desktop correctness

| Behavior | Decision | Upstream evidence | Fork tail to test |
|---|---|---|---|
| Durable-history rebasing | RETAIN | renderer submits against sessionId; SessionDB owns canonical history; transcript backfill/tail grafting | stale renderer state cannot replace canonical history |
| Canonical projection repair | RETAIN | appendLiveSessionProjection + replay repair | repair persistence on repeated resume |
| Receipt & final-response persistence | RETAIN | finalizer + transactional append + tool-tail closure tests | final response/receipt exactly once |
| Background session-revision polling | DROP | sessions.changed events + background sync (fork poll hits unregistered route 404) | n/a — event-driven refresh instead |
| Active-session refresh | RETAIN | event-driven refresh + stale-request guards | late event must not overwrite newer state |
| Submit locking & queued prompts | RETAIN | per-session locks, queue park/drain, route-drift guard, pre-turn settle | queued prompt session isolation |
| Interruption persistence | RETAIN | persisted partial messages + interrupt closure tests | interrupted tool tail closes correctly |
| Warm restart/resume | RETAIN | resume_pending + startup restore + deferred hydration | restart does not duplicate turns |
| Stale/duplicate turn avoidance | RETAIN | turn leases, row stamps, fencing, compression-tip adoption, seq repair | cross-process fence; optimistic dedup |

## 2. Context & persistence integrity

| Behavior | Decision | Upstream evidence | Fork tail |
|---|---|---|---|
| Deterministic dispatch-history pruning | PARITY | proactive tool-result pruning + restart safety | strict JSON after pruning; bounded multimodal receipts; idempotence |
| Provider request admission | PARITY | none equivalent found (only Feishu bot ref) | output reserve; provider payload shape; fail-closed limits |
| Atomic compaction snapshots | RETAIN | compression machinery incl. rotation fix | n/a |
| Verified-checkpoint preservation | RETAIN | compression tip adoption | n/a |

## 3. Provider / profile / credential behavior

| Behavior | Decision | Upstream evidence | Fork tail |
|---|---|---|---|
| Profile-scoped credential isolation | RETAIN | secret scope + multiplex isolation tests | per-profile leak tests |
| Native OAuth / token store | RETAIN | auth.json pool, native OAuth | no credential crosses profile boundary |
| Model picker secret handling | RETAIN | model-picker secret scope tests | no secret persisted to config on switch |
| Root->profile inheritance | DROP (initial) | profiles materialized; no inheritance port | test each active profile complete |
| Local-first route/tier/mode resolution | PORT (narrow) | upstream has generic delegation; no route precedence resolver | explicit route > tier > mode > default; no silent fallback |

## 4. Delegation & BestPlan

| Behavior | Decision | Upstream evidence | Fork tail |
|---|---|---|---|
| Full BestPlan (explorer/synthesizer/review/promotion/proofs) | PORT (plugin) | zero upstream presence | full semantics incl. quorum, receipts, cancellation |
| Broker-only child workers | PORT (plugin) | upstream delegation lifecycle retained | inherited broker guard test |
| Read-only containment | PORT (plugin) | upstream tool policies | prepared cwd/tool runtime only if tests fail |
| Host-owned final response | PORT (bridge) | transform_llm_output hook exists | no parent rewrite of final; delivery identity |
| Completion-proof gatekeeper | PORT (plugin) | transform_llm_output + delivery plumbing | plugin sees host final; interrupt/error not rewritten |
| Delegation recovery | RETAIN | ownership/restore/steer/pause/resume upstream | local-first lane selection + degraded fallback |

## 5. Coding-verification & tool runtime

| Behavior | Decision | Upstream evidence | Fork tail |
|---|---|---|---|
| Coding verification on stop | RETAIN | verification nudges upstream | budget-exhausted hard stop only if false-completion passes |
| Codex/Sol containment | PARITY | request-scoped routing upstream | executable resolution; isolated home; process-group cleanup |
| Session/workspace cwd identity | PARITY | execution_cwd upstream | cwd across submit/resume/delegation/BestPlan |

## 6. Discarded / deferred (explicit)

- Standalone WebUI (`web/`), dashboard admin, WebUI lifecycle guard
- CLI-only diagnostics/formatting/commands
- `local-first-supervision`, `radar-telemetry`, `verified-state-guard`, `safe-change-nextfix`, `zeus-tps-dashboard`, `gitnexus-governor` (stale config; do not recreate)
- cron extensions (0 jobs active), remote gateway work, autonomy/trajectory, research-lab UI
- Broad required-policy framework (`tool_policy.py`) — dormant; `plugins.required_policies` empty
- Fork projection schema, `session_revision.py`, fork submit/queue/resume/stale-turn code

## Decision rules

1. A missing fork symbol is NOT a failure. Only a failing behavioral test creates port work.
2. Every core edit needs: failing test on untouched upstream -> smallest patch -> wider regression.
3. No wholesale copy of `run_agent.py`, `hermes_state.py`, `delegate_tool.py`, `async_delegation.py`, `conversation_loop.py`, gateway files.
4. Max three stable core seams for the BestPlan bridge; beyond that, produce an API-gap report.
