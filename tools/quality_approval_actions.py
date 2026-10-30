"""Native sheet-level route/cancel plans for unprocessed direct quality sheets."""
import copy
import json
import re
from typing import Literal
from tools import mcp,quality as q,quality_plans as p,quality_approvals as a,quality_event_triage as t,quality_event_update as u
from tools import quality_event_creation as creation
from tools import quality_approval_history as history
from tools.quality_definitions import resources
from tools.quality_event_lifecycle import contract
from tools.quality_spc_annotations import iid
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SERVICES={'route':('RouteApprovalSheet',7838),'cancel':('CancelApprovalSheet',7837),'resubmit':('RouteApprovalSheet',7838)}
SELECT='QualityObject,Organization,ApprovalSheet,Comments,AvailableActions,IgnoreProcessModel,ESigRequirement,AdditionalESigReqs,QualityObjectDetail'

async def source(args):
    kind=args['object_kind'];name=q.text(args['name']);sheet_id=iid(args['approval_sheet_id']);q.text(args['organization_name'])
    context=await a.read_context(kind,name,args['organization_name'],args.get('instance_id'))
    target=context['target'];action=args['action'];service,typ=SERVICES[action]
    if target['IsFrozen'] or target['Status'] not in (1,8):raise ValueError('审批操作只支持未冻结Active/InReview质量对象。')
    if kind=='event':
        raw=await u.event_record(name,target['Id'])
        if raw['EventDataId'] or raw['ChecklistId'] or raw['ChecklistSaved'] or raw['ProcessModelId'] or raw['OrigProcessModelTemplateId'] or not raw['IsSubmitted']:
            raise ValueError('受限审批不处理事件数据、检查表、嵌套流程或未提交事件。')
    else:
        raw=await q.find_record('capa',name,target['Id'])
        if raw['ProcessModelId'] or not raw['IsSubmitted']:raise ValueError('CAPA必须已提交且无流程上下文。')
    org=await t.asset('organization',name=args['organization_name'],key=target['OrganizationId'],signature_txn_type=typ)
    sheets=[s for s in context['sheets'] if s['ApprovalSheetId']==sheet_id]
    if len(sheets)!=1:raise ValueError('明确审批表不属于原质量对象。')
    sheet=sheets[0]
    if sheet['IsFrozen'] or sheet['Status'] not in ({'route':(0,),'cancel':(10,),'resubmit':(0,30)}[action]):raise ValueError('审批表状态不允许首次路由、取消或拒绝/取消后重提。')
    if action=='route' and any(sheet[k] not in (None,'') for k in ('LastApprovalCycle','LastApprovalCycleGMT')):raise ValueError('已有审批周期，不清除/复用旧周期。')
    previous=None
    if action=='resubmit':
        if args.get('previous_cycle_gmt') is None:raise ValueError('重提必须明确previous_cycle_gmt，不能默认使用当前周期。')
        previous=await history.previous_cycle_evidence(context,sheet,args['previous_cycle_gmt'])
    elif args.get('previous_cycle_gmt') is not None:raise ValueError('previous_cycle_gmt仅用于明确重提。')
    if not sheet['entries'] or not sheet['decision_list'] or sheet['decision_list']['IsFrozen'] or not sheet['allowed_decisions']:
        raise ValueError('审批表须有完整条目及有效决策列表。')
    for entry in sheet['entries']:
        if entry['SubstituteOption']!=1 or entry['DelegationTaskId'] is not None:raise ValueError('当前表级操作不处理角色替代或委托审批。')
        if action=='resubmit':
            if entry['IsFrozen'] or entry['SubstituteOption']!=1 or entry['SheetLevel']<1 or not entry['ApproverId'] or not entry['ApproverRoleId']:
                raise ValueError('重提仅支持有效层级、未冻结的原指定审批人，不支持角色替代。')
            continue
        if entry['IsFrozen'] or entry['Status'] not in ((0,) if action=='route' else (0,10)):
            raise ValueError('条目已冻结或已处理，不撤销已有签批结论。')
        if (entry['DecisionType'] not in (0,'0') or entry['Comments'] not in (None,'') or any(entry[k] is not None for k in ('ApprovalDecisionId','ApprovedById','LastDecisionId','LastCompletedOnGMT'))):
            raise ValueError('已有决策/签批历史，不能清空或撤销。')
        if not entry['ApproverId'] or not entry['ApproverRoleId']:raise ValueError('审批人和角色必须明确；原生仍验证成员资格。')
    owner=await t.asset('owner',key=context['current_context']['Owner']['instance_id'])
    role=await t.asset('role',key=context['current_context']['Role']['instance_id'])
    policy=await creation.notification_policy(org)
    if type(args['allow_notifications']) is not bool or policy['smtp_configured'] and policy['entries'] and not args['allow_notifications']:
        raise ValueError('组织可能发送审批通知，须明确allow_notifications=true，不清空配置。')
    return dict(context=context,raw_target=raw,organization=org,sheet=sheet,owner=owner,role=role,notification_policy=policy,previous_cycle=previous)

