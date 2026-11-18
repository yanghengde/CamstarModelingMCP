"""Regression coverage for Spec usage, transaction reads and revision plans."""

import asyncio
import copy
import json
import re
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.safety import confirmation_policy
from tools import get_tool_func, mcp, spec_context as sc, spec_versions as sv
from tools.modeling_workspace import compare_modeling_versions


def result(coroutine):
    return json.loads(asyncio.run(coroutine))


def record(identifier, name, revision="A", current=True):
    return {"InstanceID": identifier, "Name": name, "Revision": revision, "IsRevOfRcd": current}


def ref(obj, **changes):
    return {"InstanceId": obj["InstanceID"], "Name": obj["Name"], "Revision": obj["Revision"], **changes}


@pytest.fixture
def camstar(monkeypatch, tmp_path):
    source = record("s1", "SPEC")
    target = record("s2", "SPEC", "B", False)
    leaf, parent, unrelated = [record(wid, name) for wid, name in (("w1", "Leaf"), ("w2", "Parent"), ("w3", "Unrelated"))]
    product = record("p1", "Product") | {"Workflow": ref(parent)}
    operation = {"InstanceID": "op1", "Name": "Op"}
    rows = {
        "s1": source | {field: [] for field in sv.NAV_LISTS} | {
            "Operation": {"InstanceId": "op1", "Name": "Op"}, "Setup": None, "ElectronicProcedure": None,
            "DocumentSet": None, "ResourceGroup": None, "SchedulingDetail": None, "WIPMsgDefMgr": None,
            "BillOfProcessAssignments": [], "Status": 1, "Description": "source", "IsFrozen": False,
        },
    }
    workflows = {
        "w1": leaf | {"Steps": [{"Name": "Fixed", "StepType": 1, "Spec": ref(source, UseROR=None)},
                              {"Name": "Dynamic", "StepType": 1, "Spec": ref(target, UseROR=True)}]},
        "w2": parent | {"Steps": [{"Name": "Child", "StepType": 2, "SubWorkflow": ref(leaf)}]},
        "w3": unrelated | {"Steps": []},
    }
    collections = {"Specs": rows, "Workflows": workflows, "Products": {"p1": product}, "Operations": {"op1": operation}}
    requests = []

    async def get(self, entity, key=None, params=None):
        requests.append((entity, key, params))
        values = collections[entity]
        if key:
            return copy.deepcopy(values[key])
        candidates = list(values.values())
        expr = params.get("$filter", "")
        for field, actual in re.findall(r"(Name|Revision) eq '([^']+)'", expr):
            candidates = [item for item in candidates if item[field] == actual]
        if "IsRevOfRcd eq true" in expr:
            candidates = [item for item in candidates if item["IsRevOfRcd"]]
        start = params.get("$skip", 0)
        return {"value": copy.deepcopy(candidates[start:start + params["$top"]])}

    monkeypatch.setattr(sc.ReadContext, "get", get)
    monkeypatch.setattr(sv, "PLAN_DB", tmp_path / "spec.sqlite3")
    return collections, requests


def test_where_used_tracks_fixed_and_ror_via_subworkflow_and_bounds(camstar, monkeypatch):
    collections, _ = camstar
    # A cycle preserves the finite evidence path and does not make the results duplicate.
    collections["Workflows"]["w1"]["Steps"].append({
        "Name": "Ancestor", "StepType": 2,
        "SubWorkflow": {"Name": "Parent", "InstanceId": "old-id", "UseROR": True},
    })
    # Detail GET in the deployed adapter can omit the ROR flag returned by the list.
    original_get = sc.ReadContext.get

    async def get(self, entity, key=None, params=None):
        response = await original_get(self, entity, key, params)
        if entity == "Workflows" and key:
            response["IsRevOfRcd"] = None
        return response

    monkeypatch.setattr(sc.ReadContext, "get", get)
    found = result(sc.get_spec_usage("SPEC", "A"))
    assert found["complete"] and [s["reference_mode"] for s in found["steps"]] == ["fixed", "use_ror"]
    assert found["steps"][1]["resolves_to_requested_version"]
    assert [row["step"] for row in found["products"][0]["spec_paths"]["fixed"]] == ["Child", "Fixed"]
    limited = result(sc.get_spec_usage("SPEC", "A", limit=1, max_workflows=2))
    assert not limited["complete"] and not limited["coverage"]["workflow_inventory_complete"]
    assert limited["pages"]["steps"]["next_offset"] == 1


