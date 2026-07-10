"""Routing-truth diagnostics for ``hermes status --routing``."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace



def _routing_config() -> dict:
    return {
        "_config_version": 33,
        "model": {
            "default": "glm-4.7-flash",
            "model": "glm-4.7-flash",
            "provider": "custom:zeus",
            "api_key": "MAIN-SECRET",
        },
        "providers": {
            "zeus": {
                "api": "http://192.168.1.92:8080/v1",
                "key_env": "ZEUS_API_KEY",
                "default_model": "glm-4.7-flash",
            }
        },
        "delegation": {
            "provider": "custom:neuralwatt",
            "model": "glm-5.2-fast",
            "api_key": "FALLBACK-SECRET",
            "default_lane": "local_worker",
            "lanes": {
                "local_worker": {
                    "provider": "custom:zeus",
                    "model": "glm-4.7-flash",
                    "toolsets": ["terminal", "file"],
                },
                "smart_reviewer": {
                    "provider": "custom:neuralwatt",
                    "model": "glm-5.2",
                    "toolsets": ["file", "web"],
                },
            },
            "tier_routes": {
                "small": "local_worker",
                "review": "smart_reviewer",
            },
        },
        "auxiliary": {
            "title_generation": {
                "provider": "custom:zeus",
                "model": "glm-4.7-flash",
            },
            "compression": {
                "provider": "custom:neuralwatt",
                "model": "qwen3.5-397b-fast",
                "api_key": "AUX-SECRET",
            },
        },
    }


def test_status_parser_accepts_routing_flag():
    from hermes_cli.subcommands.status import build_status_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    handler = lambda _args: None
    build_status_parser(subparsers, cmd_status=handler)

    args = parser.parse_args(["status", "--routing", "--session", "session-123"])

    assert args.routing is True
    assert args.session == "session-123"
    assert args.func is handler


def test_show_status_routing_reports_config_sessions_lanes_aux_and_capacity(
    monkeypatch, capsys
):
    from hermes_cli import status as status_mod

    config = _routing_config()
    monkeypatch.setattr(status_mod, "load_config", lambda: config)
    monkeypatch.setattr(status_mod, "check_config_version", lambda: (32, 33))

    def fake_runtime(*, requested=None, target_model=None, explicit_base_url=None, **_kwargs):
        base_url = explicit_base_url or (
            "http://192.168.1.92:8080/v1" if requested == "custom:zeus" else "https://api.neuralwatt.test/v1"
        )
        return {
            "provider": "custom" if str(requested).startswith("custom:") else requested,
            "requested_provider": requested,
            "model": target_model,
            "base_url": base_url,
            "api_mode": "chat_completions",
            "api_key": "RUNTIME-SECRET",
            "source": "test",
        }

    monkeypatch.setattr(status_mod, "resolve_runtime_provider", fake_runtime)
    monkeypatch.setattr(
        status_mod,
        "_load_active_runtime_sessions",
        lambda limit=5, session_id=None: [
            {
                "id": "webui-session",
                "source": "webui",
                "model": "gpt-5.6-sol",
                "billing_provider": "openai-codex",
                "last_active": 123.0,
            }
        ],
    )
    monkeypatch.setattr(
        status_mod,
        "_probe_local_capacity",
        lambda _base_url, timeout=1.5: {
            "status": "reachable",
            "slots_total": 4,
            "slots_busy": 1,
            "context_lengths": [65536],
        },
    )

    status_mod.show_status(SimpleNamespace(routing=True, all=False, deep=False))

    out = capsys.readouterr().out
    assert "Effective Routing" in out
    assert "Config schema: 32 → 33" in out
    assert "Configured main: custom:zeus / glm-4.7-flash" in out
    assert "webui-session" in out
    assert "openai-codex / gpt-5.6-sol" in out
    assert "differs from configured main" in out
    assert "Default lane: local_worker" in out
    assert "local_worker: custom:zeus / glm-4.7-flash" in out
    assert "review → smart_reviewer" in out
    assert "title_generation" in out
    assert "4 slots, 1 busy, context 65,536" in out
    assert "MAIN-SECRET" not in out
    assert "FALLBACK-SECRET" not in out
    assert "AUX-SECRET" not in out
    assert "RUNTIME-SECRET" not in out


def test_probe_local_capacity_skips_public_endpoints_without_calling_opener():
    from hermes_cli import status as status_mod

    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("public endpoint must not be probed")

    result = status_mod._probe_local_capacity(
        "https://api.example.com/v1", opener=opener
    )

    assert result == {"status": "skipped_nonlocal"}
    assert called is False


def test_probe_local_capacity_parses_llama_slots():
    from hermes_cli import status as status_mod

    payload = [
        {"id": 0, "is_processing": False, "n_ctx": 65536},
        {"id": 1, "is_processing": True, "n_ctx": 65536},
    ]

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    seen = []

    def opener(request, timeout):
        seen.append((request.full_url, timeout))
        return FakeResponse()

    result = status_mod._probe_local_capacity(
        "http://192.168.1.92:8080/v1", timeout=0.25, opener=opener
    )

    assert seen == [("http://192.168.1.92:8080/slots", 0.25)]
    assert result == {
        "status": "reachable",
        "slots_total": 2,
        "slots_busy": 1,
        "context_lengths": [65536],
    }


def test_list_active_runtime_sessions_excludes_children_and_ended_rows(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("old-main", "webui", model="glm-4.7-flash")
        db.update_session_billing_route(
            "old-main", provider="custom:zeus", base_url="http://local/v1"
        )
        db._conn.execute(
            "UPDATE sessions SET started_at = started_at - 100 WHERE id = 'old-main'"
        )
        db._conn.commit()

        db.create_session("new-main", "webui", model="gpt-5.6-sol")
        db.update_session_billing_route(
            "new-main", provider="openai-codex", base_url="https://example.test/v1"
        )
        db.create_session(
            "child", "subagent", model="glm-5.2", parent_session_id="new-main"
        )
        db.create_session("ended", "cli", model="old-model")
        db.end_session("ended", "completed")

        rows = db.list_active_runtime_sessions(limit=5)

        assert [row["id"] for row in rows] == ["new-main", "old-main"]
        assert rows[0]["billing_provider"] == "openai-codex"
        assert rows[0]["last_active"] >= rows[0]["started_at"]

        exact = db.list_active_runtime_sessions(session_id="child")
        assert [row["id"] for row in exact] == ["child"]

        ended_exact = db.list_active_runtime_sessions(session_id="ended")
        assert [row["id"] for row in ended_exact] == ["ended"]
        assert ended_exact[0]["model"] == "old-model"
    finally:
        db.close()
