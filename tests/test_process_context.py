import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from tools import process_context as module, workflow_steps, container_inquiry


def node(identifier="wf", step_type=1, child=None):
    return {"workflow": {"name": identifier, "revision": "A", "instance_id": identifier},
            "steps_complete": True, "warnings": [], "steps": [
                {"name": "S", "step_type": step_type, "paths": [], "subworkflow": child,
                 "spec": {"Name": "SPEC", "Revision": "A", "InstanceId": "spec"},
                 "operation": {"Name": "OP", "InstanceId": "op"}}]}


def advanced(identifier="wf"):
    return {"Name": identifier, "Revision": "A", "Gates": [{"Name": "gate"}], "Steps": [
        {"Name": "S", "PathSelectors": [{"Expression": "Container.Qty > 1", "Path": {"Name": "branch"}}],
         "ReworkPaths": [{"Name": "redo", "ToStep": {"Name": "OTHER", "Parent": {"Name": "other-wf"}}}],
         "ReworkPathSelectors": [], "SchedulingDetail": {"SetupTime": 5}}]}


def test_advanced_fields_preserved_without_overwriting_step_refs(monkeypatch):
    monkeypatch.setattr(module, "_read_steps", AsyncMock(return_value=node()))
    monkeypatch.setattr(module, "request", AsyncMock(return_value=json.dumps(advanced())))
    r = json.loads(asyncio.run(module.get_workflow_structure("wf")))
    assert r["complete"]
    step = r["workflows"]["wf"]["steps"][0]
    assert step["spec"]["InstanceId"] == "spec"
    assert step["advanced"]["PathSelectors"][0]["Expression"] == "Container.Qty > 1"
    assert step["advanced"]["ReworkPaths"][0]["ToStep"]["Parent"]["Name"] == "other-wf"


@pytest.mark.parametrize("change", ["revision", "steps", "missing", "nextlink"])
def test_incomplete_advanced_reads_never_claim_complete(monkeypatch, change):
    data = advanced()
    if change == "revision": data["Revision"] = "B"
    if change == "steps": data["Steps"] = []
    if change == "missing": del data["Steps"][0]["PathSelectors"]
    if change == "nextlink": data["Steps"][0]["PathSelectors@odata.nextLink"] = "next"
    monkeypatch.setattr(module, "_read_steps", AsyncMock(return_value=node()))
    monkeypatch.setattr(module, "request", AsyncMock(return_value=json.dumps(data)))
    r = json.loads(asyncio.run(module.get_workflow_structure("wf")))
    assert not r["complete"] and r["warnings"]
    assert r["workflows"]["wf"]["steps"][0]["spec"]


def test_subworkflow_cycle_stops(monkeypatch):
    async def steps(identifier):
        return node(identifier, 2, {"InstanceId": "wf", "Name": "wf", "Revision": "A"})
    monkeypatch.setattr(module, "_read_steps", steps)
    async def request(method, path, params=None, **kwargs):
        assert method == "GET"
        return json.dumps(advanced() if params else {"InstanceID": "wf", "Name": "wf", "Revision": "A"})
    monkeypatch.setattr(module, "request", request)
    r = json.loads(asyncio.run(module.get_workflow_structure("wf")))
    assert not r["complete"]
    assert len(r["workflows"]) == 1
    assert any("循环" in w for w in r["warnings"])


@pytest.mark.parametrize("depth, complete, count", [(0, False, 1), (1, True, 2)])
def test_depth_limit_and_shared_subworkflow(monkeypatch, depth, complete, count):
    async def steps(identifier):
        if identifier == "child":
            return node("child")
        parent = node("wf", 2, {"InstanceId": "child", "Name": "child", "Revision": "A"})
        second = copy.deepcopy(parent["steps"][0]); second["name"] = "T"
        parent["steps"].append(second)
        return parent
    async def request(method, path, params=None, **kwargs):
        identifier = path.rsplit("/", 1)[-1]
        if not params:
            return json.dumps({"InstanceID": identifier, "Name": identifier, "Revision": "A"})
        data = advanced(identifier)
        if identifier == "wf":
            second = copy.deepcopy(data["Steps"][0]); second["Name"] = "T"
            data["Steps"].append(second)
        return json.dumps(data)
    reads = AsyncMock(side_effect=steps)
    monkeypatch.setattr(module, "_read_steps", reads)
    monkeypatch.setattr(module, "request", request)
    result = json.loads(asyncio.run(module.get_workflow_structure("wf", depth)))
    assert result["complete"] is complete
    assert len(result["workflows"]) == count
    assert reads.await_count == count
    assert not any("循环" in warning for warning in result["warnings"])


