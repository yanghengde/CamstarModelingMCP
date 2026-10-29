"""First triage of a plain Event, retaining category, owner, role and CAR policy."""
import copy
import json
import re
import uuid

from tools import mcp, quality as q, quality_inspection as i, quality_plans as p
from tools.quality_spc_annotations import iid, schema_check
from tools.quality_definitions import resources
from tools.process_context import ReadContext
from tools.modeling_details import raw_json, _has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

EVENT_TYPE = 7489
TRIAGE_TYPE = 7788
TRIAGE_ACTION = 16  # QualityRecordActionsEnum.Triage, standard metadata.
EVENT_COLUMNS = ('EventId,EventName,CDOTypeId,ChangeCount,Status,Category,OrganizationId,OwnerId,RoleId,PriorityLevelId,'
                 'IsFrozen,IsSubmitted,TriageComplete,IsCARRequiredToClose,ProcessModelId,OrigProcessModelTemplateId,'
                 'ChecklistId,ChecklistSaved,CloseDateGMT')
SCHEMA = {
    'Event': EVENT_COLUMNS,
    'Organization':'OrganizationId,ChangeCount', 'Employee':'EmployeeId,ChangeCount',
    'RoleDef':'RoleId,ChangeCount', 'PriorityLevel':'PriorityLevelId,ChangeCount',
    'TriageSpec':'TriageSpecId,ChangeCount',
    'CategoryMap':'CategoryMapId,ParentId,Category,TriageSpecId,IsFrozen,ChangeCount',
    'TriageSpecDetail':'TriageSpecDetailId,TriageSpecId,ToCategory,OwnerId,RoleId,CARRequired,AutoRoute,ProcessModelId,ChecklistTemplateId,IsFrozen,ChangeCount',
    'ApprovalSheet':'ApprovalSheetId,ParentId',
    'NonconformanceReport':'NonconformanceReportId,EscalatedEventId,ContainerId,ChangeCount',
}
ASSETS = {'organization':('Organizations','Organization','OrganizationId'),
          'owner':('Employees','Employee','EmployeeId'), 'role':('Roles','RoleDef','RoleId'),
          'priority':('PriorityLevels','PriorityLevel','PriorityLevelId'), 'spec':('TriageSpecs','TriageSpec','TriageSpecId'),
          'quality_resolution':('QualityResolutionCodes','QualityResolutionCode','QualityResolutionCodeId')}


async def event_record(name='', instance_id=None):
    if instance_id is not None: where="EventId='"+iid(instance_id,'原事件ID')+"'"
    else: where='EventName='+q.literal(q.text(name,'事件名称'))
    rows=await q.sql_rows('SELECT TOP 2 '+EVENT_COLUMNS+' FROM Event WHERE '+where,2)
    if not rows:return None
    if len(rows)!=1 or not set(EVENT_COLUMNS.split(','))<=rows[0].keys():raise ValueError('事件不存在、不唯一或原始字段不完整。')
    row=copy.deepcopy(rows[0])
    for field in ('EventId','OrganizationId'):row[field]=iid(row[field],field)
    if instance_id is not None and row['EventId']!=iid(instance_id) or name and row['EventName']!=q.text(name):
        raise ValueError('原事件名称/持久ID不一致。')
    q.text(row['EventName'],'事件名称')
    for field in ('CDOTypeId','ChangeCount','Status','Category'):row[field]=i.wire_int(row[field],field)
    for field in ('IsFrozen','IsSubmitted','TriageComplete','IsCARRequiredToClose','ChecklistSaved'):
        row[field]=i.wire_bool(row[field],field)
    for field in ('OwnerId','RoleId','PriorityLevelId','ProcessModelId','OrigProcessModelTemplateId','ChecklistId'):
        if row[field] not in (None,''):row[field]=iid(row[field],field)
    return row


