---
name: perfectloop
description: Use when designing autonomous, recurring, or goal-seeking agent loops; interview for goal/context/actions/feedback/state/stop/boundary, decide if a loop is justified, then emit a safe loop spec with objective gates, state, budget, reviewer split, and first-run verification.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [agentic-loops, automation, loop-engineering, cron, skills, verification, autonomous-agents]
    related_skills: [plan, test-driven-development, systematic-debugging, subagent-driven-development, codex]
---

# Perfectloop

## Overview

Use this skill to design a loop before running or scheduling it. The job is not to “make an agent” by writing a big prompt. The job is to design the system that repeatedly prompts, observes, verifies, records state, learns, and safely decides the next action.

A good loop is a small harness around an agent:

```text
Act -> Observe -> Learn -> Repeat
```

The agent may propose work, but an external gate must judge the result. A loop without objective feedback is just an agent grading its own homework repeatedly.

This skill is a **design layer**, not a blind orchestration command. It should first interview the user for the healthy-loop pieces, reject bad loop candidates, then emit a portable loop spec that can be run in-session, scheduled by cron, delegated to external coding agents, or implemented as a small script.

## Source Synthesis

Detailed crawl/OCR notes live in `references/source-notes.md`.

This skill distills three source patterns:

1. **Looper design-coach pattern** from the provided X post/screenshot:
   - Design the loop end-to-end before running it.
   - Interview for `goal`, `context`, `actions`, `feedback`, `state`, `stop`, and `boundary`.
   - Kill vague or unfalsifiable goals.
   - Define verification, host model, review council, gates/control, confirmation, and owned artifacts.
   - Run with gated revisions: draft -> reviewer gate -> delivery -> delivery gate -> record state -> final output.
   - If a gate fails, revise and redo the step; do not proceed on vibes.

2. **Loop-engineering roadmap** from the X Article:
   - Loops are worthwhile only when the task repeats, automated verification exists, budget can absorb waste, and the agent has the tools/environment to reproduce and inspect failures.
   - Minimum viable loop: one automation, one skill, one state file, one gate.
   - Build order matters: get one manual run reliable, turn it into a skill, wrap it in a loop, then schedule it.
   - Key failure modes: no objective gate, maker grades its own work, no state file, vague stop conditions, no budget cap, loops on judgment-call work, comprehension debt, security/permission creep, and orphaned scheduler jobs that keep running after terminal state.

3. **CoMPILOT / agentic auto-scheduling paper pattern**:
   - The LLM acts as an optimizer only because it is grounded by empirical compiler feedback.
   - The useful loop is closed-loop interaction with an external system: propose transformation -> apply/check legality -> measure speedup/slowdown -> feed result back -> refine strategy.
   - General lesson: agentic loops improve when feedback is concrete, measured, and tied to the real target environment.

## When to Use

Use when the user asks to:

- design an agentic loop, autonomous routine, recurring workflow, `/loop`, `/goal`, cron job, watchdog, or background agent;
- turn a repeated manual prompt into an automated loop;
- design a “perfect loop”, “looper”, “loop harness”, “review-gated loop”, or “agentic system”;
- schedule coding, triage, QA, monitoring, research, learning, or operational work;
- improve an existing loop that burns tokens, stalls, drifts, or produces unverifiable output.

Do **not** use this skill to blindly schedule a job. If the loop fit test fails, say so and recommend a manual prompt, checklist, one-shot script, or observation-only run instead.

## Core Contract

Before designing the loop, answer these questions in order:

1. **Should this be a loop at all?**
2. **What exactly is the falsifiable goal?**
3. **What outside signal can reject bad output?**
4. **What persistent state survives between runs?**
5. **What can the agent do, and what is forbidden?**
6. **When does the loop stop, escalate, or clean itself up?**
7. **Who/what verifies the verifier?**
8. **If scheduled, what verified cleanup proves the controller is removed or intentionally paused?**

If any answer is missing and materially changes the design, ask the user. If the missing answer is low-stakes, state a default assumption and continue.

