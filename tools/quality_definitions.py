"""Typed inspection modeling plans. CRUD bodies follow the deployed REST contract."""
import copy
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,StrictBool,StrictInt
from tools import mcp,quality as reads
from tools import quality_inspection as inspection
from tools.process_context import ReadContext
from tools.modeling_details import _has_next_link

ENTITIES={'sample_data_point':'SampleDataPoints','sample_test':'SampleTests','sampling_plan':'SamplingPlans'}
TABLES={'sample_data_point':('SampleDataPoint','SampleDataPointId'),'sample_test':('SampleTest','SampleTestId'),
        'sampling_plan':('SamplingPlan','SamplingPlanId')}
COMMON=('InstanceID','Name','Revision','IsRevOfRcd','IsFrozen','Status','Description')
FIELDS={
    'sample_data_point':COMMON+('DataType','DefaultValue','LowerLimit','UpperLimit','ShowLimits','UOM'),
    'sample_test':COMMON+inspection.TEST_FIELDS[5:]+('SampleDataPoints','WIPMsgDefMgr'),
    'sampling_plan':COMMON+('AQLLevel','InspectionLevel','SampleRate','SampleRateMaxDuration','SampleSizeBasedOnStartQty',
        'MoveContainerOnCompletion','Resource','Spec','SwitchingRule','SwitchingRuleMaxDuration','Vendor','VendorItem','SamplingPlanDetails','WIPMsgDefMgr'),
}
ROW_FIELDS=('InstanceID','AQLLevel','InspectAll','InspectAllContQty','InspectAllQtyOnFailure','InspectionLevel','IsFrozen',
            'Resource','SampleRate','SampleTest','Sequence','Spec','SwitchingRule','Vendor','VendorItem')
TYPES={'integer':1,'float':2,'fixed':3,'decimal':9}


