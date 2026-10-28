import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from tools import mcp, get_tool_func, quality_ncr_escalation as e, quality_plans as p
from tests.test_quality_configuration import env as base_env, key
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION
from agent.safety import evaluate_mutations, MutationCounts, plan_preview

NCR, CONT, ORG, CLASS, SUB, PRIORITY, MAP, FAILURE, EVENT = [key(n) for n in range(100, 109)]
def result(coro): return json.loads(asyncio.run(coro))
def named(name, iid, **kw): return dict(Name=name, InstanceID=iid, IsFrozen=False, Description='', **kw)


@pytest.fixture
def env(base_env, monkeypatch):
    state = base_env
    mapped = dict(InstanceID=MAP, Parent={'InstanceId': ORG}, IsFrozen=False, PEDefault=False,
                  Classification={'Name': 'Class', 'InstanceId': CLASS}, Subclassification={'Name': 'Sub', 'InstanceId': SUB},
                  ChecklistTemplate=None, FailureModeGroup=None, Owner=None, Role=None)
    org = named('Org', ORG, CollectESigForAllQualityTxns=False, UserRequiredForEsig=False,
                QualityESigTxns=[], EventClassificationSpecMaps=[mapped])
    state['rows'].update(Organizations=[org], Classifications=[named('Class', CLASS)],
                         SubClassifications=[named('Sub', SUB)], PriorityLevels=[named('High', PRIORITY)])
    state['counters'].update({iid: 1 for iid in (ORG, CLASS, SUB, PRIORITY)})
    report = dict.fromkeys(e.q.NCR_COLUMNS.split(','))
    report.update(NonconformanceReportId=NCR, NonconformanceReportName='N', ContainerId=CONT,
                  NCRStatus='1', ChangeCount='1', NCRComment='observed defect')
    assignment = dict(NonconformanceReportId=NCR, FieldId='10019', Sequence='0', NCRFailureCodesId=FAILURE,
                      NCRFailureCodeName='Failure', FailureCodeChangeCount='1')
    container = dict(ContainerId=CONT, ContainerName='C', Status='1', CurrentHoldCount='1', ChangeCount='1')
    state.update(report=report, assignments=[assignment], container=container, org=org, mapped=mapped, transform=None, transient=0)
    original_sql = e.q.sql_rows
    async def sql(query, maximum):
        if ' FROM NonconformanceReport WHERE ' in query: return [copy.deepcopy(state['report'])] if state['report'] else []
        if 'FROM NonconformanceReportNCRFailure' in query: return copy.deepcopy(state['assignments'])
        if ' FROM Container WHERE ' in query: return [copy.deepcopy(state['container'])] if state['container'] else []
        for _, table, column in e.CONFIG.values():
            if ' FROM '+table+' WHERE ' in query:
                iid = query.split("='", 1)[1].split("'", 1)[0]
                return [{column: iid, 'ChangeCount': str(state['counters'][iid])}]
        return await original_sql(query, maximum)
    sql_mock = AsyncMock(side_effect=sql); monkeypatch.setattr(e.q, 'sql_rows', sql_mock)
    def echo(value):
        if isinstance(value, dict): return {p.FIELD_CASE.get(k, k[0].upper()+k[1:]): echo(v) for k, v in value.items()}
        if isinstance(value, list): return [echo(v) for v in value]
        return value
    async def preview(method, path, **kw):
        assert method == 'POST' and path == '/api/NCREscalate' and kw['params']['execute'] == 'false'
        state['transient'] += 1
        data = echo(copy.deepcopy(kw['body']))
        data.update(Container={'Name': 'C', 'InstanceId': CONT}, BriefDescription='Event escalated from Camstar Manufacturing NCR',
                    EventDetail={'InstanceId': key(1000+state['transient']), 'Parent': {'InstanceId': key(2000+state['transient'])}})
        if state['transform']: state['transform'](data)
        return json.dumps({'data': data})
    preview_mock = AsyncMock(side_effect=preview); monkeypatch.setattr(e, 'request', preview_mock)
    client = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200, text='{"value":"completed"}')))
    monkeypatch.setattr(p, 'get_client', lambda: client)
    # The old NCR inspector uses Inquiry history; it remains evidence only.
    monkeypatch.setattr(e.q, 'get_quality_history', AsyncMock(return_value='{"rows":[],"complete":false}'))
    state.update(sql=sql_mock, preview=preview_mock, client=client)
    return state


