"""Versioned native evidence for restricted SPC registrations, never engine verdicts."""
from tools import quality as q, quality_inspection as inspection
from tools import quality_spc_annotations as annotations, quality_spc_violations as violations
from tools import quality_disposition_history as disposition, quality_plans as plans


SERVICES = {'spc_annotation': ('AddSPCAnnotation', 8393),
            'spc_violation': ('RecordSPCViolation', 8396)}
TABLES = {
    'mainlines': ('HistoryMainline', annotations.SCHEMA['HistoryMainline'], 'HistoryMainlineId'),
    'annotations': ('AddSPCAnnotationHistory', annotations.SCHEMA['AddSPCAnnotationHistory'], 'AddSPCAnnotationHistoryId'),
    'summaries': ('RecordSPCViolationHistory', violations.SCHEMA['RecordSPCViolationHistory'], 'RecordSPCViolationSummaryId'),
    'charts': ('SPCChartHistoryDetail', violations.SCHEMA['SPCChartHistoryDetail'], 'RecordSPCChartViolationHistDId'),
    'violations': ('SPCViolationHistoryDetail', violations.SCHEMA['SPCViolationHistoryDetail'], 'SPCChartViolationsHistDtlsId'),
}


def supported(snap):
    return (snap.get('kind') in SERVICES and type(snap.get('spc_evidence_version')) is int
            and snap['spc_evidence_version'] == 1)


def validate(snap):
    if not supported(snap):
        raise ValueError('旧SPC快照没有本版证据契约，保留原人工审计流程。')
    plans.transport(snap)
    disposition.comments(snap)


def normalize(row):
    row = dict(row)
    for field, value in row.items():
        if field.endswith('Id') and field not in ('CustomSPCDataId', 'ExternalDataId') and value not in ('', None):
            row[field] = annotations.iid(value, field)
        elif field in ('ChangeCount', 'TxnType', 'ReversalStatus'):
            row[field] = inspection.wire_int(value, field)
        elif field == 'Exclude':
            row[field] = inspection.wire_bool(value, field)
    return row


async def roots(snap, offset=0, limit=20):
    validate(snap)
    q.paging(offset, limit)
    # Search globally: filtering by the expected point or type could conceal a
    # second transaction carrying the same marker on another point/service.
    rows = await q.sql_rows('SELECT '+TABLES['mainlines'][1]+' FROM HistoryMainline WHERE Comments='
        +q.literal(snap['body']['comments'])
        +f' ORDER BY HistoryMainlineId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY', limit+1)
    rows = [normalize(row) for row in rows]
    seen = set()
    for row in rows:
        key = annotations.iid(row['HistoryMainlineId'])
        annotations.iid(row['TxnId'])
        if key in seen or row['Comments'] != snap['body']['comments']:
            raise ValueError('SPC主历史重复或未精确匹配完整请求评论。')
        seen.add(key)
    return rows[:limit], len(rows) > limit


async def transaction(txn_id):
    key = annotations.iid(txn_id, 'SPC事务ID')
    result = {}
    # Read every SPC table for this transaction, including the other service's
    # tables. Extra charts, points, summaries or mainlines are never discarded.
    for label, (table, columns, identity) in TABLES.items():
        where = 'TxnId='+q.literal(key)
        parent = {'annotations': ('HistoryMainlineId', 'mainlines'),
                  'summaries': ('HistoryMainlineId', 'mainlines'),
                  'charts': ('RecordSPCViolationSummaryId', 'summaries'),
                  'violations': ('ParentId', 'charts')}.get(label)
        if parent:
            field, parent_label = parent
            parent_key = TABLES[parent_label][2]
            parent_ids = [annotations.iid(row[parent_key]) for row in result[parent_label]]
            if parent_ids:
                # Include all children of the actual parent, even if their TxnId
                # is corrupt/foreign. A transaction-only filter hides them.
                where += ' OR '+field+' IN ('+','.join(q.literal(value) for value in parent_ids)+')'
        rows = [normalize(row) for row in await q.sql_rows('SELECT TOP 3 '+columns+' FROM '+table+' WHERE '
                                +where+' ORDER BY '+identity, 3)]
        seen = set()
        for row in rows:
            rid = annotations.iid(row[identity])
            if rid in seen or annotations.iid(row['TxnId']) != key or not set(columns.split(',')) <= row.keys():
                raise ValueError('SPC事务历史字段不完整、重复或串错事务。')
            seen.add(rid)
        if len(rows) > 1:
            raise ValueError('受限SPC事务含额外主历史或明细，不以子集结案。')
        result[label] = rows
    return result


