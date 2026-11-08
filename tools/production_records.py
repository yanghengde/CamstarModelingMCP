"""Application production ledger and MES-native reject/elapsed-time evidence.

Manual labor and declared yield belong to the application ledger. Their source
is never mislabeled as a Camstar TaskTime, quality verdict, or native order close.
"""
import asyncio
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import time
from typing import Literal
import uuid

from tools import mcp
from tools import production_reporting as p
from tools.collection_validation import timestamp_ticks
from tools.modeling_details import raw_json
from tools.process_context import ReadContext
from config import CAMSTAR_BASE_URL

LEDGER_DB = Path(__file__).resolve().parents[1] / 'data' / 'production_records.sqlite3'


def environment():
    return p.digest([CAMSTAR_BASE_URL.rstrip('/'), p.CAMSTAR_QUERY_BASE_URL.rstrip('/'),
                     p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')])


def text(value, label, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ValueError(label + '须为明确的非空文本，且不超过' + str(maximum) + '字符。')
    return value.strip()


def amount(value, label, positive=False):
    if type(value) not in (str, int, float) or len(str(value)) > 100:
        raise ValueError(label + '须为有限数值。')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(label + '不是数值。') from None
    if not number.is_finite() or number < 0 or number > Decimal('1e12') or positive and number == 0:
        raise ValueError(label + '须为有限非负数（最大1e12），本项要求正数时不能为零。')
    if number.as_tuple().exponent < -12:
        raise ValueError(label + '最多保留12位小数，不自动舍入。')
    return number


def decimal_text(value):
    return format(value, 'f').rstrip('0').rstrip('.') if value != value.to_integral_value() else str(int(value))


def connect():
    LEDGER_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(LEDGER_DB, timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY,environment TEXT NOT NULL, '
               'kind TEXT NOT NULL,entry_key TEXT NOT NULL,digest TEXT NOT NULL,arguments TEXT NOT NULL, '
               'payload TEXT NOT NULL,created REAL NOT NULL,UNIQUE(environment,kind,entry_key))')
    db.execute('CREATE TABLE IF NOT EXISTS native_sources (environment TEXT NOT NULL,source_kind TEXT NOT NULL, '
               'source_id TEXT NOT NULL,record_id TEXT NOT NULL,PRIMARY KEY(environment,source_kind,source_id))')
    db.execute('CREATE TABLE IF NOT EXISTS labor_intervals (record_id TEXT PRIMARY KEY,environment TEXT NOT NULL, '
               'employee_id TEXT NOT NULL,start_ticks INTEGER NOT NULL,end_ticks INTEGER NOT NULL)')
    return db


def existing(kind, key, arguments):
    if not LEDGER_DB.is_file():
        return None
    with connect() as db:
        row = db.execute('SELECT arguments,payload,id,digest FROM records WHERE environment=? AND kind=? AND entry_key=?',
                         (environment(), kind, key)).fetchone()
    if not row:
        return None
    if p.digest(json.loads(row[1])) != row[3]:
        raise ValueError('既有台账摘要损坏，不能按幂等成功返回。')
    if row[0] != raw_json(arguments):
        raise ValueError('同一业务编号已保存不同内容，不能覆盖或重复记账。')
    return {'saved': True, 'record_id': row[2], 'record': json.loads(row[1]), 'idempotent_replay': True,
            'source': 'application_production_ledger', 'mes_write_performed': False}


def save(kind, key, arguments, payload, sources=(), interval=None):
    record_id = uuid.uuid4().hex
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT arguments,payload,id,digest FROM records WHERE environment=? AND kind=? AND entry_key=?',
                         (environment(), kind, key)).fetchone()
        if old:
            if p.digest(json.loads(old[1])) != old[3]:
                raise ValueError('既有台账摘要损坏，不能返回既有记录。')
            if old[0] != raw_json(arguments):
                raise ValueError('业务编号已被不同内容占用，不能覆盖。')
            return {'saved': True, 'record_id': old[2], 'record': json.loads(old[1]), 'idempotent_replay': True,
                    'source': 'application_production_ledger', 'mes_write_performed': False}
        if interval:
            worker, start, end = interval
            if db.execute('SELECT 1 FROM labor_intervals WHERE environment=? AND employee_id=? '
                          'AND start_ticks<? AND end_ticks>?', (environment(), worker, end, start)).fetchone():
                raise ValueError('该员工已有重叠的人工工时记录，不能重复计算人时；可相邻接续。')
        for source_kind, source_id in sources:
            if db.execute('SELECT 1 FROM native_sources WHERE environment=? AND source_kind=? AND source_id=?',
                          (environment(), source_kind, source_id)).fetchone():
                raise ValueError('同一原生提交计划已用于报工，不能重复累计良品/不良品。')
        db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?)',
                   (record_id, environment(), kind, key, p.digest(payload), raw_json(arguments), raw_json(payload), time.time()))
        for source_kind, source_id in sources:
            db.execute('INSERT INTO native_sources VALUES (?,?,?,?)', (environment(), source_kind, source_id, record_id))
        if interval:
            db.execute('INSERT INTO labor_intervals VALUES (?,?,?,?,?)', (record_id, environment(), *interval))
    return {'saved': True, 'record_id': record_id, 'record': payload, 'idempotent_replay': False,
            'source': 'application_production_ledger', 'mes_write_performed': False}


