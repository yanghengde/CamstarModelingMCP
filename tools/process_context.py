"""Bounded read-only process traversal using actual object references."""
import json
from urllib.parse import quote

from core.http_client import request
from tools import mcp
from tools.modeling_details import _compact, _has_next_link, raw_json
from tools.workflow_steps import _read_steps

ADVANCED_FIELDS = ("PathSelectors", "ReworkPaths", "ReworkPathSelectors", "SchedulingDetail")


class ReadContext:
    def __init__(self, max_requests=160):
        self.remaining = max_requests
        self.cache = {}

    async def get(self, entity, key=None, params=None):
        if self.remaining <= 0:
            raise ValueError("达到查询请求上限，结果未完整展开。")
        self.remaining -= 1
        path = f"/api/{entity}" + (f"/{quote(str(key), safe='')}" if key else "")
        raw = await request("GET", path, params=params, response_formatter=raw_json)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError(str(raw)) from None
        if not isinstance(data, dict) or data.get("error") or data.get("ErrorCode"):
            raise ValueError(str(data))
        return data

    async def resolve(self, entity, ref, revisioned=True):
        if not isinstance(ref, dict) or not ref:
            raise ValueError(f"{entity} 引用未返回，不能推断关联对象。")
        cache_key = (entity, json.dumps(ref, sort_keys=True))
        if cache_key in self.cache:
            return self.cache[cache_key]
        name, revision = ref.get("Name"), ref.get("Revision")
        use_ror = ref.get("UseROR") is True
        instance_id = ref.get("InstanceId") or ref.get("InstanceID")
        if instance_id and not use_ror:
            data = await self.get(entity, instance_id)
        else:
            if not name or (revisioned and not use_ror and not revision):
                raise ValueError(f"{entity} 缺少可唯一定位的 ID 或名称/版本。")
            escape = lambda text: str(text).replace("'", "''")
            expression = f"Name eq '{escape(name)}'"
            if revisioned:
                expression += " and IsRevOfRcd eq true" if use_ror else f" and Revision eq '{escape(revision)}'"
            found = await self.get(entity, params={"$filter": expression, "$top": 2})
            rows = found.get("value")
            if not isinstance(rows, list) or len(rows) != 1 or _has_next_link(found):
                raise ValueError(f"{entity} 引用未唯一匹配：{name} / {revision}。")
            identifier = rows[0].get("InstanceID")
            if not identifier:
                raise ValueError(f"{entity} 匹配结果缺少 InstanceID。")
            data = await self.get(entity, identifier)
        if (name and data.get("Name") != name) or (revisioned and not use_ror and revision and data.get("Revision") != revision):
            raise ValueError(f"{entity} 返回对象与引用名称/版本不一致。")
        if use_ror and data.get("IsRevOfRcd") is not True:
            raise ValueError(f"{entity} 当前记录版本无法确认。")
        if not data.get("InstanceID"):
            raise ValueError(f"{entity} 详情缺少 InstanceID。")
        if instance_id and not use_ror and data["InstanceID"] != instance_id:
            raise ValueError(f"{entity} 返回的 InstanceID 与引用不一致。")
        self.cache[cache_key] = data
        return data


def _cycles(steps):
    """Report DFS back edges, not every possible cyclic walk."""
    names = {s["name"] for s in steps}
    adjacency = {s["name"]: [] for s in steps}
    for step in steps:
        for path in (step.get("paths") or []):
            target = (path.get("to_step") or {}).get("Name")
            if target in names:
                adjacency[step["name"]].append(target)
    colors, found = {}, []
    def visit(node, trail):
        colors[node] = 1
        for target in adjacency[node]:
            if colors.get(target) == 1:
                found.append(trail[trail.index(target):] + [target])
            elif not colors.get(target):
                visit(target, trail + [target])
        colors[node] = 2
    for name in adjacency:
        if not colors.get(name):
            visit(name, [name])
    return found


