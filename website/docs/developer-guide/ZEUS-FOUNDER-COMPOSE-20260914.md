# Zeus founder-question composer bridge

Candidate branch: `feat/zeus-founder-compose-20260914`, based on native-chat revision `1aabe34274`. This is an additive Zeus-native-chat integration; it does not change generic Hermes chat, model/provider selection, session persistence, authorization, streaming RPCs or terminal behavior.

The parent may prepare a question only through a matching origin, parent window, frame-generation and bounded request ID. A maximum of 64 IDs is retained per frame without replay-enabling eviction. The receiver never submits a prompt or overwrites an existing draft, active/restoring turn, uncertain send or pending approval. An occupied composer gets a visible question with explicit Use, Copy and Dismiss actions. Use is disabled until existing input/work is clear.

`compose.test.ts` checks validation, replay and draft/turn protection. The actual production bundle is exercised by `web/tests/zeus-native-chat.browser.mjs` with the candidate Zeus shell and simulated RPCs: no production prompt or mutation is sent. The harness covers short phone viewports with the occupied-question card as well as draft handoff, history, uncertain acknowledgements, approvals and streaming. Physical iOS and real model response latency are not certified by those fixtures.

Build: `cd web && npm test && npm run build`. Browser acceptance requires the existing Zeus broker lease and explicit `ZEUS_CHAT_SHELL` / `ZEUS_CHAT_EVIDENCE` paths. Do not deploy this bundle independently and claim the parent handoff is live: both repository changes and the exact combined release must be qualified. Production service files, generic Hermes routes, systemd and provider settings were not changed by this work.


## Exact-source isolation correction

The first commit `2a19e045c3` accidentally included an import/render reference and styles for a concurrently edited `QuickAnswers` component whose implementation was still untracked. The clean checkout correctly failed compilation. This qualification branch removes only that incomplete dependency from its isolated candidate; it does not delete, revert, stage or approve the original worktree’s continuing quick-answer implementation. Do not certify `2a19e045c3` from an older compiled bundle. Build and test the correction commit itself before integration.


## Wave 6 release continuation

The current isolated release line re-integrates the complete Instant company answers drawer with its validated message parser and tests; no untracked import is required. It uses only correlated parent snapshot queries and expires displayed evidence. Unsupported queries prepare a normal AI draft. The normal chat model and gateway remain unchanged.

The independent design review found a malformed rejected-link label, visually clipped long code and an intrusive parent draft-preservation toast. The shared Markdown parser now consumes balanced one-level URL parentheses and renders unsupported schemes as non-navigable labelled text; escaped raw HTML remains escaped. Zeus-only CSS soft-wraps long code without modifying copy text and uses its own native colour tokens. The parent suppresses the duplicate occupied-draft toast; the native recovery card remains authoritative. The exact built-bundle browser suite proves blocked schemes produce no anchors, code fits the composer viewport, no auto-submission occurs and existing drafts survive. This is candidate web evidence, not physical-device certification or production release.
