import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from agent.safety import plan_preview,evaluate_mutations,MutationCounts
from tools import mcp,quality_lot_execution as e,quality_inspection as i,quality_plans as p,quality_lots as l

LOT='0000000000000101';CID='0000000000000201';CID2='0000000000000202';TID='0000000000000301';PLAN='0000000000000401';PROD='0000000000000501';SPEC='0000000000000601';ROW='0000000000000701';POINT='0000000000000801'


def result(coro):return json.loads(asyncio.run(coro))


def echo(value):
    if isinstance(value,dict):return {k[0].upper()+k[1:]:echo(v) for k,v in value.items()}
    if isinstance(value,list):return [echo(v) for v in value]
    if type(value) in (bool,int):return str(value).lower()
    return value


@pytest.fixture
def env(monkeypatch,tmp_path):
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'quality.sqlite3')
    test={k:None for k in i.TEST_FIELDS};test.update(Name='Visual',Revision='A',InstanceID=TID,Status=1,SampleType=1,
        ScrapCountedRejectsByReason=False,AllowMoveOnFailure=False,IsFrozen=False)
    member={'ContainerId':CID,'ContainerName':'C','SamplingLotId':LOT,'ProductId':PROD,'Status':1,'CurrentHoldCount':0,
        'ChangeCount':1,'Qty':'10','Qty2':None,'ChildCount':0,'ParentContainerId':None,'SamplingRequired':True,'SamplingPassed':False}
    task={'LotSampleDataId':ROW,'SamplingLotId':LOT,'SamplingPlanId':PLAN,'SampleTestId':TID,'SamplingPlanDetailId':ROW,
        'SpecId':SPEC,'ResourceId':None,'VendorItemId':None,'TotalSamplesRequired':10,'CompletedSamples':2,'Rejects':0,
        'AllowedRejects':1,'SamplesTested':2,'SamplesFailed':0,'InspectAllContQty':False,'InspectAllQtyOnFailure':False,'ChangeCount':1}
    state={'lot':{'MfgLotId':LOT,'MfgLotName':'L','CDOTypeId':'8587','ProductId':PROD,'SamplingPlanId':PLAN,'LotStatus':'1',
            'Qty':'20','ChangeCount':'1','SamplingCompleted':'false','SamplingPassed':'false','SamplingLotResult':None},
        'members':[member,{**member,'ContainerId':CID2,'ContainerName':'C2'}],'tasks':[task],
        'definition':{'test':test,'points':[],'reject_reasons':[{'name':'Crack','instanceId':POINT}],'reject_group':None},
        'history':[],'in_process':True,'counter':0}
    async def identity(name,instance_id=None):
        found=[r for r in state['members'] if r['ContainerName']==name]
        if len(found)!=1:raise ValueError('member not found')
        row=found[0]
        if instance_id and row['ContainerId']!=instance_id:raise ValueError('member id mismatch')
        state['counter']+=1
        return {'name':name,'instanceId':row['ContainerId']},{'InProcess':state['in_process'],
            'Spec':{'Name':'Spec','Revision':'A','InstanceId':SPEC},'InstanceID':f'{state["counter"]+1000:016x}'}
    monkeypatch.setattr(e.reads,'container_identity',AsyncMock(side_effect=identity))
    monkeypatch.setattr(i,'test_definition',AsyncMock(side_effect=lambda *a:copy.deepcopy(state['definition'])))
    async def sql(query,maximum):
        if 'FROM MfgLot WHERE' in query:return [copy.deepcopy(state['lot'])] if state['lot'] else []
        if 'FROM Container WHERE ContainerId=' in query:
            iid=query.split("ContainerId='",1)[1].split("'",1)[0]
            return [copy.deepcopy(r) for r in state['members'] if r['ContainerId']==iid]
        if 'FROM Container WHERE SamplingLotId=' in query:return copy.deepcopy(state['members'])
        if 'FROM LotSampleData WHERE' in query:
            rows=state['tasks']
            if "LotSampleDataId='" in query:
                iid=query.split("LotSampleDataId='",1)[1].split("'",1)[0];rows=[r for r in rows if r['LotSampleDataId']==iid]
            return copy.deepcopy(rows)
        if 'FROM CollectSamplingDataHistory' in query:return copy.deepcopy(state['history'])
        raise AssertionError(query)
    sql_call=AsyncMock(side_effect=sql);monkeypatch.setattr(e.reads,'sql_rows',sql_call)
    async def preview(*a,**kw):
        data=echo(kw['body'])
        if a[1]=='/api/CollectLotSamplingData':
            task=state['tasks'][0];data.update(RequiredSamples=str(task['TotalSamplesRequired']),SamplesTested=str(task['SamplesTested']),
                AllowedRejects=str(task['AllowedRejects']),SampleType=str(state['definition']['test']['SampleType']),SamplingStatus='In Process')
            for d in data['ServiceDetails']:
                d['CDOTypeId']='8462';d['InstanceId']=f'{state["counter"]+2000:016x}'
                for point in d.get('SamplingDataPointDetails',[]):point['CDOTypeId']='8463'
        elif a[1]=='/api/ExecuteSamplingLot':data['Containers']=[r['ContainerName'] for r in state['members']]
        else:raise AssertionError(a)
        return json.dumps({'data':data})
    preview_call=AsyncMock(side_effect=preview);monkeypatch.setattr(e,'request',preview_call);monkeypatch.setattr(i,'request',preview_call)
    client=SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200,text='{"value":"complete"}')))
    monkeypatch.setattr(p,'get_client',lambda:client)
    return state,preview_call,client,sql_call


