"""Explicit station transitions following separately committed throughput.

These are native MoveIn / MoveStd plans, never a fallback for failed MoveOut.
"""
import asyncio
import json
import time
import uuid
from typing import Literal
from tools import mcp
from tools import production_reporting as p
from tools.modeling_details import raw_json
from tools.process_context import ReadContext


async def station_snapshot(args):
    status,cid=await p.container_state(args['container_name'],args.get('container_instance_id'))
    p.ensure_no_unresolved_throughput(cid);p.ensure_no_unresolved_quality(cid);p.ensure_no_unresolved_collection(cid)
    if status.get('Status')!=1 or type(status.get('InProcess')) is not bool:
        raise ValueError('只支持明确Active且进站状态已知的容器。')
    ctx=ReadContext(12)
    level=await ctx.resolve('ContainerLevels',status.get('ContainerLevel'),revisioned=False)
    op=await ctx.resolve('Operations',status.get('Operation'),revisioned=False)
    workflow=await ctx.resolve('Workflows',status.get('Workflow'))
    if level.get('AllowMove') is not True:raise ValueError('层级未明确允许移动；不修改层级。')
    rows=await p.query_rows('SELECT c.ContainerId,c.ContainerName,c.ChangeCount,c.Qty,c.Qty2,c.UOMId,c.MfgOrderId,'
        'c.CurrentStatusId,c.CurrentThruputQty,c.ThruputAdjustmentQty,c.ParentContainerId,c.ChildCount,'
        'COALESCE(c.CurrentHoldCount,0) AS CurrentHoldCount,c.ProductionComplete,s.WorkflowStepId,s.CurrentStepPass,'
        's.InRework,s.CarrierId FROM Container c JOIN CurrentStatus s ON s.CurrentStatusId=c.CurrentStatusId '
        f"WHERE c.ContainerId='{cid}'")
    if len(rows)!=1 or rows[0].get('ContainerId')!=cid:raise ValueError('持久状态身份不唯一。')
    row=rows[0];step=p.identifier(row.get('WorkflowStepId'))
    ref=status.get('Step') or {}
    if step != (ref.get('InstanceId') or ref.get('InstanceID')):raise ValueError('状态视图与持久步骤身份不符。')
    if (p.sql_number(row.get('ChildCount'),'子容器数',integer=True)!=0 or row.get('ParentContainerId')
            or row.get('CarrierId') or p.sql_number(row.get('CurrentHoldCount'),'冻结数',integer=True)!=0
            or p.sql_boolean(row.get('ProductionComplete'),'生产完成')
            or p.sql_boolean(row.get('InRework'),'返工标志')):
        raise ValueError('仅支持无父子/载具/冻结/返工且未生产完成的基础容器。')
    leaves=await ctx.get('Workflows',workflow['InstanceID'],{'$expand':'Steps'})
    steps=[s for s in leaves.get('Steps',[]) if s.get('Name')==ref.get('Name')]
    persisted=await p.query_rows('SELECT WorkflowStepId,WorkflowStepName,WorkflowId,ChangeCount '
        f"FROM WorkflowStep WHERE WorkflowStepId='{step}'")
    if (len(steps)!=1 or len(persisted)!=1 or persisted[0].get('WorkflowId')!=workflow['InstanceID']
            or persisted[0].get('WorkflowStepName')!=ref.get('Name')):
        raise ValueError('工艺步骤所属或配置未唯一核实。')
    action=args['action'];path=None;quantity_read=None
    body={'container':{'name':args['container_name'].strip(),'instanceId':cid},'comments':p.receipt_comments(args)}
    if action=='enter':
        if status['InProcess'] or op.get('UseQueue') is not True:
            raise ValueError('仅排队工序且尚未进站的容器可明确进站。')
        if args.get('path_name'):raise ValueError('进站不能指定移站路径。')
    else:
        if status['InProcess'] is not True:raise ValueError('出站/末工序完成须先处于加工中。')
        remaining,quantity_read=await p.remaining_quantity(row,cid,args['container_name'].strip())
        if remaining!=0:raise ValueError('须先核实全部加工量已独立上报；标准移站不补报或重复报吞吐。')
        paths=await p.query_rows('SELECT TOP 101 PathId,PathName,FromStepId,ToStepId,ChangeCount FROM Path '
            f"WHERE FromStepId='{step}' ORDER BY PathId")
        if len(paths)>100:raise ValueError('步骤路径超出读取上限。')
        if action in ('exit','complete'):
            selected=[x for x in paths if x.get('PathName')==args.get('path_name')]
            if len(selected)!=1 or not selected[0].get('ToStepId'):
                raise ValueError('出站须明确选择本步骤的一条真实后续路径。')
            path=selected[0]
            targets=await p.query_rows('SELECT WorkflowStepId,WorkflowStepName,WorkflowId,ChangeCount '
                f"FROM WorkflowStep WHERE WorkflowStepId='{p.identifier(path['ToStepId'])}'")
            target_leaves=[s for s in leaves.get('Steps',[]) if len(targets)==1 and s.get('Name')==targets[0].get('WorkflowStepName')]
            if len(targets)!=1 or targets[0].get('WorkflowId')!=workflow['InstanceID'] or len(target_leaves)!=1:
                raise ValueError('目标步骤未在同一工作流中唯一核实。')
            target_last=target_leaves[0].get('IsLastStep')
            if type(target_last) is not bool or (action=='complete') != target_last:
                raise ValueError('complete须明确移入生产完成节点；普通exit不得隐含整容器生产完成。')
            path={**path,'target_configuration':{k:target_leaves[0].get(k) for k in ('Name','IsLastStep','StepType')},
                'target_persistent':targets[0]}
            body['path']={'name':path['PathName'],'instanceId':p.identifier(path['PathId'])}
        else:raise ValueError('未知生产动作。')
        body.update(moveAllQty=True,close=False)
    resource=None
    if args.get('resource_name'):
        resource=await ctx.resolve('Resources',{'Name':args['resource_name']},revisioned=False)
        if resource.get('IsFrozen') is not False:raise ValueError('资源未明确未冻结。')
        body['resource']={'name':resource['Name'],'instanceId':p.identifier(resource['InstanceID'])}
    # Changes-CDO row IDs are allocated on each Modeling read; bind real
    # configuration scalars and persistent WorkflowStep.ChangeCount instead.
    stable_step={k:steps[0].get(k) for k in ('Name','StepType','Sequence','IsLastStep')}
    return {'service':args['service'],'action':action,'environment':p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),
        'query_environment':p.CAMSTAR_QUERY_BASE_URL.rstrip('/'),'container_id':cid,'body':body,
        'receipt_marker':args['_receipt_marker'],'status':{k:status.get(k) for k in
            ('Container','MfgOrder','Status','InProcess','Workflow','Step','Spec','Operation','ContainerLevel')},
        'counters':row,'operation':op,'level':level,'workflow_step':persisted[0],
        'step_configuration':stable_step,'path':path,'resource':resource,'history_baseline':[],
        'native_quantity_read':quantity_read}


