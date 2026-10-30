"""Bounded existing EventData text/date edits through native UpdateEventData.

PreValidate unconditionally sets IsExecute=true, so preparation is fixed Load
and local diff only. Preserve both EventDataDetailsToEventData and the service's
Event root map; never execute arbitrary custom details or material side effects.
"""
import copy
import json
import re
from pydantic import BaseModel,ConfigDict,Field
from tools import mcp,quality as q,quality_plans as p,quality_event_update as u,quality_event_triage as t
from tools import quality_event_classification as c,quality_approval_authorization as auth
from tools import quality_event_data_dates as dates
from tools.quality_definitions import resources
from tools.quality_spc_annotations import iid,schema_check
from tools.collection_validation import timestamp_valid,timestamp_ticks
from core.http_client import request
from core.signature_channel import verified_channel
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SERVICE='UpdateEventData';SERVICE_TYPE=8764;DATA_TYPE=8085;DETAIL_TYPE=8087
TEXT={'problem_description':('ProblemDescription',255),'device_location':('DeviceLocation',30)}
DATA_TEXT={'CauseCodeName':30,'MaintenanceReqName':30,'MaintenanceReqRev':30,'NonconformanceNumber':30,'OperationName':30,
    'ProductName':30,'ProductRev':15,'ResourceName':30,'SpecName':30,'SpecRev':15,'WorkCenterName':30,'WorkflowName':30,
    'WorkflowRev':15,'DeviceLocation':30,'EventDate':30,'ProblemDescription':255,'RecallNumber':30,'ReturnedContactName':30,
    'ReturnedPhoneNumber':30,'RMANumber':30,'SampleQuantity':30,'WorkflowStepName':30}
DATA_DATES=('DateReceived','DateReceivedGMT','ReportDate','ReportDateGMT')
DATA_REFS=('CompensateCustomerAction','Occupation')
DATA_FIELDS=tuple(DATA_TEXT)+DATA_DATES+DATA_REFS
COMPLEX_REFS=('ContactCustomerId','ReportingCustomerId','FailureInvestigationCommentsId','CorrectiveActionCommentsId',
    'PreventiveActionCommentsId','EventCustomDataId')
DATA_COLUMNS='EventDataId,EventId,CDOTypeId,ChangeCount,'+','.join((*DATA_TEXT,*DATA_DATES,*(f+'Id' for f in DATA_REFS),*COMPLEX_REFS))
ROOT_FIELDS=('BriefDescription','Description','DiscoveryArea',*u.REF_FIELDS,*u.DATE_FIELDS)
BLOCK_LISTS={'EventFailure':'EventFailureId,EventDataId','EventLot':'EventLotId,EventDataId','EventLog':'EventLogId,EventDataId',
    'EventFailureAction':'EventFailureActionId,ParentId','SuspectDeviceData':'SuspectDeviceDataId,EventDataId'}
SCHEMA=u.SCHEMA|c.SCHEMA|{'Event':u.SCHEMA['Event']+',ClassificationId,SubClassificationId','EventData':DATA_COLUMNS,
    'EventDisallowedTxns':'EventId,FieldId,Sequence,DisallowedTxns'}|BLOCK_LISTS
SELECT=('QualityObject,Organization,Comments,Employee,AvailableActions,IgnoreProcessModel,ESigRequirement,AdditionalESigReqs,'
    'ESigDetails,QualityESigDetail,QualityObjectDetail,Classification,SubClassification,RegulatoryReportDetails,DataCollectionDef,ParametricData,'
    'DisallowedTxns,EventDataDetail,'+','.join(ROOT_FIELDS))
EXPAND='QualityObjectDetail($select='+u.CURRENT_FIELDS+'),EventDataDetail($select=CDOTypeId,EventData,'+','.join(DATA_FIELDS)+'),RegulatoryReportDetails'
DETAIL_BUSINESS=set(DATA_FIELDS)|{'AdverseEventField','ContactCustomerDetail','CorrectiveActionComments','DeviceAvailableField','DeviceEvaluatedField',
    'DeviceOperatorField','DeviceReturnedField','EventCustomDetail','EventData','EventFailureActionDetails','EventFailureDetails','EventLogDetails',
    'EventLotDetails','EventTypeField','FailureInvestigationComments','HealthProfessionalField','PreventiveActionComments','ProductProblemField',
    'ReportFiledWithFDAField','ReportingCustomerDetail','ReportSourceField','ResourceGroup','SuspectDeviceDetails','WorkflowStack'}
