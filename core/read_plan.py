"""Bounded JSON read plans. No eval, scripts, arbitrary URLs, SQL or writes."""
import asyncio
import copy
from decimal import Decimal, InvalidOperation, localcontext
import json
import re
import time
from typing import Annotated, Any, Literal, Union

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from core import read_catalog as catalog


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Filter(Strict):
    field: str
    operator: Literal['eq', 'in', 'is_null'] = 'eq'
    value: Any


class DatasetStep(Strict):
    id: str
    op: Literal['dataset']
    dataset: Literal['orders', 'containers', 'current_status', 'history']
    fields: list[str] = Field(min_length=1, max_length=30)
    filters: list[Filter] = Field(default_factory=list, max_length=8)
    order_by: str | None = None
    descending: bool = False
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=100, ge=1, le=200)
    max_pages: int = Field(default=1, ge=1, le=5)


class CallStep(Strict):
    id: str
    op: Literal['call']
    tool: str
    arguments: dict[str, Any]
    foreach: Any = None


class SelectStep(Strict):
    id: str
    op: Literal['select']
    source: Any
    fields: dict[str, str] = Field(min_length=1, max_length=30)


class Metric(Strict):
    op: Literal['count', 'min', 'max', 'sum']
    field: str | None = None
    kind: Literal['number', 'utc_timestamp', 'text'] = 'number'


class GroupStep(Strict):
    id: str
    op: Literal['group']
    source: Any
    keys: list[str] = Field(min_length=1, max_length=5)
    metrics: dict[str, Metric] = Field(min_length=1, max_length=15)


class JoinStep(Strict):
    id: str
    op: Literal['join']
    left: Any
    right: Any
    left_key: str
    right_key: str
    how: Literal['left', 'inner'] = 'left'
    right_prefix: str = 'related_'


Step = Annotated[Union[DatasetStep, CallStep, SelectStep, GroupStep, JoinStep], Field(discriminator='op')]


class ReadPlan(Strict):
    version: Literal[1] = 1
    steps: list[Step] = Field(min_length=1, max_length=12)
    output: Any
    concurrency: int = Field(default=3, ge=1, le=4)
    timeout_seconds: int = Field(default=60, ge=1, le=120)
    include_intermediate: bool = False


PLAN_SCHEMA = ReadPlan.model_json_schema()
MAX_ROWS = 500
MAX_RESULT_CHARS = 120000
MAX_CALLS = 30
_REF = re.compile(r'([A-Za-z][A-Za-z0-9_]{0,39})#(/.*)?')


