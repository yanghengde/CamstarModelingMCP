"""Immutable resource plans shared by master data and native resource transactions."""
import asyncio
import copy
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Literal

from tools import mcp
from tools.boms import encode
from tools.bom_modeling import stable
from tools.production_reporting import digest
from core.http_client import get_client, get_headers
from core.transfer_attempt_lock import attempt_lock, AttemptBusy
from config import CAMSTAR_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL, CAMSTAR_QUERY_BASE_URL

PLAN_DB = Path(__file__).resolve().parents[1] / 'data/resource_plans.sqlite3'
TTL = 3600


def environments():
    return {'modeling': CAMSTAR_BASE_URL.rstrip('/'), 'shopfloor': CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),
            'query': CAMSTAR_QUERY_BASE_URL.rstrip('/')}


def db_connect():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB, timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,digest TEXT,payload TEXT,state TEXT,created REAL,outcome TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS attempts(plan_id TEXT PRIMARY KEY,attempt_id TEXT,started REAL,finished REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,plan_id TEXT,record TEXT)')
    return db


def load(db, key):
    return db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (key,)).fetchone()


def checked(row, fingerprint):
    if not row or row[0] != fingerprint: raise ValueError('资源计划不存在或摘要错误。')
    value = json.loads(row[1]); snap = value['snapshot']
    if digest(snap) != fingerprint or snap['environments'] != environments(): raise ValueError('资源计划内容/环境不符。')
    return value


def ready(row):
    if row[2] != 'ready' or time.time()-row[3] > TTL: raise ValueError('资源计划已用或过期，不可重放。')


def pending(db, snap):
    for raw, in db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')"):
        other = json.loads(raw)['snapshot']
        if other['environments'] == snap['environments'] and set(other['resources']) & set(snap['resources']):
            raise ValueError('相关设备/定义有未核实尝试；不能另建计划绕过。')


def public_snapshot(snapshot):
    result=copy.deepcopy(snapshot)
    result['dependencies']=[{'entity':d['entity'],'id':d['id'],'name':d['configuration'].get('Name'),
                             'revision':d['configuration'].get('Revision')} for d in result.get('dependencies',[])]
    preview=result.get('native_preview')
    if isinstance(preview,dict) and 'ParametricData' in preview:
        preview['ParametricData']={'echo_verified':True,'display':'详细点值见不可变请求body；完整原生回显保存在本地计划库。'}
    return result


def save(snap, args):
    snap['environments'] = environments(); key = uuid.uuid4().hex; fingerprint = digest(snap)
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE'); pending(db, snap)
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)', (key,fingerprint,json.dumps({'snapshot':snap,'arguments':args},ensure_ascii=False),'ready',time.time()))
    public=public_snapshot(snap)
    return {'plan_id':key,'plan_digest':fingerprint,'state':'ready','submitted':False,'confirmation_required':True,
        'expires_in_seconds':TTL,'kind':snap['kind'],'service':snap.get('service'), 'request_preview':{k:snap[k] for k in ('method','path','body')},
        'before':snap.get('before'),'expected':snap.get('expected'),'native_preview':public.get('native_preview'),
        'execute_tool':'execute_resource_change','verify_tool':'reconcile_resource_change'}


def confirmation_summary(plan_id, plan_digest):
    with db_connect() as db: row = load(db,plan_id)
    snap=checked(row,plan_digest)['snapshot']; ready(row)
    return '确认设备资源维护/事务：'+json.dumps(snap,ensure_ascii=False)+'\n只提交一次；激活指维护要求启停，不等于设备可用。未知先核实，原计划不可重放。'


@mcp.tool
async def get_resource_change_plan(plan_id: str, include_details: bool=False) -> str:
    """读取设备资源维护/事务的不可变计划、原响应、核实结果及最近五次审计摘要。不发MES请求。"""
    with db_connect() as db:
        row=load(db,plan_id); reviews=db.execute('SELECT record FROM reviews WHERE plan_id=? ORDER BY rowid',(plan_id,)).fetchall()
    if not row: return encode({'error':'资源计划不存在。'})
    payload=json.loads(row[1]); outcome=json.loads(row[4]) if row[4] else None
    if not include_details: payload['snapshot']=public_snapshot(payload['snapshot'])
    if outcome: outcome={k:v for k,v in outcome.items() if k not in ('verification','human_review')}
    records=[{k:v for k,v in json.loads(r[0]).items() if k not in ('previous_outcome','readback','evidence')} for r in reviews[-5:]]
    return encode({'plan_id':plan_id,'plan_digest':row[0],'plan':payload,'state':row[2],'expired':time.time()-row[3]>TTL,
                   'outcome':outcome,'reviews':records,'review_count':len(reviews)})


