"""Typed BOM maintenance with immutable plans and single-attempt MES writes."""
import asyncio
import copy
import json
import math
import sqlite3
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from config import CAMSTAR_BASE_URL
from core.http_client import get_client, get_headers
from core.transfer_attempt_lock import attempt_lock, AttemptBusy
from tools import mcp
from tools.boms import encode
from tools.collection_modeling import digest, matches, name
from tools.modeling_details import _has_next_link
from tools.process_context import ReadContext

PLAN_DB = Path(__file__).resolve().parents[1] / 'data/bom_modeling_plans.sqlite3'
ROOT_FIELDS = {'description': 'Description', 'notes': 'Notes', 'eco': 'ECO', 'status': 'Status'}
LINE_FIELDS = {'description': 'Description', 'quantity': 'QtyRequired', 'quantity2': 'Qty2Required',
    'issue_control': 'IssueControl', 'reference_designator': 'ReferenceDesignator',
    'assembly_sequence': 'AssemblySequence', 'scrap_percent': 'ScrapPercent',
    'setup_quantity': 'SetupQty', 'setup_quantity2': 'SetupQty2',
    'allow_over_consumption': 'AllowOverConsumption', 'allow_under_consumption': 'AllowUnderConsumption',
    'allow_substitution': 'AllowSubstitution', 'allow_all_revisions_as_substitute': 'AllowAllRevisionsAsSubstitute',
    'vendor_control': 'VendorControl'}
BOOLS = {v for v in LINE_FIELDS.values() if v.startswith('Allow')} | {'VendorControl'}
NUMBERS = {'QtyRequired', 'Qty2Required', 'ScrapPercent', 'SetupQty', 'SetupQty2'}
REFS = {'product': ('Products', 'Product'), 'unit': ('UOMs', 'UOM'),
        'unit2': ('UOMs', 'UOM2'), 'spec': ('Specs', 'Spec')}
WIRE = {'QtyRequired': 'qtyRequired', 'Qty2Required': 'qty2Required', 'UOM': 'uom', 'UOM2': 'uoM2', 'ECO': 'eco'}
IGNORED = {'@odata.context', 'InstanceID', 'InstanceId', 'DisplayName', 'Parent', 'ParentRevision',
    'ObjectToChange', 'ListItemToChange', 'ReplaceDetails', 'ReplaceDetailsSubentityListNames',
    'SkipSubentityListsToSequence', 'CanChangeRevOfRcd', 'UseRor', 'UseRORProxy', 'AttachAsRORProxy',
    'AttachDocumentDetails-Expanded', 'Attributes-Expanded', 'MaterialList-Expanded', 'OtherProductRevisions',
    'OtherProductRevisions-Expanded', 'MatchingProds', 'MaterialList_Tmp', 'Base'}

REF_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['name', 'revision'],
              'properties': {'name': {'type': 'string'}, 'revision': {'type': 'string'}}}
LINE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['name'], 'properties': {
    'name': {'type': 'string'}, 'op': {'type': 'string', 'enum': ['add', 'update', 'remove']},
    'new_name': {'type': 'string'}, 'product': REF_SCHEMA,
    'spec': {'anyOf': [REF_SCHEMA, {'type': 'null'}]},
    'unit': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
    'unit2': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
    **{key: ({'type': 'boolean'} if field in BOOLS else
             {'type': 'integer', 'minimum': 1, 'maximum': 6} if field == 'IssueControl' else
             {'type': 'integer', 'minimum': 0} if field == 'AssemblySequence' else
             {'anyOf': [{'type': 'number'}, {'type': 'string'}]} if field in NUMBERS else
             {'anyOf': [{'type': 'string'}, {'type': 'null'}]}) for key, field in LINE_FIELDS.items()}}}
CHANGE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'description': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
    'notes': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
    'eco': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
    'status': {'type': 'integer', 'enum': [1, 2]},
    'lines': {'type': 'array', 'maxItems': 200, 'items': LINE_SCHEMA}}}


