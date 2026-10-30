"""Typed adapter fixtures are hypothetical extensions, never deployed-contract evidence."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path
import httpx
import pytest
from fastmcp import Client
from tools import mcp,quality_plans as p,quality as q,quality_native_signatures as n
from core import signature_channel,quality_signature_vault as v,http_client
from tests.test_quality_plans import setup,result,echo,CID,RID

REQ='000000000000a001';DETAIL='000000000000a002';ROLE='000000000000a003';COSIGN_ROLE='000000000000a004';MEANING='000000000000a005'
ACTOR='000000000000a006';FACTORY='000000000000a007';REASON='000000000000a008'
PRIVATE='DUMMY_NATIVE_SIGNATURE_SECRET'


def reference(name,key):return {'Name':name,'InstanceID':key}


def archived():return copy.deepcopy(json.loads(Path('docs/quality-fixtures/contracts.json').read_text(encoding='utf8'))['Shopfloor/NCRUpdate'])


def adapter_contract():
    doc=archived();schemas=doc['components']['schemas']
    schemas['EventToPerform']={'properties':{'eventName':{'type':'string'}}}
    schemas['NCRUpdatePerformEventModel']={'properties':{'event':{'$ref':'#/components/schemas/EventToPerform'},'inputData':{'$ref':'#/components/schemas/NCRUpdate'}}}
    doc['paths']['/api/NCRUpdate/PerformEvent']={'post':{'requestBody':{'content':{'application/json':{'schema':{'$ref':'#/components/schemas/NCRUpdatePerformEventModel'}}}}}}
    schemas['ESigServiceDetail']['properties']['captureDetails']['items']={'$ref':'#/components/schemas/ESigPasswordCapture'}
    schemas['ESigPasswordCapture']={'properties':{k:{'$ref':'#/components/schemas/NamedObjectRef'} for k in ('signer','cosigner','cosignReason')}}
    schemas['ESigPasswordCapture']['properties'].update(signerPassword={'type':'string'},cosignerPassword={'type':'string'})
    return doc


@pytest.fixture
def env(setup,monkeypatch):
    state,base_request,old_client=setup
    row=dict.fromkeys(q.NCR_COLUMNS.split(','));row.update(NonconformanceReportId=RID,NonconformanceReportName='N',ContainerId=CID,NCRStatus=1,ChangeCount=1)
    state['record']=row
    vault=v.SignatureVault();vault.enable('http://127.0.0.1:8030');monkeypatch.setattr(v,'VAULT',vault)
    group='000000000000a009'
    people={name:reference(name,f'{0xb000+index:016x}')|{'IsFrozen':False,'ESigRoleGroup':reference('ESig roles',group)} for index,name in enumerate(('Signer A','Signer B','Cosigner C'))}
    detail=reference('Review',DETAIL)|{'Parent':reference('Requirement',REQ),'IsFrozen':False,'Role':reference('Reviewer',ROLE),
        'CosignerRole':reference('Supervisor',COSIGN_ROLE),'Meaning':reference('Checked',MEANING),'SignatureCount':2,'VerificationMethod':1}
    state.update(vault=vault,doc=adapter_contract(),people=people,requirement=detail,native_posts=[],model_reads=[],load_hook=None,post_mode=None)
    async def get(ctx,entity,key=None,params=None):
        state['model_reads'].append((entity,key,params))
        if entity=='Employees':
            assert 'Password' not in params['$select'] and 'password' not in params['$select']
            if key is None:
                rows=[x for name,x in people.items() if params['$filter']=="Name eq '"+name+"'"]
                return {'value':copy.deepcopy(rows)}
            return copy.deepcopy(next(x for x in people.values() if x['InstanceID']==key))
        if entity=='ESigRoleGroups':return reference('ESig roles',group)|{'IsFrozen':False,'ResolvedEntries':[reference('Reviewer',ROLE)|{'IsFrozen':False},reference('Supervisor',COSIGN_ROLE)|{'IsFrozen':False}]}
        if entity=='ESigRequirements':return reference('Requirement',REQ)|{'IsFrozen':False,'ESigReqDetails':[copy.deepcopy(detail)]}
        if entity=='ESigCosignReasons':
            row=reference('Witnessed',REASON)|{'IsFrozen':False}
            return {'value':[row]} if key is None else row
        raise AssertionError(entity)
    monkeypatch.setattr(n.ReadContext,'get',get)
    async def request(method,path,**kw):
        if method=='GET':return json.dumps(state['doc'])
        assert path=='/api/NCRUpdate/PerformEvent' and kw['body']['event']['eventName']=='LoadESigDetails'
        assert 'execute' not in kw['params'] and 'eSigDetails' not in kw['body']['inputData']
        data=echo(kw['body']['inputData'])
        e={k:copy.deepcopy(detail[k]) for k in ('SignatureCount','VerificationMethod','Role','CosignerRole','Meaning')}
        e.update(ESigReqDetail=reference('Review',DETAIL),CaptureDetails=[])
        data.update(Container=reference('Container',CID),Employee=reference('Operator',ACTOR),Factory=reference('Factory',FACTORY),
            ESigRequirement=None,AdditionalESigReqs=[reference('Requirement',REQ)],ESigDetails=[e])
        if state['load_hook']:state['load_hook'](data)
        return json.dumps({'data':data})
    monkeypatch.setattr(n,'request',AsyncMock(side_effect=request))
    from tools import quality_ncr_escalation
    monkeypatch.setattr(quality_ncr_escalation,'container_state',AsyncMock(return_value=dict(ContainerId=CID,ContainerName='Container',Status=1,CurrentHoldCount=0,ChangeCount=1)))
    async def submit(method,url,**kw):
        assert url.endswith('/api/NCRUpdate') and kw['params']['execute']=='true' and kw['headers']['Authorization']=='Bearer DUMMY_SESSION'
        body=kw['json'];assert body['processESignatures'] is True
        esigs=body['eSigDetails'];assert len(esigs)==1 and esigs[0]['eSigReqDetail']['parent']['instanceId']==REQ
        captures=esigs[0]['captureDetails'];assert len(captures)==2
        assert all(c['signerPassword']==PRIVATE for c in captures)
        state['native_posts'].append(copy.deepcopy(body))
        if state['post_mode']=='timeout':raise TimeoutError(PRIVATE)
        return SimpleNamespace(status_code=400 if state['post_mode']=='400' else 200,text=PRIVATE)
    client=SimpleNamespace(request=AsyncMock(side_effect=submit),build_request=lambda method,url,headers:httpx.Request(method,url,headers=headers),is_closed=False)
    state['native_client']=client
    # Synthetic adapter acceptance only; actual native parameter mapping is NOT
    # verified by these mocks. Production has no switch to bypass this gate.
    monkeypatch.setattr(n,'native_execution_support',lambda:dict(verified=True,source='isolated_synthetic_fixture_only'))
    monkeypatch.setattr(signature_channel,'get_client',lambda:client)
    monkeypatch.setattr(http_client,'get_headers',lambda:{'Authorization':'Bearer DUMMY_SESSION'})
    yield state
    vault.disable()


def assignments(cosign=False):
    return [n.NativeSignatureAssignment(requirement_detail_id=DETAIL,signer_name=name,
        cosigner_name='Cosigner C' if cosign and index==0 else None,cosign_reason_name='Witnessed' if cosign and index==0 else None)
        for index,name in enumerate(('Signer A','Signer B'))]


def prepare(**kw):return result(p.prepare_nonconformance_report('update','N',ncr_comment='Reviewed',require_native_esign=True,native_signature_assignments=assignments(),**kw))


def capture(state,ready):
    token=ready['signature_input_url'].split('#')[1];data,cookie=state['vault'].bootstrap(token)
    state['vault'].capture_many(token,cookie,data['csrf'],[dict(slot_id=x['slot_id'],password=PRIVATE,approved=True) for x in data['participants']],True)
    return state['vault'].entries[token]['secret']


def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))


def test_archived_standard_rest_contract_does_not_support_password_capture():
    report=n.contract(archived())
    assert not report['typed_password_submit_contract_verified'] and not report['ready_for_execution']
    assert any('ESigPasswordCapture' in x for x in report['blockers'])


def test_native_authentication_requires_more_than_a_password_schema():
    support=n.native_execution_support()
    assert support['verified'] is False
    assert n.contract(adapter_contract())['typed_password_submit_contract_verified']
    assert not n.contract(adapter_contract())['native_execution_support']['verified']


def test_unverified_native_parameter_flow_stops_even_with_typed_schema(env,monkeypatch):
    monkeypatch.setattr(n,'native_execution_support',lambda:dict(verified=False,blocker='原生认证映射待验收'))
    out=prepare()
    assert 'error' in out and not env['vault'].entries
    assert not any(call.args[0]=='POST' for call in n.request.await_args_list)
    env['native_client'].request.assert_not_awaited()


def test_actual_contract_gap_stops_before_ui_or_native_preview(env):
    env['doc']=archived();out=prepare()
    assert 'error' in out and not env['vault'].entries
    assert not any(call.args[0]=='POST' for call in n.request.await_args_list)
    env['native_client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['base_type','readonly_password','missing_password','unknown_field'])
def test_incomplete_or_ambiguous_password_subtype_contract_is_rejected(mode):
    doc=adapter_contract();s=doc['components']['schemas'];cap=s['ESigPasswordCapture']['properties']
    if mode=='base_type':s['ESigServiceDetail']['properties']['captureDetails']['items']['$ref']='#/components/schemas/ESigCaptureDetail'
    elif mode=='readonly_password':cap['cosignerPassword']['readOnly']=True
    elif mode=='missing_password':cap.pop('signerPassword')
    else:cap['unverifiedPolicyOverride']={'type':'boolean'}
    assert not n.contract(doc)['typed_password_submit_contract_verified']


@pytest.mark.parametrize('cosign',[False,True])
def test_two_signatures_and_optional_cosign_one_native_attempt(env,cosign):
    ready=result(p.prepare_nonconformance_report('update','N',ncr_comment='Reviewed',require_native_esign=True,native_signature_assignments=assignments(cosign)))
    assert ready.get('state')=='ready',ready
    assert PRIVATE not in json.dumps(ready) and 'eSigDetails' not in ready['request_preview']['body']
    value=capture(env,ready);out=execute(ready)
    assert out['state']=='unknown' and out['raw_response'] is None and not value
    assert len(env['native_posts'])==1
    cap=env['native_posts'][0]['eSigDetails'][0]['captureDetails'][0]
    assert ('cosignerPassword' in cap) is cosign
    if cosign:assert cap['cosignerPassword']==PRIVATE and cap['cosignReason']['instanceId']==REASON
    assert 'eSigDetails' not in env['native_client'].request.await_args.kwargs['json']
    stored=result(p.get_quality_transaction_plan(ready['plan_id']))
    assert PRIVATE not in json.dumps(stored) and PRIVATE.encode() not in p.PLAN_DB.read_bytes()
    assert 'error' in execute(ready);env['native_client'].request.assert_awaited_once()


@pytest.mark.parametrize('mode',['count','method','missing','overreturn','actor_change','configuration_change','role_change'])
def test_requirements_and_actor_changes_stop_before_submission(env,mode):
    ready=prepare();assert ready.get('state')=='ready',ready
    value=capture(env,ready)
    if mode=='count':env['requirement']['SignatureCount']=1
    elif mode=='method':env['requirement']['VerificationMethod']=2
    elif mode=='missing':env['load_hook']=lambda data:data.update(ESigDetails=[])
    elif mode=='overreturn':env['load_hook']=lambda data:data['ESigDetails'][0].update(CaptureDetails=[{'SignerPassword':PRIVATE}])
    elif mode=='actor_change':env['load_hook']=lambda data:data.update(Employee=reference('Changed actor',REASON))
    elif mode=='configuration_change':env['requirement']['Meaning']=reference('Changed meaning',REASON)
    else:env['people']['Signer A']['ESigRoleGroup']=None
    assert 'error' in execute(ready) and not value;env['native_client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['400','timeout'])
def test_generic_unknown_does_not_retry_or_echo(env,mode):
    ready=prepare();value=capture(env,ready);env['post_mode']=mode
    out=execute(ready);assert out['state']=='unknown' and PRIVATE not in json.dumps(out) and not value
    assert 'error' in execute(ready);env['native_client'].request.assert_awaited_once()


def test_read_context_has_no_plan_and_reports_actual_contract_gap(env):
    env['doc']=archived();out=result(q.get_nonconformance_report('N',include_native_signature_context=True))
    assert out['found'] and not out['native_signature_context']['ready_for_execution'] and out['native_signature_context']['blockers']
    assert not env['vault'].entries;env['native_client'].request.assert_not_awaited()


def test_mcp_signature_assignment_is_closed_and_has_no_password(env):
    async def check():
        async with Client(mcp) as client:
            tool=next(x for x in await client.list_tools() if x.name=='prepare_nonconformance_report')
            assert tool.input_schema['properties']['require_native_esign']['default'] is False
            s=tool.input_schema['properties']['native_signature_assignments']['anyOf'][0]['items']
            assert s['additionalProperties'] is False and not any('password' in k.lower() for k in s['properties'])
            called=await client.call_tool('prepare_nonconformance_report',dict(action='update',report_name='N',ncr_comment='Reviewed',
                require_native_esign=True,native_signature_assignments=[x.model_dump() for x in assignments()]))
            ready=json.loads(called.content[0].text)
            assert ready.get('state')=='ready' and ready['signature_input_status']['state']=='awaiting_input',ready
    asyncio.run(check())


@pytest.mark.parametrize('mode',['same_signer','same_cosigner','wrong_detail','missing_reason','extra_capture'])
def test_invalid_roster_cannot_create_input_page(env,mode):
    rows=assignments(True)
    if mode=='same_signer':rows[1].signer_name=rows[0].signer_name
    elif mode=='same_cosigner':rows[0].cosigner_name=rows[0].signer_name
    elif mode=='wrong_detail':rows[0].requirement_detail_id=REASON
    elif mode=='missing_reason':rows[0].cosign_reason_name=None
    else:rows.append(copy.deepcopy(rows[0]))
    out=result(p.prepare_nonconformance_report('update','N',ncr_comment='Reviewed',require_native_esign=True,native_signature_assignments=rows))
    assert 'error' in out and not env['vault'].entries;env['native_client'].request.assert_not_awaited()


def test_original_generic_signature_history_is_evidence_only(env,monkeypatch):
    ready=prepare();capture(env,ready);execute(ready)
    from tools import quality_spc_annotations
    monkeypatch.setattr(quality_spc_annotations,'schema_check',AsyncMock())
    row=dict(HistoryMainlineId='000000000000d001',HistoryId='000000000000d002',TxnId='000000000000d003',ContainerId=CID,
        TxnServiceName='NCRUpdate',Comments=ready['request_preview']['body']['comments'],ESigHistorySummaryId='000000000000d004',
        ESigReqDetailId=DETAIL,SignatureCount=2,VerificationMethod=1,ESigHistoryDetailId='000000000000d005',SignerId=env['people']['Signer A']['InstanceID'],CosignerId=None,CosignReasonId=None)
    async def sql(query,maximum):
        assert 's.HistoryId=h.HistoryId' in query and 'd.TxnId=s.TxnId' in query
        return [row]
    monkeypatch.setattr(q,'sql_rows',AsyncMock(side_effect=sql))
    evidence=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert evidence['generic_signature_history'][0]['SignerId']==row['SignerId'] and not evidence['verified_success']
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'
