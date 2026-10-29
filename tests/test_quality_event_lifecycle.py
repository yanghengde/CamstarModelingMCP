import asyncio
import copy
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from tools import mcp,get_tool_func,quality_event_lifecycle as l,quality_event_close_plan as c,quality_plans as p,quality_event_triage as t
from tests.test_quality_event_update import env as update_env
from tests.test_quality_event_triage import env as triage_env,EVENT,ORG,OWNER,ROLE,PRIORITY,OTHER,NCR,CONT,echo
from agent.safety import evaluate_mutations,MutationCounts,plan_preview

ORIGINAL_VALIDATOR=l.creation.validate_quality_event_creation

NEW_OWNER,NEW_ROLE,RULE,MAP,PROC,REF,TARGET,SHEET,CODE=[f'{n:016x}' for n in range(2001,2010)]
def result(coro):return json.loads(asyncio.run(coro))
def opened():return result(l.prepare_quality_event_open('E','Org'))
def reassigned(**kw):return result(l.prepare_quality_event_reassignment('E','Org','NewOwner','NewRole',**kw))
def created(**kw):return result(l.prepare_quality_event_creation('Org','Class','Sub','Normal','Issue','Reporter','Owner',
    '2026-10-29T10:00:00.1234567+08:00','2026-10-29T02:01:00Z',**kw))
def closed(**kw):return result(c.prepare_quality_event_closure('E','Org','Resolved','Resolved after review',allow_complex_context=True,**kw))
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))
def snap(ready):return result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']

