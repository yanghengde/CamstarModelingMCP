import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import mcp, get_tool_func, quality_lot_maintenance as u, quality_plans as p, quality_history as h
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION
from agent.safety import MutationCounts, evaluate_mutations, plan_preview


def key(n): return f'{n:016x}'
LOT, C1, C2, PROD, PLAN, TEST, TASK, SPEC, DETAIL, TXN, MAIN, GROUP, HIST = [key(n) for n in range(1, 14)]
def result(coro): return json.loads(asyncio.run(coro))


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'quality.sqlite3')
    lot = dict(MfgLotId=LOT, MfgLotName='L', CDOTypeId='8587', ProductId=PROD, Qty='20', SampleRate='2',
               AllowAdjustToLotSize='false', LotStatus='1', SamplingCompleted='false', SamplingPassed='false',
               SamplingLotResult=None, SamplingPlanId=PLAN, ChangeCount='1')
    member = dict(ContainerId=C1, ContainerName='C1', SamplingLotId=LOT, ProductId=PROD, Qty='10', Qty2=None,
                  Status='1', CurrentHoldCount='0', ParentContainerId=None, ChildCount='0', ChangeCount='1')
    task = dict(LotSampleDataId=TASK, SamplingLotId=LOT, SamplingPlanId=PLAN, SampleTestId=TEST, SamplingPlanDetailId=DETAIL,
                SpecId=SPEC, ResourceId=None, VendorItemId=None, TotalSamplesRequired=4, CompletedSamples=0, Rejects=0,
                AllowedRejects=0, SamplesTested=0, SamplesFailed=0, InspectAllContQty=False, InspectAllQtyOnFailure=False, ChangeCount=1)
    state = dict(lot=lot, members=[member, {**member, 'ContainerId': C2, 'ContainerName': 'C2'}], tasks=[task],
                 active_history=[], history=[], transform=None)
    async def sql(query, maximum):
        if 'FROM MfgLot WHERE' in query: return [copy.deepcopy(state['lot'])] if state['lot'] else []
        if 'FROM Container WHERE SamplingLotId=' in query: return copy.deepcopy(state['members'])
        if 'FROM LotSampleData WHERE SamplingLotId=' in query: return copy.deepcopy(state['tasks'])
        if 'FROM CollectSamplingDataHistory' in query: return copy.deepcopy(state['active_history'])
        if 'FROM UpdateSamplingLotHistory' in query: return copy.deepcopy(state['history'])
        raise AssertionError(query)
    query = AsyncMock(side_effect=sql); monkeypatch.setattr(u.q, 'sql_rows', query)
    async def preview(method, path, **kwargs):
        assert method == 'POST' and path == '/api/UpdateSamplingLot' and kwargs['params']['execute'] == 'false'
        body = kwargs['body']; data = {k[0].upper()+k[1:]: copy.deepcopy(v) for k, v in body.items()}
        data['SamplingLot'] = {'Name': body['samplingLot']['name'], 'InstanceId': body['samplingLot']['instanceId']}
        data.update(Qty=str(body['qty']), SampleRate=str(body['sampleRate']),
                    AllowAdjustToLotSize=str(body['allowAdjustToLotSize']).lower(), CurrentSampleRate=state['lot']['SampleRate'],
                    SamplingPlan={'InstanceId': PLAN}, Containers=[r['ContainerName'] for r in state['members']])
        rows = []
        for row in state['tasks']:
            out = {'InstanceID': row['LotSampleDataId'], 'SampleType': '1', 'ExternalSampleData': []}
            for field, column in [('SampleTest', 'SampleTestId'), ('SamplingPlan', 'SamplingPlanId'),
                                  ('SamplingPlanDetail', 'SamplingPlanDetailId'), ('Spec', 'SpecId'),
                                  ('Resource', 'ResourceId'), ('VendorItem', 'VendorItemId')]:
                out[field] = {'InstanceId': row[column]} if row.get(column) else None
            for field in ('TotalSamplesRequired', 'CompletedSamples', 'Rejects', 'AllowedRejects', 'SamplesTested', 'SamplesFailed',
                          'InspectAllContQty', 'InspectAllQtyOnFailure'):
                out[field] = str(row[field]).lower()
            rows.append(out)
        data['LotSampleData'] = rows
        if state['transform']: state['transform'](data)
        return json.dumps({'data': data})
    echo = AsyncMock(side_effect=preview); monkeypatch.setattr(u, 'request', echo)
    client = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200, text='{"updated":true}')))
    monkeypatch.setattr(p, 'get_client', lambda: client)
    state.update(sql=query, preview=echo, client=client)
    return state


