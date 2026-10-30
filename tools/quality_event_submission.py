"""Submit one existing Pending Event through native UpdateEvent, retaining its detail."""
import copy
import json
import re
from tools import mcp,quality as q,quality_plans as p,quality_event_update as u,quality_event_triage as t,quality_event_creation as creation
from tools.quality_spc_annotations import iid,schema_check
from tools.quality_definitions import resources
from tools.process_context import ReadContext
from tools.modeling_details import raw_json,_has_next_link
from tools.collection_validation import timestamp_valid,timestamp_ticks
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

CLASS_COLUMNS='EventId,ChangeCount,ClassificationId,SubClassificationId'
SCHEMA=u.SCHEMA|{k:t.SCHEMA[k] for k in ('CategoryMap','TriageSpec','TriageSpecDetail')}
SCHEMA['Event']+=',ClassificationId,SubClassificationId'


async def source(args,enforce_notice=True):
    q.text(args['event_name']);q.text(args['organization_name'])
    if args.get('event_instance_id') is not None:iid(args['event_instance_id'])
    if type(args.get('allow_notifications',False)) is not bool:raise ValueError('allow_notifications须为明确布尔值。')
    await schema_check(SCHEMA)
    row=await u.event_record(args['event_name'],args.get('event_instance_id'))
    if (not row or row['CDOTypeId']!=7489 or row['Category']!=1 or row['Status']!=2 or row['IsFrozen'] or row['IsSubmitted'] or row['TriageComplete']):
        raise ValueError('正式提交仅支持Category1、Pending2、未提交/未分流且未冻结的基础Event，不重复提交Active对象。')
    if any(row[k] not in (None,'') for k in ('ProcessModelId','OrigProcessModelTemplateId','ChecklistId','CloseDateGMT','EventDataId')) or row['ChecklistSaved']:
        raise ValueError('草稿含流程、事件数据、检查表或关闭信息，不属于本轮正式提交范围。')
    for f in ('OwnerId','RoleId','PriorityLevelId','ReporterId','InitiatorId'):iid(row[f],f)
    u.text_value(row['BriefDescription'],'原简述',100,True)
    for table,key in [('ApprovalSheet','ApprovalSheetId'),('RegulatoryReport','RegulatoryReportId')]:
        if await q.sql_rows(f"SELECT TOP 1 {key} FROM {table} WHERE ParentId='{row['EventId']}'",1):raise ValueError('草稿存在审批表或法规报告，本入口不挂表/清表或覆盖法规资料。')
    if await q.sql_rows(f"SELECT TOP 1 NonconformanceReportId FROM NonconformanceReport WHERE EscalatedEventId='{row['EventId']}'",1):
        raise ValueError('本轮不提交关联NCR的草稿事件。')
    # Classifications stay on the original object; they are never sent as edits.
    classes=await q.sql_rows('SELECT TOP 1 '+CLASS_COLUMNS+f" FROM Event WHERE EventId='{row['EventId']}'",1)
    if len(classes)!=1 or not set(CLASS_COLUMNS.split(','))<=classes[0].keys() or classes[0]['EventId']!=row['EventId'] or u.i.wire_int(classes[0]['ChangeCount'],'类别版本')!=row['ChangeCount']:
        raise ValueError('草稿分类身份或版本未核实。')
    classifications={k:iid(classes[0][k]) for k in ('ClassificationId','SubClassificationId')}
    org=await t.asset('organization',name=args['organization_name'],key=row['OrganizationId'],signature_txn_type=7578)
    owner=await t.asset('owner',key=row['OwnerId']);role=await t.asset('role',key=row['RoleId']);priority=await t.asset('priority',key=row['PriorityLevelId'])
    configuration=await t.triage_configuration(row)
    spec=configuration['spec'];root=await ReadContext(1).get('TriageSpecs',spec['InstanceID'],
        {'$select':'InstanceID,Name,IsFrozen,BusinessRules','$expand':'BusinessRules($select=InstanceID)'})
    if (q.identifier(root)!=spec['InstanceID'] or root.get('Name')!=spec['Name'] or _has_next_link(root) or u.i.wire_bool(root.get('IsFrozen'),'分流规则冻结')
        or 'BusinessRules' not in root or root['BusinessRules']!=[]):
        raise ValueError('提交后AutoTriage会执行分流BusinessRules；规则非空/未知则拒绝，不禁用或清空。')
    notification=await creation.notification_policy(org)
    if enforce_notice and notification['smtp_configured'] and notification['entries'] and not args.get('allow_notifications',False):
        raise ValueError('正式提交可能原生发送责任归属通知，须明确allow_notifications=true。')
    return dict(event=row,organization=org,owner=owner,role=role,priority=priority,classifications=classifications,linked_ncrs=[],
        triage_configuration=configuration,auto_triage_business_rules=[],notification_policy=notification)


