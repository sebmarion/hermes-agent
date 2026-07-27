# Model routing troubleshooting

## HTTP 404 or "Model not found" after switching providers

A provider/model/endpoint mismatch can route a valid model name to the wrong
service. For example, `provider: openai-codex` combined with a stale custom
`model.base_url` can produce HTTP 404 or "Model not found" even though the
model and credentials are valid.

Inspect the persisted configured state with:

```bash
hermes config show
```

This command reports configuration saved in `config.yaml`; it does not show
classic CLI or gateway session overrides. Inspect only the Model section:
`model.provider`, `model.default`, `model.base_url`, and `model.api_mode`. Do not
paste or share the full output because it can contain unrelated local details.
Never share API keys, OAuth tokens, cookies, or auth files.

Check the current session model and route indicator separately in the active UI
or session. Compare that session state with the persisted Model section to
identify whether the mismatch is temporary or saved.

Recover by assigning a complete known-provider route explicitly:

```text
/model gpt-5.4 --provider openai-codex --global
```

Or correct the complete `model:` block in `config.yaml` so the model,
provider, endpoint, and API mode agree. For a custom OpenAI-compatible route,
the non-secret shape is:

```yaml
model:
  default: model-id
  provider: custom
  base_url: https://inference.example.invalid/v1
  api_mode: chat_completions
```

Successful global provider changes now update the route atomically and clear
endpoint fields left by the old provider. If the global save fails, Hermes
keeps the working session switch but reports that the global route was not
saved.

Bare `/model <name>` switches and bare picker choices are non-persistent and
session-only. Use `--session` as the explicit session-only override; it wins over
`--global` if both are present. Only `--global` saves a provider/model/endpoint
route to `config.yaml`.
