"""Structural contracts for model-routing troubleshooting guidance."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TROUBLESHOOTING = ROOT / "docs" / "troubleshooting.md"
FAQ = ROOT / "website" / "docs" / "reference" / "faq.md"
CLI_REFERENCE = ROOT / "website" / "docs" / "reference" / "cli-commands.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _paragraphs(text: str) -> list[str]:
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", text)]


def test_model_routing_troubleshooting_exists_and_is_linked_from_faq():
    assert TROUBLESHOOTING.is_file()

    faq = _read(FAQ)
    assert re.search(
        r"\[[^\]]+\]\([^)]*docs/troubleshooting\.md(?:#[^)]*)?\)",
        faq,
    )


def test_reference_docs_do_not_claim_routes_persist_automatically():
    references = _paragraphs(_read(FAQ)) + _paragraphs(_read(CLI_REFERENCE))
    automatic_persistence = re.compile(
        r"(?:automatic(?:ally)?|by default).{0,80}\b(?:persist|sav)"
        r"|\b(?:persist|sav)\w*.{0,80}(?:automatic(?:ally)?|by default)",
        re.IGNORECASE,
    )

    routing_paragraphs = (
        paragraph
        for paragraph in references
        if "provider" in paragraph.lower()
        and any(
            term in paragraph.lower()
            for term in ("base url", "endpoint", "route")
        )
    )
    assert not any(
        automatic_persistence.search(paragraph) for paragraph in routing_paragraphs
    )


def test_persisted_config_check_keeps_secret_output_private():
    text = _read(TROUBLESHOOTING)
    normalized = " ".join(text.split()).lower()

    assert "hermes config show" in normalized
    assert "config.yaml" in normalized
    privacy_warning = next(
        (
            paragraph.lower()
            for paragraph in _paragraphs(text)
            if "api key" in paragraph.lower()
        ),
        "",
    )
    assert any(term in privacy_warning for term in ("do not", "don't", "never"))
    assert any(term in privacy_warning for term in ("token", "cookie", "auth"))


def test_troubleshooting_uses_real_session_diagnostic_surfaces():
    text = _read(TROUBLESHOOTING)
    paragraphs = _paragraphs(text)

    classic_config = next(
        (paragraph for paragraph in paragraphs if "`/config`" in paragraph),
        "",
    )
    gateway_model = next(
        (
            paragraph
            for paragraph in paragraphs
            if "`/model`" in paragraph and "gateway" in paragraph.lower()
        ),
        "",
    )

    assert "model" in classic_config.lower()
    assert "base url" in classic_config.lower()
    assert "model" in gateway_model.lower()
    assert "provider" in gateway_model.lower()

    normalized = " ".join(text.split())
    assert re.search(
        r"(?:neither|not|does not|cannot).{0,120}(?:endpoint|`?api_mode`?)"
        r"|(?:endpoint|`?api_mode`?).{0,120}(?:neither|not|does not|cannot)",
        normalized,
        re.IGNORECASE,
    )


def test_troubleshooting_avoids_misleading_route_claims():
    text = " ".join(_read(TROUBLESHOOTING).split())

    assert not re.search(
        r"\b(?:inspect|show|report)\w*.{0,60}\bactive\b.{0,40}\b(?:session|route)",
        text,
        re.IGNORECASE,
    )
    assert not re.search(r"\bbare\b[^.]{0,120}\bsafe\b", text, re.IGNORECASE)