SYSTEM={'displayName','instanceID','parent','smartBarcode','historyDetail'}


class DataScopeError(ValueError):
    """Only fixed, non-reflecting business scope messages are exposed to readers."""


def wire(field):return 'rmaNumber' if field=='RMANumber' else field[0].lower()+field[1:]


class EventDataChanges(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    problem_description:str|None=Field(default=None,max_length=255)
    device_location:str|None=Field(default=None,max_length=30)
    date_received:str|None=Field(default=None,max_length=50)
    report_date:str|None=Field(default=None,max_length=50)


# Preserve Python callers while the public schema describes text and dates.
EventDataTextChanges=EventDataChanges


def changes(args):
    values=EventDataChanges.model_validate(args['changes']).model_dump(exclude_none=True)
    if not values:raise ValueError('至少明确修改问题描述、设备位置或收到/报告日期；省略/null保持，不自动补写。')
    for key,value in values.items():
        if key in TEXT:u.text_value(value,key,TEXT[key][1])
        else:dates.utc(value)
    return values


async def disallowed(event_id):
    rows=await q.sql_rows('SELECT TOP 101 EventId,FieldId,Sequence,DisallowedTxns '+
        f"FROM EventDisallowedTxns WHERE EventId='{iid(event_id)}' ORDER BY Sequence",101)
    if len(rows)>100:raise ValueError('事件禁用事务清单超过100条，不使用部分清单。')
    clean=[]
    for row in rows:
        if not isinstance(row,dict) or not set(SCHEMA['EventDisallowedTxns'].split(','))<=row.keys():raise ValueError('禁用事务清单字段缺失。')
        item=dict(EventId=iid(row['EventId']),**{k:u.i.wire_int(row[k],k) for k in ('FieldId','Sequence','DisallowedTxns')})
        if item['EventId']!=event_id or item['FieldId']!=25978 or item['Sequence']<0 or item['DisallowedTxns']<1:raise ValueError('禁用事务清单归属/字段或数值不符。')
        clean.append(item)
    if len({r['Sequence'] for r in clean})!=len(clean) or len({r['DisallowedTxns'] for r in clean})!=len(clean):raise ValueError('禁用事务清单重复，不清理配置后执行。')
    if SERVICE_TYPE in [r['DisallowedTxns'] for r in clean]:raise ValueError('原事件明确禁止UpdateEventData，不移除禁用配置。')
    return sorted(clean,key=lambda r:r['Sequence'])


async def source(args,expected_data_id=None):
    q.text(args['event_name']);q.text(args['organization_name']);await schema_check(SCHEMA)
    row=await u.event_record(args['event_name'],args.get('event_instance_id'))
    if expected_data_id is not None and (not row or row['EventDataId']!=iid(expected_data_id)):
        raise DataScopeError('原EventData关联已变化，证据读取不跟随替代数据。')
    if not row or row['CDOTypeId']!=7489 or row['Category']!=1 or row['Status']!=1 or row['IsFrozen'] or not row['IsSubmitted'] or row['ChangeCount']<0:
        raise DataScopeError('扩展资料维护仅支持已提交Active Category1未冻结基础Event，不保存草稿/关闭/审批或制造NCR。')
    if any(row[k] not in (None,'') for k in ('ProcessModelId','OrigProcessModelTemplateId','ChecklistId','CloseDateGMT')) or row['ChecklistSaved']:
        raise DataScopeError('事件含流程、检查表或关闭信息，不覆盖复杂上下文。')
    for table,columns,key in [('ApprovalSheet',u.SCHEMA['ApprovalSheet'],'ParentId'),('RegulatoryReport',u.SCHEMA['RegulatoryReport'],'ParentId'),
        ('NonconformanceReport',u.SCHEMA['NonconformanceReport'],'EscalatedEventId')]:
        if await q.sql_rows('SELECT TOP 1 '+columns.split(',')[0]+f" FROM {table} WHERE {key}='{row['EventId']}'",1):raise ValueError('已有审批、法规报告或关联NCR，不使用扩展资料受限入口。')
    if row['EventDataId'] in (None,''):raise DataScopeError('原事件没有已有EventData，本入口仅维护原数据，不创建替代结构。')
    data_id=iid(row['EventDataId']);rows=await q.sql_rows('SELECT TOP 1 '+DATA_COLUMNS+f" FROM EventData WHERE EventDataId='{data_id}'",1)
    if len(rows)!=1 or not set(DATA_COLUMNS.split(','))<=rows[0].keys():raise ValueError('原EventData字段不完整，不以默认值保存。')
    data={k:copy.deepcopy(rows[0][k]) for k in DATA_COLUMNS.split(',')}
    if iid(data['EventDataId'])!=data_id or iid(data['EventId'])!=row['EventId'] or u.i.wire_int(data['CDOTypeId'],'事件数据类型')!=DATA_TYPE:
        raise DataScopeError('仅支持原事件所属已有8085标准EventData，不创建、替换或编辑未知派生类型。')
    data.update(EventDataId=data_id,EventId=row['EventId'],CDOTypeId=DATA_TYPE)
    if data['EventCustomDataId'] not in (None,''):
        raise DataScopeError('原记录有EventCustomData现场自定义资料，需实际字段、明细保存映射及部署契约；本入口不猜字段或覆盖自定义内容。')
    data['ChangeCount']=u.i.wire_int(data['ChangeCount'],'事件数据版本')
    if data['ChangeCount']<0 or any(data[k] not in (None,'') for k in COMPLEX_REFS) or data['NonconformanceNumber'] not in (None,''):
        raise DataScopeError('事件数据含客户/自定义数据/NCR评论或编号，受限入口不覆盖。')
    for table,cols in BLOCK_LISTS.items():
        pk,fk=cols.split(',')
        if await q.sql_rows(f"SELECT TOP 1 {pk} FROM {table} WHERE {fk}='{data_id}'",1):raise ValueError('事件数据含批次、失败、日志、动作或设备明细，不以空清单覆盖。')
    org=await t.asset('organization',name=args['organization_name'],key=row['OrganizationId'],signature_txn_type=SERVICE_TYPE)
    refs={k:await t.asset(k,key=iid(row[col])) for k,col in [('owner','OwnerId'),('role','RoleId'),('priority','PriorityLevelId')]}
    classes=await q.sql_rows('SELECT TOP 1 EventId,ChangeCount,ClassificationId,SubClassificationId '+f"FROM Event WHERE EventId='{row['EventId']}'",1)
    if len(classes)!=1 or iid(classes[0].get('EventId'))!=row['EventId'] or u.i.wire_int(classes[0].get('ChangeCount'),'原分类版本')!=row['ChangeCount']:
        raise ValueError('原分类资料版本/归属不符。')
    ids={k:None if classes[0].get(k) in (None,'') else iid(classes[0][k]) for k in c.KINDS}
    if not set(c.KINDS)<=classes[0].keys():raise ValueError('原分类保持值字段缺失。')
    return dict(event=row,data=data,organization=org,**refs,classifications=ids,classification_configuration=await c.configuration(org,ids),
        disallowed=await disallowed(row['EventId']),linked_ncrs=[])


async def contract():
    raw=await request('GET','/swagger/'+SERVICE+'/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=u.raw_json)
    try:
        doc=json.loads(raw);schemas=doc['components']['schemas'];schema=doc['paths']['/api/'+SERVICE+'/PerformEvent']['post']['requestBody']['content']['application/json']['schema']
        model=schemas[schema['$ref'].split('/')[-1]]['properties'];root_name=model['inputData']['$ref'].split('/')[-1];root=schemas[root_name]['properties']
        event=schemas[model['event']['$ref'].split('/')[-1]]['properties'];detail_name=root['eventDataDetail']['$ref'].split('/')[-1];detail=schemas[detail_name]['properties']
        if root_name!=SERVICE or detail_name!='EventDataDetails' or event['eventName']['type']!='string' or {'serviceDetail','serviceDetails','submit'}&root.keys():raise ValueError('服务字段不符')
        for field in ('qualityObject','organization','employee'):
            if root[field]['$ref'].split('/')[-1]!='NamedObjectRef':raise ValueError('根引用类型不符')
        for field in DATA_TEXT:
            if detail[wire(field)]['type']!='string':raise ValueError('扩展文字类型不符')
        for field in DATA_DATES:
            if detail[wire(field)]['type']!='string' or detail[wire(field)].get('format')!='date-time':raise ValueError('扩展时间类型不符')
        for field in DATA_REFS:
            if detail[wire(field)]['$ref'].split('/')[-1]!='NamedObjectRef':raise ValueError('扩展引用类型不符')
        if set(detail)-{wire(f) for f in DETAIL_BUSINESS}-SYSTEM:raise ValueError('未知扩展字段')
        if not {wire(f) for f in DETAIL_BUSINESS}<=detail.keys():raise ValueError('标准扩展字段缺失')
        if root['disallowedTxns']['type']!='array' or root['disallowedTxns']['items']['type']!='integer':raise ValueError('事务清单类型不符')
        for field in ROOT_FIELDS:
            expected='NamedObjectRef' if field in u.REF_FIELDS else 'string'
            prop=root[wire(field)]
            if (prop.get('$ref','').split('/')[-1] if field in u.REF_FIELDS else prop.get('type'))!=expected:raise ValueError('根保持字段类型不符')
        # Verify the actual mutation endpoint too, without calling it.
        post=doc['paths']['/api/'+SERVICE]['post']['requestBody']['content']['application/json']['schema']
        if post['$ref'].split('/')[-1]!=SERVICE:raise ValueError('保存输入类型不符')
    except (ValueError,TypeError,KeyError,AttributeError) as exc:raise ValueError('当前UpdateEventData固定Load/保存及8087明细契约未核实或存在未知扩展；不猜字段或普通POST预览。') from exc
    return dict(digest=p.digest(dict(model=model,root=root,detail=detail)),load_event='Load',schema_checked=True,ordinary_preview_allowed=False)


def root_body(before,marker,comments=''):
    u.text_value(comments,'扩展资料说明',200)
    return dict(qualityObject=dict(name=before['event']['EventName'],instanceId=before['event']['EventId']),organization=t.ref(before['organization']),comments=marker+(' '+comments if comments else ''))


def verify_context(data,before,body):
    if not p.wire_matches(body,data):raise ValueError('原事件/组织/标记未完整回显。')
    mask=u.i.wire_int(data.get('AvailableActions'),'Edit动作')
    if mask<0 or mask&2!=2 or u.i.wire_bool(data.get('IgnoreProcessModel'),'流程标志') is not False:raise ValueError('原生Edit权限或流程保持值不符。')
    for key in ('ESigRequirement','QualityESigDetail','DataCollectionDef','ParametricData'):
        if key not in data or data[key] is not None:raise ValueError('签名或数据采集要求非空/未知，不覆盖。')
    for key in ('AdditionalESigReqs','ESigDetails'):
        if key not in data or data[key] not in (None,[]):raise ValueError('签名明细非空或未知。')
    if data.get('RegulatoryReportDetails')!=[]:raise ValueError('法规报告非空或未知。')
    actor=iid(q.identifier(data.get('Employee')))
    if actor!=before['owner']['InstanceID']:raise ValueError('当前员工不等于原责任人，不允许身份覆盖。')
    actual=data.get('QualityObjectDetail');row=before['event']
    if not isinstance(actual,dict) or not set(u.CURRENT_FIELDS.split(','))<=actual.keys():raise ValueError('原生当前质量上下文缺失。')
    for field in ('Category','Status'):
        if u.i.wire_int(actual[field],field)!=row[field]:raise ValueError('当前质量状态不符。')
    for field in ('Organization','Owner','Role','PriorityLevel'):
        if iid(q.identifier(actual[field]))!=row[field+'Id']:raise ValueError('当前组织/归属/优先级不符。')
    for field,value in [('TriageComplete',row['TriageComplete']),('IsCARRequiredToClose',row['IsCARRequiredToClose']),('IsApprovalRequired',False),('UserHasRole',True),('UserIsOwner',True)]:
        if u.i.wire_bool(actual[field],field) is not value:raise ValueError('当前分流/CAR/审批或员工权限不符。')
    if any(actual[k] is not None for k in ('ProcessModel','CompletionApprovalSheet','EntryForApproval')) or actual['CompletionApprovalStatus'] not in (None,''):raise ValueError('流程/审批上下文不在范围。')
    c.verify_loaded(data,before)
    return actor


def loaded_values(data,before):
    # Reuse the strict original Event scalar/ref/time verification. The service
    # stores those same values at its root rather than inside EventDetail.
    frame={field:copy.deepcopy(data.get(field)) for field in ROOT_FIELDS}
    if not set(ROOT_FIELDS)<=data.keys():raise ValueError('原事件根保持值不完整。')
    frame.update(CDOTypeId=7541,EventDataDetail=None,RegulatoryReportDetails=[])
    root=u.loaded_detail({'ServiceDetail':frame},before)
    raw=data.get('DisallowedTxns')
    if not isinstance(raw,list) or [u.i.wire_int(v,'禁用事务') for v in raw]!=[r['DisallowedTxns'] for r in before['disallowed']]:raise ValueError('Load禁用事务清单与原持久值不符，不清空或派生默认值。')
    preserved={wire(f):root[wire(f)] for f in ROOT_FIELDS};preserved['disallowedTxns']=raw
    dt=data.get('EventDataDetail');original=before['data']
    if not isinstance(dt,dict) or u.i.wire_int(dt.get('CDOTypeId'),'事件扩展明细类型')!=DETAIL_TYPE or not set(DATA_FIELDS)<=dt.keys():raise ValueError('原8087扩展明细不完整/类型不符。')
    if set(dt)-set(DATA_FIELDS)-{'CDOTypeId','InstanceId','InstanceID','Parent','DisplayName','HistoryDetail','SmartBarcode','EventData'}:raise ValueError('Load含未核实扩展明细，不丢弃未知资料后保存。')
    if dt.get('EventData') is not None and iid(q.identifier(dt['EventData']))!=original['EventDataId']:raise ValueError('明细引用了替代EventData。')
    clean={}
    for field,maximum in DATA_TEXT.items():
        value=dt[field]
        if type(value) is not type(original[field]) or value!=original[field]:raise ValueError('原扩展文字保持值与SQL不符，不补默认资料。')
        if value is not None:u.text_value(value,field,maximum)
        clean[wire(field)]=value
    for field in DATA_REFS:
        value=dt[field];key=original[field+'Id']
        if key in (None,''):
            if value is not None:raise ValueError('Load派生原数据没有的职业/补偿引用，不回填。')
            clean[wire(field)]=None
        else:
            if not isinstance(value,dict) or iid(q.identifier(value))!=iid(key):raise ValueError('原扩展引用与SQL不符。')
            clean[wire(field)]=dict(name=q.text(value.get('Name')),instanceId=iid(key))
    for field in DATA_DATES:
        if not u.date_match(original[field],dt[field],field.endswith('GMT')):raise ValueError('扩展原时间与SQL不符，不补当前时间或猜时区。')
        clean[wire(field)]=dt[field]
    for local,gmt in [('DateReceived','DateReceivedGMT'),('ReportDate','ReportDateGMT')]:
        if dt[local] is not None or dt[gmt] is not None:
            if not timestamp_valid(dt[local]) or not timestamp_valid(dt[gmt]) or timestamp_ticks(dt[local])!=timestamp_ticks(dt[gmt]):raise ValueError('扩展本地/GMT保持值不是同一时刻。')
    return dict(root=preserved,data=clean)


async def load(before,marker,bound):
    body=root_body(before,marker)
    raw=await request('POST','/api/'+SERVICE+'/PerformEvent',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body={'event':{'eventName':'Load'},'inputData':body},params={'$select':SELECT,'$expand':EXPAND},bound_headers=bound,require_stable_session=True,response_formatter=u.raw_json)
    data=u.parse_reply(raw);actor=verify_context(data,before,body)
    return dict(values=loaded_values(data,before),employee_id=actor,current={k:copy.deepcopy(data['QualityObjectDetail'][k]) for k in u.CURRENT_FIELDS.split(',')})


def body_for(before,original,values,marker,comments,date_storage=None):
    body=root_body(before,marker,comments);body.update(copy.deepcopy(original['root']));dt=copy.deepcopy(original['data'])
    for key,value in values.items():
        if key in TEXT:dt[wire(TEXT[key][0])]=value
    patch,_=dates.apply(original['data'],values,date_storage);dt.update(patch)
    if dt==original['data']:raise DataScopeError('原扩展资料与明确变更相同，无需保存。')
    body['eventDataDetail']=dt
    return body


async def _build(args,marker,execution_binding=None):
    values=changes(args);u.text_value(args['comments'],'扩展资料说明',200,True)
    with verified_channel():
        before=await source(args);schema=await contract();date_storage=await dates.storage() if dates.INPUTS.keys()&values.keys() else None
        bound=auth.capture_headers(SERVICE);original=await load(before,marker,bound)
        proof=await auth.membership(original['employee_id'],before['organization'],before['role'])
        body=body_for(before,original['values'],values,marker,args['comments'],date_storage)
        if await source(args)!=before or await load(before,marker,bound)!=original or await auth.membership(original['employee_id'],before['organization'],before['role'])!=proof:
            raise ValueError('原事件/扩展资料、分类/禁用清单、引用或当前员工授权变化。')
        if date_storage is not None and await dates.storage()!=date_storage:raise dates.DatePolicyError('日期列类型或精度在准备期间变化，停止准备。')
        _,date_changes=dates.apply(original['values']['data'],values,date_storage)
        changed=[k for k in values if (k in TEXT and body['eventDataDetail'][wire(TEXT[k][0])]!=original['values']['data'][wire(TEXT[k][0])])
            or k in date_changes and not date_changes[k]['same_instant_preserved']]
        reads=set()
        for key in ('organization','owner','role','priority'):reads.update(resources(before[key]['entity'],before[key]))
        reads.update(c.read_resources(before['classification_configuration']))
        if date_storage is not None:reads.add('schema:dbo:EventData:dates')
        if execution_binding is not None:execution_binding.update(headers=bound,actor_id=original['employee_id'])
        row=before['event'];data_id=before['data']['EventDataId']
        return dict(kind='event_data_update',action='update_data',service=SERVICE,name=row['EventName'],target_id=row['EventId'],scope_key='quality-event:id:'+row['EventId'],
            before=before,original_values=original['values'],actor_authorization=proof,date_storage=date_storage,body=body,arguments=args,marker=marker,environment=p.environment(),
            resource_reads=sorted(reads),resource_writes=['runtime:quality-event:id:'+row['EventId'],'runtime:event-data:id:'+data_id],
            preview=dict(native_contract=schema,employee_id=original['employee_id'],static_preview_only=True,patched_request_echo_verified=False,
                ordinary_preview_requested=False,dynamic_validation_verified=False,event_data_updated_verified=False,
                changed_fields=sorted(changed),date_changes=date_changes,date_policy=dates.policy(),date_storage_checked=date_storage is not None,custom_fields_supported=False,
                note='仅原标准EventData文字/明确偏移量日期静态计划；日期成对保持同一时刻并核实实际存储精度，不推断MES时区。原生PreValidate强制内部执行，不调用普通POST假预览。保持原Event及未改扩展值/禁用清单；实际时间/历史待验收，不提交/分流/关闭/审批或改分类。'))


async def build(args,marker,execution_binding=None):
    try:return await _build(args,marker,execution_binding)
    except (DataScopeError,dates.DatePolicyError):raise
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
        # Remote read errors may reflect Authorization/Cookie. Only fixed scope
        # messages may pass through; no free-form remote exception text.
        raise ValueError('原事件/扩展资料、固定Load契约、保持值或当前员工授权未核实；不保存可能反射会话的异常正文，不新增提交。') from None


def validate_transport(snap):
    before=snap['before'];row=before['event']
    values=changes(snap['arguments'])
    if (snap.get('kind')!='event_data_update' or snap.get('service')!=SERVICE or snap.get('action') not in ('update_data','update_data_text')
        or snap.get('action')=='update_data_text' and dates.INPUTS.keys()&values.keys()
        or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker','')) or snap.get('target_id')!=row['EventId'] or snap.get('name')!=row['EventName']
        or row['Status']!=1 or row['Category']!=1 or row['CDOTypeId']!=7489 or row['IsSubmitted'] is not True or row['IsFrozen'] is not False
        or before['data']['CDOTypeId']!=DATA_TYPE or before['data']['EventId']!=row['EventId'] or before['data']['EventDataId']!=row['EventDataId']):raise ValueError('扩展资料服务、原Event/EventData身份或状态不符。')
    expected=body_for(before,snap['original_values'],values,snap['marker'],snap['arguments']['comments'],snap.get('date_storage'))
    if snap['body']!=expected:raise ValueError('扩展资料载荷或原资料保持值不符。')


async def date_evidence(snap,current):
    requested={k:v for k,v in changes(snap['arguments']).items() if k in dates.INPUTS}
    if not requested:return None
    unavailable=dict(complete=False,causal_success_verified=False,retry_allowed=False,
        note='日期同值/同一时刻亦不证明本次保存；没有最终人工事务证据不得解锁或重试。')
    original_id=snap['before']['data']['EventDataId']
    if not current or current['EventDataId']!=original_id:return unavailable|dict(reason='original_data_link_changed')
    try:
        before=await source(snap['arguments'],expected_data_id=original_id);await contract()
        loaded=await load(before,snap['marker'],auth.capture_headers(SERVICE))
        if await source(snap['arguments'],expected_data_id=original_id)!=before:return unavailable|dict(reason='changed_during_read')
        actual=loaded['values']['data'];expected=snap['body']['eventDataDetail'];fields={}
        for key in requested:
            local,gmt=dates.INPUTS[key];local,gmt=wire(local),wire(gmt)
            local_value,gmt_value=actual[local],actual[gmt];planned=expected[gmt]
            matches=all(timestamp_valid(v) for v in (local_value,gmt_value,planned)) and timestamp_ticks(local_value)==timestamp_ticks(gmt_value)==timestamp_ticks(planned)
            fields[key]=dict(observed_local=local_value,observed_gmt=gmt_value,planned_gmt=planned,same_instant=matches)
        return unavailable|dict(complete=True,native_and_sql_original_values_consistent=True,fields=fields,
            all_planned_instants_match=all(f['same_instant'] for f in fields.values()),data_change_count=before['data']['ChangeCount'],
            observed_sql_dates={f:before['data'][f] for f in DATA_DATES},mes_timezone_inferred=False)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):return unavailable|dict(reason='current_read_context_unavailable')


async def _inspect(snap):
    with verified_channel():
        current=await u.event_record(instance_id=snap['target_id'])
        data_id=snap['before']['data']['EventDataId'];rows=await q.sql_rows('SELECT TOP 1 '+DATA_COLUMNS+f" FROM EventData WHERE EventDataId='{data_id}'",1)
        if len(rows)>1 or rows and (not set(DATA_COLUMNS.split(','))<=rows[0].keys() or iid(rows[0].get('EventDataId'))!=data_id or iid(rows[0].get('EventId'))!=snap['target_id']):raise ValueError('原扩展资料当前归属/完整性不符。')
        cols='UpdateEventDetailsHistoryId,CDOTypeId,HistoryId,HistoryMainlineId,TxnId,ChangeCount'
        await schema_check({'UpdateEventDetailsHistory':cols,'HistoryMainline':'HistoryMainlineId,HistoryId,TxnId,TxnType,Comments,ReversalStatus'})
        history=await q.sql_rows('SELECT TOP 21 s.'+',s.'.join(cols.split(','))+',h.TxnType,h.Comments,h.ReversalStatus FROM UpdateEventDetailsHistory s JOIN HistoryMainline h '+
            'ON h.HistoryMainlineId=s.HistoryMainlineId AND h.HistoryId=s.HistoryId AND h.TxnId=s.TxnId '+
            f"WHERE s.CDOTypeId=8752 AND s.HistoryId='{snap['target_id']}' AND h.Comments="+q.literal(snap['body']['comments'])+' ORDER BY s.UpdateEventDetailsHistoryId',21)
        if len(history)>20:raise ValueError('扩展资料候选历史超过20条，不返回部分证据。')
        seen=set();clean=[]
        for raw in history:
            if not isinstance(raw,dict) or not set((cols+',TxnType,Comments,ReversalStatus').split(','))<=raw.keys():raise ValueError('扩展资料历史字段缺失。')
            item={k:copy.deepcopy(raw[k]) for k in (cols+',TxnType,Comments,ReversalStatus').split(',')}
            for k in ('UpdateEventDetailsHistoryId','HistoryId','HistoryMainlineId','TxnId'):item[k]=iid(item[k])
            for k in ('CDOTypeId','TxnType','ChangeCount','ReversalStatus'):item[k]=u.i.wire_int(item[k],k)
            if item['CDOTypeId']!=8752 or item['TxnType']!=SERVICE_TYPE or item['HistoryId']!=snap['target_id'] or item['Comments']!=snap['body']['comments'] or item['UpdateEventDetailsHistoryId'] in seen:
                raise ValueError('候选历史不属于原对象/本次标记/原生数据事务，或清单重复。')
            seen.add(item['UpdateEventDetailsHistoryId']);clean.append(item)
        return dict(original_event_id=snap['target_id'],original_event_data_id=data_id,current_event=current,current_original_event_data=rows[0] if rows else None,
            history_candidates=clean,date_evidence=await date_evidence(snap,current),verified_success=False,state_changed=False,retry_allowed=False,
            note='仅原Event/原EventData及有限原生历史证据；同值/HTTP200不归因本次成功，不跟随替代数据。unknown须最终人工审计，不自动解锁或重试。')


async def inspect(snap):
    try:return await _inspect(snap)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise ValueError('原事件/扩展资料或有界历史证据不完整；未确认成功，不回显可能反射会话的异常正文。') from None


@mcp.tool
async def get_quality_event_data_context(event_name:str,organization_name:str,event_instance_id:str|None=None,include_date_storage:bool=False)->str:
    """只读已有标准EventData扩展资料及受限维护条件。仅已提交Active Category1基础Event、已有8085 EventData，无流程/审批/检查表/NCR/法规报告/客户/自定义数据或批次失败日志等复杂明细。
    固定UpdateEventData Load保持完整原Event与28项扩展映射值/禁用清单，核对当前员工Edit/责任人角色及签名/采集要求。原生PreValidate强制内部执行，禁止普通POST假预览；此入口无计划、无业务保存。
    支持明确问题描述(255 UTF-16字符)/设备位置(30)及扩展收到日期date_received、报告日期report_date。日期核验可include_date_storage=true读取四列实际类型/精度；默认不增加日期元数据依赖，不授权写入。日期须完整明确偏移量、最多7位小数；datetime只接收整秒，datetime2拒绝超精度输入。
    收到/报告日期是EventData日期，不改Event发生/报告时间、不猜MES时区或两种日期先后顺序。任意现场EventCustomData字段无标准业务结构，不自动发现成可写、不接通用字典。不授权签名、改分类、创建替代数据、提交或关闭事件。
    """
    try:
        if type(include_date_storage) is not bool:raise DataScopeError('include_date_storage须为明确布尔值，只控制只读元数据查询。')
        with verified_channel():
            args=dict(event_name=event_name,organization_name=organization_name,event_instance_id=event_instance_id)
            before=await source(args);schema=await contract();date_storage=await dates.storage() if include_date_storage else None
            bound=auth.capture_headers(SERVICE);loaded=await load(before,'MCP-QUAL:'+'0'*32,bound)
            if await source(args)!=before:raise ValueError('读取期间原Event/扩展资料或配置变化。')
            if date_storage is not None and await dates.storage()!=date_storage:raise dates.DatePolicyError('读取期间日期列类型或精度变化。')
            return q.encode(dict(complete=True,before=before,original_values=loaded['values'],native_contract=schema,current_context=loaded['current'],
                date_policy=dates.policy(),date_storage=date_storage,date_storage_checked=date_storage is not None,
                ready_for_execution=False,event_data_updated_verified=False,custom_fields_supported=False,ordinary_preview_requested=False))
    except (DataScopeError,dates.DatePolicyError) as error:return q.encode(dict(complete=False,ready_for_execution=False,custom_fields_supported=False,error=str(error)))
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):return q.encode(dict(complete=False,ready_for_execution=False,custom_fields_supported=False,
        error='扩展资料读取/原值/契约或受限条件未核实；不调用普通预览或保存，不猜自定义字段。'))


