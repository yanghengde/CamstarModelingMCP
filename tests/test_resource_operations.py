import asyncio
import copy
import json
import time
from unittest.mock import AsyncMock
import pytest

from tools import resource_operations as r, resource_execution as p
from core.tool_catalog import TOOL_POLICIES
from agent.safety import plan_preview

RID='0000000000000001'
SID='0000000000000002'

def run(coro): return asyncio.run(coro)
def decode(coro): return json.loads(run(coro))


@pytest.fixture
def base(monkeypatch,tmp_path):
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'resource.db')
    root={'InstanceID':RID,'Name':'R','Description':'old','IsFrozen':False,'InstanceLocked':False,
          'ParentResource':None,'ChildResources':[],'Availability':None,'VendorModel':'KEEP'}
    state={'ResourceId':RID,'ResourceName':'R','IsFrozen':'false','ProductionStatusId':SID,'ResourceChangeCount':'1',
        'ParentResourceId':'','ChildCount':'0','Availability':'1','ProductId':'','SetupId':'','ResourceStatusModelId':'','Run':'',
        'TotalThruputQty':'0','TotalThruputQty2':'0'}
    async def read(*args,**kw): return copy.deepcopy(root)
    async def operational(*args): return copy.deepcopy(state)
    monkeypatch.setattr(r,'read_resource',read);monkeypatch.setattr(r,'operational',operational)
    monkeypatch.setattr(r,'lookup',AsyncMock(return_value=[]))
    async def resolve(entity,value,deps,revision=''):
        obj={'Name':value,'InstanceID':'0000000000000003','Status':1,'Revision':revision}
        deps.append({'entity':entity,'id':obj['InstanceID'],'configuration':obj})
        return {'name':value,'instanceId':obj['InstanceID'],**({'revision':revision} if revision else {})},obj
    monkeypatch.setattr(r,'resolve_ref',resolve)
    monkeypatch.setattr(r,'native_preview',AsyncMock(return_value={}))
    return root,state


def test_master_sparse_preserves_runtime_and_other_fields(base):
    root,state=base
    plan=decode(r.prepare_resource_change('update','R',{'notes':'new','vendor_model':'updated'}))
    assert plan['request_preview']['body']=={'notes':'new','vendorModel':'updated'}
    assert plan['expected']['Description']=='old' and plan['expected']['Availability'] is None
    assert plan['before']['VendorModel']=='KEEP'
    assert 'availability' not in plan['request_preview']['body']


@pytest.mark.parametrize('changes',[
    {'availability':1},{'initial_availability':2},{'isFrozen':False},{'currentSetup':{'name':'S'}},
    {'replaceDetails':True},{'childResources':[]},{'clearance_level':True},{'clearance_level':3},
    {'description':42},{'notes':'x'*2001},{'status_model':'REPLACE'},
    {'vendor_model':'x'*31},{'vendor_serial_number':'x'*31},{'vendor_serial_number':'😀'*16},
])
def test_master_rejects_runtime_or_unsafe_fields(base,changes):
    assert 'error' in decode(r.prepare_resource_change('update','R',changes))
    with p.db_connect() as db: assert db.execute('SELECT count(*) FROM plans').fetchone()[0]==0


def test_create_has_no_claimed_availability(base):
    plan=decode(r.prepare_resource_change('create','NEW',{'description':'new'}))
    assert plan['request_preview']['body']=={'name':'NEW','description':'new'}
    assert 'Availability' not in plan['expected']


def test_setup_explicit_native_no_bypass(base):
    plan=decode(r.prepare_resource_setup('R',2,setup_name='S',setup_revision='1',product_name='P',product_revision='1',run='RUN'))
    assert plan['service']=='ResourceSetup'
    body=plan['request_preview']['body'];assert body['availability']==2 and body['setup']['revision']=='1'
    assert body['comments'].startswith('MCP-RES:')
    assert not any(k.startswith(('skip','process')) for k in body)
    assert 'error' in decode(r.prepare_resource_setup('R',1,transition=True))
    assert 'error' in decode(r.prepare_resource_setup('R',1,transition=True,status_code='UP'))


@pytest.mark.parametrize('bad', [True,0,-1,float('nan'),float('inf')])
def test_invalid_throughput_never_previews(base,bad):
    assert 'error' in decode(r.prepare_resource_throughput('R',bad,'EACH',product_name='P',product_revision='1'))
    r.native_preview.assert_not_awaited()


def test_secondary_qty_requires_unit_no_conversion(base):
    plan=decode(r.prepare_resource_throughput('R',5,'EACH',quantity2=2,unit2_name='UNIT',product_name='P',product_revision='1'))
    body=plan['request_preview']['body'];assert body['qty']==5 and body['qty2']==2
    assert body['uom']['name']=='EACH' and body['uoM2']['name']=='UNIT'
    assert 'error' in decode(r.prepare_resource_throughput('R',5,'EACH',quantity2=2,product_name='P',product_revision='1'))


