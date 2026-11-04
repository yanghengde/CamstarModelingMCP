"""Read persisted SPC inputs and diagnose deployment prerequisites without running the engine."""
import asyncio
from datetime import datetime
import re
from typing import Annotated

from pydantic import Field, StrictStr

from tools import mcp, quality as q, quality_spc as spc, quality_spc_annotations as annotations
from tools.quality_spc_config import chart_readiness
from tools import quality_inspection as inspection
from tools.collection_submission import digest
from tools.process_context import ReadContext


HistoryGroup = Annotated[list[StrictStr], Field(min_length=1, max_length=10)]
HistoryGroups = Annotated[list[HistoryGroup], Field(min_length=1, max_length=100)]
MAX_POINTS = 100
PROCESS_IDS = ('ProductId', 'OperationId', 'SpecId', 'WorkflowStepId')
OPTIONAL_PROCESS_IDS = ('FactoryId', 'ResourceId')
MAINLINE_FIELDS = annotations.SCHEMA['HistoryMainline'] + ',' + ','.join(PROCESS_IDS + OPTIONAL_PROCESS_IDS)
SCHEMA = {
    table: annotations.SCHEMA[table] for table in
    ('DataPointHistoryDetail', 'DataPointHistory', 'AddSPCAnnotationHistory')
} | {'HistoryMainline': MAINLINE_FIELDS}
HISTORY_JOIN = (
    ' FROM DataPointHistoryDetail d INNER JOIN DataPointHistory h ON h.DataPointHistoryId=d.DataPointHistoryId'
    ' INNER JOIN HistoryMainline m ON m.HistoryMainlineId=h.HistoryMainlineId'
)
POINT_FIELDS = (
    'd.DataPointHistoryDetailId,d.DataPointHistoryId,d.DataCollectionDefId,d.DataPointId,d.DataName,d.DataType,'
    'd.DataValue,d.UOMId,d.HistoryId,d.TxnId,d.ChangeCount,h.ChangeCount AS ParentChangeCount,'
    'h.DataCollectionDefId AS ParentDefinitionId,h.HistoryId AS ParentHistoryId,h.TxnId AS ParentTxnId,'
    'm.HistoryMainlineId,m.HistoryId AS MainlineHistoryId,m.TxnId AS MainlineTxnId,m.ContainerId,'
    'm.ChangeCount AS MainlineChangeCount,m.ReversalStatus,m.TxnDateGMT,'
    + ','.join('m.' + key for key in PROCESS_IDS + OPTIONAL_PROCESS_IDS)
)
POINT_IDS = (
    'DataPointHistoryDetailId', 'DataPointHistoryId', 'DataCollectionDefId', 'DataPointId', 'UOMId', 'HistoryId',
    'TxnId', 'ParentDefinitionId', 'ParentHistoryId', 'ParentTxnId', 'HistoryMainlineId', 'MainlineHistoryId',
    'MainlineTxnId', 'ContainerId',
) + PROCESS_IDS
SCOPE_FIELDS = ('DataCollectionDefId', 'DataPointId', 'DataName', 'DataType', 'UOMId') + PROCESS_IDS + OPTIONAL_PROCESS_IDS
EVIDENCE_FIELDS = ('DataPointHistoryDetailId', 'DataPointHistoryId', 'HistoryMainlineId', 'HistoryId',
                   'TxnId', 'ContainerId', 'TxnDateGMT', 'DataValue', 'ChangeCount', 'ParentChangeCount',
                   'MainlineChangeCount')
