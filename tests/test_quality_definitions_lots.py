import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import mcp,quality_definitions as d,quality_lots as l,quality_plans as p
from agent.safety import plan_preview,evaluate_mutations,MutationCounts

POINT='0000000000000101';TEST='0000000000000201';PLAN='0000000000000301';PROD='0000000000000401';UNIT='0000000000000501';LOT='0000000000000601'


def result(coro):return json.loads(asyncio.run(coro))


def root(name,iid,**kw):
    return {'Name':name,'InstanceID':iid,'Revision':'A','IsRevOfRcd':True,'IsFrozen':False,'Status':1,'Description':'saved',**kw}


@pytest.fixture
def env(monkeypatch,tmp_path):
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'quality.sqlite3')
    state={'rows':{'SampleDataPoints':[root('Length',POINT,DataType=9,LowerLimit='0',UpperLimit='10',DefaultValue=None,ShowLimits=True,UOM=None)],
        'SampleTests':[root('Visual',TEST,SampleType=1,SampleDataPoints=[],AllowMoveOnFailure=False)],
        'SamplingPlans':[root('Plan',PLAN,SamplingPlanDetails=[{'InstanceID':PLAN,'SampleTest':{'Name':'Visual','Revision':'A','InstanceId':TEST},
            'Sequence':1,'IsFrozen':False,'SampleRate':1}])],
        'Products':[root('Product',PROD,SamplingPlan={'Name':'Plan','Revision':'A','InstanceId':PLAN},SamplingPlanResolved=None)],
        'UOMs':[{'Name':'Each','InstanceID':UNIT}], 'AQLLevels':[{'Name':'AQL','InstanceID':UNIT}],
        'InspectionLevels':[{'Name':'Level','InstanceID':UNIT}],'LossReasonGroups':[{'Name':'Reasons','InstanceID':UNIT}]},
        'counters':{POINT:1,TEST:1,PLAN:1},'lot':None,'containers':[],'samples':[]}
    class Context:
        def __init__(self,*a,**kw):pass
        async def resolve(self,entity,ref,revisioned=True):
            found=[r for r in state['rows'].get(entity,[]) if (not ref.get('Name') or ref['Name']==r['Name'])
                and (not (ref.get('InstanceID') or ref.get('InstanceId')) or (ref.get('InstanceID') or ref.get('InstanceId'))==r['InstanceID'])
                and (not ref.get('Revision') or ref['Revision']==r.get('Revision'))]
            if len(found)!=1:raise ValueError('reference absent or ambiguous')
            return copy.deepcopy(found[0])
        async def get(self,entity,key=None,params=None):
            if key:
                return copy.deepcopy(next(r for r in state['rows'][entity] if r['InstanceID']==key))
            expression=params['$filter'];name=expression.split("Name eq '",1)[1].split("'",1)[0]
            rows=[r for r in state['rows'].get(entity,[]) if r['Name']==name]
            if "Revision eq '" in expression:
                revision=expression.split("Revision eq '",1)[1].split("'",1)[0];rows=[r for r in rows if r.get('Revision')==revision]
            return {'value':copy.deepcopy(rows[:2])}
    monkeypatch.setattr(d,'ReadContext',Context);monkeypatch.setattr(l,'ReadContext',Context)
    async def sql(query,maximum):
        for table,key in d.TABLES.values():
            if ' FROM '+table+' WHERE ' in query:
                iid=query.split("='",1)[1].split("'",1)[0]
                return [{key:iid,'ChangeCount':str(state['counters'][iid])}]
        if ' FROM MfgLot WHERE ' in query:
            lot=state['lot']
            if lot and (("MfgLotName=N'"+lot['MfgLotName']+"'") in query or ("MfgLotId='"+lot['MfgLotId']+"'") in query):return [copy.deepcopy(lot)]
            return []
        if 'FROM Container WHERE' in query:return copy.deepcopy(state['containers'])
        if 'FROM LotSampleData' in query:return copy.deepcopy(state['samples'])
        raise AssertionError(query)
    query=AsyncMock(side_effect=sql);monkeypatch.setattr(d.reads,'sql_rows',query)
    async def preview(*a,**kw):
        body=kw['body'];data={k[0].upper()+k[1:]:copy.deepcopy(v) for k,v in body.items()}
        data['UOM']=data.pop('Uom');data['Qty']=str(body['qty'])
        data['AllowAdjustToLotSize']=str(body['allowAdjustToLotSize']).lower()
        if 'sampleRate' in body:data['SampleRate']=str(body['sampleRate'])
        for value in data.values():
            if isinstance(value,dict):
                for key in list(value):value[key[0].upper()+key[1:]]=value.pop(key)
        return json.dumps({'data':data})
    preview_call=AsyncMock(side_effect=preview);monkeypatch.setattr(l,'request',preview_call)
    client=SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200,text='{"value":"saved"}')))
    monkeypatch.setattr(p,'get_client',lambda:client)
    return state,preview_call,client,query


