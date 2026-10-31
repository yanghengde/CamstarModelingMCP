"""Regression for collection PATCH/create confusion and GET-snapshot PUT errors.

All tests are offline. The MockTransport contract reproduces the observed Spec
update: PUT accepts just allowReprocess, other current fields are preserved.
It does not assert untested field persistence on other deployed entities.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.prompts import MODELING_UPDATE_RULE, SYSTEM_PROMPT
from agent.tool_routing import ROUTING_INSTRUCTION, select_tools
from core import http_client
from core.modeling_payload import build_update_payload
from core.tool_catalog import DISABLED_MODELING_PATCH_TOOLS, TOOL_POLICIES
from tools import (mfglines, mfgorders, operations, products, producttypes, specs,
                   workflows, mcp)
from tools.tool_discovery import get_capabilities, load_tool_group


CASES = [
    (specs, "spec", "Specs"),
    (operations, "operation", "Operations"),
    (products, "product", "Products"),
    (workflows, "workflow", "Workflows"),
    (mfgorders, "mfgorder", "MfgOrders"),
    (mfglines, "mfgline", "MfgLines"),
    (producttypes, "producttype", "ProductTypes"),
]
INSTANCE_ID = "00071c8000000053"


@pytest.mark.parametrize("module,singular,entity", CASES)
@pytest.mark.parametrize("odata", [False, True])
def test_update_requires_no_creation_fields_and_sends_only_change(monkeypatch, module, singular, entity, odata):
    request = AsyncMock(return_value="updated")
    monkeypatch.setattr(module, "request", request)
    func = getattr(module, "update_" + singular + ("_by_odata_key" if odata else ""))
    assert asyncio.run(func(key=INSTANCE_ID, body_json='{"Description":"new description"}')) == "updated"
    path = f"/api/{entity}({INSTANCE_ID})" if odata else f"/api/{entity}/{INSTANCE_ID}"
    request.assert_awaited_once_with("PUT", path, body={"description": "new description"})


@pytest.mark.parametrize("body_json", [
    "[]", "null", '"value"', "{", "{}",
    '{"UseRor":false}', '{"useROR":true}', '{"__useROR":false}',
    '{"InstanceID":"00071c8000000053","AllowReprocess":true}',
    '{"IsFrozen":false,"AllowReprocess":true}',
    '{"@odata.context":"metadata","AllowReprocess":true}',
    '{"ObjectToChange":null,"AllowReprocess":true}',
    '{"allowReprocess":"true"}',
    '{"allowReprocess":true,"AllowReprocess":false}',
    '{"allowReprocess":true,"allowReprocess":false}',
    '{"operation":{"Name":"OP","name":"OTHER"}}',
])
def test_bad_snapshot_or_ambiguous_payload_never_writes(monkeypatch, body_json):
    request = AsyncMock(return_value="sent")
    monkeypatch.setattr(specs, "request", request)
    result = asyncio.run(specs.update_spec(key=INSTANCE_ID, body_json=body_json))
    assert result.startswith("❌"), result
    request.assert_not_awaited()


def test_case_normalization_distinguishes_root_field_from_reference():
    payload = build_update_payload("Specs", {}, json.dumps({
        "AllowReprocess": False,
        "Operation": {"Name": "OP-MOD-EOL", "InstanceId": "op-id", "CDOTypeName": "Operation"},
        "Setup": {"Name": "SETUP", "Revision": "A", "UseRor": False},
    }))
    assert payload == {
        "allowReprocess": False,
        "operation": {"name": "OP-MOD-EOL", "instanceId": "op-id", "cdoTypeName": "Operation"},
        "setup": {"name": "SETUP", "revision": "A", "useROR": False},
    }


def test_false_zero_empty_string_and_explicit_null_are_preserved():
    assert build_update_payload("Specs", {"description": None},
                                '{"AllowReprocess":false,"Status":0,"Notes":"","ResourceGroup":null}') == {
        "allowReprocess": False, "status": 0, "notes": "", "resourceGroup": None,
    }
    assert build_update_payload("MfgOrders", {"qty": 0}, '{"Qty":5}') == {"qty": 5}
    assert build_update_payload("Specs", {}, '{"AllowReprocess":null}') == {"allowReprocess": None}


def test_nested_list_update_preserves_explicit_action_and_reference_version():
    # List wrappers are write input, unlike the expanded GET snapshots.
    payload = build_update_payload("Specs", {}, json.dumps({
        "TxnMap-Expanded": [{"Key": {"Name": "CollectData"}, "Value": {
            "Required": False, "Sequence": 0,
            "DataCollectionDef": {"Name": "EOL", "Revision": "A", "UseRor": False},
        }}],
    }))
    assert payload == {"txnMap-Expanded": [{"key": {"name": "CollectData"}, "value": {
        "required": False, "sequence": 0,
        "dataCollectionDef": {"name": "EOL", "revision": "A", "useROR": False},
    }}]}
    with pytest.raises(ValueError, match="只读"):
        build_update_payload("Specs", {}, '{"TxnMap-Expanded":[{"value":{"InstanceID":"id"}}]}')


@pytest.mark.parametrize("entity", [case[2] for case in CASES])
def test_shared_rules_apply_across_all_seven_entities(entity):
    with pytest.raises(ValueError, match="只读"):
        build_update_payload(entity, {}, '{"InstanceID":"id","Description":"new"}')
    with pytest.raises(ValueError, match="UseRor"):
        build_update_payload(entity, {}, '{"UseRor":false,"Description":"new"}')


def test_named_fields_keep_old_callers_and_json_overrides_without_case_duplicates(monkeypatch):
    request = AsyncMock(return_value="ok")
    monkeypatch.setattr(specs, "request", request)
    asyncio.run(specs.update_spec(INSTANCE_ID, "SPEC", "A", "OP", description="old",
                                 body_json='{"Description":"new","AllowReprocess":true}'))
    assert request.await_args.kwargs["body"] == {
        "name": "SPEC", "revision": "A", "operation": {"name": "OP"},
        "description": "new", "allowReprocess": True,
    }


def test_mfgorder_does_not_invent_product_revision(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(mfgorders, "request", request)
    assert asyncio.run(mfgorders.update_mfgorder(INSTANCE_ID, product_name="P")).startswith("Error:")
    request.assert_not_awaited()


@pytest.mark.parametrize("name", DISABLED_MODELING_PATCH_TOOLS)
def test_collection_patch_cannot_be_recommended_or_sent(monkeypatch, name):
    module = next(m for m, singular, _ in CASES if name == "patch_" + singular)
    request = AsyncMock()
    monkeypatch.setattr(module, "request", request)
    assert asyncio.run(getattr(module, name)('{"AllowReprocess":true}')).startswith("Error:")
    request.assert_not_awaited()
    assert not TOOL_POLICIES[name].default_visible
    detail = json.loads(asyncio.run(get_capabilities(tool_name=name)))["tool"]
    assert detail["status"] == "disabled"
    loaded = json.loads(asyncio.run(load_tool_group(TOOL_POLICIES[name].group, include_writes=True)))
    assert name not in loaded["tools"]


def test_real_tool_schemas_expose_minimal_updates_but_creation_still_requires_fields():
    registered = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for _, singular, _ in CASES:
        for suffix in ("", "_by_odata_key"):
            assert registered["update_" + singular + suffix].parameters["required"] == ["key"]
    assert set(registered["create_spec"].parameters["required"]) == {"name", "revision", "operation_name"}
    assert "qty" in registered["create_mfgorder"].parameters["required"]


def test_original_multiturn_query_then_allowreprocess_change_loads_update_tool():
    tools = [{"function": {"name": n}} for n in TOOL_POLICIES]
    messages = [
        {"role": "user", "content": "SP-MOD-EOL-001的详细配置"},
        {"role": "assistant", "content": "AllowReprocess: false"},
        {"role": "user", "content": "AllowReprocess这个改为true"},
    ]
    names = {t["function"]["name"] for t in select_tools(tools, messages)}
    assert {"update_spec", "get_spec"} <= names
    assert "patch_spec" not in names
    assert MODELING_UPDATE_RULE in SYSTEM_PROMPT
    assert MODELING_UPDATE_RULE in ROUTING_INSTRUCTION  # legacy and LangGraph


def test_observed_spec_contract_through_real_http_dispatch_and_readback(monkeypatch):
    before = {"InstanceID": INSTANCE_ID, "Name": "SP-MOD-EOL-001", "Revision": "A",
              "AllowReprocess": False, "Operation": {"Name": "OP-MOD-EOL"},
              "IsRevOfRcd": True, "TxnMap-Expanded": []}
    state = dict(before)
    methods = []

    def server(req):
        methods.append(req.method)
        assert req.url.path.endswith("/api/Specs/" + INSTANCE_ID)
        if req.method == "PUT":
            assert req.headers["content-type"] == "application/json"
            assert json.loads(req.content) == {"allowReprocess": True}
            state["AllowReprocess"] = True
            return httpx.Response(200, json={"value": "Spec updated"})
        return httpx.Response(200, json=state)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            monkeypatch.setattr(http_client, "_client", client)
            monkeypatch.setattr(http_client, "get_headers", lambda: {"Content-Type": "application/json"})
            result = await specs.update_spec(key=INSTANCE_ID, body_json='{"AllowReprocess":true}')
            assert "Spec updated" in result
            return json.loads(await specs.get_spec(INSTANCE_ID))

    after = asyncio.run(run())
    assert methods == ["PUT", "GET"]
    assert after == {**before, "AllowReprocess": True}


def test_unknown_write_outcome_is_not_retried(monkeypatch):
    calls = []

    async def server(req):
        calls.append(req.method)
        raise httpx.ReadTimeout("server outcome unknown", request=req)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            monkeypatch.setattr(http_client, "_client", client)
            monkeypatch.setattr(http_client, "get_headers", lambda: {})
            return await specs.update_spec(key=INSTANCE_ID, body_json='{"allowReprocess":true}')

    result = asyncio.run(run())
    assert "timed out" in result
    assert calls == ["PUT"]
