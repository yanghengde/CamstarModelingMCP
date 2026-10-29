"""Read-only closure diagnostics; never grants permission to close an Event."""
import copy
import json
from tools import mcp, quality as q, quality_event_triage as t, quality_inspection as i
from tools.quality_spc_annotations import schema_check, iid

CLOSE_TYPE=7712
SCHEMA={k:v for k,v in t.SCHEMA.items() if k in ('Event','Organization','ApprovalSheet','NonconformanceReport')}
FIELDS=('Status','Category','Organization','Owner','Role','ProcessModel','IsApprovalRequired',
        'CompletionApprovalStatus','CompletionApprovalSheet','EntryForApproval')


@mcp.tool
async def get_quality_event_closure_context(event_name:str,organization_name:str,event_instance_id:str|None=None)->str:
    """只读基础Event关闭前置诊断，需明确事件/组织，可绑定原ID。汇总状态、提交/分流、CAR、流程/检查表、审批、签名及最多20条关联NCR。
    查询原生Close处置候选，不准备计划、不关闭事件或关联NCR、不绕过流程/审批/签名。complete仅表示本次读取完整；ready_for_execution恒false。
    CAR要求不等于缺失CAR，存在流程不等于未完成；批次处置、CAR关联、流程完成及动态权限仍须原生校验，不据空清单宣称可关闭。
    """
    try:
        q.text(event_name,'事件名称');q.text(organization_name,'组织名称')
        if event_instance_id is not None:iid(event_instance_id)
        await schema_check(SCHEMA)
        before=await t.event_record(event_name,event_instance_id)
        if not before or before['CDOTypeId']!=t.EVENT_TYPE:raise ValueError('仅诊断明确存在的基础Event，不跟随同名替代对象。')
        org=await t.asset('organization',name=organization_name,key=before['OrganizationId'],signature_txn_type=-1)
        blockers=[]
        def block(code,detail):blockers.append(dict(code=code,detail=detail))
        for condition,code,message in [
            (before['Status']!=1,'status','当前不在受限关闭候选的Active状态。'),
            (before['IsFrozen'],'frozen','原事件已冻结。'),
            (not before['IsSubmitted'],'not_submitted','原事件尚未提交。'),
            (not before['TriageComplete'],'not_triaged','尚未完成首次分流，当前入口不自动分流。'),
            (before['IsCARRequiredToClose'],'car_review','要求CAR/CAPA关联；需核实有效关联，不能据此断言缺失。'),
            (bool(before['ProcessModelId'] or before['OrigProcessModelTemplateId']),'process_review','存在流程或原流程模板，完成状态尚未核实，不自动绕过。'),
            (bool(before['ChecklistId'] or before['ChecklistSaved']),'checklist_review','存在检查表上下文，完成情况须另行核实。'),
            (bool(before['CloseDateGMT']),'close_information','已有关闭时间，不能重复准备关闭。'),
            (CLOSE_TYPE in org['QualityESigTxns'],'signature','组织要求关闭事务电子签名。')]:
            if condition:block(code,message)
        approvals=await q.sql_rows(f"SELECT TOP 1 ApprovalSheetId FROM ApprovalSheet WHERE ParentId='{before['EventId']}'",1)
        if approvals:block('approval_review','存在直接审批表，需核实审批顺序及最终状态。')
        links=await q.sql_rows('SELECT TOP 21 NonconformanceReportId,EscalatedEventId,ContainerId,ChangeCount '
            f"FROM NonconformanceReport WHERE EscalatedEventId='{before['EventId']}' ORDER BY NonconformanceReportId",21)
        if len(links)>20:raise ValueError('关联NCR超过20条，不返回部分关系作为完整诊断。')
        for row in links:
            for field in ('NonconformanceReportId','EscalatedEventId','ContainerId'):row[field]=iid(row.get(field),field)
            row['ChangeCount']=i.wire_int(row.get('ChangeCount'),'NCR版本')
            if row['EscalatedEventId']!=before['EventId'] or row['ChangeCount']<0:raise ValueError('关联NCR身份/版本不符。')
        if len({r['NonconformanceReportId'] for r in links})!=len(links):raise ValueError('关联NCR重复。')
        inquiry=await q.object_details(before['EventName'],before['EventId'])
        detail=inquiry['QualityObjectInquiryDetail']
        if not all(k in detail for k in FIELDS):raise ValueError('质量查询状态/审批字段不完整。')
        for key in ('Status','Category'):
            if i.wire_int(detail[key],key)!=before[key]:raise ValueError('质量查询与原事件状态不一致。')
        if q.identifier(detail['Organization'])!=before['OrganizationId']:raise ValueError('查询组织不一致。')
        if i.wire_bool(detail['IsApprovalRequired'],'审批要求'):block('approval_required','原生查询要求审批。')
        options=await q.closure_options({'qualityObject':{'name':before['EventName'],'instanceId':before['EventId']},
                                         'organization':t.ref(org)})
        close=[r for r in options if r['Name']=='Close']
        if len(close)!=1:block('close_option','未返回唯一Close处置候选，不猜测原码。')
        if await t.event_record(event_name,before['EventId'])!=before or await t.asset('organization',name=organization_name,key=before['OrganizationId'],signature_txn_type=-1)!=org:
            raise ValueError('诊断期间事件或组织策略变化。')
        return q.encode({'complete':True,'ready_for_execution':False,'original_event_id':before['EventId'],
            'event':before,'organization':org,'blockers':blockers,'linked_ncrs':links,
            'inquiry_context':{k:copy.deepcopy(detail[k]) for k in FIELDS},'resolution_options':options,
            'unverified_checks':['CAR/CAPA有效关联','流程最终完成及检查表响应','全部关联批次处置','当前账号原生关闭权限','关闭审批/签名动态校验','部署关闭副作用及NCR联动'],
            'note':'无静态阻塞不代表可关闭；没有保存计划或业务提交，不自动关闭关联NCR。'})
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        return q.encode({'complete':False,'ready_for_execution':False,'error':str(exc)})
