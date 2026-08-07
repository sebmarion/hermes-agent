"""Behavioral contract for the report-only completion proof gatekeeper."""

import importlib.util
from pathlib import Path


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
