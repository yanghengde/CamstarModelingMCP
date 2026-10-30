import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from fastmcp import Client
from tools import mcp,quality_approval_decisions as d,quality_plans as p
from core import quality_signature_vault as vault_module,signature_channel,http_client
from tests.test_quality_approval_decisions import env as decision_env,actions_env,approval_env,base_env,prepare as decide
from tests.test_quality_approval_actions import execute,snap,EVENT,ORG,OWNER,ROLE,SHEET,ENTRY
from tests.test_quality_event_update import schema_fixture

SECRET='FAKE_HUMAN_SIGNATURE_SECRET'


def result(x):return json.loads(asyncio.run(x))
def prepare(**kw):return decide(**({'require_quality_esign':True}|kw))


@pytest.fixture
def env(decision_env,monkeypatch):
    s=decision_env;s['roots']['Organizations'].update(QualityESigTxns=[7839],UserRequiredForEsig=True)
    now=[10.0];vault=vault_module.SignatureVault(clock=lambda:now[0]);vault.enable('http://127.0.0.1:8030')
    monkeypatch.setattr(vault_module,'VAULT',vault)
    s.update(vault=vault,now=now,signed_hook=None,signed_requests=[],signed_schema_bad=False,native_attempts=[],expected_signer=OWNER)
    async def submit(method,url,**kw):
        assert method=='POST' and url.endswith('/api/SignApproval') and kw['params']['execute']=='true'
        body=kw['json'];detail=body['qualityESigDetail']
        assert detail['signerPassword']==SECRET and detail['signer']['instanceId']==s['expected_signer'] and detail['userRequired'] is True
        assert body['processESignatures'] is True and not {'employee','password','user','eSigDetails'}&body.keys()
        s['native_attempts'].append({'signer_id':detail['signer']['instanceId'],'password_present':True})
        return SimpleNamespace(status_code=200,text=SECRET+' FAKE_REFLECTED_PRIVATE_COOKIE')
    client=SimpleNamespace(request=AsyncMock(side_effect=submit),build_request=lambda method,url,headers:httpx.Request(method,url,headers=headers),is_closed=False)
    s['signature_client']=client
    monkeypatch.setattr(signature_channel,'get_client',lambda:client)
    monkeypatch.setattr(http_client,'get_headers',lambda:{'Authorization':'Bearer FAKE_PRIVATE_API_TOKEN'})
    async def transport(method,path,**kw):
        s['signed_requests'].append((method,path,copy.deepcopy(kw)))
        if method=='GET':
            schemas=copy.deepcopy(schema_fixture()['components']['schemas'])
            props={k:{'$ref':'#/components/schemas/'+v} for k,v in dict(qualityObject='NamedObjectRef',organization='NamedObjectRef',
                approvalSheet='NamedSubentityRef',approvalSheetEntry='SubentityRef',approvalDecision='NamedSubentityRef',qualityESigDetail='QualityESigDetail').items()}
            props.update(processESignatures={'type':'boolean'})
            schemas['SignApproval']={'properties':props}
            schemas['SignApprovalPerformEventModel']={'properties':{'event':{'$ref':'#/components/schemas/EventToPerform'},'inputData':{'$ref':'#/components/schemas/SignApproval'}}}
            if s['signed_schema_bad']:schemas['QualityESigDetail']['properties']['extraUnverified']={'type':'string'}
            return json.dumps({'components':{'schemas':schemas},'paths':{'/api/SignApproval/PerformEvent':{'post':{'requestBody':{'content':{'application/json':{'schema':{'$ref':'#/components/schemas/SignApprovalPerformEventModel'}}}}}}}})
        assert path=='/api/SignApproval/PerformEvent' and kw['body']['event']=={'eventName':'LoadESigDetails'} and 'execute' not in kw['params']
        assert 'qualityESigDetail' not in kw['body']['inputData'] and SECRET not in json.dumps(kw['body'])
        assert 'SignerPassword' not in kw['params']['$expand']
        from tests.test_quality_event_triage import echo
        data=echo(copy.deepcopy(kw['body']['inputData']));data.update(Employee={'InstanceId':OWNER},AvailableActions=513,IgnoreProcessModel=False,
            ESigRequirement=None,AdditionalESigReqs=[],ESigDetails=[],QualityESigDetail=dict(UserRequired=True,Signer=None,SignerPassword='FAKE_OVERRETURNED_SECRET'),
            QualityObjectDetail=dict(Status=s['event']['Status'],Category=s['event']['Category'],Organization={'InstanceId':ORG},EntryForApproval={'InstanceId':ENTRY},ProcessModel=None))
        if s['signed_hook']:s['signed_hook'](data)
        return json.dumps({'data':data})
    monkeypatch.setattr(d,'request',AsyncMock(side_effect=transport))
    yield s
    vault.disable()


