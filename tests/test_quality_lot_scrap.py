import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import mcp, quality_lot_scrap as scrap, quality_quantity_loss as loss, quality_plans as plans
from tests.test_quality_lot_rework import env as rework_env, lot_env, key, SPEC, OP, SRC, LOT, CID, CID2
from tests.test_quality_lot_rework import result, execute, snapshot, reference
from tests.test_quality_lot_execution import echo

REASON, GROUP, UOM = [key(n) for n in (960, 961, 962)]


@pytest.fixture
def env(rework_env, monkeypatch):
    state = rework_env
    state['models']['LossReasons'] = [dict(Name='SCRAP', InstanceID=REASON)]
    state['models']['LossReasonGroups'] = [dict(Name='Losses', InstanceID=GROUP,
        ResolvedEntries=[reference('SCRAP', REASON)])]
    state['models']['UOMs'] = [dict(Name='EA', InstanceID=UOM)]
    state['operation']['LossReasons'] = reference('Losses', GROUP)
    state['tokens'].update({REASON: 1, GROUP: 1, UOM: 1})
    for number, row in enumerate(state['members']):
        row.update(CDOTypeId=1040, UOMId=UOM, UOM2Id=None, InRework=False,
            CurrentStatusId=key(970 + number), StatusRecordId=key(970 + number),
            CarrierId=None, SpecId=SPEC, WorkflowStepId=SRC)
    state.update(reason_type='LossReason', child_defaults_hook=None, scrap_batch_hook=None)
    original_sql = scrap.q.sql_rows

    async def sql(query, maximum):
        if 'FROM Container c INNER JOIN CurrentStatus' in query:
            return [copy.deepcopy(row) for row in state['originals'] if "'" + row['ContainerId'] + "'" in query]
        for entity in ('LossReasons', 'LossReasonGroups', 'UOMs'):
            _, column, _ = loss.ASSETS[entity]
            table = loss.ASSETS[entity][0]
            if ' FROM ' + table + ' WHERE ' in query:
                selected = query.split("='", 1)[1].split("'", 1)[0]
                return [{column: selected, 'ChangeCount': state['tokens'][selected]}]
        return await original_sql(query, maximum)

    monkeypatch.setattr(scrap.q, 'sql_rows', AsyncMock(side_effect=sql))
    async def request(method, path, **kwargs):
        assert method == 'POST'
        body = kwargs['body']
        if path.endswith('/PerformEvent'):
            assert body['event'] == {'eventName': 'Load'}
            ref = body['inputData']['scrapReason']
            return json.dumps({'value': [dict(ScrapReason=dict(Name=ref['name'], InstanceId=ref['instanceId'],
                CDOTypeName=state['reason_type']))]})
        assert kwargs['params']['execute'] == 'false'
        data = echo(body)
        if path == '/api/ChangeQty':
            data.update({field: 'true' for field in scrap.DEFAULTS})
            data['ServiceDetails'][0].update(CDOTypeId='4030', QtyMultiplier='-1', CountsAgainstProduction='true',
                Container=copy.deepcopy(data['Container']), AdjustThruput='true', Qty='0', Qty2=None,
                ChargeToStep=reference('Inspect', SRC))
            if state['child_defaults_hook']:state['child_defaults_hook'](data)
        else:
            assert path == '/api/ExecuteSamplingLot'
            data['Containers'] = [row['ContainerName'] for row in state['members']]
            if state['scrap_batch_hook']:state['scrap_batch_hook'](data)
        return json.dumps({'data': data})
    monkeypatch.setattr(scrap, 'request', AsyncMock(side_effect=request))
    return state


def prepare(**kwargs):
    return result(scrap.prepare_sampling_lot_scrap('L', 'SCRAP', **kwargs))


def test_whole_lot_loss_reason_id_and_one_native_submission(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    body = ready['request_preview']['body']
    assert body['samplingLotResult'] == 3
    assert body['scrapReason'] == dict(name='SCRAP', instanceId=REASON)
    assert set(body) == {'samplingLot', 'samplingLotResult', 'scrapReason', 'comments'}
    saved = snapshot(ready)
    assert len(saved['preview']['member_dispositions']) == 2
    assert all(member['expected_remaining_quantity'] == '0' for member in saved['preview']['member_dispositions'])
    assert execute(ready)['state'] == 'unknown'
    env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/ExecuteSamplingLot')
    assert 'error' in execute(ready)
    env['client'].request.assert_awaited_once()
    assert all(call.kwargs['params'].get('execute') != 'true' for call in scrap.request.await_args_list)


@pytest.mark.parametrize('kind', ['ScrapReason', 'UserCode', None])
def test_reason_concrete_type_must_be_loss_reason(env, kind):
    env['reason_type'] = kind
    assert 'error' in prepare()
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode', ['skipped', 'duplicate', 'result', 'reason', 'signature'])
def test_root_preview_must_match_complete_batch(env, mode):
    def hook(data):
        if mode == 'skipped':data['Containers'].pop()
        elif mode == 'duplicate':data['Containers'].append(data['Containers'][0])
        elif mode == 'result':data['SamplingLotResult'] = '2'
        elif mode == 'reason':data['ScrapReason']['InstanceId'] = key(999)
        elif mode == 'signature':data['ESigRequirement'] = {'InstanceId': key(999)}
    env['scrap_batch_hook'] = hook
    assert 'error' in prepare()
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value', [('CDOTypeId', '4010'), ('QtyMultiplier', '1'), ('RecordAllQty', 'false'),
    ('CountsAgainstProduction', 'false'), ('Qty2', '1')])
