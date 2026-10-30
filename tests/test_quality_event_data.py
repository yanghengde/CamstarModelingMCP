"""Explicit isolated native EventData transitions; no MES writes or custom fields."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from fastmcp import Client
from pydantic import ValidationError
from tools import mcp,get_tool_func,quality_event_data as e,quality_event_update as u,quality_plans as p
from tests.test_quality_event_drafts import env as draft_env,submission_env,update_env,triage_env,TOKEN,result,execute,snap
from tests.test_quality_event_triage import EVENT,ORG,OWNER,ROLE,PRIORITY,OTHER,echo
from tests.test_quality_event_update import schema_fixture
from tests.test_quality_event_submission import final_fixture_review
from agent.safety import evaluate_mutations,MutationCounts,plan_preview
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools,ROUTING_INSTRUCTION
from tools.tool_discovery import get_capabilities

DATA='000000000000f001';HISTORY='000000000000f002';TXN='000000000000f003';MAIN='000000000000f004'


def contract_fixture():
    doc=schema_fixture();schemas=doc['components']['schemas'];old=schemas['UpdateEvent']['properties'];detail=schemas['EventDetail']['properties']
    props={k:copy.deepcopy(v) for k,v in old.items() if k not in ('serviceDetail','submit')}
    for field in e.ROOT_FIELDS:props[e.wire(field)]=copy.deepcopy(detail[e.wire(field)])
    props.update(eventDataDetail={'$ref':'#/components/schemas/EventDataDetails'},disallowedTxns={'type':'array','items':{'type':'integer'}},
        regulatoryReportDetails=copy.deepcopy(detail['regulatoryReportDetails']))
    schemas['UpdateEventData']={'type':'object','properties':props}
    schemas['UpdateEventDataPerformEventModel']={'properties':{'event':{'$ref':'#/components/schemas/EventToPerform'},'inputData':{'$ref':'#/components/schemas/UpdateEventData'}}}
    doc['paths']={
        '/api/UpdateEventData':{'post':{'requestBody':{'content':{'application/json':{'schema':{'$ref':'#/components/schemas/UpdateEventData'}}}}}},
        '/api/UpdateEventData/PerformEvent':{'post':{'requestBody':{'content':{'application/json':{'schema':{'$ref':'#/components/schemas/UpdateEventDataPerformEventModel'}}}}}}}
    return doc


def prepare(**kw):
    args=dict(event_name='E',organization_name='Org',changes=e.EventDataTextChanges(problem_description='Reviewed problem'),comments='Reviewed data edit')|kw
    if isinstance(args['changes'],dict):args['changes']=e.EventDataTextChanges(**args['changes'])
    return result(e.prepare_quality_event_data_update(**args))


@pytest.fixture
def env(draft_env,monkeypatch):
    s=draft_env;s['event'].update(Status=1,IsSubmitted=True,TriageComplete=True)
    s['extra']['EventDataId']=DATA
    data={f:None for f in e.DATA_TEXT};data.update({f:None for f in e.DATA_DATES})
    data.update({f+'Id':None for f in e.DATA_REFS});data.update({f:None for f in e.COMPLEX_REFS})
    data.update(EventDataId=DATA,EventId=EVENT,CDOTypeId=8085,ChangeCount=1,ProblemDescription='Original problem',DeviceLocation='Station A',ProductName='P',RMANumber='RMA1',
        DateReceived='2026-10-29 09:00:00.1234567',DateReceivedGMT='2026-10-29 01:00:00.1234567')
    s.update(data=data,data_contract=contract_fixture(),data_hook=None,data_history=[],data_posts=[],data_mode=None,data_loads=0,disallowed=[],complex_tables={},data_sql_hook=None)
    original=e.q.sql_rows
    async def sql(query,maximum):
        if ' FROM EventData WHERE ' in query:
            rows=[copy.deepcopy(s['data'])] if DATA in query else []
            if s['data_sql_hook']:s['data_sql_hook'](rows)
            return rows
        if 'FROM EventDisallowedTxns WHERE ' in query:return copy.deepcopy(s['disallowed'])
        if 'FROM UpdateEventDetailsHistory s JOIN HistoryMainline h' in query:return copy.deepcopy(s['data_history'])
        for table in e.BLOCK_LISTS:
            if ' FROM '+table+' WHERE ' in query:return copy.deepcopy(s['complex_tables'].get(table,[]))
        return await original(query,maximum)
    monkeypatch.setattr(e.q,'sql_rows',AsyncMock(side_effect=sql))
    def date(value,gmt):return None if value is None else value.replace(' ','T')+('Z' if gmt else '+08:00')
    def frame(body):
        out=echo(copy.deepcopy(body));row=s['event']
        out.update(Employee={'InstanceId':s['actor']},AvailableActions=3,IgnoreProcessModel=False,ESigRequirement=None,AdditionalESigReqs=[],ESigDetails=[],
            QualityESigDetail=None,DataCollectionDef=None,ParametricData=None,RegulatoryReportDetails=[],DisallowedTxns=[r['DisallowedTxns'] for r in s['disallowed']])
        out['QualityObjectDetail']=dict(Category=1,Status=row['Status'],Organization={'InstanceId':ORG},Owner={'InstanceId':OWNER},Role={'InstanceId':ROLE},PriorityLevel={'InstanceId':PRIORITY},
            ProcessModel=None,TriageComplete=row['TriageComplete'],IsCARRequiredToClose=row['IsCARRequiredToClose'],IsApprovalRequired=False,CompletionApprovalSheet=None,
            CompletionApprovalStatus=None,EntryForApproval=None,UserHasRole=True,UserIsOwner=True)
        for field in ('BriefDescription','Description','DiscoveryArea'):out[field]=s['extra'][field]
        for field in u.REF_FIELDS:
            key=row['PriorityLevelId'] if field=='PriorityLevel' else s['extra'][field+'Id']
            out[field]=None if key is None else dict(InstanceId=key,Name='Original '+field)
        for field in u.DATE_FIELDS:out[field]=date(s['extra'][field],field.endswith('GMT'))
        for col,field in [('ClassificationId','Classification'),('SubClassificationId','SubClassification')]:
            key=s['classes'][col];out[field]=None if key is None else dict(InstanceId=key)
        dt={f:copy.deepcopy(s['data'][f]) for f in e.DATA_TEXT};dt.update(CDOTypeId=8087,InstanceId=f'{s["data_loads"]+700:016x}',Parent={'InstanceId':OTHER},EventData=None)
        for field in e.DATA_DATES:dt[field]=date(s['data'][field],field.endswith('GMT'))
        for field in e.DATA_REFS:
            key=s['data'][field+'Id'];dt[field]=None if key is None else dict(InstanceId=key,Name='Original '+field)
        out['EventDataDetail']=dt
        return out
    async def request(method,path,**kw):
        if method=='GET':
            assert path=='/swagger/UpdateEventData/swagger.json'
            return json.dumps(s['data_contract'])
        assert (method,path)==('POST','/api/UpdateEventData/PerformEvent'), 'Never ordinary POST fake preview'
        assert kw['body']['event']=={'eventName':'Load'} and set(kw['body'])=={'event','inputData'} and 'execute' not in kw['params']
        assert kw['bound_headers']['Authorization']==TOKEN and kw['require_stable_session']
        s['data_loads']+=1;data=frame(kw['body']['inputData'])
        if s['data_hook']:s['data_hook'](data)
        return json.dumps({'data':data})
    monkeypatch.setattr(e,'request',AsyncMock(side_effect=request))
    async def submit(method,url,**kw):
        assert (method,url.rsplit('/',1)[-1])==('POST','UpdateEventData') and kw['params']=={'execute':'true'} and kw['follow_redirects'] is False
        assert kw['headers']['Authorization']==TOKEN and kw['headers']['Cookie']==''
        s['data_posts'].append((url,copy.deepcopy(kw['json']),dict(kw['headers'])))
        if s['data_mode']=='timeout':raise TimeoutError(TOKEN)
        if s['data_mode']=='cancel':raise asyncio.CancelledError()
        if s['data_mode']=='400':return SimpleNamespace(status_code=400,text=TOKEN)
        for name in ('ProblemDescription','DeviceLocation'):s['data'][name]=kw['json']['eventDataDetail'][e.wire(name)]
        s['data']['ChangeCount']+=1;s['event']['ChangeCount']+=1;s['classes']['ChangeCount']=s['event']['ChangeCount']
        s['data_history']=[dict(UpdateEventDetailsHistoryId=HISTORY,CDOTypeId=8752,HistoryId=EVENT,HistoryMainlineId=MAIN,TxnId=TXN,ChangeCount=1,TxnType=8764,Comments=kw['json']['comments'],ReversalStatus=0)]
        return SimpleNamespace(status_code=200,text=TOKEN)
    s['client'].request=AsyncMock(side_effect=submit)
    return s


def test_context_then_single_edit_preserves_all_original_maps_and_human_review_allows_next_edit(env):
    before_data=copy.deepcopy(env['data']);before_event=copy.deepcopy(env['event']);context=result(e.get_quality_event_data_context('E','Org'))
    assert context['complete'] and not context['ready_for_execution'] and not p.PLAN_DB.exists(),context
    ready=prepare(changes=dict(problem_description='Reviewed issue',device_location='Station B'));assert ready.get('state')=='ready',ready
    saved=snap(ready);body=ready['request_preview']['body'];detail=body['eventDataDetail']
    assert not ready['static_and_echo_verified'] and saved['preview']['static_preview_only']
    assert detail==saved['original_values']['data']|dict(problemDescription='Reviewed issue',deviceLocation='Station B')
    assert body['reporter']['instanceId']==env['extra']['ReporterId'] and body['reportedDateGMT']=='2026-10-29T03:00:00Z'
    assert detail['dateReceivedGMT']=='2026-10-29T01:00:00.1234567Z' and detail['rmaNumber']=='RMA1'
    assert not {'submit','employee','status','classification','owner','qualityESigDetail','eventCustomDetail'}&body.keys()
    assert not env['data_posts'] and execute(ready)['state']=='unknown' and len(env['data_posts'])==1
    for key,value in before_data.items():
        if key not in ('ChangeCount','ProblemDescription','DeviceLocation'):assert env['data'][key]==value
    for key,value in before_event.items():
        if key!='ChangeCount':assert env['event'][key]==value
    proof=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert not proof['verified_success'] and proof['history_candidates'][0]['CDOTypeId']==8752 and proof['original_event_data_id']==DATA
    next_plan=prepare(changes=dict(device_location='Station C'));assert next_plan.get('state')=='ready',next_plan
    assert 'error' in execute(next_plan) and len(env['data_posts'])==1
    final_fixture_review(ready)
    assert execute(next_plan)['state']=='unknown' and len(env['data_posts'])==2
    persisted=result(p.get_quality_transaction_plan(next_plan['plan_id']))
    assert TOKEN not in json.dumps(persisted) and TOKEN.encode() not in p.PLAN_DB.read_bytes() and persisted['outcome']['raw_response'] is None


def test_preserves_nullable_values_refs_and_nonempty_disallowed_list_without_defaults(env):
    env['data']['OccupationId']=OTHER;env['data']['CompensateCustomerActionId']=OTHER
    env['disallowed']=[dict(EventId=EVENT,FieldId=25978,Sequence=0,DisallowedTxns=7712)]
    ready=prepare(changes=dict(problem_description='',device_location=None));assert ready.get('state')=='ready',ready
    body=ready['request_preview']['body'];assert body['disallowedTxns']==[7712]
    assert body['eventDataDetail']['occupation']['instanceId']==OTHER and body['eventDataDetail']['reportDate'] is None
    assert body['eventDataDetail']['problemDescription']=='' and body['eventDataDetail']['deviceLocation']=='Station A'


@pytest.mark.parametrize('changes',[{},dict(problem_description=None),dict(problem_description='Original problem'),dict(problem_description='x'*256),
    dict(problem_description='😀'*128),dict(device_location='x'*31),dict(device_location='x\x00y')])
def test_invalid_or_noop_text_is_rejected_without_business_attempt(env,changes):
    try:out=prepare(changes=changes)
    except ValidationError:out={'error':'closed typed patch rejected'}
    assert 'error' in out and not env['data_posts']


@pytest.mark.parametrize('field,value',[('Status',2),('Category',3),('CDOTypeId',7734),('IsSubmitted',False),('IsFrozen',True),('ProcessModelId',OTHER),
    ('ChecklistSaved',True),('CloseDateGMT','2026-10-30'),('OwnerId',None)])
def test_only_existing_plain_active_submitted_event_can_be_edited(env,field,value):
    env['event'][field]=value
    assert 'error' in prepare() and not env['data_posts']


@pytest.mark.parametrize('mode',['data_subtype','data_parent','custom_data','contact','ncr_number','ncr_comment','lots','failures','logs','devices','actions',
    'approval','regulatory','ncr_link','signature_policy','data_missing_field','data_version','disallowed','duplicate_ban','wrong_ban_field','too_many_bans'])
def test_complex_unknown_or_side_effecting_context_is_not_silently_overwritten(env,mode):
    if mode=='data_subtype':env['data']['CDOTypeId']=8163
    elif mode=='data_parent':env['data']['EventId']=OTHER
    elif mode=='custom_data':env['data']['EventCustomDataId']=OTHER
    elif mode=='contact':env['data']['ContactCustomerId']=OTHER
    elif mode=='ncr_number':env['data']['NonconformanceNumber']='NCR1'
    elif mode=='ncr_comment':env['data']['CorrectiveActionCommentsId']=OTHER
    elif mode in ('lots','failures','logs','devices','actions'):
        table={'lots':'EventLot','failures':'EventFailure','logs':'EventLog','devices':'SuspectDeviceData','actions':'EventFailureAction'}[mode]
        env['complex_tables'][table]=[dict.fromkeys(e.BLOCK_LISTS[table].split(','),OTHER)]
    elif mode=='approval':env['approvals']=[{'ApprovalSheetId':OTHER}]
    elif mode=='regulatory':env['regulatory']=[{'RegulatoryReportId':OTHER}]
    elif mode=='ncr_link':env['links']=[{'NonconformanceReportId':OTHER}]
    elif mode=='signature_policy':env['roots']['Organizations']['QualityESigTxns']=[8764]
    elif mode=='data_missing_field':env['data_sql_hook']=lambda rows:rows[0].pop('WorkflowStepName')
    elif mode=='data_version':env['data']['ChangeCount']=-1
    else:
        env['disallowed']=[dict(EventId=EVENT,FieldId=25978,Sequence=0,DisallowedTxns=8764 if mode=='disallowed' else 7712)]
        if mode=='duplicate_ban':env['disallowed']*=2
        elif mode=='wrong_ban_field':env['disallowed'][0]['FieldId']=1
        elif mode=='too_many_bans':env['disallowed']*=101
    assert 'error' in prepare() and not env['data_posts']


@pytest.mark.parametrize('mode',['employee','edit','negative_actions','role','owner','process_bypass','signature','signature_details','data_collection','parametric','classification',
    'root_text','root_time','mapped_text','mapped_ref','mapped_date','data_identity','detail_type','extra_field','banned_list'])
def test_fixed_load_must_preserve_native_identity_authorization_and_exact_original_values(env,mode):
    def hook(data):
        if mode=='employee':data['Employee']={'InstanceId':OTHER}
        elif mode=='edit':data['AvailableActions']=1
        elif mode=='negative_actions':data['AvailableActions']=-1
        elif mode=='role':data['QualityObjectDetail']['UserHasRole']=False
        elif mode=='owner':data['QualityObjectDetail']['UserIsOwner']=False
        elif mode=='process_bypass':data['IgnoreProcessModel']=True
        elif mode=='signature':data['ESigRequirement']={}
        elif mode=='signature_details':data['ESigDetails']=[{}]
        elif mode=='data_collection':data['DataCollectionDef']={'InstanceId':OTHER}
        elif mode=='parametric':data['ParametricData']={}
        elif mode=='classification':data['Classification']={'InstanceId':OTHER}
        elif mode=='root_text':data['Description']='derived text'
        elif mode=='root_time':data['ReportedDateGMT']='2026-10-29T03:00:01Z'
        elif mode=='mapped_text':data['EventDataDetail']['ProductName']='derived product'
        elif mode=='mapped_ref':data['EventDataDetail']['Occupation']={'InstanceId':OTHER,'Name':'Default'}
        elif mode=='mapped_date':data['EventDataDetail']['DateReceivedGMT']='2026-10-29T01:00:00.1234568Z'
        elif mode=='data_identity':data['EventDataDetail']['EventData']={'InstanceId':OTHER}
        elif mode=='detail_type':data['EventDataDetail']['CDOTypeId']=8166
        elif mode=='extra_field':data['EventDataDetail']['UnverifiedBusinessValue']='custom'
        else:data['DisallowedTxns']=[8764]
    env['data_hook']=hook
    assert 'error' in prepare() and not env['data_posts']


@pytest.mark.parametrize('mode',['unknown_extension','no_load','wrong_model','wrong_ref','root_service_detail','wrong_date','wrong_mutation_model'])
def test_runtime_schema_needs_actual_fixed_load_and_safe_standard_mapping_shape(env,mode):
    doc=env['data_contract'];schemas=doc['components']['schemas']
    if mode=='unknown_extension':schemas['EventDataDetails']['properties']['siteCustomField']={'type':'string'}
    elif mode=='no_load':doc['paths'].pop('/api/UpdateEventData/PerformEvent')
    elif mode=='wrong_model':schemas['UpdateEventDataPerformEventModel']['properties']['inputData']['$ref']='#/components/schemas/UpdateEvent'
    elif mode=='wrong_ref':schemas['EventDataDetails']['properties']['occupation']['$ref']='#/components/schemas/SubentityRef'
    elif mode=='root_service_detail':schemas['UpdateEventData']['properties']['serviceDetail']={'type':'object'}
    elif mode=='wrong_date':schemas['EventDataDetails']['properties']['dateReceived']['format']='text'
    else:doc['paths']['/api/UpdateEventData']['post']['requestBody']['content']['application/json']['schema']['$ref']='#/components/schemas/UpdateEvent'
    assert 'error' in prepare() and env['data_loads']==0 and not env['data_posts']


@pytest.mark.parametrize('mode',['event','data','reference','mapping','authorization','actor'])
def test_ready_plan_rebuilds_original_object_data_config_and_actor_before_single_write(env,mode):
    ready=prepare();assert ready.get('state')=='ready',ready
    if mode=='event':env['event']['ChangeCount']+=1;env['classes']['ChangeCount']+=1
    elif mode=='data':env['data']['ProductName']='changed elsewhere';env['data']['ChangeCount']+=1
    elif mode=='reference':env['classification_refs']['000000000000b001']['ChangeCount']+=1
    elif mode=='mapping':env['classification_maps'][0]['ChangeCount']+=1
    elif mode=='authorization':env['authorized']=False
    else:env['actor']=OTHER
    assert 'error' in execute(ready) and not env['data_posts']


@pytest.mark.parametrize('mode',['400','timeout','cancel'])
def test_every_attempted_outcome_retains_unknown_and_prevents_replay_or_new_plan_bypass(env,mode):
    ready=prepare();env['data_mode']=mode
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:assert execute(ready)['state']=='unknown'
    assert 'error' in execute(ready) and len(env['data_posts'])==1
    other=prepare();assert other.get('state')=='ready',other
    assert 'error' in execute(other) and len(env['data_posts'])==1


@pytest.mark.parametrize('field,value',[('submit',True),('employee',{'instanceId':OTHER}),('classification',{'instanceId':OTHER}),('disallowedTxns',[8764]),('eventDataDetail',{})])
def test_transport_cannot_change_identity_state_or_preserved_payload(env,field,value):
    saved=snap(prepare());saved['body'][field]=value
    with pytest.raises(ValueError):p.transport(saved)


@pytest.mark.parametrize('mode',['wrong_object','wrong_type','wrong_history_type','wrong_marker','duplicate','too_many'])
def test_history_uses_shared_native_table_and_specific_type_without_claiming_success(env,mode):
    ready=prepare();execute(ready)
    if mode=='wrong_object':env['data_history'][0]['HistoryId']=OTHER
    elif mode=='wrong_type':env['data_history'][0]['TxnType']=7578
    elif mode=='wrong_history_type':env['data_history'][0]['CDOTypeId']=8751
    elif mode=='wrong_marker':env['data_history'][0]['Comments']='other marker'
    elif mode=='duplicate':env['data_history']*=2
    else:env['data_history']*=21
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert not out['verified_success'] and 'error' in out
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_inspector_reads_original_data_id_even_if_event_link_is_replaced(env):
    ready=prepare();execute(ready);env['extra']['EventDataId']=OTHER
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['current_event']['EventDataId']==OTHER and out['current_original_event_data']['EventDataId']==DATA and not out['verified_success']


def test_journal_fault_after_attempt_never_claims_not_submitted_or_unlocks(env,monkeypatch):
    import sqlite3
    ready=prepare();original=p.db_connect;failed=[False]
    def database():
        if env['data_posts'] and not failed[0]:failed[0]=True;raise sqlite3.OperationalError('isolated journal fault')
        return original()
    monkeypatch.setattr(p,'db_connect',database)
    out=execute(ready);assert out['state']=='unknown' and out['submitted'] is None and not out['retry_allowed']
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='in_flight'
    assert 'error' in execute(ready) and len(env['data_posts'])==1


@pytest.mark.parametrize('name',['employee','status','submit','event_data_id','classification','custom_fields','corrective_action_comments','occupation'])
def test_closed_patch_never_accepts_unimplemented_custom_or_identity_fields(name):
    with pytest.raises(ValidationError):e.EventDataTextChanges.model_validate({name:'unapproved'})


def test_native_maps_and_parent_apply_do_not_execute_unmapped_custom_detail_or_service_details():
    model=json.loads(Path('docs/quality-fixtures/event-classification-native-maps.json').read_text(encoding='utf-8'))
    key=next(r['CDOMapID'] for r in model['maps'] if r['Name']=='EventDataDetailsToEventData')
    fields={r['TargetField'] for r in model['fields'] if r['CDOMapID']==key}
    assert fields==set(e.DATA_FIELDS) and {'ProblemDescription','DeviceLocation'}<=fields and 'CorrectiveActionComments' not in fields
    super_flow=json.loads(Path('docs/quality-fixtures/event-custom-super-apply.json').read_text(encoding='utf-8'))
    assert [r['function'] for r in super_flow['calls']]==['callif','callif','callif']
    assert any('IsFieldDefined("ServiceDetail")'==p['value'] for r in super_flow['calls'] for p in r['parameters'])


def test_registered_mcp_typed_patch_and_single_system_confirmation(env):
    assert get_tool_func('prepare_quality_event_data_update') is e.prepare_quality_event_data_update
    async def check():
        async with Client(mcp) as client:
            tools={r.name:r for r in await client.list_tools()};schema=tools['prepare_quality_event_data_update'].input_schema
            assert schema['additionalProperties'] is False and schema['properties']['changes']['additionalProperties'] is False
            assert set(schema['properties']['changes']['properties'])=={'problem_description','device_location'}
            reply=await client.call_tool('prepare_quality_event_data_update',dict(event_name='E',organization_name='Org',changes=dict(device_location='B'),comments='Reviewed extension'))
            ready=json.loads(reply.content[0].text);assert ready.get('state')=='ready',ready
            calls=[dict(function=dict(name='execute_quality_transaction',arguments=json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})))]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval and 'UpdateEventData' in plan_preview(calls)
    asyncio.run(check());assert not env['data_posts']


def test_agent_direct_json_dispatch_and_extra_fields_are_validated_without_leaking_input(env):
    fn=get_tool_func('prepare_quality_event_data_update')
    ready=result(fn(event_name='E',organization_name='Org',changes=dict(device_location='Direct JSON B'),comments='Reviewed JSON'))
    assert ready.get('state')=='ready',ready
    bad=result(fn(event_name='E',organization_name='Org',changes=dict(password=TOKEN),comments='Rejected field'))
    assert 'error' in bad and TOKEN not in json.dumps(bad) and not env['data_posts']


def test_custom_data_diagnostic_preserves_explicit_deployment_dependency_without_plan(env):
    env['data']['EventCustomDataId']=OTHER
    out=result(e.get_quality_event_data_context('E','Org'))
    assert not out['complete'] and not out['custom_fields_supported'] and 'EventCustomData' in out['error']
    assert not p.PLAN_DB.exists() and not env['data_posts']


def test_missing_existing_data_is_explicitly_diagnosed_without_creation(env):
    env['extra']['EventDataId']=None
    out=result(e.get_quality_event_data_context('E','Org'))
    assert not out['complete'] and '没有已有EventData' in out['error'] and not p.PLAN_DB.exists() and not env['data_posts']


@pytest.mark.parametrize('turns,writes',[
    (['查询事件E扩展资料维护条件，不要保存'],False),(['请修改事件E的问题描述'],True),
    (['查看事件E扩展资料','把设备位置改为站点B'],True),(['查看事件E扩展资料','查看设备位置的原值'],False),
    (['查看事件E扩展资料','查看资源R的设备位置'],False),(['查看事件E扩展资料','修改资源R设备位置'],True),
    (['查看事件E问题描述','刚才超时了，不要重试，查询原计划'],False)])
def test_multi_turn_questions_keep_quality_text_scope_and_explicit_resource_topic_switch(turns,writes):
    tools=[dict(type='function',function=dict(name=n)) for n in TOOL_POLICIES]
    selected={r['function']['name'] for r in select_tools(tools,[dict(role='user',content=t) for t in turns])}
    switched='资源R' in turns[-1]
    assert ('prepare_quality_event_data_update' in selected) is not switched
    if not switched:assert ('execute_quality_transaction' in selected) is writes
    assert 'prepare_quality_event_data_update' in ROUTING_INSTRUCTION and '禁止普通execute=false POST假预览' in ROUTING_INSTRUCTION


def test_capability_and_inventory_are_explicit_about_bounded_text_and_custom_data_limit():
    cap=result(get_capabilities(tool_name='prepare_quality_event_data_update'))
    assert '255' in json.dumps(cap) and 'problem_description' in json.dumps(cap)
    inventory=result(e.q.get_quality_api_inventory())
    assert 'prepare_quality_event_data_update' in json.dumps(inventory)


def test_real_read_transport_cookie_rotation_stops_before_plan_or_business_attempt(env,monkeypatch):
    from core import signature_channel,http_client
    original=e.request
    async def check():
        async def handler(req):
            if req.method=='GET':return httpx.Response(200,json=env['data_contract'])
            assert req.url.path.endswith('/PerformEvent')
            payload=json.loads(req.content)
            raw=await original('POST','/api/UpdateEventData/PerformEvent',body=payload,params={},bound_headers={'Authorization':TOKEN},require_stable_session=True)
            return httpx.Response(200,json=json.loads(raw),headers={'set-cookie':'session=ISOLATED_ROTATION; Path=/'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(signature_channel,'get_client',lambda:client)
            monkeypatch.setattr(e,'request',http_client.request)
            return json.loads(await e.prepare_quality_event_data_update('E','Org',e.EventDataTextChanges(device_location='B'),'Reviewed cookie condition'))
    out=asyncio.run(check());assert 'error' in out and TOKEN not in json.dumps(out) and not p.PLAN_DB.exists() and not env['data_posts']


def test_remote_prepare_and_inspect_errors_cannot_reflect_bound_credentials(env,monkeypatch):
    ready=prepare();execute(ready)
    monkeypatch.setattr(e.q,'sql_rows',AsyncMock(side_effect=ValueError(TOKEN)))
    out=prepare();assert 'error' in out and TOKEN not in json.dumps(out)
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert 'error' in out and TOKEN not in json.dumps(out) and len(env['data_posts'])==1
