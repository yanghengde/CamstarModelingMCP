"""
Container movement MCP tools
============================
Swagger: Shopfloor /api/MoveStd, /api/MoveIn, /api/MoveOut
"""

import json
import math
import re
from pathlib import Path
from typing import Optional

from core.http_client import request_shopfloor
from tools import mcp
from tools.container_inquiry import get_container_details
from tools.process_context import ReadContext


_MOVE_KEYS = {
    "close": "close",
    "comments": "comments",
    "container": "container",
    "moveallqty": "moveAllQty",
    "path": "path",
    "qty": "qty",
    "resource": "resource",
    "thruputallqty": "thruputAllQty",
    "toresource": "toResource",
}

_REFERENCE_KEYS = {
    "cdotypename": "cdoTypeName",
    "instanceid": "instanceId",
    "name": "name",
}


def _merge_case_insensitive(
    target: dict,
    source: dict,
    known_keys: Optional[dict[str, str]] = None,
) -> None:
    """Recursively merge JSON fields without OData case-only duplicates."""
    canonical_keys = {key.casefold(): key for key in target}
    for key, value in source.items():
        folded_key = key.casefold()
        target_key = canonical_keys.get(
            folded_key,
            (known_keys or {}).get(folded_key, key),
        )
        current_value = target.get(target_key)
        if isinstance(current_value, dict) and isinstance(value, dict):
            _merge_case_insensitive(
                current_value,
                value,
                _REFERENCE_KEYS,
            )
        elif isinstance(value, dict) and target_key in {
            "container",
            "path",
            "resource",
            "toResource",
        }:
            normalized_value: dict = {}
            _merge_case_insensitive(
                normalized_value,
                value,
                _REFERENCE_KEYS,
            )
            target[target_key] = normalized_value
        else:
            target[target_key] = value
        canonical_keys[folded_key] = target_key


def _container_ref(name: str, instance_id: Optional[str]) -> dict:
    reference: dict = {"name": name.strip()}
    if instance_id:
        reference["instanceId"] = instance_id.strip()
    return reference


async def _movement_context(name, instance_id, level_name, service):
    """Read current identity and static move permission; never POST Shopfloor."""
    if instance_id and not re.fullmatch(r"[0-9a-fA-F]{16}", instance_id):
        raise ValueError("container_instance_id 须为查询返回的16位 InstanceID。")
    raw = await get_container_details(name, instance_id, view="status")
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('容器状态查询返回非JSON：'+str(raw)) from None
    if not isinstance(result, dict) or result.get('error'):
        raise ValueError('容器状态查询失败：'+str(result.get('error') if isinstance(result,dict) else result))
    status = result.get("data", {}).get("ContainerStatusDetails")
    if result.get("complete") is not True or not isinstance(status, dict):
        raise ValueError("容器状态读取失败或不完整；未执行移动。")
    ref = status.get("Container") or {}
    cid = ref.get("InstanceId") or ref.get("InstanceID")
    if (ref.get("Name") != name or not isinstance(cid, str)
            or not re.fullmatch(r"[0-9a-fA-F]{16}", cid)
            or instance_id and cid != instance_id):
        raise ValueError("容器身份未唯一核实；不能用层级 Level 节点代替实际 InstanceID。")
    if type(status.get("Status")) is not int or status["Status"] != 1:
        raise ValueError("容器不是已确认的 Active 状态；未执行移动。")
    in_process = status.get("InProcess")
    if type(in_process) is not bool:
        raise ValueError("InProcess 未明确返回，不能推断是否已进站。")
    if service == "MoveIn" and in_process:
        raise ValueError("当前站已进站（InProcess=true），不能重复 MoveIn。")
    if service == "MoveOut" and not in_process:
        raise ValueError("当前站尚未进站（InProcess=false），不能执行 MoveOut。")
    level_ref = status.get("ContainerLevel")
    if level_name and (not isinstance(level_ref, dict) or level_ref.get("Name") != level_name.strip()):
        raise ValueError("明确指定的层级与现场容器层级不符；未执行移动。")
    level = await ReadContext(max_requests=3).resolve("ContainerLevels", level_ref, revisioned=False)
    if level.get("AllowMove") is not True:
        raise ValueError("当前层级 AllowMove=false 或未明确允许；未执行移动，不自动修改层级配置。")
    return cid


