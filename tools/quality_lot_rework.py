"""Phase-one native lot rework on fixed same-workflow default ReworkPaths.

ExecuteSamplingLotToRework does not map Spec or explicit destination fields.
Per-container Rework calls below are nonexecuting dependency/route probes only.
"""
import copy
import json

from tools import mcp, quality as q, quality_inspection as i, quality_lot_execution as e
from tools import quality_plans as p, quality_definitions as d
from tools.process_context import ReadContext
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

ASSETS = {'Workflows': ('Workflow', 'WorkflowId', True), 'Specs': ('Spec', 'SpecId', True),
          'Operations': ('Operation', 'OperationId', False), 'ReworkReasons': ('ReworkReason', 'ReworkReasonId', False),
          'ReworkReasonGroups': ('ReworkReasonGroup', 'ReworkReasonGroupId', False)}


def stable_model(value):
    """Changes-CDO reference IDs are allocated on reads, not runtime identities."""
    if isinstance(value, list): return [stable_model(v) for v in value]
    if not isinstance(value, dict): return value
    changes = str(value.get('CDOTypeName', value.get('@odata.type', ''))).endswith('Changes')
    return {k: stable_model(v) for k, v in value.items()
            if k != '@odata.context' and not (changes and k in ('InstanceId', 'InstanceID'))}


async def workflow_steps(ctx, root):
    """Join leaf fields and nested lists by scoped name; SQL pins runtime IDs."""
    wid = root['InstanceID']
    sql = ("SELECT TOP 201 WorkflowStepId,WorkflowStepName,WorkflowId,StepType,SpecId,SpecBaseId,ChangeCount "
           f"FROM WorkflowStep WHERE WorkflowId='{wid}' ORDER BY WorkflowStepId")
    path_sql = ('SELECT TOP 401 p.PathId,p.PathName,p.CDOTypeId,p.FromStepId,p.ToStepId,p.ReEntryStepId,'
                'p.EndReworkStepId,p.ReturnToStepId,p.ChangeCount FROM Path p INNER JOIN WorkflowStep s '
                f"ON s.WorkflowStepId=p.FromStepId WHERE s.WorkflowId='{wid}' ORDER BY p.PathId")
    rows = await q.sql_rows(sql, 201); paths = await q.sql_rows(path_sql, 401)
    if not 1 <= len(rows) <= 200 or len(paths) > 400:
        raise ValueError('返工流程步骤/路径为空或超过有界读取上限。')
    if (any(not q.valid_id(x.get('WorkflowStepId')) or x.get('WorkflowId') != wid for x in rows)
            or len({x['WorkflowStepId'] for x in rows}) != len(rows)
            or len({x.get('WorkflowStepName') for x in rows}) != len(rows)):
        raise ValueError('流程持久步骤身份重复或所属不符。')
    leaf = await ctx.get('Workflows', wid, {'$expand': 'Steps'})
    advanced = await ctx.get('Workflows', wid, {'$expand': 'Steps($expand=ReworkPaths,ReworkPathSelectors)'})
    names = {x['WorkflowStepName'] for x in rows}; by_id = {x['WorkflowStepId']: x for x in rows}
    for data in (leaf, advanced):
        values = data.get('Steps')
        if (data.get('InstanceID') != wid or data.get('Name') != root['Name'] or data.get('Revision') != root['Revision']
                or q._has_next_link(data) or not isinstance(values, list) or len(values) != len(rows)
                or {x.get('Name') for x in values} != names):
            raise ValueError('流程分层读取的步骤集合不完整或身份变化。')
    leaves = {x['Name']: x for x in leaf['Steps']}; extras = {x['Name']: x for x in advanced['Steps']}
    result = []
    for row in rows:
        item = leaves[row['WorkflowStepName']]; extra = extras[row['WorkflowStepName']]
        if i.wire_int(item.get('StepType'), '步骤类型') != i.wire_int(row.get('StepType'), '持久步骤类型'):
            raise ValueError('建模步骤与持久步骤类型不同。')
        reworks = extra.get('ReworkPaths'); selectors = extra.get('ReworkPathSelectors')
        if not isinstance(reworks, list) or len(reworks) > 50 or not isinstance(selectors, list) or len(selectors) > 50:
            raise ValueError('流程返工路径/选择器未完整展开。')
        canonical = stable_model(item) | {'Name': row['WorkflowStepName'], 'InstanceID': row['WorkflowStepId'],
                                         'Parent': {'Name': root['Name'], 'InstanceId': wid}, 'runtime_step': row}
        selected = []
        for path in reworks:
            # OData omits @odata.type for the declared navigation element type.
            # Bind that omitted annotation to the actual persisted subtype below.
            if path.get('@odata.type') not in (None, '#modeling.ReworkPathChanges'):
                raise ValueError('路径不是明确ReworkPathChanges。')
            found = [x for x in paths if x['FromStepId'] == row['WorkflowStepId'] and x['PathName'] == path.get('Name')]
            if len(found) != 1 or not q.valid_id(found[0].get('PathId')): raise ValueError('返工路径持久身份不唯一。')
            record = found[0]; route = stable_model(path) | {'InstanceID': record['PathId'], 'runtime_path': record}
            if i.wire_int(record.get('CDOTypeId'), '持久路径类型') != 7112:
                raise ValueError('持久路径不是ReworkPath。')
            if record.get('ReturnToStepId'):
                raise ValueError('第一阶段不处理额外ReturnToStep配置。')
            for field in ('ToStep', 'ReEntryStep', 'EndReworkStep'):
                target_id = record.get(field+'Id'); ref = path.get(field)
                if target_id:
                    target = by_id.get(target_id)
                    if not target or not isinstance(ref, dict) or ref.get('Name') != target['WorkflowStepName']:
                        raise ValueError('返工路径目标/返回/结束不在当前流程或名称不符。')
                    parent = ref.get('Parent')
                    if parent and (parent.get('Name') != root['Name'] or parent.get('Revision') != root['Revision']):
                        raise ValueError('返工路径引用跨Workflow版本。')
                    route[field] = {'Name': target['WorkflowStepName'], 'InstanceId': target_id}
                elif ref:
                    raise ValueError('返工路径配置与持久目标字段不一致。')
            selected.append(route)
        canonical.update(ReworkPaths=selected, ReworkPathSelectors=stable_model(selectors))
        result.append(canonical)
    if await q.sql_rows(sql, 201) != rows or await q.sql_rows(path_sql, 401) != paths:
        raise ValueError('读取流程期间持久步骤/路径发生变化。')
    return result


