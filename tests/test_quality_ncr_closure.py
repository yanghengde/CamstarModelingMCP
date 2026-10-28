import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from tools import mcp, get_tool_func, quality_ncr_closure as u, quality_plans as p
from tests.test_quality_configuration import env as base_env, key
from tests.test_quality_ncr_escalation import env as escalation_env, NCR, CONT, ORG, EVENT, named, snapshot
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION
from agent.safety import evaluate_mutations, MutationCounts, plan_preview

RESOLUTION, CLOSER = key(500), key(501)
def result(coro): return json.loads(asyncio.run(coro))


@pytest.fixture
def env(escalation_env, monkeypatch):
    state = escalation_env
    state['report']['EscalatedEventId'] = EVENT
    event = dict(EventId=EVENT, EventName='Linked', CDOTypeId='7540', Category='1', Status='5', OrganizationId=ORG,
                 IsFrozen='true', CloseDateGMT='2026-10-28T01:00:00Z', ClosedById=CLOSER, ChangeCount='1')
    code = named('Resolved', RESOLUTION)
    state['rows']['NCRResolutionCodes'] = [code]; state['counters'][RESOLUTION] = 1
    state.update(event=event, code=code, transform=None, transient=0)
    old_sql = u.q.sql_rows
    async def sql(query, maximum):
        if ' FROM Event WHERE ' in query: return [copy.deepcopy(state['event'])] if state['event'] else []
        if ' FROM NCRResolutionCode WHERE ' in query:
            return [{'NCRResolutionCodeId': RESOLUTION, 'ChangeCount': str(state['counters'][RESOLUTION])}]
        return await old_sql(query, maximum)
    sql_mock = AsyncMock(side_effect=sql); monkeypatch.setattr(u.q, 'sql_rows', sql_mock)
    def echo(value):
        if isinstance(value, dict): return {p.FIELD_CASE.get(k, k[0].upper()+k[1:]): echo(v) for k, v in value.items()}
        if isinstance(value, list): return [echo(v) for v in value]
        return value
    async def preview(method, path, **kwargs):
        assert method == 'POST' and path == '/api/NCRCloseQuality' and kwargs['params']['execute'] == 'false'
        state['transient'] += 1
        data = echo(copy.deepcopy(kwargs['body']))
        data['Container'] = {'Name': 'C', 'InstanceId': CONT}
        data['ServiceDetail'].update(InstanceId=key(1000+state['transient']), Parent={'InstanceId': key(2000+state['transient'])})
        if state['transform']: state['transform'](data)
        return json.dumps({'data': data})
    preview_mock = AsyncMock(side_effect=preview); monkeypatch.setattr(u, 'request', preview_mock)
    state.update(sql=sql_mock, preview=preview_mock)
    return state


def prepare(**kwargs): return result(u.prepare_quality_ncr_closure('N', 'Resolved', **kwargs))
def execute(ready): return result(p.execute_quality_transaction(ready['plan_id'], ready['plan_digest']))


def test_native_quality_close_requires_closed_original_event_and_exact_resolution(env):
    ready = prepare(ncr_comment='quality investigation complete', comments='明确收尾')
    assert ready['state'] == 'ready' and ready['static_and_echo_verified']
    assert ready['request_preview']['path'] == '/api/NCRCloseQuality'
    body = ready['request_preview']['body']
    assert set(body) == {'nonconformanceReport', 'serviceDetail', 'comments'}
    assert body['serviceDetail'] == {'ncrResolutionCode': {'name': 'Resolved', 'instanceId': RESOLUTION},
                                     'ncrComment': 'quality investigation complete'}
    assert body['nonconformanceReport']['instanceId'] == NCR
    assert body['comments'].startswith('MCP-QUAL:') and body['comments'].endswith(' 明确收尾')
    assert snapshot(ready)['event_before']['Status'] == 5
    assert ready['server_context']['detail_contract'] == 'NCRCloseQualityDetail'
    assert not ready['server_context']['ncr_closed_verified']
    env['client'].request.assert_not_awaited()


def test_single_execution_transient_detail_ids_and_same_unknown_policy(env):
    ready = prepare()
    assert execute(ready)['state'] == 'unknown' and 'error' in execute(ready)
    assert env['transient'] == 2
    env['client'].request.assert_awaited_once()
    call = env['client'].request.await_args
    assert call.kwargs['params'] == {'execute': 'true'} and not call.kwargs['follow_redirects']


