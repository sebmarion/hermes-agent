#!/usr/bin/env python3
"""Print model catalog table from _PROVIDER_MODELS with capabilities + cost + routing from models.dev.

Usage:
    python3 print_model_table.py                    # all providers, markdown
    python3 print_model_table.py --provider nvidia  # single provider
    python3 print_model_table.py --format csv       # CSV output
    python3 print_model_table.py --format json      # JSON output
    python3 print_model_table.py --with-caps        # include capabilities (reasoning, tools, vision, context, cost)
    python3 print_model_table.py --routing          # show routing/escalation matrix
    python3 print_model_table.py --provider nvidia --with-caps --with-cost --format md
"""

import argparse
import json
import sys
from pathlib import Path

# Add hermes-agent to path for import
HERMES_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(HERMES_ROOT))

try:
    from hermes_cli.models import _PROVIDER_MODELS
except ImportError as e:
    print(f"Error importing _PROVIDER_MODELS: {e}", file=sys.stderr)
    print("Run from hermes-agent root or ensure hermes_cli is on PYTHONPATH", file=sys.stderr)
    sys.exit(1)

# Try to import models.dev capabilities
try:
    from agent.models_dev import get_model_capabilities, PROVIDER_TO_MODELS_DEV
    MODELS_DEV_AVAILABLE = True
except ImportError:
    MODELS_DEV_AVAILABLE = False