def prepare(**kwargs): return result(u.prepare_sampling_lot_update('L', **kwargs))
def execute(ready): return result(p.execute_quality_transaction(ready['plan_id'], ready['plan_digest']))


def header(body):
    return dict(SummaryId=HIST, SamplingLotId=LOT, Qty=str(body['qty']), SampleRate=str(body['sampleRate']),
                StoredComments=body['comments'], Comments=None, HistoryMainlineId=MAIN, HistoryId=GROUP,
                HeaderHistoryId=GROUP, TxnId=TXN, TxnType='8592', ReversalStatus='1')


def journal():
    with p.db_connect() as db:
        return [db.execute('SELECT * FROM '+table).fetchall() for table in ('plans', 'attempts', 'reviews')]


def test_quantity_update_echo_binds_complete_members_and_current_tasks(env):
    ready = prepare(new_quantity='30')
    assert ready['state'] == 'ready' and ready['static_and_echo_verified']
    body = ready['request_preview']['body']
    assert body['qty'] == 30.0 and body['sampleRate'] == 2 and body['allowAdjustToLotSize'] is False
    assert set(body) == {'samplingLot', 'qty', 'sampleRate', 'allowAdjustToLotSize', 'comments'}
    assert ready['server_context']['affected_container_names'] == ['C1', 'C2']
    assert ready['server_context']['member_quantity_total'] == '20'
    assert not ready['server_context']['projected_task_requirements_verified']
    assert ready['server_context']['current_task_context'][0]['TotalSamplesRequired'] == 4
    assert ready['request_preview']['path'] == '/api/UpdateSamplingLot'
    env['client'].request.assert_not_awaited()
    assert execute(ready)['state'] == 'unknown'
    assert 'error' in execute(ready)
    call = env['client'].request.await_args
    assert call.args[0] == 'POST' and call.kwargs['params'] == {'execute': 'true'}
    assert call.kwargs['follow_redirects'] is False
    env['client'].request.assert_awaited_once()


def test_rate_update_preserves_quantity_and_original_adjust_option(env):
    env['lot']['AllowAdjustToLotSize'] = 'true'
    ready = prepare(new_sample_rate=3)
    body = ready['request_preview']['body']
    assert body['qty'] == 20.0 and body['sampleRate'] == 3 and body['allowAdjustToLotSize'] is True
    assert not ready['server_context']['quantity_changed'] and ready['server_context']['sample_rate_changed']


@pytest.mark.parametrize('kwargs', [{}, {'new_quantity': '20'}, {'new_sample_rate': 2},
    {'new_quantity': '0'}, {'new_quantity': '-1'}, {'new_quantity': '1e13'}, {'new_quantity': 'NaN'},
    {'new_quantity': '0.123456789012345678901'}, {'new_quantity': '19.999'},
    {'new_sample_rate': True}, {'new_sample_rate': '3'}, {'new_sample_rate': 1.5}, {'new_sample_rate': 0}])
def test_invalid_noop_or_below_member_total_requests_refused(env, kwargs):
    assert 'error' in prepare(**kwargs)
    env['preview'].assert_not_awaited(); env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value', [('LotStatus', '2'), ('SamplingCompleted', 'true'), ('SamplingCompleted', ''),
    ('SamplingPassed', 'true'), ('SamplingPassed', None), ('SamplingLotResult', '2'), ('SampleRate', ''),
    ('AllowAdjustToLotSize', None), ('Qty', 'NaN')])
def test_lot_unknown_or_disposed_states_blocked(env, field, value):
    env['lot'][field] = value
    assert 'error' in prepare(new_quantity='30')
    env['preview'].assert_not_awaited()


