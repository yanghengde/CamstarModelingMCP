"""Read-only inspection evidence using fixed, bounded history queries.

HistoryId groups a transaction; it is never used as a detail's parent key.
Matching historical input is evidence for review, not a commit certificate.
"""
import json
from collections import Counter
from pydantic import StrictInt

from tools import mcp, quality as q, quality_inspection as i, quality_plans as p
from tools import quality_disposition_history as disposition
from core.transfer_attempt_lock import attempt_lock, AttemptBusy

FLAGS = {'verified_success': False, 'state_changed': False, 'retry_allowed': False,
         'completion_causal_chain_verified': False, 'request_correlation_verified': False}
SERVICE_TYPES = {'inspection': 8452, 'lot_inspection': 8589, 'lot_maintenance': 8592}
SERVICES = {'inspection': 'CollectSamplingData', 'lot_inspection': 'CollectLotSamplingData', 'lot_maintenance': 'UpdateSamplingLot'}


def iid(value, label):
    if not q.valid_id(value):
        raise ValueError(label+'须为16位十六进制ID。')
    return value


def detail_rows(rows, maximum, owner_ids, header):
    if len(rows) > maximum:
        raise ValueError('历史明细超过有界展开上限，不能用子集核对。')
    ids = set()
    for row in rows:
        rid = iid(row.get('InstanceID'), '历史明细ID')
        if rid in ids or row.get('ParentId') not in owner_ids or row.get('TxnId') != header['TxnId'] or row.get('HistoryId') != header['HistoryId']:
            raise ValueError('历史明细重复、所属对象或事务组不一致。')
        ids.add(rid)
    return ids


def reference(value):
    return {'InstanceID': value} if value else None


async def sampling_details(header):
    # Parent-column mapping follows the deployed inherited HistoryDetails schema.
    # Empty-site reads verify query shape only; this mapping is explicitly
    # provisional until accepted against a nonempty inspection transaction.
    summary_id = iid(header['SummaryId'], '汇总历史ID')
    join = (' FROM CollectSamplingHistoryDetails d INNER JOIN CollectSamplingDataHistory s '
            'ON d.HistoryDetailsId=s.CollectSamplingDataHistoryId AND d.TxnId=s.TxnId AND d.HistoryId=s.HistoryId ')
    where = f"WHERE s.CollectSamplingDataHistoryId='{summary_id}' AND s.TxnId='{iid(header['TxnId'], '事务ID')}' "
    samples = await q.sql_rows('SELECT TOP 51 d.CollectSamplingHistoryDetailId AS InstanceID,d.HistoryDetailsId AS ParentId,'
                               'd.HistoryId,d.TxnId,d.SampleTestId,d.SampleTestName,d.Rejected,d.Rejects'+join+where+
                               'ORDER BY d.CollectSamplingHistoryDetailId', 51)
    sample_ids = detail_rows(samples, 50, {summary_id}, header)
    points = await q.sql_rows('SELECT TOP 101 p.CollectSamplingTestDtlHistorId AS InstanceID,p.HistoryDetailsId AS ParentId,'
                              'p.HistoryId,p.TxnId,p.SampleDataPointId,p.DataType,p.DataValue,p.Passed,p.LowerLimit,p.UpperLimit,'
                              'p.RejectReasonId,p.EnteredQty'+join+
                              'INNER JOIN CollectSamplingTestDtlHistory p ON p.HistoryDetailsId=d.CollectSamplingHistoryDetailId '
                              'AND p.TxnId=d.TxnId AND p.HistoryId=d.HistoryId '+where+'ORDER BY p.CollectSamplingTestDtlHistorId', 101)
    detail_rows(points, 100, sample_ids, header)
    if not samples:
        raise ValueError('没有可关联的样本明细；父键映射尚待非空现场验证，不能将空结果当完整。')
    grouped = []
    for sample in samples:
        sample = {**sample, 'SampleTest': reference(sample.get('SampleTestId'))}
        children = [{**point, 'SampleDataPoint': reference(point.get('SampleDataPointId')),
                     'RejectReason': reference(point.get('RejectReasonId'))}
                    for point in points if point['ParentId'] == sample['InstanceID']]
        grouped.append({'sample_history': sample, 'points': children})
    return grouped


