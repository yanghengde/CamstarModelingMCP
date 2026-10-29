"""Restricted standalone plain Event close plans, without NCR child transactions."""
import copy
import json
import re
from tools import mcp,quality as q,quality_plans as p,quality_event_triage as t,quality_event_update as u,quality_inspection as i
from tools.quality_definitions import resources
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SERVICE='CloseQualityObject'
SERVICE_TYPE=7712
RESOLVE_ACTION=64
SCHEMA=u.SCHEMA|{'QualityResolutionCode':'QualityResolutionCodeId,ChangeCount'}
SELECT=('QualityObject,Organization,ResolutionAction,QualityResolutionCode,CloseDescription,Comments,Employee,'
        'AvailableActions,IgnoreProcessModel,ApprovalRequired,ApprovalSheetMap,ApprovalSheetTemplate,ApprovalSheetStatus,'
        'ESigRequirement,AdditionalESigReqs,QualityObjectDetail')
EXPAND='QualityObjectDetail($select='+u.CURRENT_FIELDS+')'

async def source(args):
    if args.get('allow_complex_context'):
        from tools.quality_event_lifecycle import source as complex_source
        return await complex_source(args,SERVICE_TYPE,1,True)
    await u.schema_check(SCHEMA)
    before=await u.context(args,signature_txn_type=SERVICE_TYPE)
    row=before['event']
    if not row['TriageComplete'] or row['IsCARRequiredToClose'] or before['linked_ncrs']:
        raise ValueError('受限事件关闭要求已分流、无CAR要求且无关联NCR；不处理NCR联动或清除CAR策略。')
    return before

