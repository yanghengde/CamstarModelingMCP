"""One native whole-lot scrap using an explicitly resolved LossReason ID.

The deployed Load accepts LossReason in the ScrapReason slot. Pin and verify
that concrete type instead of resolving a ScrapReason by a similar name.
"""
import json
import re

from tools import mcp, quality as q, quality_inspection as inspection, quality_plans as plans
from tools import quality_lot_execution as lots, quality_lot_rework as rework, quality_quantity_loss as loss
from tools import quality_lot_rework_evidence as evidence
from tools.quality_definitions import resources
from core.http_client import request
from tools.modeling_details import raw_json, _has_next_link
from config import CAMSTAR_SHOPFLOOR_BASE_URL

DEFAULTS = ('CloseWhenZero', 'DisassociateWhenClosed', 'GenerateRollups', 'RecordAllQtySummaryOnly')


def reply(raw):
    outer = json.loads(raw)
    if not isinstance(outer, dict) or outer.get('error') or outer.get('ErrorCode') or _has_next_link(outer):
        raise ValueError('原生批报废读取失败或分页。')
    data = outer.get('data', outer)
    if data is outer and 'value' in outer:
        values = outer['value']
        if not isinstance(values, list) or len(values) != 1:
            raise ValueError('原生批报废读取须返回唯一服务。')
        data = values[0]
    if not isinstance(data, dict) or data.get('error') or data.get('ErrorCode') or _has_next_link(data):
        raise ValueError('原生批报废回显不完整。')
    return data


def signatures(data):
    if data.get('ESigRequirement') or data.get('AdditionalESigReqs') or data.get('ESigDetails'):
        raise ValueError('批报废需要额外签名，本入口保持原生签名要求。')


async def reason_binding(reason):
    data = reply(await request('POST', '/api/ExecuteSamplingLot/PerformEvent',
        base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body={'event': {'eventName': 'Load'}, 'inputData': {'scrapReason': reason}},
        params={'$select': 'ScrapReason,SamplingLot,SamplingLotResult'}, response_formatter=raw_json))
    actual = data.get('ScrapReason')
    if (not plans.wire_matches({'scrapReason': reason}, data)
            or not isinstance(actual, dict) or actual.get('CDOTypeName') != 'LossReason'):
        raise ValueError('当前部署未将明确LossReason ID绑定到整批ScrapReason槽，不转换或按同名替代。')
    return {'concrete_type': 'LossReason', 'instance_id': reason['instanceId'], 'load_verified': True}


async def child_defaults(ref, reason, step_id):
    body = {'container': ref, 'serviceDetails': [{'@odata.type': '#shopfloor.LossDetails',
        'changeQtyType': 2, 'recordAllQty': True, 'reasonCode': reason}]}
    data = reply(await request('POST', '/api/ChangeQty', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body=body, params={'execute': 'false', '$select': 'Container,ServiceDetails,ESigRequirement,AdditionalESigReqs,'
            + ','.join(DEFAULTS), '$expand': 'ServiceDetails'}, response_formatter=raw_json))
    signatures(data)
    rows = data.get('ServiceDetails')
    if not plans.wire_matches({'container': ref}, data) or not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('原生报废子预览目标或明细不完整。')
    line = rows[0]
    expected = {k: v for k, v in body['serviceDetails'][0].items() if k != '@odata.type'}
    if (not plans.wire_matches(expected, line) or str(line.get('CDOTypeId')) != '4030'
            or str(line.get('QtyMultiplier')) != '-1'
            or inspection.wire_bool(line.get('CountsAgainstProduction'), '生产数量标志') is not True
            or not plans.wire_matches({'container': ref}, line)):
        raise ValueError('报废子预览不是原目标的全量LossDetails。')
    if line.get('ChargeToStep') and q.identifier(line['ChargeToStep']) != step_id:
        raise ValueError('报废计入步骤不符。')
    if line.get('Qty2') not in (None, '') and inspection.numeric(str(line['Qty2']), '第二数量') != 0:
        raise ValueError('批报废首版不接受第二数量。')
    return dict(root={key: inspection.wire_bool(data.get(key), key) for key in DEFAULTS},
        adjust_throughput=inspection.wire_bool(line.get('AdjustThruput'), '原生生产数量默认'),
        record_all_quantity=True, detail_type=4030, counts_against_production=True,
        child_execution_verified=False)