def point(action='create',name='NewPoint',revision='A',**kw):
    changes=kw.pop('changes',{'data_type':'decimal','lower_limit':'0','upper_limit':'10','show_limits':True})
    return result(d.prepare_inspection_definition('sample_data_point',action,name,revision,changes,**kw))


def execute(plan):return result(p.execute_quality_transaction(plan['plan_id'],plan['plan_digest']))
def create_lot(**kw):return result(l.prepare_sampling_lot_create('Lot','Product','A','10.5','Each',**kw))


def test_modeling_static_preview_routes_to_post_not_shopfloor_and_one_attempt(env):
    ready=point();assert ready['state']=='ready' and ready['static_and_echo_verified'] is False
    assert ready['server_context']['echo_verified'] is False
    assert ready['request_preview']['method']=='POST' and ready['request_preview']['path']=='/api/SampleDataPoints'
    assert ready['request_preview']['body']['dataType']==9
    env[1].assert_not_awaited();env[2].request.assert_not_awaited()
    assert execute(ready)['state']=='unknown'
    call=env[2].request.await_args
    assert '/Modeling/api/SampleDataPoints' in call.args[1] and call.args[0]=='POST'
    assert call.kwargs['params']=={} and call.kwargs['follow_redirects'] is False
    assert execute(ready)['submitted'] is False


def test_update_preserves_omitted_fields_and_explicit_null_clears(env):
    ready=point('update','Length',changes={'lower_limit':None,'description':'updated'})
    body=ready['request_preview']['body']
    assert body['lowerLimit'] is None and 'upperLimit' not in body and body['dataType']==9
    assert ready['before']['UpperLimit']=='10'
    assert ready['request_preview']['path'].endswith(POINT)


def test_revision_requires_explicit_current_ror_and_uses_patch(env):
    ready=point('revise','Length','B',source_revision='A',changes={'description':'next'})
    assert ready['request_preview']['method']=='PATCH' and ready['request_preview']['path']=='/api/SampleDataPoints'
    assert ready['request_preview']['body']['revision']=='B'
    env[0]['rows']['SampleDataPoints'][0]['IsRevOfRcd']=False
    assert 'error' in execute(ready)
    env[2].request.assert_not_awaited()


@pytest.mark.parametrize('kind,action,name,changes,extra',[
    ('sample_data_point','create','Length',{'data_type':'float'},{}),
    ('sample_data_point','create','New',{},{}),
    ('sample_data_point','create','New',{'data_type':'float','lower_limit':'11','upper_limit':'10'},{}),
    ('sample_data_point','create','New',{'data_type':'float','upper_limit':'Spec.Max'},{}),
    ('sample_data_point','create','New',{'sample_type':'counted'},{}),
    ('sample_data_point','update','Length',{'description':'x'},{'source_revision':'A'}),
    ('sample_data_point','revise','Length',{'description':'x'},{}),
    ('sample_test','create','New',{'sample_type':'measured'},{}),
    ('sample_test','create','New',{'sample_type':'counted','sample_data_points':[{'name':'Length','revision':'A'}]},{}),
    ('sampling_plan','create','New',{'sample_rate':0},{}),
    ('sampling_plan','create','New',{'tests':[]},{}),
    ('sampling_plan','create','New',{'tests':[{'op':'remove','sample_test':{'name':'Visual','revision':'A'}}]},{}),
])
def test_bad_definition_commands_never_write(env,kind,action,name,changes,extra):
    out=result(d.prepare_inspection_definition(kind,action,name,'A',changes,**extra))
    assert 'error' in out
    env[2].request.assert_not_awaited()


def test_measured_test_requires_active_unique_numeric_fixed_point_references(env):
    args={'sample_type':'measured','sample_data_points':[{'name':'Length','revision':'A'}]}
    ready=result(d.prepare_inspection_definition('sample_test','create','Measured','A',args))
    body=ready['request_preview']['body'];assert body['sampleType']==2
    assert body['sampleDataPoints'][0]['instanceId']==POINT and body['scrapCountedRejectsByReason'] is False
    args['sample_data_points']*=2
    assert 'error' in result(d.prepare_inspection_definition('sample_test','create','Measured','A',args))


