import asyncio
import copy
import json
import time
from unittest.mock import AsyncMock

import pytest

from tools import collection_groups as cg,collection_submission as cs,collection_verification as cv,collection_group_history as gh

CID='0000000000000001';DID='0000000000000002';GID='0000000000000003';PID='0000000000000004';SID='0000000000000005';TXN='0000000000000006';HID='0000000000000007'


def run(coro):return asyncio.run(coro)
def result(coro):return json.loads(run(coro))


@pytest.fixture
def setup(monkeypatch,tmp_path):
    monkeypatch.setattr(cs,'PLAN_DB',tmp_path/'plans.db')
    monkeypatch.setattr('tools.collection_modeling.PLAN_DB',tmp_path/'modeling.db')
    definition={'complete':True,'definition':{'InstanceID':DID,'Name':'D','Revision':'1'},'raw_items':[
        {'Name':'G','InstanceID':GID,'MinimumSamples':2,'Points':[
            {'Name':'P','InstanceID':PID,'DataType':9,'MinimumSamples':2,'IsRequired':True}]}]}
    monkeypatch.setattr(cg,'definition',AsyncMock(return_value=definition))
    monkeypatch.setattr(cg,'get_container_details',AsyncMock(return_value=json.dumps({'data':{'Container':{'Name':'C','InstanceId':CID}}})))
    context={'spec':{'InstanceID':SID},'static_mappings_complete':True,'mappings':[{'definition':definition}]}
    monkeypatch.setattr(cg,'get_container_collection_requirements',AsyncMock(return_value=json.dumps(context)))
    response={'Container':{'Name':'C','InstanceId':CID},'DataCollectionDef':{'Name':'D','Revision':'1','InstanceId':DID},
        'ParametricDataDefType':8373,'ParametricData':{'CDOTypeId':8373,'DPCollectionGroups':[
            {'Group':{'Name':'G','InstanceId':GID,'Parent':{'InstanceId':DID}},'ServiceDetails':[
                {'Point':{'Name':'P','InstanceId':PID,'Parent':{'InstanceId':GID}},'Samples':[
                    {'CellName':'1','DataValue':'1'},{'CellName':'2','DataValue':'2'}]}]}]}}
    request=AsyncMock(side_effect=lambda *a,**k:json.dumps(response))
    monkeypatch.setattr(cg,'request',request)
    return definition,response,request


def preview():return result(cs.preview_container_data_collection('C',{'G':{'P':[1,2]}},instance_id=DID,collection_kind='point'))


def test_group_plan_and_repreview_digest(setup):
    ready=preview();assert ready['executable'] and ready['plan_id']
    saved=result(cs.get_collection_submission_plan(ready['plan_id']))['plan']
    refreshed=result(cs._preview(**saved['arguments'],save_plan=False))
    assert cs.digest(cs.snapshot(refreshed))==ready['plan_digest']
    assert saved['snapshot']['body']['comments'].startswith('MCP-COL:')
    assert saved['snapshot']['body']['parametricData']['dpCollectionGroups'][0]['serviceDetails'][0]['samples'][1]['dataValue']=='2'


@pytest.mark.parametrize('values',[{}, {'G':{'P':[1]}},{'G':{'P':[True,2]}},{'G':{'other':[1,2]}},{'other':{'P':[1,2]}},{'G':{'P':[float('nan'),2]}}])
def test_invalid_group_values_prevent_preview(setup,values):
    assert result(cg.validate_grouped_collection_values(values,instance_id=DID))['static_valid'] is False
    setup[2].assert_not_awaited()


@pytest.mark.parametrize('change',['sample','point_parent','group_parent','duplicate_point','nextlink','wrong_type'])
def test_group_echo_mismatch_prevents_plan(setup,change):
    response=setup[1];group=response['ParametricData']['DPCollectionGroups'][0];point=group['ServiceDetails'][0]
    if change=='sample':point['Samples'][0]['DataValue']='99'
    elif change=='point_parent':point['Point']['Parent']['InstanceId']=CID
    elif change=='group_parent':group['Group']['Parent']['InstanceId']=CID
    elif change=='duplicate_point':group['ServiceDetails'].append(copy.deepcopy(point))
    elif change=='nextlink':group['ServiceDetails@odata.nextLink']='next'
    else:response['ParametricDataDefType']=7273
    assert not preview()['executable']
    with cs.db_connect() as db:assert db.execute('SELECT count(*) FROM plans').fetchone()[0]==0


