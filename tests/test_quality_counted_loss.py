import asyncio
import json

import pytest
from fastmcp import Client
from tools import mcp, quality_inspection as inspection, quality_inspection_effects as effects
from tools import quality_plans as plans
from tests.test_quality_inspection_effects import env as effects_env, loss_env, result, execute, snap
from tests.test_quality_quantity_loss import REASON


@pytest.fixture
def env(effects_env):
    state = effects_env
    state['definition']['test']['SampleType'] = 1
    state['definition']['points'] = []
    state['models']['SampleDataPoints'] = []
    state['definition']['reject_reasons'] = [dict(name='Reject', instanceId=REASON)]
    return state


def prepare(**overrides):
    arguments = dict(container_name='C', sample_test_name='Visual', sample_test_revision='A',
        samples_collected=3, rejects=0, allow_automatic_quantity_loss=True)
    arguments.update(overrides)
    return result(inspection.prepare_container_inspection(**arguments))


def test_counted_sample_size_loss_uses_explicit_increment_and_one_parent(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    body = ready['request_preview']['body']
    assert body['samplesTested'] == 3
    assert body['serviceDetails'][0]['totalSamplesCollected'] == 3
    context = ready['server_context']['native_effects']
    assert context['mode'] == 'counted_sample_size_loss'
    assert context['automatic_quantity_loss']['quantity'] == '3'
    assert context['automatic_quantity_loss']['projected_remaining_quantity'] == '7'
    assert execute(ready)['state'] == 'unknown'
    env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/CollectSamplingData')
    assert all(call.kwargs['params']['execute'] == 'false' for call in env['children'].await_args_list)
    assert 'error' in execute(ready)
    env['client'].request.assert_awaited_once()


def test_counted_sample_loss_deducts_samples_not_rejects_or_completed_total(env):
    ready = prepare(rejects=1, reject_reason_name='Reject')
    assert ready.get('state') == 'ready', ready
    body = ready['request_preview']['body']
    assert body['samplesTested'] == 3
    assert body['serviceDetails'][0]['rejects'] == 1
    assert body['serviceDetails'][0]['samplingDataPointDetails'][0]['dataValue'] == '1'
    assert env['task']['CompletedSamples'] == 2
    assert ready['server_context']['native_effects']['automatic_quantity_loss']['quantity'] == '3'


@pytest.mark.parametrize('count', [10, 11, 0, -1, True])
def test_invalid_increment_or_zero_remaining_rejected(env, count):
    assert 'error' in prepare(samples_collected=count)
    env['client'].request.assert_not_awaited()


def test_native_echo_must_keep_current_increment(env):
    env['parent_hook'] = lambda data: data.update(SamplesTested='2')
    assert 'error' in prepare()
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('change', ['quantity', 'reason', 'task'])
def test_changed_state_invalidates_prepared_plan(env, change):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    if change == 'quantity':env['row']['Qty'] = '9'
    elif change == 'reason':env['definition']['test']['DecreaseBySampleSizeReason'] = None
    elif change == 'task':env['task']['CompletedSamples'] += 1
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


def test_transport_rejects_root_count_tampering(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    saved = snap(ready)
    saved['body']['samplesTested'] = 2
    with pytest.raises(ValueError):
        effects.validate_transport(saved)


def test_mcp_counted_increment_path(env):
    async def flow():
        async with Client(mcp) as client:
            reply = await client.call_tool('prepare_container_inspection', dict(
                container_name='C', sample_test_name='Visual', sample_test_revision='A',
                samples_collected=3, rejects=0, allow_automatic_quantity_loss=True))
            ready = json.loads(reply.content[0].text)
            assert ready['state'] == 'ready'
            assert ready['request_preview']['body']['samplesTested'] == 3
    asyncio.run(flow())
    env['client'].request.assert_not_awaited()


def test_exact_zero_balance_is_rejected_even_with_remaining_task_samples(env):
    env['row']['Qty'] = '3'
    assert 'error' in prepare()
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('reject_basis', [False, True])
def test_explicit_zero_loss_uses_native_close_and_one_parent(env, reject_basis):
    env['row']['Qty'] = '3'
    if reject_basis:
        env['definition']['test'].update(DecreaseBySampleSizeReason=None, DecreaseByRejectCountReason={'Name':'SCRAP','InstanceId':REASON})
    ready = prepare(samples_collected=3, rejects=3 if reject_basis else 0,
                    reject_reason_name='Reject' if reject_basis else None, allow_zero_quantity_loss=True)
    assert ready.get('state') == 'ready', ready
    projection = ready['server_context']['native_effects']['automatic_quantity_loss']
    assert projection['projected_close'] and projection['allow_zero_quantity_loss']
    assert projection['projected_remaining_quantity'] == '0'
    assert projection['native_child_defaults']['record_all_quantity'] is False
    assert 'allow_zero_quantity_loss' not in ready['request_preview']['body']
    assert execute(ready)['state'] == 'unknown'
    env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/api/CollectSamplingData')
    assert all(call.kwargs['params']['execute'] == 'false' for call in env['children'].await_args_list)


@pytest.mark.parametrize('case', ['no_quantity_permission', 'no_native_close', 'overdraw', 'stale_close', 'tampered'])
def test_zero_loss_requires_explicit_permission_stable_close_and_available_quantity(env, case):
    env['row']['Qty'] = '3'
    args = dict(allow_zero_quantity_loss=True)
    if case == 'no_quantity_permission':
        args['allow_automatic_quantity_loss'] = False
    elif case == 'no_native_close':
        env['child_hook'] = lambda data: data.update(CloseWhenZero='false')
    elif case == 'overdraw':
        args['samples_collected'] = 4
    ready = prepare(**args)
    if case == 'stale_close':
        assert ready['state'] == 'ready'
        env['child_hook'] = lambda data: data.update(CloseWhenZero='false')
        assert 'error' in execute(ready)
    elif case == 'tampered':
        assert ready['state'] == 'ready'
        saved = snap(ready)
        saved['inspection_effects']['context']['automatic_quantity_loss']['projected_close'] = False
        with pytest.raises(ValueError):
            effects.validate_transport(saved)
    else:
        assert 'error' in ready
    env['client'].request.assert_not_awaited()


def test_zero_permission_does_not_project_closure_for_partial_loss(env):
    ready = prepare(allow_zero_quantity_loss=True)
    assert ready['state'] == 'ready'
    assert ready['server_context']['native_effects']['automatic_quantity_loss']['projected_close'] is False
    saved = snap(ready)
    saved['inspection_effects']['context']['automatic_quantity_loss']['projected_close'] = True
    with pytest.raises(ValueError):
        effects.validate_transport(saved)


def test_zero_loss_option_roundtrips_through_mcp_schema(env):
    env['row']['Qty'] = '3'
    async def flow():
        async with Client(mcp) as client:
            response = await client.call_tool('prepare_container_inspection', dict(
                container_name='C', sample_test_name='Visual', sample_test_revision='A',
                samples_collected=3, rejects=0, allow_automatic_quantity_loss=True, allow_zero_quantity_loss=True))
            ready = json.loads(response.content[0].text)
            assert ready['state'] == 'ready', ready
            assert ready['server_context']['native_effects']['automatic_quantity_loss']['projected_close']
    asyncio.run(flow())
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('reject_basis',[False,True])
def test_counted_combination_deducts_from_configured_basis_and_exposes_both_effects(env,reject_basis):
    env['task']['InspectAllQtyOnFailure']=True
    if reject_basis:
        env['definition']['test'].update(DecreaseBySampleSizeReason=None,DecreaseByRejectCountReason={'Name':'SCRAP','InstanceId':REASON})
    ready=prepare(samples_collected=3,rejects=1,reject_reason_name='Reject',allow_full_inspection=True)
    assert ready.get('state')=='ready',ready
    context=ready['server_context']['native_effects']
    expected='9' if reject_basis else '7'
    assert context['automatic_quantity_loss']['projected_remaining_quantity']==expected
    assert context['full_inspection']['projected_quantity_after_loss']==expected
    assert context['full_inspection']['conditional_required_samples_after_failure']==int(expected)
    assert ready['request_preview']['body']['samplesTested']==3
    assert execute(ready)['state']=='unknown'
    env['client'].request.assert_awaited_once()


def test_remaining_task_boundary_and_positive_container_balance(env):
    ready = prepare(samples_collected=8)
    assert ready.get('state') == 'ready', ready
    assert ready['server_context']['native_effects']['automatic_quantity_loss']['projected_remaining_quantity'] == '2'


def test_unknown_counted_attempt_blocks_another_plan(env):
    first = prepare()
    assert first.get('state') == 'ready', first
    execute(first)
    second = prepare()
    assert second.get('state') == 'ready', second
    assert 'error' in execute(second)
    env['client'].request.assert_awaited_once()


def test_inspection_evidence_retains_counted_loss_expectation(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    execute(ready)
    proof = result(plans.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert proof['expected_native_effects']['mode'] == 'counted_sample_size_loss'
    assert not proof['verified_success'] and not proof['native_child_history_verified']


@pytest.fixture
def reject_count_env(env):
    test = env['definition']['test']
    test['DecreaseByRejectCountReason'] = test['DecreaseBySampleSizeReason']
    test['DecreaseBySampleSizeReason'] = None
    env['task']['Rejects'] = 1
    return env


def test_reject_count_loss_deducts_only_current_rejects(reject_count_env):
    env = reject_count_env
    ready = prepare(rejects=2, reject_reason_name='Reject')
    assert ready.get('state') == 'ready', ready
    body = ready['request_preview']['body']
    assert body['samplesTested'] == 3
    assert body['serviceDetails'][0]['samplingDataPointDetails'][0]['dataValue'] == '2'
    assert 'failedSamples' not in body
    context = ready['server_context']['native_effects']
    assert context['mode'] == 'counted_reject_count_loss'
    assert context['automatic_quantity_loss']['quantity'] == '2'
    assert context['automatic_quantity_loss']['projected_remaining_quantity'] == '8'
    assert env['children'].await_args.kwargs['body']['serviceDetails'][0]['qty'] == 2
    assert execute(ready)['state'] == 'unknown'
    env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/CollectSamplingData')
    assert all(call.kwargs['params']['execute'] == 'false' for call in env['children'].await_args_list)


@pytest.mark.parametrize('case', ['zero_rejects', 'missing_consent', 'exceeds_available', 'zero_balance'])
def test_reject_count_loss_rejects_unsupported_inputs(reject_count_env, case):
    env = reject_count_env
    args = dict(rejects=2, reject_reason_name='Reject')
    if case == 'zero_rejects':
        args.update(rejects=0, reject_reason_name=None)
    elif case == 'missing_consent':
        args['allow_automatic_quantity_loss'] = False
    elif case == 'exceeds_available':
        env['row']['Qty'] = '2.5'
    elif case == 'zero_balance':
        env['row']['Qty'] = '2'
        args['samples_collected'] = 2
    assert 'error' in prepare(**args)
    env['children'].assert_not_awaited()
    env['client'].request.assert_not_awaited()


def test_changed_reject_count_reason_invalidates_plan(reject_count_env):
    env = reject_count_env
    ready = prepare(rejects=2, reject_reason_name='Reject')
    assert ready.get('state') == 'ready', ready
    env['definition']['test']['DecreaseByRejectCountReason'] = None
    assert execute(ready)['submitted'] is False
    env['client'].request.assert_not_awaited()


def test_reject_count_loss_transport_rejects_tampered_failure_detail(reject_count_env):
    ready = prepare(rejects=2, reject_reason_name='Reject')
    assert ready.get('state') == 'ready', ready
    saved = snap(ready)
    saved['body']['serviceDetails'][0]['samplingDataPointDetails'][0]['dataValue'] = '1'
    with pytest.raises(ValueError):
        effects.validate_transport(saved)


def grouped_input(env):
    env['definition']['reject_reasons'].append(dict(name='Scratch', instanceId='00000000000007ff'))
    return dict(samples_collected=None, rejects=None, counted_groups=[
        dict(samples_collected=2, rejects=1, reject_reason_name='Reject'),
        dict(samples_collected=3, rejects=2, reject_reason_name='Scratch'),
        dict(samples_collected=1, rejects=0),
    ])


@pytest.mark.parametrize(('fixture','mode','deduction'), [
    ('env', 'counted_sample_size_loss', 6),
    ('reject_count_env', 'counted_reject_count_loss', 3),
])
def test_grouped_loss_preserves_all_details_and_submits_one_parent(request, fixture, mode, deduction):
    env = request.getfixturevalue(fixture)
    arguments = grouped_input(env)
    ready = prepare(**arguments)
    assert ready.get('state') == 'ready', ready
    context = ready['server_context']
    assert context['counted_groups_this_submission'] == 3
    assert context['samples_this_submission'] == 6 and context['rejects_this_submission'] == 3
    assert not context['sample_partition_verified']
    assert context['native_effects']['mode'] == mode
    loss = context['native_effects']['automatic_quantity_loss']
    assert loss['quantity'] == str(deduction)
    assert loss['projected_remaining_quantity'] == str(10-deduction)
    assert env['children'].await_args.kwargs['body']['serviceDetails'][0]['qty'] == deduction

    body = ready['request_preview']['body']
    assert body['samplesTested'] == 6 and 'failedSamples' not in body
    assert [row['totalSamplesCollected'] for row in body['serviceDetails']] == [2, 3, 1]
    assert [row['rejects'] for row in body['serviceDetails']] == [1, 2, 0]
    assert [row['samplingDataPointDetails'][0]['rejectReason']['name'] for row in body['serviceDetails'][:2]] == ['Reject', 'Scratch']
    assert 'samplingDataPointDetails' not in body['serviceDetails'][2]

    assert execute(ready)['state'] == 'unknown'
    assert execute(prepare(**arguments))['submitted'] is False
    env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/CollectSamplingData')
    assert env['client'].request.await_args.kwargs['json'] == body
    assert all(call.kwargs['params']['execute'] == 'false' for call in env['children'].await_args_list)
    proof = result(plans.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert proof['expected_counted_submission'] == {'group_count': 3, 'samples': 6, 'rejects': 3}
    assert not proof['verified_success'] and not proof['native_child_history_verified']


@pytest.mark.parametrize('case', ['duplicate_reason', 'mixed_input', 'exceeds_task', 'exceeds_stock', 'zero_rejects'])
def test_invalid_grouped_loss_never_reaches_child_preview(reject_count_env, case):
    env = reject_count_env
    arguments = grouped_input(env)
    if case == 'duplicate_reason':
        arguments['counted_groups'][1]['reject_reason_name'] = 'Reject'
    elif case == 'mixed_input':
        arguments['samples_collected'] = 6
    elif case == 'exceeds_task':
        env['task']['CompletedSamples'] = 5
    elif case == 'exceeds_stock':
        env['row']['Qty'] = '5'
    else:
        arguments['counted_groups'] = [dict(samples_collected=6, rejects=0)]
    assert 'error' in prepare(**arguments)
    env['children'].assert_not_awaited()
    env['client'].request.assert_not_awaited()


def test_grouped_loss_rejects_missing_echo_detail(reject_count_env):
    env = reject_count_env
    arguments = grouped_input(env)
    env['parent_hook'] = lambda data: data['ServiceDetails'].pop()
    assert 'error' in prepare(**arguments)
    env['client'].request.assert_not_awaited()


def test_grouped_loss_rejects_changed_reason_group_and_tampered_counts(reject_count_env):
    env = reject_count_env
    ready = prepare(**grouped_input(env))
    assert ready.get('state') == 'ready', ready
    saved = snap(ready)
    saved['body']['serviceDetails'][1]['samplingDataPointDetails'][0]['dataValue'] = '1'
    with pytest.raises(ValueError):
        effects.validate_transport(saved)
    env['definition']['reject_reasons'].pop()
    assert execute(ready)['submitted'] is False
    env['client'].request.assert_not_awaited()