@pytest.fixture
def env(update_env,monkeypatch):
    s=update_env;s['event']['TriageComplete']=True
    def obj(name,key):return dict(Name=name,InstanceID=key,IsFrozen=False)
    extra={'Employees':[obj('NewOwner',NEW_OWNER),obj('Owner',OWNER)],'Roles':[obj('NewRole',NEW_ROLE)],'QualityResolutionCodes':[obj('Resolved',CODE)]}
    for key in (NEW_OWNER,NEW_ROLE,RULE,CODE):s['counters'][key]=1
    base=t.ReadContext
    class ActorContext(base):
        async def get(self,entity,key=None,params=None):
            if key is None:
                for row in extra.get(entity,[]):
                    if "Name eq '"+row['Name']+"'" in params['$filter']:return {'value':[copy.deepcopy(row)]}
            for row in extra.get(entity,[]):
                if key==row['InstanceID']:return copy.deepcopy(row)
            return await super().get(entity,key,params)
    monkeypatch.setattr(t,'ReadContext',ActorContext)
    s.update(proc=None,refs=[],targets={},sheets=[],policy=dict(smtp_configured=False,entries=[],notification_effect_verified=False),
        numbering=dict(InstanceID=ORG,NumberingRuleMap=[dict(InstanceID=MAP,Category=1,NumberingRule={'InstanceId':RULE},IsFrozen=False)]),
        rule=obj('Rule',RULE)|dict(IsRollover=False,NumberingRuleType=1,SequenceLength=12,Prefix='"Q"',Suffix='""'),
        hook=None,contract_bad=False,lifecycle_requests=[])
    s['create_check']=dict(complete=True,static_valid=True,signature_required=False,errors=[],
        classification_mapping=dict(Owner=OWNER,Role=ROLE,ChecklistTemplate=None,FailureModeGroup=None),
        notification_policy=s['policy'],reporter=dict(entity='Employees',Name='Reporter',InstanceID=OTHER,IsFrozen=False,ChangeCount=1),
        references={k:dict(Name=name,InstanceID=key,IsFrozen=False,ChangeCount=1) for k,name,key in
            [('organization','Org',ORG),('classification','Class',REF),('subclassification','Sub',TARGET),('priority','Normal',PRIORITY)]})
    monkeypatch.setattr(l.creation,'validate_quality_event_creation',AsyncMock(side_effect=lambda **kw:json.dumps(copy.deepcopy(s['create_check']))))
    monkeypatch.setattr(l.creation,'notification_policy',AsyncMock(side_effect=lambda *a:copy.deepcopy(s['policy'])))
    monkeypatch.setattr(l.q,'closure_options',AsyncMock(return_value=[dict(Name='Close',Value='7')]))
    class NumberContext:
        def __init__(self,*a):pass
        async def get(self,entity,key,params):
            assert 'Password' not in str(params)
            return copy.deepcopy(s['numbering'] if entity=='Organizations' else s['rule'])
    monkeypatch.setattr(l,'ReadContext',NumberContext)
    base_sql=l.q.sql_rows
    async def sql(query,maximum):
        if 'INFORMATION_SCHEMA.COLUMNS' in query:
            return [dict(TABLE_NAME=table,COLUMN_NAME=col) for table,cols in re.findall(r"TABLE_NAME='(\w+)' AND COLUMN_NAME IN \((.*?)\)",query) for col in re.findall(r"'([^']+)'",cols)]
        if ' FROM ApprovalSheet WHERE ' in query:
            return copy.deepcopy(s['sheets'])
        if ' FROM ProcessModel WHERE ' in query:return [copy.deepcopy(s['proc'])] if s['proc'] else []
        if ' FROM QualityCrossReference WHERE ' in query:return copy.deepcopy(s['refs'])
        if ' FROM QualityObject WHERE ' in query:return [copy.deepcopy(s['targets'][query.split("='")[-1].rstrip("'")])]
        if ' FROM NumberingRule WHERE ' in query:return [dict(NumberingRuleId=RULE,ChangeCount=s['counters'][RULE])]
        return await base_sql(query,maximum)
    monkeypatch.setattr(l.q,'sql_rows',AsyncMock(side_effect=sql))
    contracts=json.loads(Path('docs/quality-fixtures/contracts.json').read_text(encoding='utf8'))
    async def transport(method,path,**kw):
        s['lifecycle_requests'].append((method,path,copy.deepcopy(kw)))
        if method=='GET':
            service=path.split('/')[2]
            if service=='ChangeOwner':
                # Explicit mock of fields found in native ChangeOwner metadata;
                # not a claimed current deployed Swagger capture.
                props=copy.deepcopy(contracts['Shopfloor/TriageQualityObject']['components']['schemas']['TriageQualityObject']['properties'])
                props.update(owner={'$ref':'#/components/schemas/NamedObjectRef'},role={'$ref':'#/components/schemas/NamedObjectRef'})
                doc={'components':{'schemas':{'ChangeOwner':{'properties':props}}}}
            else:doc=copy.deepcopy(contracts['Shopfloor/'+service])
            if s['contract_bad']:doc['components']['schemas'][service]['properties'].pop('organization')
            return json.dumps(doc)
        assert path in ('/api/OpenQualityObject','/api/ChangeOwner','/api/CloseQualityObject')
        assert kw['params']['execute']=='false'
        row=s['event'];data=echo(copy.deepcopy(kw['body']))
        data.update(AvailableActions=69,IgnoreProcessModel=False,ESigRequirement=None,AdditionalESigReqs=[],Employee={'InstanceId':OWNER},
            ApprovalRequired=False,ApprovalSheetMap=None,ApprovalSheetTemplate=None,ApprovalSheetStatus=None)
        data['QualityObjectDetail']=dict(Status=row['Status'],Category=row['Category'],Organization={'InstanceId':ORG},Owner={'InstanceId':OWNER},
            Role={'InstanceId':ROLE},PriorityLevel={'InstanceId':PRIORITY},TriageComplete=True,IsCARRequiredToClose=row['IsCARRequiredToClose'],
            IsApprovalRequired=False,UserIsOwner=True,UserHasRole=True,ProcessModel=None if not row['ProcessModelId'] else {'InstanceId':row['ProcessModelId']},
            CompletionApprovalStatus=None,CompletionApprovalSheet=None,EntryForApproval=None)
        if s['hook']:s['hook'](data)
        return json.dumps({'data':data})
    monkeypatch.setattr(l,'request',AsyncMock(side_effect=transport));monkeypatch.setattr(c,'request',AsyncMock(side_effect=transport))
    return s


