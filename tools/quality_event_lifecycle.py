"""Explicit lifecycle plans. Native validation remains authoritative; no bypass flags."""
import copy
import json
import re
from datetime import datetime,timedelta,timezone
from tools import mcp,quality as q,quality_plans as p,quality_event_creation as creation
from tools import quality_event_triage as t,quality_event_update as u,quality_event_close_plan as close
from tools.quality_spc_annotations import iid
from tools.quality_definitions import resources
from tools.process_context import ReadContext
from tools.modeling_details import raw_json
from tools.collection_validation import timestamp_ticks
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SCHEMA=u.SCHEMA|{'ProcessModel':'ProcessModelId,ParentId,Stage,IsFrozen,ChangeCount',
    'QualityCrossReference':'QualityCrossReferenceId,TrackingId,CrossReferenceId,ChangeCount',
    'ApprovalSheet':'ApprovalSheetId,ParentId,Status,ChangeCount',
    'NumberingRule':'NumberingRuleId,ChangeCount'}
SELECT='QualityObject,Organization,Owner,Role,TriageComplete,Comments,AvailableActions,IgnoreProcessModel,ESigRequirement,AdditionalESigReqs,QualityObjectDetail'

async def contract(service,fields):
    doc=json.loads(await request('GET','/swagger/'+service+'/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json))
    try:
        props=doc['components']['schemas'][service]['properties']
        for field,kind in fields.items():
            if kind=='ref':
                if props[field]['$ref'].split('/')[-1]!='NamedObjectRef':raise ValueError('引用类型不符')
            elif props[field]['type']!=kind:raise ValueError('字段类型不符')
    except (KeyError,TypeError,ValueError) as exc:raise ValueError('当前'+service+'输入契约不完整，不能准备计划。') from exc
    if service=='CreateEvent':
        try:
            if props['serviceDetail']['$ref'].split('/')[-1]!='EventDetail':raise ValueError('明细类型错误')
            detail=doc['components']['schemas']['EventDetail']['properties']
            fields={k[0].lower()+k[1:] for k in u.DETAIL_FIELDS}
            if not fields<=detail.keys() or set(detail)-fields-u.DETAIL_SYSTEM_FIELDS:raise ValueError('未知事件明细字段')
        except (ValueError,KeyError,TypeError) as exc:raise ValueError('当前创建明细未核实。') from exc
    return p.digest(props)

async def related(row):
    key=row['EventId']
    links=await q.sql_rows('SELECT TOP 21 NonconformanceReportId,EscalatedEventId,ContainerId,ChangeCount '
        f"FROM NonconformanceReport WHERE EscalatedEventId='{key}' ORDER BY NonconformanceReportId",21)
    if len(links)>20:raise ValueError('关联NCR超过20条。')
    for r in links:
        for field in ('NonconformanceReportId','EscalatedEventId','ContainerId'):r[field]=iid(r.get(field))
        if r['EscalatedEventId']!=key:raise ValueError('NCR关联指向错误。')
        r['ChangeCount']=iint(r.get('ChangeCount'))
    if len({r['NonconformanceReportId'] for r in links})!=len(links):raise ValueError('关联NCR重复。')
    return sorted(links,key=lambda r:r['NonconformanceReportId'])

def iint(value):
    n=u.i.wire_int(value,'变更计数/状态')
    if n<0:raise ValueError('负数状态/版本无效。')
    return n

async def source(args,service_type,status,complex_context=False):
    q.text(args['event_name']);q.text(args['organization_name'])
    await u.schema_check(SCHEMA)
    row=await u.event_record(args['event_name'],args.get('event_instance_id'))
    if not row or row['CDOTypeId']!=7489 or row['Status']!=status or not row['IsSubmitted'] or row['IsFrozen'] or not row['TriageComplete']:
        raise ValueError('仅支持明确状态、已提交/分流且未冻结的基础Event。')
    if row['EventDataId'] or row['ChecklistId'] or row['ChecklistSaved']:
        raise ValueError('事件数据/批次自动处置/NCR子事务或检查表执行仍不在此入口范围。')
    if status==5 and not row['CloseDateGMT']:raise ValueError('Closed事件缺少关闭时间，不能准备开启。')
    if not complex_context and (row['ProcessModelId'] or row['OrigProcessModelTemplateId'] or row['IsCARRequiredToClose']):
        raise ValueError('此入口不处理流程或CAR上下文。')
    for field in ('OwnerId','RoleId','PriorityLevelId'):iid(row[field])
    org=await t.asset('organization',name=args['organization_name'],key=row['OrganizationId'],signature_txn_type=service_type)
    before={'event':row,'organization':org,'owner':await t.asset('owner',key=row['OwnerId']),
        'role':await t.asset('role',key=row['RoleId']),'priority':await t.asset('priority',key=row['PriorityLevelId']),
        'linked_ncrs':await related(row),'complex_context':complex_context}
    if before['linked_ncrs'] and not complex_context:raise ValueError('当前操作不处理关联NCR。')
    sheets=await q.sql_rows('SELECT TOP 21 ApprovalSheetId,ParentId,Status,ChangeCount '
        f"FROM ApprovalSheet WHERE ParentId='{row['EventId']}' ORDER BY ApprovalSheetId",21)
    if len(sheets)>20:raise ValueError('审批表超过20条。')
    for sheet in sheets:
        sheet['ApprovalSheetId']=iid(sheet.get('ApprovalSheetId'))
        if iid(sheet.get('ParentId'))!=row['EventId']:raise ValueError('审批表父级错误。')
        sheet['Status']=iint(sheet.get('Status'));sheet['ChangeCount']=iint(sheet.get('ChangeCount'))
        if sheet['Status']!=40:raise ValueError('存在未最终Approved=40的审批表，不代签、不撤回或重启审批。')
    if len({s['ApprovalSheetId'] for s in sheets})!=len(sheets):raise ValueError('审批表重复。')
    if sheets and not complex_context:raise ValueError('当前操作不处理审批上下文。')
    before['approvals']=sheets;before['process']=None;before['cross_references']=[];before['car_targets']=[]
    if row['ProcessModelId']:
        rows=await q.sql_rows('SELECT TOP 1 '+SCHEMA['ProcessModel']+f" FROM ProcessModel WHERE ProcessModelId='{row['ProcessModelId']}'",1)
        if len(rows)!=1 or iid(rows[0].get('ProcessModelId'))!=row['ProcessModelId'] or iid(rows[0].get('ParentId'))!=row['EventId']:
            raise ValueError('流程持久身份或归属不符。')
        proc=rows[0];proc['Stage']=iint(proc.get('Stage'));proc['ChangeCount']=iint(proc.get('ChangeCount'))
        if u.i.wire_bool(proc.get('IsFrozen'),'流程冻结状态'):raise ValueError('流程已冻结。')
        before['process']=proc
    if complex_context:
        refs=await q.sql_rows('SELECT TOP 51 '+SCHEMA['QualityCrossReference']+
            f" FROM QualityCrossReference WHERE TrackingId='{row['EventId']}' ORDER BY QualityCrossReferenceId",51)
        if len(refs)>50:raise ValueError('交叉引用超过50条。')
        for r in refs:
            for field in ('QualityCrossReferenceId','TrackingId','CrossReferenceId'):r[field]=iid(r.get(field))
            r['ChangeCount']=iint(r.get('ChangeCount'))
            if r['TrackingId']!=row['EventId']:raise ValueError('交叉引用父级不符。')
            rows=await q.sql_rows('SELECT TOP 1 Id,Category,Status,Organization FROM QualityObject '+f"WHERE Id='{r['CrossReferenceId']}'",1)
            if len(rows)!=1 or iid(rows[0].get('Id'))!=r['CrossReferenceId']:raise ValueError('关联质量对象未核实。')
            target=rows[0];target['Id']=iid(target.get('Id'));target['Organization']=iid(target.get('Organization'))
            target['Category']=iint(target.get('Category'));target['Status']=iint(target.get('Status'))
            if target['Status']==6 or iid(target.get('Organization'))!=row['OrganizationId']:raise ValueError('关联对象已删除或组织不符。')
            before['car_targets'].append(target)
        if len({r['QualityCrossReferenceId'] for r in refs})!=len(refs):raise ValueError('交叉引用重复。')
        before['cross_references']=refs
        if row['IsCARRequiredToClose'] and not any(r['Category'] in (2,5) for r in before['car_targets']):
            raise ValueError('要求CAR/CAPA关联但未核实有效关联，不修改CAR策略。')
    return before

def verify(data,body,before,action=None,complex_context=False):
    if not p.wire_matches(body,data):raise ValueError('生命周期请求未完整回显。')
    mask=iint(data.get('AvailableActions'))
    if action is not None and mask&action!=action:raise ValueError('缺少原生动作权限。')
    if u.i.wire_bool(data.get('IgnoreProcessModel'),'流程绕过标志') is not False:raise ValueError('不允许流程绕过。')
    if 'ESigRequirement' not in data or data['ESigRequirement'] is not None or 'AdditionalESigReqs' not in data or data['AdditionalESigReqs'] not in (None,[]):
        raise ValueError('原生签名要求非空或未知。')
    detail=data.get('QualityObjectDetail');row=before['event']
    if not isinstance(detail,dict) or not set(u.CURRENT_FIELDS.split(','))<=detail.keys():raise ValueError('原生状态上下文不完整。')
    for k in ('Status','Category'):
        if iint(detail[k])!=row[k]:raise ValueError('原生状态/类别不一致。')
    for k,col in [('Organization','OrganizationId'),('Owner','OwnerId'),('Role','RoleId'),('PriorityLevel','PriorityLevelId')]:
        if q.identifier(detail[k])!=row[col]:raise ValueError('原生归属/优先级不一致。')
    for k,want in [('TriageComplete',True),('IsCARRequiredToClose',row['IsCARRequiredToClose']),('UserIsOwner',True),('UserHasRole',True),('IsApprovalRequired',False)]:
        if u.i.wire_bool(detail[k],k) is not want:raise ValueError('原生账号/分流/CAR/审批不满足要求。')
    if row['ProcessModelId']:
        if q.identifier(detail['ProcessModel'])!=row['ProcessModelId']:raise ValueError('流程引用不一致。')
    elif detail['ProcessModel'] is not None:raise ValueError('原生派生了额外流程。')
    if complex_context:
        approved={s['ApprovalSheetId'] for s in before['approvals']}
        if detail['CompletionApprovalSheet'] is not None:
            if q.identifier(detail['CompletionApprovalSheet']) not in approved or iint(detail['CompletionApprovalStatus'])!=40:
                raise ValueError('完成审批未对应已Approved的原表。')
        elif detail['CompletionApprovalStatus'] not in (None,''):raise ValueError('无审批表却出现审批状态。')
    elif detail['CompletionApprovalSheet'] is not None or detail['CompletionApprovalStatus'] not in (None,''):
        raise ValueError('当前操作不处理审批。')
    if detail['EntryForApproval'] is not None:raise ValueError('存在当前待审批入口。')
    return dict(verified_input_echo=body,available_actions_raw=mask,
        current_quality_context={k:copy.deepcopy(detail[k]) for k in u.CURRENT_FIELDS.split(',')},
        dynamic_validation_verified=False,workflow_completion_verified=False,event_change_verified=False)

async def operation(args,marker,kind):
    service,typ,status,action=('OpenQualityObject',8412,5,None) if kind=='event_open' else ('ChangeOwner',7728,1,4)
    u.text_value(args.get('comments',''),'事务备注',200)
    before=await source(args,typ,status);row=before['event']
    body=dict(qualityObject={'name':row['EventName'],'instanceId':row['EventId']},organization=t.ref(before['organization']),
        comments=marker+(' '+args['comments'] if args.get('comments') else ''))
    refs={};policy=None
    fields={'qualityObject':'ref','organization':'ref'}
    if kind=='event_reassign':
        refs={'new_owner':await t.asset('owner',name=args['owner_name']),'new_role':await t.asset('role',name=args['role_name'])}
        if refs['new_owner']['InstanceID']==row['OwnerId'] and refs['new_role']['InstanceID']==row['RoleId']:raise ValueError('归属未变化。')
        policy=await creation.notification_policy(before['organization'])
        if type(args['allow_notifications']) is not bool or (policy['smtp_configured'] and policy['entries'] and not args['allow_notifications']):
            raise ValueError('组织可能发送分派通知，须明确allow_notifications=true；不清空通知配置。')
        body.update(owner=t.ref(refs['new_owner']),role=t.ref(refs['new_role']),triageComplete=True)
        fields.update(owner='ref',role='ref',triageComplete='boolean')
    schema=await contract(service,fields)
    raw=await request('POST','/api/'+service,base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,
        params={'execute':'false','$select':SELECT,'$expand':close.EXPAND},response_formatter=raw_json)
    preview=verify(u.parse_reply(raw),body,before,action)
    if await source(args,typ,status)!=before:raise ValueError('准备期间原事件/配置/关联变化。')
    for key,state in refs.items():
        if await t.asset('owner' if key=='new_owner' else 'role',name=state['Name'],key=state['InstanceID'])!=state:raise ValueError('目标归属变化。')
    if policy is not None and await creation.notification_policy(before['organization'])!=policy:raise ValueError('通知策略变化。')
    preview.update(contract_digest=schema,notification_policy=policy,
        note='原生最终校验权限及目标人员/角色组合；预览不是业务批准。开启仅按原生设置Active，不手工清空旧关闭信息。')
    reads=set()
    for state in [before[k] for k in ('organization','owner','role','priority')]+list(refs.values()):reads.update(resources(state['entity'],state))
    return dict(kind=kind,action='open' if kind=='event_open' else 'reassign',service=service,name=row['EventName'],target_id=row['EventId'],
        scope_key='quality-event:id:'+row['EventId'],before=before,body=body,preview=preview,references=refs,
        marker=marker,arguments=args,environment=p.environment(),resource_reads=sorted(reads),resource_writes=['runtime:quality-event:id:'+row['EventId']])

def utc(value):
    ticks=timestamp_ticks(value);seconds,fraction=divmod(ticks,10000000)
    return (datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%S')+f'.{fraction:07d}Z'

async def numbering(org):
    ctx=ReadContext(3)
    data=await ctx.get('Organizations',org['InstanceID'],{'$select':'InstanceID,Name,NumberingRuleMap',
        '$expand':'NumberingRuleMap($select=InstanceID,Category,NumberingRule,IsFrozen)'})
    if q.identifier(data)!=org['InstanceID'] or q._has_next_link(data) or not isinstance(data.get('NumberingRuleMap'),list) or len(data['NumberingRuleMap'])>100:
        raise ValueError('自动编号映射不完整。')
    rows=data['NumberingRuleMap'];seen=set();matching=[]
    for r in rows:
        key=iid(q.identifier(r))
        if key in seen:raise ValueError('编号映射重复。')
        seen.add(key)
        if iint(r.get('Category'))==1:matching.append(r)
    if len(matching)!=1 or u.i.wire_bool(matching[0].get('IsFrozen'),'编号映射冻结状态'):raise ValueError('没有唯一有效的Event自动编号规则。')
    key=iid(q.identifier(matching[0].get('NumberingRule')))
    state=await ctx.get('NumberingRules',key,{'$select':'InstanceID,Name,IsFrozen,IsRollover,NumberingRuleType,SequenceLength,Prefix,Suffix'})
    if q.identifier(state)!=key or q._has_next_link(state) or u.i.wire_bool(state.get('IsFrozen'),'编号规则冻结') or u.i.wire_bool(state.get('IsRollover'),'编号回卷'):
        raise ValueError('编号规则无效、冻结或允许回卷。')
    if iint(state.get('NumberingRuleType'))!=1 or not 1<=iint(state.get('SequenceLength'))<=30:raise ValueError('仅支持禁止跳号、有效长度且不回卷的编号规则。')
    versions=await q.sql_rows(f"SELECT NumberingRuleId,ChangeCount FROM NumberingRule WHERE NumberingRuleId='{key}'",1)
    if len(versions)!=1 or iid(versions[0].get('NumberingRuleId'))!=key:raise ValueError('编号规则版本身份错误。')
    return dict(mapping_id=iid(q.identifier(matching[0])),rule=state,ChangeCount=iint(versions[0].get('ChangeCount')))

async def build_create(args,marker):
    validate_args={k:args[k] for k in ('organization_name','classification_name','subclassification_name','priority_level_name','brief_description','reporter_name','occurrence_date','reported_date','description','discovery_area')}
    check=json.loads(await creation.validate_quality_event_creation(event_name='',automatic_numbering=True,**validate_args))
    if not check['complete'] or not check['static_valid'] or check['signature_required']:raise ValueError('创建资料/配置不满足要求：'+str(check.get('error') or check.get('errors') or '要求签名'))
    mapping=check['classification_mapping']
    if not mapping['Owner'] or not mapping['Role'] or mapping['ChecklistTemplate'] or mapping['FailureModeGroup']:
        raise ValueError('创建仅支持唯一直接Owner/Role且无检查表/失败组的分类映射，不依赖未核实归属回退。')
    assigned={'owner':await t.asset('owner',key=mapping['Owner']),'role':await t.asset('role',key=mapping['Role'])}
    initiator=await t.asset('owner',name=args['initiator_name'])
    policy=check['notification_policy']
    if type(args['allow_notifications']) is not bool or policy['smtp_configured'] and policy['entries'] and not args['allow_notifications']:
        raise ValueError('保存未提交事件可能发送待分派通知，须明确allow_notifications=true。')
    u.text_value(args['comments'],'创建备注',200)
    org=check['references']['organization'];numbers=await numbering(org)
    def ref(kind):return t.ref(check['references'][kind])
    detail=dict(briefDescription=args['brief_description'],description=args['description'],discoveryArea=args['discovery_area'],
        reporter=t.ref(check['reporter']),reporterOrganization=ref('organization'),initiator=t.ref(initiator),initiatorOrganization=ref('organization'),
        priorityLevel=ref('priority'),occurrenceDate=args['occurrence_date'],occurrenceDateGMT=utc(args['occurrence_date']),
        reportedDate=args['reported_date'],reportedDateGMT=utc(args['reported_date']),eventDataDetail=None,regulatoryReportDetails=[])
    schema=await contract('CreateEvent',{'eventName':'string','submit':'boolean','organization':'ref','classification':'ref','subClassification':'ref'})
    body=dict(organization=ref('organization'),classification=ref('classification'),subClassification=ref('subclassification'),
        submit=False,serviceDetail=detail,comments=marker+(' '+args['comments'] if args['comments'] else ''))
    # No CreateEvent execute=false/PerformEvent calls: preparation is exclusively read-only.
    if await numbering(org)!=numbers:raise ValueError('自动编号配置变化。')
    refs=check['references']|{'reporter':check['reporter'],'initiator':initiator}|assigned
    reads=set()
    for kind,state in refs.items():
        entity=state.get('entity') or {'organization':'Organizations','classification':'Classifications','subclassification':'SubClassifications','priority':'PriorityLevels'}[kind]
        reads.update(resources(entity,state))
    reads.update(resources('NumberingRules',numbers['rule']))
    preview=dict(contract_digest=schema,static_creation_validation=check,assignment_defaults=assigned,numbering=numbers,
        dynamic_validation_verified=False,created_event_verified=False,native_preview_requested=False,
        note='本地静态计划；没有原生非执行回显。执行时原生自动编号，事件以Pending且未提交保存；按已审阅组织配置可能分派/通知。')
    return dict(kind='event_create',action='create',service='CreateEvent',name='[native automatic name]',target_id=None,
        scope_key='quality-event:create:org:'+iid(org['InstanceID']),event_org_id=iid(org['InstanceID']),before=check,body=body,preview=preview,
        initiator=initiator,assignment=assigned,numbering=numbers,arguments=args,marker=marker,environment=p.environment(),
        resource_reads=sorted(reads),resource_writes=['runtime:quality-event:org:'+iid(org['InstanceID'])])


def validate_transport(snap):
    kind=snap.get('kind');body=snap['body']
    if not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker','')):
        raise ValueError('生命周期关联标记无效。')
    if snap.get('action')!={'event_open':'open','event_reassign':'reassign','event_create':'create'}.get(kind):
        raise ValueError('生命周期动作不符。')
    expected_service={'event_open':'OpenQualityObject','event_reassign':'ChangeOwner','event_create':'CreateEvent'}.get(kind)
    if snap.get('service')!=expected_service or not expected_service:raise ValueError('生命周期服务与计划类型不符。')
    if kind=='event_create':
        check=snap['before'];args=snap['arguments'];org=t.ref(check['references']['organization'])
        expected=dict(organization=org,classification=t.ref(check['references']['classification']),subClassification=t.ref(check['references']['subclassification']),
            submit=False,comments=snap['marker']+(' '+args['comments'] if args['comments'] else ''),
            serviceDetail=dict(briefDescription=args['brief_description'],description=args['description'],discoveryArea=args['discovery_area'],
                reporter=t.ref(check['reporter']),reporterOrganization=org,initiator=t.ref(snap['initiator']),initiatorOrganization=org,
                priorityLevel=t.ref(check['references']['priority']),occurrenceDate=args['occurrence_date'],occurrenceDateGMT=utc(args['occurrence_date']),
                reportedDate=args['reported_date'],reportedDateGMT=utc(args['reported_date']),eventDataDetail=None,regulatoryReportDetails=[]))
        if (body!=expected or type(body.get('submit')) is not bool or snap.get('target_id') is not None
            or snap.get('event_org_id')!=iid(check['references']['organization']['InstanceID'])
            or snap.get('scope_key')!='quality-event:create:org:'+snap['event_org_id']):raise ValueError('创建请求字段、自动编号或Submit=false不符。')
    else:
        row=snap['before']['event'];args=snap['arguments']
        expected=dict(qualityObject={'name':row['EventName'],'instanceId':row['EventId']},organization=t.ref(snap['before']['organization']),
            comments=snap['marker']+(' '+args['comments'] if args.get('comments') else ''))
        if kind=='event_reassign':expected.update(owner=t.ref(snap['references']['new_owner']),role=t.ref(snap['references']['new_role']),triageComplete=True)
        if body!=expected or snap.get('target_id')!=row['EventId'] or snap.get('name')!=row['EventName']:raise ValueError('生命周期目标或允许字段不符。')
        if kind=='event_reassign' and type(body.get('triageComplete')) is not bool:raise ValueError('分流标志类型错误。')

async def inspect(snap,outcome=None):
    if snap['kind']=='event_create':
        candidate=None
        try:
            raw=json.loads((outcome or {}).get('raw_response','{}'));raw=raw.get('data',raw)
            candidate=iid(q.identifier(raw.get('QualityObject')))
        except (ValueError,TypeError,AttributeError):pass
        current=await t.event_record(instance_id=candidate) if candidate else None
        return dict(candidate_id_from_execution_response=candidate,candidate_record=current,verified_success=False,state_changed=False,retry_allowed=False,
            note='执行响应ID仅候选，不证明由本次创建；无ID不按名称猜测，不自动解锁或重试。')
    current=await t.event_record(instance_id=snap['target_id']);history=None
    if current:history=json.loads(await q.get_quality_history('quality_object',current['EventName'],snap['target_id'],limit=20))
    return dict(original_event_id=snap['target_id'],current_event=current,bounded_quality_history=history,
        verified_success=False,state_changed=False,retry_allowed=False,note='当前状态/归属相同不证明本次事务，不自动重试/解锁。')

@mcp.tool
async def prepare_quality_event_open(event_name:str,organization_name:str,event_instance_id:str|None=None,comments:str='')->str:
    """准备原生OpenQualityObject，将Closed基础Event请求开启为Active；已提交/已分流，无事件数据/批次/NCR联动、流程、检查表、审批或签名。
    固定原ID、原生账号/角色上下文，execute=false完整回显；不手工清除历史关闭时间，不把预览当权限批准。确认单次提交，unknown保留占用、不重试。
    """
    return await p.prepare('event_open',dict(event_name=event_name,organization_name=organization_name,event_instance_id=event_instance_id,comments=comments))

@mcp.tool
async def prepare_quality_event_reassignment(event_name:str,organization_name:str,owner_name:str,role_name:str,
        event_instance_id:str|None=None,comments:str='',allow_notifications:bool=False)->str:
    """准备Active基础Event原生ChangeOwner重新分派；明确新责任人及角色，原账号有ChangeOwner动作4并为原责任人且有角色。
    保持已分流标志，原生最终验证目标人员/角色/组织组合；无流程/检查表/审批/事件数据/NCR关联或签名。
    可能原生发送分派通知，已配置时须明确allow_notifications=true；展示原/新归属和通知策略后系统确认单次提交，unknown不重试。
    """
    return await p.prepare('event_reassign',dict(event_name=event_name,organization_name=organization_name,owner_name=owner_name,role_name=role_name,
        event_instance_id=event_instance_id,comments=comments,allow_notifications=allow_notifications))

@mcp.tool
async def prepare_quality_event_creation(organization_name:str,classification_name:str,subclassification_name:str,priority_level_name:str,
        brief_description:str,reporter_name:str,initiator_name:str,occurrence_date:str,reported_date:str,
        description:str='',discovery_area:str='',comments:str='',allow_notifications:bool=False)->str:
    """准备基础Event实际创建计划，仅使用原生自动编号，不接受指定名称以避免并发覆盖同名草稿。Submit固定false，保存Pending未提交事件，不自动分流。
    明确组织、唯一分类组合、直接Owner/Role映射、优先级、报告人/发现人及带时区时间；禁止回卷/跳号的有效编号规则。
    准备仅只读和本地静态校验，不调用CreateEvent execute=false或PerformEvent；无原生回显/动态批准。存在通知配置须明确allow_notifications=true。
    确认后仅一条CreateEvent execute=true，unknown保留整个组织事件占用；响应ID仅候选，不按名称猜测，不重试，最终证据人工review。
    """
    return await p.prepare('event_create',dict(organization_name=organization_name,classification_name=classification_name,subclassification_name=subclassification_name,
        priority_level_name=priority_level_name,brief_description=brief_description,reporter_name=reporter_name,initiator_name=initiator_name,
        occurrence_date=occurrence_date,reported_date=reported_date,description=description,discovery_area=discovery_area,comments=comments,allow_notifications=allow_notifications))
