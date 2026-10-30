import asyncio
import copy
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from tools import mcp, quality_capa_approval_revision as c, quality_plans as p
from tests.test_quality_event_triage import env as base_env, ORG, OWNER, ROLE, PRIORITY, OTHER, echo
from tests.test_quality_approvals import CAPA, SHEET, ENTRY, DECISION
from tests.test_quality_approval_history import OLD,CYCLE,HISTORY,SIGN


def result(x):return json.loads(asyncio.run(x))
def prepare(**kw):return result(c.prepare_capa_approval_revision('E','Org',SHEET,OLD,'Revise corrective-action evidence',
    description='Revised corrective action',**({'allow_reported_date_refresh':True}|kw)))
def snap(ready):return result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))


@pytest.fixture
def env(base_env,monkeypatch):
    s=base_env;row=dict.fromkeys(c.SCHEMA['CAPA'].split(','));row.update(CAPAId=CAPA,CAPAName='E',Category=5,Status=8,
        ChangeCount=1,OrganizationId=ORG,OwnerId=OWNER,RoleId=ROLE,PriorityLevelId=PRIORITY,IsFrozen=False,IsSubmitted=True,
        BriefDescription='Original CAPA',Description='Original corrective action',SystemicIssue=False,
        InitiatorId=OWNER,InitiatorOrganizationId=ORG,ReporterId=OWNER,ReporterOrganizationId=ORG,
        OccurrenceDate='2026-10-29 10:00:00.1234567',OccurrenceDateGMT='2026-10-29 02:00:00.1234567',
        ReportedDate='2026-10-29 11:00:00',ReportedDateGMT='2026-10-29 03:00:00')
    entry=dict(ApprovalSheetEntryId=ENTRY,ParentId=SHEET,Status=30,DecisionType=20,ApproverId=OWNER,ApproverRoleId=ROLE,
        ApprovedById=OWNER,ApprovalDecisionId=DECISION,LastDecisionId=SIGN,LastCompletedOnGMT=CYCLE,SheetLevel=1,IsFrozen=False,
        SubstituteOption=1,Comments='Original rejection',DelegationTaskId=None,AssignedApproverId=OWNER)
    sheet=dict(ApprovalSheetId=SHEET,Status=30,IsFrozen=False,LastApprovalCycleGMT=OLD,entries=[entry])
    context=dict(target=dict(Id=CAPA,Name='E',Category=5,Status=8,ChangeCount=1,OrganizationId=ORG,IsFrozen=False),
        sheets=[sheet],current_employee_entry_id=None,current_context=dict(IsApprovalRequired=True,
        CompletionApprovalSheet={'instance_id':SHEET},CompletionApprovalStatus=30,EntryForApproval=None))
    route=dict.fromkeys(c.h.SCHEMA['ApprovalSheetHistory'].split(','));route.update(ApprovalSheetHistoryId=HISTORY,ApprovalSheetId=SHEET,
        QualityObjectId=CAPA,ApprovalSheetEntryId=ENTRY,ApprovalCycleGMT=OLD,Status=10,ChangeCount=1,HistoryId=OTHER,TxnId=OTHER)
    sign=dict.fromkeys(c.h.SCHEMA['SignApprovalHistory'].split(','));sign.update(SignApprovalHistoryId=SIGN,ApprovalSheetId=SHEET,
        QualityObjectId=CAPA,ApprovalSheetEntryId=ENTRY,ApprovalCycleGMT=OLD,DecisionType=20,ApproverId=OWNER,ApproverRoleId=ROLE,
        ApprovedById=OWNER,ApprovalDecisionId=DECISION,SheetLevel=1,Comments='Original rejection',LastCompletedOnGMT=CYCLE,
        ChangeCount=1,HistoryId=OTHER,TxnId=OTHER)
    s.update(capa=row,approval_context=context,route_history=[route],sign_history=[sign],regulatory=[],
        capa_load_hook=None,capa_preview_hook=None,capa_requests=[],schema_extra=False)
    monkeypatch.setattr(c.a,'read_context',AsyncMock(side_effect=lambda *x:copy.deepcopy(s['approval_context'])))
    original=c.q.sql_rows
    async def sql(query,maximum):
        if 'INFORMATION_SCHEMA.COLUMNS' in query:
            return [dict(TABLE_NAME=t,COLUMN_NAME=k) for t,cols in re.findall(r"TABLE_NAME='(\w+)' AND COLUMN_NAME IN \((.*?)\)",query) for k in re.findall(r"'([^']+)'",cols)]
        if ' FROM CAPA WHERE ' in query:return [copy.deepcopy(s['capa'])]
        if ' FROM RegulatoryReport WHERE ' in query:return copy.deepcopy(s['regulatory'])
        if ' FROM ApprovalSheetHistory WHERE ' in query:return copy.deepcopy(s['route_history'])
        if ' FROM SignApprovalHistory WHERE ' in query:return copy.deepcopy(s['sign_history'])
        return await original(query,maximum)
    monkeypatch.setattr(c.q,'sql_rows',AsyncMock(side_effect=sql))
    def contract():
        doc=copy.deepcopy(json.loads(Path('docs/quality-fixtures/contracts.json').read_text(encoding='utf8'))['Shopfloor/UpdateCAPA'])
        schemas=doc['components']['schemas'];schemas['EventToPerform']={'properties':{'eventName':{'type':'string'}}}
        schemas['UpdateCAPAPerformEventModel']={'properties':{'inputData':{'$ref':'#/components/schemas/UpdateCAPA'},'event':{'$ref':'#/components/schemas/EventToPerform'}}}
        doc['paths']['/api/UpdateCAPA/PerformEvent']={'post':{'requestBody':{'content':{'application/json':{'schema':{'$ref':'#/components/schemas/UpdateCAPAPerformEventModel'}}}}}}
        if s['schema_extra']:schemas['CAPADetail']['properties']['customUnverifiedField']={'type':'string'}
        return doc
    def original_detail():
        raw=s['capa'];detail=dict(CDOTypeId=7734,AdditionalOrganizations=[],CAPACustomDetail=None,BriefDescription=raw['BriefDescription'],
            Description=raw['Description'],IsSystemicIssue=raw['SystemicIssue'])
        for field in c.REFS:
            key=raw[field+'Id'];detail[field]=None if key is None else dict(Name='Original '+field,InstanceId=key,Password='NEVER_KEEP_EMPLOYEE_SECRET')
        detail.update(OccurrenceDate='2026-10-29T10:00:00.1234567+08:00',OccurrenceDateGMT='2026-10-29T02:00:00.1234567Z',
            ReportedDate='2026-10-29T11:00:00+08:00',ReportedDateGMT='2026-10-29T03:00:00Z')
        return detail
    async def transport(method,path,**kw):
        s['capa_requests'].append((method,path,copy.deepcopy(kw)))
        if method=='GET':return json.dumps(contract())
        loaded=path.endswith('/PerformEvent');body=kw['body']['inputData'] if loaded else kw['body']
        if loaded:assert kw['body']['event']=={'eventName':'Load'} and 'execute' not in kw['params']
        else:assert path=='/api/UpdateCAPA' and kw['params']['execute']=='false'
        data=echo(copy.deepcopy(body));detail=original_detail() if loaded else echo(copy.deepcopy(body['serviceDetail']))|dict(CDOTypeId=7734)
        ctx=s['approval_context'];raw=s['capa']
        data.update(ServiceDetail=detail,ApprovalSheetDetails=None,QualityCrossRefDetails=[],AvailableActions=3,IgnoreProcessModel=False,
            ESigRequirement=None,AdditionalESigReqs=[],QualityObjectDetail=dict(Category=5,Status=raw['Status'],Organization={'InstanceId':ORG},
            Owner={'InstanceId':OWNER},Role={'InstanceId':ROLE},PriorityLevel={'InstanceId':PRIORITY},UserIsOwner=True,UserHasRole=True,
            ProcessModel=None,IsApprovalRequired=True,CompletionApprovalSheet={'InstanceId':SHEET},
            CompletionApprovalStatus=ctx['current_context']['CompletionApprovalStatus'],EntryForApproval=None))
        if loaded:data['ApprovalSheetDetails']=dict(ApprovalSheet={'InstanceId':SHEET},Status=ctx['sheets'][0]['Status'],ApprovalEntries=[dict(ApprovalEntry={'InstanceId':ENTRY})])
        hook=s['capa_load_hook'] if loaded else s['capa_preview_hook']
        if hook:hook(data)
        return json.dumps({'data':data})
    monkeypatch.setattr(c,'request',AsyncMock(side_effect=transport))
    return s