def set_closed(s):s['event'].update(Status=5,CloseDateGMT='2026-10-28T01:00:00Z')
def add_car(s):
    s['event']['IsCARRequiredToClose']=True
    s['refs']=[dict(QualityCrossReferenceId=REF,TrackingId=EVENT,CrossReferenceId=TARGET,ChangeCount=1)]
    s['targets'][TARGET]=dict(Id=TARGET,Category=2,Status=5,Organization=ORG)
def add_workflow(s):
    s['event']['ProcessModelId']=PROC;s['proc']=dict(ProcessModelId=PROC,ParentId=EVENT,Stage=40,IsFrozen=False,ChangeCount=1)


def test_auto_creation_is_static_only_then_once_native_execution(env):
    ready=created();assert ready.get('state')=='ready',ready
    body=ready['request_preview']['body']
    assert 'eventName' not in body and body['submit'] is False
    assert body['serviceDetail']['initiator']['instanceId']==OWNER
    assert body['serviceDetail']['occurrenceDateGMT']=='2026-10-29T02:00:00.1234567Z'
    assert not ready['static_and_echo_verified'] and not ready['server_context']['native_preview_requested']
    assert all(method=='GET' for method,path,kw in env['lifecycle_requests'])
    assert execute(ready)['state']=='unknown';env['client'].request.assert_awaited_once()
    call=env['client'].request.await_args
    assert call.args[1].endswith('/api/CreateEvent') and call.kwargs['params']['execute']=='true'
    assert 'Password' not in call.kwargs['params']['$select']
    assert 'error' in execute(ready)


def test_open_keeps_old_close_information_and_native_context(env):
    set_closed(env);ready=opened();assert ready.get('state')=='ready',ready
    assert set(ready['request_preview']['body'])=={'qualityObject','organization','comments'}
    assert ready['before']['event']['CloseDateGMT'] and execute(ready)['state']=='unknown'
    assert env['client'].request.await_args.args[1].endswith('/api/OpenQualityObject')


def test_reassignment_has_explicit_both_targets_and_native_changeowner(env):
    ready=reassigned();assert ready.get('state')=='ready',ready
    body=ready['request_preview']['body'];assert body['owner']['instanceId']==NEW_OWNER and body['role']['instanceId']==NEW_ROLE and body['triageComplete'] is True
    assert execute(ready)['state']=='unknown' and env['client'].request.await_args.args[1].endswith('/api/ChangeOwner')

@pytest.mark.parametrize('op',['create','reassign'])
def test_notifications_are_explicit_opt_in_and_stale_policy_blocks(env,op):
    env['policy'].update(smtp_configured=True,entries=[dict(notification_type=1)])
    fn=created if op=='create' else reassigned
    assert 'error' in fn()
    ready=fn(allow_notifications=True);assert ready.get('state')=='ready',ready
    env['policy']['entries'].append(dict(notification_type=2))
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('kind',['event_open','event_reassign','event_close','event_triage','event_update'])
def test_unknown_creation_blocks_all_event_mutations_in_same_org(env,kind):
    ready=created();saved=snap(ready)
    other=dict(kind=kind,environment=p.environment(),scope_key='another',target_id=EVENT,name='E',before={'event':dict(OrganizationId=ORG)},body={},resource_writes=['runtime:quality-event:id:'+EVENT])
    assert p.conflicts(saved,other)
    other['before']['event']['OrganizationId']=OTHER
    assert not p.conflicts(saved,other)