def stable(value):
    """Changes CDO references/row IDs are regenerated on reads; compare business identities."""
    if isinstance(value, list):
        items = [stable(v) for v in value]
        if items and all(isinstance(v, dict) and isinstance(v.get('Name'), str) for v in items):
            items.sort(key=lambda v: v['Name'].casefold())
        return items
    if not isinstance(value, dict):
        return value
    # Native reference objects carry Name/Revision/UseROR; CDO IDs aren't stable here.
    if 'Name' in value and set(value) <= {'Name','Revision','UseROR','InstanceId','InstanceID','CDOTypeName','CdoTypeName'}:
        result = {k: value[k] for k in ('Name','Revision','UseROR') if k in value}
        # This adapter returns null for an explicit fixed-version useROR=false.
        # Only normalize null/absent when a concrete revision is present; never erase true.
        if result.get('Revision') and result.get('UseROR') is not True:
            result['UseROR'] = False
        return result
    return {k: stable(v) for k, v in value.items() if k not in IGNORED and not k.startswith('@')}


def wire(field):
    return WIRE.get(field, field[0].lower() + field[1:])


def scalar(field, value):
    if field in BOOLS:
        if type(value) is not bool: raise ValueError(field + '须为布尔值。')
    elif field in {'Status', 'IssueControl', 'AssemblySequence'}:
        allowed = {1, 2} if field == 'Status' else set(range(1, 7)) if field == 'IssueControl' else None
        if type(value) is not int or (value not in allowed if allowed else not 0 <= value <= 2147483647):
            raise ValueError(field + '枚举/整数范围无效。')
    elif field in NUMBERS:
        if isinstance(value, bool) or not isinstance(value, (str, float, int)): raise ValueError(field + '须为数量。')
        try: decimal = Decimal(str(value))
        except InvalidOperation: raise ValueError(field + '数量格式无效。') from None
        if not decimal.is_finite() or decimal < 0 or (field == 'QtyRequired' and decimal == 0):
            raise ValueError('用量须为正有限数；副用量/损耗/备料量须非负。')
        if field == 'ScrapPercent' and decimal > 100: raise ValueError('损耗率须为0..100。')
        value = float(decimal)
        if not math.isfinite(value) or Decimal(str(value)) != decimal: raise ValueError('数量超过接口可无损表示的精度。')
    elif value is not None and (not isinstance(value, str) or len(value) > 2000):
        raise ValueError(field + '须为最多2000字符的字符串或null。')
    return value


def editable(obj):
    if obj.get('IsFrozen') is not False or obj.get('InstanceLocked') is True:
        raise ValueError('对象冻结状态未知、已冻结或被变更管理锁定。')


async def lookup(entity, object_name, revision=None):
    escape = lambda s: s.replace("'", "''")
    expression = "Name eq '" + escape(object_name) + "'"
    if revision is not None: expression += " and Revision eq '" + escape(revision) + "'"
    result = await ReadContext(max_requests=1).get(entity, params={'$filter': expression, '$top': 2})
    rows = result.get('value')
    if not isinstance(rows, list): raise ValueError('对象不存在性查询响应无效。')
    if _has_next_link(result) or (revision is not None and len(rows) > 1): raise ValueError('对象版本不唯一/查询未完整。')
    return rows


async def read_bom(object_name, revision):
    ctx = ReadContext(max_requests=4)
    root = await ctx.resolve('BOMs', {'Name': object_name, 'Revision': revision})
    expanded = await ctx.get('BOMs', root['InstanceID'], {'$expand': 'MaterialList'})
    if any(expanded.get(k) != root.get(k) for k in ('InstanceID', 'Name', 'Revision')) or _has_next_link(expanded):
        raise ValueError('BOM展开身份不一致或有未读取分页。')
    rows = expanded.get('MaterialList')
    if not isinstance(rows, list) or len(rows) > 200: raise ValueError('BOM未完整返回或超过200条明细。')
    seen = set()
    for row in rows:
        if not isinstance(row, dict): raise ValueError('BOM明细格式无效。')
        key = name(row.get('Name')).casefold()
        if key in seen: raise ValueError('BOM明细名称重复，不能按位置猜测目标。')
        seen.add(key)
    root['MaterialList'] = rows
    return root


async def read_product(object_name, revision):
    return await ReadContext(max_requests=2).resolve('Products', {'Name': object_name, 'Revision': revision})


