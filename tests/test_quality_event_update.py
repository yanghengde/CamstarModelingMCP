import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from tools import mcp,get_tool_func,quality_event_update as u,quality_event_triage as t,quality_plans as p
from tests.test_quality_event_triage import env as triage_env,EVENT,ORG,OWNER,ROLE,PRIORITY,OTHER,NCR,CONT,echo
from core.tool_catalog import TOOL_POLICIES
from agent.safety import evaluate_mutations,MutationCounts,plan_preview
from agent.tool_routing import select_tools,ROUTING_INSTRUCTION

def result(coro):return json.loads(asyncio.run(coro))


def schema_fixture():
    # The archived UpdateEvent root plus the archived framework PerformEvent
    # envelope form a test response, not a claimed live UpdateEvent schema read.
    root=json.loads(Path('docs/quality-fixtures/contracts.json').read_text(encoding='utf-8'))['Shopfloor/UpdateEvent']
    data=copy.deepcopy(root)
    schemas=data['components']['schemas']
    common=json.loads(Path('docs/quality-fixtures/RecordSPCViolation-schema.json').read_text(encoding='utf-8'))['components']['schemas']
    schemas['EventToPerform']=copy.deepcopy(common['EventToPerform'])
    schemas['UpdateEventShopfloorPerformEventModel']={'type':'object','properties':{
        'event':{'$ref':'#/components/schemas/EventToPerform'},'inputData':{'$ref':'#/components/schemas/UpdateEvent'}}}
    data['paths']['/api/UpdateEvent/PerformEvent']={'post':{'requestBody':{'content':{'application/json':{'schema':{'$ref':'#/components/schemas/UpdateEventShopfloorPerformEventModel'}}}}}}
    return data


@pytest.fixture
def env(triage_env,monkeypatch):
    state=triage_env
    state.update(extra=dict(BriefDescription='Original title',Description='Original description',DiscoveryArea='Line A',EventDataId=None,
        InitiatorId=OWNER,InitiatorOrganizationId=ORG,ReporterId=OTHER,ReporterOrganizationId=ORG,
        OccurrenceDate='2026-10-29 10:00:00.1234567',OccurrenceDateGMT='2026-10-29 02:00:00.1234567',
        ReportedDate='2026-10-29 11:00:00',ReportedDateGMT='2026-10-29 03:00:00'),
        regulatory=[],load_hook=None,update_hook=None,load_count=0,extra_count=None,contract=schema_fixture(),schema_bad=False)
    base_sql=t.q.sql_rows
    async def sql(query,maximum):
        if 'INFORMATION_SCHEMA.COLUMNS' in query and "TABLE_NAME='RegulatoryReport'" in query:
            rows=[dict(TABLE_NAME=table,COLUMN_NAME=col) for table,cols in u.SCHEMA.items() for col in cols.split(',')]
            return rows[:-1] if state['schema_bad'] else rows
        if query.startswith('SELECT TOP 1 EventId,ChangeCount,'):
            return [dict(EventId=EVENT,ChangeCount=state['event']['ChangeCount'] if state['extra_count'] is None else state['extra_count'],**copy.deepcopy(state['extra']))]
        if ' FROM RegulatoryReport WHERE ' in query:return copy.deepcopy(state['regulatory'])
        return await base_sql(query,maximum)
    monkeypatch.setattr(u.q,'sql_rows',AsyncMock(side_effect=sql))
    def current_detail():
        detail={k:copy.deepcopy(state['extra'][k]) for k in ('BriefDescription','Description','DiscoveryArea')}
        detail.update(CDOTypeId='7541',EventDataDetail=None,RegulatoryReportDetails=[],InstanceId=f'{500+state["load_count"]:016x}',Parent={'InstanceId':OTHER})
        for field in u.REF_FIELDS:
            key=state['event']['PriorityLevelId'] if field=='PriorityLevel' else state['extra'][field+'Id']
            detail[field]=None if key is None else {'Name':'Original '+field,'InstanceId':key}
        detail.update(OccurrenceDate='2026-10-29T10:00:00.1234567+08:00',OccurrenceDateGMT='2026-10-29T02:00:00.1234567Z',
                      ReportedDate='2026-10-29T11:00:00+08:00',ReportedDateGMT='2026-10-29T03:00:00Z')
        return detail
    def frame(body,detail):
        row=state['event'];data=echo(copy.deepcopy(body))
        data.update(AvailableActions=3,IgnoreProcessModel=False,ESigRequirement=None,AdditionalESigReqs=[],
                    ApprovalSheetDetails=None,ApprovalSheetTemplate=None,AssignedLots=[],QualityCrossRefDetails=[],QualityCrossReferences=[],ServiceDetail=detail)
        data['QualityObjectDetail']=dict(Category=row['Category'],Status=row['Status'],Organization={'InstanceId':ORG},Owner={'InstanceId':OWNER},
            Role={'InstanceId':ROLE},PriorityLevel={'InstanceId':PRIORITY},ProcessModel=None,TriageComplete=row['TriageComplete'],
            IsCARRequiredToClose=row['IsCARRequiredToClose'],IsApprovalRequired=False,CompletionApprovalSheet=None,
            CompletionApprovalStatus=None,EntryForApproval=None,UserHasRole=True,UserIsOwner=True)
        return data
    async def request(method,path,**kw):
        if method=='GET':
            assert path=='/swagger/UpdateEvent/swagger.json'
            return json.dumps(state['contract'])
        if path.endswith('/PerformEvent'):
            assert kw['body']['event']=={'eventName':'Load'} and set(kw['body'])=={'event','inputData'}
            assert 'execute' not in kw['params'] # No invented execute=false semantics for PerformEvent.
            state['load_count']+=1
            data=frame(kw['body']['inputData'],current_detail())
            if state['load_hook']:state['load_hook'](data)
            return json.dumps({'data':data})
        assert (method,path)==('POST','/api/UpdateEvent') and kw['params']['execute']=='false'
        detail=echo(copy.deepcopy(kw['body']['serviceDetail']));detail.update(CDOTypeId=7541,InstanceId=OTHER,Parent={'InstanceId':OTHER})
        data=frame(kw['body'],detail)
        if state['update_hook']:state['update_hook'](data)
        return json.dumps(data)
    monkeypatch.setattr(u,'request',AsyncMock(side_effect=request))
    return state


