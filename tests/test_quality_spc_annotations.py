import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from tools import mcp, get_tool_func, quality_spc_annotations as a, quality_plans as p
from core.tool_catalog import TOOL_POLICIES
from agent.safety import evaluate_mutations, MutationCounts, plan_preview
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION

CID,POINT,PARENT,DEF,ORIG_POINT,HM,HID,TXN,CHART,GROUP,HIST = [f'{n:016x}' for n in range(101,112)]
def result(coro): return json.loads(asyncio.run(coro))
def echo(value):
    if isinstance(value,dict):return {p.FIELD_CASE.get(k,k[0].upper()+k[1:]):echo(v) for k,v in value.items()}
    if isinstance(value,list):return [echo(v) for v in value]
    return value


def history():
    return dict(AddSPCAnnotationHistoryId=HIST,HistoryMainlineId=HM,HistoryId=POINT,TxnId=TXN,DataPointId=POINT,
                DataCollectionDefId=DEF,SPCChartDefId=CHART,Annotation='existing note',Exclude=False,
                CustomSPCDataId=None,ExternalDataId=None,MainlineHistoryId=POINT,MainlineTxnId=TXN,
                ReversalStatus=1,TxnType=8393,TxnDateGMT='2026-10-29T00:00:00Z',Comments='old')


@pytest.fixture
def env(monkeypatch,tmp_path):
    source=dict(DataPointHistoryDetailId=POINT,DataPointHistoryId=PARENT,DataCollectionDefId=DEF,DataPointId=ORIG_POINT,
                HistoryId=HID,TxnId=TXN,ChangeCount=1,DataName='Diameter',DataType=2,DataValue='10.1',UOMId=None,
                ParentChangeCount=1,ParentDefinitionId=DEF,ParentHistoryId=HID,ParentTxnId=TXN,
                HistoryMainlineId=HM,MainlineHistoryId=HID,MainlineTxnId=TXN,ContainerId=CID,
                MainlineChangeCount=1,ReversalStatus=1,TxnDateGMT='2026-10-29T00:00:00Z',TxnType=2609)
    state=dict(source=source,histories=[],chart=dict(InstanceID=CHART,Name='Diameter chart',IsFrozen=False,RecordAgainstCustomSPCDataSrc=False),
               definition=dict(InstanceID=DEF,Name='Measure',Revision='A',Status=1,IsFrozen=False,SPCChartDefGroup={'InstanceId':GROUP}),
               group=dict(InstanceID=GROUP,Name='Charts',IsFrozen=False,ResolvedEntries=[dict(InstanceId=CHART,Name='Diameter chart')]),
               chart_count=1,def_count=1,preview_hook=None,context_hook=None,
               columns=[dict(TABLE_NAME=t,COLUMN_NAME=c) for t,cols in a.SCHEMA.items() for c in cols.split(',')])
    async def sql(query,maximum):
        if 'INFORMATION_SCHEMA.COLUMNS' in query:
            if "TABLE_NAME='NamedObjectGroup'" in query:
                return [dict(TABLE_NAME='NamedObjectGroup',COLUMN_NAME=c) for c in a.GROUP_COLUMNS.split(',')]
            return copy.deepcopy(state['columns'])
        if ' FROM NamedObjectGroup WHERE ' in query:
            return [copy.deepcopy(state.get('persisted_group',dict(NamedObjectGroupId=GROUP,NamedObjectGroupName='Charts',
                CDOTypeId='8198',ChangeCount='1',IsFrozen='false')))]
        if ' FROM DataPointHistoryDetail d ' in query:
            return [copy.deepcopy(state['source'])] if state['source'] is not None else []
        if ' FROM AddSPCAnnotationHistory a ' in query:return copy.deepcopy(state['histories'])
        if ' FROM SPCChartDef WHERE ' in query:return [dict(SPCChartDefId=CHART,ChangeCount=state['chart_count'])]
        if ' FROM DataCollectionDef WHERE ' in query:return [dict(DataCollectionDefId=DEF,ChangeCount=state['def_count'],SPCChartDefGroupId=GROUP)]
        raise AssertionError(query)
    class Context:
        def __init__(self,*args):pass
        async def resolve(self,entity,ref,revisioned=True):
            assert entity in ('SPCChartDefs','UserDataCollectionDefs')
            if entity=='SPCChartDefs':
                assert ref.get('Name')=='Diameter chart'
                if 'InstanceID' in ref:assert ref['InstanceID']==CHART
                return copy.deepcopy(state['chart'])
            assert ref['InstanceID']==DEF;return copy.deepcopy(state['definition'])
        async def get(self,entity,key,params=None):
            assert entity=='SPCChartDefGroups' and key==GROUP and params=={'$expand':'ResolvedEntries'}
            if state['context_hook']:state['context_hook']()
            return copy.deepcopy(state['group'])
    async def preview(method,path,**kw):
        assert (method,path)==('POST','/api/AddSPCAnnotation') and kw['params']['execute']=='false'
        data=echo(copy.deepcopy(kw['body']))
        data.update(DataCollectionDef=dict(InstanceId=DEF,Name='Measure',Revision='A'),ESigRequirement=None,AdditionalESigReqs=[])
        if state['preview_hook']:state['preview_hook'](data)
        return json.dumps(data)
    monkeypatch.setattr(a.q,'sql_rows',AsyncMock(side_effect=sql))
    monkeypatch.setattr(a,'ReadContext',Context)
    monkeypatch.setattr(a.q,'container_identity',AsyncMock(return_value=(dict(name='C',instanceId=CID),{})))
    monkeypatch.setattr(a,'request',AsyncMock(side_effect=preview))
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'quality.sqlite3')
    client=type('Client',(),{})();client.request=AsyncMock(return_value=type('Reply',(),dict(status_code=200,text='{"CompletionMsg":"done"}'))())
    monkeypatch.setattr(p,'get_client',lambda:client);monkeypatch.setattr(p,'get_headers',lambda:{})
    state['client']=client
    return state


