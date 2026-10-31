"""
ContainerLevels MCP tools
===================================
Swagger: Modeling /api/ContainerLevels

Complete ContainerLevel modeling tools; never move Containers.
"""

from typing import Optional
import json
import re
from urllib.parse import quote

from pydantic import StrictBool

from core.http_client import request
from core.modeling_payload import build_update_payload, _schema
from tools.modeling_details import raw_json, _has_next_link
from tools import mcp


@mcp.tool
async def list_container_levels(
    filter_expr: Optional[str] = None,
    top: Optional[int] = None,
    skip: Optional[int] = None,
    select: Optional[str] = None,
    expand: Optional[str] = None,
    orderby: Optional[str] = None,
) -> str:
    """
    List Container Levels.
    GET Modeling /api/ContainerLevels

    Useful fields include name, description, containerNumberingRule,
    isNameUnique, parentLevels, childLevels, and allowMove. Use this before
    container_start when the level_name or its default numbering rule is
    unknown.
    """
    params = {}
    if filter_expr:
        params["$filter"] = filter_expr
    if top is not None:
        params["$top"] = top
    if skip is not None:
        params["$skip"] = skip
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
        params.setdefault("$select", "*")
    if orderby:
        params["$orderby"] = orderby

    return await request(
        "GET",
        "/api/ContainerLevels",
        params=params or None,
        response_formatter=raw_json,
    )


@mcp.tool
async def get_container_level(key: str, expand: Optional[str] = None) -> str:
    """
    Get one Container Level by name or instance ID.
    GET Modeling /api/ContainerLevels/{key}

    Use this to validate level_name and inspect containerNumberingRule before
    container_start.
    """
    if not key.strip():
        return raw_json({'error':'须提供层级名称或实际ID。'})
    kwargs = {'response_formatter': raw_json}
    if expand:
        kwargs['params'] = {'$expand':expand, '$select':'*'}
    return await request("GET", f"/api/ContainerLevels/{quote(key.strip(), safe='')}", **kwargs)


# Wire input and GET property names differ for acronym prefixes.
NAVIGATION = {"childLevels": "ChildLevels", "parentLevels": "ParentLevels",
              "attributes": "Attributes", "containerNumberingRule": "ContainerNumberingRule",
              "uiPreference": "UIPreference", "wipMsgConfig": "WIPMsgConfig",
              "wipMsgDefMgr": "WIPMsgDefMgr", "changeHistory": "ChangeHistory"}
CONTROLS = {"replaceDetails", "replaceDetailsSubentityListNames", "skipSubentityListsToSequence",
            "attachAsRORProxy", "useROR", "useRORProxy"}
AUDIT = {'@odata.context', 'ChangeHistory', 'ChangeCount', 'LastChangeDate', 'LastChangeDateGMT',
         'LastChangedDate', 'LastChangedDateGMT'}
MISSING = object()


def _value(obj, field):
    return next((v for k, v in obj.items() if k.casefold() == field.casefold()), MISSING)


def _label(field):
    return NAVIGATION.get(field, field[0].upper() + field[1:])


def _name(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 128
            or any(c in value for c in '\r\n\x00')):
        raise ValueError('名称须为1..128字符的非空字符串。')
    return value.strip()


def _target(key, expected_name):
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-fA-F]{16}', key):
        raise ValueError('key 须为查询返回的实际16位层级 InstanceID。')
    if expected_name is not None:
        _name(expected_name)


def _fields(name=None, allow_move=None, is_name_unique=None, description=None, notes=None,
            container_numbering_rule_name=None, parent_level_names=None, child_level_names=None):
    result = {k:v for k,v in {'name': _name(name) if name is not None else None,
        'allowMove':allow_move, 'isNameUnique':is_name_unique, 'description':description, 'notes':notes}.items() if v is not None}
    if container_numbering_rule_name is not None:
        result['containerNumberingRule'] = {'name': _name(container_numbering_rule_name)}
    for field, names in [('parentLevels', parent_level_names), ('childLevels', child_level_names)]:
        if names is not None:
            if not isinstance(names, list):
                raise ValueError(field + ' 须为明确名称的列表。')
            result[field] = [{'name':_name(n)} for n in names]
    return result


