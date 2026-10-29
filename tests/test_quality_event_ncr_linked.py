import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from tools import quality_event_ncr_linked as n,quality_event_close_plan as c,quality_plans as p
from tests.test_quality_event_lifecycle import env as base_env,update_env,triage_env,EVENT,ORG,OWNER,ROLE,PRIORITY,NCR,CONT,CODE,OTHER,closed,execute,snap

DATA,LOT,DISP,REASON=[f'{x:016x}' for x in range(3001,3005)]
def prepare(**kw):return closed(allow_native_ncr_closure=True,**kw)

@pytest.fixture
def env(base_env,monkeypatch):
    s=base_env;s['event']['Category']=3;s['extra']['EventDataId']=DATA
    s.update(data=dict(EventDataId=DATA,EventId=EVENT,CDOTypeId=8085,NonconformanceNumber='Original NCR',EventCustomDataId=None,ChangeCount=1),
        report=dict(NonconformanceReportId=NCR,NonconformanceReportName='Original NCR',EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1,
            NCRStatus=1,CloseDateGMT=None,failure_code_assignments=[dict(NCRFailureCodesId=REASON)]),
        container=dict(ContainerId=CONT,ContainerName='C',Status=1,CurrentHoldCount=0,ChangeCount=1),
        child_code=dict(InstanceID=REASON,Name='Resolved',IsFrozen=False,ChangeCount=1),
        lots=[dict(EventLotId=LOT,EventDataId=DATA,ContainerId=CONT,IsContainer=True,Qty='0.3',ChangeCount=1)],
        assignments=[dict(DispositionsId=DISP,EventLotId=LOT,FieldId=21122,Sequence=0)],
        dispositions=[dict(EventDispositionId=DISP,ParentId=LOT,Qty='0.3',DispositionId=REASON,ChangeCount=1)])
    s['links']=[dict(NonconformanceReportId=NCR,EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1)]
    original=n.q.sql_rows
    async def sql(query,maximum):
        for table,key in [('EventData','data'),('EventLot','lots'),('EventLotEventDispositions','assignments'),('EventDisposition','dispositions')]:
            if ' FROM '+table+' WHERE ' in query:
                rows=s[key];return [copy.deepcopy(rows)] if isinstance(rows,dict) else copy.deepcopy(rows)
        return await original(query,maximum)
    monkeypatch.setattr(n.q,'sql_rows',AsyncMock(side_effect=sql))
    monkeypatch.setattr(n.q,'ncr_snapshot',AsyncMock(side_effect=lambda *a:copy.deepcopy(s['report'])))
    monkeypatch.setattr(n.n,'container_state',AsyncMock(side_effect=lambda *a:copy.deepcopy(s['container'])))
    monkeypatch.setattr(n.n,'resolution_state',AsyncMock(side_effect=lambda *a:copy.deepcopy(s['child_code'])))
    return s


def test_one_parent_request_binds_child_and_all_dispositions(env):
    ready=prepare();assert ready.get('state')=='ready',ready
    saved=snap(ready);child=saved['before']['native_ncr_closure']
    assert child['report']['NonconformanceReportId']==NCR and child['lots'][0]['covered_qty']=='0.3'
    assert not child['disposition_execution_verified'] and not child['compound_transaction_atomicity_verified']
    assert 'runtime:container:id:'+CONT in saved['resource_writes']
    assert execute(ready)['state']=='unknown';env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/api/CloseQualityObject')
    assert not any(path.endswith('/NCRCloseQuality') for method,path,kw in env['lifecycle_requests'])


def test_default_mode_stays_blocked_and_opt_in_cannot_silently_fall_back(env):
    assert 'error' in closed()
    env['extra']['EventDataId']=None
    assert 'error' in prepare()
    env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('field,value',[('EventId',OTHER),('EventDataId',OTHER),('CDOTypeId',9000),('NonconformanceNumber',''),('EventCustomDataId',OTHER)])
def test_event_data_identity_is_exact(env,field,value):
    env['data'][field]=value;assert 'error' in prepare()

@pytest.mark.parametrize('field,value',[('EscalatedEventId',OTHER),('ContainerId',OTHER),('ChangeCount',2),('NCRStatus',2),
    ('CloseDateGMT','2026-10-29'),('failure_code_assignments',[])])
def test_original_ncr_is_not_replaced_or_reclosed(env,field,value):
    env['report'][field]=value;assert 'error' in prepare()