def eligible(row):
    if (not row or row['CDOTypeId']!=EVENT_TYPE or row['Status']!=1 or row['Category']<1 or row['ChangeCount']<0
            or row['IsFrozen'] or not row['IsSubmitted'] or row['TriageComplete']):
        raise ValueError('仅支持已提交、Active、未冻结且尚未首次分流的基础Event；不处理CAPA/CAR/派生事件。')
    if any(row[f] not in (None,'') for f in ('ProcessModelId','OrigProcessModelTemplateId','ChecklistId','CloseDateGMT')) or row['ChecklistSaved']:
        raise ValueError('事件已有流程、原流程模板、检查表或关闭信息；受限首次分流不覆盖这些状态。')
    if any(not q.valid_id(row[f]) for f in ('OwnerId','RoleId','PriorityLevelId')):
        raise ValueError('必须已明确原责任人、角色和优先级；不以分流代替分派或补默认值。')


async def asset(kind, *, name=None, key=None, signature_txn_type=TRIAGE_TYPE):
    entity,table,column=ASSETS[kind];ctx=ReadContext(3)
    select='InstanceID,Name,IsFrozen'
    if kind=='organization':select+=',CollectESigForAllQualityTxns,UserRequiredForEsig,QualityESigTxns'
    if key is None:
        clean=q.text(name,'明确配置名称')
        data=await ctx.get(entity,params={'$select':select,'$filter':"Name eq '"+clean.replace("'","''")+"'",'$top':2})
        rows=data.get('value')
        if not isinstance(rows,list) or len(rows)!=1 or _has_next_link(data):raise ValueError('明确配置名称没有唯一匹配。')
        key=iid(q.identifier(rows[0]))
    else:key=iid(key)
    async def token():
        rows=await q.sql_rows(f"SELECT {column},ChangeCount FROM {table} WHERE {column}='{key}'",1)
        if len(rows)!=1 or iid(rows[0].get(column))!=key:raise ValueError('事件配置持久身份不符。')
        count=i.wire_int(rows[0].get('ChangeCount'),'配置变更计数')
        if count<0:raise ValueError('事件配置变更计数无效。')
        return count
    count=await token()
    # Explicit selection is essential: Employee also has credential fields.
    root=await ctx.get(entity,key,{'$select':select})
    if (iid(q.identifier(root))!=key or name is not None and root.get('Name')!=q.text(name)
            or _has_next_link(root) or i.wire_bool(root.get('IsFrozen'),'配置冻结状态') is not False):
        raise ValueError('事件参考配置身份不符、已冻结或字段未知。')
    state={'entity':entity,'InstanceID':key,'Name':q.text(root.get('Name')),'IsFrozen':False,'ChangeCount':count}
    if kind=='organization':
        state['CollectESigForAllQualityTxns']=i.wire_bool(root.get('CollectESigForAllQualityTxns'),'组织质量签名策略')
        state['UserRequiredForEsig']=i.wire_bool(root.get('UserRequiredForEsig'),'组织签名用户策略')
        values=root.get('QualityESigTxns')
        if not isinstance(values,list) or len(values)>100:raise ValueError('组织质量签名事务清单未知或不完整。')
        values=[i.wire_int(v,'组织签名事务类型') for v in values]
        if len(values)!=len(set(values)) or any(v<1 for v in values):raise ValueError('组织签名事务类型无效或重复。')
        state['QualityESigTxns']=sorted(values)
        if signature_txn_type is not None and (state['CollectESigForAllQualityTxns'] or signature_txn_type in values):
            raise ValueError('当前组织要求该质量事务电子签名，本受限入口不收集口令或覆盖签名。')
    if await token()!=count:raise ValueError('读取事件配置期间发生变化。')
    return state


