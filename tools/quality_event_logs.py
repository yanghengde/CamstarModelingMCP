"""Append one Event log with the complete native list and immutable old values.

UpdateSubentityList receives the entire list. Omitting old items can delete them;
omitting old dates/authors can resolve current defaults. Never do either.
"""
import copy
import json
import re
from tools import mcp, quality as q, quality_plans as p
from tools import quality_event_data as e, quality_event_update as u
from tools import quality_approval_authorization as auth
from tools.quality_definitions import resources
from tools.quality_spc_annotations import iid, schema_check
from tools.collection_validation import timestamp_valid, timestamp_ticks
from core.http_client import request
from core.signature_channel import verified_channel
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SERVICE='UpdateEventLogs'
SERVICE_TYPE=8761
COLUMNS='EventLogId,EventDataId,CDOTypeId,ChangeCount,Comments,EmployeeId,CommentTypeId,CreationDate,CreationDateGMT'
FIELDS=('Comments','Employee','CommentType','CreationDate','CreationDateGMT','EventLog')
SELECT='QualityObject,Organization,Comments,Employee,User,AvailableActions,IgnoreProcessModel,ESigRequirement,AdditionalESigReqs,ESigDetails,QualityESigDetail,QualityObjectDetail,Classification,SubClassification,DataCollectionDef,ParametricData,EventLogDetails'
EXPAND='QualityObjectDetail($select='+u.CURRENT_FIELDS+'),EventLogDetails($select=CDOTypeId,'+','.join(FIELDS)+')'


async def records(data_id):
    data_id=iid(data_id)
    await schema_check({'EventLog':COLUMNS})
    rows=await q.sql_rows('SELECT TOP 101 '+COLUMNS+f" FROM EventLog WHERE EventDataId='{data_id}' ORDER BY EventLogId",101)
    if not isinstance(rows,list) or len(rows)>100:raise ValueError('日志超过100条或不完整，不提交部分清单。')
    clean=[];seen=set()
    for row in rows:
        if not isinstance(row,dict) or not set(COLUMNS.split(','))<=row.keys():raise ValueError('日志字段缺失。')
        item={f:copy.deepcopy(row[f]) for f in COLUMNS.split(',')}
        key=iid(item['EventLogId'])
        if key in seen or iid(item['EventDataId'])!=data_id or u.i.wire_int(item['CDOTypeId'],'日志类型')!=8367:
            raise ValueError('日志重复、归属或类型不符。')
        item.update(EventLogId=key,EventDataId=data_id,CDOTypeId=8367,ChangeCount=u.i.wire_int(item['ChangeCount'],'日志版本'))
        if item['ChangeCount']<0:raise ValueError('日志版本无效。')
        # Missing historical identity/date would resolve to today's actor/time.
        item['EmployeeId']=iid(item['EmployeeId'])
        if item['CommentTypeId'] not in (None,''):item['CommentTypeId']=iid(item['CommentTypeId'])
        if item['Comments'] is not None:u.text_value(item['Comments'],'原日志',4000)
        if any(not isinstance(item[f],str) or not item[f] for f in ('CreationDate','CreationDateGMT')):raise ValueError('原日志时间缺失，不补当前时间。')
        seen.add(key);clean.append(item)
    return sorted(clean,key=lambda r:r['EventLogId'])


async def source(args,expected_data_id=None):
    before=await e.source(args,expected_data_id,log_append=True)
    before['logs']=await records(before['data']['EventDataId'])
    return before


