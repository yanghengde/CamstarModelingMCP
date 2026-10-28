"""Bounded quality reads, tied to this deployment's Swagger and SQL metadata."""
import json
import re
from typing import Literal

from tools import mcp
from tools.boms import encode,bom_ref
from tools.modeling_details import raw_json,_has_next_link
from tools.process_context import ReadContext
from tools.container_inquiry import get_container_details
from core.http_client import request
from config import CAMSTAR_QUERY_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL

ReferenceKind=Literal['ncr_failure','ncr_cause','ncr_resolution','ncr_type','quality_resolution','disposition',
    'failure_mode','failure_severity','failure_action','organization','classification','subclassification','priority',
    'sampling_plan','sample_test','sample_data_point','aql_level','loss_reason','loss_reason_group','inspection_level','auto_hold','spc_control','spc_chart','hold_reason','release_reason','defect_reason','rework_reason']
REFERENCES={
 'ncr_failure':('NCRFailureCodes',False,''),'ncr_cause':('NCRCauseCodes',False,''),
 'ncr_resolution':('NCRResolutionCodes',False,''),'ncr_type':('NCRFailureTypes',False,''),
 'quality_resolution':('QualityResolutionCodes',False,''),'disposition':('Dispositions',False,''),
 'failure_mode':('FailureModes',False,''),'failure_severity':('FailureSeverities',False,''),
 'failure_action':('FailureActionTypes',False,''),'organization':('Organizations',False,''),
 'classification':('Classifications',False,''),'subclassification':('SubClassifications',False,''),
 'priority':('PriorityLevels',False,''),'sampling_plan':('SamplingPlans',True,'SamplingPlanDetails'),
 'sample_test':('SampleTests',True,'SampleDataPoints'),'inspection_level':('InspectionLevels',False,'LotSizeDetails'),
 'sample_data_point':('SampleDataPoints',True,''),'aql_level':('AQLLevels',False,'SampleSizeDetails'),
 'loss_reason':('LossReasons',False,''),'loss_reason_group':('LossReasonGroups',False,'ResolvedEntries'),
 'auto_hold':('ContainerAutoHoldReqs',False,'ContainerList,LotList'),'spc_control':('SPCControls',False,''),
 'spc_chart':('SPCChartDefs',False,''),'hold_reason':('HoldReasons',False,''),
 'release_reason':('ReleaseReasons',False,''),'defect_reason':('ContainerDefectReasons',False,''),
 'rework_reason':('ReworkReasons',False,''),
}
NCR_COLUMNS='NonconformanceReportId,NonconformanceReportName,ContainerId,NCRStatus,ChangeCount,NCRComment,NCRFailureTypeId,NCRCauseCodeId,NCRResolutionCodeId,CreationDateGMT,CloseDateGMT,EscalatedEventId'
CAPA_COLUMNS='CAPAId,CAPAName,Category,Status,ChangeCount,IsFrozen,IsSubmitted,OrganizationId,BriefDescription,Description,ClassificationId,SubClassificationId,PriorityLevelId,SystemicIssue,OccurrenceDateGMT,CloseDateGMT,CloseDescription,QualityResolutionCodeId,ProcessModelId'


