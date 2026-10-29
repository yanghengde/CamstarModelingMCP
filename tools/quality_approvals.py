"""Read-only direct quality approval sheets, entries and allowed decisions."""
import asyncio
import copy
from typing import Literal
from tools import mcp,quality as q,quality_event_triage as t,quality_inspection as i
from tools.quality_spc_annotations import iid,schema_check

SCHEMA={'Event':t.EVENT_COLUMNS,'CAPA':q.CAPA_COLUMNS,'Organization':'OrganizationId,ChangeCount',
 'ApprovalSheet':'ApprovalSheetId,ApprovalSheetName,ParentId,Status,IsFrozen,ChangeCount,ApprovalRequired,ApprovalDecisionListId,LastApprovalCycleGMT',
 'ApprovalSheetEntry':'ApprovalSheetEntryId,ParentId,Status,SheetLevel,ApproverId,ApproverRoleId,AssignedApproverId,SubstituteOption,EntryRequired,IsFrozen,ChangeCount,ApprovalDecisionId,DecisionType,ApprovedById,LastDecisionId,LastCompletedOnGMT',
 'ApprovalDecisionList':'ApprovalDecisionListId,ApprovalDecisionListName,IsFrozen,ChangeCount',
 'ApprovalDecision':'ApprovalDecisionId,ApprovalDecisionListId,ApprovalDecisionName,DecisionType,IncludeComments,IsFrozen,ChangeCount'}
STATUS={0:'Pending',10:'Routed',20:'Cancelled',30:'Rejected',40:'Approved'}
CURRENT=('Status','Category','Organization','Owner','Role','ProcessModel','IsApprovalRequired','CompletionApprovalSheet','CompletionApprovalStatus','EntryForApproval')

def integer(value,label,minimum=0):
    n=i.wire_int(value,label)
    if n<minimum:raise ValueError(label+'低于允许范围。')
    return n

def optional(value):return None if value in (None,'') else iid(value)

def complete(rows,columns):
    if any(not isinstance(r,dict) or not set(columns.split(','))<=r.keys() for r in rows):raise ValueError('审批查询字段不完整，不用缺失字段补默认值。')

def unique(rows,key):
    if len({iid(r[key]) for r in rows})!=len(rows):raise ValueError('审批清单持久ID重复。')

async def target(kind,name,key):
    row=await t.event_record(name,key) if kind=='event' else await q.find_record('capa',name,key)
    if not row:raise ValueError('明确质量对象不存在。')
    prefix='Event' if kind=='event' else 'CAPA'
    return dict(Id=iid(row[prefix+'Id']),Name=q.text(row[prefix+'Name']),OrganizationId=iid(row['OrganizationId']),
        Status=integer(row['Status'],'对象状态'),Category=integer(row['Category'],'对象类别',1),
        ChangeCount=integer(row['ChangeCount'],'对象变更计数'),IsFrozen=i.wire_bool(row['IsFrozen'],'对象冻结状态'))

