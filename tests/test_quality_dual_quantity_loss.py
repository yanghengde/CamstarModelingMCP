import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import mcp, quality_quantity_loss as loss, quality_plans as plans
from tests.test_quality_quantity_loss import env as loss_env, prepare, execute, snapshot, result, key, UOM

UOM2 = key(3099)


@pytest.fixture
def env(loss_env, monkeypatch):
    state = loss_env
    state['row'].update(Qty2='5', UOM2Id=UOM2)
    state['models']['UOMs'].append(dict(Name='KG', InstanceID=UOM2))
    state['counters'][UOM2] = 1
    state['secondary_echo_hook'] = None
    original = loss.request

    async def preview(*args, **kwargs):
        reply = json.loads(await original(*args, **kwargs))
        if 'qty2' in kwargs['body']['serviceDetails'][0]:
            reply['data']['ServiceDetails'][0]['Qty2'] = str(kwargs['body']['serviceDetails'][0]['qty2'])
        if state['secondary_echo_hook']:
            state['secondary_echo_hook'](reply['data'])
        return json.dumps(reply)

    monkeypatch.setattr(loss, 'request', AsyncMock(side_effect=preview))
    return state


def dual(**overrides):
    return prepare(**(dict(secondary_loss_quantity='1.25') | overrides))


def test_dual_partial_loss_binds_both_units_and_one_submission(env):
    ready = dual()
    assert ready.get('state') == 'ready', ready
    detail = ready['request_preview']['body']['serviceDetails'][0]
    assert detail['qty'] == 1.5 and detail['qty2'] == 1.25
    context = ready['server_context']
    assert context['projected_remaining_quantity'] == '8.5'
    assert context['projected_remaining_secondary_quantity'] == '3.75'
    assert context['secondary_unit'] == dict(name='KG', instanceId=UOM2)
    assert not context['unit_conversion_performed']
    assert len(snapshot(ready)['dependencies']) == 6
    assert execute(ready)['state'] == 'unknown'
    env['client'].request.assert_awaited_once()
    assert 'error' in execute(ready)
    env['client'].request.assert_awaited_once()


def test_second_quantity_remains_restricted_without_explicit_increment(env):
    assert 'error' in prepare()
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('value', ['0', '-1', '5', '6', '', 'NaN', '1/2'])
def test_invalid_secondary_or_full_depletion_is_rejected(env, value):
    assert 'error' in dual(secondary_loss_quantity=value)
    env['client'].request.assert_not_awaited()


def test_record_all_cannot_mix_explicit_secondary_increment(env):
    assert 'error' in dual(loss_quantity=None, record_all_quantity=True)
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value', [('UOM2Id', None), ('Qty2', None), ('Qty2', '0')])
def test_missing_secondary_basis_is_rejected(env, field, value):
    env['row'][field] = value
    assert 'error' in dual()


@pytest.mark.parametrize('value', [None, '0', '2'])
def test_native_secondary_echo_must_match(env, value):
    env['secondary_echo_hook'] = lambda data: data['ServiceDetails'][0].update(Qty2=value)
    assert 'error' in dual()
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('change', ['quantity', 'unit_version'])
def test_secondary_changes_invalidate_plan(env, change):
    ready = dual()
    assert ready.get('state') == 'ready', ready
    if change == 'quantity':env['row']['Qty2'] = '4'
    else:env['counters'][UOM2] += 1
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


def test_transport_rejects_secondary_payload_tampering(env):
    ready = dual()
    assert ready.get('state') == 'ready', ready
    saved = snapshot(ready)
    saved['body']['serviceDetails'][0]['qty2'] = 2
    with pytest.raises(ValueError):
        loss.validate_transport(saved)


def test_mcp_dual_quantity_entry(env):
    async def flow():
        async with Client(mcp) as client:
            reply = await client.call_tool('prepare_container_quantity_loss', dict(
                container_name='C', loss_reason_name='SCRAP', close_when_zero=False,
                adjust_throughput=False, loss_quantity='1.5', secondary_loss_quantity='1.25'))
            assert json.loads(reply.content[0].text)['state'] == 'ready'
    asyncio.run(flow())
    env['client'].request.assert_not_awaited()


def test_dual_balance_readback_remains_evidence_only(env):
    ready = dual()
    assert ready.get('state') == 'ready', ready
    execute(ready)
    env['row'].update(Qty='8.5', Qty2='3.75', ChangeCount='2')
    proof = result(plans.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert proof['dual_quantity_evidence']['current_balances_match_projection']
    assert not proof['verified_success']
    assert not proof['dual_quantity_evidence']['causal_chain_verified']


def test_unit_replacement_does_not_match_balance_projection(env):
    ready = dual()
    assert ready.get('state') == 'ready', ready
    execute(ready)
    env['row'].update(Qty='8.5', Qty2='3.75', UOM2Id=key(3199), ChangeCount='2')
    proof = result(plans.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert not proof['dual_quantity_evidence']['original_units_currently_match']
    assert not proof['dual_quantity_evidence']['current_balances_match_projection']


def test_sampling_lot_member_is_still_rejected(env):
    env['row']['SamplingLotId'] = key(3198)
    assert 'error' in dual()
    env['client'].request.assert_not_awaited()


def test_bool_is_not_a_valid_secondary_quantity_transport(env):
    ready = dual()
    assert ready.get('state') == 'ready', ready
    saved = snapshot(ready)
    saved['body']['serviceDetails'][0]['qty2'] = True
    with pytest.raises(ValueError):
        loss.validate_transport(saved)
