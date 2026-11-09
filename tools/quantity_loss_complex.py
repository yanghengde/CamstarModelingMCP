"""Explicit hierarchy/carrier loss plans; native Batch ChangeQty remains prohibited."""
import copy
import re
from decimal import Decimal, localcontext
from typing import Literal

from tools import mcp, quality as q, quality_plans as p, quality_inspection as i
from tools import quality_quantity_loss as base, quality_lot_execution as execution
from tools.modeling_details import raw_json
from tools.process_context import ReadContext

MAX_SCOPE=8
CARRIER_LOSS_EXECUTION_VERIFIED=True  # Native isolated partial/full + shared-status detachment: 2026-11-09.


async def children(cid):
    rows=await q.sql_rows('SELECT TOP 9 ContainerId,ContainerName FROM Container WHERE ParentContainerId='+q.literal(cid)+' AND Status=1 ORDER BY ContainerId',9)
    if len(rows)>MAX_SCOPE or any(not q.valid_id(row.get('ContainerId')) for row in rows): raise ValueError('数量损失层级超过8个容器或身份不完整。')
    return rows


async def carrier_view(cid):
    carrier=await ReadContext(2).resolve('Carriers',{'InstanceID':cid},revisioned=False)
    if carrier.get('IsFrozen') is not False or carrier.get('InstanceLocked') is True: raise ValueError('载具冻结/锁定状态不允许当前数量处置。')
    members=await q.sql_rows('SELECT TOP 9 c.ContainerId,c.ContainerName FROM Container c JOIN CurrentStatus s '
        'ON c.CurrentStatusId=s.CurrentStatusId WHERE s.CarrierId='+q.literal(cid)+' ORDER BY c.ContainerId',9)
    if len(members)>MAX_SCOPE: raise ValueError('载具成员超过8个，不能按片段判断影响。')
    records=[]
    for member in members:
        row=await base.record(member['ContainerId'])
        if row is None or row['ContainerName']!=member['ContainerName'] or row['CarrierId']!=cid: raise ValueError('载具成员读取期间变化。')
        records.append(row)
    links=await q.sql_rows('SELECT r.ResourceId,p.isTrackingContainerId,c.isCarrier,c.isAssociatedCarrierId '
        'FROM ResourceDef r JOIN ProductionStatus p ON p.ProductionStatusId=r.ProductionStatusId '
        'LEFT JOIN Container c ON c.ContainerId=p.isTrackingContainerId WHERE r.ResourceId='+q.literal(cid),1)
    if len(links)!=1 or links[0]['ResourceId']!=cid: raise ValueError('载具生产状态身份不完整。')
    link=links[0]
    if link['isTrackingContainerId'] and (link['isCarrier']!='true' or link['isAssociatedCarrierId']!=cid):
        raise ValueError('载具跟踪容器的双向身份不一致。')
    return {'configuration':carrier,'members':records,'tracking_identity':link}


async def scope_view(target):
    row=await base.record(target)
    if row is None: raise ValueError('原容器记录缺失。')
    if row['CDOTypeId']==7939: raise ValueError('原生ChangeQty_Validate明确禁止Batch类型7939；不改变类型、不直接改数据库、不以SamplingLot或循环成员扣量代替。')
    root=row;seen={target}
    while root['ParentContainerId']:
        pid=root['ParentContainerId']
        if not q.valid_id(pid) or pid in seen or len(seen)>=MAX_SCOPE: raise ValueError('父链循环/身份无效/超过8个。')
        seen.add(pid); root=await base.record(pid)
        if root is None: raise ValueError('父容器缺失。')
    records={}; adjacency={}; queue=[root['ContainerId']]
    while queue:
        cid=queue.pop(0)
        if cid in records: raise ValueError('当前层级存在重复/循环。')
        if len(records)>=MAX_SCOPE: raise ValueError('完整层级超过8个容器。')
        current=await base.record(cid)
        if current is None: raise ValueError('层级容器缺失。')
        records[cid]=current; nodes=await children(cid); adjacency[cid]=[x['ContainerId'] for x in nodes]
        if len(nodes)!=current['ChildCount']: raise ValueError('子容器计数与完整成员不一致。')
        queue.extend(adjacency[cid])
    if target not in records: raise ValueError('目标不在已核实的完整层级中。')
    carriers={}
    for current in records.values():
        cid=current['CarrierId']
        if cid and cid not in carriers: carriers[cid]=await carrier_view(cid)
    if len(set(records)|{r['ContainerId'] for c in carriers.values() for r in c['members']})>MAX_SCOPE: raise ValueError('层级和载具的完整影响范围超过8个容器。')
    return {'root_id':root['ContainerId'],'target_id':target,'containers':records,'children':adjacency,'carriers':carriers}