@mcp.tool
async def get_quality_api_inventory()->str:
    """读取2026-10-26现场质量API核查与MCP覆盖清单。标明已封装、已有旧工具及仅发现待验证的接口；不是实时健康检查，更不是质量业务/权限验收。"""
    return encode({'checked_date':'2026-10-26','implementation_updated_date':'2026-10-28',
        'source':'live Swagger index + schemas + EDM + Query column metadata; archived docs/quality-fixtures',
        'live_health_check':False,'families':[
            {'domain':'NCR','checked_date':'2026-10-28','discovered':['NCRCreate','NCRUpdate','NCRClose','NCREscalate','NCRCloseQuality','GetNCRs','NCRHistoryInquiry'],
             'tools':['prepare_nonconformance_report','list_nonconformance_reports','get_nonconformance_report','get_quality_history','get_ncr_escalation_options','prepare_ncr_escalation','prepare_quality_ncr_closure'],
             'limitations':'创建/更新/关闭及按组织唯一分类映射升级质量事件已封装；不覆盖签名/审批，原报告最终状态待验收。NCRCloseQuality仅处理原关联Event已Closed而仍Open的NCR，实际写入及副作用待现场验收。'},
            {'domain':'CAPA','discovered':['CreateCAPA','UpdateCAPA','CloseQualityObject'],
             'tools':['prepare_capa','prepare_capa_closure','get_quality_closure_options'],
             'limitations':'仅类别5创建/更新及Close处置关闭计划；不覆盖流程审批/签名，真实提交/关闭验收待补。'},
            {'domain':'质量对象','discovered':['QualityObjectInquiry','QualityObjectHistoryInquiry','OpenQualityObject','CloseQualityObject','TriageQualityObject'],
             'tools':['list_quality_objects','get_quality_object_details','get_quality_history','get_quality_closure_options'],
             'limitations':'查询/历史及关闭处置候选已封装；关闭计划仅CAPA，通用开启/关闭/分派尚未开放。'},
            {'domain':'质量检验/抽样','discovered':['SamplingPlans','SampleTests','SampleDataPoints','CollectSamplingData','CollectLotSamplingData','CreateSamplingLot','UpdateSamplingLot','ExecuteSamplingLot','CurrentSamplingStatusUpdate'],
             'tools':['list_quality_references','get_quality_reference_details','get_container_quality_context','get_inspection_test_definition',
                 'get_container_inspection_tasks','prepare_container_inspection','get_container_inspection_history','prepare_inspection_definition',
                 'prepare_product_sampling_plan','get_sampling_lot_details','prepare_sampling_lot_create',
                 'prepare_lot_inspection','get_sampling_lot_readiness','prepare_sampling_lot_completion','get_sampling_lot_history',
                 'get_inspection_result_history','find_quality_execution_candidates','reconcile_quality_execution',
                 'get_sampling_reference_definition','prepare_sampling_reference_definition','preview_sampling_table_lookup',
                 'prepare_sampling_lot_update','get_sampling_lot_maintenance_history','check_sampling_plan_configuration',
                 'get_current_sampling_status','prepare_current_sampling_status_update','prepare_sampling_lot_rework'],
             'limitations':'单容器计数/测量提交计划、检验定义及AQL/检验水平维护、配置查表预览、产品关联、抽样批创建及只读详情。Modeling仅静态校验；零计数且无有效采样历史批的数量/频率调整、批采样记录及MES已通过批的OK执行计划已封装；另有同Workflow明确默认路径的受限批返工计划；批报废、全检转换和自动扣量未开放，单容器移站false；真实写入验收待补。'},
            {'domain':'SPC','discovered':['SPCControls','SPCChartDefs','RecordSPCViolation'],
             'tools':['list_quality_references','get_quality_reference_details'],'limitations':'只读配置，不执行SPC计算/判异或自动放行。'},
            {'domain':'不良/返工/冻结','discovered':['ContainerDefect','Rework','Hold','Release'],
             'tools':['container_defect','rework','hold_container','release_container'],'limitations':'已有直接封装，不能假定具备新NCR/CAPA事务journal。需确认、现场写入验收待完成，不盲重试。'},
            {'domain':'报废','checked_date':'2026-10-28','discovered':['PartScrap','ScrapReasons','ChangeQty','LossReasons','ExecuteSamplingLot'],'tools':['prepare_container_quantity_loss','get_sampling_lot_scrap_readiness'],
             'limitations':'单个基础Container支持原生LossDetails的部分损失/明确全量报废准备；不支持Batch/抽样批成员/层级/双单位/载具。PartScrap要求resource/scrapQty，不等于Container报废。抽样批报废仅只读前置检查，原因类型冲突及部署效果未核实，结果3仍阻断，真实写入待验收。'},
        ]})