def collect(**kw):return result(e.prepare_lot_inspection('L','C','Visual','A',samples_collected=3,rejects=0,**kw))
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))
def finish():return result(e.prepare_sampling_lot_completion('L',comments='MES sampling checked'))


def passed(env):
    env[0]['lot'].update(SamplingCompleted='true',SamplingPassed='true')
    env[0]['tasks'][0].update(CompletedSamples=10,SamplesTested=10)


def test_lot_counted_collection_uses_lot_task_and_member_identity(env):
    ready=collect(comments='lot delta')
    assert ready['state']=='ready'
    body=ready['request_preview']['body']
    assert ready['request_preview']['path']=='/api/CollectLotSamplingData'
    assert body['samplingLot']=={'name':'L','instanceId':LOT} and body['lotSampleDataItem']=={'instanceId':ROW}
    assert body['serviceDetails'][0]['totalSamplesCollected']==3 and ready['before']['task']['CompletedSamples']==2
    assert body['moveContainerOnCompletion'] is False and body['isExternal'] is False and body['isWebhook'] is False
    assert ready['server_context']['quality_verdict'] is None
    assert execute(ready)['state']=='unknown'
    env[2].request.assert_awaited_once()


def test_random_lot_sampling_allowed_but_in_queue_not_automatically_moved(env):
    env[0]['members'][0]['SamplingRequired']=False
    assert collect()['server_context']['random_sampling'] is True
    env[0]['in_process']=False
    assert 'error' in collect()
    env[2].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('SamplingLotId',PLAN),('ProductId',PLAN),('CurrentHoldCount',1),('CurrentHoldCount',''),('Status',2)])
def test_member_must_match_lot_and_product_and_be_unheld(env,field,value):
    env[0]['members'][0][field]=value
    assert 'error' in collect()
    env[1].assert_not_awaited()


@pytest.mark.parametrize('field,value',[('SamplingCompleted','true'),('SamplingCompleted',''),('LotStatus','2'),('SamplingLotResult','2')])
def test_cannot_collect_into_closed_completed_or_decided_lot(env,field,value):
    env[0]['lot'][field]=value
    assert 'error' in collect()
    env[1].assert_not_awaited()


@pytest.mark.parametrize('field,value',[('SpecId',PLAN),('ResourceId',POINT),('VendorItemId',POINT),('SamplesTested',1),
    ('SamplesFailed',1),('InspectAllQtyOnFailure',True),('CompletedSamples',10),('SamplingPlanId',POINT)])
def test_task_scope_counts_and_plan_are_not_guessed(env,field,value):
    env[0]['tasks'][0][field]=value
    assert 'error' in collect()
    env[1].assert_not_awaited()


def test_ambiguous_task_requires_explicit_row_id(env):
    env[0]['tasks'].append({**env[0]['tasks'][0],'LotSampleDataId':POINT})
    assert 'error' in collect()
    assert collect(lot_sample_data_instance_id=ROW)['state']=='ready'


