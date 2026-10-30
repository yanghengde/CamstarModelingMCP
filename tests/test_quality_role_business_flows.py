"""Role substitution from configured template through revision/new cycle and history.

All MES transitions and final human attestations are explicit isolated simulations.
"""
import asyncio
import copy
import json
from unittest.mock import AsyncMock
import pytest
from tools import quality_approval_authorization as auth,quality_approval_assignment as assignment,quality_approval_actions as action
from tools import quality_approval_decisions as decision,quality_event_update as revision,quality_approval_history as history,quality_plans as p
from tests.test_quality_approval_business_flows import env as business_env,assignment_env,actions_env,approval_env,base_env
from tests.test_quality_approval_business_flows import simulated_route,simulated_decision,simulated_cancel,attest,execute,snap,EVENT,ORG,OWNER,ROLE,OTHER,SHEET,ENTRY,DECISION,REJECT


def result(x):return json.loads(asyncio.run(x))


@pytest.fixture
def env(business_env,monkeypatch):
    s=business_env;s['template_entries'][0]['SubstituteOption']=2;s['actor']=OWNER
    monkeypatch.setattr(auth.http,'get_headers',lambda:dict(Authorization='Bearer FAKE_SESSION_FOR_'+s['actor']))
    monkeypatch.setattr(p,'get_headers',lambda:auth.http.get_headers())
    original=assignment.q.sql_rows
    async def sql(query,maximum):
        if ' FROM sys.objects ' in query:return [dict(FunctionId=91,DefinitionHash='a'*64)]
        if ' FROM EmployeeRole WHERE ' in query:
            return [dict(EmployeeRoleId='000000000000b001',EmployeeId=OTHER,RoleId=ROLE,OrganizationId=ORG,IsFrozen=False,
                PropagateToChildOrgs=True,ChangeCount=1)]
        if 'FROM dbo.csiAuthGetAllowedUsers(' in query:return [dict(RowCount=1)]
        return await original(query,maximum)
    monkeypatch.setattr(assignment.q,'sql_rows',AsyncMock(side_effect=sql))
    return s


def assign(s):
    s['actor']=OWNER
    return result(assignment.prepare_quality_approval_assignment('E','Org','Completion','QRC','Review proposed resolution','Start completion approval',
        allow_role_substitution=True))


def sign(s,index=0,typ=10,allow=True):
    e=s['entries'][index];s['actor']=OTHER if e['SubstituteOption']==2 else OWNER
    s['inquiry']['EntryForApproval']={'InstanceId':e['ApprovalSheetEntryId']}
    return result(decision.prepare_quality_approval_decision('approve' if typ==10 else 'reject','E','Org',SHEET,e['ApprovalSheetEntryId'],
        DECISION if typ==10 else REJECT,'Act in my authorized role',allow_role_substitution=allow))


@pytest.mark.parametrize('origin',['reject','cancel'])
def test_role_template_revision_resubmit_and_actual_signer_history(env,origin):
    ready=assign(env);assert ready.get('state')=='ready',ready
    assert ready['request_preview']['body']['approvalSheetDetails']['approvalEntries'][0]['substituteOption']==2
    execute(ready);simulated_route(env,first=True);attest(ready)
    old=env['sheets'][0]['LastApprovalCycleGMT']
    assert 'error' in sign(env,allow=False)
    if origin=='reject':
        ready=sign(env,typ=20);assert ready.get('state')=='ready',ready
        comments=snap(ready)['body']['comments'];assert execute(ready)['state']=='unknown'
        assert env['client'].request.await_args.kwargs['headers']['Authorization']=='Bearer FAKE_SESSION_FOR_'+OTHER
        simulated_decision(env,0,20,comments);attest(ready)
        assert env['sign_history'][0]['ApproverId']==OWNER and env['sign_history'][0]['ApprovedById']==OTHER
    else:
        env['actor']=OWNER
        ready=result(action.prepare_quality_approval_action('cancel','E','Org',SHEET,'Cancel before revision',allow_role_substitution=True))
        assert ready.get('state')=='ready',ready
        execute(ready);simulated_cancel(env);attest(ready)
    original=copy.deepcopy(env['sign_history']);env['actor']=OWNER
    ready=result(revision.prepare_quality_event_update('E','Org',description='Revised role-reviewed evidence',approval_sheet_id=SHEET,
        previous_cycle_gmt=old,comments='Revise rejected evidence',allow_role_substitution=True))
    assert ready.get('state')=='ready',ready
    assert 'approvalSheetDetails' not in ready['request_preview']['body']
    execute(ready);env['extra']['Description']='Revised role-reviewed evidence';env['event']['ChangeCount']+=1;attest(ready)
    ready=result(action.prepare_quality_approval_action('resubmit','E','Org',SHEET,'Resubmit role approval',previous_cycle_gmt=old,allow_role_substitution=True))
    assert ready.get('state')=='ready',ready
    execute(ready);simulated_route(env);attest(ready)
    assert env['entries'][0]['SubstituteOption']==2 and env['entries'][0]['ApproverId']==OWNER
    ready=sign(env);assert ready.get('state')=='ready',ready
    comments=snap(ready)['body']['comments'];execute(ready);simulated_decision(env,0,10,comments);attest(ready)
    out=result(history.get_quality_approval_history('E','Org',SHEET));assert out['complete'] and len(out['cycles'])==2
    current=out['cycles'][0]['decision_history'][0]
    assert current['ApproverId']==OWNER and current['ApprovedById']==OTHER and current['DecisionType']==10
    assert env['sign_history'][:1]==original and env['client'].request.await_count==5
    assert not out['transaction_attribution_verified']


def test_mixed_named_and_role_entries_keep_independent_identity_rules(env):
    env['template_entries'][0]['SubstituteOption']=1
    second=copy.deepcopy(env['template_entries'][0]);second.update(ApprovalSheetEntryId='000000000000b002',SubstituteOption=2)
    env['template_entries'].append(second)
    ready=assign(env);assert ready.get('state')=='ready',ready
    assert [e['substituteOption'] for e in ready['request_preview']['body']['approvalSheetDetails']['approvalEntries']]==[1,2]
    execute(ready);simulated_route(env,first=True);attest(ready)
    for index in (0,1):
        ready=sign(env,index);assert ready.get('state')=='ready',ready
        assert ready['server_context']['signature_actor_policy']==('named' if index==0 else 'role')
        comments=snap(ready)['body']['comments'];execute(ready);simulated_decision(env,index,10,comments);attest(ready)
    out=result(history.get_quality_approval_history('E','Org',SHEET));assert out['complete']
    records=out['cycles'][0]['decision_history']
    assert {(r['ApproverId'],r['ApprovedById']) for r in records}=={(OWNER,OWNER),(OWNER,OTHER)}
