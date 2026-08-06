"""Regression tests for bounded delegated-child handoffs."""

import os

import tools.delegate_tool as dt


class _Compressor:
    context_length = 65_536
    max_tokens = 4_096


class _Parent:
    context_compressor = _Compressor()
    max_tokens = 4_096


def test_handoff_budget_reserves_output_and_prompt_overhead():
    budget = dt._child_handoff_char_budget(
        context_length=32_768,
        max_tokens=4_096,
    )

    # 32K total minus 4K output and 4K system/tool safety reserve.
    assert budget == 24_576 * 4


def test_small_handoff_is_unchanged():
    context = "short plan"
    result = dt._bound_child_handoff(
        goal="Execute the plan",
        context=context,
        context_length=32_768,
        max_tokens=4_096,
        task_index=0,
    )

    assert result == context


def test_large_handoff_spills_full_context_and_keeps_pointer(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    context = "PLAN_HEAD\n" + ("x" * 110_000) + "\nPLAN_TAIL"

    result = dt._bound_child_handoff(
        goal="Execute the plan",
        context=context,
        context_length=32_768,
        max_tokens=4_096,
        task_index=7,
    )

    assert "PLAN_HEAD" in result
    assert "PLAN_TAIL" in result
    assert "read_file" in result
    assert "Full delegated context saved to:" in result
    path = result.split("Full delegated context saved to:", 1)[1].splitlines()[0].strip()
    assert os.path.exists(path)
    assert open(path, encoding="utf-8").read() == context


def test_child_context_length_prefers_explicit_task_override():
    creds = {"context_length": 32_768, "model": "ornith", "base_url": "http://local"}
    assert dt._resolve_child_context_length(
        {}, creds, _Parent(), task={"context_length": 65_536}
    ) == 65_536