def test_lot_measured_failures_are_recorded_once_per_sample(env):
    env[0]['definition']['test']['SampleType']=2
    point={k:None for k in i.POINT_FIELDS};point.update(Name='Length',Revision='A',InstanceID=POINT,Status=1,DataType=9,LowerLimit='0',UpperLimit='10')
    env[0]['definition']['points']=[point]
    ready=result(e.prepare_lot_inspection('L','C','Visual','A',samples=[{'points':[{'data_point_name':'Length','value':'11'}]}]))
    d=ready['request_preview']['body']['serviceDetails'][0]
    assert d['rejects']==1 and d['samplingDataPointDetails'][0]['passed'] is False
    assert ready['server_context']['samples_this_submission']==1


def test_counted_lot_rejects_require_one_real_reason_and_delta_not_total(env):
    ready=result(e.prepare_lot_inspection('L','C','Visual','A',samples_collected=3,rejects=1,reject_reason_name='Crack'))
    assert ready['request_preview']['body']['serviceDetails'][0]['samplingDataPointDetails'][0]['rejectReason']['instanceId']==POINT
    assert 'error' in result(e.prepare_lot_inspection('L','C','Visual','A',samples_collected=9,rejects=0))
    assert 'error' in result(e.prepare_lot_inspection('L','C','Visual','A',samples_collected=3,rejects=1))


def test_unknown_lot_collection_blocks_other_member_of_same_lot(env):
    first=collect()
    second=result(e.prepare_lot_inspection('L','C2','Visual','A',samples_collected=1,rejects=0))
    assert execute(first)['state']=='unknown'
    assert 'error' in execute(second)
    env[2].request.assert_awaited_once()


@pytest.mark.parametrize('change',['lot','counter','member','definition'])
def test_collection_rebuild_detects_changed_cumulative_or_applicability_context(env,change):
    ready=collect()
    if change=='lot':env[0]['lot']['ChangeCount']='2'
    elif change=='counter':env[0]['tasks'][0].update(CompletedSamples=3,SamplesTested=3)
    elif change=='member':env[0]['members'][0]['ChangeCount']=2
    else:env[0]['definition']['test']['Instructions']='new'
    assert 'error' in execute(ready)
    env[2].request.assert_not_awaited()


def test_readiness_requires_mes_flags_not_just_local_counts(env):
    env[0]['tasks'][0].update(CompletedSamples=10,SamplesTested=10)
    out=result(e.get_sampling_lot_readiness('L'))
    assert not out['ok_preconditions_satisfied'] and out['quality_verdict'] is None
    assert 'error' in finish()
    env[1].assert_not_awaited()
    passed(env)
    out=result(e.get_sampling_lot_readiness('L'))
    assert out['ok_preconditions_satisfied'] and out['permission_verified'] is False


def test_complete_ok_plan_binds_every_member_and_does_not_allow_disposition_override(env):
    passed(env);ready=finish()
    assert ready['state']=='ready'
    body=ready['request_preview']['body']
    assert set(body)=={'samplingLot','samplingCompleted','samplingLotResult','comments'}
    assert body['samplingCompleted'] is True and body['samplingLotResult']==1
    assert ready['server_context']['affected_container_names']==['C','C2']
    text=plan_preview([{'function':{'name':'execute_quality_transaction','arguments':json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})}}])
    assert 'C2' in text and 'ExecuteSamplingLot' in text
    assert execute(ready)['state']=='unknown'
    assert env[2].request.await_args.args[1].endswith('/ExecuteSamplingLot')
    for value in (2,3,True):
        snap=result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot'];snap['body']['samplingLotResult']=value
        with pytest.raises(ValueError):p.transport(snap)


@pytest.mark.parametrize('field,value',[('SamplingPassed','false'),('SamplingCompleted',None),('SamplingLotResult','1'),('LotStatus','2')])
def test_completion_does_not_fabricate_pass_or_repeat_existing_decision(env,field,value):
    passed(env);env[0]['lot'][field]=value
    assert 'error' in finish()
    env[1].assert_not_awaited();env[2].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['partial','divergent','over_rejects','no_tasks','no_members','held','unknown_hold','hierarchy','wrong_product','too_many'])