def prepare(**kwargs): return result(e.prepare_ncr_escalation('N', 'Org', 'Class', 'Sub', 'High', **kwargs))
def execute(ready): return result(p.execute_quality_transaction(ready['plan_id'], ready['plan_digest']))
def snapshot(ready):
    with p.db_connect() as db:
        return json.loads(db.execute('SELECT payload FROM plans WHERE id=?', (ready['plan_id'],)).fetchone()[0])


def test_options_are_complete_organization_pairs_not_transaction_permission(env):
    out = result(e.get_ncr_escalation_options('Org'))
    assert out['complete'] and out['organization']['EventClassificationSpecMaps'][0]['InstanceID'] == MAP
    assert not out['target_permission_verified'] and not out['signature_verified']
    assert not p.PLAN_DB.exists()
    env['preview'].assert_not_awaited(); env['client'].request.assert_not_awaited()


def test_prepare_binds_source_and_explicit_required_refs_with_no_event_fabrication(env):
    ready = prepare(comments='升级调查')
    assert ready['state'] == 'ready' and ready['static_and_echo_verified']
    assert ready['request_preview']['path'] == '/api/NCREscalate'
    body = ready['request_preview']['body']
    assert set(body) == {'nonconformanceReport', 'organization', 'classification', 'subClassification', 'priorityLevel', 'comments'}
    assert body['nonconformanceReport'] == {'name': 'N', 'instanceId': NCR}
    assert body['organization']['instanceId'] == ORG and body['priorityLevel']['instanceId'] == PRIORITY
    assert body['comments'].startswith('MCP-QUAL:') and body['comments'].endswith(' 升级调查')
    assert ready['server_context']['selected_classification_map']['InstanceID'] == MAP
    assert ready['before']['failure_code_assignments'][0]['FieldId'] == 10019
    assert ready['before']['failure_code_assignments'][0]['Sequence'] == 0
    assert ready['server_context']['created_event_verified'] is False
    assert 'EventDetail' not in body and 'eventName' not in body
    assert ready['before']['NCRStatus'] == '1'
    env['client'].request.assert_not_awaited()


def test_escalation_held_active_container_is_allowed_without_release(env):
    ready = prepare()
    assert ready['state'] == 'ready' and snapshot(ready)['container_before']['CurrentHoldCount'] == 1
    assert execute(ready)['state'] == 'unknown'
    env['client'].request.assert_awaited_once()
    call = env['client'].request.await_args
    assert call.kwargs['params'] == {'execute': 'true'} and call.kwargs['follow_redirects'] is False
    assert 'error' in execute(ready)


def test_transient_event_detail_ids_do_not_break_digest_or_claim_creation(env):
    ready = prepare()
    assert execute(ready)['state'] == 'unknown'
    assert env['transient'] == 2
    assert not ready['server_context']['created_event_verified']


@pytest.mark.parametrize('field,value', [('NCRStatus', '2'), ('NCRStatus', ''), ('NCRStatus', None),
    ('EscalatedEventId', EVENT), ('CloseDateGMT', '2026-10-28T00:00:00Z'), ('ChangeCount', ''), ('ContainerId', 'bad')])
def test_closed_escalated_or_unknown_source_cannot_be_used(env, field, value):
    env['report'][field] = value
    assert 'error' in prepare()
    env['preview'].assert_not_awaited()


def test_missing_source_fields_and_missing_report_are_refused(env):
    del env['report']['EscalatedEventId']
    assert 'error' in prepare()
    env['report'] = None
    assert 'error' in prepare()


@pytest.mark.parametrize('mode', ['empty', 'field', 'sequence_bool', 'sequence_gap_invalid', 'foreign', 'duplicate', 'overflow'])
def test_failure_assignment_ownership_and_live_numeric_strings_are_validated(env, mode):
    if mode == 'empty': env['assignments'] = []
    if mode == 'field': env['assignments'][0]['FieldId'] = '999'
    if mode == 'sequence_bool': env['assignments'][0]['Sequence'] = True
    if mode == 'sequence_gap_invalid': env['assignments'][0]['Sequence'] = '-1'
    if mode == 'foreign': env['assignments'][0]['NonconformanceReportId'] = EVENT
    if mode == 'duplicate': env['assignments'] *= 2
    if mode == 'overflow': env['assignments'] *= 51
    assert 'error' in prepare()


@pytest.mark.parametrize('field,value', [('Status', '2'), ('CurrentHoldCount', None), ('ChangeCount', ''), ('ContainerId', EVENT)])
def test_container_unknown_or_mismatched_state_is_refused(env, field, value):
    env['container'][field] = value
    assert 'error' in prepare()


