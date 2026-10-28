"""Typed native LossDetails plans; SamplingLot scrap remains blocked by contract conflict."""
import copy
import json
from decimal import localcontext

from tools import mcp, quality as q, quality_plans as p, quality_inspection as i
from tools import quality_lot_execution as e, quality_definitions as d
from tools.process_context import ReadContext
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

ASSETS = {'Specs': ('Spec', 'SpecId', True), 'Operations': ('Operation', 'OperationId', False),
          'LossReasons': ('LossReason', 'LossReasonId', False),
          'LossReasonGroups': ('LossReasonGroup', 'LossReasonGroupId', False), 'UOMs': ('UOM', 'UOMId', False)}
COLUMNS = ('c.ContainerId,c.ContainerName,c.CDOTypeId,c.Status,c.CurrentHoldCount,c.ChangeCount,c.ProductId,'
           'c.SamplingLotId,c.ParentContainerId,c.ChildCount,c.Qty,c.Qty2,c.UOMId,c.UOM2Id,c.CurrentStatusId,'
           's.CurrentStatusId AS StatusRecordId,s.CarrierId,s.InRework,s.SpecId,s.WorkflowStepId')
FIELDS = {'ContainerId', 'ContainerName', 'CDOTypeId', 'Status', 'CurrentHoldCount', 'ChangeCount', 'ProductId',
          'SamplingLotId', 'ParentContainerId', 'ChildCount', 'Qty', 'Qty2', 'UOMId', 'UOM2Id', 'CurrentStatusId',
          'StatusRecordId', 'CarrierId', 'InRework', 'SpecId', 'WorkflowStepId'}


class Dependencies:
    def __init__(self):
        self.ctx = ReadContext(40); self.states = {}; self.refs = []

    async def asset(self, entity, ref):
        table, column, revisioned = ASSETS[entity]
        root = await self.ctx.resolve(entity, ref, revisioned); key = root.get('InstanceID')
        if not q.valid_id(key): raise ValueError('数量处置配置ID无效。')
        if revisioned and i.wire_int(root.get('Status'), 'Spec状态') != 1: raise ValueError('当前Spec不是Active固定版本。')
        async def token():
            rows = await q.sql_rows(f"SELECT {column},ChangeCount FROM {table} WHERE {column}='{key}'", 1)
            if len(rows) != 1 or rows[0].get(column) != key: raise ValueError('数量处置配置变更计数身份不符。')
            return i.wire_int(rows[0].get('ChangeCount'), '配置变更计数')
        first = await token()
        if entity == 'LossReasonGroups':
            expanded = await self.ctx.get(entity, key, {'$expand': 'ResolvedEntries'})
            entries = expanded.get('ResolvedEntries')
            if (expanded.get('InstanceID') != key or expanded.get('Name') != root.get('Name') or q._has_next_link(expanded)
                    or not isinstance(entries, list) or len(entries) > 100):
                raise ValueError('LossReason组未完整展开或超过100项。')
            if (any(not q.valid_id(q.identifier(v)) for v in entries)
                    or len({q.identifier(v) for v in entries}) != len(entries)):
                raise ValueError('LossReason组引用缺失或重复。')
            root = expanded
        if await token() != first: raise ValueError('读取数量处置配置期间发生变化。')
        pair = (entity, key)
        state = {'entity': entity, 'InstanceID': key, 'Name': q.text(root.get('Name')), 'Revision': root.get('Revision'),
                 'ChangeCount': first, 'content_digest': p.digest(root)}
        if pair in self.states and self.states[pair] != state: raise ValueError('数量处置配置读取不一致。')
        self.states[pair] = state; self.refs.append((entity, copy.deepcopy(ref), pair))
        return root

    async def stable(self):
        fresh = Dependencies()
        for entity, ref, pair in self.refs:
            root = await fresh.asset(entity, ref)
            if (entity, root['InstanceID']) != pair or fresh.states[pair] != self.states[pair]:
                raise ValueError('准备期间Spec、Operation、损失原因/组或单位发生变化。')


