import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from tools import mcp, quality_spc_violations as v, quality_spc_annotations as a, quality_plans as p, get_tool_func
from tests.test_quality_spc_annotations import env as annotation_env, CID,POINT,PARENT,DEF,CHART,HM,HID,TXN,history as annotation_history
from core.tool_catalog import TOOL_POLICIES
from agent.safety import evaluate_mutations, MutationCounts, plan_preview
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION

RULE,VIOL,MEMBER,DETAIL,SUMMARY,ROW=[f'{n:016x}' for n in range(201,207)]
UNIT=f'{210:016x}'
REPORT=dict(source_kind='operator_reported',evidence_reference='SPC operator report 2026-10-29/17',
            reported_value='10.1',plotted_value='10.1',uom_id=UNIT,lcl='0',center='5',ucl='10')
def result(coro):return json.loads(asyncio.run(coro))
def echo(obj):
    if isinstance(obj,dict):return {v.FIELD_CASE.get(k,k[0].upper()+k[1:]):echo(x) for k,x in obj.items()}
    if isinstance(obj,list):return [echo(x) for x in obj]
    if type(obj) is float:return str(obj)
    return obj


def history():
    return dict(SPCChartViolationsHistDtlsId=ROW,ParentId=DETAIL,HistoryId=HID,TxnId=TXN,DataPointId=POINT,ContainerId=CID,
        ChartVariable='Diameter',CL='5',LCL='0',UCL='10',CollectedDataValue='10.1',DerivedChartDataValue='10.1',
        FailureAction='Violation',RuleViolated='Site rules',ViolationName='Beyond limits',HoldReasonId=None,ResourceId=None,
        ResourceStatusCodeId=None,CustomSPCDataId=None,RecordSPCChartViolationHistDId=DETAIL,RecordSPCViolationSummaryId=SUMMARY,
        SPCChartDefId=CHART,SPCChartDefName='Diameter chart',ChartTxnId=TXN,HistoryMainlineId=HM,SummaryTxnId=TXN,
        MainlineHistoryId=HID,MainlineTxnId=TXN,TxnType=8396,ReversalStatus=1,TxnDateGMT='2026-10-29T00:00:00Z',Comments='old')


@pytest.fixture
def env(annotation_env,monkeypatch):
    state=annotation_env
    state['source']['UOMId']=UNIT
    state['chart'].update(ChartVariable='Diameter',EmailNotification='',EMailMessage=None,PutContainerOnHold=None,
                          SetResourceStatus=None,TriggerOCAPs=False,SPCRules={'Name':'Site rules','InstanceId':RULE})
    state.update(rule=dict(Name='Site rules',InstanceID=RULE,IsFrozen=False),violation=dict(Name='Beyond limits',InstanceID=VIOL,IsFrozen=False),
        members=[dict(SPCRulesViolationsId=MEMBER,ParentId=RULE,SPCViolationId=VIOL,IsFrozen=False,ChangeCount=1,
                      ViolationName='Beyond limits',ViolationIsFrozen=False,ViolationChangeCount=1)],
        rule_count=1,violation_count=1,violation_history=[],violation_hook=None,violation_calls=[])
    base_sql=a.q.sql_rows
    async def sql(query,maximum):
        if 'INFORMATION_SCHEMA.COLUMNS' in query and "TABLE_NAME='A_SPCRules'" in query:
            return [dict(TABLE_NAME=t,COLUMN_NAME=c) for t,cols in v.SCHEMA.items() for c in cols.split(',')]
        if ' FROM A_SPCRules WHERE ' in query:return [dict(SPCRulesId=RULE,ChangeCount=state['rule_count'])]
        if ' FROM SPCViolation WHERE ' in query:return [dict(SPCRuleViolationId=VIOL,ChangeCount=state['violation_count'])]
        if 'FROM SPCRuleViolations m ' in query:return copy.deepcopy(state['members'])
        if ' FROM SPCViolationHistoryDetail v ' in query:return copy.deepcopy(state['violation_history'])
        return await base_sql(query,maximum)
    monkeypatch.setattr(a.q,'sql_rows',AsyncMock(side_effect=sql))
    class Context:
        def __init__(self,*args):pass
        async def resolve(self,entity,ref,revisioned=True):
            if entity=='SPCChartDefs':return copy.deepcopy(state['chart'])
            if entity=='SPCRules':
                assert a.iid(a.q.identifier(ref))==RULE
                return copy.deepcopy(state['rule'])
            assert entity=='SPCViolations' and ref['Name']=='Beyond limits'
            state['violation_calls'].append(copy.deepcopy(ref))
            if 'InstanceID' in ref:assert ref['InstanceID']==VIOL
            return copy.deepcopy(state['violation'])
    monkeypatch.setattr(v,'ReadContext',Context)
    async def preview(method,path,**kwargs):
        assert (method,path)==('POST','/api/RecordSPCViolation') and kwargs['params']['execute']=='false'
        data=echo(copy.deepcopy(kwargs['body']));data.update(ESigRequirement=None,AdditionalESigReqs=[])
        detail=data['ServiceDetails'][0]
        detail.update(CDOTypeId='8400',EMailMessage=None,Recipients='',Message=None,FileName=None,FileDirectory='')
        detail['Violations'][0].update(CDOTypeId='8401',HoldReason=None,Resource=None,ResourceStatusCode=None)
        if state['violation_hook']:state['violation_hook'](data)
        return json.dumps(data)
    monkeypatch.setattr(v,'request',AsyncMock(side_effect=preview))
    return state


