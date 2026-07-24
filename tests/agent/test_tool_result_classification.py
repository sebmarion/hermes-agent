"""Tests for shared tool result classification helpers."""

import json

from agent.tool_result_classification import (
    file_mutation_result_landed,
    file_mutation_result_proves_change,
)


def test_write_file_with_nested_lint_error_counts_as_landed():
    result = json.dumps({
        "bytes_written": 12,
        "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
    })

    assert file_mutation_result_landed("write_file", result) is True


def test_patch_with_nested_lsp_diagnostics_counts_as_landed():
    result = json.dumps({
        "success": True,
        "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
        "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
    })

    assert file_mutation_result_landed("patch", result) is True


def test_top_level_file_mutation_error_does_not_count_as_landed():
    result = json.dumps({"success": True, "error": "post-write verification failed"})

    assert file_mutation_result_landed("patch", result) is False


def test_non_empty_patch_diff_proves_material_change():
    result = json.dumps({
        "success": True,
        "diff": "--- a/tmp.py\n+++ b/tmp.py\n@@\n-old\n+new\n",
    })

    assert file_mutation_result_proves_change("patch", result) is True


def test_empty_or_whitespace_patch_diff_does_not_prove_material_change():
    empty = json.dumps({"success": True, "diff": ""})
    whitespace = json.dumps({"success": True, "diff": " \n\t"})

    assert file_mutation_result_proves_change("patch", empty) is False
    assert file_mutation_result_proves_change("patch", whitespace) is False


def test_patch_error_or_malformed_result_does_not_prove_material_change():
    errored = json.dumps({
        "success": True,
        "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
        "error": "post-write verification failed",
    })

    assert file_mutation_result_proves_change("patch", errored) is False
    assert file_mutation_result_proves_change("patch", "{not-json") is False
    assert file_mutation_result_proves_change("patch", None) is False


def test_current_write_file_metadata_does_not_prove_material_change():
    result = json.dumps({
        "bytes_written": 12,
        "files_modified": ["/tmp/example.py"],
    })

    assert file_mutation_result_proves_change("write_file", result) is False


def test_side_effect_classification_keeps_session_mutations():
    from agent.tool_result_classification import tool_may_have_side_effect

    assert tool_may_have_side_effect("todo") is True
    assert tool_may_have_side_effect("memory") is True
    assert tool_may_have_side_effect("write_file") is True
    assert tool_may_have_side_effect("mcp_unknown") is True
    assert tool_may_have_side_effect("read_file") is False
    assert tool_may_have_side_effect("web_search") is False