## The Healthy-Loop Interview

Ask only the questions needed for the current task. Start with the compact version; expand only when risk is high.

### Compact Interview

1. **Goal:** What outcome should be true at the end, in falsifiable terms?
2. **Cadence / trigger:** What starts each run: schedule, event, manual command, or goal-not-yet-met?
3. **Context:** What sources must the loop read every run: repo files, docs, issues, logs, APIs, screenshots, database queries?
4. **Allowed actions:** What may it change or call: files, branches, PRs, tickets, messages, deploys, devices?
5. **Feedback / gate:** What automated check can fail the work: tests, build, typecheck, linter, API result, benchmark, screenshot diff, human approval?
6. **State:** Where does progress live outside the chat: `STATE.md`, issue tracker, database row, JSON artifact, cron output, dashboard?
7. **Stop conditions:** What ends or pauses it: success gate, max iterations, time, budget, no-progress count, repeated failure, human decision?
8. **Boundaries:** What must never happen without approval: merge, deploy, payment, credential change, deletion, public post, production data mutation?

### Expanded Interview for Production or High-Risk Loops

- What is the smallest manual run that proves the task is loopable?
- How often does the task recur, and what is the cost of not automating it?
- What is the expected token/time/money budget per accepted change?
- Which model/agent is the maker? Which model/agent/check is the judge?
- Should the judge be independent, different-model, read-only, or deterministic?
- What counts as no progress?
- What exact escalation message should the loop send when blocked?
- What permissions are needed now, and what permissions should remain impossible?
- What secrets/logs must be sanitized?
- What metric proves the loop is paying back: accepted-change rate, incident reduction, mean time to resolution, cost per accepted change, false-positive rate?
- What is the cleanup path if the loop is wrong or stale?

## Loop Fit Test

Do not design a full loop until the candidate passes this test.

| Condition | Pass Signal | Fail Signal | Default Recommendation |
| --- | --- | --- | --- |
| Repeats | Happens at least weekly or is event-driven | One-off exploratory task | Use a strong manual prompt or one-shot script |
| Automated verification | A deterministic or empirical gate can reject bad output | “Looks good” / agent opinion only | Keep human in the chair |
| Tool/environment access | Agent can inspect, reproduce, run, and observe the real system | No logs/tests/API/env/device access | Build observability first |
| Budget and stop control | Explicit max runs/time/tokens/cost and no-progress stops | Open-ended loop | Add caps before running |
| Safe boundaries | Irreversible actions require approval | Merge/deploy/delete/pay/post without gate | Add human approval or forbid action |

Decision labels:

- `LOOP`: all core conditions pass; design a real loop.
- `MANUAL-FIRST`: promising but needs one reliable manual run before scheduling.
- `OBSERVE-FIRST`: missing evidence; design a bounded observation-to-action loop.
- `DO-NOT-LOOP`: one-off, judgment-heavy, unsafe, or unverifiable.

## Minimum Viable Loop

When a task passes, keep v1 small:

1. **One automation** — a schedule, event trigger, or explicit `/goal` continuation.
2. **One skill** — the reusable task context and rules the agent rereads each run.
3. **One state file/system** — durable progress and lessons outside the chat.
4. **One objective gate** — command/API/check/benchmark that can fail the work.

Build order:

1. Run the task manually once with the exact context and gate.
2. Convert the reliable manual instructions into a skill/checklist.
3. Add state persistence.
4. Wrap it in a loop with caps and escalation.
5. Add terminal cleanup before scheduling: `DONE` removes the job; `DECIDE` / `BLOCKED` pauses or removes by spec; every terminal path writes a cleanup receipt.
6. Schedule only after the manual run, gate, and cleanup path are proven.

## Review-Gated Loop Shape

Use this as the default structure for a designed loop:

