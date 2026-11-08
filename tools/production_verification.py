"""Attribute a single attempted production write to its native committed history."""
import json
import re
import time
from decimal import Decimal

from tools import mcp
from tools import production_reporting as p
from tools.modeling_details import raw_json
from core.transfer_attempt_lock import attempt_lock, AttemptBusy


def reference_id(ref):
    return p.identifier((ref or {}).get('InstanceId') or (ref or {}).get('InstanceID'))


def validate_plan(row, signature):
    if not row or row[0] != signature or row[2] not in ('unknown', 'history_committed'):
        raise ValueError('仅核实已经尝试且本机执行已结束的unknown计划；不核实ready/in_flight或人工终态。')
    data = json.loads(row[1]); snap = data['snapshot']
    if (p.digest(snap) != signature or snap['environment'] != p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')
            or snap['query_environment'] != p.CAMSTAR_QUERY_BASE_URL.rstrip('/')):
        raise ValueError('原计划内容或环境不符。')
    marker = snap.get('receipt_marker')
    if not isinstance(marker, str) or not re.fullmatch(r'MCP-PROD:[0-9a-f]{32}', marker):
        raise ValueError('旧计划没有唯一事务标记；须核对最终证据并人工审计。')
    if snap['body'].get('comments') != p.receipt_comments(data['arguments']):
        raise ValueError('原请求评论与事务标记不符。')
    outcome = json.loads(row[3]) if row[3] else {}
    if outcome.get('submitted') is not True:
        raise ValueError('没有本机实际提交尝试的持久证据。')
    if snap.get('service') not in ('Thruput', 'ContainerDefect', 'MoveIn', 'MoveStd'):
        raise ValueError('此原生服务的自动归因尚未核实；须人工核对最终事务。')
    return snap, outcome


