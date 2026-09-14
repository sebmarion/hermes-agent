# Zeus OS native mobile chat — 14 September 2026

## Status
Implemented and tested in isolated worktrees. **Not merged or deployed.** The production interface is still the previous version. The existing production AfterCI receipt is for `7fe01dc05e8062f453870cb8b6cb5cfa37375834` and does not certify this candidate.

The owner asked for a standard chat instead of the unusable integrated terminal on mobile. This is a presentation change for `/ai/chat?profile=zeus-os&embed=zeus` only. The generic Hermes dashboard and its terminal page are unchanged.

## What changed
The Zeus surface now has a readable streaming transcript, a bottom composer, mobile Enter for newlines, explicit Send/Stop, new conversations, searchable/paginated saved history, safe Markdown, copy with the existing compatibility helper, and collapsed tool activity. Scrolling up is not interrupted by incoming text; a jump-to-latest control returns to the bottom.

Approval and clarification requests use normal cards/forms. Approval only offers denial or one-time permission, never session-wide or permanent permission. A clarification replaces the ordinary composer rather than presenting two competing text boxes. Answering one approval rechecks for remaining requests so a queued approval cannot disappear. Secure replies are not added to the transcript or browser persistence.

The outer Zeus shell becomes full-height on mobile, removes the duplicate marketing header and fixed terminal minimum heights, and follows the actual visual viewport when the keyboard opens. The frame must acknowledge native rendering with a correlated, same-origin, source-window-checked handshake. A missing/old/broken frontend retains an independent Back/Retry path instead of trapping the phone. No nginx, Tailscale, port, or root-route configuration was changed.

Execution uses the existing authenticated JSON-RPC gateway and existing `zeus-os` profile. New sessions honestly advertise the web surface rather than unsupported desktop panes; the configured server toolset and approval mechanism are reused. Existing histories remain resumable. A resumed desktop session gets an explicit unsupported response for GUI-pane bridges rather than a fabricated action or an endless wait.

Drafts use tab-local sessionStorage; only the active conversation identifier uses localStorage. Transcripts remain server-owned. An uncertain send is never automatically replayed, including across reconnect and reload. A failed restore cannot silently create a replacement conversation, and an acknowledgement cannot erase a newer draft.

## Observed verification
- TypeScript project build, scoped ESLint, and production Vite build passed.
- Full frontend suite: 42 files, 300 tests passed.
- Zeus hub suite: 1,008 tests passed; asset validation and JavaScript syntax checks passed.
- Managed-browser integration: 17 checks passed against the built chat and the actual candidate Zeus shell. Viewports: 320×568, 390×844, 390×420, 844×390, 768×1024, and 1440×900. Coverage includes streaming, preserved drafts, mobile newline behavior, approval, clarification, stopping, saved history, scroll anchoring, uncertain delivery across reload, safe links, bootstrap failure escape, and same-origin frame messaging. No PTY request or JavaScript exception occurred in that run.
- A separate, real-backend smoke submitted one deliberately labeled QA prompt, received `ZEUS_NATIVE_CHAT_OK`, and restored the reply after reload with exactly one prompt submission. This created one labeled QA conversation; it did not edit an existing owner conversation. No real credentials were entered in testing.

These are browser-emulation results, **not physical iPhone/Safari/VoiceOver certification**. The real-backend happy-path smoke preceded the final clipboard/queued-request/composer refinements; final mock integration and unit suites cover those refinements. Do not overstate that distinction.

## Files and evidence
Hermes worktree: `/home/seb/worktrees/hermes-zeus-mobile-chat-20260914`.
Zeus shell worktree: `/home/seb/worktrees/zeus-home/mobile-chat-20260914`.
Both use branch `fix/zeus-mobile-chat-20260914` in their respective repositories.
Evidence directory: `/home/seb/.local/state/zeus-mobile-chat-20260914`.
Key records: `browser/results.json`, `browser/mobile-empty.png`, `browser/mobile-conversation.png`, `web-regressions.log`, `zeus-regressions.log`, `asset-validation.log`, `build.log`, and `live-transport-smoke.json`.
The browser harness is tracked at `web/tests/zeus-native-chat.browser.mjs`. It requires the managed browser broker, an explicit shell path, and an explicit evidence directory; its RPC actions are fixtures, and its live-service access is read-only metadata. The separate opt-in live smoke is kept in the evidence directory.

## Remaining rollout
Integrate only these scoped changes through the current authorized repository/release workflow, accounting for concurrent Zeus work. Run AfterCI for the exact integrated Zeus revision and bind the separately built chat artifact and manual/browser evidence to the same coordinated release. Do not reuse the old production receipt or bypass a gate.

Both artifact sets must be published together: the chat frontend served by the existing dashboard, and the Zeus shell CSS/JavaScript/index. Publishing only the chat would retain the old mobile iframe sizing; publishing only the shell must never be represented as native-chat completion. Preserve old hashed frontend assets, back up both current artifact sets, use the approved index-last publisher, and retain a matched rollback. Verify actual HTTPS artifacts and the real native-ready handshake after promotion. Check a physical iPhone with the keyboard open before claiming device-level acceptance.

No production publication, service restart, model change, credential rotation, account migration, or financial/customer action has been performed by this implementation. Privileged command inspection was denied by the tool policy and was not retried through another mechanism; use the authorized release path, not ad hoc elevation.