async def contract():
    raw=await request('GET','/swagger/'+SERVICE+'/swagger.json',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=u.raw_json)
    doc=json.loads(raw);schemas=doc['components']['schemas'];root=schemas[SERVICE]['properties'];detail=schemas['EventLogDetail']['properties']
    for path,model in [('/api/'+SERVICE,SERVICE),('/api/'+SERVICE+'/PerformEvent',SERVICE+'ShopfloorPerformEventModel')]:
        ref=doc['paths'][path]['post']['requestBody']['content']['application/json']['schema']['$ref']
        if ref.split('/')[-1]!=model:raise ValueError('日志服务输入契约不符。')
    model=schemas[SERVICE+'ShopfloorPerformEventModel']['properties']
    if model['inputData']['$ref'].split('/')[-1]!=SERVICE or model['event']['$ref'].split('/')[-1]!='EventToPerform':raise ValueError('Load契约不符。')
    if root['eventLogDetails'].get('type')!='array' or root['eventLogDetails']['items']['$ref'].split('/')[-1]!='EventLogDetail':raise ValueError('日志清单契约不符。')
    for field in ('qualityObject','organization','employee','user'):
        if root[field]['$ref'].split('/')[-1]!='NamedObjectRef':raise ValueError('日志根引用契约不符。')
    expected={'comments':'string','creationDate':'string','creationDateGMT':'string','employee':'NamedObjectRef','commentType':'NamedObjectRef','eventLog':'SubentityRef'}
    if set(detail)-set(expected)-e.SYSTEM:raise ValueError('日志明细存在未知扩展。')
    for field,kind in expected.items():
        prop=detail[field]
        if (prop.get('type') if kind=='string' else prop.get('$ref','').split('/')[-1])!=kind:raise ValueError('日志明细字段类型不符。')
        if field.startswith('creationDate') and prop.get('format')!='date-time':raise ValueError('日志时间契约不符。')
    return dict(digest=p.digest(dict(root=root,detail=detail,model=model)),ordinary_preview_allowed=False)


def loaded_logs(data,before):
    values=data.get('EventLogDetails');old={r['EventLogId']:r for r in before['logs']};result={}
    if not isinstance(values,list) or len(values)!=len(old):raise ValueError('Load日志清单不完整，不补空清单。')
    for value in values:
        if not isinstance(value,dict) or not set(FIELDS)<=value.keys() or u.i.wire_int(value.get('CDOTypeId'),'日志明细类型')!=8371:raise ValueError('Load日志字段缺失或类型不符。')
        if set(value)-set(FIELDS)-{'CDOTypeId','InstanceId','InstanceID','Parent','DisplayName','HistoryDetail','SmartBarcode'}:raise ValueError('Load含未知日志字段。')
        key=iid(q.identifier(value['EventLog']))
        if key not in old or key in result:raise ValueError('Load日志引用重复或替换。')
        row=old[key]
        if type(value['Comments']) is not type(row['Comments']) or value['Comments']!=row['Comments']:raise ValueError('原日志内容不符。')
        item={'eventLog':{'instanceId':key},'comments':value['Comments']}
        for field in ('Employee','CommentType'):
            target=row[field+'Id'];actual=value[field]
            if target in (None,''):
                if actual is not None:raise ValueError('日志类别派生默认值。')
                item[e.wire(field)]=None
            else:
                if not isinstance(actual,dict) or iid(q.identifier(actual))!=target:raise ValueError('日志作者或类别不符。')
                item[e.wire(field)]=dict(name=q.text(actual.get('Name')),instanceId=target)
        for field in ('CreationDate','CreationDateGMT'):
            if not timestamp_valid(value[field]) or not u.date_match(row[field],value[field],field.endswith('GMT')):raise ValueError('原日志日期不符。')
            item[e.wire(field)]=value[field]
        if timestamp_ticks(value['CreationDate'])!=timestamp_ticks(value['CreationDateGMT']):raise ValueError('原日志Local/GMT不是同一时刻。')
        result[key]=item
    return [result[key] for key in sorted(result)]


async def load(before,marker,bound):
    body=e.root_body(before,marker)
    raw=await request('POST','/api/'+SERVICE+'/PerformEvent',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
        body={'event':{'eventName':'Load'},'inputData':body},params={'$select':SELECT,'$expand':EXPAND},
        bound_headers=bound,require_stable_session=True,response_formatter=u.raw_json)
    data=u.parse_reply(raw)
    # This service has no RegulatoryReportDetails field; source independently
    # proves absence in the persistent RegulatoryReport table.
    actor=e.verify_context(data,before,body,check_regulatory=False)
    if iid(q.identifier(data.get('User')))!=actor:raise ValueError('原生日志作者User与已核实当前员工不一致。')
    return dict(logs=loaded_logs(data,before),employee_id=actor)


def body_for(before,original,args,marker):
    u.text_value(args['log_text'],'新增处理日志',4000,True)
    u.text_value(args['comments'],'日志追加说明',200,True)
    body=e.root_body(before,marker,args['comments'])
    # Entire original list is preserved. New item has no EventLog reference;
    # native current user and system date expressions supply its provenance.
    body['eventLogDetails']=copy.deepcopy(original)+[dict(comments=args['log_text'],commentType=None)]
    return body


