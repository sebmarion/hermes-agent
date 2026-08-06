"""The configured local deployment policy permits the 32K Ornith model."""


def test_hermes_minimum_context_policy_is_32k_for_local_models():
    from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

    assert MINIMUM_CONTEXT_LENGTH == 32_768