async def record(cid):
    if not q.valid_id(cid): raise ValueError('数量处置容器ID无效。')
    rows = await q.sql_rows(f'SELECT {COLUMNS} FROM Container c INNER JOIN CurrentStatus s '
                            f"ON s.CurrentStatusId=c.CurrentStatusId WHERE c.ContainerId='{cid}'", 1)
    if not rows: return None
    if len(rows) != 1 or rows[0].get('ContainerId') != cid or not FIELDS <= rows[0].keys():
        raise ValueError('数量处置容器/当前状态记录不完整。')
    row = dict(rows[0])
    for field in ('CDOTypeId', 'Status', 'CurrentHoldCount', 'ChangeCount', 'ChildCount'):
        row[field] = i.wire_int(row[field], field)
    row['InRework'] = i.wire_bool(row['InRework'], '在返工标志')
    if not q.valid_id(row['CurrentStatusId']) or row['StatusRecordId'] != row['CurrentStatusId']:
        raise ValueError('容器当前状态持久身份不符。')
    return row


async def snapshot(name, cid=None):
    ref, status = await q.container_identity(name, cid)
    row = await record(ref['instanceId'])
    if row is None or row['ContainerName'] != ref['name']: raise ValueError('数量处置容器名称/ID不一致。')
    if row['CDOTypeId'] != 1040: raise ValueError('第一阶段仅支持基础Container类型，不支持Batch或其他子类型。')
    if row['Status'] != 1 or row['CurrentHoldCount'] != 0 or row['InRework'] is not False:
        raise ValueError('数量处置要求Active、明确无冻结且未在返工。')
    if row['SamplingLotId']: raise ValueError('抽样批成员不能通过单容器损失工具替代整批报废。')
    if row['ParentContainerId'] or row['ChildCount'] != 0 or row['CarrierId']:
        raise ValueError('第一阶段不处理父子容器或已绑定载具的数量处置。')
    if row['UOM2Id'] or row['Qty2'] not in (None, '') and i.numeric(str(row['Qty2']), '第二数量') != 0:
        raise ValueError('第一阶段不处理第二单位/非零第二数量。')
    if not q.valid_id(row['UOMId']) or not q.valid_id(row['ProductId']): raise ValueError('主单位或产品身份未确认。')
    if i.numeric(str(row['Qty']), '当前数量') <= 0: raise ValueError('当前数量必须为正。')
    if q.identifier(status.get('Spec')) != row['SpecId'] or q.identifier(status.get('Step')) != row['WorkflowStepId']:
        raise ValueError('Query运行工序/步骤与持久当前状态不一致。')
    if status.get('UOM') and q.identifier(status['UOM']) != row['UOMId']: raise ValueError('当前单位与持久记录不符。')
    if 'InRework' in status and i.wire_bool(status['InRework'], 'Query返工标志') != row['InRework']:
        raise ValueError('当前返工标志读取不一致。')
    return ref, row, e.status_snapshot(status)


def quantity_options(args, row):
    for name in ('record_all_quantity', 'close_when_zero', 'adjust_throughput'):
        if type(args.get(name)) is not bool: raise ValueError(name+'必须为明确布尔值。')
    all_qty = args['record_all_quantity']; requested = args.get('loss_quantity')
    if all_qty == (requested is not None): raise ValueError('全量损失与明确loss_quantity必须且只能选择一种。')
    old = i.numeric(str(row['Qty']), '原数量')
    if old > 1e12 or i.numeric(str(float(old)), '原数量传输精度') != old:
        raise ValueError('当前数量超过1e12或不能无损表示为JSON数值。')
    amount = old if all_qty else i.numeric(requested, '本次损失量')
    if not 0 < amount <= old or amount > 1e12: raise ValueError('本次损失量必须为正、<=当前数量且<=1e12，不截断。')
    if amount == old and not all_qty: raise ValueError('全量损失须明确record_all_quantity=true，不以精确数量隐式代替。')
    number = float(amount)
    if i.numeric(str(number), '传输数量') != amount: raise ValueError('数量不能无损传输为JSON数值，不自动舍入。')
    with localcontext() as context:
        context.prec = 640
        remaining = old-amount
    if i.numeric(str(float(remaining)), '预计余额传输精度') != remaining:
        raise ValueError('预计余额不能无损表示，不接受会被舍入或忽略的微小扣量。')
    return amount, remaining, number


