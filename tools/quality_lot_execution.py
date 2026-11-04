"""Lot sampling collection and guarded execution of an already-passed lot."""
import json
from typing import Annotated
from pydantic import Field,StrictInt
from tools import mcp,quality as reads,quality_inspection as inspection,quality_lots as lots
from tools.quality_definitions import resources
from tools.modeling_details import raw_json,_has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

# Native additive counters are SQL NULL until the first collection. Normalize
# only these known numeric columns in SQL; missing/malformed wire values still fail.
TASK_COLUMNS='LotSampleDataId,SamplingLotId,SamplingPlanId,SampleTestId,SamplingPlanDetailId,SpecId,ResourceId,VendorItemId,TotalSamplesRequired,COALESCE(CompletedSamples,0) AS CompletedSamples,COALESCE(Rejects,0) AS Rejects,AllowedRejects,COALESCE(SamplesTested,0) AS SamplesTested,COALESCE(SamplesFailed,0) AS SamplesFailed,InspectAllContQty,InspectAllQtyOnFailure,ChangeCount'
MEMBER_COLUMNS='ContainerId,ContainerName,SamplingLotId,ProductId,Status,COALESCE(CurrentHoldCount,0) AS CurrentHoldCount,ParentContainerId,ChildCount,Qty,Qty2,ChangeCount'
INTS=('TotalSamplesRequired','CompletedSamples','Rejects','AllowedRejects','SamplesTested','SamplesFailed','ChangeCount')


def runtime_claim(kind,iid):
    if not reads.valid_id(iid):raise ValueError('运行对象ID无效。')
    return 'runtime:'+kind+':id:'+iid.lower()


def lot_state(row):
    if not isinstance(row,dict):raise ValueError('抽样批不存在。')
    row=dict(row)
    row['MfgLotName']=reads.text(row.get('MfgLotName'),'批名称')
    for field in ('LotStatus','ChangeCount'):row[field]=inspection.wire_int(row.get(field),field)
    for field in ('SamplingCompleted','SamplingPassed'):row[field]=inspection.wire_bool(row.get(field),field,nullable=True)
    row['SamplingLotResult']=inspection.wire_int(row.get('SamplingLotResult'),'SamplingLotResult',nullable=True)
    for field in ('ProductId','SamplingPlanId'):
        if not reads.valid_id(row.get(field)):raise ValueError('抽样批产品或计划ID未确认。')
    return row


def tasks(rows,lot,strict_counts=False):
    result=[]
    for raw in rows:
        row=dict(raw)
        if row.get('SamplingLotId')!=lot['MfgLotId'] or row.get('SamplingPlanId')!=lot['SamplingPlanId']:
            raise ValueError('批任务父批/计划不一致。')
        for field in ('LotSampleDataId','SampleTestId'):
            if not reads.valid_id(row.get(field)):raise ValueError('批任务ID/测试ID无效。')
        for field in INTS:row[field]=inspection.wire_int(row.get(field),field)
        for field in ('InspectAllContQty','InspectAllQtyOnFailure'):row[field]=inspection.wire_bool(row.get(field),field,nullable=True)
        if strict_counts and (row['CompletedSamples']!=row['SamplesTested'] or row['Rejects']!=row['SamplesFailed']
                or row['Rejects']>row['CompletedSamples']):raise ValueError('批任务继承/派生计数不一致，不能推断本次增量。')
        result.append(row)
    if len({r['LotSampleDataId'] for r in result})!=len(result):raise ValueError('批任务重复。')
    return result


async def selected_task(lot,test_id,task_id=None):
    if task_id is not None and not reads.valid_id(task_id):raise ValueError('批检验任务ID无效。')
    where=f"SamplingLotId='{lot['MfgLotId']}' AND SampleTestId='{test_id}'"
    if task_id:where+=f" AND LotSampleDataId='{task_id}'"
    rows=tasks(await reads.sql_rows('SELECT TOP 2 '+TASK_COLUMNS+' FROM LotSampleData WHERE '+where+' ORDER BY LotSampleDataId',2),lot,True)
    if len(rows)!=1 or rows[0]['SampleTestId']!=test_id or task_id and rows[0]['LotSampleDataId']!=task_id:
        raise ValueError('批任务不存在或不唯一，需明确lot_sample_data_instance_id。')
    return rows[0]


def status_snapshot(status):
    return {k:status[k] for k in ('Container','Spec','Step','Workflow','Operation','Resource','Product','Qty','Status','InProcess') if k in status}