@pytest.mark.parametrize('field,value', [('CompletedSamples', 1), ('SamplesTested', 1), ('SamplesFailed', 1),
    ('Rejects', 1), ('TotalSamplesRequired', 0), ('InspectAllContQty', True), ('InspectAllQtyOnFailure', None)])
def test_any_started_or_full_inspection_task_is_blocked(env, field, value):
    env['tasks'][0][field] = value
    assert 'error' in prepare(new_quantity='30')
    env['preview'].assert_not_awaited()


@pytest.mark.parametrize('field,value', [('Status', '2'), ('CurrentHoldCount', '1'), ('CurrentHoldCount', None),
    ('ChildCount', '1'), ('ChildCount', None), ('ParentContainerId', C2), ('ProductId', PLAN), ('Qty', '-1')])
def test_member_scope_hold_hierarchy_and_quantity_are_checked(env, field, value):
    env['members'][0][field] = value
    assert 'error' in prepare(new_quantity='30')


def test_active_history_blocks_even_zero_current_counters(env):
    env['active_history'] = [{'CollectSamplingDataHistoryId': HIST}]
    assert 'error' in prepare(new_quantity='30')
    env['preview'].assert_not_awaited()
    query = env['sql'].await_args.args[0]
    assert "s.SamplingLotId='"+LOT in query and 'h.ReversalStatus=1' in query and 'h.TxnId=s.TxnId' in query


def test_no_task_or_over_limit_or_duplicate_member_is_not_a_complete_batch(env):
    original = copy.deepcopy(env['tasks']); env['tasks'] = []
    assert 'error' in prepare(new_quantity='30')
    env['tasks'] = original; env['members'].append(copy.deepcopy(env['members'][0]))
    assert 'error' in prepare(new_quantity='30')
    env['members'] = [dict(env['members'][0], ContainerId=key(100+n), ContainerName=str(n)) for n in range(51)]
    assert 'error' in prepare(new_quantity='1000')


def test_empty_members_are_explicitly_verified_not_automatically_created(env):
    env['members'] = []
    ready = prepare(new_quantity='30')
    assert ready['state'] == 'ready' and ready['server_context']['affected_container_names'] == []
    assert ready['server_context']['membership_change_requested'] is False


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(Qty='31'), lambda d: d.update(SampleRate='3'),
    lambda d: d.update(CurrentSampleRate='1'), lambda d: d.update(AllowAdjustToLotSize='true'),
    lambda d: d['SamplingLot'].update(InstanceId=PLAN), lambda d: d.update(Comments='different'),
    lambda d: d.update(Containers=['C1']), lambda d: d.update(Containers=['C1', 'C1']),
    lambda d: d.update(LotSampleData=[]), lambda d: d['LotSampleData'].append(copy.deepcopy(d['LotSampleData'][0])),
    lambda d: d['LotSampleData'][0].update(SamplesTested='1'),
    lambda d: d['LotSampleData'][0].update(SampleType='3'),
    lambda d: d['LotSampleData'][0].update(ExternalSampleData=[{'InstanceID': HIST}]),
    lambda d: d['LotSampleData'][0].pop('ExternalSampleData'),
    lambda d: d['LotSampleData'][0]['SampleTest'].update(InstanceId=PLAN),
    lambda d: d['LotSampleData'][0].update({'@odata.nextLink': '/next'}),
])
def test_full_echo_and_no_external_sample_or_paging_is_required(env, mutate):
    env['transform'] = mutate
    assert 'error' in prepare(new_quantity='30')
    env['client'].request.assert_not_awaited()


def test_member_change_during_preview_stales_preparation(env):
    env['transform'] = lambda d: env['members'][0].update(ChangeCount='2')
    assert 'error' in prepare(new_quantity='30')


def test_active_history_appearing_during_preview_is_not_missed(env):
    env['transform'] = lambda d: env['active_history'].append({'CollectSamplingDataHistoryId': HIST})
    assert 'error' in prepare(new_quantity='30')
    env['client'].request.assert_not_awaited()