async def order_context(name):
    order = await ReadContext(max_requests=3).resolve('MfgOrders', {'Name': text(name, '工单名')}, revisioned=False)
    return {'name': order['Name'], 'instance_id': p.identifier(order['InstanceID'])}


async def report_context(name, cid=None):
    status, actual = await p.container_state(name, cid)
    rows = await p.query_rows('SELECT c.ContainerId,c.ContainerName,c.ChangeCount,c.Qty,c.UOMId,c.MfgOrderId,'
        'c.CDOTypeId,c.Status,c.ParentContainerId,c.ChildCount,COALESCE(c.CurrentHoldCount,0) AS CurrentHoldCount,'
        'c.SamplingLotId,c.UOM2Id,c.Qty2,c.CurrentStatusId,s.CurrentStepPass,s.CarrierId,s.WorkflowStepId,s.InRework '
        'FROM Container c JOIN CurrentStatus s ON s.CurrentStatusId=c.CurrentStatusId '
        f"WHERE c.ContainerId='{actual}'")
    if len(rows) != 1 or rows[0].get('ContainerId') != actual or rows[0].get('ContainerName') != name.strip():
        raise ValueError('报工持久容器身份不一致。')
    row = rows[0]
    oid = p.identifier(row.get('MfgOrderId'))
    unit = p.identifier(row.get('UOMId'))
    step = p.identifier(row.get('WorkflowStepId'))
    order_ref = status.get('MfgOrder') or {}
    if oid != (order_ref.get('InstanceId') or order_ref.get('InstanceID')):
        raise ValueError('容器工单身份不一致。')
    return {'container_id': actual, 'container_name': name.strip(), 'order_id': oid,
            'order_name': order_ref.get('Name'), 'unit_id': unit, 'step_id': step,
            'status_id': p.identifier(row.get('CurrentStatusId')),
            'step_pass': p.sql_number(row.get('CurrentStepPass'), '当前步骤次数', integer=True)}, row, status


async def reject_history(cid):
    return await p.query_rows('SELECT TOP 4 ContainerDefectHistoryDetailId,ContainerId,DefectCount,ReasonCodeId,TxnId '
        f"FROM ContainerDefectHistoryDetail WHERE ContainerId='{p.identifier(cid)}' "
        'ORDER BY ContainerDefectHistoryDetailId DESC')


