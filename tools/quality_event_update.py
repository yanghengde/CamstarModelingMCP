"""Preserve a loaded EventDetail while updating explicitly selected text fields."""
import copy
import json
import re
from typing import Literal

from tools import mcp, quality as q, quality_plans as p, quality_inspection as i, quality_event_triage as t
from tools.quality_spc_annotations import iid, schema_check
from tools.quality_definitions import resources
from tools.collection_validation import timestamp_valid, timestamp_ticks
from tools.modeling_details import raw_json, _has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SERVICE_TYPE=7578
DETAIL_TYPE=7541
EDIT_ACTION=2
EXTRA=('BriefDescription,Description,DiscoveryArea,EventDataId,OccurrenceDate,OccurrenceDateGMT,ReportedDate,ReportedDateGMT,'
       'InitiatorId,InitiatorOrganizationId,ReporterId,ReporterOrganizationId')
SCHEMA={k:v for k,v in t.SCHEMA.items() if k not in ('CategoryMap','TriageSpec','TriageSpecDetail')}
SCHEMA['Event']=t.EVENT_COLUMNS+','+EXTRA
SCHEMA['RegulatoryReport']='RegulatoryReportId,ParentId'
TEXT_FIELDS={'brief_description':('briefDescription',100),'description':('description',4000),'discovery_area':('discoveryArea',30)}
DATE_FIELDS=('OccurrenceDate','OccurrenceDateGMT','ReportedDate','ReportedDateGMT')
REF_FIELDS=('Initiator','InitiatorOrganization','Reporter','ReporterOrganization','PriorityLevel')
DETAIL_FIELDS=('BriefDescription','Description','DiscoveryArea','EventDataDetail',*REF_FIELDS,*DATE_FIELDS,'RegulatoryReportDetails')
DETAIL_SYSTEM_FIELDS={'displayName','historyDetail','instanceID','parent','smartBarcode'}
ARRAYS=('AssignedLots','QualityCrossRefDetails','QualityCrossReferences')
CURRENT_FIELDS=('Category,Status,Organization,Owner,Role,PriorityLevel,ProcessModel,TriageComplete,IsCARRequiredToClose,'
                'IsApprovalRequired,CompletionApprovalSheet,CompletionApprovalStatus,EntryForApproval,UserHasRole,UserIsOwner')
SELECT=('QualityObject,Organization,Submit,Comments,AvailableActions,IgnoreProcessModel,ESigRequirement,AdditionalESigReqs,'
        'ApprovalSheetDetails,ApprovalSheetTemplate,AssignedLots,QualityCrossRefDetails,QualityCrossReferences,'
        'ServiceDetail,QualityObjectDetail')
EXPAND=('ServiceDetail($select=CDOTypeId,'+','.join(DETAIL_FIELDS)+';$expand=RegulatoryReportDetails),'
        'QualityObjectDetail($select='+CURRENT_FIELDS+'),AssignedLots,QualityCrossRefDetails,QualityCrossReferences')


def text_value(value,label,maximum,required=False):
    if not isinstance(value,str) or '\x00' in value or len(value.encode('utf-16-le'))//2>maximum or required and not value.strip():
        raise ValueError(f'{label}须为最多{maximum}个UTF-16字符的'+('非空' if required else '')+'文本，不截断或补默认值。')
    return value


async def event_record(name='',instance_id=None):
    row=await t.event_record(name,instance_id)
    if row is None:return None
    extra=await q.sql_rows('SELECT TOP 1 EventId,ChangeCount,'+EXTRA+f" FROM Event WHERE EventId='{row['EventId']}'",1)
    if (len(extra)!=1 or iid(extra[0].get('EventId'))!=row['EventId']
            or i.wire_int(extra[0].get('ChangeCount'),'事件资料版本')!=row['ChangeCount']
            or not set(EXTRA.split(','))<=extra[0].keys()):
        raise ValueError('事件文字资料读取不完整或版本已变化。')
    result=row|{k:copy.deepcopy(extra[0][k]) for k in EXTRA.split(',')}
    for key in ('EventDataId','InitiatorId','InitiatorOrganizationId','ReporterId','ReporterOrganizationId'):
        if result[key] not in (None,''):result[key]=iid(result[key],key)
    return result


