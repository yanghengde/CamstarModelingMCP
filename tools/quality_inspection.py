"""Container AQL inspection: explicit deltas, fixed tasks and guarded submission plans."""
import json
import re
from decimal import Decimal,InvalidOperation
from typing import Annotated

from pydantic import BaseModel,ConfigDict,Field,StrictInt
from tools import mcp,quality as reads
from tools.boms import encode
from tools.process_context import ReadContext
from tools.modeling_details import raw_json,_has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

TASK_COLUMNS='ContainerSampleDataId,ContainerId,SamplingPlanId,SampleTestId,InspectionLevelId,AQLLevelId,TotalSamplesRequired,CompletedSamples,Rejects,AllowedRejects,InspectAllContQty,InspectAllQtyOnFailure,ChangeCount'
CONTAINER_COLUMNS='ContainerId,ContainerName,ProductId,Status,COALESCE(CurrentHoldCount,0) AS CurrentHoldCount,InQualityControl,SamplingRequired,SamplingPassed,SamplingLotId,Qty,ChangeCount'
TEST_FIELDS=('InstanceID','Name','Revision','Status','IsFrozen','SampleType','Instructions','AllowMoveOnFailure',
    'AQLRejectReasons','DecreaseByRejectCountReason','DecreaseBySampleSizeReason','DefaultFailureMode',
    'DefaultPEDescription','Classification','SubClassification','ScrapCountedRejectsByReason')
POINT_FIELDS=('InstanceID','Name','Revision','Status','IsFrozen','DataType','LowerLimit','UpperLimit','ShowLimits','UOM')


