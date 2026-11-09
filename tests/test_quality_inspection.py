import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from agent.safety import plan_preview,evaluate_mutations,MutationCounts
from tools import quality_inspection as i,quality_plans as p,mcp

CID='0000000000000101';TID='0000000000000201';PID='0000000000000301';DID='0000000000000401';REASON='0000000000000501'


def result(coro):return json.loads(asyncio.run(coro))


def definition(kind=1):
    test={k:None for k in i.TEST_FIELDS}
    test.update(InstanceID=TID,Name='Visual',Revision='A',Status=1,IsFrozen=False,SampleType=kind,
        Instructions='Inspect each item',AllowMoveOnFailure=False,ScrapCountedRejectsByReason=False)
    point={k:None for k in i.POINT_FIELDS}
    point.update(InstanceID=DID,Name='Length',Revision='A',Status=1,IsFrozen=False,DataType=9,LowerLimit='0',UpperLimit='10',ShowLimits=True)
    return {'test':test,'points':[] if kind==1 else [point],'reject_reasons':[{'name':'Crack','instanceId':REASON}],
        'reject_group':{'Name':'Reasons','InstanceID':REASON}}


def echo(value):
    if isinstance(value,dict):return {k[0].upper()+k[1:]:echo(v) for k,v in value.items()}
    if isinstance(value,list):return [echo(v) for v in value]
    if type(value) is bool:return str(value).lower()
    if type(value) is int:return str(value)
    return value


@pytest.fixture
def setup(monkeypatch,tmp_path):
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'quality.sqlite3')
    state={'definition':definition(),'container':{'ContainerId':CID,'ContainerName':'C','Status':1,'CurrentHoldCount':0,
        'SamplingRequired':True,'SamplingPassed':False,'SamplingLotId':None,'Qty':20,'ChangeCount':1},
        'tasks':[{'ContainerSampleDataId':PID,'ContainerId':CID,'SamplingPlanId':PID,'SampleTestId':TID,
            'TotalSamplesRequired':10,'CompletedSamples':2,'Rejects':0,'AllowedRejects':1,
            'InspectAllContQty':False,'InspectAllQtyOnFailure':False,'ChangeCount':1}], 'counter':0,'history':[]}
    monkeypatch.setattr(i,'test_definition',AsyncMock(side_effect=lambda *a,**kw:copy.deepcopy(state['definition'])))
    class Context:
        def __init__(self,*a):pass
        async def resolve(self,*a):return copy.deepcopy(state['definition']['test'])
    monkeypatch.setattr(i,'ReadContext',Context)
    monkeypatch.setattr(i.reads,'container_identity',AsyncMock(return_value=({'name':'C','instanceId':CID},{'Qty':20})))
    async def sql(query,maximum):
        if 'FROM ContainerSampleData' in query:return copy.deepcopy(state['tasks'])
        if 'FROM Container WHERE' in query:return [copy.deepcopy(state['container'])]
        if 'FROM CollectSamplingDataHistory' in query:return copy.deepcopy(state['history'])
        raise AssertionError(query)
    sql_call=AsyncMock(side_effect=sql);monkeypatch.setattr(i.reads,'sql_rows',sql_call)
    async def preview(*a,**kw):
        state['counter']+=1;data=echo(kw['body']);task=state['tasks'][0]
        data.update(RequiredSamples=str(task['TotalSamplesRequired']),SamplesTested=str(kw['body'].get('samplesTested',task['CompletedSamples'])),
            AllowedRejects=str(task['AllowedRejects']),SampleType=str(state['definition']['test']['SampleType']),SamplingStatus='In Process')
        for n,detail in enumerate(data['ServiceDetails']):
            detail['InstanceId']=f'{state["counter"]+100+n:016x}';detail['Parent']={'InstanceId':f'{state["counter"]+200:016x}'};detail['CDOTypeId']='8462'
            for point in detail.get('SamplingDataPointDetails',[]):point['CDOTypeId']='8463'
        return json.dumps({'data':data})
    preview_call=AsyncMock(side_effect=preview);monkeypatch.setattr(i,'request',preview_call)
    client=SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200,text='{"value":"complete"}')))
    monkeypatch.setattr(p,'get_client',lambda:client)
    return state,preview_call,client,sql_call