async def preview(body, current):
    raw = await request('POST', '/api/ChangeQty', base_url=CAMSTAR_SHOPFLOOR_BASE_URL, body=body,
                        params={'execute': 'false', '$select': 'Container,CloseWhenZero,DisassociateWhenClosed,GenerateRollups,RecordAllQtySummaryOnly,Comments,ServiceDetails,ESigRequirement',
                                '$expand': 'ServiceDetails'}, response_formatter=raw_json)
    try: outer = json.loads(raw)
    except (ValueError, TypeError): raise ValueError('数量损失非执行预览失败：'+str(raw)[:1000]) from None
    if not isinstance(outer, dict): raise ValueError('数量损失预览形状无效。')
    data = outer.get('data', outer)
    if (not isinstance(data, dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error') or data.get('ErrorCode')
            or q._has_next_link(data)):
        raise ValueError('数量损失预览错误或不完整。')
    root = {k: v for k, v in body.items() if k != 'serviceDetails'}
    if not p.wire_matches(root, data): raise ValueError('数量损失根字段/目标回显不符。')
    rows = data.get('ServiceDetails')
    if not isinstance(rows, list) or len(rows) != 1: raise ValueError('数量损失必须完整回显唯一明细。')
    line = rows[0]; expected = {k: v for k, v in body['serviceDetails'][0].items() if k not in ('@odata.type', 'qty')}
    if (not p.wire_matches(expected, line) or str(line.get('CDOTypeId')) != '4030'
            or str(line.get('QtyMultiplier')) != '-1' or i.wire_bool(line.get('CountsAgainstProduction'), '计入生产标志') is not True
            or not p.wire_matches({'container': body['container']}, line)):
        raise ValueError('必须回显LossDetails类型4030、损失方向-1、原因/目标和计入生产标志，普通类型4010不能代替。')
    if 'qty' in body['serviceDetails'][0] and i.numeric(str(line.get('Qty')), '回显数量') != i.numeric(str(body['serviceDetails'][0]['qty']), '请求数量'):
        raise ValueError('本次损失量回显不符。')
    if line.get('Qty2') not in (None, '') and i.numeric(str(line['Qty2']), '回显第二数量') != 0:
        raise ValueError('数量损失出现非预期第二数量。')
    if line.get('IsDeleted') is not None and i.wire_bool(line['IsDeleted'], '明细删除标志') is not False:
        raise ValueError('数量损失明细被删除。')
    if line.get('ChargeToStep') and q.identifier(line['ChargeToStep']) != current['WorkflowStepId']:
        raise ValueError('数量损失计入步骤与当前步骤不同。')
    return {'verified_input_echo': body, 'native_detail_type': 4030, 'native_quantity_multiplier': -1,
            'native_counts_against_production': True, 'signature_requirement': data.get('ESigRequirement'),
            'server_custom_fields': {k: line[k] for k in ('ES_Scrapped',) if k in line}, 'custom_scrap_process_verified': False,
            'dynamic_validation_verified': False, 'quantity_effects_verified': False, 'signature_verified': False,
            'note': '单容器原生LossDetails；全量由RecordAllQty处理，预览不是实际扣量、关闭或质量批准。固定不生成层级rollup/不解关联；MES其他生产/历史/采样影响待验收。'}


async def build_loss(args, marker):
    comment = i.inspection_comment(args.get('comments', ''))
    ref, row, status = await snapshot(args['container_name'], args.get('container_instance_id'))
    amount, after, number = quantity_options(args, row)
    dep = Dependencies(); spec = await dep.asset('Specs', status.get('Spec'))
    operation = await dep.asset('Operations', spec.get('Operation'))
    if operation['InstanceID'] != q.identifier(status.get('Operation')): raise ValueError('当前Operation与Spec配置不符。')
    reason = await dep.asset('LossReasons', {'Name': q.text(args['loss_reason_name'], '损失原因')})
    group = await dep.asset('LossReasonGroups', operation.get('LossReasons'))
    if sum(q.identifier(v) == reason['InstanceID'] and v.get('Name') == reason['Name'] for v in group['ResolvedEntries']) != 1:
        raise ValueError('明确LossReason不属于当前Operation完整LossReasons组，不能使用ScrapReason或按同名替换。')
    unit = await dep.asset('UOMs', {'InstanceId': row['UOMId']})
    reason_ref = {'name': reason['Name'], 'instanceId': reason['InstanceID']}
    detail = {'@odata.type': '#shopfloor.LossDetails', 'changeQtyType': 2, 'recordAllQty': args['record_all_quantity'],
              'adjustThruput': args['adjust_throughput'], 'reasonCode': reason_ref}
    if not args['record_all_quantity']: detail['qty'] = number
    body = {'container': ref, 'closeWhenZero': args['close_when_zero'], 'disassociateWhenClosed': False,
            'generateRollups': False, 'recordAllQtySummaryOnly': False, 'serviceDetails': [detail],
            'comments': marker+(' '+comment if comment else '')}
    context = await preview(body, row)
    if await snapshot(ref['name'], ref['instanceId']) != (ref, row, status): raise ValueError('准备期间容器、数量、单位或当前步骤发生变化。')
    await dep.stable()
    context.update(loss_quantity=str(amount), current_quantity=str(row['Qty']), projected_remaining_quantity=str(after),
                   projected_close=after == 0 and args['close_when_zero'], unit={'name': unit['Name'], 'instanceId': unit['InstanceID']},
                   projection_is_execution_result=False, quality_verdict=None)
    reads = set()
    for (entity, _), state in dep.states.items(): reads.update(d.resources(entity, state))
    return {'kind': 'quantity_loss', 'action': 'loss', 'service': 'ChangeQty', 'name': ref['name'], 'target_id': ref['instanceId'],
            'scope_key': 'quantity-loss:'+ref['instanceId'], 'reason': reason_ref, 'expected_loss_quantity': str(amount),
            'before': {'container': row, 'container_status': status}, 'dependencies': list(dep.states.values()),
            'resource_reads': sorted(reads), 'resource_writes': [e.runtime_claim('container', ref['instanceId'])],
            'body': body, 'preview': context, 'arguments': args, 'marker': marker, 'environment': p.environment()}


def validate_transport(snap):
    body = snap['body']; args = snap['arguments']
    if (snap.get('kind') != 'quantity_loss' or snap.get('action') != 'loss' or snap.get('service') != 'ChangeQty'
            or set(body) != {'container', 'closeWhenZero', 'disassociateWhenClosed', 'generateRollups', 'recordAllQtySummaryOnly', 'serviceDetails', 'comments'}):
        raise ValueError('数量损失服务、动作或根字段不符。')
    if not q.valid_id(snap.get('target_id')) or body['container'] != {'name': snap['name'], 'instanceId': snap['target_id']}:
        raise ValueError('数量损失目标与原计划不符。')
    for field in ('disassociateWhenClosed', 'generateRollups', 'recordAllQtySummaryOnly'):
        if body[field] is not False: raise ValueError('数量损失不开放层级/解关联覆盖。')
    if type(body['closeWhenZero']) is not bool or body['closeWhenZero'] != args['close_when_zero']:
        raise ValueError('数量归零关闭选项与原计划不符。')
    rows = body['serviceDetails']
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict): raise ValueError('数量损失仅接受一条LossDetails。')
    line = rows[0]; all_qty = args['record_all_quantity']
    keys = {'@odata.type', 'changeQtyType', 'recordAllQty', 'adjustThruput', 'reasonCode'} | (set() if all_qty else {'qty'})
    if (set(line) != keys or line['@odata.type'] != '#shopfloor.LossDetails' or type(line['changeQtyType']) is not int
            or line['changeQtyType'] != 2 or type(line['recordAllQty']) is not bool or line['recordAllQty'] != all_qty
            or type(line['adjustThruput']) is not bool or line['adjustThruput'] != args['adjust_throughput']
            or line['reasonCode'] != snap['reason']):
        raise ValueError('数量损失类型、数量模式、生产数量选项或原因不符。')
    if not all_qty and (type(line['qty']) not in (int, float) or i.numeric(str(line['qty']), '传输数量') != i.numeric(snap['expected_loss_quantity'], '原损失量')):
        raise ValueError('损失量与原计划不符。')
    marker = snap.get('marker'); comment = body['comments']
    if not isinstance(marker, str) or not isinstance(comment, str) or not (comment == marker or comment.startswith(marker+' ')):
        raise ValueError('数量损失缺少原关联标记。')


