#!/usr/bin/env python3
"""Prototype: SOTA model landscape evaluator for BestPlan lanes.

Fetches coding benchmark data (Aider) + model availability/pricing (OpenRouter),
cross-references against configured providers in config.yaml, and reports
ranked recommendations for updating bestplan.lanes.

Usage:
    .venv/bin/python3 scripts/sota_landscape.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import requests

AIDER_URL = "https://aider.chat/docs/leaderboards/"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def fetch_aider_leaderboard() -> list[dict[str, Any]]:
    """Scrape the Aider coding leaderboard HTML table."""
    r = requests.get(AIDER_URL, timeout=15)
    r.raise_for_status()
    html = r.text

    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)
    if not tables:
        return []

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tables[0], re.DOTALL)
    entries: list[dict[str, Any]] = []
    headers: list[str] = []

    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if not cells:
            continue

        # First row = headers
        if not headers:
            headers = [c.lower().replace(" ", "_") for c in cells]
            continue

        # Data rows: skip rows that start with ▶ and contain expanded details
        if cells[0] == "▶" or cells[0] == "":
            # Clean entries: model, percent_correct, cost, command, edit_format
            if len(cells) >= 6:
                model = cells[1]
                percent_str = cells[2].replace("%", "")
                try:
                    percent = float(percent_str)
                except ValueError:
                    continue
                cost_str = cells[3].replace("$", "")
                try:
                    cost = float(cost_str)
                except ValueError:
                    cost = 0.0
                entries.append({
                    "model": model,
                    "percent_correct": percent,
                    "cost": cost,
                    "command": cells[4] if len(cells) > 4 else "",
                    "edit_format_pct": cells[5] if len(cells) > 5 else "",
                })
    return entries


def fetch_openrouter_models() -> list[dict[str, Any]]:
    """Fetch the OpenRouter models catalog (availability + pricing)."""
    r = requests.get(OPENROUTER_MODELS_URL, timeout=15)
    r.raise_for_status()
    models = r.json().get("data", [])
    results: list[dict[str, Any]] = []
    for m in models:
        pricing = m.get("pricing", {})
        results.append({
            "id": m.get("id", ""),
            "name": m.get("name", ""),
            "context_length": m.get("context_length", 0),
            "pricing_prompt": float(pricing.get("prompt", 0) or 0),
            "pricing_completion": float(pricing.get("completion", 0) or 0),
            "reasoning": m.get("reasoning", {}),
            "supported_parameters": m.get("supported_parameters", []),
        })
    return results


def load_current_bestplan_lanes() -> list[dict[str, Any]]:
    """Load the current bestplan.lanes from config.yaml."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from hermes_cli.config import load_config

        config = load_config()
        return config.get("bestplan", {}).get("lanes", [])
    except Exception:
        return []


def parse_aider_model_name(raw: str) -> str:
    """Normalize Aider model names for cross-referencing."""
    # "gpt-5 (high)" → "gpt-5", "claude-3.5-sonnet" → "claude-3.5-sonnet"
    return re.sub(r"\s*\([^)]*\)\s*", "", raw).strip().lower()


def match_aider_to_openrouter(
    aider: list[dict[str, Any]],
    or_models: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cross-reference Aider leaderboard entries to OpenRouter models."""
    or_by_name: dict[str, dict[str, Any]] = {}
    for m in or_models:
        or_by_name[m["id"].lower()] = m
        or_by_name[m["name"].lower()] = m
        # Also index by short name (after the /)
        if "/" in m["id"]:
            short = m["id"].split("/", 1)[1].lower()
            or_by_name[short] = m

    matched: list[dict[str, Any]] = []
    for entry in aider:
        normalized = parse_aider_model_name(entry["model"])
        or_match = or_by_name.get(normalized)
        if not or_match:
            # Try partial match
            for key, val in or_by_name.items():
                if normalized in key or key in normalized:
                    or_match = val
                    break
        matched.append({
            **entry,
            "normalized": normalized,
            "openrouter_id": or_match["id"] if or_match else None,
            "context_length": or_match["context_length"] if or_match else 0,
            "pricing_prompt": or_match["pricing_prompt"] if or_match else None,
            "reasoning_capable": bool(or_match and or_match.get("reasoning", {}).get("default_enabled")) if or_match else False,
            "available_on_openrouter": or_match is not None,
        })
    return matched


def main() -> int:
    print("Fetching Aider coding leaderboard...")
    aider = fetch_aider_leaderboard()
    print(f"  {len(aider)} entries")

    print("Fetching OpenRouter model catalog...")
    or_models = fetch_openrouter_models()
    print(f"  {len(or_models)} models")

    print("Cross-referencing...")
    matched = match_aider_to_openrouter(aider, or_models)

    print("\n" + "=" * 100)
    print("SOTA Coding Model Landscape (Aider benchmark, ranked by % correct)")
    print("=" * 100)
    print(f"{'Rank':<6} {'Model':<35} {'% Correct':<12} {'Cost':<10} {'OR Available':<15} {'Reasoning':<10}")
    print(f"{'─'*6} {'─'*35} {'─'*12} {'─'*10} {'─'*15} {'─'*10}")

    for i, m in enumerate(matched[:20], 1):
        print(
            f"{i:<6} {m['model']:<35} {m['percent_correct']:<12.1f} "
            f"${m['cost']:<9.2f} {'✓' if m['available_on_openrouter'] else '✗':<15} "
            f"{'✓' if m['reasoning_capable'] else '✗':<10}"
        )

    # Compare against current config
    print("\n" + "=" * 100)
    print("Current BestPlan Lane Configuration")
    print("=" * 100)
    lanes = load_current_bestplan_lanes()
    if not lanes:
        print("  (could not load config)")
    else:
        for lane in lanes:
            print(f"  {lane.get('name','?'):<8} {lane.get('model','?'):<20} {lane.get('provider','?')}")
            # Check if current model is in the leaderboard
            current = lane.get("model", "").lower()
            for m in matched:
                if current in m.get("normalized", ""):
                    rank = matched.index(m) + 1
                    print(f"    → Ranked #{rank} on Aider ({m['percent_correct']:.1f}% correct, ${m['cost']:.2f})")
                    break

    # Recommendations
    print("\n" + "=" * 100)
    print("Recommendations")
    print("=" * 100)
    if not lanes:
        print("  (no current config to compare against)")
    else:
        for lane in lanes:
            current_model = lane.get("model", "").lower()
            lane_name = lane.get("name", "?")
            current_rank = None
            for m in matched:
                if current_model in m.get("normalized", ""):
                    current_rank = matched.index(m) + 1
                    break
            if current_rank and current_rank > 3:
                top = matched[0]
                print(f"  {lane_name} lane ({current_model}, rank #{current_rank}):")
                print(f"    → Consider {top['model']} (rank #1, {top['percent_correct']:.1f}%, "
                      f"OR: {'✓' if top['available_on_openrouter'] else '✗'})")
            elif current_rank:
                print(f"  {lane_name} lane ({current_model}): rank #{current_rank} — within top tier, no change needed")
            else:
                print(f"  {lane_name} lane ({current_model}): not found in Aider leaderboard")

    print("\n" + "=" * 100)
    print(f"Data sources: Aider ({len(aider)} entries), OpenRouter ({len(or_models)} models)")
    print(f"Cross-referenced: {sum(1 for m in matched if m['available_on_openrouter'])}/{len(matched)} matched to OpenRouter")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
