import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import quality_plans as p
from agent.safety import plan_preview

CID='0000000000000101';RID='0000000000000201';ORG='0000000000000301'
def result(coro):return json.loads(asyncio.run(coro))


def echo(value):
    if isinstance(value,dict):return {p.FIELD_CASE.get(k,k[0].upper()+k[1:]):echo(v) for k,v in value.items()}
    if isinstance(value,list):return [echo(v) for v in value]
    if type(value) is bool:return 'true' if value else 'false'
    if type(value) is int:return str(value)
    return value


@pytest.fixture
def setup(monkeypatch,tmp_path):
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'quality.sqlite3')
    state={'record':None,'status':{'Qty':1,'Status':1},'counter':0,
        'assignments':[{'NonconformanceReportId':RID,'FieldId':10019,'Sequence':0,'NCRFailureCodesId':RID,'NCRFailureCodeName':'Failure-A','FailureCodeChangeCount':1}],
        'object_detail':{'Status':'1','Category':'5','Organization':{'InstanceId':ORG}},
        'closure_options':[{'Value':'1','Name':'Close','Label':'Close - No Action Required'}],
        'approval_required':'true','direct_sheets':[]}
    monkeypatch.setattr(p.reads,'sql_rows',AsyncMock(side_effect=lambda *x:copy.deepcopy(state['direct_sheets'])))
    async def find(*a,**kw):return copy.deepcopy(state['record'])
    monkeypatch.setattr(p.reads,'find_record',find)
    monkeypatch.setattr(p.reads,'ncr_failure_assignments',AsyncMock(side_effect=lambda *a:copy.deepcopy(state['assignments'])))
    monkeypatch.setattr(p.reads,'object_details',AsyncMock(side_effect=lambda *a:{'QualityObject':{'Name':'A','InstanceId':RID},'QualityObjectInquiryDetail':copy.deepcopy(state['object_detail'])}))
    monkeypatch.setattr(p.reads,'closure_options',AsyncMock(side_effect=lambda *a:copy.deepcopy(state['closure_options'])))
    async def reference(kind,name):
        if not name:raise ValueError('empty code')
        return {'name':name,'instanceId':ORG if kind=='organization' else RID}
    monkeypatch.setattr(p.reads,'resolve_reference',reference)
    async def container(*a,**kw):return {'name':'C','instanceId':CID},copy.deepcopy(state['status'])
    monkeypatch.setattr(p.reads,'container_identity',container)
    async def preview(*a,**kw):
        state['counter']+=1;data=echo(kw['body'])
        if 'ServiceDetail' in kw['params'].get('$expand',''):data.setdefault('ServiceDetail',{})
        if 'ServiceDetail' in data:
            dtype='7734' if 'CAPA' in a[1] else '7209'
            data['ServiceDetail'].update(InstanceId=f"{state['counter']+100:016x}",Parent={'InstanceId':f"{state['counter']+200:016x}"},CDOTypeId=dtype)
        if a[1]=='/api/CloseQualityObject':
            data['QualityObjectDetail']=copy.deepcopy(state['object_detail'])
            data['ApprovalRequired']=state['approval_required']
        return json.dumps(data)
    request=AsyncMock(side_effect=preview);monkeypatch.setattr(p,'request',request)
    client=SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200,text='{"value":"completed"}')))
    monkeypatch.setattr(p,'get_client',lambda:client)
    return state,request,client


def ncr(**kw):return result(p.prepare_nonconformance_report('create','N','C',failure_codes=['Failure-A'],**kw))
def capa(**kw):return result(p.prepare_capa('create','A','Org',brief_description='test',**kw))


def test_ordinary_capa_update_cannot_bypass_existing_approval_cycle(setup):
    setup[0]['record']=dict(CAPAId=RID,CAPAName='A',Category=5,Status=8,IsFrozen=False,OrganizationId=ORG)
    setup[0]['direct_sheets']=[{'ApprovalSheetId':CID}]
    out=result(p.prepare_capa('update','A','Org',description='Bypass attempt'))
    assert 'error' in out and 'prepare_capa_approval_revision' in out['error']
    setup[1].assert_not_awaited();setup[2].request.assert_not_awaited()
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))


