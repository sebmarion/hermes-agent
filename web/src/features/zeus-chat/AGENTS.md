# Zeus OS chat surface

Owner directive, 14 September 2026: Zeus OS must use a standard mobile chat, not the embedded terminal.
This explicitly replaces the TUI-only presentation rule for the `profile=zeus-os&embed=zeus` surface only.
Keep the generic Hermes dashboard/TUI unchanged. Reuse the existing authenticated JSON-RPC gateway,
profile-scoped conversation store, prompt streaming and approval contracts. No new execution authority,
implicit model overrides, credential scheme or automatic prompt replay. Never store transcript or credentials
in browser persistence. Drafts may use sessionStorage; the active conversation ID may use localStorage.

Owner directive, 16 September 2026: expose an explicit per-conversation model selector in this chat. Reuse model.options/config.set with --session, retain provider confirmation warnings, and do not change profile defaults or other conversations.
