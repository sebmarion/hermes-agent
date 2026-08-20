#!/usr/bin/env python3
"""Apply an approved skill candidate to live ~/.hermes safely.

Contract with the improve loop: the orchestrator approves an edit (all gates
green, skill-path class) and hands us (live_skills_dir, skill_relpath,
candidate_path, state_dir). Steps:

  1. Snapshot the CURRENT live file to <target>.bak (only the first time;
     an existing .bak stays untouched as the older known-good).
  2. Atomically copy candidate → live target (tmp file + os.replace).
  3. Record the change in the apply manifest so a torn or later-deemed-bad
     change can be restored from the .bak deterministically.

`apply()` takes `_copy(src, dst)` as an injectable seam so tests can fault
mid-write and prove rollback; production uses shutil.copy2 via os.replace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pipeline_state as ps


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=dst.name + ".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            shutil.copyfileobj(open(src, "rb"), fh)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply(live: Path, target: str, candidate: Path, state_dir: Path, _copy=None) -> dict:
    """Back up the live file (first time), place the candidate, record manifest.

    Returns {"ok": True, "bak": str, "applied": str, "candidate_sha": str}.
    Raises on any failure — callers must treat a raise as 'do not advance the
    watermark / do not report success'."""
    if not target or target.startswith(("/", "..")) or "\x00" in target:
        raise ValueError(f"unsafe target path: {target!r}")
    live_path = Path(live) / target
    candidate_path = Path(candidate)
    if not candidate_path.is_file():
        raise FileNotFoundError(f"candidate not found: {candidate_path}")
    if not live_path.parent.is_dir():
        raise FileNotFoundError(f"live target dir missing: {live_path.parent}")

    cpy = _copy or _atomic_copy
    bak_path = None
    if live_path.is_file():
        bak_path = live_path.with_name(live_path.name + ".bak")
        if not bak_path.is_file():
            cpy(live_path, bak_path)

    cpy(candidate_path, live_path)

    manifest = ps.read_manifest(state_dir) or {"targets": []}
    manifest["targets"].append(
        {
            "path": target,
            "target_abs": str(live_path),
            "bak": str(bak_path) if bak_path else None,
            "candidate_sha": _sha256(candidate_path),
        }
    )
    ps.write_manifest(state_dir, manifest)
    return {
        "ok": True,
        "bak": str(bak_path) if bak_path else None,
        "applied": str(live_path),
        "candidate_sha": _sha256(candidate_path),
    }


# ---------------------------------------------------------------------------
# rollback / recovery
# ---------------------------------------------------------------------------

def restore_from_bak(bak: Path, target: Path) -> None:
    """Restore target from its .bak (atomic). Raises if no backup exists."""
    bak = Path(bak)
    if not bak.is_file():
        raise FileNotFoundError(f"no backup to restore from: {bak}")
    _atomic_copy(bak, Path(target))


def recover_all(live: Path, state_dir: Path) -> None:
    """Restore every target recorded in the manifest from its .bak.

    Used by the operator (or a follow-up job) when an applied change is
    judged bad post-hoc. Idempotent: if nothing is in the manifest, no-op."""
    manifest = ps.read_manifest(state_dir)
    if not manifest:
        return
    for t in manifest.get("targets", []):
        bak = t.get("bak")
        target = t.get("target_abs")
        if bak and target and Path(bak).is_file() and Path(target).is_file():
            _atomic_copy(Path(bak), Path(target))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", required=True, help="Live skills dir (e.g. ~/.hermes/skills)")
    ap.add_argument("--target", required=True, help="Relative target path, e.g. research/bestplan/SKILL.md")
    ap.add_argument("--candidate", required=True, help="Path to the approved candidate file")
    ap.add_argument("--state-dir", required=True, help="State dir for the apply manifest")
    args = ap.parse_args(argv[1:] if argv and not argv[0].startswith("-") else argv)

    result = apply(
        Path(args.live), args.target, Path(args.candidate), Path(args.state_dir)
    )
    print(json.dumps(result, indent=2))
    print("RESULT: OK (applied with .bak + manifest)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))