def prepare(**kw):return result(u.prepare_quality_event_update('E','Org',**({'description':'Changed description'}|kw)))
def snap(ready):return result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))


def test_full_detail_is_loaded_preserved_and_only_explicit_text_changed(env):
    ready=prepare();assert ready['state']=='ready',ready
    body=ready['request_preview']['body'];detail=body['serviceDetail']
    assert body['submit'] is False and set(body)=={'qualityObject','organization','submit','comments','serviceDetail'}
    assert detail['description']=='Changed description' and detail['briefDescription']=='Original title' and detail['discoveryArea']=='Line A'
    assert detail['reporter']==dict(name='Original Reporter',instanceId=OTHER)
    assert detail['occurrenceDate']=='2026-10-29T10:00:00.1234567+08:00' and detail['occurrenceDateGMT']=='2026-10-29T02:00:00.1234567Z'
    assert detail['priorityLevel']['instanceId']==PRIORITY and detail['regulatoryReportDetails']==[]
    assert not {'instanceID','parent','historyDetail','smartBarcode'}&set(detail)
    assert env['load_count']==2 and not ready['server_context']['event_updated_verified']
    assert 'SHOULD_NEVER_BE_STORED' not in json.dumps(snap(ready));env['client'].request.assert_not_awaited()


def test_context_load_is_readonly_and_does_not_create_plan(env):
    out=result(u.get_quality_event_update_context('E','Org'))
    assert out['complete'] and not out['ready_for_execution'] and out['original_detail']['reporter']['instanceId']==OTHER
    assert env['load_count']==1 and not p.PLAN_DB.exists();env['client'].request.assert_not_awaited()


def test_update_does_not_reset_triage_and_uses_its_own_signature_policy(env):
    env['event']['TriageComplete']=True
    env['roots']['Organizations']['QualityESigTxns']=[7788]
    ready=prepare();assert ready['state']=='ready'
    assert ready['server_context']['current_quality_context']['TriageComplete'] is True
    env['roots']['Organizations']['QualityESigTxns']=[7578]
    assert 'error' in prepare()


def test_explicit_clear_and_null_omission_are_different(env):
    ready=prepare(description='',discovery_area='',brief_description=None)
    detail=ready['request_preview']['body']['serviceDetail']
    assert detail['description']=='' and detail['discoveryArea']=='' and detail['briefDescription']=='Original title'
    out=result(u.prepare_quality_event_update('E','Org'))
    assert 'error' in out
    assert 'error' in prepare(description='Original description')