@pytest.mark.parametrize('bad',['rollover','skipping','map_duplicate','map_missing','rule_frozen','invalid_length','unknown_policy','signature','fallback','checklist','invalid_contract'])
def test_creation_does_not_sneak_past_unsafe_or_unknown_context(env,bad):
    if bad=='rollover':env['rule']['IsRollover']=True
    elif bad=='skipping':env['rule']['NumberingRuleType']=0
    elif bad=='map_duplicate':env['numbering']['NumberingRuleMap']*=2
    elif bad=='map_missing':env['numbering']['NumberingRuleMap']=[]
    elif bad=='rule_frozen':env['rule']['IsFrozen']=True
    elif bad=='invalid_length':env['rule']['SequenceLength']=0
    elif bad=='unknown_policy':env['create_check']['complete']=False
    elif bad=='signature':env['create_check']['signature_required']=True
    elif bad=='fallback':env['create_check']['classification_mapping']['Owner']=None
    elif bad=='checklist':env['create_check']['classification_mapping']['ChecklistTemplate']=OTHER
    else:env['contract_bad']=True
    assert 'error' in created();env['client'].request.assert_not_awaited()
    assert all(method=='GET' for method,path,kw in env['lifecycle_requests'])

@pytest.mark.parametrize('op',['open','reassign'])
@pytest.mark.parametrize('bad',['wrong_status','eventdata','ncr','workflow','approval','signature'])
def test_existing_lifecycle_blocks_unsupported_cases(env,op,bad):
    if op=='open':set_closed(env)
    if bad=='wrong_status':env['event']['Status']=1 if op=='open' else 5
    elif bad=='eventdata':env['extra']['EventDataId']=OTHER
    elif bad=='ncr':env['links']=[dict(NonconformanceReportId=NCR,EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1)]
    elif bad=='workflow':add_workflow(env)
    elif bad=='approval':env['sheets']=[dict(ApprovalSheetId=SHEET,ParentId=EVENT,Status=40,ChangeCount=1)]
    else:env['roots']['Organizations']['QualityESigTxns']=[8412 if op=='open' else 7728]
    assert 'error' in (opened() if op=='open' else reassigned());env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('field,value',[('AvailableActions',1),('IgnoreProcessModel',True),('ESigRequirement',{}),('AdditionalESigReqs',[{}]),('Owner',{'InstanceId':OTHER})])
def test_reassignment_echo_masks_and_signatures_are_not_overridden(env,field,value):
    env['hook']=lambda d:d.update({field:value})
    assert 'error' in reassigned()

@pytest.mark.parametrize('field,value',[('UserIsOwner',False),('UserHasRole',False),('Status',5),('PriorityLevel',{'InstanceId':OTHER}),('EntryForApproval',{})])
def test_existing_service_context_is_pinned(env,field,value):
    env['hook']=lambda d:d['QualityObjectDetail'].update({field:value})
    assert 'error' in reassigned()


def test_complex_close_pins_car_workflow_approved_sheet_and_linked_container(env):
    add_car(env);add_workflow(env)
    env['sheets']=[dict(ApprovalSheetId=SHEET,ParentId=EVENT,Status=40,ChangeCount=1)]
    env['links']=[dict(NonconformanceReportId=NCR,EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1)]
    def approved(d):d['QualityObjectDetail'].update(CompletionApprovalSheet={'InstanceId':SHEET},CompletionApprovalStatus=40)
    env['hook']=approved
    ready=closed();assert ready.get('state')=='ready',ready
    saved=snap(ready);assert 'runtime:container:id:'+CONT in saved['resource_writes'] and 'runtime:quality-event:id:'+TARGET in saved['resource_writes']
    assert ready['before']['event']['IsCARRequiredToClose'] and not ready['server_context']['workflow_completion_verified']
    assert execute(ready)['state']=='unknown';env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/api/CloseQualityObject')

@pytest.mark.parametrize('bad',['no_car','wrong_org','deleted_target','pending_sheet','bad_process_parent','duplicate_refs','new_approval'])
def test_complex_close_never_bypasses_unknown_or_pending_context(env,bad):
    add_car(env);add_workflow(env)
    if bad=='no_car':env['refs']=[]
    elif bad=='wrong_org':env['targets'][TARGET]['Organization']=OTHER
    elif bad=='deleted_target':env['targets'][TARGET]['Status']=6
    elif bad=='pending_sheet':env['sheets']=[dict(ApprovalSheetId=SHEET,ParentId=EVENT,Status=0,ChangeCount=1)]
    elif bad=='bad_process_parent':env['proc']['ParentId']=OTHER
    elif bad=='duplicate_refs':env['refs']*=2
    else:env['hook']=lambda d:d.update(ApprovalRequired=True)
    assert 'error' in closed();env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('change',['source','workflow','refs','sheet'])
