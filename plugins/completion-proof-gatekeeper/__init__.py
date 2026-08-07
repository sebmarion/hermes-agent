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
    r"|\b(?:tests?|checks?|build)\s+(?:(?:is|are|was|were)\s+)?"
    r"(?:pass|passed|passing|green)\b",
    re.IGNORECASE,
)
_WORK_CONTEXT_RE = re.compile(
    r"\b(?:bug|checks?|code|commit|deploy|file|fix|handler|implementation|lint|"
    r"patch|pr|repo(?:sitory)?|route|tests?|build|change|edited|implemented)\b",
    re.IGNORECASE,
)
_DIRECT_COMPLETION_RE = re.compile(
    r"^\s*(?:done|fixed|verified|deployed|pushed|published|resolved|"
    r"complete(?:d)?|working)[.!]?\s*$",
    re.IGNORECASE,
)
_EXPLICIT_BLOCKER_RE = re.compile(
    r"\b(?:could\s+not|couldn't|cannot|can't|unable\s+to|blocked|"
    r"(?:not|\w+n['’]t)\s+(?:be(?:en)?\s+)?(?:done|fixed|working|"
    r"verified|deployed|pushed|published|"
    r"resolved|complete(?:d)?|pass(?:ed|ing)?|green|confirmed|tested|run)|"
    r"did\s+not\s+pass|needs?\s+follow[- ]?up|not\s+ready)\b",
    re.IGNORECASE,
)
_BARE_COMMAND_PROOF_RE = re.compile(
    r"\b(?:pytest|tox|nox|ruff|mypy|eslint|tsc|vitest|jest|npm|pnpm|"
    r"yarn|bun|cargo|go|make|curl|httpie|git|python(?:3(?:\.\d+)?)?)"
    r"\b(?![.\w-])",
    re.IGNORECASE,
)
_BACKTICK_SPAN_RE = re.compile(r"`([^`\n]+)`")
_COMMAND_TOKEN_RE = re.compile(
    r"(?:\.{0,2}/)?[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)*"
)
_INLINE_DATA_SUFFIXES = (
    ".ini",
    ".json",
    ".md",
    ".py",
    ".txt",
    ".yaml",
    ".yml",
)
_API_PROOF_RE = re.compile(
    r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+/[^\s,;)]*",
    re.IGNORECASE,
)
_SUCCESS_RE = re.compile(
    r"\b(?:pass(?:ed|es|ing)?|green|successful(?:ly)?|success|"
    r"exit(?:ed)?\s+(?:with\s+)?(?:code\s+)?0|0\s+(?:failures?|errors?)|"
    r"HTTP\s*2\d\d|status\s*[:=]?\s*2\d\d|"
    r"returned\s+2\d\d|healthy|read[- ]back[^.\n]*(?:match|confirm)|"
    r"matches?[^.\n]*(?:expected|recorded|source))\b",
    re.IGNORECASE,
)
_PROOF_BOUNDARY_RE = re.compile(r"(?:[.!?](?=\s+|$)|;|\n)+\s*")
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


def _is_command_like_backtick_span(span: str) -> bool:
    parts = span.strip().split()
    if not parts or not _COMMAND_TOKEN_RE.fullmatch(parts[0]):
        return False
    executable = parts[0]
    if executable.endswith(_INLINE_DATA_SUFFIXES) and not executable.startswith("./"):
        return False
    if len(parts) > 1:
        return True
    return executable.startswith("./") or (
        "/" in executable and executable.endswith((".bash", ".sh", ".zsh"))
    )


def _has_command_proof_source(clause: str) -> bool:
    if _BARE_COMMAND_PROOF_RE.search(clause):
        return True
    return any(
        _is_command_like_backtick_span(span)
        for span in _BACKTICK_SPAN_RE.findall(clause)
    )


def _has_successful_proof(response_text: str) -> bool:
    for clause in _PROOF_BOUNDARY_RE.split(response_text):
        has_proof_source = bool(
            _has_command_proof_source(clause)
            or _API_PROOF_RE.search(clause)
            or re.search(r"\bread[- ]back\b", clause, re.IGNORECASE)
        )
        if has_proof_source and _SUCCESS_RE.search(clause):
            return True
    return False


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