def prepare(**kw):return result(a.prepare_spc_annotation('C',POINT,'Diameter chart','Investigated; continue observation',**kw))
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))
def snapshot(ready):return result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']


def test_context_proves_original_source_and_group_and_reads_all_chart_history(env):
    env['histories']=[history()]
    out=result(a.get_spc_annotation_context('C',POINT,'Diameter chart'))
    assert out['complete'] and out['source']['DataPointId']==ORIG_POINT
    assert out['source']['DataPointHistoryDetailId']==POINT and out['configuration']['definition']['Revision']=='A'
    assert out['annotation_history'][0]['Annotation']=='existing note' and not out['verified_success']
    queries=[c.args[0] for c in a.q.sql_rows.await_args_list]
    h=next(q for q in queries if ' FROM AddSPCAnnotationHistory a ' in q)
    assert 'm.ReversalStatus=1' in h and 'a.SPCChartDefId=' not in h and 'TOP 101' in h
    assert 'LEFT JOIN HistoryMainline' in h and 'm.HistoryMainlineId IS NULL' in h
    a.request.assert_not_awaited();env['client'].request.assert_not_awaited();assert not p.PLAN_DB.exists()


def test_preparation_exact_native_body_and_no_mes_write(env):
    ready=prepare();assert ready['state']=='ready' and ready['static_and_echo_verified']
    body=ready['request_preview']['body']
    assert body['dataPoint']==dict(instanceId=POINT,parent={'instanceId':PARENT})
    assert body['spcChartDef']==dict(name='Diameter chart',instanceId=CHART)
    assert body['exclude'] is False and set(body)=={'dataPoint','spcChartDef','annotation','exclude','comments'}
    assert ready['request_preview']['path']=='/api/AddSPCAnnotation'
    assert not ready['server_context']['annotation_written_verified']
    assert 'runtime:spc-point:id:'+POINT in snapshot(ready)['resource_writes']
    env['client'].request.assert_not_awaited()


