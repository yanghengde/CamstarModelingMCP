import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from tools import production_reporting as p

CID = '0000000000000001'
OID = '0000000000000002'


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path / 'plans.sqlite3')


def snapshot():
    return {'environment': p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),
            'query_environment': p.CAMSTAR_QUERY_BASE_URL.rstrip('/'), 'container_id': CID,
            'qty': 3, 'remaining_qty': 10, 'service': 'Thruput',
            'body': {'container': {'name': 'LOT-1', 'instanceId': CID}, 'qty': 3, 'thruputAllQty': False}}


def plan(monkeypatch):
    monkeypatch.setattr(p, 'checked_snapshot', AsyncMock(return_value=snapshot()))
    result = json.loads(run(p.prepare_container_throughput('LOT-1', 3)))
    assert result['state'] == 'ready'
    return {'plan_id': result['plan_id'], 'plan_digest': result['plan_digest']}


def mock_context(monkeypatch, *, in_process=False, current=2, adjustment=1, child=0):
    status = {'Container': {'Name': 'LOT-1', 'InstanceId': CID}, 'Status': 1,
              'InProcess': in_process, 'Operation': {'Name': 'OP'}, 'ContainerLevel': {'Name': 'PCB'}}
    monkeypatch.setattr(p, 'container_state', AsyncMock(return_value=(status, CID)))
    async def resolve(self, entity, ref, revisioned=True):
        return {'Operations': {'InstanceID': OID, 'ThruputReportingLevel': {'Name': 'PCB'}},
                'ContainerLevels': {'InstanceID': CID, 'Name': 'PCB', 'AllowMove': True},
                'Resources': {'InstanceID': OID, 'Name': 'RES', 'IsFrozen': False}}[entity]
    monkeypatch.setattr(p.ReadContext, 'resolve', resolve)
    counters = {'ContainerId': CID, 'Qty': 10, 'Qty2': 0, 'ChangeCount': 3,
                'CurrentThruputQty': current, 'ThruputAdjustmentQty': adjustment,
                'ChildCount': child, 'ParentContainerId': None, 'CurrentHoldCount': 0, 'ProductionComplete': False}
    reads = AsyncMock(side_effect=[[counters], []])
    monkeypatch.setattr(p, 'query_rows', reads)
    return status, reads


def test_quantity_adjustment_sign_matches_vendor_definition(monkeypatch):
    mock_context(monkeypatch)
    result = run(p.checked_snapshot({'container_name': 'LOT-1', 'qty': 9, 'service': 'Thruput'}))
    assert result['remaining_qty'] == 9  # Qty - (CurrentThruputQty - AdjustmentQty)
    assert result['body']['qty'] == 9
    assert result['body']['thruputAllQty'] is False


def test_real_query_string_scalars_are_parsed_explicitly(monkeypatch):
    mock_context(monkeypatch)
    monkeypatch.setattr(p, 'query_rows', AsyncMock(side_effect=[[
        {'ContainerId': CID, 'Qty': '10', 'ChangeCount': '3', 'CurrentThruputQty': '2',
         'ThruputAdjustmentQty': '1', 'ChildCount': '0', 'ParentContainerId': '',
         'CurrentHoldCount': '0', 'ProductionComplete': 'false'}], []]))
    result = run(p.checked_snapshot({'container_name': 'LOT-1', 'qty': 9, 'service': 'Thruput'}))
    assert result['remaining_qty'] == 9
    assert result['counters']['ProductionComplete'] is False


@pytest.mark.parametrize('qty', [0, -1, True, float('nan'), float('inf'), '3'])
def test_invalid_quantity_never_reads_or_writes(monkeypatch, qty):
    read = AsyncMock()
    write = AsyncMock()
    monkeypatch.setattr(p, 'container_state', read)
    monkeypatch.setattr(p, 'request', write)
    result = json.loads(run(p.prepare_container_throughput('LOT-1', qty)))
    assert result['executable'] is False
    read.assert_not_awaited()
    write.assert_not_awaited()


def test_move_with_throughput_only_explicit_all_remaining(monkeypatch):
    _, reads = mock_context(monkeypatch)
    result = json.loads(run(p.prepare_container_throughput('LOT-1', 3, service='MoveWithThruput')))
    assert result['executable'] is False
    assert reads.await_count == 1
    mock_context(monkeypatch)
    result = json.loads(run(p.prepare_container_throughput('LOT-1', 9, service='MoveWithThruput')))
    assert result['request'] == {'container': {'name': 'LOT-1', 'instanceId': CID},
                                 'moveAllQty': True, 'thruputAllQty': True, 'close': False}
    assert 'qty' not in result['request']  # Compound service has no qty root field.


