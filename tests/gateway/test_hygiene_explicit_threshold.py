"""Respect explicit large-context thresholds without lowering gateway safety defaults."""
from types import SimpleNamespace

import pytest

from gateway.run_turn_hygiene import GatewayTurnHygieneMixin


class HygieneRunner(GatewayTurnHygieneMixin):
    async def _session_has_compression_in_flight(self, _session_key):
        return False


async def plan_for(threshold, prompt_tokens):
    runner = HygieneRunner()
    settings = SimpleNamespace(
        model="inkling-2-mini", threshold_pct=0.85, compression_enabled=True,
        hard_msg_limit=5000, timeout_seconds=30.0, total_ceiling_seconds=600.0,
        max_turn_hold_seconds=10.0, failure_cooldown_seconds=300.0,
        config_context_length=None, provider=None, base_url=None,
    )
    compression = {} if threshold is None else {"threshold": threshold}
    runner._hmwa_hygiene_read_config(settings, {
        "model": {"default": "inkling-2-mini", "provider": "custom:inkling",
                  "base_url": "http://127.0.0.1:18081/v1", "context_length": 262144},
        "compression": compression,
    })
    settings.api_key = None
    entry = SimpleNamespace(last_prompt_tokens=prompt_tokens, session_id="threshold-session")
    return await runner._hmwa_hygiene_plan(
        settings, [{"role": "user", "content": "existing history"}],
        entry, "agent:main:cli:threshold-session",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold,below,at", [
    (0.86, 225442, 225443),
    (0.98, 256900, 256901),
    ("0.98", 256900, 256901),
])
async def test_explicit_higher_threshold_controls_real_usage_hygiene_plan(threshold, below, at):
    """A higher owner threshold must not compact history at the old 85% gate."""
    assert not (await plan_for(threshold, below)).needs_compress
    assert (await plan_for(threshold, at)).needs_compress


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [
    None, 0.5, 0.85, 0, -1, 1, 1.2, True, False, "invalid", "NaN", "Infinity",
])
async def test_missing_low_or_invalid_threshold_keeps_safety_net(threshold):
    """Neither a lower threshold nor malformed input can weaken default hygiene."""
    assert not (await plan_for(threshold, 222821)).needs_compress
    assert (await plan_for(threshold, 222822)).needs_compress
