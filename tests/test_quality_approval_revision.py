"""Revision uses UpdateEvent text only; approval configuration is read, never replayed."""
import asyncio
import copy
import json
import re
from unittest.mock import AsyncMock

import pytest
from tools import quality_event_update as u, quality_approvals as a, quality_approval_history as h
from tests.test_quality_event_update import env as update_env, triage_env, EVENT, ORG, OWNER, ROLE, OTHER, prepare as update, execute, snap
from tests.test_quality_approvals import SHEET, ENTRY, DECISION
from tests.test_quality_approval_history import OLD, CYCLE, HISTORY, SIGN


@pytest.fixture
def env(update_env,monkeypatch):
    s=update_env;s['event']['IsSubmitted']=True;s['event']['Status']=8
    entry=dict(ApprovalSheetEntryId=ENTRY,ParentId=SHEET,Status=30,DecisionType=20,ApproverId=OWNER,ApproverRoleId=ROLE,
        ApprovedById=OWNER,ApprovalDecisionId=DECISION,LastDecisionId=SIGN,LastCompletedOnGMT=CYCLE,SheetLevel=1,IsFrozen=False,SubstituteOption=1,Comments=None,DelegationTaskId=None)
    sheet=dict(ApprovalSheetId=SHEET,Status=30,IsFrozen=False,LastApprovalCycleGMT=OLD,entries=[entry])
    context=dict(target=dict(Id=EVENT,Status=8,ChangeCount=s['event']['ChangeCount']),sheets=[sheet],current_employee_entry_id=None,
        current_context=dict(IsApprovalRequired=True,CompletionApprovalSheet={'instance_id':SHEET},CompletionApprovalStatus=30,EntryForApproval=None))
    s.update(approval_context=context,approval_load_hook=None,approval_preview_hook=None)
    def native_context():return copy.deepcopy(s['approval_context'])
    monkeypatch.setattr(a,'read_context',AsyncMock(side_effect=lambda *x:native_context()))
    route=dict.fromkeys(h.SCHEMA['ApprovalSheetHistory'].split(','));route.update(ApprovalSheetHistoryId=HISTORY,
        ApprovalSheetId=SHEET,QualityObjectId=EVENT,ApprovalSheetEntryId=ENTRY,ApprovalCycleGMT=OLD,Status=10,
        TxnId=OTHER,HistoryId=OTHER,HistoryMainlineId=OTHER,ChangeCount=1)
    sign=dict.fromkeys(h.SCHEMA['SignApprovalHistory'].split(','));sign.update(SignApprovalHistoryId=SIGN,
        ApprovalSheetId=SHEET,QualityObjectId=EVENT,ApprovalSheetEntryId=ENTRY,ApprovalCycleGMT=OLD,DecisionType=20,
        ApproverId=OWNER,ApproverRoleId=ROLE,ApprovedById=OWNER,ApprovalDecisionId=DECISION,SheetLevel=1,
        LastCompletedOnGMT=CYCLE,TxnId=OTHER,HistoryId=OTHER,HistoryMainlineId=OTHER,ChangeCount=1)
    s.update(route_history=[route],sign_history=[sign])
    original=u.q.sql_rows
    async def sql(query,maximum):
        if 'INFORMATION_SCHEMA.COLUMNS' in query:
            return [dict(TABLE_NAME=t,COLUMN_NAME=c) for t,cols in re.findall(r"TABLE_NAME='(\w+)' AND COLUMN_NAME IN \((.*?)\)",query) for c in re.findall(r"'([^']+)'",cols)]
        if ' FROM ApprovalSheetHistory WHERE ' in query:return copy.deepcopy(s['route_history'])
        if ' FROM SignApprovalHistory WHERE ' in query:return copy.deepcopy(s['sign_history'])
        return await original(query,maximum)
    monkeypatch.setattr(u.q,'sql_rows',AsyncMock(side_effect=sql))
    def adjust(data,loaded):
        ctx=s['approval_context']['current_context'];detail=data['QualityObjectDetail']
        detail.update(IsApprovalRequired=ctx['IsApprovalRequired'],CompletionApprovalSheet={'InstanceId':SHEET},
            CompletionApprovalStatus=ctx['CompletionApprovalStatus'],EntryForApproval=None)
        if loaded:
            data['ApprovalSheetDetails']=dict(ApprovalSheet={'InstanceId':SHEET},Status=sheet['Status'],
                ApprovalEntries=[dict(ApprovalEntry={'InstanceId':ENTRY})],Password='NEVER_FORWARD_APPROVAL_LOAD')
        hook=s['approval_load_hook'] if loaded else s['approval_preview_hook']
        if hook:hook(data)
    s['load_hook']=lambda d:adjust(d,True);s['update_hook']=lambda d:adjust(d,False)
    return s


