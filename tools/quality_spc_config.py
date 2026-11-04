"""Bounded inspection of one chart's persisted native SPC dependencies."""
from tools import quality as q, quality_inspection as inspection, quality_spc_annotations as annotations
from tools.collection_submission import digest


DEPENDENCIES = {
    'chart_type': ('SPCChartTypeId', 'A_SPCChart', 'SPCChartId'),
    'rules': ('SPCRulesId', 'A_SPCRules', 'SPCRulesId'),
    'connection': ('SPCConnectionId', 'A_SPCConnection', 'SPCConnectionId'),
    'query': ('SPCQueryId', 'UserQuery', 'UserQueryId'),
}
# SPCQuery shares UserQuery storage with other query types in the standard model.
SPC_QUERY_CDO_TYPE = 4751424
CHART_FLAGS = ('IsFrozen', 'RecordAgainstCustomSPCDataSrc', 'TriggerOCAPs', 'RequireAnnotation')
CHART_FIELDS = (
    'SPCChartDefId,SPCChartDefName,ChangeCount,SPCChartTypeId,SPCRulesId,SPCQueryId,SPCConnectionId,'
    'IsFrozen,RecordAgainstCustomSPCDataSrc,TriggerOCAPs,RequireAnnotation,'
    "CASE WHEN NULLIF(EmailNotification,'') IS NULL AND NULLIF(EMailMessageId,'') IS NULL THEN 0 ELSE 1 END AS NotificationConfigured,"
    "CASE WHEN NULLIF(PutContainerOnHoldId,'') IS NULL THEN 0 ELSE 1 END AS HoldConfigured,"
    "CASE WHEN NULLIF(SetResourceStatusId,'') IS NULL THEN 0 ELSE 1 END AS ResourceStatusConfigured,"
    "CASE WHEN NULLIF(ChartProperties,'') IS NULL THEN 0 ELSE 1 END AS RulePathConfigured"
)
CHART_PRESENCE_FLAGS = ('NotificationConfigured', 'HoldConfigured', 'ResourceStatusConfigured', 'RulePathConfigured')
COLLECTIONS = {
    'chart_parameters': ('SPCChartParams', 'SPCChartParamsId'),
    'query_parameters': ('SPCChartQueryParams', 'SPCChartQueryParamsId'),
    'rule_members': ('SPCRuleViolations', 'SPCRulesViolationsId'),
}


def optional_id(value, label):
    return None if value in ('', None) else annotations.iid(value, label)


def binary_flag(value, label):
    parsed = inspection.wire_int(value, label)
    if parsed not in (0, 1):
        raise ValueError(label+'须为0或1。')
    return parsed == 1


async def read_chart(name, chart_id):
    where = 'SPCChartDefId='+q.literal(chart_id) if chart_id else 'SPCChartDefName='+q.literal(name)
    rows = await q.sql_rows('SELECT TOP 2 '+CHART_FIELDS+' FROM SPCChartDef WHERE '+where, 2)
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('SPC图名称或持久ID未唯一解析。')
    row = rows[0]
    key = annotations.iid(row['SPCChartDefId'], 'SPC图ID')
    actual_name = q.text(row['SPCChartDefName'], 'SPC图名称')
    if (chart_id is not None and key != chart_id) or (name is not None and actual_name != name):
        raise ValueError('SPC图的名称与持久ID不符。')
    return {
        'id': key, 'name': actual_name, 'change_count': inspection.wire_int(row['ChangeCount'], 'SPC图变更计数'),
        'references': {field: optional_id(row[field],field)
                       for field in ('SPCChartTypeId','SPCRulesId','SPCQueryId','SPCConnectionId')},
        'flags': {field: inspection.wire_bool(row[field],field,nullable=True) for field in CHART_FLAGS}
                 | {field: binary_flag(row[field],field) for field in CHART_PRESENCE_FLAGS},
    }


async def read_dependency(name, key):
    if key is None:
        return {'state': 'not_configured'}
    _, table, field = DEPENDENCIES[name]
    extra = {
        'connection': ",CASE WHEN NULLIF(SPCApiUrl,'') IS NULL THEN 0 ELSE 1 END AS EndpointConfigured",
        'query': ',CDOTypeId',
    }.get(name, '')
    rows = await q.sql_rows(f'SELECT TOP 2 {field},ChangeCount,IsFrozen{extra} FROM {table} WHERE {field}='+q.literal(key), 2)
    if not rows:
        return {'id': key, 'state': 'missing_reference'}
    if len(rows) != 1 or annotations.iid(rows[0][field],field) != key:
        raise ValueError('SPC配置依赖持久身份不符。')
    row = rows[0]
    if name == 'query' and inspection.wire_int(row['CDOTypeId'], 'SPC查询类型') != SPC_QUERY_CDO_TYPE:
        return {'id': key, 'state': 'wrong_type'}
    result = {'id': key, 'state': 'present', 'change_count': inspection.wire_int(row['ChangeCount'],field),
              'is_frozen': inspection.wire_bool(row['IsFrozen'],field,nullable=True)}
    if name == 'connection':
        result['endpoint_configured'] = binary_flag(row['EndpointConfigured'], 'SPC引擎地址配置')
    return result