async def triage_configuration(row):
    rows=await q.sql_rows('SELECT TOP 101 CategoryMapId,ParentId,Category,TriageSpecId,IsFrozen,ChangeCount '
        f"FROM CategoryMap WHERE ParentId='{row['OrganizationId']}' ORDER BY CategoryMapId",101)
    if len(rows)>100:raise ValueError('组织类别映射超过100项，不使用部分清单。')
    maps=[]
    for raw in rows:
        item=copy.deepcopy(raw)
        for field in ('CategoryMapId','ParentId'):item[field]=iid(item.get(field),field)
        if item['ParentId']!=row['OrganizationId']:raise ValueError('组织类别映射父级不符。')
        item['Category']=i.wire_int(item.get('Category'),'映射类别')
        item['ChangeCount']=i.wire_int(item.get('ChangeCount'),'映射变更计数')
        item['IsFrozen']=i.wire_bool(item.get('IsFrozen'),'映射冻结状态')
        if item.get('TriageSpecId') not in (None,''):item['TriageSpecId']=iid(item['TriageSpecId'])
        if item['Category']<1 or item['ChangeCount']<0:raise ValueError('组织类别映射状态无效。')
        maps.append(item)
    if len({m['CategoryMapId'] for m in maps})!=len(maps):raise ValueError('组织类别映射重复。')
    found=[m for m in maps if m['Category']==row['Category']]
    if len(found)!=1 or found[0]['IsFrozen'] or not q.valid_id(found[0].get('TriageSpecId')):
        raise ValueError('当前事件类别没有唯一、有效的组织分流规则，不猜测首行。')
    spec=await asset('spec',key=found[0]['TriageSpecId'])
    details=await q.sql_rows('SELECT TOP 101 '+SCHEMA['TriageSpecDetail']+' FROM TriageSpecDetail '
        f"WHERE TriageSpecId='{spec['InstanceID']}' ORDER BY TriageSpecDetailId",101)
    if len(details)>100:raise ValueError('分流明细超过100项，不使用部分清单。')
    normalized=[]
    for raw in details:
        item=copy.deepcopy(raw)
        for field in ('TriageSpecDetailId','TriageSpecId'):item[field]=iid(item.get(field),field)
        if item['TriageSpecId']!=spec['InstanceID']:raise ValueError('分流明细父级不符。')
        for field in ('ToCategory','ChangeCount'):item[field]=i.wire_int(item.get(field),field)
        for field in ('CARRequired','AutoRoute','IsFrozen'):item[field]=i.wire_bool(item.get(field),field)
        for field in ('OwnerId','RoleId','ProcessModelId','ChecklistTemplateId'):
            if field not in item:raise ValueError('分流明细字段不完整。')
            if item[field] not in (None,''):item[field]=iid(item[field],field)
        if item['ToCategory']<1 or item['ChangeCount']<0:raise ValueError('分流明细类别/版本计数无效。')
        normalized.append(item)
    if len({d['TriageSpecDetailId'] for d in normalized})!=len(normalized):raise ValueError('分流明细ID重复。')
    found=[d for d in normalized if d['ToCategory']==row['Category']]
    if len(found)!=1:raise ValueError('保留当前类别没有唯一分流明细，不按列表第一条选择。')
    detail=found[0]
    if (detail['IsFrozen'] or detail['AutoRoute'] or detail['ProcessModelId'] not in (None,'')
            or detail['ChecklistTemplateId'] not in (None,'') or detail['CARRequired']!=row['IsCARRequiredToClose']
            or detail['OwnerId'] not in (None,'',row['OwnerId']) or detail['RoleId'] not in (None,'',row['RoleId'])):
        raise ValueError('分流默认要求重新分派、流程/检查表或CAR策略变化，本受限入口不清空或绕过。')
    return {'category_maps':maps,'spec':spec,'details':normalized,'selected_detail':detail}


