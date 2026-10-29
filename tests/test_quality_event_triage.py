import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from tools import mcp, get_tool_func, quality_event_triage as t, quality_plans as p
from core.tool_catalog import TOOL_POLICIES
from agent.safety import evaluate_mutations, MutationCounts, plan_preview
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION

EVENT,ORG,OWNER,ROLE,PRIORITY,SPEC,DETAIL,MAP,OTHER,NEW_PRIORITY,NCR,CONT=[f'{i:016x}' for i in range(301,313)]
def result(coro):return json.loads(asyncio.run(coro))
def echo(value):
    if isinstance(value,dict):return {p.FIELD_CASE.get(k,k[0].upper()+k[1:]):echo(v) for k,v in value.items()}
    if isinstance(value,list):return [echo(v) for v in value]
    return value


@pytest.fixture
def env(monkeypatch,tmp_path):
    event=dict(EventId=EVENT,EventName='E',CDOTypeId=7489,ChangeCount=1,Status=1,Category=1,OrganizationId=ORG,
        OwnerId=OWNER,RoleId=ROLE,PriorityLevelId=PRIORITY,IsFrozen=False,IsSubmitted=True,TriageComplete=False,
        IsCARRequiredToClose=False,ProcessModelId=None,OrigProcessModelTemplateId=None,ChecklistId=None,
        ChecklistSaved=False,CloseDateGMT=None)
    def obj(name,key):return dict(Name=name,InstanceID=key,IsFrozen=False)
    roots={'Organizations':obj('Org',ORG)|dict(CollectESigForAllQualityTxns=False,UserRequiredForEsig=False,QualityESigTxns=[]),
           'Employees':obj('Owner',OWNER)|dict(DocManagerPassword='SHOULD_NEVER_BE_STORED'),
           'Roles':obj('Role',ROLE),'PriorityLevels':obj('Normal',PRIORITY),'TriageSpecs':obj('Spec',SPEC)}
    state=dict(event=event,roots=roots,counters={k:1 for k in (ORG,OWNER,ROLE,PRIORITY,SPEC,NEW_PRIORITY)},
        approvals=[],links=[],maps=[dict(CategoryMapId=MAP,ParentId=ORG,Category=1,TriageSpecId=SPEC,IsFrozen=False,ChangeCount=1)],
        details=[dict(TriageSpecDetailId=DETAIL,TriageSpecId=SPEC,ToCategory=1,OwnerId=None,RoleId=None,CARRequired=False,
                      AutoRoute=False,ProcessModelId=None,ChecklistTemplateId=None,IsFrozen=False,ChangeCount=1)],
        hook=None,requests=[],schema=[dict(TABLE_NAME=table,COLUMN_NAME=col) for table,cols in t.SCHEMA.items() for col in cols.split(',')])
    async def sql(query,maximum):
        if 'INFORMATION_SCHEMA.COLUMNS' in query:return copy.deepcopy(state['schema'])
        if ' FROM Event WHERE ' in query:return [copy.deepcopy(state['event'])] if state['event'] else []
        if ' FROM NonconformanceReport WHERE ' in query:return copy.deepcopy(state['links'])
        if ' FROM ApprovalSheet WHERE ' in query:return copy.deepcopy(state['approvals'])
        if 'FROM CategoryMap WHERE ' in query:return copy.deepcopy(state['maps'])
        if ' FROM TriageSpecDetail ' in query:return copy.deepcopy(state['details'])
        for entity,table,column in t.ASSETS.values():
            if f' FROM {table} WHERE ' in query:
                key=query.split("='")[-1].rstrip("'")
                return [{column:key,'ChangeCount':state['counters'][key]}]
        raise AssertionError(query)
    class Context:
        def __init__(self,*args):pass
        async def get(self,entity,key=None,params=None):
            state['requests'].append((entity,key,copy.deepcopy(params)))
            assert '$select' in params
            assert not any(s in params['$select'].lower() for s in ('password','signer','docmanager'))
            if key is None:
                assert entity=='PriorityLevels' and params['$top']==2
                return {'value':[obj('Urgent',NEW_PRIORITY)]}
            if key==NEW_PRIORITY:return obj('Urgent',NEW_PRIORITY)
            assert entity in state['roots'] and key==state['roots'][entity]['InstanceID']
            return copy.deepcopy(state['roots'][entity])
    async def preview(method,path,**kw):
        assert (method,path)==('POST','/api/TriageQualityObject') and kw['params']['execute']=='false'
        assert 'Password' not in kw['params']['$expand'] and 'Signer' not in kw['params']['$expand']
        data=echo(copy.deepcopy(kw['body']));data.update(AvailableActions='31',IgnoreProcessModel=False,RouteProcessModel=False,
            ProcessModelTemplate=None,ChecklistTemplate=None,ESigRequirement=None,AdditionalESigReqs=[],
            TriageSpecDetail={'InstanceId':DETAIL,'Parent':{'InstanceId':SPEC}})
        row=state['event']
        data['QualityObjectDetail']=dict(Category=row['Category'],Status=row['Status'],Organization={'InstanceId':ORG},
            Owner={'InstanceId':OWNER},Role={'InstanceId':ROLE},PriorityLevel={'InstanceId':PRIORITY},ProcessModel=None,
            TriageComplete=False,IsCARRequiredToClose=False,IsApprovalRequired=False,CompletionApprovalSheet=None,
            CompletionApprovalStatus=None,EntryForApproval=None,UserHasRole=True,UserIsOwner=True,InstanceId=OTHER)
        if state['hook']:state['hook'](data)
        return json.dumps({'data':data})
    monkeypatch.setattr(t.q,'sql_rows',AsyncMock(side_effect=sql));monkeypatch.setattr(t,'ReadContext',Context)
    monkeypatch.setattr(t,'request',AsyncMock(side_effect=preview));monkeypatch.setattr(p,'PLAN_DB',tmp_path/'quality.sqlite3')
    monkeypatch.setattr(t.q,'get_quality_history',AsyncMock(return_value=json.dumps({'data':{'Histories':[]},'page_complete':True})))
    client=type('Client',(),{})();client.request=AsyncMock(return_value=type('Reply',(),dict(status_code=200,text='{"CompletionMsg":"done"}'))())
    monkeypatch.setattr(p,'get_client',lambda:client);monkeypatch.setattr(p,'get_headers',lambda:{})
    state['client']=client
    return state