@pytest.mark.parametrize('settings', [{'in_process': True}, {'child': 1}])
def test_compound_move_rejects_processing_or_hierarchy(monkeypatch, settings):
    mock_context(monkeypatch, **settings)
    write = AsyncMock()
    monkeypatch.setattr(p, 'request', write)
    result = json.loads(run(p.prepare_container_throughput('LOT-1', 9, service='MoveWithThruput')))
    assert result['executable'] is False
    write.assert_not_awaited()


def test_view_row_id_not_bound_into_plan(monkeypatch):
    status, _ = mock_context(monkeypatch)
    status['InstanceID'] = 'temporary-view-id-1'
    first = run(p.checked_snapshot({'container_name': 'LOT-1', 'qty': 3, 'service': 'Thruput'}))
    status, _ = mock_context(monkeypatch)
    status['InstanceID'] = 'temporary-view-id-2'
    second = run(p.checked_snapshot({'container_name': 'LOT-1', 'qty': 3, 'service': 'Thruput'}))
    assert p.digest(first) == p.digest(second)


def test_single_submit_and_unknown_are_not_replayed(monkeypatch):
    args = plan(monkeypatch)
    write = AsyncMock(return_value='{"CompletionMsg":"ok"}')
    monkeypatch.setattr(p, 'request', write)
    monkeypatch.setattr(p, 'query_rows', AsyncMock(return_value=[]))
    result = json.loads(run(p.execute_container_throughput(**args)))
    assert result['submitted'] is True and result['state'] == 'unknown'
    assert result['transaction_verified'] is False
    assert write.await_count == 1
    assert write.await_args.kwargs['params'] == {'execute': 'true'}
    assert 'retry_read_only' not in write.await_args.kwargs
    second = json.loads(run(p.execute_container_throughput(**args)))
    assert second['submitted'] is False
    assert write.await_count == 1
    with pytest.raises(ValueError, match='未核实'):
        p.ensure_no_unresolved_throughput(CID)


def test_legacy_movement_cannot_bypass_unresolved_attempt(monkeypatch):
    from tools import container_moves
    args = plan(monkeypatch)
    with p.db_connect() as db:
        db.execute("UPDATE plans SET state='unknown' WHERE id=?", (args['plan_id'],))
    monkeypatch.setattr(container_moves, '_movement_context', AsyncMock(return_value=CID))
    write = AsyncMock()
    monkeypatch.setattr(container_moves, 'request_shopfloor', write)
    result = run(container_moves.container_move('LOT-1'))
    assert '未核实吞吐请求' in result
    write.assert_not_awaited()


def test_expired_or_tampered_plan_never_submits(monkeypatch):
    args = plan(monkeypatch)
    with p.db_connect() as db:
        db.execute('UPDATE plans SET created=? WHERE id=?', (p.time.time() - p.TTL - 1, args['plan_id']))
    write = AsyncMock()
    monkeypatch.setattr(p, 'request', write)
    assert json.loads(run(p.execute_container_throughput(**args)))['submitted'] is False
    write.assert_not_awaited()


def test_unknown_not_released_while_process_lock_held(monkeypatch):
    args = plan(monkeypatch)
    with p.db_connect() as db:
        db.execute("UPDATE plans SET state='in_flight' WHERE id=?", (args['plan_id'],))
    with p.attempt_lock(p.PLAN_DB, args['plan_id']):
        result = json.loads(run(p.recover_container_throughput_attempt(**args,
            evidence_reference='unexpected interruption', review_note='核对中。')))
    assert 'error' in result
    assert json.loads(run(p.get_container_throughput_plan(args['plan_id'])))['state'] == 'in_flight'


def test_changed_state_or_environment_never_submits(monkeypatch):
    args = plan(monkeypatch)
    changed = {**snapshot(), 'qty': 4}
    monkeypatch.setattr(p, 'checked_snapshot', AsyncMock(return_value=changed))
    write = AsyncMock()
    monkeypatch.setattr(p, 'request', write)
    assert json.loads(run(p.execute_container_throughput(**args)))['submitted'] is False
    monkeypatch.setattr(p, 'CAMSTAR_QUERY_BASE_URL', 'https://different-environment/Query')
    assert json.loads(run(p.execute_container_throughput(**args)))['submitted'] is False
    write.assert_not_awaited()


def test_two_plans_cannot_submit_for_unresolved_container(monkeypatch):
    first = plan(monkeypatch)
    second = plan(monkeypatch)
    write = AsyncMock(return_value='HTTP 500')
    monkeypatch.setattr(p, 'request', write)
    monkeypatch.setattr(p, 'query_rows', AsyncMock(return_value=[]))
    async def execute_both():
        return await asyncio.gather(p.execute_container_throughput(**first), p.execute_container_throughput(**second))
    results = [json.loads(r) for r in run(execute_both())]
    assert sum(r['submitted'] for r in results) == 1
    assert write.await_count == 1


