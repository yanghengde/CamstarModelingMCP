import asyncio
import copy
import json
import time
from unittest.mock import AsyncMock

import pytest

from tools import collection_modeling as cm,collection_submission as cs
from core.tool_catalog import TOOL_POLICIES
from agent.safety import plan_preview


def run(coro):return asyncio.run(coro)
def decode(coro):return json.loads(run(coro))


@pytest.fixture
def setup(monkeypatch,tmp_path):
    monkeypatch.setattr(cm,'PLAN_DB',tmp_path/'modeling.db')
    monkeypatch.setattr(cs,'PLAN_DB',tmp_path/'submission.db')
    before={'InstanceID':'0000000000000001','Name':'D','Revision':'1','IsFrozen':False,'IsRevOfRcd':True,
            'DataPointLayout':1,'Description':'original','DataPoints':[
                {'InstanceID':'0000000000000002','Name':'X','DataType':9,'RowPosition':1,'ColumnPosition':1,'IsRequired':True,'UpperLimit':'100'},
                {'InstanceID':'0000000000000003','Name':'Y','DataType':4,'RowPosition':2,'ColumnPosition':1,'IsRequired':False}]}
    async def read(*args):return copy.deepcopy(before)
    monkeypatch.setattr(cm,'read_definition',read)
    monkeypatch.setattr(cm,'lookup',AsyncMock(return_value=[]))
    return before


def test_value_subtype_and_strict_fields(setup):
    value=run(cm.point_payload({'name':'X','data_type':'decimal','lower_limit':'1','unit':None},'user'))
    assert value['@odata.type']=='#modeling.ValueDataPointChanges'
    assert value['lowerLimit']=='1' and value['uom'] is None
    with pytest.raises(ValueError):run(cm.point_payload({'name':'X','data_type':'string','instanceID':'evil'},'user'))
    with pytest.raises(ValueError):run(cm.point_payload({'name':'X','data_type':'decimal','minimum_samples':1},'user'))
    with pytest.raises(ValueError):run(cm.point_payload({'name':'X','data_type':'object'},'user'))


def test_sparse_update_preserves_other_point_and_constraints(setup):
    result=run(cm.build_definition('user','update','D','1',{'points':[{'name':'X','required':False}]}))
    wrapper=result['body']['dataPoints-Expanded'][0]
    assert wrapper['key']=={'name':'X'}
    assert 'upperLimit' not in wrapper['value']
    assert result['expected']['DataPoints'][0]['UpperLimit']=='100'
    assert result['expected']['DataPoints'][1]['Name']=='Y'
    assert result['expected']['Description']=='original'


@pytest.mark.parametrize('changes',[
    {'points':[{'op':'add','name':'X','data_type':'integer'}]},
    {'points':[{'op':'remove','name':'missing'}]},
    {'points':[{'name':'X'},{'name':'X'}]},
    {'points':[{'name':'X','new_name':'Y'}]},
    {'points':[42]}, {'groups':[]}, {'unexpected':True},
])
def test_invalid_changes_no_plan(setup,changes):
    assert 'error' in decode(cm.prepare_data_collection_definition('user','update','D','1',changes))
    with cm.db_connect() as db:assert db.execute('SELECT count(*) FROM plans').fetchone()[0]==0


def test_remove_uses_expanded_empty_value(setup):
    result=run(cm.build_definition('user','update','D','1',{'points':[{'name':'X','op':'remove'}]}))
    assert result['body']['dataPoints-Expanded']==[{'key':{'name':'X'},'value':{}}]
    assert [p['Name'] for p in result['expected']['DataPoints']]==['Y']


def test_revise_requires_current_ror_and_frozen_guard(setup):
    result=run(cm.build_definition('user','revise','D','2',{},'1'))
    assert result['method']=='PATCH' and result['path']=='/api/UserDataCollectionDefs'
    setup['IsRevOfRcd']=False
    with pytest.raises(ValueError):run(cm.build_definition('user','revise','D','2',{},'1'))
    setup['IsFrozen']=True
    with pytest.raises(ValueError):run(cm.build_definition('user','delete','D','1',{}))


def test_group_nested_changes_and_create(setup):
    result=run(cm.build_definition('point','create','G','1',{'groups':[{'name':'A','points':[{'name':'P','data_type':'decimal','minimum_samples':2,'outer_upper_limit':'10'}]}]}))
    assert result['body']['groups'][0]['points'][0]['@odata.type']=='#modeling.DataPointCollectionPointChgs'
    assert result['expected']['Groups'][0]['Points'][0]['OuterUpperLimit']=='10'
    assert result['expected']['Groups'][0]['Points'][0]['MinimumSamples']==2