def prepare(**kw):return result(t.prepare_quality_event_triage('E','Org',**kw))
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))
def snapshot(ready):return result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']


def test_context_native_action_mask_and_no_local_plan_or_business_write(env):
    out=result(t.get_quality_event_triage_context('E','Org'))
    assert out['complete'] and not out['ready_for_execution']
    assert out['server_context']['available_actions_raw']==31 and out['server_context']['triage_action_mask']==16
    assert not p.PLAN_DB.exists();env['client'].request.assert_not_awaited()


def test_preparation_preserves_owner_role_category_car_and_priority_by_default(env):
    ready=prepare(comments='初次分流确认');assert ready['state']=='ready',ready
    body=ready['request_preview']['body']
    assert body['qualityObject']==dict(name='E',instanceId=EVENT)
    assert body['qualityObjectOwner']==dict(name='Owner',instanceId=OWNER) and body['qualityObjectRole']['instanceId']==ROLE
    assert body['priorityLevel']['instanceId']==PRIORITY and body['category']==1 and body['triageComplete'] is True
    assert body['isCARRequiredToClose'] is False
    assert set(body)=={'qualityObject','organization','category','qualityObjectOwner','qualityObjectRole','priorityLevel',
                       'isCARRequiredToClose','triageComplete','comments'}
    assert not ready['server_context']['triage_completed_verified']
    assert 'SHOULD_NEVER_BE_STORED' not in json.dumps(snapshot(ready))
    env['client'].request.assert_not_awaited()


def test_explicit_priority_only_and_single_submission_unknown(env):
    ready=prepare(priority_level_name='Urgent')
    assert ready['request_preview']['body']['priorityLevel']==dict(name='Urgent',instanceId=NEW_PRIORITY)
    assert snapshot(ready)['before']['original_priority']['InstanceID']==PRIORITY
    assert execute(ready)['state']=='unknown' and 'error' in execute(ready)
    env['client'].request.assert_awaited_once()
    call=env['client'].request.await_args
    assert call.args[1].endswith('/api/TriageQualityObject') and call.kwargs['params']=={'execute':'true'}
    assert call.kwargs['follow_redirects'] is False


def test_unknown_blocks_same_event_and_related_ncr_container(env):
    env['links']=[dict(NonconformanceReportId=NCR,EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1)]
    first=prepare();second=prepare();saved=snapshot(first)
    assert p.conflicts(saved,dict(kind='ncr',scope_key='ncr:N',target_id=NCR,environment=p.environment(),body={},before={'ContainerId':CONT}))
    assert execute(first)['state']=='unknown' and 'error' in execute(second)
    env['client'].request.assert_awaited_once()


