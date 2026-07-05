from pathlib import Path

from hermes_cli.runtime_diagnostics import collect_runtime_diagnostics, format_runtime_diagnostics


def test_runtime_diagnostics_collects_config_and_recent_log_signals(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        """
model:
  provider: openai-codex
  default: gpt-5.5
  api_mode: codex_responses
compression:
  enabled: true
stt:
  enabled: true
  provider: local
  model: base
tts:
  enabled: true
  provider: kittentts
""".strip(),
        encoding="utf-8",
    )
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "desktop.log").write_text(
        "\n".join(
            [
                "info: startup",
                "warning: context compression warning: budget exceeded",
                "error: APIConnectionError token=sk-secret-123 request timed out after 808.04s",
                "error: Composer is not available",
            ]
        ),
        encoding="utf-8",
    )
    (logs / "gateway-exit-diag.log").write_text(
        "gateway.exit_nonzero success=false\n", encoding="utf-8"
    )

    report = collect_runtime_diagnostics(home=tmp_path, log_lines=20)

    assert report["model"]["provider"] == "openai-codex"
    assert report["model"]["default"] == "gpt-5.5"
    signals = {row["label"]: row for row in report["logs"]["signals"]}
    assert signals["provider_connection_error"]["count"] == 1
    assert signals["provider_timeout"]["count"] == 1
    assert signals["compression_warning"]["count"] == 1
    assert signals["composer_unavailable"]["count"] == 1
    assert signals["gateway_exit_nonzero"]["count"] == 1

    rendered = format_runtime_diagnostics(report)
    assert "openai-codex" in rendered
    assert "gpt-5.5" in rendered
    assert "sk-secret-123" not in rendered
    assert "token=[REDACTED]" in rendered


def test_runtime_diagnostics_keeps_log_scan_to_requested_tail(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "desktop.log").write_text(
        "APIConnectionError old\ninfo line\nAPIConnectionError new\n", encoding="utf-8"
    )

    report = collect_runtime_diagnostics(home=tmp_path, log_lines=1)

    signals = {row["label"]: row for row in report["logs"]["signals"]}
    assert signals["provider_connection_error"]["count"] == 1
    assert "new" in signals["provider_connection_error"]["latest"]
