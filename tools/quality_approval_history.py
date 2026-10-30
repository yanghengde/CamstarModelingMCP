"""Bounded, exact-cycle native evidence for direct quality approval sheets."""
import asyncio
import copy
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from tools import mcp, quality as q, quality_approvals as a
from tools.collection_validation import timestamp_ticks
from tools.quality_spc_annotations import iid, schema_check

SCHEMA={
 'ApprovalSheetHistory':'ApprovalSheetHistoryId,ApprovalSheetId,QualityObjectId,ApprovalSheetEntryId,ApprovalCycleGMT,Status,GeneralInstructions,Comments,HistoryId,HistoryMainlineId,TxnId,ChangeCount',
 'SignApprovalHistory':'SignApprovalHistoryId,ApprovalSheetId,QualityObjectId,ApprovalSheetEntryId,ApprovalCycleGMT,ApproverId,ApproverRoleId,ApprovedById,ApprovalDecisionId,DecisionType,SheetLevel,Comments,SpecialInstructions,LastCompletedOnGMT,HistoryId,HistoryMainlineId,TxnId,ChangeCount'}
LIMITS={'ApprovalSheetHistory':100,'SignApprovalHistory':200}


def cycle_key(value):
    """GMT database columns may be naive; never truncate their seventh digit."""
    if not isinstance(value,str):raise ValueError('审批周期GMT须为时间字符串。')
    value=value.replace(' ','T')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?',value):value+='Z'
    ticks=timestamp_ticks(value)
    seconds,fraction=divmod(ticks,10000000)
    try:base=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(seconds=seconds)
    except OverflowError:raise ValueError('审批周期时间超出范围。') from None
    return f'{base.year:04d}-'+base.strftime('%m-%dT%H:%M:%S')+f'.{fraction:07d}Z'


async def rows_for(target_id,sheet_id,cycle=None):
    target_id=iid(target_id);sheet_id=iid(sheet_id)
    selected=None if cycle is None else cycle_key(cycle)
    where=f"ApprovalSheetId='{sheet_id}'"
    # Exact datetime2 equality, unlike the native inquiry's second-resolution match.
    if selected:where+=" AND ApprovalCycleGMT=CONVERT(datetime2(7),"+q.literal(selected[:-1])+",126)"
    result={}
    for table,columns in SCHEMA.items():
        maximum=LIMITS[table];key=table+'Id'
        rows=await q.sql_rows(f'SELECT TOP {maximum+1} '+columns+f' FROM {table} WHERE '+where+f' ORDER BY ApprovalCycleGMT,{key}',maximum+1)
        if len(rows)>maximum:raise ValueError('审批历史超过本次读取上限，请指定单个approval_cycle_gmt；不返回截断历史。')
        a.complete(rows,columns);a.unique(rows,key)
        clean=[]
        for raw in rows:
            row={k:copy.deepcopy(raw[k]) for k in columns.split(',')}
            for field in ('ApprovalSheetId','QualityObjectId',key):row[field]=iid(row[field])
            if row['ApprovalSheetId']!=sheet_id or row['QualityObjectId']!=target_id:raise ValueError('历史不属于原质量对象/审批表。')
            row['ApprovalCycleGMT']=cycle_key(row['ApprovalCycleGMT'])
            if selected and row['ApprovalCycleGMT']!=selected:raise ValueError('历史不属于明确审批周期。')
            for field in ('ApprovalSheetEntryId','HistoryId','HistoryMainlineId','TxnId'):
                row[field]=a.optional(row[field])
            row['ChangeCount']=a.integer(row['ChangeCount'],'历史版本')
            if table=='ApprovalSheetHistory':
                row['Status']=a.integer(row['Status'],'历史审批状态')
            else:
                if not row['ApprovalSheetEntryId']:raise ValueError('签批历史缺少原条目ID。')
                for field in ('ApproverId','ApproverRoleId','ApprovedById','ApprovalDecisionId'):row[field]=a.optional(row[field])
                for field in ('DecisionType','SheetLevel'):row[field]=a.integer(row[field],field)
                if row['LastCompletedOnGMT'] not in (None,''):row['LastCompletedOnGMT']=cycle_key(row['LastCompletedOnGMT'])
            clean.append(row)
        result[table]=sorted(clean,key=lambda r:(r['ApprovalCycleGMT'],r[key]))
    return result