@pytest.mark.parametrize('field,value',[('CDOTypeId',7734),('Status',2),('Status',5),('Status',7),('Category',0),
    ('ChangeCount',None),('IsSubmitted',False),('IsSubmitted',None),('IsFrozen',True),('TriageComplete',True),
    ('OwnerId',None),('RoleId',None),('PriorityLevelId',None),('ProcessModelId',OTHER),
    ('OrigProcessModelTemplateId',OTHER),('ChecklistId',OTHER),('ChecklistSaved',True),('CloseDateGMT','2026-10-29'),
    ('OrganizationId','bad'),('EventId',OTHER)])
def test_only_plain_submitted_active_first_triage_is_eligible(env,field,value):
    env['event'][field]=value
    assert 'error' in prepare(**({'event_instance_id':EVENT} if field=='EventId' else {}))
    t.request.assert_not_awaited();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['missing','incomplete','schema_missing','schema_duplicate','approval'])
def test_unknown_schema_or_event_or_any_approval_sheet_stops_prepare(env,mode):
    if mode=='missing':env['event']=None
    elif mode=='incomplete':env['event'].pop('ChecklistSaved')
    elif mode=='schema_missing':env['schema'].pop()
    elif mode=='schema_duplicate':env['schema'].append(env['schema'][0])
    else:env['approvals']=[{'ApprovalSheetId':OTHER}]
    assert 'error' in prepare();t.request.assert_not_awaited()


@pytest.mark.parametrize('entity,field,value', [('Organizations','CollectESigForAllQualityTxns',True),
    ('Organizations','QualityESigTxns',[7788]),('Organizations','QualityESigTxns',None),
    ('Organizations','QualityESigTxns',[True]),('Organizations','QualityESigTxns',[100,100]),
    ('Organizations','UserRequiredForEsig',None),('Employees','IsFrozen',True),('Roles','IsFrozen',True),
    ('PriorityLevels','IsFrozen',None),('TriageSpecs','IsFrozen',True)])
def test_signatures_and_unknown_frozen_reference_states_are_not_bypassed(env,entity,field,value):
    env['roots'][entity][field]=value
    assert 'error' in prepare();t.request.assert_not_awaited()


@pytest.mark.parametrize('mode',['empty','duplicate_category','duplicate_id','frozen','wrong_parent','too_many'])
def test_organization_category_map_requires_a_single_exact_current_category(env,mode):
    if mode=='empty':env['maps']=[]
    elif mode=='duplicate_category':env['maps'].append({**env['maps'][0],'CategoryMapId':OTHER})
    elif mode=='duplicate_id':env['maps']*=2
    elif mode=='frozen':env['maps'][0]['IsFrozen']=True
    elif mode=='wrong_parent':env['maps'][0]['ParentId']=OTHER
    else:env['maps']*=101
    assert 'error' in prepare();t.request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('TriageSpecId',OTHER),('ToCategory',2),('OwnerId',OTHER),('RoleId',OTHER),
    ('CARRequired',True),('AutoRoute',True),('ProcessModelId',OTHER),('ChecklistTemplateId',OTHER),('IsFrozen',True),('ChangeCount',None)])
def test_configured_reassignment_escalation_car_or_workflow_is_not_cleared(env,field,value):
    env['details'][0][field]=value
    assert 'error' in prepare();t.request.assert_not_awaited()


def test_default_owner_role_may_explicitly_match_original_and_other_signature_txn_does_not_require_triage_signature(env):
    env['details'][0].update(OwnerId=OWNER,RoleId=ROLE)
    env['roots']['Organizations']['QualityESigTxns']=[100]
    assert prepare()['state']=='ready'


@pytest.mark.parametrize('field,value',[('AvailableActions',15),('AvailableActions',None),('IgnoreProcessModel',True),
    ('RouteProcessModel',True),('ProcessModelTemplate',{'InstanceId':OTHER}),('ChecklistTemplate',{'InstanceId':OTHER}),
    ('ESigRequirement',{'InstanceId':OTHER}),('AdditionalESigReqs',[{'InstanceId':OTHER}]),
    ('QualityObjectOwner',{'Name':'Another','InstanceId':OTHER}),('Category',2)])
def test_native_preview_cannot_silently_change_action_or_add_side_effects(env,field,value):
    env['hook']=lambda data:data.update({field:value})
    assert 'error' in prepare() and not p.PLAN_DB.exists()


