import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from tools import production_records as r, production_reporting as p

CID, OID, UNIT, STEP, EMP, STATUS = [f'{n:016x}' for n in range(1, 7)]
CONTEXT = {'container_id': CID, 'container_name': 'C', 'order_id': OID, 'order_name': 'MO',
           'unit_id': UNIT, 'step_id': STEP, 'status_id': STATUS, 'step_pass': 1}


def run(coro):
    return json.loads(asyncio.run(coro))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(r, 'LEDGER_DB', tmp_path / 'ledger.sqlite3')
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path / 'plans.sqlite3')
    monkeypatch.setattr(r, 'order_context', AsyncMock(return_value={'name': 'MO', 'instance_id': OID}))
    async def resolve(self, entity, ref, revisioned=True):
        return {'Name': 'EMP', 'InstanceID': EMP, 'IsFrozen': False}
    monkeypatch.setattr(r.ReadContext, 'resolve', resolve)


def labor(**kw):
    return r.record_production_labor(**(dict(entry_key='L-1', mfgorder_name='MO', employee_name='EMP',
        start_time='2026-11-08T08:00:00+08:00', end_time='2026-11-08T09:00:00+08:00', actual_minutes='45') | kw))


def native_sources():
    throughput = {'service': 'Thruput', 'qty': 10, 'container_id': CID,
        'counters': {'UOMId': UNIT, 'CurrentStatusId': STATUS, 'StepPass': '1'},
        'status': {'Container': {'Name': 'C'}, 'MfgOrder': {'InstanceId': OID, 'Name': 'MO'}, 'Step': {'InstanceId': STEP}}}
    defect = {'service': 'ContainerDefect', 'context': copy.deepcopy(CONTEXT), 'reject_qty': '2', 'inspected_qty': '10'}
    loss = {'service': 'ChangeQty', 'kind': 'quantity_loss', 'target_id': CID, 'expected_loss_quantity': '2',
        'production_context': copy.deepcopy(CONTEXT), 'before': {'container': {'UOMId': UNIT, 'WorkflowStepId': STEP}}}
    return {'T': throughput, 'D': defect, 'S': loss}


def report(disposition='record', **kw):
    return r.record_production_report(**(dict(report_key='R-1', good_qty='8', reject_qty='2', disposition=disposition,
        throughput_plan_id='T', throughput_plan_digest='td', reject_plan_id='S' if disposition == 'scrap' else 'D',
        reject_plan_digest='rd') | kw))


def test_manual_actual_labor_is_explicit_local_and_idempotent(monkeypatch):
    first = run(labor())
    assert first['source'] == 'application_production_ledger'
    assert first['record']['manual_labor_hours'] == '0.75'
    assert first['record']['native_task_time_written'] is False
    second = run(labor(start_time='2026-11-08T00:00:00Z', end_time='2026-11-08T01:00:00Z'))
    assert second['record_id'] == first['record_id'] and second['idempotent_replay'] is True
    changed = run(labor(actual_minutes='40'))
    assert changed['saved'] is False


def test_overlap_rejected_but_adjacent_intervals_allowed():
    run(labor())
    bad = run(labor(entry_key='L-2', start_time='2026-11-08T08:30:00+08:00', end_time='2026-11-08T09:30:00+08:00'))
    assert '重叠' in bad['error']
    good = run(labor(entry_key='L-3', start_time='2026-11-08T09:00:00+08:00', end_time='2026-11-08T10:00:00+08:00'))
    assert 'record_id' in good


@pytest.mark.parametrize('change', [dict(actual_minutes='61'), dict(actual_minutes='0'), dict(actual_minutes='NaN'),
    dict(start_time='2026-11-08T08:00:00'), dict(end_time='2026-11-08T07:00:00+08:00'),
    dict(container_instance_id=CID)])
def test_invalid_labor_never_saved(change):
    assert run(labor(**change))['saved'] is False


@pytest.mark.parametrize('disposition', ['record', 'scrap'])
def test_good_and_bad_units_saved_once_with_native_sources(monkeypatch, disposition):
    sources = native_sources()
    monkeypatch.setattr(r, 'reviewed_source', lambda kind, key, signature: copy.deepcopy(sources[key]))
    result = run(report(disposition))
    assert result['record']['good_qty'] == '8' and result['record']['reject_qty'] == '2'
    assert result['record']['quantity_deduction'] == (disposition == 'scrap')
    assert result['mes_write_performed'] is False
    assert run(report(disposition))['idempotent_replay'] is True
    duplicate = run(report(disposition, report_key='R-2'))
    assert '不能重复累计' in duplicate['error']
    summary = run(r.get_production_reports('MO'))
    assert summary['summary_by_step_and_unit'][0]['good_qty'] == '8'
    assert summary['order_completed_qty'] is None


