"""Single-use explicit disassociation and restricted quantity combination plans."""
import json
import sqlite3
import time
import uuid
import hashlib
import asyncio
from pathlib import Path
from typing import Literal

from tools import mcp
from tools.boms import encode
from tools.container_transfers import preview_container_disassociate, preview_container_combine, preview_container_associate, quantity, amount, ref
from tools.container_trace import get_container_trace
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL
from tools.container_split_plan import split_snapshot
from core.transfer_attempt_lock import attempt_lock, AttemptBusy

PLAN_DB = Path(__file__).resolve().parents[1] / 'data' / 'container_transfer_plans.sqlite3'


def db_connect():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB)
    db.execute('CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY,digest TEXT,payload TEXT,state TEXT,created REAL,outcome TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS resolutions (id TEXT PRIMARY KEY,plan_id TEXT UNIQUE,record TEXT,created REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS transfer_attempts (plan_id TEXT PRIMARY KEY,attempt_id TEXT UNIQUE,started REAL,finished REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS transfer_recoveries (plan_id TEXT PRIMARY KEY,record TEXT,created REAL)')
    return db


def resolution_summary(plan_id, plan_digest, conclusion, evidence_reference, review_note):
    with db_connect() as db:
        row=db.execute('SELECT digest,payload,state FROM plans WHERE id=?',(plan_id,)).fetchone()
    if not row or row[0]!=plan_digest or row[2]!='unknown':
        return '此计划不能人工核实：必须是摘要匹配的unknown计划。'
    snap=json.loads(row[1])['snapshot']
    return ('人工核实并解除此计划的容器锁，原计划不会恢复可执行。\n'
            +json.dumps({'operation':snap['operation'],'request':snap['body'],'conclusion':conclusion,
                         'evidence_reference':evidence_reference,'review_note':review_note},ensure_ascii=False)
            +'\n请人工核对服务端事务记录/审计证据。当前状态匹配或空历史页不足以证明结论。')


def recovery_summary(plan_id, plan_digest, evidence_reference, review_note):
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state FROM plans WHERE id=?', (plan_id,)).fetchone()
    if not row or row[0] != plan_digest or row[2] != 'in_flight':
        return '仅可恢复摘要匹配的in_flight计划。'
    snap = json.loads(row[1])['snapshot']
    return ('将已无本地执行者的in_flight计划转为unknown；保留全部容器及新名称占用，不重发请求。\n'
            + json.dumps({'operation':snap['operation'],'request':snap['body'],
                          'evidence_reference':evidence_reference,'review_note':review_note}, ensure_ascii=False)
            + '\n本地进程退出不表示服务端事务结束或失败。后续人工核实仍须服务端最终事务/审计证据。'
              '\n仅支持升级后带进程互斥记录的计划；旧进程必须停止，锁文件不得删除，数据库须位于本机磁盘。')


@mcp.tool
async def recover_container_transfer_attempt(plan_id: str, plan_digest: str,
        evidence_reference: str, review_note: str) -> str:
    """经运维确认将无本地执行者的in_flight转为unknown，记录恢复审计，保留所有占用。不证明服务端结束，不重发、不解锁。仅本机磁盘、新版互斥尝试；旧版无记录拒绝恢复。直接MCP调用方须执行同等人工审批。"""
    if (not isinstance(evidence_reference,str) or not 1 <= len(evidence_reference.strip()) <= 1000
            or not isinstance(review_note,str) or not 1 <= len(review_note.strip()) <= 4000):
        return encode({'state_changed':False,'error':'必须提供运维证据引用和恢复说明。'})
    try:
        with attempt_lock(PLAN_DB, plan_id):
            with db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT digest,payload,state,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
                if not row or row[0] != plan_digest or row[2] != 'in_flight':
                    raise ValueError('仅允许摘要匹配的in_flight计划。')
                snap = json.loads(row[1])['snapshot']
                if digest(snap) != plan_digest or snap['environment'] != CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'):
                    raise ValueError('计划内容或环境不一致。')
                attempt = db.execute('SELECT attempt_id,started,finished FROM transfer_attempts WHERE plan_id=?', (plan_id,)).fetchone()
                if not attempt:
                    raise ValueError('旧版计划缺少进程互斥尝试记录；不能证明原执行者已退出，须离线运维处理。')
                record = {'recovery_id':uuid.uuid4().hex,'plan_id':plan_id,'plan_digest':plan_digest,
                          'attempt_id':attempt[0],'attempt_started':attempt[1],'attempt_finished':attempt[2],
                          'previous_state':row[2],'previous_outcome':json.loads(row[3]) if row[3] else None,
                          'evidence_reference':evidence_reference.strip(),'review_note':review_note.strip(),
                          'created':time.time(),'verification_method':'local_process_lock_and_operator_review',
                          'server_outcome_verified':False}
                outcome = {'state':'unknown','verified_success':False,'retry_allowed':False,
                           'locks_released':False,'submission_may_have_occurred':True,'recovery':record,
                           'note':'本地执行者已无互斥占用，服务端仍可能处理原请求。须核对最终事务证据，禁止自动重试或解锁。'}
                db.execute('INSERT INTO transfer_recoveries VALUES (?,?,?)',
                           (plan_id,json.dumps(record,ensure_ascii=False),record['created']))
                db.execute("UPDATE plans SET state='unknown',outcome=? WHERE id=? AND state='in_flight'",
                           (json.dumps(outcome,ensure_ascii=False),plan_id))
        return encode({'state_changed':True,'mes_write_performed':False,**outcome})
    except (AttemptBusy, ValueError, TypeError, KeyError) as error:
        return encode({'state_changed':False,'error':str(error)})