@pytest.mark.parametrize('field,value',[('Status',7),('Category',2),('Owner',{'InstanceId':OTHER}),('PriorityLevel',{'InstanceId':OTHER}),
    ('IsApprovalRequired',True),('CompletionApprovalSheet',{'InstanceId':OTHER}),('EntryForApproval',{'InstanceId':OTHER}),
    ('CompletionApprovalStatus',1),('ProcessModel',{'InstanceId':OTHER}),('UserHasRole',False),('UserIsOwner',False),('TriageComplete',True)])
def test_current_native_quality_status_and_account_context_must_match(env,field,value):
    env['hook']=lambda data:data['QualityObjectDetail'].update({field:value})
    assert 'error' in prepare() and not p.PLAN_DB.exists()


def test_missing_signature_field_or_wrong_persistent_triage_detail_parent_fails(env):
    env['hook']=lambda data:data.pop('ESigRequirement')
    assert 'error' in prepare()
    env['hook']=lambda data:data['TriageSpecDetail']['Parent'].update(InstanceId=OTHER)
    assert 'error' in prepare()


def test_native_hex_reference_letter_case_is_the_same_identity(env):
    def upper(value):
        if isinstance(value,dict):
            for k,v in value.items():
                if k in ('InstanceId','InstanceID') and isinstance(v,str):value[k]=v.upper()
                else:upper(v)
        elif isinstance(value,list):
            for v in value:upper(v)
    env['hook']=upper
    assert prepare()['state']=='ready'


@pytest.mark.parametrize('what',['event','owner','role','priority','organization','map','detail','approval','ncr'])
def test_configuration_and_source_changes_before_execution_prevent_business_write(env,what):
    ready=prepare()
    if what=='event':env['event']['ChangeCount']=2
    elif what in ('owner','role','priority'):env['counters'][{'owner':OWNER,'role':ROLE,'priority':PRIORITY}[what]]=2
    elif what=='organization':env['roots']['Organizations']['QualityESigTxns']=[100]
    elif what=='map':env['maps'][0]['ChangeCount']=2
    elif what=='detail':env['details'][0]['ChangeCount']=2
    elif what=='approval':env['approvals']=[{'ApprovalSheetId':OTHER}]
    else:env['links']=[dict(NonconformanceReportId=NCR,EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1)]
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_late_event_change_during_preview_invalidates_preparation(env):
    env['hook']=lambda data:env['event'].update(ChangeCount=2)
    assert 'error' in prepare() and not p.PLAN_DB.exists()


@pytest.mark.parametrize('field',['qualityObjectOwner','qualityObjectRole','category','isCARRequiredToClose','triageComplete',
    'ignoreProcessModel','routeProcessModel','processESignatures','processModelTemplate','checklistTemplate','availableActions'])
def test_transport_cannot_override_preserved_fields_or_permissions(env,field):
    saved=snapshot(prepare());saved['body'][field]=False if field=='triageComplete' else True
    with pytest.raises(ValueError):p.transport(saved)


def test_transport_refuses_other_quality_service(env):
    saved=snapshot(prepare());saved['service']='OpenQualityObject'
    with pytest.raises(ValueError):p.transport(saved)


@pytest.mark.parametrize('mode',['timeout','cancel','http400'])
def test_attempt_failures_keep_unknown_and_cannot_retry(env,mode):
    ready=prepare()
    if mode=='http400':env['client'].request.return_value.status_code=400
    else:env['client'].request.side_effect=TimeoutError('uncertain') if mode=='timeout' else asyncio.CancelledError()
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:assert execute(ready)['state']=='unknown'
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'
    assert 'error' in execute(ready);env['client'].request.assert_awaited_once()


def test_inspection_reads_original_event_after_triage_without_claiming_success_or_following_name(env):
    ready=prepare();execute(ready);env['event'].update(TriageComplete=True,EventName='Renamed')
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['original_event_id']==EVENT and out['current_event']['TriageComplete'] is True
    assert not out['verified_success'] and not out['retry_allowed'] and not out['state_changed']
    assert t.q.get_quality_history.await_args.args==('quality_object','Renamed',EVENT)
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_event_name_is_escaped_and_explicit_id_does_not_get_guessed(env):
    env['event']['EventName']="E' --"
    ready=result(t.prepare_quality_event_triage("E' --",'Org'))
    assert ready['state']=='ready'
    query=next(c.args[0] for c in t.q.sql_rows.await_args_list if ' FROM Event WHERE ' in c.args[0])
    assert "EventName=N'E'' --'" in query


