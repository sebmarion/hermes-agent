"""Gateway supervisors preserve the immutable release identity."""

from __future__ import annotations

import plistlib
import re

from gateway.runtime_identity import IDENTITY_ENV_FIELDS
from hermes_cli import gateway as gateway_cli


def _complete_identity_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for index, field in enumerate(IDENTITY_ENV_FIELDS, start=1):
        if field.value_kind == "path":
            values[field.env_name] = f"/opt/Hermes Release {index}/artifact"
        elif field.value_kind == "oid":
            values[field.env_name] = f"{index % 10}" * 40
        elif field.value_kind == "sha256":
            values[field.env_name] = f"{index % 10}" * 64
        elif field.value_kind == "positive_int":
            values[field.env_name] = str(index)
        elif field.value_kind == "launchd_label":
            values[field.env_name] = f"ai.hermes.gateway.pair-{index}"
        else:
            values[field.env_name] = f"receipt-{index}.abc-123"
    return values


def _systemd_environment(unit: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in unit.splitlines():
        match = re.fullmatch(r'Environment="([^=]+)=(.*)"', line)
        if not match:
            continue
        key, escaped = match.groups()
        parsed[key] = escaped.replace(r'\"', '"').replace(r"\\", "\\")
    return parsed


def test_systemd_unit_round_trips_sealed_identity(monkeypatch):
    expected = _complete_identity_env()
    for key, value in expected.items():
        monkeypatch.setenv(key, value)

    unit = gateway_cli.generate_systemd_unit(system=False)

    actual = _systemd_environment(unit)
    assert {key: actual[key] for key in expected} == expected


def test_launchd_plist_round_trips_sealed_identity(monkeypatch):
    expected = _complete_identity_env()
    for key, value in expected.items():
        monkeypatch.setenv(key, value)

    document = plistlib.loads(gateway_cli.generate_launchd_plist().encode("utf-8"))

    actual = document["EnvironmentVariables"]
    assert {key: actual[key] for key in expected} == expected


def test_service_definitions_omit_missing_or_invalid_identity(monkeypatch):
    for field in IDENTITY_ENV_FIELDS:
        monkeypatch.delenv(field.env_name, raising=False)
    monkeypatch.setenv("HERMES_AGENT_COMMIT", "bad\nvalue")

    unit = gateway_cli.generate_systemd_unit(system=False)
    document = plistlib.loads(gateway_cli.generate_launchd_plist().encode("utf-8"))

    assert "HERMES_AGENT_COMMIT" not in _systemd_environment(unit)
    assert "HERMES_AGENT_COMMIT" not in document["EnvironmentVariables"]
    for field in IDENTITY_ENV_FIELDS[1:]:
        assert field.env_name not in unit
        assert field.env_name not in document["EnvironmentVariables"]