```text
GOAL + CONTEXT
  - Read source-of-truth context.
  - Load the task skill.
  - Read state from previous runs.

DRAFT PLAN
  - Maker agent proposes the next bounded action.
  - Plan is written to an artifact, not just chat.

PLAN GATE
  - Judge verifies the plan covers the goal, respects boundaries, and has a real check.
  - If fail: revise plan, up to cap.

DELIVER WORK
  - Maker performs one bounded step.
  - No irreversible action unless explicitly allowed.

DELIVERY GATE
  - Deterministic check first where possible.
  - Independent reviewer or judge second for semantic/spec coverage.
  - If fail: revise from the failed step, not from scratch, up to cap.

RECORD RESULT
  - Update state with files touched, checks run, classifications, blockers, decisions, and next action.
  - Store artifacts/logs needed for audit.

STOP / REPEAT / ESCALATE
  - Stop on success, budget cap, no-progress threshold, or human decision needed.
  - Repeat only when the next action is safe and the gate provides feedback.

TERMINAL CLEANUP
  - A scheduled controller loop is not complete until scheduler state, loop state, temp artifacts, and final report agree on the terminal result.
  - Remove the scheduler on `DONE`; pause or remove it by spec on `DECIDE`, `BLOCKED`, `FAILED`, timeout, or corrupt state.
  - Verify cleanup by scheduler read-back and record a cleanup receipt.
```

## State Design

A loop state artifact should be short, append-friendly, and machine-readable enough for future runs.

Recommended sections:

```markdown
# Loop State · <name>

## Current Goal
<falsifiable goal>

## Last Run
<timestamp, trigger, summary, result>

## In Progress
- <bounded item> — owner, status, next check

## Completed
- <item> — evidence link/command/result

## Blocked / Escalated
- <item> — why blocked, who owns decision, recommended default

## Learned Rules
- <durable rule discovered by the loop; promote to a skill when reusable>

## Next Action
<exact next bounded action or STOP>

## Cleanup Receipt
<terminal status, scheduler action, job id, delivery action, state/archive action, temp artifact action, verification method, verified true/false>
```

For production/team loops, use an issue tracker, database table, or dashboard instead of a repo-local Markdown file when visibility/querying matters.

## Gate Design

Prefer gates in this order:

1. Deterministic command: tests, typecheck, linter, build, formatter check.
2. Empirical system result: benchmark speedup/slowdown, API response, health check, screenshot diff, log query, device verification.
3. Independent reviewer: separate model/agent with read-only access and a rubric.
4. Human approval: only for judgment calls, irreversible changes, or business/product decisions.

A reviewer is not enough by itself when a deterministic or empirical gate exists. The maker should not be the only judge.

Gate rubric template:

```text
Gate: <name>
Inputs: <artifact/check/result>
Pass if:
- <objective criterion>
- <boundary respected>
- <evidence exists>
Fail if:
- <specific failure>
- <missing proof>
- <unsafe action>
On fail: <revise step, retry cap, escalation>
```

## Stop and Control Defaults

Default caps for a first loop unless the user specifies otherwise:

- Max revisions per gated step: `3`.
- Max total iterations: `12`.
- No-progress stop: `2` consecutive runs with no new evidence or accepted change.
- Time cap: `30 minutes` for an interactive run; explicit horizon for cron.
- Token/cost cap: state a concrete cap or use the platform's configured budget.
- Escalate when blocked by missing credentials, missing environment, permissions, product judgment, repeated gate failure, or unsafe side effect.

Never let a loop run indefinitely just because it is making noise.

## Controller Scheduler Cleanup Contract

When a loop uses a scheduled controller job to outlive an interactive `/goal` or model turn cap, cleanup is part of completion, not an afterthought.

Invariant:

> A controller scheduler loop is not done until the scheduler is removed or intentionally paused, state is archived, temp artifacts are handled, and a read-back proves it is no longer silently running.

Terminal matrix:

