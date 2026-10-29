import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from tools import mcp,get_tool_func,quality_event_close_plan as c,quality_event_triage as t,quality_plans as p
from tests.test_quality_event_update import env as update_env
from tests.test_quality_event_triage import env as triage_env,EVENT,ORG,OWNER,ROLE,PRIORITY,OTHER,NCR,CONT,echo
from agent.safety import evaluate_mutations,MutationCounts,plan_preview

CODE='00000000000000aa'
def result(coro):return json.loads(asyncio.run(coro))
def prepare(**kw):return result(c.prepare_quality_event_closure('E','Org','Resolved',**({'close_description':'Verified and resolved'}|kw)))
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))

@pytest.fixture
def env(update_env,monkeypatch):
    state=update_env;state['event']['TriageComplete']=True
    state['roots']['QualityResolutionCodes']=dict(Name='Resolved',InstanceID=CODE,IsFrozen=False)
    state['counters'][CODE]=1;state['close_hook']=None;state['close_options']=[dict(Name='Close',Value='7')]
    state['close_contract']=copy.deepcopy(json.loads(Path('docs/quality-fixtures/contracts.json').read_text(encoding='utf8'))['Shopfloor/CloseQualityObject'])
    sql=c.q.sql_rows
    async def sql_extra(query,maximum):
        if 'INFORMATION_SCHEMA.COLUMNS' in query and "TABLE_NAME='QualityResolutionCode'" in query:
            return [dict(TABLE_NAME=t,COLUMN_NAME=k) for t,v in c.SCHEMA.items() for k in v.split(',')]
        return await sql(query,maximum)
    monkeypatch.setattr(c.q,'sql_rows',AsyncMock(side_effect=sql_extra))
    original=t.ReadContext
    class Context(original):
        async def get(self,entity,key=None,params=None):
            if entity=='QualityResolutionCodes' and key is None:return {'value':[copy.deepcopy(state['roots'][entity])]}
            return await super().get(entity,key,params)
    monkeypatch.setattr(t,'ReadContext',Context)
    monkeypatch.setattr(c.q,'closure_options',AsyncMock(side_effect=lambda *a:copy.deepcopy(state['close_options'])))
    async def request(method,path,**kw):
        if method=='GET':return json.dumps(state['close_contract'])
        assert (method,path)==('POST','/api/CloseQualityObject') and kw['params']['execute']=='false'
        row=state['event'];data=echo(copy.deepcopy(kw['body']))
        data.update(AvailableActions='65',IgnoreProcessModel=False,ApprovalRequired=False,ApprovalSheetMap=None,
            ApprovalSheetTemplate=None,ApprovalSheetStatus=None,ESigRequirement=None,AdditionalESigReqs=[],Employee={'InstanceId':OWNER})
        data['QualityObjectDetail']=dict(Category=row['Category'],Status=row['Status'],Organization={'InstanceId':ORG},
            Owner={'InstanceId':OWNER},Role={'InstanceId':ROLE},PriorityLevel={'InstanceId':PRIORITY},ProcessModel=None,
            TriageComplete=True,IsCARRequiredToClose=False,IsApprovalRequired=False,CompletionApprovalSheet=None,
            CompletionApprovalStatus=None,EntryForApproval=None,UserHasRole=True,UserIsOwner=True)
        if state['close_hook']:state['close_hook'](data)
        return json.dumps({'data':data})
    monkeypatch.setattr(c,'request',AsyncMock(side_effect=request))
    return state


def test_native_close_plan_uses_reported_action_and_keeps_original_scope(env):
    out=prepare();assert out.get('state')=='ready',out
    assert out['request_preview']['body']['resolutionAction']==7
    assert out['request_preview']['body']['qualityObject']['instanceId']==EVENT
    assert not out['server_context']['event_closed_verified'] and out['server_context']['resolve_action_mask']==64
    assert 'SHOULD_NEVER_BE_STORED' not in json.dumps(out)
    env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('field,value',[('TriageComplete',False),('IsCARRequiredToClose',True),('Status',5),('IsSubmitted',False),
    ('IsFrozen',True),('ProcessModelId',OTHER),('ChecklistId',OTHER),('EventDataId',OTHER)])