def counted(**kw):return result(i.prepare_container_inspection('C','Visual','A',samples_collected=3,rejects=0,**kw))
def execute(plan):return result(p.execute_quality_transaction(plan['plan_id'],plan['plan_digest']))
def measured(values):return result(i.prepare_container_inspection('C','Visual','A',samples=values))
def sample(value):return {'points':[{'data_point_name':'Length','value':value}]}


def test_counted_plan_is_delta_preserves_comments_and_single_unknown_submission(setup):
    ready=counted(comments='inspected now')
    assert ready['state']=='ready' and ready['submitted'] is False
    body=ready['request_preview']['body'];detail=body['serviceDetails'][0]
    assert body['moveContainerOnCompletion'] is False and body['comments'].endswith(' inspected now')
    assert detail['totalSamplesCollected']==3 and detail['rejects']==0 and detail['rejected'] is False
    assert 'countedRejectReason' not in detail and 'qty' not in body
    assert ready['before']['task']['CompletedSamples']==2
    assert ready['server_context']['samples_this_submission']==3
    assert setup[1].await_args.kwargs['params']['execute']=='false'
    setup[2].request.assert_not_awaited()
    text=plan_preview([{'function':{'name':'execute_quality_transaction','arguments':json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})}}])
    assert 'Visual' in text and 'inspected now' in text and 'samples_this_submission' in text
    assert execute(ready)['state']=='unknown'
    setup[2].request.assert_awaited_once()
    assert setup[2].request.await_args.kwargs['params']=={'execute':'true'}
    assert execute(ready)['submitted'] is False


def test_counted_reject_requires_explicit_configured_reason(setup):
    out=result(i.prepare_container_inspection('C','Visual','A',samples_collected=3,rejects=1,reject_reason_name='Crack'))
    d=out['request_preview']['body']['serviceDetails'][0]
    assert d['samplingDataPointDetails']==[{'dataValue':'1','passed':False,'rejectReason':{'name':'Crack','instanceId':REASON}}]
    assert 'countedRejectReason' not in d and d['rejected'] is True
    for name in (None,'Unknown'):
        assert 'error' in result(i.prepare_container_inspection('C','Visual','A',samples_collected=3,rejects=1,reject_reason_name=name))


@pytest.mark.parametrize('count,rejects',[(True,0),(0,0),(-1,0),(3,True),(3,-1),(3,4),(9,0),(None,0),(3,None),(3,1.0)])
def test_invalid_counted_inputs_never_preview_or_execute(setup,count,rejects):
    assert 'error' in result(i.prepare_container_inspection('C','Visual','A',samples_collected=count,rejects=rejects))
    setup[1].assert_not_awaited();setup[2].request.assert_not_awaited()


def test_counted_mixed_measurements_and_spurious_reason_rejected(setup):
    assert 'error' in counted(samples=[sample('1')])
    assert 'error' in counted(reject_reason_name='Crack')
    setup[1].assert_not_awaited()


def test_measured_groups_values_per_sample_counts_multiple_bad_points_once(setup):
    d=setup[0]['definition']=definition(2)
    d['points'].append({**d['points'][0],'Name':'Width','InstanceID':PID})
    values=[{'points':[{'data_point_name':'Length','value':'0'},{'data_point_name':'Width','value':'10'}]},
        {'points':[{'data_point_name':'Length','value':'-1'},{'data_point_name':'Width','value':'11'}]}]
    ready=measured(values);details=ready['request_preview']['body']['serviceDetails']
    assert len(details)==2 and [r['totalSamplesCollected'] for r in details]==[1,1]
    assert [r['rejects'] for r in details]==[0,1]
    assert [p['passed'] for p in details[1]['samplingDataPointDetails']]==[False,False]
    assert details[1]['samplingDataPointDetails'][0]['dataValue']=='-1'
    assert ready['server_context']['rejects_this_submission']==1
    assert ready['server_context']['local_checks'][0]['failed_sample'] is False
    assert ready['dynamic_validation_verified'] is False


@pytest.mark.parametrize('value',['NaN','Infinity','1+2','1,000',' 1','[Spec.Target]','1e9999',''])
def test_bad_numeric_measurement_never_becomes_a_plan(setup,value):
    setup[0]['definition']=definition(2)
    assert 'error' in measured([sample(value)])
    setup[1].assert_not_awaited()


