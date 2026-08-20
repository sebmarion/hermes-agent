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
11. **Commit** — one local commit of allowed files only (see invariant 4). No push.

## Cleanup

- Remove the worktree with `git worktree remove <path>` after the run is done.
- The run dir stays outside git for Seb's review; never add it to a commit.

## Daily upstream sync — core stays upstream, edge functionality is conserved

`merge_upstream.py` is the daily pull/upgrade path. It does **not** run a blind
`git pull` or merge arbitrary fork code. It builds a temporary candidate from
`origin/main`, overlays only `optional-skills/` and `tests/skills/` paths that
are locally owned, verifies conservation, runs the skills suite, and only then
can apply/publish.

```text
origin/main
    ↓ clean temporary worktree
owned edge paths only  ←  local edge manifest/state
    ↓ tests/skills green
live checkout + sebmarion/main (apply/publish mode)
```

Safety contract:

- Core Hermes is never copied from the local fork into the candidate; the
  candidate core is byte-for-byte `origin/main`.
- First bootstrap conservatively classifies local edge differences as owned;
  subsequent runs use `~/.hermes/labs/bestplan-research/state/upstream-sync.json`
  to detect newly edited edge files and preserve them.
- A dirty live checkout halts. No stash, reset, or implicit conflict choice.
- Publishing uses `--force-with-lease` only after verifying the push URL is the
  `sebmarion/hermes-agent` fork; `origin` is fetch-only.
- A separate `flock` wrapper prevents overlapping daily runs. Success and halt
  summaries go through the redacting Telegram notifier.

The registered job invokes:

```bash
hermes cron create --name hermes-upstream-sync "30 4 * * *" \
  --no-agent --script upstream_merge_wrapper.py --deliver local
```

Manual preview (no live mutation):

```bash
.venv/bin/python optional-skills/research/darwinian-evolver/labs/scripts/merge_upstream.py \
  --repo /home/seb/projects/hermes-agent --state-dir ~/.hermes/labs/bestplan-research/state
```

The wrapper's apply/publish mode will safely halt while another checkout has
uncommitted work; it resumes on the next daily run after that work is committed
or discarded by its owner.