def test_ncr_create_wire_contract_and_false_preview(setup):
    ready=ncr(ncr_comment='defect observed')
    assert ready['state']=='ready'
    body=ready['request_preview']['body']
    assert body['nonconformanceName']=='N' and body['container']['instanceId']==CID
    assert body['ncrFailureCodes']==[{'name':'Failure-A','instanceId':RID}]
    assert 'serviceDetail' not in body
    assert setup[1].await_args.kwargs['params']['execute']=='false'
    assert body['comments'].startswith('MCP-QUAL:')
    setup[2].request.assert_not_awaited()


@pytest.mark.parametrize('codes',[None,[],['A','A'],[''],[1]])
def test_invalid_failure_list_no_plan(setup,codes):
    out=result(p.prepare_nonconformance_report('create','N','C',failure_codes=codes))
    assert 'error' in out
    setup[1].assert_not_awaited();setup[2].request.assert_not_awaited()


def test_ncr_update_uses_required_nested_detail_and_comments(setup):
    setup[0]['record']={'NonconformanceReportId':RID,'NonconformanceReportName':'N','NCRStatus':1}
    ready=result(p.prepare_nonconformance_report('update','N',failure_codes=['Failure-A'],corrective_comments='operator action',cause_code_name='Cause'))
    body=ready['request_preview']['body']
    assert body['nonconformanceReport']['instanceId']==RID
    assert body['serviceDetail']['correctiveActionComments']=={'comments':'operator action'}
    assert body['serviceDetail']['ncrCauseCode']['name']=='Cause'
    assert 'ncrFailureCodes' not in body and 'ncrFailureCodes' in body['serviceDetail']


def test_ncr_update_omission_preserves_all_codes_in_order_and_binds_before(setup):
    setup[0]['record']={'NonconformanceReportId':RID,'NonconformanceReportName':'N'}
    setup[0]['assignments'].append({'NonconformanceReportId':RID,'FieldId':10019,'Sequence':1,'NCRFailureCodesId':CID,'NCRFailureCodeName':'Failure-B','FailureCodeChangeCount':1})
    ready=result(p.prepare_nonconformance_report('update','N',ncr_comment='investigating'))
    assert ready['request_preview']['body']['serviceDetail']['ncrFailureCodes']==[
        {'name':'Failure-A','instanceId':RID},{'name':'Failure-B','instanceId':CID}]
    assert len(ready['before']['failure_code_assignments'])==2
    setup[0]['assignments'].pop()
    assert 'error' in execute(ready)
    setup[2].request.assert_not_awaited()


def test_ncr_explicit_replacement_still_binds_old_assignments(setup):
    setup[0]['record']={'NonconformanceReportId':RID,'NonconformanceReportName':'N'}
    ready=result(p.prepare_nonconformance_report('update','N',failure_codes=['Failure-New']))
    assert ready['request_preview']['body']['serviceDetail']['ncrFailureCodes'][0]['name']=='Failure-New'
    setup[0]['assignments'][0]['FailureCodeChangeCount']=2
    assert 'error' in execute(ready)
    setup[2].request.assert_not_awaited()


def test_ncr_omission_with_empty_assignments_or_no_changes_does_not_plan(setup):
    setup[0]['record']={'NonconformanceReportId':RID,'NonconformanceReportName':'N'}
    assert 'error' in result(p.prepare_nonconformance_report('update','N'))
    setup[0]['assignments']=[]
    assert 'error' in result(p.prepare_nonconformance_report('update','N',ncr_comment='explanation'))
    setup[1].assert_not_awaited()


def test_ncr_close_requires_resolution_not_a_guessed_status(setup):
    setup[0]['record']={'NonconformanceReportId':RID,'NonconformanceReportName':'N','NCRStatus':77}
    assert 'error' in result(p.prepare_nonconformance_report('close','N'))
    ready=result(p.prepare_nonconformance_report('close','N',resolution_code_name='Resolved',ncr_comment='closed after inspection'))
    assert ready['request_preview']['path']=='/api/NCRClose'
    assert ready['request_preview']['body']['serviceDetail']['ncrResolutionCode']['name']=='Resolved'
    assert 'ncrStatus' not in ready['request_preview']['body']['serviceDetail']