def test_nullable_original_values_are_preserved_without_actor_or_time_defaults(env):
    for key in ('Description','DiscoveryArea','InitiatorId','InitiatorOrganizationId',*u.DATE_FIELDS):env['extra'][key]=None
    def no_defaults(data):
        for key in u.DATE_FIELDS:data['ServiceDetail'][key]=None
    env['load_hook']=no_defaults
    ready=prepare(description=None,brief_description='Changed title')
    assert ready['state']=='ready',ready
    detail=ready['request_preview']['body']['serviceDetail']
    assert all(detail[k] is None for k in ('description','discoveryArea','initiator','initiatorOrganization','occurrenceDate','reportedDateGMT'))
    assert detail['reporter']['instanceId']==OTHER


@pytest.mark.parametrize('raw',['null','[]','bad json',json.dumps({'data':{},'@odata.nextLink':'more'}),json.dumps({'data':{},'ErrorCode':'failure'})])
def test_incomplete_or_failed_native_envelopes_are_rejected(raw):
    with pytest.raises(ValueError):u.parse_reply(raw)


@pytest.mark.parametrize('argument,value',[('brief_description',' '),('brief_description','a'*101),('description','a'*4001),
    ('discovery_area','a'*31),('comments','a'*201),('description','a\x00b'),('brief_description','😀'*51)])
def test_invalid_text_fails_before_any_query_or_truncation(env,argument,value):
    assert 'error' in prepare(**{argument:value});u.q.sql_rows.assert_not_awaited();u.request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('Status',5),('IsSubmitted',False),('IsFrozen',True),('ProcessModelId',OTHER),
    ('OrigProcessModelTemplateId',OTHER),('ChecklistId',OTHER),('ChecklistSaved',True),('ReporterId',None),('EventDataId',OTHER)])
def test_complex_or_uneditable_event_is_not_updated(env,field,value):
    if field in env['extra']:env['extra'][field]=value
    else:env['event'][field]=value
    assert 'error' in prepare();u.request.assert_not_awaited()


@pytest.mark.parametrize('mode',['schema','approval','regulatory','extra_version','extra_field'])
def test_incomplete_schema_approvals_or_regulatory_relations_fail(env,mode):
    if mode=='schema':env['schema_bad']=True
    elif mode=='approval':env['approvals']=[{'ApprovalSheetId':OTHER}]
    elif mode=='regulatory':env['regulatory']=[{'RegulatoryReportId':OTHER}]
    elif mode=='extra_version':env['extra_count']=2
    else:env['extra'].pop('Description')
    # Missing source fields must be represented as an incomplete real response.
    if mode=='extra_field':
        base=u.q.sql_rows
        async def sql(query,maximum):
            if query.startswith('SELECT TOP 1 EventId,ChangeCount,'):return [{'EventId':EVENT,'ChangeCount':1}]
            return await base(query,maximum)
        from unittest.mock import patch
        with patch.object(u.q,'sql_rows',AsyncMock(side_effect=sql)):assert 'error' in prepare()
    else:assert 'error' in prepare()
    u.request.assert_not_awaited()


@pytest.mark.parametrize('mode',['no_path','wrong_model','no_event_name','missing_detail_field','extra_detail_field','wrong_submit'])
def test_runtime_perform_event_contract_is_verified_not_guessed(env,mode):
    doc=env['contract'];schemas=doc['components']['schemas']
    if mode=='no_path':doc['paths'].pop('/api/UpdateEvent/PerformEvent')
    elif mode=='wrong_model':schemas['UpdateEventShopfloorPerformEventModel']['properties']['inputData']['$ref']='#/components/schemas/Other'
    elif mode=='no_event_name':schemas['EventToPerform']['properties'].pop('eventName')
    elif mode=='missing_detail_field':schemas['EventDetail']['properties'].pop('reportedDateGMT')
    elif mode=='extra_detail_field':schemas['EventDetail']['properties']['customDisposition']={'type':'string'}
    else:schemas['UpdateEvent']['properties']['submit']['type']='string'
    assert 'error' in prepare();assert env['load_count']==0


@pytest.mark.parametrize('field,value',[('BriefDescription','Wrong'),('Reporter',{'Name':'Current User','InstanceId':OWNER}),
    ('PriorityLevel',{'Name':'Other','InstanceId':OTHER}),('EventDataDetail',{}),('RegulatoryReportDetails',[{}]),
    ('OccurrenceDateGMT','2026-10-29T02:00:00.1234568Z'),('OccurrenceDate','2026-10-29T10:00:00.1234567+07:00'),
    ('ReportedDateGMT',None),('CDOTypeId',7734)])
