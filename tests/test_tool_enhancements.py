import asyncio
import json
from unittest.mock import AsyncMock

from agent.safety import classify_tool, evaluate_mutations, MutationCounts
from agent.tool_routing import select_tools
from core.tool_catalog import TOOL_POLICIES
from tools import get_tool_func, mcp
from tools import modeling_details, container_inquiry


def catalog():
    return [{"type": "function", "function": {"name": name}} for name in TOOL_POLICIES]


def names(messages):
    return {t["function"]["name"] for t in select_tools(catalog(), messages)}


def test_read_task_excludes_writes_and_keeps_cross_domain_reads():
    selected = names([{"role": "user", "content": "查询产品工艺路线和工序"}])
    assert {"get_product_details", "list_workflow_steps", "get_spec_details", "get_operation_details"} <= selected
    assert all(TOOL_POLICIES[n].effect == "read" for n in selected)
    assert not any(n.endswith("_by_odata_key") for n in selected)


def test_discovery_can_expand_without_keyword_and_resets_next_turn():
    messages = [{"role": "user", "content": "继续"}, {"role": "tool", "name": "load_tool_group",
                "content": json.dumps({"loaded": True, "group": "container", "include_writes": True})}]
    assert "container_move" in names(messages)
    assert "container_move" not in names(messages + [{"role": "user", "content": "你好"}])


def test_registration_and_execution_use_explicit_catalog():
    registered = asyncio.run(mcp.list_tools())
    assert {t.name for t in registered} == set(TOOL_POLICIES)
    assert all(get_tool_func(t.name) is not None for t in registered)
    assert get_tool_func("request") is None
    assert get_tool_func("_read_steps") is None
    assert classify_tool("unknown_future_tool") == "unknown"


def test_missed_writes_and_bulk_confirm():
    for name in ["add_spec_step_to_workflow", "connect_workflow_steps", "import_workflows_from_excel"]:
        assert classify_tool(name) == "update"
    decision = evaluate_mutations(MutationCounts(), [{"function": {"name": "import_workflows_from_excel", "arguments": "{}"}}])
    assert decision.requires_approval


def test_details_preserve_refs_and_report_pagination(monkeypatch):
    mock = AsyncMock(return_value=json.dumps({"Name": "S", "Operation": {"Name": "O"}, "TxnMap": [{"Value": i} for i in range(5)]}))
    monkeypatch.setattr(modeling_details, "request", mock)
    result = json.loads(asyncio.run(modeling_details.get_spec_details("id", "transactions", 2, 2)))
    assert result["data"]["Operation"] == {"Name": "O"}
    assert result["data"]["TxnMap"] == [{"Value": 2}, {"Value": 3}]
    assert result["complete"] is False
    assert result["pages"]["TxnMap"]["next_offset"] == 4
    assert mock.call_args.kwargs["params"] == {"$expand": "TxnMap"}


def test_query_status_uses_verified_navigation_and_instance_id(monkeypatch):
    mock = AsyncMock(return_value='{"ContainerStatusDetails":{"StepName":"A"}}')
    monkeypatch.setattr(container_inquiry, "request", mock)
    result = json.loads(asyncio.run(container_inquiry.get_container_details("SN", "id")))
    assert result["data"]["ContainerStatusDetails"]["StepName"] == "A"
    assert mock.call_args.kwargs["body"] == {"container": {"name": "SN", "instanceId": "id"}}
    assert mock.call_args.kwargs["params"]["$expand"] == "ContainerStatusDetails"


def test_history_is_always_a_page_and_errors_survive(monkeypatch):
    mock = AsyncMock(return_value='{"Histories":[]}')
    monkeypatch.setattr(container_inquiry, "request", mock)
    result = json.loads(asyncio.run(container_inquiry.get_container_history("SN", start_row=31)))
    assert not result["complete"]
    assert mock.call_args.kwargs["body"]["resultsetStartRow"] == 31
    mock.return_value = "Error: server unavailable"
    assert asyncio.run(container_inquiry.get_container_history("SN")) == "Error: server unavailable"


def test_invalid_arguments_never_issue_requests(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(modeling_details, "request", mock)
    assert asyncio.run(modeling_details.get_product_details("id", limit=0)).startswith("Error")
    mock.assert_not_called()


def test_langgraph_sends_selected_tools_and_records_metrics(monkeypatch):
    from types import SimpleNamespace
    from agent import langgraph_runtime as runtime
    from unittest.mock import Mock
    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="查询结果", tool_calls=None))])
    create = AsyncMock(return_value=chunks())
    log = Mock()
    monkeypatch.setattr(runtime, "_client", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(runtime, "_openai_tools", catalog())
    monkeypatch.setattr(runtime, "get_stream_writer", lambda: lambda _: None)
    monkeypatch.setattr(runtime, "record_perf", log)
    monkeypatch.setattr(runtime, "build_runtime_experience_context", lambda *a: "")
    asyncio.run(runtime._agent_node({"messages": [{"role": "user", "content": "查询container状态"}]}))
    selected = create.call_args.kwargs["tools"]
    assert len(selected) < 10
    assert "get_container_details" in {t["function"]["name"] for t in selected}
    assert log.call_args.args[4]["selected_tool_count"] == len(selected)


def test_missing_navigation_is_not_complete():
    result = json.loads(modeling_details.read_envelope('{"Name":"S"}', "test", page_fields=["TxnMap"]))
    assert not result["complete"]
    assert result["missing_fields"] == ["TxnMap"]


def test_topic_switch_drops_old_domains_and_write_intent():
    selected = names([{"role": "user", "content": "创建产品"},
                      {"role": "user", "content": "查询container状态"}])
    assert "get_container_details" in selected
    assert "get_product_details" not in selected
    assert all(TOOL_POLICIES[n].effect == "read" for n in selected)


def test_elliptical_followup_keeps_nearest_topic_but_not_old_writes():
    messages = [{"role": "user", "content": "创建产品"},
                {"role": "user", "content": "查询container SN001"},
                {"role": "user", "content": "它的历史和状态呢"}]
    selected = names(messages)
    assert {"get_container_history", "get_container_details"} <= selected
    assert "get_product_details" not in selected
    assert all(TOOL_POLICIES[n].effect == "read" for n in selected)


def test_local_empty_page_reports_range_not_absence():
    result = json.loads(modeling_details.read_envelope(
        '{"Attributes":[{"Name":"A"}]}', "test", offset=5, page_fields=["Attributes"]))
    assert result["data"]["Attributes"] == []
    assert result["pages"]["Attributes"]["available_count"] == 1
    assert result["pages"]["Attributes"]["pagination"] == "local"
    assert not result["complete"]
    assert any("offset" in warning for warning in result["warnings"])


def test_next_link_only_marks_incomplete_when_nonempty():
    for link, expected in [(None, True), ("", True), ("next-page", False)]:
        result = json.loads(modeling_details.read_envelope(
            json.dumps({"Attributes": [], "Attributes@odata.nextLink": link}),
            "test", page_fields=["Attributes"]))
        assert result["complete"] is expected
        assert result["pages"]["Attributes"]["server_total_known"] is expected
