"""Contracts for model-routing diagnosis and troubleshooting discoverability."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _compact_markdown(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_troubleshooting_distinguishes_persisted_config_from_active_route():
    text = _compact_markdown(ROOT / "docs" / "troubleshooting.md")

    assert "persisted configured state" in text
    assert "does not show classic CLI or gateway session overrides" in text
    assert "Model section" in text
    assert "Do not paste or share the full output" in text
    assert "active UI or session" in text
    assert "active non-secret routing fields" not in text


def test_troubleshooting_describes_bare_switches_as_non_persistent():
    text = _compact_markdown(ROOT / "docs" / "troubleshooting.md")

    assert "non-persistent and session-only" in text
    assert "safe and session-only" not in text


def test_model_routing_troubleshooting_is_linked_from_existing_faq():
    faq = (ROOT / "website" / "docs" / "reference" / "faq.md").read_text(
        encoding="utf-8"
    )
    cli_reference = (
        ROOT / "website" / "docs" / "reference" / "cli-commands.md"
    ).read_text(encoding="utf-8")

    assert "docs/troubleshooting.md" in faq
    stale_claim = (
        "Provider and base URL changes are persisted to `config.yaml` automatically"
    )
    assert stale_claim not in faq
    assert (
        stale_claim not in cli_reference
    )
    assert "Only `--global` saves the provider/model/base URL route" in cli_reference
