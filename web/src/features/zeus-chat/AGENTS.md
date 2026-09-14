# Zeus OS chat surface

Owner directive, 14 September 2026: Zeus OS must use a standard mobile chat, not the embedded terminal.
This explicitly replaces the TUI-only presentation rule for the `profile=zeus-os&embed=zeus` surface only.
Keep the generic Hermes dashboard/TUI unchanged. Reuse the existing authenticated JSON-RPC gateway,
profile-scoped conversation store, prompt streaming and approval contracts. No new execution authority,
model overrides, credential scheme or automatic prompt replay. Never store transcript or credentials
in browser persistence. Drafts may use sessionStorage; the active conversation ID may use localStorage.