def test_binding_uses_collect_data_own_cdo_and_preserves_other_rows(setup,monkeypatch):
    other={'TxnToUse':2200,'DataCollectionDef':{'Name':'OTHER','Revision':'1','InstanceId':'0000000000000009'},'Required':False,'Sequence':8}
    before={'Name':'S','Revision':'1','InstanceID':'0000000000000010','IsFrozen':False,'TxnMap':[other]}
    monkeypatch.setattr(cm,'read_binding_spec',AsyncMock(return_value=before))
    result=run(cm.build_binding('S','1','user','D','1'))
    assert result['body']['txnMap-Expanded'][0]['value']['txnToUse']==2200
    assert result['expected']['TxnMap'][0]['DataCollectionDef']['Name']=='OTHER'
    before['TxnMap'].append({'TxnName':'CollectData','DataCollectionDef':{'Name':'D','Revision':'1','InstanceId':setup['InstanceID']}})
    result=run(cm.build_binding('S','1','user','D','1','unbind'))
    assert result['body']['txnMap-Expanded']==[{'index':1,'value':{}}]
    before['TxnMap'].append(copy.deepcopy(before['TxnMap'][1]))
    with pytest.raises(ValueError):run(cm.build_binding('S','1','user','D','1'))


def test_execute_rechecks_single_attempt_and_audit(setup,monkeypatch):
    class Response:
        status_code=200
        text='{"value":"D updated"}'
        def json(self):return json.loads(self.text)
    client=type('Client',(),{'request':AsyncMock(return_value=Response())})()
    monkeypatch.setattr(cm,'get_client',lambda:client)
    prepared=decode(cm.prepare_data_collection_definition('user','update','D','1',{'description':'changed'}))
    args={k:prepared[k] for k in ('plan_id','plan_digest')}
    preview=plan_preview([{'function':{'name':'execute_data_collection_change','arguments':json.dumps(args)}}])
    assert 'original' in preview and 'changed' in preview
    assert decode(cm.execute_data_collection_change(**args))['state']=='unknown'
    assert 'error' in decode(cm.execute_data_collection_change(**args))
    assert client.request.await_count==1
    assert decode(cm.reconcile_data_collection_change(**args))['verified_success'] is False
    setup['Description']='changed'
    result=decode(cm.reconcile_data_collection_change(**args))
    assert result['verified_success'] is True
    with cm.db_connect() as db:assert db.execute('SELECT count(*) FROM reviews').fetchone()[0]==2


def test_stale_no_write_and_busy_unknown_holds(setup,monkeypatch):
    client=type('Client',(),{'request':AsyncMock()})()
    monkeypatch.setattr(cm,'get_client',lambda:client)
    ready=decode(cm.prepare_data_collection_definition('user','update','D','1',{'description':'new'}))
    setup['Description']='changed externally'
    assert 'error' in decode(cm.execute_data_collection_change(ready['plan_id'],ready['plan_digest']))
    client.request.assert_not_awaited()


def test_audit_requires_attempt_retains_original_result(setup):
    ready=decode(cm.prepare_data_collection_definition('user','update','D','1',{'description':'new'}))
    args={k:ready[k] for k in ('plan_id','plan_digest')}
    args.update(action='recover',evidence_reference='audit/1',review_note='operator confirmed inactive worker')
    assert 'error' in decode(cm.review_data_collection_change(**args))
    with cm.db_connect() as db:
        db.execute("UPDATE plans SET state='in_flight',outcome=?",(json.dumps({'original':'kept'}),))
        db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)',(ready['plan_id'],'attempt',time.time()))
    assert decode(cm.review_data_collection_change(**args))['state']=='unknown'
    args.update(action='resolve',conclusion='not_applied')
    assert decode(cm.review_data_collection_change(**args))['state']=='human_not_applied'
    assert 'error' in decode(cm.execute_data_collection_change(ready['plan_id'],ready['plan_digest']))


def test_runtime_and_configuration_cross_guards(setup):
    ready=decode(cm.prepare_data_collection_definition('user','update','D','1',{'description':'new'}))
    with cm.db_connect() as db:db.execute("UPDATE plans SET state='unknown'")
    snap={'environment':cs.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),'body':{'dataCollectionDef':{'instanceId':setup['InstanceID'],'name':'D'}},'spec':{}}
    with pytest.raises(ValueError):cm.assert_no_pending_changes(snap)
    with cs.db_connect() as db:
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',('runtime','d',json.dumps({'snapshot':snap}),'unknown',time.time()))
    with pytest.raises(ValueError):cm.assert_no_pending_submissions({'target_id':setup['InstanceID']})


def test_new_mutations_require_app_confirmation():
    for name in ['execute_data_collection_change','reconcile_data_collection_change','review_data_collection_change','verify_collection_submission']:
        assert TOOL_POLICIES[name].always_confirm
