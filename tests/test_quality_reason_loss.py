import asyncio
import json
import pytest
from fastmcp import Client
from tools import mcp, quality_inspection_effects as effects, quality_plans as plans
from tests.test_quality_counted_loss import env as counted_env, prepare
from tests.test_quality_inspection_effects import env as effects_env, loss_env, execute, snap
from tests.test_quality_quantity_loss import REASON, GROUP, ref, key


@pytest.fixture
def env(counted_env):
    state = counted_env
    state['definition']['test'].update(DecreaseBySampleSizeReason=None, DecreaseByRejectCountReason=None,
        ScrapCountedRejectsByReason=True, AQLRejectReasons=ref('Losses',GROUP))
    state['definition']['reject_reasons'] = [{'name':'SCRAP','instanceId':REASON}]
    return state


def plan(**args):
    return prepare(rejects=1, reject_reason_name='SCRAP', **args)


def test_reason_loss_is_one_native_parent_with_original_reason_and_quantity(env):
    ready = plan()
    assert ready.get('state') == 'ready', ready
    context = ready['server_context']['native_effects']
    assert context['mode'] == 'counted_reason_loss'
    assert context['automatic_quantity_loss']['quantity'] == '1'
    assert context['automatic_quantity_loss']['projected_remaining_quantity'] == '9'
    assert context['automatic_quantity_loss']['reason'] == {'name':'SCRAP','instanceId':REASON}
    assert ready['request_preview']['body']['samplesTested'] == 3
    assert execute(ready)['state'] == 'unknown'
    env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/api/CollectSamplingData')
    assert all(call.kwargs['params']['execute'] == 'false' for call in env['children'].await_args_list)
    assert 'error' in execute(ready)


@pytest.mark.parametrize('case', ['zero_rejects','missing_permission','ambiguous','multiple_rows','unknown_reason','group_changed','stale','tampered'])
def test_reason_loss_rejects_ambiguous_or_incomplete_native_mapping(env, case):
    args = {}
    if case == 'zero_rejects':
        out = prepare(rejects=0)
    else:
        if case == 'missing_permission':args['allow_automatic_quantity_loss'] = False
        elif case == 'ambiguous':env['definition']['test']['DecreaseBySampleSizeReason'] = ref('SCRAP',REASON)
        elif case == 'unknown_reason':env['group']['ResolvedEntries'] = []
        elif case == 'group_changed':env['definition']['reject_reasons'].append({'name':'Other','instanceId':GROUP})
        elif case == 'multiple_rows':
            out = prepare(samples_collected=None, rejects=None, reject_reason_name=None, counted_groups=[
                {'samples_collected':2,'rejects':1,'reject_reason_name':'SCRAP'},
                {'samples_collected':1,'rejects':0}])
        if case != 'multiple_rows':out = plan(**args)
    if case == 'stale':
        assert out['state'] == 'ready',out
        env['group']['ResolvedEntries'] = []
        assert 'error' in execute(out)
    elif case == 'tampered':
        assert out['state'] == 'ready',out
        saved = snap(out)
        saved['inspection_effects']['context']['automatic_quantity_loss']['reason']['instanceId'] = GROUP
        with pytest.raises(ValueError):
            plans.transport(saved)
    else:
        assert 'error' in out,out
    env['client'].request.assert_not_awaited()


def test_reason_loss_combines_with_failure_full_inspection(env):
    env['task']['InspectAllQtyOnFailure'] = True
    ready = plan(allow_full_inspection=True)
    assert ready.get('state') == 'ready',ready
    assert ready['server_context']['native_effects']['full_inspection']['conditional_required_samples_after_failure'] == 9
    assert execute(ready)['state'] == 'unknown'
    env['client'].request.assert_awaited_once()


def test_reason_loss_roundtrips_through_mcp(env):
    async def flow():
        async with Client(mcp) as client:
            reply = await client.call_tool('prepare_container_inspection',dict(
                container_name='C',sample_test_name='Visual',sample_test_revision='A',
                samples_collected=3,rejects=1,reject_reason_name='SCRAP',allow_automatic_quantity_loss=True))
            out = json.loads(reply.content[0].text)
            assert out.get('state') == 'ready',out
    asyncio.run(flow())
    env['client'].request.assert_not_awaited()


def test_reason_loss_can_close_only_with_explicit_zero_permission(env):
    env['row']['Qty']='3'
    ready=prepare(samples_collected=3,rejects=3,reject_reason_name='SCRAP',allow_zero_quantity_loss=True)
    assert ready.get('state')=='ready',ready
    projected=ready['server_context']['native_effects']['automatic_quantity_loss']
    assert projected['quantity']=='3' and projected['projected_remaining_quantity']=='0'
    assert projected['projected_close']
    assert execute(ready)['state']=='unknown'
    env['client'].request.assert_awaited_once()


