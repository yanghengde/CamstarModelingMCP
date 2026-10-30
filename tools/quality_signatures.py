"""Single current-actor QualityESigDetail only; generic ESig/multi-signing stays blocked."""
import copy
import re
from tools import quality as q,quality_approvals as a
from tools.quality_spc_annotations import iid,schema_check


def requested(args):
    value=args.get('require_quality_esign',False)
    if type(value) is not bool:raise ValueError('require_quality_esign须为明确布尔值。')
    return value


def contract(doc):
    try:
        schemas=doc['components']['schemas'];root=schemas['SignApproval']['properties']
        if root['qualityESigDetail']['$ref'].split('/')[-1]!='QualityESigDetail' or root['processESignatures']['type']!='boolean':raise ValueError()
        detail=schemas['QualityESigDetail']['properties']
        if detail['signer']['$ref'].split('/')[-1]!='NamedObjectRef' or detail['signerPassword']['type']!='string' or detail['userRequired']['type']!='boolean':raise ValueError()
        allowed={'signer','signerPassword','userRequired','displayName','instanceID','parent','historyDetail','smartBarcode'}
        if set(detail)-allowed:raise ValueError()
        envelope=doc['paths']['/api/SignApproval/PerformEvent']['post']['requestBody']['content']['application/json']['schema']
        props=schemas[envelope['$ref'].split('/')[-1]]['properties']
        if props['inputData']['$ref'].split('/')[-1]!='SignApproval' or schemas[props['event']['$ref'].split('/')[-1]]['properties']['eventName']['type']!='string':raise ValueError()
    except (KeyError,TypeError,ValueError):raise ValueError('当前QualityESigDetail及固定LoadESigDetails契约未核实。') from None


def requirement(data,actor,organization):
    detail=data.get('QualityESigDetail')
    if not isinstance(detail,dict) or 'UserRequired' not in detail:raise ValueError('原生质量签名要求未完整加载。')
    user_required=a.i.wire_bool(detail['UserRequired'],'签名用户策略')
    if user_required!=organization['UserRequiredForEsig']:raise ValueError('原生质量签名用户策略与组织不一致。')
    if detail.get('Signer') is not None and iid(q.identifier(detail['Signer']))!=actor['InstanceID']:raise ValueError('原生签署员工不是当前实际审批员工。')
    # Never retain the service detail, even if the server overreturns credential fields.
    return dict(kind='quality_transaction',signer_id=actor['InstanceID'],signer_name=actor['Name'],user_required=user_required,
                verification='native_VerifyPasswordSignature_on_single_submission',credential_in_plan=False)


def payload(snapshot,credential):
    requirement=snapshot['quality_signature']
    body=copy.deepcopy(snapshot['body'])
    body['qualityESigDetail']=dict(signer=dict(name=requirement['signer_name'],instanceId=requirement['signer_id']),
        signerPassword=credential.password(),userRequired=requirement['user_required'])
    return body


def display(snapshot,digest):
    obj=snapshot['before']['context']['target'];requirement=snapshot['quality_signature']
    shown={'对象':obj['Name'],'对象ID':obj['Id'],'组织':snapshot['before']['organization']['Name'],
        '审批表':snapshot['before']['sheet']['ApprovalSheetName'],'审批条目ID':snapshot['before']['entry']['ApprovalSheetEntryId'],
        '审批周期':snapshot['before']['sheet']['LastApprovalCycleGMT'],'决定':snapshot['before']['decision']['ApprovalDecisionName'],
        '实际签署员工（MES会话）':requirement['signer_name'],'签署员工ID':requirement['signer_id'],
        '审批说明':snapshot['arguments']['comments'],'计划摘要':digest,'MES接口':snapshot['environment']['shopfloor']}
    proof=snapshot.get('actor_authorization') or {}
    if proof.get('delegation_verified'):
        shown.update({'原转派发起人':proof['originally_assigned_approver']['Name'],'原生转派历史ID':proof['delegation_history']['DelegateTaskHistoryId'],
            '当前审批人ID':proof['current_approver_id']})
    return shown


async def history_evidence(snapshot):
    from tools import quality_approval_history as h
    obj=snapshot['before']['context']['target'];sheet=snapshot['before']['sheet']
    await schema_check({'HistoryMainline':'HistoryMainlineId,TxnId,TxnType,QualityESigConfirmed,ChangeCount'})
    rows=await h.rows_for(obj['Id'],sheet['ApprovalSheetId'],sheet['LastApprovalCycleGMT'])
    result=[]
    for row in rows['SignApprovalHistory']:
        if row['ApprovalSheetEntryId']!=snapshot['before']['entry']['ApprovalSheetEntryId']:continue
        txn=iid(row['TxnId'])
        mains=await q.sql_rows('SELECT TOP 21 HistoryMainlineId,TxnId,TxnType,QualityESigConfirmed,ChangeCount '+
            f"FROM HistoryMainline WHERE TxnId='{txn}' ORDER BY HistoryMainlineId",21)
        if len(mains)>20:raise ValueError('签名事务主历史超过20条，不返回部分证据。')
        a.complete(mains,'HistoryMainlineId,TxnId,TxnType,QualityESigConfirmed,ChangeCount');a.unique(mains,'HistoryMainlineId')
        for main in mains:
            if iid(main['TxnId'])!=txn:raise ValueError('签名事务主历史关联不符。')
            if a.integer(main['TxnType'],'事务类型')!=7839:continue
            exact=row['HistoryMainlineId'] is not None and iid(row['HistoryMainlineId'])==iid(main['HistoryMainlineId'])
            if row['HistoryMainlineId'] is not None and not exact:continue
            result.append(dict(sign_history_id=row['SignApprovalHistoryId'],original_approver_id=row['ApproverId'],actual_approved_by_id=row['ApprovedById'],
                mainline_id=iid(main['HistoryMainlineId']),txn_id=txn,mainline_association_verified=exact,
                quality_esig_confirmed=a.i.wire_bool(main['QualityESigConfirmed'],'原生质量签名标记') if exact else None,
                change_count=a.integer(main['ChangeCount'],'历史版本')))
    return dict(candidates=result,verified_success=False,note='原周期/条目及原生QualityESigConfirmed仅供人工最终审计，不自动归因本次请求或解除unknown。')