def check_member(lot,container,status):
    if container.get('SamplingLotId')!=lot['MfgLotId'] or container.get('ProductId')!=lot['ProductId']:
        raise ValueError('容器不属于明确的抽样批或产品版本不符。')
    if container.get('Status')!=1 or container.get('CurrentHoldCount')!=0:
        raise ValueError('容器状态非Active、冻结或冻结计数未知。')
    if inspection.wire_bool(status.get('InProcess'),'容器InProcess') is not True:
        raise ValueError('批采样要求容器已进站；不自动执行Move In。')
    if type(container.get('SamplingRequired')) is not bool:raise ValueError('容器采样标志未知。')
    # SamplingRequired=false is allowed for random lot sampling (manual 8-17).
    spec=reads.identifier(status.get('Spec'))
    if not reads.valid_id(spec):raise ValueError('当前工序身份未确认。')
    return spec


async def build_collection(args,marker):
    from tools import quality_plans as plans
    lot=lot_state(await lots.lot_record(args['lot_name'],args.get('lot_instance_id')))
    if lot['LotStatus']!=1 or lot['SamplingCompleted'] is not False or lot['SamplingLotResult'] not in (None,0):
        raise ValueError('抽样批非Active、已完成/已有处置，或完成状态未知。')
    ref,container,status=await inspection.container_snapshot(args['container_name'],args.get('container_instance_id'))
    spec=check_member(lot,container,status)
    definition=await inspection.test_definition(args['sample_test_name'],args['sample_test_revision'])
    test_ref=inspection.revision_ref(definition['test'])
    task=await selected_task(lot,test_ref['instanceId'],args.get('lot_sample_data_instance_id'))
    if task.get('SpecId') and task['SpecId']!=spec:raise ValueError('批任务限定工序与当前容器工序不一致。')
    if task.get('ResourceId') or task.get('VendorItemId'):raise ValueError('设备/供应商专属批任务当前未支持，不绕过适用范围。')
    if task['InspectAllContQty'] is not False or task['InspectAllQtyOnFailure'] is not False:
        raise ValueError('当前批任务全检/失败转全检标志为真或未知。')
    if task['CompletedSamples']>=task['TotalSamplesRequired'] or task['Rejects']>task['AllowedRejects']:
        raise ValueError('批任务已满足要求或已有拒收超阈值，不能继续追加。')
    details,checks,count,rejects=inspection.collection_details(definition,args)
    if count>100 or count>task['TotalSamplesRequired']-task['CompletedSamples']:
        raise ValueError('本次采样超出100或整批任务剩余数，不使用单容器要求数替代。')
    comment=inspection.inspection_comment(args.get('comments',''))
    body={'container':ref,'samplingLot':{'name':lot['MfgLotName'],'instanceId':lot['MfgLotId']},
        'sampleTest':test_ref,'lotSampleDataItem':{'instanceId':task['LotSampleDataId']},'serviceDetails':details,
        'moveContainerOnCompletion':False,'isExternal':False,'isWebhook':False,'comments':marker+(' '+comment if comment else '')}
    # CollectLotSampling_Execution adds this ROOT input for counted tests.
    # Bind both writable context fields to the actual definition/submission;
    # native nonexecuting previews otherwise default SamplesTested to one.
    body['samplesTested'] = count
    body['sampleType'] = definition['test']['SampleType']
    if len(json.dumps(body,ensure_ascii=False))>22000:raise ValueError('批采样请求超过预览长度上限。')
    preview=await inspection.inspection_preview(body,'CollectLotSamplingData')
    for field,value in (('RequiredSamples',task['TotalSamplesRequired']),('SamplesTested',count),
                        ('AllowedRejects',task['AllowedRejects']),('SampleType',definition['test']['SampleType'])):
        if type(preview['task_context'].get(field)) not in (str,int) or str(preview['task_context'][field])!=str(value):
            raise ValueError('批采样服务回显与整批任务不符：'+field)
    after_ref,after_container,after_status=await inspection.container_snapshot(args['container_name'],ref['instanceId'])
    if after_ref!=ref or after_container!=container or status_snapshot(after_status)!=status_snapshot(status):
        raise ValueError('准备期间成员容器或工序已变化。')
    if lot_state(await lots.lot_record('',lot['MfgLotId']))!=lot or await selected_task(lot,test_ref['instanceId'],task['LotSampleDataId'])!=task:
        raise ValueError('准备期间抽样批或任务累计数已变化。')
    claims=resources('SampleTests',definition['test'])
    for point in definition['points']:claims.update(resources('SampleDataPoints',point))
    claims.add('modeling:SamplingPlans:id:'+lot['SamplingPlanId'])
    preview.update(local_checks=checks,samples_this_submission=count,rejects_this_submission=rejects,
        quantity_semantics='current submission deltas, accumulated on the lot task',automatic_move=False,
        quality_verdict=None,random_sampling=not container['SamplingRequired'])
    preview.update(inspection.counted_group_context(args))
    return {'kind':'lot_inspection','action':'collect','name':lot['MfgLotName'],'target_id':lot['MfgLotId'],
        'scope_key':'sampling-lot:'+lot['MfgLotName'].casefold(),'service':'CollectLotSamplingData','body':body,
        'before':{'lot':lot,'task':task,'container':container,'container_status':status_snapshot(status)},'definition':definition,
        'resource_reads':sorted(claims),'resource_writes':[runtime_claim('lot',lot['MfgLotId']),runtime_claim('container',ref['instanceId'])],
        'preview':preview,'arguments':args,'marker':marker,'environment':plans.environment()}


