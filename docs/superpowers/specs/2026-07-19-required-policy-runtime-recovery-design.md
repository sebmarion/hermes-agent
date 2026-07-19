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
standalone directory plugin in the same active `HERMES_HOME` and process. It
also stops a turn deterministically when required-policy enforcement itself is
unavailable.

It does not hot-replace an already loaded plugin, hot-unload a disabled plugin,
reload pip entry points, or claim full cross-profile isolation for the ordinary
plugin manager. Hermes' general plugin registries include process-global tools,
providers, platforms, and module names; solving per-profile isolation for every
plugin capability is a separate architectural change. This recovery path
therefore refuses plugins that need those global registration surfaces, scopes
its own recovered hooks/policies to one explicit home, and never rebuilds the
global plugin universe.

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

WebUI will bind its already resolved `_profile_home` through
`hermes_constants.set_hermes_home_override()` for the exact
`agent.run_conversation()` call and reset the token in `finally`. This is the
explicit cross-thread handoff: recovery and later authorization never infer the
turn's home from process-global `os.environ`, which another WebUI turn can
change concurrently.

At the start of each Agent turn, before plugin lifecycle and `pre_llm_call`
hooks, Hermes will best-effort reconcile configured required policies for that
explicit active home. Authorization will repeat the same reconciliation
immediately before returning a missing or disabled-plugin block, covering an
install that lands after turn setup.

Recovery is serialized by one process-wide re-entrant lock. A recovery attempt
captures the resolved `HERMES_HOME`, project-plugin enablement, project root,
safe-mode state, required-policy mapping, and the manager's recorded discovery
home once. The capture is checked again before publication; a changed context
or manager/home mismatch aborts fail-closed. Initial and forced discovery record
the resolved home only after a successful sweep. This is provenance checking,
not a claim that the ordinary global manager is profile-isolated.

For each absent or never-loaded required plugin, Hermes resolves the current
winner from an existing disabled manifest or by rescanning the active user and,
when enabled, project plugin directories. This scan sees `plugin.yaml` created
inside an existing directory. The candidate must:

- be currently enabled and not explicitly disabled;
- declare every required policy in its manifest;
- be a standalone directory plugin whose normal `register(ctx)` path has not
  already run and whose manifest declares no tools;
- register only hooks and required-policy callbacks during recovery.

The plugin source is executed in a private staging module that is not installed
under its canonical `hermes_plugins.*` name. Registration receives a detached,
sealed context exposing only `register_hook` and `register_policy`. Access to
any other `register_*` surface fails before that context can mutate global tool,
provider, or platform registries. The current `gitnexus-governor` qualifies: it
registers hooks plus `tool_dispatch`, and no tools/providers/platforms.

Only after registration succeeds and every required callback exists does Hermes
build a new frozen required-policy runtime snapshot. The snapshot contains
home-scoped recovered plugins with immutable hook and policy-registration
tuples and keeps their private modules alive. Publication is one reference swap
under a short lock. Hook and authorization readers capture the ordinary manager
generation and recovered snapshot while holding the same recovery lock used by
recovery and forced discovery, then release it before invoking callbacks. They
therefore observe the complete old generation or complete new generation,
never hooks from one and policies from another.

Module-level hook accessors append only the recovered hooks whose home matches
the current ContextVar override. Required-policy authorization resolves one
plugin/policy pair from the same captured snapshot. It may consult the ordinary
startup-discovered manager only when that manager has a known discovery home
equal to the captured active home. If the discovery home is unknown or differs,
authorization returns the existing stable `required_policy_plugin_load_error`
infrastructure block without reading that manager's plugin, hook, or policy
maps. Thus neither recovered nor ordinary manager state for home A is visible
under home B. Normal discovery, explicit `force=True`, and direct
`_plugin_manager` test replacement keep their existing semantics within a
matching active home.

Authorization freezes one sorted sequence containing either a concrete policy
callback or a stable static block for each pair, stopping capture at the first
static block. It then invokes captured callbacks outside the recovery lock in
that same order. A concurrent force reload therefore cannot substitute another
home or mix generations, while an earlier explicit callback block still wins
over a later infrastructure failure. Callback executor workers receive a copy
of the dispatch ContextVar context, so the callback observes the same bound
home as the authorizing turn.

This is monotonic during narrow recovery. An enabled loaded plugin is never
re-imported, and no recovered hook is removed by another recovery attempt. A
successful explicit `force=True` discovery atomically retires the matching
home's recovered ownership after the ordinary generation is ready. UUID-named
private modules remain retained for callbacks already in flight, including lazy
relative imports, and cannot shadow the new canonical generation. A candidate
that is still absent/disabled, runs in safe mode, uses an unsupported
registration surface, changes execution context during load, comes from a home
different from the manager's discovery home, or fails to register the declared
policy remains fail-closed with the existing stable required-policy failure
codes. Tool-registry generation and the model's cached schema remain unchanged.

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

For `required_policy_halt`, finalization will not run the file-mutation footer,
output transforms, `post_llm_call`, the completion explainer, external-memory
sync/prefetch, or background memory/skill model review. Persistence, cleanup,
session-end lifecycle, pending-steer handoff, and non-model telemetry still run.

## Verification

Automated regressions will prove:

- one manager discovers an empty active home, a required hooks+policy plugin is
  then installed/enabled, and the next turn recovers it under the same PID;
- the same recovery succeeds when a manifest was initially discovered but the
  plugin was not enabled;
- paused and concurrent recovery proves snapshot readers see only a complete
  old or new hook+policy generation, detects changed/mismatched home/project
  context, and leaves tool-registry generation unchanged;
- when home B requires the same plugin key as an ordinary manager discovered
  under home A, authorization returns a stable infrastructure block and never
  invokes A's callback;
- WebUI binds the resolved profile home before `run_conversation`, resets it on
  every exit, and concurrent turns cannot make recovery read another profile;
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
2. Sync only reviewed Agent files plus the narrow WebUI home-binding change into
   their installed/runtime owners, restart WebUI, wait for `/health`, and execute
   a harmless real tool call under the configured governor. Real `~/.hermes`
   state is preserved throughout.
