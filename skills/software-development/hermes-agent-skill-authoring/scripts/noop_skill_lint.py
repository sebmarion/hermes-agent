#!/usr/bin/env python3
"""Flag obvious no-op prose in Hermes SKILL.md files.

This is a deliberately conservative heuristic, not a semantic quality proof. It
catches standalone generic instructions that normally do not change agent
behavior. Concrete but weak guidance still requires human review.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


EXACT_NO_OPS = {
    "be accurate",
    "be careful",
    "be clear",
    "be concise",
    "be consistent",
    "be helpful",
    "be precise",
    "be robust",
    "be safe",
    "be thorough",
    "do a good job",
    "do it correctly",
    "do it properly",
    "ensure accuracy",
    "ensure correctness",
    "ensure high quality",
    "ensure quality",
    "follow best practices",
    "handle errors appropriately",
    "handle errors gracefully",
    "make it production ready",
    "use best practices",
    "use good judgment",
}

_LIST_PREFIX = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
_MARKDOWN_EDGE = re.compile(r"^[\s>*_`~]+|[\s*_`~]+$")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    text: str
    normalized: str


def _normalize(line: str) -> str:
    value = _LIST_PREFIX.sub("", line.strip())
    value = _MARKDOWN_EDGE.sub("", value)
    value = value.strip().lower()
    value = re.sub(r"[.!?;:]+$", "", value)
    return _SPACE.sub(" ", value).strip()


def lint_path(path: Path) -> list[Finding]:
    if path.is_dir():
        path = path / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(path)

    findings: list[Finding] = []
    in_frontmatter = False
    in_fence = False

    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()

        if line_no == 1 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue

        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("|"):
            continue

        normalized = _normalize(raw)
        if normalized in EXACT_NO_OPS:
            findings.append(Finding(path, line_no, raw.strip(), normalized))

    return findings


def _self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="noop-skill-lint-") as tmp:
        root = Path(tmp)
        clean = root / "clean.md"
        bad = root / "bad.md"
        clean.write_text(
            "---\nname: clean\ndescription: Use when testing.\n---\n"
            "# Clean\n\n- Run `pytest -q`; completion requires exit code 0.\n"
            "```text\nBe careful.\n```\n",
            encoding="utf-8",
        )
        bad.write_text(
            "---\nname: bad\ndescription: Use when testing.\n---\n"
            "# Bad\n\n- Be careful.\n- **Use best practices.**\n",
            encoding="utf-8",
        )
        clean_findings = lint_path(clean)
        bad_findings = lint_path(bad)
        if clean_findings or [f.normalized for f in bad_findings] != [
            "be careful",
            "use best practices",
        ]:
            print(
                "self-test failed: "
                f"clean={clean_findings!r} bad={bad_findings!r}",
                file=sys.stderr,
            )
            return 1
    print("noop_skill_lint self-test passed.")
    return 0


def _iter_findings(paths: Iterable[Path]) -> tuple[list[Finding], list[Path]]:
    findings: list[Finding] = []
    missing: list[Path] = []
    for path in paths:
        try:
            findings.extend(lint_path(path))
        except FileNotFoundError:
            missing.append(path)
    return findings, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Flag standalone generic no-op prose in SKILL.md files."
    )
    parser.add_argument("paths", nargs="*", type=Path, help="SKILL.md file or skill directory")
    parser.add_argument("--self-test", action="store_true", help="run built-in fixtures")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.paths:
        parser.error("provide at least one SKILL.md file or skill directory")

    findings, missing = _iter_findings(args.paths)
    for path in missing:
        print(f"missing SKILL.md: {path}", file=sys.stderr)
    for finding in findings:
        print(
            f"{finding.path}:{finding.line}: candidate no-op "
            f"[{finding.normalized}]: {finding.text}"
        )

    if missing:
        return 2
    if findings:
        print(f"Found {len(findings)} candidate no-op line(s).")
        return 1

    print("No candidate no-op lines found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
