import asyncio
import copy
import json
from decimal import Decimal
from unittest.mock import AsyncMock
import pytest

from tools import quantity_loss_complex as x, quality_quantity_loss as base, quality_plans as p

P='0000000000000001';A='0000000000000002';B='0000000000000003';U='0000000000000004';S='0000000000000005';W='0000000000000006'


def run(coro):return asyncio.run(coro)
def decode(coro):return json.loads(run(coro))


def row(cid,name,qty,parent='',count=0):
    return {'ContainerId':cid,'ContainerName':name,'CDOTypeId':1040,'Status':1,'CurrentHoldCount':0,'InRework':False,
        'SamplingLotId':'','ParentContainerId':parent,'ChildCount':count,'CarrierId':'','Qty':str(qty),'Qty2':'','UOMId':U,'UOM2Id':'',
        'ProductId':P,'SpecId':S,'WorkflowStepId':W,'ChangeCount':2,'CurrentStatusId':S,'StatusRecordId':S}


def scope(target=A):
    return {'root_id':P,'target_id':target,'containers':{P:row(P,'P',8,count=2),A:row(A,'A',5,P),B:row(B,'B',3,P)},
            'children':{P:[A,B],A:[],B:[]},'carriers':{}}


def args(**kwargs):
    return {'complex_scope':True,'container_name':'A','loss_reason_name':'LOSS','loss_quantity':'2','record_all_quantity':False,
        'close_when_zero':False,'adjust_throughput':False,'disassociate_when_closed':False,'generate_rollups':True,**kwargs}


def test_leaf_partial_rolls_up_ancestors_not_siblings():
    v=scope();x.validate_scope(v);effects,amount,_=x.projection(v,args())
    assert amount==Decimal(2)
    assert effects[A]['remaining']=='3' and effects[P]['remaining']=='6'
    assert effects[B]['remaining']=='3' and effects[B]['loss']=='0'
    assert effects[A]['direct_loss'] and not effects[P]['direct_loss']


def test_closed_target_keeps_historical_parent_but_not_active_count():
    effects,_,_=x.projection(scope(),args(loss_quantity=None,record_all_quantity=True,close_when_zero=True,disassociate_when_closed=True))
    assert effects[A]['parent_id']==P and effects[A]['status']==2
    assert effects[P]['child_count']==1 and effects[P]['remaining']=='3'
    assert effects[A]['disassociated'] is False


def test_whole_parent_closes_entire_subtree_with_consolidated_root_loss():
    effects,amount,_=x.projection(scope(P),args(loss_quantity=None,record_all_quantity=True,close_when_zero=True,disassociate_when_closed=True))
    assert amount==8 and all(v['remaining']=='0' and v['status']==2 for v in effects.values())
    assert effects[P]['direct_loss'] and not effects[A]['direct_loss']
    assert effects[A]['parent_id']==P and effects[P]['child_count']==0
    with pytest.raises(ValueError):x.projection(scope(P),args())


@pytest.mark.parametrize('patch',[
    {'CDOTypeId':7939},{'Status':2},{'CurrentHoldCount':1},{'InRework':True},{'SamplingLotId':P},
    {'UOM2Id':U},{'Qty2':'1'},{'UOMId':B},{'ProductId':B},{'SpecId':B},{'WorkflowStepId':B},
])
def test_mixed_or_unsafe_graph_rejected(patch):
    v=scope();v['containers'][A].update(patch)
    with pytest.raises(ValueError):x.validate_scope(v)


def test_parent_sum_requires_full_same_unit_tree():
    v=scope();v['containers'][P]['Qty']='9'
    with pytest.raises(ValueError):x.validate_scope(v)


def test_parent_sum_does_not_round_away_a_tiny_member():
    v=scope();v['containers'][P]['Qty']='1e12';v['containers'][A]['Qty']='1e12';v['containers'][B]['Qty']='1e-100'
    with pytest.raises(ValueError,match='父数量'):x.validate_scope(v)