def validate_scope(view):
    target=view['containers'][view['target_id']]
    for carrier_id,carrier in view['carriers'].items():
        link=carrier.get('tracking_identity') or {}
        if link.get('ResourceId')!=carrier_id or link.get('isCarrier')!='true' or link.get('isAssociatedCarrierId')!=carrier_id or link.get('isTrackingContainerId') not in view['containers']:
            raise ValueError('当前载具执行限已双向核实、包含完整活动层级的原生跟踪载具。')
    tracking={c['tracking_identity']['isTrackingContainerId'] for c in view['carriers'].values() if c.get('tracking_identity') and c['tracking_identity'].get('isTrackingContainerId')}
    if view['target_id'] in tracking: raise ValueError('载具跟踪根仅用于汇总；数量损失须选择明确的实物容器，不能把空产品/单位根当物料。')
    for row in view['containers'].values():
        if row['CDOTypeId']!=1040: raise ValueError('当前层级含Batch或未验收子类型；禁止混用基础Container事务。')
        if row['Status']!=1 or row['CurrentHoldCount'] or row['InRework'] or row['SamplingLotId']: raise ValueError('范围内全部容器须Active、无冻结/返工/抽样批。')
        if row['UOM2Id'] or row['Qty2'] not in (None,'') and i.numeric(str(row['Qty2']),'第二数量')!=0: raise ValueError('复杂层级/载具入口本轮限单单位；双单位独立基础容器使用原已验收入口。')
        # Native carrier tracking roots have no product/UOM. They are structural
        # ancestors, proven by both resource->tracking and tracking->resource IDs;
        # they cannot be the loss target or justify mixed real product members.
        structural=row['ContainerId'] in tracking and row['ContainerId']!=view['target_id'] and row['ChildCount']>0
        fields=('SpecId','WorkflowStepId') if structural else ('ProductId','UOMId','SpecId','WorkflowStepId')
        if any(row[k]!=target[k] for k in fields): raise ValueError('层级各实物容器须同产品、主单位及当前步骤，不猜测混合产品/单位的汇总规则。')
        if structural:
            if row['ProductId'] not in ('',target['ProductId']) or row['UOMId'] not in ('',target['UOMId']): raise ValueError('载具跟踪根存在不同产品/单位。')
        elif not q.valid_id(row['UOMId']) or not q.valid_id(row['ProductId']): raise ValueError('范围内产品/单位身份不完整。')
        quantity=i.numeric(str(row['Qty']),'范围内数量')
        if quantity<0 or quantity>Decimal('1e12') or Decimal(str(float(quantity)))!=quantity: raise ValueError('范围内数量须非负、<=1e12且可无损表示，不能只校验叶容器。')
        child_ids=view['children'][row['ContainerId']]
        if child_ids:
            with localcontext() as ctx:
                ctx.prec=640
                total=sum((i.numeric(str(view['containers'][c]['Qty']),'子容器数量') for c in child_ids),Decimal(0))
            if total!=i.numeric(str(row['Qty']),'父容器数量'): raise ValueError('父数量不等于同单位完整子数量之和，拒绝猜测自有量或计数层级。')
    if i.numeric(str(target['Qty']),'目标数量')<=0: raise ValueError('目标当前数量须为正。')


