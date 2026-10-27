"""CollectData non-executing server preview, based on live subtype metadata."""
import json
import re
import hashlib
import sqlite3
import time
import uuid
import asyncio
from pathlib import Path
from decimal import Decimal

from tools import mcp
from tools.boms import encode
from tools.collection_validation import validate_collection_values
from tools.data_collection import get_container_collection_requirements
from tools.container_inquiry import get_container_details
from tools.modeling_details import raw_json, _has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL
from core.transfer_attempt_lock import attempt_lock, AttemptBusy

PLAN_DB = Path(__file__).resolve().parents[1] / 'data' / 'collection_plans.sqlite3'


def db_connect():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB)
    db.execute('CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, digest TEXT, payload TEXT, state TEXT, created REAL, outcome TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS collection_attempts (plan_id TEXT PRIMARY KEY,attempt_id TEXT,started REAL,finished REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS collection_reviews (plan_id TEXT,kind TEXT,record TEXT,created REAL,PRIMARY KEY(plan_id,kind))')
    return db


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def snapshot(preview):
    return {'environment': CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),
            'body': preview['request_preview']['body'], 'spec': preview['context'].get('spec'),
            'mappings': preview['context'].get('mappings'),
            'validation': preview['validation'], 'server_points': preview['server_points']}


def confirmation_summary(plan_id, plan_digest):
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created FROM plans WHERE id=?', (plan_id,)).fetchone()
    if not row or row[0] != plan_digest or row[2] != 'ready' or time.time() - row[3] > 3600:
        return '采集计划无效、已使用或已过期，请重新预览。'
    body = json.loads(row[1])['snapshot']['body']
    return '提交采集：' + json.dumps(body, ensure_ascii=False) + '\n不自动覆盖越限；提交后可能需要人工核实历史。'

PREVIEW_PARAMS = {
    'execute': 'false',
    '$select': 'Container,DataCollectionDef,HasUserDataCollection,ParametricDataDefType',
    '$expand': 'ParametricData($expand=shopfloor.DataPointSummary/DataPointDetails)',
}


def identifier(ref):
    return (ref.get('InstanceId') or ref.get('InstanceID')) if isinstance(ref, dict) else None


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{16}', value) is not None


def true(value):
    return value is True or value == 'true'


def serialize(check):
    value, kind = check['value'], check['data_type']
    if kind == 7 and type(value) is bool:
        return 'true' if value else 'false'
    if kind == 6:
        from tools.collection_validation import timestamp_valid
        if timestamp_valid(value):return value
    if kind == 4 and isinstance(value, str):
        return value
    if kind in (1, 2, 3, 9) and not isinstance(value, bool) and isinstance(value, (int, float)):
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError('数值必须有限。')
        if kind == 1 and (number != number.to_integral_value() or not -2147483648 <= number <= 2147483647):
            raise ValueError('Integer 需要32位整数。')
        return format(number, 'f')
    raise ValueError('采集值类型无效；对象引用提交须额外核实CDO契约，暂不接受。')


