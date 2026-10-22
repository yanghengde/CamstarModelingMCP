"""Persist immutable, single-use workflow import plans before any write."""
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

import pandas as pd
from tools import mcp
from tools.process_context import ReadContext, get_workflow_structure
from tools.modeling_details import raw_json
from core.http_client import request
from core.import_lock import workflow_locks
from config import CAMSTAR_BASE_URL

PLAN_DB = Path(__file__).resolve().parents[1] / "data" / "import_plans.sqlite3"


def _db():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB)
    db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, digest TEXT, payload TEXT, state TEXT, created REAL, outcome TEXT)")
    return db


def _stable(value):
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in {"InstanceID", "InstanceId", "instance_id"} and not k.startswith("@odata")}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _environment_id():
    return hashlib.sha256(CAMSTAR_BASE_URL.strip().rstrip("/").encode()).hexdigest()


def confirmation_summary(plan_id, digest):
    with _db() as db:
        row = db.execute("SELECT digest,payload,state FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row or row[0] != digest or row[2] != "ready":
        return "计划无效或不可执行，请重新预览。"
    plan = json.loads(row[1])
    lines = [plan["warning"]]
    for workflow in plan["workflows"]:
        old_steps = []
        if workflow["before"]:
            old_steps = next(iter(workflow["before"]["workflows"].values()))["steps"]
        lines.append(f"{workflow['name']}/{workflow['revision']}：{workflow['action']}；删除原步骤 {len(old_steps)} 个，新建步骤 {len(workflow['after_rows'])} 个。")
        if old_steps:
            lines.append("删除：" + "、".join(s["name"] for s in old_steps))
        for item in workflow["after_rows"]:
            lines.append(f"Excel 第 {item['excel_row']} 行 → {item['step_name']} → Spec {item['spec_name']}/{item['spec_revision']}")
    return "\n".join(lines)


async def _existing(ctx, name, revision):
    esc = lambda s: s.replace("'", "''")
    result = await ctx.get("Workflows", params={"$filter": f"Name eq '{esc(name)}' and Revision eq '{esc(revision)}'", "$top": 2})
    rows = result.get("value")
    if not isinstance(rows, list) or len(rows) > 1 or result.get("@odata.nextLink"):
        raise ValueError("Workflow 查询不完整或存在歧义")
    if not rows:
        return None, None
    identifier = rows[0]["InstanceID"]
    structure = json.loads(await get_workflow_structure(identifier, max_depth=0))
    if not structure.get("complete"):
        raise ValueError("现有路线无法完整读取，禁止覆盖")
    structure["source_steps"] = (await ctx.get("Workflows", identifier, {"$expand": "Steps"})).get("Steps")
    if not isinstance(structure["source_steps"], list):
        raise ValueError("无法读取现有步骤完整配置")
    return identifier, _stable(structure)


def _actions(name, revision, rows, before):
    actions = []
    if before:
        node = next(iter(before["workflows"].values()))
        steps = node["steps"]
        if node.get("gates") or any(s.get("step_type") != 1 or any(s.get("advanced", {}).get(k) for k in ("PathSelectors", "ReworkPaths", "ReworkPathSelectors")) for s in steps):
            raise ValueError("现有路线含高级结构，线性 Excel 导入不允许覆盖")
        if any(any(v not in (None, False, "", 0) for k, v in s.get("advanced", {}).get("SchedulingDetail", {}).items() if k not in {"InstanceID", "InstanceId", "IsFrozen"}) for s in steps):
            raise ValueError("现有步骤含调度配置，线性导入不允许覆盖")
        if steps:
            actions.append({"steps-Expanded": [{"listItemAction": "delete", "key": {"name": s["name"]}} for s in steps]})
    for row in rows:
        actions.append({"steps-Expanded": [{"listItemAction": "add", "value": {
            "@odata.type": "#modeling.SpecStepChanges", "name": row["step_name"],
            "spec": {"name": row["spec_name"], "revision": row["spec_revision"]}}}]})
    for left, right in zip(rows, rows[1:]):
        path = left["step_name"] + "_to_" + right["step_name"]
        actions.append({"steps-Expanded": [{"listItemAction": "change", "key": {"name": left["step_name"]}, "value": {
            "@odata.type": "#modeling.SpecStepChanges", "name": left["step_name"],
            "paths-Expanded": [{"listItemAction": "add", "value": {"@odata.type": "#modeling.MovePathChanges",
                "name": path, "toStep": {"name": right["step_name"], "parent": {"name": name, "revision": revision}}}}],
            "defaultPath": {"name": path}}}]})
    return actions


@mcp.tool
async def preview_workflow_import(file_path: str, mapping_json: str) -> str:
    """Read ALL mapped Excel rows (max 200) and validate exact Specs, duplicates and conflicts. Return immutable 1-hour plan, original rows, before/after routes and exact write payloads. No business writes. Mapping fields: workflow_name, workflow_revision, step_name, spec_name; optional spec_revision, sequence, workflow_description. Existing routes are completely replaced; advanced routes are rejected."""
    try:
        mapping = json.loads(mapping_json)
        fields = mapping["fields"]
        required = {"workflow_name", "workflow_revision", "step_name", "spec_name"}
        if not required <= fields.keys():
            raise ValueError("缺少必要字段映射")
        frame = pd.read_excel(file_path, sheet_name=mapping["sheet_name"], dtype=str, keep_default_na=False)
        if not 1 <= len(frame) <= 200 or any(col not in frame for col in fields.values()):
            raise ValueError("必须为 1..200 行且映射列全部存在")
        groups, errors = {}, []
        for index, source in frame.iterrows():
            row = {field: str(source[column]).strip() for field, column in fields.items()}
            row["excel_row"] = int(index) + 2
            if any(not row.get(k) for k in required):
                errors.append({"row": row["excel_row"], "error": "必要字段为空，不会跳过此行"})
                continue
            if "sequence" in fields:
                if not row["sequence"].isdigit() or int(row["sequence"]) < 1:
                    errors.append({"row": row["excel_row"], "error": "序号必须为正整数"})
                    continue
                row["sequence"] = int(row["sequence"])
            groups.setdefault((row["workflow_name"], row["workflow_revision"]), []).append(row)
        ctx, workflows = ReadContext(600), []
        for (name, revision), rows in groups.items():
            try:
                if len({r["step_name"] for r in rows}) != len(rows):
                    raise ValueError("步骤名称重复")
                if "sequence" in fields:
                    if len({r["sequence"] for r in rows}) != len(rows):
                        raise ValueError("步骤序号重复")
                    rows.sort(key=lambda r: r["sequence"])
                if len({r.get("workflow_description", "") for r in rows}) > 1:
                    raise ValueError("同一路线描述不一致")
                for row in rows:
                    ref = {"Name": row["spec_name"], "Revision": row.get("spec_revision"), "UseROR": not bool(row.get("spec_revision"))}
                    spec = await ctx.resolve("Specs", ref)
                    row["spec_revision"] = spec["Revision"]
                    row["spec_id"] = spec["InstanceID"]
                identifier, before = await _existing(ctx, name, revision)
                workflows.append({"name": name, "revision": revision, "id": identifier,
                    "action": "replace_route" if identifier else "create", "before": before, "after_rows": rows,
                    "description": rows[0].get("workflow_description", ""), "payloads": _actions(name, revision, rows, before)})
            except (ValueError, KeyError) as exc:
                errors.append({"workflow": name, "revision": revision, "error": str(exc)})
        if errors:
            return raw_json({"executable": False, "errors": errors, "validated_workflows": workflows})
        plan = {"environment_id": _environment_id(), "workflows": workflows, "row_count": len(frame), "warning": "重建删除原步骤及其配置，再按 after_rows 创建线性路线；多次写入不具备跨请求事务性。现有描述不修改。"}
        plan_id, digest = uuid.uuid4().hex, _digest(plan)
        with _db() as db:
            db.execute("INSERT INTO plans VALUES (?,?,?,?,?,NULL)", (plan_id, digest, raw_json(plan), "ready", time.time()))
        return raw_json({"executable": True, "plan_id": plan_id, "plan_digest": digest, "expires_in_seconds": 3600, "plan": plan})
    except Exception as exc:
        return raw_json({"executable": False, "error": str(exc)})


@mcp.tool
async def execute_workflow_import_plan(plan_id: str, plan_digest: str) -> str:
    """Execute the exact previously previewed workflow import plan after user confirmation. Single-use, expires in 1 hour. Recheck all targets/Specs before first write; reject changed state. No implicit retry/rollback; report each completed action and stop on first failure. Not a cross-request transaction."""
    with _db() as db:
        row = db.execute("SELECT digest,payload FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row or row[0] != plan_digest:
        return raw_json({"error": "计划不存在或摘要不符"})
    try:
        with workflow_locks(PLAN_DB.parent / "import_locks", json.loads(row[1])["workflows"]):
            return await _execute_plan(plan_id, plan_digest)
    except RuntimeError as exc:
        return raw_json({"error": str(exc)})


async def _execute_plan(plan_id, plan_digest):
    outcomes = []
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT digest,payload,state,created FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not row or row[0] != plan_digest or row[2] != "ready" or time.time() - row[3] > 3600:
            return raw_json({"error": "计划不存在、摘要不符、已使用或已过期；请重新预览"})
        plan = json.loads(row[1])
        if _digest(plan) != plan_digest:
            return raw_json({"error": "计划内容校验失败"})
        if plan.get("environment_id") != _environment_id():
            return raw_json({"error": "计划属于不同服务环境或缺少环境绑定；请在当前环境重新预览。"})
        db.execute("UPDATE plans SET state='running' WHERE id=?", (plan_id,))
    try:
        ctx = ReadContext(600)
        for workflow in plan["workflows"]:
            identifier, before = await _existing(ctx, workflow["name"], workflow["revision"])
            if identifier != workflow["id"] or before != workflow["before"]:
                raise ValueError("路线在预览后发生变化，请重新预览")
            for step in workflow["after_rows"]:
                await ctx.resolve("Specs", {"Name": step["spec_name"], "Revision": step["spec_revision"], "InstanceId": step["spec_id"]})
        for workflow in plan["workflows"]:
            identifier = workflow["id"]
            latest_id, latest = await _existing(ReadContext(), workflow["name"], workflow["revision"])
            if latest_id != identifier or latest != workflow["before"]:
                raise ValueError("写入前再次核对发现路线变化，已停止；之前路线的结果请查看日志。")
            if not identifier:
                await _write("POST", "/api/Workflows", {"name": workflow["name"], "revision": workflow["revision"], "description": workflow["description"]}, outcomes, plan_id)
                found = await ctx.get("Workflows", params={"$filter": "Name eq '" + workflow["name"].replace("'", "''") + "' and Revision eq '" + workflow["revision"].replace("'", "''") + "'", "$top": 2})
                rows = found.get("value", [])
                if len(rows) != 1:
                    raise ValueError("创建后无法唯一定位对象，停止执行")
                identifier = rows[0]["InstanceID"]
            for payload in workflow["payloads"]:
                await _write("PUT", f"/api/Workflows/{identifier}", payload, outcomes, plan_id)
            from tools.workflow_steps import _read_steps
            actual = await _read_steps(identifier)
            expected = [r["step_name"] for r in workflow["after_rows"]]
            if not actual.get("standard_route_complete") or actual.get("standard_route") != expected:
                raise ValueError("写入后路线顺序验证未通过，停止后续 Workflow")
            actual_specs = {s["name"]: s.get("spec") for s in actual["steps"]}
            if len(actual_specs) != len(expected) or any((actual_specs.get(r["step_name"]) or {}).get("Name") != r["spec_name"] or (actual_specs.get(r["step_name"]) or {}).get("Revision") != r["spec_revision"] for r in workflow["after_rows"]):
                raise ValueError("写入后 Spec 引用验证未通过")
        result = {"status": "verified", "actions": outcomes}
    except Exception as exc:
        result = {"status": "stopped", "error": str(exc), "actions": outcomes, "note": "已完成的写入不会自动回滚；不要自动重试，请核对现场状态。"}
    with _db() as db:
        db.execute("UPDATE plans SET state=?, outcome=? WHERE id=?", (result["status"], raw_json(result), plan_id))
    return raw_json(result)


@mcp.tool
async def reconcile_workflow_import_plan(plan_id: str) -> str:
    """Read-only comparison of a saved import plan against current Workflow routes. Report before-match, planned-route-match, difference or unknown. Does not retry writes, reset approval, release a running task or prove which request caused a change."""
    with _db() as db:
        row = db.execute("SELECT payload,state FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        return raw_json({"error": "计划不存在"})
    plan = json.loads(row[0])
    if plan.get("environment_id") != _environment_id():
        return raw_json({"error": "计划环境与当前服务不一致或未绑定，不能跨环境核对。"})
    result = {"plan_id": plan_id, "plan_status": row[1], "workflows": [],
              "note": "只读现状核对，不代表执行归因或事务快照；不会重置计划、自动重试或证明所有配置一致。"}
    ctx = ReadContext(600)
    from tools.workflow_steps import _read_steps
    for workflow in plan["workflows"]:
        entry = {"name": workflow["name"], "revision": workflow["revision"]}
        result["workflows"].append(entry)
        try:
            escape = lambda s: s.replace("'", "''")
            found = await ctx.get("Workflows", params={"$filter": f"Name eq '{escape(workflow['name'])}' and Revision eq '{escape(workflow['revision'])}'", "$top": 2})
            rows = found.get("value")
            if not isinstance(rows, list) or len(rows) > 1 or found.get("@odata.nextLink"):
                raise ValueError("对象查询不完整或有歧义")
            if not rows:
                entry["comparison"] = "matches_before_absent" if not workflow["id"] else "different_missing"
                continue
            identifier = rows[0]["InstanceID"]
            actual = await _read_steps(identifier)
            if actual.get("error"):
                raise ValueError(actual["error"])
            entry["actual_steps"] = actual["steps"]
            entry["actual_route"] = actual["standard_route"]
            planned = workflow["after_rows"]
            refs = {s["name"]: s.get("spec") or {} for s in actual["steps"]}
            matches = (actual.get("steps_complete") and actual.get("standard_route_complete")
                       and actual["standard_route"] == [s["step_name"] for s in planned]
                       and len(refs) == len(planned)
                       and all(refs.get(s["step_name"], {}).get("Name") == s["spec_name"] and refs.get(s["step_name"], {}).get("Revision") == s["spec_revision"] for s in planned))
            if matches:
                entry["comparison"] = "matches_planned_route_and_specs"
            else:
                try:
                    before_id, before = await _existing(ctx, workflow["name"], workflow["revision"])
                    entry["comparison"] = "matches_before" if before_id == workflow["id"] and before == workflow["before"] else "different"
                except ValueError:
                    entry["comparison"] = "unknown_or_partial"
            entry["warnings"] = actual.get("warnings", [])
        except (ValueError, KeyError, TypeError) as exc:
            entry.update({"comparison": "unknown", "error": str(exc)})
    return raw_json(result)


def _save_progress(plan_id, outcomes):
    if plan_id:
        with _db() as db:
            db.execute("UPDATE plans SET outcome=? WHERE id=?", (raw_json({"status": "running", "actions": outcomes}), plan_id))


async def _write(method, path, payload, outcomes, plan_id=None):
    outcome = {"method": method, "path": path, "success": None, "note": "请求已准备发送，最终结果尚未确认"}
    outcomes.append(outcome)
    _save_progress(plan_id, outcomes)
    raw = await request(method, path, body=payload, response_formatter=raw_json)
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    failed = (data is None and not raw.startswith("✅")) or raw.startswith(("❌", "Error")) or isinstance(data, dict) and bool(data.get("error") or data.get("ErrorCode"))
    uncertain = any(text in raw for text in ("Request timed out", "Request failed", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"))
    outcome.update({"success": None if uncertain else not failed, "response": raw,
                    "note": "通信失败，服务端可能已执行；需核对现场，禁止自动重试" if uncertain else "请求返回；不代表整个计划已完成"})
    _save_progress(plan_id, outcomes)
    if failed:
        raise ValueError("写请求失败，已停止后续动作")


@mcp.tool
async def get_workflow_import_plan(plan_id: str) -> str:
    """Read a persisted import plan and execution journal by exact plan ID. Running means claimed, not proof of a live worker. Never retry a claimed plan automatically; inspect uncertain actions against Camstar."""
    with _db() as db:
        row = db.execute("SELECT digest,payload,state,created,outcome FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        return raw_json({"error": "计划不存在"})
    return raw_json({"plan_id": plan_id, "plan_digest": row[0], "plan": json.loads(row[1]),
        "status": row[2], "expired": time.time() - row[3] > 3600,
        "outcome": json.loads(row[4]) if row[4] else None,
        "note": "running 仅表示计划已领取；进程中断后不可自动重放，需核对请求结果。"})