def test_cycles_include_nondefault_branch():
    steps = [{"name": "A", "paths": [{"to_step": {"Name": "B"}}]},
             {"name": "B", "paths": [{"to_step": {"Name": "A"}, "is_default": False}]}]
    assert module._cycles(steps) == [["A", "B", "A"]]


def test_resolver_exact_revision_and_request_local_cache(monkeypatch):
    mock = AsyncMock(side_effect=[json.dumps({"value": [{"InstanceID": "s"}]}),
                                 json.dumps({"InstanceID": "s", "Name": "O'Brien", "Revision": "B"})])
    monkeypatch.setattr(module, "request", mock)
    async def run():
        ctx = module.ReadContext()
        ref = {"Name": "O'Brien", "Revision": "B"}
        first = await ctx.resolve("Specs", ref)
        assert await ctx.resolve("Specs", ref) == first
    asyncio.run(run())
    assert mock.await_count == 2
    assert mock.call_args_list[0].kwargs["params"]["$filter"] == "Name eq 'O''Brien' and Revision eq 'B'"


def test_resolver_ror_ignores_stale_id(monkeypatch):
    mock = AsyncMock(side_effect=[json.dumps({"value": [{"InstanceID": "current"}]}),
        json.dumps({"InstanceID": "current", "Name": "WF", "Revision": "B", "IsRevOfRcd": True})])
    monkeypatch.setattr(module, "request", mock)
    r = asyncio.run(module.ReadContext().resolve("Workflows", {"InstanceId": "old", "Name": "WF", "Revision": "A", "UseROR": True}))
    assert r["Revision"] == "B"
    assert mock.call_args_list[0].kwargs["params"]["$filter"] == "Name eq 'WF' and IsRevOfRcd eq true"


@pytest.mark.parametrize("data", [{"value": []}, {"value": [{"InstanceID": "a"}, {"InstanceID": "b"}]}])
def test_resolver_never_guesses_ambiguous_reference(monkeypatch, data):
    monkeypatch.setattr(module, "request", AsyncMock(return_value=json.dumps(data)))
    with pytest.raises(ValueError):
        asyncio.run(module.ReadContext().resolve("Specs", {"Name": "X", "Revision": "A"}))


def test_product_chain_keeps_partial_spec_when_operation_fails(monkeypatch):
    monkeypatch.setattr(module, "_read_steps", AsyncMock(return_value=node()))
    async def read(method, path, params=None, **kwargs):
        data = {"/api/Products/p": {"Name": "P", "Workflow": {"InstanceId": "wf", "Name": "wf", "Revision": "A"}},
                "/api/Specs/spec": {"Name": "SPEC", "Revision": "A", "InstanceID": "spec", "Operation": None}}
        if path == "/api/Workflows/wf":
            return json.dumps(advanced() if params else {"Name": "wf", "Revision": "A", "InstanceID": "wf"})
        return json.dumps(data[path])
    monkeypatch.setattr(module, "request", read)
    r = json.loads(asyncio.run(module.get_product_process("p")))
    assert not r["complete"]
    assert r["specs"]["spec"]["Name"] == "SPEC"
    assert r["workflows"]["wf"]["steps"][0]["resolution_error"]


def test_subworkflow_uses_verified_metadata_field(monkeypatch):
    first = {"Name": "WF", "Revision": "A", "Steps": [
        {"Name": "child", "StepType": 2, "SubWorkflow": {"Name": "SUB", "Revision": "B"}}]}
    second = copy.deepcopy(first); second["Steps"][0]["Paths"] = []
    monkeypatch.setattr(workflow_steps, "request", AsyncMock(side_effect=[json.dumps(first), json.dumps(second)]))
    r = asyncio.run(workflow_steps._read_steps("wf"))
    assert r["steps"][0]["subworkflow"] == {"Name": "SUB", "Revision": "B"}