async def context(args,signature_txn_type=SERVICE_TYPE):
    from tools import quality_approval_authorization as authorization
    authorization.role_opt_in(args)
    q.text(args['event_name'],'事件名称');q.text(args['organization_name'],'组织名称')
    if args.get('event_instance_id') is not None:iid(args['event_instance_id'])
    await schema_check(SCHEMA)
    row=await event_record(args['event_name'],args.get('event_instance_id'))
    revision=args.get('approval_sheet_id') is not None or args.get('previous_cycle_gmt') is not None
    if (not row or row['CDOTypeId']!=t.EVENT_TYPE or row['Status'] not in ((1,8) if revision else (1,)) or row['ChangeCount']<0
            or row['Category']<1 or row['IsFrozen'] or not row['IsSubmitted']):
        raise ValueError('文字更新仅支持已提交Active、未冻结的基础Event，不重开/关闭或提交草稿。')
    if (any(row[k] not in (None,'') for k in ('ProcessModelId','OrigProcessModelTemplateId','ChecklistId','CloseDateGMT','EventDataId'))
            or row['ChecklistSaved']):
        raise ValueError('事件已有流程、检查表、自定义事件数据或关闭信息，受限文字更新不覆盖这些内容。')
    for field in ('OwnerId','RoleId','PriorityLevelId','ReporterId'):iid(row[field],field)
    approval_revision=None
    if revision:
        from tools import quality_approvals as a,quality_approval_history as h
        if args.get('approval_sheet_id') is None or args.get('previous_cycle_gmt') is None:raise ValueError('审批资料修订须同时明确approval_sheet_id和previous_cycle_gmt。')
        approval=await a.read_context('event',args['event_name'],args['organization_name'],row['EventId'])
        if approval['target']['ChangeCount']!=row['ChangeCount'] or approval['target']['Status']!=row['Status']:raise ValueError('资料修订的原对象版本/状态不符。')
        if len(approval['sheets'])!=1 or approval['sheets'][0]['ApprovalSheetId']!=iid(args['approval_sheet_id']):raise ValueError('受限资料修订仅支持原对象唯一的明确审批表。')
        sheet=approval['sheets'][0]
        if sheet['IsFrozen'] or sheet['Status'] not in (0,30) or approval['current_employee_entry_id'] is not None:raise ValueError('只允许拒绝/取消后且无待签条目的资料修订，不修改路由中的资料。')
        for entry in sheet['entries']:
            authorization.entry_policy(entry,args)
            if entry['IsFrozen']:raise ValueError('不处理冻结条目的审批资料修订。')
        proof=await h.previous_cycle_evidence(approval,sheet,args['previous_cycle_gmt'])
        approval_revision=dict(context=approval,previous_cycle=proof)
    elif await q.sql_rows(f"SELECT TOP 1 ApprovalSheetId FROM ApprovalSheet WHERE ParentId='{row['EventId']}'",1):
        raise ValueError('事件已有直接审批表，须明确旧周期进入受限审批资料修订。')
    if await q.sql_rows(f"SELECT TOP 1 RegulatoryReportId FROM RegulatoryReport WHERE ParentId='{row['EventId']}'",1):
        raise ValueError('事件已有法规报告，不以空明细覆盖。')
    linked=await q.sql_rows('SELECT TOP 21 NonconformanceReportId,EscalatedEventId,ContainerId,ChangeCount '
        f"FROM NonconformanceReport WHERE EscalatedEventId='{row['EventId']}' ORDER BY NonconformanceReportId",21)
    if len(linked)>20:raise ValueError('关联NCR超过20条，不能使用部分关系声明占用。')
    links=[]
    for raw in linked:
        item={k:iid(raw.get(k),k) for k in ('NonconformanceReportId','EscalatedEventId','ContainerId')}
        item['ChangeCount']=i.wire_int(raw.get('ChangeCount'),'关联NCR版本')
        if item['EscalatedEventId']!=row['EventId'] or item['ChangeCount']<0:raise ValueError('关联NCR身份/版本不一致。')
        links.append(item)
    if len({x['NonconformanceReportId'] for x in links})!=len(links):raise ValueError('关联NCR重复。')
    org=await t.asset('organization',name=args['organization_name'],key=row['OrganizationId'],signature_txn_type=signature_txn_type)
    owner=await t.asset('owner',key=row['OwnerId']);role=await t.asset('role',key=row['RoleId'])
    priority=await t.asset('priority',key=row['PriorityLevelId'])
    result={'event':row,'organization':org,'owner':owner,'role':role,'priority':priority,'linked_ncrs':links}
    if approval_revision is not None:result['approval_revision']=approval_revision
    return result