def blockers(view, disposition='返工'):
    lot = view['lot']; issues = []
    if lot['LotStatus'] != 1 or lot['SamplingPassed'] is not False or type(lot['SamplingCompleted']) is not bool:
        issues.append('只处理Active、MES未通过且完成标志明确的抽样批')
    if lot['SamplingLotResult'] not in (None, 0): issues.append('抽样批已有处置结果')
    if not 1 <= len(view['members']) <= 20: issues.append('第一阶段须有完整1..20个成员，不选择成员子集')
    if not view['tasks']: issues.append('没有可核对的批检验任务')
    failed = False
    for row in view['tasks']:
        if (row['SamplesTested'] != row['CompletedSamples'] or row['SamplesFailed'] != row['Rejects']
                or row['Rejects'] > row['CompletedSamples'] or row['TotalSamplesRequired'] < 1):
            issues.append('任务继承/派生计数不一致或要求数未知')
        if row['Rejects'] > row['AllowedRejects']: failed = True
        if row['InspectAllContQty'] is not False or row['InspectAllQtyOnFailure'] is not False:
            issues.append('第一阶段不处理全检或失败转全检任务')
    if not failed: issues.append('没有实际拒收超过MES任务阈值的证据，不'+disposition+'未开始或已通过批')
    names = set()
    for row in view['members']:
        try:
            name = q.text(row.get('ContainerName'), '成员名称')
            if name in names: raise ValueError('成员名称重复')
            names.add(name)
            if row.get('ProductId') != lot['ProductId']: raise ValueError('成员产品版本不一致')
            if i.wire_int(row.get('Status'), '成员状态') != 1 or i.wire_int(row.get('CurrentHoldCount'), '冻结数') != 0:
                raise ValueError('成员不是Active无冻结状态')
            if row.get('ParentContainerId') or i.wire_int(row.get('ChildCount'), '子容器数') != 0:
                raise ValueError('层级容器未覆盖')
            if i.numeric(str(row.get('Qty')), '成员数量') <= 0: raise ValueError('成员数量非正')
            if row.get('Qty2') not in (None, '') and i.numeric(str(row['Qty2']), '第二数量') != 0:
                raise ValueError('第一阶段不处理非零第二数量')
            i.wire_int(row.get('ChangeCount'), '成员变更计数')
        except ValueError as error: issues.append(str(row.get('ContainerId'))+'：'+str(error))
    return issues