def projection(view,args,legacy=False,semantics=3):
    if legacy: semantics=1
    target=view['target_id']; rows=view['containers']; target_row=rows[target]
    amount,remaining,number=base.quantity_options(args,target_row)
    if view['children'][target] and not args['record_all_quantity']: raise ValueError('多层父容器仅支持明确整棵目标子树全量损失；部分损失须明确选择实际叶容器。')
    affected=set(); queue=[target]
    while queue:
        cid=queue.pop(0);affected.add(cid);queue.extend(view['children'][cid])
    losses={cid:(i.numeric(str(rows[cid]['Qty']),'原量') if args['record_all_quantity'] else amount) for cid in affected}
    ancestor=target_row['ParentContainerId']
    while ancestor:
        losses[ancestor]=amount;ancestor=rows[ancestor]['ParentContainerId']
    detach_statuses={rows[cid]['CurrentStatusId'] for cid in affected if rows[cid]['CarrierId'] and
        i.numeric(str(rows[cid]['Qty']),'原量')==losses[cid]}
    expected={}
    for cid,row in rows.items():
        with localcontext() as ctx:
            ctx.prec=640
            after=i.numeric(str(row['Qty']),'原量')-losses.get(cid,Decimal(0))
        if after<0: raise ValueError('损失投影超过层级余额。')
        if Decimal(str(float(after)))!=after: raise ValueError('父子数量投影不能无损传输，拒绝会被父级舍入忽略的扣量。')
        closed=cid in affected and after==0 and args['close_when_zero']
        # This deployment retains historical ParentId on closed targets and
        # descendants; only active ChildCount loses the closed members.
        detached=closed and (semantics==1 or semantics==2 and cid!=target) and bool(row['ParentContainerId']) and args['disassociate_when_closed']
        carrier_detached=bool(row['CarrierId']) and row['CurrentStatusId'] in detach_statuses
        expected[cid]={'remaining':str(after),'loss':str(losses.get(cid,Decimal(0))), 'status':2 if closed else 1,
            'parent_id':'' if detached else row['ParentContainerId'],'carrier_id':'' if carrier_detached else row['CarrierId'],
            'closed':closed,'disassociated':detached,'carrier_detached':carrier_detached,'direct_loss':cid==target if semantics>=3 else cid in affected and not view['children'][cid]}
    for cid,item in expected.items(): item['child_count']=sum(x['parent_id']==cid and (semantics==1 or x['status']==1) for x in expected.values())
    return expected,amount,number