def test_completion_requires_full_consistent_simple_members_and_tasks(env,mode):
    passed(env)
    if mode=='partial':env[0]['tasks'][0].update(CompletedSamples=9,SamplesTested=9)
    elif mode=='divergent':env[0]['tasks'][0]['SamplesTested']=8
    elif mode=='over_rejects':env[0]['tasks'][0].update(Rejects=2,SamplesFailed=2)
    elif mode=='no_tasks':env[0]['tasks']=[]
    elif mode=='no_members':env[0]['members']=[]
    elif mode=='held':env[0]['members'][1]['CurrentHoldCount']=1
    elif mode=='unknown_hold':env[0]['members'][1]['CurrentHoldCount']=''
    elif mode=='hierarchy':env[0]['members'][1]['ChildCount']=1
    elif mode=='wrong_product':env[0]['members'][1]['ProductId']=PLAN
    else:env[0]['members']=[{**env[0]['members'][0],'ContainerId':f'{n+3000:016x}','ContainerName':str(n)} for n in range(51)]
    assert 'error' in finish()
    env[1].assert_not_awaited()


@pytest.mark.parametrize('change',['missing_member','wrong_result','false_completion','truncated'])
def test_completion_echo_must_cover_all_members_and_original_result(env,change):
    passed(env);original=env[1].side_effect
    async def bad(*a,**kw):
        out=json.loads(await original(*a,**kw));data=out['data']
        if change=='missing_member':data['Containers'].pop()
        elif change=='wrong_result':data['SamplingLotResult']='3'
        elif change=='false_completion':data['SamplingCompleted']='false'
        else:data['Containers@odata.nextLink']='next'
        return json.dumps(out)
    env[1].side_effect=bad
    assert 'error' in finish()
    env[2].request.assert_not_awaited()


def test_completion_rechecks_members_and_blocks_after_another_member_transaction(env):
    passed(env);ready=finish();env[0]['members'][1]['ChangeCount']=2
    assert 'error' in execute(ready)
    env[2].request.assert_not_awaited()


def test_lot_completion_and_member_ncr_share_runtime_claims(env):
    passed(env);ready=finish();completion=result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']
    ncr={'kind':'ncr','scope_key':'ncr:X','target_id':None,'name':'X','environment':p.environment(),
        'body':{'container':{'instanceId':CID}},'before':None}
    assert p.conflicts(ncr,completion) and p.conflicts(completion,ncr)


def test_cancelled_batch_collection_stays_unknown_and_inspection_does_not_unlock(env):
    ready=collect();env[2].request.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):execute(ready)
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['verified_success'] is False and out['state_changed'] is False
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_lot_history_is_joined_by_historical_lot_not_current_container_membership(env):
    env[0]['history']=[{'CollectSamplingDataHistoryId':ROW,'TxnId':ROW,'SamplingLotId':LOT,'ContainerId':CID,'TotalSamplesCompleted':'7'}]
    out=result(e.get_sampling_lot_history('L'));assert out['rows'][0]['TotalSamplesCompleted']=='7' and not out['verified_success']
    query=env[3].await_args.args[0]
    assert "s.SamplingLotId='"+LOT+"'" in query and 'h.TxnId=s.TxnId' in query
    env[0]['history'][0]['SamplingLotId']=PROD
    assert 'error' in result(e.get_sampling_lot_history('L'))


@pytest.mark.parametrize('kind',['collection','completion'])
def test_new_bodies_match_fresh_live_openapi_contracts(env,kind):
    import jsonschema
    if kind=='completion':passed(env);ready=finish();name='ExecuteSamplingLot'
    else:ready=collect();name='CollectLotSamplingData'
    schemas=json.loads(Path('docs/quality-fixtures/lot-execution-contracts.json').read_text(encoding='utf-8'))['schemas'][name]['schema']['components']['schemas']
    jsonschema.Draft7Validator({'components':{'schemas':schemas},'$ref':'#/components/schemas/'+name}).validate(ready['request_preview']['body'])


