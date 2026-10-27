"""UpdateSamplingLot plans for zero-count lots; no membership or result overrides."""
import json
from decimal import Decimal, localcontext

from pydantic import StrictInt
from tools import mcp, quality as q, quality_inspection as i, quality_lots as lots
from tools import quality_lot_execution as e
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL


def quantity(value, label):
    number = i.numeric(value, label)
    if not 0 < number <= Decimal('1000000000000'):
        raise ValueError(label+'须大于0且不超过1e12。')
    wire = float(number)
    if Decimal(str(wire)) != number:
        raise ValueError(label+'无法由接口Double无文本精度损失表示。')
    return number, wire


def prerequisites(view):
    lot = view['lot']
    if (lot['LotStatus'] != 1 or lot['SamplingCompleted'] is not False
            or lot['SamplingPassed'] is not False or lot['SamplingLotResult'] not in (None, 0)):
        raise ValueError('仅维护Active、尚未完成/通过且无处置结果的批。')
    qty, _ = quantity(str(lot.get('Qty')), '当前批数量')
    rate = i.int32(i.wire_int(lot.get('SampleRate'), '当前批采样频率'), '当前批采样频率', 1)
    adjust = i.wire_bool(lot.get('AllowAdjustToLotSize'), '原批量调整选项')
    if not view['tasks']:
        raise ValueError('抽样批没有可核对的当前检验任务。')
    for row in view['tasks']:
        if (row['TotalSamplesRequired'] < 1 or any(row[k] != 0 for k in
                ('CompletedSamples', 'Rejects', 'SamplesTested', 'SamplesFailed'))):
            raise ValueError('已有采样/拒收或任务要求未知，当前不调整已开始检验的批。')
        if row.get('InspectAllContQty') is not False or row.get('InspectAllQtyOnFailure') is not False:
            raise ValueError('全检或失败转全检任务当前未支持维护。')
    names = set(); amounts = []
    for row in view['members']:
        name = q.text(row.get('ContainerName'), '成员容器名称')
        if name != row.get('ContainerName') or name in names or row.get('ProductId') != lot['ProductId']:
            raise ValueError('成员名称重复/无效或产品与批不一致。')
        names.add(name)
        if i.wire_int(row.get('Status'), '成员状态') != 1 or i.wire_int(row.get('CurrentHoldCount'), '成员冻结数') != 0:
            raise ValueError('成员非Active、冻结或冻结状态未知。')
        if row.get('ParentContainerId') or i.wire_int(row.get('ChildCount'), '成员子容器数') != 0:
            raise ValueError('层级容器整批维护尚未支持。')
        i.wire_int(row.get('ChangeCount'), '成员变更计数')
        amounts.append(quantity(str(row.get('Qty')), '成员数量')[0])
    # Decimal's default precision could round the sum of many small members.
    with localcontext() as ctx:
        ctx.prec = 400
        total = sum(amounts, Decimal(0))
    return qty, rate, adjust, total, names


async def reject_active_sampling_history(lot_id):
    if not q.valid_id(lot_id): raise ValueError('有效采样历史查询的批ID无效。')
    rows = await q.sql_rows('SELECT TOP 1 s.CollectSamplingDataHistoryId,s.SamplingLotId,s.TxnId,h.ReversalStatus '
        'FROM CollectSamplingDataHistory s INNER JOIN HistoryMainline h '
        'ON h.HistoryMainlineId=s.HistoryMainlineId AND h.TxnId=s.TxnId '
        f"WHERE s.SamplingLotId='{lot_id}' AND h.ReversalStatus=1 ORDER BY s.CollectSamplingDataHistoryId", 1)
    if rows: raise ValueError('存在有效采样历史，即使当前计数为0也不调整。')