def body_for(before,original,marker,comments):
    body=u.root_body(before,marker,comments);body['submit']=True;body['serviceDetail']=copy.deepcopy(original)
    return body


async def original_detail(before,marker):
    loaded=await u.load(before,marker);detail=loaded['detail']
    if any(not timestamp_valid(detail.get(f)) for f in ('occurrenceDate','occurrenceDateGMT','reportedDate','reportedDateGMT')):
        raise ValueError('原草稿发生/报告时间不完整或无明确时区，不能补当前时间后提交。')
    if timestamp_ticks(detail['reportedDateGMT'])<timestamp_ticks(detail['occurrenceDateGMT']):raise ValueError('原报告时间早于发生时间，须先明确修订草稿资料。')
    return loaded


async def build(args,marker):
    u.text_value(args.get('comments',''),'提交说明',200,True)
    before=await source(args);verified=await u.contract();original=await original_detail(before,marker)
    body=body_for(before,original['detail'],marker,args['comments'])
    # Unlike the save-draft branch, Submit=true does not activate the standard
    # PreValidate override that forces Transaction::IsExecute=true.
    raw=await request('POST','/api/UpdateEvent',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,
        params={'execute':'false','$select':u.SELECT,'$expand':u.EXPAND},response_formatter=raw_json)
    data=u.parse_reply(raw);current=u.validate_common(data,body,before)
    if u.i.wire_int(data['ServiceDetail'].get('CDOTypeId'),'事件明细类型')!=7541:raise ValueError('提交回显不是原7541事件明细。')
    if await source(args)!=before or await original_detail(before,marker)!=original:raise ValueError('准备正式提交期间草稿、原资料、权限、分类、分流或通知配置变化。')
    row=before['event'];reads=set()
    for state in [before[k] for k in ('organization','owner','role','priority')]+[before['triage_configuration']['spec']]:reads.update(resources(state['entity'],state))
    return dict(kind='event_submit',action='submit_pending',service='UpdateEvent',name=row['EventName'],target_id=row['EventId'],scope_key='quality-event:id:'+row['EventId'],
        before=before,original_detail=original['detail'],body=body,arguments=args,marker=marker,environment=p.environment(),
        resource_reads=sorted(reads),resource_writes=['runtime:quality-event:id:'+row['EventId']],
        preview=dict(verified_input_echo=body,native_load=verified,**current,dynamic_validation_verified=False,event_submission_verified=False,
            native_auto_triage_called=True,automatic_triage_completed_claimed=False,notification_policy=before['notification_policy'],
            note='提交会原生调用AutoTriage；本轮仅空BusinessRules且保留归属/类别/CAR、无流程/检查表的配置。不主动追加分流或签批，实际提交、分流标志及通知须核实。'))


def validate_transport(snap):
    row=snap['before']['event']
    if (snap.get('kind')!='event_submit' or snap.get('service')!='UpdateEvent' or snap.get('action')!='submit_pending'
        or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker','')) or snap.get('target_id')!=row['EventId'] or snap.get('name')!=row['EventName']
        or row['Status']!=2 or row['IsSubmitted'] is not False or snap['body']!=body_for(snap['before'],snap['original_detail'],snap['marker'],snap['arguments']['comments'])
        or snap['body']['submit'] is not True):raise ValueError('事件正式提交服务、原草稿、原明细或Submit=true不符。')