async def batch_preview(body, names):
    data = reply(await request('POST', '/api/ExecuteSamplingLot', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body=body, params={'execute': 'false', '$select':
            'SamplingLot,SamplingLotResult,ScrapReason,Comments,Containers,ESigRequirement,AdditionalESigReqs'},
        response_formatter=raw_json))
    signatures(data)
    members = data.get('Containers')
    if (not plans.wire_matches(body, data) or not isinstance(members, list)
            or any(not isinstance(name, str) for name in members) or len(set(members)) != len(members)
            or set(members) != set(names)):
        raise ValueError('整批报废回显未绑定完整原批、结果3、原因及成员集合。')
    return dict(verified_input_echo=body, affected_container_names=sorted(names))


async def member_context(member, lot_id, reason, dependencies):
    ref, row, status = await loss.snapshot(member['ContainerName'], member['ContainerId'],
                                         expected_sampling_lot_id=lot_id)
    if (row['ChangeCount'] != inspection.wire_int(member['ChangeCount'], '成员版本')
            or row['ProductId'] != member['ProductId']
            or inspection.numeric(str(row['Qty']), '成员数量') != inspection.numeric(str(member['Qty']), '批成员数量')):
        raise ValueError('批头成员与原生数量处置成员版本不一致。')
    spec = await dependencies.asset('Specs', status['Spec'])
    operation = await dependencies.asset('Operations', spec.get('Operation'))
    if operation['InstanceID'] != q.identifier(status.get('Operation')):
        raise ValueError('批报废成员工序和Operation不一致。')
    group = await dependencies.asset('LossReasonGroups', operation.get('LossReasons'))
    if sum(q.identifier(item) == reason['instanceId'] and item.get('Name') == reason['name']
           for item in group['ResolvedEntries']) != 1:
        raise ValueError('LossReason不属于每个原成员当前Operation的完整LossReasons组。')
    unit = await dependencies.asset('UOMs', {'InstanceId': row['UOMId']})
    loss.quantity_options(dict(record_all_quantity=True, close_when_zero=False,
        adjust_throughput=False, loss_quantity=None), row)
    return dict(container=ref, record=row, status=status, unit={'name': unit['Name'], 'instanceId': unit['InstanceID']},
        expected_loss_quantity=str(row['Qty']), expected_remaining_quantity='0',
        native_defaults=await child_defaults(ref, reason, row['WorkflowStepId']))


