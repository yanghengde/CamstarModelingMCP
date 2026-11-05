import asyncio
import copy
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import mcp, get_tool_func, quality_history as h, quality_plans as p
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION


def key(n): return f'{n:016x}'
ROOT, MAIN, GROUP, TXN, CONTAINER, LOT, TEST, POINT = [key(n) for n in range(1, 9)]
S1, S2, P1, P2 = [key(n) for n in range(11, 15)]
COMMENT = 'quality-request: explicit marker 原始备注'


def result(coro): return json.loads(asyncio.run(coro))
def ref(value): return {'InstanceID': value}
def wire(value): return {'instanceId': value}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'quality.sqlite3')
    header = {'SummaryId': ROOT, 'HistoryMainlineId': MAIN, 'HistoryId': GROUP, 'HeaderHistoryId': GROUP,
              'TxnId': TXN, 'ContainerId': CONTAINER, 'ContainerName': 'C', 'SampleTestId': TEST,
              'SamplingLotId': None, 'Comments': COMMENT, 'TxnType': '8452', 'TxnTypeName': 'CollectSamplingData',
              'TxnServiceName': 'CollectSamplingData', 'TxnServiceSequence': '0', 'ReversalStatus': '1'}
    objects = {ROOT: {'InstanceID': ROOT, 'Parent': ref(MAIN), 'HistoryId': ref(GROUP),
                      'HistoryDetails': [ref(S1), ref(S2)]}}
    details = []
    for sid, pid, value in [(S1, P1, '1.00'), (S2, P2, '2.00')]:
        details.append({'sampleTest': wire(TEST), 'totalSamplesCollected': 1, 'rejects': 0, 'rejected': False,
                        'samplingDataPointDetails': [{'sampleDataPoint': wire(POINT), 'dataValue': value, 'passed': True}]})
        objects[sid] = {'InstanceID': sid, 'Parent': ref(ROOT), 'HistoryId': ref(GROUP),
                        'SampleTest': ref(TEST), 'Rejects': '0', 'Rejected': 'false', 'CollectSamplingTestDtlHistory': [ref(pid)]}
        objects[pid] = {'InstanceID': pid, 'Parent': ref(sid), 'HistoryId': ref(GROUP), 'SampleDataPoint': ref(POINT),
                        'DataType': '2', 'DataValue': value, 'Passed': 'true', 'LowerLimit': '0', 'UpperLimit': '3',
                        'EnteredQty': '', 'RejectReason': None}
    snap = {'kind': 'inspection', 'action': 'collect', 'target_id': CONTAINER, 'name': 'C',
            'body': {'comments': COMMENT, 'container': wire(CONTAINER), 'sampleTest': wire(TEST), 'serviceDetails': details},
            'service': 'CollectSamplingData', 'environment': p.environment(),
            'definition': {'test': {'SampleType': 2}, 'points': [{'InstanceID': POINT, 'DataType': 2, 'LowerLimit': '0', 'UpperLimit': '3'}]}}
    state = {'header': header, 'objects': objects, 'snap': snap, 'candidate_rows': None}
    async def sql(query, maximum):
        if 'FROM HistoryMainline WHERE Comments=' in query:
            return copy.deepcopy(state['roots'][:maximum])
        if query.startswith('SELECT TOP 51'):
            rows = []
            for sr in state['objects'][ROOT]['HistoryDetails']:
                sid = sr['InstanceID']; obj = state['objects'].get(sid, {'InstanceID': sid, 'Parent': ref(ROOT), 'HistoryId': ref(GROUP)})
                rows.append({**obj, 'ParentId': obj['Parent']['InstanceID'], 'HistoryId': obj['HistoryId']['InstanceID'],
                             'TxnId': TXN, 'SampleTestId': obj.get('SampleTest', ref(TEST))['InstanceID']})
            return copy.deepcopy(rows)
        if query.startswith('SELECT TOP 101'):
            rows = []
            for sr in state['objects'][ROOT]['HistoryDetails']:
                sid = sr['InstanceID']
                for pr in state['objects'].get(sid, {}).get('CollectSamplingTestDtlHistory', []):
                    pid = pr['InstanceID']; obj = state['objects'].get(pid, {'InstanceID': pid, 'Parent': ref(sid), 'HistoryId': ref(GROUP)})
                    rows.append({**obj, 'ParentId': obj['Parent']['InstanceID'], 'HistoryId': obj['HistoryId']['InstanceID'],
                                 'TxnId': TXN, 'SampleDataPointId': obj.get('SampleDataPoint', ref(POINT))['InstanceID']})
            return copy.deepcopy(rows)
        assert 's.HistoryMainlineId=h.HistoryMainlineId' in query or 'h.HistoryMainlineId=s.HistoryMainlineId' in query
        assert 'h.TxnId=s.TxnId' in query
        if 'OFFSET' in query:
            return copy.deepcopy(state['candidate_rows'] if state['candidate_rows'] is not None else [state['header']])
        return [copy.deepcopy(state['header'])]
    sql_call = AsyncMock(side_effect=sql); get_call = AsyncMock()
    monkeypatch.setattr(h.q, 'sql_rows', sql_call)
    state.update(sql=sql_call, get=get_call)
    save(state)
    return state


