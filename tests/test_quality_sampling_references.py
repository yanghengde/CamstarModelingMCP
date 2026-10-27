import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import mcp, get_tool_func, quality_sampling_references as s, quality_plans as p
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION
from agent.safety import plan_preview, evaluate_mutations, MutationCounts


def key(n): return f'{n:016x}'
AQL, LEVEL, A, B, R1, R2 = [key(n) for n in range(1, 7)]
def result(coro): return json.loads(asyncio.run(coro))


def row(iid, parent, **values):
    return dict(InstanceID=iid, Parent={'InstanceId': parent}, IsFrozen=False, **values)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'quality.sqlite3')
    state = {'rows': {
        'AQLLevels': [{'InstanceID': AQL, 'Name': 'AQL', 'Description': 'explicit', 'IsFrozen': False,
            'SampleSizeDetails': [row(A, AQL, SampleSizeCode='A', SampleSize='5', Accepts=0, Rejects=1),
                                  row(B, AQL, SampleSizeCode='B', SampleSize='10', Accepts=1, Rejects=2)]}],
        'InspectionLevels': [{'InstanceID': LEVEL, 'Name': 'Level', 'Description': '', 'IsFrozen': False,
            'LotSizeDetails': [row(R1, LEVEL, SampleSizeCode='A', MinLotQty=1, MaxLotQty=10),
                               row(R2, LEVEL, SampleSizeCode='B', MinLotQty=11, MaxLotQty=100)]}]},
        'counters': {AQL: 1, LEVEL: 1}, 'get_count': 0}
    class Context:
        def __init__(self, *a, **kw): pass
        async def get(self, entity, key=None, params=None):
            state['get_count'] += 1
            records = state['rows'][entity]
            if key:
                return copy.deepcopy(next(r for r in records if r['InstanceID'] == key))
            name = params['$filter'].split("Name eq '", 1)[1][:-1].replace("''", "'")
            return {'value': copy.deepcopy([r for r in records if r['Name'] == name])}
    monkeypatch.setattr(s, 'ReadContext', Context)
    async def sql(query, maximum):
        for kind in s.CONFIG:
            _, table, column, _, _ = s.CONFIG[kind]
            if 'FROM '+table+' WHERE' in query:
                iid = query.split("='", 1)[1].split("'", 1)[0]
                return [{column: iid, 'ChangeCount': str(state['counters'][iid])}]
        raise AssertionError(query)
    call = AsyncMock(side_effect=sql); monkeypatch.setattr(s.q, 'sql_rows', call)
    client = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200, text='{"saved":true}')))
    monkeypatch.setattr(p, 'get_client', lambda: client)
    state.update(client=client, sql=call)
    return state


def aql_change(**values):
    return dict(op='add', sample_size_code='C', sample_size=20, accepts=1, rejects=2, **values)


def create(**kwargs):
    changes = kwargs.pop('changes', {'aql_rows': [aql_change()]})
    return result(s.prepare_sampling_reference_definition('aql_level', 'create', kwargs.pop('name', 'New'), changes, **kwargs))


def update(changes, **kwargs):
    return result(s.prepare_sampling_reference_definition('aql_level', 'update', 'AQL', changes, **kwargs))


def execute(ready):
    return result(p.execute_quality_transaction(ready['plan_id'], ready['plan_digest']))


def test_create_static_named_plan_and_single_confirmed_attempt(env):
    ready = create()
    assert ready['state'] == 'ready' and not ready['submitted'] and not ready['static_and_echo_verified']
    request = ready['request_preview']
    assert request['api'] == 'modeling' and request['method'] == 'POST' and request['path'] == '/api/AQLLevels'
    assert request['params'] == {} and 'revision' not in request['body'] and 'status' not in request['body']
    assert request['body']['sampleSizeDetails'][0] == dict(sampleSizeCode='C', sampleSize='20', accepts=1, rejects=2, isFrozen=False)
    assert ready['server_context']['effective_rows'][0]['SampleSize'] == '20'
    env['client'].request.assert_not_awaited()
    preview = plan_preview([{'function': {'name': 'execute_quality_transaction', 'arguments': json.dumps({'plan_id': ready['plan_id'], 'plan_digest': ready['plan_digest']})}}])
    assert '/api/AQLLevels' in preview and 'sampleSize' in preview
    assert execute(ready)['state'] == 'unknown'
    call = env['client'].request.await_args
    assert call.args[0] == 'POST' and call.kwargs['params'] == {} and call.kwargs['follow_redirects'] is False
    assert 'error' in execute(ready)
    env['client'].request.assert_awaited_once()