@pytest.mark.parametrize('status', ['1', '2', '3', '6', '7', '8', '', None, True])
def test_pending_review_deleted_and_unknown_events_are_not_closed(env, status):
    env['event']['Status'] = status
    assert 'error' in prepare()
    env['preview'].assert_not_awaited()


@pytest.mark.parametrize('field,value', [('CloseDateGMT', None), ('CloseDateGMT', ''), ('CloseDateGMT', ' '),
    ('ClosedById', None), ('ClosedById', 'invalid'), ('EventId', key(999)), ('Category', None),
    ('CDOTypeId', 0), ('OrganizationId', ''), ('ChangeCount', ''), ('IsFrozen', None)])
def test_closed_event_without_complete_typed_evidence_is_refused(env, field, value):
    env['event'][field] = value
    assert 'error' in prepare()


def test_event_lookup_uses_original_typed_event_table_not_id_prefix_or_name(env):
    ready = prepare()
    assert ready['state'] == 'ready'
    queries = [call.args[0] for call in env['sql'].await_args_list if ' FROM Event WHERE ' in call.args[0]]
    assert queries and all("EventId='"+EVENT+"'" in q and 'TOP 2' in q for q in queries)
    assert snapshot(ready)['event_before']['EventName'] == 'Linked'


@pytest.mark.parametrize('field,value', [('NCRStatus', '2'), ('NCRStatus', ''), ('EscalatedEventId', None),
    ('EscalatedEventId', ''), ('EscalatedEventId', 'not-an-id'), ('CloseDateGMT', '2026-10-28'), ('ChangeCount', None)])
def test_closed_unlinked_or_unknown_ncr_does_not_use_quality_close(env, field, value):
    env['report'][field] = value
    assert 'error' in prepare()
    env['preview'].assert_not_awaited()


def test_missing_original_event_and_missing_source_fields_are_refused(env):
    env['event'] = None
    assert 'error' in prepare()
    env['event'] = {}
    assert 'error' in prepare()


def test_failure_list_and_active_container_stay_required_without_hold_release(env):
    assert prepare()['state'] == 'ready'  # Active container with a quality hold.
    env['assignments'] = []
    assert 'error' in prepare()
    env['container']['Status'] = '2'
    assert 'error' in prepare()


def test_resolution_code_is_explicit_not_automatic_first_choice(env):
    env['rows']['NCRResolutionCodes'] = []
    assert 'error' in prepare()


@pytest.mark.parametrize('target', ['code', 'event'])
def test_frozen_reference_is_a_configuration_freeze_not_closure_permission(env, target):
    if target == 'code': env['code']['IsFrozen'] = True
    else: env['event']['IsFrozen'] = 'true'
    assert prepare()['state'] == 'ready'


@pytest.mark.parametrize('mode', ['report', 'resolution', 'ncr_comment', 'comments', 'container', 'container_name', 'paging'])
def test_exact_nonexecuting_echo_and_derived_container_are_required(env, mode):
    def transform(data):
        if mode == 'report': data['NonconformanceReport']['InstanceId'] = key(999)
        if mode == 'resolution': data['ServiceDetail']['NCRResolutionCode']['InstanceId'] = key(999)
        if mode == 'ncr_comment': data['ServiceDetail']['NCRComment'] = 'altered'
        if mode == 'comments': data['Comments'] = 'different marker'
        if mode == 'container': data['Container']['InstanceId'] = key(999)
        if mode == 'container_name': data['Container']['Name'] = 'different'
        if mode == 'paging': data['ServiceDetail@odata.nextLink'] = 'more'
    env['transform'] = transform
    assert 'error' in prepare(ncr_comment='requested')
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('target', ['report', 'event', 'container', 'resolution'])
def test_prepare_rechecks_original_source_and_dependencies(env, target):
    def transform(data):
        if target == 'report': env['report']['ChangeCount'] = '2'
        if target == 'event': env['event']['ChangeCount'] = '2'
        if target == 'container': env['container']['CurrentHoldCount'] = '2'
        if target == 'resolution': env['counters'][RESOLUTION] += 1
    env['transform'] = transform
    assert 'error' in prepare()