async def read_history(args):
    kind=args['object_kind']
    if kind not in ('event','capa'):raise ValueError('对象类型仅event/capa。')
    sheet_id=iid(args['approval_sheet_id'])
    cycle=args.get('approval_cycle_gmt')
    if cycle is not None:cycle=cycle_key(cycle)
    await schema_check(SCHEMA)
    context=await a.read_context(kind,args['name'],args['organization_name'],args.get('instance_id'))
    sheet=next((s for s in context['sheets'] if s['ApprovalSheetId']==sheet_id),None)
    if sheet is None:raise ValueError('明确审批表不属于原质量对象。')
    target_id=context['target']['Id']
    rows=await rows_for(target_id,sheet_id,cycle)
    current=sheet['LastApprovalCycleGMT']
    current=None if current in (None,'') else cycle_key(current)
    cycles={r['ApprovalCycleGMT'] for values in rows.values() for r in values}
    if current and (cycle is None or current==cycle):cycles.add(current)
    if cycle is not None and cycle not in cycles:raise ValueError('明确周期在当前表及有界历史中均不存在。')
    grouped=[]
    for key in sorted(cycles,reverse=True):
        route=[r for r in rows['ApprovalSheetHistory'] if r['ApprovalCycleGMT']==key]
        sign=[r for r in rows['SignApprovalHistory'] if r['ApprovalCycleGMT']==key]
        grouped.append(dict(approval_cycle_gmt=key,is_current_cycle=key==current,
            sheet_history=route,decision_history=sign,
            routed_history_present=any(r['Status']==10 for r in route)))
    if (await rows_for(target_id,sheet_id,cycle)!=rows or
        await a.read_context(kind,args['name'],args['organization_name'],target_id)!=context):
        raise ValueError('读取期间原对象、审批表或历史发生变化，请重新读取。')
    return dict(complete=True,ready_for_execution=False,target=context['target'],approval_sheet_id=sheet_id,
        current_cycle_gmt=current,selected_cycle_gmt=cycle,cycles=grouped,
        row_counts={k:len(v) for k,v in rows.items()},limits=LIMITS,
        transaction_attribution_verified=False,retry_allowed=False,
        note='包含取消事务的原生历史，GMT精确到7位小数；旧条目即使已从当前表移除仍保留其历史ID。完整仅指本次有界读取，不证明某次MCP执行成功，不解除unknown占用。')


async def previous_cycle_evidence(context,sheet,expected_cycle):
    """Permit a native reset only when every current decision has durable evidence."""
    await schema_check(SCHEMA)
    cycle=cycle_key(expected_cycle)
    if cycle_key(sheet['LastApprovalCycleGMT'])!=cycle:raise ValueError('明确旧周期与原审批表不符。')
    rows=await rows_for(context['target']['Id'],sheet['ApprovalSheetId'],cycle)
    if not any(r['Status']==10 and r['TxnId'] and r['HistoryId'] for r in rows['ApprovalSheetHistory']):
        raise ValueError('旧周期缺少原生路由历史，不能开启新周期。')
    decisions={r['SignApprovalHistoryId']:r for r in rows['SignApprovalHistory']}
    rejected=False;cancelled=False
    for entry in sheet['entries']:
        typ=a.integer(entry['DecisionType'],'原决策类型')
        if entry['Status'] not in (0,30,40) or typ not in (10,20,30) or not entry['LastDecisionId']:
            raise ValueError('重提仅处理已完成决策或有原生取消历史的条目，不猜测未处理条目。')
        last=decisions.get(entry['LastDecisionId'])
        if not last or not last['TxnId'] or not last['HistoryId']:raise ValueError('旧决策指针没有同周期持久事务历史。')
        expected={'ApprovalSheetEntryId':entry['ApprovalSheetEntryId'],'DecisionType':typ,
                  'ApprovalDecisionId':entry['ApprovalDecisionId'],'ApprovedById':entry['ApprovedById'],
                  'ApproverId':entry['ApproverId'],'ApproverRoleId':entry['ApproverRoleId'],'SheetLevel':entry['SheetLevel']}
        if any(last[k]!=v for k,v in expected.items()):raise ValueError('旧决策历史与原条目/审批人/决策不一致。')
        if last['Comments']!=entry['Comments']:raise ValueError('旧审批评论未完整保存在同周期历史中，不能清理当前评论。')
        if typ==30:
            if entry['Status']!=0 or entry['ApprovedById'] or entry['ApprovalDecisionId']:raise ValueError('原生取消条目状态或决策不符。')
            cancelled=True
        else:
            if entry['Status']!={10:40,20:30}[typ] or not entry['ApprovalDecisionId'] or not entry['ApprovedById']:
                raise ValueError('已签批条目的状态/实际审批人不完整。')
            if cycle_key(entry['LastCompletedOnGMT'])!=last['LastCompletedOnGMT']:raise ValueError('旧签批完成时间与历史不一致。')
            rejected=rejected or typ==20
    if sheet['Status']==30 and not rejected or sheet['Status']==0 and not cancelled:
        raise ValueError('拒绝/取消结果缺少对应历史，不以普通Pending表模拟取消。')
    if await rows_for(context['target']['Id'],sheet['ApprovalSheetId'],cycle)!=rows:raise ValueError('旧周期证据读取期间变化。')
    return dict(approval_cycle_gmt=cycle,history=rows,history_preservation_verified=True,
                native_reset_required=True,transaction_attribution_verified=False)


