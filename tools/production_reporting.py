"""Production evidence and single-use native throughput plans.

No inferred yield, labor units, order-status updates, or replay of uncertain writes.
"""
import asyncio
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from config import CAMSTAR_QUERY_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL
from core.http_client import request
from core.transfer_attempt_lock import AttemptBusy, attempt_lock
from tools import mcp
from tools.container_inquiry import get_container_details
from tools.modeling_details import raw_json
from tools.process_context import ReadContext

PLAN_DB = Path(__file__).resolve().parents[1] / 'data' / 'production_reporting.sqlite3'
TTL = 3600


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{16}', value):
        raise ValueError('必须使用真实16位实例ID。')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def receipt_comments(arguments):
    """Only internally minted markers can attribute a write to this plan."""
    comments = arguments.get('comments') or ''
    if not isinstance(comments, str) or len(comments) > 1000:
        raise ValueError('comments须为不超过1000字符的文本。')
    marker = arguments.get('_receipt_marker')
    if marker is None:  # Historical plans retain their original exact body.
        return comments
    if not isinstance(marker, str) or not re.fullmatch(r'MCP-PROD:[0-9a-f]{32}', marker):
        raise ValueError('原计划事务标记无效。')
    return marker + (' ' + comments if comments else '')


async def remaining_quantity(counters, cid, name):
    """Nullable new-container counters are resolved by MES, never defaulted locally."""
    for field in ('Qty', 'CurrentThruputQty', 'ThruputAdjustmentQty'):
        if field not in counters:
            raise ValueError(field + '列缺失，未按零处理。')
        if counters[field] not in (None, ''):
            sql_number(counters[field], field)
    sql_number(counters['Qty'], 'Qty')
    if all(counters[k] not in (None, '') for k in ('CurrentThruputQty', 'ThruputAdjustmentQty')):
        remaining = (Decimal(str(counters['Qty'])) - Decimal(str(counters['CurrentThruputQty']))
                     + Decimal(str(counters['ThruputAdjustmentQty'])))
        if remaining < 0:
            raise ValueError('剩余未加工量为负数。')
        return float(remaining), None
    # execute=false evaluates the native read-only QtyRemaining field. It does
    # not execute Thruput, and is not evidence of dynamic write validation.
    raw = await request('POST', '/api/Thruput', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body={'container': {'name': name, 'instanceId': identifier(cid)}},
        params={'execute': 'false', '$select': 'Container,QtyRemaining'}, response_formatter=raw_json)
    try:
        response = json.loads(raw); data = response.get('data', {})
    except (ValueError, TypeError):
        raise ValueError('原生剩余加工量读取失败：' + str(raw)) from None
    ref = data.get('Container', {})
    if (ref.get('InstanceId') != cid or ref.get('Name') != name or response.get('error')
            or data.get('ErrorCode') or data.get('error')):
        raise ValueError('原生剩余量引用未核实；不将空计数推断为零。')
    remaining = sql_number(data.get('QtyRemaining'), '原生QtyRemaining')
    return remaining, {'source': 'Thruput.QtyRemaining execute=false', 'container_id': cid, 'remaining_qty': remaining}


def sql_number(value, label, *, integer=False):
    """Query/AdHoc encodes scalar values as strings on this deployment."""
    if type(value) not in (str, int, float):
        raise ValueError(label + '未返回有效数值。')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(label + '不是有效数值。') from None
    if not number.is_finite() or number < 0 or not math.isfinite(float(number)):
        raise ValueError(label + '不是有限非负数。')
    if integer:
        if number != number.to_integral_value():
            raise ValueError(label + '须为整数。')
        return int(number)
    return float(number)


def sql_boolean(value, label):
    if type(value) is bool:
        return value
    if value == 'true':
        return True
    if value == 'false':
        return False
    raise ValueError(label + '未明确返回true/false。')


def db_connect():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB, timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, digest TEXT NOT NULL, '
               'payload TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, outcome TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS reviews (plan_id TEXT PRIMARY KEY, evidence TEXT NOT NULL, '
               'conclusion TEXT NOT NULL, note TEXT NOT NULL, created REAL NOT NULL)')
    return db