async def update_preview(body, view, names):
    from tools.quality_plans import wire_matches
    fields = 'SamplingLot,Qty,SampleRate,AllowAdjustToLotSize,Comments,CurrentSampleRate,SamplingPlan,Containers,LotSampleData'
    params = {'execute': 'false', '$select': fields, '$expand': 'LotSampleData($expand=ExternalSampleData)'}
    raw = await request('POST', '/api/UpdateSamplingLot', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                        body=body, params=params, response_formatter=raw_json)
    try:
        outer = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('抽样批维护非执行预览未通过：'+str(raw)[:2500]) from None
    if not isinstance(outer, dict): raise ValueError('维护预览响应形状无效。')
    data = outer.get('data', outer)
    if (not isinstance(data, dict) or outer.get('error') or outer.get('ErrorCode')
            or data.get('error') or data.get('ErrorCode') or q._has_next_link(data)):
        raise ValueError('维护预览错误或未完整展开。')
    if i.numeric(str(data.get('Qty')), '数量回显') != Decimal(str(body['qty'])):
        raise ValueError('维护数量回显与原请求不一致。')
    if not wire_matches({k: v for k, v in body.items() if k != 'qty'}, data):
        raise ValueError('维护批引用、频率、保留选项或完整评论回显不一致。')
    lot = view['lot']
    if (i.wire_int(data.get('CurrentSampleRate'), '服务当前采样频率') != i.wire_int(lot['SampleRate'], '当前频率')
            or q.identifier(data.get('SamplingPlan')) != lot['SamplingPlanId']):
        raise ValueError('维护服务加载的当前频率/计划与原批不一致。')
    containers = data.get('Containers')
    if (not isinstance(containers, list) or any(not isinstance(n, str) for n in containers)
            or len(containers) != len(set(containers)) or set(containers) != names):
        raise ValueError('维护预览成员集合与完整批快照不一致。')
    rows = data.get('LotSampleData')
    if not isinstance(rows, list) or len(rows) != len(view['tasks']):
        raise ValueError('维护预览没有完整返回原批检验任务。')
    expected = {r['LotSampleDataId']: r for r in view['tasks']}; seen = set(); context = []
    for row in rows:
        key = q.identifier(row)
        if key not in expected or key in seen:
            raise ValueError('维护回显任务不存在或重复。')
        seen.add(key); before = expected[key]
        for field, column in (('SampleTest', 'SampleTestId'), ('SamplingPlan', 'SamplingPlanId'),
                              ('SamplingPlanDetail', 'SamplingPlanDetailId'), ('Spec', 'SpecId'),
                              ('Resource', 'ResourceId'), ('VendorItem', 'VendorItemId')):
            if column not in before:
                raise ValueError('原任务固定引用字段缺失：'+column)
            expected_ref = before[column] or None
            actual_ref = row.get(field)
            # The REST serializer omits null properties. Accept that only when
            # the independently queried task explicitly contains a null FK.
            if ((expected_ref is None and actual_ref not in (None, {}))
                    or (expected_ref is not None and (not q.valid_id(expected_ref) or q.identifier(actual_ref) != expected_ref))):
                raise ValueError('维护回显任务固定引用不符：'+field)
        actual = {}
        for field in ('TotalSamplesRequired', 'CompletedSamples', 'Rejects', 'AllowedRejects', 'SamplesTested', 'SamplesFailed'):
            actual[field] = i.wire_int(row.get(field), '回显'+field)
            if actual[field] != before[field]: raise ValueError('维护加载的原任务计数不一致：'+field)
        for field in ('InspectAllContQty', 'InspectAllQtyOnFailure'):
            if i.wire_bool(row.get(field), field) is not False: raise ValueError('维护回显全检标志不一致。')
        sample_type = i.wire_int(row.get('SampleType'), '回显采样类型')
        if sample_type not in (1, 2) or row.get('ExternalSampleData') != []:
            raise ValueError('外部采样、已有LIMS样本或未完整展开的外部数据不支持维护。')
        context.append({'task_id': key, 'sample_type': sample_type, **actual})
    context.sort(key=lambda r: r['task_id'])
    return {'params': params, 'verified_input_echo': body, 'affected_container_names': sorted(names),
            'current_task_context': context, 'projected_task_requirements_verified': False,
            'dynamic_validation_verified': False, 'quality_verdict': None,
            'note': '回显核对原任务，不推算执行后的要求数/计数或成员采样标志；调整影响整批。'}


