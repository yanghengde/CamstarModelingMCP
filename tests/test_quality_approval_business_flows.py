"""Business scripts through real MCP handlers; MES transitions are explicitly simulated.

This checks orchestration and evidence guards, not the actual native engine or emails.
"""
import asyncio
import copy
import json
import re
from unittest.mock import AsyncMock
import pytest

from tools import quality_approval_assignment as assignment, quality_approval_actions as action, quality_approval_decisions as decision
from tools import quality_event_update as revision, quality_approval_history as history, quality_plans as p
from tests.test_quality_approval_assignment import env as assignment_env, actions_env, approval_env, base_env, TEMPLATE, REJECT, RESOLUTION
from tests.test_quality_approval_assignment import prepare as assign, EVENT, ORG, OWNER, ROLE, OTHER, SHEET, ENTRY, LIST, DECISION, snap, execute
from tests.test_quality_event_triage import PRIORITY,echo
from tests.test_quality_event_update import schema_fixture


def result(x):return json.loads(asyncio.run(x))


@pytest.fixture
def env(assignment_env,monkeypatch):
    s=assignment_env;s.update(route_history=[],sign_history=[],clock=0,actor=OWNER)
    extra=dict(BriefDescription='Original event',Description='Original evidence',DiscoveryArea='Line A',EventDataId=None,
        InitiatorId=OWNER,InitiatorOrganizationId=ORG,ReporterId=OWNER,ReporterOrganizationId=ORG,
        OccurrenceDate='2026-10-29 10:00:00',OccurrenceDateGMT='2026-10-29 02:00:00',
        ReportedDate='2026-10-29 11:00:00',ReportedDateGMT='2026-10-29 03:00:00')
    s['extra']=extra
    monkeypatch.setattr(revision,'event_record',AsyncMock(side_effect=lambda *x,**kw:copy.deepcopy(s['event']|s['extra'])))
    original_sql=assignment.q.sql_rows
    async def sql(query,maximum):
        if ' FROM RegulatoryReport WHERE ' in query:return []
        for table,storage in [('ApprovalSheetHistory','route_history'),('SignApprovalHistory','sign_history')]:
            if ' FROM '+table+' WHERE ' in query:
                rows=s[storage]
                key=re.search(table+"Id='([0-9a-f]+)'",query)
                if key:rows=[r for r in rows if r[table+'Id']==key[1]]
                cycle=re.search(r"CONVERT\(datetime2\(7\),N'([^']+)'",query)
                if cycle:rows=[r for r in rows if history.cycle_key(r['ApprovalCycleGMT'])==history.cycle_key(cycle[1])]
                return copy.deepcopy(rows[:maximum])
        return await original_sql(query,maximum)
    monkeypatch.setattr(assignment.q,'sql_rows',AsyncMock(side_effect=sql))
    original_asset=assignment.t.asset
    async def asset(kind,**kw):
        if kind=='owner' and kw.get('key')==OTHER:return dict(entity='Employees',InstanceID=OTHER,Name='Second approver',IsFrozen=False,ChangeCount=1)
        return await original_asset(kind,**kw)
    monkeypatch.setattr(assignment.t,'asset',AsyncMock(side_effect=asset))
    def current():
        row=s['event'];sheet=s['sheets'][0]
        return dict(Status=row['Status'],Category=row['Category'],Organization={'InstanceId':ORG},Owner={'InstanceId':OWNER},
            Role={'InstanceId':ROLE},PriorityLevel={'InstanceId':PRIORITY},ProcessModel=None,TriageComplete=row['TriageComplete'],
            IsCARRequiredToClose=row['IsCARRequiredToClose'],IsApprovalRequired=True,CompletionApprovalSheet={'InstanceId':SHEET},
            CompletionApprovalStatus=sheet['Status'],EntryForApproval=copy.deepcopy(s['inquiry']['EntryForApproval']),UserIsOwner=True,UserHasRole=True)
    async def sign_request(method,path,**kw):
        if method=='GET':
            props={k:{'$ref':'#/components/schemas/'+v} for k,v in dict(qualityObject='NamedObjectRef',organization='NamedObjectRef',approvalSheet='NamedSubentityRef',
                approvalSheetEntry='SubentityRef',approvalDecision='NamedSubentityRef').items()}
            return json.dumps({'components':{'schemas':{'SignApproval':{'properties':props}}}})
        assert path=='/api/SignApproval' and kw['params']['execute']=='false'
        data=echo(copy.deepcopy(kw['body']));data.update(Employee={'InstanceId':s['actor']},AvailableActions=513,IgnoreProcessModel=False,ESigRequirement=None,
            AdditionalESigReqs=[],QualityObjectDetail=current())
        return json.dumps({'data':data})
    monkeypatch.setattr(decision,'request',AsyncMock(side_effect=sign_request))
    async def revision_request(method,path,**kw):
        if method=='GET':return json.dumps(schema_fixture())
        loaded=path.endswith('/PerformEvent');body=kw['body']['inputData'] if loaded else kw['body']
        if loaded:
            detail={k:copy.deepcopy(extra[k]) for k in ('BriefDescription','Description','DiscoveryArea')}
            detail.update(CDOTypeId=7541,EventDataDetail=None,RegulatoryReportDetails=[])
            for field in revision.REF_FIELDS:
                key=PRIORITY if field=='PriorityLevel' else extra[field+'Id'];detail[field]={'Name':'Original '+field,'InstanceId':key}
            detail.update(OccurrenceDate='2026-10-29T10:00:00+08:00',OccurrenceDateGMT='2026-10-29T02:00:00Z',
                ReportedDate='2026-10-29T11:00:00+08:00',ReportedDateGMT='2026-10-29T03:00:00Z')
        else:detail=echo(copy.deepcopy(body['serviceDetail']))|dict(CDOTypeId=7541)
        data=echo(copy.deepcopy(body));data.update(ServiceDetail=detail,AvailableActions=3,IgnoreProcessModel=False,ESigRequirement=None,AdditionalESigReqs=[],
            ApprovalSheetTemplate=None,ApprovalSheetDetails=None,AssignedLots=[],QualityCrossRefDetails=[],QualityCrossReferences=[],QualityObjectDetail=current())
        if loaded:data['ApprovalSheetDetails']=dict(ApprovalSheet={'InstanceId':SHEET},Status=s['sheets'][0]['Status'],
            ApprovalEntries=[dict(ApprovalEntry={'InstanceId':e['ApprovalSheetEntryId']}) for e in s['entries']])
        return json.dumps({'data':data})
    monkeypatch.setattr(revision,'request',AsyncMock(side_effect=revision_request))
    return s


