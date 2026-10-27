"""Immutable mode-2/5 issues with shared participants and audited outcomes."""
import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from tools import mcp
from tools.boms import encode
from tools.component_issue import get_component_requirement, get_component_issue_history
from tools.component_issue_payload import build_quantity_issue, verify_quantity_echo, preview_params, reference, quantity, quantity_context
from tools.container_inquiry import get_container_details, get_container_history
from tools.modeling_details import raw_json
from core.http_client import request
from core.transfer_attempt_lock import attempt_lock, AttemptBusy
from config import CAMSTAR_SHOPFLOOR_BASE_URL

PLAN_DB = Path(__file__).resolve().parents[1]/'data/component_issue_plans.sqlite3'
CONTEXT_FIELDS = ('Container','Product','MfgOrder','Spec','Operation','Step','UOM',
                 'UOM2','Qty','Qty2','Status','InProcess','InRework','LastActivityDate','LastMoveDate','ContainerLevel')
TRANSIENT_FIELDS = ('InstanceID', 'Parent', 'HistoryDetail', 'SmartBarcode', 'DisplayName')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def db_connect():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB)
    db.execute('CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,digest TEXT,payload TEXT,state TEXT,created REAL,outcome TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS attempts(plan_id TEXT PRIMARY KEY,attempt_id TEXT,started REAL,finished REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS reviews(plan_id TEXT,kind TEXT,record TEXT,PRIMARY KEY(plan_id,kind))')
    db.execute('CREATE TABLE IF NOT EXISTS receipt_checks(id TEXT PRIMARY KEY,plan_id TEXT,record TEXT,created REAL)')
    return db


def issue_mode(snapshot):
    mode = snapshot.get('issue_control', 5)  # Preserve immutable pre-upgrade mode-5 snapshots.
    if type(mode) is not int or mode not in (2, 5):
        raise ValueError('计划追踪模式不支持。')
    return mode


def participants(snapshot):
    """Read every source/target from stored unresolved requests; fail closed."""
    body = snapshot['body']
    references = [body['container']]
    for line in body.get('serviceDetails', []):
        references.extend(a['fromContainer'] for a in line.get('actuals', []) if 'fromContainer' in a)
    references.extend(a['fromContainer'] for a in body.get('issueActualDetails', []) if 'fromContainer' in a)
    identifiers = set()
    for ref in references:
        value = ref['instanceId']
        if not isinstance(value, str) or not re.fullmatch('[0-9a-fA-F]{16}', value):
            raise ValueError('存储计划参与容器身份不完整，不能绕过占用。')
        identifiers.add(value.lower())
    return identifiers


def bulk_requirement(snapshot):
    return {'complete': True, 'status': 'matched', 'data': {
        'ParentContainer': snapshot['context']['Container'],
        'ObjectName': snapshot['source_context']['Container']['Name'], 'IssueDetails': snapshot['constraints']}}


def bulk_reconstructed(snapshot):
    from tools.component_issue_bulk import build_preview, derived_request, checked_status
    args = snapshot['arguments']
    for key, name_key, id_key in (('context', 'container_name', 'container_instance_id'),
                                   ('source_context', 'source_container_name', 'source_container_instance_id')):
        checked_status({'complete': True, 'data': {'ContainerStatusDetails': snapshot[key]}}, args[name_key], args[id_key])
    body, balance = build_preview(snapshot['context'], snapshot['source_context'], bulk_requirement(snapshot),
                                 snapshot['source_balance'], [], args['qty'])
    return derived_request(body), balance


