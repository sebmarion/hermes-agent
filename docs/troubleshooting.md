# Model routing troubleshooting

## HTTP 404 or "Model not found" after switching providers

A provider/model/endpoint mismatch can route a valid model name to the wrong
service. For example, `provider: openai-codex` combined with a stale custom
`model.base_url` can produce HTTP 404 or "Model not found" even though the
model and credentials are valid.

Inspect the active non-secret routing fields with:

```bash
hermes config show
```

Check `model.default`, `model.provider`, `model.base_url`, and
`model.api_mode` as one route. Do not paste API keys, OAuth tokens, cookies, or
auth files into logs or support requests.

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

Bare `/model <name>` switches and bare picker choices are safe and
session-only. Use `--session` as the explicit session-only override; it wins
over `--global` if both are present. Only `--global` saves a
provider/model/endpoint route to `config.yaml`.
