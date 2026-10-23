"""CollectData non-executing server preview, based on live subtype metadata."""
import json
import re
import hashlib
import sqlite3
import time
import uuid
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

PLAN_DB = Path(__file__).resolve().parents[1] / 'data' / 'collection_plans.sqlite3'


def db_connect():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB)
    db.execute('CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, digest TEXT, payload TEXT, state TEXT, created REAL, outcome TEXT)')
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
    if kind == 4 and isinstance(value, str):
        return value
    if kind in (1, 2, 3, 9) and not isinstance(value, bool) and isinstance(value, (int, float)):
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError('数值必须有限。')
        if kind == 1 and (number != number.to_integral_value() or not -2147483648 <= number <= 2147483647):
            raise ValueError('Integer 需要32位整数。')
        return format(number, 'f')
    raise ValueError('本阶段仅支持数值和字符串的提交预览；布尔、时间和对象值转换尚待验证。')


async def _preview(container_name: str, values: dict,
                                             name: str = '', revision: str = '',
                                             instance_id: str | None = None, use_ror: bool = False,
                                             container_instance_id: str | None = None, save_plan: bool = True) -> str:
    """采集提交预览：读取定义/容器/工序映射，构造 UserDataCollection 请求并以 execute=false 验证服务回显，绝不执行采集。只支持数值和字符串；executable=false，不代表工序、权限、质量或提交成功。"""
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
                                             container_instance_id: str | None = None) -> str:
    """只预览采集，不执行事务。核对明确版本、容器工序映射、采集点及 execute=false 回显；仅数值/字符串。满足条件才生成有效1小时的单次计划，执行仍须确认；现场验收待补。"""
    return await _preview(container_name, values, name, revision, instance_id, use_ror, container_instance_id)


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
    """确认后执行单次采集计划：重新预览检查工序/版本/值/约束变化，原子领取，POST不重试。超时或缺失关联标识返回unknown；历史回读仅证据，不擅自报告核实成功。现场验收待补。"""
    with db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
    if not row or row[0] != plan_digest:
        return encode({'error': '计划不存在或摘要不匹配。', 'submitted': False})
    if row[2] != 'ready':
        return encode({'state': row[2], 'error': '计划已使用，禁止再次提交。',
                       'outcome': json.loads(row[4]) if row[4] else None})
    if time.time() - row[3] > 3600:
        return encode({'error': '计划已过期，请重新预览。', 'submitted': False})
    stored = json.loads(row[1])
    refreshed = json.loads(await _preview(**stored['arguments'], save_plan=False))
    if not refreshed.get('executable') or refreshed.get('error') or digest(snapshot(refreshed)) != plan_digest:
        return encode({'error': '工序、定义、值或约束变化/无法重新确认，未提交。', 'submitted': False})
    # Claim before the network request: a crash or timeout must never reopen this plan.
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE')
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
    if not changed:
        return encode({'error': '计划已被领取，禁止重复提交。', 'submitted': False})
    outcome = {'state': 'unknown', 'submission_attempted': True, 'verified_success': False,
               'note': '不自动重试。没有可验证的事务关联标识时，历史回读不能证明本次提交成功。'}
    try:
        raw = await request('POST', '/api/CollectData', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                            body=stored['snapshot']['body'], params={'execute': 'true'}, response_formatter=raw_json)
        outcome['response'] = raw
        # No tested response-to-history correlation contract yet. Preserve unknown even for HTTP 200.
        from tools.collection_history import get_container_collection_history
        args = stored['arguments']
        outcome['history_evidence'] = json.loads(await get_container_collection_history(
            args['container_name'], args['container_instance_id']))
    except Exception as error:
        outcome['error'] = str(error)
    with db_connect() as db:
        db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',
                   ('unknown', json.dumps(outcome, ensure_ascii=False), plan_id))
    return encode(outcome)
