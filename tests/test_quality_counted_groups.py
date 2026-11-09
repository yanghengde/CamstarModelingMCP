import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tests.test_quality_inspection import setup, definition, echo
from tests.test_quality_lot_execution import env as lot_env
from tools import mcp, quality_inspection as i, quality_lot_execution as e, quality_plans as p, quality_history as h


SECOND='0000000000000901'
def result(coro): return json.loads(asyncio.run(coro))
def groups(): return [dict(samples_collected=3,rejects=1,reject_reason_name='Crack'),
                      dict(samples_collected=2,rejects=1,reject_reason_name='Scratch'),dict(samples_collected=1,rejects=0)]


def target(request,family):
    fixture=request.getfixturevalue('setup' if family=='container' else 'lot_env')
    fixture[0]['definition']['reject_reasons'].append({'name':'Scratch','instanceId':SECOND})
    def prepare(**kwargs):
        if family=='container': return result(i.prepare_container_inspection('C','Visual','A',**kwargs))
        return result(e.prepare_lot_inspection('L','C','Visual','A',**kwargs))
    return fixture,prepare


@pytest.mark.parametrize('family',['container','lot'])
def test_explicit_counted_groups_keep_counts_reasons_and_single_submission(request,family):
    env,prepare=target(request,family)
    ready=prepare(counted_groups=groups())
    assert ready['state']=='ready' and ready['static_and_echo_verified']
    body=ready['request_preview']['body'];rows=body['serviceDetails']
    assert [r['totalSamplesCollected'] for r in rows]==[3,2,1]
    assert [r['rejects'] for r in rows]==[1,1,0]
    assert rows[1]['samplingDataPointDetails']==[dict(dataValue='1',passed=False,rejectReason={'name':'Scratch','instanceId':SECOND})]
    assert 'samplingDataPointDetails' not in rows[2] and all('countedRejectReason' not in r for r in rows)
    assert ready['server_context']['samples_this_submission']==6 and ready['server_context']['rejects_this_submission']==2
    assert ready['server_context']['counted_groups_this_submission']==3
    assert not ready['server_context']['sample_partition_verified'] and not ready['server_context']['counted_group_accumulation_verified']
    assert result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))['state']=='unknown'
    env[2].request.assert_awaited_once()
    assert len(env[2].request.await_args.kwargs['json']['serviceDetails'])==3
    assert 'error' in result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))


@pytest.mark.parametrize('family',['container','lot'])
@pytest.mark.parametrize('mode',['duplicate_reason','duplicate_passing','sum_over_remaining','sum_over_100','missing_reason',
    'unknown_reason','zero_with_reason','rejects_over_count','empty','too_many','root_count','root_rejects','root_reason','measurements'])
def test_invalid_group_counts_scope_and_mixed_modes_are_rejected(request,family,mode):
    env,prepare=target(request,family);values=groups();extra={}
    if mode=='duplicate_reason':values[1]['reject_reason_name']='Crack'
    elif mode=='duplicate_passing':values.append(dict(samples_collected=1,rejects=0))
    elif mode=='sum_over_remaining':values[0]['samples_collected']=6
    elif mode=='sum_over_100':values[0]['samples_collected']=100
    elif mode=='missing_reason':del values[0]['reject_reason_name']
    elif mode=='unknown_reason':values[0]['reject_reason_name']='Invented'
    elif mode=='zero_with_reason':values[2]['reject_reason_name']='Crack'
    elif mode=='rejects_over_count':values[0]['rejects']=4
    elif mode=='empty':values=[]
    elif mode=='too_many':values=values*7
    elif mode=='root_count':extra['samples_collected']=6
    elif mode=='root_rejects':extra['rejects']=2
    elif mode=='root_reason':extra['reject_reason_name']='Crack'
    else:extra['samples']=[{'points':[{'data_point_name':'Length','value':'1'}]}]
    assert 'error' in prepare(counted_groups=values,**extra)
    env[1].assert_not_awaited();env[2].request.assert_not_awaited()


@pytest.mark.parametrize('family',['container','lot'])
def test_measured_tests_do_not_accept_counted_groups(request,family):
    env,prepare=target(request,family);env[0]['definition']['test']['SampleType']=2
    assert 'error' in prepare(counted_groups=groups())
    env[1].assert_not_awaited()


@pytest.mark.parametrize('family',['container','lot'])
def test_extra_or_changed_canonical_reason_child_fails_echo(request,family):
    env,prepare=target(request,family);original=env[1].side_effect
    async def corrupt(*a,**kw):
        d=json.loads(await original(*a,**kw));d['data']['ServiceDetails'][0]['SamplingDataPointDetails'][0]['RejectReason']['InstanceId']=SECOND
        return json.dumps(d)
    env[1].side_effect=corrupt
    assert 'error' in prepare(counted_groups=groups())
    env[2].request.assert_not_awaited()