def valid_id(value):return isinstance(value,str) and re.fullmatch(r'[0-9a-fA-F]{16}',value) is not None
def identifier(ref):return (ref.get('InstanceID') or ref.get('InstanceId')) if isinstance(ref,dict) else None
def text(value,label='名称',maximum=128):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum or any(c in value for c in '\r\n\x00'):
        raise ValueError(label+'须为非空、无换行的字符串。')
    return value.strip()
def literal(value):return "N'"+value.replace("'","''")+"'"
def paging(offset,limit):
    if type(offset) is not int or not 0<=offset<=1000000 or type(limit) is not int or not 1<=limit<=100:
        raise ValueError('offset=0..1000000，limit=1..100。')


async def sql_rows(query,maximum):
    data=json.loads(await request('POST','/api/AdHoc',base_url=CAMSTAR_QUERY_BASE_URL,
        body={'queryText':query},response_formatter=raw_json,retry_read_only=True))
    if not isinstance(data,list) or len(data)>maximum or any(not isinstance(r,dict) for r in data):
        raise ValueError('固定质量查询未返回受限记录列表。')
    return data


async def find_record(kind,name='',instance_id=None):
    table,key,label,columns={'ncr':('NonconformanceReport','NonconformanceReportId','NonconformanceReportName',NCR_COLUMNS),
                             'capa':('CAPA','CAPAId','CAPAName',CAPA_COLUMNS)}[kind]
    if instance_id is not None and not valid_id(instance_id):raise ValueError('显式ID须为16位十六进制。')
    if not instance_id:name=text(name)
    where=f"{key}='{instance_id}'" if instance_id else f'{label}={literal(name)}'
    rows=await sql_rows(f'SELECT TOP 2 {columns} FROM {table} WHERE {where}',2)
    if len(rows)>1:raise ValueError('质量记录名称不唯一。')
    if rows and (not valid_id(rows[0].get(key)) or instance_id and rows[0].get(key)!=instance_id or name and rows[0].get(label)!=name):raise ValueError('质量记录身份不匹配。')
    return rows[0] if rows else None


async def ncr_failure_assignments(instance_id):
    from tools.quality_inspection import wire_int
    if not valid_id(instance_id):raise ValueError('NCR关联查询ID无效。')
    # FieldId 10019 is NonconformanceReport.NCRFailureCodes in the deployed EDM.
    rows=await sql_rows('SELECT TOP 51 a.NonconformanceReportId,a.FieldId,a.Sequence,a.NCRFailureCodesId,'
        'c.NCRFailureCodeName,c.ChangeCount AS FailureCodeChangeCount FROM NonconformanceReportNCRFailure a '
        'LEFT JOIN NCRFailureCode c ON c.NCRFailureCodeId=a.NCRFailureCodesId '
        +f"WHERE a.NonconformanceReportId='{instance_id}' AND a.FieldId=10019 ORDER BY a.Sequence,a.NCRFailureCodesId",51)
    if len(rows)>50:raise ValueError('NCR失败代码超过50个，不能完整维护。')
    for row in rows:
        row['FieldId']=wire_int(row.get('FieldId'),'NCR失败码关联字段')
        row['Sequence']=wire_int(row.get('Sequence'),'NCR失败码顺序')
        if (row.get('NonconformanceReportId')!=instance_id or row.get('FieldId')!=10019
                or type(row.get('Sequence')) is not int or row['Sequence']<0
                or not valid_id(row.get('NCRFailureCodesId'))):raise ValueError('NCR失败代码关联身份/顺序未确认。')
        text(row.get('NCRFailureCodeName'),'现有NCR失败代码')
    if (len({r['Sequence'] for r in rows})!=len(rows)
            or len({r['NCRFailureCodesId'] for r in rows})!=len(rows)
            or len({r['NCRFailureCodeName'].casefold() for r in rows})!=len(rows)):
        raise ValueError('NCR失败代码关联重复或不唯一。')
    if rows!=sorted(rows,key=lambda r:(r['Sequence'],r['NCRFailureCodesId'])):raise ValueError('NCR失败代码顺序不完整。')
    return rows


