"""Sampling-lot reads and creation plans. No container start or lot disposition."""
import json
from decimal import Decimal
from pydantic import StrictInt,StrictBool
from tools import mcp,quality as reads
from tools import quality_inspection as inspection
from tools import quality_definitions as definitions
from tools.process_context import ReadContext
from tools.modeling_details import raw_json,_has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

LOT_COLUMNS='MfgLotId,MfgLotName,CDOTypeId,ProductId,Qty,UOMId,LotStatus,ChangeCount,SamplingPlanId,SampleRate,AllowAdjustToLotSize,SamplingCompleted,SamplingLotResult,SamplingPassed,CreationDate'


async def lot_record(name='',instance_id=None):
    if instance_id is not None and not reads.valid_id(instance_id):raise ValueError('抽样批ID无效。')
    if not instance_id:name=reads.text(name,'抽样批名')
    where=f"MfgLotId='{instance_id}'" if instance_id else 'MfgLotName='+reads.literal(name)
    rows=await reads.sql_rows(f'SELECT TOP 2 {LOT_COLUMNS} FROM MfgLot WHERE '+where,2)
    if len(rows)>1:raise ValueError('抽样批名称不唯一。')
    if not rows:return None
    row=rows[0]
    if (not reads.valid_id(row.get('MfgLotId')) or name and row.get('MfgLotName')!=name
            or instance_id and row.get('MfgLotId')!=instance_id):raise ValueError('抽样批身份不符。')
    if inspection.wire_int(row.get('CDOTypeId'),'批类型')!=8587:raise ValueError('同名记录不是SamplingLot，不能作为抽样批操作。')
    return row


@mcp.tool
async def get_sampling_lot_details(lot_name:str='',lot_instance_id:str|None=None,container_offset:int=0,sample_offset:int=0,limit:int=20)->str:
    """只读SamplingLot（MfgLot派生类型8587）详情、成员容器及LotSampleData两个独立分页。显示MES状态/计数/计划引用，不判定批合格、不执行返工/报废，不把抽样批当普通Container。limit<=100，偏移分别从0起。"""
    try:
        reads.paging(container_offset,limit);reads.paging(sample_offset,limit)
        lot=await lot_record(lot_name,lot_instance_id)
        if lot is None:return reads.encode({'found':False,'data':None,'complete':True,'quality_verdict':None})
        iid=lot['MfgLotId']
        containers=await reads.sql_rows('SELECT ContainerId,ContainerName,SamplingLotId,ProductId,Qty,Status,ChangeCount FROM Container '
            +f"WHERE SamplingLotId='{iid}' ORDER BY ContainerId OFFSET {container_offset} ROWS FETCH NEXT {limit+1} ROWS ONLY",limit+1)
        samples=await reads.sql_rows('SELECT LotSampleDataId,SamplingLotId,SamplingPlanId,SampleTestId,SamplingPlanDetailId,'
            'TotalSamplesRequired,CompletedSamples,Rejects,AllowedRejects,SamplesTested,SamplesFailed,ChangeCount FROM LotSampleData '
            +f"WHERE SamplingLotId='{iid}' ORDER BY LotSampleDataId OFFSET {sample_offset} ROWS FETCH NEXT {limit+1} ROWS ONLY",limit+1)
        for rows,key in ((containers,'ContainerId'),(samples,'LotSampleDataId')):
            if any(r.get('SamplingLotId')!=iid or not reads.valid_id(r.get(key)) for r in rows) or len({r[key] for r in rows})!=len(rows):
                raise ValueError('抽样批关联父ID/行ID不符或重复。')
        if await lot_record('',iid)!=lot:raise ValueError('读取关联期间抽样批已变化。')
        return reads.encode({'found':True,'data':lot,'containers':{'rows':containers[:limit],'has_more':len(containers)>limit,
                'next_offset':container_offset+limit if len(containers)>limit else None},
            'samples':{'rows':samples[:limit],'has_more':len(samples)>limit,'next_offset':sample_offset+limit if len(samples)>limit else None},
            'page_complete':True,'complete':False,'quality_verdict':None,'release_authorized':False,
            'note':'跨查询只读快照；成员与样本分别分页，不把空页当作创建失败或整批合格。'})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return reads.encode({'complete':False,'error':str(error)})