@mcp.tool
async def prepare_lot_inspection(lot_name:str,container_name:str,sample_test_name:str,sample_test_revision:str,
        lot_instance_id:str|None=None,container_instance_id:str|None=None,lot_sample_data_instance_id:str|None=None,
        samples_collected:StrictInt|None=None,rejects:StrictInt|None=None,reject_reason_name:str|None=None,
        samples:Annotated[list[inspection.InspectionSample]|None,Field(max_length=50)]=None,comments:str='',
        counted_groups:Annotated[list[inspection.CountedInspectionGroup]|None,Field(min_length=1,max_length=20)]=None)->str:
    """准备CollectLotSamplingData批采样，明确批、已进站成员、固定测试版本；多任务需任务ID。计数可旧单组参数或counted_groups明确1..20个不重叠分组，每组samples_collected/rejects和非零拒收原因，合计<=100；不混用根级计数，不分摊、不重复原因/零拒收组。多组累计及原因历史待现场验收。测量逐样本完整点值；按批任务剩余数校验。不代进站，支持随机成员SamplingRequired=false；拒绝设备/供应商专属、LIMS、全检、扣量/事件；移站false。统一确认单次提交，unknown不重试。"""
    from tools import quality_plans as plans
    try:
        values=None if samples is None else [inspection.InspectionSample.model_validate(v).model_dump() for v in samples]
        groups=None if counted_groups is None else [inspection.CountedInspectionGroup.model_validate(g).model_dump() for g in counted_groups]
        return await plans.prepare('lot_inspection',dict(lot_name=lot_name,container_name=container_name,sample_test_name=sample_test_name,
            sample_test_revision=sample_test_revision,lot_instance_id=lot_instance_id,container_instance_id=container_instance_id,
            lot_sample_data_instance_id=lot_sample_data_instance_id,samples_collected=samples_collected,rejects=rejects,
            reject_reason_name=reject_reason_name,samples=values,comments=comments,counted_groups=groups))
    except (ValueError,TypeError,KeyError,AttributeError) as error:return reads.encode({'submitted':False,'error':str(error)})


async def lot_view(name,instance_id=None):
    lot=lot_state(await lots.lot_record(name,instance_id));iid=lot['MfgLotId']
    members=await reads.sql_rows(f"SELECT TOP 51 {MEMBER_COLUMNS} FROM Container WHERE SamplingLotId='{iid}' ORDER BY ContainerId",51)
    sample_rows=tasks(await reads.sql_rows(f"SELECT TOP 51 {TASK_COLUMNS} FROM LotSampleData WHERE SamplingLotId='{iid}' ORDER BY LotSampleDataId",51),lot)
    if len(members)>50 or len(sample_rows)>50:raise ValueError('整批校验最多50个成员及50个任务，不能用局部分页批准整批操作。')
    if any(r.get('SamplingLotId')!=iid or not reads.valid_id(r.get('ContainerId')) for r in members) or len({r['ContainerId'] for r in members})!=len(members):
        raise ValueError('抽样批成员身份不匹配或重复。')
    if lot_state(await lots.lot_record('',iid))!=lot:raise ValueError('读取整批期间批头已变化。')
    return {'lot':lot,'members':members,'tasks':sample_rows}


