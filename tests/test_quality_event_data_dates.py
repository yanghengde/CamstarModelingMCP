"""Explicit instants, real storage contracts, and isolated native date readback."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock
from pathlib import Path
import pytest
from fastmcp import Client
from pydantic import ValidationError
from tools import mcp,get_tool_func,quality_event_data as e,quality_event_data_dates as d,quality_plans as p
from tests.test_quality_event_data import env as data_env,draft_env,submission_env,update_env,triage_env,prepare,execute,snap,result,DATA,TOKEN
from tests.test_quality_event_submission import final_fixture_review
from tests.test_quality_event_triage import OTHER
from tools.tool_discovery import get_capabilities
from agent.tool_routing import select_tools,ROUTING_INSTRUCTION
from core.tool_catalog import TOOL_POLICIES


@pytest.fixture
def env(data_env):return data_env


def storage(env,kind='datetime2',precision=7):
    for row in env['date_storage_rows']:row.update(DATA_TYPE=kind,DATETIME_PRECISION=precision)
    # Existing stored timestamps must be compatible with the simulated schema.
    if kind=='datetime' or precision<7:
        env['data'].update(DateReceived='2026-10-29 09:00:00',DateReceivedGMT='2026-10-29 01:00:00')


def test_date_context_profiles_storage_only_when_explicitly_requested_without_plan(env):
    out=result(e.get_quality_event_data_context('E','Org'))
    assert out['complete'] and out['date_storage'] is None and not out['date_storage_checked'] and not out['date_policy']['mes_timezone_inferred']
    assert not any('DATA_TYPE,DATETIME_PRECISION' in call.args[0] for call in e.q.sql_rows.await_args_list)
    out=result(e.get_quality_event_data_context('E','Org',include_date_storage=True))
    assert out['complete'] and out['date_storage_checked'] and out['date_storage']['columns']['DateReceived']['precision']==7
    assert not out['ready_for_execution'] and not p.PLAN_DB.exists() and not env['data_posts']


def test_precision_as_metadata_strings_is_normalized_and_context_flag_is_not_a_write_bypass(env):
    for row in env['date_storage_rows']:row['DATETIME_PRECISION']='7'
    out=result(e.get_quality_event_data_context('E','Org',include_date_storage=True))
    assert out['complete'] and out['date_storage']['columns']['ReportDate']['precision']==7
    bad=result(e.get_quality_event_data_context('E','Org',include_date_storage='true'))
    assert not bad['complete'] and not env['data_posts']


def test_two_dates_with_different_offsets_save_readback_and_review_preserve_root_event_and_other_values(env):
    original=copy.deepcopy(env['data']);root=copy.deepcopy(env['extra'])
    ready=prepare(changes=dict(date_received='2026-11-01T02:03:04.1234567-05:30',report_date='2026-10-31T23:30:00.0000001+14:00'))
    assert ready.get('state')=='ready',ready
    body=ready['request_preview']['body'];dt=body['eventDataDetail'];saved=snap(ready)
    assert dt['dateReceivedGMT']=='2026-11-01T07:33:04.1234567Z' and dt['reportDateGMT']=='2026-10-31T09:30:00.0000001Z'
    assert dt['dateReceived']=='2026-11-01T02:03:04.1234567-05:30' and dt['reportDate']=='2026-10-31T23:30:00.0000001+14:00'
    assert body['reportedDateGMT']=='2026-10-29T03:00:00Z' and body['occurrenceDateGMT']=='2026-10-29T02:00:00.1234567Z'
    assert saved['preview']['date_storage_checked'] and not ready['static_and_echo_verified'] and not saved['preview']['date_policy']['native_timezone_verified']
    assert saved['preview']['changed_fields']==['date_received','report_date']
    assert execute(ready)['state']=='unknown' and len(env['data_posts'])==1
    assert env['data']['DateReceived']=='2026-11-01 15:33:04.1234567' and env['data']['ReportDateGMT']=='2026-10-31 09:30:00.0000001'
    assert env['extra']==root
    for key,value in original.items():
        if key not in (*e.DATA_DATES,'ChangeCount'):assert env['data'][key]==value
    evidence=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert not evidence['verified_success'] and evidence['date_evidence']['complete'] and evidence['date_evidence']['all_planned_instants_match'],evidence
    assert evidence['date_evidence']['fields']['date_received']['observed_local']=='2026-11-01T15:33:04.1234567+08:00'
    assert not evidence['date_evidence']['causal_success_verified'] and not evidence['date_evidence']['retry_allowed']
    assert evidence['date_evidence']['root_values_match_plan'] and evidence['date_evidence']['other_data_values_match_plan']
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'
    next_plan=prepare(changes=dict(report_date='2026-11-02T12:00:00+08:00'));assert next_plan.get('state')=='ready',next_plan
    assert 'error' in execute(next_plan) and len(env['data_posts'])==1
    final_fixture_review(ready)
    assert execute(next_plan)['state']=='unknown' and len(env['data_posts'])==2
    assert env['data']['ReportDateGMT']=='2026-11-02 04:00:00'


def test_same_instant_different_offset_or_fraction_spelling_does_not_rewrite_date_pair(env):
    value='2026-10-29T01:00:00.1234567Z'
    assert 'error' in prepare(changes=dict(date_received=value)) and not env['data_posts']
    ready=prepare(changes=dict(date_received=value,device_location='B'));assert ready.get('state')=='ready',ready
    dt=ready['request_preview']['body']['eventDataDetail'];saved=snap(ready)
    assert dt['dateReceived']=='2026-10-29T09:00:00.1234567+08:00' and dt['dateReceivedGMT']=='2026-10-29T01:00:00.1234567Z'
    assert saved['preview']['changed_fields']==['device_location'] and saved['preview']['date_changes']['date_received']['same_instant_preserved']


def test_omission_null_preserve_and_missing_pair_can_be_explicitly_filled(env):
    ready=prepare(changes=dict(date_received=None,report_date='2026-11-01T12:00:00+08:00'))
    assert ready.get('state')=='ready',ready
    dt=ready['request_preview']['body']['eventDataDetail']
    assert dt['dateReceived']=='2026-10-29T09:00:00.1234567+08:00' and dt['reportDateGMT']=='2026-11-01T04:00:00.0000000Z'
    assert 'error' in prepare(changes=dict(date_received=None,report_date=None))


@pytest.mark.parametrize('received,reported',[
    ('2026-11-10T00:00:00Z','2026-11-01T00:00:00Z'),('2026-11-01T00:00:00Z','2026-11-10T00:00:00Z')])
def test_no_unproven_chronology_between_distinct_received_and_report_dates(env,received,reported):
    ready=prepare(changes=dict(date_received=received,report_date=reported));assert ready.get('state')=='ready',ready
    assert not env['data_posts'] and 'no_assumed_order' in snap(ready)['preview']['date_policy']['chronology_rule']


@pytest.mark.parametrize('value',['','2026-11-01','2026-11-01T12:00:00','now','2026-11-01T12:00:00.12345678Z',
    '2026-11-01T12:00:00+14:01','2026-11-01T12:00:00-14:30','2026-11-01T12:00:00+23:00','2026-11-01T12:00:00+01:60',
    '2026-11-01T12:00:00-00:00','2026-02-30T12:00:00Z','2026-11-01T12:00:60Z','0001-01-01T00:00:00+14:00','9999-12-31T23:59:59-14:00',True,1])
def test_missing_invalid_or_unrepresentable_offset_dates_never_prepare_or_write(env,value):
    try:out=prepare(changes=dict(date_received=value))
    except ValidationError:out={'error':'closed timestamp input rejected'}
    assert 'error' in out and env['data_loads']==0 and not env['data_posts']


@pytest.mark.parametrize('precision,value,valid',[(7,'2026-11-01T12:00:00.1234567+08:00',True),
    (6,'2026-11-01T12:00:00.1234560+08:00',True),(6,'2026-11-01T12:00:00.1234567+08:00',False),
    (3,'2026-11-01T12:00:00.1230000+08:00',True),(3,'2026-11-01T12:00:00.1234000+08:00',False),
    (0,'2026-11-01T12:00:00.0000000+08:00',True),(0,'2026-11-01T12:00:00.0000001+08:00',False)])
def test_actual_datetime2_precision_accepts_exact_values_and_never_rounds(env,precision,value,valid):
    storage(env,precision=precision);out=prepare(changes=dict(report_date=value))
    assert (out.get('state')=='ready') is valid,out
    assert not env['data_posts']
    if valid:assert out['request_preview']['body']['eventDataDetail']['reportDateGMT']==d.utc(value)


@pytest.mark.parametrize('value,valid',[('2026-11-01T12:00:00+08:00',True),('2026-11-01T12:00:00.0000000+08:00',True),
    ('2026-11-01T12:00:00.003+08:00',False),('1752-12-31T12:00:00Z',False),('1753-01-01T00:00:00+14:00',False),
    ('1753-01-01T00:00:00Z',True)])
def test_legacy_datetime_allows_whole_seconds_with_both_local_and_gmt_in_range(env,value,valid):
    storage(env,kind='datetime',precision=3);out=prepare(changes=dict(report_date=value))
    assert (out.get('state')=='ready') is valid,out
    assert not env['data_posts']


@pytest.mark.parametrize('value,utc',[
    ('0001-01-01T00:00:00Z','0001-01-01T00:00:00.0000000Z'),
    ('9999-12-31T23:59:59.9999999Z','9999-12-31T23:59:59.9999999Z'),
    ('2026-11-01T00:15:00.0000001+14:00','2026-10-31T10:15:00.0000001Z')])
def test_exact_utc_conversion_preserves_year_padding_date_boundary_and_seventh_digit(value,utc):
    assert d.utc(value)==utc


@pytest.mark.parametrize('mode',['missing','duplicate','wrong_table','unknown_column','missing_type','unknown_type','wrong_precision','bool_precision','null_precision'])
def test_unknown_or_partial_storage_metadata_cannot_be_assumed_writable(env,mode):
    rows=env['date_storage_rows']
    if mode=='missing':rows.pop()
    elif mode=='duplicate':rows[-1]=copy.deepcopy(rows[0])
    elif mode=='wrong_table':rows[0]['TABLE_NAME']='OtherTable'
    elif mode=='unknown_column':rows[0]['COLUMN_NAME']='OtherDate'
    elif mode=='missing_type':rows[0].pop('DATA_TYPE')
    elif mode=='unknown_type':rows[0]['DATA_TYPE']='datetimeoffset'
    elif mode=='wrong_precision':rows[0]['DATETIME_PRECISION']=8
    elif mode=='bool_precision':rows[0]['DATETIME_PRECISION']=True
    else:rows[0]['DATETIME_PRECISION']=None
    assert 'error' in prepare(changes=dict(report_date='2026-11-01T12:00:00Z')) and env['data_loads']==0 and not p.PLAN_DB.exists()
    # No new schema dependency is imposed on the existing text-only path.
    text=prepare();assert text.get('state')=='ready',text


def test_local_and_gmt_column_precision_are_both_enforced(env):
    storage(env,precision=7)
    env['date_storage_rows'][0]['DATETIME_PRECISION']=0
    assert 'error' in prepare(changes=dict(date_received='2026-11-01T12:00:00.1Z'))


def test_storage_metadata_changes_during_prepare_are_detected(env):
    seen=[0]
    def mutate(rows):
        seen[0]+=1
        if seen[0]==1:env['date_storage_rows'][0]['DATETIME_PRECISION']=6
    env['date_storage_hook']=mutate
    assert 'error' in prepare(changes=dict(report_date='2026-11-01T12:00:00Z')) and not p.PLAN_DB.exists() and not env['data_posts']


@pytest.mark.parametrize('mode',['precision','data','event','actor','role'])
def test_date_plan_rebuilds_temporal_storage_source_and_authorization_before_write(env,mode):
    storage(env,precision=7);env['data'].update(DateReceived='2026-10-29 09:00:00',DateReceivedGMT='2026-10-29 01:00:00')
    ready=prepare(changes=dict(report_date='2026-11-01T12:00:00Z'));assert ready.get('state')=='ready',ready
    if mode=='precision':env['date_storage_rows'][0]['DATETIME_PRECISION']=3
    elif mode=='data':env['data']['ChangeCount']+=1
    elif mode=='event':env['event']['ChangeCount']+=1;env['classes']['ChangeCount']+=1
    elif mode=='actor':env['actor']=OTHER
    else:env['authorized']=False
    assert 'error' in execute(ready) and not env['data_posts']


@pytest.mark.parametrize('mode',['gmt_override','root_time','missing_contract','wrong_precision_contract','legacy_action'])
def test_transport_recomputes_date_pairs_and_does_not_accept_raw_gmt_or_root_time_overrides(env,mode):
    saved=snap(prepare(changes=dict(report_date='2026-11-01T12:00:00.1234567Z')))
    if mode=='gmt_override':saved['body']['eventDataDetail']['reportDateGMT']='2026-11-01T12:00:00.1234566Z'
    elif mode=='root_time':saved['body']['reportedDateGMT']='2026-11-01T12:00:00Z'
    elif mode=='missing_contract':saved.pop('date_storage')
    elif mode=='wrong_precision_contract':saved['date_storage']['columns']['ReportDate']['precision']=0
    else:saved['action']='update_data_text'
    with pytest.raises(ValueError):p.transport(saved)


@pytest.mark.parametrize('mode',['400','timeout','cancel'])
def test_date_attempts_never_retry_or_bypass_unknown_scope(env,mode):
    ready=prepare(changes=dict(report_date='2026-11-01T12:00:00Z'));env['data_mode']=mode
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:assert execute(ready)['state']=='unknown'
    assert 'error' in execute(ready)
    other=prepare(changes=dict(report_date='2026-11-02T12:00:00Z'));assert other.get('state')=='ready',other
    assert 'error' in execute(other) and len(env['data_posts'])==1


def test_offset_dropping_backend_cannot_be_treated_as_timezone_verified_or_successful(env):
    env['date_backend']='drop_offset'
    ready=prepare(changes=dict(report_date='2026-11-01T12:00:00-05:00'));assert execute(ready)['state']=='unknown'
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert not out['verified_success'] and not out['date_evidence']['complete'] and not out['date_evidence']['causal_success_verified']
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown' and 'error' in execute(ready)


def test_coherent_but_different_current_dates_are_evidence_of_mismatch_not_automatic_failure_resolution(env):
    ready=prepare(changes=dict(report_date='2026-11-01T12:00:00Z'));execute(ready)
    env['data'].update(ReportDate='2026-11-03 20:00:00',ReportDateGMT='2026-11-03 12:00:00',ChangeCount=3)
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['date_evidence']['complete'] and not out['date_evidence']['all_planned_instants_match']
    assert not out['verified_success'] and result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_matching_data_dates_do_not_hide_unexpected_root_time_or_other_field_changes(env):
    ready=prepare(changes=dict(report_date='2026-11-01T12:00:00Z'));execute(ready)
    env['extra'].update(ReportedDate='2026-10-29 12:00:00',ReportedDateGMT='2026-10-29 04:00:00')
    env['data']['ProblemDescription']='Changed by another operation'
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    dates=out['date_evidence'];assert dates['complete'] and dates['all_planned_instants_match']
    assert not dates['root_values_match_plan'] and 'reportedDateGMT' in dates['root_value_mismatches']
    assert not dates['other_data_values_match_plan'] and 'problemDescription' in dates['other_data_value_mismatches']
    assert not out['verified_success'] and result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_date_inspector_refuses_replacement_data_and_never_queries_it(env):
    ready=prepare(changes=dict(report_date='2026-11-01T12:00:00Z'));execute(ready);env['extra']['EventDataId']=OTHER
    e.q.sql_rows.reset_mock()
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['original_event_data_id']==DATA and out['date_evidence']['reason']=='original_data_link_changed' and not out['verified_success']
    assert not any(' FROM EventData WHERE ' in call.args[0] and OTHER in call.args[0] for call in e.q.sql_rows.await_args_list)


def test_original_incomplete_pair_is_not_repaired_by_ignoring_native_sql_mismatch(env):
    env['data']['DateReceivedGMT']=None
    assert 'error' in prepare(changes=dict(date_received='2026-11-01T12:00:00Z')) and not env['data_posts']


def test_unrepresentable_existing_native_timezone_value_returns_safe_incomplete_context(env):
    env['data'].update(DateReceived='0001-01-01 00:00:00',DateReceivedGMT='0001-01-01 00:00:00')
    out=result(e.get_quality_event_data_context('E','Org'))
    assert not out['complete'] and not env['data_posts']


def test_legacy_text_action_can_still_be_inspected_but_cannot_be_used_for_date_payloads(env):
    old=snap(prepare());old['action']='update_data_text'
    assert p.transport(old)['path']=='/api/UpdateEventData'


def test_metadata_and_date_inspection_errors_do_not_reflect_session_credentials(env,monkeypatch):
    ready=prepare(changes=dict(report_date='2026-11-01T12:00:00Z'));execute(ready)
    original=e.q.sql_rows
    async def sql(query,maximum):
        if 'DATA_TYPE,DATETIME_PRECISION' in query:raise ValueError(TOKEN)
        return await original(query,maximum)
    monkeypatch.setattr(e.q,'sql_rows',AsyncMock(side_effect=sql))
    reply=prepare(changes=dict(report_date='2026-11-02T12:00:00Z'))
    assert 'error' in reply and TOKEN not in json.dumps(reply)
    out=result(e.get_quality_event_data_context('E','Org',include_date_storage=True))
    assert not out['complete'] and TOKEN not in json.dumps(out)
    def reflected_error(data):raise ValueError(TOKEN)
    env['data_hook']=reflected_error
    proof=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert not proof['verified_success'] and not proof['date_evidence']['complete'] and TOKEN not in json.dumps(proof)


def test_legacy_python_alias_and_native_contract_dates_are_explicit_not_root_event_timestamps(env):
    assert e.EventDataTextChanges is e.EventDataChanges
    evidence=json.loads(Path('docs/quality-fixtures/event-data-dates-native-evidence.json').read_text(encoding='utf-8'))
    assert {r['TargetField'] for r in evidence['date_mapping']}==set(e.DATA_DATES)
    assert not evidence['event_data_time_conversion_observed_in_modify_details'] and evidence['root_time_conversion_fields']==['ReportedDate','OccurrenceDate']
    for name in ('date_received_gmt','report_date_gmt','reported_date','occurrence_date','clear_date_received'):
        with pytest.raises(ValidationError):e.EventDataChanges.model_validate({name:'2026-11-01T12:00:00Z'})


def test_mcp_and_agent_direct_json_support_dates_without_new_tools_or_identity_fields(env):
    direct=result(get_tool_func('prepare_quality_event_data_update')('E','Org',dict(report_date='2026-11-01T12:00:00Z'),'Reviewed direct date'))
    assert direct.get('state')=='ready',direct
    async def check():
        async with Client(mcp) as client:
            tools={t.name:t for t in await client.list_tools()};schema=tools['prepare_quality_event_data_update'].input_schema
            patch=schema['properties']['changes'];assert patch['additionalProperties'] is False
            assert set(patch['properties'])=={'problem_description','device_location','date_received','report_date'}
            reply=await client.call_tool('prepare_quality_event_data_update',dict(event_name='E',organization_name='Org',changes=dict(date_received='2026-11-01T12:00:00+08:00'),comments='Reviewed MCP date'))
            ready=json.loads(reply.content[0].text);assert ready.get('state')=='ready',ready
            assert tools['get_quality_event_data_context'].input_schema['properties']['include_date_storage']['default'] is False
    asyncio.run(check());assert len([p for p in TOOL_POLICIES.values() if p.group=='quality'])==77 and not env['data_posts']


@pytest.mark.parametrize('turns,writes',[
    (['查询事件E收到日期维护条件，不要保存'],False),(['查看事件E扩展资料','将收到日期改为明确时刻'],True),
    (['修改事件E扩展报告日期'],True),(['查看事件E扩展报告日期','查询数据库时间精度'],False),
    (['查看事件E扩展资料','修改资源R设备位置'],False)])
def test_date_questions_keep_field_meaning_and_do_not_reuse_resource_or_root_event_date_semantics(turns,writes):
    tools=[dict(type='function',function=dict(name=n)) for n in TOOL_POLICIES]
    selected={x['function']['name'] for x in select_tools(tools,[dict(role='user',content=x) for x in turns])}
    if '资源R' in turns[-1]:assert 'prepare_quality_event_data_update' not in selected
    else:
        assert 'get_quality_event_data_context' in selected
        assert ('execute_quality_transaction' in selected) is writes
    assert 'include_date_storage=true' in ROUTING_INSTRUCTION and '不能当成Event.OccurrenceDate/ReportedDate' in ROUTING_INSTRUCTION


def test_capabilities_and_inventory_disclose_storage_precision_dates_and_no_mes_timezone_inference():
    cap=result(get_capabilities(tool_name='prepare_quality_event_data_update'))
    text=json.dumps(cap,ensure_ascii=False)
    assert 'date_received' in text and 'report_date' in text and 'datetime2' in text
    assert 'date_received' in json.dumps(result(e.q.get_quality_api_inventory()))