def capture(s,ready):
    token=ready['signature_input_url'].split('#')[1];data,cookie=s['vault'].bootstrap(token)
    s['vault'].capture(token,cookie,data['csrf'],SECRET,True)
    return token,data['csrf'],cookie


def test_prepare_has_no_password_and_fixed_load_then_one_signed_submission(env):
    ready=prepare();assert ready.get('state')=='ready',ready
    assert ready['signature_input_status']['state']=='awaiting_input'
    assert ready['request_preview']['body']['processESignatures'] is True and 'qualityESigDetail' not in ready['request_preview']['body']
    assert 'FAKE_OVERRETURNED_SECRET' not in json.dumps(ready)
    env['signature_client'].request.assert_not_awaited()
    token,csrf,cookie=capture(env,ready);buffer=env['vault'].entries[token]['secret']
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['signature_input_status']['state']=='captured'
    out=execute(ready);assert out['state']=='unknown' and out['response_capture']=='omitted_for_signature_privacy'
    env['signature_client'].request.assert_awaited_once();assert not buffer
    assert env['signature_client'].request.await_args.kwargs['json']['qualityESigDetail']=={}
    persisted=result(p.get_quality_transaction_plan(ready['plan_id']))
    assert persisted['signature_input_status']['state']=='consumed'
    for secret in (SECRET,'FAKE_OVERRETURNED_SECRET','FAKE_PRIVATE_API_TOKEN'):
        assert secret not in json.dumps(persisted) and secret.encode() not in p.PLAN_DB.read_bytes()
    assert 'error' in execute(ready)


@pytest.mark.parametrize('mode',['no_input','expired','cancelled','restart','other_plan'])
def test_unavailable_one_time_input_cannot_reach_mes(env,mode):
    ready=prepare();assert ready.get('state')=='ready',ready
    if mode!='no_input':token,csrf,cookie=capture(env,ready)
    if mode=='expired':env['now'][0]+=121
    elif mode=='cancelled':env['vault'].cancel(token,cookie,csrf)
    elif mode=='restart':env['vault'].disable()
    elif mode=='other_plan':ready=prepare()
    assert 'error' in execute(ready);env['signature_client'].request.assert_not_awaited()


@pytest.mark.parametrize('change',['version','signature_policy','user_required','read_failure'])
def test_failed_recheck_consumes_and_clears_grant_without_submitting(env,change):
    ready=prepare();token,csrf,cookie=capture(env,ready);buffer=env['vault'].entries[token]['secret']
    if change=='version':env['event']['ChangeCount']+=1
    elif change=='signature_policy':env['roots']['Organizations']['QualityESigTxns']=[]
    elif change=='user_required':env['roots']['Organizations']['UserRequiredForEsig']=False
    else:d.request.side_effect=RuntimeError('FAKE_PRIVATE_API_TOKEN')
    out=execute(ready);assert 'error' in out and SECRET not in json.dumps(out) and 'FAKE_PRIVATE_API_TOKEN' not in json.dumps(out)
    assert not buffer;env['signature_client'].request.assert_not_awaited()
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['signature_input_status']['state']=='consumed'