def ensure_no_unresolved_throughput(cid):
    """Legacy movement must not bypass an unresolved throughput attempt."""
    if not PLAN_DB.is_file():
        return
    with db_connect() as db:
        for payload, in db.execute("SELECT payload FROM plans WHERE state IN ('unknown','in_flight')"):
            snap = json.loads(payload)['snapshot']
            if snap['environment'] == CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/') and snap['container_id'] == cid:
                raise ValueError('此容器有未核实吞吐请求；不能通过普通移站绕过占用。')


def ensure_no_unresolved_collection(cid):
    from tools import collection_submission as c
    if not c.PLAN_DB.is_file():
        return
    with c.db_connect() as db:
        rows = db.execute("SELECT payload FROM plans WHERE state IN ('unknown','in_flight')").fetchall()
    for raw, in rows:
        snap = json.loads(raw)['snapshot']
        if (snap.get('environment') == CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')
                and snap['body']['container'].get('instanceId') == cid):
            raise ValueError('此容器有未核实采集/工时提交；先核实原事务，不移动或重复报工。')


async def query_rows(sql):
    raw = await request('POST', '/api/AdHoc', base_url=CAMSTAR_QUERY_BASE_URL,
                        body={'queryText': sql}, response_formatter=raw_json, retry_read_only=True)
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('生产证据查询失败：' + str(raw)) from None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('生产证据不是完整记录数组；未按空记录处理。')
    return rows


def ensure_no_unresolved_quality(cid):
    """Respect existing quality runtime container claims before new reporting."""
    from tools import quality_plans
    if not quality_plans.PLAN_DB.is_file():
        return
    from tools.quality_lot_execution import runtime_claim
    claim = runtime_claim('container', identifier(cid))
    with quality_plans.db_connect() as db:
        rows = db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')").fetchall()
    for payload, in rows:
        snap = json.loads(payload)
        if snap.get('environment') == quality_plans.environment() and claim in quality_plans.resource_sets(snap)[1]:
            raise ValueError('此容器有未核实质量事务，不能用新报工绕过占用。')


async def container_state(name, cid=None):
    if not isinstance(name, str) or not name.strip():
        raise ValueError('明确容器名必填。')
    if cid is not None:
        identifier(cid)
    result = json.loads(await get_container_details(name.strip(), cid, view='status'))
    row = result.get('data', {}).get('ContainerStatusDetails')
    if result.get('error') or result.get('complete') is not True or not isinstance(row, dict):
        raise ValueError('容器状态不完整，未继续：' + str(result.get('error', '缺少状态')))
    ref = row.get('Container') or {}
    actual = identifier(ref.get('InstanceId') or ref.get('InstanceID'))
    if ref.get('Name') != name.strip() or cid and actual != cid:
        raise ValueError('实际容器名称/ID不一致；视图行ID不能用作容器ID。')
    return row, actual


def throughput_sql(cid, offset, limit):
    return ('SELECT t.ThruputHistoryId, t.HistoryMainlineId, t.ContainerId, t.OperationId, '
            't.ResourceId, t.Qty, t.Qty2, t.UOMId, t.UOM2Id, t.ThruputAllQty, '
            'h.TxnId, h.TxnDateGMT, h.TxnTypeName, h.WorkflowStepId, h.StepPass, '
            'h.ReversalStatus, h.Comments FROM ThruputHistory t '
            'JOIN HistoryMainline h ON h.HistoryMainlineId=t.HistoryMainlineId '
            f"WHERE t.ContainerId='{identifier(cid)}' "
            'ORDER BY h.TxnDateGMT DESC, t.ThruputHistoryId DESC '
            f'OFFSET {offset} ROWS FETCH NEXT {limit + 1} ROWS ONLY')


