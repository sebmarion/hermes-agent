# Bounded Zeus Research Pilot — Runbook

This runbook is the **only** execution contract for the pilot. It extends the
existing `darwinian-evolver` skill; it does not create a new app, provider, or
deployment. Everything runs inside the isolated worktree on one local branch.

## Non-negotiable invariants

1. The primary agent (`zeus/qwen3.8-27b`) **proposes only**. It harvests session
   evidence and produces a candidate patch. It never scores itself.
2. All A/B judging is done by a **non-Zeus** reviewer reached through
   `delegate_task(mode="review")`. The judge prompt is frozen, versioned, and
   its `sha256` recorded in every dataset row.
3. Evidence lives **outside git**: `~/.hermes/labs/bestplan-research/runs/<run-id>/`.
4. Only the target `SKILL.md`, contracts, tests, runbook, and scripts may be
   committed. Run directories, transcripts, judge JSONL, scorecards, and raw
   diffs are **never** committed.
5. At most **one promotion commit** at the end, only after validation and OCR pass.
   Push is fast-forward-only, and the remote SHA must be read back into the report.

## Directory layout (run dir)

```
~/.hermes/labs/bestplan-research/runs/<run-id>/
  run.yaml                  # filled template from templates/research_loop.yaml
  dataset.jsonl             # validated by validate_hermes_skill_dataset.py
  before/                   # anonymized before-session excerpts (scrubbed)
  candidate.patch           # unified diff, sha256 = candidate_patch_sha256
  judges.jsonl              # blind A/B judge rows
  review_notes.md           # independent + GitNexus risk review
  scorecard.tsv             # produced by score_hermes_skill_run.py
```

## Gates (fail-closed; do not skip)

| # | Gate | Tool | On failure |
|---|------|------|-----------|
| G0 | Worktree clean, branch `feat/bestplan-research-lab` at main's HEAD | git | Stop; re-create worktree |
| G1 | Dataset rows valid | `scripts/validate_hermes_skill_dataset.py` | Fix rows or stop |
| G2 | Every dataset row has a real session id resolvable via `session_search` | manual + tool | Discard the row |
| G3 | Judge prompt frozen; `judge_prompt_hash` matches what was sent | sha256sum | Abort run, re-blind |
| G4 | ≥1 valid judge row with verdict in {better, equal, worse} per candidate | scorecard | No decision; retry or stop |
| G5 | Independent (non-Zeus) review of diff scope/safety | `delegate_task(mode="review")` | Do not promote |
| G6 | GitNexus risk review of affected symbols | gitnexus impact/trace | Do not commit |
| G7 | Full repo validation green | `scripts/run_tests.sh` (full) | Fix before any commit |
| G8 | BestPlan validator + OCR receipt passed | `validate_bestplan.py` + live OCR plugin | Do not commit or push |

## Steps

1. **Preflight** — confirm G0; run baseline skill tests in the worktree.
2. **Harvest** (primary=Zeus) — `improve_cron_entry.py` reads completed
   conversations from Hermes' canonical `~/.hermes/state.db` through the
   read-only `SessionDB` adapter, preserving the real session ids and the
   highest message id as the watermark. Manual `session_search` remains the
   G2 evidence check for selected rows. Use `harvest_failures.py --sessions-json`
   only for offline fixtures; production uses `--db-path` or the canonical
   Hermes home by default.
3. **Candidate** — draft a single unified-diff patch to the target SKILL.md.
   Write it to `<run>/candidate.patch`; compute its sha256 → dataset row.
4. **Validate dataset** — run G1; every row must carry a resolvable session id (G2).
5. **Blind A/B** — assign `blind_id` {A,B}; send anonymized diff + frozen prompt to
   the non-Zeus reviewer via `delegate_task(mode="review")`; capture `judges.jsonl`
   and `judge_prompt_hash`.
6. **Score** — run `scripts/score_hermes_skill_run.py --judge-file judges.jsonl
   --out scorecard.tsv`. A blank/empty result is a stop, not a verdict.
7. **Independent review** — second non-Zeus review of scope + safety (G5).
8. **GitNexus risk review** — impact/trace over symbols the patch touches (G6).
9. **Full validation** — G7 in the worktree.
10. **Decision** — promote only on a *consistent* blind win **and** clean reviews.
    `equal` is a valid, acceptable outcome: it means "no regression", not "win".
11. **Promote** — validate the target, stage only the accepted SKILL.md, create one
    commit, push `HEAD:main` fast-forward-only, and verify `git ls-remote` matches
    the commit. Pre-existing staged work aborts promotion; unrelated unstaged work
    remains untouched.