def clock(s):
    s['clock']+=1
    return f'2026-10-30T01:00:00.{s["clock"]:07d}Z'


def native_sheet_history(s,status):
    r=dict.fromkeys(history.SCHEMA['ApprovalSheetHistory'].split(','));r.update(ApprovalSheetHistoryId=f'{8000+s["clock"]:016x}',
        ApprovalSheetId=SHEET,QualityObjectId=EVENT,ApprovalCycleGMT=s['sheets'][0]['LastApprovalCycleGMT'],Status=status,ChangeCount=1,
        GeneralInstructions='Review corrective actions',HistoryId=OTHER,TxnId=OTHER)
    s['route_history'].append(r)


def simulated_route(s,first=False):
    cycle=clock(s)
    if first:
        sheet=dict.fromkeys(action.a.SCHEMA['ApprovalSheet'].split(','));sheet.update(ApprovalSheetId=SHEET,ApprovalSheetName='FromCompleted',ParentId=EVENT,
            Status=10,IsFrozen=False,ChangeCount=1,ApprovalRequired=True,ApprovalDecisionListId=LIST)
        s['sheets']=[sheet];s['entries']=copy.deepcopy(s['template_entries'])
        for index,e in enumerate(s['entries']):e.update(ApprovalSheetEntryId=f'{7001+index:016x}',ParentId=SHEET)
    sheet=s['sheets'][0];sheet.update(Status=10,LastApprovalCycleGMT=cycle,LastApprovalCycle=cycle,ChangeCount=sheet['ChangeCount']+1)
    level=min(e['SheetLevel'] for e in s['entries'])
    for e in s['entries']:e.update(Status=10 if e['SheetLevel']==level else 0,DecisionType=0,ApprovalDecisionId=None,ApprovedById=None,LastCompletedOnGMT=None,Comments='')
    s['event']['Status']=8;s['event']['ChangeCount']+=1
    s['inquiry'].update(Status=8,CompletionApprovalSheet={'InstanceId':SHEET},CompletionApprovalStatus=10,EntryForApproval={'InstanceId':s['entries'][0]['ApprovalSheetEntryId']})
    native_sheet_history(s,10)


