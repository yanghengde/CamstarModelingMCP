"""Offline cycle scripts. All native transitions below are explicit simulated state."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from tools import quality_approval_actions as c, quality_approval_decisions as d, quality_approval_history as h, quality_plans as p
from tests.test_quality_approval_decisions import env as decision_env, actions_env, approval_env, base_env, EVENT, ORG, OWNER, ROLE, OTHER, SHEET, ENTRY, DECISION, prepare as decide
from tests.test_quality_approval_history import env as history_env, CYCLE, OLD, HISTORY, SIGN
from tests.test_quality_approval_actions import execute, snap


def result(x):return json.loads(asyncio.run(x))
def prepare(**kw):return result(c.prepare_quality_approval_action('resubmit','E','Org',SHEET,'Reviewed revised submission',previous_cycle_gmt=OLD,**kw))


@pytest.fixture
def env(decision_env,history_env):
    s=decision_env
    s['sheets'][0].update(Status=30,LastApprovalCycleGMT=OLD)
    s['entries'][0].update(Status=30,DecisionType=20,ApprovalDecisionId=DECISION,ApprovedById=OWNER,
                          LastDecisionId=SIGN,LastCompletedOnGMT=CYCLE,Comments='Native comment')
    s['inquiry'].update(CompletionApprovalStatus=30,EntryForApproval=None)
    return s


def cancelled(s):
    s['sheets'][0]['Status']=0
    s['entries'][0].update(Status=0,DecisionType=30,ApprovalDecisionId=None,ApprovedById=None,LastCompletedOnGMT=None)
    s['sign_history'][0].update(DecisionType=30,ApprovalDecisionId=None,ApprovedById=None,LastCompletedOnGMT=None)
    s['inquiry'].update(CompletionApprovalStatus=0,EntryForApproval=None)


@pytest.mark.parametrize('origin',['rejected','cancelled'])
def test_durable_previous_cycle_allows_native_resubmit_once(env,origin):
    if origin=='cancelled':cancelled(env)
    ready=prepare();assert ready.get('state')=='ready',ready
    saved=snap(ready);proof=saved['before']['previous_cycle']
    assert proof['history_preservation_verified'] and proof['approval_cycle_gmt']==OLD
    body=saved['body'];assert set(body)=={'qualityObject','organization','approvalSheet','comments'}
    assert 'decisionType' not in json.dumps(body) and 'lastApprovalCycle' not in json.dumps(body)
    assert execute(ready)['state']=='unknown';env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/api/RouteApprovalSheet')
    assert 'error' in execute(ready)
    # Preparation and execution never modify the stored history fixtures.
    assert env['history'][0]['ApprovalCycleGMT']==OLD and env['sign_history'][0]['SignApprovalHistoryId']==SIGN


@pytest.mark.parametrize('mode',['no_route','no_sign','wrong_cycle','wrong_entry','wrong_actor','wrong_decision',
    'no_txn','missing_pointer','wrong_time','not_rejected','unsigned_pending','role_substitute','frozen','bad_level','current_cycle'])
def test_missing_or_mismatched_old_evidence_blocks_reset(env,mode):
    if mode=='no_route':env['history']=[]
    elif mode=='no_sign':env['sign_history']=[]
    elif mode=='wrong_cycle':env['history'][0]['ApprovalCycleGMT']=CYCLE
    elif mode=='wrong_entry':env['sign_history'][0]['ApprovalSheetEntryId']=OTHER
    elif mode=='wrong_actor':env['sign_history'][0]['ApprovedById']=OTHER
    elif mode=='wrong_decision':env['sign_history'][0]['ApprovalDecisionId']=OTHER
    elif mode=='no_txn':env['sign_history'][0]['TxnId']=None
    elif mode=='missing_pointer':env['entries'][0]['LastDecisionId']=None
    elif mode=='wrong_time':env['entries'][0]['LastCompletedOnGMT']=OLD
    elif mode=='not_rejected':env['entries'][0].update(Status=40,DecisionType=10);env['sign_history'][0]['DecisionType']=10
    elif mode=='unsigned_pending':env['sheets'][0]['Status']=0
    elif mode=='role_substitute':env['entries'][0]['SubstituteOption']=2
    elif mode=='frozen':env['entries'][0]['IsFrozen']=True
    elif mode=='bad_level':env['entries'][0]['SheetLevel']=0
    else:env['sheets'][0]['LastApprovalCycleGMT']=CYCLE
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


def test_resubmit_requires_explicit_old_cycle_and_normal_route_cannot_reset(env):
    out=result(c.prepare_quality_approval_action('resubmit','E','Org',SHEET,'Reviewed'))
    assert 'error' in out
    assert 'error' in result(c.prepare_quality_approval_action('route','E','Org',SHEET,'Reviewed'))


@pytest.mark.parametrize('change',['history','decision','sheet','owner','signature'])
def test_late_previous_cycle_or_permission_change_stops_write(env,change):
    ready=prepare();assert ready.get('state')=='ready',ready
    if change=='history':env['history'][0]['Comments']='Changed old evidence'
    elif change=='decision':env['sign_history'][0]['ChangeCount']+=1
    elif change=='sheet':env['sheets'][0]['ChangeCount']+=1
    elif change=='owner':env['hook']=lambda d:d['QualityObjectDetail'].update(UserIsOwner=False)
    else:env['roots']['Organizations']['CollectESigForAllQualityTxns']=True
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['same_cycle','future','other_entry','other_object','no_txn','missing'])
def test_retained_last_decision_must_be_from_original_older_cycle(env,monkeypatch,mode):
    env['sheets'][0].update(Status=10,LastApprovalCycleGMT=CYCLE)
    env['entries'][0].update(Status=10,DecisionType=0,ApprovalDecisionId=None,ApprovedById=None,LastCompletedOnGMT=None)
    env['inquiry'].update(CompletionApprovalStatus=10,EntryForApproval={'InstanceId':ENTRY})
    row=env['sign_history'][0]
    if mode=='same_cycle':row['ApprovalCycleGMT']=CYCLE
    elif mode=='future':row['ApprovalCycleGMT']='2026-10-30T00:00:00Z'
    elif mode=='other_entry':row['ApprovalSheetEntryId']=OTHER
    elif mode=='other_object':row['QualityObjectId']=OTHER
    elif mode=='no_txn':row['TxnId']=None
    else:env['sign_history']=[]
    assert 'error' in decide();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('origin',['rejected','cancelled'])
def test_simulated_resubmit_new_approval_and_both_cycle_history(env,origin):
    if origin=='cancelled':cancelled(env)
    old_history=copy.deepcopy((env['history'],env['sign_history']))
    ready=prepare();assert ready.get('state')=='ready',ready
    assert execute(ready)['state']=='unknown'
    # A real workflow needs human final audit evidence. This is a local simulated attestation only.
    audit=result(p.review_quality_transaction(ready['plan_id'],ready['plan_digest'],'resolve',
        'offline-fixture:simulated-transaction','Offline simulated final audit only; no MES acceptance.','committed'))
    assert audit['state']=='human_committed'
    # Simulate native Route: current decision fields cleared, LastDecision remains, new cycle routed.
    env['sheets'][0].update(Status=10,LastApprovalCycleGMT=CYCLE)
    env['entries'][0].update(Status=10,DecisionType=0,ApprovalDecisionId=None,ApprovedById=None,LastCompletedOnGMT=None)
    env['inquiry'].update(CompletionApprovalStatus=10,EntryForApproval={'InstanceId':ENTRY})
    second=decide();assert second.get('state')=='ready',second
    assert snap(second)['before']['prior_decision']['SignApprovalHistoryId']==SIGN
    assert execute(second)['state']=='unknown' and env['client'].request.await_count==2
    # Simulate stored second-cycle history while preserving the original cycle rows.
    env['history'].append(env['make_history']('ApprovalSheetHistory','0000000000005003',CYCLE))
    new=env['make_history']('SignApprovalHistory','0000000000005004',CYCLE);new['DecisionType']=10
    env['sign_history'].append(new)
    out=result(h.get_quality_approval_history('E','Org',SHEET))
    assert out['complete'] and len(out['cycles'])==2
    assert out['cycles'][0]['decision_history'][0]['DecisionType']==10
    assert out['cycles'][1]['decision_history'][0]['DecisionType']==(20 if origin=='rejected' else 30)
    assert (env['history'][:1],env['sign_history'][:1])==old_history
    assert not out['transaction_attribution_verified']


def test_unknown_old_attempt_blocks_resubmit_even_with_valid_history(env):
    first=prepare();second=prepare();assert execute(first)['state']=='unknown'
    assert 'error' in execute(second);env['client'].request.assert_awaited_once()