def _apply_quantity(
    payload: dict,
    move_all_qty: bool,
    qty: Optional[float],
) -> Optional[str]:
    payload["moveAllQty"] = move_all_qty
    if move_all_qty:
        if qty is not None:
            return "❌ qty must be omitted when move_all_qty is true."
        return None
    if qty is None or type(qty) not in (int, float) or not math.isfinite(qty) or qty <= 0:
        return "❌ qty must be greater than 0 when move_all_qty is false."
    payload["qty"] = qty
    return None


def _merge_body_json(payload: dict, body_json: Optional[str]) -> Optional[str]:
    if not body_json:
        return None
    try:
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key.casefold() in {k.casefold() for k in value}:
                    raise ValueError("重复字段：" + key)
                value[key] = item
            return value
        extra = json.loads(body_json, object_pairs_hook=unique)
    except ValueError as exc:
        return f"❌ Invalid body_json: {exc}"
    if not isinstance(extra, dict):
        return "❌ Invalid body_json: expected a JSON object."
    unknown = set(key.casefold() for key in extra) - set(_MOVE_KEYS)
    if unknown:
        return "❌ 不支持的移动字段：" + ", ".join(sorted(unknown)) + "；不能猜测 Details/ServiceDetails 或覆盖签名/采集要求。"
    for key, value in extra.items():
        if key.casefold() == "container":
            return "❌ body_json 不可覆盖移动目标；请使用 container_name/container_instance_id。"
        if isinstance(value, dict) and any(k.casefold() not in _REFERENCE_KEYS for k in value):
            return "❌ 引用含不支持字段（包括 Level）；仅使用名称或实际 InstanceID。"
    _merge_case_insensitive(payload, extra, _MOVE_KEYS)
    return None


async def _submit(service, payload, instance_id, level_name):
    try:
        allowed = set(_MOVE_KEYS.values()) - ({"thruputAllQty"} if service == "MoveStd" else set())
        if service == "MoveIn":
            allowed = {"container", "resource", "comments"}
        if set(payload) - allowed:
            raise ValueError(service + " 不支持这些字段：" + ", ".join(sorted(set(payload) - allowed)))
        for field in ("path", "resource", "toResource"):
            if field in payload:
                ref = payload[field]
                if (not isinstance(ref, dict) or not ref or set(ref) - set(_REFERENCE_KEYS.values())
                        or not (isinstance(ref.get("name"), str) and ref["name"].strip()
                                or isinstance(ref.get("instanceId"), str)
                                and re.fullmatch(r"[0-9a-fA-F]{16}", ref["instanceId"]))):
                    raise ValueError(field + " 须为明确名称或16位实例ID引用，不能传 Level/任意对象。")
        if "comments" in payload and payload["comments"] is not None and not isinstance(payload["comments"], str):
            raise ValueError("comments 须为字符串。")
        # Recheck after merging: body_json cannot bypass quantity validation.
        if service != "MoveIn":
            if type(payload.get("moveAllQty")) is not bool:
                raise ValueError("moveAllQty 须为布尔值。")
            error = _apply_quantity(payload, payload["moveAllQty"], payload.get("qty"))
            if error:
                return error
        for field in ("close", "thruputAllQty"):
            if field in payload and type(payload[field]) is not bool:
                raise ValueError(field + " 须为布尔值。")
        name = payload["container"]["name"]
        cid = await _movement_context(name, instance_id, level_name, service)
        from tools.production_reporting import ensure_no_unresolved_throughput
        ensure_no_unresolved_throughput(cid)
        payload["container"] = _container_ref(name, cid)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return "❌ 移动前检查未通过：" + str(exc)
    result = await request_shopfloor("POST", "/api/" + service, body=payload)
    if service == "MoveOut" and "Transaction Details" in result:
        return result + ("\nMoveOut 是含 Move/Thruput/ChangeQty 的组合事务；此错误未证明缺少顶层 JSON 字段。"
                         "本机原生定义中 Transaction Details 精确对应 ChangeQty.ServiceDetails（Required=2）；"
                         "Thruput.ServiceDetails 的标签为 Containers to Thruput。候选已收敛到 ChangeQty，"
                         "实时 ChangeQty Swagger 也将同ID字段标为必填，但尚未取得 MoveOut 运行失败栈。用 get_moveout_diagnostics 只读查看证据；"
                         "需核实服务端 MoveOut.ChangeQty 自动创建/必填明细处理，不为过站改工序吞吐层级，"
                         "不造零调整/损失明细，不关校验或换事务，不重发。")
    return result