class InspectionPointValue(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    data_point_name:str=Field(min_length=1,max_length=128)
    value:str=Field(min_length=1,max_length=100,description='明确数值的字符串；不求值表达式，不采用默认值。')


class InspectionSample(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    points:list[InspectionPointValue]=Field(min_length=1,max_length=20)


class CountedInspectionGroup(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    samples_collected:StrictInt=Field(ge=1,le=100)
    rejects:StrictInt=Field(ge=0,le=100)
    reject_reason_name:str|None=Field(default=None,min_length=1,max_length=128)


def inspection_comment(value):
    if not isinstance(value,str) or '\x00' in value or len(value.encode('utf-16-le'))>360:
        raise ValueError('检验备注最多180个UTF-16单元且不能包含NUL；事务标记另占长度。')
    return value


def counted_details(definition,args):
    groups=args.get('counted_groups')
    if groups is None:
        raw=[{key:args.get(key) for key in ('samples_collected','rejects','reject_reason_name')}]
    else:
        if any(args.get(key) is not None for key in ('samples_collected','rejects','reject_reason_name')):
            raise ValueError('counted_groups不能混用根级计数或拒收原因。')
        if not isinstance(groups,list) or not 1<=len(groups)<=20:raise ValueError('计数分组须为1..20组。')
        raw=[CountedInspectionGroup.model_validate(g).model_dump() for g in groups]
    details=[];checks=[];seen=set();total=0;failed=0;test_ref=revision_ref(definition['test'])
    for number,item in enumerate(raw,1):
        count=int32(item['samples_collected'],'本组采样数',1);rejects=int32(item['rejects'],'本组拒收数')
        if rejects>count:raise ValueError('本组拒收数不能大于本组采样数。')
        reason=item.get('reject_reason_name');ref=None
        detail={'sampleTest':test_ref,'totalSamplesCollected':count,'rejects':rejects,'rejected':rejects>0}
        if rejects:
            if not reason:raise ValueError('拒收组必须明确一个已配置的拒收原因。')
            matches=[r for r in definition['reject_reasons'] if r['name']==reads.text(reason,'拒收原因')]
            if len(matches)!=1 or not reads.valid_id(matches[0].get('instanceId')):
                raise ValueError('拒收原因不属于完整配置组或身份不唯一。')
            ref=matches[0]
            # CountedRejectReason is an alias of the first point child's reason.
            # A shortcut-only input did not expose a child in the live nonexecuting echo.
            detail['samplingDataPointDetails']=[{'dataValue':str(rejects),'passed':False,'rejectReason':ref}]
        elif reason is not None:raise ValueError('零拒收组不能指定拒收原因。')
        reason_id=ref['instanceId'] if ref else None
        if groups is not None:
            if reason_id in seen:raise ValueError('同一拒收原因或零拒收组不能重复，不自动合并组。')
            seen.add(reason_id)
            checks.append({'group_number':number,'samples_collected':count,'rejects':rejects,'reject_reason':ref,
                           'sample_partition_verified':False})
        total+=count;failed+=rejects;details.append(detail)
    if groups is not None and total>100:raise ValueError('计数分组本次合计最多100个样本。')
    return details,checks,total,failed


def counted_group_context(args):
    if args.get('counted_groups') is None:return {}
    return {'counted_groups_this_submission':len(args['counted_groups']),'sample_partition_verified':False,
            'counted_group_accumulation_verified':False,'counted_reason_history_verified':False,
            'counted_group_semantics':'explicit disjoint cohorts; per-group counts are never allocated from a global total'}


def int32(value,label,minimum=0):
    if type(value) is not int or not minimum<=value<=2147483647:raise ValueError(label+'须为明确32位整数。')
    return value


def wire_int(value,label,nullable=False):
    # Live Query/AdHoc renders SQL Int32 columns as decimal strings.
    if nullable and value in (None,''):return None
    if isinstance(value,str) and re.fullmatch(r'0|[1-9][0-9]{0,9}',value):value=int(value)
    return int32(value,label)


def wire_bool(value,label,nullable=False):
    if type(value) is bool:return value
    if isinstance(value,str) and value in ('true','false'):return value=='true'
    if nullable and value in (None,''):return None
    raise ValueError(label+'布尔值未知。')


def present(ref):
    if ref is None:return False
    if not isinstance(ref,dict):raise ValueError('建模引用形状无效。')
    if ref and not any(k in ref for k in ('Name','InstanceId','InstanceID')):raise ValueError('建模引用内容未知。')
    return bool(ref.get('Name') or reads.identifier(ref))


def revision_ref(row):
    iid=row.get('InstanceID')
    if not reads.valid_id(iid):raise ValueError('检验定义没有有效ID。')
    return {'name':reads.text(row.get('Name')),'revision':reads.text(row.get('Revision'),'版本'),'instanceId':iid}


async def container_snapshot(name,instance_id=None):
    ref,status=await reads.container_identity(name,instance_id)
    rows=await reads.sql_rows(f"SELECT {CONTAINER_COLUMNS} FROM Container WHERE ContainerId='{ref['instanceId']}'",1)
    if len(rows)!=1 or rows[0].get('ContainerId')!=ref['instanceId'] or rows[0].get('ContainerName')!=ref['name']:
        raise ValueError('检验容器身份未确认。')
    rows[0]['Status']=wire_int(rows[0].get('Status'),'容器Status')
    rows[0]['ChangeCount']=wire_int(rows[0].get('ChangeCount'),'容器ChangeCount')
    rows[0]['CurrentHoldCount']=wire_int(rows[0].get('CurrentHoldCount'),'冻结计数',nullable=True)
    for field in ('SamplingRequired','SamplingPassed'):rows[0][field]=wire_bool(rows[0].get(field),field,nullable=True)
    return ref,rows[0],status


def check_tasks(rows,cid):
    for row in rows:
        if row.get('ContainerId')!=cid or any(not reads.valid_id(row.get(k)) for k in ('ContainerSampleDataId','SampleTestId','SamplingPlanId')):
            raise ValueError('检验任务父容器/测试/计划身份未确认。')
        for field in ('TotalSamplesRequired','CompletedSamples','Rejects','AllowedRejects','ChangeCount'):row[field]=wire_int(row.get(field),field)
        for field in ('InspectAllContQty','InspectAllQtyOnFailure'):row[field]=wire_bool(row.get(field),field,nullable=True)
        if row['Rejects']>row['CompletedSamples']:raise ValueError('现有检验任务拒收数大于已采数。')
    if len({r['ContainerSampleDataId'] for r in rows})!=len(rows):raise ValueError('检验任务ID重复。')


async def test_definition(name,revision,instance_id=None):
    ctx=ReadContext(64)
    root=await ctx.resolve('SampleTests',{'Name':reads.text(name),'Revision':reads.text(revision,'检验测试版本'),
        **({'InstanceID':instance_id} if instance_id else {})})
    revision_ref(root)
    if any(k not in root for k in TEST_FIELDS):raise ValueError('检验测试配置字段缺失，不能确定副作用。')
    for field in ('Status','SampleType'):root[field]=wire_int(root[field],field)
    for field in ('IsFrozen','AllowMoveOnFailure','ScrapCountedRejectsByReason'):root[field]=wire_bool(root[field],field,nullable=True)
    expanded=await ctx.get('SampleTests',root['InstanceID'],{'$expand':'SampleDataPoints'})
    if expanded.get('InstanceID')!=root['InstanceID'] or _has_next_link(expanded) or not isinstance(expanded.get('SampleDataPoints'),list):
        raise ValueError('检验点清单未完整展开。')
    refs=expanded['SampleDataPoints']
    if len(refs)>20:raise ValueError('单个测试最多支持20个检验点。')
    points=[]
    for ref in refs:
        point=await ctx.resolve('SampleDataPoints',ref)
        if any(k not in point for k in POINT_FIELDS):raise ValueError('检验点类型/限值/状态/单位字段不完整。')
        for field in ('Status','DataType'):point[field]=wire_int(point[field],field)
        revision_ref(point);points.append({k:point.get(k) for k in POINT_FIELDS})
    if len({p['Name'].casefold() for p in points})!=len(points) or len({p['InstanceID'] for p in points})!=len(points):
        raise ValueError('检验点名称或ID重复。')
    result={'test':{k:root.get(k) for k in TEST_FIELDS},'points':points,'reject_reasons':[],'reject_group':None}
    group_ref=root.get('AQLRejectReasons')
    if present(group_ref):
        group=await ctx.resolve('LossReasonGroups',group_ref,False)
        expanded_group=await ctx.get('LossReasonGroups',group['InstanceID'],{'$expand':'ResolvedEntries'})
        entries=expanded_group.get('ResolvedEntries')
        if expanded_group.get('InstanceID')!=group['InstanceID'] or _has_next_link(expanded_group) or not isinstance(entries,list) or len(entries)>100:
            raise ValueError('拒收原因组未完整展开。')
        values=[]
        for entry in entries:
            if not reads.valid_id(reads.identifier(entry)):raise ValueError('拒收原因缺少有效ID。')
            values.append({'name':reads.text(entry.get('Name'),'拒收原因'),'instanceId':reads.identifier(entry)})
        if len({r['name'].casefold() for r in values})!=len(values) or len({r['instanceId'] for r in values})!=len(values):
            raise ValueError('拒收原因重复或不唯一。')
        result['reject_group']={'Name':group.get('Name'),'InstanceID':group['InstanceID']}
        result['reject_reasons']=values
    return result


@mcp.tool
async def get_inspection_test_definition(sample_test_name:str,sample_test_revision:str)->str:
    """只读固定版本SampleTest及最多20个SampleDataPoint完整定义：类型、单位、上下限、作业说明、拒收原因和扣量/事件配置。不是普通DataCollectionDef，不自动选原因、默认数值或判定放行。外部采样类型3暂不支持执行。"""
    try:return encode({'definition':await test_definition(sample_test_name,sample_test_revision),'complete':True,'quality_verdict':None})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})


@mcp.tool
async def get_container_inspection_tasks(container_name:str,container_instance_id:str|None=None,offset:int=0,limit:int=10)->str:
    """只读容器当前ContainerSampleData检验任务及固定测试版本/说明，显示MES要求样本数、已采/拒收数、阈值和本页剩余数。limit<=20；不是全部工序待办或最终合格结论。关联抽样批的容器不能用单容器检验提交。"""
    try:
        reads.paging(offset,limit)
        if limit>20:raise ValueError('检验任务每页最多20条。')
        ref,state,_=await container_snapshot(container_name,container_instance_id)
        rows=await reads.sql_rows(f"SELECT {TASK_COLUMNS} FROM ContainerSampleData WHERE ContainerId='{ref['instanceId']}' ORDER BY ContainerSampleDataId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY",limit+1)
        check_tasks(rows,ref['instanceId']);tasks=[];ctx=ReadContext(20)
        for row in rows[:limit]:
            test=await ctx.resolve('SampleTests',{'InstanceID':row['SampleTestId']})
            tasks.append({'task':row,'sample_test':revision_ref(test),'sample_type':test.get('SampleType'),
                'instructions':test.get('Instructions'),'remaining_required':max(0,row['TotalSamplesRequired']-row['CompletedSamples']),
                'requires_separate_lot_service':bool(state.get('SamplingLotId'))})
        more=len(rows)>limit
        return encode({'container':ref,'status':state,'tasks':tasks,'page_complete':True,'complete':offset==0 and not more,
            'has_more':more,'next_offset':offset+limit if more else None,'quality_verdict':None,'release_authorized':False,
            'scope':'current persisted container sampling tasks; not all process tasks'})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})