async def reference_payload(key, value, dependencies):
    entity, field = REFS[key]
    if value is None:
        if key == 'product': raise ValueError('物料产品不能清除。')
        return None
    if entity == 'UOMs':
        ref = {'Name': name(value)}
    else:
        if not isinstance(value, dict) or set(value) != {'name', 'revision'}:
            raise ValueError(key + '须明确name与revision，不接受动态ROR。')
        ref = {'Name': name(value['name']), 'Revision': name(value['revision'])}
    cached = next((d for d in dependencies if d['entity'] == entity and d['config'].get('Name') == ref['Name']
                   and (entity == 'UOMs' or d['config'].get('Revision') == ref['Revision'])), None)
    obj = (dict(cached['config'], InstanceID=cached['id']) if cached else
           await ReadContext(max_requests=2).resolve(entity, ref, revisioned=entity != 'UOMs'))
    if entity != 'UOMs' and obj.get('Status') != 1: raise ValueError(key + '引用对象须为Active。')
    if not cached: dependencies.append({'entity': entity, 'id': obj['InstanceID'], 'config': stable(obj)})
    payload = {'name': obj['Name'], 'instanceId': obj['InstanceID']}
    if entity != 'UOMs': payload.update(revision=obj['Revision'], useROR=False)
    return payload


async def line_changes(items, before, creating, dependencies):
    if not isinstance(items, list) or len(items) > 200: raise ValueError('lines最多200条变更。')
    after = copy.deepcopy(before); wrappers = []; seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) - (set(LINE_FIELDS) | set(REFS) | {'op', 'name', 'new_name'}):
            raise ValueError('明细包含未知/只读字段。')
        line_name = name(item.get('name')); key = line_name.casefold()
        if key in seen: raise ValueError('同一明细在一次计划中只能维护一次。')
        seen.add(key); op = item.get('op', 'add' if creating else 'update')
        found = [(i, row) for i, row in enumerate(after) if row['Name'].casefold() == key]
        if op == 'add':
            if found: raise ValueError('新增明细名称已存在：' + line_name)
            if not {'product', 'quantity', 'issue_control', 'unit'} <= set(item):
                raise ValueError('新增明细须明确product、quantity、unit（可明确null）与issue_control。')
            old = {}; index = None
        elif op in ('update', 'remove'):
            if creating or len(found) != 1: raise ValueError('维护明细不存在/不唯一：' + line_name)
            index, old = found[0]
        else: raise ValueError('op须为add/update/remove。')
        if op == 'remove':
            if set(item) != {'name', 'op'}: raise ValueError('remove仅接受name与op。')
            wrappers.append({'key': {'name': old['Name']}, 'value': {}}); after.pop(index)
            continue
        value = {'name': name(item.get('new_name', line_name))}
        new_key = value['name'].casefold()
        if new_key != key and new_key in seen: raise ValueError('不能在一次计划中链式维护已重命名/移除的明细名称。')
        seen.add(new_key)
        if any(row['Name'].casefold() == value['name'].casefold() for i, row in enumerate(after) if i != index):
            raise ValueError('明细重命名冲突。')
        for key, field in LINE_FIELDS.items():
            if key in item: value[wire(field)] = scalar(field, item[key])
        for key, (entity, field) in REFS.items():
            if key in item: value[wire(field)] = await reference_payload(key, item[key], dependencies)
        # Both are required by the derived Changes CDO even on a sparse update.
        for field in ('QtyRequired', 'IssueControl'):
            if wire(field) not in value:
                if field not in old: raise ValueError('明细缺少必需字段：' + field)
                value[wire(field)] = scalar(field, old[field])
        projected = {field: stable(value[wire(field)]) for field in set(LINE_FIELDS.values()) | {v[1] for v in REFS.values()} | {'Name'} if wire(field) in value}
        # Request references use lower camel case; readback uses upper case.
        for key, (_, field) in REFS.items():
            if wire(field) in value and value[wire(field)] is not None:
                ref = value[wire(field)]; projected[field] = {'Name': ref['name']}
                if 'revision' in ref: projected[field].update(Revision=ref['revision'], UseROR=False)
        if op == 'add':
            wrappers.append({'listItemAction': 'add', 'value': value}); after.append(projected)
        else:
            wrappers.append({'key': {'name': old['Name']}, 'value': value}); after[index].update(projected)
    if len(after) > 200: raise ValueError('结果超过200条明细。')
    return wrappers, stable(after)