@mcp.tool
async def resolve_container_transfer_outcome(plan_id: str, plan_digest: str,
        conclusion: Literal['committed','not_committed'], evidence_reference: str, review_note: str) -> str:
    """仅在人工核对服务端事务/审计证据并明确确认后，记录人工结论、解除unknown计划占用。非自动验证，禁止以空页/当前状态推断。原计划永不重开；in_flight/ready不能解除。不调用MES或自动重试。"""
    if (conclusion not in ('committed','not_committed') or not isinstance(evidence_reference,str)
            or not 1<=len(evidence_reference.strip())<=1000 or not isinstance(review_note,str)
            or not 1<=len(review_note.strip())<=4000):
        return encode({'error':'必须明确人工结论、证据引用及核实说明。','state_changed':False})
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT digest,payload,state,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
        if not row or row[0]!=plan_digest or row[2]!='unknown':
            return encode({'error':'仅允许摘要匹配且已结束请求的unknown计划；不允许解除in_flight。','state_changed':False})
        snap=json.loads(row[1])['snapshot']
        if digest(snap)!=plan_digest or snap['environment']!=CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'):
            return encode({'error':'计划内容或环境不一致。','state_changed':False})
        state='manually_confirmed_'+conclusion
        record={'resolution_id':uuid.uuid4().hex,'plan_id':plan_id,'plan_digest':plan_digest,
                'conclusion':conclusion,'verification_method':'human_attestation',
                'evidence_reference':evidence_reference.strip(),'review_note':review_note.strip(),
                'previous_state':row[2],'previous_outcome':json.loads(row[3]) if row[3] else None,
                'created':time.time(),'automatically_verified':False}
        outcome={'state':state,'verified_success':False,'manual_resolution':record,
                 'note':'人工确认结论，系统未自动验证。原计划不可再执行；任何后续操作须新建预览并确认。'}
        db.execute('INSERT INTO resolutions VALUES (?,?,?,?)',
                   (record['resolution_id'],plan_id,json.dumps(record,ensure_ascii=False),record['created']))
        db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',
                   (state,json.dumps(outcome,ensure_ascii=False),plan_id))
    return encode({'state_changed':True,'original_plan_reusable':False,'mes_write_performed':False,**outcome})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def checked_combine(preview):
    if preview.get('error') or preview.get('server_echo_verified') is not True:
        raise ValueError('合批服务回显未验证。')
    source, target = preview['source'], preview['target']
    if source['id'] == target['id']:
        raise ValueError('来源和目标不能相同。')
    for node in (source, target):
        if (not node['family_complete'] or not node['parent_known'] or node['parent_id']
                or type(node['status'].get('Status')) is not int or node['status']['Status'] != 1):
            raise ValueError('仅支持已确认无父容器且Active的数量容器。')
        if any(r.get('ParentContainerId') == node['id'] for r in node['family']):
            raise ValueError('含子容器的合批未开放。')
        for field in ('Product','ContainerLevel','UOM','UOM2','Spec'):
            value = node['status'].get(field)
            if not isinstance(value,dict) or not value.get('Name') or not (value.get('InstanceId') or value.get('InstanceID')):
                raise ValueError(f'{field} 缺少明确身份。')
            if field in ('Product','Spec') and not value.get('Revision'):
                raise ValueError(f'{field} 缺少明确版本。')
    if any(source['status'][f] != target['status'][f] for f in ('Product','ContainerLevel','UOM','UOM2','Spec')):
        raise ValueError('产品、层级、单位或工序不一致，当前窄范围合批不支持。')
    body = preview['request_preview']['body']
    details = body.get('fromContainerDetails')
    if not isinstance(details,list) or len(details)!=1:
        raise ValueError('必须为单来源明细。')
    d=details[0]
    q=amount(d['qty'])
    q2=quantity({'status':{'Qty':d.get('qty2')}})
    if q2 is None:
        raise ValueError('必须明确有限非负第二数量。')
    for field, requested in [('Qty',q),('Qty2',q2)]:
        a,b=quantity(source,field),quantity(target,field)
        if a is None or b is None or a<requested:
            raise ValueError('数量未知或来源余额不足。')
    close = d.get('closeWhenEmpty')
    if type(close) is not bool or body != {'container':ref(target),'closeWhenEmpty':close,
            'fromContainerDetails':[{'fromContainer':ref(source),'qty':d['qty'],'qty2':d['qty2'],
                                    'combineAllQty':False,'closeWhenEmpty':close}]}:
        raise ValueError('合批请求与核对范围不一致。')
    if d.get('combineAllQty') is not False:
        raise ValueError('不支持隐式全部合批。')
    return {'environment':CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),'operation':'Combine',
            'body':body,'parent':target,'children':[{'child':source}],
            'quantity_effect':preview['quantity_effect'],'quantity2_effect':preview['quantity2_effect']}