UTC_TIME = re.compile(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?(?:Z|\+00:00)?\Z')


def timestamp(value):
    match = UTC_TIME.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError('原始事务GMT时间缺失、精度未知或带非UTC偏移。')
    second, fraction = match.groups()
    # SQL datetime2 has seven fractional digits; Python datetime keeps six.
    return datetime.fromisoformat(second), (fraction or '').ljust(7, '0')


def point_groups(groups, chart_type, minimum):
    if not isinstance(groups, list) or not minimum <= len(groups) <= MAX_POINTS:
        raise ValueError(f'每段须提供{minimum}..{MAX_POINTS}组原始采集历史ID。')
    if chart_type not in ('i_mr', 'xbar_r'):
        raise ValueError('历史计算仅支持i_mr/xbar_r。')
    allowed = {1} if chart_type == 'i_mr' else set(spc.FACTORS)
    if any(not isinstance(group, list) or len(group) not in allowed for group in groups):
        raise ValueError('i_mr每组须1点，xbar_r须明确固定2..10点子组。')
    if len({len(group) for group in groups}) != 1:
        raise ValueError('历史子组大小须固定。')
    return [[annotations.iid(key, '原始采集历史点') for key in group] for group in groups]


def point_record(raw):
    row = dict(raw)
    for key in POINT_IDS:
        row[key] = annotations.iid(row.get(key), key)
    for key in OPTIONAL_PROCESS_IDS:
        value = row[key]
        row[key] = None if value in (None, '') else annotations.iid(value, key)
    for key in ('ChangeCount', 'ParentChangeCount', 'MainlineChangeCount', 'ReversalStatus', 'DataType'):
        row[key] = inspection.wire_int(row.get(key), key)
    if (row['DataCollectionDefId'] != row['ParentDefinitionId']
            or len({row['HistoryId'], row['ParentHistoryId'], row['MainlineHistoryId']}) != 1
            or len({row['TxnId'], row['ParentTxnId'], row['MainlineTxnId']}) != 1):
        raise ValueError('原始采集点的定义、历史父链或事务ID不一致。')
    if row['ReversalStatus'] != 1 or row['DataType'] not in (1, 2, 3, 9):
        raise ValueError('历史计算只接受未冲销的直接数值采集点。')
    q.text(row['DataName'], '原始点名')
    timestamp(row['TxnDateGMT'])
    spc.number(row['DataValue'])
    return row


async def read_points(keys):
    clause = ','.join(q.literal(key) for key in keys)
    rows = await q.sql_rows('SELECT TOP 101 '+POINT_FIELDS+HISTORY_JOIN
                            +' WHERE d.DataPointHistoryDetailId IN ('+clause+')', MAX_POINTS + 1)
    points = {}
    for raw in rows:
        row = point_record(raw)
        key = row['DataPointHistoryDetailId']
        if key in points:
            raise ValueError('原始历史点关联重复。')
        points[key] = row
    if set(points) != set(keys):
        raise ValueError('原始采集历史点缺失或返回了额外点。')
    return points


def interval_where(scope, first_mainline, last_mainline):
    clauses = [f'd.{key}={q.literal(scope[key])}' for key in ('DataCollectionDefId', 'DataPointId')]
    clauses.extend(f'm.{key}={q.literal(scope[key])}' for key in PROCESS_IDS)
    for key in OPTIONAL_PROCESS_IDS:
        value = scope[key]
        clauses.append(f"NULLIF(m.{key},'') IS NULL" if value is None else f'm.{key}={q.literal(value)}')
    # Compare the stored timestamps using their native SQL type. Serializing
    # a rounded boundary to a different datetime type can skip an endpoint.
    for operator, key in [('>=', first_mainline), ('<=', last_mainline)]:
        clauses.append(f'm.TxnDateGMT{operator}(SELECT TxnDateGMT FROM HistoryMainline '
                       f'WHERE HistoryMainlineId={q.literal(annotations.iid(key))})')
    return ' AND '.join(clauses)


async def read_window(keys, groups):
    points = await read_points(keys)
    scope = {key: points[keys[0]][key] for key in SCOPE_FIELDS}
    if any(any(row[key] != value for key, value in scope.items()) for row in points.values()):
        raise ValueError('历史点的采集定义、特性、单位或生产过程不一致。')
    times = [[timestamp(points[key]['TxnDateGMT']) for key in group] for group in groups]
    if any(series != sorted(series) for series in times) or any(max(left) >= min(right) for left,right in zip(times,times[1:])):
        raise ValueError('各组须按原始GMT时间排列，相邻组的时间范围不能重叠或并列。')
    where = interval_where(scope, points[groups[0][0]]['HistoryMainlineId'],
                           points[groups[-1][-1]]['HistoryMainlineId'])
    interval = await q.sql_rows('SELECT TOP 101 d.DataPointHistoryDetailId'+HISTORY_JOIN
                                +' WHERE '+where+' ORDER BY m.TxnDateGMT,d.DataPointHistoryDetailId', MAX_POINTS+1)
    interval_ids = [annotations.iid(row.get('DataPointHistoryDetailId')) for row in interval]
    if len(interval_ids) != len(keys) or set(interval_ids) != set(keys):
        raise ValueError('同一过程的原始时间区间存在遗漏、重复或超限，不能拼接为连续SPC窗口。')
    return points, scope


async def active_annotations(keys):
    clause = ','.join(q.literal(key) for key in keys)
    rows = await q.sql_rows(
        'SELECT TOP 101 a.AddSPCAnnotationHistoryId,a.DataPointId,a.HistoryId,a.TxnId,a.Exclude,'
        'm.HistoryMainlineId,m.HistoryId AS MainlineHistoryId,m.TxnId AS MainlineTxnId,m.ReversalStatus,m.TxnType '
        'FROM AddSPCAnnotationHistory a LEFT JOIN HistoryMainline m ON m.HistoryMainlineId=a.HistoryMainlineId '
        'WHERE a.DataPointId IN ('+clause+') AND (m.ReversalStatus=1 OR m.HistoryMainlineId IS NULL) '
        'ORDER BY a.AddSPCAnnotationHistoryId', MAX_POINTS+1)
    if len(rows) > MAX_POINTS:
        raise ValueError('原始点有效注释超过100条，不能验证完整排除状态。')
    seen = set()
    for row in rows:
        key = annotations.iid(row.get('DataPointId'))
        identity = annotations.iid(row.get('AddSPCAnnotationHistoryId'))
        annotations.iid(row.get('HistoryMainlineId'))
        if (key not in keys or identity in seen or annotations.iid(row.get('HistoryId')) != key
                or annotations.iid(row.get('MainlineHistoryId')) != key
                or annotations.iid(row.get('TxnId')) != annotations.iid(row.get('MainlineTxnId'))
                or inspection.wire_int(row.get('TxnType'), '注释事务类型') != 8393
                or inspection.wire_int(row.get('ReversalStatus'), '冲销状态') != 1):
            raise ValueError('原始点注释历史关联不完整。')
        if inspection.wire_bool(row.get('Exclude'), '原始点排除标志'):
            raise ValueError('区间包含具有有效排除记录的原始点，请选择独立连续区间。')
        seen.add(identity)
    return rows


async def analyze_history(chart_type, groups, rules, baseline_groups, controls):
    evaluation = point_groups(groups, chart_type, 1)
    baseline = point_groups(baseline_groups, chart_type, 2) if baseline_groups is not None else []
    if (baseline_groups is None) == (controls is None):
        raise ValueError('须且只能提供独立baseline_groups或有版本的controls。')
    if baseline and len(baseline[0]) != len(evaluation[0]):
        raise ValueError('基线和评估段的历史子组大小必须相同。')
    groups = baseline + evaluation
    keys = [key for group in groups for key in group]
    if len(keys) > MAX_POINTS or len(set(keys)) != len(keys):
        raise ValueError('基线及评估段合计最多100个不同的原始历史点。')
    await annotations.schema_check(SCHEMA)
    points, scope = await read_window(keys, groups)
    existing_annotations = await active_annotations(keys)
    ctx = ReadContext(2)
    unit = await ctx.resolve('UOMs', {'InstanceID': scope['UOMId']}, False)
    unit_name = q.text(unit.get('Name'), '单位名称')
    if annotations.iid(unit.get('InstanceID')) != scope['UOMId']:
        raise ValueError('单位身份与历史点不一致。')
    if await read_window(keys, groups) != (points, scope) or await active_annotations(keys) != existing_annotations:
        raise ValueError('读取期间原始历史点、连续区间或排除状态已变化。')
    if await ctx.get('UOMs', scope['UOMId']) != unit:
        raise ValueError('读取期间单位定义发生变化。')

    evidence = {'scope': scope, 'groups': groups, 'baseline_group_count': len(baseline),
                'points': [{field: points[key][field] for field in EVIDENCE_FIELDS} for key in keys],
                'active_annotation_ids': [row['AddSPCAnnotationHistoryId'] for row in existing_annotations]}
    source_digest = digest(evidence)
    observations = [{'observation_id': group[0], 'sequence': index,
                     'values': [points[key]['DataValue'] for key in group]}
                    for index, group in enumerate(groups, 1)]
    result = spc.analyze(chart_type, observations[len(baseline):],
        dict(characteristic=scope['DataName'], unit=unit_name, process_scope=digest(scope),
             data_reference=source_digest, order_reference='MES HistoryMainline.TxnDateGMT'),
        rules, observations[:len(baseline)] if baseline else None, source_digest if baseline else None, controls)
    result.update(complete=True, data_verified_against_mes=True, source_kind='mes_persisted_direct_collection',
                  source_history=evidence, source_history_digest=source_digest,
                  source_window_verified=True, subgroup_assignment_verified=False)
    result['limitations'][0] = '已核对MES原始采集身份、同一历史范围和时间连续性。子组合理性、过程稳定性与统计假设仍需核实。'
    return result


@mcp.tool
async def analyze_spc_history(
    chart_type: spc.Chart,
    groups: HistoryGroups,
    rules: Annotated[list[spc.Rule], Field(min_length=1, max_length=4)],
    baseline_groups: HistoryGroups | None = None,
    controls: spc.SPCFixedControls | None = None,
) -> str:
    """从MES原始DataPointHistoryDetail持久ID读回数值并本地判异。

    groups按原始GMT时间提供，i_mr每组1点，xbar_r每组固定2..10点。总计最多100点。
    基线与评估段同定义版本、点、单位、产品、工序、Spec、步骤、设备及工厂，完整覆盖对应时间区间。
    明确baseline_groups或有版本controls二选一。保留源历史及摘要。输出为本地WECO，不认证原生规则/控制限，不触发登记或放行。
    """
    try:
        normalized = spc.SPCFixedControls.model_validate(controls).model_dump() if controls is not None else None
        return q.encode(await analyze_history(chart_type, groups, rules, baseline_groups, normalized))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return q.encode({'complete': False, 'error': str(exc)})


async def model_presence(entity):
    try:
        data = await ReadContext(1).get(entity, params={'$select': 'InstanceID', '$top': 1})
        rows = data['value']
        if not isinstance(rows, list) or len(rows) > 1 or (not rows and q._has_next_link(data)):
            raise ValueError('模型集合回显不完整。')
        if rows:
            annotations.iid(q.identifier(rows[0]))
        return {'state': 'present' if rows else 'empty'}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'state': 'unavailable'}