class VersionReference(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    name:str=Field(min_length=1,max_length=128)
    revision:str=Field(min_length=1,max_length=128)


class PlanTestChange(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    op:Literal['add','update','remove']
    sample_test:VersionReference
    detail_instance_id:str|None=None
    aql_level_name:str|None=None
    inspection_level_name:str|None=None
    sample_rate:StrictInt|None=None
    sequence:StrictInt|None=None
    inspect_all:StrictBool|None=None
    inspect_all_container_qty:StrictBool|None=None
    inspect_all_qty_on_failure:StrictBool|None=None
    spec:VersionReference|None=None
    resource_name:str|None=None


class InspectionDefinitionChanges(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    description:str|None=Field(default=None,max_length=2000)
    status:StrictInt|None=Field(default=None,ge=1,le=2)
    data_type:Literal['integer','float','fixed','decimal']|None=None
    lower_limit:str|None=Field(default=None,max_length=100)
    upper_limit:str|None=Field(default=None,max_length=100)
    default_value:str|None=Field(default=None,max_length=100)
    show_limits:StrictBool|None=None
    uom_name:str|None=None
    sample_type:Literal['counted','measured']|None=None
    sample_data_points:list[VersionReference]|None=Field(default=None,max_length=20)
    instructions:str|None=Field(default=None,max_length=2000)
    reject_reason_group_name:str|None=None
    allow_move_on_failure:StrictBool|None=None
    aql_level_name:str|None=None
    inspection_level_name:str|None=None
    sample_rate:StrictInt|None=None
    move_container_on_completion:StrictBool|None=None
    sample_size_based_on_start_qty:StrictBool|None=None
    tests:list[PlanTestChange]|None=Field(default=None,max_length=50)


def resources(entity,row):
    result={f'modeling:{entity}:name:'+reads.text(row['Name']).casefold()}
    iid=row.get('InstanceID')
    if iid:
        if not reads.valid_id(iid):raise ValueError('配置引用ID无效。')
        result.add(f'modeling:{entity}:id:'+iid)
    return result


async def lookup(entity,name,revision=None):
    name=reads.text(name);where="Name eq '"+name.replace("'","''")+"'"
    if revision is not None:where+=" and Revision eq '"+reads.text(revision,'版本').replace("'","''")+"'"
    data=await ReadContext(1).get(entity,params={'$filter':where,'$top':2})
    rows=data.get('value')
    if not isinstance(rows,list) or _has_next_link(data) or revision is not None and len(rows)>1:
        raise ValueError('配置名称/版本存在性未确认。')
    if any(not reads.valid_id(r.get('InstanceID')) or r.get('Name')!=name or revision is not None and r.get('Revision')!=revision for r in rows):
        raise ValueError('配置列表身份不符。')
    return rows


async def token(kind,iid):
    if not reads.valid_id(iid):raise ValueError('配置ID无效。')
    table,key=TABLES[kind]
    rows=await reads.sql_rows(f"SELECT {key},ChangeCount FROM {table} WHERE {key}='{iid}'",1)
    if len(rows)!=1 or rows[0].get(key)!=iid:raise ValueError('配置变更计数身份未确认。')
    return inspection.wire_int(rows[0].get('ChangeCount'),'配置ChangeCount')


async def read_state(kind,name,revision):
    entity=ENTITIES[kind];ctx=ReadContext(4)
    row=await ctx.resolve(entity,{'Name':reads.text(name),'Revision':reads.text(revision,'版本')})
    inspection.revision_ref(row);first=await token(kind,row['InstanceID'])
    field={'sample_test':'SampleDataPoints','sampling_plan':'SamplingPlanDetails'}.get(kind)
    if field:
        expanded=await ctx.get(entity,row['InstanceID'],{'$expand':field})
        if expanded.get('InstanceID')!=row['InstanceID'] or _has_next_link(expanded) or not isinstance(expanded.get(field),list):
            raise ValueError('配置列表未完整展开。')
        if len(expanded[field])>(20 if kind=='sample_test' else 50):raise ValueError('配置列表超过维护上限。')
        row[field]=expanded[field]
    if any(k not in row for k in COMMON):raise ValueError('配置基本字段缺失。')
    result={k:copy.deepcopy(row.get(k)) for k in FIELDS[kind]}
    result['IsFrozen']=inspection.wire_bool(row['IsFrozen'],'配置冻结状态')
    result['IsRevOfRcd']=inspection.wire_bool(row['IsRevOfRcd'],'当前ROR状态')
    result['Status']=inspection.wire_int(row['Status'],'配置Status')
    if kind=='sampling_plan':
        values=[];seen=set()
        for detail in result['SamplingPlanDetails']:
            if not isinstance(detail,dict) or not reads.valid_id(detail.get('InstanceID')) or detail['InstanceID'] in seen:
                raise ValueError('抽样计划明细ID缺失或重复。')
            seen.add(detail['InstanceID']);values.append({k:copy.deepcopy(detail.get(k)) for k in ROW_FIELDS})
        result['SamplingPlanDetails']=values
    if await token(kind,row['InstanceID'])!=first:raise ValueError('读取配置期间发生变化。')
    result['ChangeCount']=first
    return result


async def reference(entity,value,dependencies,revisioned=True):
    ctx=ReadContext(3)
    if revisioned:
        value=VersionReference.model_validate(value)
        row=await ctx.resolve(entity,{'Name':reads.text(value.name),'Revision':reads.text(value.revision,'引用版本')})
        wire=inspection.revision_ref(row)
    else:
        row=await ctx.resolve(entity,{'Name':reads.text(value)},False)
        if not reads.valid_id(row.get('InstanceID')):raise ValueError('命名配置引用ID无效。')
        wire={'name':reads.text(row.get('Name')),'instanceId':row['InstanceID']}
    dependencies.append({'entity':entity,'row':{k:copy.deepcopy(row.get(k)) for k in (
        'Name','InstanceID','Revision','Status','IsFrozen','SamplingPlan','SamplingPlanResolved','UOM') if k in row}})
    return wire,row


def scalar(body,key,value):
    if value is None:raise ValueError(key+'不能清为空值。')
    body[key]=value


async def plan_rows(changes,before,body,dependencies,creating):
    old=copy.deepcopy(before.get('SamplingPlanDetails',[]) if before else []);seen=set();wrappers=[]
    for raw in changes:
        item=PlanTestChange.model_validate(raw);values=item.model_dump(exclude_unset=True)
        test,row=await reference('SampleTests',item.sample_test,dependencies)
        if inspection.wire_int(row.get('Status'),'测试Status')!=1:raise ValueError('关联测试不是Active状态。')
        if item.detail_instance_id is not None and not reads.valid_id(item.detail_instance_id):raise ValueError('明细ID无效。')
        candidates=[(n,d) for n,d in enumerate(old) if reads.identifier(d.get('SampleTest'))==test['instanceId']
            and (item.detail_instance_id is None or d.get('InstanceID')==item.detail_instance_id)]
        selector=('row:'+candidates[0][1]['InstanceID']) if item.op!='add' and len(candidates)==1 else ('test:'+test['instanceId'])
        if selector in seen:raise ValueError('一次计划不能重复修改同一测试明细。')
        seen.add(selector)
        if item.op=='add':
            if item.detail_instance_id or candidates:raise ValueError('新增测试已存在或不应指定明细ID。')
            sequences=[inspection.wire_int(d.get('Sequence'),'现有明细顺序') for d in old]
            rate=values.get('sample_rate',body.get('sampleRate',(before or {}).get('SampleRate')))
            rate=inspection.int32(inspection.wire_int(rate,'明细采样频率'),'明细采样频率',1)
            value={'sampleTest':test,'sequence':max(sequences,default=0)+len(wrappers)+1,'sampleRate':rate,'isFrozen':False,
                'inspectAll':False,'inspectAllContQty':False,'inspectAllQtyOnFailure':False}
        else:
            if creating or len(candidates)!=1:raise ValueError('待维护测试明细不存在或不唯一，请明确明细ID。')
            index,prior=candidates[0]
            if inspection.wire_bool(prior.get('IsFrozen'),'明细冻结状态') is not False:raise ValueError('测试明细已冻结。')
            if item.op=='remove':
                if set(values)-{'op','sample_test','detail_instance_id'}:raise ValueError('删除明细不能包含其他变更。')
                wrappers.append({'index':index,'listItemAction':'delete'});continue
            value={'sampleTest':test}
        for arg,key in [('sample_rate','sampleRate'),('sequence','sequence')]:
            if arg in values:scalar(value,key,inspection.int32(values[arg],arg,1))
        for arg,key in [('inspect_all','inspectAll'),('inspect_all_container_qty','inspectAllContQty'),('inspect_all_qty_on_failure','inspectAllQtyOnFailure')]:
            if arg in values:scalar(value,key,values[arg])
        for arg,entity,key in [('aql_level_name','AQLLevels','aqlLevel'),('inspection_level_name','InspectionLevels','inspectionLevel'),('resource_name','Resources','resource')]:
            if arg in values:value[key]=None if values[arg] is None else (await reference(entity,values[arg],dependencies,False))[0]
        if 'spec' in values:value['spec']=None if values['spec'] is None else (await reference('Specs',values['spec'],dependencies))[0]
        wrappers.append({'listItemAction':'add','value':value} if item.op=='add' else {'index':index,'value':value})
    # Original indices remain valid: update first, delete descending, then append.
    ordered=sorted(wrappers,key=lambda w:(2,0) if w.get('listItemAction')=='add' else (
        (1,-w['index']) if w.get('listItemAction')=='delete' else (0,w['index'])))
    body['samplingPlanDetails' if creating else 'samplingPlanDetails-Expanded']=[w['value'] for w in wrappers] if creating else ordered


async def build_definition(args,marker):
    from tools import quality_plans as plans
    kind=args['definition_kind'];action=args['action']
    if kind not in ENTITIES or action not in ('create','update','revise'):raise ValueError('定义类型或维护操作未开放。')
    name=reads.text(args['name']);revision=reads.text(args['revision'],'目标版本');entity=ENTITIES[kind]
    changes=InspectionDefinitionChanges.model_validate(args['changes']).model_dump(exclude_unset=True)
    if not changes:raise ValueError('必须明确配置变更。')
    allowed={'description','status'}|{
        'sample_data_point':{'data_type','lower_limit','upper_limit','default_value','show_limits','uom_name'},
        'sample_test':{'sample_type','sample_data_points','instructions','reject_reason_group_name','allow_move_on_failure'},
        'sampling_plan':{'aql_level_name','inspection_level_name','sample_rate','move_container_on_completion','sample_size_based_on_start_qty','tests'},
    }[kind]
    if set(changes)-allowed:raise ValueError('这些字段不适用于所选定义类型。')
    before=None
    if action=='create':
        if args.get('source_revision'):raise ValueError('创建不接受源版本。')
        if await lookup(entity,name):raise ValueError('该名称已有版本，请明确源ROR并使用revise。')
    else:
        source=reads.text(args.get('source_revision'),'源版本') if action=='revise' else revision
        if action=='update' and args.get('source_revision'):raise ValueError('更新不接受源版本。')
        if action=='revise' and await lookup(entity,name,revision):raise ValueError('目标版本已存在。')
        before=await read_state(kind,name,source)
        if before['IsFrozen'] is not False:raise ValueError('源定义已冻结，当前不维护。')
        if action=='revise' and (before['IsRevOfRcd'] is not True or source==revision):raise ValueError('升版必须明确不同于目标版本的当前ROR源版本。')
    body={'name':name,'revision':revision};dependencies=[];creating=action=='create'
    if creating:body['status']=1
    for key in ('description','status'):
        if key in changes:
            if key=='status' and (type(changes[key]) is not int or changes[key] not in (1,2)):raise ValueError('status仅1/2整数。')
            body[key]=changes[key]
    if kind=='sample_data_point':
        dtype=TYPES.get(changes.get('data_type')) if 'data_type' in changes else (before or {}).get('DataType')
        dtype=inspection.wire_int(dtype,'检验点DataType')
        if dtype not in TYPES.values():raise ValueError('仅维护数值检验点，创建必须明确data_type。')
        body['dataType']=dtype
        for arg,key in [('lower_limit','lowerLimit'),('upper_limit','upperLimit'),('default_value','defaultValue')]:
            if arg in changes:
                if changes[arg] not in (None,''):inspection.numeric(changes[arg],arg)
                body[key]=changes[arg]
        effective={k:(body[key] if key in body else (before or {}).get(k)) for k,key in (
            ('LowerLimit','lowerLimit'),('UpperLimit','upperLimit'))}
        lo=effective['LowerLimit'];hi=effective['UpperLimit']
        if lo not in (None,'') and hi not in (None,'') and inspection.numeric(lo,'下限')>inspection.numeric(hi,'上限'):raise ValueError('上下限反置。')
        default=body.get('defaultValue',(before or {}).get('DefaultValue'))
        if default not in (None,''):inspection.point_check({'Name':name,'DataType':dtype,**effective},default)
        if 'show_limits' in changes:scalar(body,'showLimits',changes['show_limits'])
        if 'uom_name' in changes:body['uom']=None if changes['uom_name'] is None else (await reference('UOMs',changes['uom_name'],dependencies,False))[0]
    elif kind=='sample_test':
        dtype={'counted':1,'measured':2}.get(changes.get('sample_type')) if 'sample_type' in changes else (before or {}).get('SampleType')
        dtype=inspection.wire_int(dtype,'SampleType')
        if dtype not in (1,2):raise ValueError('仅维护计数/测量测试，创建必须明确sample_type。')
        body['sampleType']=dtype
        if creating:body.update(allowMoveOnFailure=False,scrapCountedRejectsByReason=False)
        if before and inspection.wire_int(before.get('SampleType'),'源测试类型')!=dtype:raise ValueError('不在原版本中切换计数/测量类型，请创建新名称。')
        if 'instructions' in changes:body['instructions']=changes['instructions']
        if 'allow_move_on_failure' in changes:scalar(body,'allowMoveOnFailure',changes['allow_move_on_failure'])
        if 'reject_reason_group_name' in changes:body['aqlRejectReasons']=None if changes['reject_reason_group_name'] is None else (await reference('LossReasonGroups',changes['reject_reason_group_name'],dependencies,False))[0]
        if 'sample_data_points' in changes:
            items=changes['sample_data_points']
            if items is None:raise ValueError('检验点列表必须是明确列表，不接受null。')
            refs=[]
            for ref in items:
                wire,row=await reference('SampleDataPoints',ref,dependencies)
                if inspection.wire_int(row.get('DataType'),'关联点DataType') not in TYPES.values() or inspection.wire_int(row.get('Status'),'关联点Status')!=1:
                    raise ValueError('关联检验点须为Active数值类型。')
                refs.append(wire)
            if len({r['instanceId'] for r in refs})!=len(refs) or len({r['name'].casefold() for r in refs})!=len(refs):raise ValueError('关联检验点重复。')
            if creating:body['sampleDataPoints']=refs
            else:
                old=(before or {})['SampleDataPoints']
                body['sampleDataPoints-Expanded']=[{'index':n,'listItemAction':'delete'} for n in reversed(range(len(old)))]+[{'listItemAction':'add','value':r} for r in refs]
        else:refs=(before or {}).get('SampleDataPoints',[])
        if dtype==1 and refs or dtype==2 and not refs:raise ValueError('计数测试不接受测量点；测量测试必须有完整检验点。')
    else:
        if creating:body.update(moveContainerOnCompletion=False,sampleRate=1,sampleSizeBasedOnStartQty=False)
        for arg,key in [('move_container_on_completion','moveContainerOnCompletion'),('sample_size_based_on_start_qty','sampleSizeBasedOnStartQty')]:
            if arg in changes:scalar(body,key,changes[arg])
        if 'sample_rate' in changes:body['sampleRate']=inspection.int32(changes['sample_rate'],'采样频率',1)
        for arg,ent,key in [('aql_level_name','AQLLevels','aqlLevel'),('inspection_level_name','InspectionLevels','inspectionLevel')]:
            if arg in changes:body[key]=None if changes[arg] is None else (await reference(ent,changes[arg],dependencies,False))[0]
        if 'tests' in changes:
            if changes['tests'] is None or not changes['tests']:raise ValueError('tests须为非空明细变更列表，不用空列表隐式清空计划。')
            await plan_rows(changes['tests'],before,body,dependencies,creating)
        if creating and not body.get('samplingPlanDetails'):raise ValueError('新抽样计划必须明确测试明细。')
    method={'create':'POST','update':'PUT','revise':'PATCH'}[action]
    target=before['InstanceID'] if before else None
    writes=resources(entity,{'Name':name,**({'InstanceID':target} if target else {})})
    read_claims=set().union(*(resources(d['entity'],d['row']) for d in dependencies)) if dependencies else set()
    return {'kind':'inspection_definition','action':action,'name':name,'revision':revision,'definition_kind':kind,
        'entity':entity,'target_id':target,'scope_key':'inspection-definition:'+entity+':'+name.casefold(),
        'api':'modeling','method':method,'path':'/api/'+entity+('/'+target if action=='update' else ''),
        'body':body,'before':before,'dependencies':dependencies,'resource_reads':sorted(read_claims),'resource_writes':sorted(writes),
        'preview':{'static_contract_verified':True,'echo_verified':False,'dynamic_validation_verified':False,
            'note':'Modeling CRUD未执行非写入回显，尚未验证服务端业务规则；PUT仅改请求字段，PATCH由MES复制当前ROR。'},
        'marker':marker,'arguments':args,'environment':plans.environment()}


@mcp.tool
async def prepare_inspection_definition(definition_kind:Literal['sample_data_point','sample_test','sampling_plan'],
        action:Literal['create','update','revise'],name:str,revision:str,changes:InspectionDefinitionChanges,source_revision:str='')->str:
    """准备检验点SampleDataPoint、测试SampleTest或抽样计划SamplingPlan的创建/更新/升版，不写MES。变化字段按类型闭合校验；引用必须固定版本。测量点列表为明确完整替换；计划tests为add/update/remove明细增量，重名需detail_instance_id。升版仅明确当前ROR源；冻结拒绝。Modeling为静态预览，没有execute=false验证，不等于业务批准。由execute_quality_transaction按风险策略单次提交，新建定义可免逐项确认；更新/升版仍需确认。"""
    from tools import quality_plans as plans
    try:
        normalized=InspectionDefinitionChanges.model_validate(changes).model_dump(exclude_unset=True)
        return await plans.prepare('inspection_definition',dict(definition_kind=definition_kind,action=action,name=name,
            revision=revision,changes=normalized,source_revision=source_revision))
    except (ValueError,TypeError,KeyError,AttributeError) as error:return reads.encode({'submitted':False,'error':str(error)})


async def build_binding(args,marker):
    from tools import quality_plans as plans
    deps=[];product_ref,product=await reference('Products',{'name':args['product_name'],'revision':args['product_revision']},deps)
    if inspection.wire_bool(product.get('IsFrozen'),'产品冻结状态') is not False:raise ValueError('产品已冻结或状态未知。')
    plan=await read_state('sampling_plan',args['sampling_plan_name'],args['sampling_plan_revision'])
    if plan['Status']!=1:raise ValueError('关联抽样计划不是Active状态。')
    before={k:copy.deepcopy(product.get(k)) for k in COMMON+('SamplingPlan','SamplingPlanResolved')}
    deps=[{'entity':'SamplingPlans','row':plan}]
    return {'kind':'sampling_binding','action':'bind','name':product_ref['name'],'revision':product_ref['revision'],'entity':'Products',
        'target_id':product_ref['instanceId'],'scope_key':'product-sampling:'+product_ref['name'].casefold(),
        'api':'modeling','method':'PUT','path':'/api/Products/'+product_ref['instanceId'],
        'body':{'samplingPlan':inspection.revision_ref(plan)},'before':before,'dependencies':deps,
        'resource_reads':sorted(resources('SamplingPlans',plan)),'resource_writes':sorted(resources('Products',product)),
        'preview':{'static_contract_verified':True,'echo_verified':False,'dynamic_validation_verified':False,
            'note':'仅设置产品直接SamplingPlan引用，不修改现有容器已冻结的检验任务，不据此宣称当前容器已获得任务。'},
        'marker':marker,'arguments':args,'environment':plans.environment()}


@mcp.tool
async def prepare_product_sampling_plan(product_name:str,product_revision:str,sampling_plan_name:str,sampling_plan_revision:str)->str:
    """准备固定产品版本关联固定SamplingPlan版本，仅PUT直接SamplingPlan引用，保留其他产品配置。不批量改现有容器任务，不解绑继承计划、不自动启动检验。静态预览，确认后execute_quality_transaction单次提交；结果先unknown。"""
    from tools import quality_plans as plans
    return await plans.prepare('sampling_binding',dict(product_name=product_name,product_revision=product_revision,
        sampling_plan_name=sampling_plan_name,sampling_plan_revision=sampling_plan_revision))