def checked_snapshot(preview):
    if preview.get('error') or preview.get('server_echo_verified') is not True:
        raise ValueError('服务回显未验证，不能生成执行计划。')
    parent = preview['parent']
    children = preview['relationships']
    if not children or not preview.get('relationship_checks_complete') or preview.get('relationship_conflicts'):
        raise ValueError('当前关系校验不完整或存在冲突。')
    for child in children:
        if child['parent_before'] != parent['id']:
            raise ValueError('拆箱对象必须是已确认的直属子容器。')
    for snap in [parent] + [c['child'] for c in children]:
        if not snap['family_complete'] or type(snap['status'].get('Status')) is not int or snap['status']['Status'] != 1:
            raise ValueError('只支持关系完整且状态为Active的容器。')
    body = preview['request_preview']['body']
    expected = {'container': {'name': parent['container']['Name'], 'instanceId': parent['id']},
                'childContainers': [{'name': c['child']['container']['Name'], 'instanceId': c['child']['id']} for c in children],
                'disassociateAll': False}
    if body != expected or body.get('disassociateAll') is not False:
        raise ValueError('执行请求与已审阅的明确拆箱对象不一致。')
    # Business capacity/level allowlists govern association, not removal. Keep server validations enabled.
    return {'environment': CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'), 'operation': 'Disassociate',
            'body': preview['request_preview']['body'], 'parent': parent,
            'children': children, 'ancestors': preview['ancestor_check']['nodes']}