@pytest.mark.parametrize('field,value', [('unit_id', EMP), ('step_id', EMP), ('container_id', EMP),
    ('order_id', EMP), ('step_pass', 2), ('status_id', EMP)])
def test_reject_and_throughput_context_must_match(monkeypatch, field, value):
    sources = native_sources()
    sources['D']['context'][field] = value
    monkeypatch.setattr(r, 'reviewed_source', lambda kind, key, signature: sources[key])
    assert run(report())['saved'] is False


def test_yield_counts_and_defects_are_not_interchangeable(monkeypatch):
    sources = native_sources()
    monkeypatch.setattr(r, 'reviewed_source', lambda kind, key, signature: sources[key])
    assert run(report(good_qty='9'))['saved'] is False
    sources['D']['reject_qty'] = '3'
    assert run(report())['saved'] is False


def test_unreviewed_native_plan_cannot_be_reported(monkeypatch):
    with p.db_connect() as db:
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)', ('T', 'td', '{}', 'unknown', p.time.time()))
    assert run(report())['saved'] is False


def test_scrap_choice_requires_explicit_effect_flags(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(p, 'request', request)
    result = run(r.prepare_production_reject('C', '2', 'scrap', 'LOSS'))
    assert result['executable'] is False
    request.assert_not_awaited()


def test_record_choice_rejects_scrap_flags_and_missing_defect_count(monkeypatch):
    assert run(r.prepare_production_reject('C', '2', 'record', 'DEF', close_when_zero=True))['executable'] is False
    monkeypatch.setattr(r, 'report_context', AsyncMock(return_value=(CONTEXT, {
        'CDOTypeId': '1040', 'Status': '1', 'CurrentHoldCount': '0', 'ChildCount': '0',
        'ParentContainerId': '', 'CarrierId': '', 'SamplingLotId': '', 'InRework': 'false', 'Qty': '10'}, {})))
    result = run(r.prepare_production_reject('C', '2', 'record', 'DEF', inspected_qty='10'))
    assert result['executable'] is False and 'defect_count' in result['error']


def test_scrap_wrapper_uses_typed_loss_plan_and_binds_order(monkeypatch):
    from tools import quality_plans
    monkeypatch.setattr(r, 'report_context', AsyncMock(return_value=(CONTEXT, {'Qty': '2'}, {})))
    prepare = AsyncMock(return_value='{"plan_id":"loss-plan","plan_digest":"digest","state":"ready"}')
    monkeypatch.setattr(quality_plans, 'prepare', prepare)
    result = run(r.prepare_production_reject('C', '2', 'scrap', 'LOSS', close_when_zero=True, adjust_throughput=False))
    assert result['execute_tool'] == 'execute_quality_transaction'
    arguments = prepare.await_args.args[1]
    assert arguments['record_all_quantity'] is True and arguments['loss_quantity'] is None
    assert arguments['production_context'] == CONTEXT


def test_record_plan_emits_native_defects_not_quantity_loss(monkeypatch):
    snap = {'service': 'ContainerDefect', 'container_id': CID, 'reject_qty': '2',
        'environment': p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),
        'query_environment': p.CAMSTAR_QUERY_BASE_URL.rstrip('/'),
        'body': {'container': {'name': 'C', 'instanceId': CID}, 'qtyInspected': 10,
                 'serviceDetails': [{'defectCount': 3, 'reasonCode': {'name': 'DEF', 'instanceId': EMP}}]}}
    monkeypatch.setattr(r, 'reject_snapshot', AsyncMock(return_value=snap))
    result = run(r.prepare_production_reject('C', '2', 'record', 'DEF', inspected_qty='10', defect_count=3))
    assert result['quantity_deduction'] is False
    assert result['request']['serviceDetails'][0]['defectCount'] == 3
    request = AsyncMock(return_value='{"CompletionMsg":"done"}')
    monkeypatch.setattr(p, 'request', request)
    monkeypatch.setattr(r, 'reject_history', AsyncMock(return_value=[]))
    args = {k: result[k] for k in ('plan_id', 'plan_digest')}
    execution = run(r.execute_production_reject(**args))
    assert execution['state'] == 'unknown' and request.await_count == 1
    assert request.await_args.args == ('POST', '/api/ContainerDefect')
    assert 'qty' not in request.await_args.kwargs['body']
    assert run(r.execute_production_reject(**args))['submitted'] is False