def test_stale_complex_close_cannot_commit(env,change):
    add_car(env);add_workflow(env);env['sheets']=[dict(ApprovalSheetId=SHEET,ParentId=EVENT,Status=40,ChangeCount=1)]
    ready=closed();assert ready.get('state')=='ready',ready
    if change=='source':env['event']['ChangeCount']=2
    elif change=='workflow':env['proc']['Stage']=20
    elif change=='refs':env['refs'][0]['ChangeCount']=2
    else:env['sheets'][0]['Status']=0
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('op',['create','open','reassign'])
@pytest.mark.parametrize('mode',['timeout','cancel','400'])
def test_attempts_are_unknown_once_only_and_never_replayed(env,op,mode):
    if op=='open':set_closed(env)
    fn={'create':created,'open':opened,'reassign':reassigned}[op]
    ready=fn();assert ready.get('state')=='ready',ready
    if mode=='400':env['client'].request.return_value.status_code=400
    else:env['client'].request.side_effect=TimeoutError('uncertain') if mode=='timeout' else asyncio.CancelledError()
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:assert execute(ready)['state']=='unknown'
    assert 'error' in execute(ready);env['client'].request.assert_awaited_once()

@pytest.mark.parametrize('field,value',[('eventName','existing draft'),('submit',True),('ignoreProcessModel',True),('serviceDetail',{})])
def test_creation_transport_rejects_named_draft_reuse_and_extra_fields(env,field,value):
    saved=snap(created());saved['body'][field]=value
    with pytest.raises(ValueError):p.transport(saved)


def test_missing_creation_id_never_guesses_candidate_by_name(env):
    ready=created();execute(ready)
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['candidate_id_from_execution_response'] is None and out['candidate_record'] is None and not out['verified_success']


def test_creation_response_id_remains_candidate_not_success(env):
    ready=created();env['client'].request.return_value.text=json.dumps({'data':{'QualityObject':{'InstanceId':EVENT}}});execute(ready)
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['candidate_id_from_execution_response']==EVENT and not out['verified_success'] and not out['retry_allowed']


def test_mcp_registration_and_confirmation_for_all_new_plans(env):
    async def check():
        async with Client(mcp) as client:
            rows={t.name:t for t in await client.list_tools()}
            for name in ('prepare_quality_event_creation','prepare_quality_event_open','prepare_quality_event_reassignment'):
                assert get_tool_func(name) is not None and rows[name].input_schema['additionalProperties'] is False
            ready=json.loads(await l.prepare_quality_event_reassignment('E','Org','NewOwner','NewRole'));calls=[dict(function=dict(name='execute_quality_transaction',arguments=json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})))]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval and 'ChangeOwner' in plan_preview(calls)
    asyncio.run(check())


@pytest.mark.parametrize('op',['open','reassign'])
def test_native_business_failures_are_not_reported_as_success(env,op):
    if op=='open':set_closed(env)
    ready=opened() if op=='open' else reassigned()
    env['client'].request.return_value.status_code=400
    env['client'].request.return_value.text='{"ErrorCode":"Native permission or assignee-role validation failed"}'
    out=execute(ready);assert out['state']=='unknown' and not out['verified_success']

@pytest.mark.parametrize('field,value',[('IsFrozen',True),('Stage',20),('ChangeCount',2)])
def test_process_context_change_after_ready_prevents_write(env,field,value):
    add_workflow(env);ready=closed();assert ready.get('state')=='ready',ready
    env['proc'][field]=value
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_capa_dependency_claim_conflicts_with_complex_closure(env):
    add_car(env);env['targets'][TARGET]['Category']=5
    saved=snap(closed())
    other=dict(kind='capa',environment=p.environment(),scope_key='capa:some',target_id=TARGET,body={},before={})
    assert p.conflicts(saved,other)


