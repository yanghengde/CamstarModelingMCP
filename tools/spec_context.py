"""Bounded Spec reference tracing and transaction configuration reads."""

import asyncio
from collections import defaultdict, deque
import json
from typing import Literal

from core.read_catalog import sql_rows
from tools import mcp
from tools.data_collection import definition
from tools.modeling_details import _has_next_link, raw_json
from tools.process_context import ReadContext
from tools.resources import read_spec_resources
from tools.spec_documents import read_spec_documents

IDENTITY = ("InstanceID", "Name", "Revision", "IsRevOfRcd")
IDENTITY_SELECT = ",".join(IDENTITY)
# Service CDOs confirmed in the deployed CDODefinition table.
SERVICE_IDS = {
    "Start": 2400, "MoveIn": 2650, "MoveOut": 6843, "MoveStd": 6640,
    "CollectData": 2200, "CollectSamplingData": 8452, "ComponentIssue": 6440,
    "Rework": 6660, "ChangeQty": 4000, "Associate": 2210, "Disassociate": 2220,
    "Split": 5490, "Combine": 5520, "PrintContainerLabel": 7309, "ReprintContainerLabel": 7313,
}
MAPS = {
    "TxnMap": ("TxnToUse", "DataCollectionDef", "DataCollectionDefs", True, None),
    "ESigTxnMap": ("TxnId", "ESigRequirement", "ESigRequirements", False, "ESigReqDetails"),
    "LabelTxnMap": ("TxnType", "PrinterLabelDefinition", "PrinterLabelDefinitions", False, "LabelTags"),
    "StartTimerTxnMap": ("TxnType", "ProcessTimer", "ProcessTimers", True, None),
    "EndTimerTxnMap": ("TxnType", "ProcessTimer", "ProcessTimers", True, None),
}
COLLECTION_KIND = {"UserDataCollectionDef": "user", "DataPointCollectionDef": "point"}


def identity(row):
    return {key: row.get(key) for key in IDENTITY}


def text_arg(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label} 须提供明确值，长度不超过 256。")
    return value.strip()


def literal(value):
    return "'" + value.replace("'", "''") + "'"


async def find_spec(ctx, name, revision=None, *, current=False):
    where = f"Name eq {literal(name)}"
    if current:
        where += " and IsRevOfRcd eq true"
    else:
        where += f" and Revision eq {literal(revision)}"
    result = await ctx.get("Specs", params={"$filter": where, "$select": IDENTITY_SELECT, "$top": 2})
    rows = result.get("value")
    if not isinstance(rows, list) or _has_next_link(result) or len(rows) > 1:
        raise ValueError("Spec 版本查询结果不完整或不唯一。")
    if not rows:
        return None
    row = rows[0]
    if (not valid_identity(row) or row["Name"] != name
            or (row.get("IsRevOfRcd") is not True if current else row["Revision"] != revision)):
        raise ValueError("Spec 版本查询返回的对象身份不一致。")
    return row


def valid_identity(row):
    return isinstance(row, dict) and all(isinstance(row.get(k), str) and row[k] for k in IDENTITY[:3])


def same_identity(row, expected):
    return isinstance(row, dict) and all(row.get(k) == expected[k] for k in IDENTITY[:3])


async def exact_spec(ctx, name, revision):
    row = await find_spec(ctx, text_arg(name, "Spec 名称"), text_arg(revision, "Spec 版本"))
    if row is None:
        raise ValueError("未找到指定 Spec 版本。")
    if type(row.get("IsRevOfRcd")) is not bool:
        current = await find_spec(ctx, row["Name"], current=True)
        if current:
            row = row | {"IsRevOfRcd": same_identity(row, current)}
    return row