def test_update_uses_id_and_no_revision_parameter(env):
    ready = update({'aql_rows': [{'op': 'update', 'detail_instance_id': A, 'sample_size': 6}]}, instance_id=AQL)
    request = ready['request_preview']
    assert request['path'] == '/api/AQLLevels/'+AQL and request['params'] == {}
    assert request['body']['sampleSizeDetails-Expanded'] == [{'index': 0, 'listItemAction': 'change', 'value': {'sampleSize': '6'}}]
    assert ready['server_context']['effective_rows'][1]['SampleSize'] == '10'
    assert execute(ready)['state'] == 'unknown'
    assert env['client'].request.await_args.args[0] == 'PUT'


def test_row_actions_preserve_original_indices_and_apply_deletes_descending(env):
    root = env['rows']['AQLLevels'][0]
    root['SampleSizeDetails'] += [row(key(30), AQL, SampleSizeCode='D', SampleSize='20', Accepts=1, Rejects=2)]
    ready = update({'aql_rows': [{'op': 'remove', 'detail_instance_id': A},
                               {'op': 'update', 'detail_instance_id': B, 'accepts': 2, 'rejects': 3},
                               {'op': 'remove', 'detail_instance_id': key(30)}, aql_change()]})
    wrappers = ready['request_preview']['body']['sampleSizeDetails-Expanded']
    assert [r['listItemAction'] for r in wrappers] == ['change', 'delete', 'delete', 'add']
    assert [r['index'] for r in wrappers[:3]] == [1, 2, 0]
    assert [r['SampleSizeCode'] for r in ready['server_context']['effective_rows']] == ['B', 'C']


@pytest.mark.parametrize('field,value', [('sample_size', True), ('sample_size', '20'), ('sample_size', 2.5),
                                       ('sample_size', 0), ('sample_size', 2147483648), ('accepts', -1),
                                       ('accepts', True), ('rejects', 0), ('rejects', '2'),
                                       ('sample_size', None), ('sample_size_code', ''), ('sample_size_code', ' A ')])
def test_invalid_aql_values_do_not_prepare(env, field, value):
    change = aql_change(); change[field] = value
    assert 'error' in create(changes={'aql_rows': [change]})
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('accepts,rejects', [(5, 5), (21, 22), (0, 22)])
def test_invalid_accept_reject_relationships_rejected(env, accepts, rejects):
    change = aql_change(); change.update(accepts=accepts, rejects=rejects)
    assert 'error' in create(changes={'aql_rows': [change]})


def test_explicit_threshold_gap_preserved_without_quality_verdict(env):
    change = aql_change(); change.update(accepts=0, rejects=3)
    ready = create(changes={'aql_rows': [change]})
    assert ready['request_preview']['body']['sampleSizeDetails'][0]['rejects'] == 3


@pytest.mark.parametrize('changes', [
    {'aql_rows': [dict(op='add', sample_size_code='A', sample_size=5, accepts=0, rejects=1),
                  dict(op='add', sample_size_code='a', sample_size=10, accepts=1, rejects=2)]},
    {'aql_rows': [dict(op='add', sample_size_code='A', sample_size=5, rejects=1)]},
    {'aql_rows': []}, {'aql_rows': None}, {'lot_ranges': [dict(op='add', sample_size_code='A', min_lot_qty=1, max_lot_qty=5)]},
    {'description': None}, {'status': 1}, {'revision': 'A'}, {'description': 'x\x00'},
    {'aql_rows': [dict(op='add', sample_size_code='A', sample_size=5, accepts=0, rejects=1, detail_instance_id=A)]},
])
def test_closed_fields_duplicates_nulls_and_missing_create_fields_rejected(env, changes):
    assert 'error' in create(changes=changes)