class Dependencies:
    def __init__(self):
        self.ctx = ReadContext(160); self.states = {}; self.bodies = {}; self.references = []

    async def asset(self, entity, ref):
        table, column, revisioned = ASSETS[entity]
        root = await self.ctx.resolve(entity, ref, revisioned); key = root.get('InstanceID')
        if not q.valid_id(key): raise ValueError('返工配置没有有效ID。')
        pair = (entity, key); self.references.append((entity, copy.deepcopy(ref), pair))
        if pair in self.states: return self.bodies[pair]
        if len(self.states) >= 100: raise ValueError('返工配置依赖超过100个。')
        if revisioned and i.wire_int(root.get('Status'), '配置Status') != 1: raise ValueError('流程或工序不是Active固定版本。')
        sql = f"SELECT {column},ChangeCount FROM {table} WHERE {column}='{key}'"
        async def token():
            rows = await q.sql_rows(sql, 1)
            if len(rows) != 1 or rows[0].get(column) != key: raise ValueError('返工配置变更计数身份不符。')
            return i.wire_int(rows[0].get('ChangeCount'), '配置ChangeCount')
        first = await token()
        if entity == 'Workflows':
            root = stable_model(root) | {'Steps': await workflow_steps(self.ctx, root)}
        elif entity == 'ReworkReasonGroups':
            name = root.get('Name')
            root = await self.ctx.get(entity, key, {'$expand': 'ResolvedEntries'})
            if root.get('InstanceID') != key or root.get('Name') != name or q._has_next_link(root) or not isinstance(root.get('ResolvedEntries'), list) or len(root['ResolvedEntries']) > 100:
                raise ValueError('返工配置展开不完整或超限。')
        if await token() != first: raise ValueError('读取返工配置期间发生变化。')
        self.states[pair] = {'entity': entity, 'InstanceID': key, 'Name': q.text(root.get('Name')),
                             'Revision': root.get('Revision'), 'ChangeCount': first, 'content_digest': p.digest(root)}
        self.bodies[pair] = root
        return root

    async def stable(self):
        fresh = Dependencies()
        for entity, ref, pair in self.references:
            body = await fresh.asset(entity, ref)
            if (entity, body['InstanceID']) != pair or fresh.states[pair] != self.states[pair]:
                raise ValueError('准备期间返工流程、工序、原因或组配置发生变化。')


def owned_step(steps, ref, workflow_id):
    key = q.identifier(ref)
    if not q.valid_id(key): raise ValueError('返工步骤引用缺失。')
    matches = [row for row in steps if q.identifier(row) == key]
    if len(matches) != 1 or i.wire_int(matches[0].get('StepType'), '步骤类型') != 1:
        raise ValueError('第一阶段只支持当前Workflow内唯一的Spec步骤。')
    if ref.get('Name') != matches[0].get('Name'): raise ValueError('步骤名称/ID不一致。')
    if ref.get('Parent') and q.identifier(ref['Parent']) != workflow_id: raise ValueError('步骤所属Workflow不符。')
    if matches[0].get('Parent') and q.identifier(matches[0]['Parent']) != workflow_id:
        raise ValueError('展开步骤所属Workflow不符。')
    return matches[0]


