import asyncio
import copy
import json

import pytest
from fastmcp import Client

from tools import mcp, quality_current_sampling as u, quality_plans as p
from tests.test_quality_configuration import env as config_env, ref, key, PLAN, TEST, AQL, LEVEL
from tests.test_quality_current_sampling import env, prepare, execute, snapshot, STATUS, STATUS2
from agent.safety import evaluate_mutations, MutationCounts, plan_preview
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION
from core.tool_catalog import TOOL_POLICIES


def test_reset_only_uses_native_flag_and_keeps_counter_and_level_omitted(env):
    ready = prepare(reset_inspection_level=True)
    assert ready['state'] == 'ready' and ready['static_and_echo_verified']
    body = ready['request_preview']['body']
    assert set(body) == {'product', 'samplingCurrentStatus', 'resetInspectionLevel', 'comments'}
    assert body['resetInspectionLevel'] is True
    context = ready['server_context']
    assert context['reset_inspection_level_requested'] and context['effective_default_level'] is None
    assert not context['resolved_default_level_verified'] and not context['switching_rule_effects_verified']
    assert not context['reset_necessity_verified'] and not context['future_container_effects_verified']
    ids = {state['InstanceID'] for state in snapshot(ready)['dependencies']}
    assert {PLAN, TEST, AQL, LEVEL} <= ids
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('manual', [None, LEVEL])
def test_reset_is_explicit_native_command_not_a_guessed_noop(env, manual):
    env['statuses'][0]['SamplePlanInspectionLevelId'] = manual
    ready = prepare(reset_inspection_level=True)
    assert ready['state'] == 'ready' and not ready['server_context']['reset_necessity_verified']


def test_counter_and_reset_are_one_confirmed_submission(env):
    ready = prepare(new_sample_rate_counter=0, reset_inspection_level=True)
    body = ready['request_preview']['body']
    assert body['resetInspectionLevel'] is True and body['newSampleRateCounterValue'] == 0
    assert 'newInspectionLevel' not in body
    assert execute(ready)['state'] == 'unknown'
    assert 'error' in execute(ready)
    env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.kwargs['params'] == {'execute': 'true'}


@pytest.mark.parametrize('level', ['Level', ''])
def test_named_level_and_reset_are_mutually_exclusive(env, level):
    assert 'error' in prepare(new_inspection_level_name=level, reset_inspection_level=True)
    env['sql'].assert_not_awaited(); env['preview'].assert_not_awaited()


@pytest.mark.parametrize('reset', ['true', 'false', 0, 1, None])
def test_reset_flag_cannot_be_coerced_from_strings_or_numbers(env, reset):
    assert 'error' in prepare(reset_inspection_level=reset)
    env['sql'].assert_not_awaited()


@pytest.mark.parametrize('mode', ['missing_flag', 'false_flag', 'default_counter', 'default_level', 'extra_selected', 'holding_extra'])
def test_preview_cannot_change_reset_intent_or_inject_other_changes(env, mode):
    def transform(data):
        if mode == 'missing_flag': del data['ResetInspectionLevel']
        if mode == 'false_flag': data['ResetInspectionLevel'] = 'false'
        if mode == 'default_counter': data['NewSampleRateCounterValue'] = '0'
        if mode == 'default_level': data['NewInspectionLevel'] = {'InstanceId': LEVEL}
        if mode == 'extra_selected': data['SamplingCurrentStatus'].append({'InstanceId': STATUS2})
        if mode == 'holding_extra': data['ServiceDetails'] = [{'InstanceId': STATUS2}]
    env['preview_transform'] = transform
    assert 'error' in prepare(reset_inspection_level=True)
    env['client'].request.assert_not_awaited()


def test_snapshot_includes_all_plan_and_matching_detail_table_candidates(env):
    other = copy.deepcopy(env['level']); other.update(InstanceID=key(800), Name='OtherLevel')
    other['LotSizeDetails'][0].update(InstanceID=key(801), Parent={'InstanceId': key(800)})
    env['rows']['InspectionLevels'].append(other); env['counters'][key(800)] = 1
    env['plan']['SamplingPlanDetails'].append({**env['detail'], 'InstanceID': key(802), 'InspectionLevel': ref(other)})
    ready = prepare(reset_inspection_level=True)
    assert ready['state'] == 'ready'
    ids = {state['InstanceID'] for state in snapshot(ready)['dependencies']}
    assert {AQL, LEVEL, key(800)} <= ids
    assert ready['server_context']['effective_default_level'] is None
    assert 'newInspectionLevel' not in ready['request_preview']['body']


def test_does_not_resolve_formulas_or_claim_lookup_as_effective_default(env):
    env['aql']['SampleSizeDetails'][0]['SampleSize'] = 'sqrt(n)'
    ready = prepare(reset_inspection_level=True)
    assert ready['state'] == 'ready'
    assert any(r.get('SampleSizeDetails', [{}])[0].get('SampleSize') == 'sqrt(n)' for r in snapshot(ready)['dependencies'])
    assert ready['server_context']['effective_default_level'] is None
    assert not ready['server_context']['dynamic_validation_verified']


def test_absent_configured_table_references_are_not_replaced_with_invented_default(env):
    env['plan'].update(AQLLevel=None, InspectionLevel=None)
    env['detail'].update(AQLLevel=None, InspectionLevel=None)
    ready = prepare(reset_inspection_level=True)
    assert ready['state'] == 'ready' and ready['server_context']['effective_default_level'] is None
    assert 'newInspectionLevel' not in ready['request_preview']['body']


