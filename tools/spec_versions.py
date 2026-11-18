"""Spec configuration comparison and confirmed, single-attempt revision changes."""

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from config import CAMSTAR_BASE_URL
from core.http_client import get_client, get_headers
from core.modeling_payload import _schema, build_update_payload
from core.read_catalog import digest, environment
from core.transfer_attempt_lock import AttemptBusy, attempt_lock
from tools import mcp
from tools.modeling_details import _has_next_link, raw_json
from tools.process_context import ReadContext
from tools.spec_context import (
    exact_spec, find_spec, identity, read_spec_views, ref_mode, same_identity, scan_spec_usage, text_arg,
)

PLAN_DB = Path(__file__).resolve().parents[1] / "data/spec_revision_plans.sqlite3"
EXPIRES_SECONDS = 3600
NAV_LISTS = (
    "TxnMap", "ESigTxnMap", "LabelTxnMap", "StartTimerTxnMap", "EndTimerTxnMap",
    "BPSpecBizRuleTxnMap", "AssociateActionTxnMap", "ContainerAutoHoldReqTxnMap", "AttachDocumentDetails",
)
EXPAND = (*NAV_LISTS, "SchedulingDetail", "WIPMsgDefMgr")
ROOT_SYSTEM_FIELDS = {
    "name", "revision", "base", "isrevofrcd", "canchangerevofrcd", "associatedpackages",
    "instancelocked", "isfrozen", "useror", "changehistory", "replacedetails",
    "replacedetailssubentitylistnames", "skipsubentityliststosequence", "txnmapitem",
}
SYSTEM_FIELDS = {"instanceid", "displayname", "parent", "objecttochange", "listitemtochange"}
CALCULATED_FIELDS = {"txnname", "txntousename"}


def configured(value):
    if isinstance(value, dict):
        return any(configured(v) for k, v in value.items()
                   if k.casefold() not in SYSTEM_FIELDS and not k.startswith("@")
                   and (k.casefold() != "isfrozen" or v is True))
    if isinstance(value, list):
        return bool(value)
    return value is not None and value != ""


def project(value, schema, schemas, path="", *, root=False):
    """Project documented write fields, recording configuration that cannot be copied."""
    ref = schema.get("$ref", "").rsplit("/", 1)[-1]
    schema = schemas[ref] if ref else schema
    if schema.get("type") == "array" and isinstance(value, list):
        rows, unsupported = [], {}
        for index, row in enumerate(value):
            item, unknown = project(row, schema["items"], schemas, f"{path}/{index}")
            rows.append(item)
            unsupported.update(unknown)
        return rows, unsupported
    if schema.get("type") != "object" or not isinstance(value, dict):
        return value, {}
    if ref in {"NamedObjectRef", "RevisionedObjectRef"}:
        name = value.get("Name")
        if not isinstance(name, str) or not name or (ref == "RevisionedObjectRef" and ref_mode(value) is None):
            return {}, {path: value}
        if ref == "NamedObjectRef":
            return {"name": name}, {}
        if value.get("UseROR") is True:
            return {"name": name, "useROR": True}, {}
        if isinstance(value.get("Revision"), str) and value["Revision"]:
            return {"name": name, "revision": value["Revision"], "useROR": False}, {}
        return {}, {path: value}

    fields = {key.casefold(): key for key in schema.get("properties", {})}
    wire_values = {key.casefold(): item for key, item in value.items()}
    projected, unsupported = {}, {}
    for key, item in value.items():
        folded = key.casefold()
        field_path = f"{path}/{key}"
        if item is None or key.startswith("@") or folded in SYSTEM_FIELDS:
            continue
        if (root and folded in ROOT_SYSTEM_FIELDS) or folded in CALCULATED_FIELDS:
            continue
        if folded.endswith("-expanded"):
            unwrapped = wire_values.get(folded[:-9])
            if not item or (isinstance(unwrapped, list) and len(item) == len(unwrapped)):
                continue
            unsupported[field_path] = item
            continue
        if folded == "isfrozen":
            if item is True:
                unsupported[field_path] = item
            continue
        field = fields.get(folded)
        # WIP/attachments include independent managed objects, not simple clone payloads.
        if not field or schema["properties"][field].get("readOnly") or folded in {"wipmsgdefmgr", "isattachimage"}:
            if configured(item) and not (folded == "isattachimage" and item is False):
                unsupported[field_path] = item
            continue
        content, unknown = project(item, schema["properties"][field], schemas, field_path)
        unsupported.update(unknown)
        if content not in ({}, []):
            projected[field] = content
    return projected, unsupported