def test_mcp_prepare_confirmation_execute_and_unknown_batch_inspection(env):
    async def scenario():
        async with Client(mcp) as client:
            async def call(name,args):
                out=await client.call_tool(name,args);assert not out.is_error;return json.loads(out.content[0].text)
            ready=await call('prepare_lot_inspection',{'lot_name':'L','container_name':'C','sample_test_name':'Visual',
                'sample_test_revision':'A','samples_collected':3,'rejects':0})
            keys={k:ready[k] for k in ('plan_id','plan_digest')};calls=[{'function':{'name':'execute_quality_transaction','arguments':json.dumps(keys)}}]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval
            assert 'LotSampleDataItem' in plan_preview(calls) or 'lotSampleDataItem' in plan_preview(calls)
            assert (await call('execute_quality_transaction',keys))['state']=='unknown'
            assert (await call('inspect_quality_transaction',keys))['verified_success'] is False
            assert (await call('execute_quality_transaction',keys))['submitted'] is False
    asyncio.run(scenario());env[2].request.assert_awaited_once()


@pytest.mark.parametrize('bad',[True,'3',1.0])
def test_mcp_lot_counts_are_strict_before_any_read(env,bad):
    async def scenario():
        async with Client(mcp) as client:
            out=await client.call_tool('prepare_lot_inspection',{'lot_name':'L','container_name':'C','sample_test_name':'Visual',
                'sample_test_revision':'A','samples_collected':bad,'rejects':0},raise_on_error=False)
            assert out.is_error
    asyncio.run(scenario());env[3].assert_not_awaited()


def test_observed_sql_string_encodings_are_normalized_for_batch_snapshots(env):
    state=env[0]
    for row in state['tasks']:
        for key in e.INTS:row[key]=str(row[key])
        for key in ('InspectAllContQty','InspectAllQtyOnFailure'):row[key]=str(row[key]).lower()
    for member in state['members']:
        for key in ('Status','CurrentHoldCount','ChangeCount'):member[key]=str(member[key])
        for key in ('SamplingRequired','SamplingPassed'):member[key]=str(member[key]).lower()
    ready=collect()
    assert ready['before']['task']['CompletedSamples']==2
    assert execute(ready)['state']=='unknown'


def test_preparation_detects_member_or_task_change_during_service_preview(env):
    original=env[1].side_effect
    async def mutate(*a,**kw):
        response=await original(*a,**kw)
        env[0]['tasks'][0].update(CompletedSamples=3,SamplesTested=3)
        return response
    env[1].side_effect=mutate
    assert 'error' in collect()
    env[2].request.assert_not_awaited()


def test_failed_measured_lot_request_cannot_drop_point_values_in_echo(env):
    env[0]['definition']['test']['SampleType']=2
    point={k:None for k in i.POINT_FIELDS};point.update(Name='Length',Revision='A',InstanceID=POINT,Status=1,DataType=9,LowerLimit='0',UpperLimit='10')
    env[0]['definition']['points']=[point];original=env[1].side_effect
    async def bad(*a,**kw):
        out=json.loads(await original(*a,**kw));out['data']['ServiceDetails'][0]['SamplingDataPointDetails']=[];return json.dumps(out)
    env[1].side_effect=bad
    ready=result(e.prepare_lot_inspection('L','C','Visual','A',samples=[{'points':[{'data_point_name':'Length','value':'11'}]}]))
    assert 'error' in ready


@pytest.mark.parametrize('extra',[{'sampling_lot_result':3},{'scrap_reason':'Bad'},{'sampling_completed':True}])
def test_mcp_completion_rejects_hidden_overrides_before_reads(env,extra):
    async def scenario():
        async with Client(mcp) as client:
            out=await client.call_tool('prepare_sampling_lot_completion',{'lot_name':'L',**extra},raise_on_error=False)
            assert out.is_error
    asyncio.run(scenario());env[3].assert_not_awaited()


def test_completion_cancellation_stays_unknown_and_unsupported_fields_cannot_execute(env):
    passed(env);ready=finish();env[2].request.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):execute(ready)
    stored=result(p.get_quality_transaction_plan(ready['plan_id']))
    assert stored['state']=='unknown' and stored['outcome']['interrupted']
    snapshot=stored['snapshot'];snapshot['body']['ignoreProcessModel']=True
    with pytest.raises(ValueError):p.transport(snapshot)


