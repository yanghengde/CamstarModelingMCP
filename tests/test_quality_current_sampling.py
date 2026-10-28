import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from tools import mcp, get_tool_func, quality_current_sampling as u, quality_plans as p
from tests.test_quality_configuration import env as config_env, root, ref, PLAN, TEST, AQL, LEVEL, key
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION
from agent.safety import evaluate_mutations, MutationCounts, plan_preview

PROD, STATUS, STATUS2 = key(100), key(175), key(176)
def result(coro): return json.loads(asyncio.run(coro))


@pytest.fixture
def env(config_env, monkeypatch):
    state = config_env
    product = root('Product', PROD)
    state['rows']['Products'] = [product]; state['counters'][PROD] = 1
    row = dict.fromkeys(u.COLUMNS.split(','))
    row.update(SamplingCurrentStatusId=STATUS, CDOTypeId='8426', ProductId=PROD, SamplingPlanId=PLAN,
               SampleTestId=TEST, AQLLevelId=AQL, CurrentInspectionLevelId=LEVEL,
               CurrentSampleRate='2', SampleRateCounter='3', ChangeCount='1', LastUpdateDate='2026-10-27T00:00:00')
    state.update(product=product, statuses=[row], preview_transform=None)
    original_sql = u.q.sql_rows
    async def sql(query, maximum):
        if ' FROM Product WHERE ' in query:
            return [{'ProductId': PROD, 'ChangeCount': str(state['counters'][PROD])}]
        if ' FROM SamplingCurrentStatus ' in query:
            rows = state['statuses']
            if ' IN (' in query:
                selected = query.split(' IN (', 1)[1].split(')', 1)[0]
                rows = [r for r in rows if "'"+r['SamplingCurrentStatusId']+"'" in selected]
            elif ' OFFSET ' in query:
                offset = int(query.split(' OFFSET ')[1].split()[0])
                rows = rows[offset:offset+maximum]
            return copy.deepcopy(rows)
        return await original_sql(query, maximum)
    sql_mock = AsyncMock(side_effect=sql); monkeypatch.setattr(u.q, 'sql_rows', sql_mock)
    def echo(value):
        if isinstance(value, dict): return {k[0].upper()+k[1:]: echo(v) for k, v in value.items()}
        if isinstance(value, list): return [echo(v) for v in value]
        return str(value).lower() if type(value) in (bool, int) else value
    async def preview(method, path, **kw):
        assert method == 'POST' and path == '/api/CurrentSamplingStatusUpdate' and kw['params']['execute'] == 'false'
        data = echo(copy.deepcopy(kw['body']))
        if state['preview_transform']: state['preview_transform'](data)
        return json.dumps({'data': data})
    preview_mock = AsyncMock(side_effect=preview); monkeypatch.setattr(u, 'request', preview_mock)
    client = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200, text='{"updated":true}')))
    monkeypatch.setattr(p, 'get_client', lambda: client)
    state.update(sql=sql_mock, preview=preview_mock, client=client)
    return state


def prepare(ids=None, **kwargs):
    return result(u.prepare_current_sampling_status_update('Product', 'A', ids if ids is not None else [STATUS], **kwargs))
def execute(ready): return result(p.execute_quality_transaction(ready['plan_id'], ready['plan_digest']))
def snapshot(ready):
    with p.db_connect() as db:
        return json.loads(db.execute('SELECT payload FROM plans WHERE id=?', (ready['plan_id'],)).fetchone()[0])


def test_read_returns_real_ids_counter_rate_and_explicit_page_semantics(env):
    out = result(u.get_current_sampling_status('Product', 'A'))
    assert out['page_complete'] and not out['complete'] and not out['has_more']
    row = out['rows'][0]
    assert row['SampleRateCounter'] == 3 and row['CurrentSampleRate'] == 2
    assert row['SamplingCurrentStatusId'] == STATUS and row['container_based_candidate']
    assert not p.PLAN_DB.exists()
    env['preview'].assert_not_awaited(); env['client'].request.assert_not_awaited()


def test_read_page_and_batch_record_are_not_execution_permission(env):
    env['statuses'].append({**env['statuses'][0], 'SamplingCurrentStatusId': STATUS2, 'SamplingLotId': key(200)})
    page = result(u.get_current_sampling_status('Product', 'A', limit=1))
    assert page['has_more'] and page['next_offset'] == 1
    second = result(u.get_current_sampling_status('Product', 'A', offset=1, limit=1))
    assert not second['rows'][0]['container_based_candidate']
    env['statuses'] = []
    empty = result(u.get_current_sampling_status('Product', 'A'))
    assert empty['rows'] == [] and not empty['complete']


