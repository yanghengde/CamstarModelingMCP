"""One direct, current-cycle DelegateTask; never create timed delegation settings."""
import copy
import json
import re
from typing import Literal
from tools import mcp,quality as q,quality_plans as p,quality_approvals as a,quality_event_triage as t,quality_event_update as u
from tools import quality_approval_authorization as auth,quality_approval_history as history
from tools.quality_spc_annotations import iid,schema_check
from tools.quality_definitions import resources
from tools.process_context import ReadContext
from tools.modeling_details import raw_json,_has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

HISTORY_COLUMNS=('DelegateTaskHistoryId,HistoryId,HistoryMainlineId,TxnId,DelegationTargetId,SourceEmployeeId,TargetEmployeeId,'
    'DelegationReasonCodeId,Comments,ChangeCount')
SCHEMA={'DelegateTaskHistory':HISTORY_COLUMNS,
    'HistoryMainline':'HistoryMainlineId,HistoryId,TxnId,TxnType,TxnDateGMT,EmployeeId,ReversalStatus',
    'DelegationReasonCode':'DelegationReasonCodeId,ChangeCount'}


def opted(args):
    value=args.get('allow_delegated_approval',False)
    if type(value) is not bool:raise ValueError('allow_delegated_approval须为明确布尔值。')
    evidence=args.get('delegation_history_id')
    if value:
        iid(evidence,'明确原生转派历史ID')
    elif evidence is not None:raise ValueError('转派历史须配合明确allow_delegated_approval=true。')
    return value


async def rows_for(target_id,entry_id,cycle):
    target_id=iid(target_id);entry_id=iid(entry_id);cycle=history.cycle_key(cycle)
    await schema_check({k:v for k,v in SCHEMA.items() if k!='DelegationReasonCode'})
    fields=','.join('d.'+f for f in HISTORY_COLUMNS.split(','))
    sql=('SELECT TOP 51 '+fields+',h.TxnType,h.TxnDateGMT,h.EmployeeId,h.ReversalStatus '
        'FROM DelegateTaskHistory d JOIN HistoryMainline h ON h.HistoryMainlineId=d.HistoryMainlineId AND h.TxnId=d.TxnId AND h.HistoryId=d.HistoryId '
        f"WHERE d.DelegationTargetId='{entry_id}' AND d.HistoryId='{target_id}' AND h.TxnDateGMT>=CONVERT(datetime2(7),"+q.literal(cycle[:-1])+',126) '
        'ORDER BY h.TxnDateGMT,d.DelegateTaskHistoryId')
    rows=await q.sql_rows(sql,51)
    if len(rows)>50:raise ValueError('本周期转派历史超过50条，不使用部分证据。')
    columns=HISTORY_COLUMNS+',TxnType,TxnDateGMT,EmployeeId,ReversalStatus'
    a.complete(rows,columns);a.unique(rows,'DelegateTaskHistoryId');result=[]
    for raw in rows:
        row={k:copy.deepcopy(raw[k]) for k in columns.split(',')}
        for f in ('DelegateTaskHistoryId','HistoryId','HistoryMainlineId','TxnId','DelegationTargetId','SourceEmployeeId','TargetEmployeeId','DelegationReasonCodeId','EmployeeId'):row[f]=iid(row[f])
        row['TxnDateGMT']=history.cycle_key(row['TxnDateGMT'])
        for f in ('ChangeCount','TxnType','ReversalStatus'):row[f]=a.integer(row[f],f)
        if (row['HistoryId']!=target_id or row['DelegationTargetId']!=entry_id or row['TxnDateGMT']<cycle or row['TxnType']!=8682):
            raise ValueError('转派历史不属于原对象、原条目、当前周期或DelegateTask8682。')
        result.append(row)
    return result