def test_single_submission_unknown_never_replays(env):
    ready=prepare();out=execute(ready)
    assert out['state']=='unknown' and not out['verified_success'] and not out['retry_allowed']
    assert 'error' in execute(ready);env['client'].request.assert_awaited_once()
    call=env['client'].request.await_args
    assert call.args[0]=='POST' and call.args[1].endswith('/api/AddSPCAnnotation')
    assert call.kwargs['params']=={'execute':'true'} and call.kwargs['follow_redirects'] is False


def test_second_plan_and_other_quality_container_claim_blocked_by_unknown(env):
    first=prepare();second=prepare();snap=snapshot(first)
    assert p.conflicts(snap,dict(kind='ncr',scope_key='ncr:other',target_id=HM,environment=p.environment(),
                                before={'ContainerId':CID},body={}))
    assert execute(first)['state']=='unknown' and 'error' in execute(second)
    env['client'].request.assert_awaited_once()


def test_two_charts_same_point_conflict_and_shared_configuration_resources(env):
    snap=snapshot(prepare());other=copy.deepcopy(snap);other['name']='other';other['scope_key']='changed chart name'
    assert p.conflicts(snap,other)
    reads,writes=p.resource_sets(snap)
    assert 'modeling:SPCChartDefs:id:'+CHART in reads and 'runtime:container:id:'+CID in writes


@pytest.mark.parametrize('field,value', [('DataPointHistoryDetailId',ORIG_POINT),('DataPointHistoryId','bad'),
    ('DataCollectionDefId',HM),('ParentHistoryId',HM),('ParentTxnId',HM),('MainlineTxnId',HM),
    ('MainlineHistoryId',HM),('ContainerId',HM),('ChangeCount',None),('ParentChangeCount',True),
    ('MainlineChangeCount',-1),('ReversalStatus',2),('DataType',4),('DataType',True),('DataValue',None),
    ('DataValue',''),('DataName',' '),('UOMId','bad'),('TxnDateGMT',None)])
def test_bad_original_source_never_previews_or_submits(env,field,value):
    env['source'][field]=value
    assert 'error' in prepare();a.request.assert_not_awaited();env['client'].request.assert_not_awaited()
    assert not p.PLAN_DB.exists()


@pytest.mark.parametrize('mode',['missing_source','missing_field','schema_missing','schema_duplicate','schema_extra'])
def test_unavailable_or_ambiguous_schema_chain_is_not_assumed(env,mode):
    if mode=='missing_source':env['source']=None
    elif mode=='missing_field':env['source'].pop('ParentTxnId')
    elif mode=='schema_missing':env['columns'].pop()
    elif mode=='schema_duplicate':env['columns'].append(env['columns'][0])
    else:env['columns'].append(dict(TABLE_NAME='Other',COLUMN_NAME='Guessed'))
    assert 'error' in prepare();a.request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('IsFrozen',True),('IsFrozen',None),('RecordAgainstCustomSPCDataSrc',True),
    ('RecordAgainstCustomSPCDataSrc',None)])
def test_custom_unknown_or_frozen_chart_blocked(env,field,value):
    env['chart'][field]=value;assert 'error' in prepare();a.request.assert_not_awaited()


@pytest.mark.parametrize('target,field,value',[
    ('definition','Status',2),('definition','IsFrozen',True),('definition','Revision',''),
    ('group','InstanceID',HM),('group','IsFrozen',True),
    ('group','ResolvedEntries',[]),('group','ResolvedEntries',None),('group','ResolvedEntries',[dict(InstanceId=CHART,Name='wrong')]),
    ('group','ResolvedEntries',[dict(InstanceId=CHART,Name='Diameter chart')]*2),('group','@odata.nextLink','next')])
def test_definition_group_and_complete_membership_required(env,target,field,value):
    env[target][field]=value;assert 'error' in prepare();a.request.assert_not_awaited()


