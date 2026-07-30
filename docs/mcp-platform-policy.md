# MCP runtime-platform policy

An MCP server can be limited to the Hermes runtime surfaces on which it may be
advertised and called. This is a runtime policy, not an operating-system or
host policy.

```yaml
mcp_servers:
  zeus-agentic-browser:
    command: python
    args: ["-m", "zeus_browser_mcp"]
    enabled: true
    allowed_platforms: [cli]
```

`allowed_platforms` is optional for backwards compatibility. When it is
omitted, an enabled configured MCP server retains its previous all-surface
behaviour. Once present, it must be a non-empty list of non-empty runtime names;
an empty or malformed value denies the server until corrected.

The standalone TUI and desktop chat backend use the `cli` toolset policy. Use
`cli` for those surfaces rather than `tui` or `desktop`.

The policy is applied while Hermes builds and refreshes a tool snapshot,
including explicit `all` selections and tool-search. Hermes also checks the
live configuration immediately before an MCP dispatch, so a stale snapshot
cannot run a disabled or newly restricted configured server.

Editor-provided ACP MCP servers are process-global registrations but are
limited to the `acp` runtime surface. This source boundary does not establish
per-editor-session isolation; callers that need that isolation should use
distinct worker processes or a session-bound MCP transport.
