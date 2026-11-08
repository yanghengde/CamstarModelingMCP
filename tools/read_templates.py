"""Declarative business recipes over the same generic read executor."""
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo
from core import read_plan


def ref(step, column=None):
    return {'$ref': step + '#/data', **({'column': column} if column else {})}


def order_dates_plan(names):
    return {'version': 1, 'steps': [
        {'id': 'orders', 'op': 'dataset', 'dataset': 'orders',
         'fields': ['MfgOrderId', 'MfgOrderName', 'PlannedStartDateGMT', 'ProductionStartDateGMT', 'ProductionCompleteDateGMT'],
         'filters': [{'field': 'MfgOrderName', 'operator': 'in', 'value': names}], 'limit': 20},
        {'id': 'containers', 'op': 'dataset', 'dataset': 'containers',
         'fields': ['ContainerId', 'ContainerName', 'MfgOrderId', 'Status', 'FactoryStartDateGMT'],
         'filters': [{'field': 'MfgOrderId', 'operator': 'in', 'value': ref('orders', 'MfgOrderId')}],
         'limit': 100, 'max_pages': 5},
        {'id': 'first_start', 'op': 'group', 'source': ref('containers'), 'keys': ['MfgOrderId'],
         'metrics': {'container_count': {'op': 'count'},
                     'first_container_start': {'op': 'min', 'field': 'FactoryStartDateGMT', 'kind': 'utc_timestamp'}}},
        {'id': 'dates', 'op': 'join', 'left': ref('orders'), 'right': ref('first_start'),
         'left_key': 'MfgOrderId', 'right_key': 'MfgOrderId', 'right_prefix': 'actual_'}
    ], 'output': ref('dates')}


def display_gmt(value, zone):
    if value in (None, ''):
        return None
    from tools.collection_validation import timestamp_ticks
    original = value if re.search(r'(Z|[+-]\d\d:\d\d)$', value) else value + 'Z'
    timestamp_ticks(original)
    parsed = datetime.fromisoformat(original.replace('Z', '+00:00'))
    if parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError('GMT字段返回非UTC偏移，未自动改解释。')
    fraction = re.search(r'\.(\d{1,7})', value)
    # DateTimeOffset seventh digit is retained; Python datetime only stores six.
    local = parsed.astimezone(zone)
    offset = local.strftime('%z')
    return local.strftime('%Y-%m-%dT%H:%M:%S') + ('.' + fraction[1] if fraction else '') + offset[:3] + ':' + offset[3:]


async def order_dates(parameters):
    if not isinstance(parameters, dict) or set(parameters) - {'order_names', 'timezone'}:
        raise ValueError('order_dates只接受order_names及timezone。')
    names = parameters.get('order_names')
    if (not isinstance(names, list) or not 1 <= len(names) <= 20
            or any(not isinstance(n, str) or not n.strip() or len(n) > 100 or '\x00' in n for n in names)
            or len(set(names)) != len(names)):
        raise ValueError('order_names须为1..20个唯一明确工单名，每个最多100字符。')
    zone_name = parameters.get('timezone', 'Asia/Shanghai')
    if not isinstance(zone_name, str) or len(zone_name) > 100:
        raise ValueError('timezone须为明确IANA时区。')
    zone = ZoneInfo(zone_name)
    result = await read_plan.run(order_dates_plan(names))
    # Do not label missing containers as zero unless the complete filtered read,
    # group and left join all succeeded.
    if not result['complete'] or not isinstance(result['output'], list):
        return {'complete': False, 'rows': [], 'mes_write_performed': False, 'execution': result,
                'note': '查询失败/分页未完整时不推断未开工、零容器或无日期。'}
    orders = result['output']
    by_name = {}
    for row in orders:
        if row['MfgOrderName'] in by_name or row['MfgOrderName'] not in names:
            raise ValueError('工单名重复或与原请求不符。')
        by_name[row['MfgOrderName']] = row
    rows = []
    for name in names:
        if name not in by_name:
            rows.append({'order_name': name, 'found': False, 'status': 'not_found', 'container_count': None})
            continue
        row = by_name[name]; matched = row['actual_matched']
        count = row['actual_container_count'] if matched else 0
        start = row.get('actual_first_container_start')
        missing = row.get('actual_first_container_start_missing_count', 0)
        entry = {'order_name': name, 'order_id': row['MfgOrderId'], 'found': True,
                 'container_count': count, 'containers_with_missing_start': missing,
                 'actual_start_basis': 'earliest FactoryStartDateGMT among all currently order-linked containers; all statuses',
                 'actual_start_state': ('no_current_linked_containers' if not matched else
                     'all_container_dates_missing' if not start else
                     'earliest_recorded_date_with_missing_values' if missing else 'earliest_recorded_date'),
                 'timezone': zone_name}
        for output, field in [('planned_start', 'PlannedStartDateGMT'),
                              ('native_production_start', 'ProductionStartDateGMT'),
                              ('native_production_complete', 'ProductionCompleteDateGMT')]:
            value = row.get(field)
            entry[output] = {'source': 'MfgOrder.' + field, 'utc_raw': value,
                             'display': display_gmt(value, zone)}
        entry['actual_first_container_start'] = {'source': 'Container.FactoryStartDateGMT',
            'utc_raw': start, 'display': display_gmt(start, zone)}
        rows.append(entry)
    return {'complete': True, 'all_requested_orders_found': all(r['found'] for r in rows),
            'rows': rows, 'metrics': result['metrics'], 'mes_write_performed': False,
            'source': 'order_dates v1 via shared deterministic read plan', 'atomic_snapshot': False,
            'warnings': ['当前关联范围，历史改绑未回溯；无当前容器不等于历史未开工。',
                         '原生工单日期与实际首箱日期分别保留；日期缺失不解释为未开工。'],
            'evidence': {'steps': {key: {k: value[k] for k in ('status', 'complete', 'source', 'page', 'elapsed_ms') if k in value}
                                  for key, value in result['steps'].items()}}}
