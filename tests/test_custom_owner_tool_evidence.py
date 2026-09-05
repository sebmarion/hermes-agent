"""Our owner archive evidence spans pages and ignores rewound results."""
from types import SimpleNamespace
from tui_gateway.owner_inbox import _active_message_pages, _pending_tool_call_ids

def test_call_and_result_on_different_pages_match():
    first = [{"id":n,"role":"system"} for n in range(1,500)]
    first.append({"id":500,"role":"assistant","tool_calls":[{"id":"tool-1"}]})
    calls = []
    def get_messages(sid, *, limit, after_id):
        calls.append((sid, limit, after_id))
        return first if after_id is None else [{"id":501,"role":"tool","tool_call_id":"tool-1"}]
    assert not _pending_tool_call_ids(_active_message_pages(SimpleNamespace(get_messages=get_messages), "child"))
    assert calls == [("child",500,None),("child",500,500)]

def test_rewound_result_does_not_complete_active_call():
    rows = [{"role":"assistant","tool_calls":[{"id":"tool-1"}],"active":1},
            {"role":"tool","tool_call_id":"tool-1","active":0}]
    assert _pending_tool_call_ids(rows) == {"tool-1"}

def test_rewound_call_does_not_block_archive():
    assert not _pending_tool_call_ids([{"role":"assistant","tool_calls":[{"id":"old"}],"active":0}])


def test_old_result_does_not_settle_later_reused_call():
    rows = [{"role":"tool","tool_call_id":"same"},
            {"role":"assistant","tool_calls":[{"id":"same"}]}]
    assert _pending_tool_call_ids(rows) == {"same"}


def test_malformed_call_evidence_is_not_treated_as_idle():
    import pytest
    for value in ([None], [{"function": {"name": "tool"}}], 7):
        with pytest.raises(ValueError):
            _pending_tool_call_ids([{"role":"assistant","tool_calls":value}])
