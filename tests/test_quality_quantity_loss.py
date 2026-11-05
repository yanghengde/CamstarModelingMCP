"""LossDetails type/quantity semantics and non-executable SamplingLot scrap assessment."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import mcp,get_tool_func,process_context
from tools import quality_quantity_loss as r,quality_plans as p,quality_history as h
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import ROUTING_INSTRUCTION,select_tools
from agent.safety import evaluate_mutations,MutationCounts,plan_preview
from tests.test_quality_lot_execution import env as lot_env


def key(n):return f'{n:016x}'
CID,STATUS,PROD,SPEC,STEP,OP,REASON,GROUP,UOM,WF=[key(n) for n in range(3001,3011)]
def ref(name,iid,revision=None):return dict(Name=name,InstanceId=iid,**({'Revision':revision} if revision else {}))
def result(coro):return json.loads(asyncio.run(coro))
def echo(value):
    if isinstance(value,dict):return {k[0].upper()+k[1:]:echo(v) for k,v in value.items()}
    if isinstance(value,list):return [echo(v) for v in value]
    if type(value) in (bool,int,float):return str(value).lower()
    return value


@pytest.fixture
def env(monkeypatch,tmp_path):
    row=dict(ContainerId=CID,ContainerName='C',CDOTypeId='1040',Status='1',CurrentHoldCount='0',ChangeCount='1',ProductId=PROD,
        SamplingLotId=None,ParentContainerId=None,ChildCount='0',Qty='10',Qty2=None,UOMId=UOM,UOM2Id=None,CurrentStatusId=STATUS,
        StatusRecordId=STATUS,CarrierId=None,InRework='false',SpecId=SPEC,WorkflowStepId=STEP)
    spec=dict(Name='Spec',InstanceID=SPEC,Revision='A',IsRevOfRcd=True,Status=1,Operation=ref('Op',OP))
    operation=dict(Name='Op',InstanceID=OP,LossReasons=ref('Losses',GROUP))
    reason=dict(Name='SCRAP',InstanceID=REASON)
    group=dict(Name='Losses',InstanceID=GROUP,ResolvedEntries=[ref('SCRAP',REASON)])
    unit=dict(Name='EA',InstanceID=UOM)
    state=dict(row=row,spec=spec,operation=operation,reason=reason,group=group,unit=unit,
        models={'Specs':[spec],'Operations':[operation],'LossReasons':[reason],'LossReasonGroups':[group],'UOMs':[unit]},
        counters={n:1 for n in (SPEC,OP,REASON,GROUP,UOM)},model_hook=None,preview_hook=None,status_hook=None)
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'quality.sqlite3')
    async def identity(name,iid=None):
        if name!=state['row']['ContainerName'] or iid and iid!=CID:raise ValueError('identity mismatch')
        status=dict(Container=ref(name,CID),Spec=ref('Spec',SPEC,'A'),Step=ref('Inspect',STEP),Operation=ref('Op',OP),
                    Workflow=ref('Flow',WF,'A'),UOM=ref('EA',UOM),Qty=state['row']['Qty'],InRework=False,InProcess=True)
        if state['status_hook']:state['status_hook'](status)
        return dict(name=name,instanceId=CID),status
    monkeypatch.setattr(r.q,'container_identity',AsyncMock(side_effect=identity))
    async def sql(query,maximum):
        if 'FROM Container c INNER JOIN CurrentStatus' in query:
            return [copy.deepcopy(state['row'])] if state['row']['ContainerId']==CID else []
        for _,(table,column,_) in r.ASSETS.items():
            if ' FROM '+table+' WHERE' in query:
                iid=query.split("='",1)[1].split("'",1)[0]
                return [{column:iid,'ChangeCount':str(state['counters'][iid])}]
        raise AssertionError(query)
    monkeypatch.setattr(r.q,'sql_rows',AsyncMock(side_effect=sql))
    async def read(method,path,body=None,params=None,**kw):
        assert method=='GET' and body is None
        if state['model_hook']:state['model_hook'](path,params)
        entity,*parts=path.removeprefix('/api/').split('/')
        rows=state['models'][entity]
        if parts:
            found=[row for row in rows if row['InstanceID']==parts[0]]
            return json.dumps(copy.deepcopy(found[0]) if len(found)==1 else {'error':'missing'})
        expression=params['$filter'];found=[row for row in rows if "Name eq '"+row['Name']+"'" in expression]
        return json.dumps({'value':copy.deepcopy(found[:2])})
    reads=AsyncMock(side_effect=read);monkeypatch.setattr(process_context,'request',reads)
    async def probe(method,path,**kw):
        assert method=='POST' and path=='/api/ChangeQty' and kw['params']['execute']=='false'
        data=echo(kw['body']);line=data['ServiceDetails'][0]
        line.update(CDOTypeId='4030',QtyMultiplier='-1',CountsAgainstProduction='true',Container=copy.deepcopy(data['Container']),
                    Qty=line.get('Qty','0'),Qty2=None,ChargeToStep=ref('Inspect',STEP),IsDeleted='false',
                    Parent=ref('Temporary',key(7000+len(reads.await_args_list))))
        if state['preview_hook']:state['preview_hook'](data)
        return json.dumps({'data':data})
    previews=AsyncMock(side_effect=probe);monkeypatch.setattr(r,'request',previews)
    client=SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200,text='{"CompletionMsg":"accepted"}')))
    monkeypatch.setattr(p,'get_client',lambda:client)
    state.update(reads=reads,previews=previews,client=client)
    return state


def prepare(**kw):
    args=dict(container_name='C',loss_reason_name='SCRAP',close_when_zero=False,adjust_throughput=False,loss_quantity='1.5')
    args.update(kw)
    return result(r.prepare_container_quantity_loss(**args))
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))
def snapshot(ready):return result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']


def test_partial_loss_is_typed_negative_single_submission_not_cumulative(env):
    ready=prepare(comments='cracked material');assert ready.get('state')=='ready',ready
    body=ready['request_preview']['body'];detail=body['serviceDetails'][0];context=ready['server_context']
    assert detail['@odata.type']=='#shopfloor.LossDetails' and detail['changeQtyType']==2 and detail['qty']==1.5
    assert not detail['recordAllQty'] and not detail['adjustThruput']
    assert context['current_quantity']=='10' and context['loss_quantity']=='1.5' and context['projected_remaining_quantity']=='8.5'
    assert context['unit']==dict(name='EA',instanceId=UOM) and not context['projection_is_execution_result']
    assert not context['quantity_effects_verified'] and context['quality_verdict'] is None
    assert body['closeWhenZero'] is False and body['generateRollups'] is False and body['disassociateWhenClosed'] is False
    snap=snapshot(ready);assert len(snap['dependencies'])==5
    assert 'runtime:container:id:'+CID in snap['resource_writes']
    assert execute(ready)['state']=='unknown';env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/api/ChangeQty')
    assert env['client'].request.await_args.kwargs['json']==body


@pytest.mark.parametrize('close,adjust',[(True,True),(False,False)])
def test_full_quantity_requires_explicit_all_mode_and_closing_policy(env,close,adjust):
    ready=prepare(loss_quantity=None,record_all_quantity=True,close_when_zero=close,adjust_throughput=adjust)
    assert ready.get('state')=='ready',ready
    body=ready['request_preview']['body'];line=body['serviceDetails'][0]
    assert line['recordAllQty'] is True and 'qty' not in line and 'qty2' not in line
    assert body['closeWhenZero']==close and line['adjustThruput']==adjust
    assert ready['server_context']['projected_remaining_quantity']=='0' and ready['server_context']['projected_close']==close
    assert not ready['server_context']['quantity_effects_verified']


@pytest.mark.parametrize('kwargs',[{'loss_quantity':None},{'record_all_quantity':True},{'loss_quantity':'10'},
    {'loss_quantity':'11'},{'loss_quantity':'0'},{'loss_quantity':'-1'},{'loss_quantity':'NaN'},
    {'loss_quantity':'1+1'},{'loss_quantity':'1e-100'},{'loss_quantity':2},{'loss_quantity':'1.0000000000000001'},
    {'close_when_zero':None},{'adjust_throughput':'false'},{'record_all_quantity':1}])
def test_quantity_mode_and_numeric_options_are_explicit(env,kwargs):
    assert 'error' in prepare(**kwargs);env['previews'].assert_not_awaited();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('CDOTypeId','7939'),('CDOTypeId','9999'),('Status','2'),('CurrentHoldCount','1'),
    ('CurrentHoldCount',None),('InRework','true'),('InRework',None),('SamplingLotId',key(999)),
    ('ParentContainerId',key(999)),('ChildCount','1'),('CarrierId',key(999)),('Qty','0'),('Qty2','1'),
    ('UOM2Id',key(999)),('UOMId',''),('ProductId',''),('StatusRecordId',key(999)),('SpecId',key(999)),
    ('WorkflowStepId',key(999))])
def test_restricted_container_scope_blocks_before_native_preview(env,field,value):
    env['row'][field]=value
    assert 'error' in prepare();env['previews'].assert_not_awaited()


def test_missing_persistent_fields_do_not_become_absence_or_zero(env):
    del env['row']['CarrierId']
    assert 'error' in prepare();env['previews'].assert_not_awaited()


@pytest.mark.parametrize('target,field,value',[('spec','Status',2),('spec','Operation',ref('Other',REASON)),
    ('operation','LossReasons',None),('group','ResolvedEntries',[]),
    ('group','ResolvedEntries',[ref('SCRAP',key(999))]),('group','ResolvedEntries',[ref('SCRAP',REASON),ref('SCRAP',REASON)]),
    ('group','ResolvedEntries@odata.nextLink','more'),('unit','Name','')])
def test_reason_is_current_operation_loss_reason_and_unit_is_known(env,target,field,value):
    env[target][field]=value
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('CDOTypeId','4010'),('QtyMultiplier','1'),('CountsAgainstProduction','false'),
    ('RecordAllQty','true'),('AdjustThruput','true'),('Qty','2'),('Qty2','1'),('ReasonCode',ref('Other',REASON)),
    ('Container',ref('Other',CID)),('ChargeToStep',ref('Other',key(999))),('IsDeleted','true')])
def test_generic_type_or_wrong_loss_echo_cannot_prepare_plan(env,field,value):
    env['preview_hook']=lambda data:data['ServiceDetails'][0].update({field:value})
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('ServiceDetails',[]),('CloseWhenZero','true'),('GenerateRollups','true'),
    ('RecordAllQtySummaryOnly','true'),('Comments','wrong'),('Container',ref('Other',CID)),('error','failed')])
def test_full_root_echo_and_exactly_one_detail_required(env,field,value):
    env['preview_hook']=lambda data:data.update({field:value})
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('target',['row','status','reason','group','spec','unit','counter'])
def test_pre_execution_rebuild_binds_original_quantity_route_and_model(env,target):
    ready=prepare();assert ready.get('state')=='ready',ready
    if target=='row':env['row']['Qty']='9'
    elif target=='status':env['status_hook']=lambda status:status.update(InProcess=False)
    elif target=='counter':env['counters'][GROUP]+=1
    else:env[target]['Description']='changed'
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_mid_prepare_quantity_change_is_rejected(env):
    env['preview_hook']=lambda data:env['row'].update(Qty='9')
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['service','kind','type','code','reason','amount','mode','close','adjust','extra','marker'])
def test_transport_accepts_only_reviewed_loss_not_bonus_or_sell(env,mode):
    snap=snapshot(prepare());line=snap['body']['serviceDetails'][0]
    if mode=='service':snap['service']='ExecuteSamplingLot'
    if mode=='kind':snap['kind']='lot_completion'
    if mode=='type':line['@odata.type']='#shopfloor.ChangeQtyDetails'
    if mode=='code':line['changeQtyType']=5
    if mode=='reason':line['reasonCode']['instanceId']=key(999)
    if mode=='amount':line['qty']=2
    if mode=='mode':line['recordAllQty']=True
    if mode=='close':snap['body']['closeWhenZero']=True
    if mode=='adjust':line['adjustThruput']=True
    if mode=='extra':line['qtyMultiplier']=-1
    if mode=='marker':snap['body']['comments']='unbound'
    with pytest.raises(ValueError):p.transport(snap)


def test_unknown_never_replays_loss_and_original_id_inspection_does_not_unlock(env, monkeypatch):
    first=prepare();second=prepare();assert execute(first)['state']=='unknown'
    assert 'error' in execute(first) and 'error' in execute(second)
    env['row'].update(Qty='8.5',ContainerName='Renamed')
    out=result(p.inspect_quality_transaction(first['plan_id'],first['plan_digest']))
    assert out['current_record']['ContainerId']==CID and out['current_record']['ContainerName']=='Renamed'
    assert not out['verified_success'] and not out['state_changed'] and not out['retry_allowed']
    async def empty_history(query, maximum):
        assert 'FROM HistoryMainline WHERE Comments=' in query
        return []
    monkeypatch.setattr(h.q,'sql_rows',empty_history)
    assert result(h.find_quality_execution_candidates(first['plan_id'],first['plan_digest']))['rows'] == []
    assert result(p.get_quality_transaction_plan(first['plan_id']))['state']=='unknown'
    env['client'].request.assert_awaited_once()


@pytest.mark.parametrize('kind',['ncr','lot_rework','lot_inspection','quantity_loss'])
def test_unknown_loss_conflicts_with_related_container_quality_plans(env,kind):
    ready=prepare();snap=snapshot(ready)
    other=dict(kind=kind,environment=p.environment(),scope_key='different',resource_writes=['runtime:container:id:'+CID])
    assert p.conflicts(snap,other)


@pytest.mark.parametrize('value',['\0','😀'*91,True])
def test_comments_are_bounded_before_reads(env,value):
    assert 'error' in prepare(comments=value);env['reads'].assert_not_awaited();env['previews'].assert_not_awaited()


def test_mcp_closed_schema_confirmation_and_single_loss_lifecycle(env):
    name='prepare_container_quantity_loss';assert get_tool_func(name) and TOOL_POLICIES[name].effect=='read'
    assert name in ROUTING_INSTRUCTION
    selected=select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[{'role':'user','content':'容器C损失1.5件，原因SCRAP，保持开启，不调整已处理数量'}])
    assert name in {v['function']['name'] for v in selected}
    assert 'change_container_quantity' not in {v['function']['name'] for v in selected}
    async def run():
        async with Client(mcp) as client:
            schema=next(v.input_schema for v in await client.list_tools() if v.name==name)
            assert schema['additionalProperties'] is False
            assert schema['required']==['container_name','loss_reason_name','close_when_zero','adjust_throughput']
            raw=await client.call_tool(name,dict(container_name='C',loss_reason_name='SCRAP',close_when_zero=False,adjust_throughput=False,loss_quantity='1.5'))
            ready=json.loads(raw.content[0].text);assert ready.get('state')=='ready',ready
            args={k:ready[k] for k in ('plan_id','plan_digest')}
            calls=[{'function':{'name':'execute_quality_transaction','arguments':json.dumps(args)}}]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval
            text=plan_preview(calls);assert 'ChangeQty' in text and 'LossDetails' in text and '1.5' in text
            sent=await client.call_tool('execute_quality_transaction',args)
            assert json.loads(sent.content[0].text)['state']=='unknown'
            bad=await client.call_tool(name,dict(container_name='C',loss_reason_name='SCRAP',close_when_zero=False,adjust_throughput=False,loss_quantity='1.5',change_qty_type=5),raise_on_error=False)
            assert bad.is_error
    asyncio.run(run());env['client'].request.assert_awaited_once()


def test_lot_scrap_assessment_is_explicitly_blocked_without_plan_or_write(lot_env):
    out=result(r.get_sampling_lot_scrap_readiness('L'))
    assert out['complete'] and not out['ready_for_execution'] and not out['plan_created'] and not out['mes_write_performed']
    assert out['native_batch_branch_observed'] and not out['native_batch_branch_runtime_verified']
    assert {v['code'] for v in out['blockers']}=={'explicit_loss_reason_required','native_defaults_review_required','deployed_batch_effects_unverified'}
    assert not out['automatic_fallback'] and not p.PLAN_DB.exists()
    lot_env[1].assert_not_awaited();lot_env[2].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('Status',2),('Qty','0')])
def test_native_batch_would_skip_members_is_exposed_not_executed(lot_env,field,value):
    lot_env[0]['members'][0][field]=value
    out=result(r.get_sampling_lot_scrap_readiness('L'))
    assert out['member_issues'][0]['code']=='native_branch_would_skip_member' and not out['ready_for_execution']


def test_batch_readiness_checks_full_membership_and_rejects_change(lot_env):
    original=lot_env[3].side_effect;count=0
    async def changed(query,maximum):
        nonlocal count
        if 'FROM Container WHERE SamplingLotId=' in query:
            count+=1
            if count==2:lot_env[0]['members'][0]['ChangeCount']=2
        return await original(query,maximum)
    lot_env[3].side_effect=changed
    out=result(r.get_sampling_lot_scrap_readiness('L'))
    assert not out['complete'] and not out['plan_created'] and not out['ready_for_execution']


def test_batch_scrap_registration_closed_schema_and_mcp_read_only(lot_env):
    name='get_sampling_lot_scrap_readiness'
    assert TOOL_POLICIES[name].effect=='read' and get_tool_func(name) and name in ROUTING_INSTRUCTION
    async def run():
        async with Client(mcp) as client:
            raw=await client.call_tool(name,dict(lot_name='L'));out=json.loads(raw.content[0].text)
            assert out['complete'] and not out['ready_for_execution']
            schema=next(v.input_schema for v in await client.list_tools() if v.name==name)
            assert schema['additionalProperties'] is False
    asyncio.run(run());lot_env[2].request.assert_not_awaited()
