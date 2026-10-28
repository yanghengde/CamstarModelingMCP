"""Restricted NCRCloseQuality plans after the linked quality event is closed.

The closed-event prerequisite is this tool's conservative scope, not a claim
that every deployment must call this service manually after closing an event.
"""
import json

from tools import mcp, quality as q, quality_inspection as i, quality_definitions as d
from tools.quality_ncr_escalation import container_state
from tools.process_context import ReadContext
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

EVENT_COLUMNS = 'EventId,EventName,CDOTypeId,Category,Status,OrganizationId,IsFrozen,CloseDateGMT,ClosedById,ChangeCount'


async def event_record(key):
    if not q.valid_id(key): raise ValueError('原NCR升级事件ID无效。')
    rows = await q.sql_rows(f"SELECT TOP 2 {EVENT_COLUMNS} FROM Event WHERE EventId='{key}'", 2)
    if len(rows) != 1 or rows[0].get('EventId') != key or any(k not in rows[0] for k in EVENT_COLUMNS.split(',')):
        raise ValueError('关联Event不存在、不唯一或字段不完整。')
    row = rows[0]; q.text(row.get('EventName'), '关联事件名称')
    for field in ('CDOTypeId', 'Category', 'Status', 'ChangeCount'): row[field] = i.wire_int(row[field], field)
    if row['CDOTypeId'] < 1 or row['Category'] < 1 or not q.valid_id(row['OrganizationId']):
        raise ValueError('关联Event类型、类别或组织未知。')
    row['IsFrozen'] = i.wire_bool(row['IsFrozen'], '事件冻结状态')
    return row


async def resolution_state(name, instance_id=None):
    ref = {'Name': q.text(name, 'NCR解决代码')}
    if instance_id is not None:
        if not q.valid_id(instance_id): raise ValueError('NCR解决代码ID无效。')
        ref['InstanceID'] = instance_id
    root = await ReadContext(3).resolve('NCRResolutionCodes', ref, False)
    key = root['InstanceID']
    if not q.valid_id(key): raise ValueError('NCR解决代码没有有效ID。')
    rows = await q.sql_rows(f"SELECT NCRResolutionCodeId,ChangeCount FROM NCRResolutionCode WHERE NCRResolutionCodeId='{key}'", 1)
    if len(rows) != 1 or rows[0].get('NCRResolutionCodeId') != key: raise ValueError('NCR解决代码变更计数身份不符。')
    return {'InstanceID': key, 'Name': root['Name'], 'Description': root.get('Description'),
            'IsFrozen': i.wire_bool(root.get('IsFrozen'), '解决代码冻结状态'),
            'ChangeCount': i.wire_int(rows[0].get('ChangeCount'), '解决代码变更计数')}


def validate_transport(snapshot):
    body = snapshot['body']; detail = body.get('serviceDetail'); ref = body.get('nonconformanceReport')
    if (snapshot.get('kind') != 'ncr' or snapshot.get('action') != 'quality_close'
            or set(body) != {'nonconformanceReport', 'serviceDetail', 'comments'}
            or not isinstance(ref, dict) or set(ref) != {'name', 'instanceId'}
            or ref.get('instanceId') != snapshot.get('target_id') or ref.get('name') != snapshot.get('name')
            or not q.valid_id(ref.get('instanceId')) or not isinstance(detail, dict)
            or not {'ncrResolutionCode'} <= set(detail) or set(detail)-{'ncrResolutionCode', 'ncrComment'}):
        raise ValueError('质量侧NCR关闭服务、原报告或允许字段不符。')
    code = detail['ncrResolutionCode']
    if not isinstance(code, dict) or set(code) != {'name', 'instanceId'} or not q.valid_id(code.get('instanceId')):
        raise ValueError('质量侧NCR关闭解决代码引用无效。')
    q.text(code.get('name'), '解决代码')
    if code['instanceId'] != snapshot['resolution_before']['InstanceID'] or code['name'] != snapshot['resolution_before']['Name']:
        raise ValueError('质量侧NCR关闭解决代码与原配置快照不符。')
    if (snapshot['before']['NonconformanceReportId'] != snapshot['target_id']
            or i.wire_int(snapshot['before']['NCRStatus'], '原NCR状态') != 1
            or snapshot['before']['EscalatedEventId'] != snapshot['event_before']['EventId']
            or snapshot['event_before']['Status'] != 5):
        raise ValueError('原升级事件关联或Closed快照不一致。')
    if not isinstance(body['comments'], str) or not body['comments'].startswith(snapshot['marker']):
        raise ValueError('质量侧NCR关闭事务评论不符。')
    if 'ncrComment' in detail:
        from tools.quality_plans import note
        note(detail['ncrComment'], 'NCR备注')