async def build_update(args, marker):
    from tools import quality_plans as plans
    new_qty = args.get('new_quantity'); new_rate = args.get('new_sample_rate')
    if new_qty is None and new_rate is None: raise ValueError('必须明确新批数量或新采样频率。')
    proposed_qty = quantity(new_qty, '新批数量') if new_qty is not None else None
    if new_rate is not None: i.int32(new_rate, '新采样频率', 1)
    comment = args.get('comments', '')
    if not isinstance(comment, str) or '\x00' in comment or len(comment.encode('utf-16-le')) > 360:
        raise ValueError('备注最多180个UTF-16单元且不能包含NUL。')
    view = await e.lot_view(args['lot_name'], args.get('lot_instance_id'))
    qty, rate, adjust, total, names = prerequisites(view); lot = view['lot']
    await reject_active_sampling_history(lot['MfgLotId'])
    target_qty, wire_qty = proposed_qty if proposed_qty is not None else quantity(str(lot['Qty']), '保留批数量')
    target_rate = rate if new_rate is None else new_rate
    if target_qty == qty and target_rate == rate: raise ValueError('维护请求没有实际数量或频率变化。')
    if target_qty < total: raise ValueError('新批数量小于当前完整成员数量合计，不自动触发批量回调或忽略成员。')
    body = {'samplingLot': {'name': lot['MfgLotName'], 'instanceId': lot['MfgLotId']}, 'qty': wire_qty,
            'sampleRate': target_rate, 'allowAdjustToLotSize': adjust, 'comments': marker+(' '+comment if comment else '')}
    preview = await update_preview(body, view, names)
    if await e.lot_view('', lot['MfgLotId']) != view: raise ValueError('维护准备期间批头、成员或任务已变化。')
    await reject_active_sampling_history(lot['MfgLotId'])
    preview.update({'member_quantity_total': str(total), 'quantity_changed': target_qty != qty,
                    'sample_rate_changed': target_rate != rate, 'preserved_allow_adjust_to_lot_size': adjust,
                    'requires_zero_current_sampling_counts': True, 'active_sampling_history_found': False,
                    'historical_ever_sampled_verified': False, 'membership_change_requested': False})
    reads = {'modeling:SamplingPlans:id:'+lot['SamplingPlanId']}
    reads.update('modeling:SampleTests:id:'+r['SampleTestId'] for r in view['tasks'])
    return {'kind': 'lot_maintenance', 'action': 'update', 'name': lot['MfgLotName'], 'target_id': lot['MfgLotId'],
            'scope_key': 'sampling-lot:'+lot['MfgLotName'].casefold(), 'service': 'UpdateSamplingLot', 'body': body,
            'before': view, 'resource_reads': sorted(reads), 'resource_writes': [e.runtime_claim('lot', lot['MfgLotId'])]+
            [e.runtime_claim('container', r['ContainerId']) for r in view['members']],
            'preview': preview, 'marker': marker, 'arguments': args, 'environment': plans.environment()}


@mcp.tool
async def prepare_sampling_lot_update(lot_name: str, new_quantity: str | None = None, new_sample_rate: StrictInt | None = None,
        lot_instance_id: str | None = None, comments: str = '') -> str:
    """准备UpdateSamplingLot数量/采样频率调整，不写MES。至少明确一项实际变化；只支持采样计数全零、无有效采样历史、无完成/通过/处置且任务完整的Active批，最多50成员/50任务。数量用明确正数字符串，频率正整数。保留未修改数值和AllowAdjustToLotSize；拒绝小于成员数量合计、冻结/层级成员、全检或LIMS。非执行预览核对完整成员及原任务，不预测执行后的要求数或质量结果。影响整批，execute_quality_transaction确认后单次提交，unknown不重试。"""
    from tools import quality_plans as plans
    return await plans.prepare('lot_maintenance', dict(lot_name=lot_name, new_quantity=new_quantity,
        new_sample_rate=new_sample_rate, lot_instance_id=lot_instance_id, comments=comments))


@mcp.tool
async def get_sampling_lot_maintenance_history(lot_name: str, lot_instance_id: str | None = None,
        offset: StrictInt = 0, limit: StrictInt = 20) -> str:
    """只读UpdateSamplingLotHistory维护历史当前页。按历史SamplingLotId关联，返回保存数量、频率、汇总评论、事务及冲销状态；不是变化增量或执行后的任务重算证明，不自动成功/解锁。"""
    from tools.quality_history import header_sql, validate_headers, FLAGS
    try:
        q.paging(offset, limit); lot = await lots.lot_record(lot_name, lot_instance_id)
        if not lot: return q.encode({'found': False, 'rows': [], 'complete': False, **FLAGS})
        key = lot['MfgLotId']
        rows = await q.sql_rows(header_sql('lot_maintenance')+f"WHERE s.SamplingLotId='{key}' "
            f'ORDER BY s.UpdateSamplingLotHistoryId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY', limit+1)
        validate_headers(rows)
        if any(r.get('SamplingLotId') != key for r in rows): raise ValueError('维护历史的目标批不一致。')
        more = len(rows) > limit
        return q.encode({'found': True, 'rows': rows[:limit], 'page_complete': True, 'complete': False,
                         'has_more': more, 'next_offset': offset+limit if more else None, **FLAGS})
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return q.encode({'complete': False, **FLAGS, 'error': str(error)})