async def prior_decision_evidence(context,sheet,entry):
    """ClearApprovals leaves LastDecision intact; validate that it is from an older cycle."""
    await schema_check(SCHEMA)
    key=iid(entry['LastDecisionId']);columns=SCHEMA['SignApprovalHistory']
    rows=await q.sql_rows('SELECT TOP 2 '+columns+f" FROM SignApprovalHistory WHERE SignApprovalHistoryId='{key}'",2)
    a.complete(rows,columns)
    if len(rows)!=1:raise ValueError('保留的旧决策指针没有唯一持久历史。')
    raw=rows[0]
    for field in ('TxnId','HistoryId','ApproverId','ApproverRoleId','ApprovedById','ApprovalDecisionId','HistoryMainlineId'):
        if raw[field] not in (None,''):iid(raw[field])
    a.integer(raw['ChangeCount'],'旧决策历史版本')
    for field,expected in [('SignApprovalHistoryId',key),('QualityObjectId',context['target']['Id']),
                           ('ApprovalSheetId',sheet['ApprovalSheetId']),('ApprovalSheetEntryId',entry['ApprovalSheetEntryId'])]:
        if iid(raw[field])!=expected:raise ValueError('保留的旧决策历史不属于原对象/表/条目。')
    if (cycle_key(raw['ApprovalCycleGMT'])>=cycle_key(sheet['LastApprovalCycleGMT']) or
        a.integer(raw['DecisionType'],'旧决策类型') not in (10,20,30) or not raw['TxnId'] or not raw['HistoryId']):
        raise ValueError('决策指针不是有事务依据的更早周期，不允许覆盖本周期签批。')
    return {k:copy.deepcopy(raw[k]) for k in columns.split(',')}


@mcp.tool
async def get_quality_approval_history(name:str,organization_name:str,approval_sheet_id:str,
        object_kind:Literal['event','capa']='event',instance_id:str|None=None,approval_cycle_gmt:str|None=None)->str:
    """只读直接Event/CAPA审批周期及路由/签批/取消历史。固定原对象与表ID，可精确指定approval_cycle_gmt（GMT，保留7位小数）。
    单次最多100条表历史、200条签批历史；超限拒绝部分结果，须指定单周期。两次完整重读防变化，不按秒合并周期、不隐藏取消历史。
    展示旧/当前周期、原审批人/决策/评论/条目及事务ID。历史不等于本次执行归因，不审批、不清空历史、不解除unknown、不允许重试。
    """
    try:
        out=await asyncio.wait_for(read_history(dict(name=name,organization_name=organization_name,approval_sheet_id=approval_sheet_id,
            object_kind=object_kind,instance_id=instance_id,approval_cycle_gmt=approval_cycle_gmt)),90)
        if len(q.encode(out))>78000:raise ValueError('审批历史超过输出上限，请指定单周期。')
        return q.encode(out)
    except (ValueError,TypeError,KeyError,AttributeError,asyncio.TimeoutError) as exc:
        return q.encode(dict(complete=False,ready_for_execution=False,error=str(exc) or '审批历史读取超时。'))