def numeric(value,label):
    if not isinstance(value,str) or len(value)>100 or not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?',value):
        raise ValueError(label+'须为明确有限数值字符串，不接受表达式。')
    try:number=Decimal(value)
    except InvalidOperation:raise ValueError(label+'数值无效。') from None
    if not number.is_finite() or abs(number.adjusted())>308:raise ValueError(label+'数值超出受限范围。')
    return number


def point_check(point,value):
    dtype=point.get('DataType')
    if type(dtype) is not int or dtype not in (1,2,3,9):raise ValueError('当前测量检验仅支持已核实的Integer/Float/Fixed/Decimal数值点。')
    number=numeric(value,point['Name'])
    if dtype==1 and (not re.fullmatch(r'[+-]?[0-9]+',value) or not -2147483648<=number<=2147483647):
        raise ValueError('整数检验点须为32位整数字符串。')
    lower=point.get('LowerLimit');upper=point.get('UpperLimit')
    lo=None if lower in (None,'') else numeric(lower,'下限')
    hi=None if upper in (None,'') else numeric(upper,'上限')
    if lo is not None and hi is not None and lo>hi:raise ValueError('检验点上下限反置。')
    passed=(lo is None or number>=lo) and (hi is None or number<=hi)
    return passed,{'data_point':point['Name'],'value':value,'lower_limit':lower,'upper_limit':upper,'within_literal_limits':passed}


