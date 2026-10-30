"""Static Pending Event edit plans: never POST Submit=false as a fake preview."""
import copy
import json
import re
from pydantic import BaseModel,ConfigDict,Field
from tools import mcp,quality as q,quality_plans as p,quality_event_update as u,quality_event_triage as t
from tools import quality_approval_authorization as auth,quality_event_submission as submission,quality_event_lifecycle as lifecycle
from tools import quality_event_classification as classification
from tools.quality_spc_annotations import iid,schema_check
from tools.quality_definitions import resources
from tools.collection_validation import timestamp_valid,timestamp_ticks
from core.http_client import request
from core.signature_channel import verified_channel
from config import CAMSTAR_SHOPFLOOR_BASE_URL


class EventDraftChanges(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    brief_description:str|None=Field(default=None,max_length=100)
    description:str|None=Field(default=None,max_length=4000)
    discovery_area:str|None=Field(default=None,max_length=30)
    reporter_name:str|None=Field(default=None,max_length=100)
    reporter_organization_name:str|None=Field(default=None,max_length=100)
    initiator_name:str|None=Field(default=None,max_length=100)
    initiator_organization_name:str|None=Field(default=None,max_length=100)
    priority_level_name:str|None=Field(default=None,max_length=100)
    occurrence_date:str|None=Field(default=None,max_length=50)
    reported_date:str|None=Field(default=None,max_length=50)


REFS={'reporter_name':('owner','reporter'),'initiator_name':('owner','initiator'),
    'reporter_organization_name':('organization','reporterOrganization'),'initiator_organization_name':('organization','initiatorOrganization'),
    'priority_level_name':('priority','priorityLevel')}
DATES={'occurrence_date':('occurrenceDate','occurrenceDateGMT'),'reported_date':('reportedDate','reportedDateGMT')}
SCHEMA=u.SCHEMA|{'Event':u.SCHEMA['Event']+',ClassificationId,SubClassificationId'}


async def source(args):
    q.text(args['event_name']);q.text(args['organization_name'])
    await schema_check(SCHEMA)
    row=await u.event_record(args['event_name'],args.get('event_instance_id'))
    if (not row or row['CDOTypeId']!=7489 or row['Category']!=1 or row['Status']!=2 or row['IsFrozen'] or row['IsSubmitted'] or row['TriageComplete']):
        raise ValueError('草稿维护仅支持未冻结、未提交/未分流的Pending2 Category1基础Event。')
    if any(row[k] not in (None,'') for k in ('ProcessModelId','OrigProcessModelTemplateId','ChecklistId','EventDataId','CloseDateGMT')) or row['ChecklistSaved']:
        raise ValueError('草稿含流程、检查表、事件数据或关闭信息，不覆盖复杂上下文。')
    for table,key in [('ApprovalSheet','ApprovalSheetId'),('RegulatoryReport','RegulatoryReportId')]:
        if await q.sql_rows(f"SELECT TOP 1 {key} FROM {table} WHERE ParentId='{row['EventId']}'",1):raise ValueError('草稿已有审批/法规报告，不覆盖或清空。')
    if await q.sql_rows(f"SELECT TOP 1 NonconformanceReportId FROM NonconformanceReport WHERE EscalatedEventId='{row['EventId']}'",1):raise ValueError('本轮不修改关联NCR的事件草稿。')
    classes=await q.sql_rows('SELECT TOP 1 '+submission.CLASS_COLUMNS+f" FROM Event WHERE EventId='{row['EventId']}'",1)
    if (len(classes)!=1 or not set(submission.CLASS_COLUMNS.split(','))<=classes[0].keys() or classes[0]['EventId']!=row['EventId']
        or u.i.wire_int(classes[0]['ChangeCount'],'分类版本')!=row['ChangeCount']):raise ValueError('草稿分类/原版本读取不完整。')
    class_ids={f:None if classes[0][f] in (None,'') else iid(classes[0][f]) for f in ('ClassificationId','SubClassificationId')}
    # Submit=false suppresses the standard QualityTxn password requirement. We
    # show the policy but never claim it is satisfied for a later Submit=true.
    org=await t.asset('organization',name=args['organization_name'],key=row['OrganizationId'],signature_txn_type=None)
    owner=await t.asset('owner',key=iid(row['OwnerId']));role=await t.asset('role',key=iid(row['RoleId']))
    priority=await t.asset('priority',key=iid(row['PriorityLevelId'])) if row['PriorityLevelId'] not in (None,'') else None
    config=await classification.configuration(org,class_ids)
    return dict(event=row,organization=org,owner=owner,role=role,priority=priority,classifications=class_ids,linked_ncrs=[],classification_configuration=config)


async def load(before,marker,bound):
    body=u.root_body(before,marker)
    raw=await request('POST','/api/UpdateEvent/PerformEvent',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body={'event':{'eventName':'Load'},'inputData':body},params={'$select':u.SELECT+',Employee,ESigDetails,QualityESigDetail,Classification,SubClassification',
            '$expand':u.EXPAND},bound_headers=bound,require_stable_session=True,response_formatter=u.raw_json)
    data=u.parse_reply(raw);current=u.validate_common(data,body,before,loaded=True);classification.verify_loaded(data,before)
    detail=data.get('ServiceDetail')
    allowed=set(u.DETAIL_FIELDS)|{'CDOTypeId','InstanceId','Parent','DisplayName','HistoryDetail','SmartBarcode'}
    if not isinstance(detail,dict) or set(detail)-allowed:
        raise ValueError('草稿Load含未核实的扩展资料，不丢弃未知字段后保存。')
    actor=iid(q.identifier(data.get('Employee')))
    if actor!=before['owner']['InstanceID']:raise ValueError('原生当前员工不是原草稿责任人；不覆盖Employee或借报告人身份保存。')
    if 'ESigDetails' not in data or data['ESigDetails'] not in (None,[]) or 'QualityESigDetail' not in data or data['QualityESigDetail'] is not None:
        raise ValueError('草稿Load派生签名明细或要求未知，不转发口令、不绕过。')
    return dict(detail=u.loaded_detail(data,before),current=current,employee_id=actor)


def missing_fields(detail,before):
    missing=[]
    if not isinstance(detail['briefDescription'],str) or not detail['briefDescription'].strip():missing.append('brief_description')
    for field in ('reporter','initiator','priorityLevel'):
        if not detail[field]:missing.append(field)
    for field in ('occurrenceDate','occurrenceDateGMT','reportedDate','reportedDateGMT'):
        if not timestamp_valid(detail[field]):missing.append(field)
    missing.extend(f for f,k in before['classifications'].items() if k is None)
    return missing


async def resolve_changes(args):
    changes=EventDraftChanges.model_validate(args['changes']).model_dump(exclude_none=True)
    if not changes:raise ValueError('至少明确一项草稿资料变更；省略/null保留，不补默认人员或时间。')
    resolved={}
    for key,value in changes.items():
        if key in u.TEXT_FIELDS:u.text_value(value,key,u.TEXT_FIELDS[key][1])
        elif key in REFS:
            kind,_=REFS[key];q.text(value,key)
            resolved[key]=await t.asset(kind,name=value,**({'signature_txn_type':None} if kind=='organization' else {}))
        elif key in DATES:
            if not timestamp_valid(value):raise ValueError('发生/报告时间必须带明确时区且最多7位小数，不补当前时间。')
    return changes,resolved


def patch(original,changes,resolved):
    result=copy.deepcopy(original)
    for key,value in changes.items():
        if key in u.TEXT_FIELDS:result[u.TEXT_FIELDS[key][0]]=value
        elif key in REFS:result[REFS[key][1]]=t.ref(resolved[key])
        else:
            local,gmt=DATES[key];result[local]=value;result[gmt]=lifecycle.utc(value)
    if result['occurrenceDateGMT'] is not None and result['reportedDateGMT'] is not None:
        if timestamp_ticks(result['reportedDateGMT'])<timestamp_ticks(result['occurrenceDateGMT']):raise ValueError('拟保存报告时间早于发生时间，须明确修正日期。')
    if result==original:raise ValueError('明确草稿字段与原值相同，没有资料变更。')
    return result


def body_for(before,detail,marker,comments):
    body=u.root_body(before,marker,comments);body['serviceDetail']=copy.deepcopy(detail)
    return body


async def build(args,marker,execution_binding=None):
    with verified_channel():
        u.text_value(args['comments'],'草稿保存说明',200,True)
        before=await source(args);verified=await u.contract();bound=auth.capture_headers('UpdateEvent')
        original=await load(before,marker,bound);changes,resolved=await resolve_changes(args)
        detail=patch(original['detail'],changes,resolved)
        proof=await auth.membership(original['employee_id'],before['organization'],before['role'])
        if await source(args)!=before or await load(before,marker,bound)!=original:raise ValueError('准备期间原草稿、资料、权限/签名或归属变化。')
        if await resolve_changes(args)!=(changes,resolved) or await auth.membership(original['employee_id'],before['organization'],before['role'])!=proof:
            raise ValueError('准备期间拟用人员/组织/优先级或当前员工授权变化。')
        body=body_for(before,detail,marker,args['comments']);row=before['event'];reads=set()
        for state in list(before[k] for k in ('organization','owner','role','priority'))+list(resolved.values()):
            if state:reads.update(resources(state['entity'],state))
        reads.update(classification.read_resources(before['classification_configuration']))
        if execution_binding is not None:execution_binding.update(headers=bound,actor_id=original['employee_id'])
        return dict(kind='event_draft_update',action='save_pending_draft',service='UpdateEvent',name=row['EventName'],target_id=row['EventId'],
            scope_key='quality-event:id:'+row['EventId'],before=before,original_detail=original['detail'],resolved_changes=resolved,actor_authorization=proof,
            body=body,marker=marker,arguments=args,environment=p.environment(),resource_reads=sorted(reads),resource_writes=['runtime:quality-event:id:'+row['EventId']],
            preview=dict(native_load=verified,original_detail_verified=True,static_preview_only=True,patched_request_echo_verified=False,
                regular_nonexecuting_post_requested=False,employee_id=original['employee_id'],edit_context=original['current'],
                remaining_submission_fields=missing_fields(detail,before),ready_for_submission=False,dynamic_validation_verified=False,draft_saved_verified=False,
                classification_validation=before['classification_configuration']['current'],classification_write_support=classification.write_support(),
                note='仅固定Load与本地变更计划；Submit=false会强制内部执行，禁止普通POST假预览。确认后一次真实保存，保持Pending，不调用AutoTriage/正式提交；native字段校验被草稿分支禁用，MES最终效果待验收。'))


def validate_transport(snap):
    if (snap.get('kind')!='event_draft_update' or snap.get('service')!='UpdateEvent' or snap.get('action')!='save_pending_draft'
        or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker','')) or snap['target_id']!=snap['before']['event']['EventId']
        or snap.get('name')!=snap['before']['event']['EventName']
        or snap['before']['event']['Status']!=2 or snap['before']['event']['IsSubmitted'] is not False
        or snap['before']['event']['CDOTypeId']!=7489 or snap['before']['event']['Category']!=1
        or snap['before']['event']['IsFrozen'] is not False or snap['before']['event']['TriageComplete'] is not False):raise ValueError('草稿保存服务、原对象或状态不符。')
    changes=EventDraftChanges.model_validate(snap['arguments']['changes']).model_dump(exclude_none=True)
    expected=body_for(snap['before'],patch(snap['original_detail'],changes,snap['resolved_changes']),snap['marker'],snap['arguments']['comments'])
    if snap['body']!=expected or snap['body']['submit'] is not False:raise ValueError('草稿保存载荷、Submit=false或原资料保持值不符。')


async def read_context(args):
    with verified_channel():
        before=await source(args);verified=await u.contract();bound=auth.capture_headers('UpdateEvent')
        original=await load(before,'MCP-QUAL:'+'0'*32,bound)
        if await source(args)!=before:raise ValueError('读取草稿期间原数据或配置变化。')
        return dict(complete=True,before=before,native_contract=verified,original_detail=original['detail'],server_context=original['current'],
            missing_submission_fields=missing_fields(original['detail'],before),classification_validation=before['classification_configuration']['current'],
            classification_write_support=classification.write_support(),ready_for_execution=False,ready_for_submission=False,
            regular_nonexecuting_post_requested=False,note='只读原草稿和Load保持值。保存与正式提交是两个独立计划，未确认任何资料变更或提交许可。')


async def inspect(snap):
    # Reuse the exact native history association logic, selecting Submit=false.
    with verified_channel():
        out=await submission.inspect(snap,submit_flag=False)
        out.update(draft_saved_verified=False,planned_remaining_submission_fields=snap['preview']['remaining_submission_fields'])
        return out


@mcp.tool
async def prepare_quality_event_draft_update(event_name:str,organization_name:str,changes:EventDraftChanges,comments:str,event_instance_id:str|None=None)->str:
    """准备已有Pending2 Category1未提交草稿的资料保存，不正式提交。可明确修改简述/说明/发现区域、报告人/发现人及其组织、优先级、带时区发生/报告时间；省略/null保留，文字可显式空串清除。
    先get_quality_event_update_context(purpose='edit_pending_draft')核对原资料/缺项；不自动选人员/时间，不改事件组织/责任人/角色/分类。无流程/审批/检查表/法规报告/事件数据/NCR关联。
    原分类/子分类只读保持并核对原组织完整映射与配置版本；分类缺失/组合不符可保留草稿其它资料，正式提交会拒绝。分类修改原生写入未核实，先purpose='classify_pending_draft'诊断，不能给changes传分类或用CreateEvent覆盖。
    Submit=false原生PreValidate会禁用字段校验并强制内部执行，所以准备只调用固定Load与本地静态差异，不调用普通execute=false POST假预览。严格验证允许变更、原Edit2/责任人/原生角色组织及同一内存会话，验证证书HTTPS。
    系统确认后仅一次UpdateEvent execute=true、Submit=false保存。保持原ID/Pending，缺资料可继续保留；保存不完成签名、不运行正式提交AutoTriage，也不保证已具备提交条件。
    unknown不重试，最终人工证据审计后重新prepare_quality_event_submission进行独立Submit=true复核/确认，不能把保存和提交合并或绕过提交签名/通知/规则门禁。
    """
    return await p.prepare('event_draft_update',dict(event_name=event_name,organization_name=organization_name,event_instance_id=event_instance_id,
        changes=changes.model_dump(),comments=comments))