def prepare(**kw):
    args=dict(container_name='C',data_point_history_id=POINT,spc_chart_name='Diameter chart',violation_name='Beyond limits',reported_result=copy.deepcopy(REPORT))
    args.update(kw);return result(v.prepare_spc_violation(**args))
def snap(ready):return result(p.get_quality_transaction_plan(ready['plan_id']))['snapshot']
def execute(ready):return result(p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']))


def test_reported_result_body_and_provenance_are_not_native_engine_certification(env):
    ready=prepare();assert ready['state']=='ready',ready
    saved=snap(ready);body=saved['body'];detail=body['serviceDetails'][0];child=detail['violations'][0]
    assert set(body)=={'serviceDetails','comments'} and len(detail['violations'])==1
    assert child['dataPoint']==dict(instanceId=POINT,parent={'instanceId':PARENT})
    assert child['container']==dict(name='C',instanceId=CID) and child['collectedDataValue']=='10.1'
    assert child['ruleViolated']=='Site rules' and child['violationName']=='Beyond limits'
    assert [child[k] for k in ('lcl','cl','ucl')]==[0.0,5.0,10.0]
    assert 'operator-reported' in body['comments'] and ready['server_context']['reported_result']==REPORT
    assert not ready['server_context']['external_engine_result_verified']
    assert 'recipients' not in detail and 'holdReason' not in child
    env['client'].request.assert_not_awaited()


def test_context_can_read_rules_without_picking_a_violation_and_never_previews(env):
    out=result(v.get_spc_violation_context('C',POINT,'Diameter chart'))
    assert out['complete'] and out['rules']['selected_violation'] is None
    assert out['rules']['members'][0]['SPCViolationId']==VIOL and not out['native_spc_result_verified']
    assert out['rules']['members'][0]['ViolationName']=='Beyond limits'
    v.request.assert_not_awaited();assert not p.PLAN_DB.exists()


def test_submission_once_http200_stays_unknown_and_blocks_annotation_and_second_plan(env):
    ready=prepare();second=prepare()
    assert execute(ready)['state']=='unknown' and 'error' in execute(ready) and 'error' in execute(second)
    annotation=result(a.prepare_spc_annotation('C',POINT,'Diameter chart','note'))
    assert 'error' in result(p.execute_quality_transaction(annotation['plan_id'],annotation['plan_digest']))
    env['client'].request.assert_awaited_once()
    call=env['client'].request.await_args
    assert call.args[1].endswith('/api/RecordSPCViolation') and call.kwargs['params']=={'execute':'true'}
    assert call.kwargs['follow_redirects'] is False


@pytest.mark.parametrize('key,value',[('source_kind','native_engine'),('source_kind','local_weco'),('evidence_reference',' '),
    ('reported_value','11'),('reported_value',10.1),('reported_value','NaN'),('lcl','5'),('ucl','4'),
    ('ucl','10.1'),('ucl','11'),('center','1e2'),('lcl',False),('specification_limit','10'),('uom_id',HM),('uom_id',None),
    ('plotted_value','10.2'),('plotted_value',None)])
def test_source_limits_and_original_values_are_not_invented(env,key,value):
    report={**REPORT,key:value};assert 'error' in prepare(reported_result=report)
    v.request.assert_not_awaited();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('EmailNotification','someone@example.com'),('EMailMessage',{'InstanceId':RULE}),
    ('PutContainerOnHold',{'InstanceId':RULE}),('SetResourceStatus',{'InstanceId':RULE}),
    ('TriggerOCAPs',True),('TriggerOCAPs',None),('ChartVariable','derivedMean'),('SPCRules',None)])