def reject_effectful_definition(definition):
    test=definition['test']
    for field in ('DecreaseBySampleSizeReason','DecreaseByRejectCountReason','DefaultFailureMode','Classification','SubClassification'):
        if present(test.get(field)):raise ValueError('当前受限检验不执行扣量或自动质量事件；请先核对配置字段 '+field+'。')
    if test.get('DefaultPEDescription'):raise ValueError('检验配置带自动生产事件说明，当前不提交。')
    if test.get('ScrapCountedRejectsByReason') is not False:raise ValueError('拒收报废配置为真或未知，当前不提交。')
    if type(test.get('Status')) is not int or test['Status']!=1:raise ValueError('检验测试不是已确认Active状态。')


def measured_details(definition,samples):
    if not isinstance(samples,list) or not 1<=len(samples)<=50:raise ValueError('测量检验须明确1..50个样本。')
    points=definition['points']
    if not points:raise ValueError('测量检验没有完整检验点。')
    if len(samples)*len(points)>100:raise ValueError('一次最多100个测量值，请缩小样本批次。')
    if any(type(p.get('Status')) is not int or p['Status']!=1 for p in points):raise ValueError('检验点不是已确认Active状态。')
    expected={p['Name']:p for p in points};details=[];checks=[]
    for i,raw in enumerate(samples):
        sample=InspectionSample.model_validate(raw)
        values={v.data_point_name:v.value for v in sample.points}
        if len(values)!=len(sample.points) or set(values)!=set(expected):raise ValueError('每个样本必须逐一提供全部检验点，不能缺少、重复或多传。')
        pdata=[];sample_checks=[]
        for name,point in expected.items():
            passed,check=point_check(point,values[name]);sample_checks.append(check)
            pdata.append({'sampleDataPoint':revision_ref(point),'dataValue':values[name],'passed':passed})
        failed=any(not p['passed'] for p in pdata)
        details.append({'sampleTest':revision_ref(definition['test']),'totalSamplesCollected':1,'rejects':int(failed),
            'rejected':failed,'samplingDataPointDetails':pdata})
        checks.append({'sample_number':i+1,'failed_sample':failed,'point_checks':sample_checks})
    return details,checks