@mcp.tool
async def prepare_production_station_transition(container_name: str, action: Literal['enter','exit','complete'],
        container_instance_id: str | None=None, path_name: str | None=None,
        resource_name: str | None=None, comments: str='') -> str:
    """明确准备分步生产流程：enter原生MoveIn；exit/complete原生MoveStd，绝非MoveOut失败后的自动替代。
    exit须先全部独立吞吐已提交核实并明确真实路径；complete沿真实路径移入IsLastStep生产完成节点。
    不补报吞吐、不扣不良、不跳过原生采集/签名/培训/计时等校验，不直接修改工单状态或日期。
    失败立即停止该容器流程；每步独立提交而非跨步骤原子事务，未知先查原事务不重试。
    """
    try:
        if action not in ('enter','exit','complete'):raise ValueError('action须为enter/exit/complete。')
        args=dict(container_name=container_name,container_instance_id=container_instance_id,action=action,
            path_name=path_name,resource_name=resource_name,comments=comments,
            service='MoveIn' if action=='enter' else 'MoveStd',_receipt_marker='MCP-PROD:'+uuid.uuid4().hex)
        snap=await asyncio.wait_for(station_snapshot(args),60)
        args['container_instance_id']=snap['container_id']
        key,signature=uuid.uuid4().hex,p.digest(snap)
        with p.db_connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',(key,signature,
                raw_json({'arguments':args,'snapshot':snap}),'ready',time.time()))
        return raw_json({'plan_id':key,'plan_digest':signature,'state':'ready','request':snap['body'],
            'service':snap['service'],'action':action,'execute_tool':'execute_production_station_transition',
            'atomic_with_other_steps':False,'writes_executed':False,'native_dynamic_validation_verified':False})
    except (ValueError,TypeError,KeyError,AttributeError,asyncio.TimeoutError) as exc:
        return raw_json({'executable':False,'error':str(exc)})


@mcp.tool
async def execute_production_station_transition(plan_id: str, plan_digest: str) -> str:
    """仅执行准备好的MoveIn/MoveStd生产步骤；执行前重核，单次POST，未知不重放。
    complete原生移入生产完成节点，由MES规则计算生产完成；不强制关闭容器或直接改工单状态/日期。
    """
    try:
        with p.db_connect() as db:
            row=db.execute('SELECT digest,payload,state,created FROM plans WHERE id=?',(plan_id,)).fetchone()
        snap=p.checked_plan(row,plan_digest)['snapshot']
        if snap['service'] not in ('MoveIn','MoveStd'):raise ValueError('须使用原生产进出站计划。')
    except (ValueError,TypeError,KeyError) as exc:
        return raw_json({'submitted':False,'error':str(exc)})
    return await p.execute_container_throughput(plan_id,plan_digest)