@mcp.tool
async def get_container_throughput_history(container_name: str, container_instance_id: str | None = None,
                                          offset: int = 0, limit: int = 30) -> str:
    """只读实际ThruputHistory主记录及事务/工序/单位/冲销标志，offset>=0、limit=1..100。
    不合并父子明细，不跨工序累计成完工量，不将吞吐量等同良品量；统计仅当前页。
    固定当前部署SQL契约，不接受调用者SQL；读失败不返回零产量。
    """
    out = {'complete': False, 'source': 'ThruputHistory + HistoryMainline',
           'scope': 'direct_container_current_page', 'rows': []}
    try:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('offset>=0，limit=1..100，均为整数。')
        _, cid = await container_state(container_name, container_instance_id)
        rows = await query_rows(throughput_sql(cid, offset, limit))
        if len(rows) > limit + 1 or any(not r.get('ThruputHistoryId') or r.get('ContainerId') != cid for r in rows):
            raise ValueError('吞吐历史身份或分页响应不一致。')
        page = rows[:limit]
        out.update(container_instance_id=cid, rows=page, page={'offset': offset, 'limit': limit,
                   'has_more': len(rows) > limit, 'next_offset': offset + limit if len(rows) > limit else None},
                   complete=True, all_history_returned=offset == 0 and len(rows) <= limit,
                   warnings=['跨页不是事务快照；原始记录可能包含重加工或冲销，不能当作净产量或良品数。'])
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        out['error'] = str(exc)
    return raw_json(out)