def test_frozen_root_or_frozen_touched_row_is_blocked(env):
    env['rows']['AQLLevels'][0]['IsFrozen'] = True
    assert 'error' in update({'description': 'new'})
    env['rows']['AQLLevels'][0]['IsFrozen'] = False
    env['rows']['AQLLevels'][0]['SampleSizeDetails'][0]['IsFrozen'] = True
    assert 'error' in update({'aql_rows': [{'op': 'remove', 'detail_instance_id': A}]})


@pytest.mark.parametrize('rows', [
    [{'op': 'update', 'sample_size': 6}],
    [{'op': 'update', 'detail_instance_id': key(999), 'sample_size': 6}],
    [{'op': 'update', 'detail_instance_id': A}],
    [{'op': 'update', 'detail_instance_id': A, 'sample_size': 6}, {'op': 'remove', 'detail_instance_id': A}],
    [{'op': 'remove', 'detail_instance_id': A, 'sample_size': 6}],
    [{'op': 'remove', 'detail_instance_id': A}, {'op': 'remove', 'detail_instance_id': B}],
])
def test_ambiguous_repeated_empty_or_destructive_row_changes_rejected(env, rows):
    assert 'error' in update({'aql_rows': rows})


def test_existing_name_wrong_identity_and_absent_target_rejected(env):
    assert 'error' in create(name='AQL')
    assert 'error' in update({'description': 'new'}, instance_id=LEVEL)
    assert 'error' in result(s.prepare_sampling_reference_definition('aql_level', 'update', 'Missing', {'description': 'new'}))


@pytest.mark.parametrize('name', ['x'*31, '😀'*16])
def test_name_storage_capacity_checked_before_read(env, name):
    assert 'error' in create(name=name)
    assert env['get_count'] == 0


def test_description_and_code_storage_capacities_follow_live_columns(env):
    assert 'error' in create(changes={'aql_rows': [aql_change()], 'description': '😀'*128})
    change = aql_change(); change['sample_size_code'] = '😀'*16
    assert 'error' in create(changes={'aql_rows': [change]})


def test_removed_codes_are_shown_in_confirmation_preview(env):
    ready = update({'aql_rows': [{'op': 'remove', 'detail_instance_id': A}]})
    assert ready['server_context']['codes_removed_from_table'] == ['A']
    assert ready['server_context']['cross_table_references_verified'] is False


@pytest.mark.parametrize('mutate', ['wrong_parent', 'duplicate_id', 'missing_value', 'unknown_frozen', 'too_many'])
def test_read_rejects_incomplete_or_invalid_owned_table(env, mutate):
    root = env['rows']['AQLLevels'][0]; rows = root['SampleSizeDetails']
    if mutate == 'wrong_parent': rows[0]['Parent'] = {'InstanceId': LEVEL}
    if mutate == 'duplicate_id': rows.append(copy.deepcopy(rows[0]))
    if mutate == 'missing_value': del rows[0]['SampleSize']
    if mutate == 'unknown_frozen': rows[0]['IsFrozen'] = None
    if mutate == 'too_many': root['SampleSizeDetails'] = [row(key(100+n), AQL, SampleSizeCode=str(n), SampleSize='5', Accepts=0, Rejects=1) for n in range(51)]
    assert not result(s.get_sampling_reference_definition('aql_level', 'AQL'))['complete']


def test_read_preserves_formula_but_maintenance_and_lookup_refuse_to_evaluate(env):
    env['rows']['AQLLevels'][0]['SampleSizeDetails'][0]['SampleSize'] = 'Qty * 0.1'
    assert result(s.get_sampling_reference_definition('aql_level', 'AQL'))['data']['SampleSizeDetails'][0]['SampleSize'] == 'Qty * 0.1'
    assert 'error' in update({'description': 'new'})
    assert not result(s.preview_sampling_table_lookup('Level', 'AQL', 10))['complete']