def collection_details(definition,args,*,effects_validated=False):
    """Shared counted/measured deltas; target task and lot checks remain separate."""
    if not effects_validated:reject_effectful_definition(definition)
    test=definition['test'];test_ref=revision_ref(test);dtype=test.get('SampleType')
    if type(dtype) is not int or dtype not in (1,2):raise ValueError('目前仅支持Counted=1、Measured=2，不提交外部采样。')
    if dtype==1:
        if args.get('samples') is not None:raise ValueError('计数检验不接受测量样本。')
        return counted_details(definition,args)
    if any(args.get(k) is not None for k in ('samples_collected','rejects','reject_reason_name','counted_groups')):
        raise ValueError('测量检验从完整样本计算本次个数，不接受计数覆盖值。')
    details,checks=measured_details(definition,args.get('samples'))
    return details,checks,len(details),sum(d['rejects'] for d in details)


async def build_inspection(args,marker):
    from tools import quality_plans as plans
    from tools import quality_inspection_effects as effects
    full,quantity=effects.options(args)
    name=reads.text(args['container_name'],'容器名');test_name=reads.text(args['sample_test_name'],'检验测试')
    definition=await test_definition(test_name,args['sample_test_revision']);test=definition['test'];test_ref=revision_ref(test)
    ref,before,status=await container_snapshot(name,args.get('container_instance_id'));cid=ref['instanceId']
    if (type(before.get('Status')) is not int or before['Status']!=1 or type(before.get('CurrentHoldCount')) is not int
            or before['CurrentHoldCount']!=0 or before.get('SamplingRequired') is not True):
        raise ValueError('当前容器不是已确认需检验、有效且无冻结状态。')
    if before.get('SamplingLotId'):raise ValueError('该容器属于抽样批，必须使用单独批抽样流程，不能按单容器提交。')
    rows=await reads.sql_rows(f"SELECT TOP 2 {TASK_COLUMNS} FROM ContainerSampleData WHERE ContainerId='{cid}' AND SampleTestId='{test_ref['instanceId']}' ORDER BY ContainerSampleDataId",2)
    check_tasks(rows,cid)
    if len(rows)!=1 or rows[0]['SampleTestId']!=test_ref['instanceId']:raise ValueError('当前容器没有唯一的该版本检验任务。')
    task=rows[0]
    if task['CompletedSamples']>=task['TotalSamplesRequired']:raise ValueError('当前任务已满足要求样本数，不再追加。')
    effects.task_policy(task,before,args)
    mode=effects.definition_policy(definition,args)
    details,checks,count,rejects=collection_details(definition,args,effects_validated=quantity);dtype=test['SampleType']
    if count>100 or count>task['TotalSamplesRequired']-task['CompletedSamples']:raise ValueError('本次采样超过100或MES要求剩余数，不自动截断。')
    comment=inspection_comment(args.get('comments',''))
    body={'container':ref,'sampleTest':test_ref,'serviceDetails':details,'moveContainerOnCompletion':False,
        'comments':marker+(' '+comment if comment else '')}
    counted_loss = quantity and dtype == 1
    if counted_loss:
        body['samplesTested'] = count
    if len(json.dumps(body,ensure_ascii=False))>22000:raise ValueError('检验请求超过安全预览长度，请缩小样本批次。')
    native_effects=await effects.prepare_effects(definition,before,status,task,args,mode,count,rejects)
    preview=(await inspection_preview(body,expected_task_id=task['ContainerSampleDataId'])
             if native_effects else await inspection_preview(body))
    # The loaded service's numeric totals must agree with the persisted task.
    for field,expected in (('RequiredSamples',task['TotalSamplesRequired']),('SamplesTested',count if counted_loss else task['CompletedSamples']),
                           ('AllowedRejects',task['AllowedRejects'])):
        if type(preview['task_context'].get(field)) not in (str,int) or str(preview['task_context'][field])!=str(expected):
            raise ValueError('MES检验回显与任务快照不一致：'+field)
    if str(preview['task_context'].get('SampleType'))!=str(dtype):raise ValueError('MES回显采样类型与测试定义不符。')
    if (await container_snapshot(name,cid))[1]!=before:raise ValueError('准备检验期间容器状态已变化。')
    current=await reads.sql_rows(f"SELECT TOP 2 {TASK_COLUMNS} FROM ContainerSampleData WHERE ContainerId='{cid}' AND SampleTestId='{test_ref['instanceId']}' ORDER BY ContainerSampleDataId",2)
    check_tasks(current,cid)
    if current!=rows:raise ValueError('准备检验期间任务计数已变化。')
    if native_effects and await test_definition(test_name,args['sample_test_revision'])!=definition:
        raise ValueError('准备原生全检/扣量期间检验定义或点集合已变化。')
    if native_effects:await effects.final_recheck(native_effects)
    preview.update({'local_checks':checks,'samples_this_submission':count,'rejects_this_submission':rejects,
        'result_semantics':'literal numeric precheck only; MES final result and release unverified',
        'automatic_move':False,'quantity_or_event_effects_supported':False})
    preview.update(counted_group_context(args))
    result={'kind':'inspection','action':'collect','name':name,'target_id':cid,'scope_key':'inspection-container:'+cid,
        'before':{'container':before,'task':task,'container_status':{k:status[k] for k in (
            'Container','Spec','Step','Workflow','Operation','Resource','Product','Qty','Status') if k in status}},
        'definition':definition,'service':'CollectSamplingData',
        'body':body,'marker':marker,'preview':preview,'arguments':args,'environment':plans.environment()}
    if native_effects:
        result['inspection_effects']=native_effects
        result['resource_reads']=native_effects['resource_reads']
        preview.update(native_effects=native_effects['context'],quantity_or_event_effects_supported=quantity)
    return result