@pytest.mark.parametrize('mode',['timeout','400','cancel','exception'])
def test_signed_unknown_never_retries_or_reflects_secret(env,mode):
    ready=prepare();token,csrf,cookie=capture(env,ready);buffer=env['vault'].entries[token]['secret']
    if mode=='400':env['signature_client'].request.side_effect=None;env['signature_client'].request.return_value=SimpleNamespace(status_code=400,text=SECRET)
    else:env['signature_client'].request.side_effect={'timeout':TimeoutError(SECRET),'cancel':asyncio.CancelledError(),'exception':RuntimeError(SECRET)}[mode]
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:
        out=execute(ready);assert out['state']=='unknown' and SECRET not in json.dumps(out)
    assert not buffer and 'error' in execute(ready);env['signature_client'].request.assert_awaited_once()
    assert SECRET.encode() not in p.PLAN_DB.read_bytes()


@pytest.mark.parametrize('mode',['no_policy','generic','additional','multi','wrong_signer','wrong_user_policy','schema','ui_unavailable'])
def test_only_required_single_current_actor_quality_signature_is_supported(env,mode):
    if mode=='no_policy':env['roots']['Organizations']['QualityESigTxns']=[]
    elif mode=='schema':env['signed_schema_bad']=True
    elif mode=='ui_unavailable':env['vault'].disable()
    else:
        def hook(data):
            if mode=='generic':data['ESigRequirement']={'InstanceId':OWNER}
            elif mode=='additional':data['AdditionalESigReqs']=[{}]
            elif mode=='multi':data['ESigDetails']=[{}]
            elif mode=='wrong_signer':data['QualityESigDetail']['Signer']={'InstanceId':ROLE}
            else:data['QualityESigDetail']['UserRequired']=False
        env['signed_hook']=hook
    assert 'error' in prepare();env['signature_client'].request.assert_not_awaited()


def test_mcp_accepts_only_requirement_flag_not_credentials(env):
    async def check():
        async with Client(mcp) as client:
            tool=next(t for t in await client.list_tools() if t.name=='prepare_quality_approval_decision')
            assert tool.input_schema['properties']['require_quality_esign']['default'] is False
            assert not {'password','signer_password','signature','quality_esig_detail'}&tool.input_schema['properties'].keys()
    asyncio.run(check())


def test_unknown_signed_attempt_prevents_new_capture_from_becoming_retry(env):
    first=prepare();capture(env,first);execute(first)
    second=prepare();assert second.get('state')=='ready',second
    token,csrf,cookie=capture(env,second);buffer=env['vault'].entries[token]['secret']
    assert 'error' in execute(second) and not buffer
    env['signature_client'].request.assert_awaited_once()


def test_post_submission_local_journal_failure_never_reports_not_submitted(env,monkeypatch):
    import sqlite3
    ready=prepare();token,csrf,cookie=capture(env,ready);buffer=env['vault'].entries[token]['secret']
    original=p.db_connect;failed=[False]
    def database():
        if env['native_attempts'] and not failed[0]:
            failed[0]=True;raise sqlite3.OperationalError(SECRET)
        return original()
    monkeypatch.setattr(p,'db_connect',database)
    out=execute(ready)
    assert out['state']=='unknown' and out['submitted'] is None and not out['retry_allowed']
    assert SECRET not in json.dumps(out) and not buffer
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='in_flight'
    assert 'error' in execute(ready);env['signature_client'].request.assert_awaited_once()


def test_role_substitution_and_quality_signature_keep_actual_signer_bound(env,monkeypatch):
    from tests.test_quality_approval_actions import OTHER
    env['entries'][0]['SubstituteOption']=2;env['expected_signer']=OTHER
    env['signed_hook']=lambda data:data.update(Employee={'InstanceId':OTHER})
    original_asset=d.t.asset
    async def asset(kind,**kw):
        if kind=='owner' and kw.get('key')==OTHER:return dict(entity='Employees',InstanceID=OTHER,Name='Actual role signer',IsFrozen=False,ChangeCount=1)
        return await original_asset(kind,**kw)
    monkeypatch.setattr(d.t,'asset',AsyncMock(side_effect=asset))
    original_sql=d.q.sql_rows
    async def sql(query,maximum):
        if ' FROM sys.objects ' in query:return [dict(FunctionId=91,DefinitionHash='a'*64)]
        if ' FROM EmployeeRole WHERE ' in query:return [dict(EmployeeRoleId='000000000000c001',EmployeeId=OTHER,RoleId=ROLE,OrganizationId=ORG,
            IsFrozen=False,PropagateToChildOrgs=True,ChangeCount=1)]
        if 'FROM dbo.csiAuthGetAllowedUsers(' in query:return [dict(RowCount=1)]
        return await original_sql(query,maximum)
    monkeypatch.setattr(d.q,'sql_rows',AsyncMock(side_effect=sql))
    ready=prepare(allow_role_substitution=True);assert ready.get('state')=='ready',ready
    assert ready['server_context']['employee_id']==OTHER and snap(ready)['quality_signature']['signer_id']==OTHER
    capture(env,ready);assert execute(ready)['state']=='unknown'
    assert env['native_attempts']==[dict(signer_id=OTHER,password_present=True)]