async def read_snapshot(ctx, summary):
    raw, paginated = await read_spec_views(ctx, summary, EXPAND)
    missing = [field for field in NAV_LISTS if not isinstance(raw.get(field), list) or len(raw[field]) > 500]
    missing.extend(field for field in ("SchedulingDetail", "WIPMsgDefMgr", "BillOfProcessAssignments")
                   if field not in raw)
    schema, schemas = _schema("Specs")
    config, unsupported = project(raw, schema, schemas, root=True)
    return {
        "spec": identity(raw), "configuration": config, "source": "Modeling/Specs",
        "configuration_without_copy_contract": unsupported,
        "metadata": {k: raw.get(k) for k in ("IsFrozen", "InstanceLocked", "AssociatedPackages", "CanChangeRevOfRcd")},
        "complete": not missing and not paginated, "missing_fields": missing,
    }


def comparison_view(snapshot):
    return {k: snapshot[k] for k in ("configuration", "configuration_without_copy_contract")}


async def compare_specs(left_name, left_revision, right_name, right_revision):
    ctx = ReadContext()

    async def snapshot(name, revision):
        return await read_snapshot(ctx, await exact_spec(ctx, name, revision))

    async with asyncio.timeout(45):
        return await asyncio.gather(snapshot(left_name, left_revision), snapshot(right_name, right_revision))


def check_editable(snapshot, *, promoting=False):
    meta = snapshot["metadata"]
    if (meta["IsFrozen"] is True or meta["InstanceLocked"] is True
            or meta["AssociatedPackages"] not in (None, 0)):
        raise ValueError("Spec 已冻结、锁定或关联变更包，请在 MES 中核实。")
    if promoting and meta["CanChangeRevOfRcd"] is False:
        raise ValueError("目标 Spec 明确禁止切换当前记录版本。")


async def build_plan(action, spec_name, source_revision, target_revision, max_workflows, max_products):
    if action not in {"create", "set_current"}:
        raise ValueError("升版操作须为 create 或 set_current。")
    name = text_arg(spec_name, "Spec 名称")
    source_revision = text_arg(source_revision, "来源版本")
    target_revision = text_arg(target_revision, "目标版本")
    if source_revision.casefold() == target_revision.casefold():
        raise ValueError("来源版本与目标版本须不同。")
    if not (1 <= max_workflows <= 1000 and 1 <= max_products <= 3000):
        raise ValueError("max_workflows=1..1000，max_products=1..3000。")

    ctx = ReadContext()
    current = await find_spec(ctx, name, current=True)
    if current is None or current["Revision"] != source_revision:
        raise ValueError("来源版本须为唯一的当前记录版本。")
    source = await read_snapshot(ctx, current)
    check_editable(source)
    if not source["complete"] or source["spec"]["IsRevOfRcd"] is not True:
        raise ValueError("来源 Spec 配置未完整读取。")
    target = await find_spec(ctx, name, target_revision)
    if action == "create":
        if target is not None:
            raise ValueError("目标 Spec 版本已存在。")
        if source["configuration_without_copy_contract"]:
            paths = ", ".join(sorted(source["configuration_without_copy_contract"])[:20])
            raise ValueError("来源包含未核实复制契约的配置：" + paths)
        if "operation" not in source["configuration"]:
            raise ValueError("来源 Spec 未返回明确 Operation，无法按创建契约复制。")
        body = build_update_payload("Specs", {"name": name, "revision": target_revision,
                                              "isRevOfRcd": False, **source["configuration"]})
        target_snapshot = None
        impact = None
        # The Modeling REST revision contract uses collection PATCH with the
        # existing ROR name and the new revision. Send the supported copy explicitly.
        method, path = "PATCH", "/api/Specs"
    else:
        if target is None or target.get("IsRevOfRcd") is True:
            raise ValueError("须指定已存在的非当前 Spec 版本。")
        target_snapshot = await read_snapshot(ctx, target)
        check_editable(target_snapshot, promoting=True)
        if not target_snapshot["complete"] or target_snapshot["spec"]["IsRevOfRcd"] is not False:
            raise ValueError("目标 Spec 配置或非 ROR 状态未完整核实。")
        usage = await scan_spec_usage(current, max_workflows, max_products)
        impact = {
            "complete": usage["complete"], "coverage": usage["coverage"],
            "fixed_source_steps": sum(row["reference_mode"] == "fixed" for row in usage["steps"]),
            "ror_following_steps": sum(row["reference_mode"] == "use_ror" for row in usage["steps"]),
            "ror_following_products": sum("use_ror" in row["spec_paths"] for row in usage["products"]),
            "ror_step_examples": [row for row in usage["steps"] if row["reference_mode"] == "use_ror"][:5],
            "ror_product_examples": [row["product"] for row in usage["products"] if "use_ror" in row["spec_paths"]][:5],
            "usage_digest": digest(usage),
        }
        body = {"isRevOfRcd": True}
        method, path = "PUT", "/api/Specs/" + quote(target["InstanceID"], safe="")
    return {
        "environment": environment(), "action": action, "name": name, "source_revision": source_revision,
        "target_revision": target_revision, "max_workflows": max_workflows, "max_products": max_products,
        "source": source, "target": target_snapshot, "impact": impact,
        "request": {"method": method, "path": path, "body": body},
    }