def test_configured_notifications_and_dispositions_are_rejected_not_disabled(env,field,value):
    env['chart'][field]=value;assert 'error' in prepare();v.request.assert_not_awaited()


def test_unknown_notification_default_is_not_assumed_empty(env):
    del env['chart']['EmailNotification'];assert 'error' in prepare();v.request.assert_not_awaited()


@pytest.mark.parametrize('mode',['empty','overflow','duplicate','wrong_parent','frozen_member','unknown_member','frozen_rule','frozen_violation','not_member'])
def test_full_rule_membership_freeze_and_identity_required(env,mode):
    if mode=='empty':env['members']=[]
    elif mode=='overflow':env['members']*=101
    elif mode=='duplicate':env['members']*=2
    elif mode=='wrong_parent':env['members'][0]['ParentId']=HM
    elif mode=='frozen_member':env['members'][0]['IsFrozen']=True
    elif mode=='unknown_member':env['members'][0]['ChangeCount']=None
    elif mode=='frozen_rule':env['rule']['IsFrozen']=True
    elif mode=='frozen_violation':env['violation']['IsFrozen']=True
    else:env['members'][0]['SPCViolationId']=HM
    assert 'error' in prepare();v.request.assert_not_awaited()


def test_excluded_point_and_existing_same_violation_are_not_re_recorded(env):
    env['histories']=[{**annotation_history(),'Exclude':True}]
    assert 'error' in prepare();v.request.assert_not_awaited()
    env['histories']=[];env['violation_history']=[history()]
    assert 'error' in prepare(violation_name=' Beyond limits ');v.request.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('ParentId',HM),('HistoryId',HM),('ChartTxnId',HM),('SummaryTxnId',HM),
    ('MainlineTxnId',HM),('DataPointId',HM),('ContainerId',HM),('TxnType',8393),('ReversalStatus',2),('CustomSPCDataId','external')])
def test_violation_history_follows_real_three_level_parent_chain(env,field,value):
    env['violation_history']=[{**history(),field:value}]
    assert 'error' in prepare();v.request.assert_not_awaited()


@pytest.mark.parametrize('mode',['duplicates','overflow','missing_field'])
def test_incomplete_violation_history_never_becomes_complete(env,mode):
    env['violation_history']=[history()]* (101 if mode=='overflow' else 2 if mode=='duplicates' else 1)
    if mode=='missing_field':env['violation_history'][0].pop('Comments')
    assert 'error' in prepare();v.request.assert_not_awaited()


@pytest.mark.parametrize('field',['EMailMessage','Recipients','Message','FileName','FileDirectory','HoldReason','Resource','ResourceStatusCode',
    'ESigRequirement','AdditionalESigReqs','CDOTypeId','DataPoint','CL','CollectedDataValue'])
def test_preview_must_prove_passive_defaults_exact_types_and_payload(env,field):
    def hook(data):
        detail=data['ServiceDetails'][0];child=detail['Violations'][0]
        if field in ('ESigRequirement','AdditionalESigReqs'):data[field]={'InstanceId':HM}
        elif field in ('EMailMessage','Recipients','Message','FileName','FileDirectory'):detail[field]='unexpected'
        elif field in ('HoldReason','Resource','ResourceStatusCode'):child[field]={'InstanceId':HM}
        elif field=='CDOTypeId':child[field]=8400
        elif field=='DataPoint':child[field]['InstanceId']=HM
        elif field=='CL':child[field]=True
        else:child[field]='11'
    env['violation_hook']=hook;assert 'error' in prepare();assert not p.PLAN_DB.exists()