def test_manual_and_elapsed_are_separate_and_summary_is_not_page_total(monkeypatch):
    run(labor())
    monkeypatch.setattr(p, 'query_rows', AsyncMock(return_value=[{'IntervalCount': '3',
        'ElapsedMicroseconds': '7200000000', 'IncludedCount': '2', 'ExcludedCount': '1'}]))
    result = run(r.get_production_worktime('MO', offset=1, limit=1))
    assert result['manual']['total_hours'] == '0.75' and result['manual']['records'] == []
    assert result['automatic']['recorded_elapsed_hours'] == '2'
    assert result['automatic']['coverage_complete'] is False
    assert result['combined_hours'] is None
    query = p.query_rows.await_args.args[0]
    assert "TxnTypeName='MoveStd'" in query and 'ReversalStatus=1' in query
    assert 'JOIN MoveHistory' in query and 'JOIN MoveInHistory' in query and 'MatchingMoveIns=1' in query
    assert 'entered.ActualMoveInGMT' in query and 'i.StepEntryTxnId=h.StepEntryTxnId' in query
    assert 'NativeRows=1' in query and 'MICROSECOND' in query


def test_worktime_query_failure_preserves_manual_data(monkeypatch):
    run(labor())
    monkeypatch.setattr(p, 'query_rows', AsyncMock(side_effect=ValueError('query unavailable')))
    result = run(r.get_production_worktime('MO'))
    assert result['complete'] is False and result['manual']['total_hours'] == '0.75'
    assert 'automatic' not in result


def test_ledger_corruption_is_not_silently_summed():
    record = run(labor())
    with r.connect() as db:
        db.execute('UPDATE records SET digest=? WHERE id=?', ('bad', record['record_id']))
    with pytest.raises(ValueError, match='摘要损坏'):
        r.ledger_records('labor', OID)


def test_completion_requires_native_date_and_post_procedure(monkeypatch):
    from tools import quality_plans
    monkeypatch.setattr(quality_plans, 'PLAN_DB', r.LEDGER_DB.with_name('quality.sqlite3'))
    monkeypatch.setattr(p, 'query_rows', AsyncMock(side_effect=[
        [{'MfgOrderId': OID, 'MfgOrderName': 'MO', 'ProductionCompleteDateGMT': '',
          'PostProductionProcedureId': EMP, 'PostProductionCompleteDateGMT': ''}],
        [{'IncompleteActiveContainers': '2'}]]))
    result = run(r.get_mfgorder_completion_readiness('MO'))
    assert result['complete'] is True and result['ready_to_record_local_completion'] is False
    assert len(result['blockers']) == 3
    assert result['native_close_executed'] is False


def test_completion_records_local_closeout_once_without_native_close(monkeypatch):
    ready = {'complete': True, 'ready_to_record_local_completion': True,
        'order': {'name': 'MO', 'instance_id': OID},
        'native_completion': {'ProductionCompleteDateGMT': '2026-11-08T01:00:00'}}
    monkeypatch.setattr(r, 'get_mfgorder_completion_readiness', AsyncMock(return_value=json.dumps(ready)))
    result = run(r.record_mfgorder_completion('DONE-1', 'MO'))
    assert result['record']['native_close_executed'] is False and result['mes_write_performed'] is False
    assert run(r.record_mfgorder_completion('DONE-1', 'MO'))['idempotent_replay'] is True
    assert run(r.record_mfgorder_completion('DONE-2', 'MO'))['saved'] is False


def test_yield_records_are_grouped_by_step_and_unit(monkeypatch):
    sources = native_sources()
    monkeypatch.setattr(r, 'reviewed_source', lambda kind, key, signature: copy.deepcopy(sources[key]))
    run(report())
    sources['T2'] = copy.deepcopy(sources['T'])
    sources['D2'] = copy.deepcopy(sources['D'])
    sources['T2']['status']['Step']['InstanceId'] = EMP
    sources['D2']['context']['step_id'] = EMP
    run(report(report_key='R-2', throughput_plan_id='T2', reject_plan_id='D2'))
    result = run(r.get_production_reports('MO', limit=1))
    assert len(result['summary_by_step_and_unit']) == 2 and result['page']['has_more'] is True
    assert all(g['good_qty'] == '8' for g in result['summary_by_step_and_unit'])


