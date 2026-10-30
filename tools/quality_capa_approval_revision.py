"""Native CAPA text revision after a durably evidenced rejected/cancelled cycle."""
import copy
import json
import re
from tools import mcp,quality as q,quality_plans as p,quality_approvals as a,quality_event_triage as t,quality_event_update as u,quality_approval_history as h
from tools.quality_definitions import resources
from tools.quality_spc_annotations import iid,schema_check
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SERVICE_TYPE=7654
EXTRA='OwnerId,RoleId,OrigProcessModelTemplateId,CAPACustomDataId,CARSeverityId,InitiatorId,InitiatorOrganizationId,ReporterId,ReporterOrganizationId,OccurrenceDate,ReportedDate,ReportedDateGMT'
SCHEMA={'CAPA':q.CAPA_COLUMNS+','+EXTRA,'RegulatoryReport':'RegulatoryReportId,ParentId'}
REFS=('Initiator','InitiatorOrganization','Reporter','ReporterOrganization','PriorityLevel','CARSeverity')
FIELDS=('AdditionalOrganizations','BriefDescription','CAPACustomDetail','CARSeverity','Description','Initiator','InitiatorOrganization','IsSystemicIssue','OccurrenceDate','OccurrenceDateGMT','PriorityLevel','ReportedDate','ReportedDateGMT','Reporter','ReporterOrganization')
CURRENT=('Category','Status','Organization','Owner','Role','PriorityLevel','ProcessModel','UserHasRole','UserIsOwner','IsApprovalRequired','CompletionApprovalSheet','CompletionApprovalStatus','EntryForApproval')
SELECT='QualityObject,Organization,Submit,Comments,ServiceDetail,ApprovalSheetDetails,QualityCrossRefDetails,AvailableActions,IgnoreProcessModel,ESigRequirement,AdditionalESigReqs,QualityObjectDetail'
EXPAND='ServiceDetail($select=CDOTypeId,'+','.join(FIELDS)+'),QualityObjectDetail,ApprovalSheetDetails($expand=ApprovalEntries),QualityCrossRefDetails'


async def source(args):
    if args.get('allow_reported_date_refresh') is not True:raise ValueError('原生CAPA修订会刷新报告时间，必须明确allow_reported_date_refresh=true；不承诺保留原报告时间。')
    await schema_check(SCHEMA)
    ctx=await a.read_context('capa',args['name'],args['organization_name'],args.get('instance_id'))
    obj=ctx['target'];key=iid(args['approval_sheet_id'])
    if obj['Category']!=5 or obj['IsFrozen'] or obj['Status'] not in (1,8) or len(ctx['sheets'])!=1 or ctx['sheets'][0]['ApprovalSheetId']!=key:
        raise ValueError('CAPA修订仅支持未冻结Active/InReview类别5及其唯一明确审批表。')
    sheet=ctx['sheets'][0]
    if sheet['IsFrozen'] or sheet['Status'] not in (0,30) or ctx['current_employee_entry_id'] is not None:raise ValueError('CAPA须已拒绝/取消且没有待签条目。')
    if any(e['IsFrozen'] or e['SubstituteOption']!=1 or e['DelegationTaskId'] is not None for e in sheet['entries']):raise ValueError('CAPA修订不处理冻结、角色替代或委托。')
    proof=await h.previous_cycle_evidence(ctx,sheet,args['previous_cycle_gmt'])
    rows=await q.sql_rows('SELECT TOP 2 '+SCHEMA['CAPA']+f" FROM CAPA WHERE CAPAId='{obj['Id']}'",2)
    a.complete(rows,SCHEMA['CAPA'])
    if len(rows)!=1:raise ValueError('CAPA持久资料不存在或不唯一。')
    raw={k:copy.deepcopy(rows[0][k]) for k in SCHEMA['CAPA'].split(',')}
    if iid(raw['CAPAId'])!=obj['Id'] or raw['CAPAName']!=obj['Name'] or a.integer(raw['Category'],'CAPA类别')!=5 or a.integer(raw['ChangeCount'],'CAPA版本')!=obj['ChangeCount'] or a.integer(raw['Status'],'CAPA状态')!=obj['Status'] or iid(raw['OrganizationId'])!=obj['OrganizationId']:
        raise ValueError('CAPA资料身份/版本/状态/组织与审批上下文不符。')
    if not a.i.wire_bool(raw['IsSubmitted'],'CAPA提交') or a.i.wire_bool(raw['IsFrozen'],'CAPA冻结') or any(raw[k] not in (None,'') for k in ('ProcessModelId','OrigProcessModelTemplateId','CAPACustomDataId','CloseDateGMT')):
        raise ValueError('CAPA必须已提交且无流程、自定义数据或关闭信息。')
    if await q.sql_rows(f"SELECT TOP 1 RegulatoryReportId FROM RegulatoryReport WHERE ParentId='{obj['Id']}'",1):raise ValueError('CAPA已有法规报告，不在受限修订范围。')
    org=await t.asset('organization',name=args['organization_name'],key=obj['OrganizationId'],signature_txn_type=SERVICE_TYPE)
    owner=await t.asset('owner',key=iid(raw['OwnerId']));role=await t.asset('role',key=iid(raw['RoleId']))
    priority=await t.asset('priority',key=iid(raw['PriorityLevelId']))
    return dict(context=ctx,capa=raw,previous_cycle=proof,organization=org,owner=owner,role=role,priority=priority)


