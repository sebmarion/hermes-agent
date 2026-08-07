"""Post-tool progress replies continue without polluting durable history."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from run_agent import AIAgent


def _detector_agent():
    agent = AIAgent.__new__(AIAgent)
    agent._strip_think_blocks = lambda content: content
    return agent


def _tool_tail():
    return [
        {"role": "user", "content": "inspect the project"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"ok": true}',
        },
    ]


def test_detects_short_future_action_after_tool_result():
    agent = _detector_agent()

    assert agent._looks_like_post_tool_progress_update(
        "Let me check what scripts actually exist.",
        _tool_tail(),
    )


def test_detects_genuine_single_sentence_progress_update():
    agent = _detector_agent()

    assert agent._looks_like_post_tool_progress_update(
        "Let me inspect the logs now.",
        _tool_tail(),
    )


def test_detects_simple_progress_with_demonstrative_target():
    agent = _detector_agent()

    assert agent._looks_like_post_tool_progress_update(
        "I'll inspect that log now.",
        _tool_tail(),
    )


@pytest.mark.parametrize(
    "content",
    [
        "  Now I'll inspect the logs now.  ",
        "Next I’ll inspect the logs now.",
    ],
)
def test_detects_leading_whitespace_and_now_or_next_future_action_form(content):
    agent = _detector_agent()

    assert agent._looks_like_post_tool_progress_update(
        content,
        _tool_tail(),
    )


@pytest.mark.parametrize(
    ("content", "messages"),
    [
        (
            "Let me inspect the repository files first.",
            [{"role": "user", "content": "inspect the project"}],
        ),
        (
            "I will inspect the repository. " + ("Detailed context. " * 50),
            _tool_tail(),
        ),
        (
            "I checked the scripts and found that the deployment is healthy.",
            _tool_tail(),
        ),
        (
            "The check is complete and all systems are healthy. "
            "Let me know if you need anything else.",
            _tool_tail(),
        ),
        (
            "I will check the report now: deployment is healthy and all tests pass.",
            _tool_tail(),
        ),
        ("Let me inspect the report. I found two errors.", _tool_tail()),
        ("Found two failures. I’ll inspect the remaining logs.", _tool_tail()),
        ("The deployment is down. I will inspect the logs now.", _tool_tail()),
        ("I will inspect the logs now. The deployment is down.", _tool_tail()),
        ("Let me inspect the logs. I will review the config.", _tool_tail()),
        ("I will inspect the logs now; deployment is down.", _tool_tail()),
        ("I will inspect the logs now, deployment is down.", _tool_tail()),
        ("I will inspect the logs now — deployment is down.", _tool_tail()),
        ("I will inspect the logs now – deployment is down.", _tool_tail()),
        ("I’ll inspect the logs because the service is offline.", _tool_tail()),
        ("I’ll inspect the logs and the deployment is down.", _tool_tail()),
        ("I’ll inspect the logs but the service is offline.", _tool_tail()),
        ("I’ll inspect the logs although the service is offline.", _tool_tail()),
        ("I’ll inspect the logs while the service is restarting.", _tool_tail()),
        ("I’ll inspect the logs since the service is offline.", _tool_tail()),
        ("I’ll inspect the logs whereas the service is offline.", _tool_tail()),
        ("I’ll inspect the logs so the next step is clear.", _tool_tail()),
        ("I’ll inspect the logs yet the service is offline.", _tool_tail()),
        ("I’ll inspect the logs however the service is offline.", _tool_tail()),
        ("I’ll inspect the logs therefore the next step is clear.", _tool_tail()),
        ("I’ll inspect the logs which contain recent entries.", _tool_tail()),
        ("I’ll inspect the logs that contain recent entries.", _tool_tail()),
        ("I’ll inspect the directory where recent entries appear.", _tool_tail()),
        ("I will review it now; the output shows success.", _tool_tail()),
        ("Let me check it now. The result indicates a failure.", _tool_tail()),
        ("I will inspect it now. Analysis complete.", _tool_tail()),
        ("Let me verify it now. Verification completed.", _tool_tail()),
        ("I will run the check now. Result: healthy.", _tool_tail()),
        ("Let me test it now. Tests fail with an explicit error.", _tool_tail()),
        (
            "Let me check the report now:\n- deployment healthy\n- tests pass",
            _tool_tail(),
        ),
        (
            "I will inspect the output now.\n```text\nresult: success\n```",
            _tool_tail(),
        ),
        ("I will continue shortly.", _tool_tail()),
    ],
    ids=[
        "before-tools",
        "long",
        "substantive",
        "final",
        "colon-results",
        "found",
        "result-before-future-action",
        "future-action-after-prior-sentence",
        "result-after-future-action",
        "multiple-progress-sentences",
        "semicolon-clause",
        "comma-clause",
        "em-dash-clause",
        "en-dash-clause",
        "because-clause",
        "and-clause",
        "but-clause",
        "although-clause",
        "while-clause",
        "since-clause",
        "whereas-clause",
        "so-clause",
        "yet-clause",
        "however-clause",
        "therefore-clause",
        "which-relative-clause",
        "that-relative-clause",
        "where-relative-clause",
        "shows-success",
        "indicates-failure",
        "complete",
        "completed",
        "explicit-result",
        "tests-fail-error",
        "bulleted-results",
        "code-block-result",
        "no-action-marker",
    ],
)
def test_detector_rejects_non_progress_replies(content, messages):
    agent = _detector_agent()

    assert not agent._looks_like_post_tool_progress_update(content, messages)


def _response(content="done", *, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls,
                    reasoning_content=None,
                ),
                finish_reason="tool_calls" if tool_calls else "stop",
            )
        ],
        usage=None,
        model="test/model",
    )


def _tool_response():
    return _response(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call_1",
                type="function",
                function=SimpleNamespace(name="terminal", arguments="{}"),
            )
        ],
    )


@pytest.fixture
def loop_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            session_id=f"post-tool-progress-{tmp_path.name}",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            api_mode="chat_completions",
            model="test/model",
            max_iterations=8,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.valid_tool_names = ["terminal"]
    agent._cached_system_prompt = "stable test prompt"
    agent._session_db = None
    agent._session_json_enabled = False
    agent.save_trajectories = False
    agent.compression_enabled = False
    agent._cleanup_task_resources = lambda *_a, **_kw: None
    agent._save_trajectory = lambda *_a, **_kw: None

    def _execute_tool_calls(assistant_message, messages, *_args, **_kwargs):
        for call in assistant_message.tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": '{"ok": true}',
                }
            )

    agent._execute_tool_calls = _execute_tool_calls
    return agent


def _run(agent, responses, requests, prompt="inspect the project"):
    remaining = list(responses)

    def _api_call(api_kwargs):
        requests.append(api_kwargs)
        return remaining.pop(0)

    agent._interruptible_api_call = _api_call
    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        return agent.run_conversation(prompt)


def _progress_pair(label):
    return [
        {
            "role": "assistant",
            "content": f"I will inspect {label}.",
            "_post_tool_progress_synthetic": True,
        },
        {
            "role": "user",
            "content": f"continue after {label}",
            "_post_tool_progress_synthetic": True,
        },
    ]


def test_compression_archive_input_excludes_progress_scaffolding_and_noop_preserves_live_identity(
    loop_agent,
):
    messages = _tool_tail() + _progress_pair("the first result")
    original_snapshot = [dict(message) for message in messages]
    archive_inputs = []

    def _fake_archive_and_commit(_agent, compression_input, system_message, **_kwargs):
        archive_inputs.append([dict(message) for message in compression_input])
        return compression_input, system_message

    with (
        patch(
            "agent.conversation_compression.resolve_context_compression_timeouts",
            return_value=(0, 0),
        ),
        patch(
            "agent.conversation_compression.compress_context",
            side_effect=_fake_archive_and_commit,
        ),
    ):
        returned, prompt = loop_agent._compress_context(messages, "system prompt")

    assert returned is messages
    assert returned == original_snapshot
    assert prompt == "system prompt"
    assert archive_inputs
    assert all(
        not message.get("_post_tool_progress_synthetic")
        for message in archive_inputs[0]
    )


def test_successful_compression_reattaches_only_latest_trailing_progress_pair(
    loop_agent,
):
    old_pair = _progress_pair("the first result")
    latest_pair = _progress_pair("the remaining result")
    messages = _tool_tail() + old_pair + latest_pair
    commit_inputs = []

    def _fake_archive_and_commit(_agent, compression_input, _system_message, **_kwargs):
        commit_inputs.append([dict(message) for message in compression_input])
        return ([{"role": "user", "content": "compressed summary"}], "compressed prompt")

    with (
        patch(
            "agent.conversation_compression.resolve_context_compression_timeouts",
            return_value=(0, 0),
        ),
        patch(
            "agent.conversation_compression.compress_context",
            side_effect=_fake_archive_and_commit,
        ),
    ):
        returned, prompt = loop_agent._compress_context(messages, "system prompt")

    assert prompt == "compressed prompt"
    assert commit_inputs
    assert all(
        not message.get("_post_tool_progress_synthetic")
        for message in commit_inputs[0]
    )
    assert returned == [
        {"role": "user", "content": "compressed summary"},
        *latest_pair,
    ]
    assert all(message not in returned for message in old_pair)


def test_chat_completions_continues_after_post_tool_progress(loop_agent):
    requests = []
    result = _run(
        loop_agent,
        [
            _tool_response(),
            _response("Let me check what scripts actually exist."),
            _response("All systems healthy."),
        ],
        requests,
    )

    assert result["completed"] is True
    assert result["final_response"] == "All systems healthy."
    assert len(requests) == 3

    # The continuation pair is visible to the next model call, preserving
    # assistant/user alternation, but private marker fields never reach the
    # strict chat-completions wire payload.
    retry_messages = requests[2]["messages"]
    assert any(
        msg.get("role") == "assistant"
        and msg.get("content") == "Let me check what scripts actually exist."
        for msg in retry_messages
    )
    assert any(
        msg.get("role") == "user"
        and "Continue from the tool results" in (msg.get("content") or "")
        for msg in retry_messages
    )
    assert all("_post_tool_progress_synthetic" not in msg for msg in retry_messages)
    assert all(
        not msg.get("_post_tool_progress_synthetic")
        for msg in result["messages"]
    )


def test_post_tool_progress_retries_are_capped_at_two(loop_agent):
    requests = []
    progress = "I will inspect the next tool result now."
    result = _run(
        loop_agent,
        [
            _tool_response(),
            _response(progress),
            _response(progress),
            _response(progress),
        ],
        requests,
    )

    assert len(requests) == 4
    assert result["api_calls"] == 4
    assert loop_agent.iteration_budget.used == 4
    assert result["final_response"] == progress
    assert result["completed"] is True


def test_budget_exhaustion_after_progress_nudges_cleans_synthetic_tail(loop_agent):
    loop_agent.max_iterations = 3
    loop_agent.iteration_budget.max_total = 3
    requests = []
    progress = "I will inspect the next tool result now."

    result = _run(
        loop_agent,
        [_tool_response(), _response(progress), _response(progress)],
        requests,
    )

    assert len(requests) == 3
    assert result["api_calls"] == 3
    assert loop_agent.iteration_budget.used == 3
    assert result["completed"] is False
    assert all(
        not msg.get("_post_tool_progress_synthetic")
        for msg in result["messages"]
    )
    assert all(msg.get("content") != progress for msg in result["messages"])


def test_intent_ack_and_post_tool_progress_budgets_are_independent(loop_agent):
    loop_agent._intent_ack_continuation = True
    requests = []
    result = _run(
        loop_agent,
        [
            _response("Let me inspect the repository files first."),
            _tool_response(),
            _response("I will check the tool result now."),
            _response("Inspection complete."),
        ],
        requests,
    )

    assert len(requests) == 4
    assert result["final_response"] == "Inspection complete."


def test_verification_budget_is_independent_of_post_tool_progress(loop_agent, monkeypatch):
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    requests = []
    responses = [
        _tool_response(),
        _response("Let me check the first result."),
        _response("I will inspect the remaining result."),
        _response("Candidate report."),
        _response("Verified report."),
    ]

    remaining = list(responses)

    def _api_call(api_kwargs):
        requests.append(api_kwargs)
        if len(requests) == 4:
            loop_agent._turn_file_mutation_paths = {"changed.py"}
        return remaining.pop(0)

    loop_agent._interruptible_api_call = _api_call
    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=["verify it", None],
        ),
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = loop_agent.run_conversation("inspect and edit the project")

    assert len(requests) == 5
    assert result["final_response"] == "Verified report."