@pytest.mark.parametrize('mode',['overflow','duplicate','wrong_event','missing_container','unknown_count'])
def test_related_ncr_list_is_complete_and_cannot_hide_unknown_container_claims(env,mode):
    env['links']=[dict(NonconformanceReportId=NCR,EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1)]
    if mode=='overflow':env['links']*=21
    elif mode=='duplicate':env['links']*=2
    elif mode=='wrong_event':env['links'][0]['EscalatedEventId']=OTHER
    elif mode=='missing_container':env['links'][0]['ContainerId']=None
    else:env['links'][0]['ChangeCount']=None
    assert 'error' in prepare();t.request.assert_not_awaited()


def test_inspect_missing_original_event_does_not_follow_same_name_replacement(env):
    ready=prepare();execute(ready);env['event']=None
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['original_event_id']==EVENT and out['current_event'] is None and not out['verified_success']
    t.q.get_quality_history.assert_not_awaited()


@pytest.mark.parametrize('turns',[['查看质量事件首次分流条件'],['请完成事件首次分流'],
    ['查询分流上下文','继续'],['查看事件首次分流上下文','提交这个计划']])
def test_triage_routes_without_offering_unsupported_workflow_actions(turns):
    tools=[{'function':{'name':n}} for n in TOOL_POLICIES];messages=[]
    for text in turns:messages.extend([dict(role='user',content=text),dict(role='assistant',content='routing fixture only')])
    chosen={row['function']['name'] for row in select_tools(tools,messages)}
    assert {'get_quality_event_triage_context','prepare_quality_event_triage'}<=chosen
    assert ('execute_quality_transaction' in chosen)==(turns[-1].startswith('请完成') or turns[-1].startswith('提交'))
    assert '不以该工具改责任人或角色' in ROUTING_INSTRUCTION


def test_mcp_registered_schema_and_actual_confirmation(env):
    async def run():
        assert get_tool_func('prepare_quality_event_triage') is t.prepare_quality_event_triage
        async with Client(mcp) as client:
            schema=next(t.input_schema for t in await client.list_tools() if t.name=='prepare_quality_event_triage')
            assert schema['additionalProperties'] is False
            assert not {'owner','category','ignoreProcessModel','signerPassword'} & set(schema['properties'])
            reply=await client.call_tool('prepare_quality_event_triage',dict(event_name='E',organization_name='Org'))
            ready=json.loads(reply.content[0].text);assert ready['state']=='ready',ready
            calls=[dict(function=dict(name='execute_quality_transaction',arguments=json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})))]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval
            assert 'TriageQualityObject' in plan_preview(calls)
    asyncio.run(run());env['client'].request.assert_not_awaited()


def test_native_type_action_mask_and_column_mappings_are_not_invented():
    archive=json.loads(Path('docs/quality-fixtures/event-triage-native-category-map.json').read_text(encoding='utf-8'))
    event=next(r['result'] for r in archive['results'] if r['query']['kind']=='cdo' and r['query']['name']=='Event')
    assert event['CDODefId']==t.EVENT_TYPE
    columns={}
    for f in ('event-triage-native-columns.json','event-triage-native-category-map.json'):
        for r in json.loads((Path('docs/quality-fixtures')/f).read_text(encoding='utf-8'))['results']:
            if r['query']['kind']=='column':columns[r['query']['owner']]={c['name'] for c in r['result'].get('records',[])}
    nested=json.loads(Path('docs/quality-fixtures/event-triage-native-links.json').read_text(encoding='utf-8'))['metadata']
    for r in nested['results']:
        if r['query']['kind']=='column':columns[r['query']['owner']]={c['name'] for c in r['result'].get('records',[])}
    for row in json.loads(Path('docs/quality-fixtures/table-columns.json').read_text(encoding='utf-8')):
        columns.setdefault(row['TABLE_NAME'],set()).add(row['COLUMN_NAME'])
    assert all(set(cols.split(','))<=columns[table] for table,cols in t.SCHEMA.items())
    actions=json.loads(Path('docs/quality-fixtures/event-triage-native-actions.json').read_text(encoding='utf-8'))['results']
    definition=next(x['result'] for x in actions if x['query']['kind']=='field_type')
    assert '16 = Triage' in definition['Description'] and t.TRIAGE_ACTION==16