@pytest.mark.parametrize('offset,limit', [(-1, 20), (True, 20), (0, 0), (0, 51), (0, '20')])
def test_read_bounds_checked_before_queries(env, offset, limit):
    assert 'error' in result(u.get_current_sampling_status('Product', 'A', offset, limit))
    env['sql'].assert_not_awaited()


def test_counter_zero_is_explicit_and_does_not_change_frequency_or_level(env):
    ready = prepare(new_sample_rate_counter=0)
    assert ready['state'] == 'ready' and ready['static_and_echo_verified']
    body = ready['request_preview']['body']
    assert set(body) == {'product', 'samplingCurrentStatus', 'resetInspectionLevel', 'comments', 'newSampleRateCounterValue'}
    assert body['newSampleRateCounterValue'] == 0 and body['resetInspectionLevel'] is False
    assert body['samplingCurrentStatus'] == [{'instanceId': STATUS}]
    assert ready['before']['statuses'][0]['CurrentSampleRate'] == 2
    env['client'].request.assert_not_awaited()
    assert execute(ready)['state'] == 'unknown'
    assert 'error' in execute(ready)
    env['client'].request.assert_awaited_once()
    call = env['client'].request.await_args
    assert call.kwargs['params'] == {'execute': 'true'} and call.kwargs['follow_redirects'] is False


def test_level_only_omits_counter_instead_of_resetting_it(env):
    ready = prepare(new_inspection_level_name='Level')
    assert ready['state'] == 'ready'
    body = ready['request_preview']['body']
    assert 'newSampleRateCounterValue' not in body and body['newInspectionLevel']['instanceId'] == LEVEL
    assert body['resetInspectionLevel'] is False
    # Current level matches, but an explicit manual override is not yet stored.
    assert ready['before']['statuses'][0]['SamplePlanInspectionLevelId'] is None


def test_both_changes_are_one_request_and_comments_use_shared_marker(env):
    ready = prepare(new_sample_rate_counter=1, new_inspection_level_name='Level', comments='明确调整')
    assert ready['state'] == 'ready'
    body = ready['request_preview']['body']
    assert body['comments'].startswith('MCP-QUAL:') and body['comments'].endswith(' 明确调整')
    assert execute(ready)['state'] == 'unknown'
    env['client'].request.assert_awaited_once()


@pytest.mark.parametrize('ids', [[], [STATUS, STATUS], [STATUS, STATUS.upper()], ['bad'], [key(n) for n in range(21)], [True]])
def test_selection_requires_bounded_unique_explicit_ids(env, ids):
    assert 'error' in prepare(ids, new_sample_rate_counter=0)
    env['sql'].assert_not_awaited(); env['preview'].assert_not_awaited()


@pytest.mark.parametrize('counter', [-1, 2147483648, True, '0', 1.5])
def test_counter_is_nonnegative_strict_int32(env, counter):
    assert 'error' in prepare(new_sample_rate_counter=counter)
    env['preview'].assert_not_awaited()


@pytest.mark.parametrize('kwargs', [{}, {'new_sample_rate_counter': 3}, {'new_inspection_level_name': ''},
    {'new_inspection_level_name': 'Missing'}, {'new_sample_rate_counter': 0, 'comments': '\0'},
    {'new_sample_rate_counter': 0, 'comments': '😀'*91}])
def test_missing_noop_or_invalid_inputs_refused(env, kwargs):
    assert 'error' in prepare(**kwargs)
    env['client'].request.assert_not_awaited()


def test_level_noop_compares_manual_override_not_only_current_level(env):
    env['statuses'][0]['SamplePlanInspectionLevelId'] = LEVEL
    assert 'error' in prepare(new_inspection_level_name='Level')


@pytest.mark.parametrize('field,value', [('ProductId', key(999)), ('CDOTypeId', '8587'),
    ('SamplingPlanId', None), ('SampleTestId', 'bad'), ('SampleRateCounter', ''), ('CurrentSampleRate', 0),
    ('ChangeCount', None), ('SamplingLotId', key(200)), ('ResourceId', 'bad')])
def test_unknown_wrong_owned_or_batch_states_cannot_be_updated(env, field, value):
    env['statuses'][0][field] = value
    assert 'error' in prepare(new_sample_rate_counter=0)
    env['preview'].assert_not_awaited()


