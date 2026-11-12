import asyncio
import copy
import json
import re
import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from tools import mcp, quality_history as history, quality_plans as plans, quality_spc_evidence as evidence
from tools import quality_spc_annotations as annotations, quality_spc_violations as violations
from tests.test_quality_spc_annotations import env as annotation_env, POINT, CID, DEF, CHART, history as annotation_row
from tests.test_quality_spc_violations import env as spc_env, prepare as prepare_violation, history as violation_row
from tests.test_quality_spc_annotations import prepare as prepare_annotation


TXN = '0000000000000401'
MAINLINE = '0000000000000402'
SUMMARY = '0000000000000403'
CHART_HISTORY = '0000000000000404'
DETAIL = '0000000000000405'


def result(coro):
    return json.loads(asyncio.run(coro))


@pytest.fixture(params=['spc_annotation', 'spc_violation'])
def native(spc_env, request, monkeypatch):
    state = spc_env
    kind = request.param
    state['histories'] = [annotation_row()]
    if kind == 'spc_violation':
        state['violation_history'] = [violation_row() | {'ViolationName': 'Previously recorded other violation'}]
    state.update(native={label: [] for label in evidence.TABLES}, roots=[], read_hook=None, reads=0)
    prior = annotations.q.sql_rows

    async def sql(query, maximum):
        state['reads'] += 1
        if state['read_hook']:
            state['read_hook'](query)
        if 'INFORMATION_SCHEMA.COLUMNS' in query:
            # Emulate the SELECT's requested column subset from an independent
            # database catalog; older preparation queries request larger sets.
            known = violations.SCHEMA
            rows = []
            for table, columns in re.findall(r"TABLE_NAME='([^']+)' AND COLUMN_NAME IN \(([^)]+)\)", query):
                requested = set(re.findall(r"'([^']+)'", columns))
                rows.extend(dict(TABLE_NAME=table, COLUMN_NAME=column)
                            for column in known[table].split(',') if column in requested)
            return rows
        if ' FROM HistoryMainline WHERE Comments=' in query:
            offset = int(re.search(r'OFFSET (\d+) ROWS', query)[1])
            return copy.deepcopy(state['roots'][offset:offset+maximum])
        for label, (table, _, _) in evidence.TABLES.items():
            if ' FROM '+table+' WHERE TxnId=' in query:
                return copy.deepcopy(state['native'][label])
        return await prior(query, maximum)

    monkeypatch.setattr(annotations.q, 'sql_rows', AsyncMock(side_effect=sql))
    ready = prepare_annotation() if kind == 'spc_annotation' else prepare_violation()
    assert ready.get('state') == 'ready', ready
    snap = result(plans.get_quality_transaction_plan(ready['plan_id']))['snapshot']
    assert snap['spc_evidence_version'] == 1
    assert result(plans.execute_quality_transaction(ready['plan_id'], ready['plan_digest']))['state'] == 'unknown'
    with plans.db_connect() as db:
        start, finish = db.execute('SELECT started,finished FROM attempts WHERE plan_id=?', (ready['plan_id'],)).fetchone()
    date = datetime.fromtimestamp((start+finish)/2, timezone.utc).isoformat().replace('+00:00', 'Z')
    root = dict(HistoryMainlineId=MAINLINE, HistoryId=POINT, TxnId=TXN, ContainerId=CID, ChangeCount='1',
                ReversalStatus='1', TxnDateGMT=date, TxnType=str(evidence.SERVICES[kind][1]), Comments=snap['body']['comments'])
    state['roots'] = [root]
    state['native']['mainlines'] = [copy.deepcopy(root)]
    if kind == 'spc_annotation':
        row = annotation_row() | dict(AddSPCAnnotationHistoryId=DETAIL, HistoryMainlineId=MAINLINE, TxnId=TXN,
            MainlineTxnId=TXN, TxnDateGMT=date, Comments=snap['body']['comments'], Annotation=snap['body']['annotation'])
        state['histories'].append(row)
        state['native']['annotations'] = [{field: row[field] for field in annotations.SCHEMA['AddSPCAnnotationHistory'].split(',')}]
        state['native']['annotations'][0]['Exclude'] = 'false'
    else:
        row = violation_row() | dict(SPCChartViolationsHistDtlsId=DETAIL, ParentId=CHART_HISTORY, HistoryId=POINT,
            TxnId=TXN, ChartTxnId=TXN, SummaryTxnId=TXN, MainlineTxnId=TXN, MainlineHistoryId=POINT,
            HistoryMainlineId=MAINLINE, RecordSPCChartViolationHistDId=CHART_HISTORY,
            RecordSPCViolationSummaryId=SUMMARY, Comments=snap['body']['comments'], TxnDateGMT=date)
        state['violation_history'].append(row)
        state['native']['summaries'] = [dict(RecordSPCViolationSummaryId=SUMMARY, HistoryMainlineId=MAINLINE, TxnId=TXN)]
        state['native']['charts'] = [dict(RecordSPCChartViolationHistDId=CHART_HISTORY, RecordSPCViolationSummaryId=SUMMARY,
                                        SPCChartDefId=CHART, SPCChartDefName='Diameter chart', TxnId=TXN)]
        state['native']['violations'] = [{field: row[field] for field in violations.SCHEMA['SPCViolationHistoryDetail'].split(',')}]
    state.update(ready=ready, snap=snap, kind=kind)
    return state