def test_member_quantity_sum_does_not_round_away_small_members(env):
    env['members'][0]['Qty'] = '0.1'; env['members'][1]['Qty'] = '0.2'
    env['members'].append({**env['members'][0], 'ContainerId': key(50), 'ContainerName': 'Tiny', 'Qty': '1e-60'})
    out = prepare(new_quantity='0.3')
    assert 'error' in out
    env['preview'].assert_not_awaited()


def test_omitted_null_reference_is_accepted_only_with_independent_sql_null(env):
    env['transform'] = lambda d: d['LotSampleData'][0].pop('Resource')
    assert prepare(new_quantity='30')['state'] == 'ready'
    del env['tasks'][0]['ResourceId']
    assert 'error' in prepare(new_quantity='30')


@pytest.mark.parametrize('comments', ['\x00', '😀'*91, 'x'*181])
def test_comment_limits_preserve_full_correlation_marker(env, comments):
    assert 'error' in prepare(new_quantity='30', comments=comments)
    env['sql'].assert_not_awaited()


def test_stale_task_before_execution_prevents_write(env):
    ready = prepare(new_quantity='30'); env['tasks'][0]['SamplesTested'] = 1
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


def test_unknown_same_lot_and_member_requests_conflict(env):
    first = prepare(new_quantity='30'); second = prepare(new_sample_rate=3)
    assert execute(first)['state'] == 'unknown'
    assert 'error' in execute(second)
    env['client'].request.assert_awaited_once()
    with p.db_connect() as db:
        snap = json.loads(db.execute('SELECT payload FROM plans WHERE id=?', (first['plan_id'],)).fetchone()[0])
    ncr = {'environment': p.environment(), 'scope_key': 'ncr:other', 'kind': 'ncr', 'body': {'container': {'instanceId': C1}}}
    assert p.conflicts(snap, ncr)
    config = {'environment': p.environment(), 'scope_key': 'model:other', 'kind': 'sampling_reference'}
    assert p.conflicts(snap, config)


@pytest.mark.parametrize('patch', [dict(kind='lot_inspection'), dict(action='scrap'), dict(target_id=C1), dict(service='NCRCreate')])
def test_update_transport_cannot_be_reused_for_other_actions_or_targets(env, patch):
    ready = prepare(new_quantity='30')
    with p.db_connect() as db:
        snap = json.loads(db.execute('SELECT payload FROM plans WHERE id=?', (ready['plan_id'],)).fetchone()[0])
    snap.update(patch)
    with pytest.raises(ValueError): p.transport(snap)
    snap.update(kind='lot_maintenance', action='update', target_id=LOT, service='UpdateSamplingLot')
    snap['body']['containers'] = ['forged']
    with pytest.raises(ValueError): p.transport(snap)


def test_maintenance_history_exact_target_and_pagination(env):
    body = prepare(new_quantity='30')['request_preview']['body']
    env['history'] = [dict(header(body), SummaryId=key(200+n)) for n in range(3)]
    out = result(u.get_sampling_lot_maintenance_history('L', limit=2))
    assert out['has_more'] and out['next_offset'] == 2 and len(out['rows']) == 2
    assert not out['verified_success'] and not out['complete']
    env['history'][0]['SamplingLotId'] = PLAN
    assert 'error' in result(u.get_sampling_lot_maintenance_history('L'))


def test_candidate_marker_uses_stored_summary_comment_and_never_unlocks(env):
    ready = prepare(new_quantity='30', new_sample_rate=3); assert execute(ready)['state'] == 'unknown'
    env['history'] = [header(ready['request_preview']['body'])]
    before = journal()
    found = result(h.find_quality_execution_candidates(ready['plan_id'], ready['plan_digest']))
    assert found['target_linkage_available'] and found['rows'][0]['TxnId'] == TXN
    query = env['sql'].await_args.args[0]
    assert 's.Comments=N\'' in query and "s.SamplingLotId='"+LOT in query
    compared = result(h.reconcile_quality_execution(ready['plan_id'], ready['plan_digest'], TXN))
    assert compared['comparison'] == 'partial_match' and compared['compared_fields_match']
    assert all(compared[k] is False for k in h.FLAGS)
    assert journal() == before