async def context(args):
    q.text(args['event_name'],'事件名称');q.text(args['organization_name'],'组织名称')
    if args.get('event_instance_id') is not None:iid(args['event_instance_id'])
    if args.get('priority_level_name') is not None:q.text(args['priority_level_name'],'明确优先级')
    await schema_check(SCHEMA)
    row=await event_record(args['event_name'],args.get('event_instance_id'));eligible(row)
    linked=await q.sql_rows('SELECT TOP 21 NonconformanceReportId,EscalatedEventId,ContainerId,ChangeCount '
        f"FROM NonconformanceReport WHERE EscalatedEventId='{row['EventId']}' ORDER BY NonconformanceReportId",21)
    if len(linked)>20:raise ValueError('关联NCR超过20条，不能用部分关联声明事务占用。')
    links=[]
    for raw in linked:
        item={key:iid(raw.get(key),key) for key in ('NonconformanceReportId','EscalatedEventId','ContainerId')}
        item['ChangeCount']=i.wire_int(raw.get('ChangeCount'),'关联NCR变更计数')
        if item['EscalatedEventId']!=row['EventId'] or item['ChangeCount']<0:raise ValueError('关联NCR的原事件/版本不符。')
        links.append(item)
    if len({r['NonconformanceReportId'] for r in links})!=len(links):raise ValueError('关联NCR重复。')
    approvals=await q.sql_rows(f"SELECT TOP 1 ApprovalSheetId FROM ApprovalSheet WHERE ParentId='{row['EventId']}'",1)
    if approvals:raise ValueError('事件已有审批表，本入口不覆盖审批或撤回/重启审批。')
    org=await asset('organization',name=args['organization_name'],key=row['OrganizationId'])
    owner=await asset('owner',key=row['OwnerId']);role=await asset('role',key=row['RoleId'])
    old_priority=await asset('priority',key=row['PriorityLevelId'])
    priority=old_priority if args.get('priority_level_name') is None else await asset('priority',name=args['priority_level_name'])
    cfg=await triage_configuration(row)
    return {'event':row,'organization':org,'owner':owner,'role':role,'original_priority':old_priority,'priority':priority,
            'triage_configuration':cfg,'direct_approval_sheets':[],'linked_ncrs':links}


def ref(state):return {'name':state['Name'],'instanceId':state['InstanceID']}


def body_for(before, args, marker):
    row=before['event'];comment=args.get('comments','')
    if not isinstance(comment,str) or len(comment)>200 or '\x00' in comment:raise ValueError('分流备注须为最多200字符的字符串。')
    return {'qualityObject':{'name':row['EventName'],'instanceId':row['EventId']},'organization':ref(before['organization']),
            'category':row['Category'],'qualityObjectOwner':ref(before['owner']),'qualityObjectRole':ref(before['role']),
            'priorityLevel':ref(before['priority']),'isCARRequiredToClose':row['IsCARRequiredToClose'],
            'triageComplete':True,'comments':marker+(' '+comment if comment else '')}


def canonical_refs(value):
    if isinstance(value,list):return [canonical_refs(v) for v in value]
    if not isinstance(value,dict):return value
    return {k:(v.lower() if k in ('InstanceId','InstanceID') and q.valid_id(v) else canonical_refs(v)) for k,v in value.items()}