### Live cron wiring (implemented)

The scheduler entry now executes the bounded chain for each newly harvested
failure:

```text
canonical state.db
  -> sanitized failure row
  -> Zeus proposer (qwen3.8-27b)
  -> append-only candidate materialization
  -> non-Zeus headless reviewer (gpt-5.6-sol via openai-codex)
  -> strict verdict/score validation + scorecard
  -> fail-closed decision
  -> atomic apply with state/backups + manifest
  -> BestPlan validator + OCR
  -> target-only commit + fast-forward push + remote read-back
```

The reviewer is launched with `hermes -z` in headless mode because the cron row
is `--no-agent`; it is explicitly pinned to the non-Zeus `openai-codex`
provider. Zeus output may be a fenced unified diff, but the pipeline extracts
only added lines and rejects deletions, path changes, malformed judge JSON,
secrets, low scores, and `worse` verdicts. A successful apply is recorded in
the report under `live_chain`; an unavailable optional X bookmark source still
reports a halt rather than claiming a completely green run. The scheduler
caps live candidates at three per run and persists remaining sanitized rows in
`state/pending_failures.jsonl`, so a large backlog cannot overrun the cron
timeout or disappear after watermark advancement.

The isolated smoke path is:

```bash
.venv/bin/python optional-skills/research/darwinian-evolver/labs/scripts/improve_cron_entry.py \
  --state-dir <tmp>/state --db-path <tmp>/state.db \
  --live-skills <tmp>/skills \
  --skill-path <tmp>/skills/software-development/bestplan/SKILL.md
```

## Cleanup

- Remove the worktree with `git worktree remove <path>` after the run is done.
- The run dir stays outside git for Seb's review; never add it to a commit.

## Daily upstream sync — core stays upstream, edge functionality is conserved

`merge_upstream.py` is the daily pull/upgrade engine. It does **not** run a
blind `git pull` or merge arbitrary fork code. `upstream_merge_wrapper.py`
creates a disposable **separate clone** (independent refs/index), then the
engine applies the saved upstream delta there, conserves owned paths, runs the
bounded relevant test gate, and publishes only a fast-forward candidate.

```text
canonical main (read-only source, clean + pinned)
    ↓ separate disposable clone
origin delta + owned-path conservation
    ↓ bounded relevant tests / optional Zeus-Qwen conflict recovery
normal fast-forward push to sebmarion/main
    ↓ guarded `git merge --ff-only`
canonical main
    ↓ root verifier only after wrapper success
Hermes gateway reload + PID/SHA verification
```

Safety contract:

- Core Hermes is never copied from the local fork into the candidate; the
  candidate starts from fork HEAD and applies only the recorded
  anchor-to-upstream delta, while explicitly owned runtime/edge paths remain
  byte-identical to fork HEAD.
- First bootstrap conservatively classifies local edge differences as owned;
  subsequent runs use `~/.hermes/labs/bestplan-research/state/upstream-sync.json`
  to detect newly edited edge files and preserve them.
- Qwen, Git delta application, and pytest operate only inside the disposable
  clone. An OOM/kill may leave a stale run directory, but cannot alter canonical
  refs, index, or worktree; the next run prunes old disposable directories.
- A dirty or moved canonical checkout halts. No stash, reset, force checkout,
  implicit conflict choice, or force push is permitted.
- Publishing is a normal fast-forward push after exact remote-SHA and fork-URL
  verification. Canonical promotion is `git merge --ff-only` after rechecking
  its pinned HEAD and cleanliness.
- A separate `flock` prevents overlapping runs. Recovery receipts never
  overwrite sync state. Success/halt summaries use the redacting Telegram
  notifier.

The root-owned `hermes-upstream-merge.timer` invokes the wrapper as user `seb`.
Only its fixed `ExecStartPost` verifier runs as root, and only after wrapper
success, to reload **Hermes** and verify the new PID/SHA. The old internal
`hermes-upstream-sync` cron job remains paused to prevent duplicate updaters.

Manual preview (no live mutation):

```bash
.venv/bin/python optional-skills/research/darwinian-evolver/labs/scripts/merge_upstream.py \
  --repo /home/seb/projects/hermes-agent --state-dir ~/.hermes/labs/bestplan-research/state
```

The wrapper halts whenever canonical is dirty, its HEAD moves during a run, or
the fork remote differs without a matching published-SHA receipt. After the
checkout owner resolves that state, the next isolated run may retry safely.