def test_scope_detects_cycles_and_native_batch_without_write(monkeypatch):
    r=row(A,'A',5,A);monkeypatch.setattr(base,'record',AsyncMock(return_value=r))
    with pytest.raises(ValueError):run(x.scope_view(A))
    r.update(ParentContainerId='',CDOTypeId=7939)
    with pytest.raises(ValueError,match='Batch'):run(x.scope_view(A))


def test_children_query_excludes_closed_historical_links(monkeypatch):
    mock=AsyncMock(return_value=[]);monkeypatch.setattr(x.q,'sql_rows',mock)
    assert run(x.children(P))==[]
    assert 'AND Status=1' in mock.call_args.args[0]


def test_carrier_execution_is_gated_until_native_acceptance(monkeypatch):
    monkeypatch.setattr(x,'CARRIER_LOSS_EXECUTION_VERIFIED',False)
    v=scope();v['carriers']={U:{'members':[]}}
    monkeypatch.setattr(x.q,'container_identity',AsyncMock(return_value=({'name':'A','instanceId':A},{})))
    monkeypatch.setattr(x,'scope_view',AsyncMock(return_value=v))
    with pytest.raises(ValueError,match='载具'):run(x.build(args(),'MCP-QUAL:'+'a'*32))


def carrier_scope():
    v=scope();v['containers'].pop(B);v['containers'][P].update(Qty='5',ChildCount=1,ProductId='',UOMId='',CarrierId=U)
    v['containers'][A]['CarrierId']=U;v['children'].pop(B);v['children'][P]=[A]
    v['carriers']={U:{'tracking_identity':{'ResourceId':U,'isTrackingContainerId':P,'isCarrier':'true','isAssociatedCarrierId':U},
        'members':[copy.deepcopy(r) for r in v['containers'].values()]}}
    return v


def test_native_carrier_tracking_root_requires_bidirectional_proof_and_real_member_units():
    v=carrier_scope();x.validate_scope(v)
    broken=copy.deepcopy(v);broken['carriers'][U].pop('tracking_identity')
    with pytest.raises(ValueError):x.validate_scope(broken)
    broken=copy.deepcopy(v);broken['containers'][A]['UOMId']=''
    with pytest.raises(ValueError):x.validate_scope(broken)
    with pytest.raises(ValueError):x.validate_scope({**v,'target_id':P})


def test_carrier_zero_projection_includes_shared_current_status_root():
    v=carrier_scope();effects,_,_=x.projection(v,args(loss_quantity=None,record_all_quantity=True,close_when_zero=True))
    assert effects[A]['carrier_detached'] and effects[P]['carrier_detached']
    assert effects[P]['remaining']=='0' and effects[P]['status']==1
    assert effects[A]['parent_id']==P and effects[A]['status']==2


def test_plan_scope_changes_and_flags_cannot_be_retargeted():
    v=scope();a=args();effects,amount,number=x.projection(v,a);marker='MCP-QUAL:'+'a'*32
    reason={'name':'LOSS','instanceId':S}
    body={'container':{'name':'A','instanceId':A},'closeWhenZero':False,'disassociateWhenClosed':False,
        'generateRollups':True,'recordAllQtySummaryOnly':False,'rollupReason':reason,'comments':marker,
        'serviceDetails':[{'@odata.type':'#shopfloor.LossDetails','changeQtyType':2,'recordAllQty':False,'adjustThruput':False,'reasonCode':reason,'qty':number}]}
    snap={'complex_version':3,'kind':'quantity_loss','service':'ChangeQty','target_id':A,'name':'A','expected_scope':effects,
          'expected_loss_quantity':str(amount),'before':{'scope':v},'reason':reason,'body':body,'arguments':a,'marker':marker}
    x.validate_transport(snap)
    changed=copy.deepcopy(snap);changed['body']['generateRollups']=False
    with pytest.raises(ValueError):x.validate_transport(changed)
    changed=copy.deepcopy(snap);changed['expected_scope'][B]['loss']='1'
    with pytest.raises(ValueError):x.validate_transport(changed)


