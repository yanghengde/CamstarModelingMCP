"""UOM master data, verified against the archived Modeling UOM Swagger."""
import json
import re
from urllib.parse import quote

from tools import mcp
from core.http_client import request
from core.modeling_payload import build_update_payload
from tools.modeling_details import raw_json, _has_next_link


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(c in value for c in '\r\n\x00'):
        raise ValueError('单位名称须为1..128字符的非空字符串。')
    return value.strip()


def _object(raw):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('单位读取失败：' + str(raw)) from None
    if not isinstance(data, dict) or data.get('error') or data.get('ErrorCode'):
        raise ValueError('单位响应无效：' + str(raw))
    return data


async def _lookup(name):
    data = _object(await request('GET', '/api/UOMs', params={
        '$filter': "Name eq '" + name.replace("'", "''") + "'", '$top': 2}, response_formatter=raw_json))
    rows = data.get('value')
    if _has_next_link(data) or not isinstance(rows, list) or len(rows) > 1:
        raise ValueError('单位名称查询不完整或不唯一。')
    if any(not isinstance(row, dict) or row.get('Name') != name for row in rows):
        raise ValueError('单位名称查询身份不符。')
    return rows


async def _read(key):
    data = _object(await request('GET', '/api/UOMs/' + quote(key, safe=''), response_formatter=raw_json))
    if _has_next_link(data) or data.get('InstanceID') != key or not data.get('Name'):
        raise ValueError('单位ID或响应完整性未核实。')
    return data


def _key(key):
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-fA-F]{16}', key):
        raise ValueError('key 须为查询返回的实际16位 UOM InstanceID。')


def _payload(fields):
    return build_update_payload('UOMs', fields)


def _checks(row, changes):
    return {k: (k[0].upper() + k[1:]) in row and row[k[0].upper() + k[1:]] == v
            for k, v in changes.items()}


def _outcome(raw):
    if str(raw).startswith(('❌', 'Error')):
        return 'rejected' if re.search(r'HTTP (400|401|403|404|405|409|422)\b', str(raw)) else 'unknown'
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return 'acknowledged' if str(raw).startswith('✅') else 'unknown'
    return 'unknown' if not isinstance(data, dict) or data.get('error') or data.get('ErrorCode') else 'acknowledged'


async def _verify(out, key, changes):
    out['instance_id'] = key
    after = await _read(key)
    checks = _checks(after, changes)
    out.update(after=after, field_checks=checks, observed_matches=all(checks.values()))
    out['verified'] = out['submission_state'] == 'acknowledged' and all(checks.values())
    if not out['verified']:
        out['error'] = '提交或回读尚未验证；当前同值不能证明未知请求成功，请只读核实，勿自动重发。'


@mcp.tool
async def list_uoms(name_contains: str = '', offset: int = 0, limit: int = 20) -> str:
    """分页查询计量单位 UOM；用于创建采集点/检验指标/工单前选择真实单位。
    返回 has_more/next_offset/complete；单位名称不代表已配置自动换算。
    """
    try:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('offset>=0，limit=1..100。')
        params = {'$top': limit + 1, '$skip': offset, '$orderby': 'Name'}
        if name_contains:
            params['$filter'] = "contains(Name,'" + name_contains.replace("'", "''") + "')"
        data = _object(await request('GET', '/api/UOMs', params=params, response_formatter=raw_json))
        rows = data.get('value')
        if not isinstance(rows, list):
            raise ValueError('单位列表未返回。')
        more = len(rows) > limit or _has_next_link(data)
        return raw_json({'source': 'UOMs', 'rows': rows[:limit], 'has_more': bool(more),
                         'next_offset': offset + min(limit, len(rows)) if more else None,
                         'complete': offset == 0 and not more})
    except (ValueError, TypeError) as exc:
        return raw_json({'error': str(exc)})


@mcp.tool
async def get_uom(key: str) -> str:
    """按单位名称或实际 InstanceID 查询一个 UOM，保留完整字段。"""
    try:
        key = _name(key)
        return await request('GET', '/api/UOMs/' + quote(key, safe=''), response_formatter=raw_json)
    except ValueError as exc:
        return raw_json({'error': str(exc)})


@mcp.tool
async def create_uom(name: str, description: str | None = None, notes: str | None = None) -> str:
    """创建明确名称的计量单位，可附描述/备注。先查同名，单次POST后回读核验。
    已存在时不覆盖。仅维护单位主数据；不创建换算关系或修改已有定义的单位。
    未明确要求新增时先向用户明确所需名称；结果未知时只读核实，不重发。
    """
    out = {'operation': 'create_uom', 'submission_state': 'not_submitted', 'verified': False}
    try:
        name = _name(name)
        changes = _payload({'name': name, 'description': description, 'notes': notes})
        out['requested'] = changes
        if await _lookup(name):
            raise ValueError('单位名称已存在，未创建或覆盖；请直接引用已有单位。')
    except (ValueError, TypeError) as exc:
        return raw_json({**out, 'error': str(exc)})
    raw = await request('POST', '/api/UOMs', body=changes, response_formatter=raw_json)
    out.update(submission_state=_outcome(raw), write_response=raw)
    try:
        rows = await _lookup(name)
        if len(rows) != 1:
            raise ValueError('创建后未找到唯一单位。')
        key = rows[0].get('InstanceID')
        _key(key)
        await _verify(out, key, changes)
    except (ValueError, TypeError) as exc:
        out['error'] = str(exc) + '；请只读核实，勿自动重发。'
    return raw_json(out)


@mcp.tool
async def update_uom(key: str, expected_name: str, name: str | None = None,
                     description: str | None = None, notes: str | None = None) -> str:
    """按实际ID更新UOM名称/描述/备注；expected_name核对原名。省略保持，空字符串清空文字。
    全局主数据；重命名影响所有引用该单位的定义，不等于转换历史测量值。
    冻结/锁定/状态未知拒绝；已同值不写。一次PUT并回读，未知不重发。
    """
    out = {'operation': 'update_uom', 'submission_state': 'not_submitted', 'verified': False}
    try:
        _key(key)
        expected_name = _name(expected_name)
        changes = _payload({'name': _name(name) if name is not None else None,
                            'description': description, 'notes': notes})
        before = await _read(key)
        out.update(before=before, requested=changes)
        if before['Name'] != expected_name:
            raise ValueError('expected_name 与当前单位名称不符。')
        if all(_checks(before, changes).values()):
            return raw_json({**out, 'submission_state': 'not_needed', 'verified': True})
        if before.get('IsFrozen') is not False or before.get('InstanceLocked') is not False:
            raise ValueError('单位已冻结、被锁定或状态未知，未提交。')
        if name is not None and changes['name'] != before['Name'] and await _lookup(changes['name']):
            raise ValueError('目标单位名称已存在，未提交。')
    except (ValueError, TypeError) as exc:
        return raw_json({**out, 'error': str(exc)})
    raw = await request('PUT', '/api/UOMs/' + key, body={'name': before['Name'], **changes}, response_formatter=raw_json)
    out.update(submission_state=_outcome(raw), write_response=raw)
    try:
        await _verify(out, key, {'name': before['Name'], **changes})
    except (ValueError, TypeError) as exc:
        out['error'] = str(exc) + '；请只读核实，勿自动重发。'
    return raw_json(out)
