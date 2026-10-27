"""Read-only modeling diagnostics, version comparisons and immutable route templates."""
import json
import time
import uuid

from tools import mcp
from tools.process_context import ReadContext, get_workflow_structure, get_product_process
from tools.modeling_details import raw_json
from tools import import_plans as plans


async def resolve_object(kind, name, revision):
    if kind not in {"Workflows", "Products"}:
        raise ValueError("仅支持 Workflows / Products")
    return await ReadContext().resolve(kind, {"Name": name, "Revision": revision, "UseROR": not bool(revision)})


def inspect_structure(data):
    issues = []
    def issue(level, location, message):
        issues.append({"level": level, "location": location, "message": message})
    for wid, node in data.get("workflows", {}).items():
        steps = node.get("steps", [])
        names = {s["name"] for s in steps}
        starts = [s["name"] for s in steps if s.get("is_first_step") is True]
        adjacency = {n: set() for n in names}
        if not steps:
            issue("unknown", wid, "未返回步骤，无法确认路线是否为空")
        if len(names) != len(steps):
            issue("error", wid, "步骤名称重复")
        if len(starts) != 1:
            issue("warning", wid, "未识别到唯一首步骤，请核对入口配置")
        for step in steps:
            location = f"{wid}/{step['name']}"
            if step.get("step_type") == 1 and not step.get("spec"):
                issue("unknown", location, "Spec 引用未返回，不能直接断定未配置")
            if step.get("resolution_error"):
                issue("unknown", location, step["resolution_error"])
            if step.get("paths") is None:
                issue("unknown", location, "普通路径未完整读取")
            for path in step.get("paths") or []:
                target = (path.get("to_step") or {}).get("Name")
                if target not in names:
                    issue("warning", location, f"路径 {path.get('name')} 的目标未在当前步骤集合中找到：{target}")
                else:
                    adjacency[step["name"]].add(target)
        if len(starts) == 1:
            seen, queue = set(), starts[:]
            while queue:
                name = queue.pop()
                if name not in seen:
                    seen.add(name)
                    queue.extend(adjacency[name] - seen)
            for name in sorted(names - seen):
                issue("warning", f"{wid}/{name}", "普通 Paths 下不可达；需结合条件、返工及入口规则确认")
        for cycle in node.get("path_cycles", []):
            issue("info", wid, "路径环（可能为合法返工）：" + " → ".join(cycle))
    for warning in data.get("warnings", []):
        issue("unknown", "读取范围", warning)
    if data.get("error"):
        issue("unknown", "对象", data["error"])
    return {"issues": issues, "read_complete": data.get("complete") is True,
            "conclusion": "存在待核对项" if issues else "当前已读取范围未发现问题；不代表全部业务规则验收通过"}


@mcp.tool
async def check_modeling_integrity(kind: str, name: str, revision: str = "") -> str:
    """Read-only Product or Workflow integrity inspection. kind=Products/Workflows. Checks actual Spec/Operation refs, paths and ordinary-path reachability. Warnings are not automatically defects; missing data remains unknown."""
    try:
        obj = await resolve_object(kind, name, revision)
        if kind == "Products":
            data = json.loads(await get_product_process(obj["InstanceID"]))
        else:
            data = json.loads(await get_workflow_structure(obj["InstanceID"]))
            ctx = ReadContext()
            for node in data.get("workflows", {}).values():
                for step in node["steps"]:
                    if step.get("step_type") == 1:
                        try:
                            spec = await ctx.resolve("Specs", step.get("spec"))
                            operation = await ctx.resolve("Operations", spec.get("Operation"), revisioned=False)
                            step["spec_details"], step["operation_details"] = spec, operation
                            if (step.get("operation") or {}).get("Name") not in (None, operation.get("Name")):
                                raise ValueError("Step.Operation 与 Spec.Operation 不一致，请核对实际工序引用")
                        except ValueError as exc:
                            step["resolution_error"] = str(exc)
                            data["complete"] = False
        return raw_json({"structure": data, **inspect_structure(data)})
    except ValueError as exc:
        return raw_json({"error": str(exc), "read_complete": False})