def plan_quantities(snapshot):
    if issue_mode(snapshot) == 5:
        return quantity_context(snapshot)
    body, balance = bulk_reconstructed(snapshot)
    return {'scope': 'bulk_source_first_issue', 'source': body['serviceDetails'][0]['actuals'][0]['fromContainer'],
            'target': body['container'], 'required': str(quantity(snapshot['constraints']['QtyRequired'])),
            'net_required_before': str(quantity(snapshot['constraints']['NetQtyRequired'])),
            'quantity_this_request': balance['quantity_this_request'],
            'source_available_before': balance['available_observed'],
            'projected_source_quantity': balance['projected_source_quantity'], 'projected_net_required': '0',
            'note': '预测不是库存预留；提交结果须核实同一事务的批量来源历史。'}


async def bulk_preview(container_name, source_container_name, product_name, product_revision, qty,
                       container_instance_id=None, source_container_instance_id=None):
    from tools.component_issue_bulk import preview_bulk_component_issue
    result = json.loads(await preview_bulk_component_issue(container_name, source_container_name, product_name,
        product_revision, qty, container_instance_id, source_container_instance_id, verify_serialization=True))
    if result.get('source_checks_passed') is not True or result.get('serialization_verified') is not True or result.get('error'):
        raise ValueError(result.get('error') or '模式2来源校验/派生类型回显未通过。')
    body = result['request_preview']; line = body['serviceDetails'][0]; source = line['actuals'][0]['fromContainer']
    snap = {'issue_control': 2, 'environment': CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'), 'body': body,
            'context': {k: result['target_status']['data']['ContainerStatusDetails'].get(k) for k in CONTEXT_FIELDS},
            'source_context': {k: result['source_status']['data']['ContainerStatusDetails'].get(k) for k in CONTEXT_FIELDS},
            'source_balance': result['source_balance'],
            'constraints': {k: v for k, v in result['requirement']['data']['IssueDetails'].items() if k not in TRANSIENT_FIELDS},
            'arguments': {'container_name': body['container']['name'], 'container_instance_id': body['container']['instanceId'],
                          'source_container_name': source['name'], 'source_container_instance_id': source['instanceId'],
                          'product_name': line['product']['name'], 'product_revision': line['product']['revision'], 'qty': qty}}
    rebuilt, _ = bulk_reconstructed(snap)
    if (rebuilt != body or body['container']['name'] != container_name.strip() or source['name'] != source_container_name.strip()
            or line['product']['name'] != product_name.strip() or line['product']['revision'] != product_revision.strip()
            or container_instance_id is not None and body['container']['instanceId'] != container_instance_id
            or source_container_instance_id is not None and source['instanceId'] != source_container_instance_id):
        raise ValueError('模式2请求与双方状态/来源余额/需求不一致。')
    return snap


