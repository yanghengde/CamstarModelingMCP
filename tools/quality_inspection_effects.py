"""Opt-in native single-container full inspection or quantity loss.

The parent CollectSamplingData is the only business submission. Child ChangeQty
calls here are execute=false probes; failure conversion never patches task data.
"""
import json

from tools import quality as q, quality_inspection as i, quality_quantity_loss as loss
from tools import quality_lot_execution as lot, quality_plans as p, quality_definitions as d
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

EXTRA_ASSETS = {'SampleTests': ('SampleTest', 'SampleTestId', True),
                'SampleDataPoints': ('SampleDataPoint', 'SampleDataPointId', True)}
SAMPLE_SIZE_REASON = 'DecreaseBySampleSizeReason'
REJECT_COUNT_REASON = 'DecreaseByRejectCountReason'
LOSS_MODES = {
    'counted_sample_size_loss': (SAMPLE_SIZE_REASON, 'explicit current counted SamplesTested'),
    'measured_sample_size_loss': (SAMPLE_SIZE_REASON, 'current measured ServiceDetails count'),
    'counted_reject_count_loss': (REJECT_COUNT_REASON, 'current counted reject detail accumulated as native FailedSamples'),
}


def options(args):
    values = tuple(args.get(k, False) for k in ('allow_full_inspection', 'allow_automatic_quantity_loss'))
    if any(type(v) is not bool for v in values): raise ValueError('原生全检/自动扣量选项须为明确布尔值。')
    zero = args.get('allow_zero_quantity_loss', False)
    if type(zero) is not bool or zero and not values[1]:
        raise ValueError('允许扣量归零须明确布尔值并同时允许原生自动扣量。')
    return values


def definition_policy(definition, args):
    full, quantity = options(args); test = definition['test']
    if not quantity:
        i.reject_effectful_definition(definition)
        return 'full_inspection' if full else None
    if type(test.get('SampleType')) is not int or test['SampleType'] not in (1, 2):
        raise ValueError('自动扣量仅支持计数或数值测量检验。')
    sample_size = i.present(test.get(SAMPLE_SIZE_REASON))
    reject_count = i.present(test.get(REJECT_COUNT_REASON))
    if sample_size == reject_count:
        raise ValueError('检验测试必须且只能配置一种扣量原因。')
    if reject_count and test['SampleType'] != 1:
        raise ValueError('按本次拒收数扣量仅支持计数检验。')
    if test.get('ScrapCountedRejectsByReason') is not False: raise ValueError('按原因报废拒收的配置为真或未知，首版不支持。')
    for field in ('DefaultFailureMode', 'Classification', 'SubClassification'):
        if i.present(test.get(field)): raise ValueError('首版不处理检验自动质量事件：'+field)
    if test.get('DefaultPEDescription'): raise ValueError('首版不处理检验自动生产事件说明。')
    if type(test.get('Status')) is not int or test['Status'] != 1: raise ValueError('检验测试不是Active。')
    if reject_count:
        return 'counted_reject_count_loss'
    return 'counted_sample_size_loss' if test['SampleType'] == 1 else 'measured_sample_size_loss'


def task_policy(task, container, args):
    full, quantity = options(args)
    a, b = task.get('InspectAllContQty'), task.get('InspectAllQtyOnFailure')
    if type(a) is not bool or type(b) is not bool: raise ValueError('任务全检/失败转全检标志未知。')
    if not full:
        if a or b: raise ValueError('全检任务须明确allow_full_inspection=true，不能隐式提交。')
        if task['Rejects'] > task['AllowedRejects']: raise ValueError('现有拒收超阈值，当前未允许全检续检。')
        return
    if not (a or b): raise ValueError('当前任务没有原生全检规则；此选项不能强制修改检验任务。')
    qty = i.numeric(str(container['Qty']), '全检当前数量')
    if qty != qty.to_integral_value() or not 1 <= qty <= 2147483647: raise ValueError('全检首版要求明确的正整数当前数量。')
    if a and task['TotalSamplesRequired'] != int(qty): raise ValueError('已配置全检，但MES任务要求数不等于当前数量，不本地重算。')
    if task['Rejects'] > task['AllowedRejects'] and task['TotalSamplesRequired'] != int(qty):
        raise ValueError('失败后任务尚未具有当前全量要求，不自行修改或补写要求数。')


def model_ref(row):
    return {'Name': row['Name'], 'Revision': row['Revision'], 'InstanceId': row['InstanceID']}