def annotation_matches(snap, row):
    source, body = snap['before']['source'], snap['body']
    return (row['HistoryId'] == row['DataPointId'] == snap['target_id']
            and row['DataCollectionDefId'] == source['DataCollectionDefId']
            and row['SPCChartDefId'] == body['spcChartDef']['instanceId']
            and row['Annotation'] == body['annotation']
            and inspection.wire_bool(row['Exclude'], '注释排除标志') is False
            and all(row[field] in ('', None) for field in ('CustomSPCDataId', 'ExternalDataId')))


def violation_matches(snap, chart, row):
    detail = snap['body']['serviceDetails'][0]
    expected = detail['violations'][0]
    fields = ('chartVariable', 'collectedDataValue', 'derivedChartDataValue',
              'failureAction', 'ruleViolated', 'violationName')
    return (chart['SPCChartDefId'] == detail['spcChartDef']['instanceId']
            and chart['SPCChartDefName'] == detail['spcChartDefName']
            and row['DataPointId'] == snap['target_id']
            and row['ContainerId'] == expected['container']['instanceId']
            and all(row[violations.FIELD_CASE.get(field, field[0].upper()+field[1:])] == expected[field]
                    for field in fields)
            and all(violations.wire_matches(expected[field], row[violations.FIELD_CASE[field]], field)
                    for field in ('cl', 'lcl', 'ucl'))
            and all(row[field] in ('', None) for field in
                    ('HoldReasonId', 'ResourceId', 'ResourceStatusCodeId', 'CustomSPCDataId')))


def compare(snap, attempt, root, native):
    _, txn_type = SERVICES[snap['kind']]
    checks = {
        'single_original_mainline': native['mainlines'] == [root],
        'exact_request_comment': root['Comments'] == snap['body']['comments'],
        'native_transaction_type': inspection.wire_int(root['TxnType'], 'SPC事务类型') == txn_type,
        'active_native_history': inspection.wire_int(root['ReversalStatus'], 'SPC冲销状态') == 1,
        'attempt_time_window': disposition.attempt_time_matches(attempt, root),
    }
    if snap['kind'] == 'spc_annotation':
        rows = native['annotations']
        checks['complete_annotation_shape'] = len(rows) == 1 and not any(native[k] for k in ('summaries', 'charts', 'violations'))
        if len(rows) == 1:
            row = rows[0]
            checks['annotation_parent_chain'] = (row['HistoryMainlineId'] == root['HistoryMainlineId']
                                                and row['HistoryId'] == root['HistoryId'] == snap['target_id'])
            checks['annotation_payload'] = annotation_matches(snap, row)
            checks['annotation_not_in_baseline'] = row['AddSPCAnnotationHistoryId'] not in {
                item['AddSPCAnnotationHistoryId'] for item in snap['before']['annotation_history']}
    else:
        checks['complete_violation_shape'] = (not native['annotations']
            and all(len(native[k]) == 1 for k in ('summaries', 'charts', 'violations')))
        if checks['complete_violation_shape']:
            summary, chart, row = (native[k][0] for k in ('summaries', 'charts', 'violations'))
            checks['violation_parent_chain'] = (
                summary['HistoryMainlineId'] == root['HistoryMainlineId']
                and chart['RecordSPCViolationSummaryId'] == summary['RecordSPCViolationSummaryId']
                and row['ParentId'] == chart['RecordSPCChartViolationHistDId']
                and row['HistoryId'] == root['HistoryId'])
            checks['reported_violation_payload'] = violation_matches(snap, chart, row)
            checks['violation_not_in_baseline'] = row['SPCChartViolationsHistDtlsId'] not in {
                item['SPCChartViolationsHistDtlsId'] for item in snap['before']['violation_history']}
    return checks