async def _preview(container_name: str, values: dict,
                                             name: str = '', revision: str = '',
                                             instance_id: str | None = None, use_ror: bool = False,
                                             container_instance_id: str | None = None, save_plan: bool = True,
                                             collection_kind: str = 'user',request_marker: str = '') -> str:
    """Build and verify a non-executing server preview; persist only fully mapped plans."""
    if collection_kind=='point':
        from tools.collection_groups import preview_groups
        return await preview_groups(container_name,values,name,revision,instance_id,use_ror,container_instance_id,save_plan,request_marker)
    if collection_kind!='user':return encode({'executable':False,'error':'collection_kind须为user或point。'})
    out = {'complete': False, 'executable': False, 'stage': 'server_preview',
           'transaction_executed': False, 'server_echo_verified': False,
           'blockers': ['尚未完成真实提交和按事务标识回读验收；预览不能授权执行。']}
    try:
        if not container_name.strip() or (container_instance_id is not None and not valid_id(container_instance_id)):
            raise ValueError('容器名必填，显式容器ID须为16位十六进制。')
        validation = json.loads(await validate_collection_values(values, name, revision, instance_id, use_ror))
        out['validation'] = validation
        if validation.get('static_valid') is not True or validation.get('errors'):
            raise ValueError('静态值校验未通过，未调用服务预览。')
        definition = validation['definition']
        if not valid_id(definition.get('InstanceID')) or not definition.get('Name') or not definition.get('Revision'):
            raise ValueError('采集定义身份不完整。')
        details = []
        for check in validation['checks']:
            if not valid_id(check.get('point_instance_id')):
                raise ValueError('采集点缺少明确ID。')
            details.append({'dataPoint': {'name': check['point'], 'instanceId': check['point_instance_id']},
                            'dataValue': serialize(check)})
        if len(details) != len(values):
            raise ValueError('存在空值或未序列化采集点，不能静默省略。')
        info = json.loads(await get_container_details(container_name, container_instance_id, view='info'))
        ref = info.get('data', {}).get('Container')
        cid = identifier(ref)
        if (info.get('error') or not valid_id(cid) or ref.get('Name') != container_name.strip()
                or (container_instance_id is not None and cid != container_instance_id)):
            raise ValueError('容器身份未确认。')
        context = json.loads(await get_container_collection_requirements(
            container_name, cid, transaction_name='CollectData', definition_kind='user'))
        out['context'] = context
        matches = [m for m in context.get('mappings', [])
                   if m.get('definition', {}).get('definition', {}).get('InstanceID') == definition['InstanceID']]
        out['static_mapping_verified'] = (not context.get('error') and context.get('static_mappings_complete') is True
                                          and len(matches) == 1 and matches[0]['definition'].get('complete') is True
                                          and matches[0]['definition']['definition'].get('Revision') == definition['Revision'])
        if not out['static_mapping_verified']:
            out['blockers'].append('当前工序与 CollectData 采集定义关联未完整确认。')
        payload = {'container': {'name': ref['Name'], 'instanceId': cid},
                   'dataCollectionDef': {'name': definition['Name'], 'revision': definition['Revision'],
                                         'instanceId': definition['InstanceID']},
                   'parametricData': {'@odata.type': '#shopfloor.DataPointSummary', 'dataPointDetails': details}}
        if save_plan and not request_marker:request_marker='MCP-COL:'+uuid.uuid4().hex
        if request_marker:
            if not re.fullmatch(r'MCP-COL:[0-9a-f]{32}',request_marker):raise ValueError('采集关联标记无效。')
            payload['comments']=request_marker
        out['request_preview'] = {'method': 'POST', 'path': '/api/CollectData',
                                  'params': dict(PREVIEW_PARAMS), 'body': payload}
        raw = await request('POST', '/api/CollectData', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                            body=payload, params=dict(PREVIEW_PARAMS), response_formatter=raw_json)
        response = json.loads(raw)
        data = response.get('data', response)
        if response.get('error') or data.get('error') or data.get('ErrorCode') or _has_next_link(response):
            raise ValueError('服务预览报错或返回不完整。')
        if (identifier(data.get('Container')) != cid or data['Container'].get('Name') != ref['Name']
                or identifier(data.get('DataCollectionDef')) != definition['InstanceID']
                or data['DataCollectionDef'].get('Revision') != definition['Revision']
                or data['DataCollectionDef'].get('Name') != definition['Name']
                or not true(data.get('HasUserDataCollection'))
                or str(data.get('ParametricDataDefType')) != '7273'):
            raise ValueError('服务预览容器、定义版本或采集类型不匹配。')
        summary = data.get('ParametricData', {})
        rows = summary.get('DataPointDetails')
        if str(summary.get('CDOTypeId')) != '7273' or not isinstance(rows, list) or len(rows) != len(details):
            raise ValueError('服务未回显完整的 DataPointSummary 明细。')
        seen = set()
        expected = {d['dataPoint']['instanceId']: d for d in details}
        for row in rows:
            point = row.get('DataPoint', {})
            pid = identifier(point)
            wanted = expected.get(pid)
            if (wanted is None or pid in seen or identifier(point.get('Parent')) != definition['InstanceID']
                    or point.get('Name') != wanted['dataPoint']['name'] or row.get('DataValue') != wanted['dataValue']):
                raise ValueError('服务回显采集点或值与预览不一致。')
            seen.add(pid)
        out['server_echo_verified'] = True
        out['server_points'] = [{k: row.get(k) for k in ('DataPoint', 'DataName', 'DataValue', 'DataType',
                                'IsRequired', 'LowerLimit', 'UpperLimit', 'IsLimitViolation', 'IsLimitOverrideAllowed')}
                                for row in rows]
        out['note'] = '仅确认 execute=false 模式的序列化与回显；越限标记可能尚未经过 Execute 验证，不能视为质量结论。'
        if out['static_mapping_verified']:
            out['blockers'] = []
            out['executable'] = True
            out['acceptance'] = '现场提交/非空历史验收尚未完成；执行工具必须经过确认。'
            if save_plan:
                snap = snapshot(out)
                plan_digest, plan_id = digest(snap), uuid.uuid4().hex
                stored = {'snapshot': snap, 'arguments': {'container_name': ref['Name'], 'values': values,
                          'instance_id': definition['InstanceID'], 'container_instance_id': cid}}
                if request_marker:stored['arguments']['request_marker']=request_marker
                with db_connect() as db:
                    db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',
                               (plan_id, plan_digest, json.dumps(stored, ensure_ascii=False), 'ready', time.time(), None))
                out.update(plan_id=plan_id, plan_digest=plan_digest, expires_in_seconds=3600)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)