async def ncr_snapshot(name='',instance_id=None):
    row=await find_record('ncr',name,instance_id)
    if row is None:return None
    assigned=await ncr_failure_assignments(row['NonconformanceReportId'])
    if await find_record('ncr',name,row['NonconformanceReportId'])!=row:raise ValueError('读取失败代码时NCR已变化，请重新查询。')
    return {**row,'failure_code_assignments':assigned}


async def closure_options(body=None):
    raw=await request('POST','/api/CloseQualityObject/RequestSelectionValues',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body=body or {},params={'selectionValuesExpression':'ResolutionAction'},response_formatter=raw_json,retry_read_only=True)
    response=json.loads(raw)
    if not isinstance(response,dict):raise ValueError('质量关闭选项响应无效。')
    data=response.get('data',response);selection=data.get('ResolutionAction') if isinstance(data,dict) else None
    if (response.get('error') or response.get('ErrorCode') or not isinstance(selection,dict)
            or data.get('error') or data.get('ErrorCode') or _has_next_link(data)):
        raise ValueError('质量关闭选项未完整返回。')
    rows=selection.get('SelectionValuesEx')
    if not isinstance(rows,list) or not 1<=len(rows)<=100:raise ValueError('质量关闭选项列表无效。')
    for field in ('RecordCount','RowCount'):
        if type(selection.get(field)) not in (int,str) or str(selection[field])!=str(len(rows)):
            raise ValueError('质量关闭选项被截断或数量不符。')
    for row in rows:
        if (not isinstance(row,dict) or type(row.get('Value')) not in (int,str)
                or not re.fullmatch(r'[1-9][0-9]{0,9}',str(row['Value'])) or int(row['Value'])>2147483647):
            raise ValueError('质量关闭动作原码无效。')
        text(row.get('Name'),'关闭动作');text(row.get('Label'),'关闭动作说明',maximum=1000)
    if len({str(r['Value']) for r in rows})!=len(rows) or len({r['Name'] for r in rows})!=len(rows):
        raise ValueError('质量关闭动作重复或不唯一。')
    return rows


@mcp.tool
async def get_quality_closure_options()->str:
    """实时只读CloseQualityObject的ResolutionAction候选及原码。当前仅支持CAPA的Close（无需后续行动）关闭计划；AttachToCAPA/CreateCAPA未开放。候选存在不表示目标可关闭、审批完成或有权限。"""
    try:
        return encode({'options':await closure_options(),'complete':True,'supported_plan_actions':['Close'],
            'target_permission_verified':False,'quality_verdict':None})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})


async def container_identity(container_name,instance_id=None):
    name=text(container_name,'容器名')
    if instance_id is not None and not valid_id(instance_id):raise ValueError('容器ID无效。')
    data=json.loads(await get_container_details(name,instance_id,view='status'))
    status=data.get('data',{}).get('ContainerStatusDetails') or {};ref=status.get('Container') or {}
    cid=identifier(ref)
    if data.get('error') or not valid_id(cid) or ref.get('Name')!=name or instance_id is not None and instance_id!=cid:
        raise ValueError('容器身份未确认。')
    return {'name':name,'instanceId':cid},status


async def resolve_reference(kind,name):
    entity,revisioned,_=REFERENCES[kind]
    if revisioned:raise ValueError('版本引用须明确版本/ID，不采用名称默认ROR。')
    row=await ReadContext(3).resolve(entity,{'Name':text(name)},revisioned=False)
    if not valid_id(row.get('InstanceID')):raise ValueError('质量参考数据未返回有效ID。')
    return {'name':row['Name'],'instanceId':row['InstanceID']}