def save(state, *, state_name='unknown', attempt=True):
    snap = state['snap']; fingerprint = p.digest(snap)
    with p.db_connect() as db:
        db.execute('INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?,?)',
                   ('plan', fingerprint, json.dumps(snap), state_name, time.time(), '{"original":"response"}'))
        db.execute('DELETE FROM attempts WHERE plan_id=?', ('plan',))
        if attempt:
            db.execute('INSERT INTO attempts VALUES (?,?,?,?)', ('plan', 'attempt', time.time()-2, time.time()-1))
    state['digest'] = fingerprint


def reconcile(env, **kwargs):
    return result(h.reconcile_quality_execution('plan', env['digest'], kwargs.get('txn', TXN)))


def journal():
    with p.db_connect() as db:
        return [db.execute('SELECT * FROM '+table).fetchall() for table in ('plans', 'attempts', 'reviews')]


def test_trace_uses_parent_column_and_transaction_boundaries_with_explicit_limitations(env):
    out = result(h.get_inspection_result_history(ROOT))
    assert out['complete'] and len(out['samples']) == 2
    assert out['samples'][0]['points'][0]['DataValue'] == '1.00'
    assert not out['sample_order_verified'] and not out['verified_success']
    assert not out['ownership_linkage_verified']
    queries = [a.args[0] for a in env['sql'].await_args_list]
    assert any('d.HistoryDetailsId=s.CollectSamplingDataHistoryId AND d.TxnId=s.TxnId' in q for q in queries)
    assert any('p.HistoryDetailsId=d.CollectSamplingHistoryDetailId' in q for q in queries)
    assert all('d.HistoryId=s.CollectSamplingDataHistoryId' not in q for q in queries)
    env['get'].assert_not_awaited()


@pytest.mark.parametrize('node,field,value', [
    (S1, 'Parent', ref(MAIN)), (P1, 'Parent', ref(ROOT)),
    (S1, 'HistoryId', ref(MAIN)), (P1, 'HistoryId', ref(MAIN)),
    (ROOT, 'HistoryDetails', [ref(S1), ref(S1)]),
    (ROOT, 'HistoryDetails', [ref(key(300+n)) for n in range(51)]),
    (S1, 'CollectSamplingTestDtlHistory', [ref(key(300+n)) for n in range(101)]),
])
def test_incomplete_wrong_parent_group_id_duplicate_or_unbounded_trace_rejected(env, node, field, value):
    env['objects'][node][field] = value
    out = result(h.get_inspection_result_history(ROOT))
    assert 'error' in out and not out['complete'] and not out['verified_success']


def test_point_cannot_be_shared_between_samples(env):
    env['objects'][S2]['CollectSamplingTestDtlHistory'] = [ref(P1)]
    assert 'error' in result(h.get_inspection_result_history(ROOT))


@pytest.mark.parametrize('value', ['', 'bad', "' OR 1=1", '0'*17])
def test_invalid_root_ids_never_read(env, value):
    assert 'error' in result(h.get_inspection_result_history(value))
    env['sql'].assert_not_awaited(); env['get'].assert_not_awaited()


def test_measured_payload_match_never_modifies_journal_or_unlocks(env):
    before = journal(); out = reconcile(env)
    assert out['comparison'] == 'payload_match' and out['compared_fields_match']
    assert all(out[k] is False for k in h.FLAGS)
    assert out['state'] == 'unknown' and out['unverified_checks']
    assert journal() == before