@pytest.mark.parametrize('origin',['rejected','cancelled'])
def test_capa_text_revision_preserves_original_detail_and_requires_native_report_time_refresh(env,origin):
    if origin=='cancelled':
        ctx=env['approval_context'];ctx['target']['Status']=1;env['capa']['Status']=1;ctx['sheets'][0]['Status']=0;ctx['current_context']['CompletionApprovalStatus']=0
        ctx['sheets'][0]['entries'][0].update(Status=0,DecisionType=30,ApprovalDecisionId=None,ApprovedById=None,LastCompletedOnGMT=None)
        env['sign_history'][0].update(DecisionType=30,ApprovalDecisionId=None,ApprovedById=None,LastCompletedOnGMT=None)
    assert 'error' in prepare(allow_reported_date_refresh=False)
    ready=prepare();assert ready.get('state')=='ready',ready
    saved=snap(ready);body=saved['body'];detail=body['serviceDetail']
    assert body['submit'] is False and not {'approvalSheetDetails','classification','subClassification','employee'}&body.keys()
    assert detail['reportedDateGMT']=='2026-10-29T03:00:00Z' and detail['reporter']['instanceId']==OWNER
    assert detail['isSystemicIssue'] is False and detail['carSeverity'] is None
    assert 'NEVER_KEEP_EMPLOYEE_SECRET' not in json.dumps(saved)
    assert ready['server_context']['reported_date_refresh_expected'] and execute(ready)['state']=='unknown'
    env['client'].request.assert_awaited_once();assert 'error' in execute(ready)


