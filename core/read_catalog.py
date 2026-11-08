"""Shared, environment-scoped metadata for bounded read orchestration."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from config import CAMSTAR_BASE_URL, CAMSTAR_QUERY_BASE_URL, CAMSTAR_USERNAME
from core.http_client import request

CACHE_DB = Path(__file__).resolve().parents[1] / 'data' / 'read_metadata.sqlite3'
CACHE_TTL = 21600
_locks = {}

# Reviewed read operations only. A catalog policy of "read" by itself does not
# authorize wrapping previews, plans, arbitrary Inquiry events or recursive plans.
READ_TOOLS = frozenset({
    'get_container_details', 'get_container_history', 'get_container_throughput_history',
    'get_mfgorder_details', 'get_product_details', 'get_spec_details', 'get_operation_details',
    'get_mfgorder_progress', 'get_mfgorder_production_summary', 'get_production_worktime',
    'get_production_reports', 'get_product_process', 'get_workflow_structure',
    'list_workflow_steps', 'get_workflow_step', 'get_resource_details', 'list_resources',
})

DATASETS = {
    'orders': {'table': 'MfgOrder', 'key': 'MfgOrderId', 'fields': (
        'MfgOrderId', 'MfgOrderName', 'Qty', 'QtyStarted', 'UOMId', 'ProductId', 'OrderStatusId',
        'PlannedStartDate', 'PlannedStartDateGMT', 'PlannedCompletionDateGMT',
        'ProductionStartDateGMT', 'ProductionCompleteDateGMT', 'PostProductionCompleteDateGMT')},
    'containers': {'table': 'Container', 'key': 'ContainerId', 'fields': (
        'ContainerId', 'ContainerName', 'MfgOrderId', 'ProductId', 'Status', 'Qty', 'Qty2', 'UOMId',
        'UOM2Id', 'CurrentStatusId', 'ParentContainerId', 'ChildCount', 'ProductionComplete',
        'FactoryStartDate', 'FactoryStartDateGMT', 'OriginalStartDate', 'CurrentThruputQty', 'ThruputAdjustmentQty')},
    'current_status': {'table': 'CurrentStatus', 'key': 'CurrentStatusId', 'fields': (
        'CurrentStatusId', 'WorkflowStepId', 'SpecId', 'CurrentStepPass', 'InProcess', 'InRework',
        'CarrierId', 'MoveInDateGMT', 'LastMoveDateGMT')},
    'history': {'table': 'HistoryMainline', 'key': 'HistoryMainlineId', 'fields': (
        'HistoryMainlineId', 'ContainerId', 'ContainerName', 'TxnId', 'TxnDateGMT', 'TxnTypeName',
        'TxnServiceName', 'TxnParentTypeName', 'WorkflowStepId', 'StepPass', 'ReversalStatus',
        'StepEntryTxnId', 'MoveInDateGMT', 'Qty', 'Qty2', 'Comments')},
}
ENTITIES = frozenset({'Products', 'Specs', 'Operations', 'Workflows', 'MfgOrders', 'Resources',
                     'ContainerLevels', 'UOMs', 'BOMs'})

SEMANTICS = [
    {'concept': '实际首箱开工', 'source': 'containers.FactoryStartDateGMT',
     'rule': '按当前MfgOrderId关联的全部状态容器取最早GMT；不是工单ProductionStartDateGMT，也不是创建日期。',
     'limits': '当前关联口径，历史改绑/拆合容器须另查历史。已关闭Status=2不能默认排除。'},
    {'concept': '计划开工', 'source': 'orders.PlannedStartDateGMT', 'rule': '空值与实际未开工不是同一结论。'},
    {'concept': '生产完成', 'source': 'orders.ProductionCompleteDateGMT',
     'rule': '末节点、Closed、过程任务状态均不能单独代替工单原生生产完成证据。'},
    {'concept': '空列表', 'rule': 'Modeling导航未展开、分页或读取失败不能解释为无关联对象；保留unknown。'},
    {'concept': '时区', 'rule': '仅明确GMT字段按UTC解释；本地无偏移日期保留原值，不猜MES服务器时区。'},
]


def environment():
    return hashlib.sha256(json.dumps([CAMSTAR_BASE_URL.rstrip('/'),
        CAMSTAR_QUERY_BASE_URL.rstrip('/'), CAMSTAR_USERNAME]).encode()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def connect():
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(CACHE_DB, timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS metadata(environment TEXT,key TEXT,payload TEXT,digest TEXT,'
               'fetched REAL,PRIMARY KEY(environment,key))')
    return db


async def cached(key, loader, refresh=False):
    """Cache only contracts, never production rows or authentication."""
    env = environment()
    lock = _locks.setdefault((env, key), asyncio.Lock())
    async with lock:
        with connect() as db:
            row = db.execute('SELECT payload,digest,fetched FROM metadata WHERE environment=? AND key=?',
                             (env, key)).fetchone()
        now = time.time()
        if row and not refresh and 0 <= now - row[2] < CACHE_TTL:
            payload = json.loads(row[0])
            if digest(payload) != row[1]:
                raise ValueError('元数据缓存摘要不符；请明确刷新。')
            return {'content': payload, 'cached': True, 'fetched_at_unix': row[2],
                    'schema_digest': row[1], 'environment_id': env, 'ttl_seconds': CACHE_TTL}
        payload = await loader()  # failure never falls back to expired contracts
        signature = digest(payload)
        with connect() as db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?,?,?,?)',
                       (env, key, json.dumps(payload, ensure_ascii=False), signature, time.time()))
        return {'content': payload, 'cached': False, 'fetched_at_unix': time.time(),
                'schema_digest': signature, 'environment_id': env, 'ttl_seconds': CACHE_TTL}


async def sql_rows(sql):
    raw = await request('POST', '/api/AdHoc', base_url=CAMSTAR_QUERY_BASE_URL,
        body={'queryText': sql}, response_formatter=lambda d: json.dumps(d, ensure_ascii=False),
        retry_read_only=True)
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('只读查询失败：' + str(raw)[:2000]) from None
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError('只读查询未返回完整记录数组。')
    return rows


async def dataset_metadata(name, refresh=False):
    if name not in DATASETS:
        raise ValueError('不支持的数据集；先查get_camstar_read_metadata。')
    definition = DATASETS[name]
    table = definition['table']  # literals exclusively from reviewed definitions
    async def load():
        rows = await sql_rows("SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE,"
            "CONVERT(varchar(10),DATETIME_PRECISION) AS DATETIME_PRECISION "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=N'" + table +
            "' ORDER BY TABLE_SCHEMA,ORDINAL_POSITION")
        if not rows or any(r.get('TABLE_NAME') != table for r in rows):
            raise ValueError('现场表元数据为空或不匹配；不能猜测字段。')
        schemas = {r.get('TABLE_SCHEMA') for r in rows}
        if len(schemas) != 1 or not all(isinstance(s, str) and s.replace('_', '').isalnum() for s in schemas):
            raise ValueError('表存在多schema或schema身份无效，未自行选择。')
        columns = {r['COLUMN_NAME']: {k: r.get(k) for k in ('DATA_TYPE', 'DATETIME_PRECISION')}
                   for r in rows if r.get('COLUMN_NAME') in definition['fields']}
        if definition['key'] not in columns:
            raise ValueError('现场数据集缺少持久主键。')
        return {'dataset': name, 'table': table, 'schema': next(iter(schemas)), 'key': definition['key'],
                'columns': columns, 'source': 'live INFORMATION_SCHEMA.COLUMNS',
                'unavailable_reviewed_fields': sorted(set(definition['fields']) - columns.keys())}
    return await cached('dataset:' + name + ':' + digest(definition), load, refresh)


async def entity_metadata(name, refresh=False):
    if name not in ENTITIES:
        raise ValueError('不支持的Modeling实体。')
    async def load():
        raw = await request('GET', '/swagger/' + name + '/swagger.json',
            response_formatter=lambda d: json.dumps(d, ensure_ascii=False))
        try:
            doc = json.loads(raw)
            operation = doc['paths']['/api/' + name]['get']
            schemas = doc['components']['schemas']
        except (ValueError, TypeError, KeyError):
            raise ValueError('实体现场Swagger读取失败；未使用过期副本。') from None
        # Include field contracts without auth schemes, examples or defaults.
        entities = {n: {'required': s.get('required', []),
            'fields': {k: {a: v[a] for a in ('type', 'format', '$ref', 'items', 'readOnly', 'nullable', 'description') if a in v}
                       for k, v in s.get('properties', {}).items()}}
            for n, s in schemas.items() if n.endswith('Entity')}
        return {'entity': name, 'source': 'live Modeling Swagger', 'schemas': entities,
                'collection_get_available': bool(operation), 'document_digest': digest(doc)}
    return await cached('entity:' + name, load, refresh)