@contextmanager
def database():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, environment TEXT NOT NULL, spec_name TEXT NOT NULL,
                snapshot TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                attempt_id TEXT, response TEXT, verification TEXT, human_review TEXT
            )""")
            yield db
    finally:
        db.close()


def check_pending(db, snapshot):
    if db.execute("""SELECT 1 FROM plans
                      WHERE environment=? AND spec_name=? AND state IN ('in_flight','unknown') LIMIT 1""",
                  (snapshot["environment"], snapshot["name"].casefold())).fetchone():
        raise ValueError("同名 Spec 有结果待核实的提交，须先核对原计划。")


def load_plan(db, plan_id, plan_digest):
    row = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None or row["digest"] != plan_digest:
        raise ValueError("升版计划不存在或摘要不符。")
    snapshot = json.loads(row["snapshot"])
    if digest(snapshot) != plan_digest or snapshot["environment"] != environment():
        raise ValueError("计划内容或 MES 环境已变化。")
    return row, snapshot


def check_ready(row):
    if row["state"] != "ready" or time.time() - row["created"] > EXPIRES_SECONDS:
        raise ValueError("升版计划已使用或过期。")


def confirmation_summary(plan_id, plan_digest):
    with database() as db:
        row, snapshot = load_plan(db, plan_id, plan_digest)
        check_ready(row)
    if snapshot["action"] == "create":
        note = "复制来源配置，创建同名的新版本，预期原版本仍为当前记录版本。"
    else:
        note = "把目标设为当前记录版本，跟随 ROR 的引用将解析到目标，固定引用保留。"
    return f"{note}\n{raw_json(snapshot)}"


@mcp.tool
async def prepare_spec_revision(
    action: Literal["create", "set_current"], spec_name: str, source_revision: str, target_revision: str,
    max_workflows: int = 250, max_products: int = 500,
) -> str:
    """只读准备 Spec 升版计划。create 从明确的当前版本复制已有配置，目标须不存在，新版保持非 ROR。set_current 对已存在的明确目标做使用影响扫描，准备切换 ROR；固定版本引用保留。两步分别确认。输出原配置、精确请求、使用范围及摘要。"""
    try:
        async with asyncio.timeout(65):
            snapshot = await build_plan(action, spec_name, source_revision, target_revision, max_workflows, max_products)
        plan_id, plan_digest = uuid4().hex, digest(snapshot)
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            check_pending(db, snapshot)
            db.execute("""INSERT INTO plans (id,digest,environment,spec_name,snapshot,state,created)
                          VALUES (?,?,?,?,?,'ready',?)""",
                       (plan_id, plan_digest, snapshot["environment"], snapshot["name"].casefold(),
                        raw_json(snapshot), time.time()))
        return raw_json({"plan_id": plan_id, "plan_digest": plan_digest, "state": "ready",
                         "confirmation_required": True, "expires_in_seconds": EXPIRES_SECONDS,
                         **{k: v for k, v in snapshot.items() if k != "environment"}})
    except (ValueError, TimeoutError) as error:
        return raw_json({"submitted": False, "error": str(error) or "Spec 升版预览超时。"})


def lock_scope(snapshot):
    return snapshot["environment"] + ":" + snapshot["name"].casefold()


@mcp.tool
async def get_spec_revision_plan(plan_id: str, plan_digest: str) -> str:
    """读取升版计划、原提交回执、回读核实及人工审计。访问本地计划库。"""
    try:
        with database() as db:
            row, snapshot = load_plan(db, plan_id, plan_digest)
        return raw_json({
            "state": row["state"], "attempt_id": row["attempt_id"], "plan": snapshot,
            "expired": time.time() - row["created"] > EXPIRES_SECONDS,
            **{k: json.loads(row[k]) if row[k] else None for k in ("response", "verification", "human_review")},
        })
    except ValueError as error:
        return raw_json({"error": str(error)})


@mcp.tool
async def execute_spec_revision(plan_id: str, plan_digest: str) -> str:
    """经应用确认后执行同一不可变升版计划。重读原版本、目标及影响范围，保存尝试，再提交一次。始终使用 reconcile_spec_revision 核实结果。"""
    try:
        with database() as db:
            row, snapshot = load_plan(db, plan_id, plan_digest)
            check_ready(row)
        with attempt_lock(PLAN_DB, lock_scope(snapshot)):
            args = {key: snapshot[key] for key in ("action", "source_revision", "target_revision",
                                                  "max_workflows", "max_products")}
            async with asyncio.timeout(65):
                refreshed = await build_plan(spec_name=snapshot["name"], **args)
            if digest(refreshed) != plan_digest:
                raise ValueError("前值、配置或使用范围已变化，请重新准备计划。")
            attempt = uuid4().hex
            with database() as db:
                db.execute("BEGIN IMMEDIATE")
                row, _ = load_plan(db, plan_id, plan_digest)
                check_ready(row)
                check_pending(db, snapshot)
                db.execute("UPDATE plans SET state='in_flight', attempt_id=? WHERE id=?", (attempt, plan_id))
            request = snapshot["request"]
            receipt = {"attempt_id": attempt, "state": "unknown", "retry_allowed": False}
            try:
                async with asyncio.timeout(45):
                    response = await get_client().request(
                        request["method"], CAMSTAR_BASE_URL.rstrip("/") + request["path"],
                        headers=get_headers(), json=request["body"], follow_redirects=False,
                    )
                receipt.update(http_status=response.status_code, response_text=response.text[:8000])
                try:
                    receipt["response"] = response.json()
                except ValueError:
                    pass
            except Exception as error:
                receipt["error"] = str(error) or type(error).__name__
            finally:
                with database() as db:
                    db.execute("UPDATE plans SET state='unknown', response=? WHERE id=? AND state='in_flight'",
                               (raw_json(receipt), plan_id))
            return raw_json(receipt)
    except (ValueError, AttemptBusy, TimeoutError) as error:
        return raw_json({"submitted": False, "error": str(error) or "Spec 升版执行前复核超时。"})


def acknowledges(receipt, snapshot, target):
    if not 200 <= receipt.get("http_status", 0) < 300:
        return False
    reply = receipt.get("response")
    if not isinstance(reply, dict) or reply.get("error") or reply.get("ErrorCode"):
        return False
    expected = target["spec"]
    if same_identity(reply, expected):
        return True
    value = reply.get("value")
    if not isinstance(value, str):
        return False
    positive = r"\b(created|added|saved|success(?:ful(?:ly)?)?)\b" if snapshot["action"] == "create" else r"\b(updated|saved|success(?:ful(?:ly)?)?)\b"
    name = r"(?<![\w-])" + re.escape(snapshot["name"]) + r"(?![\w-])"
    return bool(re.search(name, value, re.I) and re.search(positive, value, re.I)
                and not re.search(r"\b(failed|failure|error|not|unsuccessful)\b", value, re.I))


async def verify(snapshot, receipt):
    ctx = ReadContext()
    name = snapshot["name"]
    source = await read_snapshot(ctx, await exact_spec(ctx, name, snapshot["source_revision"]))
    target = await read_snapshot(ctx, await exact_spec(ctx, name, snapshot["target_revision"]))
    ror = await find_spec(ctx, name, current=True)
    create = snapshot["action"] == "create"
    expected_target = snapshot["source"] if create else snapshot["target"]
    source_preserved = (source["complete"] and same_identity(source["spec"], snapshot["source"]["spec"])
                        and comparison_view(source) == comparison_view(snapshot["source"]))
    target_matches = (target["complete"] and comparison_view(target) == comparison_view(expected_target)
                      and (create or same_identity(target["spec"], expected_target["spec"]))
                      and target["spec"]["InstanceID"] != source["spec"]["InstanceID"])
    expected_ror = source["spec"] if create else target["spec"]
    ror_matches = (same_identity(ror, expected_ror) and source["spec"]["IsRevOfRcd"] is create
                   and target["spec"]["IsRevOfRcd"] is (not create))
    ack = acknowledges(receipt, snapshot, target)
    return {"source": source, "target": target, "current_spec": identity(ror) if ror else None,
            "source_preserved": source_preserved, "target_matches": target_matches, "ror_matches": ror_matches,
            "response_acknowledged": ack, "verified_success": bool(source_preserved and target_matches and ror_matches and ack)}


@mcp.tool
async def reconcile_spec_revision(plan_id: str, plan_digest: str) -> str:
    """回读源版本、目标配置和唯一 ROR。只有原请求有明确成功响应且回读一致才核实成功；结果未知时保留原尝试，永不自动重发。"""
    try:
        with database() as db:
            _, snapshot = load_plan(db, plan_id, plan_digest)
        with attempt_lock(PLAN_DB, lock_scope(snapshot)):
            with database() as db:
                row, _ = load_plan(db, plan_id, plan_digest)
            if row["state"] == "verified_applied":
                return raw_json(json.loads(row["verification"]) | {"state": row["state"]})
            if row["state"] not in ("in_flight", "unknown") or not row["attempt_id"]:
                raise ValueError("该计划没有待核实的提交。")
            receipt = json.loads(row["response"] or "{}")
            try:
                async with asyncio.timeout(45):
                    result = await verify(snapshot, receipt)
            except (ValueError, TimeoutError) as error:
                result = {"verified_success": False, "readback_error": str(error) or "Spec 升版核实超时。"}
            state = "verified_applied" if result["verified_success"] else "unknown"
            with database() as db:
                db.execute("UPDATE plans SET state=?, verification=? WHERE id=?", (state, raw_json(result), plan_id))
            return raw_json(result | {"state": state, "attempt_id": row["attempt_id"], "receipt": receipt})
    except (ValueError, AttemptBusy) as error:
        return raw_json({"verified_success": False, "error": str(error)})


@mcp.tool
async def review_spec_revision(plan_id: str, plan_digest: str, conclusion: Literal["applied", "not_applied"],
                               evidence_reference: str, review_note: str) -> str:
    """根据已明确的最终 MES 审计证据结案未知结果。记录人工结论及原尝试，供后续新计划使用。只写本地记录，原计划不可重放。"""
    try:
        evidence_reference = text_arg(evidence_reference, "审计证据编号")
        review_note = text_arg(review_note, "人工审计说明")
        if conclusion not in {"applied", "not_applied"} or len(review_note) < 10:
            raise ValueError("须提供明确的最终结论及至少 10 字的说明。")
        with database() as db:
            _, snapshot = load_plan(db, plan_id, plan_digest)
        with attempt_lock(PLAN_DB, lock_scope(snapshot)), database() as db:
            db.execute("BEGIN IMMEDIATE")
            row, _ = load_plan(db, plan_id, plan_digest)
            if row["state"] != "unknown" or not row["attempt_id"] or not row["verification"]:
                raise ValueError("须先回读核对原尝试，且结果仍为未知，才能人工结案。")
            state = "human_" + conclusion
            review = {"evidence_reference": evidence_reference, "review_note": review_note, "conclusion": conclusion,
                      "attempt_id": row["attempt_id"], "reviewed_at": time.time()}
            db.execute("UPDATE plans SET state=?, human_review=? WHERE id=?", (state, raw_json(review), plan_id))
        return raw_json({"state": state, "verified_success": False, "human_review": review})
    except (ValueError, AttemptBusy) as error:
        return raw_json({"error": str(error)})