@mcp.tool
async def get_moveout_diagnostics(container_name: str, container_instance_id: Optional[str] = None) -> str:
    """只读核对真实容器ID、当前状态、AllowMove、工序吞吐层级及MoveOut原生静态证据。
    不调用Shopfloor执行/预览/事件，不修改配置，不生成可执行计划。
    Transaction Details在本机原生副本中匹配ChangeQty.ServiceDetails；与实际部署及
    失败栈的一致性尚未验证，不能作为最终根因证明。层级Parent/ChildLevels配置
    不证明实际容器没有子容器。本工具不替代实际族谱查询或原生失败日志。
    ready_for_execution=null表示未评估执行就绪，不能解释为禁止执行。
    静态必填字段不证明MoveOut不能自动填充明细或已经存在契约阻塞。
    仅当前请求的真实移动失败触发停止规则，历史失败不构成永久禁令。
    """
    out = {'complete':False, 'writes_executed':False, 'root_cause_verified':False,
           'ready_for_execution':None, 'actual_child_containers_verified':False, 'errors':[],
           'execution_assessment': {
               'status':'not_evaluated',
               'reason':'只读诊断未执行事务，也不评估当前请求的授权或失败停止状态。',
               'failure_fence_scope':'current_agent_request',
           },
           'diagnosis': {
               'status':'unconfirmed_hypothesis',
               'candidate':'ChangeQty.ServiceDetails',
               'contract_blocker_verified':False,
               'runtime_initialization_observed':False,
               'unsupported_inference':'根Swagger未暴露子服务明细，不证明服务不能自动生成明细或必须修改部署。',
           }}
    try:
        path = Path(__file__).resolve().parents[1] / 'docs' / 'moveout-native-requirements.json'
        out['native_contract_evidence'] = json.loads(path.read_text(encoding='utf-8'))
        raw = await get_container_details(container_name, container_instance_id, view='status')
        data = json.loads(raw)
        if data.get('error') or data.get('complete') is not True:
            raise ValueError(str(data.get('error') or '容器读取不完整。'))
        row = data.get('data',{}).get('ContainerStatusDetails',{})
        ref = row.get('Container',{})
        cid = ref.get('InstanceId') or ref.get('InstanceID')
        if (ref.get('Name') != container_name.strip() or not isinstance(cid,str)
                or not re.fullmatch('[0-9a-fA-F]{16}',cid)
                or container_instance_id and cid != container_instance_id):
            raise ValueError('实际Container身份未核实；不能使用视图行ID。')
        out.update(container_instance_id=cid, container_ref=ref,
            status_view_record_instance_id=row.get('InstanceID'),
            status={k:row.get(k) for k in ('StepName','OperationName','InProcess','Status','Qty','LastMoveDate')})
        ctx = ReadContext(max_requests=6)
        level = await ctx.resolve('ContainerLevels', row.get('ContainerLevel'), revisioned=False)
        operation = await ctx.resolve('Operations', row.get('Operation'), revisioned=False)
        out.update(level={k:level.get(k) for k in ('InstanceID','Name','AllowMove')},
                   operation={k:operation.get(k) for k in ('InstanceID','Name','ThruputReportingLevel')})
        out['complete'] = True
        out['notes'] = ['静态定义匹配不是实际失败栈；不据此更改配置或构造ChangeQty明细。',
                        'ParentLevels/ChildLevels是层级配置，不是实际容器子成员清单。',
                        'ready_for_execution=null表示本工具未评估，不是阻止移动的证据。',
                        '新请求有明确移动授权时重新核实状态/历史及路线；历史失败不等于本轮失败，不自动重放旧请求。']
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        out['errors'].append(str(exc))
    return json.dumps(out, ensure_ascii=False)


def _add_common_fields(
    payload: dict,
    resource_name: Optional[str],
    comments: Optional[str],
) -> None:
    if resource_name:
        payload["resource"] = {"name": resource_name}
    if comments is not None:
        payload["comments"] = comments