async def preview(container_name, product_name, product_revision, qty, container_instance_id=None, issue_difference_reason=''):
    if not all(isinstance(v, str) and v.strip() for v in (container_name, product_name, product_revision)):
        raise ValueError('明确容器、产品名称和固定版本必填。')
    if quantity(qty) <= 0:
        raise ValueError('本次数量须为有限正数。')
    if not isinstance(issue_difference_reason, str):
        raise ValueError('差异原因须为明确名称。')
    status = json.loads(await get_container_details(container_name, container_instance_id))
    current = status.get('data', {}).get('ContainerStatusDetails', {})
    target = reference(current.get('Container'))
    if (status.get('complete') is not True or target['name'] != container_name.strip()
            or container_instance_id is not None and target['instanceId'] != container_instance_id
            or type(current.get('Status')) is not int or current.get('Status') != 1 or current.get('InProcess') is not True):
        raise ValueError('须核对明确活动、在制容器身份；当前状态不满足受限投料计划。')
    order = reference(current.get('MfgOrder'))
    spec = reference(current.get('Spec'), revisioned=True)
    reference(current.get('Operation')); reference(current.get('Step'))
    requirement = json.loads(await get_component_requirement(target['name'], product_name.strip(), target['instanceId']))
    reason = None
    if issue_difference_reason.strip():
        from tools.component_issue_reasons import selections, choose
        reason = choose(await selections(requirement, qty), issue_difference_reason.strip())
    body = build_quantity_issue(requirement, qty, reason)
    line = body['serviceDetails'][0]
    if (body['container'] != target or line['product']['name'] != product_name.strip()
            or line['product']['revision'] != product_revision.strip()
            or line['bomLineItem']['parent']['instanceId'] != order['instanceId']):
        raise ValueError('目标、固定产品版本或需求行所属工单不匹配；不支持替代料/BOM跨对象推断。')
    # Query IssueDetails.InstanceID is ephemeral. Bind business rules, never that ID.
    constraints = {k: v for k, v in requirement['data']['IssueDetails'].items()
                   if k not in ('InstanceID', 'Parent', 'HistoryDetail', 'SmartBarcode', 'DisplayName')}
    context = {key: current.get(key) for key in ('Container','Product','MfgOrder','Spec','Operation','Step','UOM',
               'UOM2','Qty','Qty2','Status','InProcess','InRework','LastActivityDate','LastMoveDate','ContainerLevel')}
    snapshot = {'environment': CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'), 'body': body,
                'context': context, 'constraints': constraints,
                'arguments': {'container_name': target['name'], 'container_instance_id': target['instanceId'],
                              'product_name': line['product']['name'], 'product_revision': line['product']['revision'], 'qty': qty}}
    if reason is not None:
        snapshot['difference_reason'] = reason
        snapshot['arguments']['issue_difference_reason'] = reason['Name']
    raw = await request('POST', '/api/ComponentIssue', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                        body=body, params=preview_params(body), response_formatter=raw_json)
    verify_quantity_echo(json.loads(raw.split('Response: ', 1)[-1]), body)
    return snapshot


def checked(row, expected_digest, plan_id):
    if not row or row[0] != expected_digest:
        raise ValueError('计划不存在或摘要不匹配。')
    payload = json.loads(row[1]); snap = payload['snapshot']; args = snap['arguments']
    if digest(snap) != expected_digest or snap['environment'] != CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'):
        raise ValueError('存储计划摘要或环境不一致。')
    target = reference(snap['context']['Container'])
    line = snap['body']['serviceDetails'][0]
    expected_args = {'container_name': target['name'], 'container_instance_id': target['instanceId'],
                     'product_name': line['product']['name'], 'product_revision': line['product']['revision'],
                     'qty': line['actualQtyIssued']}
    if issue_mode(snap) == 2:
        source = reference(snap['source_context']['Container'])
        expected_args.update(source_container_name=source['name'], source_container_instance_id=source['instanceId'])
        rebuilt, _ = bulk_reconstructed(snap)
        if snap.get('difference_reason') is not None or rebuilt != snap['body']:
            raise ValueError('模式2计划必须是无额外字段的单组派生来源实际量。')
    else:
        rebuilt = build_quantity_issue({'complete': True, 'status': 'matched', 'data': {
            'ParentContainer': snap['context']['Container'], 'IssueDetails': snap['constraints']}},
            line['actualQtyIssued'], snap.get('difference_reason'))
        if rebuilt != snap['body']:
            raise ValueError('模式5存储请求超出既有仅数量契约。')
    if snap.get('difference_reason') is not None:
        reason = reference(snap['difference_reason'])
        if reason != line.get('issueDifferenceReason') or reason != line['actuals'][0].get('issueDifferenceReason'):
            raise ValueError('原差异原因与两层请求不一致。')
        expected_args['issue_difference_reason'] = reason['name']
    if args != expected_args or snap['body']['container'] != target:
        raise ValueError('重读参数与原投料请求不匹配。')
    if (payload.get('plan_id') != plan_id or not isinstance(payload.get('plan_id'), str) or not re.fullmatch('[0-9a-f]{32}', payload['plan_id'])
            or payload.get('marker') != 'Codex issue '+payload['plan_id'] or snap.get('marker') != payload['marker']):
        raise ValueError('计划唯一审计标记不匹配。')
    return payload


