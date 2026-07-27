# Model Routing Transaction Hardening

## Problem

A plain interactive `/model` selection currently persists by default. The CLI
persists `model.default` and, when the provider changes, `model.provider` as
separate writes. It does not reconcile provider-owned endpoint fields such as
`model.base_url`, `model.api_mode`, or inline endpoint credentials.

This allows a valid session switch to create an invalid global hybrid. The
observed failure switched `custom / glm-5.2` to
`openai-codex / gpt-5.6-sol` while retaining NeuralWatt's base URL. WebUI then
sent Codex traffic to NeuralWatt and reported HTTP 404 as "Model not found."

## Goals

- Make unqualified `/model` commands and picker choices session-only.
- Require `--global` for persistent model changes.
- Persist a global model route in one config write.
- Reconcile provider-owned endpoint fields whenever the provider changes.
- Reuse one route-assignment policy across CLI and dashboard paths.
- Preserve same-provider custom endpoints when the user only changes models.
- Add regression coverage for managed-provider, custom-provider, and flat
  legacy model config shapes.

## Non-goals

- Change per-conversation runtime switching or prompt-cache behavior.
- Redesign provider discovery, aliases, credentials, or fallback routing.
- Automatically rewrite existing configs beyond the route being explicitly
  assigned.
- Add new configuration keys or environment variables.

## Considered Approaches

### 1. Change only the persistence default

Flip `resolve_persist_behavior()` to return `False` when no explicit override
is configured.

This prevents most accidental global changes but leaves `/model --global`
capable of producing the same hybrid route.

### 2. Patch the CLI persistence block in place

Keep the dashboard helper where it is and duplicate its endpoint reconciliation
inside `cli.py`.

This is small initially but preserves two subtly different implementations of
the same routing invariant.

### 3. Shared assignment helper plus session-only default

Move the existing dashboard route-assignment policy into the shared config
module. Both the dashboard and CLI call it. Persistent CLI switches load the
config, apply the complete route assignment, and save once.

This is the selected approach because it fixes the observed bug class without
adding a new abstraction or changing runtime switching.

## Design

### Persistence policy

`resolve_persist_behavior()` resolves in this order:

1. `--session` returns session-only.
2. `--global` returns persistent.
3. An explicit `model.persist_switch_by_default` value is honored for backward
   compatibility.
4. If the setting is absent or malformed, the built-in default is session-only.

CLI help and success text must describe the new default accurately.

### Atomic route assignment

The existing dashboard `_apply_main_model_assignment()` logic becomes a shared
config helper. It accepts the current model mapping and the resolved target
provider, model, base URL, API key, and API mode.

The helper applies these invariants:

- Provider and model are always assigned together.
- A supplied base URL, API key, or API mode replaces the old value.
- On provider change, any endpoint-owned field not supplied by the new route is
  removed.
- On a same-provider model change, an existing custom endpoint remains intact
  unless the switch explicitly supplies a replacement.
- A model-specific `context_length` override is removed.
- Legacy non-mapping `model` values are replaced by a fresh mapping.

The CLI uses `load_config()`, applies the helper, and calls `save_config()` once.
It must not issue multiple `save_config_value()` calls for a route.

### Confirmation

The existing `--global` syntax is the explicit persistence signal. No additional
modal is added: scripts and users already have an unambiguous opt-in, while the
success output states whether the switch was session-only or saved globally.

### Failure behavior

Runtime switching still occurs before persistence. If the in-place runtime
switch fails, no config write occurs. If the config write fails, the CLI reports
the failure instead of claiming the route was saved. Existing atomic YAML write
and managed-config protections remain authoritative.

## Testing

Focused tests will prove:

- No flags default to session-only when the config key is absent or malformed.
- Explicit `True`, explicit `False`, `--global`, and `--session` retain their
  documented precedence.
- The interactive picker uses the same persistence resolution.
- A global custom/NeuralWatt-to-Codex switch does not retain the old base URL,
  API mode, or inline credential.
- A global switch to a custom endpoint persists its complete route.
- A same-provider model change preserves a pre-existing custom endpoint when
  the resolved switch does not replace it.
- CLI persistent switching performs one bulk config save and no dotted-key
  writes.
- Existing dashboard assignment tests continue to pass against the shared
  helper.

After focused tests, run the repository test wrapper for all touched test files
and the broader Hermes CLI model-switch suite.

## Rollback

The change is code-only. Reverting the commit restores the previous persistence
default and CLI write path. No migration is required.