@pytest.mark.parametrize('exact_mainline',[True,False])
def test_browser_capture_to_submission_and_native_signature_evidence(env,monkeypatch,exact_mainline):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web import quality_signatures as web
    from tools import quality_approval_history as h
    from tests.test_quality_approval_actions import DECISION
    ready=prepare();assert ready.get('state')=='ready',ready
    monkeypatch.setattr(web,'VAULT',env['vault']);app=FastAPI();app.include_router(web.router)
    token=ready['signature_input_url'].split('#')[1];origin='http://127.0.0.1:8030'
    with TestClient(app,base_url=origin,client=('127.0.0.1',50100)) as browser:
        opened=browser.post('/api/quality-signature/open',headers={'Origin':origin,'X-Signature-Challenge':token})
        headers={'Origin':origin,'X-Signature-Challenge':token,'X-Signature-CSRF':opened.json()['csrf']}
        captured=browser.post('/api/quality-signature/capture',headers=headers,json={'password':SECRET,'approved':True})
        assert captured.status_code==200 and SECRET not in captured.text
    assert execute(ready)['state']=='unknown'
    txn,main_id,sign_id=['000000000000c00'+str(i) for i in (2,3,4)]
    row=dict.fromkeys(h.SCHEMA['SignApprovalHistory'].split(','));row.update(SignApprovalHistoryId=sign_id,ApprovalSheetId=SHEET,
        QualityObjectId=EVENT,ApprovalSheetEntryId=ENTRY,ApprovalCycleGMT=env['sheets'][0]['LastApprovalCycleGMT'],ApproverId=OWNER,
        ApproverRoleId=ROLE,ApprovedById=OWNER,ApprovalDecisionId=DECISION,DecisionType=10,SheetLevel=1,
        LastCompletedOnGMT='2026-10-30T01:00:00Z',HistoryMainlineId=main_id if exact_mainline else None,HistoryId=txn,TxnId=txn,ChangeCount=1,Comments=ready['request_preview']['body']['comments'])
    env['entries'][0].update(Status=40,DecisionType=10,ApprovedById=OWNER,ApprovalDecisionId=DECISION,LastDecisionId=sign_id,
        LastCompletedOnGMT=row['LastCompletedOnGMT'],Comments=row['Comments'])
    env['sheets'][0]['Status']=40;env['inquiry'].update(CompletionApprovalStatus=40,EntryForApproval=None)
    original=d.q.sql_rows
    async def sql(query,maximum):
        if ' FROM SignApprovalHistory WHERE ' in query:return [copy.deepcopy(row)]
        if ' FROM ApprovalSheetHistory WHERE ' in query:return []
        if 'FROM HistoryMainline WHERE ' in query:return [dict(HistoryMainlineId=main_id,TxnId=txn,TxnType=7839,QualityESigConfirmed=True,ChangeCount=1)]
        return await original(query,maximum)
    monkeypatch.setattr(d.q,'sql_rows',AsyncMock(side_effect=sql))
    inspected=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    proof=inspected['quality_signature_history']['candidates'][0]
    assert proof['mainline_association_verified'] is exact_mainline and not inspected['verified_success']
    assert proof['quality_esig_confirmed'] is (True if exact_mainline else None)
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'
