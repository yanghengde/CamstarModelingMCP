"""Native full-inspection / measured destructive-test opt-ins; no second write."""
import asyncio,copy,json
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from tools import mcp,quality_inspection as i,quality_inspection_effects as fx,quality_plans as p,quality_history as h
from tests.test_quality_quantity_loss import env as loss_env,CID,SPEC,OP,STEP,REASON,GROUP,UOM,ref,key,echo
from tests.test_quality_inspection import definition
from agent.safety import evaluate_mutations,MutationCounts,plan_preview

TEST,POINT,PLAN,TASK=[key(n) for n in range(5001,5005)]
def result(coro):return json.loads(asyncio.run(coro))


@pytest.fixture
def env(loss_env,monkeypatch):
    state=loss_env;state['row'].update(SamplingRequired=True,SamplingPassed=False,InQualityControl=False)
    data=definition(2);data['test']['InstanceID']=TEST;data['points'][0]['InstanceID']=POINT
    data['test']['DecreaseBySampleSizeReason']=ref('SCRAP',REASON)
    state['definition']=data
    state['models'].update(SampleTests=[data['test']],SampleDataPoints=data['points'])
    state['counters'].update({TEST:1,POINT:1})
    state['task']=dict(ContainerSampleDataId=TASK,ContainerId=CID,SamplingPlanId=PLAN,SampleTestId=TEST,
        TotalSamplesRequired=10,CompletedSamples=2,Rejects=0,AllowedRejects=1,InspectAllContQty=False,InspectAllQtyOnFailure=False,ChangeCount=1)
    state.update(child_hook=None,parent_hook=None)
    monkeypatch.setattr(i,'test_definition',AsyncMock(side_effect=lambda *a,**kw:copy.deepcopy(state['definition'])))
    original_sql=fx.q.sql_rows
    async def sql(query,maximum):
        if 'FROM Container WHERE ContainerId=' in query:return [copy.deepcopy(state['row'])]
        if 'FROM ContainerSampleData' in query:
            if "ContainerSampleDataId='" in query:
                return [copy.deepcopy(state['task'])] if state['task']['ContainerSampleDataId']==TASK and state['task']['ContainerId']==CID else []
            return [copy.deepcopy(state['task'])]
        for _,(table,column,_) in fx.EXTRA_ASSETS.items():
            if ' FROM '+table+' WHERE ' in query:
                iid=query.split("='",1)[1].split("'",1)[0]
                return [{column:iid,'ChangeCount':str(state['counters'][iid])}]
        return await original_sql(query,maximum)
    monkeypatch.setattr(fx.q,'sql_rows',AsyncMock(side_effect=sql))
    async def child(method,path,**kw):
        assert method=='POST' and path=='/api/ChangeQty' and kw['params']['execute']=='false'
        assert set(kw['body'])=={'container','serviceDetails'}
        line=kw['body']['serviceDetails'][0]
        assert set(line)=={'@odata.type','changeQtyType','qty','reasonCode'}
        data=echo(kw['body']);data.update({k:'true' for k in ('CloseWhenZero','DisassociateWhenClosed','GenerateRollups','RecordAllQtySummaryOnly')})
        data['ServiceDetails'][0].update(Container=copy.deepcopy(data['Container']),CDOTypeId='4030',QtyMultiplier='-1',
            CountsAgainstProduction='true',RecordAllQty='false',AdjustThruput='true',ChargeToStep=ref('Inspect',STEP))
        if state['child_hook']:state['child_hook'](data)
        return json.dumps({'data':data})
    children=AsyncMock(side_effect=child);monkeypatch.setattr(fx,'request',children)
    async def parent(method,path,**kw):
        assert path=='/api/CollectSamplingData' and kw['params']['execute']=='false'
        data=echo(kw['body']);task=state['task']
        data.update(RequiredSamples=str(task['TotalSamplesRequired']),SamplesTested=str(kw['body'].get('samplesTested',task['CompletedSamples'])),
            AllowedRejects=str(task['AllowedRejects']),SampleType=str(state['definition']['test']['SampleType']),
            SamplingStatus='In Process',ContainerSampleDataItem={'InstanceId':TASK})
        for line in data['ServiceDetails']:
            line['CDOTypeId']='8462'
            for point in line.get('SamplingDataPointDetails',[]):point['CDOTypeId']='8463'
        if state['parent_hook']:state['parent_hook'](data)
        return json.dumps({'data':data})
    parents=AsyncMock(side_effect=parent);monkeypatch.setattr(i,'request',parents)
    state.update(children=children,parents=parents)
    return state