async def build_scrap(arguments, marker):
    comment = inspection.inspection_comment(arguments.get('comments', ''))
    view = await lots.lot_view(arguments['lot_name'], arguments.get('lot_instance_id'))
    issues = rework.blockers(view, disposition='报废')
    if issues:
        raise ValueError('不能准备整批报废：' + '；'.join(issues[:12]))
    tasks = await evidence.sampling_tasks(view['members'])
    dependencies = loss.Dependencies(request_limit=160)
    root = await dependencies.asset('LossReasons', {'Name': q.text(arguments['loss_reason_name'], '损失原因')})
    reason = {'name': root['Name'], 'instanceId': root['InstanceID']}
    binding = await reason_binding(reason)
    lid = view['lot']['MfgLotId']
    from tools.production_reporting import ensure_no_unresolved_throughput,ensure_no_unresolved_collection
    for row in view['members']:
        ensure_no_unresolved_throughput(row['ContainerId']);ensure_no_unresolved_collection(row['ContainerId'])
    members = [await member_context(row, lid, reason, dependencies) for row in view['members']]
    body = dict(samplingLot={'name': view['lot']['MfgLotName'], 'instanceId': lid}, samplingLotResult=3,
                scrapReason=reason, comments=marker + (' ' + comment if comment else ''))
    preview = await batch_preview(body, [row['ContainerName'] for row in view['members']])
    if await lots.lot_view('', lid) != view or await evidence.sampling_tasks(view['members']) != tasks:
        raise ValueError('批、成员或采样任务在准备期间变化。')
    for member in members:
        if await loss.snapshot(member['container']['name'], member['container']['instanceId'],
                expected_sampling_lot_id=lid) != (member['container'], member['record'], member['status']):
            raise ValueError('成员数量、单位或工序在准备期间变化。')
    await dependencies.stable()
    if await reason_binding(reason) != binding:
        raise ValueError('部署的报废原因绑定变化。')
    reads = {'modeling:SamplingPlans:id:' + view['lot']['SamplingPlanId']}
    reads.update('modeling:SampleTests:id:' + task['SampleTestId'] for task in view['tasks'])
    for state in dependencies.states.values():
        reads.update(resources(state['entity'], state))
    preview.update(member_dispositions=members, reason_binding=binding, original_member_task_count=len(tasks),
        atomicity_verified=False, quantity_effects_verified=False, signature_verified=False,
        note='一次原生ExecuteSamplingLot结果3；逐成员ChangeQty均为非执行默认值探测。'
             'MES内部全量损失、原生关闭/生产数量默认及采样任务清理均须审阅。未知结果不自动重放。')
    return dict(kind='lot_scrap', action='scrap', service='ExecuteSamplingLot', name=view['lot']['MfgLotName'],
        target_id=lid, scope_key='sampling-lot:' + view['lot']['MfgLotName'].casefold(), before=view,
        reason=reason, body=body, preview=preview, member_sampling_tasks=tasks,
        resource_reads=sorted(reads), resource_writes=[lots.runtime_claim('lot', lid)]
            + [lots.runtime_claim('container', row['ContainerId']) for row in view['members']],
        dependencies=list(dependencies.states.values()), marker=marker, arguments=arguments, environment=plans.environment())


def validate_transport(snapshot):
    body = snapshot['body']
    if (snapshot.get('kind') != 'lot_scrap' or snapshot.get('action') != 'scrap'
            or snapshot.get('service') != 'ExecuteSamplingLot'
            or set(body) != {'samplingLot', 'samplingLotResult', 'scrapReason', 'comments'}
            or type(body['samplingLotResult']) is not int or body['samplingLotResult'] != 3
            or body['samplingLot'] != {'name': snapshot['name'], 'instanceId': snapshot['target_id']}
            or body['scrapReason'] != snapshot['reason']):
        raise ValueError('整批报废目标、结果3或显式LossReason载荷不符。')
    if not q.valid_id(snapshot['target_id']) or not q.valid_id(snapshot['reason']['instanceId']):
        raise ValueError('整批报废持久引用无效。')
    binding = snapshot['preview']['reason_binding']
    if binding != dict(concrete_type='LossReason', instance_id=snapshot['reason']['instanceId'], load_verified=True):
        raise ValueError('当前部署LossReason绑定未核实。')
    marker = snapshot['marker']
    if not isinstance(marker, str) or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',marker) or not (body['comments'] == marker or body['comments'].startswith(marker + ' ')):
        raise ValueError('报废标记不符。')