def test_missing_state_field_and_missing_selected_record_are_not_defaults(env):
    del env['statuses'][0]['VendorItemId']
    assert 'error' in prepare(new_sample_rate_counter=0)
    env['statuses'] = []
    assert 'error' in prepare(new_sample_rate_counter=0)


def test_plan_no_longer_containing_test_is_blocked(env):
    env['plan']['SamplingPlanDetails'] = []
    assert 'error' in prepare(new_sample_rate_counter=0)


@pytest.mark.parametrize('target', ['product', 'plan', 'test'])
def test_inactive_product_plan_or_test_is_blocked(env, target):
    env[target]['Status'] = 2
    assert 'error' in prepare(new_sample_rate_counter=0)


def test_new_level_aql_mapping_is_exact_and_complete(env):
    env['level']['LotSizeDetails'][0]['SampleSizeCode'] = 'a'
    assert 'error' in prepare(new_inspection_level_name='Level')
    env['preview'].assert_not_awaited()
    # Counter-only adjustment does not need to interpret an unrelated table.
    assert prepare(new_sample_rate_counter=0)['state'] == 'ready'


@pytest.mark.parametrize('mode', ['missing', 'extra', 'duplicate', 'wrong_product', 'counter_default',
    'level_default', 'reset', 'comments', 'holding_extra', 'next_link'])
def test_nonexecuting_echo_cannot_expand_selection_or_fill_unrequested_fields(env, mode):
    def mutate(data):
        if mode == 'missing': data.pop('SamplingCurrentStatus')
        if mode == 'extra': data['SamplingCurrentStatus'].append({'InstanceId': STATUS2})
        if mode == 'duplicate': data['SamplingCurrentStatus'] *= 2
        if mode == 'wrong_product': data['Product']['InstanceId'] = key(999)
        if mode == 'counter_default': data['NewSampleRateCounterValue'] = '0'
        if mode == 'level_default': data['NewInspectionLevel'] = {'Name': 'Other', 'InstanceId': key(999)}
        if mode == 'reset': data['ResetInspectionLevel'] = 'true'
        if mode == 'comments': data['Comments'] = 'other marker'
        if mode == 'holding_extra': data['ServiceDetails'] = [{'InstanceId': STATUS2}]
        if mode == 'next_link': data['SamplingCurrentStatus@odata.nextLink'] = 'more'
    env['preview_transform'] = mutate
    kwargs = {'new_inspection_level_name': 'Level'} if mode == 'counter_default' else {'new_sample_rate_counter': 0}
    assert 'error' in prepare(**kwargs)
    env['client'].request.assert_not_awaited()


def test_reordered_selection_and_exact_holding_ids_are_allowed(env):
    env['statuses'].append({**env['statuses'][0], 'SamplingCurrentStatusId': STATUS2})
    def mutate(data):
        data['SamplingCurrentStatus'].reverse()
        data['ServiceDetails'] = copy.deepcopy(data['SamplingCurrentStatus'])
    env['preview_transform'] = mutate
    ready = prepare([STATUS2, STATUS], new_sample_rate_counter=0)
    assert ready['state'] == 'ready'
    assert ready['server_context']['selected_status_ids'] == sorted([STATUS, STATUS2])


@pytest.mark.parametrize('target', ['status', 'product', 'definition'])
def test_change_during_preview_prevents_ready_plan(env, target):
    def mutate(data):
        if target == 'status': env['statuses'][0]['ChangeCount'] = '2'
        if target == 'product': env['counters'][PROD] += 1
        if target == 'definition': env['test']['Instructions'] = 'changed'
    env['preview_transform'] = mutate
    assert 'error' in prepare(new_sample_rate_counter=0)


def test_changed_counter_after_preparation_prevents_http_write(env):
    ready = prepare(new_sample_rate_counter=0)
    env['statuses'][0]['SampleRateCounter'] = '4'
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('kind,service', [('ncr', 'CurrentSamplingStatusUpdate'), ('current_sampling', 'NCRUpdate')])
def test_transport_binds_service_to_plan_kind(env, kind, service):
    ready = prepare(new_sample_rate_counter=0); snap = snapshot(ready)
    snap.update(kind=kind, service=service)
    with pytest.raises(ValueError): p.transport(snap)