def test_table_lookup_inclusive_boundaries_and_literal_thresholds(env):
    for qty, expected in [(1, 5), (10, 5), (11, 10), (100, 10)]:
        out = result(s.preview_sampling_table_lookup('Level', 'AQL', qty))
        assert out['complete'] and out['configured_sample_size'] == expected
        assert out['quality_verdict'] is None and not out['mes_task_requirements_verified'] and not out['standard_compliance_verified']
    assert result(s.preview_sampling_table_lookup('Level', 'AQL', 1))['sample_size_exceeds_lot']
    assert not result(s.preview_sampling_table_lookup('Level', 'AQL', 101))['complete']


def test_range_creation_does_not_send_version_or_status(env):
    ready = result(s.prepare_sampling_reference_definition('inspection_level', 'create', 'NewLevel',
        {'lot_ranges': [{'op': 'add', 'sample_size_code': 'A', 'min_lot_qty': 1, 'max_lot_qty': 5}]}))
    assert ready['state'] == 'ready' and ready['request_preview']['body']['lotSizeDetails'][0]['maxLotQty'] == 5
    assert set(ready['request_preview']['body']) == {'name', 'lotSizeDetails'}


@pytest.mark.parametrize('ranges', [
    [(1, 10), (10, 20)], [(1, 10), (2, 3)], [(10, 1)], [(-1, 10)], [(0, 0)],
])
def test_invalid_or_overlapping_closed_ranges_rejected(env, ranges):
    ready = result(s.prepare_sampling_reference_definition('inspection_level', 'create', 'NewLevel',
        {'lot_ranges': [{'op': 'add', 'sample_size_code': 'A', 'min_lot_qty': lo, 'max_lot_qty': hi} for lo, hi in ranges]}))
    assert 'error' in ready


def test_gap_allowed_but_not_filled_and_code_match_is_exact(env):
    env['rows']['InspectionLevels'][0]['LotSizeDetails'][1]['MinLotQty'] = 20
    assert not result(s.preview_sampling_table_lookup('Level', 'AQL', 15))['complete']
    env['rows']['InspectionLevels'][0]['LotSizeDetails'][0]['SampleSizeCode'] = 'a'
    assert not result(s.preview_sampling_table_lookup('Level', 'AQL', 5))['complete']


def test_lookup_exposes_unmapped_codes_without_guessing_a_replacement(env):
    env['rows']['InspectionLevels'][0]['LotSizeDetails'][1]['SampleSizeCode'] = 'Missing'
    out = result(s.preview_sampling_table_lookup('Level', 'AQL', 5))
    assert out['complete'] and out['unmapped_inspection_codes'] == ['Missing']
    assert not result(s.preview_sampling_table_lookup('Level', 'AQL', 20))['complete']


def test_paginated_definition_never_prepares_subset(env, monkeypatch):
    class Context:
        def __init__(self, *a): pass
        async def get(self, *args, **kwargs):
            return {'value': env['rows']['AQLLevels'], '@odata.nextLink': '/next'}
    monkeypatch.setattr(s, 'ReadContext', Context)
    assert 'error' in update({'description': 'new'})


def test_definition_changes_during_read_are_rejected(env, monkeypatch):
    original = s.ReadContext
    class Context(original):
        async def get(self, entity, key=None, params=None):
            data = await super().get(entity, key, params)
            if key:
                env['rows'][entity][0]['Description'] += 'modified'
            return data
    monkeypatch.setattr(s, 'ReadContext', Context)
    assert not result(s.get_sampling_reference_definition('aql_level', 'AQL'))['complete']