def confirmation_summary(plan_id, plan_digest):
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
    payload = checked(row, plan_digest, plan_id)
    if row[2] != 'ready' or time.time()-row[3] > 3600:
        raise ValueError('计划已使用或过期。')
    bulk = issue_mode(payload['snapshot']) == 2
    label = '确认模式2单次来源扣减投料：' if bulk else '确认单次仅数量投料：'
    limits = ('将扣减上述来源数量；执行中/未知结果同时占用源与目标。仅首次完整需求，无部分量/补投/第二数量。'
              if bulk else '不扣来源库存；本次数量不得超剩余需求，小于原需求量须明确差异原因。')
    return label+json.dumps({'quantity_context': plan_quantities(payload['snapshot']),
        'request': {**payload['snapshot']['body'], 'comments': payload['marker']}}, ensure_ascii=False)+'\n'+limits+'不支持退料净额/替代料。结果不明不重试。'


@mcp.tool
async def prepare_component_issue(container_name: str, product_name: str, product_revision: str,
                                  qty: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)],
                                  container_instance_id: str | None = None, issue_difference_reason: str = '') -> str:
    """准备模式5单次增量投料/补投计划，不执行事务；固定产品版本、本次数量为有限正数且不超净剩余，要求需求=累计已投+净剩余、待处理实际量及第二数量全0。本次数量小于原需求时须明确issue_difference_reason并重查选择值。确认展示累计量/净需求/本次量，绝不回填累计量；不支持退料净额/替代料/来源扣减。保存1小时单次计划，执行须确认。"""
    try:
        snap = await asyncio.wait_for(preview(container_name, product_name, product_revision, qty, container_instance_id, issue_difference_reason), 90)
        identifier = uuid.uuid4().hex
        snap['marker'] = 'Codex issue '+identifier
        fingerprint = digest(snap)
        payload = {'plan_id': identifier, 'marker': 'Codex issue '+identifier, 'snapshot': snap}
        with db_connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',
                       (identifier, fingerprint, json.dumps(payload, ensure_ascii=False), 'ready', time.time()))
        return encode({'plan_id': identifier, 'plan_digest': fingerprint, 'state': 'ready',
                       'request_preview': snap['body'], 'quantity_context': quantity_context(snap), 'serialization_verified': True, 'submitted': False,
                       'confirmation_required': True, 'expires_in_seconds': 3600,
                       'note': '服务端执行仍校验现场规则。计划不代表已投料；仅支持窄范围模式5。'})
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        return encode({'submitted': False, 'error': str(error) or '预览超时。'})


@mcp.tool
async def prepare_bulk_component_issue(container_name: str, source_container_name: str, product_name: str,
        product_revision: str, qty: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)],
        container_instance_id: str | None = None, source_container_instance_id: str | None = None) -> str:
    """准备模式2单来源扣减投料计划，不执行。仅未消耗的首次完整需求、固定版本/同单位、根叶子来源、无第二数量/退料。核对双方状态/来源余额/需求和ActualsBulk回显，保存1小时不可变计划；确认后使用issue_components，共用模式5计划库，执行中/未知同时占用源与目标。"""
    try:
        snap = await asyncio.wait_for(bulk_preview(container_name, source_container_name, product_name,
            product_revision, qty, container_instance_id, source_container_instance_id), 90)
        identifier = uuid.uuid4().hex
        snap['marker'] = 'Codex issue '+identifier
        fingerprint = digest(snap)
        payload = {'plan_id': identifier, 'marker': snap['marker'], 'snapshot': snap}
        with db_connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',
                (identifier, fingerprint, json.dumps(payload, ensure_ascii=False), 'ready', time.time()))
        return encode({'plan_id': identifier, 'plan_digest': fingerprint, 'state': 'ready', 'issue_control': 2,
            'request_preview': snap['body'], 'quantity_context': plan_quantities(snap),
            'occupied_container_ids_on_submit': sorted(participants(snap)), 'serialization_verified': True,
            'submitted': False, 'confirmation_required': True, 'expires_in_seconds': 3600,
            'note': '提交前重读双方状态/余额/需求。当前预测不预留库存，不证明现场执行许可。'})
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        return encode({'submitted': False, 'error': str(error) or '模式2计划预览超时。'})