async def build(args,marker):
    for field in ('disassociate_when_closed','generate_rollups','close_when_zero','adjust_throughput','record_all_quantity'):
        if type(args.get(field)) is not bool: raise ValueError(field+'必须明确布尔值。')
    if args['generate_rollups'] is not True: raise ValueError('复杂入口须明确generate_rollups=true，保留完整层级数量历史。')
    ref,status=await q.container_identity(args['container_name'],args.get('container_instance_id'))
    view=await scope_view(ref['instanceId']);validate_scope(view)
    if view['carriers'] and not CARRIER_LOSS_EXECUTION_VERIFIED:
        raise ValueError('载具损失执行门禁已关闭；仅可读取范围，不绕过门禁提交。')
    if not any(row['ParentContainerId'] or row['ChildCount'] or row['CarrierId'] for row in view['containers'].values()):
        raise ValueError('目标没有父子/载具关系，请使用基础prepare_container_quantity_loss。')
    expected,amount,number=projection(view,args); target=view['containers'][ref['instanceId']]
    if q.identifier(status.get('Spec'))!=target['SpecId'] or q.identifier(status.get('Step'))!=target['WorkflowStepId']: raise ValueError('当前查询与持久工序/步骤不符。')
    from tools.production_reporting import ensure_no_unresolved_throughput,ensure_no_unresolved_collection
    claims=set(view['containers'])
    for carrier in view['carriers'].values(): claims.update(r['ContainerId'] for r in carrier['members'])
    if len(claims)>MAX_SCOPE: raise ValueError('完整层级与载具相关容器总数超过8个。')
    for carrier in view['carriers'].values():
        if any(r['ContainerId'] not in view['containers'] and any(r['CurrentStatusId']==v['CurrentStatusId'] and expected[cid]['carrier_detached'] for cid,v in view['containers'].items()) for r in carrier['members']):
            raise ValueError('归零将影响活动层级外共享状态的历史载具成员，须先明确整理成员。')
    for cid in claims: ensure_no_unresolved_throughput(cid);ensure_no_unresolved_collection(cid)
    dep=base.Dependencies(request_limit=80);spec=await dep.asset('Specs',status['Spec']);op=await dep.asset('Operations',spec.get('Operation'))
    if op['InstanceID']!=q.identifier(status.get('Operation')): raise ValueError('操作身份不符。')
    reason=await dep.asset('LossReasons',{'Name':q.text(args['loss_reason_name'])});group=await dep.asset('LossReasonGroups',op.get('LossReasons'))
    if sum(q.identifier(v)==reason['InstanceID'] and v.get('Name')==reason['Name'] for v in group['ResolvedEntries'])!=1: raise ValueError('LossReason不在当前操作完整原因组中。')
    unit=await dep.asset('UOMs',{'InstanceID':target['UOMId']});reason_ref={'name':reason['Name'],'instanceId':reason['InstanceID']}
    detail={'@odata.type':'#shopfloor.LossDetails','changeQtyType':2,'recordAllQty':args['record_all_quantity'],
            'adjustThruput':args['adjust_throughput'],'reasonCode':reason_ref}
    if not args['record_all_quantity']: detail['qty']=number
    body={'container':ref,'closeWhenZero':args['close_when_zero'],'disassociateWhenClosed':args['disassociate_when_closed'],
          'generateRollups':True,'recordAllQtySummaryOnly':False,'rollupReason':reason_ref,'serviceDetails':[detail],
          'comments':marker+(' '+i.inspection_comment(args.get('comments','')) if args.get('comments') else '')}
    preview=await base.preview(body,target)
    if await scope_view(ref['instanceId'])!=view: raise ValueError('准备期间层级/载具/数量/成员发生变化。')
    await dep.stable()
    reads=set()
    from tools.quality_definitions import resources
    for (entity,_),state in dep.states.items(): reads.update(resources(entity,state))
    preview.update(expected_scope_effects=expected,loss_quantity=str(amount),projected_remaining_quantity=expected[ref['instanceId']]['remaining'],
                   projected_close=expected[ref['instanceId']]['closed'],unit={'name':unit['Name'],'instanceId':unit['InstanceID']},
                   note='完整层级/载具范围单次ChangeQty；归零会清除共享CurrentStatus的载具绑定，所有受影响对象逐项投影，不执行EmptyCarrier。须历史和全部原ID回读核实。')
    return {'kind':'quantity_loss','complex_version':3,'action':'loss','service':'ChangeQty','name':ref['name'],'target_id':ref['instanceId'],
        'scope_key':'quantity-loss:'+ref['instanceId'],'reason':reason_ref,'expected_loss_quantity':str(amount),
        'before':{'container':target,'container_status':execution.status_snapshot(status),'scope':view},'dependencies':list(dep.states.values()),
        'resource_reads':sorted(reads),'resource_writes':sorted([execution.runtime_claim('container',cid) for cid in claims]
            +[execution.runtime_claim('carrier',cid) for cid in view['carriers']]),
        'body':body,'preview':preview,'expected_scope':expected,'arguments':args,'marker':marker,'environment':p.environment()}


def validate_transport(snap):
    if snap.get('complex_version') not in (1,2,3) or snap['kind']!='quantity_loss' or snap['service']!='ChangeQty': raise ValueError('复杂数量计划版本/服务不符。')
    args=snap['arguments'];body=snap['body'];expected,amount,number=projection(snap['before']['scope'],args,semantics=snap['complex_version'])
    validate_scope(snap['before']['scope'])
    if expected!=snap['expected_scope'] or str(amount)!=snap['expected_loss_quantity']: raise ValueError('完整范围投影与原计划不符。')
    detail={'@odata.type':'#shopfloor.LossDetails','changeQtyType':2,'recordAllQty':args['record_all_quantity'],'adjustThruput':args['adjust_throughput'],'reasonCode':snap['reason']}
    if not args['record_all_quantity']: detail['qty']=number
    comment=i.inspection_comment(args.get('comments','')); wanted={'container':{'name':snap['name'],'instanceId':snap['target_id']},
        'closeWhenZero':args['close_when_zero'],'disassociateWhenClosed':args['disassociate_when_closed'],'generateRollups':True,
        'recordAllQtySummaryOnly':False,'rollupReason':snap['reason'],'serviceDetails':[detail],'comments':snap['marker']+(' '+comment if comment else '')}
    if body!=wanted or not args.get('complex_scope'): raise ValueError('复杂数量损失请求与完整不可变计划不符。')
    if not isinstance(snap.get('marker'),str) or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap['marker']): raise ValueError('原系统标记无效。')