def test_native_load_cannot_derive_defaults_or_change_reporters_dates_or_type(env,field,value):
    env['load_hook']=lambda data:data['ServiceDetail'].update({field:value})
    assert 'error' in prepare();assert not p.PLAN_DB.exists()


@pytest.mark.parametrize('field,value',[('AvailableActions',1),('IgnoreProcessModel',True),('Submit',True),
    ('ApprovalSheetDetails',{}),('ApprovalSheetTemplate',{}),('ESigRequirement',{}),('AdditionalESigReqs',[{}]),
    ('AssignedLots',[{}]),('QualityCrossReferences',[{}]),('QualityCrossRefDetails',None)])
def test_load_rejects_unknown_actions_signatures_and_complex_list_defaults(env,field,value):
    env['load_hook']=lambda data:data.update({field:value})
    assert 'error' in prepare()


@pytest.mark.parametrize('field,value',[('UserIsOwner',False),('UserHasRole',False),('IsApprovalRequired',True),
    ('TriageComplete',True),('PriorityLevel',{'InstanceId':OTHER}),('CompletionApprovalSheet',{}),('Status',7)])
def test_current_native_status_and_owner_context_must_match(env,field,value):
    env['load_hook']=lambda data:data['QualityObjectDetail'].update({field:value})
    assert 'error' in prepare()


def test_only_static_patch_and_second_independent_load_must_remain_unchanged(env):
    def dangerous_preview(data):raise AssertionError('Submit=false ordinary preview could execute')
    env['update_hook']=dangerous_preview
    ready=prepare();assert ready.get('state')=='ready',ready
    assert not ready['static_and_echo_verified'] and ready['server_context']['static_preview_only']
    assert all(call.args[0]=='GET' or call.args[1].endswith('/PerformEvent') for call in u.request.await_args_list)
    def changed(data):
        if env['load_count']>=2:data['ServiceDetail']['Reporter']['Name']='Changed actor name'
    env['load_count']=0;env['load_hook']=changed
    assert 'error' in prepare()


@pytest.mark.parametrize('what',['event','source_text','owner','signature','approval','ncr','loaded_reference'])
def test_changes_after_ready_stop_the_single_business_submission(env,what):
    ready=prepare()
    if what=='event':env['event']['ChangeCount']=2
    elif what=='source_text':env['extra']['Description']='Changed elsewhere'
    elif what=='owner':env['counters'][OWNER]=2
    elif what=='signature':env['roots']['Organizations']['QualityESigTxns']=[7578]
    elif what=='approval':env['approvals']=[{'ApprovalSheetId':OTHER}]
    elif what=='ncr':env['links']=[dict(NonconformanceReportId=NCR,EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1)]
    else:env['load_hook']=lambda data:data['ServiceDetail']['Reporter'].update(Name='Renamed')
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_single_commit_unknown_conflicts_with_triage_and_ncr_container(env):
    env['links']=[dict(NonconformanceReportId=NCR,EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1)]
    first=prepare();second=prepare();saved=snap(first)
    triage=dict(kind='event_triage',target_id=EVENT,scope_key='quality-event:id:'+EVENT,environment=p.environment(),body={},before={},resource_writes=['runtime:quality-event:id:'+EVENT])
    assert p.conflicts(saved,triage)
    assert p.conflicts(saved,dict(kind='ncr',target_id=NCR,scope_key='ncr:N',environment=p.environment(),body={},before={'ContainerId':CONT}))
    assert execute(first)['state']=='unknown' and 'error' in execute(first) and 'error' in execute(second)
    env['client'].request.assert_awaited_once()
    call=env['client'].request.await_args
    assert call.args[1].endswith('/api/UpdateEvent') and call.kwargs['params']=={'execute':'true'}
    assert call.kwargs['json']['submit'] is False and not call.kwargs['follow_redirects']


@pytest.mark.parametrize('field',['reporter','initiator','priorityLevel','occurrenceDateGMT','regulatoryReportDetails','eventDataDetail','smartBarcode'])
def test_transport_does_not_allow_nontext_detail_overrides(env,field):
    saved=snap(prepare());saved['body']['serviceDetail'][field]=True
    with pytest.raises(ValueError):p.transport(saved)