@pytest.mark.parametrize('mode',['missing','extra','duplicate','non_string','unknown_field','empty','override','bad_limit','bad_type','integer_fraction','inactive'])
def test_measured_full_schema_limits_and_sample_integrity(setup,mode):
    d=setup[0]['definition']=definition(2);values=[sample('1')];kwargs={}
    if mode=='missing':values=[{'points':[]}]
    elif mode=='extra':values[0]['points'].append({'data_point_name':'Extra','value':'2'})
    elif mode=='duplicate':values[0]['points'].append(values[0]['points'][0].copy())
    elif mode=='non_string':values=[sample(1)]
    elif mode=='unknown_field':values[0]['points'][0]['passed']=True
    elif mode=='empty':values=[]
    elif mode=='override':kwargs['samples_collected']=1
    elif mode=='bad_limit':d['points'][0]['LowerLimit']='Spec.Min'
    elif mode=='bad_type':d['points'][0]['DataType']=7
    elif mode=='integer_fraction':d['points'][0]['DataType']=1;values=[sample('1.0')]
    else:d['points'][0]['Status']=2
    assert 'error' in result(i.prepare_container_inspection('C','Visual','A',samples=values,**kwargs))
    setup[1].assert_not_awaited()


@pytest.mark.parametrize('field,value',[('DecreaseBySampleSizeReason',{'Name':'Destructive'}),('DecreaseByRejectCountReason',{'Name':'Scrap'}),
    ('DefaultFailureMode',{'Name':'Mode'}),('Classification',{'Name':'Class'}),('SubClassification',{'Name':'Sub'}),
    ('DefaultPEDescription','Create event'),('ScrapCountedRejectsByReason',True),('ScrapCountedRejectsByReason',None),('Status',2),('Status',True),('SampleType',3),('SampleType',True)])
def test_known_side_effects_or_unsupported_test_modes_block_submission(setup,field,value):
    setup[0]['definition']['test'][field]=value
    assert 'error' in counted()
    setup[1].assert_not_awaited();setup[2].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('SamplingLotId',PID),('CurrentHoldCount',1),('CurrentHoldCount',False),('SamplingRequired',False),('Status',2),('Status',True)])
def test_container_must_be_active_unheld_required_and_not_in_lot(setup,field,value):
    setup[0]['container'][field]=value
    assert 'error' in counted()
    setup[1].assert_not_awaited()


@pytest.mark.parametrize('mode',['no_task','ambiguous','wrong_parent','wrong_test','complete','over_rejects','inspect_all','unknown_inspect_all'])
def test_task_preconditions_do_not_guess_an_executable_task(setup,mode):
    task=setup[0]['tasks'][0]
    if mode=='no_task':setup[0]['tasks']=[]
    elif mode=='ambiguous':setup[0]['tasks'].append({**task,'ContainerSampleDataId':DID})
    elif mode=='wrong_parent':task['ContainerId']=PID
    elif mode=='wrong_test':task['SampleTestId']=DID
    elif mode=='complete':task['CompletedSamples']=10
    elif mode=='over_rejects':task['Rejects']=2
    elif mode=='inspect_all':task['InspectAllContQty']=True
    else:task['InspectAllQtyOnFailure']=None
    assert 'error' in counted()
    setup[1].assert_not_awaited()


@pytest.mark.parametrize('change',['task_count','task_version','container_qty','definition','reason_group'])
def test_execution_rechecks_all_business_snapshots(setup,change):
    ready=counted()
    if change=='task_count':setup[0]['tasks'][0]['CompletedSamples']=3
    elif change=='task_version':setup[0]['tasks'][0]['ChangeCount']=2
    elif change=='container_qty':setup[0]['container']['Qty']=19
    elif change=='definition':setup[0]['definition']['test']['Instructions']='changed'
    else:setup[0]['definition']['reject_reasons'].append({'name':'Other','instanceId':DID})
    assert 'error' in execute(ready)
    setup[2].request.assert_not_awaited()