def test_unexpanded_definition_group_uses_verified_persistent_foreign_key(env):
    env['definition']['SPCChartDefGroup']=None
    ready=prepare()
    assert ready.get('state')=='ready',ready
    assert snapshot(ready)['before']['configuration']['group']['InstanceID']==GROUP
    env['client'].request.assert_not_awaited()


def test_projected_group_inherited_flags_require_persistent_type_name_version_and_state(env):
    env['definition']['SPCChartDefGroup']=None
    env['group']['IsFrozen']=None
    ready=prepare()
    assert ready.get('state')=='ready',ready
    group=snapshot(ready)['before']['configuration']['group']
    assert group['persistent_state']['NamedObjectGroupId']==GROUP
    assert group['persistent_state']['CDOTypeId']=='8198' and group['persistent_state']['IsFrozen'] is False
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[
    ('NamedObjectGroupId',HM),('NamedObjectGroupName','wrong group'),('CDOTypeId','5330'),
    ('CDOTypeId',None),('ChangeCount',None),('ChangeCount','-1'),('IsFrozen','true'),('IsFrozen',None)])
def test_projected_group_persistent_unknown_or_wrong_identity_blocks_preparation(env,field,value):
    env['group']['IsFrozen']=None
    env['persisted_group']=dict(NamedObjectGroupId=GROUP,NamedObjectGroupName='Charts',CDOTypeId='8198',
                                ChangeCount='1',IsFrozen='false')
    env['persisted_group'][field]=value
    assert 'error' in prepare()
    a.request.assert_not_awaited();env['client'].request.assert_not_awaited()


def test_projected_group_persistent_version_change_during_read_blocks_preparation(env,monkeypatch):
    env['group']['IsFrozen']=None
    env['persisted_group']=dict(NamedObjectGroupId=GROUP,NamedObjectGroupName='Charts',CDOTypeId='8198',
                                ChangeCount='1',IsFrozen='false')
    prior=a.q.sql_rows;reads=0
    async def sql(query,maximum):
        nonlocal reads
        if ' FROM NamedObjectGroup WHERE ' in query:
            reads+=1
            if reads>1:env['persisted_group']['ChangeCount']='2'
        return await prior(query,maximum)
    monkeypatch.setattr(a.q,'sql_rows',AsyncMock(side_effect=sql))
    assert 'error' in prepare()
    a.request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('HistoryId',HM),('MainlineHistoryId',HM),('MainlineTxnId',HM),('DataPointId',ORIG_POINT),
    ('DataCollectionDefId',HM),('SPCChartDefId','bad'),('Exclude',None),('CustomSPCDataId','external'),('ExternalDataId','external'),
    ('TxnType',2609),('ReversalStatus',2)])
def test_history_requires_real_point_transaction_and_native_source(env,field,value):
    env['histories']=[history()];env['histories'][0][field]=value
    assert 'error' in prepare();a.request.assert_not_awaited()


def test_excluded_history_on_another_chart_cannot_be_hidden_or_unexcluded(env):
    env['histories']=[{**history(),'SPCChartDefId':HM,'Exclude':True}]
    assert 'error' in prepare();a.request.assert_not_awaited()


@pytest.mark.parametrize('mode',['duplicate','overflow','missing'])
def test_history_incompleteness_is_not_a_subset(env,mode):
    env['histories']=[history(),history()] if mode=='duplicate' else [history()]*101 if mode=='overflow' else [dict(Exclude=False)]
    assert 'error' in prepare()