def differences(left, right, path=""):
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for key in sorted(left.keys() | right.keys()):
            at = f"{path}/{key}"
            if key not in left or key not in right:
                result.append({"path": at, "change": "added" if key not in left else "removed",
                               "before": left.get(key), "after": right.get(key)})
            else:
                result.extend(differences(left[key], right[key], at))
        return result
    return [] if left == right else [{"path": path, "change": "changed", "before": left, "after": right}]


async def comparison_snapshot(kind, obj):
    if kind == "Products":
        return plans._stable(obj), True
    data = json.loads(await get_workflow_structure(obj["InstanceID"], max_depth=0))
    node = data.get("workflows", {}).get(obj["InstanceID"])
    if not node:
        raise ValueError(data.get("error", "路线结构不可用"))
    # Compare named steps, not unstable list indexes or temporary CDO IDs.
    return {"steps": {s["name"]: plans._stable(s) for s in node["steps"]},
            "gates": plans._stable(node.get("gates"))}, data.get("complete") is True


@mcp.tool
async def compare_modeling_versions(kind: str, left_name: str, left_revision: str,
                                    right_name: str, right_revision: str) -> str:
    """Compare two Products or Workflows without writes. Workflow comparison covers returned steps/ordinary and advanced paths/Gates, not all metadata. Product compares returned detail fields. Exact names/revisions; completeness reported."""
    try:
        left = await resolve_object(kind, left_name, left_revision)
        right = await resolve_object(kind, right_name, right_revision)
        a, ac = await comparison_snapshot(kind, left)
        b, bc = await comparison_snapshot(kind, right)
        return raw_json({"complete": ac and bc, "changes": differences(a, b),
                         "scope": "Workflow 步骤与 Gates；Product 当前详情返回字段。不是全部配置的等价证明。"})
    except ValueError as exc:
        return raw_json({"error": str(exc), "complete": False})


@mcp.tool
async def preview_workflow_copy(source_name: str, source_revision: str, target_name: str, target_revision: str) -> str:
    """Preview a new linear Workflow route from an existing revision. Does not copy all object metadata. Reject existing target, branches, subworkflows, selectors/rework/Gates/scheduling. Returns immutable import plan for existing confirmed execute_workflow_import_plan flow; no writes."""
    try:
        if not target_name.strip() or not target_revision.strip():
            raise ValueError("目标名称和版本必填")
        ctx = ReadContext()
        source_id, source = await plans._existing(ctx, source_name, source_revision)
        if not source_id:
            raise ValueError("来源路线不存在")
        target_id, _ = await plans._existing(ctx, target_name, target_revision)
        if target_id:
            raise ValueError("目标版本已存在；复制只允许创建新版本")
        node = next(iter(source["workflows"].values()))
        if not node.get("standard_route_complete") or len(node["standard_route"]) != len(node["steps"]):
            raise ValueError("来源不是可完整确认的线性路线；不允许丢失分支配置")
        plans._actions(target_name, target_revision, [], source)  # Reject advanced configuration.
        by_name = {s["name"]: s for s in node["steps"]}
        if any(len(s.get("paths") or []) > 1 for s in node["steps"]):
            raise ValueError("来源含分支，暂不支持复制")
        rows = []
        for index, name in enumerate(node["standard_route"], 1):
            spec = await ctx.resolve("Specs", by_name[name].get("spec"))
            rows.append({"excel_row": index, "step_name": name, "spec_name": spec["Name"],
                         "spec_revision": spec["Revision"], "spec_id": spec["InstanceID"]})
        workflow = {"name": target_name, "revision": target_revision, "id": None, "action": "create",
                    "before": None, "after_rows": rows, "description": "", "payloads": plans._actions(target_name, target_revision, rows, None)}
        plan = {"environment_id": plans._environment_id(), "workflows": [workflow], "row_count": len(rows),
                "warning": "从来源快照创建线性路线模板，仅复制步骤名称、Spec 版本及顺序；不复制产品、描述、对象附加属性。路径名称重新生成。非事务写入。"}
        plan_id, digest = uuid.uuid4().hex, plans._digest(plan)
        with plans._db() as db:
            db.execute("INSERT INTO plans VALUES (?,?,?,?,?,NULL)", (plan_id, digest, raw_json(plan), "ready", time.time()))
        return raw_json({"executable": True, "plan_id": plan_id, "plan_digest": digest, "plan": plan})
    except (ValueError, KeyError) as exc:
        return raw_json({"executable": False, "error": str(exc)})