@pytest.mark.parametrize('action',['delete','escalate','anything'])
def test_python_direct_calls_reject_unknown_action(setup,action):
    assert 'error' in result(p.prepare_nonconformance_report(action,'N','C',failure_codes=['A']))
    assert 'error' in result(p.prepare_capa(action,'A','Org',brief_description='test'))
    setup[1].assert_not_awaited()


def test_capa_category_five_organization_and_draft_are_explicit(setup):
    ready=capa(is_systemic_issue=False,occurrence_date='2026-10-25T10:00:00+08:00')
    body=ready['request_preview']['body']
    assert body['category']==5 and body['submit'] is False
    assert body['organization']=={'name':'Org','instanceId':ORG}
    assert body['serviceDetail']['isSystemicIssue'] is False
    assert ready['server_context']['detail_type']=='7734'
    text=plan_preview([{'function':{'name':'execute_quality_transaction','arguments':json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})}}])
    assert 'CreateCAPA' not in text or 'CAPA' in text
    assert 'Org' in text and 'test' in text


@pytest.mark.parametrize('field,value',[('submit','true'),('is_systemic_issue',1),('occurrence_date','2026-10-25')])
def test_capa_bool_and_timezone_strict(setup,field,value):
    assert 'error' in capa(**{field:value})
    setup[1].assert_not_awaited()


def test_closed_frozen_or_moved_capa_update_is_rejected(setup):
    row={'CAPAId':RID,'CAPAName':'A','Category':5,'Status':7,'IsFrozen':False,'OrganizationId':ORG}
    setup[0]['record']=row
    for field,value in [('IsFrozen',True),('Status',5),('OrganizationId',CID)]:
        old=row[field];row[field]=value
        assert 'error' in result(p.prepare_capa('update','A','Org',description='new'))
        row[field]=old
    assert result(p.prepare_capa('update','A','Org',description='new'))['state']=='ready'


@pytest.mark.parametrize('changes',[{'submit':True},{'classification_name':'Class'}])
def test_capa_root_only_updates_still_load_and_verify_detail_type(setup,changes):
    existing_capa(setup)
    ready=result(p.prepare_capa('update','A','Org',**changes))
    assert ready['state']=='ready' and 'serviceDetail' not in ready['request_preview']['body']
    assert ready['server_context']['detail_type']=='7734'
    assert 'ServiceDetail' in setup[1].await_args.kwargs['params']['$expand']


def closure():return result(p.prepare_capa_closure('A','Org','Resolved','Effectiveness review completed.'))


def existing_capa(setup):
    setup[0]['record']={'CAPAId':RID,'CAPAName':'A','Category':5,'Status':1,'IsFrozen':False,'OrganizationId':ORG,'ChangeCount':1}


def test_capa_closure_contract_uses_quality_resolution_and_keeps_approval(setup):
    existing_capa(setup);ready=closure()
    assert ready['state']=='ready'
    assert ready['request_preview']['path']=='/api/CloseQualityObject'
    body=ready['request_preview']['body']
    assert body['qualityObject']=={'name':'A','instanceId':RID} and body['resolutionAction']==1
    assert body['qualityResolutionCode']=={'name':'Resolved','instanceId':RID}
    assert set(body)=={'qualityObject','organization','resolutionAction','qualityResolutionCode','closeDescription','comments'}
    assert ready['server_context']['approval_context']['ApprovalRequired']=='true'
    assert ready['dynamic_validation_verified'] is False
    text=plan_preview([{'function':{'name':'execute_quality_transaction','arguments':json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})}}])
    assert 'Resolved' in text and 'Effectiveness' in text and 'ApprovalRequired' in text
    assert execute(ready)['state']=='unknown'
    setup[2].request.assert_awaited_once()


