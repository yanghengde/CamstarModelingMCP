"""Authenticated named-approver decisions; no password/signature override channel."""
import copy
import json
import re
from typing import Literal
from tools import mcp,quality as q,quality_plans as p,quality_approvals as a,quality_event_triage as t,quality_event_update as u
from tools import quality_event_creation as creation
from tools import quality_approval_history as history
from tools import quality_approval_authorization as authorization
from tools.quality_spc_annotations import iid
from tools.quality_definitions import resources
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

TYPES={'approve':10,'reject':20}
SELECT='QualityObject,Organization,ApprovalSheet,ApprovalSheetEntry,ApprovalDecision,Comments,Employee,AvailableActions,IgnoreProcessModel,ESigRequirement,AdditionalESigReqs,QualityObjectDetail'

async def source(args):
    authorization.role_opt_in(args)
    if args['action'] not in TYPES or args['object_kind'] not in ('event','capa'):raise ValueError('不支持的审批决策或对象类型。')
    ids={k:iid(args[k]) for k in ('approval_sheet_id','approval_entry_id','approval_decision_id')}
    context=await a.read_context(args['object_kind'],args['name'],args['organization_name'],args.get('instance_id'))
    target=context['target']
    if target['IsFrozen'] or target['Status'] not in (1,8):raise ValueError('仅支持未冻结Active/InReview对象签批。')
    if args['object_kind']=='event':
        raw=await u.event_record(args['name'],target['Id'])
        if raw['EventDataId'] or raw['ProcessModelId'] or raw['OrigProcessModelTemplateId'] or raw['ChecklistId'] or raw['ChecklistSaved'] or not raw['IsSubmitted']:
            raise ValueError('当前签批不处理未提交事件、事件数据、检查表或流程联动。')
    else:
        raw=await q.find_record('capa',args['name'],target['Id'])
        if raw['ProcessModelId'] or not a.i.wire_bool(raw['IsSubmitted'],'CAPA提交状态'):raise ValueError('CAPA须已提交且无流程。')
    org=await t.asset('organization',name=args['organization_name'],key=target['OrganizationId'],signature_txn_type=7839)
    rows=[s for s in context['sheets'] if s['ApprovalSheetId']==ids['approval_sheet_id']]
    if len(rows)!=1:raise ValueError('原审批表不属于原对象。')
    sheet=rows[0]
    if sheet['IsFrozen'] or sheet['Status']!=10 or not isinstance(sheet['LastApprovalCycleGMT'],str) or not sheet['LastApprovalCycleGMT'].strip():raise ValueError('审批表须已路由且当前周期明确。')
    rows=[e for e in sheet['entries'] if e['ApprovalSheetEntryId']==ids['approval_entry_id']]
    if len(rows)!=1:raise ValueError('明确条目不属于原表。')
    entry=rows[0]
    authorization.entry_policy(entry,args)
    if (entry['IsFrozen'] or entry['Status']!=10 or not entry['ApproverId'] or not entry['ApproverRoleId']
        or a.integer(entry['DecisionType'],'当前条目决策类型')!=0 or any(entry[k] is not None for k in ('ApprovedById','ApprovalDecisionId','LastCompletedOnGMT'))):
        raise ValueError('仅未冻结、未签批Routed条目；不覆盖已有决策。')
    prior=None
    if entry['LastDecisionId'] is not None:prior=await history.prior_decision_evidence(context,sheet,entry)
    if context['current_employee_entry_id']!=entry['ApprovalSheetEntryId']:raise ValueError('原生当前账号审批入口不是指定条目，不推测其他审批人身份。')
    if entry['SheetLevel']<1 or any(e['SheetLevel']<entry['SheetLevel'] and e['Status'] not in (30,40) for e in sheet['entries']):
        raise ValueError('前序层级尚未完成或层级无效，不越级签批。')
    if not sheet['decision_list'] or sheet['decision_list']['IsFrozen']:raise ValueError('决策列表未知或已冻结。')
    rows=[d for d in sheet['allowed_decisions'] if d['ApprovalDecisionId']==ids['approval_decision_id']]
    if len(rows)!=1 or rows[0]['IsFrozen'] or rows[0]['DecisionType']!=TYPES[args['action']]:raise ValueError('决策不属于原列表、已冻结或原码与批准/拒绝意图不符。')
    decision=rows[0]
    approver=await t.asset('owner',key=entry['ApproverId']);role=await t.asset('role',key=entry['ApproverRoleId'])
    policy=await creation.notification_policy(org)
    if type(args['allow_notifications']) is not bool or policy['smtp_configured'] and policy['entries'] and not args['allow_notifications']:
        raise ValueError('签批可能通知下一层级或批准/拒绝结果，须明确allow_notifications=true。')
    return dict(context=context,raw_target=raw,organization=org,sheet=sheet,entry=entry,decision=decision,approver=approver,role=role,notification_policy=policy,prior_decision=prior,
                actor_policy=authorization.entry_policy(entry,args))

