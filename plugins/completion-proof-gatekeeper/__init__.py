"""Report-only lint for completion claims without same-response proof.

The gatekeeper deliberately runs at the plugin edge through Hermes'
``transform_llm_output`` hook. It does not block a turn, run commands, inspect
the workspace, or persist response text. Its first rollout job is to make
unsupported completion claims visible so the false-success rate can be
measured before any harder policy is considered.
"""

from __future__ import annotations

import re
from typing import Any


_COMPLETION_CLAIM_RE = re.compile(
    r"\b(?:done|fixed|working|verified|deployed|pushed|published|resolved|"
    r"complete|completed)\b"
    r"|\b(?:tests?|checks?|build)\s+(?:pass|passed|passing|green)\b",
    re.IGNORECASE,
)
_WORK_CONTEXT_RE = re.compile(
    r"\b(?:bug|code|commit|deploy|file|fix|handler|implementation|lint|"
    r"patch|pr|repo(?:sitory)?|route|test|build|change|edited|implemented)\b",
    re.IGNORECASE,
)
_DIRECT_COMPLETION_RE = re.compile(
    r"^\s*(?:done|fixed|verified|deployed|pushed|published|resolved|"
    r"complete(?:d)?|working)[.!]?\s*$",
    re.IGNORECASE,
)
_EXPLICIT_BLOCKER_RE = re.compile(
    r"\b(?:could\s+not|couldn't|cannot|can't|unable\s+to|blocked|"
    r"not\s+(?:done|complete|completed|verified|confirmed|tested|run|"
    r"deployed|published)|needs?\s+follow[- ]?up|not\s+ready)\b",
    re.IGNORECASE,
)
_COMMAND_PROOF_RE = re.compile(
    r"(?:`[^`\n]+`|\b(?:pytest|tox|nox|ruff|mypy|eslint|tsc|vitest|"
    r"jest|npm|pnpm|yarn|bun|cargo|go|make|curl|httpie)\b)",
    re.IGNORECASE,
)
_API_PROOF_RE = re.compile(
    r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+/[^\s,;)]*"
    r"|\b(?:API|endpoint|health(?:\s+check)?)\b",
    re.IGNORECASE,
)
_SUCCESS_RE = re.compile(
    r"\b(?:pass(?:ed|es|ing)?|green|successful(?:ly)?|success|"
    r"exit(?:ed)?\s+(?:with\s+)?0|0\s+(?:failures?|errors?)|"
    r"HTTP\s*2\d\d|status\s*[:=]?\s*2\d\d|"
    r"returned\s+2\d\d|healthy|read[- ]back[^.\n]*(?:match|confirm)|"
    r"matches?[^.\n]*(?:expected|recorded|source))\b",
    re.IGNORECASE,
)
_WARNING = (
    "[Proof gatekeeper — report-only: a completion claim was detected without "
    "same-response command/API/read-back proof. Run the relevant check or "
    "state the concrete blocker before treating this as done.]"
)


def _has_completion_claim(response_text: str) -> bool:
    if _DIRECT_COMPLETION_RE.search(response_text):
        return True
    if not _COMPLETION_CLAIM_RE.search(response_text):
        return False
    return bool(_WORK_CONTEXT_RE.search(response_text))


def _has_successful_proof(response_text: str) -> bool:
    has_proof_source = bool(
        _COMMAND_PROOF_RE.search(response_text)
        or _API_PROOF_RE.search(response_text)
        or re.search(r"\bread[- ]back\b", response_text, re.IGNORECASE)
    )
    return has_proof_source and bool(_SUCCESS_RE.search(response_text))


def transform_llm_output(
    *,
    response_text: str = "",
    **_: Any,
) -> str | None:
    """Append a visible report when a strong completion claim lacks proof."""
    if not isinstance(response_text, str) or not response_text.strip():
        return None
    if not _has_completion_claim(response_text):
        return None
    if _EXPLICIT_BLOCKER_RE.search(response_text):
        return None
    if _has_successful_proof(response_text):
        return None
    return f"{response_text.rstrip()}\n\n{_WARNING}"


def register(ctx: Any) -> None:
    """Register the report-only final-response transform."""
    ctx.register_hook("transform_llm_output", transform_llm_output)


__all__ = ["register", "transform_llm_output"]