async def inspect(snap):
    current=await u.event_record(instance_id=snap['target_id']);bounded=None
    if current:bounded=json.loads(await q.get_quality_history('quality_object',current['EventName'],snap['target_id'],limit=20))
    await schema_check({'UpdateEventHistory':'UpdateEventHistoryId,HistoryId,HistoryMainlineId,TxnId,Submit,ChangeCount',
        'HistoryMainline':'HistoryMainlineId,HistoryId,TxnId,TxnType,ReversalStatus,Comments'})
    columns='UpdateEventHistoryId,HistoryId,HistoryMainlineId,TxnId,Submit,ChangeCount,TxnType,ReversalStatus,Comments'
    rows=await q.sql_rows('SELECT TOP 21 s.UpdateEventHistoryId,s.HistoryId,s.HistoryMainlineId,s.TxnId,s.Submit,s.ChangeCount,'
        'h.TxnType,h.ReversalStatus,h.Comments FROM UpdateEventHistory s JOIN HistoryMainline h '
        'ON h.HistoryMainlineId=s.HistoryMainlineId AND h.HistoryId=s.HistoryId AND h.TxnId=s.TxnId '
        f"WHERE s.HistoryId='{snap['target_id']}' AND s.Submit=1 AND h.Comments="+q.literal(snap['body']['comments'])+
        ' ORDER BY s.UpdateEventHistoryId',21)
    if len(rows)>20:raise ValueError('正式提交候选历史超过20条，不返回部分证据。')
    clean=[];seen=set()
    for raw in rows:
        if not isinstance(raw,dict) or not set(columns.split(','))<=raw.keys():raise ValueError('正式提交历史字段缺失。')
        row={k:copy.deepcopy(raw[k]) for k in columns.split(',')}
        for f in ('UpdateEventHistoryId','HistoryId','HistoryMainlineId','TxnId'):row[f]=iid(row[f])
        row['Submit']=u.i.wire_bool(row['Submit'],'历史Submit')
        for f in ('ChangeCount','TxnType','ReversalStatus'):row[f]=u.i.wire_int(row[f],f)
        if (row['HistoryId']!=snap['target_id'] or row['Comments']!=snap['body']['comments'] or not row['Submit'] or row['TxnType']!=7578 or row['UpdateEventHistoryId'] in seen):
            raise ValueError('提交历史不属于原对象/标记/UpdateEvent Submit=true事务，或清单重复。')
        seen.add(row['UpdateEventHistoryId']);clean.append(row)
    # State only controls which read-only context is worth checking next; it
    # cannot clear the unknown attempt or authorize a downstream mutation.
    candidate=bool(current and current['Status']==1 and current['IsSubmitted'] and not current['TriageComplete'])
    return dict(original_event_id=snap['target_id'],current_event=current,bounded_quality_history=bounded,submission_history_candidates=clean,
        manual_triage_context_candidate=candidate,verified_success=False,state_changed=False,retry_allowed=False,
        automatic_triage_completed_verified=False,next_action_authorized=False,
        note='当前Active/IsSubmitted或分流标志仅证据，不归因本次提交。unknown须最终人工审计；之后按实际状态查询首次分流上下文，不重复提交或自动补发分流。')


async def read_context(args):
    before=await source(args,False);contract=await u.contract();original=await original_detail(before,'MCP-QUAL:'+'0'*32)
    if await source(args,False)!=before:raise ValueError('读取正式提交上下文期间草稿或配置变化。')
    return dict(complete=True,before=before,native_contract=contract,original_detail=original['detail'],server_context=original['current'],ready_for_execution=False,
        submission_permission_verified=False,native_auto_triage_called_on_submit=True,note='仅草稿和Load保持值；非执行Load使用Submit=false，不证明实际Submit=true的签名/执行校验通过。')


@mcp.tool
async def prepare_quality_event_submission(event_name:str,organization_name:str,comments:str,event_instance_id:str|None=None,allow_notifications:bool=False)->str:
    """准备已有Pending2未提交基础Event的正式提交，固定原ID，原生UpdateEvent Submit=true，不创建同名替代草稿或直接改状态。
    先get_quality_event_update_context(purpose='submit_pending')只读核对。Load完整保持原报告人/发现人/时间/优先级及文字，不接受同时改资料/类别/责任人。
    原生提交会调用AutoTriage；本轮仅空BusinessRules且保留原类别/归属/CAR、无自动流程/检查表的唯一分流配置。可能发送归属通知时明确allow_notifications=true。
    需Edit2、责任人及角色，原签名要求非空/未知拒绝；审批、事件数据/NCR/法规报告等复杂上下文仍拒绝。Submit=true回显和独立重读后系统确认单次提交，unknown不重试。
    inspect只给原ID证据；最终人工审计后，按实际IsSubmitted/Status/TriageComplete查询首次分流，不把提交当作分流/审批/关闭完成，不自动补发分流。
    """
    return await p.prepare('event_submit',dict(event_name=event_name,organization_name=organization_name,event_instance_id=event_instance_id,comments=comments,allow_notifications=allow_notifications))