async def sheets_for(key):
    sheets=await q.sql_rows('SELECT TOP 21 '+SCHEMA['ApprovalSheet']+f" FROM ApprovalSheet WHERE ParentId='{key}' ORDER BY ApprovalSheetId",21)
    if len(sheets)>20:raise ValueError('直接审批表超过20张，不返回部分结果。')
    complete(sheets,SCHEMA['ApprovalSheet']);unique(sheets,'ApprovalSheetId')
    sheets=[{k:copy.deepcopy(r[k]) for k in SCHEMA['ApprovalSheet'].split(',')} for r in sheets]
    total=0;decisions_cache={}
    for sheet in sheets:
        sheet['ApprovalSheetId']=iid(sheet['ApprovalSheetId'])
        if iid(sheet['ParentId'])!=key:raise ValueError('审批表不属于原质量对象。')
        sheet['ApprovalSheetName']=q.text(sheet['ApprovalSheetName'],'审批表名称')
        sheet['Status']=integer(sheet['Status'],'审批表状态');sheet['status_name']=STATUS.get(sheet['Status'],'Unknown')
        sheet['IsFrozen']=i.wire_bool(sheet['IsFrozen'],'审批表冻结状态');sheet['ApprovalRequired']=i.wire_bool(sheet['ApprovalRequired'],'审批要求')
        sheet['ChangeCount']=integer(sheet['ChangeCount'],'审批表版本')
        list_id=optional(sheet['ApprovalDecisionListId']);sheet['ApprovalDecisionListId']=list_id
        entries=await q.sql_rows('SELECT TOP 101 '+SCHEMA['ApprovalSheetEntry']+
            f" FROM ApprovalSheetEntry WHERE ParentId='{sheet['ApprovalSheetId']}' ORDER BY SheetLevel,ApprovalSheetEntryId",101)
        total+=len(entries)
        if len(entries)>100 or total>200:raise ValueError('单表条目超过100或本次总条目超过200。')
        complete(entries,SCHEMA['ApprovalSheetEntry']);unique(entries,'ApprovalSheetEntryId')
        entries=[{k:copy.deepcopy(r[k]) for k in SCHEMA['ApprovalSheetEntry'].split(',')} for r in entries]
        for entry in entries:
            entry['ApprovalSheetEntryId']=iid(entry['ApprovalSheetEntryId'])
            if iid(entry['ParentId'])!=sheet['ApprovalSheetId']:raise ValueError('审批条目父级不符。')
            for field in ('Status','SheetLevel','ChangeCount','SubstituteOption'):entry[field]=integer(entry[field],field)
            for field in ('ApproverId','ApproverRoleId','AssignedApproverId','ApprovalDecisionId','ApprovedById','LastDecisionId'):entry[field]=optional(entry[field])
            for field in ('IsFrozen','EntryRequired'):entry[field]=i.wire_bool(entry[field],field)
            entry['status_name']=STATUS.get(entry['Status'],'Unknown')
            # A routed candidate is not the current employee's permission to sign.
            entry['routed_candidate']=sheet['Status']==10 and entry['Status']==10 and not sheet['IsFrozen'] and not entry['IsFrozen']
            entry['substitute_policy']={1:'named_approver',2:'role_substitute'}.get(entry['SubstituteOption'],'unknown')
        sheet['entries']=entries;sheet['decision_list']=None;sheet['allowed_decisions']=[]
        if list_id:
            if list_id not in decisions_cache:
                rows=await q.sql_rows('SELECT TOP 1 '+SCHEMA['ApprovalDecisionList']+f" FROM ApprovalDecisionList WHERE ApprovalDecisionListId='{list_id}'",1)
                complete(rows,SCHEMA['ApprovalDecisionList'])
                if len(rows)!=1 or iid(rows[0]['ApprovalDecisionListId'])!=list_id:raise ValueError('审批决策列表身份不符。')
                definition={k:copy.deepcopy(rows[0][k]) for k in SCHEMA['ApprovalDecisionList'].split(',')};definition['ChangeCount']=integer(definition['ChangeCount'],'决策列表版本');definition['IsFrozen']=i.wire_bool(definition['IsFrozen'],'决策列表冻结')
                options=await q.sql_rows('SELECT TOP 51 '+SCHEMA['ApprovalDecision']+f" FROM ApprovalDecision WHERE ApprovalDecisionListId='{list_id}' ORDER BY ApprovalDecisionId",51)
                if len(options)>50:raise ValueError('单个决策列表超过50项。')
                complete(options,SCHEMA['ApprovalDecision']);unique(options,'ApprovalDecisionId')
                options=[{k:copy.deepcopy(r[k]) for k in SCHEMA['ApprovalDecision'].split(',')} for r in options]
                for decision in options:
                    decision['ApprovalDecisionId']=iid(decision['ApprovalDecisionId'])
                    if iid(decision['ApprovalDecisionListId'])!=list_id:raise ValueError('决策不属于原决策列表。')
                    decision['ApprovalDecisionName']=q.text(decision['ApprovalDecisionName'],'决策名称')
                    decision['ChangeCount']=integer(decision['ChangeCount'],'决策版本');decision['DecisionType']=integer(decision['DecisionType'],'决策原码')
                    for field in ('IsFrozen','IncludeComments'):decision[field]=i.wire_bool(decision[field],field)
                decisions_cache[list_id]=(definition,options)
            sheet['decision_list'],sheet['allowed_decisions']=copy.deepcopy(decisions_cache[list_id])
    return sheets