async def build(args,marker,execution_binding=None):
    try:
        u.text_value(args['log_text'],'新增处理日志',4000,True);u.text_value(args['comments'],'日志追加说明',200,True)
        with verified_channel():
            before=await source(args);schema=await contract();bound=auth.capture_headers(SERVICE)
            if len(before['logs'])>=100:raise ValueError('追加后超出100条完整核对范围。')
            original=await load(before,marker,bound)
            proof=await auth.membership(original['employee_id'],before['organization'],before['role'])
            if await source(args)!=before or await load(before,marker,bound)!=original or await auth.membership(original['employee_id'],before['organization'],before['role'])!=proof:
                raise ValueError('日志/原事件/当前授权在准备期间变化。')
            reads=set()
            for key in ('organization','owner','role','priority'):reads.update(resources(before[key]['entity'],before[key]))
            reads.update(e.c.read_resources(before['classification_configuration']))
            if execution_binding is not None:execution_binding.update(headers=bound,actor_id=original['employee_id'])
            row=before['event'];data_id=before['data']['EventDataId']
            return dict(kind='event_log_append',action='append_log',service=SERVICE,name=row['EventName'],target_id=row['EventId'],
                scope_key='quality-event:id:'+row['EventId'],before=before,original_logs=original['logs'],actor_authorization=proof,
                body=body_for(before,original['logs'],args,marker),arguments=args,marker=marker,environment=p.environment(),
                resource_reads=sorted(reads),resource_writes=['runtime:quality-event:id:'+row['EventId'],'runtime:event-data:id:'+data_id],
                preview=dict(native_contract=schema,employee_id=original['employee_id'],static_preview_only=True,ordinary_preview_requested=False,
                    existing_log_count=len(original['logs']),append_count=1,author_and_time='native_current_user_and_system_time',comment_type=None,
                    verified_success=False,existing_history_details_may_be_reposted=True,
                    note='完整保留旧日志内容/作者/时间/引用，追加一条无类别日志；不提供改删/补录日期/代签。原生逐条Execute可能为旧项再记本次历史，最终效果待验收。'))
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise ValueError('日志原值、完整清单、服务契约或当前责任人授权未核实；未提交，不回显远端异常正文。') from None


def validate_transport(snap):
    row=snap['before']['event']
    if (snap.get('kind')!='event_log_append' or snap.get('action')!='append_log' or snap.get('service')!=SERVICE
        or snap.get('target_id')!=row['EventId'] or snap.get('name')!=row['EventName']
        or row['CDOTypeId']!=7489 or row['Category']!=1 or row['Status']!=1 or row['IsSubmitted'] is not True or row['IsFrozen'] is not False
        or snap['before']['data']['EventId']!=row['EventId'] or snap['before']['data']['EventDataId']!=row['EventDataId']
        or len(snap['before']['logs'])>=100
        or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap.get('marker',''))):raise ValueError('日志计划服务/身份不符。')
    # Revalidate the immutable originals against their SQL snapshot as well as
    # rebuilding the sole allowed append; callers cannot remove old references.
    original=snap['original_logs']
    frame={'EventLogDetails':[{'CDOTypeId':8371,**{('EventLog' if k=='eventLog' else k[0].upper()+k[1:]):
        ({'Name':v.get('name'),'InstanceId':v['instanceId']} if isinstance(v,dict) else v) for k,v in item.items()}} for item in original]}
    if loaded_logs(frame,snap['before'])!=original or snap['body']!=body_for(snap['before'],original,snap['arguments'],snap['marker']):raise ValueError('日志原值或追加载荷不符。')


def preservation(before,after):
    old={r['EventLogId']:r for r in before};now={r['EventLogId']:r for r in after}
    # Native list processing may increment ChangeCount even for preserved items.
    fields=[f for f in COLUMNS.split(',') if f!='ChangeCount']
    missing=sorted(set(old)-set(now));changed=sorted(k for k in old.keys()&now.keys() if any(old[k][f]!=now[k][f] for f in fields))
    return dict(missing_original_ids=missing,changed_original_ids=changed,original_values_preserved=not missing and not changed,
        new_rows=[now[k] for k in sorted(set(now)-set(old))],causal_success_verified=False)


