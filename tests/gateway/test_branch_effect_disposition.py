"""Branch transcript metadata must survive gateway session copies."""

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin


class _AsyncSessionDB:
    def __init__(self):
        self.messages = []

    async def create_session(self, **_kwargs):
        return None

    async def append_messages_batch(self, _session_id, messages, **_kwargs):
        self.messages = messages
        return len(messages)

    async def get_session_title(self, _session_id):
        return "parent"

    async def get_next_title_in_lineage(self, title):
        return f"{title} (branch)"

    async def set_session_title(self, _session_id, _title):
        return None


class _AsyncSessionStore:
    def __init__(self, history):
        self.history = history

    async def get_or_create_session(self, _source):
        return SimpleNamespace(session_id="parent-session")

    async def load_transcript(self, _session_id):
        return self.history

    async def switch_session(self, _session_key, session_id):
        return SimpleNamespace(session_id=session_id)


class _Runner(GatewaySlashCommandsMixin):
    def __init__(self, history):
        self._session_db = _AsyncSessionDB()
        self.async_session_store = _AsyncSessionStore(history)
        self.config = {}

    def _session_key_for_source(self, _source):
        return "gateway-session-key"

    def _clear_session_boundary_security_state(self, _session_key):
        return None

    def _evict_cached_agent(self, _session_key):
        return None


@pytest.mark.asyncio
async def test_gateway_branch_preserves_tool_effect_disposition():
    history = [
        {"role": "user", "content": "Inspect the repository"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-guarded",
                    "type": "function",
                    "function": {"name": "search_files", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "content": "Search blocked by the lifecycle guard",
            "tool_name": "search_files",
            "tool_call_id": "call-guarded",
            "effect_disposition": "none",
        },
    ]
    runner = _Runner(history)
    event = MessageEvent(
        text="/branch",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1"),
    )

    await runner._handle_branch_command(event)

    copied_tool = next(
        msg for msg in runner._session_db.messages if msg["role"] == "tool"
    )
    assert copied_tool["tool_name"] == "search_files"
    assert copied_tool["tool_call_id"] == "call-guarded"
    assert copied_tool["effect_disposition"] == "none"