async def checked_snapshot(arguments):
    service = arguments['service']
    if service in ('MoveIn', 'MoveStd'):
        from tools.production_station import station_snapshot
        return await station_snapshot(arguments)
    if service == 'ContainerDefect':
        from tools.production_records import reject_snapshot
        return await reject_snapshot(arguments)
    if service not in ('Thruput', 'MoveWithThruput'):
        raise ValueError('仅支持Thruput或MoveWithThruput。')
    qty = arguments['qty']
    if type(qty) not in (float, int) or not math.isfinite(qty) or qty <= 0:
        raise ValueError('qty必须为有限正数，表示本次加工量，不是余额或良品数量。')
    name = arguments['container_name']
    status, cid = await container_state(name, arguments.get('container_instance_id'))
    ensure_no_unresolved_throughput(cid)
    ensure_no_unresolved_collection(cid)
    ensure_no_unresolved_quality(cid)
    if type(status.get('Status')) is not int or status['Status'] != 1 or type(status.get('InProcess')) is not bool:
        raise ValueError('容器须为已明确的Active状态，InProcess必须明确。')
    ctx = ReadContext(max_requests=12)
    operation = await ctx.resolve('Operations', status.get('Operation'), revisioned=False)
    level = await ctx.resolve('ContainerLevels', status.get('ContainerLevel'), revisioned=False)
    reporting = operation.get('ThruputReportingLevel') or {}
    reporting_id = reporting.get('InstanceId') or reporting.get('InstanceID')
    if reporting_id:
        same_level = reporting_id == level.get('InstanceID')
    else:
        same_level = bool(reporting.get('Name')) and reporting.get('Name') == level.get('Name')
    if not same_level:
        raise ValueError('仅支持吞吐上报层级与当前容器层级相同的直接报工；不自动切换层级或展开父子容器。')
    if service == 'MoveWithThruput' and (status['InProcess'] or level.get('AllowMove') is not True):
        raise ValueError('带吞吐移站仅支持未进站且层级允许移动的容器；已进站须明确选择分步生产流程，不自动换事务。')
    # SQL contains only a server-verified hexadecimal identity. Current counters
    # prevent knowingly processing the same remaining quantity twice.
    rows = await query_rows('SELECT ContainerId, ChangeCount, Qty, Qty2, UOMId, CurrentStatusId, '
        '(SELECT CurrentStepPass FROM CurrentStatus s WHERE s.CurrentStatusId=Container.CurrentStatusId) AS StepPass, CurrentThruputQty, '
        'ThruputAdjustmentQty, ChildCount, ParentContainerId, '
        'COALESCE(CurrentHoldCount,0) AS CurrentHoldCount, ProductionComplete '
        f"FROM Container WHERE ContainerId='{cid}'")
    if len(rows) != 1 or rows[0].get('ContainerId') != cid:
        raise ValueError('容器计数器未唯一读取。')
    counters = rows[0]
    remaining, quantity_read = await remaining_quantity(counters, cid, name.strip())
    for field in ('Qty', 'CurrentThruputQty', 'ThruputAdjustmentQty'):
        if counters[field] not in (None, ''):
            counters[field] = sql_number(counters[field], field)
    for field in ('ChildCount', 'CurrentHoldCount', 'ChangeCount'):
        counters[field] = sql_number(counters.get(field), field, integer=True)
    counters['ProductionComplete'] = sql_boolean(counters.get('ProductionComplete'), 'ProductionComplete')
    if (counters['ChildCount'] != 0
            or counters.get('ParentContainerId') not in (None, '', '0000000000000000')
            or counters.get('CurrentHoldCount') != 0 or type(counters.get('CurrentHoldCount')) is not int
            or counters.get('ProductionComplete') is not False):
        raise ValueError('仅支持无父子容器、未冻结且尚未生产完成的基础容器。')
    if type(counters.get('ChangeCount')) is not int:
        raise ValueError('容器版本计数未返回。')
    # Vendor AdjustedThruputQty = CurrentThruputQty - ThruputAdjustmentQty.
    if qty > remaining:
        raise ValueError('本次加工量超过当前剩余未加工量。')
    # MoveWithThruput exposes no root qty field. Partial throughput is
    # deliberately restricted to standalone Thruput.
    if service == 'MoveWithThruput' and qty != remaining:
        raise ValueError('带吞吐移站仅支持明确的全部剩余量；部分吞吐请使用Thruput。')
    body = {'container': {'name': name.strip(), 'instanceId': cid}, 'thruputAllQty': False, 'qty': qty}
    if service == 'MoveWithThruput':
        body = {'container': body['container'], 'moveAllQty': True, 'thruputAllQty': True, 'close': False}
    resource_name = arguments.get('resource_name')
    resource = None
    if resource_name is not None:
        if not isinstance(resource_name, str) or not resource_name.strip():
            raise ValueError('resource_name不能为空。')
        resource = await ctx.resolve('Resources', {'Name': resource_name.strip()}, revisioned=False)
        if resource.get('IsFrozen') is not False:
            raise ValueError('资源冻结状态未明确为false。')
        body['resource'] = {'name': resource['Name'], 'instanceId': identifier(resource['InstanceID'])}
    if arguments.get('comments') is not None or arguments.get('_receipt_marker'):
        body['comments'] = receipt_comments(arguments)
    # No execute=false POST: preparation is entirely read-only. Native execution
    # retains its collection, dispatch, signature, timer, and training validation.
    history = await query_rows(throughput_sql(cid, 0, 1))
    if len(history) > 2 or any(not row.get('ThruputHistoryId') or row.get('ContainerId') != cid for row in history):
        raise ValueError('吞吐历史基线身份或分页不一致，未生成可执行计划。')
    return {'environment': CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'), 'query_environment': CAMSTAR_QUERY_BASE_URL.rstrip('/'),
            'service': service, 'body': body, 'container_id': cid, 'qty': qty, 'remaining_qty': remaining,
            'status': {key: status.get(key) for key in ('Container', 'Status', 'InProcess', 'Workflow', 'Step',
                'Spec', 'Operation', 'Qty', 'Qty2', 'UOM', 'UOM2', 'MfgOrder', 'ContainerLevel')},
            'counters': counters, 'operation': operation, 'level': level,
            'resource': resource, 'history_baseline': history, 'native_quantity_read': quantity_read,
            **({'receipt_marker': arguments['_receipt_marker']} if arguments.get('_receipt_marker') else {})}