def test_child_must_be_typed_full_loss(env, field, value):
    env['child_defaults_hook'] = lambda data: data['ServiceDetails'][0].update({field: value})
    assert 'error' in prepare()


@pytest.mark.parametrize('field,value', [('CurrentHoldCount', 1), ('ChildCount', 1), ('Qty', '0'), ('Qty2', '1'), ('ProductId', key(999))])
def test_no_member_can_be_skipped_or_implicitly_supported(env, field, value):
    env['members'][1][field] = value
    assert 'error' in prepare()
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('change', ['members', 'quantity', 'unit_version', 'reason_group'])
def test_fresh_state_required_for_execute(env, change):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    if change == 'members':env['members'].pop()
    elif change == 'quantity':env['members'][0]['Qty'] = '9'
    elif change == 'unit_version':env['tokens'][UOM] += 1
    elif change == 'reason_group':env['models']['LossReasonGroups'][0]['ResolvedEntries'] = []
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


def test_unknown_blocks_other_scrap_and_rework_plans(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    other = prepare()
    execute(ready)
    assert 'error' in execute(other)
    assert plans.conflicts(snapshot(ready), dict(kind='quantity_loss', scope_key='other', environment=plans.environment(),
        target_id=CID, body={}, resource_writes=['runtime:container:id:' + CID]))
    env['client'].request.assert_awaited_once()


def test_scrap_result_is_not_accepted_as_ok_or_rework(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    saved = snapshot(ready)
    saved['body']['samplingLotResult'] = 1
    with pytest.raises(ValueError):scrap.validate_transport(saved)


def test_current_zero_closed_records_do_not_auto_unlock(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    execute(ready)
    for row in env['members']:row.update(Qty='0', Status=2, ChangeCount=2)
    env['lot'].update(SamplingCompleted='true', SamplingLotResult='3')
    proof = result(plans.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert all(member['zero_quantity_currently_observed'] for member in proof['original_members'])
    assert not proof['verified_success'] and not proof['history_correlation_verified']


def test_mcp_scrap_prepare(env):
    async def flow():
        async with Client(mcp) as client:
            reply = await client.call_tool('prepare_sampling_lot_scrap', dict(lot_name='L', loss_reason_name='SCRAP'))
            assert json.loads(reply.content[0].text)['state'] == 'ready'
    asyncio.run(flow())
    env['client'].request.assert_not_awaited()


def test_readiness_with_reason_uses_same_preflight_without_creating_plan(env):
    ready = result(loss.get_sampling_lot_scrap_readiness('L', loss_reason_name='SCRAP'))
    assert ready['complete'] and ready['preflight_passed']
    assert not ready['plan_created'] and not ready['ready_for_execution']
    assert not plans.PLAN_DB.exists()
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode', ['timeout', 'http400'])
def test_uncertain_parent_attempt_keeps_unknown_without_retry(env, mode):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    if mode == 'timeout':env['client'].request.side_effect = TimeoutError('isolated test timeout')
    else:env['client'].request.return_value.status_code = 400
    assert execute(ready)['state'] == 'unknown'
    assert 'error' in execute(ready)
    env['client'].request.assert_awaited_once()


def test_partial_observed_effects_remain_unknown_and_visible(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    execute(ready)
    env['members'][0].update(Qty='0', Status=2, ChangeCount=2)
    proof = result(plans.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert proof['original_members'][0]['zero_quantity_currently_observed']
    assert not proof['original_members'][1]['zero_quantity_currently_observed']
    assert not proof['verified_success'] and not proof['retry_allowed']


@pytest.mark.parametrize('payload', [dict(value=[]), dict(value=[{}, {}]), dict(value=[{}], **{'@odata.nextLink': '/more'}),
    dict(error='failed', data={}), dict(data={'ErrorCode': '400'})])
def test_incomplete_or_error_envelopes_are_rejected(payload):
    with pytest.raises(ValueError):scrap.reply(json.dumps(payload))


def test_unsafe_service_result_cannot_be_reused_as_another_plan_kind(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    saved = snapshot(ready)
    saved['kind'] = 'lot_completion'
    with pytest.raises(ValueError):plans.transport(saved)


def test_scrap_plan_confirmation_is_required(env):
    from agent.safety import evaluate_mutations, MutationCounts
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    calls = [{'function': {'name': 'execute_quality_transaction', 'arguments': json.dumps({
        'plan_id': ready['plan_id'], 'plan_digest': ready['plan_digest']})}}]
    assert evaluate_mutations(MutationCounts(), calls).requires_approval