async def reject_snapshot(arguments):
    """Native defect registration preserves WIP; bad units remain separate facts."""
    context, row, status = await report_context(arguments['container_name'], arguments.get('container_instance_id'))
    p.ensure_no_unresolved_throughput(context['container_id'])
    p.ensure_no_unresolved_quality(context['container_id'])
    if (p.sql_number(row.get('CDOTypeId'), '容器类型', integer=True) != 1040
            or p.sql_number(row.get('Status'), '状态', integer=True) != 1
            or p.sql_number(row.get('CurrentHoldCount'), '冻结计数', integer=True) != 0
            or p.sql_number(row.get('ChildCount'), '子容器数量', integer=True) != 0
            or row.get('ParentContainerId') or row.get('CarrierId') or row.get('SamplingLotId')
            or p.sql_boolean(row.get('InRework'), '返工标志')):
        raise ValueError('不良登记仅支持无冻结/返工/父子/载具/抽样批的Active基础容器。')
    rejected = amount(arguments['reject_qty'], '不良数量', True)
    inspected = amount(arguments['inspected_qty'], '本次检查数量', True)
    current = amount(row.get('Qty'), '当前数量', True)
    if rejected > inspected or inspected > current:
        raise ValueError('须满足不良数量<=本次检查数量<=当前容器数量。')
    count = arguments['defect_count']
    if type(count) is not int or not 1 <= count <= 2147483647:
        raise ValueError('defect_count须明确提供正整数缺陷次数；不能从不良件数推断。')
    ctx = ReadContext(max_requests=4)
    reason = await ctx.resolve('ContainerDefectReasons', {'Name': text(arguments['reason_name'], '缺陷原因')}, revisioned=False)
    if reason.get('IsFrozen') is not False:
        raise ValueError('缺陷原因必须明确未冻结。')
    body = {'container': {'name': context['container_name'], 'instanceId': context['container_id']},
            'qtyInspected': float(inspected), 'serviceDetails': [{'reasonCode': {'name': reason['Name'],
                 'instanceId': p.identifier(reason['InstanceID'])}, 'defectCount': count}]}
    if Decimal(str(body['qtyInspected'])) != inspected:
        raise ValueError('检查数量无法无损传输，不自动舍入。')
    if arguments.get('comments'):
        body['comments'] = text(arguments['comments'], '备注')
    return {'service': 'ContainerDefect', 'environment': p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),
            'query_environment': p.CAMSTAR_QUERY_BASE_URL.rstrip('/'), 'container_id': context['container_id'],
            'body': body, 'context': context, 'counters': row, 'reason': reason,
            'reject_qty': decimal_text(rejected), 'inspected_qty': decimal_text(inspected),
            'quantity_deduction': False, 'history_baseline': await reject_history(context['container_id'])}


@mcp.tool
async def prepare_production_reject(container_name: str, reject_qty: str,
        disposition: Literal['record', 'scrap'], reason_name: str,
        container_instance_id: str | None = None, inspected_qty: str | None = None,
        defect_count: int | None = None, close_when_zero: bool | None = None,
        adjust_throughput: bool | None = None, comments: str = '') -> str:
    """报工时明确选择不良处置：record登记原生ContainerDefect并保留在制数量；scrap复用LossDetails扣量。
    record须明确检查数量及缺陷次数（不等同不良件数）；scrap须明确归零关闭和吞吐调整两个布尔值。
    返回固定执行工具及单次计划；准备不执行业务写入。scrap预览沿用已验证的质量准备契约。
    """
    try:
        rejected = amount(reject_qty, '不良数量', True)
        if disposition not in ('record', 'scrap'):
            raise ValueError('disposition仅支持record或scrap。')
        if disposition == 'scrap':
            if defect_count is not None or inspected_qty is not None:
                raise ValueError('报废扣量不接受缺陷次数或检查数量，不自动替换为缺陷登记。')
            if type(close_when_zero) is not bool or type(adjust_throughput) is not bool:
                raise ValueError('报废时须明确close_when_zero与adjust_throughput。')
            context, row, _ = await report_context(container_name, container_instance_id)
            p.ensure_no_unresolved_throughput(context['container_id'])
            current = amount(row.get('Qty'), '当前数量', True)
            if rejected > current:
                raise ValueError('报废数量超过当前数量。')
            from tools import quality_plans
            arguments = dict(container_name=container_name, loss_reason_name=reason_name,
                close_when_zero=close_when_zero, adjust_throughput=adjust_throughput,
                loss_quantity=None if rejected == current else decimal_text(rejected), record_all_quantity=rejected == current,
                container_instance_id=context['container_id'], comments=comments, production_context=context)
            result = json.loads(await quality_plans.prepare('quantity_loss', arguments))
            return raw_json({**result, 'disposition': 'scrap', 'quantity_deduction': True,
                'execute_tool': 'execute_quality_transaction', 'review_tool': 'review_quality_transaction'})
        if close_when_zero is not None or adjust_throughput is not None:
            raise ValueError('登记保留在制不接受报废扣量选项。')
        args = dict(service='ContainerDefect', container_name=container_name, container_instance_id=container_instance_id,
                    reject_qty=decimal_text(rejected), inspected_qty=inspected_qty, defect_count=defect_count,
                    reason_name=reason_name, comments=comments)
        snap = await asyncio.wait_for(reject_snapshot(args), 60)
        args['container_instance_id'] = snap['container_id']
        key, signature = uuid.uuid4().hex, p.digest(snap)
        with p.db_connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',
                       (key, signature, raw_json({'arguments': args, 'snapshot': snap}), 'ready', time.time()))
        return raw_json({'plan_id': key, 'plan_digest': signature, 'state': 'ready', 'request': snap['body'],
            'disposition': 'record', 'quantity_deduction': False, 'reject_qty': snap['reject_qty'],
            'execute_tool': 'execute_production_reject', 'review_tool': 'review_container_throughput',
            'native_dynamic_validation_verified': False, 'mes_write_performed': False})
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as exc:
        return raw_json({'executable': False, 'error': str(exc) or '准备超时。'})