@pytest.mark.parametrize('field,value', [('Classification', {'Name': 'Other', 'InstanceId': EVENT}),
    ('Subclassification', {'Name': 'Other', 'InstanceId': EVENT}), ('Subclassification', None),
    ('Parent', {'InstanceId': EVENT}), ('InstanceID', 'bad')])
def test_complete_org_map_cannot_be_replaced_by_global_named_lookup(env, field, value):
    env['mapped'][field] = value
    assert 'error' in prepare()
    env['preview'].assert_not_awaited()


def test_duplicate_combination_is_ambiguous_even_with_distinct_mapping_ids(env):
    env['org']['EventClassificationSpecMaps'].append({**env['mapped'], 'InstanceID': EVENT})
    assert 'error' in prepare()
    assert result(e.get_ncr_escalation_options('Org'))['complete']


@pytest.mark.parametrize('mode', ['absent', 'overflow', 'duplicate_id'])
def test_org_mapping_list_must_be_complete(env, mode):
    if mode == 'absent': env['org']['EventClassificationSpecMaps'] = None
    if mode == 'overflow': env['org']['EventClassificationSpecMaps'] *= 101
    if mode == 'duplicate_id': env['org']['EventClassificationSpecMaps'] *= 2
    assert 'error' in prepare()
    assert not result(e.get_ncr_escalation_options('Org'))['complete']


def test_empty_org_map_options_are_empty_but_no_plan_is_ready(env):
    env['org']['EventClassificationSpecMaps'] = []
    assert result(e.get_ncr_escalation_options('Org'))['organization']['EventClassificationSpecMaps'] == []
    assert 'error' in prepare()


@pytest.mark.parametrize('kind', ['Organizations', 'Classifications', 'SubClassifications', 'PriorityLevels'])
def test_required_named_refs_are_never_guessed(env, kind):
    env['rows'][kind] = []
    assert 'error' in prepare()


def test_frozen_configuration_does_not_imply_event_transaction_prohibited(env):
    for entity in ('Organizations', 'Classifications', 'SubClassifications', 'PriorityLevels'):
        env['rows'][entity][0]['IsFrozen'] = True
    env['mapped']['IsFrozen'] = True
    assert prepare()['state'] == 'ready'


def test_signature_settings_are_preserved_without_signing_or_disabling_processing(env):
    env['org'].update(CollectESigForAllQualityTxns=True, UserRequiredForEsig=True, QualityESigTxns=['8079'])
    env['transform'] = lambda data: data.update(ESigRequirement={'Name': 'Signer', 'InstanceId': EVENT})
    ready = prepare()
    assert ready['server_context']['organization_signature_settings']['CollectESigForAllQualityTxns']
    assert not ready['server_context']['signature_verified']
    assert 'processESignatures' not in ready['request_preview']['body']


@pytest.mark.parametrize('field', ['NonconformanceReport', 'Organization', 'Classification', 'SubClassification', 'PriorityLevel'])
def test_echo_wrong_required_ref_never_approves_plan(env, field):
    env['transform'] = lambda data: data[field].update(InstanceId=EVENT)
    assert 'error' in prepare()
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode', ['container', 'container_name', 'missing_container', 'comments', 'paging'])
def test_echo_loaded_source_container_comment_and_completeness_are_checked(env, mode):
    def mutate(data):
        if mode == 'container': data['Container']['InstanceId'] = EVENT
        if mode == 'container_name': data['Container']['Name'] = 'Other'
        if mode == 'missing_container': del data['Container']
        if mode == 'comments': data['Comments'] = 'different marker'
        if mode == 'paging': data['EventDetail@odata.nextLink'] = 'more'
    env['transform'] = mutate
    assert 'error' in prepare()


@pytest.mark.parametrize('target', ['report', 'assignments', 'container', 'org', 'class', 'priority'])
def test_late_changes_prevent_ready_plan(env, target):
    def mutate(data):
        if target == 'report': env['report']['ChangeCount'] = '2'
        if target == 'assignments': env['assignments'][0]['FailureCodeChangeCount'] = '2'
        if target == 'container': env['container']['ChangeCount'] = '2'
        if target == 'org': env['mapped']['Owner'] = {'InstanceId': EVENT}
        if target == 'class': env['rows']['Classifications'][0]['Description'] = 'changed'
        if target == 'priority': env['counters'][PRIORITY] += 1
    env['transform'] = mutate
    assert 'error' in prepare()