def test_down_frozen_and_hierarchy_block(base):
    root,state=base
    state['Availability']='2'
    assert 'error' in decode(r.prepare_resource_throughput('R',1,'EACH',product_name='P',product_revision='1'))
    state['Availability']='1';state['ChildCount']='1'
    assert 'error' in decode(r.prepare_resource_setup('R',1))
    state['ChildCount']='0';root['IsFrozen']=True
    assert 'error' in decode(r.prepare_resource_setup('R',1))


def test_activation_means_requirement_not_availability(base,monkeypatch):
    monkeypatch.setattr(r,'maintenance_rows',AsyncMock(return_value=[]))
    plan=decode(r.prepare_resource_activation('R','REQ','1',True))
    body=plan['request_preview']['body']
    assert 'availability' not in body and body['maintenanceReq']['revision']=='1'
    assert body['serviceDetails'][0]['activated'] is True
    monkeypatch.setattr(r,'maintenance_rows',AsyncMock(return_value=[{'AssignedMaintReqId':SID}]))
    assert 'error' in decode(r.prepare_resource_activation('R','REQ','1',True))


def test_single_attempt_unknown_blocks_new_plans(base,monkeypatch):
    class Response:
        status_code=200;text='{"value":"Resource R updated"}'
        def json(self): return json.loads(self.text)
    client=type('Client',(),{'request':AsyncMock(return_value=Response())})()
    monkeypatch.setattr(p,'get_client',lambda:client)
    plan=decode(r.prepare_resource_change('update','R',{'description':'new'}));args={k:plan[k] for k in ('plan_id','plan_digest')}
    summary=plan_preview([{'function':{'name':'execute_resource_change','arguments':json.dumps(args)}}]);assert 'old' in summary and 'new' in summary
    assert decode(p.execute_resource_change(**args))['state']=='unknown'
    assert 'error' in decode(p.execute_resource_change(**args))
    assert 'error' in decode(r.prepare_resource_setup('R',2))
    assert decode(p.reconcile_resource_change(**args))['verified_success'] is False
    base[0]['Description']='new'
    assert decode(p.reconcile_resource_change(**args))['verified_success'] is True
    assert client.request.await_count==1


def test_stale_state_and_wrong_environment_do_not_submit(base,monkeypatch):
    client=type('Client',(),{'request':AsyncMock()})();monkeypatch.setattr(p,'get_client',lambda:client)
    plan=decode(r.prepare_resource_setup('R',2));args={k:plan[k] for k in ('plan_id','plan_digest')}
    base[1]['ResourceChangeCount']='2'
    assert 'error' in decode(p.execute_resource_change(**args))
    client.request.assert_not_awaited()
    monkeypatch.setattr(p,'CAMSTAR_SHOPFLOOR_BASE_URL','https://other')
    assert 'error' in decode(p.execute_resource_change(**args))


def test_timeout_never_replays_or_proves_success(base,monkeypatch):
    client=type('Client',(),{'request':AsyncMock(side_effect=TimeoutError('lost'))})()
    monkeypatch.setattr(p,'get_client',lambda:client)
    plan=decode(r.prepare_resource_change('update','R',{'description':'new'}));args={k:plan[k] for k in ('plan_id','plan_digest')}
    decode(p.execute_resource_change(**args));base[0]['Description']='new'
    assert not decode(p.reconcile_resource_change(**args))['verified_success']
    assert 'error' in decode(p.execute_resource_change(**args))


def test_crash_recovery_keeps_fence_and_original_receipt(base):
    plan=decode(r.prepare_resource_change('update','R',{'notes':'x'}));args={k:plan[k] for k in ('plan_id','plan_digest')}
    audit=dict(action='recover',evidence_reference='audit/1',review_note='operator verified stopped process')
    assert 'error' in decode(p.review_resource_change(**args,**audit))
    with p.db_connect() as db:
        db.execute("UPDATE plans SET state='in_flight',outcome=?",(json.dumps({'original':'kept'}),))
        db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)',(plan['plan_id'],'attempt',time.time()))
    assert decode(p.review_resource_change(**args,**audit))['state']=='unknown'
    assert 'error' in decode(r.prepare_resource_change('update','R',{'notes':'again'}))
    audit.update(action='resolve',conclusion='not_applied')
    assert decode(p.review_resource_change(**args,**audit))['state']=='human_not_applied'
    assert decode(p.get_resource_change_plan(plan['plan_id']))['outcome']['original']=='kept'
    assert 'error' in decode(p.execute_resource_change(**args))