def test_complex_events_rejected_before_native_preview(env,field,value):
    (env['extra'] if field in env['extra'] else env['event'])[field]=value
    assert 'error' in prepare();c.request.assert_not_awaited()

@pytest.mark.parametrize('mode',['ncr','approval','regulatory','signature','all_signature'])
def test_no_linked_or_approval_or_signature_bypass(env,mode):
    if mode=='ncr':env['links']=[dict(NonconformanceReportId=NCR,EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1)]
    elif mode=='approval':env['approvals']=[dict(ApprovalSheetId=OTHER)]
    elif mode=='regulatory':env['regulatory']=[dict(RegulatoryReportId=OTHER)]
    elif mode=='signature':env['roots']['Organizations']['QualityESigTxns']=[7712]
    else:env['roots']['Organizations']['CollectESigForAllQualityTxns']=True
    assert 'error' in prepare();c.request.assert_not_awaited()

@pytest.mark.parametrize('field,value',[('AvailableActions',1),('IgnoreProcessModel',True),('ApprovalRequired',True),
    ('ApprovalSheetMap',{}),('ApprovalSheetTemplate',{}),('ApprovalSheetStatus',3),('ESigRequirement',{}),
    ('AdditionalESigReqs',[{}]),('Employee',{'InstanceId':OTHER}),('CloseDescription','ignored')])
def test_native_close_echo_and_permissions_are_not_overridden(env,field,value):
    env['close_hook']=lambda data:data.update({field:value})
    assert 'error' in prepare() and not p.PLAN_DB.exists()

@pytest.mark.parametrize('field,value',[('UserIsOwner',False),('UserHasRole',False),('IsApprovalRequired',True),
    ('Status',5),('ProcessModel',{}),('CompletionApprovalSheet',{}),('IsCARRequiredToClose',True),('TriageComplete',False)])
def test_native_object_context_must_match(env,field,value):
    env['close_hook']=lambda data:data['QualityObjectDetail'].update({field:value})
    assert 'error' in prepare()

@pytest.mark.parametrize('description',['',' ','x'*4001,'bad\x00value'])
def test_invalid_description_does_not_query(env,description):
    assert 'error' in prepare(close_description=description);c.q.sql_rows.assert_not_awaited()

@pytest.mark.parametrize('mode',['missing','duplicate','bad_contract','resolution_frozen'])
def test_candidates_and_contract_are_checked(env,mode):
    if mode=='missing':env['close_options']=[]
    elif mode=='duplicate':env['close_options']*=2
    elif mode=='bad_contract':env['close_contract']['components']['schemas']['CloseQualityObject']['properties']['resolutionAction']['type']='string'
    else:env['roots']['QualityResolutionCodes']['IsFrozen']=True
    assert 'error' in prepare()

@pytest.mark.parametrize('mode',['event','text','resolution','signature','ncr'])
def test_stale_ready_plan_never_sends_business_write(env,mode):
    ready=prepare();assert ready.get('state')=='ready',ready
    if mode=='event':env['event']['ChangeCount']=2
    elif mode=='text':env['extra']['Description']='Changed'
    elif mode=='resolution':env['counters'][CODE]=2
    elif mode=='signature':env['roots']['Organizations']['QualityESigTxns']=[7712]
    else:env['links']=[dict(NonconformanceReportId=NCR,EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1)]
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('field,value',[('ignoreProcessModel',True),('resolutionAction',True),('approvalRequired',False),
    ('qualityObject',{'name':'Other','instanceId':OTHER}),('qualityResolutionCode',{'name':'Other','instanceId':OTHER})])
def test_transport_rejects_extra_fields_and_changed_target(env,field,value):
    ready=prepare();saved=result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot'];saved['body'][field]=value
    with pytest.raises(ValueError):p.transport(saved)