def reconcile(state, txn=None):
    ready = state['ready']
    return result(history.reconcile_quality_execution(ready['plan_id'], ready['plan_digest'], txn))


def review(state, txn=TXN):
    ready = state['ready']
    return result(plans.review_quality_transaction(ready['plan_id'], ready['plan_digest'], 'resolve_history',
                                                   txn, '核对完整原生登记、原始点和配置保持，不能据此认证SPC引擎。', 'committed'))


def assert_unknown(state):
    with plans.db_connect() as db:
        assert db.execute('SELECT state FROM plans WHERE id=?', (state['ready']['plan_id'],)).fetchone()[0] == 'unknown'
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 0
    state['client'].request.assert_awaited_once()


def test_actual_plan_to_readonly_match_to_confirmed_audit(native):
    out = reconcile(native)
    assert out['request_correlation_verified'] and out['recorded_business_effects_verified'], json.dumps(out, ensure_ascii=False)
    assert out['candidate_txn_id'] == TXN and out['review_action'] == 'resolve_history'
    assert all(out['checks'].values()) and all(out['current_checks'].values())
    assert not out['state_changed'] and not out['verified_success'] and not out['native_engine_result_verified']
    assert not out['native_control_limits_verified'] and not out['release_authorized']
    assert_unknown(native)
    saved = review(native)
    assert saved['state_changed'] and saved['state'] == 'history_committed', saved
    attestation = saved['history_attestation']
    assert saved['verified_success'] and not saved['mes_write_performed'] and not saved['original_plan_reusable']
    assert attestation['candidate_txn_id'] == TXN and len(attestation['evidence_digest']) == 64
    assert not attestation['native_engine_result_verified'] and not attestation['reported_result_provenance_verified']
    assert not attestation['release_authorized']
    assert 'error' in review(native)
    assert 'error' in result(plans.execute_quality_transaction(native['ready']['plan_id'], native['ready']['plan_digest']))
    native['client'].request.assert_awaited_once()


@pytest.mark.parametrize('case', ['no_records', 'duplicate_marker', 'wrong_candidate', 'reversed', 'wrong_type',
    'wrong_time', 'unfinished_attempt', 'extra_mainline', 'missing_child', 'extra_child', 'cross_txn', 'wrong_parent',
    'wrong_payload', 'changed_source', 'changed_config', 'excluded', 'unexpected_other_service', 'old_record',
    'previous_history_missing', 'previous_history_changed', 'foreign_extra_child'])
def test_incomplete_or_ambiguous_native_evidence_cannot_release_occupancy(native, case):
    label = 'annotations' if native['kind'] == 'spc_annotation' else 'violations'
    rows = native['native'][label]
    if case == 'no_records': native['roots'] = []
    elif case == 'duplicate_marker': native['roots'].append(native['roots'][0] | {'HistoryMainlineId': SUMMARY, 'TxnId': SUMMARY})
    elif case == 'reversed':
        native['roots'][0]['ReversalStatus'] = '2'; native['native']['mainlines'][0]['ReversalStatus'] = '2'
    elif case == 'wrong_type':
        native['roots'][0]['TxnType'] = '4000'; native['native']['mainlines'][0]['TxnType'] = '4000'
    elif case == 'wrong_time':
        native['roots'][0]['TxnDateGMT'] = '2000-01-01T00:00:00Z'; native['native']['mainlines'][0]['TxnDateGMT'] = '2000-01-01T00:00:00Z'
    elif case == 'unfinished_attempt':
        with plans.db_connect() as db: db.execute('UPDATE attempts SET finished=NULL')
    elif case == 'extra_mainline': native['native']['mainlines'].append(copy.deepcopy(native['native']['mainlines'][0]))
    elif case == 'missing_child': native['native'][label] = []
    elif case == 'extra_child': rows.append(copy.deepcopy(rows[0]))
    elif case == 'foreign_extra_child':
        identity = evidence.TABLES[label][2]
        rows.append(rows[0] | {identity: '0000000000000501', 'TxnId': '0000000000000502'})
    elif case == 'cross_txn': rows[0]['TxnId'] = SUMMARY
    elif case == 'wrong_parent': rows[0]['HistoryMainlineId' if label == 'annotations' else 'ParentId'] = DEF
    elif case == 'wrong_payload': rows[0]['Annotation' if label == 'annotations' else 'UCL'] = '999'
    elif case == 'changed_source': native['source']['DataValue'] = '10.2'
    elif case == 'changed_config': native['chart_count'] += 1
    elif case == 'excluded': native['histories'][0]['Exclude'] = True
    elif case in ('previous_history_missing', 'previous_history_changed'):
        history_rows = native['histories'] if label == 'annotations' else native['violation_history']
        if case == 'previous_history_missing': history_rows.pop(0)
        else: history_rows[0]['Annotation' if label == 'annotations' else 'ViolationName'] = 'altered previous record'
    elif case == 'unexpected_other_service':
        if label == 'annotations': native['native']['summaries'] = [dict(RecordSPCViolationSummaryId=SUMMARY, HistoryMainlineId=MAINLINE, TxnId=TXN)]
        else: native['native']['annotations'] = [{field: annotation_row()[field] for field in annotations.SCHEMA['AddSPCAnnotationHistory'].split(',')}]
    elif case == 'old_record':
        snap = native['snap']; history_label = 'annotation_history' if label == 'annotations' else 'violation_history'
        snap['before'][history_label] = copy.deepcopy(native['histories'] if label == 'annotations' else native['violation_history'])
        with plans.db_connect() as db:
            fingerprint = plans.digest(snap); db.execute('UPDATE plans SET payload=?,digest=?', (json.dumps(snap), fingerprint))
        native['ready']['plan_digest'] = fingerprint
    out = reconcile(native, SUMMARY if case == 'wrong_candidate' else None)
    assert not out['request_correlation_verified'], out
    assert not review(native, SUMMARY if case == 'wrong_candidate' else TXN)['state_changed']
    assert_unknown(native)


