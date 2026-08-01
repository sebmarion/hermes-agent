"""Deterministic, no-LLM tool-history pruning for provider dispatch."""

import json
from unittest.mock import patch

from agent.context_compressor import ContextCompressor


def _compressor(*, protect_last_n: int, tail_token_budget: int) -> ContextCompressor:
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100_000,
    ):
        compressor = ContextCompressor(
            model="test/model",
            protect_last_n=protect_last_n,
            quiet_mode=True,
        )
    compressor.tail_token_budget = tail_token_budget
    return compressor


def _call_ids(messages):
    ids = []
    for message in messages:
        if message.get("tool_call_id"):
            ids.append(message["tool_call_id"])
        for tool_call in message.get("tool_calls") or []:
            ids.append(tool_call.get("id"))
    return ids


def test_dispatch_prunes_only_old_tool_results_and_preserves_protected_duplicates():
    compressor = _compressor(protect_last_n=6, tail_token_budget=1)
    duplicate_output = "same protected output\n" + ("d" * 400)
    unique_output = "unique old output\n" + ("u" * 400)
    messages = [
        {"role": "user", "content": "original user text"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-old-unique",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "old.txt", "offset": 7}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-old-unique",
            "content": unique_output,
        },
        {
            "role": "assistant",
            "content": "old assistant text",
            "tool_calls": [{
                "id": "call-old-duplicate",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "duplicate.txt"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-old-duplicate",
            "content": duplicate_output,
        },
        {
            "role": "assistant",
            "content": "protected assistant text",
            "tool_calls": [{
                "id": "call-protected-duplicate",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "duplicate.txt"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-protected-duplicate",
            "content": duplicate_output,
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-newest-duplicate",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "duplicate.txt"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-newest-duplicate",
            "content": duplicate_output,
        },
        {"role": "user", "content": "latest user text"},
        {"role": "assistant", "content": "latest assistant text"},
    ]
    original_ids = _call_ids(messages)

    pruned, count = compressor.prune_tool_results_for_dispatch(messages)

    assert count == 2
    assert pruned is not messages
    assert pruned[2]["content"] == "[read_file] read old.txt from line 7 (418 chars)"
    assert pruned[4]["content"] == (
        "[Duplicate tool output — same content as a more recent call]"
    )
    # Both copies in the protected tail remain byte-for-byte intact, including
    # the newest identical output retained by the full-list dedupe scan.
    assert pruned[6]["content"] == duplicate_output
    assert pruned[8]["content"] == duplicate_output
    assert messages[2]["content"] == unique_output
    assert messages[4]["content"] == duplicate_output
    assert [pruned[i]["content"] for i in (0, 3, 5, 9, 10)] == [
        "original user text",
        "old assistant text",
        "protected assistant text",
        "latest user text",
        "latest assistant text",
    ]
    assert _call_ids(pruned) == original_ids


def test_dispatch_truncates_old_tool_arguments_as_json_and_counts_each_mutation():
    compressor = _compressor(protect_last_n=2, tail_token_budget=1)
    arguments = [
        json.dumps({
            "path": "notes-a.md",
            "content": "x" * 1_000,
            "nested": {"note": "y" * 800},
            "enabled": True,
        }),
        json.dumps({
            "path": "notes-b.md",
            "content": "z" * 1_000,
        }),
    ]
    messages = [
        {"role": "user", "content": "write the files"},
        {
            "role": "assistant",
            "content": "calling write_file",
            "tool_calls": [
                {
                    "id": "call-write-a",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": arguments[0],
                    },
                },
                {
                    "id": "call-write-b",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": arguments[1],
                    },
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call-write-a", "content": "ok a"},
        {"role": "tool", "tool_call_id": "call-write-b", "content": "ok b"},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "done"},
    ]

    pruned, count = compressor.prune_tool_results_for_dispatch(messages)

    assert count == 2
    parsed = [
        json.loads(tool_call["function"]["arguments"])
        for tool_call in pruned[1]["tool_calls"]
    ]
    assert parsed[0]["path"] == "notes-a.md"
    assert parsed[0]["content"].endswith("...[truncated]")
    assert parsed[0]["nested"]["note"].endswith("...[truncated]")
    assert parsed[0]["enabled"] is True
    assert parsed[1]["path"] == "notes-b.md"
    assert parsed[1]["content"].endswith("...[truncated]")
    assert pruned[1]["content"] == "calling write_file"
    assert [tool_call["id"] for tool_call in pruned[1]["tool_calls"]] == [
        "call-write-a",
        "call-write-b",
    ]
    assert [messages[i]["tool_call_id"] for i in (2, 3)] == [
        "call-write-a",
        "call-write-b",
    ]
    assert [
        tool_call["function"]["arguments"]
        for tool_call in messages[1]["tool_calls"]
    ] == arguments


def test_dispatch_preserves_multimodal_pruning_and_is_idempotent():
    compressor = _compressor(protect_last_n=4, tail_token_budget=1)
    old_image = [
        {"type": "text", "text": "old screenshot text"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,OLD"}},
    ]
    protected_image = [
        {"type": "text", "text": "recent screenshot text"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,NEW"}},
    ]
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-old-image",
                "type": "function",
                "function": {"name": "browser_snapshot", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call-old-image", "content": old_image},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-new-image",
                "type": "function",
                "function": {"name": "browser_snapshot", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-new-image",
            "content": protected_image,
        },
        {"role": "user", "content": "what changed?"},
        {"role": "assistant", "content": "checking"},
    ]

    first, first_count = compressor.prune_tool_results_for_dispatch(messages)

    assert first_count == 1
    assert first[1]["content"] == [
        {"type": "text", "text": "old screenshot text"},
        {"type": "text", "text": "[screenshot removed to save context]"},
    ]
    assert first[3]["content"] == protected_image
    assert messages[1]["content"] == old_image

    second, second_count = compressor.prune_tool_results_for_dispatch(first)
    assert second_count == 0
    assert second is first


def test_dispatch_noop_returns_input_list_and_keeps_200_char_floor():
    compressor = _compressor(protect_last_n=2, tail_token_budget=1)
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-floor",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call-floor", "content": "x" * 200},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "reply"},
    ]

    unchanged, count = compressor.prune_tool_results_for_dispatch(messages)

    assert count == 0
    assert unchanged is messages


def test_dispatch_does_not_dedupe_outputs_at_exactly_200_chars():
    compressor = _compressor(protect_last_n=2, tail_token_budget=1)
    exact_floor = "x" * 200
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-floor-old",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-floor-old",
            "content": exact_floor,
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-floor-new",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-floor-new",
            "content": exact_floor,
        },
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "reply"},
    ]

    unchanged, count = compressor.prune_tool_results_for_dispatch(messages)

    assert count == 0
    assert unchanged is messages


