"""NCR -> quality event escalation, with explicit organization classification map."""
import copy
import json

from tools import mcp, quality as q, quality_inspection as i, quality_definitions as d
from tools.process_context import ReadContext
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

CONFIG = {'organization': ('Organizations', 'Organization', 'OrganizationId'),
          'classification': ('Classifications', 'Classification', 'ClassificationId'),
          'subclassification': ('SubClassifications', 'SubClassification', 'SubClassificationId'),
          'priority': ('PriorityLevels', 'PriorityLevel', 'PriorityLevelId')}
MAP_FIELDS = ('InstanceID', 'Classification', 'Subclassification', 'ChecklistTemplate',
              'FailureModeGroup', 'Owner', 'Role', 'PEDefault', 'IsFrozen')


async def named_state(kind, name, instance_id=None):
    entity, table, column = CONFIG[kind]
    ref = {'Name': q.text(name)}
    if instance_id is not None:
        if not q.valid_id(instance_id): raise ValueError('配置ID无效。')
        ref['InstanceID'] = instance_id
    ctx = ReadContext(4); root = await ctx.resolve(entity, ref, False); key = root['InstanceID']
    sql = f"SELECT {column},ChangeCount FROM {table} WHERE {column}='{key}'"
    async def token():
        rows = await q.sql_rows(sql, 1)
        if len(rows) != 1 or rows[0].get(column) != key: raise ValueError('质量参考配置变更计数身份不一致。')
        return i.wire_int(rows[0].get('ChangeCount'), '参考配置ChangeCount')
    first = await token()
    state = {field: copy.deepcopy(root.get(field)) for field in ('InstanceID', 'Name', 'Description', 'IsFrozen')}
    if not q.valid_id(key): raise ValueError('参考配置没有有效ID。')
    state['IsFrozen'] = i.wire_bool(state['IsFrozen'], '参考配置冻结状态')
    if kind == 'organization':
        params = {'$select': 'Name,InstanceID,IsFrozen,CollectESigForAllQualityTxns,UserRequiredForEsig,QualityESigTxns,EventClassificationSpecMaps',
                  '$expand': 'EventClassificationSpecMaps'}
        expanded = await ctx.get(entity, key, params)
        rows = expanded.get('EventClassificationSpecMaps')
        if (expanded.get('InstanceID') != key or expanded.get('Name') != state['Name'] or q._has_next_link(expanded)
                or not isinstance(rows, list) or len(rows) > 100):
            raise ValueError('组织分类映射未完整展开或超过100条。')
        seen = set(); values = []
        for row in rows:
            if not isinstance(row, dict) or not q.valid_id(row.get('InstanceID')) or row['InstanceID'] in seen:
                raise ValueError('组织分类映射ID无效或重复。')
            seen.add(row['InstanceID'])
            if row.get('Parent') is not None and q.identifier(row['Parent']) != key:
                raise ValueError('组织分类映射父级不符。')
            for field in ('Classification', 'Subclassification'):
                if not q.valid_id(q.identifier(row.get(field))): raise ValueError('组织分类映射引用不完整：'+field)
            item = {field: copy.deepcopy(row.get(field)) for field in MAP_FIELDS}
            item['IsFrozen'] = i.wire_bool(item['IsFrozen'], '映射冻结状态')
            values.append(item)
        state['EventClassificationSpecMaps'] = sorted(values, key=lambda row: row['InstanceID'])
        state['CollectESigForAllQualityTxns'] = i.wire_bool(expanded.get('CollectESigForAllQualityTxns'), '组织签名设置')
        state['UserRequiredForEsig'] = i.wire_bool(expanded.get('UserRequiredForEsig'), '签名用户设置')
        state['QualityESigTxns'] = copy.deepcopy(expanded.get('QualityESigTxns'))
    if await token() != first: raise ValueError('读取期间参考配置发生变化。')
    return {**state, 'ChangeCount': first}