@mcp.tool
async def list_quality_references(kind:ReferenceKind,name_contains:str='',offset:int=0,limit:int=20)->str:
    """只读质量原因/失败/解决代码、组织分类、处置、抽检计划/测试、自动冻结和SPC配置。固定实体白名单；多取1条判后页。列出原因不证明适用于当前事务；不自动挑选原因，不计算质量结论。"""
    try:
        paging(offset,limit);entity,revisioned,_=REFERENCES[kind]
        params={'$top':limit+1,'$skip':offset,'$orderby':'Name,Revision' if revisioned else 'Name',
                '$select':'InstanceID,Name,Description,IsFrozen'+(',Revision,IsRevOfRcd' if revisioned else '')}
        if name_contains:params['$filter']="contains(Name,'"+text(name_contains).replace("'","''")+"')"
        data=await ReadContext(1).get(entity,params=params);rows=data.get('value')
        if not isinstance(rows,list) or any(not isinstance(r,dict) or not valid_id(r.get('InstanceID')) for r in rows):raise ValueError('质量参考列表不完整。')
        more=len(rows)>limit or _has_next_link(data)
        return encode({'source':entity,'rows':rows[:limit],'has_more':more,'next_offset':offset+limit if more else None,'complete':offset==0 and not more})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})


@mcp.tool
async def get_quality_reference_details(kind:ReferenceKind,name:str='',revision:str='',instance_id:str|None=None,use_ror:bool=False)->str:
    """只读质量配置详情。抽检计划/测试需明确版本、ID或显式ROR；展开SamplingPlanDetails/SampleDataPoints。自动冻结展开容器/批列表。SPC为配置读取，不提供控制图计算或自动放行。"""
    try:
        entity,revisioned,expand=REFERENCES[kind];ctx=ReadContext(4)
        if instance_id is not None and not valid_id(instance_id):raise ValueError('配置ID无效。')
        if revisioned:ref=bom_ref(name,revision,instance_id,use_ror)
        else:
            if revision or use_ror:raise ValueError('此质量配置不是版本对象。')
            ref={'InstanceID':instance_id} if instance_id else {'Name':text(name)}
            if name:ref['Name']=text(name)
        root=await ctx.resolve(entity,ref,revisioned)
        if expand:
            expanded=await ctx.get(entity,root['InstanceID'],{'$expand':expand})
            if expanded.get('InstanceID')!=root['InstanceID']:raise ValueError('展开配置身份不符。')
            for field in expand.split(','):
                if not isinstance(expanded.get(field),list):raise ValueError('配置明细未展开。')
                root[field]=expanded[field]
            complete=not _has_next_link(expanded)
        else:complete=True
        if len(json.dumps(root,ensure_ascii=False))>100000:raise ValueError('质量配置超过输出上限，不能完整展示。')
        return encode({'source':entity,'data':root,'complete':complete,'quality_verdict':None,'note':'配置完整不等于动态规则、权限、质量或放行已通过。'})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})


@mcp.tool
async def list_nonconformance_reports(container_name:str='',container_instance_id:str|None=None,ncr_status:int|None=None,offset:int=0,limit:int=20)->str:
    """只读有界NCR不合格报告列表，可按明确容器和原始NCRStatus码筛选。固定SQL字段已核对部署元数据；返回原码，不猜状态枚举。多取1条，不把当前页称为全部记录。"""
    try:
        paging(offset,limit);where=[];expected_cid=None
        if container_instance_id and not container_name:raise ValueError('提供容器ID时还需名称核对。')
        if container_name:
            ref,_=await container_identity(container_name,container_instance_id);expected_cid=ref['instanceId'];where.append(f"ContainerId='{expected_cid}'")
        if ncr_status is not None:
            if type(ncr_status) is not int or not 0<=ncr_status<=2147483647:raise ValueError('NCRStatus须为明确非负整数原码。')
            where.append(f'NCRStatus={ncr_status}')
        query=f'SELECT {NCR_COLUMNS} FROM NonconformanceReport'+(' WHERE '+' AND '.join(where) if where else '')
        query+=f' ORDER BY CreationDateGMT DESC,NonconformanceReportId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY'
        rows=await sql_rows(query,limit+1)
        if any(not valid_id(r.get('NonconformanceReportId')) or expected_cid is not None and r.get('ContainerId')!=expected_cid
                or ncr_status is not None and r.get('NCRStatus')!=ncr_status for r in rows):raise ValueError('NCR记录ID/筛选身份不匹配。')
        if len({r['NonconformanceReportId'] for r in rows})!=len(rows):raise ValueError('NCR列表重复，分页不可确认。')
        more=len(rows)>limit
        return encode({'rows':rows[:limit],'has_more':more,'next_offset':offset+limit if more else None,'page_complete':True,'complete':False,'status_mapping':'raw_unverified'})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})


