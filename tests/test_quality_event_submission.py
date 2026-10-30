"""Pending->native submit->review->triage scripts use explicit isolated native states."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from tools import mcp,get_tool_func,quality_event_submission as s,quality_event_update as u,quality_event_triage as t,quality_plans as p
from tests.test_quality_event_update import env as update_env
from tests.test_quality_event_triage import env as triage_env,EVENT,ORG,OWNER,ROLE,PRIORITY,SPEC,OTHER,echo
from agent.safety import evaluate_mutations,MutationCounts,plan_preview


def result(x):return json.loads(asyncio.run(x))
def prepare(**kw):return result(s.prepare_quality_event_submission('E','Org','Submit reviewed original draft',**kw))
def execute(r):return result(p.execute_quality_transaction(r['plan_id'],r['plan_digest']))
def snap(r):return result(p.get_quality_transaction_plan(r['plan_id']))['snapshot']


@pytest.fixture
def env(update_env,monkeypatch):
    state=update_env;state['event'].update(Status=2,IsSubmitted=False,TriageComplete=False)
    state.update(business_rules=[],notice=dict(smtp_configured=False,entries=[],notification_effect_verified=False),
        classes=dict(EventId=EVENT,ChangeCount=1,ClassificationId='000000000000b001',SubClassificationId='000000000000b002'),
        submission_posts=[],submit_mode=None,history_rows=[])
    original_sql=t.q.sql_rows
    async def sql(query,maximum):
        if 'INFORMATION_SCHEMA.COLUMNS' in query:
            import re
            return [dict(TABLE_NAME=table,COLUMN_NAME=col) for table,cols in re.findall(r"TABLE_NAME='(\w+)' AND COLUMN_NAME IN \((.*?)\)",query) for col in re.findall(r"'([^']+)'",cols)]
        if query.startswith('SELECT TOP 1 '+s.CLASS_COLUMNS):return [copy.deepcopy(state['classes'])]
        if 'FROM UpdateEventHistory s JOIN HistoryMainline h ' in query:return copy.deepcopy(state['history_rows'])
        return await original_sql(query,maximum)
    monkeypatch.setattr(t.q,'sql_rows',AsyncMock(side_effect=sql))
    class RuleContext:
        def __init__(self,*x):pass
        async def get(self,entity,key,params):
            assert entity=='TriageSpecs' and key==SPEC and 'BusinessRules' in params['$select']
            return dict(InstanceID=SPEC,Name='Spec',IsFrozen=False,BusinessRules=copy.deepcopy(state['business_rules']))
    monkeypatch.setattr(s,'ReadContext',RuleContext)
    monkeypatch.setattr(s.creation,'notification_policy',AsyncMock(side_effect=lambda *x:copy.deepcopy(state['notice'])))
    # u.request is the existing fixed Load/save-preview simulator; submit calls
    # deliberately select the same native service with a true Submit field.
    monkeypatch.setattr(s,'request',u.request)
    client=state['client']
    async def submit(method,url,**kw):
        assert method=='POST' and kw['params']['execute']=='true' and kw['follow_redirects'] is False
        state['submission_posts'].append((url,copy.deepcopy(kw['json'])))
        if state['submit_mode']=='timeout':raise TimeoutError('mock timeout')
        if state['submit_mode']=='cancel':raise asyncio.CancelledError()
        if state['submit_mode']=='400':
            from types import SimpleNamespace
            return SimpleNamespace(status_code=400,text='mock rejection')
        if url.endswith('/api/UpdateEvent'):
            assert kw['json']['submit'] is True
            state['event'].update(Status=1,IsSubmitted=True,ChangeCount=state['event']['ChangeCount']+1)
            state['classes']['ChangeCount']=state['event']['ChangeCount']
            state['history_rows']=[dict(UpdateEventHistoryId='000000000000b011',HistoryId=EVENT,HistoryMainlineId='000000000000b012',TxnId='000000000000b013',
                Submit=True,ChangeCount=1,TxnType=7578,ReversalStatus=0,Comments=kw['json']['comments'])]
        elif url.endswith('/api/TriageQualityObject'):state['event'].update(TriageComplete=True,ChangeCount=state['event']['ChangeCount']+1)
        else:raise AssertionError(url)
        from types import SimpleNamespace
        return SimpleNamespace(status_code=200,text='{"CompletionMsg":"isolated fixture"}')
    client.request=AsyncMock(side_effect=submit)
    monkeypatch.setattr(t.q,'get_quality_history',AsyncMock(return_value=json.dumps({'complete':True,'rows':[],'native_effect_verified':False})))
    return state


def final_fixture_review(ready):
    # This is explicit simulated human evidence, never an automatic MES verdict.
    reviewed=result(p.review_quality_transaction(ready['plan_id'],ready['plan_digest'],'resolve','fixture://final-native-event-submit',
        'Isolated final evidence reviewed, not a real MES transaction',conclusion='committed'))
    assert 'error' not in reviewed,reviewed


def test_pending_submit_preserves_original_detail_and_can_continue_to_triage_after_review(env):
    ready=prepare();assert ready.get('state')=='ready',ready
    saved=snap(ready);body=ready['request_preview']['body']
    assert body['submit'] is True and body['serviceDetail']==saved['original_detail']
    assert body['serviceDetail']['reporter']['instanceId']==OTHER and body['serviceDetail']['reportedDateGMT']=='2026-10-29T03:00:00Z'
    assert body['serviceDetail']['occurrenceDate']=='2026-10-29T10:00:00.1234567+08:00'
    assert not {'owner','role','category','status','triageComplete','isSubmitted','approvalSheetDetails'}&body.keys()
    assert env['load_count']==2 and not env['submission_posts']
    assert execute(ready)['state']=='unknown' and len(env['submission_posts'])==1
    assert env['event']['Status']==1 and env['event']['IsSubmitted'] and env['event']['TriageComplete'] is False
    evidence=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert evidence['manual_triage_context_candidate'] and not evidence['verified_success'] and not evidence['next_action_authorized']
    assert evidence['submission_history_candidates'][0]['Submit'] and evidence['submission_history_candidates'][0]['HistoryId']==EVENT
    assert 'error' in execute(ready) and 'error' in prepare()
    next_plan=result(t.prepare_quality_event_triage('E','Org'));assert next_plan.get('state')=='ready',next_plan
    assert 'error' in execute(next_plan) and len(env['submission_posts'])==1
    final_fixture_review(ready)
    assert execute(next_plan)['state']=='unknown' and env['event']['TriageComplete'] and len(env['submission_posts'])==2
    assert env['submission_posts'][0][0].endswith('/UpdateEvent') and env['submission_posts'][1][0].endswith('/TriageQualityObject')


def test_submission_context_is_readonly_even_when_notifications_configured(env):
    env['notice']=dict(smtp_configured=True,entries=[dict(instance_id=OTHER,notification_type=1,assignee_only=False)])
    out=result(u.get_quality_event_update_context('E','Org',purpose='submit_pending'))
    assert out['complete'] and not out['ready_for_execution'] and not out['submission_permission_verified']
    assert env['load_count']==1 and not p.PLAN_DB.exists() and not env['submission_posts']
    assert 'error' in prepare()
    ready=prepare(allow_notifications=True);assert ready.get('state')=='ready',ready


@pytest.mark.parametrize('field,value',[('Status',1),('IsSubmitted',True),('TriageComplete',True),('IsFrozen',True),('Category',3),('CDOTypeId',7734),
    ('ProcessModelId',OTHER),('ChecklistId',OTHER),('ChecklistSaved',True),('ReporterId',None),('InitiatorId',None),('OwnerId',None)])
def test_only_existing_unsubmitted_plain_draft_can_be_submitted(env,field,value):
    if field in ('ReporterId','InitiatorId'):env['extra'][field]=value
    else:env['event'][field]=value
    assert 'error' in prepare();assert not env['submission_posts']


@pytest.mark.parametrize('mode',['business_rules','unknown_rules','automatic_route','process','checklist','car_change','owner_change','missing_rule','approval','ncr','regulatory','signature','bad_classification','brief_empty'])
def test_automatic_side_effects_and_complex_context_are_not_silently_bypassed(env,mode):
    if mode=='business_rules':env['business_rules']=[{'InstanceID':OTHER}]
    elif mode=='unknown_rules':env['business_rules']=None
    elif mode=='automatic_route':env['details'][0]['AutoRoute']=True
    elif mode=='process':env['details'][0]['ProcessModelId']=OTHER
    elif mode=='checklist':env['details'][0]['ChecklistTemplateId']=OTHER
    elif mode=='car_change':env['details'][0]['CARRequired']=True
    elif mode=='owner_change':env['details'][0]['OwnerId']=OTHER
    elif mode=='missing_rule':env['maps']=[]
    elif mode=='approval':env['approvals']=[{'ApprovalSheetId':OTHER}]
    elif mode=='ncr':env['links']=[{'NonconformanceReportId':OTHER}]
    elif mode=='regulatory':env['regulatory']=[{'RegulatoryReportId':OTHER}]
    elif mode=='signature':env['roots']['Organizations']['QualityESigTxns']=[7578]
    elif mode=='bad_classification':env['classes']['ClassificationId']=None
    else:env['extra']['BriefDescription']=''
    assert 'error' in prepare() and not env['submission_posts']


@pytest.mark.parametrize('mode',['no_edit','not_owner','no_role','signature','approval','detail_changed','unknown_detail_schema','no_submit_echo'])
def test_native_true_submit_preview_must_preserve_context_and_detail(env,mode):
    if mode=='unknown_detail_schema':env['contract']['components']['schemas']['EventDetail']['properties']['unverifiedExtension']={'type':'string'}
    else:
        def hook(data):
            if mode=='no_edit':data['AvailableActions']=1
            elif mode=='not_owner':data['QualityObjectDetail']['UserIsOwner']=False
            elif mode=='no_role':data['QualityObjectDetail']['UserHasRole']=False
            elif mode=='signature':data['ESigRequirement']={'InstanceId':OTHER}
            elif mode=='approval':data['ApprovalSheetDetails']={}
            elif mode=='detail_changed':data['ServiceDetail']['Description']='changed'
            else:data['Submit']=False
        env['update_hook']=hook
    assert 'error' in prepare() and not env['submission_posts']


@pytest.mark.parametrize('mode',['draft','policy','rule','notification','reporter'])
def test_recheck_change_stops_single_submit(env,mode):
    ready=prepare();assert ready.get('state')=='ready',ready
    if mode=='draft':env['event']['ChangeCount']+=1;env['classes']['ChangeCount']+=1
    elif mode=='policy':env['roots']['Organizations']['QualityESigTxns']=[7578]
    elif mode=='rule':env['business_rules']=[{'InstanceID':OTHER}]
    elif mode=='notification':env['notice']=dict(smtp_configured=True,entries=[dict(instance_id=OTHER)])
    else:env['extra']['ReporterId']=OWNER
    assert 'error' in execute(ready) and not env['submission_posts']


@pytest.mark.parametrize('mode',['400','timeout','cancel'])
def test_uncertain_outcome_never_resubmits_or_bypasses_through_text_update(env,mode):
    ready=prepare();env['submit_mode']=mode
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:assert execute(ready)['state']=='unknown'
    assert 'error' in execute(ready) and len(env['submission_posts'])==1
    new=prepare();assert new.get('state')=='ready',new
    assert 'error' in execute(new) and len(env['submission_posts'])==1


@pytest.mark.parametrize('field,value',[('submit',False),('isSubmitted',True),('status',1),('ignoreProcessModel',True),('serviceDetail',{})])
def test_transport_rejects_status_or_payload_overrides(env,field,value):
    saved=snap(prepare());saved['body'][field]=value
    with pytest.raises(ValueError):p.transport(saved)


def test_mcp_registered_closed_inputs_and_confirmation(env):
    assert get_tool_func('prepare_quality_event_submission') is not None
    async def check():
        async with Client(mcp) as client:
            tools={x.name:x for x in await client.list_tools()};schema=tools['prepare_quality_event_submission'].input_schema
            assert schema['additionalProperties'] is False and not {'submit','status','is_submitted','employee','password','brief_description'}&schema['properties'].keys()
            reply=await client.call_tool('prepare_quality_event_submission',dict(event_name='E',organization_name='Org',comments='Submit reviewed draft'))
            ready=json.loads(reply.content[0].text);assert ready.get('state')=='ready',ready
            calls=[dict(function=dict(name='execute_quality_transaction',arguments=json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})))]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval and 'UpdateEvent' in plan_preview(calls)
    asyncio.run(check());assert not env['submission_posts']


@pytest.mark.parametrize('mode',['wrong_object','wrong_type','not_submit','wrong_marker','duplicate','too_many'])
def test_history_candidates_never_turn_invalid_association_into_success(env,mode):
    ready=prepare();execute(ready)
    if mode=='wrong_object':env['history_rows'][0]['HistoryId']=OTHER
    elif mode=='wrong_type':env['history_rows'][0]['TxnType']=7491
    elif mode=='not_submit':env['history_rows'][0]['Submit']=False
    elif mode=='wrong_marker':env['history_rows'][0]['Comments']='other request'
    elif mode=='duplicate':env['history_rows']*=2
    else:env['history_rows']*=21
    evidence=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert not evidence['verified_success'] and 'error' in evidence
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_missing_or_reverse_original_timestamps_cannot_be_filled_implicitly(env):
    env['extra'].update(OccurrenceDate='2026-10-30 10:00:00.1234567',OccurrenceDateGMT='2026-10-30 02:00:00.1234567')
    def reverse(data):data['ServiceDetail'].update(OccurrenceDate='2026-10-30T10:00:00.1234567+08:00',OccurrenceDateGMT='2026-10-30T02:00:00.1234567Z')
    env['load_hook']=reverse
    out=prepare();assert 'error' in out and '报告时间早于发生时间' in out['error'] and not env['submission_posts']


def test_local_journal_failure_after_submit_does_not_claim_not_submitted(env,monkeypatch):
    import sqlite3
    ready=prepare();original=p.db_connect;failed=[False]
    def database():
        if env['submission_posts'] and not failed[0]:failed[0]=True;raise sqlite3.OperationalError('simulated local journal fault')
        return original()
    monkeypatch.setattr(p,'db_connect',database)
    out=execute(ready);assert out['submitted'] is None and out['state']=='unknown' and not out['retry_allowed']
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='in_flight'
    assert 'error' in execute(ready) and len(env['submission_posts'])==1