def test_current_scope_proves_all_cascade_changes_and_preserves_peers(monkeypatch):
    v=scope(P);a=args(loss_quantity=None,record_all_quantity=True,close_when_zero=True,disassociate_when_closed=True)
    effects,_,_=x.projection(v,a);snap={'before':{'scope':v},'expected_scope':effects,'arguments':a}
    current=copy.deepcopy(v['containers'])
    for r in current.values():r.update(Qty='0',Status=2,ChildCount=0,ChangeCount=3)
    async def read(cid):return copy.deepcopy(current[cid])
    monkeypatch.setattr(base,'record',read)
    monkeypatch.setattr(x.q,'sql_rows',AsyncMock(side_effect=lambda sql,*a:[{'ContainerId':next(cid for cid in current if cid in sql),'LastRevTxnId':'','CurrentThruputQty':'','ThruputAdjustmentQty':''}]))
    checks,_=run(x.current_evidence(snap,'4000000000000001'));assert all(checks.values())
    current[A]['ChangeCount']=4
    checks,_=run(x.current_evidence(snap,'4000000000000001'));assert not all(checks.values())


def test_old_move_lastrev_token_is_not_a_changeqty_failure(monkeypatch):
    v=scope();a=args();effects,_,_=x.projection(v,a)
    current=copy.deepcopy(v['containers'])
    current[P].update(Qty='6',ChangeCount=3);current[A].update(Qty='3',ChangeCount=3)
    monkeypatch.setattr(base,'record',AsyncMock(side_effect=lambda cid:copy.deepcopy(current[cid])))
    monkeypatch.setattr(x.q,'sql_rows',AsyncMock(side_effect=lambda sql,*a:[{'ContainerId':next(cid for cid in current if cid in sql),'LastRevTxnId':'4000000000000099','CurrentThruputQty':'','ThruputAdjustmentQty':''}]))
    checks,_=run(x.current_evidence({'before':{'scope':v},'expected_scope':effects,'arguments':a},'4000000000000001'))
    assert all(checks.values())
    current[A]['ChangeCount']=4
    checks,_=run(x.current_evidence({'before':{'scope':v},'expected_scope':effects,'arguments':a},'4000000000000001'))
    assert not all(checks.values())
    current[A].update(ChangeCount=3,Qty='1')
    checks,_=run(x.current_evidence({'before':{'scope':v},'expected_scope':effects,'arguments':a},'4000000000000001'));assert not all(checks.values())


def test_legacy_projection_is_preserved_for_validation_not_rewritten():
    v=scope();a=args(loss_quantity=None,record_all_quantity=True,close_when_zero=True,disassociate_when_closed=True)
    original=x.projection(v,a,semantics=1)[0];native=x.projection(v,a,semantics=3)[0]
    assert original[A]['parent_id']=='' and native[A]['parent_id']==P
    assert original[A]['disassociated'] and not native[A]['disassociated']