def body_for(args,before,marker):
    target=before['context']['target'];sheet=before['sheet']
    ref=dict(name=target['Name'],instanceId=target['Id'])
    return dict(qualityObject=ref,organization=t.ref(before['organization']),
        approvalSheet=dict(name=sheet['ApprovalSheetName'],instanceId=sheet['ApprovalSheetId'],parent=ref),
        comments=marker+' '+u.text_value(args['comments'],'审批操作说明',200,True))

async def build(args,marker):
    if args['action'] not in SERVICES or args['object_kind'] not in ('event','capa'):raise ValueError('审批操作或对象类型无效。')
    u.text_value(args['comments'],'审批操作说明',200,True)
    before=await source(args);service,typ=SERVICES[args['action']]
    # Check the actual current input model, not inferred references.
    doc=json.loads(await request('GET','/swagger/'+service+'/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json))
    try:
        props=doc['components']['schemas'][service]['properties']
        if props['approvalSheet']['$ref'].split('/')[-1]!='NamedSubentityRef':raise ValueError('审批表引用类型不符')
        for key in ('qualityObject','organization'):
            if props[key]['$ref'].split('/')[-1]!='NamedObjectRef':raise ValueError('父对象引用类型不符')
    except (ValueError,KeyError,TypeError) as exc:raise ValueError('当前审批表服务契约未核实。') from exc
    body=body_for(args,before,marker)
    data=u.parse_reply(await request('POST','/api/'+service,base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,
        params={'execute':'false','$select':SELECT,'$expand':'ApprovalSheet,QualityObjectDetail'},response_formatter=raw_json))
    if not p.wire_matches(body,data):raise ValueError('审批表、父对象、组织或说明未完整回显。')
    mask=a.integer(data.get('AvailableActions'),'原生动作')
    if args['action']=='cancel' and mask&256!=256:raise ValueError('缺少CancelApproval动作256。')
    if a.i.wire_bool(data.get('IgnoreProcessModel'),'流程绕过标志') is not False:raise ValueError('不允许绕过流程。')
    if 'ESigRequirement' not in data or data['ESigRequirement'] is not None or 'AdditionalESigReqs' not in data or data['AdditionalESigReqs'] not in (None,[]):
        raise ValueError('签名要求非空或未知，当前入口不代签。')
    detail=data.get('QualityObjectDetail');target=before['context']['target']
    if not isinstance(detail,dict):raise ValueError('原生账号/状态上下文缺失。')
    for field in ('Status','Category'):
        if a.integer(detail.get(field),field)!=target[field]:raise ValueError('原生对象状态/类别不符。')
    for field in ('UserIsOwner','UserHasRole'):
        if a.i.wire_bool(detail.get(field),field) is not True:raise ValueError('当前账号不是原责任人或不具原角色。')
    for field,key in [('Organization','organization'),('Owner','owner'),('Role','role')]:
        if q.identifier(detail.get(field))!=before[key]['InstanceID']:raise ValueError('原生组织/归属不一致。')
    if detail.get('ProcessModel') is not None or 'ProcessModel' not in detail:raise ValueError('原生流程上下文不为空或未知。')
    if await source(args)!=before:raise ValueError('准备期间审批周期/条目、原对象、权限或通知配置变化。')
    reads=set()
    for key in ('organization','owner','role'):reads.update(resources(before[key]['entity'],before[key]))
    return dict(kind='quality_approval',action=args['action'],service=service,name=target['Name'],target_id=target['Id'],
        scope_key='quality-event:id:'+target['Id'],before=before,body=body,arguments=args,marker=marker,environment=p.environment(),
        resource_reads=sorted(reads),resource_writes=['runtime:quality-event:id:'+target['Id'],'runtime:approval-sheet:id:'+before['sheet']['ApprovalSheetId']],
        preview=dict(verified_input_echo=body,contract_digest=p.digest(props),available_actions_raw=mask,
            native_actor_context={k:copy.deepcopy(detail[k]) for k in ('Status','Category','UserIsOwner','UserHasRole','ProcessModel')}
                |{k:{'InstanceId':q.identifier(detail[k])} for k in ('Organization','Owner','Role')},
            dynamic_validation_verified=False,approval_action_verified=False,
            expected_native_effect={'route':'路由新Pending表，原生清空空白决策并通知/更新周期',
                'cancel':'取消未签批Routed表，原生表回Pending并写RoutingCanceled历史、质量对象设Active；不假设表变Cancelled20',
                'resubmit':'旧周期持久历史已核对；原生Route清理当前决策/评论/日期并开启新周期，保留历史和LastDecision指针；可能发送通知，不修改审批人/事件资料'}[args['action']]))

def validate_transport(snap):
    if snap.get('kind')!='quality_approval' or snap.get('action') not in SERVICES or snap.get('service')!=SERVICES[snap['action']][0]:raise ValueError('审批服务/动作不符。')
    if not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker','')):raise ValueError('审批标记无效。')
    target=snap['before']['context']['target']
    if snap.get('target_id')!=target['Id'] or snap.get('name')!=target['Name'] or snap['body']!=body_for(snap['arguments'],snap['before'],snap['marker']):raise ValueError('审批原对象、原表或允许字段不符。')

async def inspect(snap):
    target=snap['before']['context']['target'];current=await a.target(snap['arguments']['object_kind'],'',target['Id'])
    sheets=await a.sheets_for(target['Id'])
    original=next((s for s in sheets if s['ApprovalSheetId']==snap['before']['sheet']['ApprovalSheetId']),None)
    return dict(original_object_id=target['Id'],current_object=current,original_sheet_id=snap['before']['sheet']['ApprovalSheetId'],current_original_sheet=original,
        verified_success=False,state_changed=False,retry_allowed=False,note='仅原对象/原表证据；当前Pending/Routed或历史一致不证明本次操作，不跟随同名表或自动解锁。')

@mcp.tool
async def prepare_quality_approval_action(action:Literal['route','cancel','resubmit'],name:str,organization_name:str,approval_sheet_id:str,
        comments:str,object_kind:Literal['event','capa']='event',instance_id:str|None=None,allow_notifications:bool=False,previous_cycle_gmt:str|None=None)->str:
    """准备直接质量审批表级路由/取消，固定原对象、审批表ID和完整周期/条目。仅未处理Pending表首次路由或未签批Routed表取消；不清空历史决策、不签批或代签。
    明确非空说明，当前原责任人/角色须核实，取消须CancelApproval256。无嵌套流程/事件数据/检查表或电子签名要求；可能通知时明确allow_notifications=true。
    resubmit仅拒绝或原生取消后的表，必须明确previous_cycle_gmt；逐条核对同周期持久路由及决策历史，再原生开启新周期，不删除历史、不改审批人/事件资料。签名及角色替代仍拒绝。
    原生CancelApprovalSheet将表回Pending并写RoutingCanceled历史，不能当作Cancelled20；普通route仍拒绝旧周期/决策。
    准备用专用表级服务execute=false回显，不调用通用RouteApproval。execute_quality_transaction系统确认单次提交，unknown占用原对象/表，inspect只读证据，不重试。
    """
    return await p.prepare('quality_approval',dict(action=action,name=name,organization_name=organization_name,approval_sheet_id=approval_sheet_id,
        comments=comments,object_kind=object_kind,instance_id=instance_id,allow_notifications=allow_notifications,previous_cycle_gmt=previous_cycle_gmt))