async def read_spec_views(ctx, spec, fields):
    """The expanded route omits root values, so verify and merge two instance reads."""
    root, expanded = await asyncio.gather(
        ctx.get("Specs", spec["InstanceID"]),
        ctx.get("Specs", spec["InstanceID"], {"$expand": ",".join(fields)}),
    )
    if (not same_identity(root, spec) or not same_identity(expanded, spec)
            or type(spec.get("IsRevOfRcd")) is bool and root.get("IsRevOfRcd") != spec["IsRevOfRcd"]):
        raise ValueError("Spec 清单、详情或展开结果的对象身份或 ROR 状态不一致。")
    merged = {k: v for k, v in root.items() if k not in fields}
    merged.update({k: expanded[k] for k in fields if k in expanded})
    return merged, _has_next_link(root) or _has_next_link(expanded)


def ref_mode(ref):
    if not isinstance(ref, dict) or not ref:
        return None
    if ref.get("UseROR") is not None and type(ref["UseROR"]) is not bool:
        return None
    # Legacy reads return UseROR=null for fixed references with an identity.
    if ref.get("UseROR") is True:
        return "use_ror" if ref.get("Name") else None
    return "fixed" if ref.get("InstanceId") or ref.get("InstanceID") or (ref.get("Name") and ref.get("Revision")) else None


def spec_match(ref, spec):
    mode = ref_mode(ref)
    if mode is None:
        raise ValueError("步骤 Spec 引用缺少有效标识。")
    name, revision = ref.get("Name"), ref.get("Revision")
    ref_id = ref.get("InstanceId") or ref.get("InstanceID")
    if mode == "use_ror":
        if ref_id == spec["InstanceID"] and name != spec["Name"]:
            raise ValueError("步骤 Spec 引用的 ID 与名称冲突。")
        return mode if name == spec["Name"] else None
    if ref_id == spec["InstanceID"]:
        if name not in (None, spec["Name"]) or revision not in (None, spec["Revision"]):
            raise ValueError("步骤 Spec 引用的 ID 与名称或版本冲突。")
        return mode
    if name == spec["Name"] and revision == spec["Revision"]:
        if ref_id:
            raise ValueError("步骤 Spec 引用的名称及版本与 ID 冲突。")
        return mode
    if name == spec["Name"] and not revision and not ref_id:
        raise ValueError("同名 Spec 引用未指明固定版本。")
    return None


async def inventory(ctx, entity, limit, fields):
    rows, seen, offset = [], set(), 0
    while offset <= limit:
        size = min(100, limit + 1 - offset)
        result = await ctx.get(entity, params={
            "$select": fields, "$orderby": "Name,Revision,InstanceID",
            "$top": size, "$skip": offset,
        })
        page = result.get("value")
        if not isinstance(page, list) or len(page) > size:
            return rows, False
        if any(not valid_identity(row) or row["InstanceID"] in seen for row in page):
            return rows, False
        ids = {row["InstanceID"] for row in page}
        if len(ids) != len(page):
            return rows, False
        seen.update(ids)
        rows.extend(page[:limit - offset])
        offset += len(page)
        if offset > limit:
            return rows, False
        if len(page) < size and not _has_next_link(result):
            return rows, True
        if not page:
            return rows, False
    return rows, False


def stored_id(value):
    text = str(value or "").strip().lower()
    return "" if text == "0000000000000000" else text


async def read_product_workflows(products):
    """Read the Workflow refs omitted by the Product collection response."""
    ids = ",".join("N" + literal(product["InstanceID"]) for product in products)
    rows = await sql_rows(
        "SELECT TOP 101 p.ProductId,p.ProductRevision,b.ProductName,p.WorkflowId,p.WorkflowBaseId,"
        "w.WorkflowName,c.WorkflowRevision FROM Product p JOIN ProductBase b ON p.ProductBaseId=b.ProductBaseId "
        "LEFT JOIN Workflow c ON c.WorkflowId=p.WorkflowId LEFT JOIN WorkflowBase w ON w.WorkflowBaseId="
        "COALESCE(NULLIF(NULLIF(p.WorkflowBaseId,'0000000000000000'),''),c.WorkflowBaseId) "
        "WHERE p.ProductId IN (" + ids + ")"
    )
    by_id = {stored_id(row.get("ProductId")): row for row in rows}
    if len(rows) != len(by_id) or len(rows) != len(products):
        raise ValueError("产品持久引用读取缺失或身份重复。")
    refs = {}
    for product in products:
        row = by_id.get(stored_id(product["InstanceID"]))
        if not row or row.get("ProductName") != product["Name"] or row.get("ProductRevision") != product["Revision"]:
            raise ValueError("产品持久引用与 Modeling 清单身份不一致。")
        fixed, base = stored_id(row.get("WorkflowId")), stored_id(row.get("WorkflowBaseId"))
        if fixed:
            refs[product["InstanceID"]] = {"InstanceId": fixed, "Name": row.get("WorkflowName"),
                                           "Revision": row.get("WorkflowRevision"), "UseROR": False}
        elif base:
            refs[product["InstanceID"]] = {"Name": row.get("WorkflowName"), "UseROR": True, "BaseInstanceId": base}
        else:
            refs[product["InstanceID"]] = None
    return refs


