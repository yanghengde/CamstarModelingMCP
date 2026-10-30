"""Role2 uses the native authorization function and a private pinned transport session."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock
from types import SimpleNamespace
import httpx
import pytest
from fastmcp import Client
from tools import mcp,quality_approval_decisions as d,quality_approval_authorization as auth,quality_approval_history as h,quality_plans as p
from tests.test_quality_approval_decisions import env as decision_env,actions_env,approval_env,base_env,prepare as decide
from tests.test_quality_approval_actions import execute,snap,EVENT,ORG,OWNER,ROLE,OTHER,SHEET,ENTRY,DECISION

GRANT='000000000000a001'
SIGN='000000000000a002'
ROUTE='000000000000a003'
TOKEN='Bearer FAKE_PRIVATE_TRANSPORT_BINDING_ONLY'


def result(x):return json.loads(asyncio.run(x))
def prepare(**kw):return decide(**({'allow_role_substitution':True}|kw))


@pytest.fixture
def env(decision_env,monkeypatch):
    s=decision_env;s['entries'][0]['SubstituteOption']=2
    s.update(current_actor=OTHER,auth_count=1,auth_definition=[dict(FunctionId=81,DefinitionHash='a'*64)],
        grants=[dict(EmployeeRoleId=GRANT,EmployeeId=OTHER,RoleId=ROLE,OrganizationId=ORG,PropagateToChildOrgs=True,IsFrozen=False,ChangeCount=1)],
        auth_queries=[],auth_hook=None,actor_frozen=False,private_token=TOKEN,private_cookie='FAKE_PRIVATE_COOKIE_ONLY',role_history=[],sheet_history=[])
    def native_actor(data):data['Employee']={'InstanceId':s['current_actor'],'Password':'FAKE_EMPLOYEE_SECRET_DO_NOT_KEEP'}
    s['decision_hook']=native_actor
    monkeypatch.setattr(auth.http,'get_headers',lambda:dict(Authorization=s['private_token'],Accept='application/json',**{'Content-Type':'application/json'}))
    def build_request(method,url,headers):return httpx.Request(method,url,headers=headers|{'Cookie':s['private_cookie']})
    monkeypatch.setattr(auth.http,'get_client',lambda:SimpleNamespace(build_request=build_request))
    original=d.t.asset
    async def asset(kind,**kw):
        if kind=='owner' and kw.get('key')==OTHER:
            if s['actor_frozen']:raise ValueError('当前员工已冻结。')
            return dict(entity='Employees',InstanceID=OTHER,Name='Current role approver',IsFrozen=False,ChangeCount=1)
        return await original(kind,**kw)
    monkeypatch.setattr(d.t,'asset',AsyncMock(side_effect=asset))
    original_sql=d.q.sql_rows
    async def sql(query,maximum):
        if ' FROM sys.objects ' in query:return copy.deepcopy(s['auth_definition'])
        if ' FROM EmployeeRole WHERE ' in query:return copy.deepcopy(s['grants'])
        if 'FROM dbo.csiAuthGetAllowedUsers(' in query:
            s['auth_queries'].append(query)
            if s['auth_hook']:s['auth_hook']()
            return [dict(RowCount=s['auth_count'])]
        if ' FROM ApprovalSheetHistory WHERE ' in query:return copy.deepcopy(s['sheet_history'])
        if ' FROM SignApprovalHistory WHERE ' in query:return copy.deepcopy(s['role_history'])
        return await original_sql(query,maximum)
    monkeypatch.setattr(d.q,'sql_rows',AsyncMock(side_effect=sql))
    return s


@pytest.mark.parametrize('action,typ',[('approve',10),('reject',20)])
def test_authorized_current_role_actor_is_distinct_from_original_and_submits_once(env,action,typ):
    env['decisions'][0]['DecisionType']=typ
    ready=decide(action,allow_role_substitution=True);assert ready.get('state')=='ready',ready
    saved=snap(ready);preview=ready['server_context']
    assert preview['employee_id']==OTHER and preview['original_approver_id']==OWNER
    assert preview['signature_actor_policy']=='role' and preview['role_organization_membership_verified']
    assert saved['actor_authorization']['actor']['InstanceID']==OTHER
    assert saved['actor_authorization']['employee_roles'][0]['EmployeeRoleId']==GRANT
    assert not {'employee','password','user','approvingFor','processESignatures'}&saved['body'].keys()
    assert "dbo.csiAuthGetAllowedUsers(N'Org',N'Role')" in env['auth_queries'][0]
    assert execute(ready)['state']=='unknown';env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.kwargs['headers']['Authorization']==TOKEN
    assert 'error' in execute(ready)


def test_role_policy_is_never_silently_enabled_or_used_to_override_named_approver(env):
    assert 'error' in decide();env['client'].request.assert_not_awaited()
    env['entries'][0]['SubstituteOption']=1
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['not_authorized','bad_count','missing_function','opaque_function','duplicate_function','frozen_actor',
    'other_employee_grant','missing_column','duplicate_grant','too_many_grants','unknown_option','delegation','no_session','unknown_current_entry'])
def test_role_or_identity_evidence_cannot_be_guessed(env,mode):
    if mode=='not_authorized':env['auth_count']=0
    elif mode=='bad_count':env['auth_count']=True
    elif mode=='missing_function':env['auth_definition']=[]
    elif mode=='opaque_function':env['auth_definition'][0]['DefinitionHash']=None
    elif mode=='duplicate_function':env['auth_definition']*=2
    elif mode=='frozen_actor':env['actor_frozen']=True
    elif mode=='other_employee_grant':env['grants'][0]['EmployeeId']=OWNER
    elif mode=='missing_column':env['grants'][0].pop('PropagateToChildOrgs')
    elif mode=='duplicate_grant':env['grants']*=2
    elif mode=='too_many_grants':env['grants']*=101
    elif mode=='unknown_option':env['entries'][0]['SubstituteOption']=3
    elif mode=='delegation':env['entries'][0]['DelegationTaskId']=GRANT
    elif mode=='no_session':env['private_token']=''
    else:env['inquiry']['EntryForApproval']=None
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('change',['grant','authorization','definition','actor','scope','signature','policy'])
def test_late_authorization_identity_or_scope_change_stops_submission(env,change):
    ready=prepare();assert ready.get('state')=='ready',ready
    if change=='grant':env['grants'][0]['ChangeCount']+=1
    elif change=='authorization':env['auth_count']=0
    elif change=='definition':env['auth_definition'][0]['DefinitionHash']='b'*64
    elif change=='actor':env['current_actor']=OWNER
    elif change=='scope':env['roots']['Roles']['Name']='Changed role'
    elif change=='signature':env['roots']['Organizations']['QualityESigTxns']=[7839]
    else:env['entries'][0]['SubstituteOption']=1
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_live_session_change_after_preview_cannot_change_submitting_identity(env):
    ready=prepare();assert ready.get('state')=='ready',ready
    # During execution's fresh authorization reads the process switches configuration.
    # The outgoing write must still use the exact session that returned this actor.
    env['auth_hook']=lambda:env.update(private_token='Bearer DIFFERENT_FAKE_SESSION',private_cookie='FAKE_DIFFERENT_COOKIE')
    assert execute(ready)['state']=='unknown'
    assert env['private_token']!=TOKEN
    assert env['client'].request.await_args.kwargs['headers']['Authorization']==TOKEN
    assert env['client'].request.await_args.kwargs['headers']['Cookie']=='FAKE_PRIVATE_COOKIE_ONLY'
    assert all(call.kwargs['bound_headers']['Authorization']==TOKEN for call in d.request.await_args_list if call.args[0]=='POST')


def test_credentials_and_overreturned_employee_fields_are_not_in_plans_or_database(env):
    env['grants'][0]['Password']='FAKE_GRANT_SECRET_DO_NOT_KEEP'
    ready=prepare();assert ready.get('state')=='ready',ready
    execute(ready)
    persisted=json.dumps(result(p.get_quality_transaction_plan(ready['plan_id'])))
    for secret in (TOKEN,'FAKE_PRIVATE_COOKIE_ONLY','FAKE_EMPLOYEE_SECRET_DO_NOT_KEEP','FAKE_GRANT_SECRET_DO_NOT_KEEP'):
        assert secret not in json.dumps(ready) and secret not in persisted and secret.encode() not in p.PLAN_DB.read_bytes()


@pytest.mark.parametrize('mode',['response','exception'])
def test_reflected_transport_credentials_are_not_kept_as_raw_evidence(env,mode):
    ready=prepare();assert ready.get('state')=='ready',ready
    reflected=TOKEN+' session=FAKE_PRIVATE_COOKIE_ONLY'
    if mode=='response':env['client'].request.return_value.text=reflected
    else:env['client'].request.side_effect=RuntimeError(reflected)
    out=execute(ready);assert out['state']=='unknown'
    assert reflected not in json.dumps(out) and reflected.encode() not in p.PLAN_DB.read_bytes()
    if mode=='response':assert out['response_capture']=='omitted_for_role_session_privacy' and out['raw_response'] is None
    else:assert out['error_type']=='RuntimeError'


@pytest.mark.parametrize('mode',['timeout','400','cancel'])
def test_unknown_role_decision_is_not_retried_or_unlocked(env,mode):
    ready=prepare();assert ready.get('state')=='ready',ready
    if mode=='400':env['client'].request.return_value.status_code=400
    else:env['client'].request.side_effect=TimeoutError('test timeout') if mode=='timeout' else asyncio.CancelledError()
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:assert execute(ready)['state']=='unknown'
    assert 'error' in execute(ready);env['client'].request.assert_awaited_once()


def test_role_authorization_cannot_substitute_for_required_electronic_signature(env):
    env['decision_hook']=lambda data:data.update(Employee={'InstanceId':OTHER},ESigRequirement={'Name':'Required'})
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


def test_history_keeps_original_and_actual_signer_after_simulated_role_decision(env):
    ready=prepare();assert ready.get('state')=='ready',ready
    assert execute(ready)['state']=='unknown'
    entry=env['entries'][0];cycle=env['sheets'][0]['LastApprovalCycleGMT'];when='2026-10-30T01:00:00Z'
    entry.update(Status=40,DecisionType=10,ApprovalDecisionId=DECISION,ApprovedById=OTHER,LastDecisionId=SIGN,LastCompletedOnGMT=when,
                 Comments=snap(ready)['body']['comments'])
    env['sheets'][0]['Status']=40;env['inquiry'].update(CompletionApprovalStatus=40,EntryForApproval=None)
    row=dict.fromkeys(h.SCHEMA['SignApprovalHistory'].split(','));row.update(SignApprovalHistoryId=SIGN,ApprovalSheetId=SHEET,QualityObjectId=EVENT,
        ApprovalSheetEntryId=ENTRY,ApprovalCycleGMT=cycle,ApproverId=OWNER,ApproverRoleId=ROLE,ApprovedById=OTHER,ApprovalDecisionId=DECISION,
        DecisionType=10,SheetLevel=1,Comments=entry['Comments'],LastCompletedOnGMT=when,ChangeCount=1,HistoryId=SIGN,TxnId=SIGN)
    env['role_history']=[row]
    row=dict.fromkeys(h.SCHEMA['ApprovalSheetHistory'].split(','));row.update(ApprovalSheetHistoryId=ROUTE,ApprovalSheetId=SHEET,
        QualityObjectId=EVENT,ApprovalCycleGMT=cycle,Status=10,ChangeCount=1,HistoryId=ROUTE,TxnId=ROUTE)
    env['sheet_history']=[row]
    out=result(h.get_quality_approval_history('E','Org',SHEET));assert out['complete'],out
    record=out['cycles'][0]['decision_history'][0]
    assert record['ApproverId']==OWNER and record['ApprovedById']==OTHER and not out['transaction_attribution_verified']
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_actual_mcp_schema_defaults_to_named_actor_and_has_no_identity_override(env):
    async def check():
        async with Client(mcp) as client:
            tool=next(t for t in await client.list_tools() if t.name=='prepare_quality_approval_decision')
            assert tool.input_schema['properties']['allow_role_substitution']['default'] is False
            assert not tool.input_schema['additionalProperties']
            assert not {'employee','user','password','headers','bound_headers','signature'}&tool.input_schema['properties'].keys()
    asyncio.run(check())


def test_native_function_handles_inherited_authorization_without_direct_role_guessing(env):
    env['grants'][0]['OrganizationId']=OWNER
    ready=prepare();assert ready.get('state')=='ready',ready
    assert ready['server_context']['role_organization_membership_verified']
    env['auth_count']=0
    assert 'error' in prepare()


@pytest.mark.parametrize('submitted',[False,'false',0,'0',None,'unknown'])
def test_capa_pending_or_unknown_submission_cannot_be_treated_as_truthy_text(env,submitted):
    from tests.test_quality_approvals import CAPA
    env['capa'].update(IsSubmitted=submitted,ProcessModelId=None)
    env['sheets'][0]['ParentId']=CAPA;env['inquiry']['Category']=5
    out=decide(object_kind='capa',allow_role_substitution=True)
    assert 'error' in out;d.request.assert_not_awaited();env['client'].request.assert_not_awaited()
