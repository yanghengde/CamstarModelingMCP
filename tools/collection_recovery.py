"""Local audited CollectData outcome review; never infer success from history."""
import json
import re
import time
import uuid
from typing import Literal

from tools import mcp
from tools.boms import encode
from tools import collection_submission as plans
from core.transfer_attempt_lock import attempt_lock, AttemptBusy


def checked_snapshot(row, plan_digest):
    snap = json.loads(row[1])['snapshot']
    if plans.digest(snap) != plan_digest or snap['environment'] != plans.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'):
        raise ValueError('计划内容或环境不一致。')
    ref = snap['body']['container']
    if not isinstance(ref.get('name'),str) or not ref['name'].strip() or not re.fullmatch('[0-9a-fA-F]{16}',ref.get('instanceId','')):
        raise ValueError('存储计划容器身份不完整。')
    return snap


def review_summary(plan_id, plan_digest, action, evidence_reference, review_note, conclusion=None):
    with plans.db_connect() as db:
        row = db.execute('SELECT digest,payload,state FROM plans WHERE id=?',(plan_id,)).fetchone()
    expected = 'in_flight' if action == 'recover' else 'unknown'
    if not row or row[0] != plan_digest or row[2] != expected:
        return '采集计划状态或摘要不符，不应继续。'
    snap = checked_snapshot(row, plan_digest)
    return ('采集计划本地管理：\n' + json.dumps({'action':action,'request':snap['body'],'conclusion':conclusion,
            'evidence_reference':evidence_reference,'review_note':review_note},ensure_ascii=False)
            + ('\n只将无本地执行者的in_flight转unknown，保留容器占用；不证明服务端结束。'
               if action == 'recover' else '\n须人工核对服务端最终事务/审计证据。仅解除此计划占用；其他未知计划占用保留，原计划不可重用。')
            + '\n当前相同值或空历史不能证明本次结果，不是自动核实，不调用MES、不重试。'
              '\n旧版无尝试记录的unknown只支持人工最终证据核实，旧版in_flight拒绝在线恢复。'
              '\n仅同机数据库、完整锁文件和升级后的执行进程适用。')


def review(plan_id, plan_digest, action, evidence_reference, review_note, conclusion=None):
    if (not isinstance(evidence_reference,str) or not 1<=len(evidence_reference.strip())<=1000
            or not isinstance(review_note,str) or not 1<=len(review_note.strip())<=4000
            or action=='resolve' and conclusion not in ('committed','not_committed')):
        return encode({'state_changed':False,'error':'必须提供明确人工结论、证据引用和说明。'})
    try:
        with attempt_lock(plans.PLAN_DB, plan_id):
            with plans.db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT digest,payload,state,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
                expected = 'in_flight' if action=='recover' else 'unknown'
                if not row or row[0]!=plan_digest or row[2]!=expected:
                    raise ValueError('计划状态/摘要不符，拒绝重复核实或重开计划。')
                snap = checked_snapshot(row, plan_digest)
                attempt = db.execute('SELECT attempt_id,started,finished FROM collection_attempts WHERE plan_id=?',(plan_id,)).fetchone()
                if action=='recover' and not attempt:
                    raise ValueError('旧版in_flight无进程互斥记录，拒绝在线恢复；须离线运维。')
                record = {'review_id':uuid.uuid4().hex,'plan_id':plan_id,'plan_digest':plan_digest,'action':action,
                          'attempt_id':attempt[0] if attempt else None,'attempt_started':attempt[1] if attempt else None,
                          'attempt_finished':attempt[2] if attempt else None,
                          'attempt_tracking':'guarded' if attempt else 'legacy_unknown_without_tracking',
                          'container':snap['body']['container'],'previous_state':row[2],
                          'previous_outcome':json.loads(row[3]) if row[3] else None,'conclusion':conclusion,
                          'evidence_reference':evidence_reference.strip(),'review_note':review_note.strip(),
                          'created':time.time(),'automatically_verified':False,
                          'verification_method':'human_attestation' if action=='resolve' else 'local_process_lock_and_operator_review'}
                state = 'unknown' if action=='recover' else 'manually_confirmed_'+conclusion
                outcome = {'state':state,'review':record,'verified_success':False,'retry_allowed':False,
                           'locks_released':action=='resolve','original_plan_reusable':False,
                           'note':'人工结论不是自动验证；恢复不证明服务端结束。原计划不可重试。'}
                db.execute('INSERT INTO collection_reviews VALUES (?,?,?,?)',(plan_id,action,json.dumps(record,ensure_ascii=False),record['created']))
                db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',(state,json.dumps(outcome,ensure_ascii=False),plan_id))
        return encode({'state_changed':True,'mes_write_performed':False,**outcome})
    except (AttemptBusy,ValueError,TypeError,KeyError,AttributeError) as error:
        return encode({'state_changed':False,'error':str(error)})


@mcp.tool
async def recover_collection_submission_attempt(plan_id: str, plan_digest: str, evidence_reference: str, review_note: str) -> str:
    """经运维确认取得本机尝试锁，将有记录且无本地执行者的in_flight转unknown，审计保存且保留容器占用。旧版无记录拒绝恢复。不证明服务端结束，不调用MES、不重试；直接MCP须同等人工审批。"""
    return review(plan_id,plan_digest,'recover',evidence_reference,review_note)


@mcp.tool
async def resolve_collection_submission_outcome(plan_id: str, plan_digest: str,
        conclusion: Literal['committed','not_committed'], evidence_reference: str, review_note: str) -> str:
    """仅人工核对服务端最终事务/审计证据并确认后，保存unknown采集计划的人工结论、解除此计划占用。非自动验证，当前相同值/空历史不够，不重开或重试原计划。旧版unknown可审计核实；in_flight须先安全恢复或离线处理。直接MCP须同等审批。"""
    return review(plan_id,plan_digest,'resolve',evidence_reference,review_note,conclusion)