@mcp.tool
async def get_component_issue_plan(plan_id: str) -> str:
    """本地查询投料计划、原请求和持久化结果。in_flight/unknown不得重放，过期须重新规划。"""
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
    if not row:
        return encode({'error': '计划不存在。'})
    return encode({'plan_id': plan_id, 'plan_digest': row[0], 'plan': json.loads(row[1]), 'state': row[2],
                   'expired': time.time()-row[3] > 3600, 'outcome': json.loads(row[4]) if row[4] else None})


async def issue_locked(plan_id, plan_digest):
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
    payload = checked(row, plan_digest, plan_id); snap = payload['snapshot']
    if row[2] != 'ready' or time.time()-row[3] > 3600:
        raise ValueError('计划已领取/使用或已过期，不允许再次提交。')
    refreshed = await asyncio.wait_for((bulk_preview if issue_mode(snap) == 2 else preview)(**snap['arguments']), 90)
    refreshed['marker'] = snap['marker']
    if digest(refreshed) != plan_digest:
        raise ValueError('需求、工序、数量、单位或约束已变化，未提交。')
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
        if current != row or time.time()-row[3] > 3600:
            raise ValueError('领取前存储计划变化、过期或已使用。')
        wanted = participants(snap)
        for (stored,) in db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')"):
            other = json.loads(stored)['snapshot']
            if other['environment'] == snap['environment'] and participants(other) & wanted:
                raise ValueError('源或目标容器有未核实投料，不能跨模式/新计划绕过占用。')
        attempt_id = uuid.uuid4().hex
        db.execute("UPDATE plans SET state='in_flight' WHERE id=?", (plan_id,))
        db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)', (plan_id, attempt_id, time.time()))
    outcome = {'state': 'unknown', 'submission_attempted': True, 'attempt_id': attempt_id,
               'issue_control': issue_mode(snap), 'occupied_container_ids': sorted(participants(snap)),
               'verified_success': False, 'retry_allowed': False,
               'note': '只提交一次；服务完成消息/当前数量/相同历史不足以自动核实，未知结果保留容器占用。'}
    try:
        body = {**snap['body'], 'comments': payload['marker']}
        outcome['response'] = await request('POST', '/api/ComponentIssue', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
            body=body, params={'execute': 'true', '$select': 'Container,CompletionMsg'}, response_formatter=raw_json)
        args = snap['arguments']
        outcome['requirement_after'] = json.loads(await asyncio.wait_for(get_component_requirement(
            args['container_name'], args.get('source_container_name', args['product_name']), args['container_instance_id']), 45))
        if issue_mode(snap) == 2:
            outcome['source_after'] = json.loads(await asyncio.wait_for(get_container_details(
                args['source_container_name'], args['source_container_instance_id']), 45))
        outcome['actual_history'] = json.loads(await asyncio.wait_for(get_component_issue_history(
            args['container_name'], args['container_instance_id']), 45))
        outcome['mainline_history'] = json.loads(await asyncio.wait_for(get_container_history(
            args['container_name'], args['container_instance_id'], history_source='mainline'), 45))
    except asyncio.CancelledError:
        outcome['interrupted'] = True
        raise
    except Exception as error:
        outcome['error'] = str(error)
    finally:
        with db_connect() as db:
            db.execute("UPDATE plans SET state='unknown',outcome=? WHERE id=? AND state='in_flight'",
                       (json.dumps(outcome, ensure_ascii=False), plan_id))
            db.execute('UPDATE attempts SET finished=? WHERE plan_id=? AND attempt_id=?', (time.time(), plan_id, attempt_id))
    return encode(outcome)