def test_where_used_recovers_sparse_product_references_with_verified_storage(camstar, monkeypatch):
    collections, _ = camstar
    collections["Products"]["p1"]["Workflow"] = None
    stored = AsyncMock(return_value=[
        {"ProductId": "p1", "ProductName": "Product", "ProductRevision": "A", "WorkflowId": "w2",
         "WorkflowBaseId": "0000000000000000", "WorkflowName": "Parent", "WorkflowRevision": "A"},
    ])
    monkeypatch.setattr(sc, "sql_rows", stored)
    found = result(sc.get_spec_usage("SPEC", "A"))
    assert found["complete"] and found["coverage"]["product_workflows_from_storage"] == 1
    assert found["products"][0]["workflow_reference_mode"] == "fixed"
    stored.side_effect = ValueError("Query access denied")
    partial = result(sc.get_spec_usage("SPEC", "A"))
    assert not partial["complete"] and partial["steps"] and not partial["products"]


def test_transaction_uses_numeric_cdos_all_transactions_and_verified_refs(camstar, monkeypatch):
    collections, _ = camstar
    spec = collections["Specs"]["s1"]
    spec.update(
        TxnMap=[{"TxnToUse": 2650, "DataCollectionDef": {"Name": "Checks", "Revision": "1", "CDOTypeName": "UserDataCollectionDef"}}],
        LabelTxnMap=[{"TxnType": 2650, "PrinterLabelDefinition": {"Name": "Label"}}],
        ESigTxnMap=[{"AllTxns": True, "ESigRequirement": {"Name": "Signature"}}, {"TxnId": 6843}],
        StartTimerTxnMap=[{"TxnType": 2650, "ProcessTimer": {"Name": "Cure", "Revision": "1"}}],
    )
    reader = AsyncMock(return_value={"complete": True})
    expanded = AsyncMock(return_value={"Name": "definition"})
    monkeypatch.setattr(sc, "definition", reader)
    monkeypatch.setattr(sc, "resolve_configuration", expanded)
    out = result(sc.get_spec_transaction_requirements("SPEC", "A", "MoveIn"))
    assert out["complete"] and out["transaction"]["id"] == 2650
    assert reader.await_args.args[2] == "user"
    assert out["mappings"]["ESigTxnMap"]["items"][0]["match"] == "all_transactions"
    assert out["mappings"]["StartTimerTxnMap"]["items"][0]["definition"]["Name"] == "definition"
    assert out["execution_allowed"] is None and not out["dynamic_requirements_verified"]
    assert result(sc.get_spec_transaction_requirements("SPEC", "A", "MoveIn", 6843))["error"]
    unknown = result(sc.get_spec_transaction_requirements("SPEC", "A", "CustomTxn"))
    assert not unknown["complete"] and unknown["mappings"]["LabelTxnMap"]["unidentified_rows"] == 1


def test_separate_native_reads_preserve_root_and_validate_expanded_instance():
    summary = record("s1", "SPEC")
    root = summary | {"AllowReprocess": True, "Description": "Stored", "Operation": {"Name": "Op"}}
    detail = summary | {"Description": None, "Operation": None, "IsRevOfRcd": None, "TxnMap": [{"TxnToUse": 2200}]}
    ctx = sc.ReadContext()
    ctx.get = AsyncMock(side_effect=[root, detail])
    data, paginated = asyncio.run(sc.read_spec_views(ctx, summary, ["TxnMap"]))
    assert data["Operation"] == {"Name": "Op"} and data["AllowReprocess"] is True
    assert data["Description"] == "Stored" and data["TxnMap"] == [{"TxnToUse": 2200}] and not paginated
    ctx.get = AsyncMock(side_effect=[root, detail | {"InstanceID": "wrong"}])
    with pytest.raises(ValueError, match="身份"):
        asyncio.run(sc.read_spec_views(ctx, summary, ["TxnMap"]))


def test_compare_expands_maps_ignoring_generated_ids(camstar):
    collections, _ = camstar
    source = collections["Specs"]["s1"]
    source["TxnMap"] = [{"InstanceID": "row1", "TxnToUse": 2200, "Required": True}]
    target = copy.deepcopy(source) | record("s2", "SPEC", "B", False)
    target["TxnMap"][0]["InstanceID"] = "row2"
    target["Description"] = "changed"
    collections["Specs"]["s2"] = target
    out = result(compare_modeling_versions("Specs", "SPEC", "A", "SPEC", "B"))
    assert out["complete"] and not out["right"]["IsRevOfRcd"]
    assert [c["path"] for c in out["changes"]] == ["/configuration/description"]