@mcp.tool
async def get_nonconformance_report(name:str='',instance_id:str|None=None)->str:
    """按明确NCR名称或ID只读当前报告、容器ID、状态原码、评论和完整失败代码关联（最多50条，含名称/ID/顺序）。根记录变化或关联不完整时失败。历史用get_quality_history(kind=ncr)，不猜测已关闭。"""
    try:
        row=await ncr_snapshot(name,instance_id)
        return encode({'data':row,'found':row is not None,'complete':True,'scope':'current_ncr_record','quality_verdict':None})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})


@mcp.tool
async def list_quality_objects(category:int|None=None,status:int|None=None,offset:int=0,limit:int=20)->str:
    """只读QualityObject视图有界列表，返回真实Id/Type/Category/Status和组织/责任人等。视图没有名称列，不用ID拼造名称；用get_quality_object_details(instance_id=返回Id)读取详情。category/status为原始整数。"""
    try:
        paging(offset,limit);where=[]
        for field,value in [('Category',category),('Status',status)]:
            if value is not None:
                if type(value) is not int or not 0<=value<=2147483647:raise ValueError('分类/状态须为原始整数。')
                where.append(f'[{field}]={value}')
        query='SELECT Id,[Type],QualityObjectType,Category,Status,Organization,Owner,[Role],PriorityLevel,TriageComplete,ReportedDateGMT FROM QualityObject'
        query+=(' WHERE '+' AND '.join(where) if where else '')+f' ORDER BY ReportedDateGMT DESC,Id OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY'
        rows=await sql_rows(query,limit+1)
        if any(not valid_id(r.get('Id')) or category is not None and r.get('Category')!=category
                or status is not None and r.get('Status')!=status for r in rows):raise ValueError('质量对象ID/筛选不匹配。')
        if len({r['Id'] for r in rows})!=len(rows):raise ValueError('质量对象列表重复，分页不可确认。')
        more=len(rows)>limit
        return encode({'rows':rows[:limit],'has_more':more,'next_offset':offset+limit if more else None,'page_complete':True,'complete':False,'scope':'quality_object_view'})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})


async def object_details(name='',instance_id=None):
    if instance_id is not None and not valid_id(instance_id):raise ValueError('质量对象ID无效。')
    ref={'instanceId':instance_id} if instance_id else {'name':text(name)}
    if name:ref['name']=text(name)
    raw=await request('POST','/api/QualityObjectInquiry',base_url=CAMSTAR_QUERY_BASE_URL,body={'qualityObject':ref},
        params={'execute':'true','$select':'QualityObject,QualityObjectInquiryDetail','$expand':'QualityObjectInquiryDetail'},response_formatter=raw_json,retry_read_only=True)
    data=json.loads(raw);data=data.get('data',data)
    returned=data.get('QualityObject') or {}
    if (data.get('error') or data.get('ErrorCode') or not valid_id(identifier(returned))
            or instance_id and identifier(returned)!=instance_id or name and returned.get('Name')!=name):raise ValueError('质量对象查询未确认身份。')
    detail=data.get('QualityObjectInquiryDetail')
    if not isinstance(detail,dict) or detail.get('Status') is None or detail.get('Category') is None or _has_next_link(data):raise ValueError('质量对象详情未完整返回。')
    return data


@mcp.tool
async def get_quality_object_details(name:str='',instance_id:str|None=None)->str:
    """固定Query/QualityObjectInquiry读取质量事件/CAR/CAPA等详情。名称/ID核对后返回状态、分类、组织、责任人和审批信息；不把查询当成开启/关闭/放行，不推断角色许可。"""
    try:return encode({'data':await object_details(name,instance_id),'complete':True,'scope':'quality_object_inquiry','quality_verdict':None})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})


