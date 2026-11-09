"""Separate source-bound Event plans, without account changes or write previews."""
import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from tools import mcp,get_tool_func,quality_inspection_event as e,quality_plans as p,quality_history as h
from tools import quality_transaction_audit as audit

CAPTURE=json.loads(Path('tests/fixtures/inspection_combinations_native.json').read_text(encoding='utf8'))['cases']
def result(coro):return json.loads(asyncio.run(coro))
def key(n):return f'{n:016x}'
ARGS=dict(inspection_plan_id='a'*32,inspection_plan_digest='b'*64,organization_name='Isolated',classification_name='Product',
    subclassification_name='FieldEvent',priority_level_name='Normal',brief_description='Inspection rejects',reporter_name='Reporter',
    initiator_name='Initiator',reported_date='2026-11-09T23:00:00+08:00',description='Explicit follow-up')


@pytest.fixture
def env(monkeypatch,tmp_path):
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'plans.sqlite3')
    src=dict(plan_id='a'*32,plan_digest='b'*64,txn_id=key(1),key=e.source_key('a'*32,key(1)),container_id=key(2),container_name='C',
        sample_test_id=key(3),summary_id=key(4),root_id=key(5),occurrence_utc='2026-11-09T10:00:00Z',samples=4,rejects=3,evidence_digest='c'*64,current={})
    refs={kind:dict(Name=name,InstanceID=key(100+n),IsFrozen=False,ChangeCount=1)for n,(kind,name)in enumerate([
        ('organization','Isolated'),('classification','Product'),('subclassification','FieldEvent'),('priority','Normal'),('reporter','Reporter')])}
    state={'source':src,'duplicates':[],'check':dict(complete=True,static_valid=True,signature_required=False,references=refs,reporter=refs['reporter'],
        classification_mapping=dict(Owner=key(200),Role=key(201),ChecklistTemplate=None,FailureModeGroup=None),
        notification_policy=dict(smtp_configured=False,entries=[]))}
    monkeypatch.setattr(e,'source',AsyncMock(side_effect=lambda *a:copy.deepcopy(state['source'])))
    monkeypatch.setattr(e,'duplicates',AsyncMock(side_effect=lambda *a:copy.deepcopy(state['duplicates'])))
    monkeypatch.setattr(e.creation,'validate_quality_event_creation',AsyncMock(side_effect=lambda **kw:json.dumps(copy.deepcopy(state['check']))))
    async def asset(kind,*,name=None,key=None,**kw):
        if kind=='organization':return copy.deepcopy(refs[kind])
        return dict(entity='Roles'if kind=='role'else'Employees',Name=name or kind,InstanceID=key or globals()['key'](202),IsFrozen=False,ChangeCount=1)
    monkeypatch.setattr(e.triage,'asset',AsyncMock(side_effect=asset))
    monkeypatch.setattr(e,'numbering',AsyncMock(return_value={'rule':dict(Name='EventRule',InstanceID=key(300),IsFrozen=False,ChangeCount=1)}))
    monkeypatch.setattr(e,'organization_extras',AsyncMock(return_value={}))
    monkeypatch.setattr(e,'contract',AsyncMock(return_value='schema'))
    client=type('Client',(),{'request':AsyncMock(return_value=type('Response',(),{'status_code':200,'text':'{}'})())})()
    monkeypatch.setattr(p,'get_client',lambda:client)
    state['client']=client
    return state


def prepare(**kw):return result(e.prepare_inspection_quality_event(**(ARGS|kw)))
def snapshot(ready):
    assert 'plan_id'in ready,ready
    return result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']


def test_prepare_is_read_only_separate_event_with_exact_original_source(env):
    r=prepare();assert r.get('state')=='ready'and r['confirmation_required'],r
    s=snapshot(r);body=s['body'];assert s['source']==env['source']and h.history_mode(s)=='inspection_event'
    assert r['request_preview']['path']=='/api/CreateProductionEvent'
    assert body['submit']is True and body['holdContainers']is False and body['printLabels']is False
    assert 'eventName'not in body and 'processESignatures'not in body and 'primaryOrganization'not in str(body)
    assert s['source']['key']in body['serviceDetail']['description']
    assert body['containers']==[dict(name='C',instanceId=key(2))]
    env['client'].request.assert_not_awaited()
    assert get_tool_func('prepare_inspection_quality_event')is e.prepare_inspection_quality_event


@pytest.mark.parametrize('change',['source','container','type','hold','submit','signature_bypass','extra_field','bool_as_int','scope'])
def test_only_exact_source_bound_body_can_execute(env,change):
    s=snapshot(prepare())
    if change=='source':s['source']['plan_id']='d'*32
    elif change=='container':s['body']['containers'][0]['instanceId']=key(999)
    elif change=='type':s['service']='CreateEvent'
    elif change=='hold':s['body']['holdContainers']=True
    elif change=='submit':s['body']['submit']=False
    elif change=='signature_bypass':s['body']['processESignatures']=False
    elif change=='extra_field':s['body']['eventName']='override'
    elif change=='bool_as_int':s['body']['submit']=1
    else:s['scope_key']='other'
    with pytest.raises(ValueError):p.transport(s)