async def contract():
    doc=json.loads(await request('GET','/swagger/UpdateCAPA/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json))
    try:
        schemas=doc['components']['schemas'];root=schemas['UpdateCAPA']['properties']
        detail=schemas[root['serviceDetail']['$ref'].split('/')[-1]]['properties']
        expected={f[0].lower()+f[1:] for f in FIELDS}
        # API acronyms use capaCustomDetail/carSeverity, not the CDO's uppercase spelling.
        expected-={'cAPACustomDetail','cARSeverity'};expected|={'capaCustomDetail','carSeverity'}
        if not expected<=detail.keys() or set(detail)-expected-u.DETAIL_SYSTEM_FIELDS:raise ValueError('明细存在未支持字段')
        model=doc['paths']['/api/UpdateCAPA/PerformEvent']['post']['requestBody']['content']['application/json']['schema']
        envelope=schemas[model['$ref'].split('/')[-1]]['properties']
        if envelope['inputData']['$ref'].split('/')[-1]!='UpdateCAPA' or schemas[envelope['event']['$ref'].split('/')[-1]]['properties']['eventName']['type']!='string':raise ValueError('Load输入不符')
        if root['qualityObject']['$ref'].split('/')[-1]!='NamedObjectRef' or root['submit']['type']!='boolean':raise ValueError('根输入不符')
    except (ValueError,TypeError,KeyError) as exc:raise ValueError('当前UpdateCAPA及固定Load契约未核实。') from exc
    return p.digest({'root':root,'detail':detail,'envelope':envelope})


def body_root(before,marker,comments=''):
    obj=before['context']['target']
    return dict(qualityObject=dict(name=obj['Name'],instanceId=obj['Id']),organization=t.ref(before['organization']),submit=False,
                comments=marker+(' '+u.text_value(comments,'CAPA修订说明',200,True) if comments else ''))


def validate_frame(data,body,before,loaded=False):
    if not p.wire_matches(body,data):raise ValueError('CAPA原对象、组织、submit=false及明确资料未完整回显。')
    mask=a.integer(data.get('AvailableActions'),'CAPA动作')
    if mask&2!=2 or a.i.wire_bool(data.get('IgnoreProcessModel'),'流程绕过') is not False:raise ValueError('CAPA当前账号没有Edit动作或试图绕过流程。')
    if data.get('ESigRequirement') is not None or 'ESigRequirement' not in data or 'AdditionalESigReqs' not in data or data['AdditionalESigReqs'] not in (None,[]):raise ValueError('CAPA动态签名要求未知或非空。')
    if data.get('QualityCrossRefDetails')!=[]:raise ValueError('CAPA交叉引用非空或未知。')
    ctx=before['context'];detail=data.get('QualityObjectDetail');raw=before['capa']
    if not isinstance(detail,dict) or not set(CURRENT)<=detail.keys():raise ValueError('CAPA当前上下文不完整。')
    for field in ('Category','Status'):
        if a.integer(detail[field],field)!=ctx['target'][field]:raise ValueError('CAPA类别或状态变化。')
    for field in ('Organization','Owner','Role','PriorityLevel'):
        if iid(q.identifier(detail[field]))!=iid(raw[field+'Id']):raise ValueError('CAPA原组织/归属/优先级变化。')
    if not a.i.wire_bool(detail['UserIsOwner'],'CAPA责任人') or not a.i.wire_bool(detail['UserHasRole'],'CAPA角色') or detail['ProcessModel'] is not None:raise ValueError('CAPA当前责任人/角色不符或存在流程。')
    expected=ctx['current_context']
    if a.i.wire_bool(detail['IsApprovalRequired'],'CAPA审批要求')!=expected['IsApprovalRequired']:raise ValueError('CAPA审批要求变化。')
    for field in ('CompletionApprovalSheet','EntryForApproval'):
        actual=None if detail[field] is None else iid(q.identifier(detail[field]))
        want=None if expected[field] is None else expected[field]['instance_id']
        if actual!=want:raise ValueError('CAPA原审批入口变化。')
    if a.integer(detail['CompletionApprovalStatus'],'CAPA审批状态')!=a.integer(expected['CompletionApprovalStatus'],'原审批状态'):raise ValueError('CAPA原审批状态变化。')
    sheet=ctx['sheets'][0]
    if loaded:
        approval=data.get('ApprovalSheetDetails')
        if not isinstance(approval,dict) or iid(q.identifier(approval.get('ApprovalSheet')))!=sheet['ApprovalSheetId'] or a.integer(approval.get('Status'),'Load表状态')!=sheet['Status']:raise ValueError('CAPA Load审批表不符。')
        entries=approval.get('ApprovalEntries')
        if not isinstance(entries,list) or len(entries)!=len(sheet['entries']):raise ValueError('CAPA Load审批条目不完整。')
        ids=[iid(q.identifier(e['ApprovalEntry'])) for e in entries]
        if len(set(ids))!=len(ids) or set(ids)!={e['ApprovalSheetEntryId'] for e in sheet['entries']}:raise ValueError('CAPA Load审批条目归属不符。')
    elif 'ApprovalSheetDetails' not in data or data['ApprovalSheetDetails'] is not None:raise ValueError('CAPA执行预览包含审批明细写入。')
    return dict(available_actions_raw=mask,edit_action_mask=2,reported_date_refresh_expected=True,
                actor_permission_verified=True,extra_signature_performed=False)


def field_key(field):return {'CAPACustomDetail':'capaCustomDetail','CARSeverity':'carSeverity'}.get(field,field[0].lower()+field[1:])


def loaded_detail(data,before):
    detail=data.get('ServiceDetail');raw=before['capa']
    if not isinstance(detail,dict) or not set(FIELDS)<=detail.keys() or a.integer(detail.get('CDOTypeId'),'CAPA明细类型')!=7734:raise ValueError('Load不是完整7734 CAPA明细。')
    if detail['AdditionalOrganizations']!=[] or detail['CAPACustomDetail'] is not None:raise ValueError('CAPA附加组织或自定义数据不在修订范围。')
    for field in ('BriefDescription','Description'):
        if type(detail[field]) is not type(raw[field]) or detail[field]!=raw[field]:raise ValueError('CAPA原文字资料不符。')
    if a.i.wire_bool(detail['IsSystemicIssue'],'CAPA系统性问题')!=a.i.wire_bool(raw['SystemicIssue'],'原系统性问题'):raise ValueError('CAPA系统性问题值变化。')
    clean={field_key(f):copy.deepcopy(detail[f]) for f in FIELDS}
    for field in REFS:
        key=raw[field+'Id'];ref=detail[field]
        if key in (None,''):
            if ref is not None:raise ValueError('CAPA Load派生了原记录没有的引用。')
            clean[field_key(field)]=None
        else:
            if iid(q.identifier(ref))!=iid(key):raise ValueError('CAPA报告人/发现人/组织/优先级/严重度不符。')
            clean[field_key(field)]=dict(name=q.text(ref.get('Name')),instanceId=iid(key))
    for field in u.DATE_FIELDS:
        if not u.date_match(raw[field],detail[field],field.endswith('GMT')):raise ValueError('CAPA原日期Load不符，不猜测时区。')
    for local,gmt in [('OccurrenceDate','OccurrenceDateGMT'),('ReportedDate','ReportedDateGMT')]:
        if detail[local] is not None or detail[gmt] is not None:
            if not u.timestamp_valid(detail[local]) or not u.timestamp_valid(detail[gmt]) or u.timestamp_ticks(detail[local])!=u.timestamp_ticks(detail[gmt]):raise ValueError('CAPA本地/GMT不是同一时刻。')
    return clean


async def load(before,marker):
    body=body_root(before,marker)
    data=u.parse_reply(await request('POST','/api/UpdateCAPA/PerformEvent',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body=dict(event=dict(eventName='Load'),inputData=body),params={'$select':SELECT,'$expand':EXPAND},response_formatter=raw_json))
    validate_frame(data,body,before,loaded=True)
    return loaded_detail(data,before)


async def build(args,marker):
    changes={k:args.get(k) for k in ('brief_description','description') if args.get(k) is not None}
    if not changes:raise ValueError('CAPA修订须有明确文字变化。')
    for k,v in changes.items():u.text_value(v,k,100 if k=='brief_description' else 4000,k=='brief_description')
    u.text_value(args['comments'],'CAPA修订说明',200,True)
    before=await source(args);verified=await contract();original=await load(before,marker)
    detail=copy.deepcopy(original)
    for key,value in changes.items():detail[{'brief_description':'briefDescription','description':'description'}[key]]=value
    if detail==original:raise ValueError('CAPA明确文字与原值一致。')
    body=body_root(before,marker,args['comments']);body['serviceDetail']=detail
    data=u.parse_reply(await request('POST','/api/UpdateCAPA',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,
        params={'execute':'false','$select':SELECT,'$expand':EXPAND},response_formatter=raw_json))
    current=validate_frame(data,body,before)
    if a.integer(data['ServiceDetail'].get('CDOTypeId'),'CAPA预览明细类型')!=7734:raise ValueError('CAPA预览明细类型不符。')
    if await source(args)!=before or await load(before,marker)!=original:raise ValueError('CAPA原资料、审批周期、历史或权限/策略在准备期间变化。')
    reads=set()
    for kind in ('organization','owner','role','priority'):reads.update(resources(before[kind]['entity'],before[kind]))
    obj=before['context']['target']
    return dict(kind='capa_approval_revision',action='revise_text',service='UpdateCAPA',name=obj['Name'],target_id=obj['Id'],
        scope_key='quality-event:id:'+obj['Id'],arguments=args,before=before,original_detail=original,body=body,marker=marker,environment=p.environment(),
        resource_reads=sorted(reads),resource_writes=['runtime:quality-event:id:'+obj['Id'],'runtime:approval-sheet:id:'+iid(args['approval_sheet_id'])],
        preview=dict(contract_digest=verified,**current,dynamic_validation_verified=False,
            note='仅修订明确CAPA简述/说明，原报告人、发生时间、优先级/严重度/系统性问题完整保持。原生ModifyEntity会刷新ReportedDate/GMT，已明确允许；不回填Load的审批明细、不修改审批配置、不自动重提。'))


def validate_transport(snap):
    if snap.get('kind')!='capa_approval_revision' or snap.get('service')!='UpdateCAPA' or snap.get('action')!='revise_text' or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker','')):raise ValueError('CAPA修订服务/计划类型/标记不符。')
    if snap['arguments'].get('allow_reported_date_refresh') is not True or snap.get('target_id')!=snap['before']['context']['target']['Id']:raise ValueError('CAPA原对象或报告时间刷新授权不符。')
    expected=body_root(snap['before'],snap['marker'],snap['arguments']['comments']);detail=copy.deepcopy(snap['original_detail']);changed=False
    for arg,key in [('brief_description','briefDescription'),('description','description')]:
        value=snap['arguments'].get(arg)
        if value is not None:
            u.text_value(value,arg,100 if arg=='brief_description' else 4000,arg=='brief_description');changed=changed or value!=detail[key];detail[key]=value
    expected['serviceDetail']=detail
    if not changed or snap['body']!=expected:raise ValueError('CAPA允许修订字段或原保持资料不符。')


async def inspect(snap):
    obj=snap['before']['context']['target']
    return dict(original_object_id=obj['Id'],current_record=await q.find_record('capa','',obj['Id']),
        current_direct_sheets=await a.sheets_for(obj['Id']),verified_success=False,retry_allowed=False,
        note='仅原CAPA/审批表证据；文字相同或报告时间刷新不证明本次成功，不解除unknown或重试。')


@mcp.tool
async def prepare_capa_approval_revision(name:str,organization_name:str,approval_sheet_id:str,previous_cycle_gmt:str,comments:str,
        brief_description:str|None=None,description:str|None=None,instance_id:str|None=None,allow_reported_date_refresh:bool=False)->str:
    """准备唯一审批表拒绝/取消后的CAPA简述/说明修订。明确原对象/组织/表ID、旧GMT周期、非空comments及至少一项实际文字变化。
    限已提交未冻结Active/InReview类别5、无流程/附加组织/自定义数据/法规报告/交叉引用；旧周期路由/决策/评论持久历史逐条核实。固定Load完整保持原报告人、发生时间、优先级、严重度和系统性问题；审批明细只读不回写。
    原生CAPADetail.ModifyEntity会刷新报告时间，必须明确allow_reported_date_refresh=true；不通过回填旧值承诺保留时间。当前原责任人/角色、Edit2、无签名要求须核实。submit=false，不自动重提或改变审批人；确认后统一执行器单次提交，unknown不重试。
    """
    return await p.prepare('capa_approval_revision',dict(name=name,organization_name=organization_name,approval_sheet_id=approval_sheet_id,
        previous_cycle_gmt=previous_cycle_gmt,comments=comments,brief_description=brief_description,description=description,
        instance_id=instance_id,allow_reported_date_refresh=allow_reported_date_refresh))
