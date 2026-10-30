"""Existing configured completion-approval template, without unsafe native preview."""
import asyncio
import copy
import json
import re
from typing import Literal

from tools import mcp, quality as q, quality_approvals as a, quality_event_triage as t, quality_event_update as u, quality_plans as p
from tools import quality_event_creation as creation
from tools import quality_approval_authorization as authorization
from tools.quality_definitions import resources
from tools.quality_spc_annotations import iid, schema_check
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SCHEMA={
 'ApprovalSheetTemplate':'ApprovalSheetTemplateId,ApprovalSheetTemplateName,Description,GeneralInstructions,ApprovalRequired,ApprovalDecisionListId,FromStage,ToStage,IsFrozen,ChangeCount',
 'ApprovalSheetEntry':a.SCHEMA['ApprovalSheetEntry']+',CompleteWithinQty,CompleteWithinUOM,EditOption,SpecialInstructions,PLMApprovalCode,PLMApprovalURL',
 'ApprovalSheetMap':'ApprovalSheetMapId,ParentId,ResolutionAction,ApprovalRequired,ApprovalSheetTemplateId,IsFrozen,ChangeCount',
 'ApprovalDecisionList':a.SCHEMA['ApprovalDecisionList'], 'ApprovalDecision':a.SCHEMA['ApprovalDecision']}