@mcp.tool
async def preview_container_data_collection(container_name: str, values: dict, name: str = '', revision: str = '',
                                             instance_id: str | None = None, use_ror: bool = False,
                                             container_instance_id: str | None = None, collection_kind: str = 'user') -> str:
    """只预览采集，不执行事务。user的values为{点名:值}，point分组为{组名:{点名:[样本值]}}。支持数字/字符串/布尔/带时区时间；对象引用暂阻断。核对版本、工序映射、execute=false回显，通过才生成1小时单次计划，执行须确认；分组未完成现场事务验收。"""
    return await _preview(container_name, values, name, revision, instance_id, use_ror, container_instance_id,
                          collection_kind=collection_kind)


@mcp.tool
async def get_collection_submission_plan(plan_id: str) -> str:
    """查询采集计划及持久化执行结果；unknown/in_flight 不能重试，历史空页不是失败证明。"""
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
    if not row:
        return encode({'error': '计划不存在。'})
    return encode({'plan_id': plan_id, 'plan_digest': row[0], 'plan': json.loads(row[1]),
                   'state': row[2], 'expired': time.time() - row[3] > 3600,
                   'outcome': json.loads(row[4]) if row[4] else None})


@mcp.tool
async def collect_container_data(plan_id: str, plan_digest: str) -> str:
    """确认后执行普通/分组单次采集计划：重新预览工序/版本/值/约束并原子领取，POST不重试。结果先unknown，新标记计划可用verify_collection_submission核实完整历史；旧计划保留人工审计。现场验收待补。"""
    try:
        with attempt_lock(PLAN_DB,'collection-domain'), attempt_lock(PLAN_DB, plan_id):
            return await collect_locked(plan_id, plan_digest)
    except AttemptBusy as error:
        return encode({'submitted':False,'error':str(error)})