async def read_members(name, parent):
    if parent is None:
        return None
    table, field = COLLECTIONS[name]
    extra = {
        'chart_parameters': ',m.IsDynamic',
        'query_parameters': ",CASE WHEN NULLIF(m.DynamicValue,'') IS NULL THEN 0 ELSE 1 END AS DynamicConfigured",
        'rule_members': ',m.SPCViolationId,v.SPCRuleViolationId,v.ChangeCount AS ViolationChangeCount,v.IsFrozen AS ViolationFrozen',
    }[name]
    join = ' LEFT JOIN SPCViolation v ON v.SPCRuleViolationId=m.SPCViolationId' if name == 'rule_members' else ''
    rows = await q.sql_rows(f'SELECT TOP 101 m.{field},m.ParentId,m.ChangeCount,m.IsFrozen{extra} FROM {table} m'+join
                            +' WHERE m.ParentId='+q.literal(parent)+f' ORDER BY m.{field}', 101)
    if len(rows) > 100:
        raise ValueError('SPC配置清单超过100项，不能使用部分清单。')
    members = {}
    for row in rows:
        key = annotations.iid(row[field],field)
        if annotations.iid(row['ParentId']) != parent or key in members:
            raise ValueError('SPC配置成员父关联不符或重复。')
        members[key] = {'id': key, 'change_count': inspection.wire_int(row['ChangeCount'],field),
                        'is_frozen': inspection.wire_bool(row['IsFrozen'],field,nullable=True)}
        if name == 'rule_members':
            violation = annotations.iid(row['SPCViolationId'], '规则违例定义ID')
            resolved = optional_id(row['SPCRuleViolationId'], '违例定义持久ID')
            if resolved is not None and resolved != violation:
                raise ValueError('规则成员与违例定义持久身份不符。')
            members[key].update(violation_id=violation, violation_reference_resolved=resolved is not None)
            if resolved is not None:
                members[key]['violation_change_count'] = inspection.wire_int(row['ViolationChangeCount'],'违例定义变更计数')
                members[key]['violation_is_frozen'] = inspection.wire_bool(row['ViolationFrozen'],'违例定义冻结状态',nullable=True)
        elif name == 'chart_parameters':
            members[key]['dynamic'] = inspection.wire_bool(row['IsDynamic'],'图参数动态标志',nullable=True)
        else:
            members[key]['dynamic'] = binary_flag(row['DynamicConfigured'], '查询参数动态配置')
    return list(members.values())


async def snapshot(chart):
    references = chart['references']
    dependencies = {}
    for name, (field, _, _) in DEPENDENCIES.items():
        dependencies[name] = await read_dependency(name, references[field])
    members = {
        'chart_parameters': await read_members('chart_parameters', chart['id']),
        'query_parameters': await read_members('query_parameters', chart['id']),
        'rule_members': await read_members('rule_members', references['SPCRulesId']),
    }
    return {'chart': chart, 'dependencies': dependencies, 'members': members}


async def chart_readiness(name, chart_id):
    name = q.text(name,'SPC图名称') if name is not None else None
    chart_id = annotations.iid(chart_id,'SPC图ID') if chart_id is not None else None
    chart = await read_chart(name, chart_id)
    if chart is None:
        return {'state': 'not_found', 'complete': True, 'blockers': ['chart_not_found']}
    first = await snapshot(chart)
    if await snapshot(chart) != first or await read_chart(name, chart_id) != chart:
        raise ValueError('读取期间SPC图、依赖或配置清单发生变化。')
    blockers = []
    for name, dependency in first['dependencies'].items():
        if dependency['state'] != 'present':
            blockers.append(name+'_'+dependency['state'])
        elif dependency['is_frozen'] is not False:
            blockers.append(name+'_frozen_or_unknown')
    if first['dependencies']['connection'].get('endpoint_configured') is False:
        blockers.append('connection_endpoint_not_configured')
    if first['members']['rule_members'] == []:
        blockers.append('rule_member_list_empty')
    if any(not member['violation_reference_resolved'] or member.get('violation_is_frozen') is not False
           for member in first['members']['rule_members'] or []):
        blockers.append('violation_reference_missing_frozen_or_unknown')
    if any(member['is_frozen'] is not False for members in first['members'].values() if members is not None for member in members):
        blockers.append('configuration_member_frozen_or_unknown')
    if any(member['dynamic'] is not False for group in ('chart_parameters','query_parameters') for member in first['members'][group]):
        blockers.append('dynamic_parameters_require_runtime_resolution')
    flags = chart['flags']
    if flags['IsFrozen'] is not False:
        blockers.append('chart_frozen_or_unknown')
    if flags['RecordAgainstCustomSPCDataSrc'] is not False:
        blockers.append('custom_data_source_or_unknown')
    if any(flags[field] is not False for field in ('TriggerOCAPs','RequireAnnotation','NotificationConfigured','HoldConfigured','ResourceStatusConfigured')):
        blockers.append('side_effect_or_annotation_review_required')
    return {'state': 'observed', 'complete': True, **first, 'configuration_digest': digest(first),
            'blockers': blockers, 'parameter_values_verified': False, 'compiled_rules_verified': False,
            'native_control_limits_verified': False, 'native_engine_result_verified': False,
            'ready_for_native_execution': False}