def prepare(**kw):return update(approval_sheet_id=SHEET,previous_cycle_gmt=OLD,**kw)


@pytest.mark.parametrize('origin',['rejected','cancelled'])
def test_rejected_or_cancelled_event_text_revision_preserves_all_approval_data(env,origin):
    if origin=='cancelled':
        env['event']['Status']=1;ctx=env['approval_context'];ctx['target']['Status']=1;ctx['sheets'][0]['Status']=0
        ctx['current_context']['CompletionApprovalStatus']=0
        ctx['sheets'][0]['entries'][0].update(Status=0,DecisionType=30,ApprovedById=None,ApprovalDecisionId=None,LastCompletedOnGMT=None)
        env['sign_history'][0].update(DecisionType=30,ApprovedById=None,ApprovalDecisionId=None,LastCompletedOnGMT=None)
    old=copy.deepcopy(env['approval_context']);ready=prepare();assert ready.get('state')=='ready',ready
    saved=snap(ready);body=saved['body']
    assert 'approvalSheetDetails' not in body and 'approvalSheetTemplate' not in body and body['submit'] is False
    assert saved['before']['approval_revision']['previous_cycle']['history_preservation_verified']
    assert 'runtime:approval-sheet:id:'+SHEET in saved['resource_writes']
    assert 'NEVER_FORWARD_APPROVAL_LOAD' not in json.dumps(saved)
    assert execute(ready)['state']=='unknown' and env['client'].request.await_count==1
    assert env['approval_context']==old


@pytest.mark.parametrize('mode',['routed','another_sheet','multiple_sheets','pending_actor','substitute','frozen','wrong_cycle','no_history'])
def test_only_explicit_terminal_cycle_can_be_revised(env,mode):
    ctx=env['approval_context'];sheet=ctx['sheets'][0]
    if mode=='routed':sheet['Status']=10
    elif mode=='another_sheet':sheet['ApprovalSheetId']=OTHER
    elif mode=='multiple_sheets':ctx['sheets']*=2
    elif mode=='pending_actor':ctx['current_employee_entry_id']=ENTRY
    elif mode=='substitute':sheet['entries'][0]['SubstituteOption']=2
    elif mode=='frozen':sheet['entries'][0]['IsFrozen']=True
    elif mode=='wrong_cycle':sheet['LastApprovalCycleGMT']=CYCLE
    else:env['route_history']=[]
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('stage',['load','preview'])
@pytest.mark.parametrize('mode',['sheet','status','actor','role','signature','approval_write'])
def test_load_and_preview_must_keep_original_approval_context(env,stage,mode):
    def hook(data):
        if mode=='sheet':data['QualityObjectDetail']['CompletionApprovalSheet']={'InstanceId':OTHER}
        elif mode=='status':data['QualityObjectDetail']['CompletionApprovalStatus']=10
        elif mode=='actor':data['QualityObjectDetail']['UserIsOwner']=False
        elif mode=='role':data['QualityObjectDetail']['UserHasRole']=False
        elif mode=='signature':data['ESigRequirement']={}
        elif stage=='preview':data['ApprovalSheetDetails']={}
        else:data['ApprovalSheetDetails']['ApprovalEntries']=[]
    env['approval_load_hook' if stage=='load' else 'approval_preview_hook']=hook
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


def test_revision_still_requires_actual_text_change_and_current_edit_permission(env):
    assert 'error' in prepare(description='Original description')
    env['approval_preview_hook']=lambda d:d.update(AvailableActions=1)
    assert 'error' in prepare()


def test_changed_old_history_after_preparation_blocks_revision_write(env):
    ready=prepare();assert ready.get('state')=='ready',ready
    env['sign_history'][0]['Comments']='Concurrent audit change'
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_neither_approval_parameter_can_be_used_alone(env):
    assert 'error' in update(approval_sheet_id=SHEET)
    assert 'error' in update(previous_cycle_gmt=OLD)


def test_role_sheet_revision_requires_opt_in_and_preserves_actual_old_signer(env):
    env['approval_context']['sheets'][0]['entries'][0].update(SubstituteOption=2,ApprovedById=OTHER)
    env['sign_history'][0]['ApprovedById']=OTHER
    assert 'error' in prepare()
    ready=prepare(allow_role_substitution=True);assert ready.get('state')=='ready',ready
    assert 'approvalSheetDetails' not in ready['request_preview']['body']
    assert snap(ready)['before']['approval_revision']['previous_cycle']['history']['SignApprovalHistory'][0]['ApprovedById']==OTHER