@pytest.mark.parametrize('change',['wrong_count','wrong_type','moved','truncated','wrong_value','wrong_cdo'])
def test_nonexecuting_service_echo_is_verified_before_plan(setup,change):
    original=setup[1].side_effect
    async def altered(*a,**kw):
        outer=json.loads(await original(*a,**kw));data=outer['data']
        if change=='wrong_count':data['RequiredSamples']='999'
        elif change=='wrong_type':data['SampleType']='2'
        elif change=='moved':data['MoveContainerOnCompletion']='true'
        elif change=='truncated':data['ServiceDetails@odata.nextLink']='next'
        elif change=='wrong_cdo':data['ServiceDetails'][0]['CDOTypeId']='7209'
        else:data['ServiceDetails'][0]['TotalSamplesCollected']='2'
        return json.dumps(outer)
    setup[1].side_effect=altered
    assert 'error' in counted()
    setup[2].request.assert_not_awaited()


def test_unknown_blocks_other_inspection_plans_for_same_container(setup):
    first=counted();second=counted()
    assert execute(first)['state']=='unknown'
    assert 'error' in execute(second)
    setup[2].request.assert_awaited_once()


def test_transient_container_inquiry_ids_do_not_invalidate_plan(setup,monkeypatch):
    counter=iter(range(10))
    async def identity(*a):
        return {'name':'C','instanceId':CID},{'Qty':20,'InstanceId':f'{next(counter)+1000:016x}','Parent':{'InstanceId':PID}}
    monkeypatch.setattr(i.reads,'container_identity',identity)
    assert execute(counted())['state']=='unknown'


def test_observed_query_string_scalar_encoding_is_normalized_before_comparison(setup):
    state=setup[0]
    for field in ('Status','CurrentHoldCount','ChangeCount'):state['container'][field]=str(state['container'][field])
    for field in ('SamplingRequired','SamplingPassed'):state['container'][field]=str(state['container'][field]).lower()
    task=state['tasks'][0]
    for field in ('TotalSamplesRequired','CompletedSamples','Rejects','AllowedRejects','ChangeCount'):task[field]=str(task[field])
    for field in ('InspectAllContQty','InspectAllQtyOnFailure'):task[field]=str(task[field]).lower()
    ready=counted()
    assert ready['before']['task']['CompletedSamples']==2
    assert ready['before']['container']['SamplingRequired'] is True
    assert execute(ready)['state']=='unknown'


def test_unknown_hold_count_is_not_silently_zero(setup):
    setup[0]['container']['CurrentHoldCount']=''
    assert 'error' in counted()
    setup[1].assert_not_awaited()


def test_http_error_preserves_readable_mes_reason(setup):
    setup[1].side_effect=None;setup[1].return_value='HTTP 400 Error: SampleTest.Name is missing'
    out=counted()
    assert 'SampleTest.Name is missing' in out['error']


def test_cancelled_inspection_attempt_stays_unknown_and_inspection_does_not_unlock(setup,monkeypatch):
    ready=counted();setup[2].request.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):execute(ready)
    monkeypatch.setattr(i,'get_container_inspection_tasks',AsyncMock(return_value='{"tasks":[]}'))
    monkeypatch.setattr(i,'get_container_inspection_history',AsyncMock(return_value='{"rows":[]}'))
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['verified_success'] is False and out['state_changed'] is False
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_history_is_bound_to_container_and_is_cumulative_evidence(setup):
    setup[0]['history']=[{'CollectSamplingDataHistoryId':PID,'TxnId':TID,'ContainerId':CID,'TotalSamplesCompleted':7,'TotalRejects':1}]
    out=result(i.get_container_inspection_history('C'))
    assert out['rows'][0]['TotalSamplesCompleted']==7 and out['verified_success'] is False
    query=setup[3].await_args.args[0]
    assert 'h.TxnId=s.TxnId' in query and "h.ContainerId='"+CID+"'" in query
    setup[0]['history'][0]['ContainerId']=DID
    assert 'error' in result(i.get_container_inspection_history('C'))


def test_tasks_return_true_fixed_revision_without_final_verdict(setup,monkeypatch):
    class Context:
        def __init__(self,*a):pass
        async def resolve(self,*a):return copy.deepcopy(setup[0]['definition']['test'])
    monkeypatch.setattr(i,'ReadContext',Context)
    out=result(i.get_container_inspection_tasks('C'))
    assert out['tasks'][0]['remaining_required']==8
    assert out['tasks'][0]['sample_test']=={'name':'Visual','revision':'A','instanceId':TID}
    assert out['quality_verdict'] is None and out['release_authorized'] is False