@pytest.mark.parametrize('mode',['missing','multiple','child_signature','child_code_frozen','child_code_name','wrong_category'])
def test_required_original_link_and_child_policy(env,mode):
    if mode=='missing':env['links']=[]
    elif mode=='multiple':env['links']*=2
    elif mode=='child_signature':env['roots']['Organizations']['QualityESigTxns']=[8081]
    elif mode=='child_code_frozen':env['child_code']['IsFrozen']=True
    elif mode=='child_code_name':env['child_code']['Name']='Other'
    else:env['event']['Category']=1
    assert 'error' in prepare();env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('value',['0.2','0.4','0','-1','NaN','Infinity','1e-29',True])
def test_disposition_quantities_are_exact_finite_positive_and_complete(env,value):
    env['dispositions'][0]['Qty']=value;assert 'error' in prepare()

@pytest.mark.parametrize('mode',['empty','too_many','wrong_parent','wrong_field','duplicates','orphan','wrong_container','non_container'])
def test_complete_bounded_lot_disposition_relations(env,mode):
    if mode=='empty':env['assignments']=[]
    elif mode=='too_many':env['lots']*=21
    elif mode=='wrong_parent':env['dispositions'][0]['ParentId']=OTHER
    elif mode=='wrong_field':env['assignments'][0]['FieldId']=21123
    elif mode=='duplicates':env['assignments']*=2
    elif mode=='orphan':env['assignments'][0]['DispositionsId']=OTHER
    elif mode=='wrong_container':env['lots'][0]['ContainerId']=OTHER
    else:env['lots'][0]['IsContainer']=False
    assert 'error' in prepare()


def test_decimal_coverage_does_not_use_float_tolerance(env):
    env['dispositions'].append(dict(EventDispositionId=OTHER,ParentId=LOT,Qty='0.2',DispositionId=REASON,ChangeCount=1))
    env['dispositions'][0]['Qty']='0.1';env['assignments'].append(dict(DispositionsId=OTHER,EventLotId=LOT,FieldId=21122,Sequence=1))
    assert prepare().get('state')=='ready'

@pytest.mark.parametrize('key',['report','container','child_code','data','lots','dispositions'])
def test_child_and_parent_changes_after_ready_block_write(env,key):
    ready=prepare();assert ready.get('state')=='ready',ready
    item=env[key][0] if isinstance(env[key],list) else env[key];item['ChangeCount']=2
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_unknown_parent_is_not_replayed_through_direct_ncr_close(env):
    ready=prepare();execute(ready)
    other=dict(kind='ncr',target_id=NCR,scope_key='ncr:Original NCR',environment=p.environment(),body={},before={'ContainerId':CONT})
    assert p.conflicts(snap(ready),other)
    assert 'error' in execute(ready);env['client'].request.assert_awaited_once()


def test_metadata_primary_columns_and_relation_field_id():
    root=Path('docs/quality-fixtures');cols={}
    for f in ('event-ncr-linked-native-index.json','event-ncr-linked-native-relations.json'):
        for r in json.loads((root/f).read_text(encoding='utf8'))['results']:
            if r['query']['kind']=='column':cols[r['query']['owner']]={x['name'] for x in r['result'].get('records',[])}
    cols['NCRResolutionCode']={'NCRResolutionCodeId','ChangeCount'}
    assert all(set(v.split(','))<=cols[k] for k,v in n.SCHEMA.items())
    rel=json.loads((root/'event-ncr-linked-native-relations.json').read_text(encoding='utf8'))
    assert next(x['result']['FieldID'] for x in rel['results'] if x['query']['name']=='Dispositions')==21122



def test_inspect_uses_original_ncr_and_container_even_if_association_changed(env):
    ready=prepare();execute(ready);env['report']['EscalatedEventId']=OTHER;env['report']['NCRStatus']=2
    out=json.loads(asyncio.run(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest'])))
    evidence=out['native_ncr_closure_evidence']
    assert evidence['original_ncr_id']==NCR and not evidence['still_points_to_original_event'] and not evidence['verified_child_success']
    n.q.ncr_snapshot.assert_awaited_with('',NCR)
    assert not out['verified_success'] and not out['retry_allowed']



def test_missing_custom_data_field_is_not_assumed_empty(env):
    env['data'].pop('EventCustomDataId')
    assert 'error' in prepare();env['client'].request.assert_not_awaited()