def test_missing_passive_default_and_unknown_signature_are_rejected(env):
    env['violation_hook']=lambda d:d['ServiceDetails'][0].pop('Recipients')
    assert 'error' in prepare()
    env['violation_hook']=lambda d:d.pop('ESigRequirement')
    assert 'error' in prepare()


@pytest.mark.parametrize('what',['source','rule','member','violation','history','chart'])
def test_late_changes_during_preview_or_after_ready_stop_submission(env,what):
    def mutate():
        if what=='source':env['source']['DataValue']='10.2'
        elif what=='rule':env['rule_count']+=1
        elif what=='member':env['members'][0]['ChangeCount']+=1
        elif what=='violation':env['violation_count']+=1
        elif what=='history':env['violation_history']=[history()]
        else:env['chart']['Description']='changed'
    ready=prepare();mutate();assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_change_during_nonexecuting_preview_invalidates_prepare(env):
    env['violation_hook']=lambda data:env['members'][0].update(ChangeCount=2)
    assert 'error' in prepare() and not p.PLAN_DB.exists()


@pytest.mark.parametrize('field,value',[('ViolationName',None),('ViolationName','wrong'),('ViolationIsFrozen',True),
    ('ViolationIsFrozen',None),('ViolationChangeCount',None),('ViolationChangeCount',2)])
def test_rule_membership_dangling_or_changed_violation_not_hidden(env,field,value):
    env['members'][0][field]=value
    assert 'error' in prepare();v.request.assert_not_awaited()


def test_missing_original_unit_is_not_assumed_to_match_report(env):
    env['source']['UOMId']=None
    assert 'error' in prepare();v.request.assert_not_awaited()


@pytest.mark.parametrize('turns',[['查看SPC违例历史'],['登记已报告的SPC越限结果'],['查询SPC当前规则清单','继续'],
    ['登记SPC越限报告','提交这个结果']])
def test_reported_violation_routing_and_followups(turns):
    tools=[{'function':{'name':name}} for name in TOOL_POLICIES];messages=[]
    for text in turns:messages.extend([dict(role='user',content=text),dict(role='assistant',content='routing fixture only')])
    chosen={t['function']['name'] for t in select_tools(tools,messages)}
    assert {'get_spc_violation_context','prepare_spc_violation'}<=chosen
    assert ('execute_quality_transaction' in chosen)==('登记' in turns[-1] or turns[-1].startswith('提交'))
    assert '不能从本地WECO、规格限或模型猜定' in ROUTING_INSTRUCTION


@pytest.mark.parametrize('field',['holdReason','resource','resourceStatusCode','customSPCDataId','lcl','failureAction','violationName'])
def test_transport_has_no_raw_override_or_second_action(env,field):
    saved=snap(prepare());saved['body']['serviceDetails'][0]['violations'][0][field]=True
    with pytest.raises(ValueError):p.transport(saved)


def test_transport_disallows_recipients_and_wrong_service(env):
    saved=snap(prepare());saved['body']['serviceDetails'][0]['recipients']='x@example.com'
    with pytest.raises(ValueError):p.transport(saved)
    saved=snap(prepare());saved['service']='InlineSPCTxn'
    with pytest.raises(ValueError):p.transport(saved)


def test_inspect_keeps_original_ids_and_unknown_even_with_matching_history(env):
    ready=prepare();execute(ready);env['violation_history']=[history()]
    out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert not out['verified_success'] and not out['state_changed'] and not out['retry_allowed']
    assert out['original_point_id']==POINT and len(out['current_context']['violation_history'])==1
    assert env['violation_calls'][-1]['InstanceID']==VIOL
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'
    env['client'].request.assert_awaited_once()


def test_inspect_never_follows_replacement_rule(env):
    ready=prepare();execute(ready);env['chart']['SPCRules']={'Name':'New','InstanceId':HM}
    assert 'error' in result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'


