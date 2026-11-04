"""Original-member cleanup and route evidence for one native lot rework."""
import copy

from tools import quality as q, quality_inspection as inspection
from tools.quality_spc_annotations import schema_check


def member_ids(members):
    ids = [row['ContainerId'] for row in members]
    if not 1 <= len(ids) <= 20 or any(not q.valid_id(key) for key in ids) or len(set(ids)) != len(ids):
        raise ValueError('原批返工成员集合不完整。')
    return sorted(ids)


async def sampling_tasks(members):
    ids = member_ids(members)
    await schema_check({'ContainerSampleData': inspection.TASK_COLUMNS})
    clause = ','.join(q.literal(key) for key in ids)
    rows = await q.sql_rows('SELECT TOP 201 ' + inspection.TASK_COLUMNS
        + ' FROM ContainerSampleData WHERE ContainerId IN (' + clause + ') ORDER BY ContainerSampleDataId', 201)
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError('成员采样任务超过200条或未完整读取。')
    clean = []
    seen = set()
    fields = inspection.TASK_COLUMNS.split(',')
    for raw in rows:
        if not isinstance(raw, dict) or not set(fields) <= raw.keys():
            raise ValueError('成员采样任务字段缺失。')
        row = {field: copy.deepcopy(raw[field]) for field in fields}
        if row['ContainerId'] not in ids:
            raise ValueError('采样任务不属于原批成员。')
        inspection.check_tasks([row], row['ContainerId'])
        key = row['ContainerSampleDataId']
        if key in seen:
            raise ValueError('成员采样任务ID重复。')
        seen.add(key)
        clean.append(row)
    return sorted(clean, key=lambda row: row['ContainerSampleDataId'])


def cleanup(before, current):
    now = {row['ContainerSampleDataId']: row for row in current}
    if before is None:
        return dict(baseline_available=False, original_task_count=None,
                    current_tasks=current, cleanup_causal_chain_verified=False)
    original = {row['ContainerSampleDataId']: row for row in before}
    return dict(baseline_available=True, original_task_count=len(original),
        missing_original_task_ids=sorted(original.keys() - now.keys()),
        retained_original_task_ids=sorted(original.keys() & now.keys()),
        changed_original_task_ids=sorted(key for key in original.keys() & now.keys() if original[key] != now[key]),
        new_tasks=[now[key] for key in sorted(now.keys() - original.keys())],
        all_original_tasks_absent=bool(original) and not original.keys() & now.keys(),
        cleanup_causal_chain_verified=False)


async def routes(snapshot, current_members):
    original_ids = member_ids(snapshot['before']['members'])
    records = {row['ContainerId']: row for row in current_members}
    results = []
    for route in snapshot['preview']['member_routes']:
        cid = route['container']['instanceId']
        if cid not in original_ids:
            raise ValueError('计划路由不属于原成员。')
        current = records.get(cid)
        if current is None:
            results.append(dict(container_id=cid, available=False, reason='original_member_record_missing'))
            continue
        try:
            ref, row, status = await inspection.container_snapshot(current['ContainerName'], cid)
        except (ValueError, TypeError, KeyError, AttributeError):
            results.append(dict(container_id=cid, available=False, reason='original_member_route_unavailable'))
            continue
        if ref['instanceId'] != cid or row['ChangeCount'] != inspection.wire_int(current['ChangeCount'], '成员变更计数'):
            raise ValueError('读取原成员当前路线期间记录变化。')
        in_rework = inspection.wire_bool(status.get('InRework'), '当前返工标志')
        expected = dict(workflow_id=route['workflow']['instanceId'], step_id=route['to_step']['instanceId'],
                        spec_id=route['target_spec']['instanceId'])
        observed = dict(workflow_id=q.identifier(status.get('Workflow')), step_id=q.identifier(status.get('Step')),
                        spec_id=q.identifier(status.get('Spec')))
        if any(not q.valid_id(value) for value in observed.values()):
            raise ValueError('当前路线持久ID不完整。')
        results.append(dict(container_id=cid, available=True, expected=expected, observed=observed,
            in_rework=in_rework, expected_route_currently_observed=in_rework and expected == observed,
            original_quantity=route['container_sql']['Qty'], current_quantity=row['Qty'],
            quantity_currently_unchanged=inspection.numeric(str(route['container_sql']['Qty']), '原数量')
                == inspection.numeric(str(row['Qty']), '当前数量'), causal_chain_verified=False))
    if sorted(row['container_id'] for row in results) != original_ids:
        raise ValueError('原成员路由证据不完整或重复。')
    return results
