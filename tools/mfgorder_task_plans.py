"""Single-use, confirmed pass/fail plans; all attempted outcomes remain unknown."""
import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Literal

from tools import mcp
from tools.boms import encode
from tools.mfgorder_task_preview import preview_mfgorder_task_result, explicit_ref, SERVICE_PARAMS
from tools.mfgorder_task_history import get_mfgorder_task_history
from tools.container_inquiry import get_mfgorder_execution
from tools.mfgorder_tasks import identifier
from tools.modeling_details import raw_json
from core.http_client import request
from core.transfer_attempt_lock import attempt_lock, AttemptBusy
from config import CAMSTAR_SHOPFLOOR_BASE_URL

PLAN_DB = Path(__file__).resolve().parents[1] / 'data' / 'mfgorder_task_plans.sqlite3'


def db_connect():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB)
    db.execute('CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY,digest TEXT,payload TEXT,state TEXT,created REAL,outcome TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS attempts (plan_id TEXT PRIMARY KEY,attempt_id TEXT,started REAL,finished REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS reviews (plan_id TEXT,kind TEXT,record TEXT,created REAL,PRIMARY KEY(plan_id,kind))')
    return db


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


async def checked_snapshot(arguments):
    preview = json.loads(await preview_mfgorder_task_result(**arguments, verify_service=True))
    if (preview.get('error') or preview.get('blockers') or preview.get('local_checks_complete') is not True
            or preview.get('server_echo_verified') is not True):
        raise ValueError('任务条件或execute=false回显未通过，不能生成计划。')
    task = preview['selected_task']
    body = {'mfgOrder': explicit_ref(preview['order']), 'mfgOrderProcedure': explicit_ref(task['procedure'], True),
            'mfgOrderTask': explicit_ref(task['task']), 'pass': arguments['pass_result']}
    if (type(body['pass']) is not bool or preview['request_preview'] !=
            {'path': '/api/ExecuteMfgOrdertask', 'params': SERVICE_PARAMS, 'body': body}
            or task['phase'] != arguments['phase'] or identifier(task['procedure']) != arguments['procedure_id']
            or identifier(task['task_list']) != arguments['task_list_id'] or identifier(task['task']) != arguments['task_id']):
        raise ValueError('任务请求与明确上下文不一致。')
    history = json.loads(await get_mfgorder_task_history(arguments['mfgorder_name']))
    if (history.get('error') or history.get('complete') is not True or history.get('order') != preview['order']):
        raise ValueError('执行记录基线不完整或工单身份不一致。')
    previous = [row for row in history['tasks'] if identifier(row.get('task')) == arguments['task_id']]
    if len(previous) > 1 or any(row.get('state') != 'not_started' or type(row.get('execution_count')) is not int
                               or row['execution_count'] != 0 for row in previous):
        raise ValueError('任务已有执行记录或记录关联不唯一；不自动重做。')
    return {'environment': CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'), 'operation': 'ExecuteMfgOrdertask',
            'order': preview['order'], 'body': body, 'task': task,
            'procedure_context': preview['procedure_context'], 'task_list_context': preview['task_list_context'],
            'history_baseline': history}


def confirmation_summary(plan_id, plan_digest):
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created FROM plans WHERE id=?', (plan_id,)).fetchone()
    if not row or row[0] != plan_digest or row[2] != 'ready' or time.time() - row[3] > 3600:
        return '工单任务计划无效、已使用或过期，请重新预览。'
    snap = json.loads(row[1])['snapshot']
    if digest(snap) != plan_digest or snap['environment'] != CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'):
        return '工单任务计划内容或环境不一致，禁止执行。'
    return ('提交工单过程任务结果：\n' + json.dumps({'phase':snap['task']['phase'],
            'task_list':snap['task']['task_list'],'request':snap['body']}, ensure_ascii=False)
            + '\n明确pass值；任务状态引用省略，服务默认映射与现场执行尚未验收。'
              '\n只提交一次，不重试；提交后unknown将占用整个工单，当前记录不能证明本次事务成功。')


@mcp.tool
async def prepare_mfgorder_task_result(mfgorder_name: str, phase: Literal['pre_production', 'post_production'],
        procedure_id: str, task_list_id: str, task_id: str, pass_result: bool) -> str:
    """准备1小时单次通过/失败任务计划；复用窄范围校验和execute=false回显，读取完整执行记录基线。仅明确单次未执行任务，不自动推断状态码，不提交。现场语义待验收。"""
    arguments = {'mfgorder_name':mfgorder_name,'phase':phase,'procedure_id':procedure_id,
                 'task_list_id':task_list_id,'task_id':task_id,'pass_result':pass_result}
    try:
        snap = await asyncio.wait_for(checked_snapshot(arguments), timeout=150)
        plan_id, plan_digest = uuid.uuid4().hex, digest(snap)
        with db_connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',
                       (plan_id, plan_digest, json.dumps({'snapshot':snap,'arguments':arguments},ensure_ascii=False), 'ready', time.time()))
        return encode({'plan_id':plan_id,'plan_digest':plan_digest,'state':'ready','request':snap['body'],
                       'phase':phase,'expires_in_seconds':3600,'transaction_verified':False,
                       'note':'计划需明确确认；服务默认状态语义及真实提交待现场验收。'})
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        return encode({'error':str(error) or '准备计划超时。','executable':False})