@mcp.tool
async def get_quality_history(kind:Literal['ncr','quality_object'],name:str,instance_id:str|None=None,start_row:int=1,limit:int=20)->str:
    """固定NCRHistoryInquiry/QualityObjectHistoryInquiry只读历史，start_row从1起、limit=1..100。核对对象引用，不按数组下标盲配历史说明；空页不证明没有事务，不证明本次提交成功。"""
    try:
        name=text(name);paging(start_row-1,limit)
        if type(start_row) is not int or start_row<1:raise ValueError('start_row须为正整数。')
        if instance_id is not None and not valid_id(instance_id):raise ValueError('历史目标ID无效。')
        service,field={'ncr':('NCRHistoryInquiry','nonconformanceReport'),'quality_object':('QualityObjectHistoryInquiry','qualityObject')}[kind]
        ref={'name':name}
        if instance_id:ref['instanceId']=instance_id
        body={field:ref,'resultsetStartRow':start_row,'resultsetSizeLimit':limit,'sortAscending':False,'showFullHistory':True,'showAllTxnTypes':True}
        data=json.loads(await request('POST','/api/'+service,base_url=CAMSTAR_QUERY_BASE_URL,body=body,
            params={'execute':'true','$select':field[0].upper()+field[1:]+',Histories,HistoryDisplayTexts'},response_formatter=raw_json,retry_read_only=True))
        data=data.get('data',data);actual=data.get(field[0].upper()+field[1:]) or {};rows=data.get('Histories');labels=data.get('HistoryDisplayTexts')
        if data.get('error') or data.get('ErrorCode') or actual.get('Name')!=name or not valid_id(identifier(actual)) or instance_id and identifier(actual)!=instance_id:
            raise ValueError('历史对象身份未确认。')
        if not isinstance(rows,list) or len(rows)>limit or _has_next_link(data):raise ValueError('历史页无效/被截断。')
        return encode({'data':data,'page_complete':True,'complete':False,'start_row':start_row,'limit':limit,'has_more':None,
            'next_start_row_to_try':start_row+limit if len(rows)==limit else None,'labels_aligned':isinstance(labels,list) and len(labels)==len(rows),
            'submission_outcome':'unknown','note':'各页不是一致性快照；不解除未知事务占用。'})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})


@mcp.tool
async def get_container_quality_context(container_name:str,container_instance_id:str|None=None,offset:int=0,limit:int=20)->str:
    """只读容器冻结计数、质量控制/抽检标志及ContainerSampleData的必采数、已采数、拒收数和计划/测试ID。不是合格判定，不把CurrentHoldCount/采样标志当成完整冻结或放行历史。采样行有界分页。"""
    try:
        paging(offset,limit);ref,_=await container_identity(container_name,container_instance_id);cid=ref['instanceId']
        rows=await sql_rows("SELECT ContainerId,ContainerName,Status,CurrentHoldCount,HoldReasonId,InQualityControl,SamplingRequired,SamplingPassed,SamplingLotId,Qty,ChangeCount FROM Container WHERE ContainerId='"+cid+"'",1)
        if len(rows)!=1 or rows[0].get('ContainerId')!=cid or rows[0].get('ContainerName')!=ref['name']:raise ValueError('质量上下文容器身份变化。')
        samples=await sql_rows('SELECT ContainerSampleDataId,ContainerId,SamplingPlanId,SampleTestId,InspectionLevelId,AQLLevelId,TotalSamplesRequired,CompletedSamples,Rejects,AllowedRejects,InspectAllContQty,InspectAllQtyOnFailure FROM ContainerSampleData '
            +f"WHERE ContainerId='{cid}' ORDER BY ContainerSampleDataId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY",limit+1)
        if any(r.get('ContainerId')!=cid or not valid_id(r.get('ContainerSampleDataId')) for r in samples):raise ValueError('采样上下文父容器不匹配。')
        more=len(samples)>limit
        return encode({'container':ref,'status':rows[0],'sampling_rows':samples[:limit],'page_complete':True,'has_more':more,'next_offset':offset+limit if more else None,
            'complete':False,'quality_verdict':None,'release_authorized':False,'note':'仅当前字段和本页采样数据；不执行采样、扣减、移站或关闭质量对象。'})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})