def body_for(args,before,marker):
    obj=before['context']['target'];sheet=before['sheet'];decision=before['decision']
    return dict(qualityObject=dict(name=obj['Name'],instanceId=obj['Id']),organization=t.ref(before['organization']),
        approvalSheet=dict(name=sheet['ApprovalSheetName'],instanceId=sheet['ApprovalSheetId'],parent={'instanceId':obj['Id']}),
        approvalSheetEntry=dict(instanceId=before['entry']['ApprovalSheetEntryId'],parent={'instanceId':sheet['ApprovalSheetId']}),
        approvalDecision=dict(name=decision['ApprovalDecisionName'],instanceId=decision['ApprovalDecisionId'],parent={'instanceId':sheet['ApprovalDecisionListId']}),
        comments=marker+' '+u.text_value(args['comments'],'签批说明',200,True))

async def build(args,marker,execution_binding=None):
    u.text_value(args['comments'],'签批说明',200,True)
    before=await source(args)
    doc=json.loads(await request('GET','/swagger/SignApproval/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json))
    try:
        props=doc['components']['schemas']['SignApproval']['properties']
        for key,kind in [('qualityObject','NamedObjectRef'),('organization','NamedObjectRef'),('approvalSheet','NamedSubentityRef'),('approvalSheetEntry','SubentityRef'),('approvalDecision','NamedSubentityRef')]:
            if props[key]['$ref'].split('/')[-1]!=kind:raise ValueError('审批引用类型不符')
    except (ValueError,KeyError,TypeError) as exc:raise ValueError('当前SignApproval输入契约未核实。') from exc
    body=body_for(args,before,marker)
    bound=authorization.capture_headers() if before['actor_policy']=='role' else None
    options={} if bound is None else {'bound_headers':bound,'require_stable_session':True}
    data=u.parse_reply(await request('POST','/api/SignApproval',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,
        params={'execute':'false','$select':SELECT,'$expand':'ApprovalSheet,ApprovalSheetEntry,ApprovalDecision,QualityObjectDetail'},response_formatter=raw_json,**options))
    if not p.wire_matches(body,data):raise ValueError('原对象/原表/条目/决策/评论未完整回显。')
    actor_id=iid(q.identifier(data.get('Employee')))
    if before['actor_policy']=='named' and actor_id!=before['entry']['ApproverId']:raise ValueError('原生执行员工与原指定审批人不同；不接受身份覆盖或代签。')
    mask=a.integer(data.get('AvailableActions'),'原生动作')
    if mask&512!=512:raise ValueError('当前账号没有原生Approve动作512。')
    if a.i.wire_bool(data.get('IgnoreProcessModel'),'流程绕过') is not False:raise ValueError('不能绕过流程。')
    if 'ESigRequirement' not in data or data['ESigRequirement'] is not None or 'AdditionalESigReqs' not in data or data['AdditionalESigReqs'] not in (None,[]):
        raise ValueError('当前服务要求额外电子签名或要求未知；不以普通确认代签。')
    detail=data.get('QualityObjectDetail');target=before['context']['target']
    if not isinstance(detail,dict):raise ValueError('原生父对象上下文缺失。')
    for field in ('Status','Category'):
        if a.integer(detail.get(field),field)!=target[field]:raise ValueError('原生对象状态/类别不符。')
    if q.identifier(detail.get('Organization'))!=target['OrganizationId'] or q.identifier(detail.get('EntryForApproval'))!=before['entry']['ApprovalSheetEntryId']:
        raise ValueError('原生组织或当前审批条目不符。')
    if 'ProcessModel' not in detail or detail['ProcessModel'] is not None:raise ValueError('流程上下文未知或不为空。')
    proof=await authorization.membership(actor_id,before['organization'],before['role']) if before['actor_policy']=='role' else None
    if await source(args)!=before:raise ValueError('准备期间原审批周期、条目、决策、账号或通知配置发生变化。')
    if proof is not None and await authorization.membership(actor_id,before['organization'],before['role'])!=proof:
        raise ValueError('准备期间当前员工的角色/组织授权变化。')
    if execution_binding is not None and proof is not None:
        execution_binding.update(headers=bound,actor_id=actor_id)
    reads=set()
    for key in ('organization','approver','role'):reads.update(resources(before[key]['entity'],before[key]))
    if proof is not None:reads.update(authorization.authorization_resources(proof))
    return dict(kind='quality_approval_decision',action=args['action'],service='SignApproval',name=target['Name'],target_id=target['Id'],
        scope_key='quality-event:id:'+target['Id'],before=before,body=body,arguments=args,marker=marker,environment=p.environment(),resource_reads=sorted(reads),actor_authorization=proof,
        resource_writes=['runtime:quality-event:id:'+target['Id'],'runtime:approval-sheet:id:'+before['sheet']['ApprovalSheetId']],
        preview=dict(verified_input_echo=body,contract_digest=p.digest(props),available_actions_raw=mask,employee_id=actor_id,original_approver_id=before['approver']['InstanceID'],
            signature_actor_policy=before['actor_policy'],
            decision_type=before['decision']['DecisionType'],dynamic_validation_verified=False,decision_executed_verified=False,
            role_organization_membership_verified=proof is not None,extra_electronic_signature_performed=False,
            note='当前认证员工的原生决策请求，无身份/签名覆盖；MES最终校验角色和组织。可能推进下一层级并通知；Rejected决策不保证审批表立即拒绝，也不是退回操作。'))

def validate_transport(snap):
    if snap.get('kind')!='quality_approval_decision' or snap.get('service')!='SignApproval' or snap.get('action') not in TYPES or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker','')):
        raise ValueError('签批服务/动作/标记无效。')
    target=snap['before']['context']['target']
    actor_policy=authorization.entry_policy(snap['before']['entry'],snap['arguments'])
    if actor_policy!=snap['before']['actor_policy'] or snap['preview']['signature_actor_policy']!=actor_policy:raise ValueError('审批身份策略与原配置/明确选择不符。')
    if actor_policy=='role':
        proof=snap.get('actor_authorization')
        if (not isinstance(proof,dict) or proof.get('role_organization_membership_verified') is not True
            or iid(proof['actor']['InstanceID'])!=snap['preview']['employee_id']
            or proof['role_id']!=snap['before']['entry']['ApproverRoleId'] or proof['organization_id']!=target['OrganizationId']):
            raise ValueError('实际员工及原生角色/组织授权快照不符。')
    if snap['before']['decision']['DecisionType']!=TYPES[snap['action']] or snap.get('target_id')!=target['Id'] or snap.get('name')!=target['Name'] or snap['body']!=body_for(snap['arguments'],snap['before'],snap['marker']):
        raise ValueError('签批原对象、决策或允许字段不符。')

async def inspect(snap):
    target=snap['before']['context']['target'];current=await a.target(snap['arguments']['object_kind'],'',target['Id']);sheets=await a.sheets_for(target['Id'])
    sheet=next((s for s in sheets if s['ApprovalSheetId']==snap['before']['sheet']['ApprovalSheetId']),None)
    entry=next((e for e in sheet['entries'] if e['ApprovalSheetEntryId']==snap['before']['entry']['ApprovalSheetEntryId']),None) if sheet else None
    return dict(original_object_id=target['Id'],current_object=current,original_sheet_id=snap['before']['sheet']['ApprovalSheetId'],current_original_sheet=sheet,
        original_entry_id=snap['before']['entry']['ApprovalSheetEntryId'],current_original_entry=entry,verified_success=False,state_changed=False,retry_allowed=False,
        planned_actor_id=snap['preview']['employee_id'],original_approver_id=snap['before']['entry']['ApproverId'],signature_actor_policy=snap['preview'].get('signature_actor_policy','named'),
        note='原ID/原表/原条目证据；当前决策、Approved/Rejected状态或LastDecision不证明本次签批，不重试或自动解锁。')

@mcp.tool
async def prepare_quality_approval_decision(action:Literal['approve','reject'],name:str,organization_name:str,approval_sheet_id:str,
        approval_entry_id:str,approval_decision_id:str,comments:str,object_kind:Literal['event','capa']='event',instance_id:str|None=None,allow_notifications:bool=False,
        allow_role_substitution:bool=False)->str:
    """准备原指定审批人对当前Routed条目的SignApproval批准/拒绝请求；明确原表/条目/允许决策ID和非空说明，固定周期、层级和决策原码10/20。
    默认仅认证员工=原审批人、SubstituteOption1；原生Approve512且当前EntryForApproval匹配，无额外电子签名要求，不接收口令/员工覆盖。
    原配置SubstituteOption2且明确allow_role_substitution=true时，可由原生会话当前员工依其原角色/组织授权审批：读取原生csiAuthGetAllowedUsers及完整员工角色快照，冻结身份核验到提交的会话，仅内存持有，不保存认证信息。不接受指定他人身份、委托或额外签名；历史须保留原审批人与实际ApprovedBy。
    可能通知或推进后续层级时按组织策略明确allow_notifications=true；拒绝不等于退回，也不保证表立即Rejected。MES最终验证人员/角色/组织，预览不是批准。
    execute_quality_transaction系统确认单次提交，unknown保留原对象/表占用，inspect只给原条目证据，不重试。
    """
    return await p.prepare('quality_approval_decision',dict(action=action,name=name,organization_name=organization_name,approval_sheet_id=approval_sheet_id,
        approval_entry_id=approval_entry_id,approval_decision_id=approval_decision_id,comments=comments,object_kind=object_kind,instance_id=instance_id,
        allow_notifications=allow_notifications,allow_role_substitution=allow_role_substitution))
