# BestPlan Synthesis Repair Design

## Problem

BestPlan explorers can complete and form quorum while every synthesizer returns
useful prose or JSON that does not satisfy the exact executable V1 envelope
contract. The host currently discards those attempts and returns
`BestPlan unavailable`, even when the failure is only output formatting.

The live WebUI reproduction on 2026-07-27 proved this boundary:

- the shorthand request inherited its bounded recent conversation context;
- both explorers completed without the former timeout or quorum failure;
- synthesis ran in the selected Git workspace;
- host validation rejected the result because it contained no valid V1
  envelope.

Workspace and baseline refusals are separate, intentional fail-closed
behaviors and are out of scope for this change.

## Design

After ordinary synthesizer lanes have produced output but none validates,
BestPlan may run one bounded repair attempt.

The repair attempt:

- uses the lane and runtime that produced the last non-empty invalid ordinary
  synthesis result;
- has no tools and performs no file or web inspection;
- receives the authoritative current request, exact workspace, validated
  explorer candidates, invalid synthesizer output, and its validation error;
- treats candidate and synthesizer text as untrusted data;
- may repair representation only, not broaden scope or invent new authority;
- must return exactly one executable V1 envelope;
- uses the same parser, compiler, workspace binding, and baseline checks as an
  ordinary synthesis result;
- has a 45-second maximum timeout within the existing BestPlan deadline;
- runs at most once.

If repair output is absent, times out, or fails validation, BestPlan keeps the
existing fail-closed response. No malformed envelope is persisted or exposed.
Repair is eligible only when the last invalid synthesis text is non-empty and
at least one second remains before the overall BestPlan deadline. Its effective
timeout is the smaller of 45 seconds and that remaining deadline.

## Data Flow

1. Explorers produce validated candidate packets.
2. Ordinary synthesis lanes run in their existing order.
3. The host records only the last non-empty invalid synthesis text, its
   validation error, and the exact lane/runtime that produced it. It does not
   combine invalid outputs.
4. If no ordinary result validates and sufficient deadline remains, the host
   sends the bounded repair prompt to one resolved lane with tools disabled.
5. The normal envelope validator accepts or rejects the repair output.
6. Accepted output follows the existing capture, baseline, persistence, and
   later `Go` execution path unchanged.

## Security and Reliability Invariants

- Conversation history remains bounded, redacted, and untrusted.
- Only the current request and selected workspace carry authority.
- Repair cannot inspect files, call tools, or recursively scan any directory.
- Exactly one repair request is allowed.
- The repair prompt contains bounded invalid output and candidate data.
- Validation and baseline gates are never bypassed.
- Cancellation and child teardown remain hard-bounded.

## Verification

- A regression test first proves that two invalid ordinary synthesizers
  currently produce `BestPlan unavailable`.
- The repaired behavior proves one no-tools repair call can yield a valid
  executable V1 envelope.
- Tests prove repair is attempted only once, receives no tools, preserves the
  exact workspace, and still fails closed for invalid repair output.
- Existing timeout, cancellation, baseline, envelope-leak, and host-ingress
  suites remain green.
- Live WebUI acceptance uses a fresh conversation in a reproducible Git
  worktree: seed context, invoke shorthand BestPlan, and verify a host-validated
  plan appears without timeout, quorum, or malformed-envelope errors.