async def preview(body, before):
    fields=('Category,Status,Organization,Owner,Role,PriorityLevel,ProcessModel,TriageComplete,IsCARRequiredToClose,'
            'IsApprovalRequired,CompletionApprovalSheet,CompletionApprovalStatus,EntryForApproval,UserHasRole,UserIsOwner')
    params={'execute':'false','$select':'QualityObject,Organization,Category,QualityObjectOwner,QualityObjectRole,PriorityLevel,'
            'IsCARRequiredToClose,TriageComplete,Comments,AvailableActions,IgnoreProcessModel,RouteProcessModel,ProcessModelTemplate,'
            'ChecklistTemplate,TriageSpecDetail,ESigRequirement,AdditionalESigReqs,QualityObjectDetail',
            '$expand':'QualityObjectDetail($select='+fields+')'}
    raw=await request('POST','/api/TriageQualityObject',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,params=params,response_formatter=raw_json)
    try:outer=json.loads(raw)
    except (ValueError,TypeError):raise ValueError('事件首次分流非执行预览失败：'+str(raw)[:1000]) from None
    data=canonical_refs(outer.get('data',outer)) if isinstance(outer,dict) else None
    if (not isinstance(data,dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error') or data.get('ErrorCode')
            or _has_next_link(data) or not p.wire_matches(body,data)):
        raise ValueError('事件、组织、原责任人/角色/类别或明确优先级未完整回显。')
    actions=i.wire_int(data.get('AvailableActions'),'可用动作原码')
    if actions<0 or actions & TRIAGE_ACTION != TRIAGE_ACTION:raise ValueError('当前账号/事件没有原生Triage动作，不覆盖动作位。')
    for field in ('IgnoreProcessModel','RouteProcessModel'):
        if i.wire_bool(data.get(field),field) is not False:raise ValueError('原生流程绕过/路由默认未知或非false，不提交。')
    for field in ('ProcessModelTemplate','ChecklistTemplate','ESigRequirement'):
        if field not in data or data[field] is not None:raise ValueError('预览派生了流程、检查表或签名要求，不提交。')
    if 'AdditionalESigReqs' not in data or data['AdditionalESigReqs'] not in (None,[]):raise ValueError('额外签名要求非空或未知。')
    selected=before['triage_configuration']['selected_detail'];spec=before['triage_configuration']['spec']
    dt=data.get('TriageSpecDetail')
    if not isinstance(dt,dict) or q.identifier(dt)!=selected['TriageSpecDetailId'] or q.identifier(dt.get('Parent'))!=spec['InstanceID']:
        raise ValueError('原生派生的分流规则明细/父级与已核实配置不一致。')
    detail=data.get('QualityObjectDetail')
    if not isinstance(detail,dict) or not set(fields.split(','))<=detail.keys():raise ValueError('事件当前状态/审批上下文未完整返回。')
    row=before['event']
    for field,expected in [('Category',row['Category']),('Status',row['Status'])]:
        if i.wire_int(detail[field],field)!=expected:raise ValueError('事件当前类别/状态回显不符。')
    for field,expected in [('Organization',row['OrganizationId']),('Owner',row['OwnerId']),('Role',row['RoleId']),('PriorityLevel',row['PriorityLevelId'])]:
        if q.identifier(detail[field])!=expected:raise ValueError('事件当前组织/责任人/角色/优先级关联不符。')
    for field,expected in [('TriageComplete',False),('IsCARRequiredToClose',row['IsCARRequiredToClose']),('IsApprovalRequired',False),('UserHasRole',True),('UserIsOwner',True)]:
        if i.wire_bool(detail[field],field) is not expected:raise ValueError('事件分流、审批或当前账号角色/责任人状态不满足受限要求。')
    if any(detail[f] is not None for f in ('ProcessModel','CompletionApprovalSheet','EntryForApproval')) or detail['CompletionApprovalStatus'] not in (None,''):
        raise ValueError('事件已有流程/审批上下文，本入口不覆盖。')
    # Discard transient service/detail IDs and never request signer/password data.
    stable={f:copy.deepcopy(detail[f]) for f in fields.split(',')}
    return {'params':params,'verified_input_echo':body,'available_actions_raw':actions,'triage_action_mask':TRIAGE_ACTION,
            'current_quality_context':stable,'selected_triage_detail_id':selected['TriageSpecDetailId'],
            'dynamic_validation_verified':False,'signature_verified':False,'triage_completed_verified':False,
            'note':'仅非执行回显；保持类别、责任人、角色和CAR策略，不挂检查表、不启动流程、不请求签名或发送通知。实际权限、效果及部署扩展待验收。'}


async def build_triage(args, marker):
    comment=args.get('comments','')
    if not isinstance(comment,str) or len(comment)>200 or '\x00' in comment:raise ValueError('分流备注须为最多200字符的字符串。')
    before=await context(args);body=body_for(before,args,marker);echo=await preview(body,before)
    if await context(args)!=before:raise ValueError('准备期间原事件、责任人/角色、组织、优先级或分流配置发生变化。')
    row=before['event'];cfg=before['triage_configuration']
    reads=set()
    for state in [before[k] for k in ('organization','owner','role','original_priority','priority')]+[cfg['spec']]:
        reads.update(resources(state['entity'],state))
    return {'kind':'event_triage','action':'complete_first_triage','service':'TriageQualityObject','name':row['EventName'],
            'target_id':row['EventId'],'scope_key':'quality-event:id:'+row['EventId'],'before':before,'body':body,'preview':echo,
            'resource_reads':sorted(reads),'resource_writes':sorted({'runtime:quality-event:id:'+row['EventId']}
                |{'runtime:container:id:'+r['ContainerId'] for r in before['linked_ncrs']}),
            'marker':marker,'arguments':args,'environment':p.environment()}


def validate_transport(snap):
    if (snap.get('kind')!='event_triage' or snap.get('action')!='complete_first_triage' or snap.get('service')!='TriageQualityObject'
            or not isinstance(snap.get('marker'),str) or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap['marker'])
            or snap.get('target_id')!=snap['before']['event']['EventId'] or snap.get('name')!=snap['before']['event']['EventName']
            or type(snap['body'].get('category')) is not int or type(snap['body'].get('triageComplete')) is not bool
            or type(snap['body'].get('isCARRequiredToClose')) is not bool
            or snap['body']!=body_for(snap['before'],snap['arguments'],snap['marker'])):
        raise ValueError('首次分流服务、原事件、保留字段或允许参数不符。')