def checked_association(preview):
    if preview.get('error') or preview.get('server_echo_verified') is not True:
        raise ValueError('装箱服务回显未验证。')
    parent, children = preview['parent'], preview['relationships']
    if (not children or not preview.get('relationship_checks_complete') or preview.get('relationship_conflicts')
            or preview['ancestor_check'].get('complete') is not True or preview['ancestor_check'].get('cycle')):
        raise ValueError('装箱祖先或直属关系未完整确认。')
    rules=preview['level_rules']
    if (rules.get('complete') is not True or len(rules.get('checks',[]))!=len(children)
            or {r['child_id'] for r in rules['checks']}!={c['child']['id'] for c in children}
            or any(r['parent_allows_child']!='listed' or r['child_allows_parent']!='listed' for r in rules['checks'])):
        raise ValueError('父/子双方层级未明确允许。')
    seen={parent['id']}
    for child in children:
        node=child['child']
        if child['parent_before'] not in (None,'') or node['id'] in seen:
            raise ValueError('子容器已有父级或重复。')
        seen.add(node['id'])
    for node in [parent]+[c['child'] for c in children]:
        if (not node['family_complete'] or not node['parent_known'] or node['parent_id']
                or type(node['status'].get('Status')) is not int or node['status']['Status']!=1):
            raise ValueError('当前仅支持无父容器且Active的对象。')
    for child in children:
        node=child['child']
        if any(r.get('ParentContainerId')==node['id'] for r in node['family']):
            raise ValueError('本阶段不支持携带已有子容器的多层装箱。')
    expected={'container':ref(parent),'childContainers':[ref(c['child']) for c in children]}
    if preview['request_preview']['body']!=expected:
        raise ValueError('装箱请求与明确对象不一致。')
    return {'environment':CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),'operation':'Associate','body':expected,
            'parent':parent,'children':children,'ancestors':preview['ancestor_check']['nodes'],'level_rules':rules,
            'capacity_policy':'未知；不得跳过服务端业务校验，不宣称预先满足容量要求。'}


def confirmation_summary(plan_id, plan_digest):
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created FROM plans WHERE id=?',(plan_id,)).fetchone()
    if not row or row[0] != plan_digest or row[2] != 'ready' or time.time()-row[3] > 3600:
        return '拆箱计划不可执行，请重新预览。'
    snap=json.loads(row[1])['snapshot']
    label={'Combine':'合批（明确Qty/Qty2与空后关闭选项）：',
           'Split':'数量拆批：明确新容器名与Qty/Qty2，扣减来源，关闭选项如下；现场执行验收待补。',
           'Associate':'装箱：增加以下父子关系；容量规则由服务端校验，可能拒绝。',
           'Disassociate':'仅解除以下明确子容器关系，不执行全部拆箱：'}[snap['operation']]
    return label+'\n'+json.dumps(snap['body'],ensure_ascii=False)


@mcp.tool
async def prepare_container_split(container_name: str, new_container_name: str, qty: float, qty2: float,
                                   close_when_empty: bool=False) -> str:
    """单目标数量拆批计划：明确双数量/新名称，固定扣减来源、不自动编号；无父子关系Active来源，明确单位/版本，名称不存在，execute=false回显。现场事务验收待补，不执行。"""
    try:
        args={'container_name':container_name,'new_container_name':new_container_name,'qty':qty,'qty2':qty2,
              'close_when_empty':close_when_empty}
        snap=await split_snapshot(**args)
        plan_id,plan_digest=uuid.uuid4().hex,digest(snap)
        with db_connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',(plan_id,plan_digest,
                json.dumps({'snapshot':snap,'arguments':args},ensure_ascii=False),'ready',time.time(),None))
        return encode({'plan_id':plan_id,'plan_digest':plan_digest,'state':'ready','request':snap['body'],
                       'quantity_effect':snap['quantity_effect'],'expires_in_seconds':3600,
                       'note':'仅序列化回显已验证；真实事务与历史关联待集中验收。'})
    except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError) as error:
        return encode({'error':str(error),'executable':False})


@mcp.tool
async def prepare_container_association(parent_container_name: str, child_container_names: list[str]) -> str:
    """准备单次明确装箱计划，检查Active、无已有父级、叶子子容器、祖先链、双向层级及execute=false回显。容量未知，执行时保留全部服务端规则，不代表业务许可。现场验收待补。"""
    try:
        args={'parent_container_name':parent_container_name,'child_container_names':child_container_names}
        preview=json.loads(await preview_container_associate(**args,verify_service=True))
        snap=checked_association(preview)
        plan_id,plan_digest=uuid.uuid4().hex,digest(snap)
        with db_connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',(plan_id,plan_digest,
                json.dumps({'snapshot':snap,'arguments':args},ensure_ascii=False),'ready',time.time(),None))
        return encode({'plan_id':plan_id,'plan_digest':plan_digest,'state':'ready','request':snap['body'],
                       'capacity_verified':False,'expires_in_seconds':3600,'note':snap['capacity_policy']})
    except (ValueError,TypeError,KeyError) as error:
        return encode({'error':str(error),'executable':False})