async def contract():
    raw=await request('GET','/swagger/CloseQualityObject/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json)
    try:
        doc=json.loads(raw);props=doc['components']['schemas'][SERVICE]['properties']
        for key in ('qualityObject','organization','qualityResolutionCode'):
            if props[key]['$ref'].split('/')[-1]!='NamedObjectRef':raise ValueError('引用类型不符')
        for key,kind in [('resolutionAction','integer'),('closeDescription','string'),('approvalRequired','boolean'),('availableActions','integer')]:
            if props[key]['type']!=kind:raise ValueError('关闭类型不符')
    except (ValueError,TypeError,KeyError,AttributeError) as exc:raise ValueError('当前关闭接口契约未核实。') from exc
    return p.digest(props)

def body_for(args,before,code,action,marker):
    u.text_value(args['close_description'],'关闭说明',4000,True)
    u.text_value(args.get('comments',''),'关闭备注',200)
    return dict(qualityObject=dict(name=before['event']['EventName'],instanceId=before['event']['EventId']),
        organization=t.ref(before['organization']),qualityResolutionCode=t.ref(code),resolutionAction=action,
        closeDescription=args['close_description'],comments=marker+(' '+args['comments'] if args.get('comments') else ''))

def verify(data,body,before):
    if before.get('complex_context'):
        from tools.quality_event_lifecycle import verify as complex_verify
        result=complex_verify(data,body,before,64,True)
        if u.i.wire_bool(data.get('ApprovalRequired'),'关闭审批要求') is not False:
            raise ValueError('仍需发起审批，不通过关闭工具启动或绕过审批。')
        for field in ('ApprovalSheetMap','ApprovalSheetTemplate'):
            if field not in data or data[field] is not None:raise ValueError('存在审批路由/模板，需审批流程处理。')
        if 'ApprovalSheetStatus' not in data or data['ApprovalSheetStatus'] not in (None,40,'40'):
            raise ValueError('关闭审批状态未核实。')
        if q.identifier(data.get('Employee'))!=before['event']['OwnerId']:raise ValueError('关闭员工不符。')
        result['event_closed_verified']=False
        result['complex_closure_note']='保持CAR/流程及已批准审批，原生最终核实流程完成及批次处置；EventData为空，不调用制造NCR子关闭。'
        return result
    if not p.wire_matches(body,data):raise ValueError('事件关闭请求未完整回显。')
    mask=i.wire_int(data.get('AvailableActions'),'原生可用动作')
    if mask<0 or mask&RESOLVE_ACTION!=RESOLVE_ACTION:raise ValueError('当前事件/账号缺少原生Resolve动作。')
    for field in ('IgnoreProcessModel','ApprovalRequired'):
        if i.wire_bool(data.get(field),field) is not False:raise ValueError('原生关闭要求审批或流程绕过标志未知。')
    for field in ('ESigRequirement','ApprovalSheetMap','ApprovalSheetTemplate','ApprovalSheetStatus'):
        if field not in data or data[field] is not None:raise ValueError('原生审批/签名上下文非空或未知。')
    if 'AdditionalESigReqs' not in data or data['AdditionalESigReqs'] not in (None,[]):raise ValueError('额外签名要求非空或未知。')
    row=before['event'];detail=data.get('QualityObjectDetail')
    if not isinstance(detail,dict) or not set(u.CURRENT_FIELDS.split(','))<=detail.keys():raise ValueError('当前关闭状态上下文不完整。')
    for field in ('Category','Status'):
        if i.wire_int(detail[field],field)!=row[field]:raise ValueError('关闭对象状态/类别不一致。')
    for field,col in [('Organization','OrganizationId'),('Owner','OwnerId'),('Role','RoleId'),('PriorityLevel','PriorityLevelId')]:
        if q.identifier(detail[field])!=row[col]:raise ValueError('关闭对象归属/优先级不一致。')
    for field,want in [('TriageComplete',True),('IsCARRequiredToClose',False),('IsApprovalRequired',False),('UserHasRole',True),('UserIsOwner',True)]:
        if i.wire_bool(detail[field],field) is not want:raise ValueError('关闭对象分流/CAR/账号或审批状态不满足要求。')
    if any(detail[k] is not None for k in ('ProcessModel','CompletionApprovalSheet','EntryForApproval')) or detail['CompletionApprovalStatus'] not in (None,''):
        raise ValueError('关闭对象存在流程/审批上下文。')
    if q.identifier(data.get('Employee'))!=row['OwnerId']:raise ValueError('原生关闭员工与原责任人不一致或未知。')
    return {'available_actions_raw':mask,'resolve_action_mask':RESOLVE_ACTION,'employee_id':row['OwnerId'],
            'quality_status_context':{k:copy.deepcopy(detail[k]) for k in u.CURRENT_FIELDS.split(',')},'verified_input_echo':body,
            'dynamic_validation_verified':False,'event_closed_verified':False}

async def build_close(args,marker):
    u.text_value(args['close_description'],'关闭说明',4000,True);u.text_value(args.get('comments',''),'关闭备注',200)
    q.text(args['quality_resolution_code_name'],'质量解决代码')
    if type(args.get('allow_complex_context',False)) is not bool:raise ValueError('复杂上下文选项须为布尔。')
    if type(args.get('allow_native_ncr_closure',False)) is not bool:raise ValueError('原生NCR联动选项须为布尔。')
    if args.get('allow_native_ncr_closure') and not args.get('allow_complex_context'):raise ValueError('NCR联动必须同时明确allow_complex_context=true。')
    before=await source(args);schema_digest=await contract()
    code=await t.asset('quality_resolution',name=args['quality_resolution_code_name'])
    ref=dict(name=before['event']['EventName'],instanceId=before['event']['EventId'])
    options=await q.closure_options(dict(qualityObject=ref,organization=t.ref(before['organization'])))
    choices=[r for r in options if r['Name']=='Close']
    if len(choices)!=1:raise ValueError('未返回唯一原生Close处置候选，不猜测原码。')
    action=i.wire_int(choices[0]['Value'],'Close处置原码')
    if action<1:raise ValueError('Close处置原码无效。')
    body=body_for(args,before,code,action,marker)
    raw=await request('POST','/api/'+SERVICE,base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,
        params={'execute':'false','$select':SELECT,'$expand':EXPAND},response_formatter=raw_json)
    preview=verify(u.parse_reply(raw),body,before)
    if await source(args)!=before or await t.asset('quality_resolution',key=code['InstanceID'],name=code['Name'])!=code:
        raise ValueError('关闭准备期间源事件、关联或参考配置变化。')
    preview['contract_digest']=schema_digest;preview['selected_resolution_action']=choices[0]
    reads=set()
    for key in ('organization','owner','role','priority'):reads.update(resources(before[key]['entity'],before[key]))
    reads.update(resources(code['entity'],code))
    if before.get('native_ncr_closure'):
        child=before['native_ncr_closure']
        if child['ncr_resolution']['Name']!=code['Name']:raise ValueError('父/子事务解决代码名称不一致。')
        reads.update(resources('NCRResolutionCodes',child['ncr_resolution']))
        preview['native_ncr_closure']=child
        preview['complex_closure_note']='仅一次父CloseQualityObject，原生内部执行NCRCloseQuality；处置记录覆盖量不证明处置已执行，最终状态/父子历史须人工核实。'
    row=before['event']
    return dict(kind='event_close',action='close',service=SERVICE,name=row['EventName'],target_id=row['EventId'],
        scope_key='quality-event:id:'+row['EventId'],before=before,resolution=code,resolution_action=action,
        body=body,preview=preview,arguments=args,marker=marker,environment=p.environment(),
        resource_reads=sorted(reads),resource_writes=['runtime:quality-event:id:'+row['EventId']]
            +['runtime:container:id:'+r['ContainerId'] for r in before['linked_ncrs']]
            +['runtime:quality-event:id:'+r['Id'] for r in before.get('car_targets',[])])

def validate_transport(snap):
    if (snap.get('kind')!='event_close' or snap.get('action')!='close' or snap.get('service')!=SERVICE
        or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker',''))
        or snap.get('target_id')!=snap['before']['event']['EventId'] or snap.get('name')!=snap['before']['event']['EventName']):
        raise ValueError('事件关闭类型、服务或原对象不符。')
    if type(snap.get('resolution_action')) is not int or snap['resolution_action']<1:raise ValueError('处置原码不符。')
    expected=body_for(snap['arguments'],snap['before'],snap['resolution'],snap['resolution_action'],snap['marker'])
    if type(snap['body'].get('resolutionAction')) is not int or snap['body']!=expected:raise ValueError('关闭请求包含未允许字段或目标/处置/说明变化。')

async def inspect_snapshot(snap):
    current=await t.event_record(instance_id=snap['target_id']);history=None
    if current:history=json.loads(await q.get_quality_history('quality_object',current['EventName'],snap['target_id'],limit=20))
    child_evidence=None
    child=snap['before'].get('native_ncr_closure')
    if child:
        from tools import quality as reads
        from tools.quality_ncr_closure import container_state
        original=child['report']['NonconformanceReportId']
        report=await reads.ncr_snapshot('',original)
        child_evidence=dict(original_ncr_id=original,current_original_ncr=report,
            still_points_to_original_event=bool(report and report.get('EscalatedEventId')==snap['target_id']),
            current_original_container=await container_state(child['container']['ContainerId']),verified_child_success=False)
    return dict(original_event_id=snap['target_id'],current_event=current,bounded_quality_history=history,native_ncr_closure_evidence=child_evidence,
        verified_success=False,state_changed=False,retry_allowed=False,request_correlation_verified=False,
        note='当前Closed状态或HTTP200不是本次关闭证明，仅原ID证据，不追随同名替代对象、不重试或自动解锁。')

@mcp.tool
async def prepare_quality_event_closure(event_name:str,organization_name:str,quality_resolution_code_name:str,
        close_description:str,event_instance_id:str|None=None,comments:str='',allow_complex_context:bool=False,allow_native_ncr_closure:bool=False)->str:
    """准备基础Event原生CloseQualityObject关闭计划，不写MES。明确事件/组织/质量解决代码及非空关闭说明。
    限已提交Active已分流、当前账号为责任人且有角色/Resolve动作；无CAR要求、流程/检查表/审批/签名、自定义EventData、法规报告或关联NCR。
    核实当前契约及原生Close处置候选，execute=false完整回显后独立重读；不执行NCR联动、不改归属或绕过流程。不用于CAPA/草稿或复杂流程。
    显式allow_complex_context可核实CAR/CAPA关联、流程快照、关联NCR及Approved40审批；保持原策略，原生最终校验流程完成，不主动关闭关联NCR。仍拒绝事件数据/检查表/待审批或签名。
    显式allow_native_ncr_closure=true且allow_complex_context=true支持类别3的标准EventData8085制造NCR联动；唯一Open原NCR/原容器、同名父子解决代码、全部同容器批处置覆盖及子签名要求须核实。只发送父关闭请求，不补发NCRCloseQuality。
    execute_quality_transaction系统确认单次提交，unknown保留原事件及依赖占用；inspect只给原ID证据，最终证据人工review，不重试或自动解锁。
    """
    return await p.prepare('event_close',dict(event_name=event_name,organization_name=organization_name,
        quality_resolution_code_name=quality_resolution_code_name,close_description=close_description,event_instance_id=event_instance_id,comments=comments,allow_complex_context=allow_complex_context,allow_native_ncr_closure=allow_native_ncr_closure))