def header_sql(kind):
    if kind == 'lot_maintenance':
        table = 'UpdateSamplingLotHistory'
        fields = 's.UpdateSamplingLotHistoryId AS SummaryId,s.SamplingLotId,s.Qty,s.SampleRate,s.Comments AS StoredComments'
    else:
        table = 'CollectSamplingDataHistory'
        fields = ('s.CollectSamplingDataHistoryId AS SummaryId,s.SamplingLotId,s.SampleTestId,'
                  's.TotalSamplesCompleted,s.TotalRejects,s.TotalPassedSamples')
    return ('SELECT '+fields+',s.HistoryId,s.HistoryMainlineId,s.TxnId,h.HistoryId AS HeaderHistoryId,'
            'h.ContainerId,h.ContainerName,h.Comments,h.TxnType,h.TxnTypeName,h.TxnServiceName,'
            'h.TxnServiceSequence,h.ReversalStatus FROM '+table+' s INNER JOIN HistoryMainline h '
            'ON h.HistoryMainlineId=s.HistoryMainlineId AND h.TxnId=s.TxnId ')


def validate_headers(rows):
    for row in rows:
        for field in ('SummaryId', 'HistoryMainlineId', 'TxnId', 'HistoryId', 'HeaderHistoryId'):
            iid(row.get(field), field)
        if row['HistoryId'] != row['HeaderHistoryId']:
            raise ValueError('汇总和主历史事务组不符。')
    if len({r['SummaryId'] for r in rows}) != len(rows):
        raise ValueError('历史汇总关联重复。')


async def trace(summary_id):
    iid(summary_id, '检验汇总历史ID')
    rows = await q.sql_rows(header_sql('inspection')+
                            f"WHERE s.CollectSamplingDataHistoryId='{summary_id}' ORDER BY s.CollectSamplingDataHistoryId", 2)
    validate_headers(rows)
    if len(rows) != 1 or rows[0]['SummaryId'] != summary_id:
        raise ValueError('检验汇总历史不存在或不唯一。')
    header = rows[0]
    samples = await sampling_details(header)
    again = await q.sql_rows(header_sql('inspection')+
                             f"WHERE s.CollectSamplingDataHistoryId='{summary_id}' ORDER BY s.CollectSamplingDataHistoryId", 2)
    if again != rows or await sampling_details(header) != samples:
        raise ValueError('展开期间历史主记录发生变化。')
    return {'header': header, 'samples': samples, 'complete': True,
            'ownership_source': 'schema-derived HistoryDetailsId parent mapping plus TxnId and HistoryId boundaries',
            'ownership_linkage_verified': False, 'sample_order_verified': False, 'nonempty_onsite_acceptance_verified': False}


def history_mode(snap):
    if snap.get('inspection_effects'):
        return 'manual'
    kind = snap.get('kind')
    if kind in disposition.SERVICE_TYPES:
        return 'native'
    return 'candidate' if kind in SERVICES else 'manual'


def attempted_plan(plan_id, fingerprint, *, allow_manual=False):
    with p.db_connect() as db:
        row = db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?', (plan_id,)).fetchone()
        attempt = db.execute('SELECT attempt_id,started,finished FROM attempts WHERE plan_id=?', (plan_id,)).fetchone()
    snap = p.checked(row, fingerprint)
    if row[2] not in ('unknown', 'in_flight') or not attempt:
        raise ValueError('只核对有实际尝试记录的unknown/in_flight计划。')
    if history_mode(snap) == 'manual':
        if allow_manual:
            p.transport(snap)
            return snap, row[2], {'attempt_id': attempt[0], 'started': attempt[1], 'finished': attempt[2]}
        raise ValueError('当前计划须用inspect_quality_transaction取证，再用review_quality_transaction人工审核。')
    kind = snap['kind']
    service = SERVICES.get(kind) or disposition.SERVICE_TYPES.get(kind,(None,None))[0]
    if snap.get('service') != service:
        raise ValueError('原计划种类与实际服务不一致。')
    if snap['kind'] == 'inspection':
        # Older evidence snapshots may omit the move option. Keep that unknown
        # on this read-only path; never synthesize false or relax write transport.
        body = snap.get('body')
        required = {'container', 'sampleTest', 'serviceDetails', 'comments'}
        if (snap.get('action') != 'collect' or not isinstance(body, dict) or not required <= body.keys()
                or set(body)-required-{'moveContainerOnCompletion'}
                or 'moveContainerOnCompletion' in body and body['moveContainerOnCompletion'] is not False):
            raise ValueError('历史检验快照字段不符；只读核对不补造执行选项。')
    else:
        p.transport(snap)
    if not isinstance(snap['body'].get('comments'), str) or not snap['body']['comments']:
        raise ValueError('原请求缺少完整事务关联评论。')
    return snap, row[2], {'attempt_id': attempt[0], 'started': attempt[1], 'finished': attempt[2]}