@mcp.tool
async def execute_production_reject(plan_id: str, plan_digest: str) -> str:
    """仅执行原ContainerDefect不良登记计划；扣量计划须使用原质量执行工具。
    单次提交、unknown保留容器占用，不重试、不将缺陷次数当作不良件数。
    """
    with p.db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created FROM plans WHERE id=?', (plan_id,)).fetchone()
    try:
        snap = p.checked_plan(row, plan_digest)['snapshot']
        if snap['service'] != 'ContainerDefect':
            raise ValueError('本入口仅接受原生缺陷登记计划。')
    except (ValueError, KeyError, TypeError) as exc:
        return raw_json({'submitted': False, 'error': str(exc)})
    return await p.execute_container_throughput(plan_id, plan_digest)


def reviewed_source(kind, key, signature):
    if kind == 'quality':
        from tools import quality_plans as q
        if not q.PLAN_DB.is_file():
            raise ValueError('质量处置计划不存在。')
        with q.db_connect() as db:
            row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (key,)).fetchone()
        snap = q.checked(row, signature)
        if row[2] not in ('human_committed', 'history_committed'):
            raise ValueError('报废计划尚未依据最终证据核实提交，不能记为已报废。')
        return snap
    if not p.PLAN_DB.is_file():
        raise ValueError('生产提交计划不存在。')
    with p.db_connect() as db:
        row = db.execute('SELECT digest,payload,state FROM plans WHERE id=?', (key,)).fetchone()
        review = db.execute('SELECT conclusion FROM reviews WHERE plan_id=?', (key,)).fetchone()
    if not row or row[0] != signature or row[2] != 'reviewed_committed' or review != ('committed',):
        raise ValueError('生产提交计划尚未依据最终证据核实提交，不能记为已报工。')
    snap = json.loads(row[1])['snapshot']
    if (p.digest(snap) != signature or snap['environment'] != p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')
            or snap['query_environment'] != p.CAMSTAR_QUERY_BASE_URL.rstrip('/')):
        raise ValueError('生产计划摘要或目标环境不一致。')
    return snap


def source_context(snap):
    if snap['service'] == 'ContainerDefect':
        return snap['context']
    if snap['service'] == 'Thruput':
        status = snap['status']
        def sid(ref):
            return p.identifier((ref or {}).get('InstanceId') or (ref or {}).get('InstanceID'))
        return {'container_id': snap['container_id'], 'container_name': status['Container']['Name'],
                'order_id': sid(status.get('MfgOrder')), 'order_name': status['MfgOrder'].get('Name'),
                'unit_id': p.identifier(snap['counters']['UOMId']), 'step_id': sid(status.get('Step')),
                'status_id': p.identifier(snap['counters']['CurrentStatusId']),
                'step_pass': p.sql_number(snap['counters'].get('StepPass'), '原加工步骤次数', integer=True)}
    context = snap.get('production_context')
    if not isinstance(context, dict):
        raise ValueError('报废计划缺少执行时核验的原工单上下文，须通过报工处置入口准备。')
    return context


