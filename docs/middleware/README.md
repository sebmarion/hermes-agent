# Hermes Middleware

Hermes middleware is the behavior-changing companion to observer hooks.
Observer hooks report what happened. Middleware can change what happens by
rewriting a request before execution or by wrapping the execution callback
itself.

This contract is intentionally backend-neutral. A plugin can use it for local
policy, request shaping, tracing, adaptive routing, cache control, sandbox
selection, or handoff to runtimes such as NeMo Relay without changing Hermes'
planner, model provider adapters, tool registry, memory, or CLI UX.

With middleware enabled, plugins can:

- Rewrite LLM provider request kwargs before Hermes calls the provider.
- Rewrite tool arguments before guardrails, approval checks, hooks, and tool
  execution see them.
- Wrap the actual LLM execution callback while preserving Hermes retry,
  streaming, interrupt, and hook behavior.
- Wrap the actual tool execution callback. Middleware that calls `next_call`
  reaches the final required-policy gate, approvals, handler, post-tool hooks,
  and tool-result transformation. Middleware that returns without calling
  `next_call` intentionally replaces that ordinary dispatch path.

## Contract

Plugins register middleware from `register(ctx)`:

```python
def register(ctx):
    ctx.register_middleware("llm_request", on_llm_request)
    ctx.register_middleware("llm_execution", on_llm_execution)
    ctx.register_middleware("tool_request", on_tool_request)
    ctx.register_middleware("tool_execution", on_tool_execution)
```

Every middleware callback receives:

- `telemetry_schema_version`: currently `hermes.observer.v1`
- `middleware_schema_version`: currently `hermes.middleware.v1`
- Runtime context such as `session_id`, `task_id`, `turn_id`,
  `api_request_id`, `provider`, `model`, `api_mode`, `tool_name`, and
  `tool_call_id` when applicable.

Supported middleware kinds:

| Kind | Payload | Return shape | Purpose |
| --- | --- | --- | --- |
| `llm_request` | `request`, `original_request` | `{"request": {...}}` | Replace effective provider kwargs before provider execution. |
| `tool_request` | `tool_name`, `args`, `original_args` | `{"args": {...}}` | Replace effective tool args before hooks, guardrails, approvals, and execution. |
| `llm_execution` | `request`, `original_request`, `next_call` | Any provider response | Wrap or replace the actual provider call. |
| `tool_execution` | `tool_name`, `args`, `original_args`, `next_call` | Any tool result | Wrap or replace the actual tool call. |

Request middleware can return optional trace fields:

```python
return {
    "request": updated_request,
    "source": "my-plugin",
    "reason": "selected fallback model",
}
```

Hermes stores those trace entries in later observer hook payloads as
`middleware_trace`.

Execution middleware receives a `next_call` callback. Call it to continue the
chain:

```python
def on_tool_execution(**kwargs):
    result = kwargs["next_call"](kwargs["args"])
    return result
```

If multiple plugins register the same execution middleware kind, Hermes runs
them as a nested chain in registration order. Middleware failures are fail-open:
Hermes logs a warning and continues with the next middleware or the base
runtime path.

## Required Tool-Dispatch Policies

Observer hooks and middleware are not fail-closed security boundaries:

- Observer-hook exceptions are caught and execution continues. The legacy
  `pre_tool_call` block return remains a compatibility feature, but a missing,
  unloaded, or failing hook cannot deny execution reliably.
- Request middleware is behavior-changing but fail-open on callback failure.
- Execution middleware is trusted host code. It can wrap ordinary dispatch by
  calling `next_call(...)`, or replace ordinary dispatch completely by
  returning without calling it.

Use a required `tool_dispatch` policy when an operator needs an explicit,
provider-neutral deny-by-default gate immediately before an ordinary tool
handler. Required policies are opt-in: the plugin declares and registers the
policy, and the operator separately enables the plugin and requires the policy.

### Manifest and registration

Declare the supported policy in `plugin.yaml`:

```yaml
name: workspace-governor
version: 1.0.0
policies:
  - tool_dispatch
```