@pytest.mark.parametrize('target', ['reopened_event', 'relinked_report', 'closed_report', 'resolution'])
def test_changed_state_before_execution_blocks_http_attempt(env, target):
    ready = prepare()
    if target == 'reopened_event': env['event']['Status'] = '1'
    if target == 'relinked_report': env['report']['EscalatedEventId'] = key(999)
    if target == 'closed_report': env['report']['NCRStatus'] = '2'
    if target == 'resolution': env['code']['Description'] = 'changed'
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode', ['kind', 'action', 'service', 'process', 'event', 'resolution', 'failure_codes'])
def test_transport_cannot_switch_service_or_override_workflow_state(env, mode):
    snap = snapshot(prepare())
    if mode == 'kind': snap['kind'] = 'capa'
    if mode == 'action': snap['action'] = 'close'
    if mode == 'service': snap['service'] = 'NCRClose'
    if mode == 'process': snap['body']['processESignatures'] = False
    if mode == 'event': snap['body']['qualityObject'] = {'instanceId': EVENT}
    if mode == 'resolution': snap['body']['serviceDetail']['ncrResolutionCode']['instanceId'] = key(999)
    if mode == 'failure_codes': snap['body']['serviceDetail']['ncrFailureCodes'] = []
    with pytest.raises(ValueError): p.transport(snap)


def test_unknown_blocks_another_ncr_plan_and_container_transaction(env):
    first = prepare(); second = prepare()
    assert execute(first)['state'] == 'unknown' and 'error' in execute(second)
    env['client'].request.assert_awaited_once()
    snap = snapshot(first)
    other = {'kind': 'lot_inspection', 'environment': snap['environment'], 'scope_key': 'other',
             'target_id': key(999), 'resource_reads': [], 'resource_writes': ['runtime:container:id:'+CONT]}
    assert p.conflicts(snap, other)


def test_inspect_does_not_follow_replaced_event_or_use_closed_state_as_success(env):
    ready = prepare(); execute(ready)
    env['report'].update(NCRStatus='2', EscalatedEventId=key(999))
    out = result(p.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert out['current_record']['NCRStatus'] == '2'
    assert out['original_linked_event_current_record']['EventId'] == EVENT
    assert not out['current_report_points_to_original_event']
    assert not out['verified_success'] and not out['state_changed'] and not out['retry_allowed']
    with p.db_connect() as db:
        assert db.execute('SELECT state FROM plans WHERE id=?', (ready['plan_id'],)).fetchone()[0] == 'unknown'


@pytest.mark.parametrize('kwargs', [{'comments': '\0'}, {'comments': '😀'*91}, {'ncr_comment': True}, {'ncr_comment': 'x'*4001}])
def test_notes_are_bounded_and_typed(env, kwargs):
    assert 'error' in prepare(**kwargs)


def test_registry_routing_confirmation_and_actual_mcp_lifecycle(env):
    name = 'prepare_quality_ncr_closure'
    assert get_tool_func(name) and TOOL_POLICIES[name].effect == 'read' and name in ROUTING_INSTRUCTION
    tools = [{'function': {'name': n}} for n in TOOL_POLICIES]
    assert name in {t['function']['name'] for t in select_tools(tools, [{'role': 'user', 'content': '质量事件已经关闭，请关闭原NCR'}])}
    async def run():
        async with Client(mcp) as client:
            schema = next(t.input_schema for t in await client.list_tools() if t.name == name)
            assert schema['additionalProperties'] is False
            raw = await client.call_tool(name, {'report_name': 'N', 'resolution_code_name': 'Resolved'})
            ready = json.loads(raw.content[0].text); assert ready['state'] == 'ready'
            keys = {k: ready[k] for k in ('plan_id', 'plan_digest')}
            calls = [{'function': {'name': 'execute_quality_transaction', 'arguments': json.dumps(keys)}}]
            assert evaluate_mutations(MutationCounts(), calls).requires_approval and 'NCRCloseQuality' in plan_preview(calls)
            posted = await client.call_tool('execute_quality_transaction', keys)
            assert json.loads(posted.content[0].text)['state'] == 'unknown'
            inspected = await client.call_tool('inspect_quality_transaction', keys)
            assert not json.loads(inspected.content[0].text)['verified_success']
            bad = await client.call_tool(name, {'report_name': 'N', 'resolution_code_name': 'Resolved', 'force_close': True}, raise_on_error=False)
            assert bad.is_error
    asyncio.run(run())
    env['client'].request.assert_awaited_once()
