"""End-to-end delegate execution against a local OpenAI-compatible provider."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

import pytest


def _tool_response():
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
                            "id": "call_1",
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


def _text_response():
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


class _Handler(BaseHTTPRequestHandler):
    response_queue = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length).decode())
        # Model-capability probes may hit the same OpenAI endpoint before the
        # delegated turn. They carry no tool schema and must not consume the
        # scripted execution responses.
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
                for index, call in enumerate(message["tool_calls"]):
                    chunks.append(
                        {
                            "id": "fake",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": index,
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
def fake_provider(tmp_path, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _parent(base_url):
    parent = MagicMock()
    parent.base_url = base_url
    parent.api_key = "test-key"
    parent.provider = "custom"
    parent.api_mode = "chat_completions"
    parent.model = "fake-model"
    parent.platform = "cli"
    parent.enabled_toolsets = ["terminal", "delegation"]
    parent.valid_tool_names = {"terminal", "delegate_task"}
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
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
    parent.openrouter_min_coding_score = None
    parent.acp_command = None
    parent.acp_args = []
    parent._client_kwargs = {"base_url": base_url, "api_key": "test-key"}
    parent._memory_manager = None
    parent.session_estimated_cost_usd = 0.0
    return parent


def _config(base_url):
    lane = {
        "provider": "custom:fake",
        "model": "fake-model",
        "base_url": base_url,
        "api_key": "test-key",
        "toolsets": ["terminal"],
    }
    return {
        "max_iterations": 5,
        "lanes": {
            "code_worker": dict(lane),
            "smart_reviewer": dict(lane),
            "local_worker": dict(lane),
        },
    }


def test_sync_delegate_executes_tool_and_reports_evidence(fake_provider, monkeypatch):
    from tools import delegate_tool

    _Handler.response_queue = [_tool_response(), _text_response()]
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: _config(fake_provider))

    payload = json.loads(
        delegate_tool.delegate_task(goal="Run pwd", parent_agent=_parent(fake_provider))
    )

    result = payload["results"][0]
    assert result["status"] == "completed"
    assert result["mode"] == "execute"
    assert result["lane"] == "code_worker"
    assert result["evidence"]["successful_tool_count"] == 1
    assert result.get("failure_kind") is None


def test_background_delegate_executes_and_delivers_evidence(fake_provider, monkeypatch):
    from gateway import session_context
    from tools import async_delegation, delegate_tool
    from tools.process_registry import process_registry

    async_delegation._reset_for_tests()
    _Handler.response_queue = [_tool_response(), _text_response()]
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: _config(fake_provider))
    monkeypatch.setattr(session_context, "async_delivery_supported", lambda: True)

    dispatched = json.loads(
        delegate_tool.delegate_task(
            goal="Run pwd",
            background=True,
            parent_agent=_parent(fake_provider),
        )
    )
    assert dispatched["status"] == "dispatched"

    deadline = time.monotonic() + 10
    event = None
    while time.monotonic() < deadline:
        try:
            candidate = process_registry.completion_queue.get(timeout=0.1)
        except Exception:
            continue
        if candidate.get("delegation_id") == dispatched["delegation_id"]:
            event = candidate
            break

    assert event is not None
    assert event["status"] == "completed"
    assert event["results"][0]["evidence"]["successful_tool_count"] == 1
    async_delegation._reset_for_tests()