def completion_blockers(view):
    lot=view['lot'];reasons=[]
    if lot['LotStatus']!=1:reasons.append('批非Active')
    if lot['SamplingCompleted'] is not True:reasons.append('MES未明确确认整批采样完成')
    if lot['SamplingPassed'] is not True:reasons.append('MES未明确确认整批采样通过')
    if lot['SamplingLotResult'] not in (None,0):reasons.append('批已有处置结果')
    if not view['tasks']:reasons.append('批没有可核对的检验任务')
    if not view['members']:reasons.append('批没有成员容器')
    for row in view['tasks']:
        if (row['TotalSamplesRequired']<1 or row['CompletedSamples']<row['TotalSamplesRequired']
                or row['SamplesTested']!=row['CompletedSamples'] or row['SamplesFailed']!=row['Rejects']
                or row['Rejects']>row['AllowedRejects'] or row['Rejects']>row['CompletedSamples']):
            reasons.append('任务数量未完成、不一致或存在超阈值拒收：'+row['LotSampleDataId'])
    for row in view['members']:
        try:
            if row.get('ProductId')!=lot['ProductId']:raise ValueError('产品版本不一致')
            if inspection.wire_int(row.get('Status'),'成员状态')!=1:raise ValueError('成员非Active')
            if inspection.wire_int(row.get('CurrentHoldCount'),'成员冻结计数')!=0:raise ValueError('成员有冻结')
            if row.get('ParentContainerId') or inspection.wire_int(row.get('ChildCount'),'子容器数量')!=0:
                raise ValueError('当前不支持层级容器整批处理')
            if inspection.numeric(str(row.get('Qty')),'成员数量')<=0:raise ValueError('成员数量非正')
            inspection.wire_int(row.get('ChangeCount'),'成员变更计数')
        except ValueError as error:reasons.append(row['ContainerId']+'：'+str(error))
    return reasons


@mcp.tool
async def get_sampling_lot_readiness(lot_name:str,lot_instance_id:str|None=None)->str:
    """读取完整有界抽样批、最多50个成员和50个任务，分别展示MES完成/通过标志及OK执行前置阻塞。不自行判质量，不把前置满足说成签名/权限/业务批准。超界失败，不用分页子集批准整批。返工使用prepare_sampling_lot_rework，报废使用prepare_sampling_lot_scrap。"""
    try:
        view=await lot_view(lot_name,lot_instance_id);blockers=completion_blockers(view)
        return reads.encode({'data':view,'complete':True,'ok_preconditions_satisfied':not blockers,'blockers':blockers,
            'quality_verdict':None,'dynamic_validation_verified':False,'permission_verified':False})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return reads.encode({'complete':False,'ok_preconditions_satisfied':False,'error':str(error)})