async def revision_body(expected, dependencies):
    """Native PATCH creates an empty revision, not a clone. Copy supported fields explicitly."""
    body = {'name': expected['Name'], 'revision': expected['Revision'], 'isRevOfRcd': True}
    root_copy = set(ROOT_FIELDS.values())
    allowed_defaults = {'IsFrozen': False, 'InstanceLocked': False, 'AssociatedPackages': 0,
        'isAttachImage': False, 'IsRevOfRcd': True}
    for field, value in expected.items():
        if field in root_copy:
            body[wire(field)] = value
        elif field in ('BillType', 'DefaultProductType') and value:
            entity = 'BillTypes' if field == 'BillType' else 'ProductTypes'
            obj = await ReadContext(max_requests=2).resolve(entity, {'Name': name(value.get('Name'))}, revisioned=False)
            dependencies.append({'entity': entity, 'id': obj['InstanceID'], 'config': stable(obj)})
            body[wire(field)] = {'name': obj['Name'], 'instanceId': obj['InstanceID']}
        elif field not in {'Name','Revision','MaterialList','BillType','DefaultProductType'} and value not in (None, [], ''):
            if field not in allowed_defaults or type(value) is not type(allowed_defaults[field]) or value != allowed_defaults[field]:
                raise ValueError('升版源含未支持复制的配置：' + field + '；拒绝丢失配置。')
    rows = []
    extra = {'AdjustmentType','AdjustmentValue','EffectiveFromDate','EffectiveFromDateGMT','EffectiveThruDate',
             'EffectiveThruDateGMT','ProducingOrder'}
    derived_defaults = {'IsFrozen': False, 'IsPhantom': False, 'ResolvedAllowAllRevisions': False}
    for row in expected['MaterialList']:
        value = {'name': row['Name']}
        for field, raw in row.items():
            if field in set(LINE_FIELDS.values()) | extra:
                if raw is not None: value[wire(field)] = scalar(field, raw) if field in LINE_FIELDS.values() else raw
            elif field in {v[1] for v in REFS.values()} and raw:
                key = next(k for k, v in REFS.items() if v[1] == field)
                if key in ('product','spec'):
                    if raw.get('UseROR') is True or not raw.get('Revision'):
                        raise ValueError('升版源产品/工序引用须为明确固定版本。')
                    ref = {'name': raw['Name'], 'revision': raw['Revision']}
                else: ref = raw['Name']
                value[wire(field)] = await reference_payload(key, ref, dependencies)
            elif field != 'Name' and raw not in (None, [], ''):
                if field not in derived_defaults or raw is not derived_defaults[field]:
                    raise ValueError('升版源明细含未支持复制的配置：' + field + '；拒绝丢失配置。')
        rows.append(value)
    body['materialList'] = rows
    return body


async def build_bom(action, object_name, revision, changes, source_revision=''):
    if action not in ('create', 'update', 'revise'): raise ValueError('BOM操作无效。')
    object_name, revision = name(object_name), name(revision)
    if not isinstance(changes, dict) or set(changes) - (set(ROOT_FIELDS) | {'lines'}): raise ValueError('changes包含未知/只读字段。')
    before = None; dependencies = []
    if action == 'create':
        if source_revision or await lookup('BOMs', object_name): raise ValueError('BOM名称已存在或不应指定源版本；已有BOM请明确升版。')
    elif action == 'revise':
        source_revision = name(source_revision)
        if revision == source_revision or await lookup('BOMs', object_name, revision): raise ValueError('目标版本已存在/等于源版本。')
        before = await read_bom(object_name, source_revision)
        if before.get('IsRevOfRcd') is not True: raise ValueError('升版源须为明确的当前ROR；不能猜测复制源。')
    else:
        if source_revision: raise ValueError('普通修改不接受source_revision。')
        before = await read_bom(object_name, revision)
        if not changes: raise ValueError('修改内容为空。')
    if before: editable(before)
    expected = stable(before) if before else {'Name': object_name, 'Revision': revision, 'MaterialList': []}
    expected.update(Name=object_name, Revision=revision)
    body = {} if action == 'update' else {'name': object_name, 'revision': revision, 'isRevOfRcd': True}
    if action != 'update': expected['IsRevOfRcd'] = True
    for key, field in ROOT_FIELDS.items():
        if key in changes: body[wire(field)] = scalar(field, changes[key]); expected[field] = body[wire(field)]
    if 'lines' in changes:
        wrappers, rows = await line_changes(changes['lines'], expected['MaterialList'], action == 'create', dependencies)
        body['materialList' if action == 'create' else 'materialList-Expanded'] = [w['value'] for w in wrappers] if action == 'create' else wrappers
        expected['MaterialList'] = rows
    if action == 'revise':
        body = await revision_body(expected, dependencies)
    target = before['InstanceID'] if before else None
    return {'kind': 'bom', 'action': action, 'entity': 'BOMs', 'name': object_name, 'revision': revision,
        'source_revision': source_revision, 'target_id': target, 'before': stable(before), 'expected': expected,
        'dependencies': dependencies, 'resources': ['bom:' + object_name.casefold()],
        'method': {'create': 'POST', 'update': 'PUT', 'revise': 'PATCH'}[action],
        'path': '/api/BOMs' + ('/' + target if action == 'update' else ''), 'body': body}


