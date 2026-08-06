# Source Notes: Perfectloop

## Sources crawled

- X post: `https://x.com/shannholmberg/status/2069323309024776456?s=46`
- Quoted Looper post: `https://twitter.com/3108019343/status/2067874418232119347`
- X post: `https://x.com/0xmovez/status/2069500921382326531?s=46`
- Quoted X Article post: `https://twitter.com/0xCodez/status/2064374643729773029`
- X Article URL: `http://x.com/i/article/2064357550225510400`
- Referenced paper: `https://arxiv.org/abs/2511.00592v2`

## X API extraction

### Shann post

Text:

> how to improve your /goal by designing the loop before you run it
>
> a poorly designed loop burns tokens and hands you slop fast, that´s why it's important to spend time designing the loop harness
>
> instead of writing a /goal and hoping it works, you run it through LOOPER skill

Attached image URL: `https://pbs.twimg.com/media/HLe2lVOa4AERkLr.jpg`

Quoted post by `@KSimback`:

> Feeling a bit loopy? Introducing Looper - your loop design coach
>
> It’s been made clear that we shouldn’t be promoting agents, we should be designing loops
>
> You can use /loop or /goal but if your loop is poorly designed then it won’t produce good output
>
> Even worse, a poorly

The quote was truncated by X API, so the screenshot supplied the operational structure.

### Looper screenshot OCR summary

Title:

> LOOPER · DESIGN A REVIEW-GATED LOOP, THEN RUN IT
> the /looper coach designs the loop end to end, then runs it in-session or emits a portable spec. by @KSimback

Design side:

1. `/looper · INTERVIEW` asks the healthy-loop pieces: `goal:context:actions:feedback:state:stop:boundary`.
2. `GOAL`: user states it, looper tightens it, kills vague or unfalsifiable goals.
3. `VERIFICATION`: define what done means — programmatic command returns pass/fail, judge model scores a rubric, human signs off.
4. `HOST MODEL`: model that drives the loop, e.g. Codex / GPT-5.
5. `COUNCIL`: review seat — second model reviewer or judge; different family than host; default egress redacts env/secrets; consent first.
6. `GATES + CONTROL`: revise <= 3, max 12 iterations, no-progress x2, budget ~2M tokens / 30 min.
7. `CONFIRM`: review the loop as an ASCII preview first.
8. `EMIT THE ARTIFACTS YOU OWN`: `RUN_IN_SESSION.md`, `loop.yaml`, `loop.resolved.json`, `run-loop.py`.
9. `RUN IT`: continues to execution.

Run side:

- Read goal + context: process notes, definition of done, no TBDs.
- Draft `plan.md`.
- Plan gate: reviewer judge, criteria covers goal, pass.
- Write delivery artifact.
- Delivery gate: check + judge, criteria required sections + covers goal, pass.
- Record result: `state.json`, `run-log.md`, checkpoint per gate; stop when gates clean / 12 iterations / stalled x2 / budget.
- Final output only after every delivery passed its gate clean.
- Gutter loop: if a gate fails, revise <= 3 and redo the step.
- Footer: design layer, not orchestration; run in-session, with /goal, on a /loop schedule, or with the Python runner.

## X Article extraction

The quoted article title was:

> Loop engineering: the 14-step roadmap from prompter to loop designer.

Core claims extracted from `data.article.plain_text`:

- Loop engineering replaces the human as prompter: a small system finds work, hands it to the agent, checks the result, records what happened, and decides the next move.
- Build loops only when four conditions hold:
  1. task repeats;
  2. verification is automated;
  3. budget can absorb waste;
  4. agent has senior-engineer tools/logs/reproduction environment.
- 30-second loop check:
  1. task happens at least weekly;
  2. test/typecheck/build/linter can reject bad output;
  3. agent can run the code it changes;
  4. loop has a hard stop;
  5. human reviews before merge/deploy/dependency changes.
- Five building blocks:
  1. automations as heartbeat;
  2. worktrees for parallel isolation;
  3. skills for reusable project knowledge;
  4. connectors/MCP for real-world tool access;
  5. sub-agents for maker/checker split.
- Minimum viable loop: one automation, one skill, one state file, one gate.
- Correct build order: manual run reliable -> skill -> loop -> schedule.
- Metric: cost per accepted change, not tokens spent or tasks attempted.
- Failure modes: soft completion, no objective verifier, goal drift, self-preferential bias, agentic laziness, comprehension debt, cognitive surrender, unattended security attack surface.
- Security concerns: generated code shipping unreviewed, skills as injection vectors, credentials in logs, permission creep.

## Referenced paper extraction

Paper:

> Agentic Auto-Scheduling: An Experimental Study of LLM-Guided Loop Optimization
> Massinissa Merouani, Islem Kara Bernou, Riyadh Baghdadi
> arXiv:2511.00592v2

Abstract facts from arXiv API:

- Presents CoMPILOT, a closed-loop LLM/compiler interaction framework.
- LLM proposes loop transformations for a loop nest.
- Compiler attempts transformations and reports legality plus measured speedup/slowdown.
- LLM uses concrete feedback to iteratively refine optimization strategy.
- Evaluation on PolyBench reports geometric mean speedups of 2.66x single run and 3.54x best-of-5 over original code.
- Generalizable loop lesson: the loop works because feedback is concrete and empirical in the real target environment.

## Distilled design rules

1. Do not start with `/goal`; start by designing the loop harness.
2. Reject loops that cannot be objectively failed.
3. Ask for goal/context/actions/feedback/state/stop/boundary.
4. Keep v1 tiny: one automation, one skill, one state, one gate.
5. Separate maker and checker whenever the work is non-trivial.
6. Persist state outside the conversation.
7. Hard-cap budget, iterations, time, and no-progress cycles.
8. Require human approval for irreversible actions.
9. Measure accepted outcomes, not activity.
10. Schedule only after a reliable manual run.