def test_collection_echo_must_cover_every_sample():
    body={'dataCollectionDef':{'instanceId':RID},'parametricData':{'dataPointDetails':[{'dataPoint':{'instanceId':SID},'dataValue':'1.5'}]}}
    data={'DataCollectionDef':{'InstanceId':RID},'ParametricDataDefType':7273,'ParametricData':{'DataPointDetails':[{'DataPoint':{'InstanceId':SID},'DataValue':'1.5'}]}}
    r.verify_collection_echo(body,data,{'collection_kind':'user'})
    data['ParametricData']['DataPointDetails'][0]['DataValue']='2'
    with pytest.raises(ValueError):r.verify_collection_echo(body,data,{'collection_kind':'user'})


def test_mutation_catalog_policy():
    assert TOOL_POLICIES['execute_resource_change'].always_confirm
    assert TOOL_POLICIES['review_resource_change'].always_confirm


def test_grouped_resource_collection_is_blocked_before_preview(base):
    result=decode(r.prepare_resource_collection('R','GROUP','1',{'G':{'P':[1,2]}},collection_kind='point'))
    assert 'error'in result and not result.get('plan_id')
    r.native_preview.assert_not_awaited()


def test_activation_requires_original_ack_not_same_assignment(base,monkeypatch):
    assigned={'AssignedMaintReqId':'0000000000000003','MaintenanceStatusId':'0000000000000004',
        'ParentId':RID,'ResourceId':RID,'MaintenanceReqId':SID,'LastRevTxnId':'4000000000000001'}
    monkeypatch.setattr(r,'maintenance_rows',AsyncMock(return_value=[assigned]))
    snap={'kind':'activation','name':'R','resource_id':RID,'service':'ResourceActivation','body':{'comments':'MCP-RES:'+'a'*32},
          'maintenance_req_id':SID,'maintenance_before':[],'expected':{'activated':True}}
    assert not run(r.verify(snap,{}))['verified_success']
    outcome={'http_status':200,'response':{'value':'Resource Activation completed on now'}}
    assert run(r.verify(snap,outcome))['verified_success']
    snap['maintenance_before']=[assigned]
    assert not run(r.verify(snap,outcome))['verified_success']


def test_history_verification_checks_causal_ids(base,monkeypatch):
    h={'HistoryMainlineId':SID,'TxnId':'4000000000000001','HistoryId':RID,'ResourceId':RID,'TxnTypeName':'ResourceThruput',
        'TxnServiceName':'ResourceThruput','TxnParentTypeName':'','ReversalStatus':'1','Comments':'MCP-RES:'+'a'*32}
    monkeypatch.setattr(r,'history_headers',AsyncMock(return_value=[h]))
    row={'ResourceThruputHistoryId':'0000000000000003','HistoryMainlineId':SID,'HistoryId':RID,'TxnId':h['TxnId'],'ResourceId':RID,'Qty':'7','UOMId':SID}
    monkeypatch.setattr(r,'query_rows',AsyncMock(side_effect=lambda *a: [copy.deepcopy(row)]))
    snap={'kind':'throughput','name':'R','resource_id':RID,'service':'ResourceThruput','body':{'comments':h['Comments']},'expected':{'Qty':7,'UOMId':SID}}
    assert run(r.verify(snap,{}))['verified_success']
    row['HistoryId']='0000000000000009'
    with pytest.raises(ValueError):run(r.verify(snap,{}))


def test_native_preview_normalizes_numeric_strings_and_retains_ids_for_collection_check(monkeypatch):
    body={'resource':{'instanceId':RID},'availability':2}
    mock=AsyncMock(return_value=json.dumps({'data':{'Resource':{'Name':'R','InstanceId':RID},'Availability':'2'}}))
    monkeypatch.setattr(r,'request',mock)
    assert run(r.native_preview('ResourceSetup',body))['Availability']=='2'
    mock.return_value=json.dumps({'data':{'Resource':{'InstanceId':RID},'DataCollectionDef':{'InstanceId':SID},'ParametricDataDefType':'7273',
        'ParametricData':{'DataPointDetails':[{'DataPoint':{'InstanceId':RID},'DataValue':'1'}]}}})
    body={'resource':{'instanceId':RID},'dataCollectionDef':{'instanceId':SID},'parametricData':{'dataPointDetails':[{'dataPoint':{'instanceId':RID},'dataValue':'1'}]}}
    run(r.native_preview('CollectResourceData',body,'user'))


def test_resource_history_rejects_invalid_filters_before_io(base,monkeypatch):
    request=AsyncMock();monkeypatch.setattr(r,'query_rows',request)
    assert 'error'in decode(r.get_resource_collection_history('R',txn_id="' OR 1=1"))
    assert 'error'in decode(r.get_resource_throughput_history('R',offset=-1))
    request.assert_not_awaited()
