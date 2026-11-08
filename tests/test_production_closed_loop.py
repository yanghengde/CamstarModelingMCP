import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from tools import production_reporting as p, production_records as r
from tools import production_verification as v, production_station as s, production_labor as l
from tools import collection_submission as c

CID,STEP,UNIT,OP,MID,TXN,ORDER,TARGET,PATH,WORKFLOW=[f'{n:016x}' for n in range(1,11)]
MARKER='MCP-PROD:'+'a'*32

def run(coro):return json.loads(asyncio.run(coro))

@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'production.sqlite3')
    monkeypatch.setattr(r,'LEDGER_DB',tmp_path/'records.sqlite3')
    monkeypatch.setattr(c,'PLAN_DB',tmp_path/'collection.sqlite3')
    monkeypatch.setattr(p,'ensure_no_unresolved_quality',lambda cid:None)

def receipt_plan():
    args={'comments':"operator's report",'_receipt_marker':MARKER}
    snap={'service':'Thruput','container_id':CID,'qty':3,
        'environment':p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),
        'query_environment':p.CAMSTAR_QUERY_BASE_URL.rstrip('/'),'receipt_marker':MARKER,
        'body':{'container':{'name':'C','instanceId':CID},'comments':p.receipt_comments(args)},
        'status':{'Step':{'InstanceId':STEP},'Container':{'Name':'C'},'MfgOrder':{'Name':'MO','InstanceId':ORDER}},
        'counters':{'StepPass':'1','UOMId':UNIT,'CurrentStatusId':MID},
        'operation':{'InstanceID':OP},'history_baseline':[]}
    sig=p.digest(snap)
    with p.db_connect() as db:
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',('P',sig,json.dumps({'snapshot':snap,'arguments':args}),
            'unknown',p.time.time(),json.dumps({'submitted':True})))
    h={'HistoryMainlineId':MID,'HistoryId':CID,'TxnId':TXN,'ContainerId':CID,'ContainerName':'C',
        'TxnTypeName':'Thruput','TxnServiceName':'Thruput','TxnParentTypeName':'','WorkflowStepId':STEP,
        'StepPass':'1','ReversalStatus':'1','Comments':snap['body']['comments']}
    t={'ThruputHistoryId':TARGET,'HistoryMainlineId':MID,'HistoryId':CID,'TxnId':TXN,'ContainerId':CID,
        'OperationId':OP,'UOMId':UNIT,'Qty':'3'}
    return sig,snap,h,t

def test_committed_native_receipt_unlocks_and_is_usable_as_report_source(monkeypatch):
    sig,snap,h,t=receipt_plan()
    reads=AsyncMock(side_effect=[[h],[t],[h],[t]])
    monkeypatch.setattr(p,'query_rows',reads)
    write=AsyncMock();monkeypatch.setattr(p,'request',write)
    result=run(v.verify_production_transaction('P',sig))
    assert result['transaction_verified'] is True and result['state']=='history_committed'
    assert result['replay_allowed'] is False
    p.ensure_no_unresolved_throughput(CID)
    assert r.reviewed_source('production','P',sig)==snap
    assert "operator''s report" in reads.await_args_list[0].args[0]
    assert run(p.execute_container_throughput('P',sig))['submitted'] is False
    write.assert_not_awaited()

@pytest.mark.parametrize('field,value',[('Qty','4'),('UOMId',OP),('TxnId',OP),('HistoryId',OP),('OperationId',UNIT)])
def test_wrong_native_detail_cannot_clear_unknown(monkeypatch,field,value):
    sig,_,h,t=receipt_plan();t[field]=value
    monkeypatch.setattr(p,'query_rows',AsyncMock(side_effect=[[h],[t]]))
    assert run(v.verify_production_transaction('P',sig))['transaction_verified'] is False
    with pytest.raises(ValueError,match='未核实'):p.ensure_no_unresolved_throughput(CID)

@pytest.mark.parametrize('field,value',[('ReversalStatus','2'),('StepPass','2'),('WorkflowStepId',OP),
    ('TxnParentTypeName','MoveOut'),('Comments','same values elsewhere')])
def test_header_semantics_are_part_of_native_proof(monkeypatch,field,value):
    sig,_,h,_=receipt_plan();h[field]=value
    monkeypatch.setattr(p,'query_rows',AsyncMock(return_value=[h]))
    assert run(v.verify_production_transaction('P',sig))['transaction_verified'] is False

def test_receipt_never_infers_failure_or_zero_from_empty_history(monkeypatch):
    sig,_,_,_=receipt_plan();monkeypatch.setattr(p,'query_rows',AsyncMock(return_value=[]))
    assert run(v.verify_production_transaction('P',sig))['transaction_verified'] is False
    with p.db_connect() as db:assert db.execute('SELECT state FROM plans').fetchone()[0]=='unknown'