def test_report_and_labor_tools_are_registered_and_routed():
    from tools import get_tool_func, mcp
    from core.tool_catalog import TOOL_POLICIES
    from agent.tool_routing import select_tools
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    for name in ('record_production_labor', 'prepare_production_reject', 'record_production_report', 'record_mfgorder_completion'):
        assert name in names and get_tool_func(name) is not None
    all_tools = [{'function': {'name': n}} for n in TOOL_POLICIES]
    for message in ('记录员工的人工工时45分钟', '登记工单的良品报工', '完成整单收尾'):
        selected = {t['function']['name'] for t in select_tools(all_tools, [{'role': 'user', 'content': message}])}
        assert 'record_production_labor' in selected
    selected = {t['function']['name'] for t in select_tools(all_tools, [{'role': 'user', 'content': '查询工单人工工时和自动历时'}])}
    assert 'get_production_worktime' in selected and 'record_production_labor' not in selected


def test_complete_report_requires_both_native_reviews(monkeypatch):
    sources = native_sources()
    throughput = {**sources['T'], 'environment': p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),
        'query_environment': p.CAMSTAR_QUERY_BASE_URL.rstrip('/'), 'remaining_qty': 10,
        'body': {'container': {'name': 'C', 'instanceId': CID}, 'qty': 10, 'thruputAllQty': False}}
    defect = {**sources['D'], 'container_id': CID, 'environment': throughput['environment'],
        'query_environment': throughput['query_environment'],
        'body': {'container': {'name': 'C', 'instanceId': CID}, 'qtyInspected': 10,
                 'serviceDetails': [{'defectCount': 3, 'reasonCode': {'name': 'DEF', 'instanceId': EMP}}]}}
    async def snapshot(args):
        return copy.deepcopy(throughput if args['service'] == 'Thruput' else defect)
    monkeypatch.setattr(p, 'checked_snapshot', snapshot)
    monkeypatch.setattr(r, 'reject_snapshot', AsyncMock(return_value=defect))
    monkeypatch.setattr(p, 'request', AsyncMock(return_value='{"CompletionMsg":"accepted"}'))
    monkeypatch.setattr(p, 'query_rows', AsyncMock(return_value=[]))
    monkeypatch.setattr(r, 'reject_history', AsyncMock(return_value=[]))
    tp = run(p.prepare_container_throughput('C', 10))
    tid = {k: tp[k] for k in ('plan_id', 'plan_digest')}
    run(p.execute_container_throughput(**tid))
    run(p.review_container_throughput(**tid, conclusion='committed', evidence_reference='final throughput TX1', review_note='核实最终原生事务。'))
    rp = run(r.prepare_production_reject('C', '2', 'record', 'DEF', inspected_qty='10', defect_count=3))
    rid = {k: rp[k] for k in ('plan_id', 'plan_digest')}
    run(r.execute_production_reject(**rid))
    args = dict(report_key='REPORT', good_qty='8', reject_qty='2', disposition='record',
        throughput_plan_id=tid['plan_id'], throughput_plan_digest=tid['plan_digest'],
        reject_plan_id=rid['plan_id'], reject_plan_digest=rid['plan_digest'])
    assert run(r.record_production_report(**args))['saved'] is False
    run(p.review_container_throughput(**rid, conclusion='committed', evidence_reference='final defect TX2', review_note='核实缺陷事务且未扣量。'))
    saved = run(r.record_production_report(**args))
    assert saved['saved'] is True and saved['record']['processed_qty'] == '10'
    assert saved['record']['quantity_deduction'] is False
    assert p.request.await_count == 2  # Posting the local ledger never sends another native request.


def test_unknown_quality_claim_blocks_new_native_reporting(monkeypatch):
    from tools import quality_plans, quality_lot_execution
    monkeypatch.setattr(quality_plans, 'PLAN_DB', r.LEDGER_DB.with_name('quality.sqlite3'))
    snapshot = {'kind': 'quantity_loss', 'environment': quality_plans.environment(),
        'resource_writes': [quality_lot_execution.runtime_claim('container', CID)]}
    with quality_plans.db_connect() as db:
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',
                   ('busy', 'digest', json.dumps(snapshot), 'unknown', p.time.time()))
    with pytest.raises(ValueError, match='未核实质量事务'):
        p.ensure_no_unresolved_quality(CID)
