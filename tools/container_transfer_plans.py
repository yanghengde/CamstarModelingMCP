"""Single-use explicit disassociation and restricted quantity combination plans."""
import json
import sqlite3
import time
import uuid
import hashlib
from pathlib import Path

from tools import mcp
from tools.boms import encode
from tools.container_transfers import preview_container_disassociate, preview_container_combine, preview_container_associate, quantity, amount, ref
from tools.container_trace import get_container_trace
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

PLAN_DB = Path(__file__).resolve().parents[1] / 'data' / 'container_transfer_plans.sqlite3'


def db_connect():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB)
    db.execute('CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY,digest TEXT,payload TEXT,state TEXT,created REAL,outcome TEXT)')
    return db


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
           'Associate':'装箱：增加以下父子关系；容量规则由服务端校验，可能拒绝。',
           'Disassociate':'仅解除以下明确子容器关系，不执行全部拆箱：'}[snap['operation']]
    return label+'\n'+json.dumps(snap['body'],ensure_ascii=False)


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


async def execute_plan(plan_id,plan_digest,operation):
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
        if operation=='Combine':
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
    except (ValueError,TypeError,KeyError) as error:
        return encode({'submitted':False,'error':str(error)})
    snap=data['snapshot']
    involved={snap['parent']['id'],*[r['child']['id'] for r in snap['children']]}
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for existing, in db.execute("SELECT payload FROM plans WHERE state IN ('unknown','in_flight')"):
            other=json.loads(existing)['snapshot']
            if other['environment']==snap['environment'] and involved.intersection({other['parent']['id'],*[r['child']['id'] for r in other['children']]}):
                return encode({'submitted':False,'error':'涉及容器已有未核实事务，禁止重复提交。'})
        if time.time()-row[3]>3600 or db.execute("UPDATE plans SET state='in_flight' WHERE id=? AND state='ready'",(plan_id,)).rowcount != 1:
            return encode({'submitted':False,'error':'计划过期或已被领取。'})
    outcome={'state':'unknown','verified_success':False,'submission_attempted':True,'evidence':[],
             'note':'无已验证的请求到历史事务关联；当前状态或历史列表不证明本次事务成功，不自动解除锁。'}
    try:
        outcome['response']=await request('POST','/api/'+operation,body=snap['body'],params={'execute':'true'},
                                          base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json)
        for node in [snap['parent']]+[r['child'] for r in snap['children']]:
            evidence=json.loads(await get_container_trace(node['container']['Name'],node['id'],section='combines' if operation=='Combine' else 'current_family'))
            outcome['evidence'].append({'container_id':node['id'],'trace':evidence})
    except Exception as error:
        outcome['error']=str(error)
    with db_connect() as db:
        db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',('unknown',json.dumps(outcome,ensure_ascii=False),plan_id))
    return encode(outcome)