@mcp.tool
async def issue_components(plan_id: str, plan_digest: str) -> str:
    """确认后单次执行已保存的模式2/5计划；模式2提交前重读源/目标/余额/需求及派生回显，同库原子占用双方，与模式5跨计划互斥。只提交一次，不重试或覆盖校验，结果先unknown；reconcile_component_issue核实对应四表链，或人工最终证据核实。其他模式不支持；直接MCP须同等确认。"""
    try:
        with attempt_lock(PLAN_DB, plan_id):
            return await issue_locked(plan_id, plan_digest)
    except (AttemptBusy, ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        return encode({'submitted': False, 'error': str(error) or '提交前核对超时。'})


def review_summary(plan_id, plan_digest, action, evidence_reference, review_note, conclusion=None):
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
    payload = checked(row, plan_digest, plan_id)
    if row[2] != ('in_flight' if action == 'recover' else 'unknown'):
        raise ValueError('投料计划状态不满足本地管理要求。')
    if action == 'verify':
        return ('自动核实投料历史：'+json.dumps({'request': payload['snapshot']['body'], 'quantity_context': plan_quantities(payload['snapshot']), 'marker': payload['marker']}, ensure_ascii=False)
                +'\n只查询MES；唯一标记、主线、需求行、历史头及实际量全部匹配才转verified_committed并解除本计划占用。'
                 '证据缺失/歧义/错误保留unknown；原计划永不重放，不认定未提交。')
    return '投料结果本地管理：'+json.dumps({'action': action, 'request': payload['snapshot']['body'],
        'marker': payload['marker'], 'conclusion': conclusion, 'evidence_reference': evidence_reference,
        'review_note': review_note}, ensure_ascii=False)+'\n恢复仅转unknown不解锁；人工核实须检查最终服务事务/审计，原计划不能重用，不调用MES或重试。'


def review(plan_id, plan_digest, action, evidence_reference, review_note, conclusion=None):
    try:
        if (not isinstance(evidence_reference, str) or not 1 <= len(evidence_reference.strip()) <= 1000
                or not isinstance(review_note, str) or not 1 <= len(review_note.strip()) <= 4000
                or action == 'resolve' and conclusion not in ('committed', 'not_committed')):
            raise ValueError('须有最终证据引用、说明与明确人工结论。')
        with attempt_lock(PLAN_DB, plan_id), db_connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
            payload = checked(row, plan_digest, plan_id)
            attempt = db.execute('SELECT * FROM attempts WHERE plan_id=?', (plan_id,)).fetchone()
            if row[2] != ('in_flight' if action == 'recover' else 'unknown') or not attempt:
                raise ValueError('状态错误或缺少受锁保护的提交尝试，不更改计划。')
            state = 'unknown' if action == 'recover' else 'manually_confirmed_'+conclusion
            record = {'action': action, 'evidence_reference': evidence_reference.strip(), 'review_note': review_note.strip(),
                      'conclusion': conclusion, 'previous_state': row[2], 'previous_outcome': json.loads(row[4]) if row[4] else None,
                      'attempt': attempt, 'created': time.time(), 'automatically_verified': False}
            outcome = {'state': state, 'review': record, 'verified_success': False, 'retry_allowed': False,
                       'original_plan_reusable': False, 'locks_released': action == 'resolve'}
            db.execute('INSERT INTO reviews VALUES (?,?,?)', (plan_id, action, json.dumps(record, ensure_ascii=False)))
            db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?', (state, json.dumps(outcome, ensure_ascii=False), plan_id))
        return encode({'state_changed': True, 'mes_write_performed': False, **outcome})
    except (AttemptBusy, ValueError, TypeError, KeyError, AttributeError) as error:
        return encode({'state_changed': False, 'error': str(error)})


@mcp.tool
async def recover_component_issue_attempt(plan_id: str, plan_digest: str, evidence_reference: str, review_note: str) -> str:
    """经运维确认取得同机进程锁，将有尝试记录且无本地执行者的in_flight转unknown，保留占用及审计。不证明服务端终止，不调用MES/重试；完整锁文件及升级客户端适用，须确认。"""
    return review(plan_id, plan_digest, 'recover', evidence_reference, review_note)


@mcp.tool
async def resolve_component_issue_outcome(plan_id: str, plan_digest: str,
        conclusion: Literal['committed', 'not_committed'], evidence_reference: str, review_note: str) -> str:
    """人工核对最终服务端事务/审计后保存unknown投料计划的人工结论，仅解除本计划占用，原计划不重用。当前数量/空历史不能证明结果，不是自动验证。无MES写入；直接MCP须同等审批。"""
    return review(plan_id, plan_digest, 'resolve', evidence_reference, review_note, conclusion)


@mcp.tool
async def reconcile_component_issue(plan_id: str, plan_digest: str) -> str:
    """核实unknown模式2/5计划的唯一标记及四表链。模式2另须匹配6939批量来源历史及原源/目标/数量。全部匹配才同事务审计保存verified_committed并解除本计划参与容器占用；缺失/重复/错误仍unknown，不推断失败，不写MES/重试，原计划不重用。"""
    try:
        with attempt_lock(PLAN_DB, plan_id):
            with db_connect() as db:
                row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
                payload = checked(row, plan_digest, plan_id)
                attempt = db.execute('SELECT * FROM attempts WHERE plan_id=?', (plan_id,)).fetchone()
                if row[2] != 'unknown' or not attempt:
                    raise ValueError('仅核实有受锁保护尝试记录的unknown计划；in_flight须先安全恢复。')
            if issue_mode(payload['snapshot']) == 2:
                from tools.component_issue_bulk_receipts import planned_receipt
                evidence = await asyncio.wait_for(planned_receipt(payload), 60)
            else:
                from tools.component_issue_receipts import receipt
                evidence = await asyncio.wait_for(receipt(payload), 60)
            verified = evidence.get('complete') is True and evidence.get('verified_success') is True
            record = {'check_id': uuid.uuid4().hex, 'plan_id': plan_id, 'plan_digest': plan_digest,
                      'created': time.time(), 'attempt': attempt, 'evidence': evidence,
                      'automatically_verified': verified, 'previous_state': row[2],
                      'previous_outcome': json.loads(row[4]) if row[4] else None}
            with db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
                current_attempt = db.execute('SELECT * FROM attempts WHERE plan_id=?', (plan_id,)).fetchone()
                if current != row or current_attempt != attempt:
                    raise ValueError('核实期间本地计划或尝试记录变化，不更新状态。')
                db.execute('INSERT INTO receipt_checks VALUES (?,?,?,?)',
                           (record['check_id'], plan_id, json.dumps(record, ensure_ascii=False), record['created']))
                if verified:
                    outcome = {'state': 'verified_committed', 'verified_success': True, 'retry_allowed': False,
                               'locks_released': True, 'original_plan_reusable': False, 'verification': record,
                               'verification_method': evidence['verification_method']}
                    db.execute('INSERT INTO reviews VALUES (?,?,?)', (plan_id, 'verify', json.dumps(record, ensure_ascii=False)))
                    db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',
                               ('verified_committed', json.dumps(outcome, ensure_ascii=False), plan_id))
            return encode({'state_changed': verified, 'state': 'verified_committed' if verified else 'unknown',
                           'verified_success': verified, 'locks_released': verified, 'original_plan_reusable': False,
                           'mes_write_performed': False, 'retry_allowed': False, 'check_id': record['check_id'],
                           'evidence': evidence})
    except (AttemptBusy, ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        return encode({'state_changed': False, 'verified_success': False, 'locks_released': False,
                       'mes_write_performed': False, 'retry_allowed': False,
                       'error': str(error) or '核实超时，结果仍未知。'})