def test_changed_history_during_verification_keeps_fence(monkeypatch):
    sig,_,h,t=receipt_plan();changed={**t,'Qty':'4'}
    monkeypatch.setattr(p,'query_rows',AsyncMock(side_effect=[[h],[t],[h],[changed]]))
    assert run(v.verify_production_transaction('P',sig))['transaction_verified'] is False

@pytest.mark.parametrize('empty',[None,''])
def test_new_container_remaining_is_read_from_mes_without_guessing_zero(monkeypatch,empty):
    counters={'Qty':'6','CurrentThruputQty':empty,'ThruputAdjustmentQty':empty}
    write=AsyncMock(return_value=json.dumps({'data':{'Container':{'Name':'C','InstanceId':CID},'QtyRemaining':'6'}}))
    monkeypatch.setattr(p,'request',write)
    remaining,proof=asyncio.run(p.remaining_quantity(counters,CID,'C'))
    assert remaining==6 and proof['remaining_qty']==6
    assert counters['CurrentThruputQty']==empty
    assert write.await_args.kwargs['params']['execute']=='false'
    assert 'qty' not in write.await_args.kwargs['body']

def test_missing_remaining_evidence_does_not_turn_into_zero(monkeypatch):
    monkeypatch.setattr(p,'request',AsyncMock(return_value=json.dumps({'data':{'Container':{'Name':'C','InstanceId':CID}}})))
    with pytest.raises(ValueError):asyncio.run(p.remaining_quantity({'Qty':6,'CurrentThruputQty':'','ThruputAdjustmentQty':''},CID,'C'))

def station_context(monkeypatch,processed=6):
    status={'Container':{'Name':'C','InstanceId':CID},'Status':1,'InProcess':True,
        'Operation':{'Name':'OP'},'ContainerLevel':{'Name':'LEVEL'},'Workflow':{'Name':'W','Revision':'1'},
        'Step':{'Name':'Process','InstanceId':STEP},'MfgOrder':{'InstanceId':ORDER}}
    row={'ContainerId':CID,'ContainerName':'C','Qty':'6','CurrentThruputQty':str(processed),
        'ThruputAdjustmentQty':'0','ChildCount':'0','ParentContainerId':'','CurrentHoldCount':'0',
        'ProductionComplete':'false','InRework':'false','CarrierId':'','WorkflowStepId':STEP,'UOMId':UNIT,
        'CurrentStepPass':'1','MfgOrderId':ORDER}
    async def resolve(self,entity,ref,revisioned=True):
        return {'ContainerLevels':{'Name':'LEVEL','AllowMove':True},'Operations':{'Name':'OP','UseQueue':True},
            'Workflows':{'Name':'W','Revision':'1','InstanceID':WORKFLOW}}[entity]
    async def query(sql):
        if 'FROM Container ' in sql:return [copy.deepcopy(row)]
        if 'FROM Path ' in sql:return [{'PathId':PATH,'PathName':'Next','FromStepId':STEP,'ToStepId':TARGET,'ChangeCount':'1'}]
        ident,name=(TARGET,'Finished') if "='"+TARGET+"'" in sql else (STEP,'Process')
        return [{'WorkflowStepId':ident,'WorkflowStepName':name,'WorkflowId':WORKFLOW,'ChangeCount':'1'}]
    monkeypatch.setattr(p,'container_state',AsyncMock(return_value=(status,CID)))
    monkeypatch.setattr(p.ReadContext,'resolve',resolve)
    monkeypatch.setattr(p.ReadContext,'get',AsyncMock(return_value={'Steps':[
        {'Name':'Process','IsLastStep':False},{'Name':'Finished','IsLastStep':True}]}))
    monkeypatch.setattr(p,'query_rows',query)
    return status,row

def test_production_completion_uses_real_terminal_path_without_forging_dates(monkeypatch):
    station_context(monkeypatch)
    result=run(s.prepare_production_station_transition('C','complete',path_name='Next'))
    assert result['state']=='ready' and result['service']=='MoveStd'
    body=result['request']
    assert body['close'] is False and body['path']['instanceId']==PATH
    assert not {'qty','thruputAllQty','toStep','productionCompleteDateGMT','orderStatus'} & body.keys()
    from core.movement_policy import movement_targets,movement_call_blocked,movement_failed
    args={k:result[k] for k in ('plan_id','plan_digest')}
    assert movement_targets('execute_production_station_transition',args)=={'c',CID}
    assert movement_call_blocked({'function':{'name':'execute_production_station_transition','arguments':json.dumps(args)}},{CID})
    assert movement_failed(json.dumps({'state':'unknown','response':'❌ HTTP 400 Error'}))