Register one callback from `register(ctx)`:

```python
from hermes_cli.tool_policy import TOOL_DISPATCH_CONFORMANCE_TOOL_NAME


def register(ctx):
    ctx.register_policy(
        "tool_dispatch",
        on_tool_dispatch,
        timeout_ms=2_000,
    )


def on_tool_dispatch(payload):
    # policy-status uses a reserved, handlerless call to prove that the loaded
    # callback sees the final args and the same prepared cwd as the terminal.
    if payload["tool_name"] == TOOL_DISPATCH_CONFORMANCE_TOOL_NAME:
        return {
            "action": "allow",
            "policy_binding": payload["policy_binding"],
        }

    if should_deny(payload):
        return {"action": "block", "message": "Workspace policy denied this call."}
    return {
        "action": "allow",
        "policy_binding": payload["policy_binding"],
    }
```

The timeout must be an integer from `1` through `10_000` milliseconds. The
default is `2_000` milliseconds. An allow is valid only when the return value
is exactly an `action: allow` mapping with the unchanged `policy_binding`. A
deny is valid only when it is exactly an `action: block` mapping with a
non-empty `message`.

The conformance tool name has no registry handler and grants no execution
capability. A policy may apply additional checks to the probe, but
`ordinaryDispatchCovered` remains false unless the callback explicitly allows
it with the correct binding.

### Operator configuration and CLI

`require-policy` does not enable a plugin. Enable and require it explicitly:

```bash
hermes plugins enable workspace-governor
hermes plugins require-policy workspace-governor tool_dispatch
hermes plugins policy-status --json
```

The resulting configuration is:

```yaml
plugins:
  enabled:
    - workspace-governor
  required_policies:
    workspace-governor:
      - tool_dispatch
```

Remove only that requirement with:

```bash
hermes plugins unrequire-policy workspace-governor tool_dispatch
```

Malformed required-policy configuration is an error. At runtime, a configured
policy that cannot be loaded or evaluated blocks ordinary dispatch; it does not
silently become an empty allow-list.

`policy-status --json` reports configuration, install/enable/load/registration
state, timeout, quarantine/error fields, and these capability facts:

- `executionMiddleware`: enabled execution middleware grouped by its
  registration-time plugin key, with `reachedNextCall` showing what the live
  conformance probe actually observed. Hermes does not infer ownership or
  safety from callback names, modules, or source text.
- `ordinaryDispatchCovered`: true only when every configured callback is loaded
  and a live self-test proves final-argument and prepared-cwd equality at the
  policy and terminal boundaries.
- `replacementExecutionAudited`: false unless an explicit conformance artifact
  covers every enabled replacement-capable execution middleware. Hermes does
  not currently accept such an artifact, so the field remains false.

### Callback payload

The callback receives one JSON-shaped mapping:

| Field | Meaning |
| --- | --- |
| `tool_name` | The ordinary Hermes tool being dispatched. |
| `original_args` | Audit snapshot captured before request middleware. It is not part of the authorization binding. |
| `effective_args` | Final arguments after request and execution middleware rewrites. |
| `task_id`, `session_id`, `turn_id`, `tool_call_id` | Real runtime identities. A missing identity stays an empty string; Hermes does not invent one. |
| `effective_cwd` | Prepared local working directory, or `null` when no authoritative local mapping exists. |
| `effective_cwd_source` | `explicit_workdir`, `live_terminal`, `task_override`, `terminal_config`, `process_cwd`, or `remote_unmapped`. |
| `effective_cwd_authoritative` | Whether `effective_cwd` is authoritative for the local handler. Remote/container-only paths are false unless a tested local mapping exists. |
| `policy_binding` | Opaque SHA-256 binding over the final execution shape, identities, and prepared cwd metadata. |

Hermes binds the prepared cwd in a process-local context only while the allowed
handler runs. The policy-approved cwd and handler-observed cwd are therefore
the same value even when another session changes shared terminal state. Child
processes and delegated agents prepare and authorize their own local calls;
they do not inherit a parent's dispatch authority.

