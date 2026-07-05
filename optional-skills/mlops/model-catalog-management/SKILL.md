---
name: model-catalog-management
description: Use when adding/updating models in Hermes _PROVIDER_MODELS catalog or generating model tables for documentation.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [models, catalog, provider, model-selection, verification]
    related_skills: [llm-routing-and-model-selection, hermes-agent-skill-authoring]
---

# Model Catalog Management Skill

## Overview

Documents the workflow for managing Hermes's static model catalog (`_PROVIDER_MODELS` in `hermes_cli/models.py`) and provides a verification script to generate model tables for documentation. **No write automation** — provider ID formats vary too much for safe automated edits. Edit manually, verify with the script.

## When to Use

- Adding a new model to a provider's curated list in `_PROVIDER_MODELS`
- Verifying the desktop model picker will show a model
- Generating a Markdown/CSV model table for docs or sharing
- Understanding provider ID format quirks (NVIDIA `nvidia/`, DeepSeek `deepseek-ai/`, etc.)

Don't use for:
- Live model discovery (use provider plugins / `models.dev` cache)
- Automated model addition (too many provider-specific quirks)
- Runtime model selection (that's `llm-routing-and-model-selection`)

## Architecture Context

```
hermes_cli/models.py:_PROVIDER_MODELS  ← desktop model picker source (static, curated)
scripts/build_model_catalog.py         → website manifest (reads _PROVIDER_MODELS + plugins)
plugins/model-providers/<name>/        → emerging canonical source (live, auto-discovered)
```

The desktop model picker (`apps/desktop/src/components/model-picker.tsx`) calls `model.options` RPC → `build_models_payload()` → `list_authenticated_providers()` which reads `_PROVIDER_MODELS`. This is the **current source of truth for the desktop picker**.

Provider plugins are the future canonical source but don't yet drive the desktop picker.

## Provider ID Format Quirks

| Provider | Prefix Pattern | Example |
|----------|----------------|---------|
| NVIDIA | `nvidia/` + `third-party/` | `nvidia/nemotron-3-ultra-550b-a55b`, `deepseek-ai/deepseek-v4-pro` |
| DeepSeek | `deepseek-ai/` | `deepseek-ai/deepseek-v4-pro` |
| OpenRouter | `provider/model` | `openai/gpt-5.5`, `google/gemini-3-pro-preview` |
| Neuralwatt | bare model name | `qwen3.5-397b`, `glm-5.2` |
| xAI | bare model name | `grok-4.3`, `grok-build-0.1` |
| Moonshot/Kimi | bare or `moonshotai/` | `kimi-k2.6`, `moonshotai/kimi-k2.6` |
|

Always verify by reading the provider's section in `_PROVIDER_MODELS` before adding.

## Manual Edit Workflow

1. **Open** `hermes_cli/models.py` and locate the provider's list in `_PROVIDER_MODELS`
2. **Add** the model ID in the correct position (alphabetical or priority order per provider)
3. **Verify** with the script below or:
   ```bash
   python3 -c "
   from hermes_cli.models import _PROVIDER_MODELS
   print(_PROVIDER_MODELS['nvidia'])
   "
   ```
4. **Restart** Hermes Desktop / TUI for the picker to pick up changes

## Verification Script: print_model_table.py

Located at `optional-skills/mlops/model-catalog-management/scripts/print_model_table.py`.

```bash
# All providers, Markdown table
python3 print_model_table.py

# Single provider
python3 print_model_table.py --provider nvidia

# CSV output
python3 print_model_table.py --provider nvidia --format csv

# JSON output
python3 print_model_table.py --provider nvidia --format json

# WITH CAPABILITIES (reasoning, tools, vision, context window, max output, family) from models.dev
python3 print_model_table.py --provider nvidia --with-caps --format md
python3 print_model_table.py --provider nvidia --with-caps --format csv
```

Output columns: `Provider | Model ID | Notes` (notes = aliases, tier, free/paid, etc.)

With `--with-caps`: adds `Reasoning | Tools | Vision | Context | Max Out | Family`

## Common Pitfalls

1. **Wrong prefix** — NVIDIA third-party models use `deepseek-ai/` not `deepseek/`. Check existing entries.
2. **Stale desktop picker** — Must restart the desktop app after editing `_PROVIDER_MODELS`.
3. **Provider not authenticated** — Desktop picker only shows providers with valid API keys in `.env` (`NVIDIA_API_KEY`, etc.).
4. **Plugin vs static divergence** — If a model exists in the provider plugin but not `_PROVIDER_MODELS`, it won't appear in the desktop picker.

## Verification Checklist

- [ ] Model ID matches provider's exact format (copy from existing entry)
- [ ] Script output shows the model in the provider's list
- [ ] Desktop model picker shows the model after restart
- [ ] If free tier model, verify `:free` suffix convention (OpenRouter/NVIDIA)

## One-Shot Recipes

### Add NVIDIA model (e.g., new Nemotron)
```bash
# 1. Edit hermes_cli/models.py, find "nvidia" section (~line 280)
# 2. Add: "nvidia/nemotron-4-ultra-XXX",  # new model
# 3. Verify:
python3 optional-skills/mlops/model-catalog-management/scripts/print_model_table.py --provider nvidia
# 4. Restart Hermes Desktop
```

### Generate docs table for all providers
```bash
python3 optional-skills/mlops/model-catalog-management/scripts/print_model_table.py --format md > model-catalog.md
```

### Verify a model is in the catalog
```bash
python3 -c "
from hermes_cli.models import _PROVIDER_MODELS
for prov, models in _PROVIDER_MODELS.items():
    for m in models:
        if 'nemotron' in m.lower():
            print(f'{prov}: {m}')
"
```