async def inspection_preview(body,service='CollectSamplingData',expected_task_id=None):
    from tools.quality_plans import wire_matches
    if service not in ('CollectSamplingData','CollectLotSamplingData'):raise ValueError('检验服务不在预览白名单。')
    fields=['RequiredSamples','SamplesTested','AllowedRejects','SampleType','SamplingStatus']
    select=['Container','SampleTest','ServiceDetails','MoveContainerOnCompletion','Comments']+fields
    if expected_task_id:select.append('ContainerSampleDataItem')
    if service=='CollectLotSamplingData':select+=['SamplingLot','LotSampleDataItem','IsExternal','IsWebhook']
    params={'execute':'false','$select':','.join(select),
        '$expand':'ServiceDetails($select=SampleTest,TotalSamplesCollected,Rejects,Rejected;'
                  '$expand=SamplingDataPointDetails($select=SampleDataPoint,DataValue,Passed,RejectReason))'}
    raw=await request('POST','/api/'+service,base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,params=params,response_formatter=raw_json)
    try:outer=json.loads(raw)
    except (ValueError,TypeError):raise ValueError('检验服务非执行预览未通过：'+str(raw)[:3000]) from None
    if not isinstance(outer,dict):raise ValueError('检验服务响应形状无效。')
    data=outer.get('data',outer)
    if not isinstance(data,dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error') or data.get('ErrorCode') or _has_next_link(data):
        raise ValueError('检验服务非执行预览失败或不完整。')
    if not wire_matches(body,data):raise ValueError('检验回显的完整样本、数值、引用、拒收数或移站选项不一致。')
    if expected_task_id and reads.identifier(data.get('ContainerSampleDataItem'))!=expected_task_id:
        raise ValueError('全检/扣量检验未回显原持久任务ID，不能用同值任务替代。')
    for expected,detail in zip(body['serviceDetails'],data['ServiceDetails']):
        if detail.get('CDOTypeId') is not None and str(detail['CDOTypeId'])!='8462':
            raise ValueError('检验样本回显明细类型与已核实契约不符。')
        points=detail.get('SamplingDataPointDetails')
        if points is None:points=[]
        if not isinstance(points,list) or len(points)!=len(expected.get('samplingDataPointDetails',[])):
            raise ValueError('检验点/拒收原因子行缺少、额外或未完整展开。')
        for point in points:
            if point.get('CDOTypeId') is not None and str(point['CDOTypeId'])!='8463':
                raise ValueError('检验点回显明细类型与已核实契约不符。')
    return {'params':params,'verified_input_echo':body,'task_context':{k:data.get(k) for k in fields},'dynamic_validation_verified':False,
            'detail_contract':'shopfloor.CollectSamplingDataDetails','point_contract':'shopfloor.SamplingDataPointDetails',
            'response_dynamic_type_verified':False}


@mcp.tool
async def prepare_container_inspection(container_name:str,sample_test_name:str,sample_test_revision:str,
        container_instance_id:str|None=None,samples_collected:StrictInt|None=None,rejects:StrictInt|None=None,reject_reason_name:str|None=None,
        samples:Annotated[list[InspectionSample]|None,Field(max_length=50)]=None,comments:str='',
        counted_groups:Annotated[list[CountedInspectionGroup]|None,Field(min_length=1,max_length=20)]=None,
        allow_full_inspection:bool=False,allow_automatic_quantity_loss:bool=False)->str:
    """准备单容器CollectSamplingData，固定版本及当前任务，计数/显式不重叠counted_groups或测量samples逐样本完整点值。默认拒绝全检和自动扣量；allow_full_inspection=true允许MES已有全检/失败转全检规则，要求数和标志不由客户端修改；allow_automatic_quantity_loss=true仅允许一种扣量配置：DecreaseBySampleSizeReason按本次测量或计数样本合计扣量，DecreaseByRejectCountReason按本次计数正数拒收合计扣量。MES内部扣量，不另发ChangeQty，不能扣至零。两选项互斥；高级模式仅基础Container、单位明确、无冻结/层级/载具/第二单位/抽样批，核对原任务ID及配置重读。计数扣量使用单组samples_collected或明确互不重叠的counted_groups，核对根SamplesTested与本次样本合计及完整分组回显；逐拒收原因报废、自动事件及批抽样仍不支持，移站false。实际转换、扣量、签名和父子历史待验收；确认单次提交，unknown不补发扣量。"""
    from tools import quality_plans as plans
    try:
        normalized=None if samples is None else [InspectionSample.model_validate(s).model_dump() for s in samples]
        groups=None if counted_groups is None else [CountedInspectionGroup.model_validate(g).model_dump() for g in counted_groups]
        return await plans.prepare('inspection',dict(container_name=container_name,sample_test_name=sample_test_name,
            sample_test_revision=sample_test_revision,container_instance_id=container_instance_id,samples_collected=samples_collected,
            rejects=rejects,reject_reason_name=reject_reason_name,samples=normalized,comments=comments,counted_groups=groups,
            allow_full_inspection=allow_full_inspection,allow_automatic_quantity_loss=allow_automatic_quantity_loss))
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'submitted':False,'error':str(error)})


