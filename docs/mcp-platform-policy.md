# MCP runtime platform policy

MCP servers may optionally restrict which Hermes runtime surfaces can expose
and execute their tools:

```yaml
mcp_servers:
  browser:
    command: npx
    args: ["-y", "@playwright/mcp"]
    allowed_platforms: [cli, cron]
```

`allowed_platforms` names Hermes runtime surfaces, not operating systems.
Typical values include `cli`, `acp`, `cron`, `telegram`, `discord`, and
`slack`.

## Policy contract

- Omitting `allowed_platforms` preserves the historical behavior: the server
  is available on every surface where MCP is otherwise enabled.
- Once present, the value must be a non-empty list of non-empty strings.
  Empty or malformed values deny every surface until corrected.
- Values are compared case-insensitively after trimming whitespace.
- `tui` and `desktop` authorize against the `cli` policy surface because both
  assemble their MCP selection from the CLI tool configuration.
- `enabled: false` always denies the server.

Policy is enforced when platform toolsets are resolved, when model schemas are
assembled (including `all`, Tool Search, and live refresh), and again directly
before handler execution. The final check reloads config so an existing agent
snapshot cannot call a server that was disabled, restricted, or removed after
the snapshot was built.

## Registration provenance

Config-defined servers and ACP editor-provided servers share a process-wide
tool registry, but their authority does not merge:

- Config registrations are checked against the exact raw `mcp_servers` key.
  Generated tool-name sanitization never changes policy identity.
- ACP registrations are ACP-only. A same-named config entry cannot relabel or
  widen an editor-provided endpoint.
- A live or in-flight server name has one immutable registration source.
  Concurrent registrations from another source are rejected.
- Missing registration provenance fails closed for MCP tools. Native Hermes
  tools remain available if MCP policy loading fails.

Platform identity is propagated through sequential and concurrent execution,
Tool Search unwrapping, background agent dispatch, and nested `execute_code`
RPC calls. Shutdown clears in-flight source reservations so a later startup
cannot inherit stale ownership.