def test_cancellation_preserves_fence(monkeypatch):
    args = plan(monkeypatch)
    monkeypatch.setattr(p, 'request', AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        run(p.execute_container_throughput(**args))
    assert json.loads(run(p.get_container_throughput_plan(args['plan_id'])))['state'] == 'unknown'
    with pytest.raises(ValueError):
        p.ensure_no_unresolved_throughput(CID)


def test_review_final_evidence_does_not_replay(monkeypatch):
    args = plan(monkeypatch)
    monkeypatch.setattr(p, 'request', AsyncMock(return_value='ok'))
    monkeypatch.setattr(p, 'query_rows', AsyncMock(return_value=[]))
    run(p.execute_container_throughput(**args))
    result = json.loads(run(p.review_container_throughput(**args, conclusion='committed',
        evidence_reference='MES final transaction TX123', review_note='服务端请求已结束且最终事务已核实。')))
    assert result['state'] == 'reviewed_committed'
    p.ensure_no_unresolved_throughput(CID)
    assert json.loads(run(p.execute_container_throughput(**args)))['submitted'] is False


def test_interrupted_inflight_recovers_without_releasing_fence(monkeypatch):
    args = plan(monkeypatch)
    with p.db_connect() as db:
        db.execute("UPDATE plans SET state='in_flight' WHERE id=?", (args['plan_id'],))
    review = json.loads(run(p.review_container_throughput(**args, conclusion='not_committed',
        evidence_reference='process stopped', review_note='未核实MES结束。')))
    assert 'error' in review
    result = json.loads(run(p.recover_container_throughput_attempt(**args,
        evidence_reference='process crash audit', review_note='本机进程已结束，MES结果仍未知。')))
    assert result['state'] == 'unknown' and result['container_fence_retained'] is True
    with pytest.raises(ValueError):
        p.ensure_no_unresolved_throughput(CID)


@pytest.mark.parametrize('value', ['NaN', '-1', 'Infinity', True, None])
def test_native_quantities_never_turn_missing_or_bad_values_into_zero(monkeypatch, value):
    raw = [{'MfgOrderName': 'MO', 'CompletedQty': value, 'ScrapQty': '0', 'OrderQty': '80'}]
    monkeypatch.setattr(p, 'request', AsyncMock(return_value=json.dumps(raw)))
    assert run(p.native_order_quantities('MO'))['complete'] is False


def test_native_completed_quantity_is_not_quality_yield(monkeypatch):
    async def resolve(self, entity, ref, revisioned=True):
        return {'Name': 'MO', 'InstanceID': OID}
    monkeypatch.setattr(p.ReadContext, 'resolve', resolve)
    monkeypatch.setattr(p, 'query_rows', AsyncMock(side_effect=[
        [{'MfgOrderId': OID, 'MfgOrderName': 'MO', 'ProductionCompleteDateGMT': None}],
        [{'Status': 3, 'ProductionComplete': False, 'CurrentContainerQty': 80}], []]))
    monkeypatch.setattr(p, 'request', AsyncMock(return_value=json.dumps([
        {'MfgOrderName': 'MO', 'CompletedQty': '20', 'ScrapQty': '2', 'OrderQty': '80'}])))
    result = json.loads(run(p.get_mfgorder_production_summary('MO')))
    assert result['complete'] is True and result['production_completed_qty'] == 20
    assert result['good_qty'] is None and result['reject_qty'] is None
    assert result['labor_hours'] is None and result['completion_percentage'] is None
    assert result['order_business_closed'] is None
    assert result['production_complete_date_recorded'] is False


def test_history_page_identity_and_scope(monkeypatch):
    monkeypatch.setattr(p, 'container_state', AsyncMock(return_value=({}, CID)))
    monkeypatch.setattr(p, 'query_rows', AsyncMock(return_value=[
        {'ThruputHistoryId': 'h1', 'ContainerId': CID}, {'ThruputHistoryId': 'h2', 'ContainerId': CID}]))
    result = json.loads(run(p.get_container_throughput_history('LOT-1', limit=1)))
    assert result['complete'] is True and result['all_history_returned'] is False
    assert result['page']['has_more'] is True and len(result['rows']) == 1


def test_registry_and_natural_language_routing():
    from tools import get_tool_func, mcp
    from core.tool_catalog import TOOL_POLICIES
    from agent.tool_routing import select_tools
    tools = [{'function': {'name': name}} for name in TOOL_POLICIES]
    names = {t['function']['name'] for t in select_tools(tools, [{'role': 'user', 'content': '请给LOT-1上报加工量3'}])}
    assert {'prepare_container_throughput', 'execute_container_throughput'} <= names
    names = {t['function']['name'] for t in select_tools(tools, [{'role': 'user', 'content': '查看工单报工进度'}])}
    assert 'get_mfgorder_production_summary' in names and 'execute_container_throughput' not in names
    registered = {t.name for t in run(mcp.list_tools())}
    for name in ('prepare_container_throughput', 'execute_container_throughput', 'get_mfgorder_production_summary'):
        assert get_tool_func(name) is not None and name in registered