def test_sample_order_is_not_invented_and_numeric_strings_are_compared_by_value(env):
    env['objects'][ROOT]['HistoryDetails'].reverse()
    env['objects'][P1]['DataValue'] = '1e0'
    assert reconcile(env)['comparison'] == 'payload_match'


def test_duplicate_sample_multiplicity_preserved(env):
    env['snap']['body']['serviceDetails'][1] = copy.deepcopy(env['snap']['body']['serviceDetails'][0]); save(env)
    assert reconcile(env)['comparison'] == 'mismatch'
    env['objects'][P2]['DataValue'] = '1'
    assert reconcile(env)['comparison'] == 'payload_match'


@pytest.mark.parametrize('node,field,value', [
    (P1, 'DataValue', '2'), (P1, 'DataType', '1'), (P1, 'Passed', 'false'),
    (P1, 'LowerLimit', '-1'), (P1, 'UpperLimit', ''), (P1, 'SampleDataPoint', ref(LOT)),
    (S1, 'Rejected', 'true'), (S1, 'Rejects', '1'), (S1, 'SampleTest', ref(LOT)),
    (ROOT, 'HistoryDetails', [ref(S1)]), (S1, 'CollectSamplingTestDtlHistory', []),
])
def test_measured_field_or_sample_difference_detected(env, node, field, value):
    env['objects'][node][field] = value
    assert reconcile(env)['comparison'] == 'mismatch'


def test_extra_or_duplicate_point_is_not_silently_discarded(env):
    extra = key(999); env['objects'][S1]['CollectSamplingTestDtlHistory'].append(ref(extra))
    env['objects'][extra] = {**env['objects'][P1], 'InstanceID': extra}
    assert 'error' in reconcile(env)
    env['objects'][extra]['SampleDataPoint'] = ref(LOT)
    assert reconcile(env)['comparison'] == 'mismatch'


@pytest.mark.parametrize('field,value', [('DataValue', 'NaN'), ('DataValue', '1+1'), ('DataType', True), ('Passed', ''), ('LowerLimit', 'min(x)')])
def test_unparseable_history_never_matches(env, field, value):
    env['objects'][P1][field] = value
    assert reconcile(env)['comparison'] == 'incomplete'


@pytest.mark.parametrize('field,value', [('ReversalStatus', '2'), ('ReversalStatus', '3'), ('TxnType', '8980')])
def test_deleted_reversal_or_wrong_service_cannot_match(env, field, value):
    env['header'][field] = value
    out = reconcile(env)
    assert out['comparison'] == 'mismatch' and not all(out['checks'].values())


@pytest.mark.parametrize('field,value', [('Comments', COMMENT+' appended'), ('ContainerId', LOT), ('SampleTestId', LOT),
                                       ('SamplingLotId', LOT), ('TxnId', MAIN), ('HeaderHistoryId', MAIN)])
def test_candidate_scope_and_exact_marker_are_revalidated(env, field, value):
    env['header'][field] = value
    assert 'error' in reconcile(env)


def test_candidate_sql_escapes_full_comment_and_does_not_infer_parent(env):
    env['snap']['body']['comments'] = "marker's 原文"; env['header']['Comments'] = "marker's 原文"; save(env)
    out = result(h.find_quality_execution_candidates('plan', env['digest']))
    assert out['rows'][0]['TxnId'] == TXN and out['page_complete'] and not out['complete']
    query = env['sql'].await_args.args[0]
    assert "h.Comments=N'marker''s 原文'" in query and ' LIKE ' not in query
    assert "h.ContainerId='"+CONTAINER in query and "s.SampleTestId='"+TEST in query


@pytest.mark.parametrize('state_name,attempt', [('ready', True), ('human_committed', True), ('unknown', False), ('in_flight', False)])
def test_unattempted_or_final_plan_cannot_query(env, state_name, attempt):
    save(env, state_name=state_name, attempt=attempt)
    assert 'error' in reconcile(env)
    env['sql'].assert_not_awaited()


def test_environment_and_digest_are_checked_before_read(env):
    env['digest'] = 'incorrect'
    assert 'error' in reconcile(env)
    env['snap']['environment']['query'] += '-other'; save(env)
    assert 'error' in reconcile(env)
    env['sql'].assert_not_awaited()