@mcp.tool
async def get_quantity_loss_scope(container_name:str,container_instance_id:str|None=None)->str:
    """只读核查真实容器类型、完整父子层级及载具成员。最多8个相关容器；Batch7939原生ChangeQty禁止，返回明确阻塞。不创建计划、不扣量。"""
    try:
        ref,_=await q.container_identity(container_name,container_instance_id);view=await scope_view(ref['instanceId']);validate_scope(view)
        return q.encode({'complete':True,'scope':view,'native_batch_changeqty_allowed':False,'carrier_execution_enabled':CARRIER_LOSS_EXECUTION_VERIFIED,'executable':False,'note':'仅范围核查；操作须明确数量和关闭/解关联等选项并准备计划。完整范围仅当前活动层级，历史关闭子容器保留父ID但不属于活动成员。'})
    except (ValueError,TypeError,KeyError,AttributeError) as exc:return q.encode({'complete':False,'executable':False,'error':str(exc)})


@mcp.tool
async def prepare_related_container_quantity_loss(container_name:str,loss_reason_name:str,close_when_zero:bool,adjust_throughput:bool,
        disassociate_when_closed:bool,generate_rollups:bool,loss_quantity:str|None=None,record_all_quantity:bool=False,
        container_instance_id:str|None=None,comments:str='')->str:
    """准备同产品/主单位/步骤的父子或跟踪载具实物容器LossDetails计划，最多8个完整相关容器，单次ChangeQty。叶容器可明确部分损失，父容器仅明确整棵目标子树全量；须明确归零关闭、关闭解关联、生产数量调整，并generate_rollups=true保留逐层历史。归零会清除共享CurrentStatus的载具绑定，完整投影全部影响，不调用EmptyCarrier。限基础Container及单单位，无冻结/返工/抽样批；真实Batch被原生禁止，不转换对象或循环扣量。execute_quality_transaction执行，reconcile_quality_execution与review_quality_transaction(resolve_history)核实结案；unknown不重放。"""
    return await p.prepare('quantity_loss',dict(complex_scope=True,**locals()))