@mcp.tool
async def prepare_container_throughput(container_name: str, qty: float,
        service: Literal['Thruput', 'MoveWithThruput'] = 'Thruput',
        container_instance_id: str | None = None, resource_name: str | None = None,
        comments: str | None = None) -> str:
    """只读准备一小时单次吞吐计划；qty是本次加工量，不是良品/不良品/余额。
    Thruput支持部分加工且不移站；MoveWithThruput仅未进站基础容器的全部剩余量移站。
    直接相同上报层级、无父子/冻结/生产完成；不调用Shopfloor预览，不绕过原生要求。
    独立Thruput已完成限定现场验收；MoveWithThruput未纳入本次完整路线验收。
    本入口不直接登记人工工时或关闭工单。
    """
    arguments = dict(container_name=container_name, qty=qty, service=service,
                     container_instance_id=container_instance_id, resource_name=resource_name, comments=comments,
                     _receipt_marker='MCP-PROD:' + uuid.uuid4().hex)
    try:
        snap = await asyncio.wait_for(checked_snapshot(arguments), timeout=60)
        # Always bind execution to the exact resolved Container even if input omitted ID.
        arguments['container_instance_id'] = snap['container_id']
        plan_id, signature = uuid.uuid4().hex, digest(snap)
        with db_connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',
                       (plan_id, signature, raw_json({'arguments': arguments, 'snapshot': snap}), 'ready', time.time()))
        return raw_json({'plan_id': plan_id, 'plan_digest': signature, 'state': 'ready',
                         'request': snap['body'], 'service': service, 'qty': qty,
                         'remaining_qty_before': snap['remaining_qty'], 'expires_in_seconds': TTL,
                         'writes_executed': False, 'native_validation_verified': False,
                         'note': '本地静态计划；尚未执行原生动态校验。提交只尝试一次，结果未知时不重放。'})
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as exc:
        return raw_json({'executable': False, 'error': str(exc) or '准备超时。'})


def checked_plan(row, signature):
    if not row or row[0] != signature or row[2] != 'ready' or not 0 <= time.time() - row[3] <= TTL:
        raise ValueError('计划不存在、摘要不符、已使用或过期。')
    data = json.loads(row[1])
    snap = data['snapshot']
    if snap.get('service') not in ('Thruput', 'MoveWithThruput', 'ContainerDefect', 'MoveIn', 'MoveStd'):
        raise ValueError('计划不属于已核实的生产事务服务。')
    if (digest(snap) != signature or snap['environment'] != CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')
            or snap['query_environment'] != CAMSTAR_QUERY_BASE_URL.rstrip('/')):
        raise ValueError('计划内容或目标环境发生变化。')
    return data


def confirmation_summary(plan_id, plan_digest):
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created FROM plans WHERE id=?', (plan_id,)).fetchone()
    try:
        snap = checked_plan(row, plan_digest)['snapshot']
        if snap['service'] in ('MoveIn', 'MoveStd'):
            return '生产进出站独立步骤（不是失败后的自动替代）：\n' + raw_json({
                'service': snap['service'], 'action': snap['action'], 'request': snap['body']})
        if snap['service'] == 'ContainerDefect':
            return '登记不良并保留在制数量：\n' + raw_json({'request': snap['body'],
                'reject_qty': snap['reject_qty'], 'quantity_deduction': False}) + '\n不良件数与缺陷次数分开；未知结果不重试。'
        return ('上报本次加工量：\n' + raw_json({'service': snap['service'], 'request': snap['body'], 'qty': snap['qty']})
                + '\n加工量不等同良品量；带吞吐移站会推进工艺路线。仅提交一次，未知结果不自动重试。')
    except (ValueError, KeyError, TypeError):
        return '吞吐计划无效或已使用，不应继续。'