@pytest.mark.parametrize('mode',['point','parent','chart','text','exclude','comments','definition','signature','missing_signature','additional','missing_additional','next_link','error','malformed'])
def test_nonexecuting_echo_must_bind_every_required_field(env,mode):
    def hook(data):
        if mode=='point':data['DataPoint']['InstanceId']=ORIG_POINT
        elif mode=='parent':data['DataPoint']['Parent']['InstanceId']=HM
        elif mode=='chart':data['SPCChartDef']['InstanceId']=HM
        elif mode=='text':data['Annotation']='changed'
        elif mode=='exclude':data['Exclude']=True
        elif mode=='comments':data['Comments']='no marker'
        elif mode=='definition':data['DataCollectionDef']['Revision']='B'
        elif mode=='signature':data['ESigRequirement']={'InstanceId':HM}
        elif mode=='missing_signature':data.pop('ESigRequirement')
        elif mode=='additional':data['AdditionalESigReqs']=[{'InstanceId':HM}]
        elif mode=='missing_additional':data.pop('AdditionalESigReqs')
        elif mode=='next_link':data['@odata.nextLink']='next'
        elif mode=='error':data['ErrorCode']='rejected'
        else:data.clear()
    env['preview_hook']=hook
    assert 'error' in prepare() and not p.PLAN_DB.exists();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('target',['source','chart','definition','group','history'])
def test_late_changes_during_parent_preview_are_detected(env,target):
    def hook(data):
        if target=='source':env['source']['DataValue']='10.2'
        elif target=='chart':env['chart']['Description']='changed'
        elif target=='definition':env['def_count']=2
        elif target=='group':env['group']['Description']='changed'
        else:env['histories']=[history()]
    env['preview_hook']=hook
    assert 'error' in prepare() and not p.PLAN_DB.exists()


@pytest.mark.parametrize('target',['source','chart','definition','group','history'])
def test_change_after_ready_prevents_business_submission(env,target):
    ready=prepare()
    if target=='source':env['source']['ChangeCount']=2
    elif target=='chart':env['chart_count']=2
    elif target=='definition':env['definition']['Revision']='B'
    elif target=='group':env['group']['Description']='changed'
    else:env['histories']=[history()]
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('annotation',['',' ',None,'a'*2001,'a\x00b'])
def test_invalid_annotation_is_not_silently_trimmed_or_shortened(env,annotation):
    out=result(a.prepare_spc_annotation('C',POINT,'Diameter chart',annotation))
    assert 'error' in out;a.q.sql_rows.assert_not_awaited()


@pytest.mark.parametrize('field',['exclude','customSPCDataId','externalDataId','externalTable','processESignatures','container','serviceDetails','annotation','dataPoint'])
def test_transport_refuses_exclusion_external_source_signature_or_payload_overrides(env,field):
    snap=snapshot(prepare());snap['body'][field]=True
    with pytest.raises((ValueError,TypeError)):p.transport(snap)


def test_transport_refuses_other_service_or_kind(env):
    snap=snapshot(prepare());snap['service']='RecordSPCViolation'
    with pytest.raises(ValueError):p.transport(snap)
    snap=snapshot(prepare());snap['kind']='inspection'
    with pytest.raises(ValueError):p.transport(snap)


@pytest.mark.parametrize('arguments', [dict(container_name=' '),dict(data_point_history_id='local-observation-1'),
    dict(spc_chart_name='bad\nname'),dict(container_instance_id='bad'),dict(spc_chart_instance_id='bad')])
def test_invalid_identifiers_and_names_do_not_query(env,arguments):
    args=dict(container_name='C',data_point_history_id=POINT,spc_chart_name='Diameter chart',annotation='note');args.update(arguments)
    assert 'error' in result(a.prepare_spc_annotation(**args));a.q.sql_rows.assert_not_awaited()


@pytest.mark.parametrize('mode',['timeout','http400','cancellation'])
def test_network_failures_and_cancellation_preserve_unknown_and_claims(env,mode):
    ready=prepare()
    if mode=='http400':env['client'].request.return_value.status_code=400
    else:env['client'].request.side_effect=TimeoutError('uncertain') if mode=='timeout' else asyncio.CancelledError()
    if mode=='cancellation':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:assert execute(ready)['state']=='unknown'
    saved=result(p.get_quality_transaction_plan(ready['plan_id']))
    assert saved['state']=='unknown' and not saved['outcome']['retry_allowed']
    assert 'error' in execute(ready);env['client'].request.assert_awaited_once()