@mcp.tool
async def prepare_container_combination(target_container_name: str, source_container_name: str,
                                         qty: float, qty2: float, close_when_empty: bool=False) -> str:
    """生成单来源合批计划，明确Qty和Qty2、不换算；仅Active、无父子关系、相同明确产品版本/层级/单位/工序。execute=false回显后保存1小时单次计划，不执行，现场验收待补。"""
    try:
        args={'target_container_name':target_container_name,'source_container_name':source_container_name,
              'qty':qty,'qty2':qty2,'close_when_empty':close_when_empty}
        preview=json.loads(await preview_container_combine(**args,verify_service=True))
        snap=checked_combine(preview)
        plan_id,plan_digest=uuid.uuid4().hex,digest(snap)
        with db_connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',(plan_id,plan_digest,
                json.dumps({'snapshot':snap,'arguments':args},ensure_ascii=False),'ready',time.time(),None))
        return encode({'plan_id':plan_id,'plan_digest':plan_digest,'state':'ready','request':snap['body'],
                       'quantity_effect':snap['quantity_effect'],'quantity2_effect':snap['quantity2_effect'],
                       'expires_in_seconds':3600,'note':'执行仍需确认；现场提交与历史关联验收待补。'})
    except (ValueError,TypeError,KeyError) as error:
        return encode({'error':str(error),'executable':False})


@mcp.tool
async def prepare_container_disassociation(parent_container_name: str, child_container_names: list[str]) -> str:
    """生成1小时有效的单次拆箱计划，明确子容器，检查直属关系、Active状态和execute=false回显。不执行事务；现场验收待补。"""
    try:
        preview = json.loads(await preview_container_disassociate(parent_container_name, child_container_names, verify_service=True))
        snap = checked_snapshot(preview)
        plan_id, plan_digest = uuid.uuid4().hex, digest(snap)
        data = {'snapshot':snap,'arguments':{'parent_container_name':parent_container_name,'child_container_names':child_container_names}}
        with db_connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',(plan_id,plan_digest,json.dumps(data,ensure_ascii=False),'ready',time.time(),None))
        return encode({'plan_id':plan_id,'plan_digest':plan_digest,'state':'ready','request':snap['body'],
                       'expires_in_seconds':3600,'note':'执行仍需确认及重新核对，现场事务验收待补。'})
    except (ValueError,TypeError,KeyError) as error:
        return encode({'error':str(error),'executable':False})


@mcp.tool
async def get_container_transfer_plan(plan_id: str) -> str:
    """查看容器拆箱/合批计划与执行结果；unknown/in_flight不能重试。"""
    with db_connect() as db:
        row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
    if not row:
        return encode({'error':'计划不存在。'})
    return encode({'plan_id':plan_id,'plan_digest':row[0],'plan':json.loads(row[1]),'state':row[2],
                   'expired':time.time()-row[3]>3600,'outcome':json.loads(row[4]) if row[4] else None})


@mcp.tool
async def execute_container_disassociation(plan_id: str, plan_digest: str) -> str:
    """确认后执行单次明确拆箱；重新核对状态、关系及回显，原子领取并锁定所有涉及容器，不重试。响应或当前关系不是事务成功证明，结果保持unknown并保留回读证据。"""
    return await execute_plan(plan_id,plan_digest,'Disassociate')


@mcp.tool
async def execute_container_combination(plan_id: str, plan_digest: str) -> str:
    """确认后执行单次窄范围数量合批计划；重新核对数量/版本/单位/关系，锁定源和目标，POST不重试。回读仅作证据，结果未知时不能重试；现场验收待补。"""
    return await execute_plan(plan_id,plan_digest,'Combine')


@mcp.tool
async def execute_container_association(plan_id: str, plan_digest: str) -> str:
    """确认后执行单次装箱计划，重读身份/关系/层级/服务回显，跨拆箱合批锁定涉及容器。只发明确父子引用，不绕过服务业务规则、不重试。结果关联未验证时保留unknown。"""
    return await execute_plan(plan_id,plan_digest,'Associate')


@mcp.tool
async def execute_container_split(plan_id: str, plan_digest: str) -> str:
    """确认后执行单次数量拆批，重查来源/名称/双数量，原子领取并锁来源及新名。不重试或自动回滚，响应/历史关联未核实保持unknown。现场执行验收待补。"""
    return await execute_plan(plan_id,plan_digest,'Split')