async def completion_preview(body,names):
    from tools.quality_plans import wire_matches
    params={'execute':'false','$select':'SamplingLot,SamplingCompleted,SamplingLotResult,Comments,Containers'}
    raw=await request('POST','/api/ExecuteSamplingLot',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,params=params,response_formatter=raw_json)
    try:outer=json.loads(raw)
    except (ValueError,TypeError):raise ValueError('整批通过执行预览未通过：'+str(raw)[:3000]) from None
    if not isinstance(outer,dict):raise ValueError('整批预览响应形状无效。')
    data=outer.get('data',outer)
    if not isinstance(data,dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error') or data.get('ErrorCode') or _has_next_link(data):
        raise ValueError('整批预览失败或不完整。')
    if not wire_matches(body,data):raise ValueError('整批通过结果、完成标志、批引用或评论回显不一致。')
    containers=data.get('Containers')
    if not isinstance(containers,list) or any(not isinstance(n,str) for n in containers) or len(containers)!=len(set(containers)) or sorted(containers)!=sorted(names):
        raise ValueError('整批服务回显成员集合与完整批快照不一致。')
    return {'params':params,'verified_input_echo':body,'affected_container_names':sorted(containers),
        'dynamic_validation_verified':False,'mes_result_required':'SamplingCompleted=true and SamplingPassed=true before this plan',
        'effects_scope':'ExecuteSamplingLot applies to the entire lot; actual MES follow-up effects need onsite acceptance'}


async def build_completion(args,marker):
    from tools import quality_plans as plans
    view=await lot_view(args['lot_name'],args.get('lot_instance_id'));blockers=completion_blockers(view)
    if blockers:raise ValueError('不能准备整批通过执行：'+'；'.join(blockers[:12]))
    lot=view['lot'];comment=args.get('comments','')
    if not isinstance(comment,str) or len(comment)>180 or '\x00' in comment:raise ValueError('备注最多180字符。')
    body={'samplingLot':{'name':lot['MfgLotName'],'instanceId':lot['MfgLotId']},'samplingCompleted':True,
        'samplingLotResult':1,'comments':marker+(' '+comment if comment else '')}
    preview=await completion_preview(body,[reads.text(r.get('ContainerName'),'成员名') for r in view['members']])
    if await lot_view('',lot['MfgLotId'])!=view:raise ValueError('整批预览期间批头、成员或检验任务已变化。')
    claims={'modeling:SamplingPlans:id:'+lot['SamplingPlanId']}
    claims.update('modeling:SampleTests:id:'+r['SampleTestId'] for r in view['tasks'])
    return {'kind':'lot_completion','action':'ok','name':lot['MfgLotName'],'target_id':lot['MfgLotId'],
        'scope_key':'sampling-lot:'+lot['MfgLotName'].casefold(),'service':'ExecuteSamplingLot','body':body,'before':view,
        'resource_reads':sorted(claims),'resource_writes':[runtime_claim('lot',lot['MfgLotId'])]+
            [runtime_claim('container',r['ContainerId']) for r in view['members']],
        'preview':preview,'marker':marker,'arguments':args,'environment':plans.environment()}


@mcp.tool
async def prepare_sampling_lot_completion(lot_name:str,lot_instance_id:str|None=None,comments:str='')->str:
    """只准备MES已明确采样完成且通过的抽样批OK结果执行，ExecuteSamplingLot结果码固定1。完整核对最多50成员/50任务及服务回显成员集合；拒绝不完整、冻结、层级容器或既有处置，不把本地计数当MES通过标志。不设置放行/签名/流程覆盖字段，不支持返工2或报废3。操作影响整批，确认后execute_quality_transaction单次提交，结果先unknown，实际后续影响仍待现场验收。"""
    from tools import quality_plans as plans
    return await plans.prepare('lot_completion',dict(lot_name=lot_name,lot_instance_id=lot_instance_id,comments=comments))


@mcp.tool
async def get_sampling_lot_history(lot_name:str,lot_instance_id:str|None=None,offset:int=0,limit:int=20)->str:
    """固定SQL只读该抽样批的采样历史汇总及来源成员容器。依据历史SamplingLotId关联，不根据当前容器归属猜历史；原始累计字段不等于本次增量。不是ExecuteSamplingLot最终因果审计，不自动成功或解锁。"""
    try:
        reads.paging(offset,limit);lot=await lots.lot_record(lot_name,lot_instance_id)
        if not lot:return reads.encode({'found':False,'rows':[],'complete':False,'verified_success':False})
        iid=lot['MfgLotId']
        rows=await reads.sql_rows('SELECT s.CollectSamplingDataHistoryId,s.SamplingLotId,s.HistoryMainlineId,s.TxnId,s.SampleTestId,'
            's.RequiredSamples,s.SamplesTested,s.TotalSamplesCompleted,s.TotalPassedSamples,s.TotalRejects,s.SamplingStatus,s.TxnDateGMT,'
            'h.ContainerId,h.ContainerName,h.Comments,h.TxnTypeName FROM CollectSamplingDataHistory s '
            'INNER JOIN HistoryMainline h ON h.HistoryMainlineId=s.HistoryMainlineId AND h.TxnId=s.TxnId '
            +f"WHERE s.SamplingLotId='{iid}' ORDER BY s.TxnDateGMT DESC,s.CollectSamplingDataHistoryId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY",limit+1)
        if any(r.get('SamplingLotId')!=iid or any(not reads.valid_id(r.get(k)) for k in ('CollectSamplingDataHistoryId','TxnId','ContainerId')) for r in rows):
            raise ValueError('批采样历史父批/事务/来源身份不符。')
        if len({r['CollectSamplingDataHistoryId'] for r in rows})!=len(rows):raise ValueError('批采样历史关联重复。')
        more=len(rows)>limit
        return reads.encode({'found':True,'rows':rows[:limit],'page_complete':True,'complete':False,'has_more':more,
            'next_offset':offset+limit if more else None,'verified_success':False,'state_changed':False,
            'quantity_semantics':'raw historical fields, not current submission deltas','completion_causal_chain_verified':False})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return reads.encode({'complete':False,'verified_success':False,'error':str(error)})