def test_measured_reference_list_full_replacement_deletes_original_indices_descending(env):
    env[0]['rows']['SampleTests'][0].update(SampleType=2,SampleDataPoints=[{'InstanceId':POINT,'Name':'Length','Revision':'A'},
        {'InstanceId':PROD,'Name':'Old','Revision':'A'}])
    out=result(d.prepare_inspection_definition('sample_test','update','Visual','A',{'sample_data_points':[{'name':'Length','revision':'A'}]}))
    changes=out['request_preview']['body']['sampleDataPoints-Expanded']
    assert changes[:2]==[{'index':1,'listItemAction':'delete'},{'index':0,'listItemAction':'delete'}]
    assert changes[2]['listItemAction']=='add'
    other=result(d.prepare_inspection_definition('sample_test','update','Visual','A',{'instructions':'new'}))
    assert 'sampleDataPoints-Expanded' not in other['request_preview']['body']


def test_plan_details_create_and_remove_have_contract_correct_payloads(env):
    args={'aql_level_name':'AQL','inspection_level_name':'Level','tests':[{'op':'add','sample_test':{'name':'Visual','revision':'A'}}]}
    ready=result(d.prepare_inspection_definition('sampling_plan','create','PlanNew','A',args))
    body=ready['request_preview']['body'];row=body['samplingPlanDetails'][0]
    assert body['moveContainerOnCompletion'] is False and row['inspectAllQtyOnFailure'] is False and row['isFrozen'] is False
    out=result(d.prepare_inspection_definition('sampling_plan','update','Plan','A',{'tests':[{'op':'remove','sample_test':{'name':'Visual','revision':'A'}}]}))
    assert out['request_preview']['body']['samplingPlanDetails-Expanded']==[{'index':0,'listItemAction':'delete'}]


def test_plan_updates_do_not_select_ambiguous_repeated_test_without_row_id(env):
    rows=env[0]['rows']['SamplingPlans'][0]['SamplingPlanDetails'];rows.append({**rows[0],'InstanceID':POINT,'Sequence':2})
    item={'op':'update','sample_test':{'name':'Visual','revision':'A'},'sample_rate':2}
    assert 'error' in result(d.prepare_inspection_definition('sampling_plan','update','Plan','A',{'tests':[item]}))
    item['detail_instance_id']=POINT
    out=result(d.prepare_inspection_definition('sampling_plan','update','Plan','A',{'tests':[item]}))
    assert out['request_preview']['body']['samplingPlanDetails-Expanded'][0]['index']==1


def test_pending_definition_reads_and_writes_conflict_in_both_directions(env):
    def measured():return result(d.prepare_inspection_definition('sample_test','create','Measured','A',{
        'sample_type':'measured','sample_data_points':[{'name':'Length','revision':'A'}]}))
    first=measured();second=point('update','Length',changes={'description':'change'})
    assert execute(first)['state']=='unknown'
    assert 'error' in execute(second)
    env[2].request.assert_awaited_once()


def test_changed_database_counter_and_frozen_state_are_rechecked(env):
    ready=point('update','Length',changes={'description':'new'})
    env[0]['counters'][POINT]=2
    assert 'error' in execute(ready)
    env[2].request.assert_not_awaited()
    env[0]['rows']['SampleDataPoints'][0]['IsFrozen']=True
    assert 'error' in point('update','Length',changes={'description':'new'})


def test_product_binding_is_exact_version_and_modeling_put(env):
    ready=result(d.prepare_product_sampling_plan('Product','A','Plan','A'))
    assert ready['request_preview']['body']=={'samplingPlan':{'name':'Plan','revision':'A','instanceId':PLAN}}
    assert ready['request_preview']['method']=='PUT' and ready['request_preview']['path']=='/api/Products/'+PROD
    assert ready['static_and_echo_verified'] is False


def test_create_lot_quantity_units_and_read_only_containers_contract(env):
    ready=create_lot(sample_rate=2)
    assert ready['state']=='ready' and ready['static_and_echo_verified'] is True
    body=ready['request_preview']['body']
    assert body['qty']==10.5 and body['uom']['instanceId']==UNIT and body['allowAdjustToLotSize'] is False
    assert 'containers' not in body and body['sampleRate']==2
    assert ready['server_context']['sample_rate_overrides_plan_rates'] is True
    assert execute(ready)['state']=='unknown'
    assert env[2].request.await_args.args[1].endswith('/Shopfloor/api/CreateSamplingLot')
    assert env[2].request.await_args.kwargs['params']=={'execute':'true'}