@mcp.tool
async def recover_container_throughput_attempt(plan_id: str, plan_digest: str,
        evidence_reference: str, review_note: str) -> str:
    """中断恢复只将in_flight改为unknown，保留容器占用且禁止重放。
    进程锁防止仍在本机执行时恢复；须明确中断证据及说明。此操作不证明MES请求结束，
    后续仍须核对服务端最终事务证据，再独立人工审计结果。
    """
    try:
        if not evidence_reference.strip() or not review_note.strip():
            raise ValueError('中断证据引用及核实说明必填。')
        with attempt_lock(PLAN_DB, plan_id), db_connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest,payload,state,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
            if not row or row[0] != plan_digest or row[2] != 'in_flight':
                raise ValueError('仅可恢复摘要匹配且本机执行已结束的in_flight计划。')
            if digest(json.loads(row[1])['snapshot']) != plan_digest:
                raise ValueError('计划内容摘要不一致。')
            outcome = json.loads(row[3]) if row[3] else {}
            outcome['interruption_recovery'] = {'evidence_reference': evidence_reference.strip(),
                'review_note': review_note.strip(), 'recorded_at': time.time(), 'server_request_finished': None}
            db.execute("UPDATE plans SET state='unknown',outcome=? WHERE id=?", (raw_json(outcome), plan_id))
        return raw_json({'plan_id': plan_id, 'state': 'unknown', 'container_fence_retained': True,
                         'writes_executed': False, 'replay_allowed': False})
    except (ValueError, TypeError, AttributeError, AttemptBusy) as exc:
        return raw_json({'error': str(exc), 'writes_executed': False})


@mcp.tool
async def execute_container_throughput(plan_id: str, plan_digest: str) -> str:
    """执行同一吞吐计划：重读身份/数量/工序/历史基线，原子领取并单次POST。
    整个容器的unknown/in_flight互斥。响应和回读保留为证据，不自动推断本次成功或重试。
    外部MCP调用方须实施等同授权；plan_id/摘要不是授权凭证。
    """
    try:
        with attempt_lock(PLAN_DB, plan_id):
            with db_connect() as db:
                row = db.execute('SELECT digest,payload,state,created FROM plans WHERE id=?', (plan_id,)).fetchone()
            data = checked_plan(row, plan_digest)
            snap = data['snapshot']
            current = await asyncio.wait_for(checked_snapshot(data['arguments']), timeout=60)
            if digest(current) != plan_digest:
                raise ValueError('容器状态、配置、数量或历史发生变化，请重新准备。')
            with db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                stored = db.execute('SELECT digest,payload,state,created FROM plans WHERE id=?', (plan_id,)).fetchone()
                if stored != row:
                    raise ValueError('计划已被领取或发生变化。')
                checked_plan(stored, plan_digest)
                for payload, in db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')"):
                    other = json.loads(payload)['snapshot']
                    if other['environment'] == snap['environment'] and other['container_id'] == snap['container_id']:
                        raise ValueError('此容器有未核实吞吐请求，先核对原事务。')
                db.execute("UPDATE plans SET state='in_flight' WHERE id=?", (plan_id,))
            outcome = {'submitted': True, 'state': 'unknown', 'transaction_verified': False,
                       'started_at': time.time(), 'note': '响应及历史仅供核实；未知请求不重放。'}
            try:
                outcome['response'] = await request('POST', '/api/' + snap['service'],
                    base_url=CAMSTAR_SHOPFLOOR_BASE_URL, body=snap['body'],
                    params={'execute': 'true'}, response_formatter=raw_json)
                if snap['service'] in ('MoveIn', 'MoveStd'):
                    outcome['history_after'] = await query_rows('SELECT TOP 3 HistoryMainlineId,TxnId,TxnTypeName,'
                        'WorkflowStepId,StepPass,Comments FROM HistoryMainline '
                        f"WHERE ContainerId='{snap['container_id']}' ORDER BY HistoryMainlineId DESC")
                elif snap['service'] == 'ContainerDefect':
                    from tools.production_records import reject_history
                    outcome['history_after'] = await reject_history(snap['container_id'])
                else:
                    outcome['history_after'] = await query_rows(throughput_sql(snap['container_id'], 0, 3))
            except Exception as exc:
                outcome['error'] = str(exc)
            finally:
                # Cancellation/process death leaves in_flight or unknown occupied.
                with db_connect() as db:
                    db.execute("UPDATE plans SET state='unknown',outcome=? WHERE id=? AND state='in_flight'",
                               (raw_json(outcome), plan_id))
            return raw_json({'plan_id': plan_id, **outcome})
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError, AttemptBusy) as exc:
        return raw_json({'submitted': False, 'error': str(exc) or '提交前校验超时。'})