async def delegated_proof(before,args,actor_id):
    if not opted(args):raise ValueError('受委托审批须明确允许及原生转派历史ID。')
    entry=before['entry'];sheet=before['sheet'];target=before['context']['target']
    if entry['DelegationTaskId'] is not None or entry['SubstituteOption']!=1 or actor_id!=entry['ApproverId']:
        raise ValueError('仅原生一次转派后的当前指定员工；不处理日期任务、角色替代或身份覆盖。')
    rows=await rows_for(target['Id'],entry['ApprovalSheetEntryId'],sheet['LastApprovalCycleGMT'])
    if len(rows)!=1 or rows[0]['DelegateTaskHistoryId']!=iid(args['delegation_history_id']):raise ValueError('须唯一当前周期原生转派历史，不接受连续转委托或猜测历史。')
    row=rows[0]
    if (row['ReversalStatus']!=0 or row['EmployeeId']!=row['SourceEmployeeId'] or row['TargetEmployeeId']!=actor_id
        or row['SourceEmployeeId']==actor_id or entry['AssignedApproverId'] not in (None,row['SourceEmployeeId'])):
        raise ValueError('原生转派已撤销、实际发起人/目标不符或原分配人链不一致。')
    membership=await auth.membership(actor_id,before['organization'],before['role'])
    original=await t.asset('owner',key=row['SourceEmployeeId'])
    return dict(membership,delegation_verified=True,delegation_mode='single_direct_native_transfer',delegation_history=row,
        originally_assigned_approver=original,current_approver_id=actor_id,approval_cycle_gmt=history.cycle_key(sheet['LastApprovalCycleGMT']))


async def reason(name,key=None):
    await schema_check({'DelegationReasonCode':SCHEMA['DelegationReasonCode']})
    ctx=ReadContext(3);select='InstanceID,Name,IsFrozen'
    if key is None:
        listing=await ctx.get('DelegationReasonCodes',params={'$select':select,'$filter':"Name eq '"+q.text(name).replace("'","''")+"'",'$top':2})
        rows=listing.get('value')
        if not isinstance(rows,list) or len(rows)!=1 or _has_next_link(listing):raise ValueError('委托原因未唯一匹配。')
        key=iid(q.identifier(rows[0]))
    async def version():
        rows=await q.sql_rows(f"SELECT DelegationReasonCodeId,ChangeCount FROM DelegationReasonCode WHERE DelegationReasonCodeId='{iid(key)}'",1)
        if len(rows)!=1 or rows[0].get('DelegationReasonCodeId')!=key:raise ValueError('委托原因版本不可核实。')
        return a.integer(rows[0].get('ChangeCount'),'委托原因版本')
    first=await version();root=await ctx.get('DelegationReasonCodes',key,{'$select':select})
    if (iid(q.identifier(root))!=key or root.get('Name')!=name or _has_next_link(root) or a.i.wire_bool(root.get('IsFrozen'),'委托原因冻结') or await version()!=first):
        raise ValueError('委托原因身份不符、冻结或读取期间变化。')
    return dict(entity='DelegationReasonCodes',Name=q.text(root['Name']),InstanceID=key,ChangeCount=first,IsFrozen=False)


async def source(args):
    if args['object_kind'] not in ('event','capa') or args['allow_notifications'] is not True:
        raise ValueError('仅直接Event/CAPA一次转派，须明确allow_notifications=true确认可能通知原/新审批人。')
    context=await a.read_context(args['object_kind'],args['name'],args['organization_name'],args.get('instance_id'))
    obj=context['target']
    if obj['IsFrozen'] or obj['Status'] not in (1,8):raise ValueError('仅未冻结Active/InReview对象。')
    raw=await u.event_record(args['name'],obj['Id']) if args['object_kind']=='event' else await q.find_record('capa',args['name'],obj['Id'])
    if not a.i.wire_bool(raw.get('IsSubmitted'),'已提交') or raw.get('ProcessModelId') or args['object_kind']=='event' and any(raw.get(f) for f in ('EventDataId','OrigProcessModelTemplateId','ChecklistId','ChecklistSaved')):
        raise ValueError('仅已提交无流程/事件数据/检查表的基础质量对象。')
    # Conservative: no organization quality-signature requirements on delegation.
    org=await t.asset('organization',key=obj['OrganizationId'],name=args['organization_name'],signature_txn_type=8682)
    sheet=next((s for s in context['sheets'] if s['ApprovalSheetId']==iid(args['approval_sheet_id'])),None)
    if not sheet or sheet['IsFrozen'] or sheet['Status']!=10 or history.cycle_key(sheet['LastApprovalCycleGMT'])!=history.cycle_key(args['approval_cycle_gmt']):
        raise ValueError('原审批表须Routed且明确周期匹配。')
    entry=next((e for e in sheet['entries'] if e['ApprovalSheetEntryId']==iid(args['approval_entry_id'])),None)
    if (not entry or entry['IsFrozen'] or entry['Status']!=10 or entry['SubstituteOption']!=1 or entry['DelegationTaskId']
        or not entry['ApproverId'] or not entry['ApproverRoleId'] or entry['AssignedApproverId'] not in (None,entry['ApproverId'])
        or entry['DecisionType']!=0 or any(entry[f] is not None for f in ('ApprovalDecisionId','ApprovedById','LastCompletedOnGMT'))):
        raise ValueError('仅当前未签批指定人Named1条目，不处理日期委托、角色替代、已有转派链或已签批结论。')
    if context['current_employee_entry_id']!=entry['ApprovalSheetEntryId'] or entry['SheetLevel']<1 or any(e['SheetLevel']<entry['SheetLevel'] and e['Status'] not in (30,40) for e in sheet['entries']):
        raise ValueError('当前审批入口或层级不符，不越级转派。')
    prior=await history.prior_decision_evidence(context,sheet,entry) if entry['LastDecisionId'] is not None else None
    old=await rows_for(obj['Id'],entry['ApprovalSheetEntryId'],sheet['LastApprovalCycleGMT'])
    if old:raise ValueError('本周期已有转派历史，不开放连续转委托或重放。')
    owner=await t.asset('owner',key=entry['ApproverId']);role=await t.asset('role',key=entry['ApproverRoleId'])
    target=await t.asset('owner',name=q.text(args['target_employee_name']))
    if owner['InstanceID']==target['InstanceID']:raise ValueError('新审批人须与原审批人不同。')
    return dict(context=context,raw_target=raw,organization=org,sheet=sheet,entry=entry,source_employee=owner,target_employee=target,role=role,
        reason=await reason(q.text(args['delegation_reason_name'])),prior_decision=prior,prior_delegations=old)


