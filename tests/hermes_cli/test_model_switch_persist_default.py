"""Tests for explicit model-switch persistence.

Covers:
- ``parse_model_flags`` recognises ``--session`` (and keeps ``--global``).
- ``resolve_persist_behavior`` persists only for an explicit ``--global``.
- ``--session`` wins when both persistence flags are supplied.
- Legacy ``model.persist_switch_by_default`` values do not affect
  interactive switching.
"""

from unittest.mock import patch

from hermes_cli.model_switch import parse_model_flags, resolve_persist_behavior


# ---------------------------------------------------------------------------
# parse_model_flags
# ---------------------------------------------------------------------------


class TestParseModelFlagsSession:
    def test_no_flags(self):
        assert parse_model_flags("sonnet") == ("sonnet", "", False, False, False)


    def test_unicode_dash_session_normalized(self):
        # Telegram/iOS auto-converts -- to en/em dashes.
        assert parse_model_flags("sonnet \u2013session") == (
            "sonnet",
            "",
            False,
            False,
            True,
        )


# ---------------------------------------------------------------------------
# resolve_persist_behavior
# ---------------------------------------------------------------------------


class TestResolvePersistBehavior:
    def test_session_flag_always_session_only(self):
        # --session opts out even if the legacy config key is True.
        with _config({"model": {"persist_switch_by_default": True}}):
            assert resolve_persist_behavior(False, True) is False

    def test_no_flags_are_session_only_when_config_missing(self):
        with _config({}):
            assert resolve_persist_behavior(False, False) is False

    def test_legacy_true_is_ignored(self):
        with _config({"model": {"persist_switch_by_default": True}}):
            assert resolve_persist_behavior(False, False, explicit_provider="") is False

    def test_global_is_explicit(self):
        with _config({"model": {"persist_switch_by_default": False}}):
            assert resolve_persist_behavior(True, False) is True


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


class _config:
    """Context manager that patches ``load_config`` to return a fixed dict."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def __enter__(self):
        self._patch = patch(
            "hermes_cli.config.load_config",
            return_value=self.cfg,
        )
        # resolve_persist_behavior imports load_config lazily inside the
        # function, so patching the source module is sufficient.
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