| Status | Deliver final message | Scheduler action | State action | Temp artifacts | Resume? |
| --- | --- | --- | --- | --- | --- |
| `DONE` | yes | remove job | mark terminal + archive | delete/compact safe scratch | no |
| `DECIDE` | yes, with question + recommended default | pause job or remove if specified | mark `DECIDE` | keep evidence | maybe |
| `BLOCKED` | yes, with blocker + next needed input | pause job | mark `BLOCKED` | keep evidence | yes, after fix |
| `FAILED` | yes, with reason | remove or pause per spec | mark `FAILED` | keep evidence | maybe manual |
| timeout/crash/corrupt state | yes if possible | pause job | create failure receipt | keep logs | manual |

At the start of every scheduled tick:

1. Read loop state before doing work.
2. If state is terminal (`DONE`, `DECIDE`, `BLOCKED`, `FAILED`), do not continue work.
3. Run cleanup protocol: deliver terminal summary if missing, remove or pause the job, archive state/evidence, handle scratch artifacts, verify scheduler read-back, and write/update the cleanup receipt.

At the end of any tick that reaches terminal state:

1. Set the terminal status in state.
2. Deliver the final summary or decision/blocker report.
3. Remove or pause the scheduler according to the matrix.
4. Re-read the scheduler registry to verify no active repeating orphan remains.
5. Write the cleanup receipt with `job_id`, action, verification method, and result.

If the scheduler cannot remove or pause itself, the loop must emit a visible `CLEANUP_REQUIRED` report with the `job_id`, intended terminal action, and exact manual cleanup command or UI path.

## Permission and Boundary Model

Classify actions before running:

| Level | Examples | Default |
| --- | --- | --- |
| Read | inspect files, logs, issues, API GETs | allow if credentials are already configured |
| Draft | create local patch, draft plan, draft PR body, write report | allow with gate |
| Local mutate | edit files in a worktree, update local state file | allow if reversible and verified |
| Remote non-prod | open PR, comment on issue, update ticket | allow only if requested or part of spec |
| Production/irreversible | merge, deploy, delete, pay, credential change, public post, prod DB mutation | require explicit human approval |

Loops should usually produce reviewed artifacts, not directly merge/deploy.

## Output Artifact Template

When asked to design a loop, emit this artifact set when appropriate:

```text
LOOP_SPEC.md          # human-readable design
loop.yaml            # machine-readable schedule/model/tools/gates/state/actions
RUN_IN_SESSION.md    # first manual run instructions
STATE.md or state.json
cleanup-receipt.json # required for scheduled controller loops
run-loop.py          # only when the user asks for an executable runner
```

For a normal chat response, use this compact output format:

```markdown
# Loop Design: <name>

Verdict: LOOP | MANUAL-FIRST | OBSERVE-FIRST | DO-NOT-LOOP
Reason: <one sentence>

## Missing Answers / Assumptions
- <ask only material questions; otherwise state defaults>

## Goal
<falsifiable goal>

## Trigger
<cadence/event/manual/goal continuation>

## Context Read Each Run
- <source>

## Actions Allowed / Forbidden
Allowed:
- <action>
Forbidden without approval:
- <action>

## Maker / Checker
- Maker: <agent/model/tool>
- Checker: <deterministic gate and/or reviewer>

## Gates
1. <gate name>: `<command/API/check>` -> pass/fail criteria
2. <gate name>: <review rubric>

## State
- Location: `<path/system>`
- Fields: <minimal fields>

## Stop / Escalation
- Success: <condition>
- Caps: <iterations/time/budget/no-progress>
- Escalate when: <condition and message>

## Cleanup / Self-Removal
- Terminal matrix: <DONE/DECIDE/BLOCKED/FAILED actions>
- Scheduler verification: <cron/job read-back proving removed or paused>
- Receipt: <where cleanup receipt is written>

## First Manual Run
1. <bounded action>
2. <gate>
3. <record state>

## Schedule / Runner
<only after manual run is reliable>

## Definition of Done
- [ ] <observable criterion>
```

## Loop Types and Defaults

### Coding Quality Loop

Good candidates: CI triage, dependency bumps, lint fixes, flaky test reproduction, issue-to-PR drafts in well-tested codebases.