def workflow_resolver(rows):
    keys = defaultdict(list)
    for row in rows:
        keys[row["InstanceID"]].append(row)
        keys[row["Name"], row["Revision"]].append(row)
        if row.get("IsRevOfRcd") is True:
            keys[row["Name"], None].append(row)

    def resolve(ref):
        mode = ref_mode(ref)
        name, revision = ref.get("Name"), ref.get("Revision")
        ref_id = ref.get("InstanceId") or ref.get("InstanceID")
        if mode is None:
            raise ValueError("Workflow 引用缺少有效标识。")
        key = (name, None) if mode == "use_ror" else ref_id or (name, revision)
        matches = keys.get(key, [])
        if len(matches) != 1:
            raise ValueError("可见范围内无法唯一解析 Workflow 引用。")
        target = matches[0]
        if (name and name != target["Name"]) or (mode == "fixed" and revision and revision != target["Revision"]):
            raise ValueError("Workflow 引用身份冲突。")
        return target["InstanceID"], mode

    return resolve


def route_paths(steps, parents):
    """One shortest evidence path per route version and Spec reference mode."""
    paths = {}
    pending = deque()
    for entry in steps:
        key = (entry["workflow"]["InstanceID"], entry["reference_mode"])
        if key not in paths:
            paths[key] = [entry]
            pending.append(key)
    while pending:
        child_id, mode = pending.popleft()
        for link in parents.get(child_id, []):
            parent = (link["workflow"]["InstanceID"], mode)
            if parent not in paths:
                paths[parent] = [link, *paths[child_id, mode]]
                pending.append(parent)
    return paths