@pytest.mark.parametrize('mode', ['reset', 'extra', 'selection', 'counter_bool', 'level_null', 'product', 'missing_change'])
def test_transport_rejects_overrides_and_mismatched_targets(env, mode):
    snap = snapshot(prepare(new_sample_rate_counter=0)); body = snap['body']
    if mode == 'reset': body['resetInspectionLevel'] = 'true'
    if mode == 'extra': body['currentSampleRate'] = 99
    if mode == 'selection': body['samplingCurrentStatus'] = [{'instanceId': STATUS2}]
    if mode == 'counter_bool': body['newSampleRateCounterValue'] = False
    if mode == 'level_null': body['newInspectionLevel'] = None
    if mode == 'product': body['product']['instanceId'] = key(999)
    if mode == 'missing_change': del body['newSampleRateCounterValue']
    with pytest.raises(ValueError): p.transport(snap)


def test_unknown_plan_blocks_same_product_and_related_configuration(env):
    first = prepare(new_sample_rate_counter=0); second = prepare(new_sample_rate_counter=1)
    assert execute(first)['state'] == 'unknown' and 'error' in execute(second)
    env['client'].request.assert_awaited_once()
    old = snapshot(first)
    reference = {'kind': 'sampling_reference', 'environment': old['environment'], 'scope_key': 'other',
                 'target_id': key(999), 'resource_reads': [], 'resource_writes': []}
    assert p.conflicts(old, reference)  # shared sampling-reference configuration gate
    binding = {**reference, 'kind': 'sampling_binding', 'resource_writes': ['modeling:Products:id:'+PROD]}
    assert p.conflicts(old, binding)


def test_inspect_only_reads_original_selection_and_never_unlocks(env):
    ready = prepare(new_sample_rate_counter=0); execute(ready)
    env['statuses'][0]['SampleRateCounter'] = '0'
    out = result(p.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert out['current_statuses'][0]['SampleRateCounter'] == 0
    assert not out['verified_success'] and not out['state_changed'] and not out['retry_allowed']
    with p.db_connect() as db:
        assert db.execute('SELECT state FROM plans WHERE id=?', (ready['plan_id'],)).fetchone()[0] == 'unknown'


def test_registry_routing_and_actual_mcp_prepare_execute_inspect_multi_turn(env):
    names = {'get_current_sampling_status', 'prepare_current_sampling_status_update'}
    assert all(get_tool_func(n) and TOOL_POLICIES[n].effect == 'read' for n in names)
    tools = [{'function': {'name': n}} for n in TOOL_POLICIES]
    for question in ('查询产品采样状态和计数器', '调整采样状态的检验水平', '修改当前采样计数器为0'):
        chosen = {t['function']['name'] for t in select_tools(tools, [{'role': 'user', 'content': question}])}
        assert names <= chosen
    assert all(n in ROUTING_INSTRUCTION for n in names)
    async def run():
        async with Client(mcp) as client:
            async def call(name, args):
                out = await client.call_tool(name, args); assert not out.is_error
                return json.loads(out.content[0].text)
            schemas = {t.name: t.input_schema for t in await client.list_tools()}
            assert all(schemas[n]['additionalProperties'] is False for n in names)
            read = await call('get_current_sampling_status', {'product_name': 'Product', 'product_revision': 'A'})
            ready = await call('prepare_current_sampling_status_update', {'product_name': 'Product', 'product_revision': 'A',
                'status_instance_ids': [read['rows'][0]['SamplingCurrentStatusId']], 'new_sample_rate_counter': 0})
            keys = {k: ready[k] for k in ('plan_id', 'plan_digest')}
            calls = [{'function': {'name': 'execute_quality_transaction', 'arguments': json.dumps(keys)}}]
            assert evaluate_mutations(MutationCounts(), calls).requires_approval
            assert 'CurrentSamplingStatusUpdate' in plan_preview(calls)
            assert (await call('execute_quality_transaction', keys))['state'] == 'unknown'
            assert not (await call('inspect_quality_transaction', keys))['verified_success']
            for bad in (True, '0', 0.5):
                failed = await client.call_tool('prepare_current_sampling_status_update', {'product_name': 'Product', 'product_revision': 'A',
                    'status_instance_ids': [STATUS], 'new_sample_rate_counter': bad}, raise_on_error=False)
                assert failed.is_error
            failed = await client.call_tool('prepare_current_sampling_status_update', {'product_name': 'Product', 'product_revision': 'A',
                'status_instance_ids': [STATUS], 'new_sample_rate_counter': 0, 'reset_inspection_level': 'true'}, raise_on_error=False)
            assert failed.is_error
    asyncio.run(run())
    env['client'].request.assert_awaited_once()