@pytest.mark.parametrize('mode',['not_submitted','custom','process','orig_process','closed','regulatory','wrong_version','wrong_org','wrong_status'])
def test_capa_complex_or_changed_source_blocks_revision(env,mode):
    field,value={'not_submitted':('IsSubmitted',False),'custom':('CAPACustomDataId',OTHER),'process':('ProcessModelId',OTHER),
        'orig_process':('OrigProcessModelTemplateId',OTHER),'closed':('CloseDateGMT',CYCLE),'wrong_version':('ChangeCount',3),
        'wrong_org':('OrganizationId',OTHER),'wrong_status':('Status',1)}.get(mode,(None,None))
    if field:env['capa'][field]=value
    else:env['regulatory']=[dict(RegulatoryReportId=OTHER)]
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['role','owner','edit','signature','approval_write','entry','cross_ref','ignore_process','severity','date','additional_org'])
def test_capa_load_or_preview_cannot_change_identity_approval_or_preserved_fields(env,mode):
    def hook(data):
        if mode=='role':data['QualityObjectDetail']['UserHasRole']=False
        elif mode=='owner':data['QualityObjectDetail']['UserIsOwner']=False
        elif mode=='edit':data['AvailableActions']=1
        elif mode=='signature':data['AdditionalESigReqs']=[{}]
        elif mode=='approval_write':data['ApprovalSheetDetails']={}
        elif mode=='entry':data['QualityObjectDetail']['EntryForApproval']={'InstanceId':ENTRY}
        elif mode=='cross_ref':data['QualityCrossRefDetails']=[{}]
        elif mode=='ignore_process':data['IgnoreProcessModel']=True
        elif mode=='severity':data['ServiceDetail']['CARSeverity']={'InstanceId':OTHER,'Name':'Other'}
        elif mode=='date':data['ServiceDetail']['ReportedDateGMT']=CYCLE
        else:data['ServiceDetail']['AdditionalOrganizations']=[{'InstanceId':OTHER}]
    env['capa_preview_hook' if mode=='approval_write' else 'capa_load_hook']=hook
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('change',['history','text','version','signature','schema'])
def test_capa_rechecks_every_dependency_before_single_write(env,change):
    ready=prepare();assert ready.get('state')=='ready',ready
    if change=='history':env['sign_history'][0]['Comments']='Changed decision evidence'
    elif change=='text':env['capa']['Description']='Concurrent description'
    elif change=='version':env['capa']['ChangeCount']+=1
    elif change=='signature':env['roots']['Organizations']['QualityESigTxns']=[7654]
    else:env['schema_extra']=True
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('approvalSheetDetails',{}),('employee',{'instanceId':OTHER}),('submit',True),('ignoreProcessModel',True)])
def test_capa_transport_has_no_override_channel(env,field,value):
    saved=snap(prepare());saved['body'][field]=value
    with pytest.raises(ValueError):p.transport(saved)


