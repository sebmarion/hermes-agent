"""Provider-level proof for delegated workspace identity."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _tool_response() -> dict:
    return {
        "id": "fake",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_pwd",
                            "type": "function",
                            "function": {
                                "name": "terminal",
                                "arguments": json.dumps({"command": "pwd"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def _text_response() -> dict:
    return {
        "id": "fake",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Executed successfully."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


class _ProviderHandler(BaseHTTPRequestHandler):
    response_queue: list[dict] = []
    request_log: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length).decode())
        type(self).request_log.append(request)
        response = (
            type(self).response_queue.pop(0)
            if request.get("tools") and type(self).response_queue
            else _text_response()
        )
        message = response["choices"][0]["message"]

        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {
                    "id": "fake",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None,
                        }
                    ],
                }
            ]
            if message.get("tool_calls"):
                call = message["tool_calls"][0]
                chunks.append(
                    {
                        "id": "fake",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": call["id"],
                                            "type": "function",
                                            "function": call["function"],
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            else:
                chunks.append(
                    {
                        "id": "fake",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": message["content"]},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            chunks.append(
                {
                    "id": "fake",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": (
                                "tool_calls" if message.get("tool_calls") else "stop"
                            ),
                        }
                    ],
                }
            )
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture()
def provider_url(tmp_path, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    _ProviderHandler.request_log = []
    _ProviderHandler.response_queue = []
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _parent(base_url: str) -> MagicMock:
    parent = MagicMock()
    parent.base_url = base_url
    parent.api_key = "test-key"
    parent.provider = "custom"
    parent.api_mode = "chat_completions"
    parent.model = "fake-model"
    parent.platform = "cli"
    parent.enabled_toolsets = ["terminal", "delegation"]
    parent.disabled_toolsets = []
    parent.valid_tool_names = {"terminal", "delegate_task"}
    parent._session_db = None
    parent._memory_manager = None
    parent._delegate_depth = 0
    parent._current_task_id = "parent-turn"
    parent._current_turn_id = "parent-turn-id"
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._interrupt_requested = False
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.reasoning_config = None
    parent.prefill_messages = None
    parent.max_tokens = None
    parent._fallback_chain = None
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent.provider_require_parameters = False
    parent.provider_data_collection = None
    parent.openrouter_min_coding_score = None
    parent.request_overrides = {}
    parent.acp_command = None
    parent.acp_args = []
    parent._client_kwargs = {"base_url": base_url, "api_key": "test-key"}
    parent.session_id = "parent-session"
    parent.session_estimated_cost_usd = 0.0
    parent.session_cost_source = "none"
    parent.session_cost_status = "unknown"
    return parent


def test_background_provider_payload_and_pwd_use_dispatch_workspace(
    provider_url, monkeypatch, tmp_path
):
    from gateway import session_context
    from tools import async_delegation, delegate_tool, terminal_tool

    dispatch_workspace = tmp_path / "dispatch-workspace"
    later_workspace = tmp_path / "later-workspace"
    dispatch_workspace.mkdir()
    later_workspace.mkdir()

    monkeypatch.setenv("TERMINAL_CWD", str(later_workspace))
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    terminal_tool.record_session_cwd("parent-turn", str(dispatch_workspace))

    _ProviderHandler.response_queue = [_tool_response(), _text_response()]
    monkeypatch.setattr(
        delegate_tool,
        "_load_config",
        lambda: {
            "max_iterations": 5,
            "provider": "custom:fake",
            "model": "fake-model",
            "base_url": provider_url,
            "api_key": "test-key",
        },
    )
    monkeypatch.setattr(session_context, "async_delivery_supported", lambda: True)

    captured = {}

    def capture_dispatch(**kwargs):
        captured["runner"] = kwargs["runner"]
        return {"status": "dispatched", "delegation_id": "captured-delegation"}

    monkeypatch.setattr(
        async_delegation,
        "dispatch_async_delegation_batch",
        capture_dispatch,
    )

    dispatched = json.loads(
        delegate_tool.delegate_task(
            goal="Run pwd in the dispatch workspace",
            context="Keep this provider request local.",
            background=True,
            parent_agent=_parent(provider_url),
        )
    )
    assert dispatched["status"] == "dispatched"

    terminal_tool.record_session_cwd("parent-turn", str(later_workspace))
    combined = captured["runner"]()
    assert combined["results"][0]["status"] == "completed"

    provider_requests = [
        request for request in _ProviderHandler.request_log if request.get("tools")
    ]
    assert len(provider_requests) >= 2
    first_system = provider_requests[0]["messages"][0]["content"]
    expected = str(dispatch_workspace.resolve())
    assert "YOUR TASK:\nRun pwd in the dispatch workspace" in first_system
    assert "CONTEXT:\nKeep this provider request local." in first_system
    assert f"WORKSPACE PATH:\n{expected}" in first_system

    tool_messages = [
        message
        for message in provider_requests[-1]["messages"]
        if message.get("role") == "tool"
    ]
    assert tool_messages
    assert expected in str(tool_messages[-1]["content"])
    assert str(later_workspace) not in str(tool_messages[-1]["content"])


def test_container_provider_payload_and_tools_share_one_non_host_workspace(
    provider_url, monkeypatch
):
    import run_agent

    from agent import coding_context, runtime_cwd
    from tools import delegate_tool, file_tools, terminal_tool

    remote_workspace = "/workspace/task42"
    host_checkout = str(Path(__file__).resolve().parents[2])
    observations: list[dict[str, str]] = []

    class FakeDockerEnvironment:
        def __init__(self):
            self.cwd = remote_workspace
            self.env = {}

        def execute(self, _command, **kwargs):
            child_keys = [
                key
                for key, value in terminal_tool._session_cwd.items()
                if key != "parent-turn" and value == remote_workspace
            ]
            assert len(child_keys) == 1
            child_key = child_keys[0]
            observed = {
                "raw": runtime_cwd.get_session_cwd_override() or "",
                "runtime": str(runtime_cwd.resolve_agent_cwd()),
                "session": terminal_tool.get_session_cwd(child_key) or "",
                "file": str(file_tools._resolve_base_dir(child_key)),
                "terminal": str(kwargs["cwd"]),
            }
            observations.append(observed)
            return {
                "output": "\n".join(f"{key}={value}" for key, value in observed.items()),
                "returncode": 0,
            }

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CWD", remote_workspace)
    monkeypatch.setattr(run_agent, "build_environment_hints", lambda: "")
    monkeypatch.setattr(coding_context, "_coding_mode", lambda _config: "on")
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(
        terminal_tool,
        "_task_env_overrides",
        {"parent-turn": {"env_type": "docker"}},
    )
    monkeypatch.setattr(
        terminal_tool,
        "_active_environments",
        {"default": FakeDockerEnvironment()},
    )
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})
    terminal_tool.record_session_cwd("parent-turn", remote_workspace)

    _ProviderHandler.response_queue = [_tool_response(), _text_response()]
    monkeypatch.setattr(
        delegate_tool,
        "_load_config",
        lambda: {
            "max_iterations": 5,
            "provider": "custom:fake",
            "model": "fake-model",
            "base_url": provider_url,
            "api_key": "test-key",
        },
    )

    result = json.loads(
        delegate_tool.delegate_task(
            goal="Run pwd in the exact container workspace",
            context="Do not use a host checkout.",
            parent_agent=_parent(provider_url),
        )
    )

    assert result["results"][0]["status"] == "completed"
    provider_requests = [
        request for request in _ProviderHandler.request_log if request.get("tools")
    ]
    assert len(provider_requests) >= 2
    first_system = provider_requests[0]["messages"][0]["content"]
    assert f"WORKSPACE PATH:\n{remote_workspace}" in first_system
    assert host_checkout not in first_system

    expected = {
        "raw": remote_workspace,
        "runtime": remote_workspace,
        "session": remote_workspace,
        "file": remote_workspace,
        "terminal": remote_workspace,
    }
    assert observations == [expected]
    tool_messages = [
        message
        for message in provider_requests[-1]["messages"]
        if message.get("role") == "tool"
    ]
    assert tool_messages
    for key, value in expected.items():
        assert f"{key}={value}" in str(tool_messages[-1]["content"])
    assert host_checkout not in str(tool_messages[-1]["content"])
