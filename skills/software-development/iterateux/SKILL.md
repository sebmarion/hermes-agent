---
name: iterateux
description: "Use when the user asks for visible UX/UI/product-surface iterations, screenshot-backed design passes, or implemented design loops on a real app/prototype/artifact."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ux, ui, design, iteration, screenshots, product-surface, implementation, visual-qa]
    related_skills: [plan, product-design-review, dogfood, test-driven-development]
---

# IterateUX

## Overview

Use this skill for **implemented visible UX iteration**, not text-only planning. A user request such as `iterate the UX`, `do N design passes`, or `$iterateux <N> <request>` means perform that many visible design upgrade passes on the latest artifact or real product surface, with evidence after each pass.

A pass does **not** count unless a visible artifact or product surface changed before review.

## When to Use

Use when:

- The user asks for visible design passes, UX iteration, UI polish, screenshot-backed design loops, or product-surface redesign.
- The task concerns UX, UI, visual hierarchy, layout, navigation, IA, dashboards, operational boards, workflow surfaces, mobile/tablet/POS surfaces, prototypes, rendered copy, or design systems **and** the user wants implemented changes.
- A prior UI/design change looked wrong and needs iterative visible improvement.

Do not use when:

- The user only wants a plan, audit, recommendation, or acceptance checklist; use `plan` and optionally `product-design-review`.
- The task is backend-only or has no rendered/user-facing surface.
- Mutating files would be inappropriate and the user has not asked to create a disposable prototype.

## Required Behavior

### 0. Load the planning and taste substrate

Before mutating anything:

1. Load `plan` or apply its evidence/risk/acceptance discipline internally.
2. Load `product-design-review` when available before any visual styling, taste, polish, color, layout, or "impressive" work. Use it to define the reference-backed direction and negative taste filter before mutation.
3. Load high-signal domain/QA skills, especially `dogfood` for exploratory browser QA when applicable.
4. Do not publish a full plan unless the user asks for it; convert the plan into an execution checklist and start iterating.

### 1. Establish target and guardrails

Identify:

- Goal and primary user task.
- Target repo/project/artifact/route.
- Repo-local guidance docs, README files, local design docs, and other project instructions that define the target surface and constraints.
- Current branch/worktree status when editing a repo.
- What must not happen: data loss, public posting, destructive ops, accidental edits in scratch/prototype dirs, breaking mobile/POS/tablet flows.
- Acceptance criteria tied to visible product behavior.

If the target is ambiguous or the active workspace is known to be a scratch/prototype area, resolve the canonical target before editing. If the user provided only a screenshot with no editable artifact, ask for the target or create a disposable prototype only when appropriate.

### 2. Capture baseline evidence

Before the first upgrade:

- Run or open the app/prototype/artifact.
- Inspect the relevant route/screen at realistic viewports.
- Capture a screenshot when possible.
- Check browser console errors for web surfaces when possible.
- Record the baseline UX diagnosis in concise terms: primary task clarity, visual hierarchy, affordances, copy, spacing, accessibility/readability, and machine-state leakage.

Do not count baseline inspection as an iteration.

### 3. Establish a design taste gate

Before changing visuals, define the design basis and anti-goals. This is mandatory when the user complains about taste, visual quality, polish, boring design, color mismatch, or asks for something to look "impressive".

Use `product-design-review` to state:

- product/job;
- reference basis;
- visual direction;
- typography/density/palette roles;
- components and hierarchy;
- anti-goals;
- stop rule.

If the proposed direction cannot be justified by a reference, existing design system, or the user's product goal, do not implement it. Create 2-3 lightweight design directions/prototypes or ask for a reference instead.

### 4. Run N implemented UX iterations

For each iteration `1..N`:

1. Choose the highest-leverage visible improvement from the latest state, not from the original baseline.
2. Implement the change in the real component/prototype/artifact.
3. Verify the artifact still builds/renders.
4. Capture or identify evidence of the upgraded state: screenshot, route, viewport, console/test result.
5. Review the upgraded state against UX and product-design criteria, including the aesthetic verdict from `product-design-review`.
6. Record concise proof for the pass.

A pass is invalid and must be redone if no visible artifact/code/design changed before review.

### 5. Verification standard

Use real checks, not descriptions of what should work:

- Static checks: typecheck/lint/build where available and relevant.
- Browser checks: load the route/artifact, inspect console, exercise key interactions.
- Screenshot evidence: before/after or per-iteration screenshots where possible.
- Responsive evidence: include mobile/tablet/POS viewports when the surface is used there.
- Accessibility/readability: obvious target size, contrast, truncation, focus/keyboard/ARIA checks when relevant.
- Aesthetic verdict: pass/fail/needs human reference.

For web apps, console logs alone are not enough; judge the visible product surface.

## Output Format

```markdown
# IterateUX: <short title>

Changed:
- <files/routes/artifacts changed>

Design gate:
- Reference basis: <...>
- Anti-goals: <...>
- Aesthetic verdict: <pass/fail/needs human reference>

Iteration evidence:
- Run 1: <implemented upgrade>; evidence: <screenshot/route/artifact>; review: <what improved + next gap>
- Run 2: <implemented upgrade>; evidence: <screenshot/route/artifact>; review: <what improved + next gap>

Verified:
- `<command/check>` → <actual result>
- `<browser/screenshot check>` → <actual result/path>

Remaining risks:
- <risk or none>

TL;DR: <one-line outcome>
```

For high iteration counts, group proof only when individual screenshots/evidence would be noisy, but still preserve the number of implemented passes and final evidence.

## Common Pitfalls

1. **Counting critique as an iteration.** Talking about a better layout is not a run. Change the artifact first, then review.
2. **Reviewing the baseline repeatedly.** Iteration N must review the output of iteration N, not the original screenshot.
3. **Skipping baseline evidence.** Without baseline inspection, you cannot prove what improved.
4. **Trusting tests over the rendered surface.** Typechecks and unit tests can pass while the screen is still confusing.
5. **Editing the wrong checkout or prototype.** Respect project guidance and canonical repo instructions.
6. **Making mobile/POS worse.** Verify constrained viewports, not just desktop.
7. **Leaving raw machine state in human-facing UI.** Translate runtime internals into human next-action states.
8. **Mistaking visual intensity for taste.** "Impressive" does not mean more gradients, glows, shadows, or accent colors.
9. **Using screenshots as proof without aesthetic judgment.** A screenshot proves what rendered, not that it is good.
10. **Calling a failed aesthetic verdict done.** Fail closed and keep iterating, simplify, revert, or ask for references.

## Verification Checklist

- [ ] Planning/taste substrate loaded or applied.
- [ ] Correct target artifact/repo inspected.
- [ ] Baseline evidence captured before counting iterations.
- [ ] Reference-backed design direction stated.
- [ ] Negative taste filter applied.
- [ ] Exactly the requested number of implemented visible upgrade passes completed, unless blocked and stated plainly.
- [ ] Each counted pass changed a visible artifact before review.
- [ ] Screenshots/rendered evidence captured where possible.
- [ ] Responsive/mobile/tablet/POS checked when relevant.
- [ ] Objective build/test/browser/console checks run.
- [ ] Aesthetic verdict reported honestly.