@mcp.tool
async def get_mfgorder_task_plan(plan_id: str) -> str:
    """读取工单任务计划、摘要及持久化尝试结果。unknown/in_flight不可重试或绕过工单占用。"""
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
    if not row:
        return encode({'error':'计划不存在。'})
    return encode({'plan_id':plan_id,'plan_digest':row[0],'plan':json.loads(row[1]),'state':row[2],
                   'expired':time.time()-row[3]>3600,'outcome':json.loads(row[4]) if row[4] else None})


@mcp.tool
async def execute_mfgorder_task_result(plan_id: str, plan_digest: str) -> str:
    """明确确认后重读任务/服务回显/历史基线，原子领取并单次提交。整个工单unknown/in_flight互斥，不重试/回滚。响应和回读仅为证据，结果保持unknown。现场执行待验收；直接MCP调用方须同等审批。"""
    try:
        with attempt_lock(PLAN_DB, plan_id):
            return await execute_locked(plan_id, plan_digest)
    except AttemptBusy as error:
        return encode({'submitted':False,'error':str(error)})


async def execute_locked(plan_id, plan_digest):
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created FROM plans WHERE id=?', (plan_id,)).fetchone()
    if not row or row[0] != plan_digest or row[2] != 'ready' or time.time()-row[3]>3600:
        return encode({'submitted':False,'error':'计划无效、已使用或过期。'})
    try:
        data = json.loads(row[1]); snap = data['snapshot']
        if (digest(snap) != plan_digest or snap['operation'] != 'ExecuteMfgOrdertask'
                or snap['environment'] != CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')):
            raise ValueError('计划摘要、操作或环境不符。')
        current = await asyncio.wait_for(checked_snapshot(data['arguments']), timeout=150)
        if digest(current) != plan_digest:
            raise ValueError('任务状态、配置或执行记录发生变化，请新建计划。')
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        return encode({'submitted':False,'error':str(error) or '提交前校验超时。'})
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE')
        stored = db.execute('SELECT digest,payload,state,created FROM plans WHERE id=?', (plan_id,)).fetchone()
        if stored != row or time.time()-stored[3]>3600:
            return encode({'submitted':False,'error':'领取前计划已变化、过期或已使用。'})
        for payload, in db.execute("SELECT payload FROM plans WHERE state IN ('unknown','in_flight')"):
            other = json.loads(payload)['snapshot']
            if other['environment'] == snap['environment'] and other['order']['InstanceID'] == snap['order']['InstanceID']:
                return encode({'submitted':False,'error':'工单已有未核实任务提交，禁止绕过占用。'})
        db.execute("UPDATE plans SET state='in_flight' WHERE id=? AND state='ready'", (plan_id,))
        attempt_id = uuid.uuid4().hex
        db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)', (plan_id, attempt_id, time.time()))
    outcome = {'state':'unknown','attempt_id':attempt_id,'submission_attempted':True,'verified_success':False,
               'retry_allowed':False,'evidence':[],
               'note':'当前任务记录未验证与本次请求的因果关联；工单占用保留，不自动重试/回滚/解锁。'}
    try:
        try:
            outcome['response'] = await request('POST', '/api/ExecuteMfgOrdertask', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                body=snap['body'], params={'execute':'true'}, response_formatter=raw_json)
        except Exception as error:
            outcome['submission_error'] = str(error)
        for source, read in [('executed_records', get_mfgorder_task_history), ('inquiry', get_mfgorder_execution)]:
            evidence = {'source':source}
            outcome['evidence'].append(evidence)
            try:
                kwargs = {'info_depth':4,'limit':2} if source == 'inquiry' else {}
                evidence['result'] = json.loads(await asyncio.wait_for(read(snap['order']['Name'], **kwargs), timeout=60))
            except Exception as error:
                evidence['error'] = str(error) or '回读超时。'
    except asyncio.CancelledError:
        outcome['interrupted'] = True
        raise
    finally:
        with db_connect() as db:
            db.execute("UPDATE plans SET state='unknown',outcome=? WHERE id=? AND state='in_flight'",
                       (json.dumps(outcome,ensure_ascii=False), plan_id))
            db.execute('UPDATE attempts SET finished=? WHERE plan_id=? AND attempt_id=?', (time.time(),plan_id,attempt_id))
    return encode(outcome)