def simulated_decision(s,index,typ,comments):
    e=s['entries'][index];when=clock(s);key=f'{9000+s["clock"]:016x}'
    e.update(Status=40 if typ==10 else 30,DecisionType=typ,ApprovalDecisionId=DECISION if typ==10 else REJECT,
        ApprovedById=s['actor'],LastDecisionId=key,LastCompletedOnGMT=when,Comments=comments,ChangeCount=e['ChangeCount']+1)
    r=dict.fromkeys(history.SCHEMA['SignApprovalHistory'].split(','));r.update(SignApprovalHistoryId=key,ApprovalSheetId=SHEET,QualityObjectId=EVENT,
        ApprovalSheetEntryId=e['ApprovalSheetEntryId'],ApprovalCycleGMT=s['sheets'][0]['LastApprovalCycleGMT'],ApproverId=e['ApproverId'],ApproverRoleId=ROLE,
        ApprovedById=s['actor'],ApprovalDecisionId=e['ApprovalDecisionId'],DecisionType=typ,SheetLevel=e['SheetLevel'],Comments=comments,LastCompletedOnGMT=when,
        HistoryId=OTHER,TxnId=OTHER,ChangeCount=1)
    s['sign_history'].append(r)
    if all(x['DecisionType'] in (10,20) for x in s['entries']):
        status=30 if any(x['DecisionType']==20 for x in s['entries']) else 40
        s['sheets'][0]['Status']=status;s['inquiry'].update(CompletionApprovalStatus=status,EntryForApproval=None)
        clock(s);native_sheet_history(s,status)
    else:
        active=[x for x in s['entries'] if x['Status']==10]
        if not active:
            level=min(x['SheetLevel'] for x in s['entries'] if x['Status']==0)
            for x in s['entries']:
                if x['SheetLevel']==level:x['Status']=10
            active=[x for x in s['entries'] if x['Status']==10]
        s['inquiry']['EntryForApproval']={'InstanceId':active[0]['ApprovalSheetEntryId']}
    s['sheets'][0]['ChangeCount']+=1;s['event']['ChangeCount']+=1


def simulated_cancel(s):
    for e in s['entries']:
        clock(s);key=f'{9000+s["clock"]:016x}'
        e.update(Status=0,DecisionType=30,LastDecisionId=key,ChangeCount=e['ChangeCount']+1)
        row=dict.fromkeys(history.SCHEMA['SignApprovalHistory'].split(','))
        row.update(SignApprovalHistoryId=key,ApprovalSheetId=SHEET,QualityObjectId=EVENT,ApprovalSheetEntryId=e['ApprovalSheetEntryId'],
            ApprovalCycleGMT=s['sheets'][0]['LastApprovalCycleGMT'],ApproverId=e['ApproverId'],ApproverRoleId=ROLE,
            DecisionType=30,SheetLevel=e['SheetLevel'],Comments=e['Comments'],ChangeCount=1,HistoryId=OTHER,TxnId=OTHER)
        s['sign_history'].append(row)
    s['sheets'][0].update(Status=0,ChangeCount=s['sheets'][0]['ChangeCount']+1)
    s['event']['Status']=1;s['event']['ChangeCount']+=1
    s['inquiry'].update(Status=1,CompletionApprovalStatus=0,EntryForApproval=None)
    clock(s);native_sheet_history(s,0)


def attest(ready):
    out=result(p.review_quality_transaction(ready['plan_id'],ready['plan_digest'],'resolve','offline:explicit-simulated-final-audit',
        'Offline simulation only, not an attestation about the actual MES.','committed'))
    assert out['state']=='human_committed'


def sign(s,index=0,typ=10):
    e=s['entries'][index];s['actor']=e['ApproverId'];s['inquiry']['EntryForApproval']={'InstanceId':e['ApprovalSheetEntryId']}
    return result(decision.prepare_quality_approval_decision('approve' if typ==10 else 'reject','E','Org',SHEET,e['ApprovalSheetEntryId'],
        DECISION if typ==10 else REJECT,'Approval cycle business script'))