@mcp.tool
async def record_production_report(report_key: str, good_qty: str, reject_qty: str,
        disposition: Literal['none', 'record', 'scrap'], throughput_plan_id: str, throughput_plan_digest: str,
        reject_plan_id: str | None = None, reject_plan_digest: str | None = None, comments: str = '') -> str:
    """保存本应用不可变良品/不良品报工台账，源于调用方明确声明，不伪称MES质量判定。
    须先核实本工序Thruput=良品+不良，再核实同容器同工序的不良登记或报废计划。
    record保留在制，scrap扣量；缺陷次数与不良件数分别保留。一个原生计划只能入账一次。
    相同report_key/内容幂等；不会再发送MES事务，也不修改工单状态或重复扣量。
    """
    try:
        key = text(report_key, '报工编号', 100)
        good, rejected = amount(good_qty, '良品数量'), amount(reject_qty, '不良数量')
        if good + rejected <= 0 or good + rejected > Decimal('1e12'):
            raise ValueError('本次总加工量须为正且不超过1e12。')
        if disposition not in ('none', 'record', 'scrap') or (rejected == 0) != (disposition == 'none'):
            raise ValueError('不良为零须选none；存在不良须明确record或scrap。')
        if bool(reject_plan_id) != bool(reject_plan_digest) or bool(reject_plan_id) != bool(rejected):
            raise ValueError('有不良必须绑定原处置计划ID和摘要，无不良不得绑定处置计划。')
        args = dict(good_qty=decimal_text(good), reject_qty=decimal_text(rejected), disposition=disposition,
            throughput_plan_id=text(throughput_plan_id, '加工量计划ID', 100), throughput_plan_digest=throughput_plan_digest,
            reject_plan_id=reject_plan_id, reject_plan_digest=reject_plan_digest, comments=comments)
        previous = existing('report', key, args)
        if previous:
            return raw_json(previous)
        throughput = reviewed_source('production', throughput_plan_id, throughput_plan_digest)
        if throughput['service'] != 'Thruput' or amount(throughput['qty'], '原加工量') != good + rejected:
            raise ValueError('须绑定本工序独立Thruput，且其加工量等于良品+不良；带移站计划不能替代。')
        context = source_context(throughput)
        sources = [('production', throughput_plan_id)]
        rejection = None
        if rejected:
            kind = 'quality' if disposition == 'scrap' else 'production'
            rejection = reviewed_source(kind, reject_plan_id, reject_plan_digest)
            if disposition == 'record':
                if (rejection['service'] != 'ContainerDefect' or amount(rejection['reject_qty'], '原不良数量') != rejected
                        or amount(rejection['inspected_qty'], '原检查数量') != good + rejected):
                    raise ValueError('登记计划类型或不良数量不符。')
            elif rejection.get('kind') != 'quantity_loss' or amount(rejection.get('expected_loss_quantity'), '原损失量') != rejected:
                raise ValueError('仅可绑定相同数量的原单容器LossDetails计划。')
            rcontext = source_context(rejection)
            if any(context.get(k) != value for k, value in rcontext.items()
                   if k in ('container_id', 'unit_id', 'step_id', 'order_id', 'status_id', 'step_pass')):
                raise ValueError('加工量与不良处置计划的容器、单位、工序或工单不一致。')
            sources.append((kind, reject_plan_id))
        payload = {'context': context, 'good_qty': decimal_text(good), 'reject_qty': decimal_text(rejected),
                   'processed_qty': decimal_text(good + rejected), 'disposition': disposition,
                   'quantity_deduction': disposition == 'scrap', 'comments': comments,
                   'quality_basis': 'operator_declared_not_native_quality_verdict',
                   'native_sources': [{'kind': k, 'plan_id': value} for k, value in sources]}
        return raw_json(save('report', key, args, payload, sources))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return raw_json({'saved': False, 'error': str(exc), 'mes_write_performed': False})


@mcp.tool
async def record_production_labor(entry_key: str, mfgorder_name: str, employee_name: str,
        start_time: str, end_time: str, actual_minutes: str, container_name: str | None = None,
        container_instance_id: str | None = None, comments: str = '') -> str:
    """保存本应用人工实报工时，不写MES TaskTime。明确员工、工单、带时区起止时间及实际分钟数。
    实际分钟须>0且<=区间经过时间；最长区间7天。同员工重叠区间拒绝重复人时，团队分别登记。
    可绑定明确容器并核对工单；相同entry_key与内容幂等，既有记录不可覆盖。
    自动进出站经过时间另查get_production_worktime，两项不相加，不把历时当人工劳动。
    """
    try:
        key = text(entry_key, '工时编号', 100)
        start, end = timestamp_ticks(start_time), timestamp_ticks(end_time)
        minutes = amount(actual_minutes, '人工实报分钟', True)
        if not 0 < end - start <= 7 * 86400 * 10000000 or minutes * 60 * 10000000 > end - start:
            raise ValueError('起止区间须为正且<=7天；人工分钟数不得超过区间经过时间。')
        if container_instance_id is not None and container_name is None:
            raise ValueError('指定容器ID时必须同时提供明确容器名。')
        args = dict(order_name=text(mfgorder_name, '工单名'), employee_name=text(employee_name, '员工名'),
                    start_ticks=start, end_ticks=end, actual_minutes=decimal_text(minutes),
                    container_name=container_name, container_instance_id=container_instance_id, comments=comments)
        previous = existing('labor', key, args)
        if previous:
            return raw_json(previous)
        order = await order_context(args['order_name'])
        worker = await ReadContext(max_requests=3).resolve('Employees', {'Name': args['employee_name']}, revisioned=False)
        if worker.get('IsFrozen') is not False:
            raise ValueError('员工必须明确未冻结。')
        employee = {'name': worker['Name'], 'instance_id': p.identifier(worker['InstanceID'])}
        context = None
        if container_name is not None:
            context, _, _ = await report_context(container_name, container_instance_id)
            if context['order_id'] != order['instance_id']:
                raise ValueError('工时容器不属于指定工单。')
        payload = {'order': order, 'employee': employee, 'context': context,
                   'start_time': start_time, 'end_time': end_time, 'start_ticks': start, 'end_ticks': end,
                   'actual_minutes': decimal_text(minutes), 'manual_labor_hours': str(minutes / 60),
                   'comments': comments, 'basis': 'operator_declared_actual_labor', 'native_task_time_written': False}
        return raw_json(save('labor', key, args, payload, interval=(employee['instance_id'], start, end)))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return raw_json({'saved': False, 'error': str(exc), 'mes_write_performed': False})