def test_unreported_remaining_units_cannot_be_moved_to_completion(monkeypatch):
    station_context(monkeypatch,processed=3)
    write=AsyncMock();monkeypatch.setattr(p,'request',write)
    assert run(s.prepare_production_station_transition('C','complete',path_name='Next'))['executable'] is False
    write.assert_not_awaited()

def test_exit_cannot_hide_native_production_completion(monkeypatch):
    station_context(monkeypatch)
    assert run(s.prepare_production_station_transition('C','exit',path_name='Next'))['executable'] is False

def test_pending_labor_collection_blocks_other_reporting_or_moves():
    snap={'environment':p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),'body':{'container':{'instanceId':CID}}}
    with c.db_connect() as db:db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',('L','hash',json.dumps({'snapshot':snap}),'unknown',0))
    with pytest.raises(ValueError,match='工时'):p.ensure_no_unresolved_collection(CID)

def test_labor_native_values_keep_employee_actual_minutes_and_timezones():
    record={'actual_minutes':'45','employee':{'name':'Worker','instance_id':OP},'order':{'name':'MO'},
        'start_time':'2026-11-08T08:00:00+08:00','end_time':'2026-11-08T09:00:00+08:00'}
    values=l.values_for('L',record)
    assert values['ActualMinutes']==45 and values['EmployeeId']==OP and len(values)==7
    assert values['StartTime'].endswith('+08:00') and 'TaskTime' not in values

def linked_labor(*,state='verified_success',wrong_body=False):
    record={'actual_minutes':'45','employee':{'name':'Worker','instance_id':OP},
        'order':{'name':'MO','instance_id':ORDER},
        'context':{'container_id':CID,'container_name':'C','order_id':ORDER},
        'start_time':'2026-11-08T08:00:00+08:00','end_time':'2026-11-08T09:00:00+08:00'}
    result=r.save('labor','LABOR',{},record)
    values=l.values_for('LABOR',record)
    details=[{'dataPoint':{'name':n},'dataValue':c.serialize({'value':value,'data_type':l.POINT_TYPES[n]})}
        for n,value in values.items()]
    if wrong_body:next(d for d in details if d['dataPoint']['name']=='ActualMinutes')['dataValue']='46.0'
    snap={'environment':p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),
        'body':{'container':{'instanceId':CID},'parametricData':{'dataPointDetails':details}}}
    sig=c.digest(snap)
    with c.db_connect() as db:
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',('COL',sig,
            json.dumps({'snapshot':snap,'arguments':{'values':values}}),state,p.time.time(),json.dumps({'verified_success':True})))
    with l.db_connect() as db:
        db.execute('INSERT INTO labor_exports VALUES (?,?,?,?,?)',(r.environment(),result['record_id'],'COL',sig,p.time.time()))
    return result['record_id']

def test_mes_labor_export_is_a_copy_of_minutes_not_additional_person_time(monkeypatch):
    rid=linked_labor()
    assert run(l.get_production_labor_submission(rid))['mes_persisted'] is True
    monkeypatch.setattr(r,'order_context',AsyncMock(return_value={'name':'MO','instance_id':ORDER}))
    monkeypatch.setattr(p,'query_rows',AsyncMock(return_value=[{'IntervalCount':'0','ElapsedMicroseconds':'0','IncludedCount':'0','ExcludedCount':'0'}]))
    result=run(r.get_production_worktime('MO'))
    assert result['manual']['total_minutes']=='45'
    assert result['manual']['mes_storage']['verified_minutes']=='45'
    assert result['combined_hours'] is None

def test_labor_collection_body_cannot_disagree_with_immutable_declared_minutes():
    rid=linked_labor(wrong_body=True)
    result=run(l.get_production_labor_submission(rid))
    assert result['mes_persisted'] is False and '请求点值' in result['error']

def test_all_new_tools_are_registered():
    from tools import get_tool_func,mcp
    from core.tool_catalog import TOOL_POLICIES
    names={t.name for t in asyncio.run(mcp.list_tools())}
    for name in ('verify_production_transaction','prepare_production_station_transition','execute_production_station_transition',
            'prepare_production_labor_submission','execute_production_labor_submission',
            'verify_production_labor_submission','get_production_labor_submission'):
        assert name in names and name in TOOL_POLICIES and get_tool_func(name)

@pytest.mark.parametrize('message,tool',[
    ('请把人工工时写入MES','execute_production_labor_submission'),
    ('请核实人工工时落账','verify_production_labor_submission'),
    ('请办理整单完工','execute_production_station_transition')])
def test_natural_language_reaches_new_production_actions(message,tool):
    from core.tool_catalog import TOOL_POLICIES
    from agent.tool_routing import select_tools
    tools=[{'function':{'name':n}} for n in TOOL_POLICIES]
    selected=select_tools(tools,[{'role':'user','content':message}])
    assert tool in {t['function']['name'] for t in selected}