@pytest.mark.parametrize('field,value', [('TxnId', MAIN), ('ParentId', MAIN), ('HistoryId', MAIN)])
def test_detail_transaction_boundaries_are_revalidated(env, field, value):
    original = env['sql'].side_effect
    async def sql(query, maximum):
        rows = await original(query, maximum)
        if query.startswith('SELECT TOP 101'):
            rows[0][field] = value
        return rows
    env['sql'].side_effect = sql
    assert 'error' in reconcile(env)


def test_detail_change_on_second_read_is_detected(env):
    original = env['sql'].side_effect; point_reads = 0
    async def sql(query, maximum):
        nonlocal point_reads
        rows = await original(query, maximum)
        if query.startswith('SELECT TOP 101'):
            point_reads += 1
            if point_reads == 2:
                rows[0]['DataValue'] = 'altered'
        return rows
    env['sql'].side_effect = sql
    assert 'error' in reconcile(env)


def test_empty_details_are_incomplete_pending_parent_mapping_validation(env):
    env['objects'][ROOT]['HistoryDetails'] = []
    out = reconcile(env)
    assert out['comparison'] == 'incomplete' and not out['complete']


def test_busy_execution_cannot_be_inspected_or_changed(env):
    before = journal()
    with h.attempt_lock(p.PLAN_DB, 'plan'):
        assert 'error' in reconcile(env)
        assert 'error' in result(h.find_quality_execution_candidates('plan', env['digest']))
    assert journal() == before
    env['sql'].assert_not_awaited()


def test_manual_plan_kind_returns_the_evidence_route_without_mes_reads(env):
    env['snap'].update(kind='inspection_definition', api='modeling', action='create', entity='SampleTests',
                       method='POST', path='/api/SampleTests')
    save(env)
    out = reconcile(env, txn=None)
    assert out['comparison'] == 'manual_review_required'
    assert out['evidence_tool'] == 'inspect_quality_transaction' and out['review_tool'] == 'review_quality_transaction'
    assert not out['complete'] and not out['state_changed']
    env['sql'].assert_not_awaited()


def test_inconsistent_kind_and_service_are_rejected(env):
    env['snap']['service'] = 'CreateCAPA'; save(env)
    assert 'error' in reconcile(env)
    env['sql'].assert_not_awaited()


@pytest.mark.parametrize('offset,limit', [(True, 1), (0, '2'), (0, 0), (-1, 20), (0, 101)])
def test_direct_invalid_paging_does_not_read(env, offset, limit):
    out = result(h.find_quality_execution_candidates('plan', env['digest'], offset, limit))
    assert 'error' in out
    env['sql'].assert_not_awaited()


def test_payload_corruption_checked_even_when_fingerprint_column_unchanged(env):
    with p.db_connect() as db:
        db.execute("UPDATE plans SET payload=?", (json.dumps({**env['snap'], 'name': 'forged'}),))
    assert 'error' in reconcile(env)
    env['sql'].assert_not_awaited()


@pytest.mark.parametrize('count,expected', [(0, 'no_records'), (2, 'ambiguous'), (101, 'incomplete')])
def test_reconciliation_requires_single_complete_candidate(env, count, expected):
    env['candidate_rows'] = [{**env['header'], 'SummaryId': key(300+n)} for n in range(count)]
    out = reconcile(env)
    assert out['comparison'] == expected and not out['complete']
    env['get'].assert_not_awaited()


def test_candidate_page_is_not_unique_proof(env):
    env['candidate_rows'] = [{**env['header'], 'SummaryId': key(300+n)} for n in range(3)]
    out = result(h.find_quality_execution_candidates('plan', env['digest'], limit=2))
    assert len(out['rows']) == 2 and out['has_more'] and out['next_offset'] == 2
    assert not out['verified_success']


def test_historical_lot_identity_is_used_for_batch_sampling(env):
    env['snap'].update(kind='lot_inspection', service='CollectLotSamplingData')
    env['snap']['body']['samplingLot'] = wire(LOT)
    env['header'].update(SamplingLotId=LOT, TxnType='8589'); save(env)
    assert reconcile(env)['comparison'] == 'payload_match'
    assert "s.SamplingLotId='"+LOT in env['sql'].await_args_list[0].args[0]