async def _read_level(key, fields=()):
    expand = sorted({NAVIGATION[f.removesuffix('-Expanded')] for f in fields if f.removesuffix('-Expanded') in NAVIGATION})
    kwargs = {'response_formatter': raw_json}
    if expand:
        # This deployment returns only navigation fields for $expand alone.
        # Keep identity/frozen/scalar fields alongside expanded configuration.
        kwargs['params'] = {'$expand': ','.join(expand), '$select':'*'}
    raw = await request('GET', '/api/ContainerLevels/' + key, **kwargs)
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('层级读取失败：' + str(raw)) from None
    if (not isinstance(obj, dict) or obj.get('error') or obj.get('ErrorCode') or _has_next_link(obj)
            or obj.get('InstanceID') != key or not isinstance(obj.get('Name'), str) or not obj['Name'].strip()):
        raise ValueError('层级身份或响应完整性未核实。')
    if 'allowMove' in fields and ('AllowMove' not in obj or obj['AllowMove'] is not None and type(obj['AllowMove']) is not bool):
        raise ValueError('AllowMove 当前值未明确返回。')
    return obj


async def _lookup(name):
    raw = await request('GET', '/api/ContainerLevels', params={'$filter':"Name eq '"+name.replace("'", "''")+"'", '$top':2}, response_formatter=raw_json)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('名称唯一性读取失败：' + str(raw)) from None
    if (not isinstance(data, dict) or data.get('error') or data.get('ErrorCode') or _has_next_link(data)
            or not isinstance(data.get('value'), list) or len(data['value']) > 1):
        raise ValueError('名称查询不完整或不唯一。')
    return data['value']


def _write_outcome(raw):
    if str(raw).startswith(('❌', 'Error')):
        return 'rejected' if re.search(r'HTTP (400|401|403|404|405|422)\b', str(raw)) else 'unknown'
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return 'acknowledged'
    return 'unknown' if isinstance(data, dict) and (data.get('error') or data.get('ErrorCode')) else 'acknowledged'