async def read_context(kind,name,org_name,key):
    q.text(name,'质量对象名称');q.text(org_name,'组织名称')
    if key is not None:iid(key)
    await schema_check(SCHEMA)
    before=await target(kind,name,key)
    org=await t.asset('organization',key=before['OrganizationId'],name=org_name,signature_txn_type=None)
    sheets=await sheets_for(before['Id'])
    inquiry=await q.object_details(before['Name'],before['Id']);detail=inquiry['QualityObjectInquiryDetail']
    if not set(CURRENT)<=detail.keys():raise ValueError('原生当前审批上下文不完整。')
    if integer(detail['Status'],'状态')!=before['Status'] or integer(detail['Category'],'类别')!=before['Category'] or iid(q.identifier(detail['Organization']))!=before['OrganizationId']:
        raise ValueError('审批Inquiry与原对象状态/组织不一致。')
    entries={e['ApprovalSheetEntryId']:e for s in sheets for e in s['entries']}
    sheet_ids={s['ApprovalSheetId'] for s in sheets}
    active=detail['EntryForApproval'];completion=detail['CompletionApprovalSheet']
    if active is not None and iid(q.identifier(active)) not in entries:raise ValueError('原生当前审批条目不在完整直接审批清单；可能为嵌套流程或读取变化。')
    if completion is not None and iid(q.identifier(completion)) not in sheet_ids:raise ValueError('原生完成审批表不在直接审批清单，不能猜测其归属。')
    if await target(kind,name,before['Id'])!=before or await t.asset('organization',key=org['InstanceID'],name=org_name,signature_txn_type=None)!=org or await sheets_for(before['Id'])!=sheets:
        raise ValueError('读取期间对象、组织策略或审批周期/条目/决策发生变化。')
    context={k:copy.deepcopy(detail[k]) for k in CURRENT}
    context['IsApprovalRequired']=i.wire_bool(detail['IsApprovalRequired'],'原生审批要求')
    # Keep only stable references and fields, never entire service/employee data.
    for field in ('Organization','Owner','Role','ProcessModel','CompletionApprovalSheet','EntryForApproval'):
        context[field]=None if detail[field] is None else {'instance_id':iid(q.identifier(detail[field]))}
    return dict(complete=True,ready_for_execution=False,scope='direct_quality_object_approval_sheets',target=before,organization=org,
        sheets=sheets,current_context=context,current_employee_entry_id=None if active is None else iid(q.identifier(active)),
        signature_policy=dict(sign_approval_txn=7839,sign_approval_required=org['CollectESigForAllQualityTxns'] or 7839 in org['QualityESigTxns'],
            route_approval_txn=8567,route_approval_required=org['CollectESigForAllQualityTxns'] or 8567 in org['QualityESigTxns'],dynamic_requirements_verified=False),
        actor_permission_verified=False,approval_action_performed=False,
        unverified_checks=['当前员工/角色/组织及代签资格','服务动态电子签名要求','嵌套流程审批及所有层级完成条件','路由/退回/撤回和签批的实际副作用及最终历史'],
        note='Routed候选、允许决策与原生EntryForApproval不是签署许可；不生成计划、不路由/清空/代签，不收集或保存口令。')

@mcp.tool
async def get_quality_approval_context(name:str,organization_name:str,object_kind:Literal['event','capa']='event',instance_id:str|None=None)->str:
    """只读质量Event/CAPA直接审批上下文：明确对象/组织，最多20表、单表100条/总200条、单列表50个决策；完整重读防变化。
    展示层级、原审批人/角色ID、状态、代签策略、允许决策及原生当前审批条目。组织签名要求只展示，不因需签名而阻断只读。
    不调用RouteApproval/SignApproval/SignatureApproval，包括execute=false；路由预校验可能清空原审批。候选不是授权，不输出/保存签名口令、不审批或自动关闭对象。
    """
    try:
        if object_kind not in ('event','capa'):raise ValueError('对象类型仅event/capa。')
        out=await asyncio.wait_for(read_context(object_kind,name,organization_name,instance_id),90)
        if len(q.encode(out))>78000:raise ValueError('审批上下文超过输出上限，拒绝部分结果。')
        return q.encode(out)
    except (ValueError,TypeError,KeyError,AttributeError,asyncio.TimeoutError) as exc:
        return q.encode(dict(complete=False,ready_for_execution=False,error=str(exc) or '审批读取超时。'))