async def pin_definition(dep, definition):
    root = await dep.asset('SampleTests', model_ref(definition['test']))
    actual = {k: root.get(k) for k in i.TEST_FIELDS}
    for k in ('Status', 'SampleType'): actual[k] = i.wire_int(actual[k], k)
    for k in ('IsFrozen', 'AllowMoveOnFailure', 'ScrapCountedRejectsByReason'):
        actual[k] = i.wire_bool(actual[k], k, nullable=True)
    if actual != definition['test']: raise ValueError('检验定义与被固定的原生副作用配置不同。')
    for point in definition['points']:
        root = await dep.asset('SampleDataPoints', model_ref(point))
        actual = {k: root.get(k) for k in i.POINT_FIELDS}
        for k in ('Status', 'DataType'): actual[k] = i.wire_int(actual[k], k)
        if actual != point: raise ValueError('数值检验点与被固定的限值/类型不同。')


async def child_defaults(ref, reason, count, step_id):
    # Do not send caller-selected CloseWhenZero/AdjustThruput/RecordAllQty here:
    # CollectSampling_ChangeQty creates this child and uses its native defaults.
    body = {'container': ref, 'serviceDetails': [{'@odata.type': '#shopfloor.LossDetails',
            'changeQtyType': 2, 'qty': count, 'reasonCode': reason}]}
    fields = ('CloseWhenZero', 'DisassociateWhenClosed', 'GenerateRollups', 'RecordAllQtySummaryOnly')
    raw = await request('POST', '/api/ChangeQty', base_url=CAMSTAR_SHOPFLOOR_BASE_URL, body=body,
                        params={'execute': 'false', '$select': 'Container,ServiceDetails,'+','.join(fields), '$expand': 'ServiceDetails'},
                        response_formatter=raw_json)
    try: outer = json.loads(raw)
    except (TypeError, ValueError): raise ValueError('原生自动扣量子服务非执行探测失败：'+str(raw)[:1000]) from None
    if not isinstance(outer, dict): raise ValueError('扣量子服务预览不是对象。')
    data = outer.get('data', outer)
    if (not isinstance(data, dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error') or data.get('ErrorCode')
            or q._has_next_link(data) or not p.wire_matches({'container': ref}, data)):
        raise ValueError('扣量子服务回显错误、目标不符或分页。')
    rows = data.get('ServiceDetails')
    if not isinstance(rows, list) or len(rows) != 1: raise ValueError('扣量子服务须回显唯一LossDetails。')
    row = rows[0]
    if (str(row.get('CDOTypeId')) != '4030' or str(row.get('QtyMultiplier')) != '-1'
            or str(row.get('ChangeQtyType')) != '2' or not p.wire_matches({'reasonCode': reason, 'container': ref}, row)
            or i.wire_bool(row.get('RecordAllQty'), '原生全量损失默认') is not False
            or i.wire_bool(row.get('CountsAgainstProduction'), '原生计入生产') is not True
            or i.numeric(str(row.get('Qty')), '原生扣量') != count):
        raise ValueError('原生扣量子类型、原因、目标、默认数量模式或方向不符。')
    if row.get('Qty2') not in (None, '') and i.numeric(str(row['Qty2']), '原生第二数量') != 0:
        raise ValueError('原生扣量出现非预期第二数量。')
    if row.get('ChargeToStep') and q.identifier(row['ChargeToStep']) != step_id: raise ValueError('原生扣量计入步骤不符。')
    return {'root': {k: i.wire_bool(data.get(k), k) for k in fields},
            'adjust_throughput': i.wire_bool(row.get('AdjustThruput'), '原生生产数量默认'),
            'record_all_quantity': False, 'detail_type': 4030, 'quantity_multiplier': -1,
            'counts_against_production': True, 'runtime_child_execution_verified': False}


async def prepare_effects(definition, basic, status, task, args, mode, count, rejects):
    full, quantity = options(args)
    if not (full or quantity): return None
    if basic.get('SamplingPassed') is not False: raise ValueError('原生全检/扣量要求MES明确尚未通过，未知或已通过状态不提交。')
    ref, row, current = await loss.snapshot(args['container_name'], basic['ContainerId'])
    if (row['ChangeCount'] != basic['ChangeCount'] or row['ProductId'] != basic.get('ProductId')
            or i.numeric(str(row['Qty']), '完整当前数量') != i.numeric(str(basic['Qty']), '检验当前数量')
            or current != lot.status_snapshot(status)):
        raise ValueError('检验与原生副作用目标/数量/当前状态读取不一致。')
    dep = loss.Dependencies(EXTRA_ASSETS)
    await pin_definition(dep, definition)
    spec = await dep.asset('Specs', status.get('Spec'))
    if spec['InstanceID'] != row['SpecId']: raise ValueError('原生副作用工序与持久当前状态不符。')
    unit = await dep.asset('UOMs', {'InstanceId': row['UOMId']})
    context = {'mode': mode,
               'allow_full_inspection': full, 'allow_automatic_quantity_loss': quantity,
               'runtime_effects_verified': False, 'deployed_logic_overrides_verified': False,
               'quality_verdict': None, 'separate_quantity_submission': False,
               'unit': {'name': unit['Name'], 'instanceId': unit['InstanceID']}}
    if full:
        context['full_inspection'] = {'stored_inspect_all': task['InspectAllContQty'],
            'failure_rule_enabled': task['InspectAllQtyOnFailure'], 'stored_required_samples': task['TotalSamplesRequired'],
            'current_whole_quantity': str(row['Qty']), 'task_flags_overridden': False, 'conversion_verified': False,
            'note': '标准逻辑在MES判Fail且失败全检规则启用时改任务要求为Container.Qty；该CLF没有实际设置InspectAllContQty=true。不补写标志、不本地修改要求数，不将预览当转换成功。'}
    if quantity:
        operation = await dep.asset('Operations', spec.get('Operation'))
        if operation['InstanceID'] != q.identifier(status.get('Operation')): raise ValueError('扣量Operation与检验当前状态不同。')
        reason_field, basis = LOSS_MODES[mode]
        reason = await dep.asset('LossReasons', definition['test'][reason_field])
        group = await dep.asset('LossReasonGroups', operation.get('LossReasons'))
        if sum(q.identifier(v) == reason['InstanceID'] and v.get('Name') == reason['Name'] for v in group['ResolvedEntries']) != 1:
            raise ValueError('测试中配置的损失原因不属于当前Operation完整LossReasons组。')
        deduction = rejects if mode == 'counted_reject_count_loss' else count
        balance = i.numeric(str(row['Qty']), '原数量')
        if deduction <= 0: raise ValueError('按拒收数扣量要求本次有明确的正数拒收。')
        if mode == 'counted_reject_count_loss' and count > balance:
            raise ValueError('本次计数样本数不能超过容器当前数量。')
        zero = balance == deduction
        if deduction > balance:
            raise ValueError('检验自动扣量不能超过当前数量。')
        if zero and not args.get('allow_zero_quantity_loss', False):
            raise ValueError('检验扣量归零须明确allow_zero_quantity_loss=true。')
        if full and zero:
            raise ValueError('全检与扣量组合须保留正数检验数量。')
        # Arithmetic validation only; these local options never become a request.
        amount, remaining, _ = loss.quantity_options({'record_all_quantity': zero, 'loss_quantity': None if zero else str(deduction),
            'close_when_zero': False, 'adjust_throughput': False}, row)
        reason_ref = {'name': reason['Name'], 'instanceId': reason['InstanceID']}
        defaults = await child_defaults(ref, reason_ref, deduction, row['WorkflowStepId'])
        if zero and not defaults['root']['CloseWhenZero']:
            raise ValueError('检验归零仅支持原生CloseWhenZero明确为true的关闭行为。')
        context['automatic_quantity_loss'] = {'basis': basis, 'quantity': str(amount),
            'projected_remaining_quantity': str(remaining), 'reason': reason_ref, 'native_child_defaults': defaults,
            'allow_zero_quantity_loss': args.get('allow_zero_quantity_loss', False), 'projected_close': zero,
            'projection_is_execution_result': False,
            'note': '只提交一次CollectSamplingData，MES内部创建LossDetails；本工具不再提交ChangeQty。标准CLF内部把子服务ProcessESignatures设false，本工具不覆盖父事务签名；部署及签名链未验证。'}
        if full:
            if remaining != remaining.to_integral_value():
                raise ValueError('全检与扣量组合的剩余数量须为正整数。')
            context['full_inspection'].update(
                projected_quantity_after_loss=str(remaining),
                conditional_required_samples_after_failure=(
                    int(remaining) if task['InspectAllQtyOnFailure'] and not task['InspectAllContQty']
                    else task['TotalSamplesRequired']),
                native_processing_order=['inspection_counters', 'quantity_loss', 'failure_full_inspection_rule'])
    reads = set()
    for (entity, _), state in dep.states.items(): reads.update(d.resources(entity, state))
    return {'context': context, 'container': row, 'container_status': current,
            'dependencies': list(dep.states.values()), 'resource_reads': sorted(reads)}


async def final_recheck(effects):
    """Recheck after the parent preview, not only before that last HTTP read."""
    saved = effects['container']
    ref, row, status = await loss.snapshot(saved['ContainerName'], saved['ContainerId'])
    if row != saved or status != effects['container_status']:
        raise ValueError('父检验预览后原生副作用目标/单位/状态已变化。')
    fresh = loss.Dependencies(EXTRA_ASSETS)
    for state in effects['dependencies']:
        entity = state['entity']
        root = await fresh.asset(entity, {'InstanceId': state['InstanceID']})
        if fresh.states[(entity, root['InstanceID'])] != state:
            raise ValueError('父检验预览后原生全检/扣量配置已变化。')


def validate_transport(snap):
    body = snap['body']
    effects = snap.get('inspection_effects')
    mode = effects['context']['mode'] if effects else None
    counted_loss = mode in ('counted_sample_size_loss', 'counted_reject_count_loss')
    expected_fields = {'container', 'sampleTest', 'serviceDetails', 'moveContainerOnCompletion', 'comments'}
    if counted_loss:
        expected_fields.add('samplesTested')
    if (snap.get('kind') != 'inspection' or snap.get('action') != 'collect'
            or set(body) != expected_fields
            or body['moveContainerOnCompletion'] is not False):
        raise ValueError('单容器检验仅固定CollectSamplingData、移站false和原检验字段，不接受扣量/全检/签名覆盖。')
    if effects:
        arguments = snap['arguments']
        full, quantity = options(arguments)
        if (effects['context']['allow_full_inspection'], effects['context']['allow_automatic_quantity_loss']) != (full, quantity):
            raise ValueError('原生检验副作用选项与原计划不符。')
        if mode != definition_policy(snap['definition'], arguments):
            raise ValueError('原生检验副作用模式不符。')
        task_policy(snap['before']['task'], snap['before']['container'], arguments)
        if quantity:
            projected = effects['context']['automatic_quantity_loss']
            remaining = i.numeric(projected['projected_remaining_quantity'], '预计剩余量')
            zero = remaining == 0
            if (projected.get('allow_zero_quantity_loss', False) != arguments.get('allow_zero_quantity_loss', False)
                    or remaining < 0
                    or 'projected_close' in projected and projected['projected_close'] is not zero
                    or zero and (not arguments.get('allow_zero_quantity_loss', False)
                                 or projected.get('projected_close') is not True
                                 or projected['native_child_defaults']['root']['CloseWhenZero'] is not True)):
                raise ValueError('检验归零许可、关闭投影或原生默认不符。')
            if full:
                full_context = effects['context']['full_inspection']
                task = snap['before']['task']
                conditional = (int(remaining) if task['InspectAllQtyOnFailure'] and not task['InspectAllContQty']
                               else task['TotalSamplesRequired'])
                if (remaining <= 0 or remaining != remaining.to_integral_value()
                        or full_context['projected_quantity_after_loss'] != str(remaining)
                        or full_context['conditional_required_samples_after_failure'] != conditional):
                    raise ValueError('全检与扣量组合的剩余数量或条件要求数不符。')
        if counted_loss:
            lines, _, count, rejects = i.counted_details(snap['definition'], arguments)
            deduction = rejects if mode == 'counted_reject_count_loss' else count
            if (not quantity or arguments.get('samples') is not None
                    or type(body['samplesTested']) is not int or body['samplesTested'] != count
                    or body['serviceDetails'] != lines or deduction <= 0
                    or effects['context']['automatic_quantity_loss']['quantity'] != str(deduction)):
                raise ValueError('计数扣量增量、根SamplesTested或完整计数明细不一致。')


async def inspect_snapshot(snap):
    cid = snap['target_id']
    tid = snap['before']['task']['ContainerSampleDataId']
    if not q.valid_id(cid) or not q.valid_id(tid): raise ValueError('原生检验原目标/任务ID无效。')
    rows = await q.sql_rows(f"SELECT {i.TASK_COLUMNS} FROM ContainerSampleData WHERE ContainerSampleDataId='{tid}' AND ContainerId='{cid}'",1)
    i.check_tasks(rows,cid)
    if len(rows)>1 or rows and rows[0]['ContainerSampleDataId']!=tid: raise ValueError('原生检验原任务身份不符。')
    evidence = {'original_container_id': cid, 'current_quantity_record': await loss.record(cid),
            'original_task_id': tid, 'current_original_task': rows[0] if rows else None,
            'expected_native_effects': snap['inspection_effects']['context'],
            'verified_success': False, 'state_changed': False, 'retry_allowed': False,
            'native_child_history_verified': False, 'full_inspection_conversion_verified': False,
            'note': '父检验记录、当前数量或任务标志不能证明本次转换/内部扣量已完成；需最终父子事务及原任务证据人工review，不补发扣量，不自动解锁。'}
    if snap['arguments'].get('counted_groups') is not None:
        evidence['expected_counted_submission'] = {
            'group_count': snap['preview']['counted_groups_this_submission'],
            'samples': snap['preview']['samples_this_submission'],
            'rejects': snap['preview']['rejects_this_submission'],
        }
    return evidence