async def close_preview(body, container):
    from tools.quality_plans import wire_matches
    params = {'execute': 'false', '$select': 'NonconformanceReport,ServiceDetail,Container,Comments,ESigRequirement',
              '$expand': 'ServiceDetail($select=NCRResolutionCode,NCRComment)'}
    raw = await request('POST', '/api/NCRCloseQuality', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                        body=body, params=params, response_formatter=raw_json)
    try: outer = json.loads(raw)
    except (ValueError, TypeError): raise ValueError('质量侧NCR关闭非执行预览失败：'+str(raw)[:1500]) from None
    if not isinstance(outer, dict): raise ValueError('质量侧NCR关闭预览形状无效。')
    data = outer.get('data', outer)
    if (not isinstance(data, dict) or outer.get('error') or outer.get('ErrorCode')
            or data.get('error') or data.get('ErrorCode') or q._has_next_link(data)
            or not wire_matches(body, data)):
        raise ValueError('质量侧NCR关闭原报告、解决代码、备注或完整回显不符。')
    if (q.identifier(data.get('Container')) != container['ContainerId']
            or (data.get('Container') or {}).get('Name') != container['ContainerName']):
        raise ValueError('质量侧NCR关闭加载的原容器不符。')
    # No unpublished dynamic CDOTypeId requirement; use the endpoint's declared
    # NCRCloseQualityDetail contract and exact input echo. Transient IDs ignored.
    return {'params': params, 'verified_input_echo': body, 'detail_contract': 'NCRCloseQualityDetail',
            'signature_requirement': data.get('ESigRequirement'), 'signature_verified': False,
            'dynamic_validation_verified': False, 'ncr_closed_verified': False,
            'linked_event_mutation_requested': False,
            'note': '仅对原关联质量事件已Closed而仍Open的NCR准备收尾。Closed前置不是权限/审批/签名证明；实际NCR状态、历史及其他原生副作用待验收。'}


async def build_quality_close(args, marker):
    from tools import quality_plans as plans
    from tools.quality_lot_execution import runtime_claim
    name = q.text(args['report_name'], 'NCR名称'); comment = i.inspection_comment(args.get('comments', ''))
    before = await q.ncr_snapshot(name)
    if not before or any(field not in before for field in q.NCR_COLUMNS.split(',')):
        raise ValueError('原NCR不存在或字段不完整。')
    if i.wire_int(before['NCRStatus'], 'NCR状态') != 1 or before['CloseDateGMT'] not in (None, ''):
        raise ValueError('只对仍Open且未记录关闭时间的NCR准备质量侧收尾。')
    i.wire_int(before['ChangeCount'], 'NCR变更计数')
    if not before['failure_code_assignments']: raise ValueError('原NCR失败代码未完整确认。')
    linked = await event_record(before['EscalatedEventId'])
    # This deliberately narrow policy leaves event approval/closure to MES.
    if (linked['Status'] != 5 or not isinstance(linked['CloseDateGMT'], str) or not linked['CloseDateGMT'].strip()
            or not q.valid_id(linked['ClosedById'])):
        raise ValueError('本工具仅处理关联Event已Closed=5且有关闭时间/关闭人的NCR；不替代质量事件关闭或审批。')
    container = await container_state(before['ContainerId'])
    code = await resolution_state(args['resolution_code_name'])
    detail = {'ncrResolutionCode': {'name': code['Name'], 'instanceId': code['InstanceID']}}
    if args.get('ncr_comment') is not None: detail['ncrComment'] = plans.note(args['ncr_comment'], 'NCR备注')
    body = {'nonconformanceReport': {'name': name, 'instanceId': before['NonconformanceReportId']},
            'serviceDetail': detail, 'comments': marker+(' '+comment if comment else '')}
    context = await close_preview(body, container)
    if (await q.ncr_snapshot(name, before['NonconformanceReportId']) != before
            or await event_record(linked['EventId']) != linked
            or await container_state(before['ContainerId']) != container
            or await resolution_state(code['Name'], code['InstanceID']) != code):
        raise ValueError('准备质量侧关闭期间原报告、关联事件、容器或解决代码发生变化。')
    return {'kind': 'ncr', 'action': 'quality_close', 'service': 'NCRCloseQuality', 'name': name,
            'target_id': before['NonconformanceReportId'], 'scope_key': 'ncr:'+name.casefold(),
            'before': before, 'event_before': linked, 'container_before': container, 'resolution_before': code,
            'resource_reads': sorted(d.resources('NCRResolutionCodes', code) | {'runtime:quality-event:id:'+linked['EventId'].lower()}),
            'resource_writes': [runtime_claim('container', before['ContainerId'])],
            'body': body, 'preview': context, 'marker': marker, 'arguments': args, 'environment': plans.environment()}


async def inspect_snapshot(snapshot):
    report = await q.ncr_snapshot('', snapshot['target_id'])
    original_event_id = snapshot['event_before']['EventId']
    event = await event_record(original_event_id)
    if await event_record(original_event_id) != event: raise ValueError('检查期间原关联事件发生变化。')
    return {'current_record': report, 'original_linked_event_current_record': event,
            'current_report_points_to_original_event': bool(report and report.get('EscalatedEventId') == original_event_id),
            'verified_success': False, 'state_changed': False, 'retry_allowed': False, 'history_correlation_verified': False,
            'note': '当前报告关闭或原事件仍Closed都不证明本次NCRCloseQuality成功；不跟随被改写的事件关联，不自动解锁或重试。'}


@mcp.tool
async def prepare_quality_ncr_closure(report_name: str, resolution_code_name: str,
        comments: str = '', ncr_comment: str | None = None) -> str:
    """准备正式NCRCloseQuality质量侧收尾，不写MES。保守范围：原NCR仍Open、失败码完整、原容器Active，且原EscalatedEvent关联的Event已Closed=5、有关闭时间和关闭人。明确NCR解决代码，可明确NCR备注；不指定新事件、不关闭/修改质量事件、不解冻、不覆盖审批或签名。精确非执行回显及原对象重读。execute_quality_transaction确认单次执行，结果unknown；inspect只给原报告/原事件证据，最终审计人工review，不重试。普通NCR关闭仍用prepare_nonconformance_report(close)，不自动换服务。"""
    from tools import quality_plans as plans
    return await plans.prepare('ncr', dict(action='quality_close', report_name=report_name,
        resolution_code_name=resolution_code_name, comments=comments, ncr_comment=ncr_comment))