def prepare(**kw):
    args=dict(container_name='C',sample_test_name='Visual',sample_test_revision='A',
        samples=[{'points':[{'data_point_name':'Length','value':'5'}]} for _ in range(3)],allow_automatic_quantity_loss=True)
    args.update(kw);return result(i.prepare_container_inspection(**args))
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))
def snap(ready):return result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']
def full(env,**kw):
    env['definition']['test'].update(SampleType=1,DecreaseBySampleSizeReason=None)
    env['definition']['points']=[];env['models']['SampleDataPoints']=[]
    env['task']['InspectAllContQty']=True
    return prepare(samples=None,samples_collected=1,rejects=0,allow_automatic_quantity_loss=False,allow_full_inspection=True,**kw)


def test_measured_native_sample_size_loss_is_one_parent_write(env):
    ready=prepare();assert ready.get('state')=='ready',ready
    context=ready['server_context']['native_effects'];quantity=context['automatic_quantity_loss']
    assert quantity['quantity']=='3' and quantity['projected_remaining_quantity']=='7' and quantity['reason']==dict(name='SCRAP',instanceId=REASON)
    assert not context['separate_quantity_submission'] and not context['runtime_effects_verified']
    assert quantity['native_child_defaults']['root']['CloseWhenZero'] is True
    assert quantity['native_child_defaults']['adjust_throughput'] is True
    assert not quantity['native_child_defaults']['record_all_quantity']
    body=ready['request_preview']['body']
    assert set(body)=={'container','sampleTest','serviceDetails','moveContainerOnCompletion','comments'}
    assert not body['moveContainerOnCompletion'] and 'changeQty' not in body
    assert len(snap(ready)['inspection_effects']['dependencies'])==7
    assert execute(ready)['state']=='unknown';env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/api/CollectSamplingData')
    assert all(call.kwargs['params']['execute']=='false' for call in env['children'].await_args_list)
    assert env['children'].await_count==2


def test_existing_full_inspection_continues_without_patching_flags(env):
    ready=full(env);assert ready.get('state')=='ready',ready
    context=ready['server_context']['native_effects']['full_inspection']
    assert context['stored_inspect_all'] and context['stored_required_samples']==10
    assert not context['task_flags_overridden'] and not context['conversion_verified']
    assert execute(ready)['state']=='unknown';env['children'].assert_not_awaited()
    assert env['client'].request.await_args.kwargs['json']['serviceDetails'][0]['totalSamplesCollected']==1


def test_failure_rule_is_not_claimed_as_converted_or_locally_recomputed(env):
    env['definition']['test']['DecreaseBySampleSizeReason']=None
    env['task'].update(InspectAllQtyOnFailure=True,TotalSamplesRequired=5)
    ready=prepare(allow_automatic_quantity_loss=False,allow_full_inspection=True)
    assert ready.get('state')=='ready',ready
    context=ready['server_context']['native_effects']['full_inspection']
    assert not context['stored_inspect_all'] and context['failure_rule_enabled'] and context['stored_required_samples']==5
    assert '没有实际设置' in context['note'] and not context['conversion_verified']


def test_failed_task_with_mes_whole_requirement_can_continue(env):
    env['task'].update(CompletedSamples=4,Rejects=2)
    ready=full(env);assert ready.get('state')=='ready',ready
    assert ready['before']['task']['Rejects']>ready['before']['task']['AllowedRejects']


@pytest.mark.parametrize('mode',['no_ack','both','counted','reject_reason','two_reasons','scrap','event','description','unknown_scrap','zero_balance'])
def test_unsupported_native_effect_combinations_never_submit(env,mode):
    args={};test=env['definition']['test']
    if mode=='no_ack':args['allow_automatic_quantity_loss']=False
    if mode=='both':args['allow_full_inspection']=True
    if mode=='counted':test['SampleType']=1
    if mode=='reject_reason':test.update(DecreaseBySampleSizeReason=None,DecreaseByRejectCountReason=ref('SCRAP',REASON))
    if mode=='two_reasons':test['DecreaseByRejectCountReason']=ref('SCRAP',REASON)
    if mode=='scrap':test['ScrapCountedRejectsByReason']=True
    if mode=='event':test['DefaultFailureMode']=ref('Failure',REASON)
    if mode=='description':test['DefaultPEDescription']='create event'
    if mode=='unknown_scrap':test['ScrapCountedRejectsByReason']=None
    if mode=='zero_balance':env['row']['Qty']='3'
    assert 'error' in prepare(**args);env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['no_rule','unknown','mismatch','failed_not_rebased','fractional','excess','no_ack','qty_combined'])