@pytest.mark.parametrize('change',['duplicate','signature','missing_owner','checklist','smtp','notification','failed_static','source_changed'])
def test_unsafe_or_changed_context_blocks_before_any_write(env,change):
    if change=='duplicate':env['duplicates']=[dict(EventId=key(999))]
    elif change=='signature':env['check']['signature_required']=True
    elif change=='missing_owner':env['check']['classification_mapping']['Owner']=None
    elif change=='checklist':env['check']['classification_mapping']['ChecklistTemplate']=key(400)
    elif change=='smtp':env['check']['notification_policy']['smtp_configured']=True
    elif change=='notification':env['check']['notification_policy']['entries']=[{}]
    elif change=='failed_static':env['check']['static_valid']=False
    else:e.source.side_effect=[copy.deepcopy(env['source']),env['source']|{'samples':99}]
    assert 'error'in prepare();env['client'].request.assert_not_awaited()


def test_exactly_one_native_write_and_unknown_source_conflict(env):
    first=prepare();second=prepare()
    assert result(p.execute_quality_transaction(first['plan_id'],first['plan_digest']))['state']=='unknown'
    assert 'error'in result(p.execute_quality_transaction(first['plan_id'],first['plan_digest']))
    assert 'error'in result(p.execute_quality_transaction(second['plan_id'],second['plan_digest']))
    env['client'].request.assert_awaited_once()
    call=env['client'].request.await_args
    assert call.args[0]=='POST'and call.args[1].endswith('/api/CreateProductionEvent')and call.kwargs['params']['execute']=='true'


@pytest.mark.parametrize('reported',['2026-11-09T23:00:00','2026-11-08T00:00:00Z'])
def test_report_time_requires_explicit_offset_and_correct_chronology(env,reported):
    assert 'error'in prepare(reported_date=reported)


@pytest.mark.parametrize('change',['ready','manual_review','no_attempt','wrong_native_status','no_current_proof','no_history_proof','no_rejects'])
def test_source_requires_verified_failed_inspection_not_an_assertion(monkeypatch,tmp_path,change):
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'source.sqlite3')
    r=copy.deepcopy(CAPTURE['reason_groups']);s=r['snapshot'];s['environment']=p.environment();digest=p.digest(s);pid='a'*32
    outcome=dict(verified_success=True,history_attestation=dict(action='resolve_history',candidate_txn_id=r['root']['TxnId']))
    state='ready'if change=='ready'else'history_committed'
    if change=='manual_review':outcome['history_attestation']['action']='resolve'
    if change=='no_rejects':s['preview']['rejects_this_submission']=0;digest=p.digest(s)
    if change=='wrong_native_status':r['evidence']['summary']['SamplingStatus']='Pass'
    with p.db_connect()as db:
        db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?)',(pid,digest,json.dumps(s),state,0,json.dumps(outcome)))
        if change!='no_attempt':db.execute('INSERT INTO attempts VALUES(?,?,?,?)',(pid,'attempt',0,1))
    monkeypatch.setattr(ih:=e.ih,'verify',AsyncMock(return_value=dict(recorded_business_effects_verified=change!='no_history_proof',evidence=r['evidence'])))
    monkeypatch.setattr(ih,'current_evidence',AsyncMock(return_value=({'original':change!='no_current_proof'},r['current'])))
    with pytest.raises(ValueError):asyncio.run(e.source(dict(inspection_plan_id=pid,inspection_plan_digest=digest)))


def test_numbering_uses_persistent_map_instead_of_volatile_modeling_view_id(monkeypatch):
    row=dict(NumberingRuleMapId=key(1),OrganizationId=key(2),Category='3',NumberingRuleId=key(3),IsFrozen='false',ChangeCount='1')
    monkeypatch.setattr(e.q,'sql_rows',AsyncMock(return_value=[row]))
    monkeypatch.setattr(e.lifecycle,'numbering',AsyncMock(side_effect=[dict(mapping_id=key(90),rule={'InstanceID':key(3)},ChangeCount=1),
        dict(mapping_id=key(91),rule={'InstanceID':key(3)},ChangeCount=1)]))
    first=asyncio.run(e.numbering({'InstanceID':key(2)}));second=asyncio.run(e.numbering({'InstanceID':key(2)}))
    assert first==second and first['persistent_mapping']==row and 'mapping_id'not in first


def test_changed_numbering_mapping_cannot_be_hidden_by_transient_view(monkeypatch):
    row=dict(NumberingRuleMapId=key(1),OrganizationId=key(2),Category='3',NumberingRuleId=key(3),IsFrozen='false',ChangeCount='1')
    monkeypatch.setattr(e.q,'sql_rows',AsyncMock(side_effect=[[row],[row|{'ChangeCount':'2'}]]))
    monkeypatch.setattr(e.lifecycle,'numbering',AsyncMock(return_value=dict(mapping_id=key(90),rule={'InstanceID':key(3)},ChangeCount=1)))
    with pytest.raises(ValueError):asyncio.run(e.numbering({'InstanceID':key(2)}))