@pytest.mark.parametrize('table', ['mainlines', 'annotations', 'summaries', 'charts', 'violations'])
def test_missing_history_columns_refuse_audit(native, monkeypatch, table):
    prior = annotations.q.sql_rows
    async def sql(query, maximum):
        rows = await prior(query, maximum)
        if 'INFORMATION_SCHEMA.COLUMNS' in query:
            return [row for row in rows if not (row['TABLE_NAME'] == evidence.TABLES[table][0]
                                               and row['COLUMN_NAME'] == evidence.TABLES[table][2])]
        return rows
    monkeypatch.setattr(annotations.q, 'sql_rows', AsyncMock(side_effect=sql))
    assert 'error' in reconcile(native)
    assert not review(native)['state_changed']
    assert_unknown(native)


def test_history_or_current_changes_during_read_are_rejected(native):
    count = 0
    def change(query):
        nonlocal count
        if ' FROM HistoryMainline WHERE Comments=' in query:
            count += 1
            if count == 2: native['roots'][0]['ChangeCount'] = '2'
    native['read_hook'] = change
    assert 'error' in reconcile(native)
    assert_unknown(native)


def test_old_snapshot_keeps_manual_path(native):
    snap = native['snap']; snap.pop('spc_evidence_version')
    fingerprint = plans.digest(snap)
    with plans.db_connect() as db: db.execute('UPDATE plans SET payload=?,digest=?', (json.dumps(snap), fingerprint))
    native['ready']['plan_digest'] = fingerprint
    out = reconcile(native)
    assert out['comparison'] == 'manual_review_required' and out['evidence_tool'] == 'inspect_quality_transaction'
    assert not review(native)['state_changed']
    assert_unknown(native)


def test_inflight_match_requires_recovery_before_final_audit(native):
    with plans.db_connect() as db: db.execute("UPDATE plans SET state='in_flight'")
    assert reconcile(native)['review_action'] == 'recover'
    assert not review(native)['state_changed']
    ready = native['ready']
    recovered = result(plans.review_quality_transaction(ready['plan_id'], ready['plan_digest'], 'recover',
        'local:stopped-worker', '已确认执行进程停止，只恢复本地状态并继续核对原生历史。'))
    assert recovered['state'] == 'unknown' and not recovered['verified_success']
    assert review(native)['state'] == 'history_committed'


def test_audit_database_failure_rolls_back_state_and_review(native):
    with plans.db_connect() as db:
        db.execute("CREATE TRIGGER fail_audit BEFORE UPDATE ON plans BEGIN SELECT RAISE(ABORT,'fault'); END")
    with pytest.raises(sqlite3.IntegrityError): review(native)
    assert_unknown(native)


def test_global_candidate_listing_and_actual_mcp_reconciliation(native):
    ready = native['ready']
    page = result(history.find_quality_execution_candidates(ready['plan_id'], ready['plan_digest'], limit=1))
    assert len(page['rows']) == 1 and page['rows'][0]['TxnId'] == TXN and not page['state_changed']
    assert page['target_linkage_available'] is False
    async def call():
        async with Client(mcp) as client:
            reply = await client.call_tool('reconcile_quality_execution', dict(plan_id=ready['plan_id'], plan_digest=ready['plan_digest']))
            return json.loads(reply.content[0].text)
    out = asyncio.run(call())
    assert out['request_correlation_verified'] and not out['state_changed']
    queries = [call.args[0] for call in annotations.q.sql_rows.await_args_list]
    assert any(' FROM AddSPCAnnotationHistory WHERE TxnId=' in query and ' OR HistoryMainlineId IN (' in query for query in queries)
    if native['kind'] == 'spc_violation':
        assert any(' FROM SPCChartHistoryDetail WHERE TxnId=' in query and ' OR RecordSPCViolationSummaryId IN (' in query for query in queries)
        assert any(' FROM SPCViolationHistoryDetail WHERE TxnId=' in query and ' OR ParentId IN (' in query for query in queries)
    assert_unknown(native)