async def inspect_snapshot(snap):
    return {'original_container_id': snap['target_id'], 'current_record': await record(snap['target_id']),
            'expected_loss_quantity': snap['expected_loss_quantity'], 'verified_success': False, 'state_changed': False,
            'retry_allowed': False, 'history_correlation_verified': False,
            'note': '只读原容器ID；当前数量或Closed状态不证明本次损失已提交，不自动解锁，不将未知请求重新扣量。最终历史因果链待核实。'}


@mcp.tool
async def prepare_container_quantity_loss(container_name: str, loss_reason_name: str, close_when_zero: bool,
        adjust_throughput: bool, loss_quantity: str | None = None, record_all_quantity: bool = False,
        container_instance_id: str | None = None, comments: str = '') -> str:
    """只准备单个基础Container的ChangeQty/LossDetails数量损失或全量报废计划，不写MES。loss_quantity为当前主单位的明确正数增量；全量须record_all_quantity=true且省略loss_quantity，不混用。必须明确close_when_zero（归零是否关闭）和adjust_throughput（是否调整已处理生产数量），不替用户选择。仅Active无冻结、未返工、无父子/载具、无第二单位/非零第二数量、主单位明确且不属于SamplingLot；拒绝Batch子类型和抽样批成员，不循环扣量模拟整批报废。原因是当前Operation完整LossReasons组中的LossReason，不能用ScrapReason替代。强制OData LossDetails子类型并核对4030/-1/计入生产回显；重读数量/工序/单位/配置，统一确认单次执行；unknown不重试，inspect仅证据，最终效果/历史/签名待验收。"""
    return await p.prepare('quantity_loss', dict(container_name=container_name, loss_reason_name=loss_reason_name,
        close_when_zero=close_when_zero, adjust_throughput=adjust_throughput, loss_quantity=loss_quantity,
        record_all_quantity=record_all_quantity, container_instance_id=container_instance_id, comments=comments))