def ledger_records(kind, oid):
    if not LEDGER_DB.is_file():
        return []
    with connect() as db:
        rows = db.execute('SELECT id,entry_key,payload,digest FROM records WHERE environment=? AND kind=? ORDER BY created,id',
                          (environment(), kind)).fetchall()
    results = []
    for record_id, key, raw, signature in rows:
        payload = json.loads(raw)
        if p.digest(payload) != signature:
            raise ValueError('本地台账内容摘要损坏，停止统计。')
        bound = payload.get('context', {}).get('order_id') if kind == 'report' else payload.get('order', {}).get('instance_id')
        if bound == oid:
            results.append({'record_id': record_id, 'entry_key': key, **payload})
    return results


@mcp.tool
async def get_mfgorder_completion_readiness(mfgorder_name: str) -> str:
    """只读整单收尾核查：MES生产完成日期、后置过程、未完成在制和本应用未核实提交。
    返回ready_to_record_local_completion仅用于本应用收尾登记，不是MES关闭/移动授权。
    原生生产完成由产品过站规则触发，不修改OrderStatus、日期或数据库伪造完成。
    """
    out = {'complete': False, 'ready_to_record_local_completion': False, 'mes_write_performed': False,
           'native_close_executed': False, 'blockers': []}
    try:
        order = await order_context(mfgorder_name)
        oid = p.identifier(order['instance_id'])
        rows = await p.query_rows('SELECT MfgOrderId,MfgOrderName,Qty,ProductionCompleteDateGMT,'
            f"PostProductionProcedureId,PostProductionCompleteDateGMT FROM MfgOrder WHERE MfgOrderId='{oid}'")
        if len(rows) != 1 or rows[0].get('MfgOrderId') != oid or rows[0].get('MfgOrderName') != order['name']:
            raise ValueError('整单收尾的工单身份未唯一核实。')
        native = rows[0]
        for field in ('ProductionCompleteDateGMT', 'PostProductionCompleteDateGMT'):
            value = native.get(field)
            if value:
                # These fixed SQL columns explicitly store GMT, unlike user input.
                timestamp_ticks(value if value.endswith('Z') or '+' in value[10:] else value + 'Z')
        counts = await p.query_rows('SELECT COUNT(*) AS IncompleteActiveContainers FROM Container '
            f"WHERE MfgOrderId='{oid}' AND Status=1 AND (ProductionComplete IS NULL OR ProductionComplete=0)")
        if len(counts) != 1:
            raise ValueError('未完成在制数量未唯一返回。')
        incomplete = p.sql_number(counts[0].get('IncompleteActiveContainers'), '未完成在制数量', integer=True)
        if not native.get('ProductionCompleteDateGMT'):
            out['blockers'].append('MES尚未记录生产完成日期；须按原工艺完成生产，不能用Closed或任务状态替代。')
        if native.get('PostProductionProcedureId') and not native.get('PostProductionCompleteDateGMT'):
            out['blockers'].append('原工单已配置后置过程，但尚未记录其完成日期。')
        if incomplete:
            out['blockers'].append('工单仍有未完成的Active容器。')
        if p.PLAN_DB.is_file():
            with p.db_connect() as db:
                active = db.execute("SELECT id,payload FROM plans WHERE state IN ('unknown','in_flight')").fetchall()
            for key, payload in active:
                snap = json.loads(payload)['snapshot']
                if snap['environment'] != p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'):
                    continue
                ref = snap.get('status', {}).get('MfgOrder') or {}
                bound_order = snap.get('context', {}).get('order_id') or ref.get('InstanceId') or ref.get('InstanceID')
                if bound_order == oid:
                    out['blockers'].append('本应用仍有未核实的生产提交：' + key)
        from tools import quality_plans
        if quality_plans.PLAN_DB.is_file():
            with quality_plans.db_connect() as db:
                active = db.execute("SELECT id,payload FROM plans WHERE state IN ('unknown','in_flight')").fetchall()
            for key, payload in active:
                snap = json.loads(payload)
                if snap.get('environment') == quality_plans.environment() and (snap.get('production_context') or {}).get('order_id') == oid:
                    out['blockers'].append('本应用仍有未核实的报废提交：' + key)
        out.update(complete=True, order=order, native_completion=native, incomplete_active_containers=incomplete,
            recorded_local_completions=ledger_records('completion', oid),
            ready_to_record_local_completion=not out['blockers'],
            scope='native_completion_dates_and_current_WIP_plus_application_reporting_attempts',
            warnings=['只核对本应用报工提交；其他客户端和非报工事务不属于完整排他锁。跨查询不是原子快照。'])
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        out['error'] = str(exc)
    return raw_json(out)