@pytest.mark.parametrize('mode',['200','timeout','cancel','400'])
def test_once_only_and_unknown_conflicts_with_event_updates(env,mode):
    first=prepare();second=prepare();assert first.get('state')=='ready',first
    if mode=='400':env['client'].request.return_value.status_code=400
    elif mode=='timeout':env['client'].request.side_effect=TimeoutError('uncertain')
    elif mode=='cancel':env['client'].request.side_effect=asyncio.CancelledError()
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(first)
    else:assert execute(first)['state']=='unknown'
    assert 'error' in execute(first) and 'error' in execute(second)
    env['client'].request.assert_awaited_once()
    call=env['client'].request.await_args;assert call.args[1].endswith('/api/CloseQualityObject') and call.kwargs['params']=={'execute':'true'}
    saved=result(p.get_quality_transaction_plan(first['plan_id']))['snapshot']
    other=dict(kind='event_update',scope_key='quality-event:id:'+EVENT,environment=p.environment(),body={},before={},resource_writes=['runtime:quality-event:id:'+EVENT])
    assert p.conflicts(saved,other)


def test_inspect_closed_renamed_original_is_evidence_only(env):
    ready=prepare();execute(ready);env['event'].update(EventName='Renamed',Status=5,CloseDateGMT='2026-10-29T01:00:00Z')
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['original_event_id']==EVENT and out['current_event']['EventName']=='Renamed'
    assert not out['verified_success'] and not out['retry_allowed']
    c.q.get_quality_history.assert_awaited_once_with('quality_object','Renamed',EVENT,limit=20)


def test_mcp_schema_and_confirmation(env):
    async def check():
        assert get_tool_func('prepare_quality_event_closure') is c.prepare_quality_event_closure
        async with Client(mcp) as client:
            schema=next(t.input_schema for t in await client.list_tools() if t.name=='prepare_quality_event_closure')
            assert schema['additionalProperties'] is False and not {'submit','ignoreProcessModel','signerPassword'}&schema['properties'].keys()
            out=await client.call_tool('prepare_quality_event_closure',dict(event_name='E',organization_name='Org',quality_resolution_code_name='Resolved',close_description='Verified'))
            ready=json.loads(out.content[0].text);assert ready.get('state')=='ready',ready
            calls=[dict(function=dict(name='execute_quality_transaction',arguments=json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})))]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval and 'CloseQualityObject' in plan_preview(calls)
    asyncio.run(check());env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('turns,write',[
    (['查看质量事件关闭前置'],False),(['请关闭质量事件E'],True),
    (['查看质量事件关闭条件','继续'],False),(['查看质量事件关闭条件','提交这个计划'],True)])
def test_closure_routing_and_followups(turns,write):
    from agent.tool_routing import select_tools,ROUTING_INSTRUCTION
    from core.tool_catalog import TOOL_POLICIES
    chosen={x['function']['name'] for x in select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[dict(role='user',content=s) for s in turns])}
    assert {'get_quality_event_closure_context','prepare_quality_event_closure'}<=chosen
    assert ('execute_quality_transaction' in chosen) is write
    assert '不得用prepare_capa_closure关闭Event' in ROUTING_INSTRUCTION


def test_native_closure_guards_and_column_evidence():
    data=json.loads(Path('docs/quality-fixtures/event-close-plan-native.json').read_text(encoding='utf8'))
    values=[p['value'] for c in data['clfs']['CloseQualityObject_StartModifyingEntity']['calls'] for p in c['parameters']]
    assert 'CloseMfgNCR' in values and any('QualityObject.EventData' in v for v in values)
    extra=json.loads(Path('docs/quality-fixtures/event-close-plan-extra.json').read_text(encoding='utf8'))
    cols=extra['metadata']['results'][0]['result']['records']
    assert {'QualityResolutionCodeId','ChangeCount'}<={x['name'] for x in cols}


def test_transient_status_ids_do_not_invalidate_plan_and_missing_original_not_followed(env):
    counter=[0]
    def transient(data):
        counter[0]+=1;data['QualityObjectDetail']['InstanceId']=str(counter[0])
    env['close_hook']=transient
    first=prepare();assert execute(first)['state']=='unknown'
    env['event']=None
    out=result(p.inspect_quality_transaction(first['plan_id'],first['plan_digest']))
    assert out['current_event'] is None and not out['verified_success']
    c.q.get_quality_history.assert_not_awaited()