async def _structure(workflow_id, max_depth, context, include_advanced=True):
    nodes, links, warnings = {}, [], []
    complete = True

    async def walk(identifier, depth, ancestors):
        nonlocal complete
        if identifier in ancestors:
            warnings.append(f"子 Workflow 循环引用：{' -> '.join(ancestors + [identifier])}")
            complete = False
            return
        if identifier in nodes:
            return
        if depth > max_depth or len(nodes) >= 20:
            warnings.append(f"达到子 Workflow 深度或数量上限，未展开 {identifier}。")
            complete = False
            return
        if context.remaining < 2:
            raise ValueError("达到查询请求上限。")
        context.remaining -= 2
        node = await _read_steps(identifier)
        if node.get("error"):
            raise ValueError(node["error"])
        nodes[identifier] = node
        steps = node["steps"]
        if len(steps) > 200:
            raise ValueError("单个 Workflow 超过 200 步，请使用 list_workflow_steps 分别查询。")
        if not node.get("steps_complete") or any(s.get("paths") is None for s in steps):
            complete = False
        if include_advanced:
            try:
                expanded = await context.get("Workflows", identifier, {
                    "$expand": "Steps($expand=" + ",".join(ADVANCED_FIELDS) + "),Gates"})
                children = expanded.get("Steps")
                expected = node["workflow"]
                if expanded.get("Name") != expected["name"] or expanded.get("Revision") != expected["revision"]:
                    raise ValueError("高级结构读取期间 Workflow 名称/版本不一致。")
                if not isinstance(children, list) or len(children) != len(steps) or {s.get("Name") for s in children} != {s["name"] for s in steps}:
                    raise ValueError("高级结构读取期间步骤集合发生变化。")
                if _has_next_link(expanded):
                    complete = False
                    warnings.append(f"{identifier} 高级结构存在服务端分页。")
                node["gates"] = _compact(expanded.get("Gates"))
                if not isinstance(node["gates"], list):
                    complete = False
                    warnings.append(f"{identifier} Gates 未返回。")
                by_name = {s["Name"]: s for s in children}
                for step in steps:
                    item = by_name[step["name"]]
                    step["advanced"] = {field: _compact(item.get(field)) for field in ADVANCED_FIELDS}
                    for field in ADVANCED_FIELDS:
                        value = item.get(field)
                        expected_type = dict if field == "SchedulingDetail" else list
                        if not isinstance(value, expected_type):
                            complete = False
                            warnings.append(f"{identifier}/{step['name']}: {field} 未返回、为 null 或类型不符。")
            except (ValueError, TypeError, AttributeError) as exc:
                complete = False
                warnings.append(f"{identifier} 高级结构无法完整读取：{exc}")
        node["path_cycles"] = _cycles(steps)
        node["path_cycle_scope"] = "当前 Workflow 内普通 Paths 的回边；返工路径保留原始目标引用，不按同名步骤合并。"
        node["scope"] = "普通路径及所请求高级配置；条件表达式仅展示，不执行求值。路径循环可能是合法返工。"
        for step in steps:
            if step.get("step_type") == 2:
                try:
                    child = await context.resolve("Workflows", step.get("subworkflow"))
                    child_id = child["InstanceID"]
                    links.append({"workflow_id": identifier, "step": step["name"], "child_workflow_id": child_id})
                    await walk(child_id, depth + 1, ancestors + [identifier])
                except ValueError as exc:
                    complete = False
                    warnings.append(f"{identifier}/{step['name']} 子 Workflow：{exc}")
            elif step.get("step_type") != 1 or step.get("subworkflow"):
                complete = False
                warnings.append(f"{identifier}/{step['name']} 步骤类型或子 Workflow 引用不一致。")

    await walk(workflow_id, 0, [])
    return {"root_workflow_id": workflow_id, "complete": complete, "workflows": nodes,
            "subworkflow_links": links, "warnings": warnings,
            "consistency_note": "跨请求读取，不是事务快照；已核对名称、版本和步骤集合。"}


def _bounded(result):
    encoded = raw_json(result)
    if len(encoded) > 160000:
        return raw_json({"complete": False, "error": "结果超过输出上限；请降低 max_depth 或按 Workflow/Step 分别查询。"})
    return encoded


@mcp.tool
async def get_workflow_structure(workflow_id: str, max_depth: int = 3) -> str:
    """Read Workflow Steps, Gates, path selectors/conditions, rework paths/selectors and scheduling. Recursively follow actual StepType=2 Workflow refs with cycle detection. InstanceID required. max_depth=0..5; at most 20 workflows/200 steps each. Expressions are not evaluated; check complete/warnings."""
    if not workflow_id.strip() or not 0 <= max_depth <= 5:
        return raw_json({"error": "workflow_id 必填，max_depth 必须为 0..5。", "complete": False})
    try:
        return _bounded(await _structure(workflow_id.strip(), max_depth, ReadContext()))
    except ValueError as exc:
        return raw_json({"error": str(exc), "complete": False})


@mcp.tool
async def get_product_process(product_id: str, max_depth: int = 3) -> str:
    """Read actual Product -> Workflow (including subworkflows) -> Step -> Spec -> Operation chain. Product InstanceID required. Resolve exact revisions or explicit UseROR; never infer refs from names. Includes advanced route configuration. Bounded requests and request-local cache; check complete and per-step errors."""
    if not product_id.strip() or not 0 <= max_depth <= 5:
        return raw_json({"error": "product_id 必填，max_depth 必须为 0..5。", "complete": False})
    context = ReadContext()
    try:
        product = await context.get("Products", product_id.strip())
        workflow = await context.resolve("Workflows", product.get("Workflow"))
        result = await _structure(workflow["InstanceID"], max_depth, context)
        result["product"] = _compact(product)
        result["specs"], result["operations"] = {}, {}
        for node in result["workflows"].values():
            for step in node["steps"]:
                if step.get("step_type") != 1:
                    continue
                try:
                    spec = await context.resolve("Specs", step.get("spec"))
                    result["specs"][spec["InstanceID"]] = _compact(spec)
                    step["resolved_spec_id"] = spec["InstanceID"]
                    operation = await context.resolve("Operations", spec.get("Operation"), revisioned=False)
                    result["operations"][operation["InstanceID"]] = _compact(operation)
                    step["resolved_operation_id"] = operation["InstanceID"]
                    step_op = step.get("operation") or {}
                    if step_op.get("Name") and step_op["Name"] != operation.get("Name"):
                        raise ValueError("Step.Operation 与 Spec.Operation 不一致，已保留两者，不推断实际执行工序。")
                except ValueError as exc:
                    step["resolution_error"] = str(exc)
                    result["complete"] = False
        return _bounded(result)
    except ValueError as exc:
        return raw_json({"error": str(exc), "complete": False})