async def persisted_presence(table, identity):
    try:
        rows = await q.sql_rows(f'SELECT TOP 1 {identity} FROM {table}', 1)
        if rows:
            annotations.iid(rows[0][identity])
        return {'state': 'present' if rows else 'empty'}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'state': 'unavailable'}


@mcp.tool
async def get_spc_native_readiness(spc_chart_name: str | None = None, spc_chart_instance_id: str | None = None) -> str:
    """只读检查当前部署可见SPC图、图类型、规则、查询、连接和持久采集点是否存在。

    未知与空表分开。无需连接外部SPC引擎，不读取连接凭据，不执行SPCTxn或InlineSPCTxn。
    提供明确图名称或持久ID时，另核对该图的图类型、查询、规则、连接、参数清单及副作用配置。
    图配置存在也不构成控制限、编译规则、引擎结果或自动处置授权。
    """
    entities = ('SPCChartDefs', 'SPCChartTypes', 'SPCRules', 'SPCQueries')
    models = dict(zip(entities, await asyncio.gather(*(model_presence(entity) for entity in entities))))
    checks = models | {
        'DataPointHistoryDetail': await persisted_presence('DataPointHistoryDetail', 'DataPointHistoryDetailId'),
        'SPCConnection': await persisted_presence('A_SPCConnection', 'SPCConnectionId'),
    }
    result = {'checks': checks, 'complete': all(item['state'] != 'unavailable' for item in checks.values()),
        'empty': [key for key,item in checks.items() if item['state'] == 'empty'],
        'unavailable': [key for key,item in checks.items() if item['state'] == 'unavailable'],
        'compiled_rules_verified': False, 'native_control_limits_verified': False,
        'native_engine_result_verified': False, 'ready_for_native_execution': False, 'release_authorized': False}
    if spc_chart_name is not None or spc_chart_instance_id is not None:
        try:
            result['chart_configuration'] = await chart_readiness(spc_chart_name, spc_chart_instance_id)
        except (ValueError, TypeError, KeyError, AttributeError):
            result['chart_configuration'] = {'state': 'unavailable', 'complete': False,
                                              'blockers': ['chart_identity_or_configuration_unverified']}
        result['complete'] &= result['chart_configuration']['complete']
    return q.encode(result)