@pytest.mark.parametrize('quantity',['0','-1','NaN','1e13','0.10000000000000000001',True])
def test_invalid_or_lossy_lot_quantity_never_previews(env,quantity):
    assert 'error' in result(l.prepare_sampling_lot_create('Lot','Product','A',quantity,'Each'))
    env[1].assert_not_awaited();env[2].request.assert_not_awaited()


def test_lot_uses_nonempty_resolved_plan_or_explicit_direct_plan(env):
    env[0]['rows']['Products'][0]['SamplingPlanResolved']={'Name':None,'InstanceId':None}
    assert create_lot()['state']=='ready'
    env[0]['rows']['Products'][0]['SamplingPlan']=None
    assert 'error' in create_lot()


def test_unknown_product_binding_prevents_lot_creation_using_that_product(env):
    binding=result(d.prepare_product_sampling_plan('Product','A','Plan','A'));lot=create_lot()
    assert execute(binding)['state']=='unknown'
    assert 'error' in execute(lot)
    env[2].request.assert_awaited_once()


def test_lot_echo_wrong_quantity_or_units_rejected(env):
    original=env[1].side_effect
    async def bad(*a,**kw):
        data=json.loads(await original(*a,**kw));data['data']['Qty']='11';return json.dumps(data)
    env[1].side_effect=bad
    assert 'error' in create_lot()
    env[2].request.assert_not_awaited()


def test_lot_read_discriminator_and_parent_linkage(env):
    env[0]['lot']={'MfgLotId':LOT,'MfgLotName':'Lot','CDOTypeId':'8587','Qty':'10','ChangeCount':'1'}
    env[0]['containers']=[{'ContainerId':PROD,'ContainerName':'C','SamplingLotId':LOT}]
    out=result(l.get_sampling_lot_details('Lot'))
    assert out['found'] and out['quality_verdict'] is None and out['containers']['rows'][0]['ContainerName']=='C'
    env[0]['containers'][0]['SamplingLotId']=POINT
    assert 'error' in result(l.get_sampling_lot_details('Lot'))
    env[0]['lot']['CDOTypeId']='1111'
    assert 'error' in result(l.get_sampling_lot_details('Lot'))


@pytest.mark.parametrize('kind',['sample_data_point','sample_test','sampling_plan'])
def test_created_payload_matches_live_schema(env,kind):
    import jsonschema
    archive=json.loads(Path('docs/quality-fixtures/inspection-contracts.json').read_text(encoding='utf-8'))
    cases={'sample_data_point':{'data_type':'float','lower_limit':'0','upper_limit':'10'},
        'sample_test':{'sample_type':'counted'},
        'sampling_plan':{'tests':[{'op':'add','sample_test':{'name':'Visual','revision':'A'}}]}}
    ready=result(d.prepare_inspection_definition(kind,'create','New','A',cases[kind]))
    entity=d.ENTITIES[kind];schemas=archive[entity]['schema']['components']['schemas']
    typ={'sample_data_point':'SampleDataPointEntity','sample_test':'SampleTestEntity','sampling_plan':'SamplingPlanEntity'}[kind]
    jsonschema.Draft7Validator({'components':{'schemas':schemas},'$ref':'#/components/schemas/'+typ}).validate(ready['request_preview']['body'])


@pytest.mark.parametrize('changes',[{'data_type':'float','status':True},{'data_type':'float','bogus':1}])
def test_mcp_changes_reject_invalid_fields_before_database_reads(env,changes):
    async def scenario():
        async with Client(mcp) as client:
            out=await client.call_tool('prepare_inspection_definition',{'definition_kind':'sample_data_point','action':'create','name':'New','revision':'A','changes':changes},raise_on_error=False)
            assert out.is_error
    asyncio.run(scenario());env[3].assert_not_awaited()


def test_mcp_definition_prepare_confirmation_and_inspection_keep_unknown(env):
    async def scenario():
        async with Client(mcp) as client:
            async def call(name,args):
                out=await client.call_tool(name,args);assert not out.is_error;return json.loads(out.content[0].text)
            ready=await call('prepare_inspection_definition',{'definition_kind':'sample_data_point','action':'create','name':'New','revision':'A','changes':{'data_type':'float'}})
            keys={k:ready[k] for k in ('plan_id','plan_digest')};calls=[{'function':{'name':'execute_quality_transaction','arguments':json.dumps(keys)}}]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval
            assert 'modeling' in plan_preview(calls) and 'SampleDataPoints' in plan_preview(calls)
            assert (await call('execute_quality_transaction',keys))['state']=='unknown'
            inspected=await call('inspect_quality_transaction',keys)
            assert inspected['current_configuration'] is None and inspected['verified_success'] is False
            assert (await call('execute_quality_transaction',keys))['submitted'] is False
    asyncio.run(scenario());env[2].request.assert_awaited_once()