@pytest.mark.parametrize('mode',['timeout','cancel','http400'])
def test_failed_or_cancelled_attempts_stay_unknown_without_replay(env,mode):
    ready=prepare()
    if mode=='http400':env['client'].request.return_value.status_code=400
    else:env['client'].request.side_effect=TimeoutError('uncertain') if mode=='timeout' else asyncio.CancelledError()
    if mode=='cancel':
        with pytest.raises(asyncio.CancelledError):execute(ready)
    else:assert execute(ready)['state']=='unknown'
    assert result(p.get_quality_transaction_plan(ready['plan_id']))['state']=='unknown'
    assert 'error' in execute(ready);env['client'].request.assert_awaited_once()


def test_mcp_real_registration_closed_report_schema_and_confirmation(env):
    async def run():
        assert get_tool_func('prepare_spc_violation') is v.prepare_spc_violation
        assert TOOL_POLICIES['prepare_spc_violation'].effect=='read'
        async with Client(mcp) as client:
            schema=next(t.input_schema for t in await client.list_tools() if t.name=='prepare_spc_violation')
            assert schema['additionalProperties'] is False
            assert all(x.get('additionalProperties') is False for x in schema.get('$defs',{}).values() if x.get('type')=='object')
            reply=await client.call_tool('prepare_spc_violation',dict(container_name='C',data_point_history_id=POINT,
                spc_chart_name='Diameter chart',violation_name='Beyond limits',reported_result=REPORT))
            ready=json.loads(reply.content[0].text);assert ready['state']=='ready',ready
            calls=[{'function':{'name':'execute_quality_transaction','arguments':json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})}}]
            assert evaluate_mutations(MutationCounts(),calls).requires_approval
            assert 'operator_reported' in plan_preview(calls)
    asyncio.run(run())


def test_fixed_query_columns_exist_in_official_metadata():
    files=['spc-write-native-columns.json','spc-violation-native-history.json','spc-violation-native-links.json','spc-violation-native-rules.json','spc-violation-native-columns-extra.json']
    columns={}
    for f in files:
        for item in json.loads((Path('docs/quality-fixtures')/f).read_text(encoding='utf-8'))['results']:
            if item['query']['kind']=='column':columns[item['query']['owner']]={r['name'] for r in item['result'].get('records',[])}
    assert all(set(cols.split(','))<=columns[table] for table,cols in v.SCHEMA.items())


def test_history_sql_preserves_orphans_and_joins_only_the_real_parent_keys(monkeypatch):
    import sqlite3
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    tables=['SPCViolationHistoryDetail','SPCChartHistoryDetail','RecordSPCViolationHistory','HistoryMainline']
    for table in tables:db.execute('CREATE TABLE '+table+' ('+','.join(c+' TEXT' for c in v.SCHEMA[table].split(','))+')')
    def insert(table,data):
        keys=list(data);db.execute('INSERT INTO '+table+' ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',list(data.values()))
    original=history()
    insert('SPCViolationHistoryDetail',{k:original[k] for k in v.SCHEMA['SPCViolationHistoryDetail'].split(',')})
    async def sql(query,maximum):
        assert query.startswith('SELECT TOP 101 ')
        return [dict(r) for r in db.execute(query.replace('SELECT TOP 101 ','SELECT ',1)+' LIMIT 101')]
    monkeypatch.setattr(v.q,'sql_rows',sql)
    parents=[('SPCChartHistoryDetail',dict(RecordSPCChartViolationHistDId=DETAIL,RecordSPCViolationSummaryId=SUMMARY,
                 SPCChartDefId=CHART,SPCChartDefName='Diameter chart',TxnId=TXN)),
             ('RecordSPCViolationHistory',dict(RecordSPCViolationSummaryId=SUMMARY,HistoryMainlineId=HM,TxnId=TXN)),
             ('HistoryMainline',dict(HistoryMainlineId=HM,HistoryId=HID,TxnId=TXN,TxnType=8396,ReversalStatus=1,
                 TxnDateGMT='2026-10-29T00:00:00Z',Comments='operator-reported result'))]
    try:
        for table,data in parents:
            with pytest.raises(ValueError):asyncio.run(v.violation_history(POINT))
            insert(table,data)
        out=asyncio.run(v.violation_history(POINT))
        assert len(out)==1 and out[0]['RecordSPCViolationSummaryId']==SUMMARY
        db.execute("UPDATE RecordSPCViolationHistory SET TxnId=?",(HM,))
        with pytest.raises(ValueError):asyncio.run(v.violation_history(POINT))
    finally:db.close()
