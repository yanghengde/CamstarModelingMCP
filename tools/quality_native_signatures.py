"""NCRUpdate generic password signatures. Unknown/subtype-less contracts fail closed.

The archived standard REST schema does NOT expose ESigPasswordCapture. The typed
submit adapter below also requires verified native authentication mapping;
offline adapter fixtures are not evidence that a deployed server supports it.
"""
import copy
import json
from pydantic import BaseModel,ConfigDict,Field
from tools import quality as q,quality_inspection as i
from tools.modeling_details import raw_json,_has_next_link
from tools.process_context import ReadContext
from tools.quality_spc_annotations import iid
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL


class NativeSignatureAssignment(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    requirement_detail_id:str=Field(min_length=16,max_length=16)
    signer_name:str=Field(min_length=1,max_length=100)
    cosigner_name:str|None=Field(default=None,min_length=1,max_length=100)
    cosign_reason_name:str|None=Field(default=None,min_length=1,max_length=100)


def requested(args):
    flag=args.get('require_native_esign',False)
    if type(flag) is not bool:raise ValueError('require_native_esign须为明确布尔值。')
    if args.get('native_signature_assignments') and not flag:raise ValueError('指定通用签名人员须明确require_native_esign=true。')
    return flag


def native_execution_support():
    """Do not enable authentication merely because a JSON subtype is exposed.

    Standard ESigPasswordCapture.Validate does not explicitly forward CSPassword
    or ESigReqDetail to Shopfloor.VerifyPasswordSignature. Neither a deployed
    mapping nor actual positive/negative authentication evidence is available.
    There is deliberately no environment, user or MCP override for this gate.
    """
    return dict(verified=False,blocker='原生主签署/协签认证参数映射尚未核实；即使Swagger暴露口令子类型，也不能创建签名执行计划。须先完成正式服务适配及原生正反向验收。')


def contract(doc):
    """Return actionable, non-secret findings; never invent a type discriminator."""
    findings=[];load_ok=False
    try:
        s=doc['components']['schemas'];root=s['NCRUpdate']['properties']
        if root['eSigDetails']['items']['$ref'].split('/')[-1]!='ESigServiceDetail' or root['processESignatures']['type']!='boolean':raise ValueError()
        envelope=doc['paths']['/api/NCRUpdate/PerformEvent']['post']['requestBody']['content']['application/json']['schema']
        e=s[envelope['$ref'].split('/')[-1]]['properties']
        if e['inputData']['$ref'].split('/')[-1]!='NCRUpdate' or s[e['event']['$ref'].split('/')[-1]]['properties']['eventName']['type']!='string':raise ValueError()
        load_ok=True
    except (KeyError,TypeError,ValueError):findings.append('NCRUpdate固定LoadESigDetails事件契约未完整暴露。')
    try:
        s=doc['components']['schemas'];detail=s['ESigServiceDetail']['properties']
        if detail['eSigReqDetail']['$ref'].split('/')[-1]!='NamedSubentityRef' or detail['captureDetails']['items']['$ref'].split('/')[-1]!='ESigPasswordCapture':raise ValueError()
        cap=s['ESigPasswordCapture']['properties']
        for key in ('signerPassword','cosignerPassword'):
            if cap[key]['type']!='string' or cap[key].get('readOnly',False):raise ValueError()
        for key in ('signer','cosigner','cosignReason'):
            if cap[key]['$ref'].split('/')[-1]!='NamedObjectRef':raise ValueError()
        allowed={'signer','cosigner','cosignReason','signerPassword','cosignerPassword','comments','displayName','instanceID','parent','smartBarcode','historyDetail','signerFullName','cosignerFullName'}
        if set(cap)-allowed:raise ValueError()
    except (KeyError,TypeError,ValueError):findings.append('缺少明确的ESigPasswordCapture集合及SignerPassword/CosignerPassword可写契约；不能向基础ESigCaptureDetail添加口令。')
    return dict(load_contract_verified=load_ok,typed_password_submit_contract_verified=not findings,blockers=findings,
        service='NCRUpdate',native_execution_support=native_execution_support(),actual_signature_verified=False,ready_for_execution=False)


def ref(raw):
    if not isinstance(raw,dict):raise ValueError('签名原生引用缺失。')
    return dict(name=q.text(raw.get('Name')),instanceId=iid(q.identifier(raw)))


async def named(ctx,entity,name):
    # Explicit select: Employee contains credential fields that must never load.
    select='InstanceID,Name,IsFrozen'+(',ESigRoleGroup' if entity=='Employees' else '')
    listing=await ctx.get(entity,params={'$select':select,'$filter':"Name eq '"+q.text(name).replace("'","''")+"'",'$top':2})
    rows=listing.get('value')
    if not isinstance(rows,list) or len(rows)!=1 or _has_next_link(listing):raise ValueError('签名人员/原因名称未唯一匹配。')
    key=iid(q.identifier(rows[0]));raw=await ctx.get(entity,key,{'$select':select})
    if q.identifier(raw)!=key or raw.get('Name')!=name or _has_next_link(raw) or i.wire_bool(raw.get('IsFrozen'),'签名配置冻结状态'):
        raise ValueError('签名人员/原因不符、冻结或不完整。')
    result=dict(ref=ref(raw),role_ids=[])
    if entity=='Employees' and raw.get('ESigRoleGroup') is not None:
        group_id=iid(q.identifier(raw['ESigRoleGroup']))
        group=await ctx.get('ESigRoleGroups',group_id,{'$select':'InstanceID,Name,IsFrozen,ResolvedEntries','$expand':'ResolvedEntries($select=InstanceID,Name,IsFrozen)'})
        entries=group.get('ResolvedEntries')
        if (q.identifier(group)!=group_id or _has_next_link(group) or i.wire_bool(group.get('IsFrozen'),'签名角色组冻结状态')
            or not isinstance(entries,list) or len(entries)>100):raise ValueError('签名角色组未完整解析。')
        result.update(role_group_id=group_id,role_ids=sorted(iid(q.identifier(x)) for x in entries))
        if len(set(result['role_ids']))!=len(entries) or any(i.wire_bool(x.get('IsFrozen'),'签名角色冻结状态') for x in entries):raise ValueError('签名角色组重复或存在冻结角色。')
    return result


async def load(before,body,doc,bound):
    from tools import quality_plans as p
    from tools.quality_ncr_escalation import container_state
    report_id=iid(before['NonconformanceReportId'])
    if i.wire_int(before['NCRStatus'],'NCR状态')!=1 or before.get('CloseDateGMT') not in (None,'') or before.get('EscalatedEventId') not in (None,''):
        raise ValueError('通用签名首次接入仅未关闭、未升级的Open NCR更新。')
    container=await container_state(before['ContainerId'])
    if container['CurrentHoldCount']!=0:raise ValueError('冻结容器的NCR不在本通用签名更新范围。')
    raw=await request('POST','/api/NCRUpdate/PerformEvent',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body={'event':{'eventName':'LoadESigDetails'},'inputData':body},
        params={'$select':'NonconformanceReport,ServiceDetail,Container,Employee,Factory,Comments,ProcessESignatures,ESigRequirement,AdditionalESigReqs,ESigDetails',
                '$expand':'ServiceDetail,ESigDetails($select=ESigReqDetail,SignatureCount,VerificationMethod,Role,CosignerRole,Meaning,CaptureDetails)'},
        bound_headers=bound,require_stable_session=True,response_formatter=raw_json)
    try:outer=json.loads(raw);data=outer.get('data',outer)
    except (ValueError,TypeError,AttributeError):raise ValueError('原生通用签名要求加载失败。') from None
    if (not isinstance(data,dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error') or data.get('ErrorCode')
        or _has_next_link(data) or not p.wire_matches(body,data) or iid(q.identifier(data.get('NonconformanceReport')))!=report_id
        or iid(q.identifier(data.get('Container')))!=container['ContainerId']):raise ValueError('原生签名要求、原报告/容器或完整回显不符。')
    actor=ref(data.get('Employee'));factory=ref(data.get('Factory'));requirements=[]
    if 'ESigRequirement' not in data or 'AdditionalESigReqs' not in data:raise ValueError('签名要求清单缺失。')
    if data['ESigRequirement'] is not None:requirements.append(ref(data['ESigRequirement']))
    extra=data['AdditionalESigReqs']
    if extra is not None and (not isinstance(extra,list) or len(extra)>5):raise ValueError('附加签名要求超过范围或不完整。')
    requirements.extend(ref(x) for x in extra or [])
    if not 1<=len(requirements)<=5 or len({x['instanceId'] for x in requirements})!=len(requirements):raise ValueError('签名要求为空、重复或超过5组，不无要求加签。')
    ctx=ReadContext(60);details=[]
    for requirement in requirements:
        root=await ctx.get('ESigRequirements',requirement['instanceId'],{'$select':'InstanceID,Name,IsFrozen,ESigReqDetails','$expand':'ESigReqDetails($select=InstanceID,Name,Parent,IsFrozen,Role,CosignerRole,Meaning,SignatureCount,VerificationMethod)'})
        rows=root.get('ESigReqDetails')
        if ref(root)!=requirement or i.wire_bool(root.get('IsFrozen'),'签名要求冻结状态') or _has_next_link(root) or not isinstance(rows,list) or not 1<=len(rows)<=5:
            raise ValueError('签名要求定义未完整读取或已冻结。')
        for row in rows:
            if iid(q.identifier(row.get('Parent')))!=requirement['instanceId'] or i.wire_bool(row.get('IsFrozen'),'签名明细冻结状态'):raise ValueError('签名要求明细归属不符或冻结。')
            method=i.wire_int(row.get('VerificationMethod'),'签名验证方法');count=i.wire_int(row.get('SignatureCount'),'签名数量')
            if method!=1 or not 1<=count<=5:raise ValueError('仅支持PasswordVerification1，单项签名数量1至5。')
            details.append(dict(detail=ref(row),requirement=requirement,role=ref(row.get('Role')),cosigner_role=ref(row['CosignerRole']) if row.get('CosignerRole') is not None else None,
                meaning=ref(row.get('Meaning')),signature_count=count,verification_method=method))
    if len(details)>5 or sum(x['signature_count'] for x in details)>5 or len({x['detail']['instanceId'] for x in details})!=len(details):raise ValueError('一次事务最多5项/5次主签名，不返回部分要求。')
    echoed=data.get('ESigDetails')
    if not isinstance(echoed,list) or len(echoed)!=len(details):raise ValueError('原生签名服务明细未完整加载。')
    for item in details:
        matches=[e for e in echoed if iid(q.identifier(e.get('ESigReqDetail')))==item['detail']['instanceId']]
        if len(matches)!=1:raise ValueError('原生签名明细缺失或重复。')
        e=matches[0]
        for f,k in [('SignatureCount','signature_count'),('VerificationMethod','verification_method')]:
            if i.wire_int(e.get(f),f)!=item[k]:raise ValueError('原生签名数量/验证方法与定义不符。')
        for f,k in [('Role','role'),('CosignerRole','cosigner_role'),('Meaning','meaning')]:
            if (ref(e[f]) if e.get(f) is not None else None)!=item[k]:raise ValueError('原生签名角色/含义与定义不符。')
        if 'CaptureDetails' not in e or e['CaptureDetails'] not in (None,[]):raise ValueError('原生已有签名采集，不覆盖或合并未知输入。')
    return dict(kind='generic_ncr_update',actor=actor,factory=factory,container=container,
        requirements=sorted(details,key=lambda x:x['detail']['instanceId']),credential_in_plan=False,native_authentication_verified=False)


async def document():
    raw=await request('GET','/swagger/NCRUpdate/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json)
    try:return json.loads(raw)
    except (ValueError,TypeError):raise ValueError('当前NCRUpdate签名契约不可读取。') from None


async def context(before):
    from core.signature_channel import verified_channel
    from tools.quality_approval_authorization import capture_headers
    with verified_channel():
        doc=await document();check=contract(doc)
        if not check['load_contract_verified']:return check
        body={'nonconformanceReport':{'name':before['NonconformanceReportName'],'instanceId':before['NonconformanceReportId']},'processESignatures':True}
        current=await load(before,body,doc,capture_headers('NCRUpdate'))
        if await q.ncr_snapshot('',before['NonconformanceReportId'])!=before:raise ValueError('签名要求读取期间NCR变化。')
        return dict(check,requirements=current,complete=True,ready_for_execution=False,note='仅签名要求，不是口令验证或执行许可。具体人员须明确选择，不自动代选。')


async def build(args,marker,execution_binding=None):
    from core.signature_channel import verified_channel
    from core.quality_signature_vault import VAULT
    from core.quality_signature_ui import ensure_running
    from tools import quality_plans as p
    from tools.quality_approval_authorization import capture_headers
    if args['action']!='update':raise ValueError('本轮通用签名仅接入NCRUpdate；不处理创建/关闭/升级或质量事件审批。')
    with verified_channel():
        doc=await document();check=contract(doc)
        if not check['typed_password_submit_contract_verified']:raise ValueError(' '.join(check['blockers']))
        support=native_execution_support()
        if support['verified'] is not True:raise ValueError(support['blocker'])
        snap=await p.build_ncr(args,marker)
        body=copy.deepcopy(snap['body']);body['processESignatures']=True
        bound=capture_headers('NCRUpdate');requirements=await load(snap['before'],body,doc,bound)
        assignments=args.get('native_signature_assignments')
        if not isinstance(assignments,list) or not 1<=len(assignments)<=5:raise ValueError('须先读取NCR通用签名要求，再明确1至5条主签署人分配。')
        ctx=ReadContext(100);captures=[];participants=[];used=set()
        for index,raw in enumerate(assignments):
            assignment=NativeSignatureAssignment.model_validate(raw);key=iid(assignment.requirement_detail_id)
            detail=next((d for d in requirements['requirements'] if d['detail']['instanceId']==key),None)
            if detail is None:raise ValueError('签署位置不属于原生当前签名要求。')
            signer=await named(ctx,'Employees',assignment.signer_name)
            unique=(detail['requirement']['instanceId'],signer['ref']['instanceId'])
            if unique in used:raise ValueError('同一签名要求内主签署员工不能重复，即使分配到不同明细。')
            used.add(unique);cosigner=reason=None
            if assignment.cosigner_name:
                if not assignment.cosign_reason_name or not detail['cosigner_role']:raise ValueError('协签须明确原配置协签角色及协签原因。')
                cosigner=await named(ctx,'Employees',assignment.cosigner_name);reason=await named(ctx,'ESigCosignReasons',assignment.cosign_reason_name)
                if cosigner['ref']['instanceId']==signer['ref']['instanceId'] or detail['cosigner_role']['instanceId'] not in cosigner['role_ids']:raise ValueError('协签人不得等于主签署人且须具备原ESigRoleGroup协签角色。')
            elif assignment.cosign_reason_name or detail['role']['instanceId'] not in signer['role_ids']:raise ValueError('无协签时主签署人须具备原ESigRoleGroup角色，不能单独填写协签原因。')
            for role,employee in [('signer',signer),('cosigner',cosigner)]:
                if employee:participants.append(dict(slot_id=f'{index}:{role}',employee_id=employee['ref']['instanceId'],employee_name=employee['ref']['name'],
                    position='主签署人' if role=='signer' else '协签人',meaning=detail['meaning']['name'],requirement=detail['requirement']['name'],detail_id=key))
            captures.append(dict(detail_id=key,signer=signer,cosigner=cosigner,reason=reason,signer_slot=f'{index}:signer',cosigner_slot=f'{index}:cosigner' if cosigner else None))
        if any(sum(c['detail_id']==d['detail']['instanceId'] for c in captures)!=d['signature_count'] for d in requirements['requirements']):raise ValueError('签署人数须逐项精确匹配原生SignatureCount，不能遗漏或额外加签。')
        if await load(snap['before'],body,doc,bound)!=requirements or await q.ncr_snapshot('',snap['before']['NonconformanceReportId'])!=snap['before']:
            raise ValueError('准备期间签名要求或NCR变化。')
        # Re-read each employee/group/reason after requirements; no cached authority.
        for c in captures:
            for entity,key in [('Employees','signer'),('Employees','cosigner'),('ESigCosignReasons','reason')]:
                if c[key] and await named(ReadContext(6),entity,c[key]['ref']['name'])!=c[key]:raise ValueError('准备期间签名员工、角色组或原因变化。')
        if not VAULT.origin:await ensure_running()
        VAULT.ensure_enabled()
        signature=dict(requirements,captures=captures,participants=participants,transport_contract_digest=p.digest(doc['components']['schemas']),
            verification='native_ESigPasswordCapture_on_single_submission')
        snap.update(body=body,quality_signature=signature)
        snap['preview'].update(verified_input_echo=body,employee_id=requirements['actor']['instanceId'],generic_signature_required=True,
            native_signature_authenticated=False,typed_signature_contract_verified=True)
        snap.setdefault('resource_reads',[]).extend('modeling:ESigRequirements:id:'+d['requirement']['instanceId'] for d in requirements['requirements'])
        for c in captures:
            for entity,key in [('Employees','signer'),('Employees','cosigner'),('ESigCosignReasons','reason')]:
                if c[key]:
                    from tools.quality_definitions import resources
                    r=c[key]['ref'];snap['resource_reads'].extend(resources(entity,{'Name':r['name'],'InstanceID':r['instanceId']}))
                    if c[key].get('role_group_id'):snap['resource_reads'].append('modeling:ESigRoleGroups:id:'+c[key]['role_group_id'])
        snap['resource_reads']=sorted(set(snap['resource_reads']))
        snap.setdefault('resource_writes',[]).append('runtime:container:id:'+requirements['container']['ContainerId'])
        if execution_binding is not None:execution_binding.update(headers=bound,actor_id=requirements['actor']['instanceId'])
        return snap


def payload(snapshot,credential):
    signature=snapshot['quality_signature'];values=credential.passwords();body=copy.deepcopy(snapshot['body'])
    try:
        if set(values)!={p['slot_id'] for p in signature['participants']}:raise ValueError('多人签名输入与固定位置不符。')
        details=[]
        for requirement in signature['requirements']:
            item=dict(eSigReqDetail=dict(requirement['detail'],parent={'instanceId':requirement['requirement']['instanceId']}),captureDetails=[])
            for c in signature['captures']:
                if c['detail_id']!=requirement['detail']['instanceId']:continue
                cap=dict(signer=c['signer']['ref'],signerPassword=values[c['signer_slot']])
                if c['cosigner']:
                    cap.update(cosigner=c['cosigner']['ref'],cosignerPassword=values[c['cosigner_slot']],cosignReason=c['reason']['ref'])
                item['captureDetails'].append(cap)
            details.append(item)
        body['eSigDetails']=details
        return body
    finally:values.clear()


def clear_payload(body):
    if not isinstance(body,dict):return
    for detail in body.get('eSigDetails',[]):
        for capture in detail.get('captureDetails',[]):capture.clear()
        detail.clear()
    body.pop('eSigDetails',None)


def display(snapshot,digest):
    signature=snapshot['quality_signature']
    return {'对象':snapshot['name'],'对象ID':snapshot['target_id'],'操作':'更新NCR','执行员工（MES会话）':signature['actor']['name'],
        '拟更新内容':json.dumps(snapshot['body'].get('serviceDetail',{}),ensure_ascii=False),'签名要求数':len(signature['requirements']),
        '口令输入人数/位置数':len(signature['participants']),'计划摘要':digest,'MES接口':snapshot['environment']['shopfloor']}


def validate_transport(snapshot):
    sig=snapshot.get('quality_signature');body=snapshot['body']
    if (snapshot.get('kind')!='ncr' or snapshot.get('action')!='update' or snapshot.get('service')!='NCRUpdate'
        or not requested(snapshot['arguments']) or not isinstance(sig,dict) or sig.get('kind')!='generic_ncr_update'
        or set(body)!={'comments','nonconformanceReport','serviceDetail','processESignatures'} or body['processESignatures'] is not True
        or body['nonconformanceReport']['instanceId']!=snapshot['target_id'] or sig['actor']['instanceId']!=snapshot['preview']['employee_id']
        or not sig.get('participants') or len(sig['participants'])>10):raise ValueError('通用签名服务、原报告、执行员工或固定输入位置不符。')


async def inspect(snapshot):
    from tools.quality_spc_annotations import schema_check
    from core.signature_channel import verified_channel
    columns={'HistoryMainline':'HistoryMainlineId,HistoryId,TxnId,ContainerId,TxnServiceName,Comments',
        'ESigHistorySummary':'ESigHistorySummaryId,HistoryMainlineId,HistoryId,TxnId,ESigReqDetailId,SignatureCount,VerificationMethod',
        'ESigHistoryDetail':'ESigHistoryDetailId,ESigHistorySummaryId,HistoryId,TxnId,SignerId,CosignerId,CosignReasonId'}
    with verified_channel():
        await schema_check(columns)
        cid=iid(snapshot['quality_signature']['container']['ContainerId'])
        query=('SELECT TOP 101 h.HistoryMainlineId,h.HistoryId,h.TxnId,h.ContainerId,h.TxnServiceName,h.Comments,'
            's.ESigHistorySummaryId,s.ESigReqDetailId,s.SignatureCount,s.VerificationMethod,'
            'd.ESigHistoryDetailId,d.SignerId,d.CosignerId,d.CosignReasonId '
            'FROM HistoryMainline h JOIN ESigHistorySummary s ON s.HistoryMainlineId=h.HistoryMainlineId AND s.HistoryId=h.HistoryId AND s.TxnId=h.TxnId '
            'JOIN ESigHistoryDetail d ON d.ESigHistorySummaryId=s.ESigHistorySummaryId AND d.HistoryId=s.HistoryId AND d.TxnId=s.TxnId '
            f"WHERE h.ContainerId='{cid}' AND h.TxnServiceName='NCRUpdate' AND h.Comments="+q.literal(snapshot['body']['comments'])+
            ' ORDER BY h.HistoryMainlineId,s.ESigHistorySummaryId,d.ESigHistoryDetailId')
        rows=await q.sql_rows(query,101)
        if len(rows)>100:raise ValueError('通用签名候选历史超过100条，不返回部分证据。')
        seen=set();clean=[]
        for r in rows:
            if not isinstance(r,dict) or r.get('ContainerId')!=cid or r.get('TxnServiceName')!='NCRUpdate' or r.get('Comments')!=snapshot['body']['comments']:
                raise ValueError('签名候选历史不属于原容器/服务/标记。')
            for f in ('HistoryMainlineId','HistoryId','TxnId','ESigHistorySummaryId','ESigReqDetailId','ESigHistoryDetailId','SignerId'):iid(r.get(f))
            if r['ESigHistoryDetailId'] in seen:raise ValueError('签名候选历史重复。')
            seen.add(r['ESigHistoryDetailId'])
            for f in ('CosignerId','CosignReasonId'):
                if r.get(f) is not None:iid(r[f])
            if i.wire_int(r.get('VerificationMethod'),'历史验证方法')!=1:raise ValueError('签名历史验证方法不符。')
            item={k:r[k] for k in ('HistoryMainlineId','HistoryId','TxnId','ESigHistorySummaryId','ESigReqDetailId','ESigHistoryDetailId','SignerId','CosignerId','CosignReasonId')}
            item['SignatureCount']=i.wire_int(r.get('SignatureCount'),'历史签名数量');clean.append(item)
        current=await q.ncr_snapshot('',snapshot['target_id'])
    return dict(current_record=current,generic_signature_history=clean,planned_participants=snapshot['quality_signature']['participants'],
        verified_success=False,state_changed=False,retry_allowed=False,history_causal_chain_verified=False,
        note='仅原容器/事务/摘要/明细关联候选；尚未核实请求到最终NCR事务因果链，不自动认定成功或解占用。')