def test_real_definition_expands_point_limits_and_complete_reason_group(monkeypatch):
    d=definition(2);test=d['test'];test['AQLRejectReasons']={'Name':'Reasons','InstanceId':REASON}
    replies={('SampleTests',TID):{**test,'SampleDataPoints':[{'Name':'Length','Revision':'A','InstanceId':DID}]},
        ('LossReasonGroups',REASON):{'InstanceID':REASON,'ResolvedEntries':[{'Name':'Crack','InstanceId':PID}]}}
    class Context:
        def __init__(self,*a):pass
        async def resolve(self,entity,ref,*a):
            return {'SampleTests':test,'SampleDataPoints':d['points'][0],'LossReasonGroups':{'Name':'Reasons','InstanceID':REASON}}[entity]
        async def get(self,entity,key,params):return copy.deepcopy(replies[(entity,key)])
    monkeypatch.setattr(i,'ReadContext',Context)
    out=result(i.get_inspection_test_definition('Visual','A'))
    assert out['complete'] and out['definition']['points'][0]['UpperLimit']=='10'
    assert out['definition']['reject_reasons']==[{'name':'Crack','instanceId':PID}]
    replies[('SampleTests',TID)]['SampleDataPoints@odata.nextLink']='next'
    assert 'error' in result(i.get_inspection_test_definition('Visual','A'))


def test_literal_numeric_limits_are_inclusive_and_expressions_not_evaluated():
    point=definition(2)['points'][0]
    assert i.point_check(point,'0')[0] and i.point_check(point,'10')[0]
    assert not i.point_check(point,'10.01')[0]
    with pytest.raises(ValueError):i.point_check({**point,'LowerLimit':'11'},'10')


@pytest.mark.parametrize('kind',[1,2])
def test_prepared_wire_body_conforms_to_archived_live_openapi(setup,kind):
    import jsonschema
    from pathlib import Path
    schemas=json.loads(Path('tests/fixtures/inspection_input_schema.json').read_text(encoding='utf-8'))['components']['schemas']
    setup[0]['definition']=definition(kind)
    ready=counted() if kind==1 else measured([sample('1')])
    jsonschema.Draft7Validator({'components':{'schemas':schemas},'$ref':'#/components/schemas/CollectSamplingData'}).validate(ready['request_preview']['body'])


@pytest.mark.parametrize('invalid',[True,'3',1.0])
def test_mcp_counted_quantity_rejects_coercion_before_any_read(setup,invalid):
    async def scenario():
        async with Client(mcp) as client:
            out=await client.call_tool('prepare_container_inspection',{'container_name':'C','sample_test_name':'Visual',
                'sample_test_revision':'A','samples_collected':invalid,'rejects':0},raise_on_error=False)
            assert out.is_error
    asyncio.run(scenario())
    i.test_definition.assert_not_awaited();setup[1].assert_not_awaited();setup[2].request.assert_not_awaited()


def test_mcp_measured_prepare_confirmation_execute_and_inspection_in_one_session(setup):
    setup[0]['definition']=definition(2)
    async def scenario():
        async with Client(mcp) as client:
            async def call(name,args):
                out=await client.call_tool(name,args)
                assert not out.is_error
                return json.loads(out.content[0].text)
            ready=await call('prepare_container_inspection',{'container_name':'C','sample_test_name':'Visual','sample_test_revision':'A','samples':[sample('11')]})
            keys={k:ready[k] for k in ('plan_id','plan_digest')}
            calls=[{'function':{'name':'execute_quality_transaction','arguments':json.dumps(keys)}}]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval
            text=plan_preview(calls);assert 'Length' in text and '11' in text
            assert (await call('execute_quality_transaction',keys))['state']=='unknown'
            inspected=await call('inspect_quality_transaction',keys)
            assert inspected['verified_success'] is False
            assert 'error' not in inspected['current_tasks'] and 'error' not in inspected['history_evidence']
            assert (await call('execute_quality_transaction',keys))['submitted'] is False
    asyncio.run(scenario())
    setup[2].request.assert_awaited_once()

