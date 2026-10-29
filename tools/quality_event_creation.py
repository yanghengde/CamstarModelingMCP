"""Read-only creation input and policy validation; never calls CreateEvent."""
import copy
from tools import mcp,quality as q,quality_inspection as i,quality_event_triage as t,quality_event_update as u
from tools import quality_ncr_escalation as n
from tools.quality_spc_annotations import iid,schema_check
from tools.process_context import ReadContext
from tools.collection_validation import timestamp_valid,timestamp_ticks
from tools.quality_plans import digest

SCHEMA={'Event':'EventId,EventName,IsSubmitted,Status,ChangeCount',
        'Organization':'OrganizationId,ChangeCount','Classification':'ClassificationId,ChangeCount',
        'SubClassification':'SubClassificationId,ChangeCount','PriorityLevel':'PriorityLevelId,ChangeCount',
        'Employee':'EmployeeId,ChangeCount'}

async def existing(name):
    rows=await q.sql_rows('SELECT TOP 2 '+SCHEMA['Event']+' FROM Event WHERE EventName='+q.literal(name),2)
    if len(rows)>1:raise ValueError('同名事件不唯一，不能核验创建。')
    if not rows:return None
    row=rows[0]
    if not set(SCHEMA['Event'].split(','))<=row.keys() or row['EventName']!=name:raise ValueError('同名事件查询身份/字段不完整。')
    return dict(EventId=iid(row['EventId']),EventName=row['EventName'],IsSubmitted=i.wire_bool(row['IsSubmitted'],'提交状态'),
        Status=i.wire_int(row['Status'],'状态'),ChangeCount=i.wire_int(row['ChangeCount'],'版本'))

async def notification_policy(org):
    ctx=ReadContext(3)
    data=await ctx.get('Organizations',org['InstanceID'],{'$select':'InstanceID,Name,SMTPTransport,NotificationEvents',
        '$expand':'NotificationEvents($select=InstanceID,NotificationType,AssigneeOnly)'})
    if (q.identifier(data)!=org['InstanceID'] or data.get('Name')!=org['Name'] or q._has_next_link(data)
        or not {'SMTPTransport','NotificationEvents'}<=data.keys()):raise ValueError('组织通知策略未完整核实。')
    smtp=data['SMTPTransport']
    if smtp is not None:iid(q.identifier(smtp),'SMTP配置引用')
    rows=data['NotificationEvents']
    if not isinstance(rows,list) or len(rows)>100:raise ValueError('通知策略清单未知或超过100项。')
    entries=[]
    for row in rows:
        entries.append(dict(instance_id=iid(q.identifier(row)),notification_type=i.wire_int(row.get('NotificationType'),'通知类型'),
                            assignee_only=i.wire_bool(row.get('AssigneeOnly'),'收件人策略')))
    if len({r['instance_id'] for r in entries})!=len(entries):raise ValueError('通知明细重复。')
    return dict(smtp_configured=smtp is not None,entries=sorted(entries,key=lambda r:r['instance_id']),
                notification_effect_verified=False)