def history(setup,monkeypatch,ready):
    saved=result(cs.get_collection_submission_plan(ready['plan_id']))['plan'];snap=saved['snapshot']
    header={'HistoryMainlineId':HID,'TxnId':TXN,'ContainerId':CID,'TxnType':2200,'Comments':snap['body']['comments'],'SpecId':SID,'ReversalStatus':'1'}
    rows=[{'DPCollectionSampleHistoryId':f'{i+20:016x}','HistoryMainlineId':HID,'TxnId':TXN,'ContainerId':CID,'TxnType':2200,
           'DataCollectionDefId':DID,'GroupId':GID,'PointId':PID,'DataType':9,'CellName':str(i+1),'DataValue':str(i+1)} for i in range(2)]
    monkeypatch.setattr(cv,'request',AsyncMock(side_effect=lambda *a,**k:json.dumps([header])))
    monkeypatch.setattr(gh,'group_rows',AsyncMock(return_value=rows))
    with cs.db_connect() as db:
        db.execute("UPDATE plans SET state='unknown',outcome=?",(json.dumps({'response':'preserve me'}),))
        db.execute('INSERT INTO collection_attempts VALUES (?,?,?,?)',(ready['plan_id'],'a',time.time(),time.time()))
    return saved,header,rows


def test_exact_marker_history_audits_and_preserves_original(setup,monkeypatch):
    ready=preview();history(setup,monkeypatch,ready)
    checked=result(cv.verify_collection_submission(ready['plan_id'],ready['plan_digest']))
    assert checked['verified_success'] and checked['mes_write_performed'] is False
    assert checked['verification']['previous_outcome']['response']=='preserve me'
    assert result(cs.get_collection_submission_plan(ready['plan_id']))['state']=='verified_success'
    assert 'error' in result(cv.verify_collection_submission(ready['plan_id'],ready['plan_digest']))


@pytest.mark.parametrize('change',['value','missing','duplicate','definition','parent','marker','spec','txn_type'])
def test_incomplete_or_wrong_history_keeps_unknown(setup,monkeypatch,change):
    ready=preview();saved,header,rows=history(setup,monkeypatch,ready)
    if change=='value':rows[0]['DataValue']='9'
    elif change=='missing':rows.pop()
    elif change=='duplicate':rows[1]=copy.deepcopy(rows[0])
    elif change=='definition':rows[0]['DataCollectionDefId']=CID
    elif change=='parent':rows[0]['HistoryMainlineId']=CID
    elif change=='marker':header['Comments']='different'
    elif change=='spec':header['SpecId']=CID
    else:header['TxnType']=1110
    assert result(cv.verify_collection_submission(ready['plan_id'],ready['plan_digest']))['verified_success'] is False
    assert result(cs.get_collection_submission_plan(ready['plan_id']))['state']=='unknown'


def test_audit_insert_failure_rolls_back_terminal_state(setup,monkeypatch):
    ready=preview();history(setup,monkeypatch,ready)
    with cs.db_connect() as db:
        db.execute("CREATE TRIGGER fail_review BEFORE INSERT ON collection_reviews BEGIN SELECT RAISE(ABORT,'audit failure'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):result(cv.verify_collection_submission(ready['plan_id'],ready['plan_digest']))
    assert result(cs.get_collection_submission_plan(ready['plan_id']))['state']=='unknown'


def test_group_claim_and_no_repeat(setup,monkeypatch):
    ready=preview();writer=AsyncMock(return_value='{"value":"accepted"}')
    monkeypatch.setattr(cs,'request',writer)
    monkeypatch.setattr('tools.collection_history.get_container_collection_history',AsyncMock(return_value='{"rows":[]}'))
    monkeypatch.setattr(gh,'get_grouped_collection_history',AsyncMock(return_value='{"rows":[]}'))
    submitted=result(cs.collect_container_data(ready['plan_id'],ready['plan_digest']))
    assert submitted['state']=='unknown'
    assert writer.await_count==1
    assert result(cs.collect_container_data(ready['plan_id'],ready['plan_digest']))['submitted'] is False
    assert writer.await_count==1
