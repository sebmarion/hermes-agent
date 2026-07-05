"""Read-only runtime diagnostics for model routing, latency symptoms, and voice setup."""

from __future__ import annotations

from collections import Counter, deque
from pathlib import Path
import importlib.util
import os
import re
import shutil
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML is a Hermes dependency, but keep doctor resilient.
    yaml = None  # type: ignore[assignment]

from hermes_cli.config import get_hermes_home

HERMES_HOME = get_hermes_home()


LOG_PATTERNS: dict[str, re.Pattern[str]] = {
    "provider_connection_error": re.compile(
        r"\b(APIConnectionError|API connection error|connection error|RemoteProtocolError|ConnectError)\b",
        re.IGNORECASE,
    ),
    "provider_timeout": re.compile(
        r"\b(timeout|timed out|TimeoutError)\b|\belapsed=\d+(?:\.\d+)?s\b|\b\d{3,}(?:\.\d+)?s\b",
        re.IGNORECASE,
    ),
    "compression_warning": re.compile(
        r"(context compression|compression warning|compression failed|compressed context)",
        re.IGNORECASE,
    ),
    "composer_unavailable": re.compile(r"Composer is not available", re.IGNORECASE),
    "gateway_exit_nonzero": re.compile(
        r"(gateway\.exit_nonzero|asyncio\.run\.returned success=false|tui_gateway_crash|KeyboardInterrupt)",
        re.IGNORECASE,
    ),
}

SECRET_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api[_-]?key[\"'=:\s]+)[^\s,;}]+"),
    re.compile(r"(?i)(authorization[\"'=:\s]+)[^\s,;}]+"),
    re.compile(r"(?i)(token[\"'=:\s]+)[^\s,;}]+"),
)

CONFIG_SECRET_KEYS = {"api_key", "key", "token", "secret", "password", "access_token", "refresh_token"}
ENV_KEYS_OF_INTEREST = (
    "HERMES_AGENT_PROFILE",
    "HERMES_PROFILE",
    "HERMES_INFERENCE_PROVIDER",
    "HERMES_MODEL",
    "OPENAI_BASE_URL",
    "OPENROUTER_BASE_URL",
    "ANTHROPIC_BASE_URL",
    "STT_PROVIDER",
    "HERMES_LOCAL_STT_COMMAND",
    "HERMES_LOCAL_STT_LANGUAGE",
)


def _read_yaml_config(home: Path) -> dict[str, Any]:
    config_path = home / "config.yaml"
    if not config_path.exists() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_runtime_config(home: Path) -> dict[str, Any]:
    # The active profile may include managed overlays; use Hermes' normal loader
    # only for the real HERMES_HOME. Tests and offline audits can pass a temp home.
    try:
        if home.resolve() == Path(HERMES_HOME).resolve():
            from hermes_cli.config import load_config

            cfg = load_config() or {}
            return cfg if isinstance(cfg, dict) else {}
    except Exception:
        pass
    return _read_yaml_config(home)


def _safe_value(value: Any) -> str:
    if value is None:
        return "unset"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    return text if text else "unset"


def _section(cfg: dict[str, Any], key: str) -> dict[str, Any]:
    value = cfg.get(key) or {}
    return value if isinstance(value, dict) else {"default": value}


def _scrub_line(line: str) -> str:
    scrubbed = line.strip()
    for pattern in SECRET_LINE_PATTERNS:
        scrubbed = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", scrubbed)
    if len(scrubbed) > 220:
        scrubbed = scrubbed[:217] + "..."
    return scrubbed


def _tail_lines(path: Path, max_lines: int) -> list[tuple[int, str]]:
    if max_lines <= 0:
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            window: deque[tuple[int, str]] = deque(maxlen=max_lines)
            for line_no, line in enumerate(handle, start=1):
                window.append((line_no, line.rstrip("\n")))
            return list(window)
    except OSError:
        return []


def _log_files(home: Path) -> list[Path]:
    logs_dir = home / "logs"
    if not logs_dir.is_dir():
        return []
    candidates = [
        "desktop.log",
        "gateway.log",
        "gateway-exit-diag.log",
        "tui_gateway.log",
        "tui_gateway_crash.log",
        "hermes.log",
    ]
    files: list[Path] = []
    for name in candidates:
        path = logs_dir / name
        if path.exists() and path.is_file():
            files.append(path)
    for path in sorted(logs_dir.glob("*.log")):
        if path not in files:
            files.append(path)
    return files


def _scan_logs(home: Path, *, log_lines: int) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}
    files_scanned: list[str] = []
    for path in _log_files(home):
        files_scanned.append(str(path))
        for line_no, line in _tail_lines(path, log_lines):
            for label, pattern in LOG_PATTERNS.items():
                if not pattern.search(line):
                    continue
                counts[label] += 1
                examples[label] = f"{path.name}:{line_no}: {_scrub_line(line)}"
    return {
        "files_scanned": files_scanned,
        "line_window_per_file": log_lines,
        "signals": [
            {"label": label, "count": counts.get(label, 0), "latest": examples.get(label, "")}
            for label in LOG_PATTERNS
        ],
    }


def _find_spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _env_summary() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for key in ENV_KEYS_OF_INTEREST:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            continue
        value = "[REDACTED]" if any(part in key.lower() for part in CONFIG_SECRET_KEYS) else raw
        rows.append({"key": key, "value": value})
    return rows