async def scan_spec_usage(spec, max_workflows, max_products):
    ctx = ReadContext(max_requests=2 * (max_workflows + max_products) // 100 + max_workflows + 15)
    workflows, workflows_complete = await inventory(ctx, "Workflows", max_workflows, IDENTITY_SELECT)
    products, products_complete = await inventory(ctx, "Products", max_products, IDENTITY_SELECT + ",Workflow")
    resolve_workflow = workflow_resolver(workflows)
    direct, product_links, issues = [], [], []
    parents = defaultdict(list)
    read_count = 0

    async def workflow_steps(workflow):
        try:
            async with semaphore:
                root = await ctx.get("Workflows", workflow["InstanceID"], {"$expand": "Steps"})
            if not same_identity(root, workflow) or (type(root.get("IsRevOfRcd")) is bool
                    and root["IsRevOfRcd"] != workflow.get("IsRevOfRcd")):
                raise ValueError("Workflow 清单与步骤读取之间的对象身份或 ROR 状态发生变化。")
            rows = root.get("Steps")
            if not isinstance(rows, list) or _has_next_link(root) or len(rows) > 500:
                raise ValueError("Workflow 步骤未完整返回。")
            return rows
        except ValueError as error:
            return error

    semaphore = asyncio.Semaphore(4)
    for start in range(0, len(workflows), 16):
        batch = workflows[start:start + 16]
        results = await asyncio.gather(*(workflow_steps(row) for row in batch))
        for workflow, steps in zip(batch, results):
            origin = identity(workflow)
            if isinstance(steps, ValueError):
                issues.append({"object": origin, "error": str(steps)})
                continue
            read_count += 1
            for step in steps:
                entry = {"workflow": origin, "step": None, "step_instance_id": None}
                try:
                    if not isinstance(step, dict) or not step.get("Name"):
                        raise ValueError("步骤缺少名称。")
                    entry.update(step=step["Name"], step_instance_id=step.get("InstanceID"))
                    if step.get("StepType") == 1:
                        ref = step.get("Spec")
                        mode = spec_match(ref, spec)
                        if mode:
                            resolved = True if mode == "fixed" else spec.get("IsRevOfRcd")
                            direct.append(entry | {"reference": ref, "reference_mode": mode,
                                                   "resolves_to_requested_version": resolved})
                    elif step.get("StepType") == 2:
                        ref = step.get("SubWorkflow")
                        target, mode = resolve_workflow(ref)
                        parents[target].append(entry | {"reference": ref, "workflow_reference_mode": mode})
                    elif step.get("Spec") or step.get("SubWorkflow") or step.get("StepType") is None:
                        raise ValueError("步骤类型无法可靠识别。")
                except (ValueError, AttributeError) as error:
                    issues.append(entry | {"error": str(error)})

    paths = route_paths(direct, parents)
    fallback = [p for p in products if not p.get("Workflow")]
    stored_workflows = {}
    for start in range(0, len(fallback), 100):
        batch = fallback[start:start + 100]
        try:
            stored_workflows.update(await read_product_workflows(batch))
        except ValueError as error:
            issues.append({"source": "Query/Product.Workflow", "product_count": len(batch), "error": str(error)[:600]})
    for product in products:
        product_id = product["InstanceID"]
        workflow_ref = product.get("Workflow") or stored_workflows.get(product_id)
        if workflow_ref is None and product_id in stored_workflows:
            continue
        try:
            target, mode = resolve_workflow(workflow_ref)
        except (ValueError, AttributeError) as error:
            issues.append({"product": identity(product), "error": str(error)})
            continue
        matches = {kind: paths[target, kind] for kind in ("fixed", "use_ror") if (target, kind) in paths}
        if matches:
            product_links.append({"product": identity(product), "workflow_reference": workflow_ref,
                                  "workflow_reference_mode": mode, "spec_paths": matches,
                                  "currently_uses_requested_version": True if "fixed" in matches else spec.get("IsRevOfRcd")})
    ancestors = [{"workflow": identity(row), "spec_paths": {mode: paths[row["InstanceID"], mode]
                  for mode in ("fixed", "use_ror") if (row["InstanceID"], mode) in paths}}
                 for row in workflows if any((row["InstanceID"], mode) in paths and len(paths[row["InstanceID"], mode]) > 1
                                             for mode in ("fixed", "use_ror"))]
    return {
        "complete": workflows_complete and products_complete and not issues and type(spec.get("IsRevOfRcd")) is bool,
        "coverage": {"workflows_listed": len(workflows), "workflow_steps_read": read_count,
                     "products_listed": len(products), "product_workflows_from_storage": len(stored_workflows),
                     "workflow_inventory_complete": workflows_complete,
                     "product_inventory_complete": products_complete},
        "steps": direct, "ancestor_workflows": ancestors, "products": product_links, "issues": issues,
    }


@mcp.tool
async def get_spec_usage(spec_name: str, spec_revision: str, offset: int = 0, limit: int = 20,
                         max_workflows: int = 250, max_products: int = 500) -> str:
    """按明确 Spec 版本反查可见 Workflow 步骤、上层 Workflow 和产品。fixed 指向目标版本，use_ror 跟随同名 Spec 当前记录版本，可能影响 ROR 切换；按 mode 和 resolves_to_requested_version 解读。各列表独立使用相同的 offset/limit。返回覆盖范围；不含容器、工单历史和其他对象引用。"""
    try:
        if not (0 <= offset and 1 <= limit <= 50 and 1 <= max_workflows <= 1000 and 1 <= max_products <= 3000):
            raise ValueError("offset>=0，limit=1..50，max_workflows=1..1000，max_products=1..3000。")
        async with asyncio.timeout(60):
            spec = await exact_spec(ReadContext(), spec_name, spec_revision)
            out = await scan_spec_usage(spec, max_workflows, max_products)
        out.update(spec=identity(spec), pages={})
        for key in ("steps", "ancestor_workflows", "products", "issues"):
            rows = out[key]
            out[key] = rows[offset:offset + limit]
            out["pages"][key] = {"total_in_scope": len(rows), "offset": offset,
                                 "all_returned": offset == 0 and len(rows) <= limit,
                                 "next_offset": offset + limit if offset + limit < len(rows) else None}
        return raw_json(out)
    except (ValueError, TimeoutError) as error:
        return raw_json({"complete": False, "error": str(error) or "Spec 使用反查超时，请缩小扫描范围。"})


def mapping_match(field, row, id_field, transaction_name, transaction_id):
    if not isinstance(row, dict):
        return None
    if field == "ESigTxnMap" and row.get("AllTxns") is True:
        return "all_transactions"
    row_id = row.get(id_field)
    row_name = row.get("TxnName") or row.get("TxnToUseName")
    compare_id = type(row_id) is int and row_id > 0 and transaction_id is not None
    compare_name = isinstance(row_name, str) and bool(row_name.strip()) and bool(transaction_name)
    if compare_id and compare_name and (row_id == transaction_id) != (row_name.casefold() == transaction_name.casefold()):
        return None
    if compare_id:
        return "id" if row_id == transaction_id else False
    if compare_name:
        return "name" if row_name.casefold() == transaction_name.casefold() else False
    return None


async def resolve_configuration(ctx, entity, ref, revisioned, expand=None):
    if revisioned and ref_mode(ref) is None:
        raise ValueError(f"{entity} 引用缺少可解析的版本信息。")
    obj = await ctx.resolve(entity, ref, revisioned=revisioned)
    if expand:
        detail = await ctx.get(entity, obj["InstanceID"], {"$expand": expand})
        if (any(detail.get(k) != obj.get(k) for k in IDENTITY[:3])
                or not isinstance(detail.get(expand), list) or _has_next_link(detail)):
            raise ValueError(f"{entity} 的 {expand} 缺失或对象身份不一致。")
        obj = obj | {expand: detail[expand]}
    if _has_next_link(obj):
        raise ValueError(f"{entity} 返回的配置存在分页。")
    return obj


@mcp.tool
async def get_spec_transaction_requirements(
    spec_name: str, spec_revision: str, transaction_name: str = "", transaction_id: int | None = None,
    collection_kind: Literal["auto", "base", "user", "point"] = "auto",
    offset: int = 0, limit: int = 10, include_resources: bool = False, include_documents: bool = False,
) -> str:
    """读取明确 Spec 版本及事务的静态采集、签名、标签和起止计时配置，并解析 Operation、Setup、电子过程；可选资源成员和作业指导书。可传事务名和/或真实 CDO ID，常见事务如 MoveIn、MoveOut、CollectData 自动识别已核实的 CDO 编号。无法匹配匿名行时报告未知。分页分别应用于各类已匹配映射。动态要求及现场执行许可始终未核实。"""
    out = {"complete": False, "dynamic_requirements_verified": False, "execution_allowed": None,
           "mappings": {}, "references": {}}
    try:
        transaction_name = text_arg(transaction_name, "事务名称") if transaction_name else ""
        native_id = next((v for name, v in SERVICE_IDS.items() if name.casefold() == transaction_name.casefold()), None)
        if transaction_id is not None and (type(transaction_id) is not int or not 1 <= transaction_id <= 2**31 - 1):
            raise ValueError("事务 ID 须为正的 32 位 CDO 编号。")
        if native_id is not None and transaction_id not in (None, native_id):
            raise ValueError("事务名称与已核实的 CDO 编号冲突。")
        transaction_id = transaction_id or native_id
        if (not (transaction_name or transaction_id) or not (0 <= offset and 1 <= limit <= 20)
                or collection_kind not in {"auto", "base", "user", "point"}):
            raise ValueError("须提供事务名称或 ID；offset>=0，limit=1..20，采集类型须有效。")

        async with asyncio.timeout(60):
            ctx = ReadContext(max_requests=170)
            spec = await exact_spec(ctx, spec_name, spec_revision)
            root, paginated = await read_spec_views(ctx, spec, MAPS)
            out.update(spec=identity(spec), transaction={"name": transaction_name or None, "id": transaction_id},
                       source="Modeling/Specs", server_paginated=paginated)
            complete = not paginated
            for field, (id_field, ref_field, entity, revisioned, expand) in MAPS.items():
                rows = root.get(field)
                result = {"items": [], "complete": False}
                out["mappings"][field] = result
                if not isinstance(rows, list) or len(rows) > 500:
                    result["error"] = "映射未展开或超过 500 行。"
                    complete = False
                    continue
                matches, unknown = [], 0
                for row in rows:
                    match = mapping_match(field, row, id_field, transaction_name, transaction_id)
                    if match:
                        matches.append((row, match))
                    elif match is None:
                        unknown += 1
                result.update(total_matched=len(matches), unidentified_rows=unknown, offset=offset,
                              next_offset=offset + limit if offset + limit < len(matches) else None)
                section_complete = not out["server_paginated"] and not unknown and offset == 0 and len(matches) <= limit
                for row, match in matches[offset:offset + limit]:
                    item = {"match": match, "mapping": row, "reference": row.get(ref_field)}
                    result["items"].append(item)
                    try:
                        if field == "TxnMap":
                            ref = row.get(ref_field)
                            if ref_mode(ref) is None:
                                raise ValueError("采集定义引用缺少可解析的版本信息。")
                            kind = collection_kind
                            if kind == "auto":
                                kind = COLLECTION_KIND.get(ref.get("CDOTypeName"), "base")
                            item["definition"] = await definition(ctx, ref, kind, 0, 20)
                            section_complete &= item["definition"].get("complete") is True
                        else:
                            item["definition"] = await resolve_configuration(ctx, entity, row.get(ref_field), revisioned, expand)
                    except (ValueError, TypeError, AttributeError) as error:
                        item["error"] = str(error)
                        section_complete = False
                result["complete"] = section_complete
                complete &= section_complete
            for field, entity, versioned, expand in (
                ("Operation", "Operations", False, None), ("Setup", "Setups", True, None),
                ("ElectronicProcedure", "ElectronicProcedures", True, "EProcedureDetails"),
                ("ResourceGroup", "ResourceGroups", False, None), ("DocumentSet", "DocumentSets", False, None),
            ):
                ref = root.get(field)
                result = {"reference": ref}
                out["references"][field] = result
                try:
                    if field not in root:
                        raise ValueError("Spec 响应未核实该引用。")
                    if ref is not None:
                        result["definition"] = await resolve_configuration(ctx, entity, ref, versioned, expand)
                    result["complete"] = True
                except (ValueError, TypeError, AttributeError) as error:
                    result.update(complete=False, error=str(error))
                    complete = False
            out["flags"] = {k: root[k] for k in ("AllowOtherResource", "RequirePreProductionProcedure",
                "IsRequiresResource", "IsDisableEProcValidation", "ValidateActiveMfgOrder",
                "ValidateLotSamplingComplete", "ValidateMaterialConsumption") if k in root}
            if include_resources:
                if root.get("ResourceGroup"):
                    resources = await read_spec_resources(verified_spec=root, max_groups=10, max_resources=30)
                    out["resources"] = json.loads(resources)
                else:
                    known_empty = "ResourceGroup" in root and root["ResourceGroup"] is None
                    out["resources"] = {"complete": known_empty, "association_status": "not_associated" if known_empty else "unknown"}
                complete &= out["resources"].get("complete") is True
            if include_documents:
                documents = await read_spec_documents(spec["InstanceID"], verified_spec=root)
                out["documents"] = json.loads(documents)
                complete &= out["documents"].get("complete") is True
            out["complete"] = complete
    except (ValueError, TimeoutError) as error:
        out["error"] = str(error) or "Spec 事务配置读取超时。"
    return raw_json(out)
