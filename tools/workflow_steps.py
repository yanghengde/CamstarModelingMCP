"""Read Workflow Steps through the live OData navigation contract.

Swagger: modeling-workflows-live-swagger.json (StepChanges, SpecStepChanges,
MovePathChanges). Live /api/$metadata declares Steps and Paths navigation.
This server returns complete scalar/reference fields only on the leaf level
of an expansion, so read Steps and Steps(Paths) separately and join by name
within the same Workflow revision. Changes-CDO IDs differ between requests.
"""

import json
from urllib.parse import quote

from core.http_client import request
from tools import mcp


def _json(data) -> str:
    # Generic smart_response discards Steps/Paths on large responses.
    return json.dumps(data, ensure_ascii=False)


def _ref(value):
    if not isinstance(value, dict):
        return None
    return {key: value[key] for key in ("Name", "Revision", "UseROR", "InstanceId") if key in value}


def _route(steps: list[dict]) -> tuple[list[str], list[str]]:
    """Follow actual default links; never sort by names or business semantics."""
    warnings = []
    starts = [s for s in steps if s.get("is_first_step") is True]
    if len(starts) != 1:
        return [], ["无法唯一确定首步骤，未推断标准路线顺序。"]
    by_name = {s["name"]: s for s in steps}
    order = []
    current = starts[0]
    while current:
        name = current["name"]
        if name in order:
            warnings.append(f"默认路径存在循环，停止于 {name}。")
            break
        order.append(name)
        if current.get("paths") is None:
            warnings.append(f"{name} 的路径未完整返回，顺序仅为已验证前缀。")
            break
        paths = current["paths"]
        default_name = (current.get("default_path") or {}).get("Name")
        defaults = [p for p in paths if p.get("is_default") is True or (default_name and p.get("name") == default_name)]
        # Camstar may leave IsLastStep false even on a terminal graph node.
        if not paths and not default_name:
            break
        if len(defaults) != 1:
            warnings.append(f"{name} 的默认路径不明确，未猜测后续顺序。")
            break
        target = (defaults[0].get("to_step") or {}).get("Name")
        if target not in by_name:
            warnings.append(f"{name} 的目标步骤未返回：{target}。")
            break
        current = by_name[target]
    return order, warnings


async def _read_steps(workflow_id: str) -> dict:
    if not workflow_id.strip():
        return {"error": "workflow_id 不能为空，请使用 list_workflows 返回的 InstanceID。"}
    path = f"/api/Workflows/{quote(workflow_id.strip(), safe='')}"
    raw = await request("GET", path, params={"$expand": "Steps"}, response_formatter=_json)
    try:
        workflow = json.loads(raw)
    except (ValueError, TypeError):
        return {"error": raw}
    if not isinstance(workflow, dict) or not isinstance(workflow.get("Steps"), list):
        return {"error": "接口未返回 Steps 明细，不能据此认定路线没有步骤。"}
    source = workflow["Steps"]
    names = [s.get("Name") for s in source if isinstance(s, dict)]
    if len(names) != len(source) or any(not n for n in names) or len(set(names)) != len(names):
        return {"error": "步骤名称缺失或重复，无法可靠关联路径。"}
    warnings = []
    partial = any("nextlink" in k.lower() for k in workflow)
    if partial:
        warnings.append("接口存在分页，当前步骤列表不完整。")
    path_steps = {}
    if source:
        raw_paths = await request("GET", path, params={"$expand": "Steps($expand=Paths)"}, response_formatter=_json)
        try:
            expanded = json.loads(raw_paths)
            if not isinstance(expanded, dict) or any(expanded.get(k) != workflow.get(k) for k in ("Name", "Revision")):
                raise ValueError("Workflow revision mismatch")
            children = expanded.get("Steps")
            if not isinstance(children, list) or len(children) != len(names) or {s.get("Name") for s in children} != set(names):
                raise ValueError("Step set changed")
            if any("nextlink" in k.lower() for k in expanded):
                raise ValueError("Paths expansion is paginated")
            path_steps = {s["Name"]: s for s in children}
        except (ValueError, TypeError, AttributeError):
            warnings.append("路径读取失败或两次读取的步骤不一致；保留步骤信息，无法确认完整顺序。")
    steps = []
    for item in source:
        path_item = path_steps.get(item["Name"], {})
        paths = path_item.get("Paths")
        if any("nextlink" in k.lower() for k in path_item):
            paths = None
        steps.append({
            "name": item["Name"], "instance_id": item.get("InstanceID"),
            "step_type": item.get("StepType"), "sequence": item.get("Sequence"),
            "is_first_step": item.get("IsFirstStep"), "is_last_step": item.get("IsLastStep"),
            "spec": _ref(item.get("Spec")), "operation": _ref(item.get("Operation")),
            "subworkflow": _ref(item.get("SubWorkflow")),
            "default_path": _ref(item.get("DefaultPath")),
            "paths": [{"name": p.get("Name"), "is_default": p.get("IsDefaultPath"),
                       "from_step": _ref(p.get("FromStep")), "to_step": _ref(p.get("ToStep"))}
                      for p in paths] if isinstance(paths, list) else None,
        })
    order, route_warnings = _route(steps) if steps else ([], [])
    warnings.extend(route_warnings)
    if not steps:
        warnings.append("接口返回空 Steps；尚不能单凭此结果确认未配置步骤。")
    return {
        "workflow": {"name": workflow.get("Name"), "revision": workflow.get("Revision"), "instance_id": workflow_id},
        "returned_step_count": len(steps), "steps_complete": bool(steps) and not partial,
        "standard_route": order, "standard_route_complete": bool(order) and not warnings,
        "steps": steps, "warnings": warnings,
        "scope": "读取 Steps、实际 Spec/Operation 引用及普通 Paths；不包含返工路径与条件选择器。顺序以 standard_route 为准，sequence 仅为原始字段。",
    }


@mcp.tool
async def list_workflow_steps(workflow_id: str) -> str:
    """Read all returned Steps, actual Spec/Operation refs and default route order.

    Use for 'Workflow 下有哪些工艺/步骤/Spec，按顺序列出'. workflow_id MUST be
    the InstanceID returned by list_workflows, not a guessed Name:Revision.
    GET /api/Workflows/{id}?$expand=Steps and Steps($expand=Paths).
    Do not infer Spec from Step.Name; use steps[].spec. Check warnings and
    standard_route_complete before claiming complete order. Branch steps remain
    in steps even when absent from standard_route. Read-only; no maintenance/save.
    """
    return _json(await _read_steps(workflow_id))


@mcp.tool
async def get_workflow_step(workflow_id: str, step_name: str) -> str:
    """Read a named Step's actual Spec, Operation and outgoing paths in a Workflow.

    workflow_id is a Workflow InstanceID; step_name is the exact Step name from
    list_workflow_steps, which may differ from the associated Spec name.
    """
    result = await _read_steps(workflow_id)
    if "error" in result:
        return _json(result)
    matches = [s for s in result["steps"] if s["name"] == step_name]
    return _json({"workflow": result["workflow"], "step": matches[0] if matches else None,
                  "warnings": result["warnings"] + ([] if matches else ["返回的步骤中未找到该名称。"])})