@mcp.tool
async def get_ncr_escalation_options(organization_name: str) -> str:
    """只读指定组织完整EventClassificationSpecMaps（最多100条），显示允许分类/子分类组合、映射ID及组织电子签名设置。优先级仍用list_quality_references(kind=priority)明确选择。冻结表示配置不可修改，不据此断言不能升级；映射不是目标NCR许可、审批或签名完成证明。"""
    try:
        state = await named_state('organization', organization_name)
        if await named_state('organization', organization_name, state['InstanceID']) != state:
            raise ValueError('选项读取期间组织配置发生变化。')
        return q.encode({'organization': state, 'complete': True, 'target_permission_verified': False,
                         'signature_verified': False, 'quality_verdict': None,
                         'note': '使用实际分类/子分类，不自动选首行；重复组合须先修正组织映射，接口没有可指定映射ID的字段。优先级须另行明确。'})
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return q.encode({'complete': False, 'quality_verdict': None, 'error': str(error)})


async def container_state(key):
    if not q.valid_id(key): raise ValueError('原NCR容器ID无效。')
    rows = await q.sql_rows('SELECT ContainerId,ContainerName,Status,CurrentHoldCount,ChangeCount FROM Container '
                            f"WHERE ContainerId='{key}'", 1)
    if len(rows) != 1 or rows[0].get('ContainerId') != key: raise ValueError('原NCR容器不存在或身份不符。')
    row = rows[0]; q.text(row.get('ContainerName'), '原容器名称')
    for field in ('Status', 'CurrentHoldCount', 'ChangeCount'): row[field] = i.wire_int(row.get(field), field)
    if row['Status'] != 1: raise ValueError('仅升级Active容器关联的NCR；不自动解除冻结。')
    return row


def validate_transport(snapshot):
    body = snapshot['body']; required = {'nonconformanceReport', 'organization', 'classification', 'subClassification', 'priorityLevel', 'comments'}
    if snapshot.get('kind') != 'ncr' or snapshot.get('action') != 'escalate' or set(body) != required:
        raise ValueError('NCR升级服务、计划动作或允许字段不符。')
    for field in required-{'comments'}:
        ref = body[field]
        if not isinstance(ref, dict) or set(ref) != {'name', 'instanceId'} or not q.valid_id(ref.get('instanceId')):
            raise ValueError('NCR升级固定引用无效：'+field)
        q.text(ref.get('name'))
    target = body['nonconformanceReport']
    if target['instanceId'] != snapshot.get('target_id') or target['name'] != snapshot.get('name'):
        raise ValueError('NCR升级目标与已核对记录不符。')
    if not isinstance(body['comments'], str) or not body['comments'].startswith(snapshot['marker']):
        raise ValueError('NCR升级评论缺少原事务标记。')


async def escalation_preview(body, container):
    from tools.quality_plans import wire_matches
    params = {'execute': 'false', '$select': 'NonconformanceReport,Organization,Classification,SubClassification,PriorityLevel,Comments,Container,BriefDescription,Description,EventDetail,ESigRequirement',
              '$expand': 'EventDetail'}
    raw = await request('POST', '/api/NCREscalate', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                        body=body, params=params, response_formatter=raw_json)
    try: outer = json.loads(raw)
    except (ValueError, TypeError): raise ValueError('NCR升级非执行预览失败：'+str(raw)[:1500]) from None
    if not isinstance(outer, dict): raise ValueError('NCR升级响应形状无效。')
    data = outer.get('data', outer)
    if (not isinstance(data, dict) or outer.get('error') or outer.get('ErrorCode')
            or data.get('error') or data.get('ErrorCode') or q._has_next_link(data)
            or not wire_matches(body, data)):
        raise ValueError('NCR升级非执行回显错误、不完整或与明确引用/评论不一致。')
    if (q.identifier(data.get('Container')) != container['ContainerId']
            or (data.get('Container') or {}).get('Name') != container['ContainerName']):
        raise ValueError('NCR升级服务加载的容器与原报告不一致。')
    # EventDetail can be transient, or unavailable before execution. Never bind
    # its generated IDs into a digest, and never treat them as a created event.
    return {'params': params, 'verified_input_echo': body,
            'loaded_container': {'name': container['ContainerName'], 'instanceId': container['ContainerId']},
            'derived_brief_description': data.get('BriefDescription'),
            'signature_requirement': data.get('ESigRequirement'), 'signature_verified': False,
            'dynamic_validation_verified': False, 'created_event_verified': False,
            'note': '仅准备从原NCR升级质量事件；不指定事件名称/类别，不额外调用NCRClose、不覆盖审批/签名或流程字段；原报告最终状态由MES决定。'}