def test_counted_matches_are_only_partial_without_delta_and_reason_proof(env):
    env['snap']['definition']['test']['SampleType'] = 1
    env['snap']['body']['serviceDetails'] = [{'sampleTest': wire(TEST), 'totalSamplesCollected': 5, 'rejects': 0, 'rejected': False}]
    env['objects'][ROOT]['HistoryDetails'] = [ref(S1)]
    env['objects'][S1]['CollectSamplingTestDtlHistory'] = []
    env['header'].update(TotalSamplesCompleted='500', TotalRejects='0'); save(env)
    out = reconcile(env)
    assert out['comparison'] == 'partial_match' and not out['verified_success']
    assert any('本次采样' in s for s in out['unverified_checks'])


@pytest.mark.parametrize('case', ['match', 'wrong_reason', 'missing_point', 'duplicate_point', 'wrong_reject_count',
                                  'wrong_samples_tested', 'missing_samples_tested', 'wrong_plan', 'wrong_required',
                                  'legacy', 'unexpected_measured_point'])
def test_counted_lot_matches_persisted_reason_children_and_root_count_only(env, case):
    plan, reason1, reason2 = map(key, (30, 31, 32))
    snap = env['snap']
    snap.update(kind='lot_inspection', service='CollectLotSamplingData',
                before={'lot': {'SamplingPlanId': plan}, 'task': {'SamplingPlanId': plan, 'TotalSamplesRequired': 20}})
    snap['definition']['test']['SampleType'] = 1
    details = []
    for sample_id, point_id, count, rejects, reason in [(S1,P1,3,1,reason1), (S2,P2,4,2,reason2)]:
        details.append({'sampleTest': wire(TEST), 'totalSamplesCollected': count, 'rejected': True, 'rejects': rejects,
                        'samplingDataPointDetails': [{'dataValue': str(rejects), 'passed': False, 'rejectReason': wire(reason)}]})
        env['objects'][sample_id].update(Rejected='true', Rejects=str(rejects))
        env['objects'][point_id].update(SampleDataPoint=ref(''), RejectReasonId=reason, DataValue=str(rejects),
                                        Passed='false', EnteredQty='')
    snap['body'].update(samplingLot=wire(LOT), samplesTested=7, serviceDetails=details)
    env['header'].update(SamplingLotId=LOT, TxnType='8589', SamplingPlanId=plan, RequiredSamples='20',
                         SamplesTested='7', TotalSamplesCompleted='180', TotalRejects='45')
    if case == 'wrong_reason':
        env['objects'][P2]['RejectReasonId'] = reason1
    elif case == 'missing_point':
        env['objects'][S2]['CollectSamplingTestDtlHistory'] = []
    elif case == 'duplicate_point':
        extra = key(40)
        env['objects'][extra] = env['objects'][P2] | {'InstanceID': extra}
        env['objects'][S2]['CollectSamplingTestDtlHistory'].append(ref(extra))
    elif case == 'wrong_reject_count':
        env['objects'][P2]['DataValue'] = '1'
    elif case == 'wrong_samples_tested':
        env['header'].update(SamplesTested='8', TotalSamplesCompleted='7')
    elif case == 'missing_samples_tested':
        env['header'].pop('SamplesTested')
    elif case == 'wrong_plan':
        env['header']['SamplingPlanId'] = key(999)
    elif case == 'wrong_required':
        env['header']['RequiredSamples'] = '21'
    elif case == 'legacy':
        snap['body'].pop('samplesTested')
    elif case == 'unexpected_measured_point':
        env['objects'][P2]['SampleDataPoint'] = ref(POINT)
    save(env)
    before = journal()
    out = reconcile(env)
    assert not out['verified_success'] and not out['completion_causal_chain_verified']
    assert journal() == before
    if case == 'missing_samples_tested':
        assert out['comparison'] == 'incomplete', out
    elif case in ('match', 'legacy'):
        assert out['comparison'] == 'partial_match', out
        assert out['counted_history_checks']['counted_groups_and_reject_reasons_match']
        assert out['counted_history_checks'].get('recorded_samples_tested_match') is (True if case == 'match' else None)
        assert any('分摊' in check for check in out['unverified_checks'])
    else:
        assert out['comparison'] == 'mismatch', out
        assert not all(out['counted_history_checks'].values())


