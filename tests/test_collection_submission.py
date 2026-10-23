import asyncio
import copy
import json
from unittest.mock import AsyncMock
import pytest
from tools import collection_submission as cs
from core.tool_catalog import TOOL_POLICIES
from agent.safety import plan_preview

CID='0004108000000052'
DID='001c618000000001'
PID='001c678000000001'


def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, 'PLAN_DB', tmp_path / 'plans.sqlite')
    definition={'InstanceID':DID,'Name':'D','Revision':'1'}
    val={'static_valid':True,'errors':[], 'definition':definition, 'checks':[{'point':'X','point_instance_id':PID,'data_type':3,'value':1}]}
    ctx={'static_mappings_complete':True,'spec':{'InstanceID':'s','Revision':'1'},'mappings':[{'definition':{'definition':definition,'complete':True}}]}
    reply={'data':{'Container':{'InstanceId':CID,'Name':'C'},'DataCollectionDef':{'InstanceId':DID,'Name':'D','Revision':'1'},'HasUserDataCollection':'true','ParametricDataDefType':'7273','ParametricData':{'CDOTypeId':'7273','DataPointDetails':[{'DataPoint':{'InstanceId':PID,'Name':'X','Parent':{'InstanceId':DID}},'DataValue':'1'}]}}}
    monkeypatch.setattr(cs,'validate_collection_values',AsyncMock(return_value=json.dumps(val)))
    monkeypatch.setattr(cs,'get_container_details',AsyncMock(return_value=json.dumps({'data':{'Container':{'Name':'C','InstanceId':CID}}})))
    monkeypatch.setattr(cs,'get_container_collection_requirements',AsyncMock(return_value=json.dumps(ctx)))
    req=AsyncMock(return_value=json.dumps(reply));monkeypatch.setattr(cs,'request',req)
    return val,ctx,reply,req


def run(coro):return json.loads(asyncio.run(coro))
def preview():return run(cs.preview_container_data_collection('C',{'X':1},instance_id=DID))


def test_ready_preview_confirmation(monkeypatch,tmp_path):
    _,_,_,req=setup(monkeypatch,tmp_path)
    r=preview()
    assert r['server_echo_verified'] and r['executable']
    assert req.call_args.kwargs['params']['execute']=='false'
    assert 'retry_read_only' not in req.call_args.kwargs
    assert req.call_args.kwargs['body']['parametricData']['dataPointDetails'][0]['dataValue']=='1'
    text=plan_preview([{'function':{'name':'collect_container_data','arguments':json.dumps({'plan_id':r['plan_id'],'plan_digest':r['plan_digest']})}}])
    assert 'dataValue' in text and '提交采集' in text
    assert TOOL_POLICIES['collect_container_data'].always_confirm


def test_missing_context_preview_without_plan(monkeypatch,tmp_path):
    _,ctx,_,_=setup(monkeypatch,tmp_path)
    ctx['static_mappings_complete']=False
    cs.get_container_collection_requirements.return_value=json.dumps(ctx)
    r=preview()
    assert r['server_echo_verified'] and not r['executable'] and 'plan_id' not in r


@pytest.mark.parametrize('failure',['value','point','parent','definition','type','missing','duplicate'])
def test_bad_echo(monkeypatch,tmp_path,failure):
    _,_,reply,req=setup(monkeypatch,tmp_path)
    data=reply['data'];rows=data['ParametricData']['DataPointDetails']
    if failure=='value':rows[0]['DataValue']='2'
    if failure=='point':rows[0]['DataPoint']['InstanceId']=DID
    if failure=='parent':rows[0]['DataPoint']['Parent']['InstanceId']=PID
    if failure=='definition':data['DataCollectionDef']['Revision']='2'
    if failure=='type':data['ParametricDataDefType']='999'
    if failure=='missing':data['ParametricData'].pop('DataPointDetails')
    if failure=='duplicate':rows.append(copy.deepcopy(rows[0]))
    req.return_value=json.dumps(reply)
    r=preview()
    assert 'error' in r and not r['executable'] and 'plan_id' not in r


def test_changed_context_never_submits(monkeypatch,tmp_path):
    _,ctx,_,req=setup(monkeypatch,tmp_path);r=preview()
    ctx['spec']['Revision']='2';cs.get_container_collection_requirements.return_value=json.dumps(ctx)
    result=run(cs.collect_container_data(r['plan_id'],r['plan_digest']))
    assert result['submitted'] is False
    assert all(c.kwargs['params']['execute']=='false' for c in req.call_args_list)


def test_submit_unknown_no_retries_or_new_plan_bypass(monkeypatch,tmp_path):
    from tools import collection_history
    _,_,_,req=setup(monkeypatch,tmp_path);r=preview()
    monkeypatch.setattr(collection_history,'get_container_collection_history',AsyncMock(return_value='{"rows":[]}'))
    result=run(cs.collect_container_data(r['plan_id'],r['plan_digest']))
    assert result['state']=='unknown' and not result['verified_success']
    count=len(req.call_args_list)
    assert run(cs.collect_container_data(r['plan_id'],r['plan_digest']))['state']=='unknown'
    assert len(req.call_args_list)==count
    other=preview()
    assert not run(cs.collect_container_data(other['plan_id'],other['plan_digest']))['submitted']
    assert sum(c.kwargs['params']['execute']=='true' for c in req.call_args_list)==1
    assert run(cs.get_collection_submission_plan(r['plan_id']))['state']=='unknown'


def test_expiry_and_digest(monkeypatch,tmp_path):
    _,_,_,req=setup(monkeypatch,tmp_path);r=preview();req.reset_mock()
    assert not run(cs.collect_container_data(r['plan_id'],'bad'))['submitted']
    with cs.db_connect() as db:db.execute('UPDATE plans SET created=0')
    assert not run(cs.collect_container_data(r['plan_id'],r['plan_digest']))['submitted']
    req.assert_not_called()


def test_integer_range_and_boolean_conversion():
    for value,kind in [(2147483648,1),(1.5,1),(True,7)]:
        with pytest.raises(ValueError):cs.serialize({'value':value,'data_type':kind})
    assert cs.serialize({'value':0,'data_type':1})=='0'

def test_timeout_is_persistent_unknown(monkeypatch,tmp_path):
    from tools import collection_history
    _,_,reply,req=setup(monkeypatch,tmp_path);r=preview()
    async def respond(*args,**kwargs):
        if kwargs['params']['execute']=='true': return 'Request timed out'
        return json.dumps(reply)
    req.side_effect=respond
    monkeypatch.setattr(collection_history,'get_container_collection_history',AsyncMock(side_effect=ValueError('history unavailable')))
    result=run(cs.collect_container_data(r['plan_id'],r['plan_digest']))
    assert result['response']=='Request timed out' and result['state']=='unknown'
    assert run(cs.get_collection_submission_plan(r['plan_id']))['state']=='unknown'


def test_concurrent_claim_submits_once(monkeypatch,tmp_path):
    from tools import collection_history
    _,_,_,req=setup(monkeypatch,tmp_path);r=preview()
    monkeypatch.setattr(collection_history,'get_container_collection_history',AsyncMock(return_value='{"rows":[]}'))
    async def both():
        return await asyncio.gather(*(cs.collect_container_data(r['plan_id'],r['plan_digest']) for _ in range(2)))
    asyncio.run(both())
    assert sum(c.kwargs['params']['execute']=='true' for c in req.call_args_list)==1