async def build_escalation(args, marker):
    from tools import quality_plans as plans
    from tools.quality_lot_execution import runtime_claim
    name = q.text(args['report_name'], 'NCR名称'); comment = i.inspection_comment(args.get('comments', ''))
    before = await q.ncr_snapshot(name)
    if not before or any(field not in before for field in q.NCR_COLUMNS.split(',')):
        raise ValueError('原NCR不存在或字段不完整。')
    if (i.wire_int(before['NCRStatus'], 'NCR状态') != 1 or before['EscalatedEventId'] not in (None, '')
            or before['CloseDateGMT'] not in (None, '')):
        raise ValueError('只能升级尚未关闭且无已升级事件关联的Open NCR。')
    i.wire_int(before['ChangeCount'], 'NCR变更计数')
    if not before['failure_code_assignments']: raise ValueError('原NCR缺少完整失败代码，不能升级。')
    container = await container_state(before['ContainerId'])
    states = {}
    for kind, arg in [('organization', 'organization_name'), ('classification', 'classification_name'),
                      ('subclassification', 'subclassification_name'), ('priority', 'priority_name')]:
        states[kind] = await named_state(kind, args[arg])
    org = states['organization']
    matches = [row for row in org['EventClassificationSpecMaps']
               if q.identifier(row['Classification']) == states['classification']['InstanceID']
               and q.identifier(row['Subclassification']) == states['subclassification']['InstanceID']]
    if len(matches) != 1: raise ValueError('组织没有唯一的明确分类/子分类映射；重复组合须先修正，不能指定映射ID绕过。')
    body = {'nonconformanceReport': {'name': name, 'instanceId': before['NonconformanceReportId']},
            'comments': marker+(' '+comment if comment else '')}
    for field, kind in [('organization', 'organization'), ('classification', 'classification'),
                        ('subClassification', 'subclassification'), ('priorityLevel', 'priority')]:
        body[field] = {'name': states[kind]['Name'], 'instanceId': states[kind]['InstanceID']}
    context = await escalation_preview(body, container)
    if await q.ncr_snapshot(name, before['NonconformanceReportId']) != before or await container_state(before['ContainerId']) != container:
        raise ValueError('准备升级期间NCR、失败代码或原容器发生变化。')
    for kind, state in states.items():
        if await named_state(kind, state['Name'], state['InstanceID']) != state:
            raise ValueError('准备升级期间组织分类映射或优先级发生变化。')
    context['selected_classification_map'] = matches[0]
    context['organization_signature_settings'] = {k: org[k] for k in ('CollectESigForAllQualityTxns', 'UserRequiredForEsig', 'QualityESigTxns')}
    reads = set()
    for kind, state in states.items(): reads.update(d.resources(CONFIG[kind][0], state))
    return {'kind': 'ncr', 'action': 'escalate', 'service': 'NCREscalate', 'name': name,
            'target_id': before['NonconformanceReportId'], 'scope_key': 'ncr:'+name.casefold(),
            'before': before, 'container_before': container, 'dependencies': states,
            'resource_reads': sorted(reads), 'resource_writes': [runtime_claim('container', before['ContainerId'])],
            'body': body, 'preview': context, 'marker': marker, 'arguments': args, 'environment': plans.environment()}


@mcp.tool
async def prepare_ncr_escalation(report_name: str, organization_name: str, classification_name: str,
        subclassification_name: str, priority_name: str, comments: str = '') -> str:
    """准备NCREscalate将既有Open NCR升级为质量事件，不写MES。明确组织、分类、子分类和优先级；先get_ncr_escalation_options核对组织唯一允许组合，重复组合须先修正配置，不能用映射ID选择。拒绝已关闭/已有升级关联、缺失败码、非Active原容器、断引用或预览不符；不解除冻结，不改原失败码，不指定生成事件名称或覆盖签名/审批/流程字段。execute_quality_transaction确认单次提交，结果unknown；inspect仅证据，须最终审计人工review，不重试、不额外调用关闭服务；原报告最终状态待MES验收。"""
    from tools import quality_plans as plans
    return await plans.prepare('ncr', dict(action='escalate', report_name=report_name, organization_name=organization_name,
        classification_name=classification_name, subclassification_name=subclassification_name,
        priority_name=priority_name, comments=comments))
