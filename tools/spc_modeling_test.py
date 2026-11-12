"""Create an unbound, fixed SPC modeling demonstration, never engine configuration.

Six new roots only. No credentials, connections, production binding, compilation,
measurement submission or transaction execution. Each POST is durable and single-use.
"""
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from jsonschema import validate
from pydantic import StrictBool

from config import CAMSTAR_BASE_URL, SAFE_CREATE_THRESHOLD
from core.http_client import get_client, get_headers
from core.transfer_attempt_lock import attempt_lock, AttemptBusy
from tools import mcp
from tools.collection_submission import digest
from tools.modeling_details import raw_json, _has_next_link
from tools.process_context import ReadContext
from tools import quality as q

DB = Path(__file__).resolve().parents[1] / 'data/spc_modeling_tests.sqlite3'
NOTE = 'MCP synthetic modeling demo ONLY; unbound, no native engine, no compiled rules, no release or production use.'
EXPAND = {'SPCRules': 'Violations($expand=SPCViolation)',
          'SPCChartDefs': 'SPCChartType,SPCQuery,SPCRules', 'SPCChartDefGroups': 'Entries'}


def models(prefix):
    if not isinstance(prefix, str) or not re.fullmatch(r'CDX-C6-MODEL-[A-Z0-9-]{1,32}', prefix):
        raise ValueError('测试前缀须为CDX-C6-MODEL-后接1..32位大写字母、数字或连字符。')
    ref = lambda suffix: {'name': prefix+'-'+suffix}
    def item(entity, suffix, **fields):
        return {'entity': entity, 'body': {'name': prefix+'-'+suffix, 'description': NOTE, **fields}}
    query = ' UNION ALL '.join(f'SELECT {i} AS SampleNo, CAST({v} AS decimal(18,4)) AS TestValue'
                              for i, v in enumerate((9, 10, 11, 14), 1))
    return [item('SPCChartTypes', 'TYPE'), item('SPCViolations', 'VIOLATION'),
            item('SPCQueries', 'QUERY', queryText=query),
            item('SPCRules', 'RULES', violations=[{'spcViolation': ref('VIOLATION')}]),
            item('SPCChartDefs', 'CHART', chartVariable='TestValue', spcChartDisplayOption=3,
                 spcChartSaveOptions=3, triggerOCAPs=False, requireAnnotation=False,
                 recordAgainstCustomSPCDataSrc=False, runTimeDisplay=False,
                 spcChartType=ref('TYPE'), spcQuery=ref('QUERY'), spcRules=ref('RULES')),
            item('SPCChartDefGroups', 'GROUP', entries=[ref('CHART')])]


def database():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,digest TEXT,payload TEXT,state TEXT,created REAL,outcome TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS attempts(plan_id TEXT,entity TEXT,body TEXT,status INTEGER,response TEXT,PRIMARY KEY(plan_id,entity))')
    db.execute('CREATE TABLE IF NOT EXISTS claims(scope TEXT PRIMARY KEY,plan_id TEXT)')
    return db


def scope(prefix):
    return digest({'environment': CAMSTAR_BASE_URL.rstrip('/'), 'prefix': prefix})


def name_lengths(items):
    # Live SPCViolation.SPCRuleViolationName is nvarchar(30); this deployment's
    # Swagger omits maxLength. Keep every demonstration name within that bound.
    if any(len(item['body']['name']) > 30 for item in items):
        raise ValueError('测试对象名称最长30字符，请缩短前缀（例如CDX-C6-MODEL-T1）。')


async def http(method, path, **kwargs):
    return await get_client().request(method, CAMSTAR_BASE_URL.rstrip('/')+path,
                                     headers=get_headers(), follow_redirects=False, **kwargs)


async def contracts(items):
    result = {}
    for item in items:
        entity = item['entity']
        response = await http('GET', '/swagger/'+entity+'/swagger.json')
        response.raise_for_status()
        document = response.json()
        content = document['paths']['/api/'+entity]['post']['requestBody']['content']
        schema = content['application/json']['schema']
        validate(item['body'], {**schema, 'components': document['components']})
        result[entity] = digest(document)
    return result


async def find(entity, name):
    data = await ReadContext(1).get(entity, params={'$filter': "Name eq '"+name+"'", '$top': 2})
    rows = data.get('value')
    if not isinstance(rows, list) or len(rows) > 1 or _has_next_link(data):
        raise ValueError('对象名称查询不完整或不唯一：'+entity)
    if rows and (rows[0].get('Name') != name or not re.fullmatch(r'[0-9a-fA-F]{16}', rows[0].get('InstanceID', ''))):
        raise ValueError('对象名称或ID不匹配：'+entity)
    return rows[0] if rows else None


async def ensure_absent(items):
    for item in items:
        if await find(item['entity'], item['body']['name']):
            raise ValueError('测试名称已存在，不覆盖或认领：'+item['body']['name'])


