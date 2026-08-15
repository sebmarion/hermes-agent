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
5. At most **one local commit** at the end, only if every gate passes. Never push.

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

## Steps

1. **Preflight** — confirm G0; run baseline skill tests in the worktree.
2. **Harvest** (primary=Zeus) — `session_search` for completed tasks touching the
   chosen `skill_path`. Select up to 5. Record session ids → `before_session_ids`.
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
11. **Commit** — one local commit of allowed files only (see invariant 4). No push.

## Cleanup

- Remove the worktree with `git worktree remove <path>` after the run is done.
- The run dir stays outside git for Seb's review; never add it to a commit.

---

# Autonomous Improve Loop — Scheduling Addendum

The improve loop reuses every gate above but runs **autonomously** (no human in
the approval path; safety = automatic rollback, not approval pings). Two
inputs feed it: session failures (harvest_failures; watermark-gated) and X
bookmarks (harvest_x_bookmarks; cheap pre-filter before any LLM spend).

## Ducted pipeline (scripts in `labs/scripts/`)

```
improve_cron_entry.py   <- scheduler invokes this (idempotent; always exit 0)
  -> harvest_failures.extract_failures()     watermark-gated, structured-only
  -> harvest_x_bookmarks.fetch_bookmarks()   read-only xurl; pre-filter
  -> run_improve_loop.decide()               deterministic apply policy
  -> apply_skill_candidate.apply()           .bak + manifest; auto-revert on fault
  -> ledger_rollup.write_weekly_report()     longitudinal net-positive
  -> notify_telegram.send()                  redacted {applied, halted, weekly}
```

## Cron registration (run ONCE, from a healthy live tree — after any
concurrent update/merge thread has finished; do not register while the live
CLI fails to import):

```bash
# improve loop: every 6h
hermes cron add improve-loop \
  --schedule "0 */6 * * *" \
  --command "python3 <repo>/optional-skills/research/darwinian-evolver/labs/scripts/improve_cron_entry.py --report-out ~/.hermes/labs/bestplan-research/state/latest.json"
# weekly net-positive rollup: Mondays 08:00
hermes cron add improve-weekly-rollup \
  --schedule "0 8 * * 1" \
  --command "python3 <repo>/optional-skills/research/darwinian-evolver/labs/scripts/ledger_rollup.py --ledger ~/.hermes/labs/bestplan-research/state/change-ledger.jsonl --out ~/.hermes/labs/bestplan-research/state/weekly-report.md"
```

Idempotency contract: a double-fire of either job is harmless — watermarks make
harvesting a no-op when nothing new exists, and all writes are atomic
(tmp+rename). Never run GitNexus index-writers in parallel with these jobs
(fleet contract: serialize index/registry mutations).

## Autonomy floor

- Skill-path changes that pass every gate (scorecard+replay+no-secrets) are
  applied automatically with `.bak` + manifest and roll back automatically on
  any fault.
- Core-path classes (config.yaml / providers / cron / auth) are NEVER mutated
  by this loop: they are parked + reported via Telegram instead. That is the
  deliberate floor that keeps "no human gate" true without risking a live
  service file.