### Fail-closed results and quarantine

A policy denial or enforcement failure returns a stable tool result:

```json
{
  "status": "blocked",
  "error_type": "required_policy_block",
  "policy": "tool_dispatch",
  "policy_code": "required_policy_timeout",
  "message": "Required policy callback timed out."
}
```

`policy_code` is stable and safe to automate against. Codes distinguish an
explicit block, malformed/non-explicit decision, binding failure, invalid
configuration, missing/disabled/unloaded plugin, missing registration,
callback error, timeout, executor saturation, and quarantine. Callback
exception text is not returned to the model, and block messages are capped at
1,000 UTF-8 bytes.

Callback error, timeout, malformed response, non-explicit action, or binding
mismatch quarantines that `(session_id, plugin, policy)` tuple. Later calls in
the same session block immediately without submitting more callback work. An
explicit policy block does not quarantine the callback. Required plugin keys
are evaluated in sorted order, so the first stable block code is deterministic.

### Coverage limits

- The gate covers ordinary Hermes tool dispatch. It does not sandbox arbitrary
  Python inside a plugin, and requiring a policy does not make an untrusted
  plugin safe.
- Human commands entered directly in a terminal are outside Hermes tool
  dispatch and are not governed.
- Direct registry calls are rejected when a required policy is configured and
  no matching one-use authorization context is present.
- `execute_code` is governed once at its outer registry call. Operations inside
  the Python sandbox are not individually visible. A policy must deny the
  outer call whenever that sandbox could reach a protected workspace.
- Opaque interpreter/shell commands are presented as their outer tool and
  arguments. The provider-neutral Hermes layer reports coverage facts; it does
  not invent product-specific command classifications.
- Replacement execution middleware that never calls `next_call` remains
  trusted host code outside ordinary dispatch. Do not claim it is covered from
  `ordinaryDispatchCovered` alone; inspect `executionMiddleware` and
  `replacementExecutionAudited` together.

## Execution Order

### LLM Calls

For each provider request, Hermes applies middleware in this order:

1. Build provider kwargs from the current conversation.
2. Apply `llm_request` middleware.
3. Emit `pre_api_request` observer hooks with the effective request.
4. Run provider execution through `llm_execution` middleware.
5. Emit `post_api_request` or `api_request_error` observer hooks.

Request middleware sees the full provider kwargs, including `messages` or
Responses API `input`, model settings, tool definitions, stream options, and
provider-specific options. Execution middleware receives the same effective
request plus `next_call`.

### Tool Calls

For each tool call, Hermes applies middleware in this order:

1. Parse and coerce model-provided tool arguments.
2. Apply `tool_request` middleware.
3. Run the normal Hermes pre-execution path against the effective arguments:
   tool availability checks, legacy observer block directives, and guardrails.
4. Run tool execution through `tool_execution` middleware.
5. If middleware reaches `next_call`, prepare the authoritative runtime cwd and
   run every configured required `tool_dispatch` policy against the final
   arguments.
6. After explicit allow, run mutation-capable approval/checkpoint/progress
   setup and invoke the ordinary handler.
7. Emit `post_tool_call` observer hooks. A required-policy block emits exactly
   one blocked observer event with the structured policy metadata.
8. Apply `transform_tool_result` hooks before the result is appended back into
   conversation context.

Tool request middleware runs before approval checks. Use it carefully: a
rewritten path, command, or URL is the value downstream policy will evaluate.

## Enablement

Middleware only runs for enabled plugins. For a bundled plugin:

```bash
hermes plugins enable <plugin-name>
```

For isolated local testing, use one `HERMES_HOME` for plugin enablement and the
agent run:

```bash
export HERMES_HOME=/tmp/hermes-middleware-test
mkdir -p "$HERMES_HOME"
hermes plugins enable <plugin-name>
hermes chat --query 'Reply exactly ok'
```

For source checkouts, prefer the source command so the runtime sees plugins and
middleware from the working tree:

```bash
uv sync
uv run hermes plugins enable <plugin-name>
uv run hermes chat --query 'Reply exactly ok'
```