@mcp.tool
async def get_container_inspection_history(container_name:str,container_instance_id:str|None=None,offset:int=0,limit:int=20)->str:
    """固定SQL只读容器CollectSamplingDataHistory及HistoryMainline关联的当前页历史汇总，包括测试/计划ID、累计样本/拒收数、MES状态、事务ID及评论。不把累计数说成本次增量，不把同值或关联标记作最终成功证据；不自动解锁未知提交。"""
    try:
        reads.paging(offset,limit);ref,_=await reads.container_identity(container_name,container_instance_id);cid=ref['instanceId']
        rows=await reads.sql_rows('SELECT s.CollectSamplingDataHistoryId,s.HistoryMainlineId,s.TxnId,s.SampleTestId,s.SamplingPlanId,'
            's.RequiredSamples,s.SamplesTested,s.TotalPassedSamples,s.TotalRejects,s.TotalSamplesCompleted,s.AllowedRejects,s.SamplingStatus,s.TxnDateGMT,'
            'h.ContainerId,h.ContainerName,h.Comments,h.TxnTypeName,h.ReversalStatus FROM CollectSamplingDataHistory s '
            'INNER JOIN HistoryMainline h ON h.HistoryMainlineId=s.HistoryMainlineId AND h.TxnId=s.TxnId '
            +f"WHERE h.ContainerId='{cid}' ORDER BY s.TxnDateGMT DESC,s.CollectSamplingDataHistoryId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY",limit+1)
        if any(r.get('ContainerId')!=cid or not reads.valid_id(r.get('CollectSamplingDataHistoryId')) or not reads.valid_id(r.get('TxnId')) for r in rows):
            raise ValueError('检验历史父容器/事务身份不匹配。')
        if len({r['CollectSamplingDataHistoryId'] for r in rows})!=len(rows):raise ValueError('检验历史关联重复。')
        more=len(rows)>limit
        return encode({'container':ref,'rows':rows[:limit],'page_complete':True,'complete':False,'has_more':more,
            'next_offset':offset+limit if more else None,'verified_success':False,'state_changed':False,'quantity_semantics':'raw MES history fields; not submission deltas'})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'verified_success':False,'error':str(error)})