async def execute_plan(plan_id,plan_digest,operation):
    try:
        with attempt_lock(PLAN_DB, plan_id):
            return await execute_locked_plan(plan_id, plan_digest, operation)
    except AttemptBusy as error:
        return encode({'submitted':False,'error':str(error)})


async def execute_locked_plan(plan_id,plan_digest,operation):
    with db_connect() as db:
        row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
    if not row or row[0] != plan_digest or row[2] != 'ready' or time.time()-row[3]>3600:
        return encode({'submitted':False,'error':'计划无效、已使用或过期。'})
    data=json.loads(row[1])
    if data['snapshot'].get('operation') != operation:
        return encode({'submitted':False,'error':'计划操作类型不匹配。'})
    if digest(data['snapshot']) != plan_digest:
        return encode({'submitted':False,'error':'存储计划摘要不匹配，禁止提交。'})
    try:
        if operation=='Split':
            refreshed=await split_snapshot(**data['arguments'])
        elif operation=='Combine':
            preview=json.loads(await preview_container_combine(**data['arguments'],verify_service=True))
            refreshed=checked_combine(preview)
        elif operation=='Associate':
            preview=json.loads(await preview_container_associate(**data['arguments'],verify_service=True))
            refreshed=checked_association(preview)
        else:
            preview=json.loads(await preview_container_disassociate(**data['arguments'],verify_service=True))
            refreshed=checked_snapshot(preview)
        if digest(refreshed) != plan_digest:
            raise ValueError('容器状态或关系变化，请重新预览。')
    except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError) as error:
        return encode({'submitted':False,'error':str(error)})
    snap=data['snapshot']
    involved={snap['parent']['id'],*[r['child']['id'] for r in snap['children']]}
    reserved={n.casefold() for n in snap.get('reserved_names',[])}
    used_names={n['container']['Name'].casefold() for n in [snap['parent']]+[r['child'] for r in snap['children']]}
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for existing, in db.execute("SELECT payload FROM plans WHERE state IN ('unknown','in_flight')"):
            other=json.loads(existing)['snapshot']
            other_reserved={n.casefold() for n in other.get('reserved_names',[])}
            other_names={n['container']['Name'].casefold() for n in [other['parent']]+[r['child'] for r in other['children']]}
            if other['environment']==snap['environment'] and (involved.intersection({other['parent']['id'],*[r['child']['id'] for r in other['children']]})
                    or reserved.intersection(other_reserved|other_names) or used_names.intersection(other_reserved)):
                return encode({'submitted':False,'error':'涉及容器已有未核实事务，禁止重复提交。'})
        if time.time()-row[3]>3600 or db.execute("UPDATE plans SET state='in_flight' WHERE id=? AND state='ready'",(plan_id,)).rowcount != 1:
            return encode({'submitted':False,'error':'计划过期或已被领取。'})
        attempt_id = uuid.uuid4().hex
        db.execute('INSERT INTO transfer_attempts VALUES (?,?,?,NULL)', (plan_id, attempt_id, time.time()))
    outcome={'state':'unknown','verified_success':False,'submission_attempted':True,'evidence':[],
             'attempt_id':attempt_id,
             'note':'无已验证的请求到历史事务关联；当前状态或历史列表不证明本次事务成功，不自动解除锁。'}
    try:
        outcome['response']=await request('POST','/api/'+operation,body=snap['body'],params={'execute':'true'},
                                          base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json)
        for node in [snap['parent']]+[r['child'] for r in snap['children']]:
            evidence=json.loads(await get_container_trace(node['container']['Name'],node['id'],section={'Combine':'combines','Split':'splits'}.get(operation,'current_family')))
            outcome['evidence'].append({'container_id':node['id'],'trace':evidence})
    except asyncio.CancelledError:
        outcome['interrupted'] = True
        outcome['error'] = '本地任务已取消；服务端结果未知，不允许自动重试。'
        raise
    except Exception as error:
        outcome['error']=str(error)
    finally:
        with db_connect() as db:
            db.execute("UPDATE plans SET state=?,outcome=? WHERE id=? AND state='in_flight'",
                       ('unknown',json.dumps(outcome,ensure_ascii=False),plan_id))
            db.execute('UPDATE transfer_attempts SET finished=? WHERE plan_id=? AND attempt_id=?',
                       (time.time(), plan_id, attempt_id))
    return encode(outcome)