@pytest.mark.parametrize('field,value',[('Category',1),('Status',5),('Status',6),('Status',99),('Status',True),('IsFrozen',True),('IsFrozen',None),('OrganizationId',CID)])
def test_capa_closure_rejects_unsupported_current_record(setup,field,value):
    existing_capa(setup);setup[0]['record'][field]=value
    assert 'error' in closure()
    setup[1].assert_not_awaited();setup[2].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('Category','2'),('Status','5'),('Organization',{'InstanceId':CID})])
def test_capa_closure_inquiry_must_match_record(setup,field,value):
    existing_capa(setup);setup[0]['object_detail'][field]=value
    assert 'error' in closure()
    setup[1].assert_not_awaited()


def test_capa_closure_does_not_guess_close_code_or_accept_blank_description(setup):
    existing_capa(setup);setup[0]['closure_options']=[{'Value':'2','Name':'AttachToCAPA','Label':'Attach'}]
    assert 'error' in closure()
    assert 'error' in result(p.prepare_capa_closure('A','Org','Resolved',' '))
    setup[1].assert_not_awaited()


@pytest.mark.parametrize('change',['missing_detail','wrong_organization','boolean_status','outer_error','wrong_resolution'])
def test_capa_closure_rejects_incomplete_wrong_or_rejected_service_echo(setup,change):
    existing_capa(setup);original=setup[1].side_effect
    async def altered(*a,**kw):
        data=json.loads(await original(*a,**kw))
        if change=='missing_detail':data.pop('QualityObjectDetail')
        elif change=='wrong_organization':data['QualityObjectDetail']['Organization']['InstanceId']=CID
        elif change=='boolean_status':data['QualityObjectDetail']['Status']=True
        elif change=='outer_error':data={'data':data,'ErrorCode':'denied'}
        else:data['QualityResolutionCode']['InstanceId']=CID
        return json.dumps(data)
    setup[1].side_effect=altered
    assert 'error' in closure()
    setup[2].request.assert_not_awaited()


@pytest.mark.parametrize('change',['approval','options','record','inquiry'])
def test_capa_closure_rechecks_workflow_options_and_snapshot(setup,change):
    existing_capa(setup);ready=closure()
    if change=='approval':setup[0]['approval_required']='false'
    elif change=='options':setup[0]['closure_options'][0]['Value']='4'
    elif change=='record':setup[0]['record']['ChangeCount']=2
    else:setup[0]['object_detail']['CompletionApprovalStatus']='3'
    assert 'error' in execute(ready)
    setup[2].request.assert_not_awaited()


def test_capa_closure_and_update_share_unknown_object_hold_and_inspection(setup,monkeypatch):
    existing_capa(setup)
    close=closure();update=result(p.prepare_capa('update','A','Org',description='new'))
    assert execute(close)['state']=='unknown'
    assert 'error' in execute(update)
    monkeypatch.setattr(p.reads,'get_quality_history',AsyncMock(return_value='{"page_complete":true}'))
    inspected=result(p.inspect_quality_transaction(close['plan_id'],close['plan_digest']))
    assert inspected['current_record']['CAPAId']==RID and inspected['verified_success'] is False
    assert p.reads.get_quality_history.await_args.args==('quality_object','A',RID)
    setup[2].request.assert_awaited_once()


def test_renaming_capa_does_not_bypass_unknown_same_id(setup):
    existing_capa(setup)
    assert execute(closure())['state']=='unknown'
    setup[0]['record']['CAPAName']='Renamed'
    ready=result(p.prepare_capa('update','Renamed','Org',description='new'))
    assert 'error' in execute(ready)
    setup[2].request.assert_awaited_once()


def test_capa_closure_ignores_transient_inquiry_instance_ids(setup,monkeypatch):
    existing_capa(setup);counter=iter(range(10))
    async def inquiry(*a):
        transient=f'{next(counter)+1000:016x}'
        return {'InstanceId':transient,'QualityObject':{'Name':'A','InstanceId':RID},
            'QualityObjectInquiryDetail':{**setup[0]['object_detail'],'InstanceId':transient,'Parent':{'InstanceId':transient}}}
    monkeypatch.setattr(p.reads,'object_details',inquiry)
    ready=closure()
    assert execute(ready)['state']=='unknown'
    setup[2].request.assert_awaited_once()