async def candidates(snap, offset=0, limit=20, txn_id=None):
    q.paging(offset, limit)
    body = snap['body']; kind = snap['kind']
    where = [('s.Comments' if kind == 'lot_maintenance' else 'h.Comments')+'='+q.literal(body['comments'])]
    if kind == 'lot_maintenance':
        where.append("s.SamplingLotId='"+iid(body['samplingLot']['instanceId'], '原批ID')+"'")
    else:
        where += ["h.ContainerId='"+iid(body['container']['instanceId'], '原容器ID')+"'",
                  "s.SampleTestId='"+iid(body['sampleTest']['instanceId'], '原测试ID')+"'"]
        if kind == 'lot_inspection':
            where.append("s.SamplingLotId='"+iid(body['samplingLot']['instanceId'], '原批ID')+"'")
        else:
            where.append("(s.SamplingLotId IS NULL OR s.SamplingLotId='')")
    if txn_id is not None:
        where.append("s.TxnId='"+iid(txn_id, '候选事务ID')+"'")
    key = 'UpdateSamplingLotHistoryId' if kind == 'lot_maintenance' else 'CollectSamplingDataHistoryId'
    rows = await q.sql_rows(header_sql(kind)+'WHERE '+' AND '.join(where)+
                            f' ORDER BY s.{key} OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY', limit+1)
    validate_headers(rows)
    # Validate predicates again rather than trusting an unexpected server shape.
    for row in rows:
        if row.get('StoredComments' if kind == 'lot_maintenance' else 'Comments') != body['comments'] or (txn_id and row['TxnId'] != txn_id):
            raise ValueError('候选事务或完整评论不符。')
        if kind == 'lot_maintenance' and row.get('SamplingLotId') != body['samplingLot']['instanceId']:
            raise ValueError('候选维护历史目标批不符。')
        if kind != 'lot_maintenance' and (
                row.get('ContainerId') != body['container']['instanceId']
                or row.get('SampleTestId') != body['sampleTest']['instanceId']
                or (row.get('SamplingLotId') or None) != (body.get('samplingLot') or {}).get('instanceId')):
            raise ValueError('候选历史容器、测试或历史批引用不符。')
    more = len(rows) > limit
    return rows[:limit], more


def ref_id(ref):
    return iid(q.identifier(ref) if 'instanceId' not in (ref or {}) else ref['instanceId'], '固定引用ID')


def nullable_number(value):
    return None if value in (None, '') else i.numeric(value, '历史限值')


def compare_sampling(snap, evidence):
    expected = snap['body']['serviceDetails']; observed = evidence['samples']
    unverified = ['历史父键映射尚未通过非空现场验收，当前分组为候选关联',
                  'MES最终事务因果链及后续业务影响尚未通过非空现场验收', '历史没有已核实的样本顺序字段',
                  '历史未完整记录原请求的移站及外部集成选项，不能证明这些选项的最终执行效果']
    dtype = snap['definition']['test']['SampleType']
    def basic(test_id, rejected, rejects):
        return (test_id, i.wire_bool(rejected, '拒收标志'), i.wire_int(rejects, '拒收数'))
    actual = []
    wanted = []
    point_defs = {ref_id(r): r for r in snap['definition']['points']}
    for detail in expected:
        base = basic(ref_id(detail['sampleTest']), detail['rejected'], detail['rejects'])
        if dtype == 2:
            points = []
            for point in detail['samplingDataPointDetails']:
                pid = ref_id(point['sampleDataPoint']); definition = point_defs[pid]
                points.append((pid, definition['DataType'], i.numeric(point['dataValue'], '原点值'), point['passed'],
                               nullable_number(definition.get('LowerLimit')), nullable_number(definition.get('UpperLimit'))))
            base += (tuple(sorted(points)),)
        wanted.append(base)
    for row in observed:
        sample = row['sample_history']
        base = basic(ref_id(sample['SampleTest']), sample.get('Rejected'), sample.get('Rejects'))
        if dtype == 2:
            points = []
            ids = set()
            for point in row['points']:
                pid = ref_id(point.get('SampleDataPoint'))
                if pid in ids:
                    raise ValueError('同一样本重复检验点，不能作完整匹配。')
                ids.add(pid)
                points.append((pid, i.wire_int(point.get('DataType'), '点数据类型'),
                               i.numeric(point.get('DataValue'), '历史点值'), i.wire_bool(point.get('Passed'), '点通过标志'),
                               nullable_number(point.get('LowerLimit')), nullable_number(point.get('UpperLimit'))))
            base += (tuple(sorted(points)),)
        actual.append(base)
    if dtype == 1:
        unverified += ['计数历史明细没有已核实的本次采样数量字段，累计数不等于本次增量',
                       '计数拒收原因和EnteredQty的归属语义尚未现场核实']
    else:
        unverified += ['测量样本个数按候选明细行数核对，本次总采样增量字段未独立核实']
    matches = Counter(wanted) == Counter(actual)
    return {'comparison': ('payload_match' if dtype == 2 else 'partial_match') if matches else 'mismatch',
            'compared_fields_match': matches, 'expected_samples': len(expected), 'historical_samples': len(observed),
            'detail_row_semantics':'individual measured samples' if dtype==2 else 'counted service groups, not individual sample units',
            'requested_sample_units':sum(r['totalSamplesCollected'] for r in expected),
            'requested_rejects':sum(r['rejects'] for r in expected),
            'sample_comparison': 'unordered multiset; duplicate sample multiplicities retained',
            'unverified_checks': unverified}