def test_transport_cannot_submit_approve_reroute_or_switch_service(env):
    for field in ('submit','ignoreProcessModel','approvalSheetDetails','assignedLots'):
        saved=snap(prepare());saved['body'][field]=True
        with pytest.raises(ValueError):p.transport(saved)
    saved=snap(prepare());saved['service']='UpdateEventDetails'
    with pytest.raises(ValueError):p.transport(saved)


@pytest.mark.parametrize('mode',['timeout','cancel','http400'])
def test_attempt_outcomes_remain_unknown_and_never_replay(env,mode):
    ready=prepare()
    if mode=='http400':env['client'].request.return_value.status_code=400
    else:env['client'].request.side_effect=TimeoutError('uncertain') if mode=='timeout' else asyncio.CancelledError()
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:assert execute(ready)['state']=='unknown'
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'
    assert 'error' in execute(ready);env['client'].request.assert_awaited_once()


def test_inspect_only_original_id_even_after_rename_and_text_match(env):
    ready=prepare();execute(ready);env['event']['EventName']='Renamed';env['extra']['Description']='Changed description'
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['original_event_id']==EVENT and out['current_event']['Description']=='Changed description'
    assert not out['verified_success'] and not out['state_changed'] and not out['retry_allowed']
    assert t.q.get_quality_history.await_args.args==('quality_object','Renamed',EVENT)


def test_mcp_real_schema_closed_and_system_confirmation(env):
    async def run():
        assert get_tool_func('prepare_quality_event_update') is u.prepare_quality_event_update
        async with Client(mcp) as client:
            schema=next(t.input_schema for t in await client.list_tools() if t.name=='prepare_quality_event_update')
            assert schema['additionalProperties'] is False and not {'reporter','priority','submit','serviceDetail'}&set(schema['properties'])
            reply=await client.call_tool('prepare_quality_event_update',dict(event_name='E',organization_name='Org',description='New'))
            ready=json.loads(reply.content[0].text);assert ready['state']=='ready',ready
            calls=[dict(function=dict(name='execute_quality_transaction',arguments=json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})))]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval and 'UpdateEvent' in plan_preview(calls)
    asyncio.run(run());env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('turns,write',[
    (['查看质量事件E文字维护的前置条件'],False),
    (['请把事件E的说明更新为已复测'],True),
    (['把发现区域改为二线'],True),
    (['查看事件E的简述','继续'],False),
    (['查看事件E的简述','提交这个计划'],True),
    (['查看质量事件文字维护','把说明清空'],True),
    (['update event description'],True),
    (['查看质量事件文字维护','查看如何清空说明'],False),
])
def test_event_text_questions_and_followups_select_quality_tools(turns,write):
    tools=[{'function':{'name':n}} for n in TOOL_POLICIES];messages=[]
    for text in turns:messages.extend([dict(role='user',content=text),dict(role='assistant',content='routing fixture only')])
    chosen={row['function']['name'] for row in select_tools(tools,messages)}
    assert {'get_quality_event_update_context','prepare_quality_event_update'}<=chosen
    assert ('execute_quality_transaction' in chosen) is write
    assert '省略/null保留原值' in ROUTING_INSTRUCTION and '不根据含糊的完善资料补写报告人' in ROUTING_INSTRUCTION


def test_native_columns_and_event_detail_model_are_primary_contract_evidence():
    cols={}
    for f in ('event-triage-native-columns.json','event-triage-native-category-map.json','event-update-native-links.json'):
        for x in json.loads((Path('docs/quality-fixtures')/f).read_text(encoding='utf-8'))['results']:
            if x['query']['kind']=='column':cols[x['query']['owner']]={r['name'] for r in x['result'].get('records',[])}
    for x in json.loads(Path('docs/quality-fixtures/event-triage-native-links.json').read_text(encoding='utf-8'))['metadata']['results']:
        if x['query']['kind']=='column':cols[x['query']['owner']]={r['name'] for r in x['result'].get('records',[])}
    for r in json.loads(Path('docs/quality-fixtures/table-columns.json').read_text(encoding='utf-8')):cols.setdefault(r['TABLE_NAME'],set()).add(r['COLUMN_NAME'])
    assert all(set(v.split(','))<=cols[k] for k,v in u.SCHEMA.items())
    default=json.loads(Path('docs/quality-fixtures/event-update-native-defaults.json').read_text(encoding='utf-8'))['results']
    detail=next(x['result'] for x in default if x['query']['kind']=='cdo')
    assert detail['CDODefId']==u.DETAIL_TYPE
