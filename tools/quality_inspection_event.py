"""Source-bound, separate Event transaction after an audited failed inspection.

Never replays CollectSamplingData or changes Employee.PrimaryOrganization.
Preparation is read-only: CreateEvent descendants can execute during preview.
"""
import json
import re

from tools import mcp, quality as q, quality_plans as p, quality_inspection_history as ih
from tools import quality_event_creation as creation, quality_event_triage as triage
from tools import quality_event_lifecycle as lifecycle, quality_ncr_escalation as escalation
from tools import quality_disposition_history as history
from tools.quality_definitions import resources
from tools.process_context import ReadContext
from tools.quality_lot_execution import runtime_claim
from tools.collection_validation import timestamp_valid, timestamp_ticks
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

KIND='inspection_event'
SERVICE='CreateProductionEvent'
TYPE=8341
EVENT=('EventId,EventName,CDOTypeId,ChangeCount,Status,Category,OrganizationId,ClassificationId,SubClassificationId,'
       'OwnerId,RoleId,PriorityLevelId,IsFrozen,IsSubmitted,ProcessModelId,ChecklistId,EventDataId,BriefDescription,Description,'
       'ReporterId,InitiatorId,OccurrenceDateGMT,ReportedDateGMT')
SUMMARY='CreateEventHistoryId,HistoryId,HistoryMainlineId,TxnId,IntegrationTxnId,CDOTypeId,OrganizationId,ClassificationId,SubClassificationId,Submit'
LOT='EventLotId,EventDataId,ContainerId,Lot,IsContainer,PlaceOnHold,Qty,QtySampled,QtyDefective'
XREF='QualityCrossReferenceId,TrackingId,CrossReferenceId,CDOTypeId'


def supported(snap):return snap.get('kind')==KIND and snap.get('inspection_event_version')==1


def source_key(plan_id,txn):return 'MCP-INSPECTION:'+plan_id+':'+txn


async def source(args):
    key=args['inspection_plan_id'];fingerprint=args['inspection_plan_digest']
    if not re.fullmatch('[0-9a-f]{32}',key)or not re.fullmatch('[0-9a-f]{64}',fingerprint):raise ValueError('须指定原检验计划ID及摘要。')
    with p.db_connect()as db:
        row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(key,)).fetchone()
        attempt=db.execute('SELECT attempt_id,started,finished FROM attempts WHERE plan_id=?',(key,)).fetchone()
    snap=p.checked(row,fingerprint)
    if row[2]!='history_committed'or not attempt or not ih.supported(snap):raise ValueError('仅对已按原生父子历史及原任务核对成功的计数扣量检验创建事件。')
    outcome=json.loads(row[4]);audit=outcome.get('history_attestation',{})
    txn=audit.get('candidate_txn_id')
    if not outcome.get('verified_success')or audit.get('action')!='resolve_history'or not q.valid_id(txn):raise ValueError('原检验缺少已核实原生事务证据。')
    attempt=dict(zip(('attempt_id','started','finished'),attempt))
    proof=await ih.verify(snap,attempt,txn);checks,current=await ih.current_evidence(snap)
    if not proof.get('recorded_business_effects_verified')or not all(checks.values()):raise ValueError('原检验历史、容器或原任务已不匹配，不能创建关联事件。')
    summary=proof['evidence']['summary']
    if summary['SamplingStatus']!='Fail'or snap['preview']['rejects_this_submission']<1:raise ValueError('仅对MES原生Fail且本次有不良的检验创建事件，不由MCP替代质量判定。')
    root=next(r for r in proof['evidence']['native']['mainlines']if str(r['TxnType'])=='8452')
    if root['HistoryMainlineId']!=summary['HistoryMainlineId']:raise ValueError('原检验父汇总关联不符。')
    return dict(plan_id=key,plan_digest=fingerprint,txn_id=txn,key=source_key(key,txn),container_id=snap['target_id'],container_name=snap['name'],
        sample_test_id=summary['SampleTestId'],summary_id=summary['CollectSamplingDataHistoryId'],root_id=root['HistoryMainlineId'],
        occurrence_utc=sql_utc(root['TxnDateGMT']),samples=snap['preview']['samples_this_submission'],rejects=snap['preview']['rejects_this_submission'],
        evidence_digest=p.digest({'history':proof['evidence'],'current':current}),current=current)