async def history_receipt(snap):
    cid = p.identifier(snap['container_id'])
    comments = snap['body']['comments'].replace("'", "''")
    headers = await p.query_rows('SELECT TOP 3 HistoryMainlineId,HistoryId,TxnId,ContainerId,ContainerName,'
        'TxnTypeName,TxnServiceName,TxnParentTypeName,WorkflowStepId,StepPass,ReversalStatus,Comments,ResourceId '
        f"FROM HistoryMainline WHERE ContainerId='{cid}' AND Comments=N'{comments}' ORDER BY HistoryMainlineId")
    if len(headers) != 1:
        raise ValueError('唯一标记尚未对应一条完整原生主历史；空记录不表示提交失败。')
    h = headers[0]
    if not {'TxnParentTypeName','WorkflowStepId','StepPass','ReversalStatus','Comments'} <= h.keys():
        raise ValueError('原生主历史字段不完整。')
    for field in ('HistoryMainlineId','HistoryId','TxnId'):
        p.identifier(h.get(field))
    status = snap.get('status', {})
    context = snap.get('context') or {}
    step = context.get('step_id') or reference_id(status.get('Step'))
    step_pass = context.get('step_pass') if context else p.sql_number(
        snap['counters'].get('StepPass', snap['counters'].get('CurrentStepPass')), '原步骤次数', integer=True)
    if (h.get('ContainerId') != cid or h.get('ContainerName') != snap['body']['container']['name']
            or h.get('Comments') != snap['body']['comments'] or h.get('TxnTypeName') != snap['service']
            or h.get('TxnServiceName') != snap['service'] or h.get('TxnParentTypeName')
            or h.get('WorkflowStepId') != step
            or p.sql_number(h.get('StepPass'), '历史步骤次数', integer=True) != step_pass
            or p.sql_number(h.get('ReversalStatus'), '历史冲销状态', integer=True) != 1):
        raise ValueError('原生主历史的身份、服务、工序、步骤次数或正常状态与原请求不符。')
    resource=snap['body'].get('resource')
    if resource and h.get('ResourceId') != resource['instanceId']:
        raise ValueError('原生实际资源与明确指定资源不符。')
    mid, txn, hid = h['HistoryMainlineId'], h['TxnId'], h['HistoryId']
    if any(r.get('TxnId') == txn for r in snap.get('history_baseline', [])):
        raise ValueError('匹配事务已存在于提交前基线中。')
    if snap['service'] == 'Thruput':
        rows = await p.query_rows('SELECT TOP 3 ThruputHistoryId,HistoryMainlineId,HistoryId,TxnId,ContainerId,'
            f"OperationId,UOMId,Qty FROM ThruputHistory WHERE HistoryMainlineId='{mid}' ORDER BY ThruputHistoryId")
        if len(rows) != 1:
            raise ValueError('吞吐持久汇总不存在或不唯一。')
        detail = rows[0]
        p.identifier(detail.get('ThruputHistoryId'))
        if (detail.get('ContainerId') != cid or detail.get('OperationId') != p.identifier(snap['operation']['InstanceID'])
                or detail.get('UOMId') != p.identifier(snap['counters']['UOMId'])
                or Decimal(str(p.sql_number(detail.get('Qty'), '实际吞吐量'))) != Decimal(str(snap['qty']))):
            raise ValueError('持久吞吐数量、单位或工序与原计划不符。')
        details = []
    elif snap['service'] == 'ContainerDefect':
        rows = await p.query_rows('SELECT TOP 3 DefectHistoryId,HistoryMainlineId,HistoryId,TxnId,QtyInspected '
            f"FROM DefectHistory WHERE HistoryMainlineId='{mid}' ORDER BY DefectHistoryId")
        if len(rows) != 1:
            raise ValueError('缺陷持久汇总不存在或不唯一。')
        detail = rows[0]
        did = p.identifier(detail.get('DefectHistoryId'))
        if Decimal(str(p.sql_number(detail.get('QtyInspected'), '实际检查数量'))) != Decimal(snap['inspected_qty']):
            raise ValueError('持久检查数量不符。')
        details = await p.query_rows('SELECT TOP 3 ContainerDefectHistoryDetailId,DefectHistoryId,HistoryId,TxnId,'
            f"ContainerId,DefectCount,ReasonCodeId FROM ContainerDefectHistoryDetail WHERE DefectHistoryId='{did}' "
            'ORDER BY ContainerDefectHistoryDetailId')
        expected = snap['body']['serviceDetails'][0]
        if len(details) != 1:
            raise ValueError('缺陷明细不存在或超出原计划范围。')
        d = details[0]; p.identifier(d.get('ContainerDefectHistoryDetailId'))
        if (d.get('ContainerId') != cid or d.get('DefectHistoryId') != did
                or d.get('ReasonCodeId') != expected['reasonCode']['instanceId']
                or p.sql_number(d.get('DefectCount'), '实际缺陷次数', integer=True) != expected['defectCount']
                or d.get('TxnId') != txn or d.get('HistoryId') != hid):
            raise ValueError('缺陷明细因果关联、原因或次数不符。')
    else:
        table='MoveInHistory' if snap['service']=='MoveIn' else 'MoveHistory'
        extra='' if table=='MoveInHistory' else ',StepId,ToStepId,PathId,CloseFlag,MoveAllQty'
        rows=await p.query_rows('SELECT TOP 3 '+table+'Id,HistoryMainlineId,HistoryId,TxnId,Qty,UOMId'+extra+
            f" FROM {table} WHERE HistoryMainlineId='{mid}' ORDER BY {table}Id")
        if len(rows)!=1:raise ValueError('原生移动汇总不存在或不唯一。')
        detail=rows[0];p.identifier(detail.get(table+'Id'))
        if (detail.get('UOMId')!=snap['counters'].get('UOMId')
                or Decimal(str(p.sql_number(detail.get('Qty'),'移动数量'))) != Decimal(str(snap['counters']['Qty']))):
            raise ValueError('移动持久数量或单位不符。')
        if table=='MoveHistory':
            path=snap.get('path') or {}
            if (detail.get('StepId') != step or (detail.get('PathId') or None)!=(path.get('PathId') or None)
                    or path and detail.get('ToStepId')!=path.get('ToStepId')
                    or p.sql_boolean(detail.get('CloseFlag'),'关闭标志')!=snap['body']['close']
                    or p.sql_boolean(detail.get('MoveAllQty'),'移动全部标志') is not True):
                raise ValueError('原生移站步骤、目标、路径或关闭标志不符。')
        details=[]
    if any(detail.get(k) != v for k,v in [('HistoryMainlineId',mid),('TxnId',txn),('HistoryId',hid)]):
        raise ValueError('持久汇总与原生主历史不属于同一事务。')
    return {'verified':True,'source':'MES committed native history','header':h,'summary':detail,'details':details,
            'snapshot_digest':p.digest(snap),'receipt_marker':snap['receipt_marker']}


@mcp.tool
async def verify_production_transaction(plan_id: str, plan_digest: str) -> str:
    """只读MES核实已尝试的Thruput/ContainerDefect/MoveIn/MoveStd，成功才保存本机history_committed终态并释放占用。
    核对原计划唯一评论、事务主历史、明细因果键、数量/单位/原因及工序/步骤次数。
    证据缺失保持unknown，不发送生产写入、不重试、不把HTTP成功或当前相同数量当作本次成功。
    in_flight先核对本机执行是否结束；无唯一标记旧计划及组合服务沿用最终证据人工审计。
    """
    try:
        with attempt_lock(p.PLAN_DB, plan_id):
            with p.db_connect() as db:
                row = db.execute('SELECT digest,payload,state,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
            snap,outcome = validate_plan(row,plan_digest)
            # A fresh read also detects later reversal of a previously verified receipt.
            receipt = await history_receipt(snap)
            if await history_receipt(snap) != receipt:
                raise ValueError('核实期间历史发生变化，未更新终态。')
            with p.db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT digest,payload,state,outcome FROM plans WHERE id=?',(plan_id,)).fetchone() != row:
                    raise ValueError('核实期间本机计划发生变化。')
                outcome.update(native_receipt=receipt,transaction_verified=True)
                db.execute("UPDATE plans SET state='history_committed',outcome=? WHERE id=?",(raw_json(outcome),plan_id))
            return raw_json({'plan_id':plan_id,'state':'history_committed','transaction_verified':True,
                'writes_executed':False,'replay_allowed':False,'receipt':receipt})
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy) as exc:
        return raw_json({'plan_id':plan_id,'transaction_verified':False,'writes_executed':False,
            'replay_allowed':False,'error':str(exc)})