def pointer(value, path):
    if path == '':
        return value
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('路径须为JSON Pointer（例如/data/0/ContainerId）。')
    current = value
    for raw in path[1:].split('/'):
        if re.search(r'~(?![01])', raw):
            raise ValueError('JSON Pointer转义无效。')
        part = raw.replace('~1', '/').replace('~0', '~')
        if isinstance(current, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', part) or int(part) >= len(current):
                raise ValueError('结果引用的数组位置不存在。')
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise ValueError('结果引用字段不存在；未补空值或猜测字段。')
    return current


def references(value):
    found = set()
    if isinstance(value, dict):
        if '$ref' in value:
            if set(value) - {'$ref', 'column'} or not isinstance(value['$ref'], str):
                raise ValueError('结果引用只接受$ref和可选column。')
            match = _REF.fullmatch(value['$ref'])
            if not match:
                raise ValueError('引用须为step_id#/JSON/Pointer。')
            found.add(match[1])
        else:
            for child in value.values():
                found.update(references(child))
    elif isinstance(value, list):
        for child in value:
            found.update(references(child))
    return found


def has_item_reference(value):
    if isinstance(value, dict):
        return '$item' in value or any(has_item_reference(v) for v in value.values())
    return isinstance(value, list) and any(has_item_reference(v) for v in value)


def output_references_only(value):
    if not isinstance(value, dict) or not value:
        return False
    if '$ref' in value:
        return bool(references(value))
    return all(output_references_only(v) for v in value.values())


def resolve(value, results, item=None):
    if isinstance(value, dict):
        if '$ref' in value:
            match = _REF.fullmatch(value['$ref'])
            source = results[match[1]]
            if source['status'] != 'ok':
                raise ValueError('引用的上游结果未成功。')
            result = pointer(source, match[2] or '')
            if 'column' in value:
                if not isinstance(result, list) or any(not isinstance(r, dict) or value['column'] not in r for r in result):
                    raise ValueError('column引用须为完整行数组中的明确列。')
                result = [r[value['column']] for r in result]
            return copy.deepcopy(result)
        if '$item' in value:
            if set(value) != {'$item'} or item is None:
                raise ValueError('$item仅用于foreach的单项JSON Pointer。')
            return copy.deepcopy(pointer(item, value['$item']))
        return {key: resolve(child, results, item) for key, child in value.items()}
    if isinstance(value, list):
        return [resolve(child, results, item) for child in value]
    return value


def rows(value):
    if not isinstance(value, list) or len(value) > MAX_ROWS or any(not isinstance(r, dict) for r in value):
        raise ValueError('本地关联/汇总须为不超过500行的对象数组。')
    return value


def literal(value):
    if value is None:
        return 'NULL'
    if type(value) is bool:
        return '1' if value else '0'
    if type(value) in (int, float):
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > Decimal('1e30'):
            raise ValueError('筛选数值无效。')
        return str(number)
    if isinstance(value, str) and 0 < len(value) <= 200 and '\x00' not in value:
        return "N'" + value.replace("'", "''") + "'"
    raise ValueError('筛选只接受有限标量；文本长度1..200。')


def bracket(value):
    # Identifiers are only taken from reviewed definitions/live schema, never a
    # caller's free text. Quote as well, including reserved names.
    return '[' + value.replace(']', ']]') + ']'


async def read_dataset(step, results):
    meta = await catalog.dataset_metadata(step.dataset)
    contract = meta['content']; available = contract['columns']
    fields = list(dict.fromkeys(step.fields))
    key = contract['key']
    order = step.order_by or key
    required = set(fields) | {key, order} | {f.field for f in step.filters}
    if not required <= available.keys():
        raise ValueError('字段不在此数据集已验证契约内：' + ','.join(sorted(required - available.keys())))
    # Keep a deterministic unique tie-breaker and persistent identity in rows.
    if key not in fields:
        fields.append(key)
    where = []
    for f in step.filters:
        value = resolve(f.value, results)
        column = bracket(f.field)
        if f.operator == 'is_null':
            if type(value) is not bool:
                raise ValueError('is_null.value须为布尔值。')
            where.append(column + (' IS NULL' if value else ' IS NOT NULL'))
        elif f.operator == 'in':
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError('in筛选须为最多100项的数组。')
            if any(v is None for v in value):
                raise ValueError('in不接受null，请明确用is_null。')
            where.append(column + ' IN (' + ','.join(literal(v) for v in value) + ')' if value else '1=0')
        elif value is None:
            where.append(column + ' IS NULL')
        else:
            where.append(column + '=' + literal(value))
    base = ('SELECT ' + ','.join(bracket(f) for f in fields) + ' FROM ' +
            bracket(contract['schema']) + '.' + bracket(contract['table']) +
            (' WHERE ' + ' AND '.join(where) if where else '') +
            ' ORDER BY ' + bracket(order) + (' DESC' if step.descending else ' ASC') +
            (',' + bracket(key) + ' ASC' if order != key else ''))
    collected = []; more = False; pages = 0
    for page in range(step.max_pages):
        # Every query has a hard bound; no unbounded "get all" hidden in plans.
        offset = step.offset + page * step.limit
        values = await catalog.sql_rows(base + f' OFFSET {offset} ROWS FETCH NEXT {step.limit + 1} ROWS ONLY')
        pages += 1
        if len(values) > step.limit + 1 or any(set(v) != set(fields) or not v.get(key) for v in values):
            raise ValueError('数据集身份列、选定字段或分页返回不完整。')
        more = len(values) > step.limit
        collected.extend(values[:step.limit])
        if len(collected) > MAX_ROWS:
            raise ValueError('累计结果超过500行；请缩小范围。')
        if not more:
            break
    ids = [r[key] for r in collected]
    if len(set(ids)) != len(ids):
        raise ValueError('跨页持久ID重复；分页期间数据可能变化。')
    return {'data': collected, 'complete': not more and step.offset == 0,
            'coverage': 'complete' if not more and step.offset == 0 else 'partial',
            'page': {'offset': step.offset, 'limit': step.limit, 'pages_read': pages,
                     'has_more': more, 'next_offset': step.offset + len(collected) if more else None},
            'source': {'dataset': step.dataset, 'table': contract['table'],
                       'schema_digest': meta['schema_digest'], 'metadata_cached': meta['cached']},
            'warnings': ['当前关联、跨请求非事务快照；未默认排除已关闭容器。']}


def compare_value(value, kind, field=''):
    if kind == 'number':
        if type(value) not in (str, int, float) or len(str(value)) > 200:
            raise ValueError('数值汇总遇到非数值。')
        try:
            result = Decimal(str(value))
        except InvalidOperation:
            raise ValueError('数值汇总遇到非法值。') from None
        if not result.is_finite() or abs(result) > Decimal('1e40') or result.as_tuple().exponent < -40:
            raise ValueError('数值汇总遇到非有限值。')
        return result
    if kind == 'utc_timestamp':
        from tools.collection_validation import timestamp_ticks
        if not isinstance(value, str):
            raise ValueError('UTC时间汇总须为时间文本。')
        explicit = bool(re.search(r'(Z|[+-]\d\d:\d\d)$', value))
        if not explicit and not field.lower().endswith(('gmt', 'utc')):
            raise ValueError('无时区时间只能在明确GMT/UTC源字段汇总；不能猜测本地时间时区。')
        return timestamp_ticks(value if explicit else value + 'Z')
    if not isinstance(value, str):
        raise ValueError('文本汇总须为文本。')
    return value


def group_rows(step, values):
    groups = {}
    for row in rows(values):
        if any(k not in row for k in step.keys):
            raise ValueError('分组字段缺失。')
        key = tuple(row[k] for k in step.keys)
        if any(type(v) not in (str, int, float, bool, type(None)) for v in key):
            raise ValueError('分组键须为标量。')
        groups.setdefault(key, []).append(row)
    output = []
    for key, group in groups.items():
        result = dict(zip(step.keys, key))
        for alias, metric in step.metrics.items():
            if alias in result:
                raise ValueError('汇总别名不能覆盖分组键。')
            if metric.op == 'count' and metric.field is None:
                result[alias] = len(group); continue
            if not metric.field or any(metric.field not in r for r in group):
                raise ValueError('汇总字段缺失。')
            present = [r[metric.field] for r in group if r[metric.field] not in (None, '')]
            missing = len(group) - len(present)
            if alias + '_missing_count' in step.metrics or alias + '_missing_count' in step.keys:
                raise ValueError('汇总缺值计数列名冲突。')
            result[alias + '_missing_count'] = missing
            if metric.op == 'count':
                result[alias] = len(present)
            elif not present:
                result[alias] = None
            elif metric.op == 'sum':
                if metric.kind != 'number':
                    raise ValueError('sum仅支持number。')
                with localcontext() as context:
                    context.prec = 100  # bounded +/-40 exponents and <=500 values
                    result[alias] = str(sum((compare_value(v, 'number') for v in present), Decimal(0)))
            else:
                index = (min if metric.op == 'min' else max)(range(len(present)),
                    key=lambda i: compare_value(present[i], metric.kind, metric.field))
                result[alias] = present[index]
        output.append(result)
    return output


def join_rows(step, left, right):
    left, right = rows(left), rows(right)
    if any(step.left_key not in r for r in left) or any(step.right_key not in r for r in right):
        raise ValueError('关联键缺失。')
    index = {}
    for row in right:
        key = row[step.right_key]
        if key not in (None, ''):
            if not isinstance(key, (str, int, float, bool)):
                raise ValueError('关联键须为标量。')
            index.setdefault((type(key).__name__, key), []).append(row)
    out = []
    for row in left:
        key = row[step.left_key]
        if key not in (None, '') and not isinstance(key, (str, int, float, bool)):
            raise ValueError('关联键须为标量。')
        matches = index.get((type(key).__name__, key), []) if key not in (None, '') else []
        if not matches and step.how == 'left':
            matches = [None]
        if step.right_prefix + 'matched' in row:
            raise ValueError('关联标志列名冲突。')
        for match in matches:
            extra = {step.right_prefix + k: v for k, v in (match or {}).items()}
            if set(extra) & (set(row) | {step.right_prefix + 'matched'}):
                raise ValueError('关联输出字段冲突。')
            out.append({**row, **extra, step.right_prefix + 'matched': match is not None})
            if len(out) > MAX_ROWS:
                raise ValueError('关联结果超过500行（可能一对多膨胀）。')
    return out


async def tool_schemas():
    from tools import mcp
    return {t.name: t.parameters for t in await mcp.list_tools() if t.name in catalog.READ_TOOLS}


def validate(plan, schemas):
    raw = plan.model_dump() if isinstance(plan, ReadPlan) else plan
    if len(json.dumps(raw, allow_nan=False)) > 40000:
        raise ValueError('计划超过40000字符。')
    parsed = ReadPlan.model_validate(plan)
    ids = [s.id for s in parsed.steps]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,39}', i) for i in ids):
        raise ValueError('步骤ID须唯一且为1..40位字母开头标识符。')
    deps = {}
    from core.tool_catalog import TOOL_POLICIES
    for step in parsed.steps:
        if isinstance(step, CallStep):
            if (step.tool not in catalog.READ_TOOLS or step.tool not in schemas
                    or TOOL_POLICIES[step.tool].effect != 'read'):
                raise ValueError('编排只接受专门审核的只读工具；不接受写入、预览、计划、任意事件或递归调用。')
            schema = schemas[step.tool]
            if set(step.arguments) - schema.get('properties', {}).keys() or not set(schema.get('required', [])) <= step.arguments.keys():
                raise ValueError('工具参数名或必填参数不符。')
            if not references(step.arguments) and not has_item_reference(step.arguments):
                Draft202012Validator(schema).validate(step.arguments)
            if step.foreach is None and has_item_reference(step.arguments):
                raise ValueError('$item引用须同时明确foreach。')
        sources = ([step.source] if isinstance(step, (SelectStep, GroupStep)) else
                   [step.left, step.right] if isinstance(step, JoinStep) else [])
        if any(not isinstance(v, dict) or '$ref' not in v for v in sources):
            raise ValueError('投影/汇总/关联的数据源必须引用真实上游步骤，不接受调用方自造结果。')
        if isinstance(step, DatasetStep):
            reviewed = set(catalog.DATASETS[step.dataset]['fields'])
            required = set(step.fields) | {f.field for f in step.filters}
            if step.order_by:
                required.add(step.order_by)
            if not required <= reviewed:
                raise ValueError('计划包含审核数据集之外的字段。')
        dep = references(step.model_dump())
        if step.id in dep or not dep <= set(ids):
            raise ValueError('步骤引用自身或未知步骤。')
        deps[step.id] = dep
    if not output_references_only(parsed.output) or not references(parsed.output) <= set(ids):
        raise ValueError('输出引用未知步骤。')
    done = set()
    while len(done) < len(ids):
        ready = {i for i in ids if i not in done and deps[i] <= done}
        if not ready:
            raise ValueError('查询计划存在循环依赖。')
        done.update(ready)
    return parsed, deps


