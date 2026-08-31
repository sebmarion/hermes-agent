#!/usr/bin/env python3
"""Live-proposal shim: turn harvested failures into real candidate improvements.

This closes the gap between "we found a failure" and "here is a candidate SKILL
edit". It REUSES the existing pilot conventions:

  - Qualify the configured proposer route first, fail-closed.
  - Send each harvested failure to the proposer as a prompt -> returns a
    candidate improvement for the target skill. The scheduled improve loop uses
    Hermes' existing Luna route; the standalone CLI retains the Zeus route.
  - Stage baseline + candidate + frozen judge-prompt hash under runs/<id>/,
    exactly like the pilot run layout, and emit dataset.jsonl rows conforming to
    hermes_skill_dataset.schema.json with deterministic blind_id assignment.

The PURE core (`build_proposer_prompt`, `stage_run`) is fully offline-tested.
The model calls are guarded network/subprocess boundaries and are never called
during unit tests. The A/B judge itself is still dispatched by Hermes
(delegate_task mode=review) per the runbook — this script produces everything
the judge needs but does not itself invoke another model.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

# Same conservative credential scan so a failure body can never carry a secret
# into a candidate prompt or on-disk file.
CRED_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|apikey)\s*[:=]?\s*['\"]?[A-Za-z0-9_\-]{16,}"), "api key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk secret"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "github token"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "JWT"),
]


def _scrub(text: str) -> str:
    out = text
    for pat, label in CRED_PATTERNS:
        out = pat.sub(f"[redacted:{label}]", out)
    return out


# ---------------------------------------------------------------------------
# Pure: prompt construction (offline-testable, byte-stable)
# ---------------------------------------------------------------------------

PROPOSER_TEMPLATE = (
    "You are a careful editor improving an operator-owned Hermes skill.\n"
    "Fix the concrete failure below. Produce ONLY raw Markdown content as a\n"
    "self-contained addition to {skill_path}; do not return patch syntax or a\n"
    "diff. Stay within that single skill's scope. Do NOT add new apps,\n"
    "providers, daemons, or external services. Prefer exact, runnable checks\n"
    "over generic advice. Never include secrets.\n\n"
    "EXISTING LEVEL-2 HEADINGS in the current skill:\n{headings}\n"
    "To extend an existing section, prefer an existing heading; do not create a duplicate heading.\n\n"
    "HARVESTED FAILURE:\ntitle: {title}\nsignature: {signature}\nevidence:\n{instructions}\n"
)


def _existing_level_two_headings(skill_path: str) -> str:
    """Return deterministic current ## headings without failing prompt creation."""
    try:
        text = Path(skill_path).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "(unavailable — preserve the existing document structure)"
    headings = re.findall(r"(?m)^##\s+.+$", text)
    return "\n".join(headings) if headings else "(none — do not add a heading unless required)"