def _matches(actual, expected):
    if actual is MISSING:
        return False
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(_matches(_value(actual, k), v) for k,v in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        if expected and all(isinstance(v, dict) and 'name' in v for v in expected):
            remaining = list(actual)
            for item in expected:
                index = next((i for i, value in enumerate(remaining) if _matches(value, item)), None)
                if index is None:
                    return False
                remaining.pop(index)
            return True
        return all(_matches(a,b) for a,b in zip(actual, expected))
    if type(actual) in (int, float) and type(expected) in (int, float):
        return actual == expected
    return type(actual) is type(expected) and actual == expected


def _verify(obj, changes):
    checks, observed = {}, {}
    for field, expected in changes.items():
        base = field.removesuffix('-Expanded')
        actual = _value(obj, base)
        if actual is not MISSING:
            observed[_label(base)] = actual
        # Expanded actions and service control flags are not persistent target
        # snapshots. Do not invent list-action semantics or claim echo is proof.
        checks[field] = None if field.endswith('-Expanded') or base in CONTROLS else _matches(actual, expected)
    return checks, observed


def _base(action, key=None):
    return {'entity':'ContainerLevel', 'action':action, 'instance_id':key,
            'submission_state':'not_submitted', 'verified':False, 'complete':False,
            'scope':'层级全局建模配置；不执行容器过站，也不改动关联对象本身。'}


def _verification_result(out, after, changes, before=None):
    checks, observed = _verify(after, changes)
    matches = bool(checks) and all(v is True for v in checks.values())
    verified = matches and out['submission_state'] == 'acknowledged'
    requested = {f.removesuffix('-Expanded').casefold() for f in changes}
    other = sorted(k for k in before.keys() | after.keys()
                   if k not in AUDIT and k.casefold().removesuffix('-expanded') not in requested
                   and before.get(k, MISSING) != after.get(k, MISSING)) if before else []
    out.update(after=observed, field_checks=checks, observed_matches=matches, verified=verified,
               complete=verified and not other, other_changed_fields=other)
    if other:
        out['warnings'] = ['两次读取间其他非审计字段变化；未回滚，也不将并发变化归因于本请求。']
    if not verified:
        out['error'] = ('写入结果未确认，回读同值不能单独证明本次提交成功；请只读核实，勿重发。'
                        if out['submission_state'] != 'acknowledged' else
                        '部分字段未通过回读核实；field_checks=null 表示扩展明细/控制字段尚不能证明持久结果，未自动重试。')
    return out


@mcp.tool
async def update_container_level(
    key: str, allow_move: Optional[StrictBool] = None, expected_name: Optional[str] = None,
    name: Optional[str] = None, is_name_unique: Optional[StrictBool] = None,
    description: Optional[str] = None, notes: Optional[str] = None,
    container_numbering_rule_name: Optional[str] = None,
    parent_level_names: Optional[list[str]] = None, child_level_names: Optional[list[str]] = None,
    body_json: Optional[str] = None,
) -> str:
    """按实际16位层级ID更新明确字段并自动回读。保留 allow_move 旧调用兼容。
    支持名称、AllowMove、唯一性、描述、备注、编号规则、父/子层级，以及 body_json
    中当前完整写入契约的其他字段（属性、WIP、UI、扩展列表及控制字段）。
    复杂字段先 get_container_level_schema；只读/未知/重复字段及 ObjectToChange 拒绝。
    省略保持，JSON null 为显式清除；列表动作遵循原生契约，不猜空数组为删除。
    expected_name 核对旧名称；冻结或未知拒绝，已符合目标则不写。单次 PUT，
    Name 缺省沿用当前名，不回传整对象。verified 仅在成功响应且全部请求字段
    可核实时为true；控制/扩展明细可能仅已提交而未验证。未知结果不重试。
    全局配置仅在用户明确要求时维护；不会自动恢复或执行 MoveIn/MoveOut。
    """
    out = _base('update', key)
    try:
        _target(key, expected_name)
        changes = build_update_payload('ContainerLevels', _fields(name, allow_move, is_name_unique, description, notes,
            container_numbering_rule_name, parent_level_names, child_level_names), body_json)
        if 'name' in changes:
            changes['name'] = _name(changes['name'])
        out['requested'] = {_label(k):v for k,v in changes.items()}
        before = await _read_level(key, changes)
        out['name'] = before['Name']
        if expected_name is not None and before['Name'] != expected_name.strip():
            raise ValueError('expected_name 与实际层级名称不符，未提交。')
        checks, observed = _verify(before, changes)
        out['before'] = observed
        if checks and all(value is True for value in checks.values()):
            return raw_json({**out, 'submission_state':'not_needed', 'verified':True, 'complete':True,
                             'observed_matches':True, 'after':observed, 'field_checks':checks})
        if before.get('IsFrozen') is not False:
            raise ValueError('层级已冻结或冻结状态未知，未提交。')
        payload = {'name':before['Name'], **changes}
    except (ValueError, TypeError, KeyError) as exc:
        return raw_json({**out, 'error':str(exc)})
    raw = await request('PUT', '/api/ContainerLevels/'+key, body=payload, response_formatter=raw_json)
    out.update(submission_state=_write_outcome(raw), write_response=raw)
    try:
        after = await _read_level(key, changes)
        if after['Name'] != payload['name']:
            raise ValueError('回读名称与目标名称不符。')
        _verification_result(out, after, changes, before)
    except (ValueError, TypeError, KeyError) as exc:
        out['error'] = '写入后未能完成回读验证：'+str(exc)+'；不要自动重发。'
    return raw_json(out)


@mcp.tool
async def create_container_level(
    name: str, allow_move: Optional[StrictBool] = None, is_name_unique: Optional[StrictBool] = None,
    description: Optional[str] = None, notes: Optional[str] = None,
    container_numbering_rule_name: Optional[str] = None,
    parent_level_names: Optional[list[str]] = None, child_level_names: Optional[list[str]] = None,
    body_json: Optional[str] = None,
) -> str:
    """创建明确名称的ContainerLevel。可写字段与 update_container_level 相同。
    必须明确名称；其余仅提交明确输入，不补 AllowMove/唯一性/编号/父子默认值。
    先检查同名对象，不覆盖或改走更新；POST一次后查同名唯一ID并回读。
    body_json可提供完整可写字段，不能改写 name 参数；未知结果不重试。
    不创建容器、不分配编号、不自动放行或过站。复杂字段先查 schema。
    """
    out = _base('create')
    try:
        name = _name(name)
        changes = build_update_payload('ContainerLevels', _fields(name, allow_move, is_name_unique, description, notes,
            container_numbering_rule_name, parent_level_names, child_level_names), body_json)
        if changes['name'] != name:
            raise ValueError('body_json.name 不可改写明确的创建名称。')
        out.update(name=name, requested={_label(k):v for k,v in changes.items()})
        if await _lookup(name):
            raise ValueError('该层级名称已存在；未创建或覆盖。')
    except (ValueError, TypeError, KeyError) as exc:
        return raw_json({**out, 'error':str(exc)})
    raw = await request('POST', '/api/ContainerLevels', body=changes, response_formatter=raw_json)
    out.update(submission_state=_write_outcome(raw), write_response=raw)
    try:
        rows = await _lookup(name)
        if len(rows) != 1:
            raise ValueError('创建后未找到同名唯一对象。')
        key = rows[0].get('InstanceID')
        _target(key, None)
        out['instance_id'] = key
        after = await _read_level(key, changes)
        _verification_result(out, after, changes)
    except (ValueError, TypeError, KeyError) as exc:
        out['error'] = '创建后未能完成回读验证：'+str(exc)+'；不要自动重发。'
    return raw_json(out)


@mcp.tool
async def delete_container_level(key: str, expected_name: Optional[str] = None) -> str:
    """明确删除现有层级；这是全局建模对象，不是移除某个容器的层级。
    key为16位实际ID，可expected_name核对原名；先读取身份/冻结状态。
    应核对引用影响；服务端仍可因被引用等规则拒绝。本工具不级联删除容器、
    父子层级或引用对象。DELETE一次后只读核对404；超时后404也不等于本次已验证。
    应用沿用删除确认策略，外部MCP沿用客户端授权。未知结果不重试。
    """
    out = _base('delete', key)
    try:
        _target(key, expected_name)
        before = await _read_level(key)
        out['name'] = before['Name']
        if expected_name is not None and before['Name'] != expected_name.strip():
            raise ValueError('expected_name 与实际层级名称不符，未删除。')
        if before.get('IsFrozen') is not False:
            raise ValueError('层级已冻结或冻结状态未知，未删除。')
    except (ValueError, TypeError, KeyError) as exc:
        return raw_json({**out, 'error':str(exc)})
    raw = await request('DELETE', '/api/ContainerLevels/'+key, response_formatter=raw_json)
    out.update(submission_state=_write_outcome(raw), write_response=raw)
    check = await request('GET', '/api/ContainerLevels/'+key, response_formatter=raw_json)
    absent = bool(re.search(r'HTTP 404\b', str(check)))
    out.update(observed_absent=absent, verified=absent and out['submission_state']=='acknowledged')
    out['complete'] = out['verified']
    if not out['verified']:
        out['error'] = '删除结果尚未验证；当前不存在也不能单独证明未知请求成功，勿自动重发。'
    return raw_json(out)


@mcp.tool
async def request_container_level_selection_values(
    selection_values_expression: Optional[str] = None, body_json: Optional[str] = None,
) -> str:
    """按原生契约读取层级字段选择值（编号规则/父子层级/UI等）；不保存配置。
    body_json仅提供上下文，不自动挑选候选值；表达式由真实字段定义确定。
    """
    try:
        payload = build_update_payload('ContainerLevels', {}, body_json, allow_empty=True) if body_json is not None else None
    except (ValueError, TypeError) as exc:
        return raw_json({'error':str(exc)})
    params = {'selectionValuesExpression': selection_values_expression} if selection_values_expression else None
    return await request('POST', '/api/ContainerLevels/RequestSelectionValues', body=payload, params=params, response_formatter=raw_json)


@mcp.tool
async def get_container_level_schema(field_name: Optional[str] = None) -> str:
    """本地读取已归档ContainerLevel完整字段契约。省略field_name返回全部字段目录；
    指定字段（大小写不敏感）返回其精确schema和递归引用类型，用于复杂body_json。
    标出只读字段及不可改写路径目标的ObjectToChange；不调用MES或执行写入。
    """
    schema, definitions = _schema('ContainerLevels')
    props = schema['properties']
    if field_name is None:
        return raw_json({'entity':'ContainerLevel', 'create_required':schema.get('required',[]),
            'fields':{name:{**{k:v for k,v in value.items() if k!='description'},
                            'write_supported':not value.get('readOnly',False) and name!='objectToChange'} for name,value in props.items()},
            'note':'写入契约不等于已现场验收；扩展明细/控制字段回读不能自动证明持久结果。'})
    name = next((key for key in props if key.casefold()==field_name.casefold()), None)
    if name is None:
        return raw_json({'error':'字段不在当前ContainerLevel契约中。'})
    refs = {}
    def visit(value):
        if isinstance(value, dict):
            if '$ref' in value:
                ref = value['$ref'].rsplit('/',1)[-1]
                if ref not in refs:
                    refs[ref] = definitions[ref]
                    visit(refs[ref])
            for part in value.values():visit(part)
        elif isinstance(value,list):
            for part in value:visit(part)
    visit(props[name])
    return raw_json({'field':name, 'schema':props[name], 'definitions':refs,
                     'write_supported':not props[name].get('readOnly',False) and name!='objectToChange'})


@mcp.tool
async def get_container_levels_count(
    filter_expr: Optional[str] = None,
) -> str:
    """Count Container Levels, optionally using an OData filter."""
    params = {"$filter": filter_expr} if filter_expr else None
    return await request(
        "GET",
        "/api/ContainerLevels/$count",
        params=params,
    )
