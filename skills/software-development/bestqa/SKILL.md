---
name: bestqa
description: Run /bestqa persona batches and record evidence.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ux-testing, personas, qa, project-agnostic]
    related_skills: [dogfood]
---

# BestQA — Persona-Batch UX Testing

Run a batch of realistic user personas against the current project, surface reproducible friction, and return a prioritized QA/UX report. This skill is project-agnostic: never assume Comandero, Next.js, a particular port, a particular test runner, or a particular directory layout.

## Trigger

User says `/bestqa N [area]`.

- `N` is the number of personas; default to 5.
- `area` is an optional focus such as checkout, onboarding, API, mobile, permissions, or performance.
- `/bestqa 5 cart` and `/bestqa 10 mobile` are valid examples, but the same workflow applies to any product or repository.

## Interactive artifact bootstrap: PERSONAS.md

The first action in every run is to locate the project root and understand who the code appears to serve. **Do not create `PERSONAS.md` silently.** Human confirmation is required before the first write.

1. Determine the root from the current working directory (`git rev-parse --show-toplevel` when it is a Git project; otherwise use the current directory).
2. Read project instructions and the relevant manifest before testing (`AGENTS.md`, `CLAUDE.md`, `README.md`, `package.json`, `pyproject.toml`, `Cargo.toml`, or equivalent). Inspect routes, screens, CLI commands, API schemas, seed data, copy, and existing tests to infer the intended user—not just the technical stack. Do not invent commands or entry points.
3. If `PERSONAS.md` exists, read it and preserve its entries. Do not replace or rewrite it without explicit user direction.
4. If it is missing, present a concise **inference for human review** before writing anything:
   - what the project appears to do;
   - the typical user and their main job-to-be-done;
   - 3–5 supporting clues from actual files/routes/tests;
   - important uncertainties and alternative user types;
   - a proposed segment mix and representative personas for the batch;
   - the proposed `PERSONAS.md` structure and pool size (at least 50 entries by default).
5. Ask the human to confirm or correct the inferred audience and proposed pool. Use the interactive clarification prompt; do not proceed on silence or assumption. If corrected, revise the proposal and ask again.
6. Only after explicit approval, create `<project-root>/PERSONAS.md` with `write_file`. It must contain a usable pool of at least 50 distinct personas inferred from the approved audience, each with:
   - stable `id` and name;
   - role/background and technical confidence;
   - goals and likely expectations;
   - relevant constraints, accessibility, and device context;
   - segment tags.
7. Verify the artifact exists, is non-empty, and contains at least 50 persona entries before running the batch. Report its absolute path and the approval that authorized its creation.

`PERSONAS.ms` is not the artifact name; the intended file is `PERSONAS.md`.

## Run the batch

1. After the `PERSONAS.md` review/approval gate, select N personas randomly from the approved pool, avoiding duplicates within the batch and avoiding the immediately previous batch when history is available. Apply the requested area as a filter, not as a reason to select only one demographic.
2. State which personas were selected and the segment coverage.
3. Discover the project's real verification surface from its files and current environment:
   - For a web app, use the already-running local/staging URL if available and inspect it with the existing browser/screenshot capability.
   - For a CLI, library, API, or desktop project, use its documented commands and exercise the relevant user flow or interface.
   - Reuse existing test scripts, fixtures, browser helpers, and project conventions. Do not create a new runner when an existing one can do the job.
4. Execute each persona's scenario. Record the action sequence, observed result, hesitation/confusion, and whether the issue reproduced. Do not claim a persona tested something that was not actually exercised.
5. Capture evidence for concrete findings: screenshot, test output, request/response, stack trace, or exact file/line. Keep evidence tied to the persona and scenario.
6. Separate:
   - reproducible code defects;
   - UX friction that is not necessarily a defect;
   - product decisions or missing capabilities;
   - environment/test-data failures.
7. Rank findings by severity × frequency, identify the smallest root-cause fix, and recommend regression coverage. Do not show invented savings, performance, or success claims.

## Report format

Return:

```markdown
## BestQA Report — <N> personas × <area> — <date>

### Artifact
- PERSONAS.md: <absolute path>
- Pool size: <count>

### Personas run
| ID | Role | Segment | Device/context | Result |
|----|------|---------|----------------|--------|

### Findings
1. **[P0/P1/P2] <short title>** — <frequency>/<N>; severity and user impact.
   - Evidence: <path, URL, test, or exact output>
   - Classification: defect / UX friction / product decision / environment
   - Likely root cause: <file/symbol or honest unknown>
   - Recommended next action: <smallest useful action>

### Verification
- Commands/scenarios actually run: <list>
- Pass/fail and blockers: <honest result>
```

## Guardrails

- Never hardcode a project path, framework, port, persona source, or command.
- Never start a server blindly; first check project instructions and whether the app is already running. If a server is required but unavailable, report the blocker unless the user explicitly asked to start it.
- Never create or overwrite `PERSONAS.md` without explicit human approval for the inferred audience and proposed pool. A user correction must be incorporated before writing.
- Never overwrite an existing `PERSONAS.md` or project files unrelated to the requested test.
- Never treat identical mock data as evidence of persona preference, and never confuse a persona's opinion with a reproducible defect.
- Never fabricate screenshots, test output, URLs, file paths, or completed runs.
- If the project has no usable interface or test entry point, you may still complete the audience proposal and, after approval, create `PERSONAS.md`; explain the testing limitation and stop with a concrete next action.

## Completion checklist

- [ ] Project root and instructions located.
- [ ] Typical user inferred from actual code/routes/tests and presented to the human.
- [ ] Human explicitly confirmed or corrected the audience and pool before any first write.
- [ ] `PERSONAS.md` was created only after approval, or an existing file was read; its absolute path is reported.
- [ ] Pool contains at least 50 distinct entries.
- [ ] N personas selected with stated coverage.
- [ ] Real scenarios exercised or the blocker documented.
- [ ] Findings backed by evidence and classified correctly.
- [ ] Report includes commands, results, and remaining blockers.