def test_metadata_source_and_all_query_columns_are_supported_by_native_snapshot():
    from pathlib import Path
    archive=json.loads(Path('docs/quality-fixtures/spc-write-native-columns.json').read_text(encoding='utf-8'))
    tables={x['query']['owner']:{r['name'] for r in x['result'].get('records',[])} for x in archive['results']}
    assert all(set(cols.split(','))<=tables[t] for t,cols in a.SCHEMA.items())


@pytest.mark.parametrize('turns', [ ['查看SPC注释历史'],['给这条SPC历史点加注释'],
    ['查看SPC注释上下文','继续'],['为原始采集点准备SPC注释','提交这个注释']])
def test_natural_language_and_multiturn_routing(turns):
    tools=[{'function':{'name':n}} for n in TOOL_POLICIES]
    messages=[]
    for t in turns:messages.extend([dict(role='user',content=t),dict(role='assistant',content='routing fixture')])
    selected={t['function']['name'] for t in select_tools(tools,messages)}
    assert {'prepare_spc_annotation','get_spc_annotation_context'}<=selected
    assert ('execute_quality_transaction' in selected)==turns[-1].startswith('提交')
    assert '不能将本地观测编号或定义点ID当历史点' in ROUTING_INSTRUCTION


def test_inspection_is_only_evidence_no_followup_write_or_unlock(env):
    ready=prepare();execute(ready);env['histories']=[history()]
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert out['original_point_id']==POINT and len(out['current_context']['annotation_history'])==1
    assert not out['verified_success'] and not out['state_changed'] and not out['retry_allowed']
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'
    assert a.q.container_identity.await_args.args==('C',CID)
    env['client'].request.assert_awaited_once()


def test_inspect_pins_original_chart_even_when_caller_supplied_only_name(env,monkeypatch):
    ready=prepare();execute(ready)
    original=a.configuration;calls=[]
    async def watch(point,name,chart_id=None):
        calls.append(chart_id);return await original(point,name,chart_id)
    monkeypatch.setattr(a,'configuration',watch)
    assert not result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))['verified_success']
    assert calls==[CHART]


def test_exact_new_annotation_is_only_a_history_candidate(env):
    ready = prepare()
    assert execute(ready)['state'] == 'unknown'
    body = ready['request_preview']['body']
    env['histories'] = [history() | {'Comments': body['comments'], 'Annotation': body['annotation']}]
    proof = result(p.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert proof['history_correlation']['state'] == 'candidate_observed'
    assert proof['history_correlation']['candidates'][0]['AddSPCAnnotationHistoryId'] == HIST
    assert not proof['request_correlation_verified'] and not proof['state_changed']
    env['client'].request.assert_awaited_once()


def test_inspect_does_not_follow_changed_parent_definition(env,monkeypatch):
    ready=prepare();execute(ready)
    env['source']['DataCollectionDefId']=HM;env['source']['ParentDefinitionId']=HM
    guard=AsyncMock(side_effect=AssertionError('must not follow replacement definition'))
    monkeypatch.setattr(a,'configuration',guard)
    assert 'error' in result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    guard.assert_not_awaited()
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


def test_mcp_registered_real_prepare_and_confirmation(env):
    async def run():
        assert get_tool_func('prepare_spc_annotation') is a.prepare_spc_annotation
        assert TOOL_POLICIES['prepare_spc_annotation'].effect=='read'
        async with Client(mcp) as client:
            schema=next(t.input_schema for t in await client.list_tools() if t.name=='prepare_spc_annotation')
            assert schema['additionalProperties'] is False and 'exclude' not in schema['properties']
            response=await client.call_tool('prepare_spc_annotation',dict(container_name='C',data_point_history_id=POINT,spc_chart_name='Diameter chart',annotation='Investigated'))
            ready=json.loads(response.content[0].text);assert ready['state']=='ready'
            calls=[dict(function=dict(name='execute_quality_transaction',arguments=json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})))]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval
            assert 'AddSPCAnnotation' in plan_preview(calls)
    asyncio.run(run());env['client'].request.assert_not_awaited()