@pytest.mark.parametrize('case', ['match', 'wrong_parent', 'duplicate_marker', 'outside_attempt'])
def test_marked_sampling_binds_root_without_claiming_final_effects(env, case):
    marker = 'MCP-QUAL:'+'f'*32
    stamp = time.time()
    env['snap'].update(marker=marker, target_id=GROUP)
    env['snap']['body']['comments'] = marker
    env['header'].update(Comments=marker, ContainerId=GROUP)
    env['snap']['body']['container'] = wire(GROUP)
    env['roots'] = [{'TxnId': TXN, 'HistoryMainlineId': MAIN, 'HistoryId': GROUP, 'Comments': marker,
                     'IntegrationTxnId': MAIN, 'TxnType': '8452', 'TxnTypeName': 'CollectSamplingData',
                     'ReversalStatus': '1', 'TxnDateGMT': datetime.fromtimestamp(stamp, timezone.utc).isoformat()}]
    save(env)
    with p.db_connect() as db:
        db.execute('UPDATE attempts SET attempt_id=?,started=?,finished=?',('a'*32,stamp-2,stamp+1))
    if case == 'wrong_parent':
        env['roots'][0]['IntegrationTxnId'] = ROOT
    elif case == 'duplicate_marker':
        env['roots'].append(env['roots'][0] | {'HistoryMainlineId': key(999)})
    elif case == 'outside_attempt':
        env['roots'][0]['TxnDateGMT'] = datetime.fromtimestamp(stamp-60, timezone.utc).isoformat()
    before = journal()
    out = reconcile(env, txn=None)
    assert out['request_correlation_verified'] is (case == 'match'), out
    assert not out['completion_causal_chain_verified'] and not out['verified_success']
    assert journal() == before


def test_header_changes_during_detail_read_fail_closed(env):
    original = env['sql'].side_effect
    async def sql(query, maximum):
        response = await original(query, maximum)
        if query.startswith('SELECT TOP 101'):
            env['header']['ReversalStatus'] = '2'
        return response
    env['sql'].side_effect = sql
    assert 'error' in reconcile(env)


def test_registry_readonly_routing_and_real_mcp_schema(env):
    names = {'get_inspection_result_history', 'find_quality_execution_candidates', 'reconcile_quality_execution'}
    assert all(get_tool_func(n) and TOOL_POLICIES[n].effect == 'read' for n in names)
    all_tools = [{'function': {'name': n}} for n in TOOL_POLICIES]
    selected = select_tools(all_tools, [{'role': 'user', 'content': '查看质量检验逐点历史并核对原提交'}])
    assert names <= {t['function']['name'] for t in selected}
    assert all(n in ROUTING_INSTRUCTION for n in names)
    async def run():
        async with Client(mcp) as client:
            schemas = {t.name: t.input_schema for t in await client.list_tools()}
            assert all(schemas[n]['additionalProperties'] is False for n in names)
            candidates = await client.call_tool('find_quality_execution_candidates', {'plan_id': 'plan', 'plan_digest': env['digest']})
            txn = json.loads(candidates.content[0].text)['rows'][0]['TxnId']
            detail = await client.call_tool('get_inspection_result_history', {'summary_history_id': ROOT})
            assert json.loads(detail.content[0].text)['samples'][0]['points'][0]['DataValue'] == '1.00'
            ok = await client.call_tool('reconcile_quality_execution', {'plan_id': 'plan', 'plan_digest': env['digest'], 'candidate_txn_id': TXN})
            assert txn == TXN
            assert json.loads(ok.content[0].text)['comparison'] == 'payload_match'
            for bad in (True, '2', 2.0):
                result = await client.call_tool('find_quality_execution_candidates',
                    {'plan_id': 'plan', 'plan_digest': env['digest'], 'limit': bad}, raise_on_error=False)
                assert result.is_error
            unknown = await client.call_tool('reconcile_quality_execution',
                {'plan_id': 'plan', 'plan_digest': env['digest'], 'candidate_txn_id': TXN, 'force_success': True}, raise_on_error=False)
            assert unknown.is_error
    asyncio.run(run())



def test_legacy_missing_move_option_remains_readable_but_not_write_transport(env):
    before=journal()
    with pytest.raises(ValueError):p.transport(env['snap'])
    saved,_,_=h.attempted_plan('plan',env['digest'])
    assert 'moveContainerOnCompletion' not in saved['body']
    assert reconcile(env)['comparison']=='payload_match'
    assert journal()==before