def test_existing_event_added_after_prepare_prevents_execution(env):
    ready = prepare(); env['report']['EscalatedEventId'] = EVENT
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode', ['kind', 'action', 'service', 'report', 'process', 'container', 'event_name'])
def test_transport_is_bidirectionally_closed_and_rejects_bypass_fields(env, mode):
    snap = snapshot(prepare())
    if mode == 'kind': snap['kind'] = 'capa'
    if mode == 'action': snap['action'] = 'close'
    if mode == 'service': snap['service'] = 'NCRUpdate'
    if mode == 'report': snap['body']['nonconformanceReport']['instanceId'] = EVENT
    if mode == 'process': snap['body']['processESignatures'] = False
    if mode == 'container': snap['body']['container'] = {'name': 'Other', 'instanceId': EVENT}
    if mode == 'event_name': snap['body']['eventName'] = 'Invented'
    with pytest.raises(ValueError): p.transport(snap)


def test_unknown_escalation_conflicts_with_old_ncr_update_and_container_inspection(env):
    first = prepare(); second = prepare()
    assert execute(first)['state'] == 'unknown' and 'error' in execute(second)
    env['client'].request.assert_awaited_once()
    snap = snapshot(first)
    old = {'kind': 'ncr', 'action': 'update', 'environment': snap['environment'], 'scope_key': 'ncr:n', 'target_id': NCR,
           'body': {}, 'before': {'ContainerId': CONT}}
    assert p.conflicts(snap, old)
    other = {'kind': 'lot_inspection', 'environment': snap['environment'], 'scope_key': 'other', 'target_id': EVENT,
             'resource_writes': ['runtime:container:id:'+CONT], 'resource_reads': []}
    assert p.conflicts(snap, other)


def test_inspection_current_escalated_event_is_evidence_not_success_or_unlock(env):
    ready = prepare(); execute(ready)
    env['report']['EscalatedEventId'] = EVENT
    out = result(p.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert out['current_record']['EscalatedEventId'] == EVENT
    assert not out['verified_success'] and not out['state_changed']
    with p.db_connect() as db:
        assert db.execute('SELECT state FROM plans WHERE id=?', (ready['plan_id'],)).fetchone()[0] == 'unknown'


@pytest.mark.parametrize('comment', ['\0', '😀'*91])
def test_utf16_comment_limit(env, comment):
    assert 'error' in prepare(comments=comment)


def test_real_mcp_multiturn_registry_routing_and_confirmed_single_execution(env):
    names = {'get_ncr_escalation_options', 'prepare_ncr_escalation'}
    assert all(get_tool_func(n) and TOOL_POLICIES[n].effect == 'read' for n in names)
    assert all(n in ROUTING_INSTRUCTION for n in names)
    tools = [{'function': {'name': n}} for n in TOOL_POLICIES]
    for question in ('把NCR升级为质量事件', '查询组织允许的质量分类组合'):
        assert names <= {t['function']['name'] for t in select_tools(tools, [{'role': 'user', 'content': question}])}
    async def run():
        async with Client(mcp) as client:
            async def call(name, args):
                out = await client.call_tool(name, args); assert not out.is_error
                return json.loads(out.content[0].text)
            schemas = {t.name: t.input_schema for t in await client.list_tools()}
            assert all(schemas[n]['additionalProperties'] is False for n in names)
            opts = await call('get_ncr_escalation_options', {'organization_name': 'Org'})
            mapped = opts['organization']['EventClassificationSpecMaps'][0]
            ready = await call('prepare_ncr_escalation', {'report_name': 'N', 'organization_name': 'Org',
                'classification_name': mapped['Classification']['Name'], 'subclassification_name': mapped['Subclassification']['Name'], 'priority_name': 'High'})
            keys = {k: ready[k] for k in ('plan_id', 'plan_digest')}
            calls = [{'function': {'name': 'execute_quality_transaction', 'arguments': json.dumps(keys)}}]
            assert evaluate_mutations(MutationCounts(), calls).requires_approval
            assert 'NCREscalate' in plan_preview(calls)
            assert (await call('execute_quality_transaction', keys))['state'] == 'unknown'
            assert not (await call('inspect_quality_transaction', keys))['verified_success']
            bad = await client.call_tool('prepare_ncr_escalation', {'report_name': 'N', 'organization_name': 'Org',
                'classification_name': 'Class', 'subclassification_name': 'Sub', 'priority_name': 'High',
                'classification_map_instance_id': MAP}, raise_on_error=False)
            assert bad.is_error
    asyncio.run(run())
    env['client'].request.assert_awaited_once()