async def template(name,key=None,allow_role_substitution=False):
    authorization.role_opt_in({'allow_role_substitution':allow_role_substitution})
    name=q.text(name,'审批模板名称')
    where=f"ApprovalSheetTemplateId='{iid(key)}'" if key is not None else 'ApprovalSheetTemplateName='+q.literal(name)
    rows=await q.sql_rows('SELECT TOP 2 '+SCHEMA['ApprovalSheetTemplate']+' FROM ApprovalSheetTemplate WHERE '+where,2)
    a.complete(rows,SCHEMA['ApprovalSheetTemplate'])
    if len(rows)!=1:raise ValueError('审批模板不存在或名称不唯一。')
    row={k:copy.deepcopy(rows[0][k]) for k in SCHEMA['ApprovalSheetTemplate'].split(',')}
    row['ApprovalSheetTemplateId']=iid(row['ApprovalSheetTemplateId'])
    if row['ApprovalSheetTemplateName']!=name or key is not None and row['ApprovalSheetTemplateId']!=iid(key):raise ValueError('审批模板名称/ID不符。')
    row['ChangeCount']=a.integer(row['ChangeCount'],'模板版本')
    if a.i.wire_bool(row['IsFrozen'],'模板冻结'):raise ValueError('审批模板已冻结。')
    row['ApprovalRequired']=a.i.wire_bool(row['ApprovalRequired'],'模板审批要求')
    if not row['ApprovalRequired']:raise ValueError('C1仅支持要求审批的有效模板。')
    list_id=iid(row['ApprovalDecisionListId'])
    lists=await q.sql_rows('SELECT TOP 2 '+SCHEMA['ApprovalDecisionList']+f" FROM ApprovalDecisionList WHERE ApprovalDecisionListId='{list_id}'",2)
    a.complete(lists,SCHEMA['ApprovalDecisionList'])
    if len(lists)!=1 or iid(lists[0]['ApprovalDecisionListId'])!=list_id or a.i.wire_bool(lists[0]['IsFrozen'],'决策列表冻结'):raise ValueError('决策列表不唯一、不属于模板或已冻结。')
    definition={k:copy.deepcopy(lists[0][k]) for k in SCHEMA['ApprovalDecisionList'].split(',')}
    a.integer(definition['ChangeCount'],'决策列表版本');q.text(definition['ApprovalDecisionListName'])
    options=await q.sql_rows('SELECT TOP 51 '+SCHEMA['ApprovalDecision']+f" FROM ApprovalDecision WHERE ApprovalDecisionListId='{list_id}' ORDER BY ApprovalDecisionId",51)
    if len(options)>50:raise ValueError('决策列表超过50条。')
    a.complete(options,SCHEMA['ApprovalDecision']);a.unique(options,'ApprovalDecisionId')
    decisions=[]
    for raw in options:
        d={k:copy.deepcopy(raw[k]) for k in SCHEMA['ApprovalDecision'].split(',')}
        if iid(d['ApprovalDecisionListId'])!=list_id:raise ValueError('审批决策不属于模板列表。')
        d['ApprovalDecisionId']=iid(d['ApprovalDecisionId']);d['DecisionType']=a.integer(d['DecisionType'],'决策类型')
        d['IsFrozen']=a.i.wire_bool(d['IsFrozen'],'决策冻结');a.integer(d['ChangeCount'],'决策版本')
        d['IncludeComments']=a.i.wire_bool(d['IncludeComments'],'决策评论要求');q.text(d['ApprovalDecisionName'])
        decisions.append(d)
    if not {10,20}<={d['DecisionType'] for d in decisions if not d['IsFrozen']}:raise ValueError('模板须有有效的Approve10及Reject20决策。')
    entries=await q.sql_rows('SELECT TOP 101 '+SCHEMA['ApprovalSheetEntry']+f" FROM ApprovalSheetEntry WHERE ParentId='{row['ApprovalSheetTemplateId']}' ORDER BY SheetLevel,ApprovalSheetEntryId",101)
    if not 1<=len(entries)<=100:raise ValueError('模板须有1..100条完整审批条目。')
    a.complete(entries,SCHEMA['ApprovalSheetEntry']);a.unique(entries,'ApprovalSheetEntryId')
    cleaned=[];actors={}
    for raw in entries:
        e={k:copy.deepcopy(raw[k]) for k in SCHEMA['ApprovalSheetEntry'].split(',')}
        e['ApprovalSheetEntryId']=iid(e['ApprovalSheetEntryId'])
        if iid(e['ParentId'])!=row['ApprovalSheetTemplateId']:raise ValueError('模板审批条目归属不符。')
        for field in ('Status','SheetLevel','SubstituteOption','ChangeCount','DecisionType','CompleteWithinQty','EditOption'):e[field]=a.integer(e[field],field)
        e['IsFrozen']=a.i.wire_bool(e['IsFrozen'],'模板条目冻结');e['EntryRequired']=a.i.wire_bool(e['EntryRequired'],'条目必填')
        for field in ('ApproverId','ApproverRoleId','AssignedApproverId','DelegationTaskId','ApprovalDecisionId','ApprovedById','LastDecisionId'):e[field]=a.optional(e[field])
        authorization.entry_policy(e,{'allow_role_substitution':allow_role_substitution})
        if (e['IsFrozen'] or e['SheetLevel']<1 or e['Status']!=0 or e['DecisionType']!=0
            or any(e[k] is not None for k in ('DelegationTaskId','ApprovalDecisionId','ApprovedById','LastDecisionId','LastCompletedOnGMT'))
            or any(e[k] not in (None,'') for k in ('Comments','PLMApprovalCode','PLMApprovalURL'))):
            raise ValueError('仅空白有效模板；角色替代须原配置Role2并明选允许，委托、PLM及已有签批仍拒绝。')
        if type(e['CompleteWithinUOM']) is bool or str(e['CompleteWithinUOM']) not in ('0','0.0','1','1.0','24','24.0','168','168.0'):
            raise ValueError('模板期限单位未核实。')
        e['CompleteWithinUOM']=float(e['CompleteWithinUOM'])
        if e['CompleteWithinQty'] and e['CompleteWithinUOM']==0:raise ValueError('模板期限数量存在但单位未知。')
        for field,kind in [('ApproverId','owner'),('ApproverRoleId','role')]:
            key=iid(e[field]);token=(kind,key)
            if token not in actors:actors[token]=await t.asset(kind,key=key)
        if e['AssignedApproverId'] not in (None,e['ApproverId']):raise ValueError('模板初始指派人不同于审批人，不做身份替换。')
        for field,maximum in [('SpecialInstructions',255)]:
            if e[field] is not None:u.text_value(e[field],field,maximum)
        cleaned.append(e)
    for field in ('Description','GeneralInstructions'):
        if row[field] is not None:u.text_value(row[field],field,255)
    return dict(template=row,entries=cleaned,decision_list=definition,allowed_decisions=decisions,
                actors=sorted(actors.values(),key=lambda r:(r['entity'],r['InstanceID'])))


