"""Behavioral contract for the report-only completion proof gatekeeper."""

import importlib.util
from pathlib import Path

import pytest


PLUGIN_PATH = (
    Path(__file__).parents[2]
    / "plugins"
    / "completion-proof-gatekeeper"
    / "__init__.py"
)


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "completion_proof_gatekeeper_test", PLUGIN_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bare_done_claim_is_reported_without_proof():
    plugin = _load_plugin()

    result = plugin.transform_llm_output(response_text="Done.")

    assert result is not None
    assert result.startswith("Done.")
    assert "completion claim" in result
    assert "report-only" in result


def test_successful_command_proof_is_left_unchanged():
    plugin = _load_plugin()
    response = "Fixed the handler. PASS: `pytest tests/test_api.py` (exit 0)."

    assert plugin.transform_llm_output(response_text=response) is None


def test_api_readback_proof_is_left_unchanged():
    plugin = _load_plugin()
    response = "Fixed the health route; GET /health returned HTTP 200 and the read-back matched."

    assert plugin.transform_llm_output(response_text=response) is None


def test_honest_blocker_is_left_unchanged():
    plugin = _load_plugin()
    response = "I fixed the handler, but I could not verify it because the test service is unavailable."

    assert plugin.transform_llm_output(response_text=response) is None


@pytest.mark.parametrize(
    "response",
    [
        "The work is not done.",
        "The bug is not fixed.",
        "The bug isn't fixed.",
        "The implementation is not working.",
        "The implementation isn't working.",
        "The result is not verified.",
        "The change is not deployed.",
        "The commit is not pushed.",
        "The release is not published.",
        "The bug is not resolved.",
        "The implementation is not complete.",
        "The work is not completed.",
        "The tests are not passing.",
        "The checks are not green.",
        "The build did not pass.",
    ],
)
def test_explicit_negated_completion_claim_is_left_unchanged(response):
    plugin = _load_plugin()

    assert plugin.transform_llm_output(response_text=response) is None


def test_generic_api_mention_with_unrelated_success_is_not_proof():
    plugin = _load_plugin()
    response = "Fixed the API bug; the migration was successful."

    result = plugin.transform_llm_output(response_text=response)

    assert result is not None
    assert "report-only" in result


def test_arbitrary_inline_code_with_unrelated_success_is_not_proof():
    plugin = _load_plugin()
    response = "Fixed the handler in `handler.py`; the migration was successful."

    result = plugin.transform_llm_output(response_text=response)

    assert result is not None
    assert "report-only" in result


def test_command_named_inline_file_with_unrelated_success_is_not_proof():
    plugin = _load_plugin()
    response = "Fixed the handler in `pytest.ini` and the migration was successful."

    result = plugin.transform_llm_output(response_text=response)

    assert result is not None
    assert "report-only" in result


@pytest.mark.parametrize(
    "proof_source",
    [
        "Ran pytest.",
        "GET /health was called.",
        "Captured a read-back.",
    ],
)
def test_proof_source_and_unrelated_success_in_different_clauses_is_not_proof(
    proof_source,
):
    plugin = _load_plugin()
    response = f"Fixed the handler. {proof_source} The migration was successful."

    result = plugin.transform_llm_output(response_text=response)

    assert result is not None
    assert "report-only" in result


def test_command_exited_with_code_zero_is_proof():
    plugin = _load_plugin()
    response = "Fixed the handler. `pytest tests/test_api.py` exited with code 0."

    assert plugin.transform_llm_output(response_text=response) is None


def test_failed_verification_keeps_the_claim_reportable():
    plugin = _load_plugin()
    response = "Fixed the handler. Ran `pytest tests/test_api.py`, but the suite failed."

    result = plugin.transform_llm_output(response_text=response)

    assert result is not None
    assert "report-only" in result


def test_registers_the_existing_transform_hook():
    plugin = _load_plugin()
    registered = []

    class Context:
        def register_hook(self, name, callback):
            registered.append((name, callback))

    plugin.register(Context())

    assert registered == [("transform_llm_output", plugin.transform_llm_output)]


def test_normal_explanation_without_completion_claim_is_left_unchanged():
    plugin = _load_plugin()
    response = "The handler now returns a structured error payload for invalid input."

    assert plugin.transform_llm_output(response_text=response) is None
