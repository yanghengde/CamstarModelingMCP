"""Agent tool-call safety policy.

This module is intentionally independent from the LLM and LangGraph runtime so
the same rules can be unit-tested and reused by other entry points.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable

from config import (
    SAFE_CREATE_THRESHOLD,
    SAFE_DELETE_THRESHOLD,
    SAFE_UPDATE_THRESHOLD,
)


from core.tool_catalog import TOOL_POLICIES


@dataclass(frozen=True)
class MutationCounts:
    creates: int = 0
    updates: int = 0
    deletes: int = 0

    def __add__(self, other: "MutationCounts") -> "MutationCounts":
        return MutationCounts(
            creates=self.creates + other.creates,
            updates=self.updates + other.updates,
            deletes=self.deletes + other.deletes,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "creates": self.creates,
            "updates": self.updates,
            "deletes": self.deletes,
        }


@dataclass(frozen=True)
class SafetyDecision:
    requires_approval: bool
    categories: tuple[str, ...]
    totals: MutationCounts


def classify_tool(name: str) -> str | None:
    """Return the mutation category for a tool, or ``None`` if read-only."""
    policy = TOOL_POLICIES.get(name)
    if policy is None:
        return "unknown"
    return None if policy.effect == "read" else policy.effect


def confirmation_policy(tool_call):
    """Resolve risk from the saved immutable plan, never caller-supplied flags."""
    function = tool_call.get('function') or {}
    name = function.get('name') or ''
    policy = TOOL_POLICIES.get(name)
    if policy is None:
        return 'unknown', True
    if name == 'execute_quality_transaction':
        try:
            from tools import quality_plans as plans
            from core.approval_policy import routine_quality_creation
            args = json.loads(function.get('arguments') or '{}')
            if not isinstance(args, dict) or set(args) != {'plan_id', 'plan_digest'}:
                return policy.effect, True
            if not plans.PLAN_DB.is_file():
                return policy.effect, True
            with plans.db_connect() as db:
                row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',
                                 (args['plan_id'],)).fetchone()
            snapshot = plans.checked(row, args['plan_digest'])
            if row[2] == 'ready' and 0 <= time.time() - row[3] <= 3600 and routine_quality_creation(snapshot):
                return 'create', False
        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
            pass
        return policy.effect, True
    return (None if policy.effect == 'read' else policy.effect), policy.always_confirm


def _argument_item_count(arguments: str) -> int:
    """Estimate how many records a single batch-shaped tool call affects."""
    try:
        payload = json.loads(arguments or "{}")
    except (TypeError, json.JSONDecodeError):
        return 1

    if not isinstance(payload, dict):
        return 1

    for key in ("container_names", "items", "records", "entities"):
        value = payload.get(key)
        if isinstance(value, list):
            return max(1, len(value))

    count = payload.get("count")
    if isinstance(count, int) and count > 0:
        return count
    return 1


def count_tool_calls(tool_calls: Iterable[dict]) -> MutationCounts:
    creates = updates = deletes = 0
    for tool_call in tool_calls:
        function = tool_call.get("function") or {}
        category, _ = confirmation_policy(tool_call)
        amount = _argument_item_count(str(function.get("arguments") or "{}"))
        if category == "create":
            creates += amount
        elif category == "update":
            updates += amount
        elif category == "delete":
            deletes += amount
    return MutationCounts(creates, updates, deletes)


def evaluate_mutations(
    completed: MutationCounts,
    pending_tool_calls: Iterable[dict],
    intended: MutationCounts | None = None,
    require_review: bool = False,
    auto_confirm: bool = False,
) -> SafetyDecision:
    pending_tool_calls = list(pending_tool_calls)
    pending = count_tool_calls(pending_tool_calls)
    totals = completed + pending
    if intended is not None:
        totals = MutationCounts(
            creates=max(totals.creates, intended.creates),
            updates=max(totals.updates, intended.updates),
            deletes=max(totals.deletes, intended.deletes),
        )
    categories: list[str] = []
    # Historical updates do not re-confirm each subsequent ordinary write.
    # Keep the cumulative creation cap so splitting a large creation into
    # single calls cannot silently bypass its existing size limit.
    scope = pending if intended is None else MutationCounts(
        creates=max(pending.creates, intended.creates),
        updates=max(pending.updates, intended.updates),
        deletes=max(pending.deletes, intended.deletes))
    if pending.creates and totals.creates > SAFE_CREATE_THRESHOLD:
        categories.append("创建")
    if pending.updates and scope.updates > SAFE_UPDATE_THRESHOLD:
        categories.append("修改")
    if pending.deletes and scope.deletes > SAFE_DELETE_THRESHOLD:
        categories.append("删除")
    if any(confirmation_policy(call)[1] for call in pending_tool_calls):
        categories.append("需核对业务影响的操作")
    if require_review and any(confirmation_policy(call)[0] is not None for call in pending_tool_calls):
        categories.append('用户要求执行前确认')
    if auto_confirm and not require_review and all(
        (call.get('function') or {}).get('name') in TOOL_POLICIES for call in pending_tool_calls
    ):
        categories = []
    return SafetyDecision(bool(categories), tuple(categories), totals)


def requests_execution_review(message: str) -> bool:
    """An explicit request to review before writing overrides routine exemptions."""
    return bool(re.search(
        r'(?:先|提交前|执行前|创建前).{0,24}(?:确认|审核|审阅)|'
        r'等我确认|经我确认|确认后再|确认后才能|确认后执行|确认后提交', message or ''))


def infer_intended_mutations(
    message: str,
    pending_tool_calls: Iterable[dict],
) -> MutationCounts:
    """Conservatively infer an explicitly stated Container creation count.

    This protects sequential tool planners that emit one Start call at a time:
    an explicit request for 50 independent Containers must be approved before
    the first write, not after call 20.
    """
    calls = list(pending_tool_calls)
    if not any(
        (call.get("function") or {}).get("name") == "container_start"
        for call in calls
    ):
        return MutationCounts()

    normalized = (message or "").casefold()
    patterns = (
        r"(?:生成|创建|启动|start)?\s*(\d{1,6})\s*(?:个|条)?\s*(?:独立的?)?\s*(?:container|容器|序列号)",
        r"(?:container|容器|序列号).*?(\d{1,6})\s*(?:个|条)",
    )
    counts = [
        int(match.group(1))
        for pattern in patterns
        for match in re.finditer(pattern, normalized, flags=re.IGNORECASE)
    ]

    range_match = re.search(
        r"(?:sn)?(\d{3,})\s*(?:~|～|-|至|到)\s*(?:sn)?(\d{3,})",
        normalized,
        flags=re.IGNORECASE,
    )
    if range_match:
        start, end = int(range_match.group(1)), int(range_match.group(2))
        if end >= start:
            counts.append(end - start + 1)

    return MutationCounts(creates=max(counts, default=0))


def is_explicit_confirmation(text: str) -> bool:
    """Accept an explicit approval only; substrings such as “不是” never pass."""
    normalized = re.sub(r"[\s，。！？、,.!?;；:：]+", "", (text or "").casefold())
    if not normalized:
        return False

    negative_markers = ("不", "否", "取消", "停止", "拒绝", "no", "cancel", "stop")
    if any(marker in normalized for marker in negative_markers):
        return False

    exact = {
        "确认",
        "确定",
        "同意",
        "批准",
        "继续",
        "继续执行",
        "确认继续",
        "确认执行",
        "确认创建",
        "确认修改",
        "确认删除",
        "确认提交",
        "确认核实",
        "确认采集",
        "确认投料",
        "好的确认",
        "好的确认执行",
        "ok",
        "okay",
        "yes",
        "y",
    }
    return normalized in exact


def is_explicit_rejection(text: str) -> bool:
    normalized = re.sub(r"[\s，。！？、,.!?;；:：]+", "", (text or "").casefold())
    if not normalized:
        return False
    return normalized in {
        "不",
        "否",
        "不确认",
        "取消",
        "取消执行",
        "停止",
        "停止执行",
        "拒绝",
        "no",
        "n",
        "cancel",
        "stop",
    }


def approval_prompt(decision: SafetyDecision) -> str:
    return (
        "以下操作需要你确认，目前尚未提交这些操作。\n"
        "核对无误后点击“确认执行”，或回复“确认执行”；如需调整，请取消后说明修改内容。"
    )


def plan_fingerprint(calls):
    """Bind approval to ordered tool names and canonical arguments."""
    import hashlib
    plan = []
    for call in calls:
        function = call.get("function") or {}
        args = function.get("arguments") or "{}"
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            pass
        plan.append({"tool": function.get("name"), "arguments": args})
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def plan_preview(calls):
    lines = []
    for index, call in enumerate(calls, 1):
        function = call.get("function") or {}
        arguments = str(function.get("arguments") or "{}")
        lines.append(f"{index}. {function.get('name')}: {arguments}")
        if function.get('name') in {'execute_quality_transaction','review_quality_transaction'}:
            try:
                from tools.quality_plans import confirmation_summary
                args=json.loads(arguments)
                lines.append(confirmation_summary(args.get('plan_id'),args.get('plan_digest')))
                if function['name']=='review_quality_transaction':
                    description = ('系统将复核指定MES历史及原对象当前效果，仅在全部匹配后登记本地结案并解除待审占用。'
                                   if args.get('action')=='resolve_history'
                                   else '须人工最终服务端事务/审计证据，当前同名、同值或空历史不能证明结果。')
                    lines.append('本地审计内容：'+json.dumps(args,ensure_ascii=False)+'。'+description)
            except (ValueError,TypeError,KeyError,AttributeError):lines.append('质量事务计划无法读取，不应继续。')
        if function.get('name')=='verify_collection_submission':
            try:
                from tools.collection_submission import db_connect, digest
                args=json.loads(arguments)
                with db_connect() as db:row=db.execute('SELECT digest,payload,state FROM plans WHERE id=?',(args.get('plan_id'),)).fetchone()
                if not row or row[0]!=args.get('plan_digest') or digest(json.loads(row[1])['snapshot'])!=row[0]:raise ValueError('摘要不符')
                lines.append('核实采集提交，仅保存本地审计与终态：'+row[1]+'；当前状态：'+row[2])
            except (ValueError,TypeError,KeyError,AttributeError):lines.append('采集核实计划无法读取，不应继续。')
        if function.get('name') in {'execute_bom_change','review_bom_change'}:
            try:
                from tools.bom_modeling import confirmation_summary,checked,db_connect,load_row
                args=json.loads(arguments)
                if function['name']=='execute_bom_change':
                    lines.append(confirmation_summary(args.get('plan_id'),args.get('plan_digest')))
                else:
                    with db_connect() as db: row=load_row(db,args.get('plan_id'))
                    lines.append('BOM维护本地审计：'+json.dumps(checked(row,args.get('plan_digest')),ensure_ascii=False))
                    lines.append('证据/结论：'+json.dumps(args,ensure_ascii=False))
            except (ValueError,TypeError,KeyError,AttributeError):
                lines.append('BOM维护计划无法读取，不应继续。')
        if function.get('name') in {'execute_data_collection_change','reconcile_data_collection_change','review_data_collection_change'}:
            try:
                from tools.collection_modeling import confirmation_summary,checked,db_connect
                args=json.loads(arguments)
                if function['name']=='execute_data_collection_change':
                    lines.append(confirmation_summary(args.get('plan_id'),args.get('plan_digest')))
                else:
                    with db_connect() as db:
                        row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(args.get('plan_id'),)).fetchone()
                    lines.append('数据采集维护核实/审计，仅改变本地状态：'+json.dumps(checked(row,args.get('plan_digest')),ensure_ascii=False))
                    lines.append('证据/结论：'+json.dumps(args,ensure_ascii=False))
            except (ValueError,TypeError,KeyError,AttributeError):
                lines.append('采集维护计划无法读取，不应继续。')
        if function.get('name') in {'recover_collection_submission_attempt','resolve_collection_submission_outcome'}:
            try:
                from tools.collection_recovery import review_summary
                args=json.loads(arguments)
                action='recover' if function['name']=='recover_collection_submission_attempt' else 'resolve'
                lines.append(review_summary(args.get('plan_id'),args.get('plan_digest'),action,
                                            args.get('evidence_reference'),args.get('review_note'),args.get('conclusion')))
            except (ValueError,TypeError,KeyError,AttributeError):
                lines.append('采集计划人工核实信息无法读取，不应继续。')
        if function.get('name') in {'recover_mfgorder_task_attempt', 'resolve_mfgorder_task_outcome'}:
            try:
                from tools.mfgorder_task_recovery import review_summary
                args = json.loads(arguments)
                action = 'recover' if function['name'] == 'recover_mfgorder_task_attempt' else 'resolve'
                lines.append(review_summary(args.get('plan_id'), args.get('plan_digest'), action,
                                            args.get('evidence_reference'), args.get('review_note'), args.get('conclusion')))
            except (ValueError, TypeError, KeyError):
                lines.append('工单任务人工核实信息无法读取，不应继续。')
        if function.get("name") in ("execute_container_throughput", "execute_production_reject", "execute_production_station_transition"):
            try:
                from tools.production_reporting import confirmation_summary
                args = json.loads(arguments)
                lines.append(confirmation_summary(args.get('plan_id'), args.get('plan_digest')))
            except (ValueError, TypeError, KeyError):
                lines.append('吞吐报工计划无法读取，不应继续。')
        if function.get('name') == 'execute_production_labor_submission':
            try:
                from tools.production_labor import linked_plan
                from tools.collection_submission import confirmation_summary
                args, _, _ = linked_plan(json.loads(arguments).get('record_id'))
                lines.append(confirmation_summary(**args))
            except (ValueError, TypeError, KeyError):
                lines.append('原人工工时提交计划无法核实，不应继续。')
        if function.get("name") == "execute_mfgorder_task_result":
            try:
                from tools.mfgorder_task_plans import confirmation_summary
                args = json.loads(arguments)
                lines.append(confirmation_summary(args.get('plan_id'), args.get('plan_digest')))
            except (ValueError, TypeError, KeyError):
                lines.append('工单任务计划无法读取，不应继续。')
        if function.get("name") == "recover_container_transfer_attempt":
            try:
                from tools.container_transfer_plans import recovery_summary
                args = json.loads(arguments)
                lines.append(recovery_summary(args.get('plan_id'), args.get('plan_digest'),
                                              args.get('evidence_reference'), args.get('review_note')))
            except (ValueError, TypeError, KeyError):
                lines.append('恢复计划无法读取，不应继续。')
        if function.get("name") == "resolve_container_transfer_outcome":
            try:
                from tools.container_transfer_plans import resolution_summary
                args = json.loads(arguments)
                lines.append(resolution_summary(args.get('plan_id'), args.get('plan_digest'), args.get('conclusion'),
                                                args.get('evidence_reference'), args.get('review_note')))
            except (ValueError, TypeError, KeyError):
                lines.append('人工核实信息无法读取，不应继续。')
        if function.get("name") in {"execute_container_disassociation", "execute_container_combination", "execute_container_association", "execute_container_split"}:
            try:
                from tools.container_transfer_plans import confirmation_summary
                args = json.loads(arguments)
                lines.append(confirmation_summary(args.get("plan_id"), args.get("plan_digest")))
            except (ValueError, TypeError, KeyError):
                lines.append("拆箱计划不可读取，请重新预览。")
        if function.get("name") == "collect_container_data":
            try:
                from tools.collection_submission import confirmation_summary
                args = json.loads(arguments)
                lines.append(confirmation_summary(args.get("plan_id"), args.get("plan_digest")))
            except (ValueError, TypeError, KeyError):
                lines.append("无法读取有效采集计划，请先重新预览。")
        if function.get('name') == 'issue_components':
            try:
                from tools.component_issue_plans import confirmation_summary
                args = json.loads(arguments)
                lines.append(confirmation_summary(args.get('plan_id'), args.get('plan_digest')))
            except (ValueError, TypeError, KeyError):
                lines.append('无法读取有效投料计划，请先重新预览。')
        if function.get('name') in {'recover_component_issue_attempt', 'resolve_component_issue_outcome'}:
            try:
                from tools.component_issue_plans import review_summary
                args = json.loads(arguments)
                action = 'recover' if function['name'] == 'recover_component_issue_attempt' else 'resolve'
                lines.append(review_summary(args.get('plan_id'), args.get('plan_digest'), action,
                    args.get('evidence_reference'), args.get('review_note'), args.get('conclusion')))
            except (ValueError, TypeError, KeyError):
                lines.append('投料计划状态或证据不可读取，不应解除占用。')
        if function.get('name') == 'reconcile_component_issue':
            try:
                from tools.component_issue_plans import review_summary
                args = json.loads(arguments)
                lines.append(review_summary(args.get('plan_id'), args.get('plan_digest'), 'verify', None, None))
            except (ValueError, TypeError, KeyError):
                lines.append('投料计划状态或摘要不可读取，不应自动更改占用。')
        if function.get("name") == "execute_workflow_import_plan":
            try:
                from tools.import_plans import confirmation_summary
                args = json.loads(arguments)
                lines.append(confirmation_summary(args.get("plan_id"), args.get("plan_digest")))
            except (ValueError, TypeError, KeyError):
                lines.append("无法读取有效导入计划，请先重新预览。")
    return "待执行工具与参数：\n" + "\n".join(lines)
