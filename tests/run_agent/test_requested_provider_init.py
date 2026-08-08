"""Regression coverage for requested-provider constructor threading."""

from run_agent import AIAgent


def test_aiagent_forwards_requested_provider_to_initializer(monkeypatch):
    captured = {}

    def fake_init_agent(agent, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("agent.agent_init.init_agent", fake_init_agent)

    AIAgent(provider="custom", requested_provider="custom:probe")

    assert captured["provider"] == "custom"
    assert captured["requested_provider"] == "custom:probe"