def test_create_and_promote_use_separate_confirmed_writes_and_readback(camstar, monkeypatch):
    collections, _ = camstar
    writes = []

    async def handler(request):
        body = json.loads(request.content)
        writes.append((request.method, request.url.path, body))
        if request.method == "PATCH":
            assert body["isRevOfRcd"] is False and body["operation"] == {"name": "Op"}
            collections["Specs"]["s2"] = copy.deepcopy(collections["Specs"]["s1"]) | record("s2", "SPEC", "B", False)
            return httpx.Response(201, json={"value": "SPEC created successfully"})
        assert request.method == "PUT" and body == {"isRevOfRcd": True}
        collections["Specs"]["s1"]["IsRevOfRcd"] = False
        collections["Specs"]["s2"]["IsRevOfRcd"] = True
        return httpx.Response(200, json={"value": "SPEC updated successfully"})

    async def flow():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(sv, "get_client", lambda: client)
            monkeypatch.setattr(sv, "get_headers", lambda: {})
            for action in ("create", "set_current"):
                plan = json.loads(await sv.prepare_spec_revision(action, "SPEC", "A", "B"))
                assert plan["state"] == "ready" and plan["confirmation_required"]
                if action == "set_current":
                    assert plan["impact"]["ror_following_steps"] == 1
                    assert plan["impact"]["ror_following_products"] == 1
                args = plan["plan_id"], plan["plan_digest"]
                assert "SPEC" in sv.confirmation_summary(*args)
                receipt = json.loads(await sv.execute_spec_revision(*args))
                assert receipt["state"] == "unknown" and not receipt["retry_allowed"]
                assert json.loads(await sv.execute_spec_revision(*args))["submitted"] is False
                checked = json.loads(await sv.reconcile_spec_revision(*args))
                assert checked["verified_success"] and checked["source_preserved"] and checked["ror_matches"]
                assert checked["current_spec"]["Revision"] == ("A" if action == "create" else "B")
                assert json.loads(await sv.get_spec_revision_plan(*args))["state"] == "verified_applied"

    asyncio.run(flow())
    assert [(method, path.rsplit("/api", 1)[1]) for method, path, _ in writes] == [("PATCH", "/Specs"), ("PUT", "/Specs/s2")]


def test_stale_plan_and_unknown_outcome_never_replay(camstar, monkeypatch):
    collections, _ = camstar
    args = ("create", "SPEC", "A", "B")
    plan = result(sv.prepare_spec_revision(*args))
    collections["Specs"]["s1"]["Description"] = "concurrent edit"
    assert result(sv.execute_spec_revision(plan["plan_id"], plan["plan_digest"]))["submitted"] is False

    sent = []

    async def timeout(request):
        sent.append(request)
        collections["Specs"]["s2"] = copy.deepcopy(collections["Specs"]["s1"]) | record("s2", "SPEC", "B", False)
        raise httpx.ReadTimeout("write response lost", request=request)

    async def flow():
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            monkeypatch.setattr(sv, "get_client", lambda: client)
            monkeypatch.setattr(sv, "get_headers", lambda: {})
            new = json.loads(await sv.prepare_spec_revision(*args))
            keys = new["plan_id"], new["plan_digest"]
            await sv.execute_spec_revision(*keys)
            unchecked = json.loads(await sv.review_spec_revision(*keys, "applied", "Audit-42", "MES audit confirms the commit"))
            assert "先回读" in unchecked["error"]
            checked = json.loads(await sv.reconcile_spec_revision(*keys))
            assert checked["target_matches"] and checked["ror_matches"] and not checked["verified_success"]
            assert json.loads(await sv.prepare_spec_revision("set_current", "SPEC", "A", "B"))["submitted"] is False
            audit = json.loads(await sv.review_spec_revision(*keys, "applied", "Audit-42", "MES audit confirms the commit"))
            assert audit["state"] == "human_applied" and not audit["verified_success"]
            assert json.loads(await sv.execute_spec_revision(*keys))["submitted"] is False

    asyncio.run(flow())
    assert len(sent) == 1


def test_copy_rejects_unsupported_config_and_tools_are_registered(camstar):
    collections, _ = camstar
    collections["Specs"]["s1"]["AttachDocumentDetails"] = [{"DocumentName": "SOP"}]
    assert "AttachDocumentDetails" in result(sv.prepare_spec_revision("create", "SPEC", "A", "B"))["error"]
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"get_spec_usage", "get_spec_transaction_requirements", "reconcile_spec_revision"} <= names
    for name in ("execute_spec_revision", "review_spec_revision"):
        assert name in names and get_tool_func(name) is not None
        assert confirmation_policy({"function": {"name": name, "arguments": "{}"}}) == ("update", True)