def test_batch_collection_transport_is_not_a_generic_shopfloor_escape_hatch(env):
    ready=collect();snapshot=result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']
    snapshot['kind']='ncr'
    with pytest.raises(ValueError):p.transport(snapshot)


@pytest.mark.parametrize('measured',[False,True])
def test_mcp_lot_lifecycle_requires_final_evidence_review_between_operations(env,measured):
    if measured:
        env[0]['definition']['test']['SampleType']=2
        point={k:None for k in i.POINT_FIELDS};point.update(Name='Length',Revision='A',InstanceID=POINT,Status=1,DataType=9,LowerLimit='0',UpperLimit='10')
        env[0]['definition']['points']=[point]
    async def server(method,url,**kw):
        body=kw['json'];state=env[0]
        if url.endswith('/CollectLotSamplingData'):
            count=sum(d['totalSamplesCollected'] for d in body['serviceDetails'])
            rejects=sum(d['rejects'] for d in body['serviceDetails']);task=state['tasks'][0]
            task['CompletedSamples']+=count;task['SamplesTested']+=count;task['Rejects']+=rejects;task['SamplesFailed']+=rejects
            task['ChangeCount']+=1;state['lot'].update(ChangeCount='2',SamplingCompleted='true',SamplingPassed='true')
            state['history']=[{'CollectSamplingDataHistoryId':ROW,'TxnId':ROW,'SamplingLotId':LOT,'ContainerId':CID,'Comments':body['comments']}]
        elif url.endswith('/ExecuteSamplingLot'):state['lot'].update(ChangeCount='3',LotStatus='2',SamplingLotResult='1')
        else:raise AssertionError(url)
        return SimpleNamespace(status_code=200,text='{"value":"complete"}')
    env[2].request.side_effect=server
    async def scenario():
        async with Client(mcp) as client:
            async def call(name,args):
                out=await client.call_tool(name,args);assert not out.is_error;return json.loads(out.content[0].text)
            values={'samples':[{'points':[{'data_point_name':'Length','value':'5'}]} for _ in range(8)]} if measured else {'samples_collected':8,'rejects':0}
            collection=await call('prepare_lot_inspection',{'lot_name':'L','container_name':'C','sample_test_name':'Visual','sample_test_revision':'A',**values})
            keys={k:collection[k] for k in ('plan_id','plan_digest')}
            assert (await call('execute_quality_transaction',keys))['state']=='unknown'
            evidence=await call('inspect_quality_transaction',keys)
            assert evidence['verified_success'] is False and len(evidence['history_evidence']['rows'])==1
            completion=await call('prepare_sampling_lot_completion',{'lot_name':'L'})
            complete_keys={k:completion[k] for k in ('plan_id','plan_digest')}
            assert 'error' in await call('execute_quality_transaction',complete_keys)
            reviewed=await call('review_quality_transaction',{**keys,'action':'resolve','evidence_reference':'simulated-mes-final-audit/collection',
                'review_note':'Simulated operator checked final MES audit for this exact transaction.','conclusion':'committed'})
            assert reviewed['state']=='human_committed' and reviewed['verified_success'] is False
            assert (await call('execute_quality_transaction',complete_keys))['state']=='unknown'
            checked=await call('inspect_quality_transaction',complete_keys)
            assert checked['current_lot']['data']['LotStatus']=='2' and checked['verified_success'] is False
            assert (await call('execute_quality_transaction',keys))['submitted'] is False
    asyncio.run(scenario())
    assert env[2].request.await_count==2


def test_failed_lot_cannot_be_completed_as_ok_after_recorded_failure(env):
    def server(*a,**kw):
        state=env[0];state['tasks'][0].update(CompletedSamples=10,SamplesTested=10,Rejects=2,SamplesFailed=2)
        state['lot'].update(ChangeCount='2',SamplingCompleted='true',SamplingPassed='false')
        return SimpleNamespace(status_code=200,text='{"value":"complete"}')
    env[2].request.side_effect=server
    plan=result(e.prepare_lot_inspection('L','C','Visual','A',samples_collected=8,rejects=2,reject_reason_name='Crack'))
    assert execute(plan)['state']=='unknown'
    assert 'error' in finish()
    env[2].request.assert_awaited_once()
