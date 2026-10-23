"""Single-use explicit disassociation plans. Other transfer contracts remain gated."""
import json
import sqlite3
import time
import uuid
import hashlib
from pathlib import Path

from tools import mcp
from tools.boms import encode
from tools.container_transfers import preview_container_disassociate
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
        if not snap['family_complete'] or snap['status'].get('Status') != 1:
            raise ValueError('只支持关系完整且状态为Active的容器。')
    # Business capacity/level allowlists govern association, not removal. Keep server validations enabled.
    return {'environment': CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'), 'operation': 'Disassociate',
            'body': preview['request_preview']['body'], 'parent': parent,
            'children': children, 'ancestors': preview['ancestor_check']['nodes']}


def confirmation_summary(plan_id, plan_digest):
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created FROM plans WHERE id=?',(plan_id,)).fetchone()
    if not row or row[0] != plan_digest or row[2] != 'ready' or time.time()-row[3] > 3600:
        return '拆箱计划不可执行，请重新预览。'
    return '仅解除以下明确子容器关系，不执行全部拆箱：\n' + json.dumps(json.loads(row[1])['snapshot']['body'],ensure_ascii=False)


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
    """查看拆箱计划与执行结果；unknown/in_flight不能重试。"""
    with db_connect() as db:
        row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
    if not row:
        return encode({'error':'计划不存在。'})
    return encode({'plan_id':plan_id,'plan_digest':row[0],'plan':json.loads(row[1]),'state':row[2],
                   'expired':time.time()-row[3]>3600,'outcome':json.loads(row[4]) if row[4] else None})


@mcp.tool
async def execute_container_disassociation(plan_id: str, plan_digest: str) -> str:
    """确认后执行单次明确拆箱；重新核对状态、关系及回显，原子领取并锁定所有涉及容器，不重试。响应或当前关系不是事务成功证明，结果保持unknown并保留回读证据。"""
    with db_connect() as db:
        row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
    if not row or row[0] != plan_digest or row[2] != 'ready' or time.time()-row[3]>3600:
        return encode({'submitted':False,'error':'计划无效、已使用或过期。'})
    data=json.loads(row[1])
    try:
        preview=json.loads(await preview_container_disassociate(**data['arguments'],verify_service=True))
        if digest(checked_snapshot(preview)) != plan_digest:
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
             'note':'无已验证的请求到历史事务关联；即使当前关系已解除，也不自动认定本次事务成功或解除锁。'}
    try:
        outcome['response']=await request('POST','/api/Disassociate',body=snap['body'],params={'execute':'true'},
                                          base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json)
        for node in [snap['parent']]+[r['child'] for r in snap['children']]:
            evidence=json.loads(await get_container_trace(node['container']['Name'],node['id'],section='current_family'))
            outcome['evidence'].append({'container_id':node['id'],'trace':evidence})
    except Exception as error:
        outcome['error']=str(error)
    with db_connect() as db:
        db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',('unknown',json.dumps(outcome,ensure_ascii=False),plan_id))
    return encode(outcome)