def body_for(before,args,marker):
    return dict(trackableObject={'instanceId':before['context']['target']['Id']},delegationTarget={'instanceId':before['entry']['ApprovalSheetEntryId']},
        targetEmployee=t.ref(before['target_employee']),delegationReasonCode=t.ref(before['reason']),comments=marker+' '+u.text_value(args['comments'],'转派说明',200,True))


def contract(doc):
    try:
        props=doc['components']['schemas']['DelegateTask']['properties']
        for field,kind in [('trackableObject','BaseObjectRef'),('delegationTarget','BaseObjectRef'),('targetEmployee','NamedObjectRef'),('delegationReasonCode','NamedObjectRef')]:
            if props[field]['$ref'].split('/')[-1]!=kind:raise ValueError()
        if props['comments']['type']!='string':raise ValueError()
        root=doc['paths']['/api/DelegateTask']['post']['requestBody']['content']['application/json']['schema']
        if root['$ref'].split('/')[-1]!='DelegateTask':raise ValueError()
    except (KeyError,ValueError,TypeError):raise ValueError('当前DelegateTask引用和固定POST契约未核实。') from None
    return props


async def build(args,marker,execution_binding=None):
    from core.signature_channel import verified_channel
    with verified_channel():return await _build(args,marker,execution_binding)