@mcp.tool
async def record_mfgorder_completion(completion_key: str, mfgorder_name: str, comments: str = '') -> str:
    """在MES实际生产完成且原后置过程完成后，保存本应用不可变整单收尾记录。
    再核对未完成在制和本应用未核实提交；本工具不关闭MES工单，不改状态或日期。
    相同编号/内容幂等，同一原生完成日期只能收尾一次；未满足前置时不保存。
    """
    try:
        key = text(completion_key, '收尾编号', 100)
        args = {'mfgorder_name': text(mfgorder_name, '工单名'), 'comments': comments}
        previous = existing('completion', key, args)
        if previous:
            return raw_json(previous)
        readiness = json.loads(await get_mfgorder_completion_readiness(args['mfgorder_name']))
        if readiness.get('complete') is not True or readiness.get('ready_to_record_local_completion') is not True:
            return raw_json({'saved': False, 'readiness': readiness, 'mes_write_performed': False})
        native = readiness['native_completion']
        source = readiness['order']['instance_id'] + ':' + native['ProductionCompleteDateGMT']
        payload = {'order': readiness['order'], 'native_completion': native, 'comments': comments,
                   'native_close_executed': False, 'scope': 'application_order_closeout_record'}
        return raw_json(save('completion', key, args, payload, [('native_order_completion', source)]))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return raw_json({'saved': False, 'error': str(exc), 'mes_write_performed': False})


ELAPSED_CTE = ("WITH intervals AS (SELECT h.ContainerId,h.TxnId,COUNT(*) AS NativeRows,"
    "MIN(CASE WHEN h.ReversalStatus=1 AND h.TxnId IS NOT NULL AND LEN(h.TxnId)=16 "
    "AND h.MoveInDateGMT IS NOT NULL AND h.TxnDateGMT IS NOT NULL "
    "AND h.TxnDateGMT>=h.MoveInDateGMT THEN 1 ELSE 0 END) AS ValidInterval,"
    "MAX(DATEDIFF_BIG(MICROSECOND,h.MoveInDateGMT,h.TxnDateGMT)) AS ElapsedMicroseconds "
    "FROM HistoryMainline h JOIN Container c ON c.ContainerId=h.ContainerId "
    "WHERE h.TxnTypeName='MoveOut' AND c.MfgOrderId='{oid}' GROUP BY h.ContainerId,h.TxnId) ")