async def history_candidates(snap):
    cols='UpdateEventDetailsHistoryId,CDOTypeId,HistoryId,HistoryMainlineId,TxnId,ChangeCount'
    await schema_check({'UpdateEventDetailsHistory':cols,'HistoryMainline':'HistoryMainlineId,HistoryId,TxnId,TxnType,Comments,ReversalStatus'})
    rows=await q.sql_rows('SELECT TOP 21 s.'+',s.'.join(cols.split(','))+',h.TxnType,h.Comments,h.ReversalStatus FROM UpdateEventDetailsHistory s JOIN HistoryMainline h '+
        'ON h.HistoryMainlineId=s.HistoryMainlineId AND h.HistoryId=s.HistoryId AND h.TxnId=s.TxnId '+
        f"WHERE s.CDOTypeId=8754 AND s.HistoryId='{iid(snap['target_id'])}' AND h.Comments="+q.literal(snap['body']['comments'])+' ORDER BY s.UpdateEventDetailsHistoryId',21)
    if not isinstance(rows,list) or len(rows)>20:raise ValueError('日志候选历史不完整或超过20条。')
    clean=[];seen=set();fields=(cols+',TxnType,Comments,ReversalStatus').split(',')
    for row in rows:
        if not isinstance(row,dict) or not set(fields)<=row.keys():raise ValueError('日志历史字段缺失。')
        item={k:copy.deepcopy(row[k]) for k in fields}
        for k in ('UpdateEventDetailsHistoryId','HistoryId','HistoryMainlineId','TxnId'):item[k]=iid(item[k])
        for k in ('CDOTypeId','TxnType','ChangeCount','ReversalStatus'):item[k]=u.i.wire_int(item[k],k)
        if (item['CDOTypeId']!=8754 or item['TxnType']!=SERVICE_TYPE or item['HistoryId']!=snap['target_id']
            or item['Comments']!=snap['body']['comments'] or item['UpdateEventDetailsHistoryId'] in seen):raise ValueError('日志历史事务/对象/标记或唯一性不符。')
        seen.add(item['UpdateEventDetailsHistoryId']);clean.append(item)
    return clean


async def inspect(snap):
    try:
        with verified_channel():
            current=await u.event_record(instance_id=snap['target_id']);data_id=snap['before']['data']['EventDataId']
            if not current or current['EventDataId']!=data_id:return dict(complete=False,reason='original_data_link_changed',verified_success=False,retry_allowed=False)
            rows=await records(data_id)
            check=await u.event_record(instance_id=snap['target_id'])
            if current!=check or rows!=await records(data_id):raise ValueError('日志读取期间变化。')
            return dict(complete=True,original_event_id=snap['target_id'],original_event_data_id=data_id,
                history_candidates=await history_candidates(snap),
                preservation=preservation(snap['before']['logs'],rows),verified_success=False,retry_allowed=False,
                note='当前日志及旧值保护证据；新增同文记录不证明本次事务成功，须核对最终事务历史后人工review，unknown保持占用。')
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):raise ValueError('日志原ID或完整证据不可用；不自动重试或确认成功。') from None


@mcp.tool
async def get_quality_event_log_context(event_name:str,organization_name:str,event_instance_id:str|None=None)->str:
    """只读已提交Active基础Event的完整处理日志及固定Load契约。最多100条，原作者/时间缺失或SQL与Load不符即拒绝。不调用普通预校验，不授权写入。"""
    try:
        with verified_channel():
            args=dict(event_name=event_name,organization_name=organization_name,event_instance_id=event_instance_id)
            before=await source(args);schema=await contract();original=await load(before,'MCP-QUAL:'+'0'*32,auth.capture_headers(SERVICE))
            if await source(args)!=before:raise ValueError('读取期间变化。')
            return q.encode(dict(complete=True,before=before,original_logs=original['logs'],native_contract=schema,ready_for_execution=False))
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):return q.encode(dict(complete=False,ready_for_execution=False,error='原日志清单、受限条件或Load契约未核实；没有业务写入。'))


@mcp.tool
async def prepare_quality_event_log_append(event_name:str,organization_name:str,log_text:str,comments:str,event_instance_id:str|None=None)->str:
    """准备追加一条处理日志（最多4000 UTF-16字符），完整保留旧日志。仅已提交Active Category1、已有标准EventData、原责任人/角色授权且无复杂关联/签名。新日志无类别，作者与时间由原生当前用户/系统生成；不改删旧项、不代签或补录日期。固定Load+静态计划，禁止普通execute=false假预览。确认后统一execute_quality_transaction单次提交；unknown不重试，inspect保留原日志证据，最终事务人工review。"""
    return await p.prepare('event_log_append',dict(event_name=event_name,organization_name=organization_name,event_instance_id=event_instance_id,log_text=log_text,comments=comments))