def test_full_inspection_must_use_configured_current_mes_task(env,mode):
    env['definition']['test']['DecreaseBySampleSizeReason']=None
    env['task']['InspectAllQtyOnFailure']=True
    args=dict(allow_automatic_quantity_loss=False,allow_full_inspection=True)
    if mode=='no_rule':env['task']['InspectAllQtyOnFailure']=False
    if mode=='unknown':env['task']['InspectAllContQty']=None
    if mode=='mismatch':env['task'].update(InspectAllContQty=True,TotalSamplesRequired=5)
    if mode=='failed_not_rebased':env['task'].update(CompletedSamples=4,Rejects=2,TotalSamplesRequired=5)
    if mode=='fractional':env['row']['Qty']='10.5'
    if mode=='excess':env['task']['TotalSamplesRequired']=4
    if mode=='no_ack':args['allow_full_inspection']=False
    if mode=='qty_combined':args['allow_automatic_quantity_loss']=True
    assert 'error' in prepare(**args);env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('UOMId',''),('CarrierId',REASON),('CDOTypeId','7939'),('ParentContainerId',REASON),
    ('ChildCount','1'),('SamplingPassed',True),('SamplingPassed',None),('UOM2Id',REASON),('SamplingLotId',REASON),('InRework','true')])
def test_advanced_modes_enforce_complete_flat_container_scope(env,field,value):
    env['row'][field]=value
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('CDOTypeId','4010'),('QtyMultiplier','1'),('RecordAllQty','true'),
    ('Qty','2'),('ReasonCode',ref('Other',REASON)),('Container',ref('Other',CID)),('CountsAgainstProduction','false'),
    ('Qty2','1'),('AdjustThruput',None)])
def test_child_nonexecuting_defaults_must_match_native_loss(env,field,value):
    env['child_hook']=lambda data:data['ServiceDetails'][0].update({field:value})
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('value',[None,{'InstanceId':REASON}])
def test_parent_requires_original_persistent_task_id(env,value):
    env['parent_hook']=lambda data:data.update(ContainerSampleDataItem=value)
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('target',['counter','test','point','qty','group','status','default'])
def test_rebuild_blocks_configuration_quantity_and_runtime_default_changes(env,target):
    ready=prepare();assert ready.get('state')=='ready',ready
    if target=='counter':env['counters'][TEST]+=1
    if target=='test':env['definition']['test']['Instructions']='changed'
    if target=='point':env['definition']['points'][0]['UpperLimit']='9'
    if target=='qty':env['row']['Qty']='9'
    if target=='group':env['group']['Description']='changed'
    if target=='status':env['status_hook']=lambda data:data.update(InProcess=False)
    if target=='default':env['child_hook']=lambda data:data.update(CloseWhenZero='false')
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_test_loss_reason_must_belong_to_operation_group(env):
    env['group']['ResolvedEntries']=[]
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field',['qty','changeQty','inspectAllContQty','processESignatures'])
def test_transport_cannot_add_client_quantity_or_task_overrides(env,field):
    saved=snap(prepare());saved['body'][field]=True
    with pytest.raises(ValueError):p.transport(saved)


def test_unknown_does_not_reissue_child_or_follow_replacement_task(env):
    ready=prepare();second=prepare();assert execute(ready)['state']=='unknown'
    assert 'error' in execute(ready) and 'error' in execute(second)
    env['row']['Qty']='7';env['task']['ContainerSampleDataId']=REASON
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['current_quantity_record']['Qty']=='7' and out['original_task_id']==TASK and out['current_original_task'] is None
    assert not out['verified_success'] and not out['state_changed'] and not out['retry_allowed']
    assert 'error' in result(h.find_quality_execution_candidates(ready['plan_id'],ready['plan_digest']))
    env['client'].request.assert_awaited_once()


def test_mcp_optins_closed_schema_and_confirmation(env):
    async def run():
        async with Client(mcp) as client:
            schema=next(t.input_schema for t in await client.list_tools() if t.name=='prepare_container_inspection')
            assert schema['additionalProperties'] is False
            assert schema['properties']['allow_full_inspection']['default'] is False
            assert schema['properties']['allow_automatic_quantity_loss']['default'] is False
            raw=await client.call_tool('prepare_container_inspection',dict(container_name='C',sample_test_name='Visual',sample_test_revision='A',
                samples=[{'points':[{'data_point_name':'Length','value':'5'}]}],allow_automatic_quantity_loss=True))
            ready=json.loads(raw.content[0].text);assert ready.get('state')=='ready',ready
            args={k:ready[k] for k in ('plan_id','plan_digest')}
            calls=[{'function':{'name':'execute_quality_transaction','arguments':json.dumps(args)}}]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval
            assert 'automatic_quantity_loss' in plan_preview(calls)
            sent=await client.call_tool('execute_quality_transaction',args)
            assert json.loads(sent.content[0].text)['state']=='unknown'
    asyncio.run(run());env['client'].request.assert_awaited_once()


@pytest.mark.parametrize('what',['carrier','group'])
def test_parent_preview_cannot_hide_a_late_dependency_change(env,what):
    def hook(data):
        if what=='carrier':env['row']['CarrierId']=REASON
        else:env['group']['Description']='changed during final parent HTTP read'
    env['parent_hook']=hook
    out=prepare()
    assert 'error' in out and not p.PLAN_DB.exists()
    env['client'].request.assert_not_awaited()