def test_reason_loss_context_keeps_loss_reason_for_confirmation(env):
    from agent.approval_display import business_preview
    ready=plan()
    technical=json.dumps({'server_context':ready['server_context']},ensure_ascii=False)
    text=business_preview([{'function':{'name':'execute_quality_transaction','arguments':'{}'}}],technical)
    assert '本次检验组拒收原因及拒收数量' in text and 'SCRAP' in text
    assert '预计剩余数量：9' in text


@pytest.fixture
def multiple(env):
    other=key(8001)
    env['models']['LossReasons'].append({'Name':'OTHER','InstanceID':other})
    env['counters'][other]=1
    env['group']['ResolvedEntries'].append(ref('OTHER',other))
    env['definition']['reject_reasons'].append({'name':'OTHER','instanceId':other})
    env['reason_inputs']=[{'reject_reason_name':'SCRAP','rejects':1},{'reject_reason_name':'OTHER','rejects':2}]
    return env


def multi_plan(env, **args):
    return prepare(samples_collected=4,rejects=None,reject_reason_name=None,
                   counted_reject_reasons=env['reason_inputs'],**args)


def test_multiple_reasons_keep_one_cohort_and_native_multi_detail_preview(multiple):
    ready=multi_plan(multiple)
    assert ready.get('state')=='ready',ready
    body=ready['request_preview']['body']
    assert ready['server_context']['counted_reject_reason_count']==2
    assert ready['server_context']['sample_partition_verified'] is False
    assert body['samplesTested']==4 and len(body['serviceDetails'])==1
    assert body['serviceDetails'][0]['rejects']==3
    assert [point['dataValue'] for point in body['serviceDetails'][0]['samplingDataPointDetails']]==['1','2']
    projected=ready['server_context']['native_effects']['automatic_quantity_loss']
    assert projected['quantity']=='3' and projected['projected_remaining_quantity']=='7'
    assert projected['reason'] is None
    assert [line['quantity'] for line in projected['reason_breakdown']]==[1,2]
    child=multiple['children'].await_args.kwargs['body']['serviceDetails']
    assert [line['qty'] for line in child]==[1,2]
    assert execute(ready)['state']=='unknown'
    multiple['client'].request.assert_awaited_once()
    from agent.approval_display import business_preview
    text=business_preview([{'function':{'name':'execute_quality_transaction','arguments':'{}'}}],
                          json.dumps({'server_context':ready['server_context']}))
    assert '按拒收原因扣量明细' in text and 'SCRAP' in text and 'OTHER' in text


@pytest.mark.parametrize('case',['duplicate','too_many','overlap','root_count','groups','false_permission','other_mode',
                                  'missing_child','wrong_child','negative','fraction','stale','tampered'])
def test_multiple_reasons_fail_closed_before_any_business_write(multiple,case):
    args={}
    if case=='duplicate':multiple['reason_inputs'][1]['reject_reason_name']='SCRAP'
    elif case=='too_many':multiple['reason_inputs']*=11
    elif case=='overlap':multiple['reason_inputs'][1]['rejects']=4
    elif case=='negative':multiple['reason_inputs'][1]['rejects']=-1
    elif case=='fraction':multiple['reason_inputs'][1]['rejects']=1.5
    elif case=='root_count':
        out=prepare(samples_collected=4,rejects=3,counted_reject_reasons=multiple['reason_inputs'])
    elif case=='groups':args['counted_groups']=[{'samples_collected':4,'rejects':0}]
    elif case=='false_permission':args['allow_automatic_quantity_loss']=False
    elif case=='other_mode':
        multiple['definition']['test'].update(ScrapCountedRejectsByReason=False,DecreaseBySampleSizeReason=ref('SCRAP',REASON))
    elif case=='missing_child':multiple['child_hook']=lambda data:data['ServiceDetails'].pop()
    elif case=='wrong_child':multiple['child_hook']=lambda data:data['ServiceDetails'][1].update(Qty='1')
    if case!='root_count':out=multi_plan(multiple,**args)
    if case=='stale':
        assert out['state']=='ready',out
        multiple['group']['ResolvedEntries'].pop()
        assert 'error' in execute(out)
    elif case=='tampered':
        assert out['state']=='ready',out
        saved=snap(out)
        saved['inspection_effects']['context']['automatic_quantity_loss']['reason_breakdown'][1]['quantity']=1
        with pytest.raises(ValueError):
            plans.transport(saved)
    else:assert 'error' in out,out
    multiple['client'].request.assert_not_awaited()


def test_multiple_reasons_actual_mcp_parameters(multiple):
    async def flow():
        async with Client(mcp) as client:
            response=await client.call_tool('prepare_container_inspection',dict(
                container_name='C',sample_test_name='Visual',sample_test_revision='A',
                samples_collected=4,counted_reject_reasons=multiple['reason_inputs'],allow_automatic_quantity_loss=True))
            out=json.loads(response.content[0].text)
            assert out.get('state')=='ready',out
    asyncio.run(flow())