def matches(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(matches(value, next((v for k,v in actual.items()
            if k.casefold() == key.casefold()), None)) for key,value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(matches(e,a) for e,a in zip(expected,actual))
    return type(actual) is type(expected) and actual == expected


async def verify(item):
    entity, expected = item['entity'], item['body']
    found = await find(entity, expected['name'])
    if found is None:
        raise ValueError('测试对象尚未读回：'+entity)
    actual = await ReadContext(1).get(entity, found['InstanceID'])
    if entity in EXPAND:
        expanded = await ReadContext(1).get(entity, found['InstanceID'], {'$select': '*', '$expand': EXPAND[entity]})
        if expanded.get('InstanceID') != found['InstanceID'] or expanded.get('Name') != expected['name'] or _has_next_link(expanded):
            raise ValueError('展开关联与测试对象身份不一致：'+entity)
        # Some deployments project scalars as null when $expand is supplied,
        # even with $select=*. Only use that response for the requested refs.
        for field, value in expected.items():
            if isinstance(value, (dict, list)):
                key = next((k for k in expanded if k.casefold() == field.casefold()), field)
                actual = {**actual, key: expanded.get(key)}
    # Generic native entities can omit inherited Description in GET projection.
    # Verify the stored field and exact identity, never interpret null as saved.
    description_sources = {'SPCRules': ('A_SPCRules', 'SPCRulesId', 'SPCRulesName'),
                           'SPCChartDefGroups': ('NamedObjectGroup', 'NamedObjectGroupId', 'NamedObjectGroupName')}
    if actual.get('Description') is None and entity in description_sources:
        table, key, name = description_sources[entity]
        rows = await q.sql_rows(f'SELECT TOP 2 {key},{name},Description FROM {table} WHERE {key}='+q.literal(found['InstanceID']), 2)
        if len(rows) != 1 or rows[0][key] != found['InstanceID'] or rows[0][name] != expected['name']:
            raise ValueError('测试说明的持久身份未核实：'+entity)
        actual = {**actual, 'Description': rows[0]['Description']}
    if actual.get('InstanceID') != found['InstanceID'] or _has_next_link(actual) or not matches(expected, actual):
        raise ValueError('持久字段/关联未通过读回核实：'+entity)
    return {'entity': entity, 'name': expected['name'], 'instance_id': found['InstanceID'], 'verified': True}


def load(plan_id, plan_digest):
    if not re.fullmatch(r'[0-9a-f]{32}', plan_id) or not re.fullmatch(r'[0-9a-f]{64}', plan_digest):
        raise ValueError('须使用准备工具返回的计划ID和摘要。')
    with database() as db:
        row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
    if row is None or row[0] != plan_digest:
        raise ValueError('测试计划不存在或摘要不匹配。')
    plan = json.loads(row[1])
    if digest(plan) != plan_digest or plan['environment'] != CAMSTAR_BASE_URL.rstrip('/') or plan['models'] != models(plan['prefix']):
        raise ValueError('测试计划或环境已变化。')
    return plan, row[2], row[3], json.loads(row[4]) if row[4] else None


@mcp.tool
async def prepare_spc_modeling_test(prefix: str) -> str:
    """只读准备六个独立SPC测试建模对象，前缀CDX-C6-MODEL-；不写MES。
    固定图类型/违例定义/合成查询/未编译规则/NoChartNever图/图组，展示完整字段。
    不创建连接、凭据或生产关联；不是实际测量、原生计算或违例历史。计划1小时有效。
    """
    try:
        items = models(prefix)
        name_lengths(items)
        if SAFE_CREATE_THRESHOLD < len(items):
            raise ValueError('六个测试根对象超过当前创建阈值。')
        with database() as db:
            if db.execute('SELECT 1 FROM claims WHERE scope=?', (scope(prefix),)).fetchone():
                raise ValueError('该测试前缀已有执行尝试，不可生成替代计划重放。')
        schema_hashes = await contracts(items)
        await ensure_absent(items)
        plan = {'prefix': prefix, 'environment': CAMSTAR_BASE_URL.rstrip('/'), 'models': items,
                'contracts': schema_hashes, 'root_count': 6, 'native_engine_ready': False,
                'connection_created': False, 'production_binding_created': False}
        key, fingerprint = uuid.uuid4().hex, digest(plan)
        with database() as db:
            db.execute('INSERT INTO plans VALUES(?,?,?,?,?,NULL)', (key, fingerprint, raw_json(plan), 'ready', time.time()))
        return raw_json({'executable': True, 'plan_id': key, 'plan_digest': fingerprint, 'plan': plan})
    except Exception as error:
        return raw_json({'executable': False, 'error': str(error)[:2000], 'mes_write_performed': False})


@mcp.tool
async def execute_spc_modeling_test(plan_id: str, plan_digest: str, continue_unattempted: StrictBool = False) -> str:
    """用户授权后单次创建已审阅的六个SPC测试根对象；只新增固定测试名称。
    每个POST前持久记账并逐个读回；任何失败/超时/中断停止且不重放，允许部分成功。
    continue_unattempted=true仅在此前每笔都有持久2xx无错误响应且当前全部字段读回通过后，
    继续从未尝试的后续对象；任何无响应、拒绝或不匹配均不能继续。不重发已有POST。
    不安装引擎、不编译规则、不建连接、不挂生产定义、不采集、不登记违例/注释。
    """
    outcome = None
    try:
        plan, state, created, previous = load(plan_id, plan_digest)
        if state != 'ready' and not (continue_unattempted is True and state in ('unknown', 'attempting')):
            return raw_json({'state': state, 'outcome': previous, 'replayed': False,
                             'error': '该计划已尝试，禁止再次提交；只能只读核实。'})
        if time.time()-created > 3600 or SAFE_CREATE_THRESHOLD < 6:
            raise ValueError('计划已过期或当前创建阈值不足。')
        name_lengths(plan['models'])
        with attempt_lock(DB, scope(plan['prefix'])):
            with database() as db:
                if continue_unattempted:
                    owner = db.execute('SELECT plan_id FROM claims WHERE scope=?', (scope(plan['prefix']),)).fetchone()
                    if not owner or owner[0] != plan_id:
                        raise ValueError('此前执行占用不属于原计划。')
                elif db.execute('INSERT OR IGNORE INTO claims VALUES(?,?)', (scope(plan['prefix']), plan_id)).rowcount != 1:
                    raise ValueError('该测试前缀已有执行尝试，禁止跨计划重放。')
                if db.execute("UPDATE plans SET state='attempting' WHERE id=? AND state=?", (plan_id, state)).rowcount != 1:
                    raise ValueError('该计划已被其他执行占用。')
            outcome = {'plan_id': plan_id, 'state': 'unknown', 'objects': [], 'native_engine_ready': False,
                       'connection_created': False, 'production_binding_created': False}
            try:
                if await contracts(plan['models']) != plan['contracts']:
                    raise ValueError('现场写入契约变化，未开始创建。')
                start = 0
                if continue_unattempted:
                    with database() as db:
                        attempts = db.execute('SELECT entity,body,status,response FROM attempts WHERE plan_id=? ORDER BY rowid', (plan_id,)).fetchall()
                    if not attempts or len(attempts) > 6:
                        raise ValueError('此前完整提交证据缺失，不能继续。')
                    for item, (entity, body, status, reply) in zip(plan['models'], attempts):
                        if entity != item['entity'] or json.loads(body) != item['body'] or not status or not 200 <= status < 300:
                            raise ValueError('此前有拒绝、无响应或请求不匹配，不能继续。')
                        try:
                            response_data = json.loads(reply)
                        except (ValueError, TypeError):
                            raise ValueError('此前无完整原生响应，不能继续。') from None
                        if not isinstance(response_data, dict) or response_data.get('ErrorCode') or response_data.get('error'):
                            raise ValueError('此前原生响应包含错误，不能继续。')
                        outcome['objects'].append(await verify(item))
                    start = len(attempts)
                await ensure_absent(plan['models'][start:])
                for item in plan['models'][start:]:
                    entity, body = item['entity'], item['body']
                    with database() as db:
                        db.execute('INSERT INTO attempts(plan_id,entity,body) VALUES(?,?,?)', (plan_id, entity, raw_json(body)))
                    response = await http('POST', '/api/'+entity, json=body)
                    with database() as db:
                        db.execute('UPDATE attempts SET status=?,response=? WHERE plan_id=? AND entity=?',
                                   (response.status_code, response.text, plan_id, entity))
                    if not 200 <= response.status_code < 300:
                        raise ValueError('原生建模请求未获成功响应，停止且不重试：'+entity+' HTTP '+str(response.status_code))
                    try:
                        reply = response.json()
                    except ValueError:
                        reply = None
                    if isinstance(reply, dict) and (reply.get('ErrorCode') or reply.get('error')):
                        raise ValueError('原生建模响应包含错误，停止且不重试：'+entity)
                    outcome['objects'].append(await verify(item))
                # Final reread includes all relationships after the last object was created.
                outcome['objects'] = [await verify(item) for item in plan['models']]
                outcome['state'] = 'verified'
            except Exception as error:
                outcome['error'] = str(error)[:2000]
            finally:
                with database() as db:
                    db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?', (outcome['state'], raw_json(outcome), plan_id))
            return raw_json(outcome)
    except (ValueError, AttemptBusy) as error:
        return raw_json({'state': 'not_submitted', 'error': str(error)[:2000]})


@mcp.tool
async def inspect_spc_modeling_test(plan_id: str, plan_digest: str) -> str:
    """只读核对原测试计划及六个建模对象，不补建、不更新、不解除unknown，不作提交成功归因。"""
    try:
        plan, state, _, outcome = load(plan_id, plan_digest)
        rows = []
        for item in plan['models']:
            try:
                rows.append(await verify(item))
            except Exception as error:
                rows.append({'entity': item['entity'], 'name': item['body']['name'], 'verified': False, 'error': str(error)[:1000]})
        return raw_json({'state': state, 'objects': rows, 'complete': all(row['verified'] for row in rows),
                         'original_outcome': outcome, 'mes_write_performed': False, 'native_engine_ready': False})
    except ValueError as error:
        return raw_json({'error': str(error), 'mes_write_performed': False})