@mcp.tool
async def container_move(
    container_name: str,
    container_level_name: Optional[str] = None,
    move_all_qty: bool = True,
    qty: Optional[float] = None,
    path_name: Optional[str] = None,
    resource_name: Optional[str] = None,
    to_resource_name: Optional[str] = None,
    close: bool = False,
    comments: Optional[str] = None,
    body_json: Optional[str] = None,
    container_instance_id: Optional[str] = None,
) -> str:
    """
    Move a Container through its standard workflow path.
    POST Shopfloor /api/MoveStd

    Reads actual Container identity, Active/InProcess and level AllowMove first.
    Use container_instance_id for nonunique names; level is checked locally,
    never serialized in ContainerRef. By default the entire quantity
    follows the default path. For a partial move set move_all_qty=false and
    provide qty. body_json supports basic fields only; it cannot replace the
    target Container, bypass quantity checks, or inject collection/signatures.
    """
    if not container_name.strip():
        return "❌ container_name must not be empty."

    payload: dict = {
        "container": _container_ref(container_name, container_instance_id),
        "close": close,
    }
    quantity_error = _apply_quantity(payload, move_all_qty, qty)
    if quantity_error:
        return quantity_error
    if path_name:
        payload["path"] = {"name": path_name}
    _add_common_fields(payload, resource_name, comments)
    if to_resource_name:
        payload["toResource"] = {"name": to_resource_name}

    merge_error = _merge_body_json(payload, body_json)
    if merge_error:
        return merge_error
    return await _submit("MoveStd", payload, container_instance_id, container_level_name)


@mcp.tool
async def container_move_in(
    container_name: str,
    container_level_name: Optional[str] = None,
    resource_name: Optional[str] = None,
    comments: Optional[str] = None,
    body_json: Optional[str] = None,
    container_instance_id: Optional[str] = None,
) -> str:
    """
    Move a Container into processing at its current workflow step.
    POST Shopfloor /api/MoveIn

    container_name is required. Supply resource_name when the operation or
    dispatch configuration requires a resource. Reads actual identity, Active,
    InProcess=false and level AllowMove before writing. Level is a local check,
    never a reference node. Advanced collection/signature fields are unsupported.
    """
    if not container_name.strip():
        return "❌ container_name must not be empty."

    payload: dict = {
        "container": _container_ref(container_name, container_instance_id),
    }
    _add_common_fields(payload, resource_name, comments)
    merge_error = _merge_body_json(payload, body_json)
    if merge_error:
        return merge_error
    return await _submit("MoveIn", payload, container_instance_id, container_level_name)


@mcp.tool
async def container_move_out(
    container_name: str,
    container_level_name: Optional[str] = None,
    move_all_qty: bool = True,
    qty: Optional[float] = None,
    thruput_all_qty: bool = True,
    path_name: Optional[str] = None,
    resource_name: Optional[str] = None,
    to_resource_name: Optional[str] = None,
    close: bool = False,
    comments: Optional[str] = None,
    body_json: Optional[str] = None,
    container_instance_id: Optional[str] = None,
) -> str:
    """
    Move a Container out of processing and onward through its workflow.
    POST Shopfloor /api/MoveOut

    By default all Container quantity and throughput quantity are processed.
    For a partial move set move_all_qty=false and provide qty. Use body_json
    only for basic declared movement fields. Reads actual identity, Active,
    InProcess=true and level AllowMove first. Transaction Details errors may
    originate in internal Thruput/ChangeQty; do not invent Details/ServiceDetails,
    retry with Qty, or substitute MoveStd. This tool is not a full-route executor.
    """
    if not container_name.strip():
        return "❌ container_name must not be empty."

    payload: dict = {
        "container": _container_ref(container_name, container_instance_id),
        "thruputAllQty": thruput_all_qty,
        "close": close,
    }
    quantity_error = _apply_quantity(payload, move_all_qty, qty)
    if quantity_error:
        return quantity_error
    if path_name:
        payload["path"] = {"name": path_name}
    _add_common_fields(payload, resource_name, comments)
    if to_resource_name:
        payload["toResource"] = {"name": to_resource_name}

    merge_error = _merge_body_json(payload, body_json)
    if merge_error:
        return merge_error
    return await _submit("MoveOut", payload, container_instance_id, container_level_name)