async def source(args):
    authorization.role_opt_in(args)
    if args['object_kind'] not in ('event','capa'):raise ValueError('对象类型仅event/capa。')
    await schema_check(SCHEMA)
    context=await a.read_context(args['object_kind'],args['name'],args['organization_name'],args.get('instance_id'))
    target=context['target']
    if target['IsFrozen'] or target['Status']!=1 or context['sheets'] or context['current_employee_entry_id'] is not None or context['current_context']['CompletionApprovalSheet'] is not None:
        raise ValueError('首次挂接只支持未冻结Active且没有直接审批表/待签入口的对象；不复用旧表。')
    raw=await u.event_record(args['name'],target['Id']) if args['object_kind']=='event' else await q.find_record('capa',args['name'],target['Id'])
    if not a.i.wire_bool(raw['IsSubmitted'],'对象提交状态') or raw['ProcessModelId']:raise ValueError('首次挂接须已提交且无嵌套流程。')
    if args['object_kind']=='event' and any(raw[k] for k in ('EventDataId','OrigProcessModelTemplateId','ChecklistId','ChecklistSaved')):
        raise ValueError('首次挂接不处理事件数据、检查表或原流程模板。')
    org=await t.asset('organization',key=target['OrganizationId'],name=args['organization_name'],signature_txn_type=7852)
    owner=await t.asset('owner',key=context['current_context']['Owner']['instance_id'])
    role=await t.asset('role',key=context['current_context']['Role']['instance_id'])
    configured=await template(args['template_name'],args.get('template_instance_id'),args.get('allow_role_substitution',False))
    options=await q.closure_options(dict(qualityObject=dict(name=target['Name'],instanceId=target['Id']),organization=t.ref(org)))
    close=[r for r in options if r['Name']=='Close']
    if len(close)!=1:raise ValueError('当前原生Close处置动作未唯一核实。')
    action=int(close[0]['Value'])
    maps=await q.sql_rows('SELECT TOP 101 '+SCHEMA['ApprovalSheetMap']+f" FROM ApprovalSheetMap WHERE ParentId='{target['OrganizationId']}' ORDER BY ApprovalSheetMapId",101)
    if len(maps)>100:raise ValueError('组织审批映射超过100条。')
    a.complete(maps,SCHEMA['ApprovalSheetMap']);a.unique(maps,'ApprovalSheetMapId')
    maps=[{k:copy.deepcopy(r[k]) for k in SCHEMA['ApprovalSheetMap'].split(',')} for r in maps]
    selected=[]
    for row in maps:
        if iid(row['ParentId'])!=target['OrganizationId']:raise ValueError('审批映射不属于原组织。')
        row['ResolutionAction']=a.integer(row['ResolutionAction'],'映射处置动作');a.integer(row['ChangeCount'],'映射版本')
        if row['ResolutionAction']==action:selected.append(row)
    if len(selected)!=1:raise ValueError('组织Close审批映射必须唯一，不自行选择默认模板。')
    row=selected[0]
    if a.i.wire_bool(row['IsFrozen'],'映射冻结') or not a.i.wire_bool(row['ApprovalRequired'],'映射要求') or iid(row['ApprovalSheetTemplateId'])!=configured['template']['ApprovalSheetTemplateId']:
        raise ValueError('组织完成审批映射不是所选有效必审模板，不覆盖组织策略。')
    resolution=await t.asset('quality_resolution',name=args['quality_resolution_code_name'])
    policy=await creation.notification_policy(org)
    if type(args['allow_notifications']) is not bool or policy['smtp_configured'] and policy['entries'] and not args['allow_notifications']:
        raise ValueError('挂接并路由可能发送通知，须明确allow_notifications=true。')
    return dict(context=context,raw_target=raw,organization=org,owner=owner,role=role,configured_template=configured,
                organization_maps=maps,close_action=close[0],quality_resolution=resolution,notification_policy=policy)


