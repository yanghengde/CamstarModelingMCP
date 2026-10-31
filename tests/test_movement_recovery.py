"""Offline regression of the failed SN_T_0029 dialog, without moving a Container."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent import langgraph_runtime as runtime
from agent import llm_client
from agent.prompts import MOVEMENT_RULE, SYSTEM_PROMPT
from agent.tool_routing import ROUTING_INSTRUCTION
from config import SAFE_UPDATE_THRESHOLD
from core.movement_policy import MOVEMENT_RETRY_BLOCKED, movement_failed
from tools import container_moves as moves


CID = "0000000000000029"


def state(in_process=True):
    return {"complete": True, "data": {"ContainerStatusDetails": {
        "Container": {"Name": "SN_T_0029", "InstanceId": CID},
        "ContainerLevel": {"Name": "BOX", "InstanceId": "0000000000000001"},
        "Status": 1, "InProcess": in_process,
    }}}


def setup(monkeypatch, snapshot=None, allow_move=True):
    read = AsyncMock(return_value=json.dumps(state() if snapshot is None else snapshot))
    level = AsyncMock(return_value={"Name": "BOX", "AllowMove": allow_move})
    request = AsyncMock(return_value="updated")
    monkeypatch.setattr(moves, "get_container_details", read)
    monkeypatch.setattr(moves.ReadContext, "resolve", level)
    monkeypatch.setattr(moves, "request_shopfloor", request)
    return read, level, request


def test_forbidden_level_stops_before_moveout_post(monkeypatch):
    read, level, request = setup(monkeypatch, allow_move=False)
    result = asyncio.run(moves.container_move_out("SN_T_0029", container_level_name="BOX"))
    assert "AllowMove" in result and result.startswith("❌")
    read.assert_awaited_once()
    level.assert_awaited_once()
    request.assert_not_awaited()


@pytest.mark.parametrize("func,in_process", [(moves.container_move_in, True), (moves.container_move_out, False)])
def test_wrong_processing_state_cannot_send_duplicate_or_premature_move(monkeypatch, func, in_process):
    _, _, request = setup(monkeypatch, snapshot=state(in_process))
    assert "InProcess" in asyncio.run(func("SN_T_0029"))
    request.assert_not_awaited()


@pytest.mark.parametrize("change", ["identity", "inactive", "incomplete", "unknown_inprocess", "level_mismatch"])
def test_unverified_identity_or_state_does_not_write(monkeypatch, change):
    snapshot = state()
    details = snapshot["data"]["ContainerStatusDetails"]
    args = {}
    if change == "identity": details["Container"]["Name"] = "OTHER"
    if change == "inactive": details["Status"] = 3
    if change == "incomplete": snapshot["complete"] = False
    if change == "unknown_inprocess": details.pop("InProcess")
    if change == "level_mismatch": args["container_level_name"] = "PCB"
    _, _, request = setup(monkeypatch, snapshot)
    assert asyncio.run(moves.container_move_out("SN_T_0029", **args)).startswith("❌")
    request.assert_not_awaited()


def test_real_precheck_pins_id_and_never_serializes_level(monkeypatch):
    read, level, request = setup(monkeypatch)
    assert asyncio.run(moves.container_move_out("SN_T_0029", container_level_name="BOX",
                                               container_instance_id=CID, path_name="LNK-01")) == "updated"
    read.assert_awaited_once_with("SN_T_0029", CID, view="status")
    assert request.await_args.kwargs["body"]["container"] == {"name": "SN_T_0029", "instanceId": CID}
    assert request.await_args.kwargs["body"]["path"] == {"name": "LNK-01"}
    assert request.await_count == 1


@pytest.mark.parametrize("body", [
    '{"Details":{"Qty":1}}', '{"ServiceDetails":[{"Qty":1}]}',
    '{"Container":{"Name":"OTHER"}}', '{"Resource":{"Level":{"Name":"BOX"}}}',
    '{"qty":1}', '{"moveAllQty":false,"qty":0}', '{"moveAllQty":false,"qty":NaN}',
    '{"close":true,"Close":false}', '{"comments":"a","comments":"b"}',
])
def test_guessed_fields_and_body_overrides_cannot_bypass_preflight(monkeypatch, body):
    read, _, request = setup(monkeypatch)
    assert asyncio.run(moves.container_move_out("SN_T_0029", body_json=body)).startswith("❌")
    read.assert_not_awaited()
    request.assert_not_awaited()


def test_transaction_details_error_is_not_assumed_to_be_missing_root_field(monkeypatch):
    _, _, request = setup(monkeypatch)
    request.return_value = '❌ HTTP 400: Field "Transaction Details" requires input'
    result = asyncio.run(moves.container_move_out("SN_T_0029"))
    assert "未证明缺少顶层" in result and "ThruputReportingLevel" in result
    request.assert_awaited_once()


@pytest.mark.parametrize("func,body", [
    (moves.container_move, '{"thruputAllQty":true}'),
    (moves.container_move_in, '{"moveAllQty":true}'),
    (moves.container_move_in, '{"path":{"name":"LNK-01"}}'),
])
def test_service_fields_are_not_transferred_between_move_variants(monkeypatch, func, body):
    read, _, request = setup(monkeypatch)
    assert asyncio.run(func("SN_T_0029", body_json=body)).startswith("❌")
    read.assert_not_awaited()
    request.assert_not_awaited()


def call(name, args):
    return {"id": name, "function": {"name": name, "arguments": json.dumps(args)}}


def test_langgraph_blocks_changed_parameters_and_substitution_but_allows_readback(monkeypatch):
    execute = AsyncMock(return_value='❌ Field "Transaction Details" requires input')
    read = AsyncMock(return_value='{"complete":true}')
    monkeypatch.setattr(runtime, "get_stream_writer", lambda: lambda _: None)
    monkeypatch.setattr(runtime, "record_perf", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "record_tool_outcome", lambda **k: None)
    monkeypatch.setattr(runtime, "get_tool_func", lambda name: read if name.startswith("get_") else execute)
    monkeypatch.setattr(runtime, "interrupt", lambda _: pytest.fail("No approval for blocked retries or reads"))
    initial = {"messages": [], "update_count": SAFE_UPDATE_THRESHOLD + 1,
               "pending_tool_calls": [call("container_move_out", {"container_name": "SN_T_0029"})]}
    failed = {**initial, **asyncio.run(runtime._execute_tool_node(initial))}
    assert "sn_t_0029" in failed["blocked_movement_targets"]
    for name in ("container_move_out", "container_move", "container_move_in"):
        pending = [call(name, {"container_name": "SN_T_0029", "body_json": '{"Qty":1}'})]
        current = {**failed, "pending_tool_calls": pending}
        assert runtime._policy_node(current) == {}
        result = asyncio.run(runtime._execute_tool_node(current))
        assert result["messages"][-1]["content"] == MOVEMENT_RETRY_BLOCKED
    execute.assert_awaited_once()
    current = {**failed, "pending_tool_calls": [call("get_container_details", {"container_name": "SN_T_0029"})]}
    assert runtime._policy_node(current) == {}
    asyncio.run(runtime._execute_tool_node(current))
    read.assert_awaited_once()


def test_approved_original_write_does_not_approve_new_write(monkeypatch):
    pending = [call("container_move_out", {"container_name": "C"})]
    from agent.safety import plan_fingerprint
    state = {"pending_tool_calls": pending, "update_count": SAFE_UPDATE_THRESHOLD + 1,
             "approved_plan": plan_fingerprint(pending)}
    monkeypatch.setattr(runtime, "interrupt", lambda _: pytest.fail("Original plan already approved"))
    assert runtime._policy_node(state) == {}
    observed = []
    monkeypatch.setattr(runtime, "interrupt", lambda value: observed.append(value) or {"approved": True})
    changed = [call("container_move_out", {"container_name": "C", "path_name": "NEW"})]
    result = runtime._policy_node({**state, "pending_tool_calls": changed})
    assert observed and result["approved_plan"] == plan_fingerprint(changed)


def test_policy_explanation_is_used_by_both_engines():
    assert MOVEMENT_RULE in SYSTEM_PROMPT and MOVEMENT_RULE in ROUTING_INSTRUCTION
    assert movement_failed('{"ErrorCode":"13500597"}')
    assert movement_failed("❌ Request timed out")
    assert not movement_failed('"MoveOut updated"')


def legacy_scenario(monkeypatch, batches, execute):
    messages = [{"role": "system", "content": "offline"}, {"role": "assistant", "content": "context"}]
    monkeypatch.setattr(llm_client, "get_user_messages", lambda *a: messages)
    monkeypatch.setattr(llm_client, "save_user_session", lambda *a: None)
    monkeypatch.setattr(llm_client, "record_perf", lambda *a, **k: None)
    read = AsyncMock(return_value='{"complete":true}')
    monkeypatch.setattr(llm_client, "get_tool_func", lambda name: read if name.startswith("get_") else execute)

    async def create(**kwargs):
        batch = batches.pop(0) if batches else []
        async def stream():
            calls = [SimpleNamespace(index=i, id=f"{len(batches)}-{i}", function=SimpleNamespace(
                name=entry["function"]["name"], arguments=entry["function"]["arguments"])) for i, entry in enumerate(batch)]
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None if calls else "已核实，停止继续移动。", tool_calls=calls))])
        return stream()

    monkeypatch.setattr(llm_client.oai_client.chat.completions, "create", create)
    async def run():
        return [event async for event in llm_client.legacy_chat_stream("offline", "进行moveout并读取状态", "test")]
    asyncio.run(run())
    return read, messages


def test_legacy_does_not_repeat_failed_move_with_new_parameters(monkeypatch):
    execute = AsyncMock(return_value="❌ Request timed out; outcome unknown")
    batches = [
        [call("container_move_out", {"container_name": "SN_T_0029"})],
        [call("container_move", {"container_name": "SN_T_0029", "body_json": '{"Qty":1}'})],
        [call("get_container_details", {"container_name": "SN_T_0029"})],
    ]
    read, messages = legacy_scenario(monkeypatch, batches, execute)
    execute.assert_awaited_once()
    read.assert_awaited_once()
    assert any(m.get("content") == MOVEMENT_RETRY_BLOCKED for m in messages)


def test_legacy_mixed_batch_still_reads_when_new_write_requires_approval(monkeypatch):
    execute = AsyncMock(return_value="updated")
    batches = [[call("container_move_out", {"container_name": f"C{i}"})]
               for i in range(SAFE_UPDATE_THRESHOLD)]
    batches.append([call("container_move_out", {"container_name": "NEW"}),
                    call("get_container_details", {"container_name": "NEW"})])
    read, messages = legacy_scenario(monkeypatch, batches, execute)
    assert execute.await_count == SAFE_UPDATE_THRESHOLD
    read.assert_awaited_once()
    assert any("legacy 模式不执行" in m.get("content", "") for m in messages)