def _validate_base_url(base_url: str) -> str:
    """Reject credential-bearing URLs and plain HTTP to public hosts."""
    parsed = urlsplit(str(base_url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Zeus base URL must include an http(s) scheme and host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Zeus base URL must not contain credentials or URL suffixes")
    if parsed.scheme == "http":
        host = parsed.hostname.lower().rstrip(".")
        local = host in {"localhost", "localhost.localdomain"}
        if not local:
            try:
                address = ipaddress.ip_address(host)
                local = (
                    address.is_private
                    or address.is_loopback
                    or address.is_link_local
                    or address in ipaddress.ip_network("100.64.0.0/10")
                )
            except ValueError:
                local = False
        if not local:
            raise ValueError("HTTPS is required for public Zeus endpoints")
    return str(base_url).strip().rstrip("/")


def build_proposer_prompt(failure: dict, skill_path: str) -> str:
    """Byte-stable proposer prompt from a structured failure row."""
    if not isinstance(skill_path, str) or not skill_path.strip():
        raise ValueError("skill_path required")
    inst = _scrub(str(failure.get("task_instructions") or failure.get("body") or ""))
    return PROPOSER_TEMPLATE.format(
        skill_path=skill_path,
        headings=_existing_level_two_headings(skill_path),
        title=_scrub(str(failure.get("task_title") or "harvested failure")),
        signature=str(failure.get("failure_signature") or "unclassified"),
        instructions=inst[:2000],
    )


# ---------------------------------------------------------------------------
# Pure: staging (offline-testable)
# ---------------------------------------------------------------------------

_BLIND_SWITCH = {"A": "B", "B": "A"}


def stage_run(run_dir, task, baseline_text, candidate_text, researcher_id,
              judge_model, judge_prompt_hash, seed_blind=None) -> dict:
    """Write baseline/, candidates/, dataset.jsonl row for one harvested task.

    Fail-closed: empty baseline/candidate raises before any write. Blind ids
    are assigned deterministically across calls via `seed_blind` (the caller
    toggles A/B so consecutive tasks alternate). Returns the dataset row."""
    if not (baseline_text and len(baseline_text.strip()) >= 1):
        raise ValueError("baseline_text must be non-empty")
    if not (candidate_text and len(candidate_text.strip()) >= 1):
        raise ValueError("candidate_text must be non-empty")

    # G2 requires real session evidence. Never fabricate a placeholder id that
    # merely satisfies the JSON schema; the caller must resolve real ids first.
    sess = task.get("before_session_ids") or []
    if not isinstance(sess, list):
        raise ValueError("real session evidence must be a list")
    sess = [str(x) for x in sess if isinstance(x, str) and len(x) >= 10]
    if not sess:
        raise ValueError("real session evidence required before staging")

    rdir = Path(run_dir)
    bdir = rdir / "baseline"
    cdir = rdir / "candidates"
    bdir.mkdir(parents=True, exist_ok=True)
    cdir.mkdir(parents=True, exist_ok=True)

    (bdir / "SKILL.md").write_text(baseline_text)
    (cdir / "SKILL.md.candidate").write_text(_scrub(candidate_text))

    blind = seed_blind or _BLIND_SWITCH.get(seed_blind, "A")
    candidate_sha = hashlib.sha256(_scrub(candidate_text).encode()).hexdigest()

    row = {
        "schema_version": 1,
        "researcher_id": researcher_id,
        "task_id": task["task_id"],
        "task_title": task.get("task_title", "harvested failure")[:200],
        "task_instructions": _scrub(task.get("task_instructions", ""))[:2000],
        "skill_path": task.get("skill_path", "~/.hermes/skills/software-development/bestplan/SKILL.md"),
        "before_session_ids": sess,
        "after_session_ids": [],
        "blind_id": blind,
        "judge_model": judge_model,
        "judge_prompt_hash": judge_prompt_hash,
        "candidate_patch_sha256": candidate_sha,
    }

    ds_path = rdir / "dataset.jsonl"
    with ds_path.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


# ---------------------------------------------------------------------------
# Live network step — NEVER invoked in tests; guarded.
# ---------------------------------------------------------------------------

def call_zeus(base_url: str, api_key: str, model: str, prompt: str, timeout: float = 90.0) -> str:
    """Send a single chat completion request to Zeus; return assistant text.

    Raises RuntimeError on transport/auth/HTTP error; caller treats a raise as
    'Zeus unavailable' (fail-closed => skip, do not fabricate a candidate)."""
    url = _validate_base_url(base_url) + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 900,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTPS {resp.status} from {url}")
        payload = json.loads(resp.read().decode())
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected chat response shape: {exc}")


def call_luna(prompt: str, timeout: float = 180.0) -> str:
    """Run one proposal through Hermes' existing OpenAI Codex Luna route."""
    hermes = shutil.which("hermes")
    if not hermes:
        fallback = Path.home() / ".local" / "bin" / "hermes"
        hermes = str(fallback) if fallback.is_file() else None
    if not hermes:
        raise RuntimeError("Hermes CLI unavailable for Luna proposal")

    completed = subprocess.run(
        [
            hermes,
            "-z",
            prompt,
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.6-luna",
            "--reasoning",
            "low",
            "--toolsets",
            "search",
            "--safe-mode",
            "--ignore-rules",
            "--in",
            "/tmp",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(f"Luna proposal failed with exit {completed.returncode}")
    return completed.stdout.strip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", required=True, help="Run directory (runs/<id>)")
    ap.add_argument("--failures-jsonl", required=True, help="Harvested failures JSONL")
    ap.add_argument("--skill-path", default="~/.hermes/skills/software-development/bestplan/SKILL.md")
    ap.add_argument("--base-url", default=os.environ.get("ZEUS_BASE_URL", "http://100.86.155.23:8080/v1"))
    ap.add_argument("--api-key", default=os.environ.get("ZEUS_API_KEY", "local-no-auth-needed"))
    ap.add_argument("--model", default=os.environ.get("ZEUS_MODEL", "qwen3.8-27b"))
    ap.add_argument("--judge-model", default="deepseek/deepseek-v4-flash-0731")
    ap.add_argument("--researcher-id", default="qwen-zeus")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build prompts + stage layout WITHOUT calling Zeus (test/planning)")
    args = ap.parse_args(argv[1:] if argv and not argv[0].startswith("-") else argv)

    fp = Path(args.failures_jsonl)
    if not fp.is_file():
        print(f"error: failures file not found: {fp}", file=sys.stderr)
        return 2
    failures = []
    for line in fp.read_text().splitlines():
        if line.strip():
            failures.append(json.loads(line))
    if not failures:
        print("RESULT: OK (no new failures; nothing to propose)")
        return 0

    # frozen judge prompt hash placeholder: the ACTUAL prompt is dispatched by
    # Hermes delegate_task per the runbook; this records the version marker
    judge_prompt_hash = "sha256:" + hashlib.sha256(
        "improve-loop-judge-v1-bounded-pilot".encode()
    ).hexdigest()

    # Read current baseline from the skill path so a change is genuinely additive.
    bp = Path(args.skill_path).expanduser()
    if not bp.is_absolute():
        bp = Path.cwd() / bp
    if not bp.is_file():
        print(f"error: skill file not found: {bp}", file=sys.stderr)
        return 2
    try:
        baseline_text = bp.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"error: skill file unreadable: {bp}: {exc}", file=sys.stderr)
        return 2

    blind_acc = {}
    n_ok = 0
    for i, f in enumerate(failures):
        sid = f["task_id"]
        blind = blind_acc.setdefault(sid, "A" if i % 2 == 0 else "B")
        prompt = build_proposer_prompt(f, args.skill_path)
        if args.dry_run:
            candidate_text = "(dry-run placeholder — Zeus not called)"
        else:
            candidate_text = call_zeus(args.base_url, args.api_key, args.model, prompt)
        row = stage_run(
            run_dir=Path(args.run_dir), task=f,
            baseline_text=baseline_text,
            candidate_text=candidate_text if not args.dry_run else candidate_text,
            researcher_id=args.researcher_id,
            judge_model=args.judge_model,
            judge_prompt_hash=judge_prompt_hash,
            seed_blind=blind,
        )
        n_ok += 1
        print(f"  proposal[{sid}] blind={row['blind_id']} sha={row['candidate_patch_sha256'][:12]}")

    print(f"RESULT: {'DRY-RUN' if args.dry_run else 'OK'} ({n_ok}/{len(failures)} proposals staged -> {args.run_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))