def test_auto_mode_validation_cannot_accept_named_event(env):
    # Exercise the original decorated callable retained in the registry rather
    # than the mock used to isolate lifecycle plan tests.
    fn=ORIGINAL_VALIDATOR
    out=result(fn(event_name='existing draft',automatic_numbering=True,organization_name='Org',classification_name='Class',
        subclassification_name='Sub',priority_level_name='Normal',brief_description='Issue',reporter_name='Reporter',
        occurrence_date='2026-10-29T01:00:00Z',reported_date='2026-10-29T02:00:00Z'))
    assert not out['complete'] and '不接受指定事件名称' in out['error']


@pytest.mark.parametrize('question,write',[
    ('查看事件开启条件',False),('请开启质量事件E',True),('请重新分派事件E',True),
    ('查询质量事件重新分派参数',False),('使用自动编号创建事件',True),('查看质量事件创建前置条件',False)])
def test_lifecycle_routes(question,write):
    from core.tool_catalog import TOOL_POLICIES
    from agent.tool_routing import select_tools,ROUTING_INSTRUCTION
    chosen={t['function']['name'] for t in select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[dict(role='user',content=question)])}
    assert {'prepare_quality_event_creation','prepare_quality_event_open','prepare_quality_event_reassignment'}<=chosen
    assert ('execute_quality_transaction' in chosen) is write
    assert '原生自动编号' in ROUTING_INSTRUCTION and 'allow_complex_context=true' in ROUTING_INSTRUCTION


def test_primary_metadata_proves_auto_numbering_pending_and_no_manual_open_cleanup():
    root=Path('docs/quality-fixtures')
    create=json.loads((root/'event-create-native-execution.json').read_text(encoding='utf8'))
    values=[p['value'] for c in create['CreateEvent_StartModifyingEntity']['calls'] for p in c['parameters']]
    assert 'Constants.QualityStatus.Pending' in values and 'QualityObject.IsSubmitted' in values
    values=[p['value'] for c in create['CreateEvent_GetEventName']['calls'] for p in c['parameters']]
    assert 'GetNextAssignedSequence' in values
    native=json.loads((root/'event-lifecycle-native-execution.json').read_text(encoding='utf8'))
    values=[p['value'] for c in native['clfs']['OpenQualityObject_ApplyDetails']['calls'] for p in c['parameters']]
    assert 'QualityObject.Status' in values and not any('CloseDate' in v for v in values)
    complex_meta=json.loads((root/'event-lifecycle-native-complex.json').read_text(encoding='utf8'))
    enum=next(x['result'] for x in complex_meta['results'] if x['query']['name']=='ApprovalStatusEnum')
    assert next(f['DefaultValue'] for f in enum['fields'] if f['name']=='Approved')=='40'


@pytest.mark.parametrize('kind',['event_create','event_open','event_reassign'])
def test_transport_rejects_service_swap(env,kind):
    if kind=='event_open':set_closed(env)
    saved=snap({'event_create':created,'event_open':opened,'event_reassign':reassigned}[kind]())
    saved['service']='UpdateEvent'
    with pytest.raises(ValueError):p.transport(saved)



def test_uppercase_related_ids_cannot_bypass_capa_dependency_claim(env):
    target='abcdef0000000001';link='abcdef0000000002'
    env['event']['IsCARRequiredToClose']=True
    env['refs']=[dict(QualityCrossReferenceId=link,TrackingId=EVENT,CrossReferenceId=target,ChangeCount=1)]
    env['targets'][target]=dict(Id=target.upper(),Category=5,Status=5,Organization=ORG.upper())
    ready=closed();assert ready.get('state')=='ready',ready
    saved=snap(ready);assert saved['before']['car_targets'][0]['Id']==target
    assert p.conflicts(saved,dict(kind='capa',target_id=target,scope_key='capa:C',environment=p.environment(),body={},before={}))