def test_dispatch_keeps_newest_historical_duplicate_full_outside_protected_tail():
    compressor = _compressor(protect_last_n=2, tail_token_budget=1)
    duplicate_output = "historical duplicate\n" + ("d" * 400)
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-duplicate-old",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-duplicate-old",
            "content": duplicate_output,
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-duplicate-new",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-duplicate-new",
            "content": duplicate_output,
        },
        {"role": "user", "content": "older follow-up"},
        {"role": "assistant", "content": "older reply"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "reply"},
    ]

    pruned, count = compressor.prune_tool_results_for_dispatch(messages)

    assert count == 1
    assert pruned[1]["content"] == (
        "[Duplicate tool output — same content as a more recent call]"
    )
    assert pruned[3]["content"] == duplicate_output
    assert messages[1]["content"] == duplicate_output
    assert messages[3]["content"] == duplicate_output


def test_dispatch_caps_long_ordinary_receipt_for_second_pass_idempotence():
    compressor = _compressor(protect_last_n=2, tail_token_budget=1)
    long_path = "/tmp/" + ("nested/" * 35) + "notes.txt"
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-long-receipt",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": long_path}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-long-receipt",
            "content": "original output\n" + ("o" * 500),
        },
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "reply"},
    ]

    first, first_count = compressor.prune_tool_results_for_dispatch(messages)
    assert first_count == 1
    assert first[1]["content"].startswith("[read_file] read ")
    assert len(first[1]["content"]) <= 200

    second, second_count = compressor.prune_tool_results_for_dispatch(first)

    assert second_count == 0
    assert second is first


def test_dispatch_prunes_raw_output_that_starts_with_tool_name_prefix():
    compressor = _compressor(protect_last_n=2, tail_token_budget=1)
    raw_output = "[read_file] legitimate raw output\n" + ("r" * 500)
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-prefixed-raw",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "raw.txt"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-prefixed-raw",
            "content": raw_output,
        },
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "reply"},
    ]

    pruned, count = compressor.prune_tool_results_for_dispatch(messages)

    assert count == 1
    assert pruned[1]["content"] == "[read_file] read raw.txt from line 1 (534 chars)"
    assert messages[1]["content"] == raw_output