@pytest.mark.parametrize('field,value', [('Qty', '31'), ('SampleRate', '4'), ('ReversalStatus', '2'),
    ('ReversalStatus', '3'), ('TxnType', '8980')])
def test_maintenance_history_mismatch_deleted_or_reversal_is_not_a_match(env, field, value):
    ready = prepare(new_quantity='30'); execute(ready)
    env['history'] = [header(ready['request_preview']['body'])]; env['history'][0][field] = value
    out = result(h.reconcile_quality_execution(ready['plan_id'], ready['plan_digest'], TXN))
    assert out['comparison'] == 'mismatch' and not out['verified_success']


@pytest.mark.parametrize('field,value', [('StoredComments', 'another marker'), ('SamplingLotId', PLAN), ('TxnId', MAIN)])
def test_candidate_scope_and_marker_are_checked_again(env, field, value):
    ready = prepare(new_quantity='30'); execute(ready)
    env['history'] = [header(ready['request_preview']['body'])]; env['history'][0][field] = value
    assert 'error' in result(h.reconcile_quality_execution(ready['plan_id'], ready['plan_digest'], TXN))


def test_readonly_reconciliation_rejects_unattempted_plan(env):
    ready = prepare(new_quantity='30')
    assert 'error' in result(h.find_quality_execution_candidates(ready['plan_id'], ready['plan_digest']))


def test_registry_routing_and_real_mcp_update_inspect_reconcile_lifecycle(env):
    names = {'prepare_sampling_lot_update', 'get_sampling_lot_maintenance_history'}
    assert all(get_tool_func(n) and TOOL_POLICIES[n].effect == 'read' for n in names)
    tools = [{'function': {'name': n}} for n in TOOL_POLICIES]
    picked = {t['function']['name'] for t in select_tools(tools, [{'role': 'user', 'content': '调整抽样批L的数量和采样频率'}])}
    assert names | {'execute_quality_transaction'} <= picked
    assert all(n in ROUTING_INSTRUCTION for n in names)
    async def server(method, url, **kwargs):
        body = kwargs['json']; env['lot'].update(Qty=str(body['qty']), SampleRate=str(body['sampleRate']), ChangeCount='2')
        env['history'] = [header(body)]
        return SimpleNamespace(status_code=200, text='{"updated":true}')
    env['client'].request.side_effect = server
    async def run():
        async with Client(mcp) as client:
            async def call(name, args):
                out = await client.call_tool(name, args); assert not out.is_error
                return json.loads(out.content[0].text)
            schemas = {t.name: t.input_schema for t in await client.list_tools()}
            assert all(schemas[n]['additionalProperties'] is False for n in names)
            ready = await call('prepare_sampling_lot_update', {'lot_name': 'L', 'new_quantity': '30', 'new_sample_rate': 3})
            keys = {k: ready[k] for k in ('plan_id', 'plan_digest')}
            calls = [{'function': {'name': 'execute_quality_transaction', 'arguments': json.dumps(keys)}}]
            assert evaluate_mutations(MutationCounts(), calls).requires_approval
            assert 'UpdateSamplingLot' in plan_preview(calls)
            assert (await call('execute_quality_transaction', keys))['state'] == 'unknown'
            inspected = await call('inspect_quality_transaction', keys)
            assert not inspected['verified_success'] and not inspected['state_changed']
            found = await call('find_quality_execution_candidates', keys)
            compared = await call('reconcile_quality_execution', {**keys, 'candidate_txn_id': found['rows'][0]['TxnId']})
            assert compared['comparison'] == 'partial_match' and not compared['retry_allowed']
            for bad in (True, '3', 1.5):
                out = await client.call_tool('prepare_sampling_lot_update', {'lot_name': 'L', 'new_sample_rate': bad}, raise_on_error=False)
                assert out.is_error
            out = await client.call_tool('prepare_sampling_lot_update', {'lot_name': 'L', 'new_quantity': '40', 'allow_adjust_to_lot_size': True}, raise_on_error=False)
            assert out.is_error
    asyncio.run(run())
    env['client'].request.assert_awaited_once()
