"""Explicit isolated native transitions; no real DelegateTask/signature/email calls."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from fastmcp import Client
from tools import mcp,quality_approval_delegation as c,quality_approval_decisions as d,quality_plans as p,quality_approvals as a,quality_approval_history as h
from core import signature_channel,http_client,quality_signature_vault as vault_module
from tests.test_quality_approval_decisions import env as decision_env,actions_env,approval_env,base_env,prepare as decide
from tests.test_quality_approval_actions import EVENT,ORG,OWNER,ROLE,OTHER,SHEET,ENTRY,DECISION,execute,snap
from tests.test_quality_event_update import schema_fixture

REASON='000000000000e001';DELEGATION='000000000000e002';TXN='000000000000e003';MAIN='000000000000e004';SIGN='000000000000e005'
TOKEN='Bearer FAKE_DELEGATION_SESSION_ONLY';SECRET='FAKE_DELEGATE_SIGNATURE_ONLY'


def result(x):return json.loads(asyncio.run(x))
def prepare(**kw):return result(c.prepare_quality_approval_delegation(**(dict(name='E',organization_name='Org',approval_sheet_id=SHEET,
    approval_entry_id=ENTRY,approval_cycle_gmt='2026-10-29T01:00:00Z',target_employee_name='Delegate',delegation_reason_name='Absence',comments='Reviewed transfer',allow_notifications=True)|kw)))


@pytest.fixture
def env(decision_env,monkeypatch):
    s=decision_env;s.update(actor=OWNER,delegations=[],reason_version=1,source_authorized=True,target_authorized=True,
        declaration_bad=False,preview_hook=None,load_hook=None,submit_mode=None,submissions=[],sign_history=[],actor_private_token=TOKEN)
    client=SimpleNamespace(is_closed=False,build_request=lambda method,url,headers:httpx.Request(method,url,headers=headers))
    monkeypatch.setattr(signature_channel,'get_client',lambda:client)
    monkeypatch.setattr(http_client,'get_headers',lambda:{'Authorization':s['actor_private_token']})
    monkeypatch.setattr(p,'get_client',lambda:client)
    original_asset=d.t.asset
    async def asset(kind,**kw):
        if kind=='owner' and (kw.get('key')==OTHER or kw.get('name')=='Delegate'):
            return dict(entity='Employees',InstanceID=OTHER,Name='Delegate',IsFrozen=False,ChangeCount=s.get('target_version',1))
        return await original_asset(kind,**kw)
    monkeypatch.setattr(d.t,'asset',AsyncMock(side_effect=asset))
    original_get=c.ReadContext.get
    async def get(ctx,entity,key=None,params=None):
        if entity=='DelegationReasonCodes':
            r=dict(InstanceID=REASON,Name='Absence',IsFrozen=s.get('reason_frozen',False))
            return r if key else {'value':[r]}
        return await original_get(ctx,entity,key,params)
    monkeypatch.setattr(c.ReadContext,'get',get)
    original_sql=d.q.sql_rows
    async def sql(query,maximum):
        if 'FROM DelegateTaskHistory d JOIN HistoryMainline' in query:return copy.deepcopy(s['delegations'])
        if ' FROM DelegationReasonCode WHERE ' in query:return [dict(DelegationReasonCodeId=REASON,ChangeCount=s['reason_version'])]
        if ' FROM sys.objects ' in query:return [dict(FunctionId=81,DefinitionHash='a'*64)]
        if ' FROM EmployeeRole WHERE ' in query:
            employee=OTHER if OTHER in query else OWNER
            return [dict(EmployeeRoleId='000000000000e010',EmployeeId=employee,RoleId=ROLE,OrganizationId=ORG,PropagateToChildOrgs=True,IsFrozen=False,ChangeCount=s.get('grant_version',1))]
        if 'FROM dbo.csiAuthGetAllowedUsers(' in query:
            return [dict(RowCount=int(s['target_authorized'] if OTHER in query else s['source_authorized']))]
        if ' FROM SignApprovalHistory WHERE ' in query:return copy.deepcopy(s['sign_history'])
        if ' FROM ApprovalSheetHistory WHERE ' in query:return []
        return await original_sql(query,maximum)
    monkeypatch.setattr(d.q,'sql_rows',AsyncMock(side_effect=sql))
    async def transfer_request(method,path,**kw):
        if method=='GET':
            props={key:{'$ref':'#/components/schemas/'+kind} for key,kind in dict(trackableObject='BaseObjectRef',delegationTarget='BaseObjectRef',targetEmployee='NamedObjectRef',delegationReasonCode='NamedObjectRef').items()}
            props['comments']={'type':'string'}
            if s['declaration_bad']:props['delegationTarget']['$ref']='#/components/schemas/NamedObjectRef'
            return json.dumps({'components':{'schemas':{'DelegateTask':{'properties':props}}},'paths':{'/api/DelegateTask':{'post':{'requestBody':{'content':{'application/json':{'schema':{'$ref':'#/components/schemas/DelegateTask'}}}}}}}})
        assert path=='/api/DelegateTask' and kw['params']['execute']=='false'
        assert kw['bound_headers']['Authorization']==s['actor_private_token'] and kw['require_stable_session']
        from tests.test_quality_event_triage import echo
        data=echo(copy.deepcopy(kw['body']));data.update(Employee={'InstanceId':s['actor']},ESigRequirement=None,AdditionalESigReqs=[],ESigDetails=[],ProcessESignatures=True)
        if s['preview_hook']:s['preview_hook'](data)
        return json.dumps({'data':data})
    monkeypatch.setattr(c,'request',AsyncMock(side_effect=transfer_request))
    original_decision_request=d.request
    async def decision_request(method,path,**kw):
        if method=='GET' and s.get('signed'):
            schemas=copy.deepcopy(schema_fixture()['components']['schemas'])
            props={key:{'$ref':'#/components/schemas/'+kind} for key,kind in dict(qualityObject='NamedObjectRef',organization='NamedObjectRef',approvalSheet='NamedSubentityRef',approvalSheetEntry='SubentityRef',approvalDecision='NamedSubentityRef',qualityESigDetail='QualityESigDetail').items()}
            props['processESignatures']={'type':'boolean'};schemas['SignApproval']={'properties':props}
            schemas['SignApprovalPerformEventModel']={'properties':{'event':{'$ref':'#/components/schemas/EventToPerform'},'inputData':{'$ref':'#/components/schemas/SignApproval'}}}
            return json.dumps({'components':{'schemas':schemas},'paths':{'/api/SignApproval/PerformEvent':{'post':{'requestBody':{'content':{'application/json':{'schema':{'$ref':'#/components/schemas/SignApprovalPerformEventModel'}}}}}}}})
        if path.endswith('/PerformEvent'):
            from tests.test_quality_event_triage import echo
            data=echo(copy.deepcopy(kw['body']['inputData']));data.update(Employee={'InstanceId':s['actor']},AvailableActions=513,IgnoreProcessModel=False,ESigRequirement=None,AdditionalESigReqs=[],ESigDetails=[],
                QualityESigDetail=dict(UserRequired=True,Signer=None),QualityObjectDetail=dict(Status=s['event']['Status'],Category=s['event']['Category'],Organization={'InstanceId':ORG},EntryForApproval={'InstanceId':ENTRY},ProcessModel=None))
            return json.dumps({'data':data})
        return await original_decision_request(method,path,**kw)
    monkeypatch.setattr(d,'request',AsyncMock(side_effect=decision_request))
    s['decision_hook']=lambda data:data.update(Employee={'InstanceId':s['actor']})
    async def submit(method,url,**kw):
        assert method=='POST' and kw['params']['execute']=='true' and kw['follow_redirects'] is False
        s['submissions'].append((url,copy.deepcopy(kw['json']),dict(kw['headers'])))
        if s['submit_mode']=='timeout':raise TimeoutError(TOKEN)
        if s['submit_mode']=='cancel':raise asyncio.CancelledError()
        if s['submit_mode']=='400':return SimpleNamespace(status_code=400,text=TOKEN)
        if url.endswith('/DelegateTask'):
            assert 'employee' not in kw['json'] and 'sourceEmployee' not in kw['json']
            s['entries'][0].update(ApproverId=OTHER,ChangeCount=s['entries'][0]['ChangeCount']+1)
            obj=s['sheets'][0]['ParentId']
            s['delegations']=[dict(DelegateTaskHistoryId=DELEGATION,HistoryId=obj,HistoryMainlineId=MAIN,TxnId=TXN,DelegationTargetId=ENTRY,
                SourceEmployeeId=OWNER,TargetEmployeeId=OTHER,DelegationReasonCodeId=REASON,Comments=kw['json']['comments'],ChangeCount=1,
                TxnType=8682,TxnDateGMT='2026-10-29T02:00:00Z',EmployeeId=OWNER,ReversalStatus=0)]
        else:
            assert url.endswith('/SignApproval')
            if s.get('signed'):assert kw['json']['qualityESigDetail']['signerPassword']==SECRET and kw['json']['qualityESigDetail']['signer']['instanceId']==OTHER
            typ=s['decisions'][0]['DecisionType']
            s['entries'][0].update(Status=40 if typ==10 else 30,DecisionType=typ,ApprovalDecisionId=DECISION,ApprovedById=OTHER,LastDecisionId=SIGN,LastCompletedOnGMT='2026-10-29T03:00:00Z',Comments=kw['json']['comments'])
            row=dict.fromkeys(h.SCHEMA['SignApprovalHistory'].split(','));row.update(SignApprovalHistoryId=SIGN,ApprovalSheetId=SHEET,QualityObjectId=s['sheets'][0]['ParentId'],ApprovalSheetEntryId=ENTRY,
                ApprovalCycleGMT=s['sheets'][0]['LastApprovalCycleGMT'],ApproverId=OTHER,ApproverRoleId=ROLE,ApprovedById=OTHER,ApprovalDecisionId=DECISION,DecisionType=typ,SheetLevel=1,Comments=kw['json']['comments'],
                LastCompletedOnGMT='2026-10-29T03:00:00Z',HistoryId=TXN,HistoryMainlineId=MAIN,TxnId=TXN,ChangeCount=1)
            s['sign_history']=[row]
        return SimpleNamespace(status_code=200,text=TOKEN)
    client.request=AsyncMock(side_effect=submit);s['client']=client
    yield s


def accept_isolated_delegation(ready):
    # Synthetic human final attestation for fixture recovery, never a MES audit.
    out=result(p.review_quality_transaction(ready['plan_id'],ready['plan_digest'],'resolve','fixture://native-delegation-final',
        'Isolated simulated final transaction checked; not a real MES record',conclusion='committed'))
    assert 'error' not in out,out


@pytest.mark.parametrize('kind',['event','capa'])
@pytest.mark.parametrize('action,typ',[('approve',10),('reject',20)])
def test_one_transfer_current_delegate_signs_preserves_source_and_native_history(env,kind,action,typ):
    if kind=='capa':
        from tests.test_quality_approvals import CAPA
        env['sheets'][0]['ParentId']=CAPA;env['inquiry']['Category']=5;env['event']['Category']=5;env['capa']['IsSubmitted']=True;env['capa']['ProcessModelId']=None
    ready=prepare(object_kind=kind);assert ready.get('state')=='ready',ready
    assert not {'employee','user','sourceEmployee','approvalDecision'}&ready['request_preview']['body'].keys()
    before_cycle=env['sheets'][0]['LastApprovalCycleGMT']
    assert execute(ready)['state']=='unknown' and len(env['submissions'])==1
    evidence=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert evidence['delegation_history'][0]['SourceEmployeeId']==OWNER and not evidence['verified_success']
    assert 'error' in execute(ready)
    env['actor']=OTHER
    env['decisions'][0]['DecisionType']=typ
    sign=decide(action,object_kind=kind,allow_delegated_approval=True,delegation_history_id=DELEGATION)
    assert sign.get('state')=='ready',sign
    assert 'error' in execute(sign) and len(env['submissions'])==1 # original unknown occupancy
    accept_isolated_delegation(ready)
    assert execute(sign)['state']=='unknown' and len(env['submissions'])==2
    detail=snap(sign);assert detail['actor_authorization']['delegation_verified'] and detail['preview']['originally_assigned_approver_id']==OWNER
    assert env['sheets'][0]['LastApprovalCycleGMT']==before_cycle and env['sign_history'][0]['ApproverId']==OTHER and env['sign_history'][0]['ApprovedById']==OTHER
    assert env['sign_history'][0]['DecisionType']==typ
    persisted=result(p.get_quality_transaction_plan(sign['plan_id']))
    assert TOKEN not in json.dumps(persisted) and TOKEN.encode() not in p.PLAN_DB.read_bytes()


@pytest.mark.parametrize('mode',['notifications','same_person','role2','dated_task','old_assignment','wrong_cycle','already_signed','future_level','wrong_actor','source_unauthorized','target_unauthorized','frozen_reason','bad_contract','signature'])
def test_transfer_preconditions_fail_without_mutation(env,mode):
    kw={}
    if mode=='notifications':kw['allow_notifications']=False
    elif mode=='same_person':env['entries'][0]['ApproverId']=OTHER;env['entries'][0]['AssignedApproverId']=OTHER
    elif mode=='role2':env['entries'][0]['SubstituteOption']=2
    elif mode=='dated_task':env['entries'][0]['DelegationTaskId']=REASON
    elif mode=='old_assignment':env['entries'][0]['AssignedApproverId']=OTHER
    elif mode=='wrong_cycle':kw['approval_cycle_gmt']='2026-10-28T01:00:00Z'
    elif mode=='already_signed':env['entries'][0]['ApprovedById']=OWNER
    elif mode=='future_level':env['inquiry']['EntryForApproval']=None
    elif mode=='wrong_actor':env['actor']=OTHER
    elif mode=='source_unauthorized':env['source_authorized']=False
    elif mode=='target_unauthorized':env['target_authorized']=False
    elif mode=='frozen_reason':env['reason_frozen']=True
    elif mode=='bad_contract':env['declaration_bad']=True
    else:env['preview_hook']=lambda data:data.update(ESigRequirement={'InstanceId':REASON})
    assert 'error' in prepare(**kw);env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('change',['reason','target','role','cycle','actor'])
def test_late_changes_stop_original_plan(env,change):
    ready=prepare();assert ready.get('state')=='ready',ready
    if change=='reason':env['reason_version']+=1
    elif change=='target':env['target_version']=2
    elif change=='role':env['grant_version']=2
    elif change=='cycle':env['sheets'][0]['LastApprovalCycleGMT']='2026-10-29T01:00:01Z'
    else:env['actor']=OTHER
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['timeout','400','cancel'])
def test_unknown_transfer_never_retries_or_reflects_session(env,mode):
    ready=prepare();env['submit_mode']=mode
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:
        out=execute(ready);assert out['state']=='unknown' and TOKEN not in json.dumps(out)
    assert 'error' in execute(ready);env['client'].request.assert_awaited_once()
    assert TOKEN.encode() not in p.PLAN_DB.read_bytes()


@pytest.mark.parametrize('mode',['no_opt_in','no_opt_in_unassigned','wrong_history','duplicate','reversed','wrong_source','wrong_target','wrong_object','old_cycle','wrong_type','dated','role2','wrong_actor'])
def test_delegated_approval_requires_exact_persistent_transfer_evidence(env,mode):
    ready=prepare();execute(ready);accept_isolated_delegation(ready);env['actor']=OTHER
    kw=dict(allow_delegated_approval=True,delegation_history_id=DELEGATION)
    if mode=='no_opt_in':kw={}
    elif mode=='no_opt_in_unassigned':kw={};env['entries'][0]['AssignedApproverId']=None
    elif mode=='wrong_history':kw['delegation_history_id']=SIGN
    elif mode=='duplicate':env['delegations']*=2
    elif mode=='reversed':env['delegations'][0]['ReversalStatus']=1
    elif mode=='wrong_source':env['delegations'][0]['EmployeeId']=OTHER
    elif mode=='wrong_target':env['delegations'][0]['TargetEmployeeId']=OWNER
    elif mode=='wrong_object':env['delegations'][0]['HistoryId']=OTHER
    elif mode=='old_cycle':env['delegations'][0]['TxnDateGMT']='2026-10-28T02:00:00Z'
    elif mode=='wrong_type':env['delegations'][0]['TxnType']=1
    elif mode=='dated':env['entries'][0]['DelegationTaskId']=REASON
    elif mode=='role2':env['entries'][0]['SubstituteOption']=2
    else:env['actor']=OWNER
    assert 'error' in decide(**kw);assert len(env['submissions'])==1


def test_read_context_and_mcp_registration_closed_inputs(env):
    inventory=result(c.q.get_quality_api_inventory())
    family=next(x for x in inventory['families'] if x['domain']=='质量对象')
    assert 'prepare_quality_approval_delegation' in family['tools'] and family['standard_model_discovered']==['DelegateTask']
    candidates=result(c.q.list_quality_references('delegation_reason'))
    assert candidates['rows'][0]['Name']=='Absence'
    ready=prepare();execute(ready)
    out=result(a.get_quality_approval_context('E','Org',include_delegation_history=True,delegation_entry_id=ENTRY))
    assert out['delegation_context']['history'][0]['DelegateTaskHistoryId']==DELEGATION and not out['ready_for_execution']
    async def check():
        async with Client(mcp) as client:
            listing={x.name:x for x in await client.list_tools()};s=listing['prepare_quality_approval_delegation'].input_schema
            assert s['additionalProperties'] is False and not {'employee','password','source_employee','headers'}&s['properties'].keys()
            assert listing['prepare_quality_approval_decision'].input_schema['properties']['allow_delegated_approval']['default'] is False
            from core.tool_catalog import TOOL_POLICIES
            assert TOOL_POLICIES['execute_quality_transaction'].always_confirm
    asyncio.run(check())


def test_delegate_signature_binds_current_delegate_not_original_source(env,monkeypatch):
    ready=prepare();execute(ready);accept_isolated_delegation(ready);env['actor']=OTHER;env['signed']=True
    env['roots']['Organizations'].update(QualityESigTxns=[7839],UserRequiredForEsig=True)
    vault=vault_module.SignatureVault();vault.enable('http://127.0.0.1:8030');monkeypatch.setattr(vault_module,'VAULT',vault)
    try:
        sign=decide(allow_delegated_approval=True,delegation_history_id=DELEGATION,require_quality_esign=True)
        assert sign.get('state')=='ready',sign
        token=sign['signature_input_url'].split('#')[1];data,cookie=vault.bootstrap(token)
        assert data['display']['原生转派历史ID']==DELEGATION and data['display']['当前审批人ID']==OTHER
        vault.capture(token,cookie,data['csrf'],SECRET,True)
        assert execute(sign)['state']=='unknown'
        assert env['submissions'][-1][1]['qualityESigDetail']['signer']['instanceId']==OTHER
        assert SECRET.encode() not in p.PLAN_DB.read_bytes()
    finally:vault.disable()


def test_post_transfer_journal_failure_remains_unknown_and_keeps_inflight(env,monkeypatch):
    import sqlite3
    ready=prepare();original=p.db_connect;failed=[False]
    def database():
        if env['submissions'] and not failed[0]:
            failed[0]=True;raise sqlite3.OperationalError(TOKEN)
        return original()
    monkeypatch.setattr(p,'db_connect',database)
    out=execute(ready)
    assert out['submitted'] is None and out['state']=='unknown' and TOKEN not in json.dumps(out)
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='in_flight'
    assert 'error' in execute(ready) and len(env['submissions'])==1


def test_redelegation_and_missing_history_do_not_create_a_second_transfer(env):
    first=prepare();execute(first);accept_isolated_delegation(first);env['actor']=OTHER
    env['entries'][0]['AssignedApproverId']=None # Native may leave the field empty; history still blocks chaining.
    assert 'error' in prepare() and len(env['submissions'])==1


def test_delegation_context_rejects_partial_or_oversupplied_history(env):
    first=prepare();execute(first)
    env['delegations']*=51
    out=result(a.get_quality_approval_context('E','Org',include_delegation_history=True,delegation_entry_id=ENTRY))
    assert not out['complete'] and not out['ready_for_execution']


def test_unassigned_original_with_no_current_cycle_transfer_keeps_normal_named_path(env):
    env['entries'][0]['AssignedApproverId']=None
    ready=decide();assert ready.get('state')=='ready',ready
    assert snap(ready)['before']['unassigned_transfer_check']==[]


def test_mcp_transfer_prepare_dispatches_and_requires_executor_confirmation(env):
    from tools import get_tool_func
    from agent.safety import evaluate_mutations,MutationCounts,plan_preview
    async def check():
        async with Client(mcp) as client:
            reply=await client.call_tool('prepare_quality_approval_delegation',dict(name='E',organization_name='Org',approval_sheet_id=SHEET,
                approval_entry_id=ENTRY,approval_cycle_gmt='2026-10-29T01:00:00Z',target_employee_name='Delegate',delegation_reason_name='Absence',comments='Reviewed',allow_notifications=True))
            ready=json.loads(reply.content[0].text);assert ready.get('state')=='ready',ready
            calls=[dict(function=dict(name='execute_quality_transaction',arguments=json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})))]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval and 'DelegateTask' in plan_preview(calls)
    assert get_tool_func('prepare_quality_approval_delegation') is not None
    asyncio.run(check());env['client'].request.assert_not_awaited()