def current_scope_checks(snapshot, current_lot, current):
    """Native scrap keeps lot membership and lot tasks, removes member tasks.

    Verified against the C4 native result and the Part 5 isolated matrix. Do not
    equate zero balances alone with an unchanged original production scope.
    """
    before=snapshot['before'];lot=current_lot['lot'];original=before['lot']
    ids={r['ContainerId'] for r in before['members']};members=current_lot['members']
    return {
        'original_lot_identity':all(lot.get(k)==original.get(k) for k in ('MfgLotId','MfgLotName','CDOTypeId','ProductId','UOMId','SamplingPlanId','LotStatus'))
            and inspection.numeric(str(lot['Qty']),'当前批量')==inspection.numeric(str(original['Qty']),'原批量'),
        'original_complete_membership':len(members)==len(ids) and {r['ContainerId'] for r in members}==ids and set(current)==ids,
        'original_lot_tasks_preserved':current_lot['tasks']==before['tasks'],
        'original_member_context':set(current)==ids and all(row and row['SamplingLotId']==snapshot['target_id']
            and row['CDOTypeId']==1040 and row['CurrentHoldCount']==0 and row['InRework'] is False
            and not row['ParentContainerId'] and row['ChildCount']==0 and not row['CarrierId'] for row in current.values()),
    }


async def inspect_snapshot(snapshot):
    current = []
    for member in snapshot['preview']['member_dispositions']:
        row = await loss.record(member['container']['instanceId'])
        units_match = bool(row and row['UOMId'] == member['record']['UOMId'])
        current.append(dict(container_id=member['container']['instanceId'], current_record=row,
            expected_unit=member['unit'], expected_remaining_quantity='0',
            zero_quantity_currently_observed=units_match and inspection.numeric(str(row['Qty']), '当前数量') == 0,
            expected_closed=member['native_defaults']['root']['CloseWhenZero'],
            closed_currently_observed=bool(row and row['Status'] == 2), causal_chain_verified=False))
    tasks = await evidence.sampling_tasks(snapshot['before']['members'])
    current_lot = await lots.lot_view('', snapshot['target_id'])
    for observed in current:
        if await loss.record(observed['container_id']) != observed['current_record']:
            raise ValueError('核实报废期间原成员记录变化。')
    if await evidence.sampling_tasks(snapshot['before']['members']) != tasks or await lots.lot_view('', snapshot['target_id']) != current_lot:
        raise ValueError('核实报废期间批或成员采样任务变化。')
    return dict(current_lot=current_lot, original_members=current,
        current_scope_checks=current_scope_checks(snapshot,current_lot,{row['container_id']:row['current_record'] for row in current}),
        sampling_task_cleanup=evidence.cleanup(snapshot.get('member_sampling_tasks'), tasks),
        verified_success=False, retry_allowed=False, state_changed=False, history_correlation_verified=False,
        note='原成员余额、关闭状态及任务缺失仅为证据；须reconcile_quality_execution核对唯一原生父子事务，再review_quality_transaction(resolve_history)核实完整原对象效果。unknown继续占用，不重放。')


@mcp.tool
async def prepare_sampling_lot_scrap(lot_name: str, loss_reason_name: str,
                                   lot_instance_id: str | None = None, comments: str = '') -> str:
    """准备一次原生ExecuteSamplingLot结果3整批报废。完整1..20个未通过、具备拒收超阈值证据的基础Container成员，Active无冻结/返工/层级/载具/第二单位。loss_reason_name明确选择LossReason，以持久ID核实当前部署ScrapReason槽真实LossReason类型，并核对每个成员Operation完整LossReasons组。逐成员ChangeQty只做execute=false全量LossDetails及原生关闭/生产数量默认值探测，正式仅提交一次整批事务。绑定数量/单位/完整成员及采样任务，变化拒绝旧计划；原生会全量扣量、按默认关闭并清理成员采样任务，保留原批成员归属和批级任务。execute_quality_transaction单次提交，再reconcile_quality_execution及review_quality_transaction(resolve_history)核实原生父子历史与完整效果结案。HTTP错误/超时/未知不重试；无历史或同值回读不能自动解锁，也不保证所有故障下的原子性。"""
    return await plans.prepare('lot_scrap', dict(lot_name=lot_name, loss_reason_name=loss_reason_name,
        lot_instance_id=lot_instance_id, comments=comments))