@mcp.tool
async def validate_quality_event_creation(event_name:str,organization_name:str,classification_name:str,
        subclassification_name:str,priority_level_name:str,brief_description:str,reporter_name:str,
        occurrence_date:str,reported_date:str,description:str='',discovery_area:str='')->str:
    """只读核验基础Event创建资料及组织配置。必填明确名称/组织/分类/子分类/优先级/简述/报告人和带时区的发生、报告时间。
    核对名称占用、唯一组织分类组合、原生默认归属候选、报告人身份、签名及SMTP/通知策略；不调用CreateEvent，包括execute=false。
    Submit=false仍可能原生分派/通知且预校验修改执行标志，当前入口只给静态核验报告；ready_for_execution恒false，无plan_id，不创建/覆盖草稿、不发送通知、不授权签名。
    """
    out=dict(complete=False,static_valid=None,ready_for_execution=False,created_event_verified=False,
        native_preview_requested=False,errors=[],unverified_checks=['当前部署CreateEvent预校验与execute=false的实际交互',
        '归属回退规则及通知收件人/实际发送行为','原生动态权限、审批和签名','创建后的持久ID及最终历史关联'])
    try:
        name=q.text(event_name,'事件名称',30);u.text_value(name,'事件名称',30,True)
        u.text_value(brief_description,'简述',100,True);u.text_value(description,'说明',4000);u.text_value(discovery_area,'发现区域',30)
        for value in (organization_name,classification_name,subclassification_name,priority_level_name,reporter_name):q.text(value)
        if not timestamp_valid(occurrence_date) or not timestamp_valid(reported_date):raise ValueError('发生和报告时间必须含明确时区，最多7位小数；不补当前时间。')
        if timestamp_ticks(reported_date)<timestamp_ticks(occurrence_date):raise ValueError('报告时间不能早于发生时间。')
        await schema_check(SCHEMA)
        before=await existing(name)
        if before:out['errors'].append(dict(code='name_occupied',detail='名称已存在，包含未提交草稿；不会使用CreateEvent覆盖原记录。'))
        states={}
        for kind,value in [('organization',organization_name),('classification',classification_name),('subclassification',subclassification_name),('priority',priority_level_name)]:
            state=await n.named_state(kind,value)
            if state['IsFrozen']:out['errors'].append(dict(code='frozen_'+kind,detail='参考配置已冻结。'))
            states[kind]=state
        org=states['organization'];signatures=org['QualityESigTxns']
        if not isinstance(signatures,list) or len(signatures)>100:raise ValueError('组织质量签名事务清单不完整。')
        signature_types=[i.wire_int(v,'签名事务类型') for v in signatures]
        if len(set(signature_types))!=len(signature_types) or any(v<1 for v in signature_types):raise ValueError('签名事务类型无效或重复。')
        reporter=await t.asset('owner',name=reporter_name)
        maps=org['EventClassificationSpecMaps']
        matching=[r for r in maps if q.identifier(r['Classification'])==states['classification']['InstanceID']
                  and q.identifier(r['Subclassification'])==states['subclassification']['InstanceID']]
        mapping=None
        if len(matching)!=1:out['errors'].append(dict(code='classification_mapping',detail='组织没有唯一分类/子分类组合，不能选择首行或传映射ID。'))
        else:
            row=matching[0]
            mapping={'instance_id':iid(row['InstanceID']),'is_frozen':i.wire_bool(row['IsFrozen'],'映射冻结状态')}
            for field in ('Owner','Role','ChecklistTemplate','FailureModeGroup'):
                value=row.get(field)
                mapping[field]=None if value is None else iid(q.identifier(value),field)
            if mapping['is_frozen']:out['errors'].append(dict(code='mapping_frozen',detail='所选分类映射已冻结。'))
        policy=await notification_policy(org)
        if await existing(name)!=before:raise ValueError('核验期间名称占用变化。')
        for kind,state in states.items():
            if await n.named_state(kind,state['Name'],state['InstanceID'])!=state:raise ValueError('核验期间参考配置或分类映射变化。')
        if await t.asset('owner',name=reporter['Name'],key=reporter['InstanceID'])!=reporter or await notification_policy(org)!=policy:
            raise ValueError('核验期间报告人或通知策略变化。')
        references={kind:{k:copy.deepcopy(state[k]) for k in ('Name','InstanceID','IsFrozen','ChangeCount')} for kind,state in states.items()}
        out.update(complete=True,static_valid=not out['errors'],existing_name_record=before,references=references,
            reporter=reporter,classification_mapping=mapping,notification_policy=policy,
            signature_required=org['CollectESigForAllQualityTxns'] or 7491 in signature_types,
            proposal=dict(event_name=name,submit=False,brief_description=brief_description,description=description,
                discovery_area=discovery_area,occurrence_date=occurrence_date,reported_date=reported_date),
            execution_blockers=['当前创建入口未开放执行；原生保存未提交事件仍可能分派并发送待分派通知。',
                '原生PreValidate修改Transaction::IsExecute，未核实部署行为前不调用创建服务预览。'])
        out['validation_digest']=digest({k:v for k,v in out.items() if k!='validation_digest'})
        return q.encode(out)
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        out['error']=str(exc);return q.encode(out)