# ─── Provider Metadata ──────────────────────────────────────────────────────
# Maps Hermes provider_id → display name, aliases, cost_model, routing_role
PROVIDER_META = {
    "nvidia": {
        "name": "NVIDIA NIM",
        "aliases": {"nemotron": "nvidia/nemotron-3-ultra-550b-a55b", "deepseek-v4": "deepseek-ai/deepseek-v4-pro", "step": "stepfun-ai/step-3.7-flash"},
        "cost_model": "usd_per_1m",
        "routing_role": "primary",
    },
    "neuralwatt": {
        "name": "Neuralwatt",
        "aliases": {"qwen": "qwen3.5-397b", "glm52": "glm-5.2", "code": "qwen3.5-397b", "fast": "kimi", "kimi": "kimi"},
        "cost_model": "energy_units",
        "routing_role": "fallback",
    },
    "openai-codex": {
        "name": "Codex CLI (OAuth)",
        "aliases": {},
        "cost_model": "usd_per_1m",
        "routing_role": "backup",
    },
    "xai": {"name": "xAI", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
    "xai-oauth": {"name": "xAI OAuth", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
    "openrouter": {"name": "OpenRouter", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "fallback"},
    "anthropic": {"name": "Anthropic", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
    "gemini": {"name": "Google Gemini", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
    "zai": {"name": "Z.ai GLM", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "escalation"},
    "moonshot": {"name": "Moonshot Kimi", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
    "kimi-coding": {"name": "Kimi Coding", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
    "stepfun": {"name": "StepFun", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
    "minimax": {"name": "MiniMax", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
    "deepseek": {"name": "DeepSeek Direct", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
    "copilot": {"name": "GitHub Copilot", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
    "gmi": {"name": "GMI", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
    "nous": {"name": "Nous", "aliases": {}, "cost_model": "usd_per_1m", "routing_role": "none"},
}


# ─── Cost Models ────────────────────────────────────────────────────────────
# USD per 1M tokens (input, output) — from models.dev cost field where available
# Energy units for Neuralwatt: 1 energy_unit ≈ 1 kWh equivalent compute
MODEL_COSTS_USD = {
    # NVIDIA (from models.dev)
    "nvidia/nemotron-3-ultra-550b-a55b": (0.50, 2.50),
    "nvidia/nemotron-3-super-120b-a12b": (0.20, 0.80),
    "nvidia/nemotron-3-nano-30b-a3b": (0.05, 0.15),
    "deepseek-ai/deepseek-v4-pro": (0.27, 1.10),
    "deepseek-ai/deepseek-v4-flash": (0.05, 0.15),
    "moonshotai/kimi-k2.6": (0.15, 0.60),
    "qwen/qwen3.5-397b-a17b": (0.20, 0.80),
    "stepfun-ai/step-3.7-flash": (0.08, 0.30),
    # Neuralwatt models — energy units (compute equivalent)
    "qwen3.5-397b": (120, 400),      # energy units per 1M tokens
    "glm-5.2": (150, 500),
    "kimi": (100, 350),
    "qwen3.6-35b-fast": (30, 100),
    # xAI
    "grok-4.3": (0.30, 1.20),
    "grok-build-0.1": (0.50, 2.00),
    # OpenAI (Codex)
    "gpt-5.5": (2.50, 10.00),
    "gpt-5.4": (1.25, 5.00),
    "gpt-5.3-codex": (1.00, 4.00),
    # DeepSeek Direct
    "deepseek-v4-pro": (0.27, 1.10),
    "deepseek-v4-flash": (0.05, 0.15),
    "deepseek-chat": (0.14, 0.28),
    "deepseek-reasoner": (0.55, 2.20),
}


ENERGY_UNIT_TO_USD = 0.12  # 1 energy unit ≈ $0.12 (rough GPU-hour equivalent)


# ─── Routing Rules ──────────────────────────────────────────────────────────
# Defines the escalation chain per task type
ROUTING_RULES = {
    "default": ["nvidia", "neuralwatt", "openai-codex", "openrouter"],
    "cheap": ["nvidia", "neuralwatt"],
    "expensive": ["nvidia", "zai", "neuralwatt"],
    "reasoning": ["nvidia", "zai", "deepseek", "neuralwatt"],
    "coding": ["nvidia", "neuralwatt", "openai-codex", "deepseek"],
    "vision": ["nvidia", "xai", "openai-codex", "neuralwatt"],
    "long_context": ["nvidia", "xai", "moonshot", "neuralwatt"],
    "fallback": ["openrouter", "neuralwatt", "openai-codex"],
}


def _map_provider_to_models_dev(provider_id: str) -> str | None:
    """Map Hermes provider ID to models.dev provider ID."""
    if MODELS_DEV_AVAILABLE:
        return PROVIDER_TO_MODELS_DEV.get(provider_id)
    return None


def _get_capabilities(provider_id: str, model_id: str) -> dict | None:
    """Fetch capabilities from models.dev if available."""
    if not MODELS_DEV_AVAILABLE:
        return None

    mdev_provider = _map_provider_to_models_dev(provider_id)
    if not mdev_provider:
        return None

    clean_model = model_id[:-5] if model_id.endswith(":free") else model_id

    caps = get_model_capabilities(mdev_provider, clean_model)
    if caps is None:
        return None

    return {
        "supports_tools": caps.supports_tools,
        "supports_vision": caps.supports_vision,
        "supports_reasoning": caps.supports_reasoning,
        "context_window": caps.context_window,
        "max_output_tokens": caps.max_output_tokens,
        "model_family": caps.model_family,
    }


def _get_cost(provider_id: str, model_id: str) -> tuple[float, float] | None:
    """Get (input_cost, output_cost) per 1M tokens."""
    clean_model = model_id[:-5] if model_id.endswith(":free") else model_id

    # Direct lookup
    if clean_model in MODEL_COSTS_USD:
        return MODEL_COSTS_USD[clean_model]

    # Try provider-specific defaults
    meta = PROVIDER_META.get(provider_id, {})
    if meta.get("cost_model") == "energy_units":
        # Neuralwatt: convert energy → USD estimate
        energy = MODEL_COSTS_USD.get(clean_model)
        if energy:
            return (energy[0] * ENERGY_UNIT_TO_USD, energy[1] * ENERGY_UNIT_TO_USD)

    return None


def _format_cost(provider_id: str, model_id: str) -> str:
    """Format cost as human-readable string."""
    cost = _get_cost(provider_id, model_id)
    if not cost:
        return "unknown"
    meta = PROVIDER_META.get(provider_id, {})
    if meta.get("cost_model") == "energy_units":
        energy = MODEL_COSTS_USD.get(model_id[:-5] if model_id.endswith(":free") else model_id, (0, 0))
        return f"${cost[0]:.4f}/${cost[1]:.4f} per 1M (~{energy[0]}/{energy[1]} EU)"
    return f"${cost[0]:.2f}/${cost[1]:.2f} per 1M"


def _get_routing_role(provider_id: str) -> str:
    return PROVIDER_META.get(provider_id, {}).get("routing_role", "none")


def format_model_row(provider_id: str, model_id: str, include_caps: bool = False, include_cost: bool = False) -> dict:
    """Format a single model row with metadata."""
    meta = PROVIDER_META.get(provider_id, {"name": provider_id, "aliases": {}})

    is_free = model_id.endswith(":free")
    clean_id = model_id[:-5] if is_free else model_id

    alias = ""
    for a, m in meta["aliases"].items():
        if m in model_id or clean_id.endswith(m.split("/")[-1]):
            alias = a
            break

    tier = "free" if is_free else ("flagship" if any(x in model_id.lower() for x in ["ultra", "opus", "max", "pro"]) else "standard")

    row = {
        "provider": meta["name"],
        "provider_id": provider_id,
        "model_id": clean_id,
        "raw_model_id": model_id,
        "alias": alias,
        "tier": tier,
        "free": is_free,
        "routing_role": _get_routing_role(provider_id),
    }

    if include_caps:
        caps = _get_capabilities(provider_id, model_id)
        if caps:
            row.update({
                "reasoning": caps["supports_reasoning"],
                "tools": caps["supports_tools"],
                "vision": caps["supports_vision"],
                "context_window": caps["context_window"],
                "max_output": caps["max_output_tokens"],
                "family": caps["model_family"],
            })
        else:
            row.update({
                "reasoning": None, "tools": None, "vision": None,
                "context_window": None, "max_output": None, "family": None,
            })

    if include_cost:
        cost = _get_cost(provider_id, model_id)
        meta = PROVIDER_META.get(provider_id, {})
        if cost:
            row["cost_input_per_1m"] = cost[0]
            row["cost_output_per_1m"] = cost[1]
            row["cost_display"] = _format_cost(provider_id, model_id)
            row["cost_model"] = meta.get("cost_model", "usd_per_1m")
        else:
            row["cost_input_per_1m"] = None
            row["cost_output_per_1m"] = None
            row["cost_display"] = "unknown"
            row["cost_model"] = meta.get("cost_model", "usd_per_1m")

    return row


def print_markdown(rows: list[dict], provider_filter: str = None, include_caps: bool = False, include_cost: bool = False):
    if provider_filter:
        rows = [r for r in rows if r["provider_id"] == provider_filter]

    if not rows:
        # Try to fetch from models.dev directly for providers not in _PROVIDER_MODELS (e.g., neuralwatt)
        if provider_filter and MODELS_DEV_AVAILABLE:
            print(f"\n## {PROVIDER_META.get(provider_filter, {'name': provider_filter})['name']} (from models.dev)")
            mdev_provider = PROVIDER_TO_MODELS_DEV.get(provider_filter)
            if mdev_provider:
                from agent.models_dev import fetch_models_dev
                data = fetch_models_dev()
                mdev_data = data.get(mdev_provider, {})
                models = mdev_data.get("models", {})
                if models:
                    for model_name, model_data in sorted(models.items()):
                        caps = get_model_capabilities(mdev_provider, model_name)
                        if caps:
                            print(f"| `{model_name}` | | standard | fallback | {'✓' if caps.supports_reasoning else ''} | {'✓' if caps.supports_tools else ''} | {'✓' if caps.supports_vision else ''} | {caps.context_window:,} | {caps.max_output_tokens:,} | {caps.model_family} |")
                    return
        print(f"No models found for provider: {provider_filter}")
        return

    from collections import defaultdict
    by_provider = defaultdict(list)
    for r in rows:
        by_provider[r["provider"]].append(r)

    for provider_name, models in sorted(by_provider.items()):
        print(f"\n## {provider_name}")
        if include_caps and include_cost:
            print("| Model ID | Alias | Tier | Role | Reasoning | Tools | Vision | Context | Max Out | Cost (in/out per 1M) | Family |")
            print("|----------|-------|------|------|-----------|-------|--------|---------|---------|----------------------|--------|")
            for m in models:
                alias = f"`{m['alias']}`" if m['alias'] else ""
                tier = m['tier']
                role = m.get('routing_role', 'none')
                reasoning = "✓" if m.get('reasoning') else ""
                tools = "✓" if m.get('tools') else ""
                vision = "✓" if m.get('vision') else ""
                ctx = f"{m['context_window']:,}" if m.get('context_window') else ""
                max_out = f"{m['max_output']:,}" if m.get('max_output') else ""
                cost = m.get('cost_display', 'unknown')
                family = m.get('family', '') or ""
                print(f"| `{m['model_id']}` | {alias} | {tier} | {role} | {reasoning} | {tools} | {vision} | {ctx} | {max_out} | {cost} | {family} |")
        elif include_caps:
            print("| Model ID | Alias | Tier | Role | Reasoning | Tools | Vision | Context | Max Out | Family |")
            print("|----------|-------|------|------|-----------|-------|--------|---------|---------|--------|")
            for m in models:
                alias = f"`{m['alias']}`" if m['alias'] else ""
                tier = m['tier']
                role = m.get('routing_role', 'none')
                reasoning = "✓" if m.get('reasoning') else ""
                tools = "✓" if m.get('tools') else ""
                vision = "✓" if m.get('vision') else ""
                ctx = f"{m['context_window']:,}" if m.get('context_window') else ""
                max_out = f"{m['max_output']:,}" if m.get('max_output') else ""
                family = m.get('family', '') or ""
                print(f"| `{m['model_id']}` | {alias} | {tier} | {role} | {reasoning} | {tools} | {vision} | {ctx} | {max_out} | {family} |")
        elif include_cost:
            print("| Model ID | Alias | Tier | Role | Cost (in/out per 1M) |")
            print("|----------|-------|------|------|----------------------|")
            for m in models:
                alias = f"`{m['alias']}`" if m['alias'] else ""
                tier = m['tier']
                role = m.get('routing_role', 'none')
                cost = m.get('cost_display', 'unknown')
                print(f"| `{m['model_id']}` | {alias} | {tier} | {role} | {cost} |")
        else:
            print("| Model ID | Alias | Tier | Role |")
            print("|----------|-------|------|------|")
            for m in models:
                alias = f"`{m['alias']}`" if m['alias'] else ""
                tier = m['tier']
                role = m.get('routing_role', 'none')
                print(f"| `{m['model_id']}` | {alias} | {tier} | {role} |")


def print_routing_matrix():
    """Print the routing/escalation matrix."""
    print("\n# Routing / Escalation Matrix\n")
    print("| Task Type | Primary → Fallback → Escalation → Backup |")
    print("|-----------|------------------------------------------|")
    for task, chain in ROUTING_RULES.items():
        chain_str = " → ".join([f"`{p}` ({PROVIDER_META.get(p, {}).get('name', p)})" for p in chain])
        print(f"| {task} | {chain_str} |")

    print("\n# Cost Models\n")
    print("| Provider | Cost Model | Notes |")
    print("|----------|------------|-------|")
    for pid, meta in PROVIDER_META.items():
        cost_model = meta.get("cost_model", "usd_per_1m")
        role = meta.get("routing_role", "none")
        name = meta["name"]
        note = f"Routing: {role}"
        if cost_model == "energy_units":
            note += f" | 1 EU ≈ ${ENERGY_UNIT_TO_USD:.2f}"
        print(f"| {name} | {cost_model} | {note} |")


def print_csv(rows: list[dict], provider_filter: str = None, include_caps: bool = False, include_cost: bool = False):
    import csv
    if provider_filter:
        rows = [r for r in rows if r["provider_id"] == provider_filter]

    writer = csv.writer(sys.stdout)
    headers = ["Provider", "Provider ID", "Model ID", "Raw Model ID", "Alias", "Tier", "Free", "Routing Role"]
    if include_caps:
        headers += ["Reasoning", "Tools", "Vision", "Context Window", "Max Output", "Family"]
    if include_cost:
        headers += ["Cost Input/1M", "Cost Output/1M", "Cost Display", "Cost Model"]
    writer.writerow(headers)
    for r in rows:
        row = [r["provider"], r["provider_id"], r["model_id"], r["raw_model_id"], r["alias"], r["tier"], r["free"], r.get("routing_role", "none")]
        if include_caps:
            row += [r.get("reasoning", ""), r.get("tools", ""), r.get("vision", ""), r.get("context_window", ""), r.get("max_output", ""), r.get("family", "")]
        if include_cost:
            row += [r.get("cost_input_per_1m", ""), r.get("cost_output_per_1m", ""), r.get("cost_display", ""), r.get("cost_model", "")]
        writer.writerow(row)


def print_json(rows: list[dict], provider_filter: str = None, include_caps: bool = False, include_cost: bool = False):
    if provider_filter:
        rows = [r for r in rows if r["provider_id"] == provider_filter]
    json.dump(rows, sys.stdout, indent=2)
    print()


def main():
    parser = argparse.ArgumentParser(description="Print model catalog table from _PROVIDER_MODELS with capabilities, cost, routing from models.dev")
    parser.add_argument("--provider", "-p", help="Filter to single provider (e.g., nvidia, neuralwatt)")
    parser.add_argument("--format", "-f", choices=["md", "csv", "json"], default="md", help="Output format")
    parser.add_argument("--with-caps", "-c", action="store_true", help="Include capabilities from models.dev (reasoning, tools, vision, context, cost)")
    parser.add_argument("--with-cost", action="store_true", help="Include cost estimates (USD or energy units)")
    parser.add_argument("--routing", action="store_true", help="Show routing/escalation matrix")
    args = parser.parse_args()

    if args.routing:
        print_routing_matrix()
        return

    # Build rows
    rows = []
    for provider_id, models in _PROVIDER_MODELS.items():
        for model_id in models:
            rows.append(format_model_row(provider_id, model_id, include_caps=args.with_caps, include_cost=args.with_cost))

    # Output
    if args.format == "md":
        print_markdown(rows, args.provider, args.with_caps, args.with_cost)
    elif args.format == "csv":
        print_csv(rows, args.provider, args.with_caps, args.with_cost)
    elif args.format == "json":
        print_json(rows, args.provider, args.with_caps, args.with_cost)


if __name__ == "__main__":
    main()