def body_for(args,before,marker):
    obj=before['context']['target'];config=before['configured_template'];row=config['template']
    actors={(r['entity'],r['InstanceID']):r for r in config['actors']}
    entries=[]
    for e in config['entries']:
        entry=dict(approvalEntry=None,approver=t.ref(actors['Employees',e['ApproverId']]),approverRole=t.ref(actors['Roles',e['ApproverRoleId']]),
            assignedApprover=None if e['AssignedApproverId'] is None else t.ref(actors['Employees',e['AssignedApproverId']]),
            completeWithinQty=e['CompleteWithinQty'],completeWithinUOM=e['CompleteWithinUOM'],delegationTask=None,editOption=e['EditOption'],
            entryRequired=e['EntryRequired'],plmApprovalCode=e['PLMApprovalCode'],plmApprovalURL=e['PLMApprovalURL'],
            sheetLevel=e['SheetLevel'],specialInstructions=e['SpecialInstructions'],substituteOption=e['SubstituteOption'])
        entries.append(entry)
    detail=dict(name='FromCompleted',executeAction=1,approvalDecisionList=dict(name=config['decision_list']['ApprovalDecisionListName'],instanceId=row['ApprovalDecisionListId']),
        approvalRequired=True,description=row['Description'],generalInstructions=row['GeneralInstructions'],approvalEntries=entries)
    return dict(qualityObject=dict(name=obj['Name'],instanceId=obj['Id']),organization=t.ref(before['organization']),
        approvalSheetTemplate=dict(name=row['ApprovalSheetTemplateName'],instanceId=row['ApprovalSheetTemplateId']),approvalSheetDetails=detail,
        resolutionAction=int(before['close_action']['Value']),qualityResolutionCode=t.ref(before['quality_resolution']),
        closeDescription=u.text_value(args['close_description'],'完成审批处置说明',255,True),comments=marker+' '+u.text_value(args['comments'],'挂接审批说明',200,True))


