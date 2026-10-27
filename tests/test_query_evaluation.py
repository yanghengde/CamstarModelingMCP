"""Cross-domain routing and honest evaluation failure/completeness semantics."""
import asyncio
import json
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from agent.tool_routing import select_tools
from core.tool_catalog import TOOL_POLICIES
from scripts.evaluate_queries import DIALOGUES, measure_read, messages_for, probe_model, routing_check, summarize


TOOLS = [{"type": "function", "function": {"name": name}}
         for name in TOOL_POLICIES]


def test_replay_separates_completed_turns_without_inventing_business_data():
    messages = messages_for(["查询容器", "再查历史", "继续"])
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]
    assert all(m["content"] == "本轮对话结束。" for m in messages if m["role"] == "assistant")


@pytest.mark.parametrize("case", DIALOGUES, ids=[c[0] for c in DIALOGUES])
def test_multi_turn_read_routing(case):
    result = routing_check(TOOLS, case)
    assert result["passed"], result


def test_explicit_sql_followup_does_not_inherit_container():
    selected = select_tools(TOOLS, [{"role": "user", "content": "查询容器 BOX-0001"},
                                   {"role": "user", "content": "SQL query"},
                                   {"role": "user", "content": "继续"}])
    groups = {TOOL_POLICIES[t["function"]["name"]].group for t in selected}
    assert "query" in groups
    assert "container" not in groups


def test_summary_counts_failed_latencies_too():
    summary = summarize([{"success": True, "seconds": 1}, {"success": False, "seconds": 50}])
    assert summary["success_count"] == 1
    assert summary["median_seconds"] == 25.5
    assert summary["p95_seconds"] == 50
    assert summarize([])["p95_seconds"] is None


def test_read_failure_is_not_empty_success():
    sample = asyncio.run(measure_read("trace", AsyncMock(return_value=json.dumps({"sections": {}}))))
    assert not sample["success"]
    sample = asyncio.run(measure_read("container", AsyncMock(side_effect=TimeoutError())))
    assert not sample["success"]
    assert sample["error_type"] == "TimeoutError"


def test_partial_order_page_is_scoped_success_not_full_completion():
    result = {"complete": False, "page": {"has_more": True}, "containers": [{"recent_history": {}}]}
    sample = asyncio.run(measure_read("order", AsyncMock(return_value=json.dumps(result))))
    assert sample["success"]
    assert sample["scope"] == {"complete": False, "page": {"has_more": True}}


@pytest.mark.parametrize("arguments,finish,expected", [
    ('{"container_name":"BOX-0001"}', "tool_calls", True),
    ('{"container_name":', "length", False),
    ('{}', "length", False),
    ('[]', "tool_calls", False),
])
def test_model_probe_rejects_truncation_and_invalid_arguments(monkeypatch, arguments, finish, expected):
    from agent.llm_client import oai_client
    call = SimpleNamespace(function=SimpleNamespace(name="get_container_history", arguments=arguments))
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(tool_calls=[call]), finish_reason=finish)], usage=None)
    monkeypatch.setattr(oai_client.chat.completions, "create", AsyncMock(return_value=response))
    sample = asyncio.run(probe_model([], TOOLS, "get_container_history"))
    assert sample["success"] is expected
    assert sample["usage"] is None
