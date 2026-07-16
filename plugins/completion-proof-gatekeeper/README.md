# Completion Proof Gatekeeper

This is a report-only Hermes plugin for the Radar candidate **Done Means
Proven**. It inspects the final response text and appends a warning when it
contains a strong completion claim without same-response command, API, or
read-back evidence.

It does not run commands, block turns, inspect private workspace files, or
persist response text. Explicit blockers such as “couldn't verify” are left
unchanged. Enable it only after reviewing the warning style:

```yaml
plugins:
  enabled:
    - completion-proof-gatekeeper
```

The plugin is intentionally report-only for the first rollout. Promotion to a
hard stop should require measured false-positive and false-negative evidence.