def sql_utc(value):
    if not isinstance(value,str):raise ValueError('原生GMT时间未知。')
    value=value.replace(' ','T')
    if not value.endswith('Z'):value+='Z'
    if not timestamp_valid(value):raise ValueError('原生GMT时间格式未支持。')
    return value


async def duplicates(key):
    # key consists exclusively of a fixed prefix and immutable hex identifiers.
    return await q.sql_rows('SELECT TOP 2 EventId,EventName,Status FROM Event WHERE Description LIKE '+q.literal('%'+key+'%')+' ORDER BY EventId',2)


async def contract():
    doc=json.loads(await request('GET','/swagger/'+SERVICE+'/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json))
    try:
        s=doc['components']['schemas'];root=s[SERVICE]['properties'];detail=s['EventDetail']['properties']
        for f in ('organization','classification','subClassification'):
            if root[f]['$ref'].split('/')[-1]!='NamedObjectRef':raise ValueError()
        for f in ('submit','holdContainers','isManuallyPrintLabels','printLabels'):
            if root[f]['type']!='boolean'or root[f].get('readOnly'):raise ValueError()
        if root['containers']['items']['$ref'].split('/')[-1]!='ContainerRef'or root['serviceDetail']['$ref'].split('/')[-1]!='EventDetail':raise ValueError()
        if detail['eventDataDetail']['$ref'].split('/')[-1]!='EventDataDetails':raise ValueError()
        if not {'problemDescription','sampleQuantity'}<=s['EventDataDetails']['properties'].keys():raise ValueError()
    except (KeyError,TypeError,ValueError)as exc:raise ValueError('当前关联事件原生输入契约不完整。')from exc
    return p.digest({name:s[name]for name in (SERVICE,'EventDetail','EventDataDetails')})


async def organization_extras(org):
    data=await ReadContext(2).get('Organizations',org['InstanceID'],{'$select':'InstanceID,Name,QualityProcessingMap,CategoryMap,PEDisallowedTxns',
        '$expand':'QualityProcessingMap,CategoryMap'})
    if q.identifier(data)!=org['InstanceID']or q._has_next_link(data):raise ValueError('组织附加配置不完整。')
    for f in ('QualityProcessingMap','CategoryMap','PEDisallowedTxns'):
        if data.get(f)!=[]:raise ValueError('此入口仅支持无流程、分流和受禁事务附加配置的基础关联事件组织。')
    return {f:data[f]for f in ('QualityProcessingMap','CategoryMap','PEDisallowedTxns')}


async def numbering(org):
    # Modeling returns transient NumberingRuleMap view IDs on this deployment.
    # Bind the persistent table row, not an ID that changes on every GET.
    sql=('SELECT NumberingRuleMapId,OrganizationId,Category,NumberingRuleId,IsFrozen,ChangeCount '
        'FROM NumberingRuleMap WHERE OrganizationId='+q.literal(org['InstanceID'])+' AND Category=3 ORDER BY NumberingRuleMapId')
    rows=await q.sql_rows(sql,2)
    native=await lifecycle.numbering(org,category=3)
    if (len(rows)!=1 or not q.valid_id(rows[0]['NumberingRuleMapId'])or rows[0]['OrganizationId']!=org['InstanceID']
        or ih.integer(rows[0]['Category'])!=3 or ih.boolean(rows[0]['IsFrozen'])
        or rows[0]['NumberingRuleId']!=native['rule']['InstanceID']or await q.sql_rows(sql,2)!=rows):
        raise ValueError('事件编号映射的持久身份/版本未完整核实。')
    # Quality event naming evaluates prefix/suffix expressions, unlike the
    # Container Start path. Restrict this bridge to explicit quoted literals.
    for field in ('Prefix','Suffix'):
        value=native['rule'].get(field)
        if value not in (None,'')and (not isinstance(value,str)or not re.fullmatch(r'"[^"\\\r\n]*"',value)):
            raise ValueError('生产事件编号前后缀须为空或双引号包围的字面值，不执行任意编号表达式。')
    return {k:v for k,v in native.items()if k!='mapping_id'}|{'persistent_mapping':rows[0]}


def body_for(args,marker,src,refs):
    ref=lambda k:triage.ref(refs[k])
    occurrence=src['occurrence_utc'];reported=lifecycle.utc(args['reported_date'])
    description=(args['description']+'\n容器 '+src['container_name']+'：本次检验 '+str(src['samples'])+'，不良 '+str(src['rejects'])
        +'\n'+src['key']+'\nInspection digest: '+src['plan_digest']+'\nNative summary: '+src['summary_id'])
    return dict(organization=ref('organization'),classification=ref('classification'),subClassification=ref('subclassification'),
        containers=[{'name':src['container_name'],'instanceId':src['container_id']}],submit=True,holdContainers=False,
        isManuallyPrintLabels=True,printLabels=False,comments=marker+' '+src['key'],
        serviceDetail=dict(briefDescription=args['brief_description'],description=description,
            reporter=ref('reporter'),reporterOrganization=ref('organization'),initiator=ref('initiator'),initiatorOrganization=ref('organization'),
            priorityLevel=ref('priority'),occurrenceDate=occurrence,occurrenceDateGMT=occurrence,reportedDate=reported,reportedDateGMT=reported,
            eventDataDetail=dict(problemDescription=src['key'],sampleQuantity=str(src['samples']))))


async def build(args,marker):
    creation.u.text_value(args['description'],'关联事件说明',3400)
    src=await source(args)
    if await duplicates(src['key']):raise ValueError('原检验已有来源标记关联事件，不能用新计划重复创建。')
    if not timestamp_valid(args['reported_date'])or timestamp_ticks(args['reported_date'])<timestamp_ticks(src['occurrence_utc']):raise ValueError('报告时间须含明确时区且不能早于原检验。')
    # Read-only validation; no nonexecuting CreateEvent/ProductionEvent request.
    check=json.loads(await creation.validate_quality_event_creation(event_name='',automatic_numbering=True,
        **{k:args[k]for k in ('organization_name','classification_name','subclassification_name','priority_level_name','brief_description','reporter_name','description')},
        occurrence_date=src['occurrence_utc'],reported_date=args['reported_date']))
    if not check.get('static_valid')or not check.get('complete')or check.get('signature_required'):raise ValueError('关联事件创建资料/组织配置不满足要求：'+str(check.get('error')or check.get('errors')))
    org=await triage.asset('organization',name=args['organization_name'],signature_txn_type=TYPE)
    mapping=check['classification_mapping'];policy=check['notification_policy']
    if policy['smtp_configured']or policy['entries']:raise ValueError('此入口不发送通知；须使用没有SMTP及通知项的组织，不能清空现有策略。')
    if not mapping['Owner']or not mapping['Role']or mapping['ChecklistTemplate']:raise ValueError('须有唯一直接责任人/角色映射且无检查表配置。')
    refs=check['references']|{'reporter':check['reporter'],'initiator':await triage.asset('owner',name=args['initiator_name']),
        'owner':await triage.asset('owner',key=mapping['Owner']),'role':await triage.asset('role',key=mapping['Role'])}
    group=None
    if mapping['FailureModeGroup']:
        key=mapping['FailureModeGroup'];data=await ReadContext(1).get('FailureModeGroups',key,{'$select':'Name,InstanceID,IsFrozen'})
        versions=await q.sql_rows("SELECT FailureModeGroupId,ChangeCount FROM FailureModeGroup WHERE FailureModeGroupId='"+key+"'",1)
        if q.identifier(data)!=key or data.get('IsFrozen')is not False or len(versions)!=1:raise ValueError('失败组配置身份或冻结状态未核实。')
        group={**data,'ChangeCount':versions[0]['ChangeCount']}
    numbers=await numbering(org);extras=await organization_extras(org);schema=await contract()
    body=body_for(args,marker,src,refs)
    if await source(args)!=src or await duplicates(src['key']):raise ValueError('准备期间原检验或关联事件发生变化。')
    reads=set()
    entities={k:value[0]for k,value in escalation.CONFIG.items()}|{'reporter':'Employees','initiator':'Employees','owner':'Employees','role':'Roles'}
    for k,v in refs.items():reads.update(resources(v.get('entity')or entities[k],v))
    reads.update(resources('NumberingRules',numbers['rule']))
    if group:reads.update(resources('FailureModeGroups',group))
    return dict(kind=KIND,inspection_event_version=1,action='create_followup',service=SERVICE,name=src['container_name'],target_id=src['container_id'],
        event_org_id=org['InstanceID'],scope_key='quality-inspection-event:'+src['txn_id'],source=src,before=check,references=refs,
        organization=org,numbering=numbers,failure_group=group,organization_extras=extras,body=body,arguments=args,marker=marker,environment=p.environment(),
        preview=dict(contract_digest=schema,native_preview_requested=False,source_inspection_verified=True,separate_transaction=True,native_creation_permission_verified=False,
            automatic_numbering=True,notifications_enabled=False,native_event_creation_verified=False,
            note='只准备独立后续事件；原检验已完成，不重发检验或扣量，不修改账号主组织。事件原生提交为Active，保留原生签名校验。'),
        resource_reads=sorted(reads),resource_writes=[runtime_claim('container',src['container_id']),'runtime:quality-event:org:'+org['InstanceID'],
            'runtime:inspection-event:source:'+src['txn_id']])


def validate_transport(snap):
    if (not supported(snap)or snap.get('service')!=SERVICE or snap.get('action')!='create_followup'
        or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker',''))):raise ValueError('检验关联事件计划种类/服务/标记不符。')
    src=snap['source']
    if (src['key']!=source_key(src['plan_id'],src['txn_id'])or src['plan_id']!=snap['arguments']['inspection_plan_id']
        or src['plan_digest']!=snap['arguments']['inspection_plan_digest']or snap['target_id']!=src['container_id']
        or snap['name']!=src['container_name']or snap['scope_key']!='quality-inspection-event:'+src['txn_id']
        or snap['event_org_id']!=snap['references']['organization']['InstanceID']
        or any(type(snap['body'].get(k))is not bool for k in ('submit','holdContainers','isManuallyPrintLabels','printLabels'))
        or snap['body']!=body_for(snap['arguments'],snap['marker'],src,snap['references'])):
        raise ValueError('关联事件输入与不可变检验来源不一致。')


async def roots(snap,offset=0,limit=20):
    validate_transport(snap);q.paging(offset,limit)
    rows=await q.sql_rows('SELECT '+history.MAINLINE+' FROM HistoryMainline WHERE TxnType=8341 AND Comments='+q.literal(snap['body']['comments'])
        +f' ORDER BY HistoryMainlineId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY',limit+1)
    return rows[:limit],len(rows)>limit


async def transaction(root):
    txn=history.identifier(root['TxnId']);eid=history.identifier(root['HistoryId']);mid=history.identifier(root['HistoryMainlineId'])
    mainlines=await q.sql_rows('SELECT '+history.MAINLINE+" FROM HistoryMainline WHERE TxnId='"+txn+"' ORDER BY HistoryMainlineId",21)
    summaries=await q.sql_rows('SELECT '+SUMMARY+" FROM CreateEventHistory WHERE HistoryMainlineId='"+mid+"' AND TxnId='"+txn+"' ORDER BY CreateEventHistoryId",2)
    rows=await q.sql_rows('SELECT '+EVENT+" FROM Event WHERE EventId='"+eid+"'",1)
    if len(rows)!=1:raise ValueError('原生创建历史指向的Event不存在。')
    event=rows[0];did=history.identifier(event['EventDataId'])
    data=await q.sql_rows("SELECT EventDataId,EventId,ProblemDescription,SampleQuantity FROM EventData WHERE EventDataId='"+did+"'",1)
    lots=await q.sql_rows('SELECT '+LOT+" FROM EventLot WHERE EventDataId='"+did+"' ORDER BY EventLotId",2)
    refs=await q.sql_rows('SELECT '+XREF+" FROM QualityCrossReference WHERE TrackingId='"+eid+"' OR CrossReferenceId='"+eid+"' ORDER BY QualityCrossReferenceId",4)
    event_history=await q.sql_rows('SELECT '+history.MAINLINE+" FROM HistoryMainline WHERE HistoryId='"+eid+"' ORDER BY HistoryMainlineId",21)
    if len(mainlines)>20 or len(event_history)>20:raise ValueError('事件事务/历史超出核对边界。')
    return dict(root=root,mainlines=mainlines,summaries=summaries,event=event,event_data=data,lots=lots,cross_references=refs,event_history=event_history)


def compare(snap,attempt,evidence):
    root=evidence['root'];summaries=evidence['summaries'];row=evidence['event'];src=snap['source'];refs=snap['references'];body=snap['body'];detail=body['serviceDetail']
    eid=row['EventId'];txn=root['TxnId'];mid=root['HistoryMainlineId'];integer=ih.integer;boolean=ih.boolean
    checks=dict(attempt_time_window=history.attempt_time_matches(attempt,root),
        exact_event_root=(root['HistoryId']==eid and integer(root['TxnType'])==TYPE and root['TxnTypeName']==SERVICE
            and integer(root['ReversalStatus'])==1 and root['Comments']==body['comments']),
        single_creation_summary=len(summaries)==1,
        no_other_transaction_roots=len(evidence['mainlines'])==1 and evidence['mainlines'][0]==root,
        no_later_event_history=evidence['event_history']==[root],
        event_identity=(integer(row['CDOTypeId'])==7489 and bool(row['EventName'])and integer(row['Status'])==1 and integer(row['Category'])==3
            and not boolean(row['IsFrozen'])and not row['ProcessModelId']and not row['ChecklistId']),
        event_assignment=all(row[col]==refs[k]['InstanceID']for col,k in [('OrganizationId','organization'),('ClassificationId','classification'),
            ('SubClassificationId','subclassification'),('OwnerId','owner'),('RoleId','role'),('PriorityLevelId','priority'),('ReporterId','reporter'),('InitiatorId','initiator')]),
        source_text_and_times=(row['BriefDescription']==detail['briefDescription']and row['Description']==detail['description']
            and timestamp_ticks(sql_utc(row['OccurrenceDateGMT']))==timestamp_ticks(detail['occurrenceDateGMT'])
            and timestamp_ticks(sql_utc(row['ReportedDateGMT']))==timestamp_ticks(detail['reportedDateGMT'])),
        source_event_data=(len(evidence['event_data'])==1 and evidence['event_data'][0]['EventDataId']==row['EventDataId']
            and evidence['event_data'][0]['EventId']==eid and evidence['event_data'][0]['ProblemDescription']==src['key']
            and str(evidence['event_data'][0]['SampleQuantity'])==str(src['samples'])),
        original_container_link=(len(evidence['lots'])==1 and evidence['lots'][0]['EventDataId']==row['EventDataId']
            and evidence['lots'][0]['ContainerId']==src['container_id']and evidence['lots'][0]['Lot']==src['container_name']
            and boolean(evidence['lots'][0]['IsContainer'])and not boolean(evidence['lots'][0]['PlaceOnHold'])),
        original_container_cross_reference=(len(evidence['cross_references'])==1 and evidence['cross_references'][0]['TrackingId']==eid
            and evidence['cross_references'][0]['CrossReferenceId']==src['container_id']))
    if len(summaries)==1:
        s=summaries[0]
        checks['creation_history_inputs']=(s['HistoryId']==eid and s['HistoryMainlineId']==mid and s['TxnId']==txn and boolean(s['Submit'])
            and all(s[f]==refs[k]['InstanceID']for f,k in [('OrganizationId','organization'),('ClassificationId','classification'),('SubClassificationId','subclassification')]))
    return checks


async def verify(snap,attempt,candidate_txn_id=None):
    found,more=await roots(snap,limit=1)
    if more or not found:return dict(comparison='ambiguous'if more else'no_records',complete=False,request_correlation_verified=False)
    root=found[0]
    if candidate_txn_id is not None and candidate_txn_id!=root['TxnId']:return dict(comparison='candidate_mismatch',complete=False,request_correlation_verified=False)
    evidence=await transaction(root);checks=compare(snap,attempt,evidence)
    if await roots(snap,limit=1)!=(found,False)or await transaction(root)!=evidence:raise ValueError('关联事件历史读取期间变化。')
    match=all(checks.values())
    return dict(comparison='native_inspection_event_match'if match else'mismatch',complete=True,candidate_txn_id=root['TxnId'],
        request_correlation_verified=match,recorded_business_effects_verified=match,checks=checks,evidence=evidence,separate_transaction=True)


async def current_evidence(snap,txn_id):
    if not q.valid_id(txn_id):raise ValueError('须指定关联事件原事务ID。')
    found,more=await roots(snap,limit=1)
    if more or not found or found[0]['TxnId']!=txn_id:raise ValueError('关联事件原事务不唯一。')
    evidence=await transaction(found[0]);current_source=await source(snap['arguments'])
    checks=dict(original_inspection_preserved=current_source==snap['source'],unique_source_event=len(await duplicates(snap['source']['key']))==1)
    return checks,dict(source=current_source,event=evidence['event'],event_data=evidence['event_data'],lots=evidence['lots'],cross_references=evidence['cross_references'])


@mcp.tool
async def prepare_inspection_quality_event(inspection_plan_id:str,inspection_plan_digest:str,organization_name:str,
        classification_name:str,subclassification_name:str,priority_level_name:str,brief_description:str,reporter_name:str,
        initiator_name:str,reported_date:str,description:str='')->str:
    """已核实失败检验的独立关联质量事件计划。原计数扣量检验必须history_committed且原生Fail，本次有不良，当前原任务仍精确匹配。
    明确组织/分类/子分类/优先级/报告人与发现人、带时区报告时间；发生时间取原检验GMT历史。要求自动编号、直接Owner/Role、无流程/检查表/通知/签名要求。
    准备只读，不调用创建预览。确认后一次原生CreateProductionEvent，Submit=true、HoldContainers=false，不打印，不改账号主组织。
    事件保存来源计划摘要/原生汇总/事务标记并原生关联容器；独立事务失败不重做检验、扣量或自动重试。unknown须原计划历史核对。
    """
    return await p.prepare(KIND,dict(inspection_plan_id=inspection_plan_id,inspection_plan_digest=inspection_plan_digest,
        organization_name=organization_name,classification_name=classification_name,subclassification_name=subclassification_name,
        priority_level_name=priority_level_name,brief_description=brief_description,reporter_name=reporter_name,initiator_name=initiator_name,
        reported_date=reported_date,description=description))