def native_history_case():
    v=scope();a=args();effects,_,_=x.projection(v,a);rootid='0000000000000010';txn='4000000000000001';summaryid='0000000000000011'
    snap={'target_id':A,'name':'A','reason':{'instanceId':S},'expected_loss_quantity':'2','arguments':a,'expected_scope':effects,
          'before':{'scope':v,'container':v['containers'][A],'container_status':{'Operation':{'InstanceId':B}}}}
    root={'HistoryMainlineId':rootid,'HistoryId':P,'ContainerId':A,'ContainerName':'A','ContainerChangeCount':'2','TxnId':txn,
          'IntegrationTxnId':rootid,'TxnTypeName':'ChangeQty','TxnType':'4000','ReversalStatus':'1','Qty':'5','ToQty':'3','Qty2':'','ToQty2':'',
          'ProductId':P,'SpecId':S,'OperationId':B,'WorkflowStepId':W,'InRework':'false','TxnDateGMT':'1970-01-01T00:00:01'}
    summary={'QtyHistoryId':summaryid,'HistoryMainlineId':rootid,'HistoryId':A,'TxnId':txn,'IntegrationTxnId':rootid,
             'CDOTypeId':'5080','Qty':'-2','Qty2':'','CloseWhenZero':'false','ThruputSummary':'false'}
    direct={'QtyHistoryDetailsId':'0000000000000012','QtyHistoryId':summaryid,'ContainerId':A,'HistoryId':P,'TxnId':txn,'IntegrationTxnId':rootid,
            'CDOTypeId':'6932','ChangeQtyType':'2','QtyMultiplier':'-1','Qty':'2','Qty2':'','UOMId':U,'UOM2Id':'','ReasonCodeId':S,
            'ProductId':P,'OperationId':B,'ChargeToStepId':W,'AdjustThruput':'false','CountsAgainstProduction':'true','RecordAllQty':'false',
            'CloseWhenZero':'false','ThisContainerClosed':'false','ThisContainerDisassociated':'false','IsRollup':'false'}
    rollup={**direct,'QtyHistoryDetailsId':'0000000000000013','ContainerId':P,'CDOTypeId':'5020','ChangeQtyType':'','QtyMultiplier':'1','Qty':'-2',
            'IntegrationTxnId':'','ChargeToStepId':'','CountsAgainstProduction':'false','IsRollup':'true'}
    evidence={'mainlines':[root],'summaries':[summary],'details':[rollup,direct]}
    attempt={'attempt_id':'a'*32,'started':1,'finished':2}
    return snap,attempt,root,evidence


def test_real_hierarchical_history_has_distinct_root_partition_and_target_summary():
    snap,attempt,root,evidence=native_history_case()
    assert root['HistoryId']!=snap['target_id'] and evidence['summaries'][0]['HistoryId']==snap['target_id']
    assert all(x.compare(snap,attempt,root,evidence).values())


@pytest.mark.parametrize('part,index,key,value',[
    ('details',0,'Qty','2'),('details',0,'IntegrationTxnId','0000000000000010'),
    ('details',1,'CountsAgainstProduction','false'),('details',1,'ReasonCodeId',B),
    ('details',1,'ProductId',B),('details',1,'RecordAllQty','true'),('details',1,'HistoryId',A),
    ('summaries',0,'Qty','-3'),('summaries',0,'CloseWhenZero','true'),('summaries',0,'CDOTypeId','5000'),
    ('mainlines',0,'ContainerChangeCount','3'),('mainlines',0,'WorkflowStepId',S),
])
def test_native_history_rejects_wrong_sign_flags_identity_and_parent_chains(part,index,key,value):
    snap,attempt,root,evidence=native_history_case();evidence[part][index][key]=value
    assert not all(x.compare(snap,attempt,root,evidence).values())


def test_missing_rollup_or_unplanned_detail_cannot_be_accepted():
    snap,attempt,root,evidence=native_history_case();evidence['details']=evidence['details'][1:]
    assert not all(x.compare(snap,attempt,root,evidence).values())
    snap,attempt,root,evidence=native_history_case();extra=copy.deepcopy(evidence['details'][0]);extra.update(ContainerId=B,Qty='0');evidence['details'].append(extra)
    assert not all(x.compare(snap,attempt,root,evidence).values())


def test_related_claims_block_same_parent_even_with_different_targets():
    def plan(target,related):return {'kind':'quantity_loss','scope_key':'quantity-loss:'+target,'environment':p.environment(),
        'resource_reads':[],'resource_writes':['runtime:container:id:'+cid for cid in related]}
    assert p.conflicts(plan(A,[A,P]),plan(B,[B,P]))
    assert not p.conflicts(plan(A,[A,P]),plan(B,[B]))