async def build_binding(product_name, product_revision, bom_name='', bom_revision='', action='bind'):
    if action not in ('bind', 'unbind'): raise ValueError('关联操作须为bind/unbind。')
    product_name, product_revision = name(product_name), name(product_revision)
    before = await read_product(product_name, product_revision); editable(before)
    expected = stable(before); dependencies = []; resources = ['product:' + before['InstanceID']]
    current = before.get('BOM')
    if current and current.get('Name'): resources.append('bom:' + current['Name'].casefold())
    if action == 'bind':
        bom_name, bom_revision = name(bom_name), name(bom_revision)
        bom = await read_bom(bom_name, bom_revision)
        if bom.get('Status') != 1: raise ValueError('关联BOM须为Active。')
        dependencies.append({'entity': 'BOMs', 'id': bom['InstanceID'], 'config': stable(bom)})
        resources.append('bom:' + bom_name.casefold())
        ref = {'name': bom_name, 'revision': bom_revision, 'instanceId': bom['InstanceID'], 'useROR': False}
        expected['BOM'] = {'Name': bom_name, 'Revision': bom_revision, 'UseROR': False}
    else:
        if bom_name or bom_revision: raise ValueError('解绑仅按计划中展示的产品当前BOM引用操作，不接受目标BOM。')
        if not current: raise ValueError('产品未关联BOM。')
        ref = None; expected['BOM'] = None
    return {'kind': 'binding', 'action': action, 'entity': 'Products', 'name': product_name, 'revision': product_revision,
        'target_id': before['InstanceID'], 'before': stable(before), 'expected': expected, 'dependencies': dependencies,
        'resources': sorted(set(resources)), 'method': 'PUT', 'path': '/api/Products/' + before['InstanceID'], 'body': {'bom': ref}}


def db_connect():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB)
    db.execute('CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,digest TEXT,payload TEXT,state TEXT,created REAL,outcome TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS attempts(plan_id TEXT PRIMARY KEY,attempt_id TEXT,started REAL,finished REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,plan_id TEXT,record TEXT)')
    return db


def pending_check(db, snapshot):
    for (raw,) in db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')"):
        other = json.loads(raw)['snapshot']
        if other['environment'] == snapshot['environment'] and set(other['resources']) & set(snapshot['resources']):
            raise ValueError('相关BOM/产品有未核实的维护尝试，不能另建计划绕过。')