@mcp.tool
async def get_container_throughput_plan(plan_id: str) -> str:
    """读取原计划、单次请求响应及回读证据；unknown/in_flight不是失败或可重试。"""
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
    if not row:
        return raw_json({'error': '计划不存在。'})
    return raw_json({'plan_id': plan_id, 'plan_digest': row[0], 'plan': json.loads(row[1]),
                     'state': row[2], 'expired': not 0 <= time.time() - row[3] <= TTL,
                     'outcome': json.loads(row[4]) if row[4] else None})


@mcp.tool
async def review_container_throughput(plan_id: str, plan_digest: str,
        conclusion: Literal['committed', 'not_committed'], evidence_reference: str, review_note: str) -> str:
    """人工核实最终原生事务后审计unknown吞吐计划；不提交、不重试、不回滚。
    必须先确认服务端请求已结束并核实实际事务ID/最终日志，不以同值或空历史作依据。
    in_flight可能仍在执行，不能从本工具释放；原计划审计后仍永不可重放。
    """
    try:
        if conclusion not in ('committed', 'not_committed') or not evidence_reference.strip() or not review_note.strip():
            raise ValueError('最终结论、事务/日志证据引用和核实说明必填。')
        with attempt_lock(PLAN_DB, plan_id), db_connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest,payload,state FROM plans WHERE id=?', (plan_id,)).fetchone()
            if not row or row[0] != plan_digest or row[2] != 'unknown':
                raise ValueError('仅可审计摘要匹配的unknown计划；in_flight不得释放。')
            if digest(json.loads(row[1])['snapshot']) != plan_digest:
                raise ValueError('计划内容摘要不一致。')
            db.execute('INSERT INTO reviews VALUES (?,?,?,?,?)',
                       (plan_id, evidence_reference.strip(), conclusion, review_note.strip(), time.time()))
            db.execute('UPDATE plans SET state=? WHERE id=?', ('reviewed_' + conclusion, plan_id))
        return raw_json({'plan_id': plan_id, 'state': 'reviewed_' + conclusion, 'writes_executed': False,
                         'native_outcome_automatically_verified': False, 'replay_allowed': False})
    except (ValueError, TypeError, AttributeError, AttemptBusy) as exc:
        return raw_json({'error': str(exc), 'writes_executed': False})