@pytest.mark.parametrize('family',['container','lot'])
def test_zero_reject_group_cannot_acquire_unrequested_child(request,family):
    env,prepare=target(request,family);original=env[1].side_effect
    async def corrupt(*a,**kw):
        d=json.loads(await original(*a,**kw));d['data']['ServiceDetails'][-1]['SamplingDataPointDetails']=[{'DataValue':'1'}]
        return json.dumps(d)
    env[1].side_effect=corrupt
    assert 'error' in prepare(counted_groups=groups())


@pytest.mark.parametrize('family',['container','lot'])
def test_live_contract_does_not_require_unpublished_cdo_type_field(request,family):
    env,prepare=target(request,family);original=env[1].side_effect
    async def omit(*a,**kw):
        d=json.loads(await original(*a,**kw))
        for row in d['data']['ServiceDetails']:
            row.pop('CDOTypeId',None)
            for point in row.get('SamplingDataPointDetails',[]):point.pop('CDOTypeId',None)
        return json.dumps(d)
    env[1].side_effect=omit
    ready=prepare(counted_groups=groups())
    assert ready['state']=='ready' and not ready['server_context']['response_dynamic_type_verified']
    params=env[1].await_args.kwargs['params']
    assert 'TotalSamplesCollected,Rejects,Rejected;' in params['$expand']
    assert 'SampleDataPoint,DataValue,Passed,RejectReason' in params['$expand']
    assert 'CDOTypeId' not in params['$expand'] and 'CountedRejectReason' not in params['$expand']


@pytest.mark.parametrize('family',['container','lot'])
@pytest.mark.parametrize('comments',['😀'*91,'x'*181,'bad\x00'])
def test_comments_use_utf16_storage_limit_and_preserve_marker(request,family,comments):
    env,prepare=target(request,family)
    assert 'error' in prepare(counted_groups=groups(),comments=comments)
    env[1].assert_not_awaited();env[2].request.assert_not_awaited()


def test_counted_group_wire_matches_live_openapi(setup):
    import jsonschema
    setup[0]['definition']['reject_reasons'].append({'name':'Scratch','instanceId':SECOND})
    ready=result(i.prepare_container_inspection('C','Visual','A',counted_groups=groups()))
    schemas=json.loads(Path('tests/fixtures/inspection_input_schema.json').read_text(encoding='utf-8'))['components']['schemas']
    jsonschema.Draft7Validator({'components':{'schemas':schemas},'$ref':'#/components/schemas/CollectSamplingData'}).validate(ready['request_preview']['body'])


@pytest.mark.parametrize('family',['container','lot'])
def test_repeated_reason_identity_under_two_names_is_rejected(request,family):
    env,prepare=target(request,family)
    env[0]['definition']['reject_reasons'][-1]['instanceId']=env[0]['definition']['reject_reasons'][0]['instanceId']
    assert 'error' in prepare(counted_groups=groups())
    env[1].assert_not_awaited()


@pytest.mark.parametrize('family',['container','lot'])
def test_changed_child_reject_count_cannot_match_parent(request,family):
    env,prepare=target(request,family);original=env[1].side_effect
    async def corrupt(*a,**kw):
        d=json.loads(await original(*a,**kw));d['data']['ServiceDetails'][0]['SamplingDataPointDetails'][0]['DataValue']='2'
        return json.dumps(d)
    env[1].side_effect=corrupt
    assert 'error' in prepare(counted_groups=groups())


@pytest.mark.parametrize('case',['canonical','passing'])
def test_projected_child_without_cdo_field_is_readable(monkeypatch,case):
    # Portable parser unit input. Actual nonempty history captures are replayed
    # by test_quality_inspection_native; an input echo is never final evidence.
    record=projection(case)
    body=copy.deepcopy(record['body'])
    for detail in body['serviceDetails']:
        if detail.get('samplingDataPointDetails')==[]:detail.pop('samplingDataPointDetails')
    monkeypatch.setattr(i,'request',AsyncMock(return_value=json.dumps(record['response'])))
    preview=asyncio.run(i.inspection_preview(body))
    assert preview['verified_input_echo']==body and not preview['response_dynamic_type_verified']
    assert all(v is None for v in preview['task_context'].values())


def test_no_task_projection_does_not_approve_a_real_plan(setup,monkeypatch):
    record=projection('canonical')
    monkeypatch.setattr(i,'request',AsyncMock(return_value=json.dumps(record['response'])))
    out=result(i.prepare_container_inspection('C','Visual','A',samples_collected=3,rejects=1,reject_reason_name='Crack'))
    assert 'error' in out
    setup[2].request.assert_not_awaited()


