import asyncio
import json
from unittest.mock import AsyncMock

from tools import get_tool_func
from tools import workflow_steps as module


def responses():
    # Step names differ from Spec names, and temporary IDs differ by read.
    workflow = {"Name": "WF", "Revision": "A", "Steps": [
        {"Name": "020", "InstanceID": "saved2", "Spec": {"Name": "SPEC-B", "Revision": "2"}},
        {"Name": "010", "InstanceID": "saved1", "IsFirstStep": True,
         "Spec": {"Name": "SPEC-A", "Revision": "1"}, "DefaultPath": {"Name": "forward"}},
        {"Name": "branch", "Spec": None},
    ]}
    expanded = {"Name": "WF", "Revision": "A", "Steps": [
        {"Name": "010", "InstanceID": "temporary", "Paths": [
            {"Name": "forward", "IsDefaultPath": True, "ToStep": {"Name": "020"}},
            {"Name": "alternate", "IsDefaultPath": False, "ToStep": {"Name": "branch"}},
        ]},
        {"Name": "020", "Paths": []}, {"Name": "branch", "Paths": []},
    ]}
    return workflow, expanded


def read(monkeypatch, first, second):
    mock = AsyncMock(side_effect=[json.dumps(first), json.dumps(second)])
    monkeypatch.setattr(module, "request", mock)
    result = asyncio.run(module._read_steps("workflow-id"))
    return result, mock


def test_reads_actual_spec_and_orders_default_route_preserving_branches(monkeypatch):
    result, mock = read(monkeypatch, *responses())
    assert result["standard_route"] == ["010", "020"]
    assert result["standard_route_complete"] is True
    assert result["returned_step_count"] == 3
    assert result["steps"][0]["spec"]["Name"] == "SPEC-B"
    assert result["steps"][2]["spec"] is None
    assert [c.kwargs["params"] for c in mock.call_args_list] == [
        {"$expand": "Steps"}, {"$expand": "Steps($expand=Paths)"}]
    assert all(c.args[0] == "GET" for c in mock.call_args_list)


def test_formatter_keeps_large_step_lists():
    data = {"Steps": [{"Name": str(i), "Spec": {"Name": "long" * 50}} for i in range(300)]}
    assert json.loads(module._json(data)) == data


def test_cycle_is_reported(monkeypatch):
    first, second = responses()
    second["Steps"][1]["Paths"] = [{"Name": "back", "IsDefaultPath": True, "ToStep": {"Name": "010"}}]
    result, _ = read(monkeypatch, first, second)
    assert not result["standard_route_complete"]
    assert any("循环" in w for w in result["warnings"])


def test_changed_revision_does_not_merge_reads(monkeypatch):
    first, second = responses(); second["Revision"] = "B"
    result, _ = read(monkeypatch, first, second)
    assert result["steps"][0]["paths"] is None
    assert not result["standard_route_complete"]


def test_multiple_defaults_does_not_guess(monkeypatch):
    first, second = responses(); second["Steps"][0]["Paths"][1]["IsDefaultPath"] = True
    result, _ = read(monkeypatch, first, second)
    assert result["standard_route"] == ["010"]
    assert not result["standard_route_complete"]


def test_pagination_is_not_claimed_complete(monkeypatch):
    first, second = responses(); first["Steps@odata.nextLink"] = "next"
    result, _ = read(monkeypatch, first, second)
    assert not result["steps_complete"]
    assert not result["standard_route_complete"]


def test_empty_expanded_field_is_not_treated_as_steps(monkeypatch):
    monkeypatch.setattr(module, "request", AsyncMock(return_value='{"Steps-Expanded": []}'))
    result = asyncio.run(module._read_steps("id"))
    assert "error" in result


def test_http_failure_preserves_error_and_does_not_retry(monkeypatch):
    mock = AsyncMock(return_value="HTTP 400 invalid request")
    monkeypatch.setattr(module, "request", mock)
    assert asyncio.run(module._read_steps("id"))["error"] == "HTTP 400 invalid request"
    assert mock.await_count == 1


def test_tools_are_available_to_agent():
    assert get_tool_func("list_workflow_steps") is module.list_workflow_steps
    assert get_tool_func("get_workflow_step") is module.get_workflow_step


def test_single_step_uses_exact_step_name(monkeypatch):
    first, second = responses()
    monkeypatch.setattr(module, "request", AsyncMock(side_effect=[json.dumps(first), json.dumps(second)]))
    result = json.loads(asyncio.run(module.get_workflow_step("id", "020")))
    assert result["step"]["spec"]["Name"] == "SPEC-B"