@mcp.tool
async def execute_resource_change(plan_id: str, plan_digest: str) -> str:
    """确认后单次执行资源主数据/激活/设置转换/吞吐/采集计划。重读设备及依赖，比对摘要后记录尝试再发送；不重试，不绕过MES状态模型/维护/签名校验。结果先unknown，再reconcile_resource_change核实。"""
    try:
        from tools.resource_operations import build
        from tools.collection_submission import PLAN_DB as collection_db
        with attempt_lock(collection_db,'collection-domain'),attempt_lock(PLAN_DB,'resource-domain'),attempt_lock(PLAN_DB,plan_id):
            with db_connect() as db: row=load(db,plan_id)
            payload=checked(row,plan_digest); ready(row); snap=payload['snapshot']
            refreshed=await asyncio.wait_for(build(payload['arguments']),90); refreshed['environments']=environments()
            if digest(refreshed)!=plan_digest: raise ValueError('设备状态/配置/引用/采集定义已变化；未提交，请重新准备。')
            with db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if load(db,plan_id)!=row: raise ValueError('领取前计划已变化。')
                ready(row); pending(db,snap)
                attempt=uuid.uuid4().hex
                db.execute("UPDATE plans SET state='in_flight' WHERE id=?",(plan_id,))
                db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)',(plan_id,attempt,time.time()))
            result={'state':'unknown','submission_attempted':True,'attempt_id':attempt,'retry_allowed':False,'verified_success':False}
            try:
                base=environments()['modeling' if snap['kind']=='master' else 'shopfloor']
                response=await asyncio.wait_for(get_client().request(snap['method'],base+snap['path'],headers=get_headers(),json=snap['body'],follow_redirects=False),60)
                result.update(http_status=response.status_code,raw_response=response.text)
                try: result['response']=response.json()
                except ValueError: pass
            except BaseException as exc:
                result['error']=repr(exc)
                if isinstance(exc,(asyncio.CancelledError,KeyboardInterrupt,SystemExit)): raise
            finally:
                with db_connect() as db:
                    db.execute("UPDATE plans SET state='unknown',outcome=? WHERE id=? AND state='in_flight'",(json.dumps(result,ensure_ascii=False),plan_id))
                    db.execute('UPDATE attempts SET finished=? WHERE plan_id=?',(time.time(),plan_id))
            return encode(result)
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy,asyncio.TimeoutError) as exc:
        return encode({'submitted':False,'error':str(exc) or '执行前检查超时。'})


@mcp.tool
async def reconcile_resource_change(plan_id: str, plan_digest: str) -> str:
    """只读核实资源计划并记录本地终态。主数据须原生成功确认且完整配置回读相符；事务按唯一标记、原生历史及具体状态/数量/采集值核对。空历史/同值不证明成功，不重发MES请求。"""
    try:
        from tools.resource_operations import verify
        with attempt_lock(PLAN_DB,'resource-domain'),attempt_lock(PLAN_DB,plan_id):
            with db_connect() as db:
                row=load(db,plan_id); snap=checked(row,plan_digest)['snapshot']
                attempt=db.execute('SELECT * FROM attempts WHERE plan_id=?',(plan_id,)).fetchone()
            if row[2]!='unknown' or not attempt: raise ValueError('只核实有提交尝试的unknown计划。')
            outcome=json.loads(row[4]) if row[4] else {}
            record=await asyncio.wait_for(verify(snap,outcome),90)
            record.update(kind='automatic_check',created=time.time())
            success=record.get('verified_success') is True
            with db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if load(db,plan_id)!=row: raise ValueError('核实期间计划状态变化。')
                db.execute('INSERT INTO reviews VALUES (?,?,?)',(uuid.uuid4().hex,plan_id,json.dumps(record,ensure_ascii=False)))
                if success:
                    outcome.update(state='verified_applied',verified_success=True,verification=record)
                    db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',('verified_applied',json.dumps(outcome,ensure_ascii=False),plan_id))
            return encode({k:v for k,v in record.items() if k not in ('readback','evidence')} | {'state':'verified_applied' if success else 'unknown','mes_write_performed':False,'original_plan_reusable':False})
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy,asyncio.TimeoutError) as exc:
        return encode({'state_changed':False,'verified_success':False,'error':str(exc) or '核实超时；仍未解锁。'})


@mcp.tool
async def review_resource_change(plan_id: str, plan_digest: str, action: Literal['recover','resolve'], evidence_reference: str, review_note: str, conclusion: Literal['applied','not_applied','unknown']='unknown') -> str:
    """资源异常的本地人工审计，不写MES。recover仅原互斥释放后in_flight转unknown且继续占用；resolve须最终人工证据。保留原响应，不自动核实成功，不重放原计划。"""
    try:
        if action not in ('recover','resolve') or conclusion not in ('applied','not_applied','unknown'): raise ValueError('审计操作/结论无效。')
        if not isinstance(evidence_reference,str) or not 1<=len(evidence_reference.strip())<=2000 or not isinstance(review_note,str) or not 10<=len(review_note.strip())<=4000: raise ValueError('须证据引用及至少10字符人工说明。')
        with attempt_lock(PLAN_DB,'resource-domain'),attempt_lock(PLAN_DB,plan_id),db_connect() as db:
            db.execute('BEGIN IMMEDIATE'); row=load(db,plan_id); checked(row,plan_digest)
            if not db.execute('SELECT 1 FROM attempts WHERE plan_id=?',(plan_id,)).fetchone(): raise ValueError('无提交记录，不能审计。')
            if row[2]!=('in_flight' if action=='recover' else 'unknown'): raise ValueError('计划状态不允许本次审计。')
            outcome=json.loads(row[4]) if row[4] else {}; state='unknown' if action=='recover' or conclusion=='unknown' else 'human_'+conclusion
            record=dict(action=action,conclusion=conclusion,evidence_reference=evidence_reference,review_note=review_note,previous_outcome=copy.deepcopy(outcome),created=time.time())
            db.execute('INSERT INTO reviews VALUES (?,?,?)',(uuid.uuid4().hex,plan_id,json.dumps(record,ensure_ascii=False)))
            outcome.update(state=state,verified_success=False,retry_allowed=False,human_review=record)
            db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',(state,json.dumps(outcome,ensure_ascii=False),plan_id))
        return encode({'state':state,'mes_write_performed':False,'verified_success':False,'original_plan_reusable':False})
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy) as exc: return encode({'state_changed':False,'error':str(exc)})


def assert_no_pending_definition(definition_id):
    if not PLAN_DB.exists(): return
    with db_connect() as db:
        for raw, in db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')"):
            snap=json.loads(raw)['snapshot']
            if snap['environments']==environments() and 'definition:'+definition_id in snap['resources']:
                raise ValueError('该定义有未核实资源采集提交，不能修改定义。')