@mcp.tool
async def prepare_quality_event_data_update(event_name:str,organization_name:str,changes:EventDataChanges,comments:str,event_instance_id:str|None=None)->str:
    """准备已有标准EventData文字和日期维护，至少一项明确变化；changes仅problem_description(255)/device_location(30 UTF-16字符)、date_received收到日期、report_date报告日期。省略/null保持，文字空串可清除，日期空串/清除拒绝。先get_quality_event_data_context核对。
    日期须完整含明确偏移量、最多7位小数；UTC由明确输入推导，不补当前时间或MES时区，不改Event发生/报告时间、不臆定收到/报告日期先后。准备自动核对四列实际SQL类型/精度；datetime仅整秒，datetime2必须精确可表示，不截断/舍入。同一时刻仅格式或偏移量变化保留原日期对，不算日期修改。
    仅已提交Active Category1、原8085数据且无复杂关联，当前责任人/原生角色授权、Edit、同次内存会话核实，验证证书HTTPS；签名/采集要求拒绝。保持原Event映射、分类/归属、28项数据映射及禁用事务，不新建或替换数据。
    准备只固定Load/本地静态差异，无普通execute=false POST（PreValidate会强制内部执行）。系统确认后execute_quality_transaction单次UpdateEventData实际保存；unknown不重试，inspect仅原ID证据，最终人工证据review。不执行提交/分流/关闭或任意EventCustomData/客户/批次/失败明细。
    """
    try:values=EventDataChanges.model_validate(changes).model_dump()
    except (ValueError,TypeError):return q.encode(dict(submitted=False,error='扩展资料变更只允许问题描述/设备位置及明确收到/报告日期的封闭结构；不接收身份、口令或任意自定义字段。'))
    return await p.prepare('event_data_update',dict(event_name=event_name,organization_name=organization_name,event_instance_id=event_instance_id,changes=values,comments=comments))