async def current_evidence(snap,txn_id=None):
    view=snap['before']['scope'];current={};checks={};native_projection=projection(view,snap['arguments'])[0]
    for cid,before in view['containers'].items():
        row=await base.record(cid); current[cid]=row; expect=native_projection[cid]
        checks['container:'+cid]=bool(row and all(row[k]==before[k] for k in ('ContainerId','ContainerName','CDOTypeId','ProductId','UOMId','UOM2Id','SpecId','WorkflowStepId'))
            and i.numeric(str(row['Qty']),'当前余额')==i.numeric(expect['remaining'],'预计余额') and row['Status']==expect['status']
            and (row['ParentContainerId'] or '')==(expect['parent_id'] or '') and (row['CarrierId'] or '')==(expect['carrier_id'] or '')
            and row['ChildCount']==expect['child_count'] and row['CurrentHoldCount']==0 and row['InRework'] is False
            and (row['ChangeCount']>before['ChangeCount'] if i.numeric(expect['loss'],'损失')>0 else row['ChangeCount']>=before['ChangeCount']))
        if txn_id is not None and i.numeric(expect['loss'],'损失')>0:
            tokens=await q.sql_rows('SELECT ContainerId,LastRevTxnId,CurrentThruputQty,ThruputAdjustmentQty FROM Container WHERE ContainerId='+q.literal(cid),1)
            token=tokens[0] if len(tokens)==1 else {}
            # LastRevTxnId is not written by ChangeQty here: it may be empty OR
            # retain a previous Transfer transaction. It is audit information,
            # not a quantity-transaction token. Exact version delta and the
            # separately verified original root history are both mandatory.
            checks['original_transaction_or_single_revision:'+cid]=(token.get('ContainerId')==cid and
                row is not None and row['ChangeCount']==before['ChangeCount']+1)
            if token: token['binding_basis']='verified_root_history_and_exact_single_change_count_delta; LastRevTxnId_not_a_ChangeQty_token; child_txn_not_stored'
            if row is not None: row['native_transaction_and_production_counters']=tokens
    for carrier_id,carrier in view['carriers'].items():
        members=await q.sql_rows('SELECT TOP 9 c.ContainerId FROM Container c JOIN CurrentStatus s ON s.CurrentStatusId=c.CurrentStatusId WHERE s.CarrierId='+q.literal(carrier_id)+' ORDER BY c.ContainerId',9)
        wanted={r['ContainerId'] for r in carrier['members'] if not native_projection.get(r['ContainerId'],{}).get('carrier_detached')}
        checks['carrier_members:'+carrier_id]=len(members)==len(wanted) and {r['ContainerId'] for r in members}==wanted
        for before in carrier['members']:
            if before['ContainerId'] not in current:
                row=await base.record(before['ContainerId']);current[before['ContainerId']]=row
                checks['unchanged_carrier_peer:'+before['ContainerId']]=row==before
    corrections={cid:{'original':snap['expected_scope'][cid],'native_contract':item} for cid,item in native_projection.items() if item!=snap['expected_scope'][cid]}
    return checks,{'records':current,'projection_corrections':corrections,'note':'原请求、原摘要和原投影未改写；旧v1/v2的历史ParentId/逐叶历史假设按现场语义单独更正。效果基于原根历史、完整原对象回读与恰好一版变更；LastRevTxnId未存储时不伪称存在子事务记录。' if corrections else '按原不可变投影、原根历史与全部原对象回读核对。'}


async def inspect_snapshot(snap):
    checks,current=await current_evidence(snap)
    return {'verified_success':False,'state_changed':False,'retry_allowed':False,'current_checks':checks,'current_records':current,
            'note':'当前状态相同不证明本次请求成功；仍须核对唯一原生事务历史。'}