async def contract():
    raw=await request('GET','/swagger/UpdateEvent/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json)
    try:
        doc=json.loads(raw);schemas=doc['components']['schemas']
        post=doc['paths']['/api/UpdateEvent/PerformEvent']['post']['requestBody']['content']['application/json']['schema']
        model=schemas[post['$ref'].split('/')[-1]]['properties']
        event=schemas[model['event']['$ref'].split('/')[-1]]['properties']
        root_name=model['inputData']['$ref'].split('/')[-1];root=schemas[root_name]['properties']
        detail=schemas[root['serviceDetail']['$ref'].split('/')[-1]]['properties']
        if root_name!='UpdateEvent' or event['eventName']['type']!='string' or root['submit']['type']!='boolean':
            raise ValueError('字段类型不匹配')
        expected={k[0].lower()+k[1:] for k in DETAIL_FIELDS}
        if not expected<=set(detail) or set(detail)-expected-DETAIL_SYSTEM_FIELDS:
            raise ValueError('事件明细字段不完整或存在尚未支持的扩展字段')
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        raise ValueError('当前UpdateEvent/PerformEvent的Load输入契约未核实，不猜测通用事件参数。') from exc
    return {'contract_digest':p.digest({'model':model,'event':event,'root':root,'detail':detail}),
            'load_event':'Load','schema_checked':True,'source':'Shopfloor/swagger/UpdateEvent/swagger.json'}


def root_body(before,marker,comments=''):
    row=before['event']
    text_value(comments,'更新备注',200)
    return {'qualityObject':{'name':row['EventName'],'instanceId':row['EventId']},'organization':t.ref(before['organization']),
            'submit':False,'comments':marker+(' '+comments if comments else '')}


def parse_reply(raw):
    try:outer=json.loads(raw)
    except (ValueError,TypeError):raise ValueError('事件更新原生读取/预览未返回有效JSON。') from None
    data=t.canonical_refs(outer.get('data',outer)) if isinstance(outer,dict) else None
    if (not isinstance(data,dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error') or data.get('ErrorCode') or _has_next_link(outer)):
        raise ValueError('事件更新原生读取/预览失败或内容不完整。')
    return data


def validate_common(data,body,before,loaded=False):
    if not p.wire_matches(body,data):raise ValueError('事件、组织、submit=false、备注或明确变更未完整回显。')
    mask=i.wire_int(data.get('AvailableActions'),'事件可用动作')
    if mask<0 or mask&EDIT_ACTION!=EDIT_ACTION:raise ValueError('当前账号/事件没有原生Edit动作，不覆盖动作位。')
    if i.wire_bool(data.get('IgnoreProcessModel'),'流程绕过标志') is not False:raise ValueError('流程绕过标志未知或不为false。')
    revision=before.get('approval_revision')
    for key in ('ESigRequirement','ApprovalSheetTemplate'):
        if key not in data or data[key] is not None:raise ValueError('签名、审批或检查表处理非空/未知，不提交文字更新。')
    if revision and loaded:
        # Load may read approval configuration. Never forward it to Execute/SaveApprovalSheet.
        from tools import quality_approvals as a
        sheet=revision['context']['sheets'][0];approval=data.get('ApprovalSheetDetails')
        if (not isinstance(approval,dict) or iid(q.identifier(approval.get('ApprovalSheet')))!=sheet['ApprovalSheetId']
            or a.integer(approval.get('Status'),'已加载审批状态')!=sheet['Status']):raise ValueError('Load审批表身份/状态与原表不符。')
        entries=approval.get('ApprovalEntries')
        if not isinstance(entries,list) or len(entries)!=len(sheet['entries']):raise ValueError('Load审批条目不完整。')
        ids=[iid(q.identifier(e.get('ApprovalEntry'))) for e in entries if isinstance(e,dict)]
        if len(ids)!=len(entries) or len(set(ids))!=len(ids) or set(ids)!={e['ApprovalSheetEntryId'] for e in sheet['entries']}:raise ValueError('Load审批条目身份/归属不符。')
    elif 'ApprovalSheetDetails' not in data or data['ApprovalSheetDetails'] is not None:
        raise ValueError('执行预览包含审批资料写入，不以文字修订覆盖审批配置。')
    if 'AdditionalESigReqs' not in data or data['AdditionalESigReqs'] not in (None,[]):raise ValueError('额外签名要求非空或未知。')
    for key in ARRAYS:
        if key not in data or data[key]!=[]:raise ValueError('批次/交叉引用明细非空或未知，不以文字更新改关联。')
    detail=data.get('QualityObjectDetail');row=before['event']
    if not isinstance(detail,dict) or not set(CURRENT_FIELDS.split(','))<=detail.keys():raise ValueError('事件当前状态/审批上下文不完整。')
    for field in ('Status','Category'):
        if i.wire_int(detail[field],field)!=row[field]:raise ValueError('事件当前状态/类别不符。')
    for field,col in [('Organization','OrganizationId'),('Owner','OwnerId'),('Role','RoleId'),('PriorityLevel','PriorityLevelId')]:
        if q.identifier(detail[field])!=row[col]:raise ValueError('事件当前组织/归属/优先级不符。')
    for field,expected in [('TriageComplete',row['TriageComplete']),('IsCARRequiredToClose',row['IsCARRequiredToClose']),
                           ('IsApprovalRequired',revision['context']['current_context']['IsApprovalRequired'] if revision else False),('UserHasRole',True),('UserIsOwner',True)]:
        if i.wire_bool(detail[field],field) is not expected:raise ValueError('事件当前分流/CAR/审批/账号状态不满足受限要求。')
    if revision:
        from tools import quality_approvals as a
        expected=revision['context']['current_context']
        for field in ('CompletionApprovalSheet','EntryForApproval'):
            actual=a.optional(q.identifier(detail[field])) if detail[field] is not None else None
            original=None if expected[field] is None else expected[field]['instance_id']
            if actual!=original:raise ValueError('资料修订预览的原审批入口变化。')
        left=detail['CompletionApprovalStatus'];right=expected['CompletionApprovalStatus']
        if (None if left in (None,'') else a.integer(left,'审批状态'))!=(None if right in (None,'') else a.integer(right,'原审批状态')):
            raise ValueError('资料修订预览的审批状态变化。')
        if detail['ProcessModel'] is not None:raise ValueError('资料修订不处理嵌套流程。')
    elif any(detail[k] is not None for k in ('ProcessModel','CompletionApprovalSheet','EntryForApproval')) or detail['CompletionApprovalStatus'] not in (None,''):
        raise ValueError('事件已有流程/审批上下文，不覆盖。')
    return {'available_actions_raw':mask,'edit_action_mask':EDIT_ACTION,
            'current_quality_context':{k:copy.deepcopy(detail[k]) for k in CURRENT_FIELDS.split(',')}}


def date_match(source,actual,gmt):
    if source in (None,''):return actual is None
    if not isinstance(source,str) or not timestamp_valid(actual):return False
    raw=source.replace(' ','T',1)
    if gmt:
        if not timestamp_valid(raw):raw+='Z'
        return timestamp_valid(raw) and timestamp_ticks(raw)==timestamp_ticks(actual)
    pattern=r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?(?:Z|[+-]\d{2}:\d{2})?'
    left=re.fullmatch(pattern,raw);right=re.fullmatch(pattern,actual)
    return bool(left and right and left[1]==right[1] and (left[2] or '').ljust(7,'0')==(right[2] or '').ljust(7,'0'))


def loaded_detail(data,before):
    detail=data.get('ServiceDetail');row=before['event']
    if (not isinstance(detail,dict) or not set(DETAIL_FIELDS)<=detail.keys()
            or i.wire_int(detail.get('CDOTypeId'),'事件明细类型')!=DETAIL_TYPE):raise ValueError('原生Load没有完整返回7541事件明细。')
    for field in ('BriefDescription','Description','DiscoveryArea'):
        if type(detail[field]) is not type(row[field]) or detail[field]!=row[field]:raise ValueError('Load的原始文字资料与持久记录不一致。')
    for field in REF_FIELDS:
        expected=row[field+'Id'];value=detail[field]
        if expected in (None,''):
            if value is not None:raise ValueError('Load派生了原记录没有的报告人/发现人等默认引用，不补写。')
        elif not isinstance(value,dict) or q.identifier(value)!=expected or not isinstance(value.get('Name'),str) or not value['Name'].strip():
            raise ValueError('Load的报告人/发现人/组织/优先级引用与原始记录不一致。')
    for field in DATE_FIELDS:
        if not date_match(row[field],detail[field],field.endswith('GMT')):raise ValueError('Load的发生/报告时间与原始记录不一致，不猜测本地时区。')
    for local,gmt in [('OccurrenceDate','OccurrenceDateGMT'),('ReportedDate','ReportedDateGMT')]:
        if detail[local] is not None or detail[gmt] is not None:
            if not timestamp_valid(detail[local]) or not timestamp_valid(detail[gmt]) or timestamp_ticks(detail[local])!=timestamp_ticks(detail[gmt]):
                raise ValueError('Load的本地时间与GMT不是同一时刻，不能原样回填。')
    if detail['EventDataDetail'] is not None or detail['RegulatoryReportDetails']!=[]:
        raise ValueError('原生Load派生了事件数据/法规报告，不在文字更新范围。')
    # Only declared business values survive; discard service, parent and history IDs.
    clean={field[0].lower()+field[1:]:copy.deepcopy(detail[field]) for field in DETAIL_FIELDS}
    for field in REF_FIELDS:
        key=field[0].lower()+field[1:]
        if clean[key] is not None:clean[key]={'name':detail[field]['Name'],'instanceId':q.identifier(detail[field])}
    return clean


async def load(before,marker):
    body=root_body(before,marker)
    raw=await request('POST','/api/UpdateEvent/PerformEvent',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body={'event':{'eventName':'Load'},'inputData':body},params={'$select':SELECT+(',Classification,SubClassification' if 'classifications' in before else ''),'$expand':EXPAND},response_formatter=raw_json)
    data=parse_reply(raw);current=validate_common(data,body,before,loaded=True)
    if 'classifications' in before:
        from tools.quality_event_classification import verify_loaded
        verify_loaded(data,before)
    return {'detail':loaded_detail(data,before),'current':current}


async def build_update(args,marker):
    changes={arg:value for arg,value in args.items() if arg in TEXT_FIELDS and value is not None}
    if not changes:raise ValueError('至少明确一个文字字段；省略/null保留原值，不自动清空。')
    for arg,value in changes.items():text_value(value,arg,TEXT_FIELDS[arg][1],arg=='brief_description')
    text_value(args.get('comments',''),'更新备注',200)
    before=await context(args);verified=await contract();original=await load(before,marker)
    detail=copy.deepcopy(original['detail'])
    for arg,value in changes.items():detail[TEXT_FIELDS[arg][0]]=value
    if detail==original['detail']:raise ValueError('明确字段与原值相同，没有资料变更。')
    body=root_body(before,marker,args.get('comments',''));body['serviceDetail']=detail
    # Standard inherited CreateEvent_PreValidate forces Transaction::IsExecute
    # for every Submit=false request, including edits of submitted Events. A
    # regular execute=false POST is therefore unsafe as a preview here too.
    current=original['current']
    # A final independent Load must still return the original detail, not the patch.
    if await context(args)!=before or await load(before,marker)!=original:raise ValueError('准备期间原事件、权限/签名、关联或原明细发生变化。')
    row=before['event'];reads=set()
    for key in ('organization','owner','role','priority'):reads.update(resources(before[key]['entity'],before[key]))
    return {'kind':'event_update','action':'update_text','service':'UpdateEvent','name':row['EventName'],'target_id':row['EventId'],
        'scope_key':'quality-event:id:'+row['EventId'],'before':before,'original_detail':original['detail'],'body':body,
        'preview':{'native_load':verified,**current,'dynamic_validation_verified':False,'static_preview_only':True,
                   'patched_request_echo_verified':False,'regular_nonexecuting_post_requested':False,
                   'event_updated_verified':False,'note':'固定Load与本地静态文字差异，不调用Submit=false普通POST假预览（标准会强制内部执行并禁用字段校验）。完整保持其它资料，不重新提交或改归属/类别/流程/审批；确认后才一次实际保存。'},
        'resource_reads':sorted(reads),'resource_writes':sorted({'runtime:quality-event:id:'+row['EventId']}
            |({'runtime:approval-sheet:id:'+args['approval_sheet_id'].lower()} if before.get('approval_revision') else set())
            |{'runtime:container:id:'+r['ContainerId'] for r in before['linked_ncrs']}),
        'marker':marker,'arguments':args,'environment':p.environment()}


def validate_transport(snap):
    if (snap.get('kind')!='event_update' or snap.get('action')!='update_text' or snap.get('service')!='UpdateEvent'
            or not isinstance(snap.get('marker'),str) or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap['marker'])
            or snap.get('target_id')!=snap['before']['event']['EventId'] or snap.get('name')!=snap['before']['event']['EventName']):
        raise ValueError('事件文字更新服务、原事件或关联标记不符。')
    expected=root_body(snap['before'],snap['marker'],snap['arguments'].get('comments',''))
    detail=copy.deepcopy(snap['original_detail']);changed=False
    for arg,(field,maximum) in TEXT_FIELDS.items():
        value=snap['arguments'].get(arg)
        if value is not None:
            text_value(value,arg,maximum,arg=='brief_description');changed=changed or value!=detail[field];detail[field]=value
    expected['serviceDetail']=detail
    if not changed or type(snap['body'].get('submit')) is not bool or snap['body']!=expected:
        raise ValueError('事件文字更新字段、原明细保持值或submit=false不符。')


async def inspect_snapshot(snap):
    current=await event_record(instance_id=snap['target_id']);history=None
    if current:history=json.loads(await q.get_quality_history('quality_object',current['EventName'],snap['target_id'],limit=20))
    return {'original_event_id':snap['target_id'],'current_event':current,'bounded_quality_history':history,
            'verified_success':False,'state_changed':False,'retry_allowed':False,'request_correlation_verified':False,
            'note':'当前文字相同/HTTP200不证明本次提交；仅原ID证据，不跟随同名替代事件、不解锁或重试。'}


@mcp.tool
async def get_quality_event_update_context(event_name:str,organization_name:str,event_instance_id:str|None=None,
        approval_sheet_id:str|None=None,previous_cycle_gmt:str|None=None,allow_role_substitution:bool=False,
        purpose:Literal['update_text','submit_pending','edit_pending_draft','classify_pending_draft']='update_text',
        classification_name:str|None=None,subclassification_name:str|None=None)->str:
    """只读检查基础Event文字更新前置，核对当前UpdateEvent/PerformEvent契约并调用固定Load事件读取原明细。
    限已提交Active、无流程/检查表/审批/自定义数据/法规报告/批次/交叉引用，当前账号有Edit动作且为责任人并有角色。
    显示原资料、报告人/日期及保持值；不保存计划、不提交业务、不认证审批或签名，不允许传任意事件名。
    purpose='submit_pending'只读已有Pending未提交草稿、原Load保持值及自动分流/通知配置，不授权通知或正式提交；实际用prepare_quality_event_submission。不能与审批文字修订参数混用。
    purpose='edit_pending_draft'只读Pending草稿资料与缺项；原值可以不完整，不补默认值，当前责任人/角色与会话须核实。保存准备使用prepare_quality_event_draft_update，无普通POST假预览。
    purpose='classify_pending_draft'只读原分类/子分类、组织完整映射及组合阻塞；可同时明确classification_name/subclassification_name比较拟选组合，不保存或生成修改计划。标准UpdateEvent这两字段只读且无保存映射，不能直接改库、同名CreateEvent覆盖或猜通用HTTP。分类对有效也非修改/提交许可。
    """
    try:
        args=dict(event_name=event_name,organization_name=organization_name,event_instance_id=event_instance_id,
                  approval_sheet_id=approval_sheet_id,previous_cycle_gmt=previous_cycle_gmt,allow_role_substitution=allow_role_substitution)
        if purpose=='classify_pending_draft':
            if approval_sheet_id is not None or previous_cycle_gmt is not None or allow_role_substitution:raise ValueError('分类核验不能混入审批资料修订参数。')
            from tools.quality_event_classification import read_context
            return q.encode(await read_context(args|dict(classification_name=classification_name,subclassification_name=subclassification_name)))
        if classification_name is not None or subclassification_name is not None:raise ValueError('拟选分类仅允许purpose=classify_pending_draft只读比较，不混入保存或正式提交。')
        if purpose in ('submit_pending','edit_pending_draft'):
            if approval_sheet_id is not None or previous_cycle_gmt is not None or allow_role_substitution:raise ValueError('草稿正式提交核验不能混入审批资料修订参数。')
            if purpose=='submit_pending':from tools.quality_event_submission import read_context
            else:from tools.quality_event_drafts import read_context
            return q.encode(await read_context(args))
        if purpose!='update_text':raise ValueError('上下文用途仅update_text/submit_pending/edit_pending_draft/classify_pending_draft。')
        before=await context(args);verified=await contract();marker='MCP-QUAL:'+'0'*32;original=await load(before,marker)
        if await context(args)!=before:raise ValueError('读取更新上下文期间事件发生变化。')
        return q.encode({'complete':True,'before':before,'native_contract':verified,'original_detail':original['detail'],
                         'server_context':original['current'],'ready_for_execution':False})
    except (ValueError,TypeError,KeyError,AttributeError) as exc:return q.encode({'complete':False,'error':str(exc)})


@mcp.tool
async def prepare_quality_event_update(event_name:str,organization_name:str,brief_description:str|None=None,
        description:str|None=None,discovery_area:str|None=None,event_instance_id:str|None=None,comments:str='',
        approval_sheet_id:str|None=None,previous_cycle_gmt:str|None=None,allow_role_substitution:bool=False)->str:
    """准备原生UpdateEvent文字更新：仅简述/说明/发现区域，至少一项明确变化。省略/null保留；说明/区域可显式空字符串清空，简述须非空。
    固定Load核对并完整保持原报告人/发现人、日期、优先级等明细，本地静态差异与独立重读；submit固定false不重新提交已提交事件。标准PreValidate会强制执行Submit=false请求，禁止普通execute=false POST假预览，static_and_echo_verified=false。
    限已提交Active基础Event、无流程/检查表/审批/复杂关联/自定义数据及更新签名要求。保持类别/责任人/角色/分流/CAR策略，不做分派/升级或绕过权限。
    可明确approval_sheet_id及previous_cycle_gmt修订唯一审批表拒绝/取消后的Active/InReview基础Event文字；旧周期持久历史须完整核实，无待签条目/角色替代。只读Load的审批明细不回填静态执行载荷；不改审批配置、不自动重提。
    execute_quality_transaction系统确认单次提交；unknown保留原事件及关联NCR容器占用，inspect只给原ID证据，最终证据人工review，不重试。
    若原表含Role2条目，须明确allow_role_substitution=true才可保持原策略修订文字；仍由原责任人/角色操作，不修改策略或授予签署资格。
    """
    return await p.prepare('event_update',dict(event_name=event_name,organization_name=organization_name,event_instance_id=event_instance_id,
        brief_description=brief_description,description=description,discovery_area=discovery_area,comments=comments,
        approval_sheet_id=approval_sheet_id,previous_cycle_gmt=previous_cycle_gmt,allow_role_substitution=allow_role_substitution))
