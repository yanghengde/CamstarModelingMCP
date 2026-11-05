"""Native whole-lot rework: real plan/journal and readers, mocked MES boundaries."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import mcp, get_tool_func, process_context, quality_lot_rework as r
from tools import quality_plans as p, quality_lot_execution as e, quality_inspection as i, quality_history as h
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION
from agent.safety import evaluate_mutations, MutationCounts, plan_preview
from tests.test_quality_lot_execution import env as lot_env, LOT, CID, CID2, SPEC, PLAN, echo


def key(n): return f'{n:016x}'
WF, SRC, DST, PATH, TARGET, OP, GROUP, REASON = [key(n) for n in range(900, 908)]
def result(coro): return json.loads(asyncio.run(coro))
def reference(name, iid, revision=None):
    return dict(Name=name, InstanceId=iid, **({'Revision': revision} if revision else {}))


@pytest.fixture
def env(lot_env, monkeypatch):
    state, _, client, original_sql = lot_env
    state['tasks'][0].update(CompletedSamples=2, SamplesTested=2, Rejects=2, SamplesFailed=2, AllowedRejects=1)
    source = dict(Name='Inspect', InstanceID=SRC, StepType=1, Parent=reference('Flow', WF),
                  Spec=reference('Spec', SPEC, 'A'),
                  ReworkPaths=[dict(Name='Default', InstanceID=PATH, ToStep=reference('Repair', DST), ReEntryStep=None, **{'@odata.type':'#modeling.ReworkPathChanges'})],
                  ReworkPathSelectors=[])
    target = dict(Name='Repair', InstanceID=DST, StepType=1, Spec=reference('RepairSpec', TARGET, 'A'), Parent=reference('Flow', WF), ReworkPaths=[], ReworkPathSelectors=[])
    workflow = dict(Name='Flow', InstanceID=WF, Revision='A', IsRevOfRcd=True, Status=1, Steps=[source, target])
    spec = dict(Name='Spec', InstanceID=SPEC, Revision='A', IsRevOfRcd=True, Status=1, Operation=reference('Op', OP))
    target_spec = dict(Name='RepairSpec', InstanceID=TARGET, Revision='A', IsRevOfRcd=True, Status=1, AllowReworkTo=True)
    operation = dict(Name='Op', InstanceID=OP, ReworkReasons=reference('Reasons', GROUP))
    reason = dict(Name='RepairReason', InstanceID=REASON)
    group = dict(Name='Reasons', InstanceID=GROUP, ResolvedEntries=[reference('RepairReason', REASON)])
    models = dict(Workflows=[workflow], Specs=[spec, target_spec], Operations=[operation], ReworkReasons=[reason], ReworkReasonGroups=[group])
    state.update(models=models, workflow=workflow, source=source, target=target, spec=spec, target_spec=target_spec,
                 operation=operation, reason=reason, group=group, tokens={n:1 for n in (WF,SPEC,TARGET,OP,GROUP,REASON)},
                 model_hook=None, child_hook=None, batch_hook=None, originals=state['members'], member_tasks=[])
    async def identity(name, instance_id=None):
        found = [row for row in state['originals'] if row['ContainerName']==name and (instance_id is None or row['ContainerId']==instance_id)]
        if len(found)!=1: raise ValueError('identity mismatch')
        row=found[0]
        status = dict(Container=reference(name,row['ContainerId']), Spec=reference('Spec',SPEC,'A'),
                      Workflow=reference('Flow',WF,'A'), Step=reference('Inspect',SRC), Operation=reference('Op',OP),
                      InRework=False, InProcess=True)
        status.update(state.get('status_override',{}))
        if state.get('identity_hook'): state['identity_hook'](status)
        return dict(name=name,instanceId=row['ContainerId']),status
    monkeypatch.setattr(r.q, 'container_identity', AsyncMock(side_effect=identity))
    async def sql(query, maximum):
        if 'INFORMATION_SCHEMA.COLUMNS' in query and "TABLE_NAME='ContainerSampleData'" in query:
            return [dict(TABLE_NAME='ContainerSampleData',COLUMN_NAME=field) for field in i.TASK_COLUMNS.split(',')]
        if 'FROM ContainerSampleData WHERE ContainerId IN (' in query:return copy.deepcopy(state['member_tasks'])
        if 'FROM WorkflowStep WHERE' in query:return copy.deepcopy(state['step_rows'])
        if 'FROM Path p INNER JOIN' in query:return copy.deepcopy(state['path_rows'])
        for entity,(table,column,_) in r.ASSETS.items():
            if ' FROM '+table+' WHERE ' in query:
                iid=query.split("='",1)[1].split("'",1)[0]
                return [{column:iid,'ChangeCount':str(state['tokens'][iid])}]
        if 'FROM Container WHERE ContainerId IN (' in query:
            return [copy.deepcopy(row) for row in state['originals'] if "'"+row['ContainerId']+"'" in query]
        return await original_sql(query,maximum)
    monkeypatch.setattr(r.q,'sql_rows',AsyncMock(side_effect=sql))
    state['step_rows']=[dict(WorkflowStepId=SRC,WorkflowStepName='Inspect',WorkflowId=WF,StepType=1,SpecId=SPEC,SpecBaseId=None,ChangeCount=1),dict(WorkflowStepId=DST,WorkflowStepName='Repair',WorkflowId=WF,StepType=1,SpecId=TARGET,SpecBaseId=None,ChangeCount=1)]
    state['path_rows']=[dict(PathId=PATH,PathName='Default',FromStepId=SRC,ToStepId=DST,ReEntryStepId=None,EndReworkStepId=None,ReturnToStepId=None,CDOTypeId=7112,ChangeCount=1)]
    state['read_serial']=0
    async def read(method,path,body=None,params=None,**kw):
        assert method=='GET' and body is None
        if state['model_hook']: state['model_hook'](path,params)
        entity,*parts=path.removeprefix('/api/').split('/')
        rows=state['models'].get(entity,[])
        if parts:
            found=[v for v in rows if v['InstanceID']==parts[0]]
            if len(found)!=1:return json.dumps({'error':'not found'})
            data=copy.deepcopy(found[0])
            if entity=='Workflows':
                state['read_serial']+=1;serial=state['read_serial']
                if not params:data.pop('Steps',None)
                elif params.get('$expand')=='Steps':
                    for item in data['Steps']:
                        item['@odata.type']='#modeling.SpecStepChanges';item['InstanceID']=key(4000+serial)
                        item['Parent']=None
                        item['DefaultPath']=dict(Name='Normal',InstanceId=key(6000+serial),CDOTypeName='MovePathChanges')
                        item.pop('ReworkPaths',None);item.pop('ReworkPathSelectors',None)
                else:
                    data['Steps']=[dict(Name=x['Name'],InstanceID=key(5000+serial),StepType=None,Spec=None,ReworkPaths=x.get('ReworkPaths'),ReworkPathSelectors=x.get('ReworkPathSelectors'),**{k:v for k,v in x.items() if 'nextLink' in k}) for x in data['Steps']]
                    for item in data['Steps']:
                        for path in item.get('ReworkPaths') or []:
                            path['InstanceID']=key(7000+serial)
            return json.dumps(data)
        expression=params['$filter']
        rows=[v for v in rows if "Name eq '"+v['Name']+"'" in expression]
        if 'IsRevOfRcd eq true' in expression: rows=[v for v in rows if v.get('IsRevOfRcd') is True]
        return json.dumps({'value':copy.deepcopy(rows[:2])})
    read_mock=AsyncMock(side_effect=read);monkeypatch.setattr(process_context,'request',read_mock)
    async def probe(method,path,**kw):
        assert method=='POST' and kw['params']['execute']=='false'
        body=kw['body'];data=echo(body)
        if path=='/api/Rework':
            assert set(body)=={'container','reworkReason'}
            data.update(Path=reference('Default',PATH),ToWorkflow=reference('Flow',WF,'A'),ToStep=reference('Repair',DST),
                        ReEntryWorkflow=reference('Flow',WF,'A'),ReEntryStep=reference('Inspect',SRC),
                        EndReworkStep=None,EndReworkWorkflow=None,MoveAllQty='false',Qty=None,Qty2=None,ESigRequirement=None)
            if state['child_hook']: state['child_hook'](data)
        elif path=='/api/ExecuteSamplingLot':
            data.update(Containers=[v['ContainerName'] for v in state['members']],ScrapReason=None)
            if state['batch_hook']: state['batch_hook'](data)
        else:raise AssertionError(path)
        return json.dumps({'data':data})
    probes=AsyncMock(side_effect=probe);monkeypatch.setattr(r,'request',probes)
    state.update(probes=probes,client=client,reads=read_mock)
    return state


def prepare(**kw):return result(r.prepare_sampling_lot_rework('L','RepairReason',**kw))
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))
def snapshot(ready):return result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']


def test_native_batch_plan_binds_every_member_config_and_cleanup(env):
    ready=prepare(comments='repair requested');assert ready.get('state')=='ready',ready
    body=ready['request_preview']['body'];context=ready['server_context'];snap=snapshot(ready)
    assert set(body)=={'samplingLot','samplingLotResult','reworkReason','comments'} and body['samplingLotResult']==2
    assert body['samplingLot']==dict(name='L',instanceId=LOT) and body['reworkReason']==dict(name='RepairReason',instanceId=REASON)
    assert len(context['member_routes'])==2 and context['affected_container_names']==['C','C2']
    assert all(row['native_default_quantity_fields']['MoveAllQty']=='false' for row in context['member_routes'])
    assert not context['quantity_effects_verified'] and not context['signature_verified'] and context['quality_verdict'] is None
    assert 'ContainerSampleData' in context['note'] and len(snap['dependencies'])==6
    assert {'runtime:lot:id:'+LOT,'runtime:container:id:'+CID,'runtime:container:id:'+CID2}<=set(snap['resource_writes'])
    assert 'modeling:sampling-reference-config:gate' in p.resource_sets(snap)[0]
    assert execute(ready)['state']=='unknown'
    args=env['client'].request.await_args
    assert args.args[0]=='POST' and args.args[1].endswith('/ExecuteSamplingLot') and args.kwargs['json']==body
    env['client'].request.assert_awaited_once()
    assert all(c.kwargs['params']['execute']=='false' for c in env['probes'].await_args_list)
    assert len([c for c in env['probes'].await_args_list if c.args[1]=='/api/Rework'])==4


@pytest.mark.parametrize('completed',['true','false'])
def test_failed_evidence_does_not_require_all_sampling_to_finish(env,completed):
    env['lot']['SamplingCompleted']=completed
    assert prepare()['state']=='ready'


@pytest.mark.parametrize('target,field,value',[
    ('lot','SamplingPassed','true'),('lot','SamplingPassed',None),('lot','SamplingCompleted',None),
    ('lot','LotStatus','2'),('lot','SamplingLotResult','2'),
    ('task','Rejects',0),('task','SamplesFailed',0),('task','SamplesTested',1),('task','TotalSamplesRequired',0),
    ('task','InspectAllContQty',True),('task','InspectAllQtyOnFailure',None),('task','Rejects',3),
    ('member','Status',2),('member','CurrentHoldCount',1),('member','CurrentHoldCount',None),
    ('member','ChildCount',1),('member','ParentContainerId',CID2),('member','Qty','0'),
    ('member','Qty2','1'),('member','ProductId',PLAN),('member','ChangeCount',None),
])
def test_whole_batch_blockers_fail_before_route_probes(env,target,field,value):
    row=env['lot'] if target=='lot' else env['tasks'][0] if target=='task' else env['members'][0]
    row[field]=value
    assert 'error' in prepare()
    env['probes'].assert_not_awaited();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['empty','duplicate','duplicate_name','too_many','no_tasks'])
def test_complete_membership_and_real_tasks_required(env,mode):
    if mode=='empty':env['members']=[]
    if mode=='duplicate':env['members'].append(copy.deepcopy(env['members'][0]))
    if mode=='duplicate_name':env['members'][1]['ContainerName']='C'
    if mode=='too_many':env['members']=[dict(env['members'][0],ContainerName=f'C{n}',ContainerId=key(1100+n)) for n in range(21)]
    if mode=='no_tasks':env['tasks']=[]
    assert 'error' in prepare();env['probes'].assert_not_awaited()


@pytest.mark.parametrize('field,value',[('InRework',True),('InRework',None),('Workflow',None),('Step',None),
    ('Spec',reference('Other',TARGET,'A')),('Operation',reference('Other',REASON))])
def test_member_current_process_cannot_be_guessed(env,field,value):
    env['status_override']={field:value}
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['no_path','wrong_path_name','two_paths','selectors','missing_paths','foreign_target',
    'foreign_source','duplicate_step','non_spec','disallowed_target','inactive_target','wrong_reason','duplicate_reason','paged_group','paged_paths'])
def test_configuration_must_be_complete_and_agree_with_native_route(env,mode):
    if mode=='no_path':env['source']['ReworkPaths']=[]
    if mode=='wrong_path_name':env['source']['ReworkPaths'][0]['Name']='Other'
    if mode=='two_paths':env['source']['ReworkPaths']*=2
    if mode=='selectors':env['source']['ReworkPathSelectors']=[reference('Dynamic',key(999))]
    if mode=='missing_paths':env['source']['ReworkPaths']=None
    if mode=='foreign_target':env['source']['ReworkPaths'][0]['ToWorkflow']=reference('Other',key(999),'A')
    if mode=='foreign_source':env['step_rows'][0]['WorkflowId']=key(999)
    if mode=='duplicate_step':env['workflow']['Steps'].append(copy.deepcopy(env['source']))
    if mode=='non_spec':env['source']['StepType']=2
    if mode=='disallowed_target':env['target_spec']['AllowReworkTo']=False
    if mode=='inactive_target':env['target_spec']['Status']=2
    if mode=='wrong_reason':env['group']['ResolvedEntries']=[]
    if mode=='duplicate_reason':env['group']['ResolvedEntries']*=2
    if mode=='paged_group':env['group']['ResolvedEntries@odata.nextLink']='more'
    if mode=='paged_paths':env['source']['ReworkPaths@odata.nextLink']='more'
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('Path',reference('Other',key(999))),('Path',reference('Other',PATH)),
    ('ToWorkflow',reference('Other',key(999),'A')),('ToStep',reference('Inspect',SRC)),
    ('ReEntryStep',reference('Repair',DST)),('ReEntryWorkflow',reference('Other',key(999),'A')),
    ('EndReworkStep',reference('Missing',key(999))),('Container',reference('Other',CID)),('error','failure')])
def test_native_child_echo_is_a_required_dependency_not_execution(env,field,value):
    env['child_hook']=lambda data:data.update({field:value})
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('Containers',['C']),('Containers',['C','C']),('SamplingLotResult','3'),
    ('ReworkReason',reference('Wrong',REASON)),('ScrapReason',reference('Loss',key(999))),('error','failed')])
def test_batch_echo_checks_all_members_and_exact_reason(env,field,value):
    env['batch_hook']=lambda data:data.update({field:value})
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('what',['lot','member','task','route','reason_group','target_spec','counter','ror'])
def test_rebuild_rejects_dependency_changes_before_any_write(env,what):
    if what=='ror':env['source']['Spec']=dict(Name='Spec',UseROR=True)
    ready=prepare();assert ready.get('state')=='ready',ready
    if what=='lot':env['lot']['ChangeCount']='2'
    if what=='member':env['members'][1]['ChangeCount']=2
    if what=='task':env['tasks'][0].update(CompletedSamples=3,SamplesTested=3)
    if what=='route':env['source']['ReworkPaths'][0]['Description']='changed'
    if what=='reason_group':env['group']['Description']='changed'
    if what=='target_spec':env['target_spec']['Description']='changed'
    if what=='counter':env['tokens'][GROUP]+=1
    if what=='ror':env['spec']['IsRevOfRcd']=False
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_mid_read_config_and_container_state_changes_fail(env):
    def hook(path,params):
        if params and params.get('$expand')=='ResolvedEntries':env['tokens'][GROUP]+=1
    env['model_hook']=hook
    assert 'error' in prepare()
    env['model_hook']=None
    count=0
    def identity(status):
        nonlocal count
        count+=1
        if count>2:status['InProcess']=False
    env['identity_hook']=identity
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode',['result3','bool_result','spec','quantity','service','kind','reason_name','reason_id','lot','comment'])
def test_transport_keeps_rework_isolated_from_ok_scrap_and_overrides(env,mode):
    snap=snapshot(prepare())
    if mode=='result3':snap['body']['samplingLotResult']=3
    if mode=='bool_result':snap['body']['samplingLotResult']=True
    if mode=='spec':snap['body']['spec']=reference('Target',TARGET,'A')
    if mode=='quantity':snap['body']['qty']=1
    if mode=='service':snap['service']='Rework'
    if mode=='kind':snap['kind']='lot_completion'
    if mode=='reason_name':snap['body']['reworkReason']['name']='Other'
    if mode=='reason_id':snap['body']['reworkReason']['instanceId']=key(999)
    if mode=='lot':snap['body']['samplingLot']['instanceId']=key(999)
    if mode=='comment':snap['body']['comments']='other'
    with pytest.raises(ValueError):p.transport(snap)


def test_unknown_single_attempt_inspects_original_members_and_never_unlocks(env,monkeypatch):
    ready=prepare();second=prepare();assert execute(ready)['state']=='unknown'
    assert 'error' in execute(ready) and 'error' in execute(second)
    env['members']=[dict(env['originals'][0],ContainerId=key(999),ContainerName='Replacement')]
    env['originals'][0].update(SamplingLotId=None,Status=2)
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert {row['ContainerId'] for row in out['original_members_current_records']}=={CID,CID2}
    assert out['current_lot']['members'][0]['ContainerName']=='Replacement' and not out['verified_success']
    assert not out['state_changed'] and not out['retry_allowed']
    async def empty_history(query, maximum):
        assert 'FROM HistoryMainline WHERE Comments=' in query
        return []
    monkeypatch.setattr(h.q,'sql_rows',empty_history)
    candidates=result(h.find_quality_execution_candidates(ready['plan_id'],ready['plan_digest']))
    assert candidates['rows']==[] and not candidates['target_linkage_available']
    for kind in ('lot_completion','lot_maintenance','lot_inspection','inspection'):
        other={'kind':kind,'environment':p.environment(),'scope_key':'other','resource_writes':['runtime:container:id:'+CID]}
        if kind=='inspection':
            other.update(target_id=CID,definition=env['definition'],before={'task':env['tasks'][0]})
        assert p.conflicts(snapshot(ready),other)
    env['client'].request.assert_awaited_once()
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


@pytest.mark.parametrize('comments',['\0','😀'*91,True])
def test_invalid_comments_fail_before_network(env,comments):
    assert 'error' in prepare(comments=comments);env['probes'].assert_not_awaited();env['reads'].assert_not_awaited()


def test_registration_closed_schema_routing_and_mcp_lifecycle(env):
    name='prepare_sampling_lot_rework'
    assert get_tool_func(name) and TOOL_POLICIES[name].effect=='read' and name in ROUTING_INSTRUCTION
    selected=select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[{'role':'user','content':'抽样批不合格，请按默认路径整批返工，原因RepairReason'}])
    assert name in {t['function']['name'] for t in selected}
    async def run():
        async with Client(mcp) as client:
            schema=next(t.input_schema for t in await client.list_tools() if t.name==name)
            assert schema['additionalProperties'] is False and schema['required']==['lot_name','rework_reason_name']
            raw=await client.call_tool(name,dict(lot_name='L',rework_reason_name='RepairReason'))
            ready=json.loads(raw.content[0].text);assert ready.get('state')=='ready',ready
            keys={k:ready[k] for k in ('plan_id','plan_digest')}
            calls=[{'function':{'name':'execute_quality_transaction','arguments':json.dumps(keys)}}]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval
            preview=plan_preview(calls);assert 'ExecuteSamplingLot' in preview and 'RepairReason' in preview and 'ContainerSampleData' in preview
            posted=await client.call_tool('execute_quality_transaction',keys)
            assert json.loads(posted.content[0].text)['state']=='unknown'
            bad=await client.call_tool(name,dict(lot_name='L',rework_reason_name='RepairReason',rework_spec_name='RepairSpec'),raise_on_error=False)
            assert bad.is_error
    asyncio.run(run());env['client'].request.assert_awaited_once()


def test_nonexecuting_route_absence_is_unknown_not_fabricated_validation(env):
    def hook(data):
        for field in ('Path','ToStep','ToWorkflow','ReEntryStep','ReEntryWorkflow','EndReworkStep','EndReworkWorkflow'):
            data.pop(field,None)
    env['child_hook']=hook
    ready=prepare();assert ready.get('state')=='ready',ready
    assert all(not row['runtime_route_resolution_verified'] for row in ready['server_context']['member_routes'])
    assert all(row['to_step']['instanceId']==DST for row in ready['server_context']['member_routes'])
    assert not ready['dynamic_validation_verified']
    assert execute(ready)['state']=='unknown'


@pytest.mark.parametrize('table,field,value',[('step_rows','WorkflowStepId',key(999)),('step_rows','SpecId',key(999)),
    ('path_rows','PathName','Other'),('path_rows','ToStepId',key(999)),('path_rows','ReEntryStepId',DST),
    ('path_rows','EndReworkStepId',DST),('path_rows','CDOTypeId',1440)])
def test_persistent_step_and_path_identities_must_agree_with_model(env,table,field,value):
    env[table][0][field]=value
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


def test_rework_navigation_can_omit_type_annotation(env):
    path = env['models']['Workflows'][0]['Steps'][0]['ReworkPaths'][0]
    path.pop('@odata.type')
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    assert ready['server_context']['member_routes'][0]['path']['instanceId'] == PATH