@mcp.tool
async def get_production_worktime(mfgorder_name: str, offset: int = 0, limit: int = 20) -> str:
    """分别统计人工实报（本应用台账）与MES进出站历时。人工分钟不与自动历时相加。
    自动口径仅直接MoveOut主历史，GMT MoveInDateGMT至TxnDateGMT，正常未冲销状态1，
    同容器同事务必须唯一；缺日期、负时长、重复/冲销分别排除并报告。无历史不证明没生产。
    自动统计按当前工单关联容器；人工按登记时工单归属。人工记录分页，全量总计单独返回。
    """
    out = {'complete': False, 'mes_write_performed': False, 'combined_hours': None}
    try:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('offset>=0，limit=1..100。')
        order = await order_context(mfgorder_name)
        labor = ledger_records('labor', order['instance_id'])
        total = sum((Decimal(r['actual_minutes']) for r in labor), Decimal(0))
        out['manual'] = {'source': 'application_production_ledger', 'basis': 'actual_person_labor',
            'total_minutes': decimal_text(total), 'total_hours': str(total / 60), 'record_count': len(labor),
            'records': labor[offset:offset + limit], 'page': {'offset': offset, 'limit': limit,
                'has_more': offset + limit < len(labor)}}
        sql = ELAPSED_CTE.format(oid=p.identifier(order['instance_id'])) + (
            'SELECT COUNT(*) AS IntervalCount,COALESCE(SUM(CASE WHEN NativeRows=1 AND ValidInterval=1 '
            'THEN ElapsedMicroseconds ELSE 0 END),0) AS ElapsedMicroseconds,'
            'COALESCE(SUM(CASE WHEN NativeRows=1 AND ValidInterval=1 THEN 1 ELSE 0 END),0) AS IncludedCount,'
            'COALESCE(SUM(CASE WHEN NativeRows<>1 OR ValidInterval<>1 THEN 1 ELSE 0 END),0) AS ExcludedCount FROM intervals')
        rows = await p.query_rows(sql)
        if len(rows) != 1:
            raise ValueError('自动耗时汇总未唯一返回。')
        totals = {key: p.sql_number(rows[0].get(key), key, integer=True) for key in
                  ('IntervalCount', 'ElapsedMicroseconds', 'IncludedCount', 'ExcludedCount')}
        if totals['IntervalCount'] != totals['IncludedCount'] + totals['ExcludedCount']:
            raise ValueError('自动耗时包含/排除计数不一致。')
        out.update(order=order, complete=True, automatic={'source': 'MES HistoryMainline MoveOut',
            'basis': 'elapsed_time_not_person_labor', **totals,
            'recorded_elapsed_hours': str(Decimal(totals['ElapsedMicroseconds']) / Decimal('3600000000')),
            'has_recorded_intervals': totals['IntervalCount'] > 0,
            'coverage_complete': totals['ExcludedCount'] == 0, 'scope': 'currently_order_linked_containers'},
            warnings=['缺失的进出站区间不能推断为零工时。直接MoveStd或无MoveIn的带吞吐移站不计为已核实加工历时。',
                      '历时包含等待/暂停可能性，不乘人员数，不计作人工工时。跨查询不是一致性快照。'])
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        out['error'] = str(exc)
    return raw_json(out)


@mcp.tool
async def get_production_reports(mfgorder_name: str, offset: int = 0, limit: int = 20) -> str:
    """查询本应用报工台账，按实际工序与单位分别统计良品、不良、登记/报废数量。
    本地operator_declared口径，不伪称MES质量判定，不跨工序重复累计为工单完成数量。
    """
    try:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('offset>=0，limit=1..100。')
        order = await order_context(mfgorder_name)
        rows = ledger_records('report', order['instance_id'])
        groups = {}
        for row in rows:
            context = row['context']
            key = (context['step_id'], context['unit_id'])
            group = groups.setdefault(key, {'good_qty': Decimal(0), 'recorded_reject_qty': Decimal(0),
                                           'scrapped_reject_qty': Decimal(0), 'report_count': 0})
            group['good_qty'] += Decimal(row['good_qty'])
            if row['disposition'] != 'none':
                group['recorded_reject_qty' if row['disposition'] == 'record' else 'scrapped_reject_qty'] += Decimal(row['reject_qty'])
            group['report_count'] += 1
        for group in groups.values():
            group['reject_qty'] = group['recorded_reject_qty'] + group['scrapped_reject_qty']
            group['processed_qty'] = group['good_qty'] + group['reject_qty']
        return raw_json({'complete': True, 'source': 'application_production_ledger', 'order': order,
            'summary_by_step_and_unit': [{'step_id': key[0], 'unit_id': key[1], **{
                k: decimal_text(v) if isinstance(v, Decimal) else v for k, v in group.items()}}
                for key, group in sorted(groups.items())], 'rows': rows[offset:offset + limit],
            'page': {'offset': offset, 'limit': limit, 'total_count': len(rows), 'has_more': offset + limit < len(rows)},
            'order_completed_qty': None, 'mes_write_performed': False})
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return raw_json({'complete': False, 'error': str(exc)})