def test_transient_preview_ids_do_not_invalidate_plan_but_real_input_does(setup):
    ready=capa()
    assert execute(ready)['state']=='unknown'
    setup[2].request.assert_awaited_once()
    assert setup[2].request.await_args.kwargs['params']=={'execute':'true'}
    assert setup[2].request.await_args.kwargs['follow_redirects'] is False
    assert execute(ready)['submitted'] is False


def test_same_object_unknown_cannot_be_bypassed_by_new_plan(setup):
    first=ncr();second=ncr()
    assert execute(first)['state']=='unknown'
    assert 'error' in execute(second)
    assert setup[2].request.await_count==1


def test_changed_context_and_expired_plan_never_write(setup):
    ready=ncr();setup[0]['status']['Qty']=2
    assert 'error' in execute(ready)
    setup[2].request.assert_not_awaited()
    setup[0]['status']['Qty']=1
    with p.db_connect() as db:db.execute('UPDATE plans SET created=?',(time.time()-4000,))
    assert 'error' in execute(ready)


def test_cancelled_request_persists_unknown_and_attempt(setup):
    ready=ncr();setup[2].request.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):execute(ready)
    stored=result(p.get_quality_transaction_plan(ready['plan_id']))
    assert stored['state']=='unknown' and stored['outcome']['interrupted']
    with p.db_connect() as db:assert db.execute('SELECT finished FROM attempts').fetchone()[0] is not None


def test_inspection_is_evidence_not_success_and_does_not_release(setup):
    ready=ncr();execute(ready)
    setup[0]['record']={'NonconformanceReportId':RID,'NonconformanceReportName':'N'}
    original=p.reads.get_quality_history;p.reads.get_quality_history=AsyncMock(return_value='{"page_complete":true,"data":{"Histories":[]}}')
    try:checked=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    finally:p.reads.get_quality_history=original
    assert checked['verified_success'] is False and checked['state_changed'] is False
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_manual_review_retains_original_and_never_reopens(setup):
    ready=ncr();execute(ready)
    args={'plan_id':ready['plan_id'],'plan_digest':ready['plan_digest'],'action':'resolve','evidence_reference':'final-audit/123','review_note':'Human checked the final server transaction.','conclusion':'committed'}
    reviewed=result(p.review_quality_transaction(**args))
    assert reviewed['state']=='human_committed' and reviewed['verified_success'] is False
    assert reviewed['human_attestation']['previous_outcome']['raw_response']=='{"value":"completed"}'
    assert 'error' in execute(ready)


def test_recovery_requires_attempt_and_preserves_unknown_hold(setup):
    ready=ncr()
    args={'plan_id':ready['plan_id'],'plan_digest':ready['plan_digest'],'action':'recover','evidence_reference':'operator/123','review_note':'Confirmed no active local worker.'}
    assert 'error' in result(p.review_quality_transaction(**args))
    with p.db_connect() as db:
        db.execute("UPDATE plans SET state='in_flight'")
        db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)',(ready['plan_id'],'a',time.time()))
    assert result(p.review_quality_transaction(**args))['state']=='unknown'
    assert 'error' in execute(ready)


def test_audit_failure_rolls_back_release(setup):
    ready=ncr();execute(ready)
    with p.db_connect() as db:db.execute("CREATE TRIGGER fail_review BEFORE INSERT ON reviews BEGIN SELECT RAISE(ABORT,'audit fail'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):result(p.review_quality_transaction(ready['plan_id'],ready['plan_digest'],'resolve','audit/1','Human checked final transaction.','not_committed'))
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_real_capa_echo_normalization_and_wrong_echo_rejected():
    from pathlib import Path
    data=json.loads(Path('docs/quality-fixtures/capa-preview-echo.json').read_text(encoding='utf-8'))
    response=data['response'].get('data',data['response'])
    assert p.wire_matches(data['request'],response)
    wrong=copy.deepcopy(response);wrong['Submit']='true'
    assert not p.wire_matches(data['request'],wrong)