@mcp.tool
async def get_inspection_result_history(summary_history_id: str) -> str:
    """只读逐样本/逐点检验历史。先从get_container_inspection_history或get_sampling_lot_history取CollectSamplingDataHistoryId。按部署表结构候选父键和事务边界展开，最多50样本/100点；不以共享HistoryId作父键；父键映射仍待非空现场验收，不按ID排序伪造样本序号。含历史原值、限值、拒收、事务及冲销状态；缺失/超界失败。非空现场验收待补，不作最终成功或放行判断。"""
    try:
        return q.encode({**await trace(summary_history_id), **FLAGS})
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return q.encode({'complete': False, **FLAGS, 'error': str(error)})


@mcp.tool
async def find_quality_execution_candidates(plan_id: str, plan_digest: str, offset: StrictInt = 0, limit: StrictInt = 20) -> str:
    """只读查有尝试记录的unknown/in_flight检验、批采样、整批OK、批维护、单容器损失、整批报废及返工候选历史。校验摘要及环境，精确匹配完整评论。采样还约束容器/测试/批ID，单容器损失和整批处置使用主历史候选。分页不能证明候选唯一，不改本地审计状态。"""
    try:
        with attempt_lock(p.PLAN_DB, plan_id):
            snap, state, attempt = attempted_plan(plan_id, plan_digest)
            reader = disposition.roots if snap['kind'] in disposition.SERVICE_TYPES else candidates
            rows, more = await reader(snap, offset, limit)
            return q.encode({'plan_id': plan_id, 'state': state, 'attempt': attempt, 'rows': rows,
                             'page_complete': True, 'complete': False, 'has_more': more,
                             'next_offset': offset+limit if more else None,
                             'target_linkage_available': snap['kind'] not in disposition.LOT_RESULTS,
                             **FLAGS})
    except (ValueError, TypeError, KeyError, AttributeError, AttemptBusy) as error:
        return q.encode({'complete': False, **FLAGS, 'error': str(error)})