@pytest.mark.parametrize('mode', ['missing', 'foreign_parent', 'oversized', 'unknown_ref'])
def test_incomplete_configured_table_candidates_block_reset(env, mode):
    if mode == 'missing': env['rows']['InspectionLevels'] = []
    if mode == 'foreign_parent': env['level']['LotSizeDetails'][0]['Parent'] = {'InstanceId': AQL}
    if mode == 'oversized': env['level']['LotSizeDetails'] *= 51
    if mode == 'unknown_ref': env['plan']['InspectionLevel'] = {'unknown': 'data'}
    assert 'error' in prepare(reset_inspection_level=True)
    env['preview'].assert_not_awaited()


@pytest.mark.parametrize('target', ['level_counter', 'level_row', 'aql', 'plan_detail', 'status'])
def test_change_during_reset_preparation_is_rejected(env, target):
    def transform(data):
        if target == 'level_counter': env['counters'][LEVEL] += 1
        if target == 'level_row': env['level']['LotSizeDetails'][0]['MaxLotQty'] = 200
        if target == 'aql': env['aql']['SampleSizeDetails'][0]['SampleSize'] = '10'
        if target == 'plan_detail': env['detail']['InspectionLevel'] = {'Name': 'Missing'}
        if target == 'status': env['statuses'][0]['SampleRateCounter'] = '4'
    env['preview_transform'] = transform
    assert 'error' in prepare(reset_inspection_level=True)


@pytest.mark.parametrize('target', ['level', 'aql'])
def test_changed_default_table_after_prepare_prevents_http_execution(env, target):
    ready = prepare(reset_inspection_level=True)
    if target == 'level': env['level']['Description'] = 'changed'
    else: env['counters'][AQL] += 1
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


def test_exact_multiple_selection_is_not_replaced_with_product_wide_all_mode(env):
    env['statuses'].append({**env['statuses'][0], 'SamplingCurrentStatusId': STATUS2})
    ready = prepare([STATUS2, STATUS], reset_inspection_level=True)
    assert ready['server_context']['selected_status_ids'] == sorted([STATUS, STATUS2])
    assert ready['request_preview']['body']['samplingCurrentStatus'] == [{'instanceId': v} for v in sorted([STATUS, STATUS2])]


def test_batch_sampling_stays_unsupported_even_for_reset(env):
    env['statuses'][0]['SamplingLotId'] = key(999)
    assert 'error' in prepare(reset_inspection_level=True)


def test_transport_accepts_native_reset_only_but_refuses_named_override(env):
    snap = snapshot(prepare(reset_inspection_level=True))
    assert p.transport(snap)['path'] == '/api/CurrentSamplingStatusUpdate'
    snap['body']['newInspectionLevel'] = {'name': 'Level', 'instanceId': LEVEL}
    with pytest.raises(ValueError): p.transport(snap)


def test_default_false_mode_keeps_legacy_request_and_omitted_argument_semantics(env):
    ready = prepare(new_sample_rate_counter=0)
    snap = snapshot(ready); legacy = dict(snap['arguments']); legacy.pop('reset_inspection_level')
    rebuilt = asyncio.run(u.build_update(legacy, snap['marker']))
    assert rebuilt['body'] == snap['body'] and rebuilt['preview'] == snap['preview']
    assert 'reset_inspection_level_requested' not in ready['server_context']
    assert ready['request_preview']['body']['resetInspectionLevel'] is False


def test_unknown_reset_inspection_preserves_unknown_and_blocks_another_update(env):
    first = prepare(reset_inspection_level=True); second = prepare(new_sample_rate_counter=0)
    assert execute(first)['state'] == 'unknown' and 'error' in execute(second)
    inspected = json.loads(asyncio.run(p.inspect_quality_transaction(first['plan_id'], first['plan_digest'])))
    assert not inspected['verified_success'] and not inspected['state_changed'] and not inspected['retry_allowed']


def test_reset_intent_routing_and_real_mcp_prepare_confirm_execute_inspect(env):
    tools = [{'function': {'name': name}} for name in TOOL_POLICIES]
    for question in ('恢复抽样计划默认检验水平', '重置采样状态的检验水平', '撤销手动检验水平设置'):
        chosen = {t['function']['name'] for t in select_tools(tools, [{'role': 'user', 'content': question}])}
        assert {'prepare_current_sampling_status_update', 'execute_quality_transaction'} <= chosen
    assert 'reset_inspection_level' in ROUTING_INSTRUCTION
    async def run():
        async with Client(mcp) as client:
            schema = next(t.input_schema for t in await client.list_tools() if t.name == 'prepare_current_sampling_status_update')
            assert schema['properties']['reset_inspection_level']['type'] == 'boolean'
            assert schema['properties']['reset_inspection_level']['default'] is False
            args = {'product_name': 'Product', 'product_revision': 'A', 'status_instance_ids': [STATUS], 'reset_inspection_level': True}
            raw = await client.call_tool('prepare_current_sampling_status_update', args)
            ready = json.loads(raw.content[0].text); assert ready['state'] == 'ready'
            keys = {k: ready[k] for k in ('plan_id', 'plan_digest')}
            calls = [{'function': {'name': 'execute_quality_transaction', 'arguments': json.dumps(keys)}}]
            assert evaluate_mutations(MutationCounts(), calls).requires_approval
            assert 'resetInspectionLevel' in plan_preview(calls)
            submitted = await client.call_tool('execute_quality_transaction', keys)
            assert json.loads(submitted.content[0].text)['state'] == 'unknown'
            checked = await client.call_tool('inspect_quality_transaction', keys)
            assert not json.loads(checked.content[0].text)['verified_success']
            for bad in ('true', 1, None):
                failed = await client.call_tool('prepare_current_sampling_status_update', {**args, 'reset_inspection_level': bad}, raise_on_error=False)
                assert failed.is_error
    asyncio.run(run())
    env['client'].request.assert_awaited_once()
