"""Tests for explicit model-switch persistence.

Covers:
- ``parse_model_flags`` recognises ``--session`` (and keeps ``--global``).
- ``resolve_persist_behavior`` persists only for an explicit ``--global``.
- ``--session`` wins when both persistence flags are supplied.
- Legacy ``model.persist_switch_by_default`` values no longer affect
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

    def test_global_flag(self):
        assert parse_model_flags("sonnet --global") == ("sonnet", "", True, False, False)

    def test_session_flag(self):
        assert parse_model_flags("sonnet --session") == (
            "sonnet",
            "",
            False,
            False,
            True,
        )

    def test_session_with_provider(self):
        assert parse_model_flags("sonnet --provider anthropic --session") == (
            "sonnet",
            "anthropic",
            False,
            False,
            True,
        )

    def test_refresh_flag_still_parsed(self):
        assert parse_model_flags("--refresh") == ("", "", False, True, False)

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
        # --session opts out even if the legacy config default is True.
        with _config({"model": {"persist_switch_by_default": True}}):
            assert resolve_persist_behavior(False, True) is False

    def test_global_flag_always_persists(self):
        # --global explicitly requests persistence.
        with _config({"model": {"persist_switch_by_default": False}}):
            assert resolve_persist_behavior(True, False) is True

    def test_no_flags_are_session_only_when_config_missing(self):
        with _config({}):
            assert resolve_persist_behavior(False, False) is False

    def test_no_flags_are_session_only_for_mapping_config(self):
        with _config({"model": {"default": "old-model"}}):
            assert resolve_persist_behavior(False, False) is False

    def test_legacy_persist_switch_by_default_true_is_ignored(self):
        with _config({"model": {"persist_switch_by_default": True}}):
            assert resolve_persist_behavior(False, False) is False

    def test_legacy_persist_switch_by_default_false_remains_session_only(self):
        with _config({"model": {"persist_switch_by_default": False}}):
            assert resolve_persist_behavior(False, False) is False

    def test_no_flags_are_session_only_when_model_is_flat_string(self):
        with _config({"model": ""}):
            assert resolve_persist_behavior(False, False) is False

    def test_session_overrides_global_when_both_set(self):
        # --session wins over --global when both flags are supplied.
        with _config({"model": {"persist_switch_by_default": True}}):
            assert resolve_persist_behavior(True, True) is False


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


class _config:
    """Patch the legacy config loader and prove the resolver does not read it."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def __enter__(self):
        self._patch = patch(
            "hermes_cli.config.load_config",
            return_value=self.cfg,
        )
        self.load_config = self._patch.start()
        return self

    def __exit__(self, *exc):
        if not exc:
            self.load_config.assert_not_called()
        self._patch.stop()
