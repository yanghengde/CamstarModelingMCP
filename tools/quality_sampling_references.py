"""Named sampling tables: explicit rows, static previews and guarded plans."""
import copy
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from tools import mcp, quality as q, quality_inspection as i, quality_definitions as d
from tools.process_context import ReadContext

Kind = Literal['aql_level', 'inspection_level']
Name = Annotated[str, Field(min_length=1, max_length=30)]
CONFIG = {
    'aql_level': ('AQLLevels', 'AQLLevel', 'AQLLevelId', 'SampleSizeDetails', ('SampleSizeCode', 'SampleSize', 'Accepts', 'Rejects')),
    'inspection_level': ('InspectionLevels', 'InspectionLevel', 'InspectionLevelId', 'LotSizeDetails', ('SampleSizeCode', 'MinLotQty', 'MaxLotQty')),
}
COMMON = ('InstanceID', 'Name', 'Description', 'IsFrozen')
MAX_ROWS = 50


class AQLRowChange(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    op: Literal['add', 'update', 'remove']
    detail_instance_id: str | None = None
    sample_size_code: str | None = Field(default=None, max_length=30)
    sample_size: StrictInt | None = Field(default=None, ge=1, le=2147483647)
    accepts: StrictInt | None = Field(default=None, ge=0, le=2147483647)
    rejects: StrictInt | None = Field(default=None, ge=1, le=2147483647)


class LotRangeChange(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    op: Literal['add', 'update', 'remove']
    detail_instance_id: str | None = None
    sample_size_code: str | None = Field(default=None, max_length=30)
    min_lot_qty: StrictInt | None = Field(default=None, ge=0, le=2147483647)
    max_lot_qty: StrictInt | None = Field(default=None, ge=1, le=2147483647)


class SamplingReferenceChanges(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    description: str | None = Field(default=None, max_length=255)
    aql_rows: list[AQLRowChange] | None = Field(default=None, min_length=1, max_length=MAX_ROWS)
    lot_ranges: list[LotRangeChange] | None = Field(default=None, min_length=1, max_length=MAX_ROWS)


def config(kind):
    if kind not in CONFIG:
        raise ValueError('仅支持aql_level/inspection_level。')
    return CONFIG[kind]


def code(value):
    result = storage_name(value, '样本量代码')
    if result != value:
        raise ValueError('样本量代码不能包含首尾空白。')
    return result


def storage_name(value, label='配置名称'):
    result = q.text(value, label, 30)
    if len(result.encode('utf-16-le')) > 60:
        raise ValueError(label+'超过部署nvarchar(30)容量。')
    return result


async def find(kind, name):
    entity = config(kind)[0]; name = storage_name(name)
    data = await ReadContext(1).get(entity, params={'$filter': "Name eq '"+name.replace("'", "''")+"'", '$top': 2})
    rows = data.get('value')
    if not isinstance(rows, list) or len(rows) > 1 or q._has_next_link(data):
        raise ValueError('命名配置不存在唯一、完整的匹配。')
    if any(r.get('Name') != name or not q.valid_id(r.get('InstanceID')) for r in rows):
        raise ValueError('命名配置列表身份不一致。')
    return rows[0] if rows else None


async def token(kind, key):
    _, table, column, _, _ = config(kind)
    if not q.valid_id(key):
        raise ValueError('配置ID无效。')
    rows = await q.sql_rows(f"SELECT {column},ChangeCount FROM {table} WHERE {column}='{key}'", 1)
    if len(rows) != 1 or rows[0].get(column) != key:
        raise ValueError('配置变更计数身份不一致。')
    return i.wire_int(rows[0].get('ChangeCount'), '配置ChangeCount')


def normalized(kind, name, key, data):
    _, _, _, field, row_fields = config(kind)
    if (not isinstance(data, dict) or data.get('Name') != name or data.get('InstanceID') != key
            or any(k not in data for k in COMMON) or q._has_next_link(data)):
        raise ValueError('配置字段、身份或展开完整性不符。')
    rows = data.get(field)
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError('配置明细缺失或超过50行，不能维护分页子集。')
    result = {k: copy.deepcopy(data[k]) for k in COMMON}
    result['IsFrozen'] = i.wire_bool(data['IsFrozen'], '配置冻结状态')
    seen = set(); details = []
    for row in rows:
        if (not isinstance(row, dict) or not q.valid_id(row.get('InstanceID')) or row['InstanceID'] in seen
                or q.identifier(row.get('Parent')) != key or any(k not in row for k in row_fields)):
            raise ValueError('明细ID重复、字段缺失或父级不一致。')
        seen.add(row['InstanceID'])
        details.append({k: copy.deepcopy(row.get(k)) for k in ('InstanceID',)+row_fields} |
                       {'IsFrozen': i.wire_bool(row.get('IsFrozen'), '明细冻结状态')})
    result[field] = details
    return result


async def read_state(kind, name, instance_id=None):
    entity, _, _, field, _ = config(kind); name = storage_name(name)
    if instance_id is not None and not q.valid_id(instance_id):
        raise ValueError('配置ID无效。')
    found = await find(kind, name)
    if not found:
        return None
    key = found['InstanceID']
    if instance_id is not None and instance_id != key:
        raise ValueError('配置名称与ID不一致。')
    ctx = ReadContext(2); first = await token(kind, key)
    params = {'$select': ','.join(COMMON+(field,)), '$expand': field}
    before = normalized(kind, name, key, await ctx.get(entity, key, params))
    after = normalized(kind, name, key, await ctx.get(entity, key, params))
    if before != after or await token(kind, key) != first:
        raise ValueError('读取期间配置或明细发生变化。')
    return {**before, 'ChangeCount': first}


def validate_table(kind, rows):
    if not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError('完整有效表必须有1..50行，不能删除最后一行。')
    if kind == 'aql_level':
        seen = set()
        for row in rows:
            value = code(row.get('SampleSizeCode'))
            if value.casefold() in seen:
                raise ValueError('AQL样本量代码重复。')
            seen.add(value.casefold())
            size = row.get('SampleSize')
            if not isinstance(size, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', size):
                raise ValueError('当前仅支持字面正整数样本量，不求值公式、百分比或箭头规则。')
            size = i.int32(int(size), '样本量', 1)
            accepts = i.wire_int(row.get('Accepts'), '接收数')
            rejects = i.wire_int(row.get('Rejects'), '拒收数')
            if accepts > size or not accepts < rejects <= size+1:
                raise ValueError('须满足0 <= accepts <= sample_size且accepts < rejects <= sample_size+1。')
    else:
        ranges = []
        for row in rows:
            code(row.get('SampleSizeCode'))
            low = i.wire_int(row.get('MinLotQty'), '批量下限')
            high = i.wire_int(row.get('MaxLotQty'), '批量上限')
            if high < 1 or low > high:
                raise ValueError('批量区间上下限反置或上限非正。')
            ranges.append((low, high))
        ranges.sort()
        if any(a[1] >= b[0] for a, b in zip(ranges, ranges[1:])):
            raise ValueError('批量闭区间重叠，含共用端点；不能选择首个匹配。')


def changed_rows(kind, raw_changes, before, creating):
    field = config(kind)[3]
    old = copy.deepcopy(before[field]) if before else []
    current = {r['InstanceID']: r for r in old}
    touched = set(); updates = []; deletions = []; adds = []
    model = AQLRowChange if kind == 'aql_level' else LotRangeChange
    mapping = {'sample_size_code': 'sampleSizeCode', 'sample_size': 'sampleSize', 'accepts': 'accepts', 'rejects': 'rejects'} if kind == 'aql_level' else {
        'sample_size_code': 'sampleSizeCode', 'min_lot_qty': 'minLotQty', 'max_lot_qty': 'maxLotQty'}
    for raw in raw_changes:
        change = model.model_validate(raw); values = change.model_dump(exclude_unset=True)
        key = change.detail_instance_id
        if change.op == 'add':
            if key is not None:
                raise ValueError('新增明细不能传ID。')
            if set(mapping)-values.keys():
                raise ValueError('新增明细必须明确全部业务字段。')
            row = {'isFrozen': False}
        else:
            if creating or not q.valid_id(key) or key not in current or key in touched:
                raise ValueError('更新/删除须唯一未重复的现有明细ID。')
            touched.add(key)
            if current[key]['IsFrozen'] is not False:
                raise ValueError('待维护明细已冻结。')
            index = next(n for n, r in enumerate(old) if r['InstanceID'] == key)
            if change.op == 'remove':
                if set(values)-{'op', 'detail_instance_id'}:
                    raise ValueError('删除明细不能同时修改字段。')
                del current[key]; deletions.append({'index': index, 'listItemAction': 'delete'}); continue
            if not (set(values) & set(mapping)):
                raise ValueError('更新明细没有明确变更。')
            row = {}
        for arg, wire in mapping.items():
            if arg not in values:
                continue
            value = values[arg]
            if value is None:
                raise ValueError(arg+'不能为null。')
            if arg == 'sample_size_code': value = code(value)
            if arg == 'sample_size': value = str(value)
            row[wire] = value
        projected = {k[0].upper()+k[1:]: v for k, v in row.items()}
        if change.op == 'add':
            adds.append(row)
        else:
            current[key].update(projected)
            updates.append({'index': index, 'listItemAction': 'change', 'value': row})
    effective = list(current.values()) + [{k[0].upper()+k[1:]: v for k, v in row.items()} for row in adds]
    validate_table(kind, effective)
    wrappers = updates + sorted(deletions, key=lambda r: -r['index']) + [{'listItemAction': 'add', 'value': row} for row in adds]
    return adds if creating else wrappers, effective


async def build_reference(args, marker):
    from tools import quality_plans as plans
    kind = args['reference_kind']; entity, _, _, field, _ = config(kind)
    name = storage_name(args['name']); action = args['action']
    if action not in ('create', 'update'):
        raise ValueError('命名抽样配置仅开放create/update，不接受升版、删除或重命名。')
    changes = SamplingReferenceChanges.model_validate(args['changes']).model_dump(exclude_unset=True)
    field_arg = 'aql_rows' if kind == 'aql_level' else 'lot_ranges'
    if not changes or set(changes)-{'description', field_arg}:
        raise ValueError('没有变更或变更字段不适用于此配置。')
    if 'description' in changes and (changes['description'] is None or '\x00' in changes['description']):
        raise ValueError('说明须为不含NUL的明确字符串。')
    if 'description' in changes and len(changes['description'].encode('utf-16-le')) > 510:
        raise ValueError('说明超过部署nvarchar(255)容量。')
    before = None
    if action == 'create':
        if args.get('instance_id') is not None or await find(kind, name):
            raise ValueError('创建不接受目标ID，且名称必须尚不存在。')
    else:
        before = await read_state(kind, name, args.get('instance_id'))
        if not before or before['IsFrozen'] is not False:
            raise ValueError('目标配置不存在、已冻结或状态未知。')
    body = {'name': name}; effective = (before or {}).get(field, [])
    if 'description' in changes: body['description'] = changes['description']
    if field_arg in changes:
        if not changes[field_arg]:
            raise ValueError('明细变更须为非空列表。')
        wire, effective = changed_rows(kind, changes[field_arg], before, action == 'create')
        body[field[0].lower()+field[1:]+('' if action == 'create' else '-Expanded')] = wire
    else:
        validate_table(kind, effective)
        if action == 'create': raise ValueError('创建必须明确完整明细。')
    key = before['InstanceID'] if before else None
    old_codes = {r['SampleSizeCode'] for r in (before or {}).get(field, [])}
    removed_codes = sorted(old_codes-{r['SampleSizeCode'] for r in effective})
    return {'kind': 'sampling_reference', 'reference_kind': kind, 'action': action, 'name': name,
            'target_id': key, 'entity': entity, 'scope_key': 'sampling-reference:'+entity+':'+name.casefold(),
            'api': 'modeling', 'method': 'POST' if action == 'create' else 'PUT',
            'path': '/api/'+entity+('/'+key if key else ''), 'body': body, 'before': before,
            'resource_reads': [], 'resource_writes': sorted(d.resources(entity, {'Name': name, **({'InstanceID': key} if key else {})})),
            'preview': {'static_contract_verified': True, 'echo_verified': False, 'dynamic_validation_verified': False,
                        'effective_rows': effective, 'named_object_without_revision_or_status': True,
                        'codes_removed_from_table': removed_codes, 'cross_table_references_verified': False,
                        'note': '仅静态契约与完整表校验，未执行Modeling写入预演；不补标准抽样表、不回填已生成任务、不判质量。'},
            'marker': marker, 'arguments': args, 'environment': plans.environment()}


@mcp.tool
async def get_sampling_reference_definition(reference_kind: Kind, name: Name, instance_id: str | None = None) -> str:
    """只读AQL等级或检验水平完整有界表，最多50行；按名称和可选ID确认，含冻结、明细ID及ChangeCount。二者是命名对象，没有Revision/Status。AQL的SampleSize保留原始字符串；不计算公式、默认标准表、最终MES样本要求或质量结论。"""
    try:
        state = await read_state(reference_kind, name, instance_id)
        return q.encode({'found': state is not None, 'complete': True, 'data': state, 'quality_verdict': None})
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return q.encode({'complete': False, 'error': str(error)})


@mcp.tool
async def prepare_sampling_reference_definition(reference_kind: Kind, action: Literal['create', 'update'], name: Name,
        changes: SamplingReferenceChanges, instance_id: str | None = None) -> str:
    """准备命名AQL等级/检验水平的创建或更新计划，不写MES。aql_rows逐行明确代码、正整数sample_size、accepts/rejects；lot_ranges明确代码及批量闭区间，重叠拒绝。行op为add/update/remove，更新/删除必须现有detail_instance_id；删除不清空全表。无版本/Status、升版、整表删除或标准表自动生成。静态预览，execute_quality_transaction确认后单次提交，unknown保留占用。"""
    from tools import quality_plans as plans
    try:
        normalized_changes = SamplingReferenceChanges.model_validate(changes).model_dump(exclude_unset=True)
        return await plans.prepare('sampling_reference', dict(reference_kind=reference_kind, action=action, name=name,
                                  changes=normalized_changes, instance_id=instance_id))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return q.encode({'submitted': False, 'error': str(error)})


@mcp.tool
async def preview_sampling_table_lookup(inspection_level_name: Name, aql_level_name: Name, lot_quantity: StrictInt) -> str:
    """只读配置查表预览：明确正整数批量落入唯一检验水平闭区间，按原样代码精确匹配唯一AQL行，返回字面样本量及接收/拒收阈值。完整表最多50行，重叠、缺代码、公式和冻结拒绝。允许配置有空档但无匹配即报错；不截断超过批量的样本量，不宣称符合某抽样标准，不替代MES当前任务要求或最终质量判定。"""
    try:
        i.int32(lot_quantity, '批量', 1)
        level = await read_state('inspection_level', inspection_level_name)
        aql = await read_state('aql_level', aql_level_name)
        if not level or not aql or level['IsFrozen'] is not False or aql['IsFrozen'] is not False:
            raise ValueError('配置不存在或已冻结。')
        ranges = level['LotSizeDetails']; rows = aql['SampleSizeDetails']
        validate_table('inspection_level', ranges); validate_table('aql_level', rows)
        matched = [r for r in ranges if i.wire_int(r['MinLotQty'], '下限') <= lot_quantity <= i.wire_int(r['MaxLotQty'], '上限')]
        if len(matched) != 1: raise ValueError('批量没有唯一匹配区间。')
        code_value = matched[0]['SampleSizeCode']; selected = [r for r in rows if r['SampleSizeCode'] == code_value]
        if len(selected) != 1: raise ValueError('区间代码在AQL表中没有唯一原样匹配。')
        if matched[0]['IsFrozen'] is not False or selected[0]['IsFrozen'] is not False:
            raise ValueError('匹配明细已冻结。')
        if await read_state('inspection_level', inspection_level_name) != level or await read_state('aql_level', aql_level_name) != aql:
            raise ValueError('查表期间配置发生变化。')
        return q.encode({'complete': True, 'lot_quantity': lot_quantity, 'inspection_range': matched[0], 'aql_row': selected[0],
                         'configured_sample_size': int(selected[0]['SampleSize']), 'sample_size_exceeds_lot': int(selected[0]['SampleSize']) > lot_quantity,
                         'unmapped_inspection_codes': sorted({r['SampleSizeCode'] for r in ranges}-{r['SampleSizeCode'] for r in rows}),
                         'quality_verdict': None, 'mes_task_requirements_verified': False, 'standard_compliance_verified': False,
                         'note': '仅配置查表，未应用任务冻结、频率、切换规则或批量调整；保留原配置样本量，不自动裁剪。'})
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return q.encode({'complete': False, 'quality_verdict': None, 'error': str(error)})