async def run(plan):
    schemas = await tool_schemas()
    parsed, deps = validate(plan, schemas)  # validate all operations before any MES reads
    results = {}; calls = 0; sem = asyncio.Semaphore(parsed.concurrency)
    from tools import get_tool_func
    async def call_one(step, args):
        nonlocal calls
        if calls >= MAX_CALLS:
            raise ValueError('达到30次工具调用上限。')
        calls += 1
        Draft202012Validator(schemas[step.tool]).validate(args)
        for name in ('limit', 'top', 'max_depth'):
            if name in args and (type(args[name]) is not int or not 1 <= args[name] <= (3 if name == 'max_depth' else 100)):
                raise ValueError('编排中的limit/top<=100，max_depth<=3。')
        async with sem:
            raw = await get_tool_func(step.tool)(**args)
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except ValueError:
                raise ValueError('原工具返回错误、裁剪文本或非结构化结果，未认定成功：' + raw[:1200]) from None
        else:
            value = raw
        if not isinstance(value, (dict, list)):
            raise ValueError('原工具未返回结构化结果。')
        if isinstance(value, dict) and (value.get('error') or value.get('ErrorCode')):
            raise ValueError('原工具查询失败：' + str(value.get('error') or value.get('ErrorCode'))[:1200])
        if len(json.dumps(value, ensure_ascii=False)) > MAX_RESULT_CHARS:
            raise ValueError('原工具返回超过上限；请缩小范围，不盲目截断。')
        return value

    async def execute(step):
        started = time.perf_counter()
        inherited = all(results[d].get('complete') is True for d in deps[step.id])
        try:
            if any(results[d]['status'] != 'ok' for d in deps[step.id]):
                return {'status': 'skipped', 'complete': False, 'coverage': 'unknown',
                        'error': '上游失败或跳过，未执行。', 'elapsed_ms': 0}
            if isinstance(step, DatasetStep):
                async with sem:
                    payload = await read_dataset(step, results)
                payload['complete'] = payload['complete'] and inherited
            elif isinstance(step, CallStep):
                items = resolve(step.foreach, results) if step.foreach is not None else [None]
                if not isinstance(items, list) or len(items) > 20:
                    raise ValueError('foreach须为最多20项数组。')
                # Reserve total call budget before starting a batch.
                if calls + len(items) > MAX_CALLS:
                    raise ValueError('foreach超过总调用上限。')
                arguments = [resolve(step.arguments, results, item) for item in items]
                for args in arguments:
                    Draft202012Validator(schemas[step.tool]).validate(args)
                values = await asyncio.gather(*(call_one(step, args) for args in arguments),
                                              return_exceptions=True)
                errors = [str(v) for v in values if isinstance(v, BaseException)]
                if errors:
                    raise ValueError('只读调用有失败；未当作空列表：' + '; '.join(errors)[:1500])
                value = values if step.foreach is not None else values[0]
                complete = all(isinstance(v, dict) and v.get('complete') is True for v in values)
                payload = {'data': value, 'complete': complete and inherited,
                           'coverage': 'complete' if complete and inherited else 'unknown',
                           'source': {'tool': step.tool}, 'warnings': ['原工具完整性标记原样保留；未声明完整则为unknown。']}
            else:
                if not inherited:
                    raise ValueError('关联/汇总/投影的上游未明确完整；请补齐分页或先查看原结果。')
                if isinstance(step, SelectStep):
                    value = [{alias: pointer(row, path) for alias, path in step.fields.items()}
                             for row in rows(resolve(step.source, results))]
                elif isinstance(step, GroupStep):
                    value = group_rows(step, resolve(step.source, results))
                else:
                    value = join_rows(step, resolve(step.left, results), resolve(step.right, results))
                payload = {'data': value, 'complete': True, 'coverage': 'complete',
                           'source': {'operation': step.op, 'dependencies': sorted(deps[step.id])}}
            payload['coverage'] = payload.get('coverage') if payload['complete'] else ('partial' if payload.get('coverage') == 'partial' else 'unknown')
            payload.update(status='ok', elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
            return payload
        except Exception as exc:
            return {'status': 'error', 'complete': False, 'coverage': 'unknown',
                    'error': str(exc)[:2000], 'elapsed_ms': round((time.perf_counter() - started) * 1000, 2)}

    async def execute_graph():
        pending = {s.id: s for s in parsed.steps}
        while pending:
            ready = [s for s in pending.values() if deps[s.id] <= results.keys()]
            values = await asyncio.gather(*(execute(s) for s in ready))
            for step, value in zip(ready, values):
                results[step.id] = value; pending.pop(step.id)
    started = time.perf_counter()
    timed_out = False
    try:
        await asyncio.wait_for(execute_graph(), parsed.timeout_seconds)
    except asyncio.TimeoutError:
        timed_out = True
        for step in parsed.steps:
            results.setdefault(step.id, {'status': 'timeout', 'complete': False, 'coverage': 'unknown',
                                        'error': '计划总时限已到；未自动重试，后续步骤未执行。'})
    try:
        output = resolve(parsed.output, results)
    except (ValueError, KeyError):
        output = None
    complete = all(r.get('complete') is True and r['status'] == 'ok' for r in results.values()) and not timed_out
    evidence = results if parsed.include_intermediate else {
        name: {key: value for key, value in result.items() if key != 'data'} for name, result in results.items()}
    answer = {'complete': complete, 'output': output, 'steps': evidence,
              'metrics': {'elapsed_ms': round((time.perf_counter() - started) * 1000, 2),
                          'tool_calls': calls, 'model_rounds_inside_executor': 0,
                          'dataset_queries': sum(r.get('page', {}).get('pages_read', 0) for r in results.values())},
              'mes_write_performed': False, 'atomic_snapshot': False,
              'warnings': ['只读编排不是MES事务快照；元数据可缓存，生产结果不跨请求缓存。']}
    encoded = json.dumps(answer, ensure_ascii=False, allow_nan=False)
    if len(encoded) > MAX_RESULT_CHARS:
        return {'complete': False, 'mes_write_performed': False, 'output': None,
                'error': '计划结果超过120000字符；未返回裁剪后的伪完整结果，请缩小选列或页数。'}
    return answer
