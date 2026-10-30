"""Isolated draft-save semantics; no real MES write, identity switch or signature."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from fastmcp import Client
from tools import mcp,get_tool_func,quality_event_drafts as d,quality_event_submission as s,quality_event_update as u,quality_plans as p
from core import signature_channel,http_client
from tests.test_quality_event_submission import env as submission_env,update_env,triage_env,final_fixture_review
from tests.test_quality_event_triage import EVENT,ORG,OWNER,ROLE,PRIORITY,OTHER,NEW_PRIORITY,echo
from agent.safety import evaluate_mutations,MutationCounts,plan_preview
from agent.tool_routing import select_tools,ROUTING_INSTRUCTION
from tools.tool_discovery import get_capabilities
from core.tool_catalog import TOOL_POLICIES

TOKEN='Bearer ISOLATED_DRAFT_SESSION_ONLY'


def result(x):return json.loads(asyncio.run(x))
def prepare(**kw):
    args=dict(event_name='E',organization_name='Org',changes=d.EventDraftChanges(description='Reviewed draft details'),comments='Reviewed draft save')|kw
    if isinstance(args['changes'],dict):args['changes']=d.EventDraftChanges(**args['changes'])
    return result(d.prepare_quality_event_draft_update(**args))
def execute(r):return result(p.execute_quality_transaction(r['plan_id'],r['plan_digest']))
def snap(r):return result(p.get_quality_transaction_plan(r['plan_id']))['snapshot']


@pytest.fixture
def env(submission_env,monkeypatch):
    state=submission_env;state.update(actor=OWNER,authorized=True,grant_version=1,draft_posts=[],draft_mode=None,draft_hook=None,load_transport_error=False)
    client=state['client'];client.is_closed=False;client.build_request=lambda method,url,headers:httpx.Request(method,url,headers=headers)
    monkeypatch.setattr(signature_channel,'get_client',lambda:client)
    monkeypatch.setattr(http_client,'get_headers',lambda:{'Authorization':TOKEN})
    base_sql=d.q.sql_rows
    async def sql(query,maximum):
        if ' FROM sys.objects ' in query:return [dict(FunctionId=81,DefinitionHash='a'*64)]
        if ' FROM EmployeeRole WHERE ' in query:
            return [dict(EmployeeRoleId='000000000000d010',EmployeeId=OWNER,RoleId=ROLE,OrganizationId=ORG,
                PropagateToChildOrgs=True,IsFrozen=False,ChangeCount=state['grant_version'])]
        if 'FROM dbo.csiAuthGetAllowedUsers(' in query:return [dict(RowCount=int(state['authorized']))]
        return await base_sql(query,maximum)
    monkeypatch.setattr(d.q,'sql_rows',AsyncMock(side_effect=sql))
    base_asset=d.t.asset
    async def asset(kind,**kw):
        if kind=='owner' and (kw.get('key')==OTHER or kw.get('name')=='Reporter'):
            return dict(entity='Employees',InstanceID=OTHER,Name='Reporter',IsFrozen=False,ChangeCount=state.get('reporter_version',1))
        if kind=='owner' and kw.get('name')=='Owner':kw=dict(key=OWNER)
        if kind=='organization' and kw.get('name')=='Org' and not kw.get('key'):kw['key']=ORG
        return await base_asset(kind,**kw)
    monkeypatch.setattr(d.t,'asset',AsyncMock(side_effect=asset))
    base_request=u.request
    def sync_detail(data):
        detail=data['ServiceDetail']
        for key in u.DATE_FIELDS:
            value=state['extra'][key]
            detail[key]=None if value is None else value.replace(' ','T')+('Z' if key.endswith('GMT') else '+08:00')
        data['QualityObjectDetail']['PriorityLevel']=None if state['event']['PriorityLevelId'] is None else {'InstanceId':state['event']['PriorityLevelId']}
    # Both new draft Load and later independent true-submit Load must see the
    # same persisted (including nullable) data, without inventing draft defaults.
    state['load_hook']=sync_detail
    state['update_hook']=lambda data:data['QualityObjectDetail'].update(PriorityLevel={'InstanceId':state['event']['PriorityLevelId']})
    state['hook']=lambda data:data['QualityObjectDetail'].update(PriorityLevel={'InstanceId':state['event']['PriorityLevelId']})
    async def request(method,path,**kw):
        assert method=='POST' and path=='/api/UpdateEvent/PerformEvent', 'Never ordinary POST Submit=false for preview'
        assert kw['require_stable_session'] and kw['bound_headers']['Authorization']==TOKEN
        if state['load_transport_error']:raise ValueError('isolated session rotation')
        data=json.loads(await base_request(method,path,**kw))['data']
        data.update(Employee={'InstanceId':state['actor']},ESigDetails=[],QualityESigDetail=None)
        if state['draft_hook']:state['draft_hook'](data)
        return json.dumps({'data':data})
    monkeypatch.setattr(d,'request',AsyncMock(side_effect=request))
    native_submit=client.request
    async def submit(method,url,**kw):
        if not url.endswith('/api/UpdateEvent') or kw['json'].get('submit') is True:
            return await native_submit(method,url,**kw)
        assert method=='POST' and kw['params']=={'execute':'true'} and kw['follow_redirects'] is False
        assert kw['headers']['Authorization']==TOKEN and kw['headers']['Cookie']==''
        state['draft_posts'].append((url,copy.deepcopy(kw['json']),dict(kw['headers'])))
        if state['draft_mode']=='timeout':raise TimeoutError(TOKEN)
        if state['draft_mode']=='cancel':raise asyncio.CancelledError()
        if state['draft_mode']=='400':return SimpleNamespace(status_code=400,text=TOKEN)
        body=kw['json'];detail=body['serviceDetail']
        assert not {'employee','owner','role','status','isSubmitted','ignoreProcessModel','qualityESigDetail'}&body.keys()
        for key in u.TEXT_FIELDS.values():state['extra'][key[0][0].upper()+key[0][1:]]=detail[key[0]]
        for field in u.REF_FIELDS:
            ref=detail[field[0].lower()+field[1:]];key=None if ref is None else ref['instanceId']
            if field=='PriorityLevel':state['event']['PriorityLevelId']=key
            else:state['extra'][field+'Id']=key
        for key in u.DATE_FIELDS:
            value=detail[key[0].lower()+key[1:]]
            state['extra'][key]=None if value is None else value.removesuffix('Z').removesuffix('+08:00').replace('T',' ')
        # Native FinishModifyEntity submits/AutoTriages only when Submit=true.
        state['event']['ChangeCount']+=1;state['classes']['ChangeCount']=state['event']['ChangeCount']
        state['history_rows']=[dict(UpdateEventHistoryId='000000000000d011',HistoryId=EVENT,HistoryMainlineId='000000000000d012',TxnId='000000000000d013',
            Submit=False,ChangeCount=1,TxnType=7578,ReversalStatus=0,Comments=body['comments'])]
        return SimpleNamespace(status_code=200,text=TOKEN)
    client.request=AsyncMock(side_effect=submit)
    return state


def make_incomplete(env):
    env['extra'].update(BriefDescription='',ReporterId=None,InitiatorId=None,ReporterOrganizationId=None,InitiatorOrganizationId=None)
    for field in u.DATE_FIELDS:env['extra'][field]=None
    env['event']['PriorityLevelId']=None


def test_incomplete_draft_fill_save_review_then_separately_submit_and_triage(env):
    make_incomplete(env)
    context=result(u.get_quality_event_update_context('E','Org',purpose='edit_pending_draft'))
    assert context['complete'] and 'reporter' in context['missing_submission_fields'] and not p.PLAN_DB.exists()
    assert not context['ready_for_submission'] and not context['regular_nonexecuting_post_requested']
    ready=prepare(changes=dict(brief_description='Complete title',reporter_name='Reporter',initiator_name='Owner',
        reporter_organization_name='Org',initiator_organization_name='Org',priority_level_name='Urgent',
        occurrence_date='2026-10-29T10:00:00.1234567+08:00',reported_date='2026-10-29T11:00:00+08:00'))
    assert ready.get('state')=='ready',ready
    saved=snap(ready);assert saved['preview']['remaining_submission_fields']==[] and not saved['preview']['ready_for_submission']
    assert not ready['static_and_echo_verified'] and saved['preview']['static_preview_only']
    assert not env['draft_posts'] and not env['submission_posts']
    assert execute(ready)['state']=='unknown' and len(env['draft_posts'])==1
    assert env['event']['Status']==2 and env['event']['IsSubmitted'] is False and env['event']['TriageComplete'] is False
    evidence=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert evidence['history_submit_flag_selected'] is False and not evidence['verified_success'],evidence
    assert evidence['submission_history_candidates'][0]['Submit'] is False
    assert 'error' in execute(ready)
    submit=result(s.prepare_quality_event_submission('E','Org','Independently reviewed submission'))
    assert submit.get('state')=='ready',submit
    assert 'error' in execute(submit) and not env['submission_posts'] # unknown draft still occupies original Event
    final_fixture_review(ready)
    assert execute(submit)['state']=='unknown' and len(env['submission_posts'])==1
    assert env['event']['Status']==1 and env['event']['IsSubmitted'] is True
    final_fixture_review(submit)
    triage=result(d.t.prepare_quality_event_triage('E','Org'))
    assert triage.get('state')=='ready',triage
    assert execute(triage)['state']=='unknown' and env['event']['TriageComplete'] is True


def test_original_details_and_explicit_clear_null_preservation_and_no_automatic_defaults(env):
    ready=prepare(changes=dict(description='',discovery_area=None))
    assert ready.get('state')=='ready',ready
    body=ready['request_preview']['body'];original=snap(ready)['original_detail']
    assert body['submit'] is False and body['serviceDetail']==original|{'description':''}
    assert set(body)=={'qualityObject','organization','submit','comments','serviceDetail'}
    assert execute(ready)['state']=='unknown'
    persisted=result(p.get_quality_transaction_plan(ready['plan_id']))
    assert TOKEN not in json.dumps(persisted) and TOKEN.encode() not in p.PLAN_DB.read_bytes()
    assert persisted['outcome']['raw_response'] is None


def test_draft_can_remain_incomplete_and_signature_policy_is_not_submission_permission(env):
    make_incomplete(env);env['roots']['Organizations']['QualityESigTxns']=[7578]
    ready=prepare();assert ready.get('state')=='ready',ready
    assert 'reporter' in snap(ready)['preview']['remaining_submission_fields']
    assert execute(ready)['state']=='unknown' and env['extra']['ReporterId'] is None and not env['submission_posts']
    assert 'error' in result(s.prepare_quality_event_submission('E','Org','Must not bypass formal requirements'))


@pytest.mark.parametrize('changes',[{},dict(description=None),dict(description='Original description'),dict(occurrence_date='2026-10-29T10:00:00'),
    dict(reported_date='2026-10-29T01:00:00Z'),dict(description='a\x00b'),dict(brief_description='😀'*51),dict(reporter_name='')])
def test_invalid_or_noop_changes_never_attempt_native_save(env,changes):
    assert 'error' in prepare(changes=changes) and not env['draft_posts']


@pytest.mark.parametrize('field,value',[('Status',1),('IsSubmitted',True),('TriageComplete',True),('IsFrozen',True),('Category',5),('CDOTypeId',7734),
    ('ProcessModelId',OTHER),('ChecklistId',OTHER),('ChecklistSaved',True),('OwnerId',None),('RoleId',None)])
def test_nonplain_or_nonpending_context_is_rejected(env,field,value):
    env['event'][field]=value
    assert 'error' in prepare() and not env['draft_posts']


@pytest.mark.parametrize('mode',['approval','ncr','regulatory','event_data','no_edit','not_owner','no_role','signature','detail_signature',
    'unknown_actor','unauthorized','bad_contract','session_rotation','unpreserved_original','native_default','unknown_detail'])
def test_complex_unknown_or_unauthorized_context_is_not_silently_overwritten(env,mode):
    if mode=='approval':env['approvals']=[{'ApprovalSheetId':OTHER}]
    elif mode=='ncr':env['links']=[{'NonconformanceReportId':OTHER}]
    elif mode=='regulatory':env['regulatory']=[{'RegulatoryReportId':OTHER}]
    elif mode=='event_data':env['extra']['EventDataId']=OTHER
    elif mode=='unknown_actor':env['actor']=OTHER
    elif mode=='unauthorized':env['authorized']=False
    elif mode=='session_rotation':env['load_transport_error']=True
    elif mode=='bad_contract':env['contract']['components']['schemas']['EventDetail']['properties']['unsupportedExtension']={'type':'string'}
    else:
        def hook(data):
            if mode=='no_edit':data['AvailableActions']=1
            elif mode=='not_owner':data['QualityObjectDetail']['UserIsOwner']=False
            elif mode=='no_role':data['QualityObjectDetail']['UserHasRole']=False
            elif mode=='signature':data['ESigRequirement']={'InstanceId':OTHER}
            elif mode=='detail_signature':data['QualityESigDetail']={}
            elif mode=='unpreserved_original':data['ServiceDetail']['Description']='unexpected change'
            elif mode=='native_default':data['ServiceDetail']['Reporter']={'InstanceId':OWNER}
            else:data['ServiceDetail']['UnverifiedField']='custom data'
        env['draft_hook']=hook
        if mode=='native_default':env['extra']['ReporterId']=None
    assert 'error' in prepare() and not env['draft_posts']


@pytest.mark.parametrize('mode',['source','grant','actor','authorized','reference','contract'])
def test_changed_source_or_actor_configuration_prevents_single_execution(env,mode):
    ready=prepare(changes=dict(description='Updated',reporter_name='Reporter'));assert ready.get('state')=='ready',ready
    if mode=='source':env['event']['ChangeCount']+=1;env['classes']['ChangeCount']+=1
    elif mode=='grant':env['grant_version']+=1
    elif mode=='actor':env['actor']=OTHER
    elif mode=='authorized':env['authorized']=False
    elif mode=='reference':env['reporter_version']=2
    else:env['contract']['components']['schemas']['EventDetail']['properties']['unsupportedExtension']={'type':'string'}
    assert 'error' in execute(ready) and not env['draft_posts']


@pytest.mark.parametrize('mode',['400','timeout','cancel'])
def test_attempted_outcome_stays_unknown_and_blocks_second_plan(env,mode):
    ready=prepare();env['draft_mode']=mode
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:assert execute(ready)['state']=='unknown'
    assert 'error' in execute(ready) and len(env['draft_posts'])==1
    other=prepare();assert other.get('state')=='ready',other
    assert 'error' in execute(other) and len(env['draft_posts'])==1


@pytest.mark.parametrize('field,value',[('submit',True),('status',1),('employee',{'instanceId':OTHER}),('ignoreProcessModel',True),('serviceDetail',{})])
def test_fixed_false_save_transport_rejects_identity_status_and_other_payload(env,field,value):
    saved=snap(prepare());saved['body'][field]=value
    with pytest.raises(ValueError):p.transport(saved)


@pytest.mark.parametrize('mode',['wrong_object','wrong_flag','wrong_type','wrong_marker','duplicate','too_many'])
def test_history_keeps_save_and_submit_branches_distinct_without_claiming_success(env,mode):
    ready=prepare();execute(ready)
    if mode=='wrong_object':env['history_rows'][0]['HistoryId']=OTHER
    elif mode=='wrong_flag':env['history_rows'][0]['Submit']=True
    elif mode=='wrong_type':env['history_rows'][0]['TxnType']=7490
    elif mode=='wrong_marker':env['history_rows'][0]['Comments']='other'
    elif mode=='duplicate':env['history_rows']*=2
    else:env['history_rows']*=21
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert not out['verified_success'] and 'error' in out
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_journal_fault_after_save_never_claims_no_attempt_or_releases_occupancy(env,monkeypatch):
    import sqlite3
    ready=prepare();original=p.db_connect;failed=[False]
    def database():
        if env['draft_posts'] and not failed[0]:failed[0]=True;raise sqlite3.OperationalError('isolated journal fault')
        return original()
    monkeypatch.setattr(p,'db_connect',database)
    out=execute(ready);assert out['state']=='unknown' and out['submitted'] is None and not out['retry_allowed']
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='in_flight'
    assert 'error' in execute(ready) and len(env['draft_posts'])==1


def test_mcp_registered_closed_patch_schema_and_single_system_confirmation(env):
    assert get_tool_func('prepare_quality_event_draft_update') is not None
    async def check():
        async with Client(mcp) as client:
            tools={x.name:x for x in await client.list_tools()};schema=tools['prepare_quality_event_draft_update'].input_schema
            assert schema['additionalProperties'] is False
            model=schema['properties']['changes'];assert model['additionalProperties'] is False
            assert not {'employee','password','owner','role','submit','status','classification','organization'}&model['properties'].keys()
            reply=await client.call_tool('prepare_quality_event_draft_update',dict(event_name='E',organization_name='Org',
                changes=dict(description='Reviewed typed patch'),comments='Reviewed save'))
            ready=json.loads(reply.content[0].text);assert ready.get('state')=='ready',ready
            calls=[dict(function=dict(name='execute_quality_transaction',arguments=json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})))]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval and 'UpdateEvent' in plan_preview(calls)
    asyncio.run(check());assert not env['draft_posts']


@pytest.mark.parametrize('field',['submit','status','owner','employee','password','organization','classification'])
def test_patch_rejects_unexposed_business_and_identity_fields(field):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):d.EventDraftChanges.model_validate({field:'unsafe'})


@pytest.mark.parametrize('question,writes',[
    ('查询事件E草稿保存条件，不要执行',False),('检查事件E草稿补齐资料的要求，不要保存',False),
    ('查看事件E草稿原资料',False),('请补齐事件E草稿资料并保存',True),('请保存事件E草稿',True),
    ('修改事件E草稿的报告人和发生时间',True),('事件E草稿缺什么资料，不要提交',False)])
def test_draft_intents_expose_preparation_but_only_explicit_writes_expose_executor(question,writes):
    tools=[dict(type='function',function=dict(name=name)) for name in TOOL_POLICIES]
    selected={x['function']['name'] for x in select_tools(tools,[dict(role='user',content=question)])}
    assert {'prepare_quality_event_draft_update','get_quality_event_update_context'}<=selected
    assert ('execute_quality_transaction' in selected) is writes
    assert 'edit_pending_draft' in ROUTING_INSTRUCTION and '禁止普通execute=false假预览' in ROUTING_INSTRUCTION


def test_capability_and_api_inventory_disclose_static_draft_and_separate_submit_boundary():
    capability=result(get_capabilities(tool_name='prepare_quality_event_draft_update'))
    assert 'static_and_echo_verified=false' in json.dumps(capability,ensure_ascii=False)
    inventory=result(d.q.get_quality_api_inventory())
    assert 'prepare_quality_event_draft_update' in json.dumps(inventory)