async def inspect_snapshot(snap):
    current=await event_record(instance_id=snap['target_id'])
    history=None
    if current:
        history=json.loads(await q.get_quality_history('quality_object',current['EventName'],snap['target_id'],limit=20))
    return {'original_event_id':snap['target_id'],'current_event':current,'bounded_quality_history':history,
            'verified_success':False,'state_changed':False,'retry_allowed':False,'request_correlation_verified':False,
            'note':'原事件当前TriageComplete/优先级变化不证明本次提交；不跟随同名替代事件、不解锁或重试，最终事务证据人工review。'}


@mcp.tool
async def get_quality_event_triage_context(event_name:str,organization_name:str,event_instance_id:str|None=None)->str:
    """只读核对基础Event首次分流前置和原生非执行回显：已提交Active、未分流/冻结，无流程/检查表/审批表，已有责任人/角色/优先级。
    核对组织CategoryMap、唯一保留类别的分流明细和签名策略，原生可用动作包含Triage=16、当前用户为责任人且有角色。
    不改变事件、不保存执行计划、不替代批准；其他状态/复杂分流用已有质量详情/历史读取，不猜默认流程。
    """
    try:
        draft=await build_triage(dict(event_name=event_name,organization_name=organization_name,event_instance_id=event_instance_id,
                                     priority_level_name=None,comments=''),'MCP-QUAL:'+uuid.uuid4().hex)
        return q.encode({'complete':True,'before':draft['before'],'server_context':draft['preview'],
                         'ready_for_execution':False,'quality_verdict':None})
    except (ValueError,TypeError,KeyError,AttributeError) as exc:return q.encode({'complete':False,'error':str(exc)})


@mcp.tool
async def prepare_quality_event_triage(event_name:str,organization_name:str,priority_level_name:str|None=None,
        event_instance_id:str|None=None,comments:str='')->str:
    """准备原生TriageQualityObject首次分流确认计划，不写MES。保持原类别、责任人、角色、CAR策略；可明确新优先级，省略则保留。
    限已提交Active基础Event、未分流/冻结、无流程/检查表/审批表；组织/分流配置唯一有效，默认不要求分派、CAR改变或流程/检查表。
    不改责任人以免触发原生分派邮件，不启流程、不降类、不覆盖签名或审批。原生Triage动作及用户角色/责任人上下文须完整回显。
    execute_quality_transaction系统确认后单次提交；unknown用inspect看原ID事件/有限历史，最终证据人工review，不换名/对象重试。
    """
    return await p.prepare('event_triage',dict(event_name=event_name,organization_name=organization_name,
        priority_level_name=priority_level_name,event_instance_id=event_instance_id,comments=comments))