async def _build(args,marker,execution_binding):
    before=await source(args);body=body_for(before,args,marker)
    doc=json.loads(await request('GET','/swagger/DelegateTask/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json))
    props=contract(doc);bound=auth.capture_headers('DelegateTask')
    params={'execute':'false','$select':'TrackableObject,DelegationTarget,TargetEmployee,DelegationReasonCode,Comments,Employee,ESigRequirement,AdditionalESigReqs,ESigDetails,ProcessESignatures'}
    data=u.parse_reply(await request('POST','/api/DelegateTask',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,params=params,
        bound_headers=bound,require_stable_session=True,response_formatter=raw_json))
    if not p.wire_matches(body,data) or iid(q.identifier(data.get('Employee')))!=before['source_employee']['InstanceID']:
        raise ValueError('转派回显或原生当前员工不符；仅当前指定审批人本人可转派，不覆盖Employee。')
    if ('ESigRequirement' not in data or data['ESigRequirement'] is not None or 'AdditionalESigReqs' not in data or data['AdditionalESigReqs'] not in (None,[])
        or 'ESigDetails' not in data or data['ESigDetails'] not in (None,[]) or a.i.wire_bool(data.get('ProcessESignatures'),'签名处理') is not True):
        raise ValueError('转派签名要求非空或未知；不绕过或关闭原生签名处理。')
    source_proof=await auth.membership(before['source_employee']['InstanceID'],before['organization'],before['role'])
    target_proof=await auth.membership(before['target_employee']['InstanceID'],before['organization'],before['role'])
    if await source(args)!=before or await auth.membership(source_proof['actor']['InstanceID'],before['organization'],before['role'])!=source_proof or await auth.membership(target_proof['actor']['InstanceID'],before['organization'],before['role'])!=target_proof:
        raise ValueError('准备期间审批周期、条目、人员、原因或角色/组织授权变化。')
    if execution_binding is not None:execution_binding.update(headers=bound,actor_id=source_proof['actor']['InstanceID'])
    reads=set()
    for key in ('organization','source_employee','target_employee','role','reason'):reads.update(resources(before[key]['entity'],before[key]))
    return dict(kind='quality_approval_delegation',action='delegate',service='DelegateTask',name=before['context']['target']['Name'],target_id=before['context']['target']['Id'],
        scope_key='quality-event:id:'+before['context']['target']['Id'],before=before,body=body,arguments=args,marker=marker,environment=p.environment(),
        source_authorization=source_proof,target_authorization=target_proof,resource_reads=sorted(reads),
        resource_writes=['runtime:quality-event:id:'+before['context']['target']['Id'],'runtime:approval-sheet:id:'+before['sheet']['ApprovalSheetId']],
        preview=dict(verified_input_echo=body,contract_digest=p.digest(props),employee_id=source_proof['actor']['InstanceID'],
            original_approver_id=source_proof['actor']['InstanceID'],target_approver_id=target_proof['actor']['InstanceID'],
            approval_cycle_gmt=history.cycle_key(before['sheet']['LastApprovalCycleGMT']),source_role_verified=True,target_role_verified=True,
            dynamic_validation_verified=False,delegation_performed_verified=False,automatic_delegation_task_created=False,
            note='仅当前条目一次转派；原生可能通知原/新审批人。不创建日期委托任务、不改审批决策；实际转派与历史待核实，unknown不重试。'))


def validate_transport(snap):
    if (snap.get('kind')!='quality_approval_delegation' or snap.get('service')!='DelegateTask' or snap.get('action')!='delegate'
        or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker','')) or snap['body']!=body_for(snap['before'],snap['arguments'],snap['marker'])
        or snap['preview']['employee_id']!=snap['before']['entry']['ApproverId'] or snap['target_id']!=snap['before']['context']['target']['Id']):
        raise ValueError('转派服务、原对象、员工或允许载荷不符。')


async def inspect(snap):
    from core.signature_channel import verified_channel
    with verified_channel():
        with_verified=await a.read_context(snap['arguments']['object_kind'],snap['name'],snap['arguments']['organization_name'],snap['target_id'])
        rows=await rows_for(snap['target_id'],snap['before']['entry']['ApprovalSheetEntryId'],snap['before']['sheet']['LastApprovalCycleGMT'])
    return dict(current_context=with_verified,original_entry_id=snap['before']['entry']['ApprovalSheetEntryId'],delegation_history=rows,
        planned_source_employee_id=snap['before']['source_employee']['InstanceID'],planned_target_employee_id=snap['before']['target_employee']['InstanceID'],
        verified_success=False,state_changed=False,retry_allowed=False,note='原条目/原周期持久转派候选，仅供人工最终审计，不自动认定本次请求成功或解占用。')


@mcp.tool
async def prepare_quality_approval_delegation(name:str,organization_name:str,approval_sheet_id:str,approval_entry_id:str,approval_cycle_gmt:str,
        target_employee_name:str,delegation_reason_name:str,comments:str,object_kind:Literal['event','capa']='event',instance_id:str|None=None,allow_notifications:bool=False)->str:
    """准备当前指定审批人本人将一个已路由质量审批条目转派给明确新人员的原生DelegateTask计划。须明确原表/条目/周期、新员工、委托原因及非空说明；可能通知原/新员工，须明确allow_notifications=true。
    限已提交基础Event/CAPA、Named1、当前入口、无已签批/日期委托任务/本周期转派链；当前员工及双方原角色/组织授权须核实，身份会话内存绑定。验证证书HTTPS，不覆盖Employee或直接修改条目。
    系统确认后单次提交，unknown不重试；inspect提供原生DelegateTaskHistory。最终审计后新审批人可用明确历史ID及allow_delegated_approval=true签批，不自动切换账号或代签。
    日期自动委托、转委托、批量、撤销委托、委托配置维护及转派签名要求仍拒绝。预览不是转派或权限验收。
    """
    return await p.prepare('quality_approval_delegation',dict(name=name,organization_name=organization_name,approval_sheet_id=approval_sheet_id,
        approval_entry_id=approval_entry_id,approval_cycle_gmt=approval_cycle_gmt,target_employee_name=target_employee_name,delegation_reason_name=delegation_reason_name,
        comments=comments,object_kind=object_kind,instance_id=instance_id,allow_notifications=allow_notifications))