def collect_runtime_diagnostics(home: str | Path | None = None, *, log_lines: int = 2000) -> dict[str, Any]:
    """Collect a read-only snapshot of runtime routing and recent failure signals."""
    root = Path(home).expanduser() if home is not None else Path(HERMES_HOME)
    cfg = _load_runtime_config(root)
    model_cfg = _section(cfg, "model")
    stt_cfg = _section(cfg, "stt")
    tts_cfg = _section(cfg, "tts")
    compression_cfg = _section(cfg, "compression")

    return {
        "home": str(root),
        "config_path": str(root / "config.yaml"),
        "model": {
            "provider": _safe_value(model_cfg.get("provider")),
            "default": _safe_value(model_cfg.get("default") or model_cfg.get("model")),
            "api_mode": _safe_value(model_cfg.get("api_mode")),
            "base_url": _safe_value(model_cfg.get("base_url")),
            "service_tier": _safe_value(model_cfg.get("service_tier")),
            "fallbacks": _safe_value(model_cfg.get("fallbacks") or model_cfg.get("fallback_models")),
        },
        "compression": {
            "enabled": _safe_value(compression_cfg.get("enabled")),
            "threshold": _safe_value(compression_cfg.get("threshold")),
            "model": _safe_value(compression_cfg.get("model")),
        },
        "voice": {
            "stt_enabled": _safe_value(stt_cfg.get("enabled", True)),
            "stt_provider": _safe_value(stt_cfg.get("provider") or os.environ.get("STT_PROVIDER") or "local"),
            "stt_model": _safe_value(stt_cfg.get("model") or (stt_cfg.get("local") or {}).get("model") or "base"),
            "stt_language": _safe_value(stt_cfg.get("language") or os.environ.get("HERMES_LOCAL_STT_LANGUAGE")),
            "tts_enabled": _safe_value(tts_cfg.get("enabled", True)),
            "tts_provider": _safe_value(tts_cfg.get("provider")),
            "tts_model": _safe_value(tts_cfg.get("model")),
            "modules": {
                "faster_whisper": _find_spec("faster_whisper"),
                "openai": _find_spec("openai"),
                "sounddevice": _find_spec("sounddevice"),
            },
            "binaries": {
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "whisper": bool(shutil.which("whisper")),
            },
        },
        "env_overrides": _env_summary(),
        "logs": _scan_logs(root, log_lines=log_lines),
    }


def _status(ok: bool) -> str:
    return "OK" if ok else "WARN"


def format_runtime_diagnostics(report: dict[str, Any]) -> str:
    """Render diagnostics as redacted text for terminal output or bug reports."""
    model = report["model"]
    voice = report["voice"]
    compression = report["compression"]
    logs = report["logs"]

    lines = [
        "Hermes Runtime Diagnostics (read-only)",
        f"Home: {report['home']}",
        f"Config: {report['config_path']}",
        "",
        "Model routing",
        f"  provider: {model['provider']}",
        f"  model: {model['default']}",
        f"  api_mode: {model['api_mode']}",
        f"  base_url: {model['base_url']}",
        f"  service_tier: {model['service_tier']}",
        f"  fallbacks: {model['fallbacks']}",
        "",
        "Compression",
        f"  enabled: {compression['enabled']}",
        f"  threshold: {compression['threshold']}",
        f"  model: {compression['model']}",
        "",
        "Voice / dictation",
        f"  STT: enabled={voice['stt_enabled']} provider={voice['stt_provider']} model={voice['stt_model']} language={voice['stt_language']}",
        f"  TTS: enabled={voice['tts_enabled']} provider={voice['tts_provider']} model={voice['tts_model']}",
        "  local modules:",
    ]
    for key, available in voice["modules"].items():
        lines.append(f"    {_status(bool(available))}: {key}")
    lines.append("  local binaries:")
    for key, available in voice["binaries"].items():
        lines.append(f"    {_status(bool(available))}: {key}")

    lines.extend(["", "Environment overrides"])
    overrides = report.get("env_overrides") or []
    if overrides:
        for row in overrides:
            lines.append(f"  {row['key']}={row['value']}")
    else:
        lines.append("  none detected")

    lines.extend([
        "",
        f"Recent log signals (last {logs['line_window_per_file']} lines per file)",
    ])
    if logs.get("files_scanned"):
        lines.append(f"  files: {', '.join(Path(p).name for p in logs['files_scanned'])}")
    else:
        lines.append("  files: none")
    for signal in logs["signals"]:
        status = "WARN" if signal["count"] else "OK"
        lines.append(f"  {status}: {signal['label']} count={signal['count']}")
        if signal.get("latest"):
            lines.append(f"    latest: {signal['latest']}")

    lines.extend([
        "",
        "Gates for reliability work",
        "  - model routing is not proven until provider/model/api_mode above match the intended lane",
        "  - latency regressions are not cleared while provider_timeout/provider_connection_error counts remain new",
        "  - response-sanity work must include compression_warning counts and transcript/context evidence",
        "  - dictation recovery must pass UI state tests plus a real recorder/transcription smoke test",
    ])
    return "\n".join(lines)


def print_runtime_diagnostics(*, home: str | Path | None = None, log_lines: int = 2000) -> None:
    print(format_runtime_diagnostics(collect_runtime_diagnostics(home=home, log_lines=log_lines)))