def compare(snap,attempt,root,evidence):
    from tools import quality_disposition_history as h
    target=snap['before']['container'];rootid=root['HistoryMainlineId'];expected=projection(snap['before']['scope'],snap['arguments'])[0];reason=snap['reason']['instanceId']
    checks={'attempt_time_window':h.attempt_time_matches(attempt,root),
        'root_identity':root['ContainerId']==snap['target_id'] and root['HistoryId']==snap['before']['scope']['root_id'] and root['ContainerName']==snap['name']
            and root['IntegrationTxnId']==rootid and root['TxnTypeName']=='ChangeQty' and i.wire_int(root['TxnType'],'事务类型')==4000
            and i.wire_int(root['ReversalStatus'],'冲销状态')==1,
        'root_quantity':h.quantity(root['Qty'])==h.quantity(target['Qty']) and h.quantity(root['ToQty'])==h.quantity(expected[snap['target_id']]['remaining']),
        'root_context':root['ProductId']==target['ProductId'] and root['SpecId']==target['SpecId'] and root['WorkflowStepId']==target['WorkflowStepId']
            and root['OperationId']==q.identifier(snap['before']['container_status']['Operation'])
            and i.wire_int(root['ContainerChangeCount'],'原容器版本')==target['ChangeCount']
            and not i.wire_bool(root['InRework'],'返工标志') and h.quantity(root['Qty2'],nullable_zero=True)==h.quantity(root['ToQty2'],nullable_zero=True)==0}
    scope_ids=set(expected);seen=set();details_ok=True;direct=set();rollups=set()
    for detail in evidence['details']:
        cid=detail['ContainerId']; key=(cid,i.wire_bool(detail['IsRollup'],'汇总标志'))
        if cid not in scope_ids or key in seen: details_ok=False;continue
        seen.add(key);before=snap['before']['scope']['containers'][cid];effect=expected[cid];rollup=key[1]
        if not rollup: direct.add(cid)
        details_ok=details_ok and detail['ReasonCodeId']==reason and detail['UOMId']==before['UOMId'] and not detail['UOM2Id']
        if rollup:
            rollups.add(cid)
            details_ok=details_ok and i.wire_int(detail['CDOTypeId'],'汇总明细类型')==5020 and detail['ChangeQtyType'] in ('',None) and str(detail['QtyMultiplier'])=='1'
            details_ok=details_ok and h.quantity(detail['Qty'])==-h.quantity(effect['loss']) and detail['IntegrationTxnId'] in ('',None)
            details_ok=details_ok and i.wire_bool(detail['CountsAgainstProduction'],'计入生产') is False
        else:
            details_ok=details_ok and i.wire_int(detail['CDOTypeId'],'明细类型')==6932 and i.wire_int(detail['ChangeQtyType'],'损失类型')==2 and str(detail['QtyMultiplier'])=='-1'
            details_ok=details_ok and h.quantity(detail['Qty'])==h.quantity(effect['loss']) and detail['IntegrationTxnId']==rootid
            details_ok=details_ok and i.wire_bool(detail['CountsAgainstProduction'],'计入生产') is True and detail['ChargeToStepId']==before['WorkflowStepId']
        details_ok=details_ok and h.quantity(detail['Qty2'],nullable_zero=True)==0
        details_ok=details_ok and i.wire_bool(detail['AdjustThruput'],'调整生产数量')==(False if rollup else snap['arguments']['adjust_throughput'])
        details_ok=details_ok and i.wire_bool(detail['RecordAllQty'],'全量')==(False if rollup else snap['arguments']['record_all_quantity'])
        details_ok=details_ok and i.wire_bool(detail['CloseWhenZero'],'归零关闭')==(False if rollup else snap['arguments']['close_when_zero'])
        details_ok=details_ok and detail['ProductId']==before['ProductId'] and detail['OperationId']==q.identifier(snap['before']['container_status']['Operation'])
        details_ok=details_ok and i.wire_bool(detail['ThisContainerClosed'],'关闭')==effect['closed'] and i.wire_bool(detail['ThisContainerDisassociated'],'解关联')==effect['disassociated']
        details_ok=details_ok and detail['TxnId']==root['TxnId'] and detail['HistoryId']==snap['before']['scope']['root_id']
    wanted_direct={cid for cid,effect in expected.items() if effect['direct_loss'] and h.quantity(effect['loss'])>0}
    wanted_rollups=set();ancestor=target['ParentContainerId']
    while ancestor: wanted_rollups.add(ancestor);ancestor=snap['before']['scope']['containers'][ancestor]['ParentContainerId']
    checks['exact_target_and_ancestor_history']=details_ok and direct==wanted_direct and rollups==wanted_rollups
    checks['scope_history_only']=len(evidence['mainlines'])==1 and evidence['mainlines'][0]==root
    checks['detail_parent_chains']=all(any(s['QtyHistoryId']==d['QtyHistoryId'] and s['HistoryId']==snap['target_id'] and s['TxnId']==d['TxnId'] and s['IntegrationTxnId']==rootid and s['HistoryMainlineId']==rootid for s in evidence['summaries']) for d in evidence['details'])
    checks['root_summary_quantity']=len(evidence['summaries'])==1 and h.quantity(evidence['summaries'][0]['Qty'])==-h.quantity(snap['expected_loss_quantity'])
    if len(evidence['summaries'])==1:
        s=evidence['summaries'][0]
        checks['summary_type_and_options']=i.wire_int(s['CDOTypeId'],'汇总类型')==5080 and h.quantity(s['Qty2'],nullable_zero=True)==0 and i.wire_bool(s['CloseWhenZero'],'归零关闭')==snap['arguments']['close_when_zero'] and not i.wire_bool(s['ThruputSummary'],'吞吐汇总')
    return checks
