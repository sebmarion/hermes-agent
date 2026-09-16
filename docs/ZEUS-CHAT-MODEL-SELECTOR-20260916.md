# Zeus Chat model selector — 16 September 2026

## Status: implemented and tested; production publication held

Owner request: “I need to be able to change model from that chat.”

The native Zeus chat now has a Model dropdown directly below its header. It opens a searchable catalogue from the existing configured providers, identifies the current model, and applies an explicit per-conversation selection. Other conversations and the profile default remain unchanged. The build is a candidate, not the interface currently served to the owner.

Implementation revision: `c30f5a55afbd474eca9d589db35e29336df802ab` in the owner's Hermes repository. Worktree: `/home/seb/worktrees/hermes-zeus-chat-model-selector-20260916`, branch `feat/zeus-chat-model-selector-20260916`.

## Behavior

The selector uses the existing authenticated `model.options`, `config.set` and session APIs. The switch includes `--provider` and **`--session`**; omitting the latter would allow the existing backend to persist a profile-wide change. No provider credentials, global defaults or backend methods were changed.

History and draft stay in the same conversation. The chosen model survives reload before and after the first prompt. The next new conversation inherits the unchanged profile default. Higher-cost selections preserve the gateway's explicit confirmation. Apply, Cancel and Escape have distinct behavior: while a submitted change is pending, closing cannot pretend to cancel it. Missing acknowledgements are not automatically retried; the chat requires authoritative reconciliation before sending again.

The active model/provider is read back from the existing same-runtime `session.activate` response. New, unpersisted sessions can return profile defaults from `session.resume`; that is not accepted as model evidence. The bounded read-back waits for the scheduled agent initialization without sending a prompt or replaying the model change. The UI disables switching during a locally active response. If another client starts a response concurrently, the gateway's existing deferred switch is labelled **Next model**, with an explicit queued notice; the running answer is unchanged.

## Actual verification

- **316 frontend tests passed**, with TypeScript and production build successful.
- **20 existing backend tests passed** through the isolated test runner: profile-aware model options, session/global persistence rules and expensive/deferred model confirmations.
- **28 broker-managed browser scenarios passed**, including small/landscape layouts, drafts, history, approvals, source expiry, selector error recovery, confirmation and reload.
- The actual production gateway was exercised through the candidate UI in an isolated browser context: Sol → Terra, model/provider read-back, reload before the first prompt, one explicit read-only QA prompt, then reload with preserved model/history/draft. The final reply took 3.439 seconds in that single test. The profile configuration remained byte-identical; its default is still `gpt-5.6-sol`, reasoning `xhigh`.

These are candidate and integration tests, not a new production-release certificate. The catalogue was read from real configured providers, but every listed model was not separately exercised. Physical iPhone/Safari was not certified.

The initial independent source review found acknowledgement, cancellation and lifecycle issues, which were corrected. A focused follow-up on the switch contract returned PASS; it is source-only, not release approval. The subsequent unsent-conversation restoration fix has dedicated unit and real-gateway regression proof. Earlier failed/timeout results remain retained rather than relabelled as passes.

## Release blocker — no bypass used

The deployed Zeus shell remains `60fca86c49d3dc092b66ce2304c85642a19284dc`. Its current protected AfterCI consumer rejects release readiness with `afterci_quality_certification_unavailable`; the underlying quality check reports **`quality_design_reference_approval_invalid`**. Existing delegated reference/review authority has expired. Both canonical and installed-gate consumers reproduce the hold.

No protected policy, reference image, threshold, expiry, delegated authority or prior certificate was rewritten to make this feature pass. The production native bundle remains unchanged: all 61 files match the previously deployed `d1b664fd3acf609724e0eba11d13d550c0589f21` build. No service was restarted and no model selector has been published yet.

Next: the owner/coordinator must renew the applicable design-review authority through the existing process; then rerun the normal paired release gate for the native candidate, preserve the existing static assets and rollback index, publish index last, and run the real no-overlay production test. This request grants no authority to bypass the gate or alter unrelated Growth/provider/backup work.