@pytest.mark.parametrize('levels',[[1],[1,2],[1,1]])
def test_simulated_assignment_and_single_multilevel_parallel_approval(env,levels):
    if len(levels)>1:
        second=copy.deepcopy(env['template_entries'][0]);second.update(ApprovalSheetEntryId='0000000000006101',SheetLevel=levels[1],ApproverId=OTHER,AssignedApproverId=OTHER)
        env['template_entries'].append(second)
    ready=assign();assert ready.get('state')=='ready',ready
    assert execute(ready)['state']=='unknown';simulated_route(env,first=True);attest(ready)
    if levels==[1,2]:assert 'error' in sign(env,1)
    for index in range(len(levels)):
        ready=sign(env,index);assert ready.get('state')=='ready',ready
        comments=snap(ready)['body']['comments'];assert execute(ready)['state']=='unknown'
        simulated_decision(env,index,10,comments);attest(ready)
    out=result(history.get_quality_approval_history('E','Org',SHEET))
    assert out['complete'] and env['sheets'][0]['Status']==40
    assert len(out['cycles'][0]['decision_history'])==len(levels)
    assert env['client'].request.await_count==1+len(levels)


@pytest.mark.parametrize('origin',['reject','cancel'])
def test_simulated_assignment_rejection_revision_resubmit_new_approval_preserves_old_cycle(env,origin):
    ready=assign();assert ready.get('state')=='ready',ready
    execute(ready);simulated_route(env,first=True);attest(ready)
    old=env['sheets'][0]['LastApprovalCycleGMT']
    if origin=='reject':
        ready=sign(env,typ=20);assert ready.get('state')=='ready',ready
        comments=snap(ready)['body']['comments'];execute(ready);simulated_decision(env,0,20,comments);attest(ready)
    else:
        ready=result(action.prepare_quality_approval_action('cancel','E','Org',SHEET,'Cancel approval before revision'))
        assert ready.get('state')=='ready',ready
        execute(ready);simulated_cancel(env);attest(ready)
    original=copy.deepcopy(env['sign_history'])
    ready=result(revision.prepare_quality_event_update('E','Org',description='Revised corrective-action evidence',
        approval_sheet_id=SHEET,previous_cycle_gmt=old,comments='Fix rejected evidence'))
    assert ready.get('state')=='ready',ready
    execute(ready);env['extra']['Description']='Revised corrective-action evidence';env['event']['ChangeCount']+=1;attest(ready)
    ready=result(action.prepare_quality_approval_action('resubmit','E','Org',SHEET,'Resubmit corrected evidence',previous_cycle_gmt=old))
    assert ready.get('state')=='ready',ready
    execute(ready);simulated_route(env);attest(ready)
    ready=sign(env);assert ready.get('state')=='ready',ready
    comments=snap(ready)['body']['comments'];execute(ready);simulated_decision(env,0,10,comments);attest(ready)
    out=result(history.get_quality_approval_history('E','Org',SHEET));assert out['complete'] and len(out['cycles'])==2
    assert out['cycles'][0]['decision_history'][0]['DecisionType']==10 and out['cycles'][1]['decision_history'][0]['DecisionType']==(20 if origin=='reject' else 30)
    assert env['sign_history'][:1]==original and env['client'].request.await_count==5
    assert env['extra']['Description']=='Revised corrective-action evidence' and not out['transaction_attribution_verified']


def test_native_reject_can_wait_for_next_level_before_final_rejection(env):
    second=copy.deepcopy(env['template_entries'][0]);second.update(ApprovalSheetEntryId='0000000000006101',SheetLevel=2,ApproverId=OTHER,AssignedApproverId=OTHER)
    env['template_entries'].append(second)
    ready=assign();assert ready.get('state')=='ready',ready
    execute(ready);simulated_route(env,first=True);attest(ready)
    ready=sign(env,typ=20);comments=snap(ready)['body']['comments'];execute(ready);simulated_decision(env,0,20,comments);attest(ready)
    assert env['sheets'][0]['Status']==10 and env['entries'][1]['Status']==10
    rejected=result(action.prepare_quality_approval_action('resubmit','E','Org',SHEET,'Too early',previous_cycle_gmt=env['sheets'][0]['LastApprovalCycleGMT']))
    assert 'error' in rejected
    ready=sign(env,1);assert ready.get('state')=='ready',ready
    comments=snap(ready)['body']['comments'];execute(ready);simulated_decision(env,1,10,comments);attest(ready)
    assert env['sheets'][0]['Status']==30
    ready=result(action.prepare_quality_approval_action('resubmit','E','Org',SHEET,'Final rejection now eligible',previous_cycle_gmt=env['sheets'][0]['LastApprovalCycleGMT']))
    assert ready.get('state')=='ready',ready