@mcp.tool
async def reconcile_quality_execution(plan_id: str, plan_digest: str, candidate_txn_id: str | None = None) -> str:
    """只读核对已尝试的质量计划。单容器损失、整批处置、批维护及带系统标记的普通检验可自动定位。整批OK只核对父批结果，检验和批维护只核对可见字段。旧检验仍须明确候选ID。其他事务给出人工取证入口。只有独立确认的本地审计可结束占用。"""
    try:
        if candidate_txn_id is not None:
            iid(candidate_txn_id, '候选事务ID')
        with attempt_lock(p.PLAN_DB, plan_id):
            snap, state, attempt = attempted_plan(plan_id, plan_digest, allow_manual=True)
            mode = history_mode(snap)
            if mode == 'manual':
                return q.encode({'plan_id': plan_id, 'state': state, 'attempt': attempt, **FLAGS,
                                 'comparison': 'manual_review_required', 'complete': False,
                                 'review_action': 'recover' if state == 'in_flight' else 'resolve',
                                 'evidence_tool': 'inspect_quality_transaction', 'review_tool': 'review_quality_transaction'})
            if mode == 'native':
                proof = await disposition.verify(snap, attempt, candidate_txn_id)
                action = 'resolve_history' if proof.get('recorded_business_effects_verified') else 'resolve'
                return q.encode({'plan_id': plan_id, 'state': state, 'attempt': attempt, **FLAGS, **proof,
                                 'review_action': 'recover' if state == 'in_flight' else action})
            root = None
            if snap['kind'] in ('inspection', 'lot_inspection') and snap.get('marker'):
                root_page = await disposition.roots(snap, limit=1)
                first, more = root_page
                if more or not first:
                    return q.encode({'comparison': 'ambiguous' if more else 'no_records', 'complete': False, **FLAGS})
                root = first[0]
                if candidate_txn_id is not None and root['TxnId'] != candidate_txn_id:
                    return q.encode({'comparison': 'candidate_mismatch', 'complete': False, **FLAGS})
                candidate_txn_id = root['TxnId']
            maintenance_page = None
            if candidate_txn_id is None and snap['kind'] == 'lot_maintenance':
                maintenance_page = await candidates(snap, limit=1)
                first, more = maintenance_page
                if more or not first:
                    return q.encode({'comparison': 'ambiguous' if more else 'no_records', 'complete': False, **FLAGS})
                candidate_txn_id = first[0]['TxnId']
            if candidate_txn_id is None:
                raise ValueError('该类型须明确候选事务ID。')
            rows, more = await candidates(snap, limit=100, txn_id=candidate_txn_id)
            if more or len(rows) != 1:
                return q.encode({'comparison': 'incomplete' if more else ('no_records' if not rows else 'ambiguous'),
                                 'candidates': rows, 'complete': False, **FLAGS})
            row = rows[0]
            checks = {'active_history': i.wire_int(row.get('ReversalStatus'), '冲销状态') == 1,
                      'transaction_type': i.wire_int(row.get('TxnType'), '事务类型') == SERVICE_TYPES[snap['kind']]}
            if snap['kind'] == 'lot_maintenance':
                if maintenance_page is not None and (rows != maintenance_page[0]
                        or await candidates(snap, limit=1) != maintenance_page):
                    raise ValueError('核对期间批维护候选发生变化。')
                checks.update(quantity=i.numeric(str(row.get('Qty')), '维护历史数量') == i.numeric(str(snap['body']['qty']), '原数量'),
                              sample_rate=i.wire_int(row.get('SampleRate'), '维护历史频率') == snap['body']['sampleRate'])
                comparison = {'comparison': 'partial_match' if all(checks.values()) else 'mismatch',
                              'compared_fields_match': all(checks.values()),
                              'unverified_checks': ['维护历史未保存AllowAdjustToLotSize，不能核实保留选项最终效果',
                                                    '任务要求重算、成员采样标志和最终事务因果链尚待非空现场验收']}
                evidence = {'header': row}
            else:
                evidence = await trace(row['SummaryId'])
                if evidence['header'] != row:
                    raise ValueError('候选查询与明细展开之间历史记录变化。')
                comparison = compare_sampling(snap, evidence)
                if root is not None:
                    target_ref = snap['body']['samplingLot' if snap['kind'] == 'lot_inspection' else 'container']
                    checks['exact_root_and_attempt'] = (
                        target_ref['instanceId'] == snap['target_id']
                        and root['HistoryMainlineId'] == root['IntegrationTxnId'] == row['HistoryMainlineId']
                        and root['HistoryId'] == row['HeaderHistoryId'] == row['ContainerId']
                        and root['TxnTypeName'] == SERVICES[snap['kind']]
                        and i.wire_int(root['TxnType'], '原生检验事务类型') == SERVICE_TYPES[snap['kind']]
                        and i.wire_int(root['ReversalStatus'], '原生检验冲销状态') == 1
                        and disposition.attempt_time_matches(attempt, root))
                    if await disposition.roots(snap, limit=1) != root_page:
                        raise ValueError('核对期间检验原标记记录发生变化。')
                if not all(checks.values()):
                    comparison['comparison'] = 'mismatch'
            linked = root is not None and all(checks.values()) and comparison['compared_fields_match']
            return q.encode({'plan_id': plan_id, 'state': state, 'attempt': attempt, 'candidate_txn_id': candidate_txn_id,
                             'checks': checks, **comparison, 'original_request': snap['body'],
                             'evidence': evidence, 'complete': True, **FLAGS, 'request_correlation_verified': linked})
    except (ValueError, TypeError, KeyError, AttributeError, AttemptBusy) as error:
        return q.encode({'comparison': 'incomplete', 'complete': False, **FLAGS, 'error': str(error)})