async def child_preview(container, reason):
    body = {'container': container, 'reworkReason': reason}
    params = {'execute': 'false', '$select': 'Container,ReworkReason,Path,ToStep,ToWorkflow,ReEntryStep,ReEntryWorkflow,EndReworkStep,EndReworkWorkflow,Qty,Qty2,MoveAllQty,ESigRequirement'}
    raw = await request('POST', '/api/Rework', base_url=CAMSTAR_SHOPFLOOR_BASE_URL, body=body, params=params, response_formatter=raw_json)
    try: outer = json.loads(raw)
    except (ValueError, TypeError): raise ValueError('成员返工非执行预览失败：'+str(raw)[:1000]) from None
    if not isinstance(outer, dict): raise ValueError('成员返工预览形状无效。')
    data = outer.get('data', outer)
    if (not isinstance(data, dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error') or data.get('ErrorCode')
            or q._has_next_link(data) or not p.wire_matches(body, data)):
        raise ValueError('成员返工预览错误、引用不符或不完整。')
    return data


async def route(member, reason, dep, expected_path_name=None):
    ref, container, status = await i.container_snapshot(member['ContainerName'], member['ContainerId'])
    if container['SamplingLotId'] != member['SamplingLotId'] or container['ProductId'] != member['ProductId']:
        raise ValueError('成员容器归属在读取时变化。')
    if container['Status'] != 1 or container['CurrentHoldCount'] != 0 or i.wire_bool(status.get('InRework'), '在返工标志') is not False:
        raise ValueError('成员冻结、非Active、已在返工或返工标志未知。')
    if i.numeric(str(container['Qty']), '状态数量') != i.numeric(str(member['Qty']), '成员数量'):
        raise ValueError('成员数量与当前状态不一致。')
    workflow = await dep.asset('Workflows', status.get('Workflow')); wid = workflow['InstanceID']; steps = workflow['Steps']
    source = owned_step(steps, status.get('Step'), wid)
    paths = source.get('ReworkPaths'); selectors = source.get('ReworkPathSelectors')
    if not isinstance(paths, list) or not paths or selectors != []:
        raise ValueError('批返工须有完整返工路径且无动态选择器。')
    if len(paths) > 1 and expected_path_name is None:
        raise ValueError('当前有多条返工路径，须明确expected_path_name核对原生首条默认路径。')
    path = paths[0]; default_id = q.identifier(path)
    if expected_path_name is not None and path.get('Name') != expected_path_name:
        raise ValueError('预期返工路径与原生首条默认路径不符；整批服务不能覆盖路径选择。')
    if path.get('ToWorkflow') and q.identifier(path['ToWorkflow']) != wid:
        raise ValueError('第一阶段不处理跨Workflow目标。')
    source_spec = await dep.asset('Specs', source.get('Spec'))
    configured_spec = source['runtime_step'].get('SpecId')
    if configured_spec and configured_spec != source_spec['InstanceID']:
        raise ValueError('源步骤持久工序版本与建模引用不符。')
    if source_spec['InstanceID'] != q.identifier(status.get('Spec')): raise ValueError('当前Spec与步骤配置不一致，复杂覆盖未开放。')
    operation = await dep.asset('Operations', source_spec.get('Operation'))
    if operation['InstanceID'] != q.identifier(status.get('Operation')): raise ValueError('当前Operation与工序配置不一致。')
    group = await dep.asset('ReworkReasonGroups', operation.get('ReworkReasons'))
    entries = group['ResolvedEntries']
    if (any(not q.valid_id(q.identifier(r)) for r in entries) or len({q.identifier(r) for r in entries}) != len(entries)
            or sum(q.identifier(r) == reason['instanceId'] and r.get('Name') == reason['name'] for r in entries) != 1):
        raise ValueError('返工原因不属于成员当前Operation完整原因组或组引用重复。')
    data = await child_preview(ref, reason)
    target = owned_step(steps, path.get('ToStep'), wid)
    target_spec = await dep.asset('Specs', target.get('Spec'))
    if target['runtime_step'].get('SpecId') and target['runtime_step']['SpecId'] != target_spec['InstanceID']:
        raise ValueError('目标步骤持久工序版本与建模引用不符。')
    if i.wire_bool(target_spec.get('AllowReworkTo'), '目标允许返工') is not True: raise ValueError('目标工序未明确允许返工。')
    entry_ref = path.get('ReEntryStep') or {'Name': source['Name'], 'InstanceId': q.identifier(source)}
    entry = owned_step(steps, entry_ref, wid)
    expected = {'Path': default_id, 'ToWorkflow': wid, 'ToStep': q.identifier(target),
                'ReEntryWorkflow': wid, 'ReEntryStep': q.identifier(entry),
                'EndReworkStep': q.identifier(path.get('EndReworkStep')), 'EndReworkWorkflow': wid}
    expected_names = {'Path': path['Name'], 'ToWorkflow': workflow['Name'], 'ToStep': target['Name'],
                      'ReEntryWorkflow': workflow['Name'], 'ReEntryStep': entry['Name'],
                      'EndReworkStep': (path.get('EndReworkStep') or {}).get('Name'), 'EndReworkWorkflow': workflow['Name']}
    for field, key in expected.items():
        if len(paths) > 1 and field in ('Path', 'ToWorkflow', 'ToStep', 'ReEntryWorkflow', 'ReEntryStep') and not data.get(field):
            raise ValueError('多路径返工须完整回显默认路径、目标及返回步骤。')
        if data.get(field) and (key is None or q.identifier(data[field]) != key or data[field].get('Name') != expected_names[field]):
            raise ValueError('非执行返工返回的路径字段与已核对配置矛盾：'+field)
    result = {'container': ref, 'container_sql': container, 'status': e.status_snapshot(status) | {'InRework': False},
            'workflow': i.revision_ref(workflow), 'path': {'name': path['Name'], 'instanceId': default_id},
            'source_step': {'name': source['Name'], 'instanceId': q.identifier(source)},
            'to_step': {'name': target['Name'], 'instanceId': q.identifier(target)},
            'reentry_step': {'name': entry['Name'], 'instanceId': q.identifier(entry)},
            'target_spec': i.revision_ref(target_spec),
            'native_default_quantity_fields': {k: data.get(k) for k in ('Qty', 'Qty2', 'MoveAllQty')},
            'signature_requirement': data.get('ESigRequirement'),
            'end_rework': path.get('EndReworkStep'), 'runtime_route_resolution_verified': False,
            'route_basis': 'csi Step_GetReworkPathToUse: no selectors and exactly one configured ReworkPath; deployment overrides unverified'}
    if len(paths) > 1:
        result.update(configured_path_order=[{'name': item['Name'], 'instanceId': q.identifier(item)} for item in paths],
                      expected_path_name=expected_path_name,
                      route_basis='csi Step_GetReworkPathToUse: no selectors; explicitly expected first configured path; deployment overrides unverified')
    return result


async def batch_preview(body, names):
    params = {'execute': 'false', '$select': 'SamplingLot,SamplingLotResult,ReworkReason,Comments,Containers,ScrapReason,Spec'}
    raw = await request('POST', '/api/ExecuteSamplingLot', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                        body=body, params=params, response_formatter=raw_json)
    try: outer = json.loads(raw)
    except (ValueError, TypeError): raise ValueError('整批返工非执行预览失败：'+str(raw)[:1000]) from None
    if not isinstance(outer, dict): raise ValueError('整批返工预览形状无效。')
    data = outer.get('data', outer)
    if (not isinstance(data, dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error') or data.get('ErrorCode')
            or q._has_next_link(data) or not p.wire_matches(body, data)):
        raise ValueError('整批返工输入回显不符或不完整。')
    members = data.get('Containers')
    if not isinstance(members, list) or any(not isinstance(n, str) for n in members) or len(set(members)) != len(members) or set(members) != set(names):
        raise ValueError('原生整批返工成员集合与完整批快照不一致。')
    if data.get('ScrapReason') not in (None, {}): raise ValueError('返工服务出现非预期报废原因。')
    return {'params': params, 'verified_input_echo': body, 'affected_container_names': sorted(names)}


def validate_transport(snap):
    body = snap['body']
    if (snap.get('kind') != 'lot_rework' or snap.get('action') != 'rework'
            or set(body) != {'samplingLot', 'samplingLotResult', 'reworkReason', 'comments'}
            or type(body.get('samplingLotResult')) is not int or body['samplingLotResult'] != 2):
        raise ValueError('批返工仅接受原生结果2、明确原因和原批，不接受数量、Spec、目标覆盖或报废字段。')
    for field in ('samplingLot', 'reworkReason'):
        ref = body[field]
        if not isinstance(ref, dict) or set(ref) != {'name', 'instanceId'} or not q.valid_id(ref.get('instanceId')):
            raise ValueError('批返工引用无效。')
        q.text(ref.get('name'))
    if body['samplingLot'] != {'name': snap['name'], 'instanceId': snap['target_id']}:
        raise ValueError('批返工目标与原计划不符。')
    if body['reworkReason'] != snap['reason']: raise ValueError('批返工原因与原配置不符。')
    expected_name = snap.get('arguments', {}).get('expected_path_name')
    if expected_name is not None:
        q.text(expected_name, '预期默认返工路径名称')
        routes = snap['preview']['member_routes']
        if len(routes) != len(snap['before']['members']) or any(route['path']['name'] != expected_name for route in routes):
            raise ValueError('批返工成员预期默认路径不一致。')
    comment = body.get('comments')
    marker = snap.get('marker')
    if not isinstance(comment, str) or not isinstance(marker, str) or not (comment == marker or comment.startswith(marker+' ')):
        raise ValueError('批返工评论缺少原关联标记。')


async def build_rework(args, marker):
    from tools.quality_lot_rework_evidence import sampling_tasks
    comment = i.inspection_comment(args.get('comments', ''))
    expected_path_name = args.get('expected_path_name')
    if expected_path_name is not None:
        expected_path_name = q.text(expected_path_name, '预期默认返工路径名称')
    view = await e.lot_view(args['lot_name'], args.get('lot_instance_id')); issues = blockers(view)
    if issues: raise ValueError('不能准备批返工：'+'；'.join(issues[:12]))
    member_tasks = await sampling_tasks(view['members'])
    dep = Dependencies(); root = await dep.asset('ReworkReasons', {'Name': q.text(args['rework_reason_name'])})
    reason = {'name': root['Name'], 'instanceId': root['InstanceID']}
    routes = [await route(member, reason, dep, expected_path_name) for member in view['members']]
    lot = view['lot']
    body = {'samplingLot': {'name': lot['MfgLotName'], 'instanceId': lot['MfgLotId']}, 'samplingLotResult': 2,
            'reworkReason': reason, 'comments': marker+(' '+comment if comment else '')}
    preview = await batch_preview(body, [m['ContainerName'] for m in view['members']])
    if await e.lot_view('', lot['MfgLotId']) != view: raise ValueError('准备返工期间批、完整成员或任务发生变化。')
    for old in routes:
        ref, state, status = await i.container_snapshot(old['container']['name'], old['container']['instanceId'])
        current_status = e.status_snapshot(status) | {'InRework': i.wire_bool(status.get('InRework'), '在返工标志')}
        if ref != old['container'] or state != old['container_sql'] or current_status != old['status']:
            raise ValueError('准备返工期间成员运行状态或当前步骤发生变化。')
    await dep.stable()
    if await sampling_tasks(view['members']) != member_tasks:
        raise ValueError('准备期间成员采样任务或已有结果变化，停止返工计划。')
    reads = {'modeling:SamplingPlans:id:'+lot['SamplingPlanId']} | {'modeling:SampleTests:id:'+t['SampleTestId'] for t in view['tasks']}
    for (entity, _), state in dep.states.items(): reads.update(d.resources(entity, state))
    preview.update(member_routes=routes, dynamic_validation_verified=False, signature_verified=False,
                   original_member_task_count=len(member_tasks), member_task_cleanup_baseline_available=True,
                   quantity_effects_verified=False, native_followup_effects_verified=False, quality_verdict=None,
                   deployed_logic_overrides_verified=False,
                   note='单次原生整批返工；逐成员Rework仅非执行探测，不循环提交。标准csi快照逻辑会删除成员ContainerSampleData并标记批完成/返工结果；未核实部署覆盖，实际路由、数量、历史与后续影响仍待验收。')
    return {'kind': 'lot_rework', 'action': 'rework', 'service': 'ExecuteSamplingLot', 'name': lot['MfgLotName'],
            'target_id': lot['MfgLotId'], 'scope_key': 'sampling-lot:'+lot['MfgLotName'].casefold(),
            'reason': reason, 'before': view, 'member_sampling_tasks': member_tasks, 'dependencies': list(dep.states.values()),
            'body': body, 'preview': preview, 'marker': marker, 'arguments': args, 'environment': p.environment(),
            'resource_reads': sorted(reads), 'resource_writes': [e.runtime_claim('lot', lot['MfgLotId'])]+
                [e.runtime_claim('container', m['ContainerId']) for m in view['members']]}


async def inspect_snapshot(snap):
    from tools.quality_lot_rework_evidence import sampling_tasks, cleanup, routes
    ids = [m['ContainerId'] for m in snap['before']['members']]
    if not 1 <= len(ids) <= 20 or any(not q.valid_id(key) for key in ids) or len(set(ids)) != len(ids):
        raise ValueError('原返工成员ID集合无效。')
    clause = ','.join("'"+key+"'" for key in ids)
    rows = await q.sql_rows(f'SELECT {e.MEMBER_COLUMNS} FROM Container WHERE ContainerId IN ({clause}) ORDER BY ContainerId', 21)
    if any(r.get('ContainerId') not in ids for r in rows) or len({r['ContainerId'] for r in rows}) != len(rows):
        raise ValueError('原成员当前状态身份不一致。')
    tasks = await sampling_tasks(snap['before']['members'])
    route_evidence = await routes(snap, rows)
    again = await q.sql_rows(f'SELECT {e.MEMBER_COLUMNS} FROM Container WHERE ContainerId IN ({clause}) ORDER BY ContainerId', 21)
    if again != rows or await sampling_tasks(snap['before']['members']) != tasks or await routes(snap, again) != route_evidence:
        raise ValueError('核实期间原成员状态或采样任务变化。')
    return {'current_lot': await e.lot_view('', snap['target_id']), 'original_members_current_records': rows,
            'sampling_task_cleanup': cleanup(snap.get('member_sampling_tasks'), tasks), 'member_route_evidence': route_evidence,
            'missing_original_member_ids': sorted(set(ids)-{r['ContainerId'] for r in rows}),
            'verified_success': False, 'state_changed': False, 'retry_allowed': False, 'history_correlation_verified': False,
            'note': '原批结果或成员当前同值不证明本次返工成功，不跟随新成员替换原成员，不自动解锁或重试。'}


@mcp.tool
async def prepare_sampling_lot_rework(lot_name: str, rework_reason_name: str,
        lot_instance_id: str | None = None, comments: str = '', expected_path_name: str | None = None) -> str:
    """准备单次ExecuteSamplingLot结果2整批返工。完整1..20个Active无冻结、无层级/非零第二数量且未在返工的成员，MES未通过且有拒收超任务阈值证据。无动态选择器时，原生使用首条ReworkPath。单路径可沿用默认；多路径须明确expected_path_name，所有成员的原生首条路径名称必须匹配，且预览须完整回显路径、目标和返回步骤。此参数核对默认路径，不覆盖整批服务的路径选择。目标Spec允许返工、原因属于各成员当前Operation完整原因组。固定步骤/路径持久ID、完整有序配置及依赖复读，状态或路径顺序变化使旧计划失效。确认后仅一次整批ExecuteSamplingLot，成员Rework仅非执行探测；unknown不重试。准备绑定最多200条原成员ContainerSampleData任务及已有结果，变化使计划失效。inspect核对原任务缺失/保留/新增、成员当前返工步骤和数量；旧计划缺少清理基线时明确未知。原生会清理成员任务并标记批完成/返工结果，实际路由/数量/签名/历史待验收。"""
    arguments = dict(lot_name=lot_name, rework_reason_name=rework_reason_name,
                     lot_instance_id=lot_instance_id, comments=comments)
    if expected_path_name is not None:
        arguments['expected_path_name'] = expected_path_name
    return await p.prepare('lot_rework', arguments)