def test_inspection_range_update_preserves_other_ranges(env):
    ready = result(s.prepare_sampling_reference_definition('inspection_level', 'update', 'Level',
        {'lot_ranges': [{'op': 'update', 'detail_instance_id': R2, 'max_lot_qty': 200}]}))
    assert ready['request_preview']['body']['lotSizeDetails-Expanded'] == [{'index': 1, 'listItemAction': 'change', 'value': {'maxLotQty': 200}}]
    assert ready['server_context']['effective_rows'][0]['MaxLotQty'] == 10


def test_change_count_stale_recheck_prevents_write(env):
    ready = update({'description': 'new'})
    env['counters'][AQL] += 1
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


def test_child_mutation_without_root_counter_change_stales_plan(env):
    ready = update({'description': 'new'})
    env['rows']['AQLLevels'][0]['SampleSizeDetails'][0]['SampleSize'] = '6'
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


def test_unknown_reference_update_blocks_other_sampling_configuration_until_review(env):
    first = update({'description': 'new'})
    assert execute(first)['state'] == 'unknown'
    second = result(s.prepare_sampling_reference_definition('inspection_level', 'update', 'Level', {'description': 'new'}))
    assert 'error' in execute(second)
    env['client'].request.assert_awaited_once()
    out = result(p.inspect_quality_transaction(first['plan_id'], first['plan_digest']))
    assert not out['verified_success'] and not out['state_changed'] and not out['retry_allowed']


def test_reference_config_conflicts_with_unresolved_sampling_but_not_ncr(env):
    root = {'environment': p.environment(), 'scope_key': 'one', 'kind': 'sampling_reference', 'resource_reads': [], 'resource_writes': []}
    for kind in ('lot_inspection', 'lot_completion', 'sampling_lot', 'inspection_definition', 'sampling_binding'):
        other = {'environment': p.environment(), 'scope_key': 'two', 'kind': kind, 'name': 'L'}
        assert p.conflicts(root, other)
    ncr = {'environment': p.environment(), 'scope_key': 'two', 'kind': 'ncr'}
    assert not p.conflicts(root, ncr)


@pytest.mark.parametrize('patch', [dict(entity='Products'), dict(action='revise', method='PATCH'),
                                  dict(path='/api/InspectionLevels/'+AQL), dict(reference_kind='inspection_level')])
def test_transport_whitelist_binds_named_kind_entity_method_and_path(env, patch):
    ready = update({'description': 'new'})
    with p.db_connect() as db:
        payload = json.loads(db.execute('SELECT payload FROM plans WHERE id=?', (ready['plan_id'],)).fetchone()[0])
    payload.update(patch)
    with pytest.raises(ValueError): p.transport(payload)