def test_history_two_pages_keep_row_alignment(monkeypatch):
    async def read(method, path, **kwargs):
        body = kwargs["body"]
        assert body["showAllTxnTypes"] and body["showFullHistory"]
        assert "$expand" not in kwargs["params"]
        start = body["resultsetStartRow"]
        return json.dumps({"Histories": [{"InstanceId": str(start)}, {"InstanceId": str(start + 1)}],
                           "HistoryDisplayTexts": [str(start), str(start + 1)]})
    monkeypatch.setattr(container_inquiry, "request", read)
    for start in [1, 3]:
        r = json.loads(asyncio.run(container_inquiry.get_container_history("SN", start_row=start, limit=2)))
        assert r["data"]["Histories"][0]["InstanceId"] == str(start)
        assert r["pages"]["Histories"]["next_start_row_to_try"] == start + 2
        assert not r["complete"]


def test_order_missing_procedure_is_not_zero_production(monkeypatch):
    monkeypatch.setattr(container_inquiry, "request", AsyncMock(return_value='HTTP 400\nResponse: {"ErrorCode":"16780524"}'))
    r = json.loads(asyncio.run(container_inquiry.get_mfgorder_execution("MO")))
    assert r["status"] == "procedure_not_configured"
    assert r["error"]["ErrorCode"] == "16780524"
    assert not r["complete"]


def test_empty_inquiry_falls_back_to_fixed_bounded_mainline(monkeypatch):
    ref = {"InstanceId": "0004108000000002", "Name": "BOX-0001"}
    mock = AsyncMock(side_effect=[json.dumps({"Container": ref, "Histories": [], "HistoryDisplayTexts": []}),
        json.dumps([{"HistoryMainlineId": str(i)} for i in range(3)])])
    monkeypatch.setattr(container_inquiry, "request", mock)
    result = json.loads(asyncio.run(container_inquiry.get_container_history("BOX-0001", limit=2)))
    assert result["history_scope"] == "direct_container_only"
    assert len(result["data"]["Histories"]) == 2
    assert result["pages"]["Histories"]["has_more"] is True
    assert not result["complete"]
    query = mock.call_args.kwargs["body"]["queryText"]
    assert "OFFSET 0 ROWS FETCH NEXT 3 ROWS ONLY" in query
    assert "BOX-0001" not in query


@pytest.mark.parametrize("identifier", ["bad' OR 1=1", "", None])
def test_mainline_never_interpolates_unverified_identifiers(monkeypatch, identifier):
    mock = AsyncMock()
    monkeypatch.setattr(container_inquiry, "request", mock)
    result = json.loads(asyncio.run(container_inquiry._mainline_history(
        {"Name": "BOX", "InstanceId": identifier}, "BOX", None, 1, 2)))
    assert result["error"]
    mock.assert_not_called()


def test_mainline_failed_query_preserves_inquiry_evidence(monkeypatch):
    ref = {"InstanceId": "0004108000000002", "Name": "BOX"}
    monkeypatch.setattr(container_inquiry, "request", AsyncMock(side_effect=[
        json.dumps({"Container": ref, "Histories": [], "HistoryDisplayTexts": []}), "Error: query permission denied"]))
    result = json.loads(asyncio.run(container_inquiry.get_container_history("BOX")))
    assert result["data"]["Container"] == ref
    assert "permission denied" in result["fallback_error"]
    assert not result["complete"]


def test_order_success_tasks_preserved(monkeypatch):
    rows = [{"Name": "MO", "Tasks": [{"Name": "Inspect", "Status": 2}]}]
    monkeypatch.setattr(container_inquiry, "request", AsyncMock(return_value=json.dumps({"MfgOrderInfos": rows})))
    r = json.loads(asyncio.run(container_inquiry.get_mfgorder_execution("MO", info_depth=4)))
    assert r["data"]["MfgOrderInfos"] == rows


def test_invalid_limits_and_request_budget(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(module, "request", mock)
    assert json.loads(asyncio.run(module.get_workflow_structure("wf", 6)))["error"]
    with pytest.raises(ValueError):
        asyncio.run(module.ReadContext(0).get("Specs", "id"))
    mock.assert_not_called()
