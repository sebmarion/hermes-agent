# Required Policy Runtime Recovery Design

## Problem

A long-lived Hermes process can discover plugins before an operator installs or
enables a required policy plugin. Required-policy configuration is read again at
dispatch time, but `PluginManager.discover_and_load()` treats its first scan as
permanent. The result is a false `required_policy_plugin_missing` block even
though the plugin is present and enabled on disk.

Hermes currently returns that block as a normal tool result and asks the model
what to do next. In the observed turn, Ornith made twelve model/tool rounds,
every tool was blocked before its handler ran, and the final prose guessed at
commands that do not exist. Host policy failures must not be delegated back to
the model.

## Scope

This change provides monotonic recovery for a newly installed or newly enabled
directory plugin in the same active `HERMES_HOME` and process. It also stops a
turn deterministically when required-policy enforcement itself is unavailable.

It does not hot-replace an already loaded plugin, hot-unload a disabled plugin,
reload pip entry points, or claim full cross-profile plugin isolation. Hermes'
general plugin registries include process-global tools, providers, platforms,
and module names; solving per-profile isolation for every plugin capability is a
separate architectural change. This recovery path therefore refuses plugins
that need those global registration surfaces and never rebuilds the global
plugin universe.

## Considered Approaches

1. Force full discovery before every dispatch. This repeatedly reloads hooks
   and can mutate process-global tools/providers/platforms after the model's
   tool schema was built.
2. Key complete plugin managers by `HERMES_HOME`. Manager-local hooks and
   policies would be separated, but global tool/provider/platform registries
   and shared `sys.modules` names would still leak across profiles. A keyed
   manager alone is not isolation.
3. Monotonically add only a missing required-policy plugin through a restricted
   staging context. This fixes the observed stale-negative cache without
   clearing or replacing any registry and keeps the current model/tool contract
   stable. This is the selected approach.

## Design

### Restricted required-policy recovery

At the start of each turn, before plugin lifecycle and `pre_llm_call` hooks,
Hermes will best-effort reconcile configured required policies. Authorization
will repeat the same reconciliation immediately before returning a missing or
disabled-plugin block, covering an install that lands after turn setup.

Recovery is serialized by one process-wide re-entrant lock. A recovery attempt
captures the resolved `HERMES_HOME`, project-plugin enablement, project root,
safe-mode state, and required-policy mapping once. The capture is checked again
before publication; a changed context aborts fail-closed.

For each absent or never-loaded required plugin, Hermes resolves the current
winner from an existing disabled manifest or by rescanning the active user and,
when enabled, project plugin directories. This scan sees `plugin.yaml` created
inside an existing directory. The candidate must:

- be currently enabled and not explicitly disabled;
- declare every required policy in its manifest;
- be a directory plugin whose normal `register(ctx)` path has not already run;
- register only hooks and required-policy callbacks during recovery.

The plugin is imported and registered against a staging `PluginManager` through
a restricted `PluginContext`. `register_hook` and `register_policy` are the only
allowed registration methods. Access to any other `register_*` method raises a
controlled load error before that context can mutate global tool, provider, or
platform registries. The current `gitnexus-governor` qualifies: it registers
hooks plus `tool_dispatch`, and no tools/providers/platforms.

Only after registration succeeds and every required callback exists does Hermes
publish copied hook, policy-registration, and plugin maps to the live manager.
The plugin map is the commit marker and is published last. Concurrent readers
therefore see either the old missing plugin or a complete usable registration;
concurrent recoverers converge on one load. Normal discovery, explicit
`force=True`, and direct `_plugin_manager` test replacement keep their existing
semantics.

This is monotonic. An enabled loaded plugin is never re-imported, and no loaded
hook is removed. A candidate that is still absent/disabled, runs in safe mode,
uses an unsupported registration surface, changes execution context during
load, or fails to register the declared policy remains fail-closed with the
existing stable required-policy failure codes. Tool-registry generation and the
model's cached schema remain unchanged.

### Trusted policy-block provenance

The serialized `required_policy_block` JSON remains in the transcript, but it
is not trusted as a control signal. Both host producers already own the
concrete `ToolPolicyBlock` before serialization: outer required-policy
authorization in middleware and the registry's one-use authorization-binding
check. Each producer will record that frozen object in a ContextVar-bound,
thread-safe batch collector keyed by `tool_call_id` immediately before its
existing serialized return.

The executor binds one collector around a tool batch. Context propagation shares
that collector with concurrent workers. Ordinary tool text, even if it exactly
spoofs the JSON envelope, cannot enter the collector and cannot halt a turn.

Only `policy_code == policy_blocked` is recoverable: it is an explicit policy
decision and the model may choose a different safe action. Every other code is
terminal, including every `required_policy_*` infrastructure code, malformed or
non-explicit policy decisions, missing/mismatched bindings, invalid/empty block
messages, and unknown future codes.

For concurrent batches, every already-submitted tool reaches a terminal result,
and the terminal block is chosen by original assistant tool-call order rather
than worker completion order. For sequential batches, execution stops after the
first terminal block; Hermes appends deterministic skipped tool results for
every unstarted sibling so every assistant `tool_call_id` has exactly one tool
result before the final assistant message.

### Deterministic turn halt

The agent records the first terminal `ToolPolicyBlock` for the batch. This state
is initialized on agent creation and reset at every turn boundary.

Immediately after tool execution, before another API request, the conversation
loop will:

- set `turn_exit_reason` to `required_policy_halt` and `failed` to true;
- build a fixed host-authored response containing the stable policy code and
  stating that Hermes stopped and the blocked tool did not run;
- append and stream that assistant response; and
- break without another model call.

The text will not suggest install commands or expose exception/plugin details.
Turn metadata will include `required_policy` with the structured safe block.

For `required_policy_halt`, finalization will not run output transforms,
`post_llm_call`, the completion explainer, or background memory/skill model
review. Persistence, cleanup, session-end lifecycle, and non-model telemetry
still run.

## Verification

Automated regressions will prove:

- one manager discovers an empty active home, a required hooks+policy plugin is
  then installed/enabled, and the next turn recovers it under the same PID;
- the same recovery succeeds when a manifest was initially discovered but the
  plugin was not enabled;
- concurrent recovery publishes one hook/callback set, detects a changed
  captured home/project context, and leaves tool-registry generation unchanged;
- a recovery candidate that tries to register a tool/provider/platform remains
  fail-closed and does not publish partial hooks or policies;
- an infrastructure block ends sequential and concurrent turns after one model
  call, while `policy_blocked` remains recoverable;
- ordinary tool text cannot spoof a halt, concurrent selection follows original
  call order, and sequential skipped siblings close the transcript;
- the blocked handler never runs, streaming receives the fixed response, and
  finalization neither rewrites it nor launches background review;
- focused plugin, required-policy, executor, guardrail, and conversation-loop
  suites remain green.

Live acceptance has two parts:

1. Run an isolated-home single-process probe that performs initial discovery,
   creates/enables a harmless required-policy plugin, and successfully dispatches
   without restarting that PID.
2. Sync only reviewed files into the installed Agent checkout, restart WebUI,
   wait for `/health`, and execute a harmless real tool call under the configured
   governor. Real `~/.hermes` state is preserved throughout.