async def collect_locked(plan_id, plan_digest):
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
    if not row or row[0] != plan_digest:
        return encode({'error': '计划不存在或摘要不匹配。', 'submitted': False})
    if row[2] != 'ready':
        return encode({'state': row[2], 'submitted':False, 'error': '计划已使用，禁止再次提交。',
                       'outcome': json.loads(row[4]) if row[4] else None})
    if time.time() - row[3] > 3600:
        return encode({'error': '计划已过期，请重新预览。', 'submitted': False})
    try:
        stored = json.loads(row[1])
        if digest(stored['snapshot']) != plan_digest or stored['snapshot']['environment'] != CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'):
            raise ValueError('存储计划摘要或环境不一致。')
        snap = stored['snapshot']
        from tools.collection_modeling import assert_no_pending_changes
        assert_no_pending_changes(snap)
        checks = snap['validation']['checks']
        bound_values = {check['point']:check['value'] for check in checks}
        expected_arguments = {'container_name':snap['body']['container']['name'],
                              'container_instance_id':snap['body']['container']['instanceId'],
                              'instance_id':snap['body']['dataCollectionDef']['instanceId'],'values':bound_values}
        if snap['body']['parametricData'].get('@odata.type')=='#shopfloor.DPCollection':
            bound_values=snap['validation']['values']
            expected_arguments.update(values=bound_values,collection_kind='point')
        if snap['body'].get('comments'):expected_arguments['request_marker']=snap['body']['comments']
        if ((expected_arguments.get('collection_kind')!='point' and len(bound_values)!=len(checks))
                or digest(stored['arguments']) != digest(expected_arguments)):
            raise ValueError('存储重读参数与已审阅采集快照不一致。')
        refreshed = json.loads(await asyncio.wait_for(_preview(**stored['arguments'], save_plan=False),timeout=90))
        if not refreshed.get('executable') or refreshed.get('error') or digest(snapshot(refreshed)) != plan_digest:
            raise ValueError('工序、定义、值或约束变化/无法重新确认，未提交。')
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        return encode({'error':str(error) or '提交前核对超时。','submitted':False})
    # Claim before the network request: a crash or timeout must never reopen this plan.
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
        if current != row:
            return encode({'error':'领取前存储计划已变化或已使用。','submitted':False})
        unresolved = db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')").fetchall()
        target = stored['snapshot']['body']['container']['instanceId']
        if any(json.loads(item[0])['snapshot']['body']['container']['instanceId'] == target
               and json.loads(item[0])['snapshot']['environment'] == stored['snapshot']['environment']
               for item in unresolved):
            return encode({'error': '该容器存在未核实的采集提交；须先人工核对，不能新建计划绕过重试限制。', 'submitted': False})
        if time.time() - row[3] > 3600:
            return encode({'error': '重新核对期间计划已过期。', 'submitted': False})
        changed = db.execute("UPDATE plans SET state='in_flight' WHERE id=? AND state='ready' AND digest=?",
                             (plan_id, plan_digest)).rowcount
        if changed:
            attempt_id = uuid.uuid4().hex
            db.execute('INSERT INTO collection_attempts VALUES (?,?,?,NULL)',(plan_id,attempt_id,time.time()))
    if not changed:
        return encode({'error': '计划已被领取，禁止重复提交。', 'submitted': False})
    outcome = {'state': 'unknown', 'submission_attempted': True, 'verified_success': False,
               'attempt_id':attempt_id,'retry_allowed':False,
               'note': '不自动重试。没有可验证的事务关联标识时，历史回读不能证明本次提交成功。'}
    try:
        raw = await request('POST', '/api/CollectData', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                            body=stored['snapshot']['body'], params={'execute': 'true'}, response_formatter=raw_json)
        outcome['response'] = raw
        # No tested response-to-history correlation contract yet. Preserve unknown even for HTTP 200.
        from tools.collection_history import get_container_collection_history
        args = stored['arguments']
        history_reader=get_container_collection_history
        if args.get('collection_kind')=='point':
            from tools.collection_group_history import get_grouped_collection_history
            history_reader=get_grouped_collection_history
        outcome['history_evidence'] = json.loads(await asyncio.wait_for(history_reader(
            args['container_name'], args['container_instance_id']),timeout=60))
    except asyncio.CancelledError:
        outcome['interrupted'] = True
        outcome['error'] = '本地任务取消，服务端结果未知；不允许自动重试。'
        raise
    except Exception as error:
        outcome['error'] = str(error)
    finally:
        with db_connect() as db:
            db.execute("UPDATE plans SET state=?,outcome=? WHERE id=? AND state='in_flight'",
                       ('unknown', json.dumps(outcome, ensure_ascii=False), plan_id))
            db.execute('UPDATE collection_attempts SET finished=? WHERE plan_id=? AND attempt_id=?',
                       (time.time(),plan_id,attempt_id))
    return encode(outcome)
