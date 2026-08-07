#!/usr/bin/env python3
"""Lightweight CSS visual-smell audit for product design review.

Usage:
  python scripts/css_visual_smell_audit.py path/to/file.css [...]

This is a heuristic guardrail, not a replacement for visual review.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

HEX_RE = re.compile(r"(?<![\w-])#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})(?![\w-])")
GRADIENT_RE = re.compile(r"(?:linear|radial|conic)-gradient\(", re.I)
SHADOW_RE = re.compile(r"(?:box-shadow|text-shadow)\s*:", re.I)
FILTER_RE = re.compile(r"(?:backdrop-filter|filter)\s*:", re.I)
WARNING_WORD_RE = re.compile(r"\b(?:warning|danger|error|destructive|yellow|gold|orange|red)\b", re.I)


def audit_file(path: Path) -> list[str]:
    text = path.read_text(errors="replace")
    warnings: list[str] = []

    hexes = HEX_RE.findall(text)
    gradients = GRADIENT_RE.findall(text)
    shadows = SHADOW_RE.findall(text)
    filters = FILTER_RE.findall(text)
    warning_words = WARNING_WORD_RE.findall(text)

    if len(set(hexes)) > 8:
        warnings.append(f"{path}: {len(set(hexes))} distinct raw hex colors; prefer design tokens")
    elif hexes:
        counts = Counter(hexes)
        common = ", ".join(f"{k}×{v}" for k, v in counts.most_common(5))
        warnings.append(f"{path}: raw hex colors present ({common}); verify token intent")

    if len(gradients) > 3:
        warnings.append(f"{path}: {len(gradients)} gradients; check for decorative excess")
    elif gradients:
        warnings.append(f"{path}: {len(gradients)} gradient(s); verify they support hierarchy")

    if len(shadows) > 8:
        warnings.append(f"{path}: {len(shadows)} shadow declarations; check glow/elevation overload")
    elif shadows:
        warnings.append(f"{path}: {len(shadows)} shadow declaration(s); verify elevation discipline")

    if filters:
        warnings.append(f"{path}: {len(filters)} filter/backdrop-filter declaration(s); check glass/blur excess")

    if warning_words:
        warnings.append(f"{path}: warning-like color/status words present; verify semantic use only")

    return warnings


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: css_visual_smell_audit.py <file.css> [...]")
        return 2

    all_warnings: list[str] = []
    for arg in argv[1:]:
        path = Path(arg)
        if not path.exists():
            all_warnings.append(f"{path}: missing")
            continue
        all_warnings.extend(audit_file(path))

    if not all_warnings:
        print("No obvious CSS visual-smell heuristics triggered.")
        return 0

    print("CSS visual-smell audit warnings:")
    for warning in all_warnings:
        print(f"- {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