## Generic Plugin Examples

The examples below are intentionally small. They show the middleware contract
shape without depending on NeMo Relay.

### LLM Request Middleware

This plugin tags provider requests and records a middleware trace entry:

```python
def register(ctx):
    ctx.register_middleware("llm_request", tag_llm_request)


def tag_llm_request(**kwargs):
    request = dict(kwargs["request"])
    extra_body = dict(request.get("extra_body") or {})
    extra_body.setdefault("metadata", {})["hermes_middleware_demo"] = True
    request["extra_body"] = extra_body
    return {
        "request": request,
        "source": "middleware-demo",
        "reason": "tagged provider request",
    }
```

The effective request is passed to `pre_api_request`, provider execution, and
`post_api_request`.

### Tool Request Middleware

This plugin constrains `terminal` calls to a known working directory:

```python
def register(ctx):
    ctx.register_middleware("tool_request", normalize_terminal_workdir)


def normalize_terminal_workdir(**kwargs):
    if kwargs.get("tool_name") != "terminal":
        return None
    args = dict(kwargs["args"])
    args.setdefault("workdir", "/tmp/hermes-middleware-demo")
    return {
        "args": args,
        "source": "middleware-demo",
        "reason": "defaulted terminal workdir",
    }
```

Because this runs before hooks and approvals, downstream telemetry and policy
observe the rewritten `workdir`.

### LLM Execution Middleware

This plugin wraps the provider call and preserves the raw provider response:

```python
import time


def register(ctx):
    ctx.register_middleware("llm_execution", time_llm_execution)


def time_llm_execution(**kwargs):
    started = time.monotonic()
    response = kwargs["next_call"](kwargs["request"])
    elapsed_ms = int((time.monotonic() - started) * 1000)
    print(f"llm_execution elapsed_ms={elapsed_ms}")
    return response
```

Return the same response shape Hermes expects from the provider adapter. Do not
wrap the response in a plugin-specific envelope unless the rest of the runtime
expects that envelope.

### Tool Execution Middleware

This plugin wraps tool execution while preserving the tool result:

```python
def register(ctx):
    ctx.register_middleware("tool_execution", annotate_tool_execution)


def annotate_tool_execution(**kwargs):
    result = kwargs["next_call"](kwargs["args"])
    # Metrics, logging, or external routing can happen here.
    return result
```

Execution middleware may call `next_call(modified_args)` to pass a changed
payload to later middleware and the base tool dispatcher.

Plugin-specific examples should live with the plugin that owns the behavior.
For NeMo Relay adaptive execution middleware, see
[`plugins/observability/nemo_relay/README.md`](../../plugins/observability/nemo_relay/README.md).

## Safety Notes

- Middleware should be deterministic for the same input unless it is explicitly
  routing to a dynamic external system.
- Request middleware should return complete replacement payloads, not partial
  patches.
- Execution middleware should call `next_call(...)` exactly once unless it is
  intentionally short-circuiting execution.
- If execution middleware raises before calling `next_call(...)`, Hermes treats
  that as middleware failure and continues with the remaining middleware chain
  and base execution.
- If execution middleware calls `next_call(...)` successfully and then raises
  during post-processing, Hermes preserves the downstream result and does not
  run the provider or tool a second time.
- If downstream provider or tool execution fails, middleware may let that error
  propagate or translate it deliberately. Hermes does not convert downstream
  failure into a successful `None` result.
- Tool request middleware runs before approvals. If it mutates file paths,
  commands, URLs, or arguments, the mutated values are what guardrails and
  approvals evaluate.
- Observer hooks remain the right place for read-only telemetry. Use middleware
  only when a plugin needs to alter or wrap behavior.
- Do not use a fail-open hook or middleware exception path as an enforcement
  boundary. Configure a required policy when missing/failing policy code must
  block the handler.
- Treat execution middleware that returns without `next_call` as trusted host
  code outside the ordinary dispatch gate.
- Required policies govern Hermes tool calls, not arbitrary plugin Python or
  commands a human runs directly in a terminal.