def projection(case):
    test={'name':'Visual','revision':'A','instanceId':'0000000000000201'}
    line=dict(sampleTest=test,totalSamplesCollected=3,rejects=1 if case=='canonical'else 0,rejected=case=='canonical',samplingDataPointDetails=[])
    if case=='canonical':line['samplingDataPointDetails']=[dict(dataValue='1',passed=False,rejectReason={'name':'Crack','instanceId':'0000000000000501'})]
    body=dict(sampleTest=test,serviceDetails=[line])
    return dict(body=body,response={'data':echo(body)})


def test_previous_shortcut_plan_is_stale_instead_of_replayed(setup):
    ready=result(i.prepare_container_inspection('C','Visual','A',samples_collected=3,rejects=1,reject_reason_name='Crack'))
    with p.db_connect() as db:
        snap=json.loads(db.execute('SELECT payload FROM plans WHERE id=?',(ready['plan_id'],)).fetchone()[0])
        row=snap['body']['serviceDetails'][0]
        row['countedRejectReason']=row.pop('samplingDataPointDetails')[0]['rejectReason']
        snap['preview']['verified_input_echo']=copy.deepcopy(snap['body'])
        snap['preview']['params']['$expand']='ServiceDetails($expand=SamplingDataPointDetails)'
        old_digest=p.digest(snap)
        db.execute('UPDATE plans SET digest=?,payload=? WHERE id=?',(old_digest,json.dumps(snap),ready['plan_id']))
    out=result(p.execute_quality_transaction(ready['plan_id'],old_digest))
    assert 'error' in out
    setup[2].request.assert_not_awaited()


def test_group_history_comparison_does_not_claim_quantity_reason_or_success():
    d=definition();d['reject_reasons'].append({'name':'Scratch','instanceId':SECOND})
    details,_,_,_=i.collection_details(d,{'counted_groups':groups()})
    evidence={'samples':[{'sample_history':{'SampleTest':{'InstanceID':d['test']['InstanceID']},'Rejected':row['rejected'],'Rejects':row['rejects']},'points':[]} for row in details]}
    compared=h.compare_sampling({'body':{'serviceDetails':details},'definition':d},evidence)
    assert compared['comparison']=='partial_match' and compared['requested_sample_units']==6 and compared['requested_rejects']==2
    assert 'counted service groups' in compared['detail_row_semantics'] and compared['unverified_checks']


@pytest.mark.parametrize('family',['container','lot'])
def test_mcp_nested_schema_and_single_attempt_lifecycle(request,family):
    env,prepare=target(request,family)
    tool='prepare_container_inspection' if family=='container' else 'prepare_lot_inspection'
    args={'container_name':'C','sample_test_name':'Visual','sample_test_revision':'A','counted_groups':groups()}
    if family=='lot':args['lot_name']='L'
    async def run():
        async with Client(mcp) as client:
            schemas={t.name:t.input_schema for t in await client.list_tools()}
            assert schemas[tool]['additionalProperties'] is False
            for value in (True,'3',1.5):
                bad=copy.deepcopy(args);bad['counted_groups'][0]['samples_collected']=value
                response=await client.call_tool(tool,bad,raise_on_error=False);assert response.is_error
            bad=copy.deepcopy(args);bad['counted_groups'][0]['passed']=True
            assert (await client.call_tool(tool,bad,raise_on_error=False)).is_error
            out=await client.call_tool(tool,args);ready=json.loads(out.content[0].text)
            assert ready['state']=='ready'
            keys={k:ready[k] for k in ('plan_id','plan_digest')}
            out=await client.call_tool('execute_quality_transaction',keys)
            assert json.loads(out.content[0].text)['state']=='unknown'
    asyncio.run(run());env[2].request.assert_awaited_once()


def test_grouped_lot_followup_routes_quality_tools_without_ordinary_collection():
    from agent.tool_routing import select_tools, ROUTING_INSTRUCTION
    from core.tool_catalog import TOOL_POLICIES
    tools=[{'function':{'name':name}} for name in TOOL_POLICIES]
    messages=[{'role':'user','content':'抽样批L的检验结果分为三组计数'},
              {'role':'user','content':'请提交这三组检验结果'}]
    selected={t['function']['name'] for t in select_tools(tools,messages)}
    assert {'prepare_lot_inspection','execute_quality_transaction'}<=selected
    assert 'collect_data' not in selected and 'counted_groups' in ROUTING_INSTRUCTION
