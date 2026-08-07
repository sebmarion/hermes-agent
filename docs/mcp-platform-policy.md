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

The Dashboard `POST /api/mcp/servers` create contract accepts the same
`allowed_platforms` field. It trims, lowercases, and de-duplicates values;
omission remains unrestricted, while an empty list or blank entry is rejected.
`GET /api/mcp/servers` returns the effective field as
`allowed_platforms: string[] | null`.

## Toolset selection

MCP server names are selectable as toolsets. A server named `docs` normally
uses `docs` (and its canonical `mcp-docs` registry target). If a raw server name
collides with a native, plugin, or custom toolset, the raw spelling always
keeps its non-MCP meaning and the MCP server must be selected as
`mcp-<server_name>`. For example, a server named `web` is selected as
`mcp-web`; selecting `web` still means the native Web toolset.

An explicit MCP selection is authoritative even when platform policy denies
it. Hermes removes the denied selection and does not replace it by enabling
every other allowed MCP server. Omit MCP selections to inherit all servers
allowed on that surface, or use `no_mcp` to opt out entirely.

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
- ACP reuse also requires an exact full-config identity match. The same name
  with a different URL, command, arguments, headers, environment, or behavior
  config is rejected rather than inheriting another editor session's endpoint.
- An explicit empty or smaller ACP server list revokes only aliases previously
  added by that ACP session. Process-global connections are retained until
  normal shutdown so other sessions using an identical endpoint are not broken.
- Missing registration provenance fails closed for MCP tools. Native Hermes
  tools remain available if MCP policy loading fails.

Platform identity is propagated through sequential and concurrent execution,
Tool Search unwrapping, background agent dispatch, and nested `execute_code`
RPC calls. Shutdown clears live, in-flight, and lazy-cache ownership,
provenance, and registry entries so a later registration can safely acquire
the name without inheriting stale ownership.