async def current_evidence(snap, txn_id):
    validate(snap)
    args = dict(snap['arguments'])
    args.update(data_point_history_id=snap['target_id'],
                container_instance_id=snap['before']['source']['ContainerId'],
                spc_chart_instance_id=snap['before']['configuration']['chart']['InstanceID'])
    if snap['kind'] == 'spc_annotation':
        current = await annotations.context(args, original=snap['before']['source'])
        label, identity = 'annotation_history', 'AddSPCAnnotationHistoryId'
    else:
        current = await violations.context(args, original=snap['before']['source'], original_rules=snap['before']['rules'])
        label, identity = 'violation_history', 'SPCChartViolationsHistDtlsId'
    checks = {'original_source_unchanged': current['source'] == snap['before']['source'],
              'original_configuration_unchanged': current['configuration'] == snap['before']['configuration'],
              'no_active_exclusions': not any(row['Exclude'] for row in current['annotation_history'])}
    if snap['kind'] == 'spc_violation':
        checks['original_rules_unchanged'] = current['rules'] == snap['before']['rules']
        checks['annotation_history_unchanged'] = current['annotation_history'] == snap['before']['annotation_history']
    before = {row[identity]: row for row in snap['before'][label]}
    after = {row[identity]: row for row in current[label]}
    checks['previous_history_preserved'] = all(after.get(key) == value for key, value in before.items())
    added = [row for key, row in after.items() if key not in before]
    native = await transaction(txn_id)
    native_label = 'annotations' if snap['kind'] == 'spc_annotation' else 'violations'
    checks['single_new_registered_history'] = (len(added) == len(native[native_label]) == 1
        and all(added[0].get(field) == value for field, value in native[native_label][0].items())
        and added[0]['Comments'] == snap['body']['comments'])
    return checks, current


async def verify(snap, attempt, txn_id=None):
    validate(snap)
    schema = {table: columns for table, columns, _ in TABLES.values()}
    await annotations.schema_check(schema)
    page = await roots(snap, limit=1)
    rows, more = page
    flags = {'request_correlation_verified': False, 'recorded_business_effects_verified': False,
             'native_engine_result_verified': False, 'native_control_limits_verified': False,
             'reported_result_provenance_verified': False, 'release_authorized': False}
    if more or not rows:
        return {'comparison': 'ambiguous' if more else 'no_records', 'complete': False, **flags}
    root = rows[0]
    candidate = annotations.iid(root['TxnId'])
    if txn_id is not None and annotations.iid(txn_id) != candidate:
        return {'comparison': 'candidate_mismatch', 'complete': False, **flags}
    native = await transaction(candidate)
    checks = compare(snap, attempt, root, native)
    current_checks, current = await current_evidence(snap, candidate)
    if (await roots(snap, limit=1) != page or await transaction(candidate) != native
            or await current_evidence(snap, candidate) != (current_checks, current)):
        raise ValueError('核对期间SPC主历史、原始点、配置或登记记录发生变化，继续保留占用。')
    matched = all(checks.values()) and all(current_checks.values())
    return {**flags, 'candidate_txn_id': candidate, 'complete': True,
            'comparison': 'native_registration_match' if matched else 'mismatch',
            'request_correlation_verified': matched, 'recorded_business_effects_verified': matched,
            'checks': checks, 'current_checks': current_checks,
            'verified_scope': 'restricted_annotation' if snap['kind'] == 'spc_annotation' else 'operator_reported_violation_registration',
            'evidence': {'native': native, 'current': current},
            'limitations': ['只核实原生登记及原始历史保持；不认证SPC引擎、报告来源、编译规则或控制限，不授权质量放行。']}