def test_mcp_two_table_lifecycle_requires_review_and_resolves_only_configuration(env):
    async def apply(method, url, **kwargs):
        entity = url.rsplit('/', 1)[1]; body = kwargs['json']
        assert method == 'POST' and entity in ('AQLLevels', 'InspectionLevels')
        root_id = key((1000 if entity == 'AQLLevels' else 3000)+len(env['rows'][entity]))
        field = 'SampleSizeDetails' if entity == 'AQLLevels' else 'LotSizeDetails'
        details = []
        for index, value in enumerate(body[field[0].lower()+field[1:]]):
            detail = {k[0].upper()+k[1:]: v for k, v in value.items()}
            details.append({**detail, 'InstanceID': key(int(root_id, 16)*10+index), 'Parent': {'InstanceId': root_id}})
        env['rows'][entity].append({'InstanceID': root_id, 'Name': body['name'], 'Description': body.get('description', ''),
                                   'IsFrozen': False, field: details})
        env['counters'][root_id] = 1
        return SimpleNamespace(status_code=200, text='{"saved":true}')
    env['client'].request.side_effect = apply
    async def run():
        async with Client(mcp) as client:
            async def call(tool, args):
                out = await client.call_tool(tool, args)
                assert not out.is_error
                return json.loads(out.content[0].text)
            first = await call('prepare_sampling_reference_definition', {'reference_kind': 'aql_level', 'action': 'create',
                'name': 'DemoAQL', 'changes': {'aql_rows': [aql_change()]}})
            first_keys = {k: first[k] for k in ('plan_id', 'plan_digest')}
            assert (await call('execute_quality_transaction', first_keys))['state'] == 'unknown'
            inspected = await call('inspect_quality_transaction', first_keys)
            assert inspected['current_configuration']['found'] and not inspected['verified_success']
            second = await call('prepare_sampling_reference_definition', {'reference_kind': 'inspection_level', 'action': 'create',
                'name': 'DemoLevel', 'changes': {'lot_ranges': [dict(op='add', sample_size_code='C', min_lot_qty=100, max_lot_qty=500)]}})
            second_keys = {k: second[k] for k in ('plan_id', 'plan_digest')}
            assert 'error' in await call('execute_quality_transaction', second_keys)
            reviewed = await call('review_quality_transaction', {**first_keys, 'action': 'resolve', 'conclusion': 'committed',
                'evidence_reference': 'offline-fixture:aql-final-audit', 'review_note': '模拟最终审计已核实指定AQL配置提交'})
            assert not reviewed['verified_success']
            assert (await call('execute_quality_transaction', second_keys))['state'] == 'unknown'
            lookup = await call('preview_sampling_table_lookup', {'inspection_level_name': 'DemoLevel', 'aql_level_name': 'DemoAQL', 'lot_quantity': 120})
            assert lookup['configured_sample_size'] == 20 and lookup['quality_verdict'] is None
            assert not lookup['mes_task_requirements_verified']
            with p.db_connect() as db:
                assert db.execute('SELECT state FROM plans WHERE id=?', (second['plan_id'],)).fetchone()[0] == 'unknown'
    asyncio.run(run())
    assert env['client'].request.await_count == 2


def test_registry_routing_confirmations_and_real_mcp_closed_schemas(env):
    names = {'get_sampling_reference_definition', 'prepare_sampling_reference_definition', 'preview_sampling_table_lookup'}
    assert all(get_tool_func(n) and TOOL_POLICIES[n].effect == 'read' for n in names)
    tools = [{'function': {'name': n}} for n in TOOL_POLICIES]
    for question in ('查看AQL样本量代码和拒收数', '检验水平的批量区间怎么配', '查看质量抽样配置查表预览', '按批量看样本量'):
        chosen = {t['function']['name'] for t in select_tools(tools, [{'role': 'user', 'content': question}])}
        assert names <= chosen
    assert all(n in ROUTING_INSTRUCTION for n in names)
    counts = MutationCounts()
    decision = evaluate_mutations(counts, [{'function': {'name': 'execute_quality_transaction', 'arguments': '{}'}}])
    assert decision.requires_approval
    async def run():
        async with Client(mcp) as client:
            schemas = {t.name: t.input_schema for t in await client.list_tools()}
            assert all(schemas[n]['additionalProperties'] is False for n in names)
            ok = await client.call_tool('preview_sampling_table_lookup', {'inspection_level_name': 'Level', 'aql_level_name': 'AQL', 'lot_quantity': 10})
            assert json.loads(ok.content[0].text)['configured_sample_size'] == 5
            for bad in (True, '10', 1.5):
                failed = await client.call_tool('preview_sampling_table_lookup',
                    {'inspection_level_name': 'Level', 'aql_level_name': 'AQL', 'lot_quantity': bad}, raise_on_error=False)
                assert failed.is_error
            failed = await client.call_tool('prepare_sampling_reference_definition', {'reference_kind': 'aql_level', 'action': 'create',
                'name': 'New', 'changes': {'aql_rows': [dict(op='add', sample_size_code='C', sample_size='20', accepts=0, rejects=1)]}}, raise_on_error=False)
            assert failed.is_error
            ready = await client.call_tool('prepare_sampling_reference_definition', {'reference_kind': 'inspection_level', 'action': 'create',
                'name': 'NewLevel', 'changes': {'lot_ranges': [dict(op='add', sample_size_code='A', min_lot_qty=1, max_lot_qty=10)]}})
            assert json.loads(ready.content[0].text)['state'] == 'ready'
    asyncio.run(run())