async def build(args,marker):
    u.text_value(args['close_description'],'完成审批处置说明',255,True);u.text_value(args['comments'],'挂接审批说明',200,True)
    before=await source(args)
    doc=json.loads(await request('GET','/swagger/AssignApprovalSheet/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json))
    try:
        schemas=doc['components']['schemas'];props=schemas['AssignApprovalSheet']['properties']
        for key,kind in [('qualityObject','NamedObjectRef'),('organization','NamedObjectRef'),('approvalSheetTemplate','NamedObjectRef'),('approvalSheetDetails','ApprovalSheetDetails'),('qualityResolutionCode','NamedObjectRef')]:
            if props[key]['$ref'].split('/')[-1]!=kind:raise ValueError('引用类型不符')
        if props['resolutionAction']['type']!='integer' or props['closeDescription']['type']!='string':raise ValueError('处置字段类型不符')
        detail=schemas['ApprovalSheetDetails']['properties'];entry=schemas['ApprovalEntryDetails']['properties']
        if detail['approvalEntries']['items']['$ref'].split('/')[-1]!='ApprovalEntryDetails' or detail['executeAction']['type']!='integer':raise ValueError('明细类型不符')
        for key,kind in [('approver','NamedObjectRef'),('approverRole','NamedObjectRef'),('approvalEntry','SubentityRef')]:
            if entry[key]['$ref'].split('/')[-1]!=kind:raise ValueError('条目引用类型不符')
        body=body_for(args,before,marker)
        if not set(body)<=props.keys() or not set(body['approvalSheetDetails'])<=detail.keys() or any(not set(e)<=entry.keys() for e in body['approvalSheetDetails']['approvalEntries']):raise ValueError('输入字段未完整核实')
    except (ValueError,KeyError,TypeError) as exc:raise ValueError('当前AssignApprovalSheet及明细契约未核实。') from exc
    if await source(args)!=before:raise ValueError('准备期间原对象、模板/条目/决策、组织策略或通知配置变化。')
    reads=set()
    for record in [before[k] for k in ('organization','owner','role','quality_resolution')]+before['configured_template']['actors']:reads.update(resources(record['entity'],record))
    return dict(kind='quality_approval_assignment',action='assign_and_route',service='AssignApprovalSheet',target_id=before['context']['target']['Id'],
        name=before['context']['target']['Name'],scope_key='quality-event:id:'+before['context']['target']['Id'],before=before,body=body,
        arguments=args,marker=marker,environment=p.environment(),resource_reads=sorted(reads),
        resource_writes=['runtime:quality-event:id:'+before['context']['target']['Id']],
        preview=dict(native_preview_requested=False,contract_digest=p.digest({'root':props,'detail':detail,'entry':entry}),
            dynamic_validation_verified=False,actor_permission_verified=False,extra_signature_requirements_verified=False,
            note='仅本地静态计划。AssignApprovalSheet预校验可能创建/清理审批表，故不调用execute=false。确认后原生单次挂接并立即路由FromCompleted完成审批，携带明确处置资料；不宣称仅挂接、不宣称已关闭。权限/动态签名最终由MES验证，失败或超时保持unknown，不重试。'))


def validate_transport(snap):
    if snap.get('kind')!='quality_approval_assignment' or snap.get('service')!='AssignApprovalSheet' or snap.get('action')!='assign_and_route':raise ValueError('挂接服务/计划类型不符。')
    if not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker','')):raise ValueError('挂接标记无效。')
    if snap.get('target_id')!=snap['before']['context']['target']['Id'] or snap['body']!=body_for(snap['arguments'],snap['before'],snap['marker']):raise ValueError('挂接原对象、模板或允许字段不符。')


async def inspect(snap):
    obj=snap['before']['context']['target'];current=await a.target(snap['arguments']['object_kind'],'',obj['Id'])
    sheets=await a.sheets_for(obj['Id'])
    return dict(original_object_id=obj['Id'],current_object=current,current_direct_sheets=sheets,
        verified_success=False,retry_allowed=False,note='挂表前原对象无直接表；当前出现的表仅为候选，不按FromCompleted同名表或当前状态自动归因本次操作，不解除unknown。')


@mcp.tool
async def get_quality_approval_template(name:str,instance_id:str|None=None,allow_role_substitution:bool=False)->str:
    """只读现有必审模板、完整审批条目及Approve10/Reject20决策，最多100条目/50决策，双重重读。安全投影审批人/角色，不读口令。
    默认仅原指定审批人模板；委托、PLM、冻结或已有签批仍拒绝。完整不等于当前对象/组织可采用；不挂接、不路由。
    可明确allow_role_substitution=true读取原配置Role2模板；不修改策略、不认证当前员工签署资格，委托/额外签名仍不支持。
    """
    try:
        async def read():
            await schema_check({k:v for k,v in SCHEMA.items() if k!='ApprovalSheetMap'})
            before=await template(name,instance_id,allow_role_substitution)
            if await template(name,instance_id,allow_role_substitution)!=before:raise ValueError('模板读取期间发生变化。')
            return dict(complete=True,ready_for_execution=False,**before)
        out=await asyncio.wait_for(read(),90)
        if len(q.encode(out))>78000:raise ValueError('审批模板超过输出上限，不返回部分结果。')
        return q.encode(out)
    except (ValueError,TypeError,KeyError,AttributeError,asyncio.TimeoutError) as exc:return q.encode(dict(complete=False,ready_for_execution=False,error=str(exc) or '模板读取超时。'))


@mcp.tool
async def prepare_quality_approval_assignment(name:str,organization_name:str,template_name:str,quality_resolution_code_name:str,
        close_description:str,comments:str,object_kind:Literal['event','capa']='event',instance_id:str|None=None,
        template_instance_id:str|None=None,allow_notifications:bool=False,allow_role_substitution:bool=False)->str:
    """准备首次挂接并立即路由完成审批（AssignApprovalSheet），不是单独挂表。只支持已提交Active、无直接审批表/复杂流程的Event/CAPA。
    明确已有必审模板、解决代码、完成处置说明及挂接评论；所选模板须等于组织唯一Close审批映射，不编造审批人/默认配置。仅命名审批人，无角色替代/委托/PLM/已知组织签名要求；配置通知时明确allow_notifications=true。
    只读源数据和当前Swagger，本地静态计划；禁止原生execute=false预览（其预校验可能清理审批）。动态权限/额外签名未核实须展示，MES最终校验。确认后execute_quality_transaction只发一次；unknown占用原对象，不重试或按同名表归因成功。
    明确allow_role_substitution=true才允许已有Role2模板条目，原策略完整保持；不赋予当前员工替代签署资格，后续决策须单独核实原生角色/组织授权。
    """
    return await p.prepare('quality_approval_assignment',dict(name=name,organization_name=organization_name,template_name=template_name,
        quality_resolution_code_name=quality_resolution_code_name,close_description=close_description,comments=comments,object_kind=object_kind,
        instance_id=instance_id,template_instance_id=template_instance_id,allow_notifications=allow_notifications,allow_role_substitution=allow_role_substitution))