Required gates:
- relevant tests;
- lint/typecheck/build;
- diff inspection;
- human review before merge/deploy.

Default state:
- branch/worktree, files touched, command output summary, PR/issue links, blocker classification.

### Research / Knowledge Loop

Good candidates: recurring source monitoring, paper/blog digestion, skill updates from explicit learning triggers.

Required gates:
- source URLs captured;
- claims tied to sources;
- duplicate/staleness check;
- update goes to an existing skill/memory bucket, not one-off sprawl.

Default state:
- sources crawled, extracted facts, accepted changes, DECIDE items.

### Monitoring / Observation Loop

Good candidates: structured logs, health checks, screenshot banks, sync failures, cost anomalies.

Required gates:
- decision question;
- data source;
- analysis cadence;
- horizon/cleanup;
- action on threshold.

Do not create passive logging. Pair observation with an analyzer that outputs `FIX`, `APPLY`, `DECIDE`, `STOP`, or stays silent.

### Optimization Loop

Good candidates: compiler/search/benchmark tuning, prompt variants, performance experiments.

Required gates:
- empirical measurement in the target environment;
- legality/correctness check before speed metric;
- baseline preserved;
- regression stop.

Pattern:

```text
propose candidate -> apply -> legality/correctness check -> measure -> record -> refine
```

## Common Failure Modes

1. **Ralph Wiggum loop:** the loop declares success early because the completion signal is soft. Fix with objective gates and independent stop-condition checks.
2. **Maker grades own homework:** the same agent writes and approves. Fix with deterministic gates and independent reviewers.
3. **No state file:** every run restarts from zero. Fix with durable state.
4. **Vague goal:** “make it better” cannot be falsified. Rewrite as a measurable condition or do not loop.
5. **No hard stop:** the loop runs until budget/rate limits kill it. Add caps.
6. **Judgment-call work:** architecture, auth, payments, production deploys, vague product strategy. Keep human approval in the loop.
7. **Comprehension debt:** loop ships more than humans understand. Require diff review, accepted-change metrics, and periodic gate audits.
8. **Security tax ignored:** unattended loops are attack surfaces. Sanitize logs, audit permissions, avoid auto-installing untrusted skills, and re-check scopes periodically.
9. **Review bottleneck moved, not removed:** the loop generates more work than humans can review. Track accepted-change rate and queue size.
10. **Observability gap:** the agent cannot run or observe the system it changes. Build logs/tests/API/screenshot access first.
11. **Orphaned controller:** the loop reaches `DONE` / `DECIDE` / `BLOCKED` but leaves its cron/controller running. Fix with terminal-state preflight, self-removal or pause, scheduler read-back, and a cleanup receipt.

## Question-Driven Design Rules

- Ask questions before design only when the answer changes safety, feasibility, or loop architecture.
- If the user wants speed, use explicit assumptions and mark them as defaults.
- Prefer a smaller loop with a real gate over a broad loop with vague review.
- A “perfect” loop is not maximal autonomy; it is the smallest loop that repeatedly produces accepted, verified changes under budget.
- If a loop is unsafe, say `DO-NOT-LOOP` and provide the closest safe alternative.

## Verification Checklist

Before saying a loop design is ready:

- [ ] The loop-fit verdict is explicit.
- [ ] Goal is falsifiable.
- [ ] Trigger/cadence is defined.
- [ ] Context sources are named.
- [ ] Allowed and forbidden actions are named.
- [ ] Objective gate exists, or the design is rejected/manual-first.
- [ ] Maker/checker split is defined where non-trivial work occurs.
- [ ] Persistent state location and fields are defined.
- [ ] Stop conditions include success, budget/time/iteration cap, no-progress, and escalation.
- [ ] Scheduled/controller loops define terminal cleanup, scheduler removal/pause, read-back verification, and cleanup receipt.
- [ ] Human approval gates cover irreversible actions.
- [ ] First manual run is specified before scheduling.
- [ ] Security/log/permission risks are addressed.
- [ ] The final output includes evidence that the design can be verified, not just run.