def plan_view(snapshot, offset=0, limit=20):
    """Page display only; immutable stored payload and execution are never cropped."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('offset>=0，limit=1..50。')
    view = copy.deepcopy(snapshot); pages = {}
    for scope, obj in [('before', view.get('before')), ('expected', view.get('expected')), ('body', view.get('body'))]:
        if not isinstance(obj, dict): continue
        for field in ('MaterialList','materialList','materialList-Expanded'):
            if isinstance(obj.get(field), list):
                rows = obj[field]; obj[field] = rows[offset:offset+limit]
                pages[scope+'.'+field] = {'total':len(rows),'offset':offset,'returned':len(obj[field]),
                    'next_offset':offset+limit if offset+limit<len(rows) else None,'complete':offset==0 and len(rows)<=limit}
    view['dependencies'] = [{'entity':d['entity'],'id':d['id'],'name':d['config'].get('Name'),
                              'revision':d['config'].get('Revision')} for d in view.get('dependencies',[])]
    view['pages'] = pages
    view['display_only_pagination'] = True
    return view


def save_plan(snapshot, arguments):
    snapshot['environment'] = CAMSTAR_BASE_URL.rstrip('/')
    key = uuid.uuid4().hex; fingerprint = digest(snapshot)
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE'); pending_check(db, snapshot)
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)', (key, fingerprint, json.dumps({'snapshot': snapshot, 'arguments': arguments}, ensure_ascii=False), 'ready', time.time()))
    view = plan_view(snapshot)
    return {'plan_id': key, 'plan_digest': fingerprint, 'state': 'ready', 'submitted': False,
        'confirmation_required': True, 'request_preview': {k: view[k] for k in ('method', 'path', 'body')},
        'before': view['before'], 'expected': view['expected'], 'pages': view['pages'],
        'display_only_pagination': True, 'expires_in_seconds': 3600}


@mcp.tool
async def prepare_bom_change(action: Literal['create','update','revise'], name: str, revision: str,
    changes: Annotated[dict, Field(description='BOM基本信息及lines增量；add必需product{name,revision}、quantity、unit、issue_control(1..6)。', json_schema_extra=CHANGE_SCHEMA)], source_revision: str = '') -> str:
    """只读准备BOM创建/明细增删改/升版计划。根description/notes/eco/status(1/2)；lines按唯一name定位，op=add/update/remove，可new_name，product须明确版本，quantity为单位用量，不做单位换算。issue_control:1序列容器/2批量容器/3外部批次/4库位/5仅数量/6只显示不记录。升版须source_revision为当前ROR；新版本成为ROR，旧版本保留。执行后必须核实。"""
    try:
        args = dict(action=action, object_name=name, revision=revision, changes=changes, source_revision=source_revision)
        snapshot = await asyncio.wait_for(build_bom(**args), 60)
        return encode(save_plan(snapshot, {'builder': 'bom', 'values': args}))
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as exc:
        return encode({'submitted': False, 'error': str(exc) or 'BOM预览超时。'})


@mcp.tool
async def prepare_product_bom_binding(product_name: str, product_revision: str, bom_name: str = '', bom_revision: str = '', action: Literal['bind','unbind'] = 'bind') -> str:
    """只读准备明确产品版本与固定BOM版本的关联/换绑/解绑。展示原引用并保留其他产品配置，不使用动态ROR；unbind展示并清除当前关联。后续execute_bom_change单次执行并核实。"""
    try:
        args = dict(product_name=product_name, product_revision=product_revision, bom_name=bom_name, bom_revision=bom_revision, action=action)
        return encode(save_plan(await asyncio.wait_for(build_binding(**args), 60), {'builder': 'binding', 'values': args}))
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as exc:
        return encode({'submitted': False, 'error': str(exc) or '产品BOM关联预览超时。'})


def load_row(db, plan_id):
    return db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()


def checked(row, fingerprint):
    if not row or row[0] != fingerprint: raise ValueError('BOM计划不存在或摘要错误。')
    value = json.loads(row[1]); snapshot = value['snapshot']
    if digest(snapshot) != fingerprint or snapshot['environment'] != CAMSTAR_BASE_URL.rstrip('/'):
        raise ValueError('BOM计划内容或环境已变化。')
    return value


def ready(row):
    if row[2] != 'ready' or time.time() - row[3] > 3600: raise ValueError('BOM计划已用或过期，不能重放。')


def confirmation_summary(plan_id, plan_digest):
    with db_connect() as db: row = load_row(db, plan_id)
    value = checked(row, plan_digest); ready(row)
    return '确认BOM维护（升版将新版本设为ROR并保留旧版）：' + json.dumps(value['snapshot'], ensure_ascii=False) + '\n只提交一次，结果不明先核实，不能重试原计划。'


@mcp.tool
async def get_bom_change_plan(plan_id: str, offset: int = 0, limit: int = 20) -> str:
    """分页读取BOM计划明细（limit=1..50）、原响应和最近5次审计摘要；不发送MES请求。显示分页不改变完整执行计划，pages.complete=false须翻页。"""
    with db_connect() as db:
        row = load_row(db, plan_id)
        reviews = db.execute('SELECT record FROM reviews WHERE plan_id=? ORDER BY rowid', (plan_id,)).fetchall()
    if not row: return encode({'error': 'BOM计划不存在。'})
    try:
        payload = json.loads(row[1]); view = plan_view(payload['snapshot'],offset,limit)
        outcome = json.loads(row[4]) if row[4] else None
        if outcome:
            outcome = {k:v for k,v in outcome.items() if k not in ('verification','human_review')}
        records = [{k:v for k,v in json.loads(r[0]).items() if k not in ('readback','source_readback','previous_outcome')} for r in reviews[-5:]]
        return encode({'plan_id': plan_id, 'plan_digest': row[0], 'plan': {'snapshot':view}, 'state': row[2],
            'expired': time.time() - row[3] > 3600, 'outcome': outcome, 'review_count':len(reviews),
            'reviews':records,'audit_note':'仅返回最近5次审计摘要；完整请求、原参数及核实回读保留于本地计划库。'})
    except (ValueError,TypeError,KeyError) as exc: return encode({'error':str(exc)})


async def rebuild(arguments):
    if arguments['builder'] == 'bom': return await build_bom(**arguments['values'])
    if arguments['builder'] == 'binding': return await build_binding(**arguments['values'])
    raise ValueError('BOM计划构建器无效。')


@mcp.tool
async def execute_bom_change(plan_id: str, plan_digest: str) -> str:
    """确认后执行不可变BOM/产品关联计划。重读前值/依赖、检查摘要/冻结与未决占用，记录尝试后仅发送一次；失败、超时都不自动重试。随后reconcile_bom_change核实。"""
    try:
        with attempt_lock(PLAN_DB, 'bom-domain'), attempt_lock(PLAN_DB, plan_id):
            with db_connect() as db: row = load_row(db, plan_id)
            value = checked(row, plan_digest); ready(row); snapshot = value['snapshot']
            refreshed = await asyncio.wait_for(rebuild(value['arguments']), 60); refreshed['environment'] = snapshot['environment']
            if digest(refreshed) != plan_digest: raise ValueError('BOM、产品或引用对象已变化，未写入；请重新预览。')
            with db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if load_row(db, plan_id) != row: raise ValueError('领取前BOM计划已变化。')
                ready(row); pending_check(db, snapshot)
                attempt = uuid.uuid4().hex
                db.execute("UPDATE plans SET state='in_flight' WHERE id=?", (plan_id,))
                db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)', (plan_id, attempt, time.time()))
            outcome = {'state': 'unknown', 'submission_attempted': True, 'attempt_id': attempt, 'retry_allowed': False, 'verified_success': False}
            try:
                response = await asyncio.wait_for(get_client().request(snapshot['method'], CAMSTAR_BASE_URL.rstrip('/') + snapshot['path'], headers=get_headers(), json=snapshot['body'], follow_redirects=False), 60)
                outcome.update(http_status=response.status_code, raw_response=response.text)
                try: outcome['response'] = response.json()
                except ValueError: pass
            except BaseException as exc:
                outcome['error'] = repr(exc)
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)): raise
            finally:
                with db_connect() as db:
                    db.execute("UPDATE plans SET state='unknown',outcome=? WHERE id=? AND state='in_flight'", (json.dumps(outcome, ensure_ascii=False), plan_id))
                    db.execute('UPDATE attempts SET finished=? WHERE plan_id=?', (time.time(), plan_id))
            return encode(outcome)
    except (ValueError, TypeError, KeyError, AttributeError, AttemptBusy, asyncio.TimeoutError) as exc:
        return encode({'submitted': False, 'error': str(exc) or 'BOM执行前复核超时。'})


@mcp.tool
async def reconcile_bom_change(plan_id: str, plan_digest: str) -> str:
    """回读BOM/产品关联并保存本地核实结果，不写MES。只有原响应明确成功且配置匹配才verified_applied；升版还须旧版明细/配置保留且ROR正确迁移。无响应的相同配置仍unknown，绝不重发。"""
    try:
        with attempt_lock(PLAN_DB, 'bom-domain'), attempt_lock(PLAN_DB, plan_id):
            with db_connect() as db:
                row = load_row(db, plan_id); value = checked(row, plan_digest)
                attempt = db.execute('SELECT * FROM attempts WHERE plan_id=?', (plan_id,)).fetchone()
            if row[2] != 'unknown' or not attempt: raise ValueError('只核实有尝试记录的unknown计划。')
            snap = value['snapshot']; outcome = json.loads(row[4]) if row[4] else {}
            reader = read_bom if snap['kind'] == 'bom' else read_product
            actual = await reader(snap['name'], snap['revision'])
            match = matches(snap['expected'], stable(actual))
            if snap['action'] == 'update' or snap['kind'] == 'binding': match = match and actual['InstanceID'] == snap['target_id']
            source = None; source_preserved = True
            if snap['action'] == 'revise':
                source = await read_bom(snap['name'], snap['source_revision'])
                expected_source = copy.deepcopy(snap['before']); expected_source['IsRevOfRcd'] = False
                source_preserved = stable(source) == expected_source and source['InstanceID'] == snap['target_id']
            reply = outcome.get('response', {})
            ack = (200 <= outcome.get('http_status', 0) < 300 and isinstance(reply, dict) and
                   not reply.get('error') and not reply.get('ErrorCode') and isinstance(reply.get('value'), str) and
                   snap['name'].casefold() in reply['value'].casefold())
            verified = bool(match and source_preserved and ack)
            record = {'kind': 'automatic_check', 'configuration_matches': bool(match), 'source_version_preserved': source_preserved,
                'response_acknowledged': bool(ack), 'verified_success': verified, 'readback': actual, 'source_readback': source, 'created': time.time()}
            with db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if load_row(db, plan_id) != row: raise ValueError('核实期间计划变化。')
                db.execute('INSERT INTO reviews VALUES (?,?,?)', (uuid.uuid4().hex, plan_id, json.dumps(record, ensure_ascii=False)))
                if verified:
                    outcome.update(state='verified_applied', verified_success=True, verification=record)
                    db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?', ('verified_applied', json.dumps(outcome, ensure_ascii=False), plan_id))
            return encode({k: v for k, v in record.items() if k not in ('readback', 'source_readback')} | {'state': 'verified_applied' if verified else 'unknown', 'mes_write_performed': False, 'original_plan_reusable': False})
    except (ValueError, TypeError, KeyError, AttributeError, AttemptBusy) as exc:
        return encode({'state_changed': False, 'verified_success': False, 'error': str(exc)})


@mcp.tool
async def review_bom_change(plan_id: str, plan_digest: str, action: Literal['recover','resolve'], evidence_reference: str, review_note: str, conclusion: Literal['applied','not_applied','unknown'] = 'unknown') -> str:
    """人工审计BOM维护异常，不写MES。recover需原互斥已释放，将遗留in_flight转unknown仍保留占用；resolve须最终审计证据转人工结论。不会自动证明成功或重放原计划，保留原响应和审计记录。"""
    try:
        if action not in ('recover','resolve') or conclusion not in ('applied','not_applied','unknown'): raise ValueError('审计操作/结论无效。')
        if not isinstance(evidence_reference, str) or not 1 <= len(evidence_reference.strip()) <= 2000 or not isinstance(review_note, str) or not 10 <= len(review_note.strip()) <= 4000:
            raise ValueError('须证据引用与至少10字符人工说明。')
        with attempt_lock(PLAN_DB, 'bom-domain'), attempt_lock(PLAN_DB, plan_id), db_connect() as db:
            db.execute('BEGIN IMMEDIATE'); row = load_row(db, plan_id); checked(row, plan_digest)
            if not db.execute('SELECT 1 FROM attempts WHERE plan_id=?', (plan_id,)).fetchone(): raise ValueError('没有提交尝试，不能审计。')
            if row[2] != ('in_flight' if action == 'recover' else 'unknown'): raise ValueError('计划状态不允许本次审计。')
            state = 'unknown' if action == 'recover' or conclusion == 'unknown' else 'human_' + conclusion
            outcome = json.loads(row[4]) if row[4] else {}
            record = dict(action=action, conclusion=conclusion, evidence_reference=evidence_reference, review_note=review_note, previous_outcome=copy.deepcopy(outcome), created=time.time())
            db.execute('INSERT INTO reviews VALUES (?,?,?)', (uuid.uuid4().hex, plan_id, json.dumps(record, ensure_ascii=False)))
            outcome.update(state=state, verified_success=False, retry_allowed=False, human_review=record)
            db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?', (state, json.dumps(outcome, ensure_ascii=False), plan_id))
        return encode({'state': state, 'mes_write_performed': False, 'verified_success': False, 'original_plan_reusable': False})
    except (ValueError, TypeError, KeyError, AttributeError, AttemptBusy) as exc:
        return encode({'state_changed': False, 'error': str(exc)})