def test_actual_mcp_schema_explains_reported_time_side_effect(env):
    async def check():
        async with Client(mcp) as client:
            tool=next(t for t in await client.list_tools() if t.name=='prepare_capa_approval_revision')
            assert not tool.input_schema['additionalProperties'] and tool.input_schema['properties']['allow_reported_date_refresh']['default'] is False
            assert not {'password','employee','signature'}&tool.input_schema['properties'].keys()
    asyncio.run(check())


def test_capa_role_sheet_revision_does_not_rewrite_original_or_actual_approver(env):
    env['approval_context']['sheets'][0]['entries'][0].update(SubstituteOption=2,ApprovedById=OTHER)
    env['sign_history'][0]['ApprovedById']=OTHER
    assert 'error' in prepare()
    ready=prepare(allow_role_substitution=True);assert ready.get('state')=='ready',ready
    stored=snap(ready)['before']['previous_cycle']['history']['SignApprovalHistory'][0]
    assert stored['ApproverId']==OWNER and stored['ApprovedById']==OTHER
    assert 'approvalSheetDetails' not in ready['request_preview']['body']


@pytest.mark.parametrize('origin',['rejected','cancelled'])
def test_simulated_capa_revision_resubmit_approval_and_old_new_cycle_history(env,monkeypatch,origin):
    from tools import quality_approval_actions as action,quality_approval_decisions as decision,quality_approval_history as h
    from tests.test_quality_approvals import LIST
    ctx=env['approval_context'];sheet=ctx['sheets'][0];entry=sheet['entries'][0]
    ctx['current_context'].update(Owner={'instance_id':OWNER},Role={'instance_id':ROLE})
    sheet.update(ApprovalSheetName='FromCompleted',ApprovalDecisionListId=LIST,ChangeCount=1,
        decision_list=dict(IsFrozen=False,ChangeCount=1),allowed_decisions=[dict(ApprovalDecisionId=DECISION,
        ApprovalDecisionName='Approve',IsFrozen=False,DecisionType=10,IncludeComments=True)])
    if origin=='cancelled':
        ctx['target']['Status']=1;env['capa']['Status']=1;sheet['Status']=0;ctx['current_context']['CompletionApprovalStatus']=0
        entry.update(Status=0,DecisionType=30,ApprovalDecisionId=None,ApprovedById=None,LastCompletedOnGMT=None)
        env['sign_history'][0].update(DecisionType=30,ApprovalDecisionId=None,ApprovedById=None,LastCompletedOnGMT=None)
    monkeypatch.setattr(action.creation,'notification_policy',AsyncMock(return_value=dict(smtp_configured=False,entries=[])))
    async def transport(method,path,**kw):
        service=path.split('/')[2] if method=='GET' else path.split('/')[-1]
        if method=='GET':
            types=dict(qualityObject='NamedObjectRef',organization='NamedObjectRef',approvalSheet='NamedSubentityRef')
            if service=='SignApproval':types.update(approvalSheetEntry='SubentityRef',approvalDecision='NamedSubentityRef')
            return json.dumps({'components':{'schemas':{service:{'properties':{k:{'$ref':'#/components/schemas/'+v} for k,v in types.items()}}}}})
        assert kw['params']['execute']=='false' and service in ('RouteApprovalSheet','SignApproval')
        data=echo(copy.deepcopy(kw['body']));data.update(Employee={'InstanceId':OWNER},AvailableActions=769,IgnoreProcessModel=False,
            ESigRequirement=None,AdditionalESigReqs=[],QualityObjectDetail=dict(Status=ctx['target']['Status'],Category=5,
            Organization={'InstanceId':ORG},Owner={'InstanceId':OWNER},Role={'InstanceId':ROLE},UserIsOwner=True,UserHasRole=True,
            ProcessModel=None,EntryForApproval=None if ctx['current_employee_entry_id'] is None else {'InstanceId':ENTRY}))
        return json.dumps({'data':data})
    monkeypatch.setattr(action,'request',AsyncMock(side_effect=transport));monkeypatch.setattr(decision,'request',AsyncMock(side_effect=transport))
    def audit(ready):
        out=result(p.review_quality_transaction(ready['plan_id'],ready['plan_digest'],'resolve','offline:simulated-capa-audit',
            'Explicit offline simulated final evidence, not actual MES acceptance.','committed'))
        assert out['state']=='human_committed'
    ready=prepare();assert ready.get('state')=='ready',ready
    assert execute(ready)['state']=='unknown'
    # Explicitly simulate the native ReportedDate refresh; do not assert preservation.
    env['capa'].update(Description='Revised corrective action',ReportedDate='2026-10-30 11:00:00',ReportedDateGMT='2026-10-30 03:00:00',ChangeCount=2)
    ctx['target']['ChangeCount']=2;audit(ready)
    ready=result(action.prepare_quality_approval_action('resubmit','E','Org',SHEET,'Resubmit revised CAPA',object_kind='capa',previous_cycle_gmt=OLD))
    assert ready.get('state')=='ready',ready
    assert execute(ready)['state']=='unknown'
    newcycle='2026-10-30T04:00:00.1234567Z'
    sheet.update(Status=10,LastApprovalCycleGMT=newcycle,ChangeCount=2);entry.update(Status=10,DecisionType=0,ApprovalDecisionId=None,
        ApprovedById=None,LastCompletedOnGMT=None,Comments='')
    ctx['current_employee_entry_id']=ENTRY;ctx['current_context'].update(CompletionApprovalStatus=10,EntryForApproval={'instance_id':ENTRY})
    env['capa']['Status']=8;ctx['target']['Status']=8
    row=copy.deepcopy(env['route_history'][0]);row.update(ApprovalSheetHistoryId='0000000000007011',ApprovalCycleGMT=newcycle)
    env['route_history'].append(row);audit(ready)
    ready=result(decision.prepare_quality_approval_decision('approve','E','Org',SHEET,ENTRY,DECISION,'Approve revised CAPA',object_kind='capa'))
    assert ready.get('state')=='ready',ready
    assert snap(ready)['before']['prior_decision']['SignApprovalHistoryId']==SIGN
    assert execute(ready)['state']=='unknown'
    row=copy.deepcopy(env['sign_history'][0]);row.update(SignApprovalHistoryId='0000000000007012',ApprovalCycleGMT=newcycle,DecisionType=10,
        ApprovalDecisionId=DECISION,ApprovedById=OWNER,Comments=snap(ready)['body']['comments'],LastCompletedOnGMT='2026-10-30T04:01:00Z')
    env['sign_history'].append(row);sheet['Status']=40;ctx['current_employee_entry_id']=None
    ctx['current_context'].update(CompletionApprovalStatus=40,EntryForApproval=None);audit(ready)
    out=result(h.get_quality_approval_history('E','Org',SHEET,object_kind='capa'))
    assert out['complete'] and len(out['cycles'])==2
    assert out['cycles'][0]['decision_history'][0]['DecisionType']==10
    assert out['cycles'][1]['decision_history'][0]['DecisionType']==(20 if origin=='rejected' else 30)
    assert env['capa']['ReportedDateGMT']=='2026-10-30 03:00:00' and env['client'].request.await_count==3