async def lot_preview(body):
    from tools.quality_plans import wire_matches
    case={'uom':'UOM'}
    select=[case.get(k,k[0].upper()+k[1:]) for k in body]
    params={'execute':'false','$select':','.join(select)}
    raw=await request('POST','/api/CreateSamplingLot',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,params=params,response_formatter=raw_json)
    try:outer=json.loads(raw)
    except (ValueError,TypeError):raise ValueError('创建抽样批预览未通过：'+str(raw)[:3000]) from None
    if not isinstance(outer,dict):raise ValueError('抽样批回显形状无效。')
    data=outer.get('data',outer)
    if not isinstance(data,dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error') or data.get('ErrorCode') or _has_next_link(data):
        raise ValueError('抽样批预览失败或不完整。')
    # Qty is an Edm.Double, rendered as a decimal string by the live service.
    actual=data.get('Qty')
    if type(actual) not in (str,int,float) or inspection.numeric(str(actual),'回显数量')!=Decimal(str(body['qty'])):
        raise ValueError('抽样批数量回显不一致。')
    check={k:v for k,v in body.items() if k not in ('qty','uom')}
    if not wire_matches(check,data) or not wire_matches(body['uom'],data.get('UOM')):
        raise ValueError('抽样批产品、计划、单位、选项或评论回显不一致。')
    return {'params':params,'verified_input_echo':body,'dynamic_validation_verified':False,
        'container_attachment_requested':False,'sample_rate_overrides_plan_rates':'sampleRate' in body}


async def build_lot(args,marker):
    from tools import quality_plans as plans
    name=reads.text(args['lot_name'],'抽样批名')
    if await lot_record(name):raise ValueError('目标抽样批已存在。')
    value=inspection.numeric(args['quantity'],'抽样批数量')
    if not 0<value<=Decimal('1000000000000'):raise ValueError('抽样批数量须大于0且不超过1e12。')
    qty=float(value)
    if Decimal(str(qty))!=value:raise ValueError('数量超出接口Double可无文本精度损失表示的范围。')
    deps=[]
    product_ref,product=await definitions.reference('Products',{'name':args['product_name'],'revision':args['product_revision']},deps)
    if inspection.wire_int(product.get('Status'),'产品Status')!=1:raise ValueError('产品不是Active状态。')
    resolved=product.get('SamplingPlanResolved');direct=product.get('SamplingPlan')
    plan_ref=resolved if inspection.present(resolved) else direct
    if not isinstance(plan_ref,dict) or not inspection.present(plan_ref):raise ValueError('产品未返回可确认的抽样计划。')
    plan_root=await ReadContext(3).resolve('SamplingPlans',plan_ref)
    plan=await definitions.read_state('sampling_plan',plan_root['Name'],plan_root['Revision'])
    if plan['Status']!=1 or not plan['SamplingPlanDetails']:raise ValueError('抽样计划不为Active或没有测试明细。')
    uom,_=await definitions.reference('UOMs',args['uom_name'],deps,False)
    if type(args['allow_adjust_to_lot_size']) is not bool:raise ValueError('allow_adjust_to_lot_size必须是布尔值。')
    body={'lotName':name,'product':product_ref,'samplingPlan':inspection.revision_ref(plan),'qty':qty,'uom':uom,
        'allowAdjustToLotSize':args['allow_adjust_to_lot_size'],'comments':marker}
    if args.get('sample_rate') is not None:body['sampleRate']=inspection.int32(args['sample_rate'],'覆盖采样频率',1)
    preview=await lot_preview(body)
    claims=set(definitions.resources('SamplingPlans',plan))
    ctx=ReadContext(50)
    for detail in plan['SamplingPlanDetails']:
        test=await ctx.resolve('SampleTests',detail.get('SampleTest'))
        if inspection.wire_int(test.get('Status'),'抽样测试Status')!=1:raise ValueError('批计划关联测试不为Active。')
        test_state={k:test.get(k) for k in ('Name','InstanceID','Revision','SampleType','Status')}
        deps.append({'entity':'SampleTests','row':test_state})
    for dep in deps:claims.update(definitions.resources(dep['entity'],dep['row']))
    return {'kind':'sampling_lot','action':'create','name':name,'target_id':None,'scope_key':'sampling-lot:'+name.casefold(),
        'service':'CreateSamplingLot','body':body,'before':None,'product':deps[0]['row'],'sampling_plan':plan,'dependencies':deps,
        'resource_reads':sorted(claims),'resource_writes':[],'preview':preview,'marker':marker,'arguments':args,'environment':plans.environment()}


@mcp.tool
async def prepare_sampling_lot_create(lot_name:str,product_name:str,product_revision:str,quantity:str,uom_name:str,
        sample_rate:StrictInt|None=None,allow_adjust_to_lot_size:StrictBool=False)->str:
    """只准备CreateSamplingLot，不写MES。明确批名、固定产品版本、数量字符串和单位；从产品实际引用固定抽样计划。sample_rate若提供会覆盖计划各采样频率，须明确；数量自适应默认false。containers是只读列表，不接收容器清单、不启动或自动归集容器。execute=false核对后由execute_quality_transaction确认单次提交；不执行批结果判定、返工或报废。"""
    from tools import quality_plans as plans
    return await plans.prepare('sampling_lot',dict(lot_name=lot_name,product_name=product_name,product_revision=product_revision,
        quantity=quantity,uom_name=uom_name,sample_rate=sample_rate,allow_adjust_to_lot_size=allow_adjust_to_lot_size))