@mcp.tool
async def get_mfgorder_production_summary(mfgorder_name: str) -> str:
    """全工单只读生产证据：计划/开工量、MES生产完成时间、容器完工标志及按工序/单位分组的原始吞吐量。
    当前关联范围；不把多工序累计、Closed、任务完成或当前容器余额当作良品/净完工数。
    原始吞吐含重加工/可能冲销，不合并主记录和明细。生产完成时间不等同整单业务关闭。
    """
    out = {'complete': False, 'production_completed_qty': None, 'good_qty': None, 'reject_qty': None,
           'labor_hours': None, 'completion_percentage': None, 'writes_executed': False}
    try:
        if not isinstance(mfgorder_name, str) or not mfgorder_name.strip():
            raise ValueError('明确工单名必填。')
        order = await ReadContext(max_requests=3).resolve('MfgOrders', {'Name': mfgorder_name.strip()}, revisioned=False)
        oid = identifier(order['InstanceID'])
        orders = await query_rows('SELECT MfgOrderId, MfgOrderName, Qty, QtyStarted, UOMId, '
            'ProductionStartDateGMT, ProductionCompleteDateGMT, PostProductionProcedureState, '
            'PostProductionCompleteDateGMT, OrderStatusId '
            f"FROM MfgOrder WHERE MfgOrderId='{oid}'")
        if len(orders) != 1 or orders[0].get('MfgOrderName') != order['Name'] or orders[0].get('MfgOrderId') != oid:
            raise ValueError('工单生产记录身份不一致。')
        containers = await query_rows('SELECT Status, ProductionComplete, UOMId, COUNT(*) AS ContainerCount, '
            'SUM(Qty) AS CurrentContainerQty FROM Container '
            f"WHERE MfgOrderId='{oid}' GROUP BY Status, ProductionComplete, UOMId")
        throughput = await query_rows('SELECT t.OperationId, t.UOMId, h.ReversalStatus, COUNT(*) AS HistoryCount, '
            'SUM(t.Qty) AS GrossThroughputQty FROM ThruputHistory t '
            'JOIN Container c ON c.ContainerId=t.ContainerId '
            'JOIN HistoryMainline h ON h.HistoryMainlineId=t.HistoryMainlineId '
            f"WHERE c.MfgOrderId='{oid}' GROUP BY t.OperationId,t.UOMId,h.ReversalStatus")
        native = await native_order_quantities(order['Name'])
        out['native_order_quantities'] = native
        # Keep MES-native CompletedQty separate from good yield. Native ScrapQty
        # may count closed Containers, so do not add it to quantities ourselves.
        if native.get('complete') is True:
            out['production_completed_qty'] = native['completed_qty']
            unit = orders[0].get('UOMId')
            ordered = sql_number(orders[0]['Qty'], '工单计划量') if orders[0].get('Qty') not in (None, '') else None
            if (ordered is not None and ordered > 0 and native['order_qty'] == ordered and unit and containers
                    and all(row.get('UOMId') == unit for row in containers)):
                out['completion_percentage'] = float(Decimal(str(native['completed_qty'])) * 100 / Decimal(str(ordered)))
                out['progress_basis'] = 'MES CompletedQty / OrderQty; same current-container UOM, not good yield'
        out.update(order=orders[0], container_distribution=containers, throughput_by_operation=throughput,
                   production_complete_date_recorded=bool(orders[0].get('ProductionCompleteDateGMT')),
                   order_business_closed=None, scope='currently_order_linked_containers', complete=True,
                   warnings=['吞吐量按工序/单位/原始冲销状态分别展示，不相加为工单产量。',
                             '当前容器余额可能重复覆盖父子容器，不汇总成完工量；改绑前历史不属已验证的工单历史归属。',
                             '生产完成日期只表示MES字段已记录；未记录不是证明未完工，也不表示后置过程或整单业务关闭。',
                             'MES原生CompletedQty表示原生完工口径，不等同质量合格数量；ScrapQty不直接解释为不良件数。',
                             '本工具保留MES原始口径；本应用良品/不良台账用get_production_reports，双口径工时用get_production_worktime。独立MES关闭仍待契约。跨查询不是一致性快照。'])
        if native.get('complete') is not True:
            out['complete'] = False
            out['warnings'].append('原生工单产量读取失败；完工数量保持未知。')
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        out['error'] = str(exc)
    return raw_json(out)


async def native_order_quantities(name):
    out = {'complete': False, 'source': 'AdvancedQuery/MfgOrderInquiry_GetOrderQuantities',
           'scope': 'native_query_definition_not_quality_yield'}
    try:
        raw = await request('POST', '/api/AdvancedQuery/MfgOrderInquiry_GetOrderQuantities',
            base_url=CAMSTAR_QUERY_BASE_URL, body={'queryType': 'system',
                'parameters': [{'name': 'MfgOrderName', 'value': name}]},
            response_formatter=raw_json, retry_read_only=True)
        rows = json.loads(raw)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get('MfgOrderName') != name:
            raise ValueError('原生产量查询为空、超量或工单不一致，不能推断零产量。')
        out['raw'] = rows[0]
        for source, target in (('CompletedQty', 'completed_qty'), ('ScrapQty', 'scrap_qty_native'), ('OrderQty', 'order_qty')):
            out[target] = sql_number(rows[0].get(source), source)
        out['complete'] = True
    except (ValueError, TypeError, AttributeError, InvalidOperation, OverflowError) as exc:
        out['error'] = str(exc)
    return out