@mcp.tool
async def get_sampling_lot_scrap_readiness(lot_name: str, lot_instance_id: str | None = None) -> str:
    """只读抽样批报废前置与接口阻塞，不创建可执行计划、不判合格。完整最多50成员/50任务，重读一致；展示原生会跳过的非Active/零数量成员，数量逐成员原样展示，不混算或推断单位。标准csi分支存在但ScrapReason标注未实现、与LossDetails要求的LossReason不同，部署映射/全量扣量/关闭/清理及原子性未验证，ready_for_execution固定false。不能改走PartScrap或循环调用单容器ChangeQty替代整批报废。"""
    try:
        view = await e.lot_view(lot_name, lot_instance_id); issues = []
        for member in view['members']:
            if i.wire_int(member.get('Status'), '成员状态') != 1 or i.numeric(str(member.get('Qty')), '成员数量') <= 0:
                issues.append({'container_id': member['ContainerId'], 'code': 'native_branch_would_skip_member'})
        if await e.lot_view('', view['lot']['MfgLotId']) != view: raise ValueError('核查期间抽样批、完整成员或任务发生变化。')
        return q.encode({'complete': True, 'ready_for_execution': False, 'plan_created': False, 'mes_write_performed': False,
            'contract_review_date': '2026-10-28', 'contract_is_live_health_check': False,
            'quality_verdict': None, 'view': view, 'member_issues': issues, 'native_batch_branch_observed': True,
            'native_batch_branch_runtime_verified': False, 'blockers': [
                {'code': 'scrap_entry_declared_unimplemented', 'detail': 'ExecuteSamplingLot.ScrapReason仍标注当前版本未实现。'},
                {'code': 'scrap_and_loss_reason_types_differ', 'detail': '入口ScrapReason与原生LossDetails.ReasonCode要求的LossReason不是同一类型，不能按同名互换。'},
                {'code': 'deployed_batch_effects_unverified', 'detail': '部署覆盖、整批原子性、全量扣量/关闭、成员抽样明细清理及最终历史未核实。'}],
            'automatic_fallback': False, 'note': '不生成结果3计划；PartScrap是资源相关事务，单容器ChangeQty拒绝抽样批成员。'})
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return q.encode({'complete': False, 'ready_for_execution': False, 'plan_created': False, 'mes_write_performed': False, 'error': str(error)})