def test_transport_does_not_allow_arbitrary_endpoints_or_delete():
    base={'kind':'inspection_definition','api':'modeling','action':'update','revision':'A','method':'PUT','entity':'SampleTests','target_id':TEST,'path':'/api/SampleTests/'+TEST}
    assert p.transport(base)['method']=='PUT'
    for key,value in [('entity','Employees'),('path','/api/Anything'),('method','DELETE'),('api','external')]:
        with pytest.raises(ValueError):p.transport({**base,key:value})
    with pytest.raises(ValueError):p.transport({**base,'kind':'sampling_binding'})


def test_plan_root_rate_is_inherited_by_new_details_without_accidental_every_container_override(env):
    ready=result(d.prepare_inspection_definition('sampling_plan','create','NewPlan','A',{'sample_rate':5,
        'tests':[{'op':'add','sample_test':{'name':'Visual','revision':'A'}}]}))
    body=ready['request_preview']['body']
    assert body['sampleRate']==5 and body['samplingPlanDetails'][0]['sampleRate']==5


def test_same_plan_row_cannot_be_changed_twice_using_two_aliases(env):
    ref={'name':'Visual','revision':'A'}
    commands=[{'op':'update','sample_test':ref,'sample_rate':2},
        {'op':'remove','sample_test':ref,'detail_instance_id':PLAN}]
    assert 'error' in result(d.prepare_inspection_definition('sampling_plan','update','Plan','A',{'tests':commands}))


@pytest.mark.parametrize('direction',[0,1])
def test_modeling_writes_conflict_with_pending_inspection_dependencies_but_reads_share(env,direction):
    inspection={'kind':'inspection','scope_key':'container-a','target_id':PROD,'environment':p.environment(),
        'definition':{'test':{'Name':'Visual','InstanceID':TEST},'points':[{'Name':'Length','InstanceID':POINT}]},
        'before':{'task':{'SamplingPlanId':PLAN}}}
    other={**inspection,'scope_key':'container-b','target_id':LOT}
    assert not p.conflicts(inspection,other)
    ready=point('update','Length',changes={'description':'new'})
    snapshot=result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']
    a,b=(snapshot,inspection) if direction==0 else (inspection,snapshot)
    assert p.conflicts(a,b)


def test_cancelled_modeling_write_keeps_unknown_without_a_comments_field(env):
    ready=point();env[2].request.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):execute(ready)
    out=result(p.get_quality_transaction_plan(ready['plan_id']))
    assert out['state']=='unknown' and out['outcome']['interrupted']


def test_create_lot_body_matches_live_schema(env):
    import jsonschema
    schemas=json.loads(Path('docs/quality-fixtures/contracts.json').read_text(encoding='utf-8'))['Shopfloor/CreateSamplingLot']['components']['schemas']
    ready=create_lot()
    jsonschema.Draft7Validator({'components':{'schemas':schemas},'$ref':'#/components/schemas/CreateSamplingLot'}).validate(ready['request_preview']['body'])


def test_mcp_lot_prepare_confirm_execute_and_readback_are_not_automatic_success(env):
    async def scenario():
        async with Client(mcp) as client:
            async def call(name,args):
                out=await client.call_tool(name,args);assert not out.is_error;return json.loads(out.content[0].text)
            ready=await call('prepare_sampling_lot_create',{'lot_name':'Lot','product_name':'Product','product_revision':'A','quantity':'10.5','uom_name':'Each'})
            keys={k:ready[k] for k in ('plan_id','plan_digest')}
            assert 'CreateSamplingLot' in plan_preview([{'function':{'name':'execute_quality_transaction','arguments':json.dumps(keys)}}])
            assert (await call('execute_quality_transaction',keys))['state']=='unknown'
            out=await call('inspect_quality_transaction',keys)
            assert out['current_lot']['found'] is False and out['verified_success'] is False
    asyncio.run(scenario());env[2].request.assert_awaited_once()


def test_update_non_ror_version_pins_query_revision_as_well_as_target_id(env):
    env[0]['rows']['SampleDataPoints'][0].update(Revision='B',IsRevOfRcd=False)
    ready=point('update','Length','B',changes={'description':'this version'})
    assert ready['request_preview']['params']=={'revision':'B'}
    assert execute(ready)['state']=='unknown'
    assert env[2].request.await_args.kwargs['params']=={'revision':'B'}
