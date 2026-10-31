"""NCR/CAPA plans with typed commands, immutable previews and single attempts."""
import asyncio
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Annotated,Literal

from pydantic import Field
from tools import mcp
from tools.boms import encode
from tools import quality as reads
from tools.collection_submission import digest
from tools.collection_validation import timestamp_valid,timestamp_ticks
from tools.modeling_details import raw_json,_has_next_link
from core.http_client import request,get_client,get_headers
from core.transfer_attempt_lock import attempt_lock,AttemptBusy
from config import CAMSTAR_BASE_URL,CAMSTAR_QUERY_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL

PLAN_DB=Path(__file__).resolve().parents[1]/'data/quality_plans.sqlite3'
FIELD_CASE={'rmaNumber':'RMANumber','ncrFailureCodes':'NCRFailureCodes','ncrFailureType':'NCRFailureType','ncrComment':'NCRComment',
            'ncrCauseCode':'NCRCauseCode','ncrResolutionCode':'NCRResolutionCode','capaName':'CAPAName','spcChartDef':'SPCChartDef','capaCustomDetail':'CAPACustomDetail','carSeverity':'CARSeverity'}


def environment():return {'modeling':CAMSTAR_BASE_URL.rstrip('/'),'query':CAMSTAR_QUERY_BASE_URL.rstrip('/'),'shopfloor':CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')}
def db_connect():
    PLAN_DB.parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(PLAN_DB)
    db.execute('CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,digest TEXT,payload TEXT,state TEXT,created REAL,outcome TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS attempts(plan_id TEXT PRIMARY KEY,attempt_id TEXT,started REAL,finished REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,plan_id TEXT,record TEXT)')
    return db


def note(value,label):
    if not isinstance(value,str) or len(value)>4000 or '\x00' in value:raise ValueError(label+'须为最多4000字符的字符串。')
    return value


def wire_matches(expected,actual,field=''):
    if isinstance(expected,dict):
        if not isinstance(actual,dict):return False
        for key,value in expected.items():
            server_key=FIELD_CASE.get(key,key[0].upper()+key[1:])
            if key=='instanceId':
                if reads.identifier(actual)!=value:return False
            elif server_key not in actual or not wire_matches(value,actual[server_key],key):return False
        return True
    if isinstance(expected,list):return isinstance(actual,list) and len(expected)==len(actual) and all(wire_matches(a,b,field) for a,b in zip(expected,actual))
    if field=='occurrenceDate' and timestamp_valid(expected) and timestamp_valid(actual):return timestamp_ticks(expected)==timestamp_ticks(actual)
    # Live quality services serialize Edm.Boolean/Int32 as lowercase strings.
    if type(expected) is bool and isinstance(actual,str):return actual==('true' if expected else 'false')
    if type(expected) is int and isinstance(actual,str):return actual==str(expected)
    return type(expected) is type(actual) and expected==actual


async def service_preview(service,body):
    select=[FIELD_CASE.get(k,k[0].upper()+k[1:]) for k in body]
    expected_type={'CreateCAPA':'7734','UpdateCAPA':'7734','NCRUpdate':'7209'}.get(service)
    if expected_type and 'ServiceDetail' not in select:select.append('ServiceDetail')
    if service=='CloseQualityObject':select+=['ApprovalRequired','ApprovalSheetStatus','ApprovalSheetMap','ApprovalSheetTemplate','QualityObjectDetail']
    params={'execute':'false','$select':','.join(select)}
    expanded=[FIELD_CASE.get(k,k[0].upper()+k[1:]) for k,v in body.items() if isinstance(v,list) or k=='serviceDetail']
    if expected_type and 'ServiceDetail' not in expanded:expanded.append('ServiceDetail')
    if service=='CloseQualityObject':expanded.append('QualityObjectDetail')
    if expanded:params['$expand']=','.join(expanded)
    raw=await request('POST','/api/'+service,base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,params=params,response_formatter=raw_json)
    response=json.loads(raw);data=response.get('data',response)
    if not isinstance(data,dict) or response.get('error') or response.get('ErrorCode') or data.get('error') or data.get('ErrorCode') or _has_next_link(data):raise ValueError('质量服务非执行预览失败/不完整。')
    if not wire_matches(body,data):raise ValueError('质量服务回显与已审阅字段/引用/完整失败码列表不一致。')
    # Nonexecuting loads allocate transient service/detail IDs on every request.
    # Bind verified input echoes and stable actor defaults, not those temporary IDs.
    detail=data.get('ServiceDetail') or {};defaults={}
    if expected_type and str(detail.get('CDOTypeId'))!=expected_type:raise ValueError('质量服务明细类型与已核实契约不匹配。')
    for field in ('Reporter','Initiator','ReporterOrganization','InitiatorOrganization'):
        ref=detail.get(field)
        if isinstance(ref,dict) and reads.valid_id(reads.identifier(ref)):
            defaults[field]={'Name':ref.get('Name'),'InstanceId':reads.identifier(ref)}
    preview={'params':params,'verified_input_echo':body,'detail_type':detail.get('CDOTypeId'),
             'server_actor_defaults':defaults,'dynamic_validation_verified':False}
    if service=='CloseQualityObject':
        preview['approval_context']={field:data[field] for field in ('ApprovalRequired','ApprovalSheetStatus','ApprovalSheetMap','ApprovalSheetTemplate') if field in data}
        qdetail=data.get('QualityObjectDetail')
        if not isinstance(qdetail,dict):raise ValueError('关闭服务未展开质量对象状态，不能准备关闭计划。')
        preview['quality_status_context']={field:qdetail[field] for field in ('Status','Category','IsApprovalRequired','CompletionApprovalStatus','Organization') if field in qdetail}
        # Verify loaded target state against the independent Inquiry in the builder.
        if 'Status' not in qdetail or 'Category' not in qdetail:raise ValueError('关闭服务未返回目标状态/类别。')
    return preview


async def failure_refs(names):
    if not isinstance(names,list) or not 1<=len(names)<=50 or any(not isinstance(n,str) or not n.strip() for n in names):raise ValueError('failure_codes须明确1..50个完整失败代码名称。')
    if len({n.strip().casefold() for n in names})!=len(names):raise ValueError('失败代码不能重复。')
    refs=[]
    for name in names:refs.append(await reads.resolve_reference('ncr_failure',name))
    return refs


async def build_ncr(args,marker):
    if args.get('action')=='quality_close':
        from tools.quality_ncr_closure import build_quality_close
        return await build_quality_close(args,marker)
    if args.get('action')=='escalate':
        from tools.quality_ncr_escalation import build_escalation
        return await build_escalation(args,marker)
    action=args['action'];name=reads.text(args['report_name'],'NCR名称')
    if action not in ('create','update','close'):raise ValueError('NCR操作仅支持create/update/close。')
    before=await reads.ncr_snapshot(name)
    if action=='create' and before:raise ValueError('该NCR名称已存在。')
    if action!='create' and not before:raise ValueError('待维护NCR不存在。')
    body={'comments':marker};references={};container_status=None
    codes=args.get('failure_codes')
    if action=='create':
        if not args.get('container_name'):raise ValueError('创建NCR必须明确容器。')
        ref,container_status=await reads.container_identity(args['container_name'],args.get('container_instance_id'))
        references['container']=ref;body.update(container=ref,nonconformanceName=name,ncrFailureCodes=await failure_refs(codes))
        service='NCRCreate';detail=body
        if any(args.get(k) is not None for k in ('cause_code_name','resolution_code_name','investigation_comments','corrective_comments','preventive_comments')):
            raise ValueError('创建NCR只接受失败类型/代码和NCR备注；调查/纠正/预防/解决信息用明确更新计划。')
    else:
        if args.get('container_name') or args.get('container_instance_id'):raise ValueError('维护NCR不接受更换容器参数。')
        body['nonconformanceReport']={'name':name,'instanceId':before['NonconformanceReportId']}
        detail={};body['serviceDetail']=detail;service='NCRUpdate' if action=='update' else 'NCRClose'
        if action=='update':
            if codes is None:
                assigned=before['failure_code_assignments']
                if not assigned:raise ValueError('现有NCR没有完整失败代码，更新需明确提供failure_codes。')
                detail['ncrFailureCodes']=[{'name':r['NCRFailureCodeName'],'instanceId':r['NCRFailureCodesId']} for r in assigned]
            else:detail['ncrFailureCodes']=await failure_refs(codes)
        elif codes is not None:raise ValueError('关闭不接受失败码替换；请先明确更新。')
        if action=='close' and not args.get('resolution_code_name'):raise ValueError('关闭NCR必须明确解决代码。')
    for argument,kind,field in [('failure_type_name','ncr_type','ncrFailureType'),('cause_code_name','ncr_cause','ncrCauseCode'),('resolution_code_name','ncr_resolution','ncrResolutionCode')]:
        value=args.get(argument)
        if value is not None:
            ref=await reads.resolve_reference(kind,value);references[argument]=ref;detail[field]=ref
    if args.get('ncr_comment') is not None:detail['ncrComment']=note(args['ncr_comment'],'NCR备注')
    for argument,field in [('investigation_comments','failureInvestigationComments'),('corrective_comments','correctiveActionComments'),('preventive_comments','preventiveActionComments')]:
        if args.get(argument) is not None:detail[field]={'comments':note(args[argument],argument)}
    if action=='update' and codes is None and len(detail)==1:raise ValueError('NCR更新没有明确变更。')
    preview=await service_preview(service,body)
    return {'kind':'ncr','action':action,'name':name,'target_id':before['NonconformanceReportId'] if before else None,
            'scope_key':'ncr:'+name.casefold(),'before':before,'references':references,'container_status':container_status,
            'service':service,'body':body,'preview':preview,'arguments':args,'environment':environment()}


async def build_capa(args,marker):
    if args['action']=='close':return await build_capa_closure(args,marker)
    action=args['action'];name=reads.text(args['capa_name'],'CAPA名称')
    if action not in ('create','update'):raise ValueError('CAPA操作仅支持create/update。')
    before=await reads.find_record('capa',name)
    if action=='create' and before:raise ValueError('CAPA名称已存在。')
    if action=='update' and not before:raise ValueError('CAPA不存在。')
    if before and (before.get('IsFrozen') is not False or before.get('Category')!=5 or before.get('Status') in (5,6)):
        raise ValueError('CAPA类别/冻结或关闭状态不允许当前受限更新。')
    if action=='update' and await reads.sql_rows("SELECT TOP 1 ApprovalSheetId FROM ApprovalSheet WHERE ParentId='"+before['CAPAId']+"'",1):
        raise ValueError('CAPA已有审批表；普通更新不绕过周期保护，请用prepare_capa_approval_revision明确旧表/周期进行受限资料修订。')
    if type(args['submit']) is not bool:raise ValueError('submit必须明确布尔。')
    organization=await reads.resolve_reference('organization',args['organization_name']);references={'organization':organization}
    if before and before.get('OrganizationId')!=organization['instanceId']:raise ValueError('不允许在普通CAPA更新中变更组织。')
    body={'organization':organization,'submit':args['submit'],'comments':marker};detail={}
    if action=='create':
        if not args.get('brief_description') or not args['brief_description'].strip():raise ValueError('创建CAPA必须提供简述。')
        body.update(capaName=name,category=5);service='CreateCAPA'
    else:
        body['qualityObject']={'name':name,'instanceId':before['CAPAId']};service='UpdateCAPA'
    for argument,field in [('brief_description','briefDescription'),('description','description')]:
        if args.get(argument) is not None:detail[field]=note(args[argument],argument)
    if args.get('is_systemic_issue') is not None:
        if type(args['is_systemic_issue']) is not bool:raise ValueError('is_systemic_issue必须是布尔值。')
        detail['isSystemicIssue']=args['is_systemic_issue']
    if args.get('occurrence_date') is not None:
        if not timestamp_valid(args['occurrence_date']):raise ValueError('发生时间必须是带时区ISO时间。')
        detail['occurrenceDate']=args['occurrence_date']
    for argument,kind,field in [('classification_name','classification','classification'),('subclassification_name','subclassification','subClassification'),('priority_name','priority','priorityLevel')]:
        if args.get(argument) is not None:
            ref=await reads.resolve_reference(kind,args[argument]);references[argument]=ref
            (detail if kind=='priority' else body)[field]=ref
    if action=='update' and not detail and not any(k in args and args[k] is not None for k in ('classification_name','subclassification_name')) and not args['submit']:
        raise ValueError('CAPA更新没有明确配置变更。')
    if detail:body['serviceDetail']=detail
    preview=await service_preview(service,body)
    return {'kind':'capa','action':action,'name':name,'target_id':before['CAPAId'] if before else None,'scope_key':'capa:'+name.casefold(),
            'before':before,'references':references,'service':service,'body':body,'preview':preview,'arguments':args,'environment':environment()}


async def build_capa_closure(args,marker):
    name=reads.text(args['capa_name'],'CAPA名称')
    before=await reads.find_record('capa',name)
    if not before:raise ValueError('待关闭CAPA不存在。')
    if (before.get('Category')!=5 or before.get('IsFrozen') is not False
            or type(before.get('Status')) is not int or before['Status'] not in (1,2,3,7,8)
            or not reads.valid_id(before.get('CAPAId'))):raise ValueError('CAPA类别/冻结/状态不允许准备关闭。')
    organization=await reads.resolve_reference('organization',args['organization_name'])
    if before.get('OrganizationId')!=organization['instanceId']:raise ValueError('关闭组织与CAPA当前组织不一致。')
    resolution=await reads.resolve_reference('quality_resolution',args['quality_resolution_code_name'])
    description=note(args['close_description'],'关闭说明')
    if not description.strip():raise ValueError('关闭CAPA必须明确非空关闭说明。')
    ref={'name':name,'instanceId':before['CAPAId']}
    inquiry=await reads.object_details(name,before['CAPAId']);detail=inquiry['QualityObjectInquiryDetail']
    # Integer strings are observed in live Query replies. Never coerce booleans.
    if (type(detail.get('Category')) not in (str,int) or str(detail['Category'])!='5'
            or type(detail.get('Status')) not in (str,int) or str(detail['Status'])!=str(before['Status'])
            or reads.identifier(detail.get('Organization'))!=organization['instanceId']):
        raise ValueError('CAPA查询详情与当前记录/组织不一致。')
    options=await reads.closure_options({'qualityObject':ref,'organization':organization})
    close=[r for r in options if r['Name']=='Close']
    if len(close)!=1:raise ValueError('当前部署未返回唯一Close处置方式，不猜测关闭原码。')
    body={'qualityObject':ref,'organization':organization,'resolutionAction':int(close[0]['Value']),
          'qualityResolutionCode':resolution,'closeDescription':description,'comments':marker}
    preview=await service_preview('CloseQualityObject',body)
    state=preview['quality_status_context']
    if (type(state.get('Category')) not in (str,int) or str(state['Category'])!='5'
            or type(state.get('Status')) not in (str,int) or str(state['Status'])!=str(before['Status'])
            or reads.identifier(state.get('Organization'))!=organization['instanceId']):
        raise ValueError('关闭服务加载的CAPA类别/状态与查询不一致。')
    preview['selected_resolution_action']=close[0]
    preview['inquiry_approval_context']={field:detail[field] for field in ('IsApprovalRequired','CompletionApprovalStatus','CompletionApprovalSheet','EntryForApproval') if field in detail}
    # Inquiry services/details can allocate a new InstanceId/Parent per read.
    # Bind persistent target/business fields, never transient inquiry instance IDs.
    inquiry_before={'qualityObject':ref,'detail':{field:detail[field] for field in (
        'Status','Category','Organization','Owner','Role','ProcessModel','IsApprovalRequired',
        'CompletionApprovalStatus','CompletionApprovalSheet','EntryForApproval') if field in detail}}
    if await reads.find_record('capa',name,before['CAPAId'])!=before:raise ValueError('准备关闭期间CAPA记录已变化。')
    return {'kind':'capa','action':'close','name':name,'target_id':before['CAPAId'],'scope_key':'capa:'+name.casefold(),
        'before':before,'inquiry_before':inquiry_before,'references':{'organization':organization,'quality_resolution':resolution},
        'closure_options':options,'service':'CloseQualityObject','body':body,'preview':preview,'arguments':args,'environment':environment()}


async def build(kind,args,marker,execution_binding=None):
    if not isinstance(marker,str) or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',marker):raise ValueError('质量事务关联标记无效。')
    if kind=='capa_approval_revision':
        from tools.quality_capa_approval_revision import build
        return await build(args,marker)
    if kind=='quality_approval_assignment':
        from tools.quality_approval_assignment import build
        return await build(args,marker)
    if kind=='quality_approval_decision':
        from tools.quality_approval_decisions import build
        return await build(args,marker) if execution_binding is None else await build(args,marker,execution_binding=execution_binding)
    if kind=='quality_approval_delegation':
        from tools.quality_approval_delegation import build
        return await build(args,marker,execution_binding=execution_binding)
    if kind=='quality_approval':
        from tools.quality_approval_actions import build
        return await build(args,marker)
    if kind in ('event_open','event_reassign','event_create'):
        from tools.quality_event_lifecycle import operation,build_create
        return await build_create(args,marker) if kind=='event_create' else await operation(args,marker,kind)
    if kind=='event_close':
        from tools.quality_event_close_plan import build_close
        return await build_close(args,marker)
    if kind=='event_update':
        from tools.quality_event_update import build_update
        return await build_update(args,marker)
    if kind=='event_submit':
        from tools.quality_event_submission import build
        return await build(args,marker)
    if kind=='event_draft_update':
        from tools.quality_event_drafts import build
        return await build(args,marker,execution_binding=execution_binding)
    if kind=='event_data_update':
        from tools.quality_event_data import build
        return await build(args,marker,execution_binding=execution_binding)
    if kind=='event_triage':
        from tools.quality_event_triage import build_triage
        return await build_triage(args,marker)
    if kind=='spc_violation':
        from tools.quality_spc_violations import build_violation
        return await build_violation(args,marker)
    if kind=='spc_annotation':
        from tools.quality_spc_annotations import build_annotation
        return await build_annotation(args,marker)
    if kind=='ncr':
        from tools import quality_native_signatures as native
        if native.requested(args):return await native.build(args,marker,execution_binding)
        return await build_ncr(args,marker)
    if kind=='capa':return await build_capa(args,marker)
    if kind=='inspection':
        from tools.quality_inspection import build_inspection
        return await build_inspection(args,marker)
    if kind=='sampling_reference':
        from tools.quality_sampling_references import build_reference
        return await build_reference(args,marker)
    if kind=='lot_maintenance':
        from tools.quality_lot_maintenance import build_update
        return await build_update(args,marker)
    if kind=='current_sampling':
        from tools.quality_current_sampling import build_update
        return await build_update(args,marker)
    if kind=='lot_rework':
        from tools.quality_lot_rework import build_rework
        return await build_rework(args,marker)
    if kind=='quantity_loss':
        from tools.quality_quantity_loss import build_loss
        return await build_loss(args,marker)
    if kind in ('inspection_definition','sampling_binding'):
        from tools.quality_definitions import build_definition,build_binding
        return await (build_definition(args,marker) if kind=='inspection_definition' else build_binding(args,marker))
    if kind=='sampling_lot':
        from tools.quality_lots import build_lot
        return await build_lot(args,marker)
    if kind in ('lot_inspection','lot_completion'):
        from tools.quality_lot_execution import build_collection,build_completion
        return await (build_collection(args,marker) if kind=='lot_inspection' else build_completion(args,marker))
    raise ValueError('质量事务类型未开放。')


async def prepare(kind,args):
    try:
        marker='MCP-QUAL:'+uuid.uuid4().hex;snapshot=await asyncio.wait_for(build(kind,args,marker),90)
        key=uuid.uuid4().hex;fingerprint=digest(snapshot)
        route=transport(snapshot)
        from core.approval_policy import routine_quality_creation
        result={'plan_id':key,'plan_digest':fingerprint,'state':'ready','submitted':False,'confirmation_required':not routine_quality_creation(snapshot),'expires_in_seconds':3600,
            'request_preview':{**route,'body':snapshot['body']},'before':snapshot['before'],
            'server_context':snapshot['preview'],
            'static_and_echo_verified':'verified_input_echo' in snapshot['preview'],'dynamic_validation_verified':False,
            'note':'静态校验或非执行回显不是业务批准、签名或质量判定。执行后先unknown；当前需最终证据人工核实。'}
        if snapshot.get('quality_signature'):
            from core.quality_signature_vault import VAULT
            from tools.quality_signatures import display
            participants=snapshot['quality_signature'].get('participants')
            if participants:
                from tools.quality_native_signatures import display
            result['signature_input_url']=VAULT.create(key,fingerprint,display(snapshot,fingerprint),participants=participants)
            result['signature_input_status']=VAULT.status(key,fingerprint)
        if len(json.dumps(result,ensure_ascii=False))>78000:raise ValueError('计划预览超过输出上限，请缩小维护或采样批次。')
        with db_connect() as db:db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',(key,fingerprint,json.dumps(snapshot,ensure_ascii=False),'ready',time.time()))
        return encode(result)
    except (ValueError,TypeError,KeyError,AttributeError,asyncio.TimeoutError) as error:return encode({'submitted':False,'error':str(error) or '质量预览超时。'})


from tools.quality_native_signatures import NativeSignatureAssignment


@mcp.tool
async def prepare_nonconformance_report(action:Literal['create','update','close'],report_name:str,container_name:str='',
        container_instance_id:str|None=None,failure_codes:Annotated[list[str]|None,Field(description='创建必填完整NCR失败码列表；更新省略则保留现有完整清单，明确传入则完整替换而非追加。',max_length=50)]=None,
        failure_type_name:str|None=None,cause_code_name:str|None=None,resolution_code_name:str|None=None,
        ncr_comment:str|None=None,investigation_comments:str|None=None,corrective_comments:str|None=None,preventive_comments:str|None=None,
        require_native_esign:bool=False,native_signature_assignments:Annotated[list[NativeSignatureAssignment]|None,Field(max_length=5)]=None)->str:
    """准备NCRCreate/NCRUpdate/NCRClose计划。创建需名称、容器和失败码；更新省略failure_codes保留完整清单；关闭需解决代码。关联变化阻止执行。
    通用多签/协签仅受限Open未升级NCRUpdate，明确require_native_esign=true及每项原requirement_detail_id、signer_name，可明确cosigner_name和cosign_reason_name。
    先get_nonconformance_report(include_native_signature_context=true)读取要求。必须实际暴露ESigPasswordCapture具体口令子类型及固定LoadESigDetails契约，并核实原生认证参数映射；当前原生映射保护门禁未开放，保存的标准REST契约亦不支持，不生成签名执行计划。
    原ESigRoleGroup授权、数量和唯一主签署人均核对；口令只在独立本机页面由指定人员亲自输入确认，一次性、短期、仅内存。签名暂存不是原生验证成功，unknown不重试。不支持事件审批多签或委托。
    """
    args=dict(action=action,report_name=report_name,container_name=container_name,container_instance_id=container_instance_id,failure_codes=failure_codes,
        failure_type_name=failure_type_name,cause_code_name=cause_code_name,resolution_code_name=resolution_code_name,ncr_comment=ncr_comment,
        investigation_comments=investigation_comments,corrective_comments=corrective_comments,preventive_comments=preventive_comments,
        require_native_esign=require_native_esign,native_signature_assignments=[x.model_dump() for x in native_signature_assignments] if native_signature_assignments is not None else None)
    return await prepare('ncr',args)


@mcp.tool
async def prepare_capa(action:Literal['create','update'],capa_name:str,organization_name:str,brief_description:str|None=None,
        description:str|None=None,classification_name:str|None=None,subclassification_name:str|None=None,priority_name:str|None=None,
        is_systemic_issue:bool|None=None,occurrence_date:str|None=None,submit:bool=False)->str:
    """只准备CreateCAPA/UpdateCAPA计划，类别固定5(CAPA)，明确组织，创建还需简述。支持说明/分类/优先级/系统性标志/带时区发生时间。submit=false保存草稿，true申请提交由MES流程/权限验证；不代表批准或关闭。普通更新不转组织、不修改关闭/删除对象，不代填签名或流程字段。执行另需确认。"""
    return await prepare('capa',dict(action=action,capa_name=capa_name,organization_name=organization_name,brief_description=brief_description,
        description=description,classification_name=classification_name,subclassification_name=subclassification_name,priority_name=priority_name,
        is_systemic_issue=is_systemic_issue,occurrence_date=occurrence_date,submit=submit))


@mcp.tool
async def prepare_capa_closure(capa_name:str,organization_name:str,quality_resolution_code_name:str,close_description:str)->str:
    """仅准备类别5 CAPA的CloseQualityObject关闭计划。明确CAPA/组织/QualityResolutionCode/关闭说明，实时查询Close处置原码并核对当前状态及审批上下文。不支持挂接/创建CAPA，不覆盖审批、签名或流程标志。预览不是已关闭；执行须系统确认且单次提交，结果先unknown。"""
    return await prepare('capa',dict(action='close',capa_name=capa_name,organization_name=organization_name,
        quality_resolution_code_name=quality_resolution_code_name,close_description=close_description))


def checked(row,fingerprint):
    if not row or row[0]!=fingerprint:raise ValueError('质量计划不存在或摘要错误。')
    snapshot=json.loads(row[1])
    if digest(snapshot)!=fingerprint or snapshot.get('environment')!=environment():raise ValueError('质量计划内容或部署环境不一致。')
    return snapshot


def transport(snapshot):
    if snapshot.get('api','shopfloor')=='shopfloor':
        if snapshot.get('kind')=='quality_approval_delegation':
            from tools.quality_approval_delegation import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='DelegateTask' and snapshot.get('kind')!='quality_approval_delegation':raise ValueError('转派服务与质量计划类型不符。')
        if (snapshot.get('quality_signature') or {}).get('kind')=='generic_ncr_update':
            from tools.quality_native_signatures import validate_transport
            validate_transport(snapshot)
        if snapshot.get('kind')=='capa_approval_revision':
            from tools.quality_capa_approval_revision import validate_transport
            validate_transport(snapshot)
        if snapshot.get('kind')=='quality_approval_assignment':
            from tools.quality_approval_assignment import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='AssignApprovalSheet' and snapshot.get('kind')!='quality_approval_assignment':raise ValueError('挂接服务与计划类型不符。')
        if snapshot.get('kind')=='quality_approval_decision':
            from tools.quality_approval_decisions import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='SignApproval' and snapshot.get('kind')!='quality_approval_decision':raise ValueError('签批服务与计划类型不符。')
        if snapshot.get('kind')=='quality_approval':
            from tools.quality_approval_actions import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service') in ('RouteApprovalSheet','CancelApprovalSheet') and snapshot.get('kind')!='quality_approval':raise ValueError('审批服务与计划类型不符。')
        if snapshot.get('kind') in ('event_open','event_reassign','event_create'):
            from tools.quality_event_lifecycle import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service') in ('OpenQualityObject','ChangeOwner','CreateEvent') and snapshot.get('kind') not in ('event_open','event_reassign','event_create'):
            raise ValueError('生命周期服务与计划类型不符。')
        if snapshot.get('kind')=='event_close':
            from tools.quality_event_close_plan import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='CloseQualityObject' and snapshot.get('kind') not in ('capa','event_close'):
            raise ValueError('关闭服务与质量对象计划类型不符。')
        if snapshot.get('kind')=='event_update' and snapshot.get('service')!='UpdateEvent':
            raise ValueError('事件文字更新计划必须使用UpdateEvent。')
        if snapshot.get('kind')=='event_submit' and snapshot.get('service')!='UpdateEvent':raise ValueError('事件正式提交计划必须使用UpdateEvent。')
        if snapshot.get('kind')=='event_draft_update' and snapshot.get('service')!='UpdateEvent':raise ValueError('草稿保存计划必须使用UpdateEvent。')
        if snapshot.get('kind')=='event_data_update' and snapshot.get('service')!='UpdateEventData':raise ValueError('事件扩展资料计划必须使用UpdateEventData。')
        if snapshot.get('service')=='UpdateEventData':
            from tools.quality_event_data import validate_transport
            validate_transport(snapshot)
        if snapshot.get('kind')=='event_triage' and snapshot.get('service')!='TriageQualityObject':
            raise ValueError('事件首次分流计划必须使用TriageQualityObject。')
        if snapshot.get('kind')=='spc_violation' and snapshot.get('service')!='RecordSPCViolation':
            raise ValueError('SPC违例登记计划必须使用RecordSPCViolation。')
        if snapshot.get('kind')=='spc_annotation' and snapshot.get('service')!='AddSPCAnnotation':
            raise ValueError('SPC注释计划必须使用AddSPCAnnotation。')
        if snapshot.get('kind')=='ncr' and snapshot.get('action')=='quality_close' and snapshot.get('service')!='NCRCloseQuality':
            raise ValueError('质量侧NCR关闭计划必须使用NCRCloseQuality。')
        if snapshot.get('kind')=='ncr' and snapshot.get('action')=='escalate' and snapshot.get('service')!='NCREscalate':
            raise ValueError('NCR升级计划必须使用NCREscalate。')
        if snapshot.get('kind')=='lot_maintenance' and snapshot.get('service')!='UpdateSamplingLot':
            raise ValueError('批维护计划必须使用UpdateSamplingLot。')
        if snapshot.get('kind')=='current_sampling' and snapshot.get('service')!='CurrentSamplingStatusUpdate':
            raise ValueError('当前采样维护计划必须使用CurrentSamplingStatusUpdate。')
        if snapshot.get('kind')=='lot_rework' and snapshot.get('service')!='ExecuteSamplingLot':
            raise ValueError('批返工计划必须使用ExecuteSamplingLot。')
        if snapshot.get('kind')=='quantity_loss' and snapshot.get('service')!='ChangeQty':
            raise ValueError('数量损失计划必须使用ChangeQty。')
        if snapshot.get('service') not in ('NCRCreate','NCRUpdate','NCRClose','NCRCloseQuality','NCREscalate','CreateCAPA','UpdateCAPA','CloseQualityObject',
                'CollectSamplingData','CreateSamplingLot','CollectLotSamplingData','ExecuteSamplingLot','UpdateSamplingLot','CurrentSamplingStatusUpdate','ChangeQty','AddSPCAnnotation','RecordSPCViolation','TriageQualityObject','UpdateEvent','OpenQualityObject','ChangeOwner','CreateEvent','RouteApprovalSheet','CancelApprovalSheet','SignApproval','AssignApprovalSheet','DelegateTask','UpdateEventData'):
            raise ValueError('质量服务不在执行白名单。')
        if snapshot.get('service')=='UpdateEvent':
            if snapshot.get('kind')=='event_submit':
                from tools.quality_event_submission import validate_transport
            elif snapshot.get('kind')=='event_draft_update':from tools.quality_event_drafts import validate_transport
            else:from tools.quality_event_update import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='TriageQualityObject':
            from tools.quality_event_triage import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='RecordSPCViolation':
            from tools.quality_spc_violations import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='AddSPCAnnotation':
            from tools.quality_spc_annotations import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='NCRCloseQuality':
            from tools.quality_ncr_closure import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='NCREscalate':
            from tools.quality_ncr_escalation import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='CurrentSamplingStatusUpdate':
            from tools.quality_current_sampling import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='ChangeQty':
            from tools.quality_quantity_loss import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='CollectSamplingData':
            from tools.quality_inspection_effects import validate_transport
            validate_transport(snapshot)
        if snapshot.get('service')=='ExecuteSamplingLot' and snapshot.get('kind')=='lot_rework':
            from tools.quality_lot_rework import validate_transport
            validate_transport(snapshot)
        elif snapshot.get('service')=='ExecuteSamplingLot' and (snapshot.get('kind')!='lot_completion'
                or snapshot.get('action')!='ok' or type(snapshot['body'].get('samplingLotResult')) is not int
                or snapshot['body']['samplingLotResult']!=1 or snapshot['body'].get('samplingCompleted') is not True
                or set(snapshot['body'])!={'samplingLot','samplingCompleted','samplingLotResult','comments'}):
            raise ValueError('整批OK计划仅接受结果1；返工须独立批返工计划，报废尚未开放。')
        if snapshot.get('service')=='CollectLotSamplingData' and (snapshot.get('kind')!='lot_inspection' or snapshot.get('action')!='collect'):
            raise ValueError('批采样服务与计划类型不符。')
        if snapshot.get('service')=='UpdateSamplingLot':
            body=snapshot['body']
            if (snapshot.get('kind')!='lot_maintenance' or snapshot.get('action')!='update'
                    or set(body)!={'samplingLot','qty','sampleRate','allowAdjustToLotSize','comments'}
                    or type(body.get('qty')) not in (int,float) or not 0<body['qty']<=1e12
                    or type(body.get('sampleRate')) is not int or not 1<=body['sampleRate']<=2147483647
                    or type(body.get('allowAdjustToLotSize')) is not bool
                    or not reads.valid_id(snapshot.get('target_id'))
                    or not isinstance(body.get('samplingLot'),dict)
                    or set(body['samplingLot'])!={'name','instanceId'} or body['samplingLot'].get('name')!=snapshot.get('name')
                    or body['samplingLot'].get('instanceId')!=snapshot['target_id']):
                raise ValueError('批维护服务、目标或允许字段不符。')
        params={'execute':'true'}
        if snapshot['kind']=='event_create':params['$select']='QualityObject,CompletionMsg,Comments,ACEStatus,ACEMessage'
        if snapshot['kind']=='capa_approval_revision':params['$select']='QualityObject,Comments,CompletionMsg,ACEStatus,ACEMessage'
        if snapshot['kind']=='quality_approval_assignment':params['$select']='QualityObject,ApprovalSheet,Comments,CompletionMsg,ACEStatus,ACEMessage'
        if snapshot['kind']=='quality_approval_decision':params['$select']='QualityObject,ApprovalSheet,ApprovalSheetEntry,ApprovalDecision,Comments,CompletionMsg,ACEStatus,ACEMessage'
        if snapshot['kind']=='quality_approval_delegation':params['$select']='TrackableObject,DelegationTarget,SourceEmployee,TargetEmployee,DelegationReasonCode,Comments,CompletionMsg,ACEStatus,ACEMessage'
        return {'api':'shopfloor','method':'POST','path':'/api/'+snapshot['service'],'params':params}
    if snapshot.get('api')!='modeling' or snapshot['kind'] not in ('inspection_definition','sampling_binding','sampling_reference'):
        raise ValueError('质量执行API类型未开放。')
    entity=snapshot.get('entity');method=snapshot.get('method')
    if snapshot['kind']=='sampling_reference':
        from tools.quality_sampling_references import CONFIG
        expected=CONFIG.get(snapshot.get('reference_kind'))
        if not expected or entity!=expected[0] or snapshot['action'] not in ('create','update'):
            raise ValueError('抽样基础配置实体或动作不符。')
    if (snapshot['kind']=='inspection_definition' and entity not in ('SampleDataPoints','SampleTests','SamplingPlans')
            or snapshot['kind']=='sampling_binding' and (entity!='Products' or snapshot['action']!='bind')):
        raise ValueError('建模实体与计划构建器不一致。')
    if entity not in ('SampleDataPoints','SampleTests','SamplingPlans','Products','AQLLevels','InspectionLevels') or method!={'create':'POST','update':'PUT','revise':'PATCH','bind':'PUT'}.get(snapshot['action']):
        raise ValueError('建模实体/动作/方法不一致。')
    path='/api/'+entity
    if method=='PUT':
        if not reads.valid_id(snapshot.get('target_id')):raise ValueError('维护目标ID无效。')
        path+='/'+snapshot['target_id']
    if snapshot.get('path')!=path:raise ValueError('建模请求路径与绑定目标不一致。')
    return {'api':'modeling','method':method,'path':path,
        'params':{'revision':reads.text(snapshot.get('revision'),'维护版本')} if method=='PUT' and snapshot['kind']!='sampling_reference' else {}}


def resource_sets(snap):
    reads_set=set(snap.get('resource_reads',[]));writes=set(snap.get('resource_writes',[]))
    # Sampling task snapshots do not always expose all resolved AQL/level IDs.
    # Keep a conservative shared gate until that dependency chain is verified.
    if snap['kind'] in ('quality_approval','quality_approval_decision','quality_approval_assignment','capa_approval_revision','quality_approval_delegation'):
        reads_set.add('runtime:quality-event:org:'+snap['before']['context']['target']['OrganizationId'])
    if snap['kind'].startswith('event_'):
        org=snap.get('event_org_id') or (snap.get('before',{}).get('event') or {}).get('OrganizationId')
        if reads.valid_id(org):reads_set.add('runtime:quality-event:org:'+org.lower())
    gate='modeling:sampling-reference-config:gate'
    if snap['kind']=='sampling_reference':writes.add(gate)
    elif snap['kind'] in ('inspection','lot_inspection','lot_completion','lot_rework','quantity_loss','lot_maintenance','current_sampling','sampling_lot','inspection_definition','sampling_binding'):
        reads_set.add(gate)
    if snap['kind']=='inspection':
        from tools.quality_definitions import resources
        definition=snap['definition'];reads_set.update(resources('SampleTests',definition['test']))
        for row in definition['points']:reads_set.update(resources('SampleDataPoints',row))
        reads_set.add('modeling:SamplingPlans:id:'+snap['before']['task']['SamplingPlanId'])
        from tools.quality_lot_execution import runtime_claim
        writes.add(runtime_claim('container',snap['target_id']))
    if snap['kind']=='capa' and reads.valid_id(snap.get('target_id')):
        writes.add('runtime:quality-event:id:'+snap['target_id'].lower())
    if snap['kind']=='sampling_lot':
        writes.add('runtime:lot:name:'+snap['name'].casefold())
    if snap['kind']=='ncr':
        from tools.quality_lot_execution import runtime_claim
        cid=(snap.get('body',{}).get('container') or {}).get('instanceId') or (snap.get('before') or {}).get('ContainerId')
        if reads.valid_id(cid):writes.add(runtime_claim('container',cid))
    return reads_set,writes


def conflicts(other,snap):
    if other['environment']!=snap['environment']:return False
    if (other['scope_key']==snap['scope_key'] or other['kind']==snap['kind']
            and bool(other.get('target_id')) and other.get('target_id')==snap.get('target_id')):return True
    ar,aw=resource_sets(other);br,bw=resource_sets(snap)
    return bool(aw & (br|bw) or bw & ar)


def confirmation_summary(plan_id,plan_digest):
    with db_connect() as db:row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
    snap=checked(row,plan_digest)
    return '质量事务计划：'+json.dumps({'kind':snap['kind'],'action':snap['action'],'name':snap['name'],'state':row[2],
        'expired':time.time()-row[3]>3600,'transport':transport(snap),'request':snap['body'],'before':snap['before'],'server_context':snap['preview']},ensure_ascii=False)+'\n仅原计划单次提交；unknown不重试、不自动解锁。静态预览/非执行回显不是质量判定或业务审批。'


@mcp.tool
async def get_quality_transaction_plan(plan_id:str)->str:
    """只读质量事务计划、请求快照、执行响应和本地审计终态；ready有效1小时，已用/未知计划不能重放。"""
    with db_connect() as db:row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
    if not row:return encode({'error':'质量计划不存在。'})
    snapshot=json.loads(row[1])
    result={'plan_id':plan_id,'plan_digest':row[0],'snapshot':snapshot,'state':row[2],'expired':time.time()-row[3]>3600,'outcome':json.loads(row[4]) if row[4] else None}
    if snapshot.get('quality_signature'):
        from core.quality_signature_vault import VAULT
        result['signature_input_status']=VAULT.status(plan_id,row[0])
    return encode(result)


@mcp.tool
async def execute_quality_transaction(plan_id:str,plan_digest:str)->str:
    """按风险策略单次执行质量、检验、定义维护、抽样基础表、产品关联或建批/批维护计划。用户明确要求执行时，新建检验指标/项目/抽检计划可免逐项确认；已有配置修改/升版、生产关联、检验及处置仍需确认。按白名单选择Modeling CRUD或Shopfloor POST；固定目标及适用版本，重建摘要并检查关联读写占用。Modeling无execute=false回显，Shopfloor默认核对回显；CreateEvent为无原生预览的静态自动编号计划，须明确审阅其限制。HTTP不重试，取消/超时保存unknown；inspect只给证据，人工最终证据才review，不自动成功或解锁。"""
    try:
        with db_connect() as db:row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
        snapshot=checked(row,plan_digest)
        if snapshot['kind']=='event_submit':
            try:return await _execute_quality_transaction(plan_id,plan_digest)
            except asyncio.CancelledError:raise
            except Exception:
                return encode({'submitted':None,'state':'unknown','verified_success':False,'retry_allowed':False,
                    'error':'事件正式提交过程异常，无法确认提交阶段；请查询原计划与最终事务，不得自动重试。'})
        if snapshot['kind'] in ('quality_approval_delegation','event_draft_update','event_data_update') or snapshot['kind']=='quality_approval_decision' and snapshot['preview'].get('signature_actor_policy')=='delegated':
            from core.signature_channel import verified_channel
            if not snapshot.get('quality_signature'):
                try:
                    with verified_channel():return await _execute_quality_transaction(plan_id,plan_digest)
                except asyncio.CancelledError:raise
                except Exception:
                    return encode({'submitted':None,'state':'unknown','verified_success':False,'retry_allowed':False,
                        'error':'受保护质量事务执行中断，无法确认提交阶段；请核对计划与最终原生事务，不得自动重试。'})
        if snapshot.get('quality_signature'):
            from core.quality_signature_vault import VAULT
            from core.signature_channel import verified_channel
            # Claim before further reads: every execution invocation consumes the
            # human grant, even if state/configuration/network checks later fail.
            claimed=False
            try:
                with VAULT.claim(plan_id,plan_digest) as credential:
                    claimed=True
                    with verified_channel() as client:return await _execute_quality_transaction(plan_id,plan_digest,credential,client)
            except asyncio.CancelledError:raise
            except Exception:
                if claimed:
                    return encode({'submitted':None,'state':'unknown','verified_success':False,'retry_allowed':False,
                        'error':'安全签名执行中断，无法确认是否已提交；输入不可复用。请查询计划及最终事务证据，不得自动重试。'})
                return encode({'submitted':False,'error':'该计划没有可用的一次性签名输入；取消、过期、重启或已领取后须重新准备。'})
        return await _execute_quality_transaction(plan_id,plan_digest)
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy,asyncio.TimeoutError):
        return encode({'submitted':False,'error':'质量计划、摘要或安全输入不可用；未新增提交。请核对计划状态；签名输入失效须重新准备。'})


async def _execute_quality_transaction(plan_id,plan_digest,credential=None,signature_client=None):
    try:
        with attempt_lock(PLAN_DB,'quality-domain'),attempt_lock(PLAN_DB,plan_id):
            with db_connect() as db:row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
            snap=checked(row,plan_digest)
            if row[2]!='ready' or time.time()-row[3]>3600:raise ValueError('质量计划已使用或过期。')
            marker=snap.get('marker') or snap['body'].get('comments')
            role_approval=(snap['kind'] in ('quality_approval_delegation','event_draft_update','event_data_update') or snap['kind']=='quality_approval_decision' and snap['preview'].get('signature_actor_policy') in ('role','delegated'))
            signed_approval=bool(snap.get('quality_signature'))
            protected_approval=role_approval or signed_approval
            if signed_approval!=(credential is not None):raise ValueError('质量签名输入与计划要求不符，未提交。')
            binding={}
            fresh=await asyncio.wait_for(build(snap['kind'],snap['arguments'],marker,execution_binding=binding) if protected_approval
                else build(snap['kind'],snap['arguments'],marker),90)
            if digest(fresh)!=plan_digest:raise ValueError('质量对象/容器/引用或服务回显已变化，未提交。')
            route=transport(snap)
            if protected_approval:
                # Use the exact transport session that returned the verified actor.
                # No credential/hash enters the immutable plan or persisted outcome.
                if binding.get('actor_id')!=snap['preview']['employee_id'] or not isinstance(binding.get('headers'),dict):
                    raise ValueError('受保护质量事务未绑定同一已核实会话，未提交。')
                post_headers=dict(binding.pop('headers'))
            else:post_headers=None
            if signed_approval:
                credential.valid()
            with db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()!=row or time.time()-row[3]>3600:raise ValueError('领取前质量计划变化或过期。')
                for (payload,) in db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')"):
                    other=json.loads(payload)
                    if conflicts(other,snap):raise ValueError('该对象或关联配置有未核实事务，不能用新计划、改名或切换维护/检验绕过占用。')
                attempt=uuid.uuid4().hex;db.execute("UPDATE plans SET state='in_flight' WHERE id=?",(plan_id,))
                db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)',(plan_id,attempt,time.time()))
            outcome={'state':'unknown','submission_attempted':True,'attempt_id':attempt,'verified_success':False,'retry_allowed':False}
            payload=None
            try:
                payload=snap['body']
                if signed_approval:
                    from tools.quality_signatures import payload as signature_payload
                    if snap['quality_signature'].get('kind')=='generic_ncr_update':
                        from tools.quality_native_signatures import payload as signature_payload
                    payload=signature_payload(snap,credential)
                base=CAMSTAR_BASE_URL if route['api']=='modeling' else CAMSTAR_SHOPFLOOR_BASE_URL
                response=await asyncio.wait_for((signature_client if signed_approval else get_client()).request(route['method'],base.rstrip('/')+route['path'],
                    headers=post_headers if protected_approval else get_headers(),json=payload,params=route['params'],follow_redirects=False),60)
                if protected_approval:
                    # Debug/error bodies can echo authorization or cookie values.
                    # Keep the attempt/status, but no free-form response text for
                    # this credential-bound path; final evidence comes from MES history.
                    outcome.update(http_status=response.status_code,raw_response=None,response_capture='omitted_for_signature_privacy' if signed_approval else 'omitted_for_role_session_privacy')
                else:outcome.update(http_status=response.status_code,raw_response=response.text[:200000],response_truncated=len(response.text)>200000)
            except asyncio.CancelledError:
                outcome['interrupted']=True;raise
            except Exception as error:
                if protected_approval:outcome.update(error_type=type(error).__name__,error='受保护质量事务请求异常；未保存可能反射签名或会话信息的异常正文，请按原对象历史核实。')
                else:outcome['error']=repr(error)
            finally:
                if signed_approval:
                    if isinstance(payload,dict):payload.get('qualityESigDetail',{}).clear()
                    if snap['quality_signature'].get('kind')=='generic_ncr_update':
                        from tools.quality_native_signatures import clear_payload
                        clear_payload(payload)
                    credential.close()
                with db_connect() as db:
                    db.execute("UPDATE plans SET state='unknown',outcome=? WHERE id=? AND state='in_flight'",(json.dumps(outcome,ensure_ascii=False),plan_id))
                    db.execute('UPDATE attempts SET finished=? WHERE plan_id=?',(time.time(),plan_id))
            return encode(outcome)
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy,asyncio.TimeoutError) as error:return encode({'submitted':False,'error':str(error) or '质量提交前核对超时。'})


@mcp.tool
async def inspect_quality_transaction(plan_id:str,plan_digest:str)->str:
    """只读未知质量计划对应的记录/配置/检验任务/抽样批及有限历史作为证据。没有现场验证的最终事务链时，同名/同值/HTTP200均不归因成功，不解锁、不重放。"""
    try:
        with attempt_lock(PLAN_DB,plan_id):
            with db_connect() as db:row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
            snap=checked(row,plan_digest)
            if row[2] not in ('unknown','in_flight'):raise ValueError('仅检查已尝试/未知质量计划。')
            if snap['kind']=='event_submit':
                from tools.quality_event_submission import inspect
                return encode(await inspect(snap))
            if snap['kind']=='event_data_update':
                from tools.quality_event_data import inspect
                return encode(await inspect(snap))
            if snap['kind']=='event_draft_update':
                from tools.quality_event_drafts import inspect
                return encode(await inspect(snap))
            if snap['kind']=='quality_approval_delegation':
                from tools.quality_approval_delegation import inspect
                return encode(await inspect(snap))
            if (snap.get('quality_signature') or {}).get('kind')=='generic_ncr_update':
                from tools.quality_native_signatures import inspect
                return encode(await inspect(snap))
            if snap['kind']=='capa_approval_revision':
                from tools.quality_capa_approval_revision import inspect
                return encode(await inspect(snap))
            if snap['kind']=='quality_approval_assignment':
                from tools.quality_approval_assignment import inspect
                return encode(await inspect(snap))
            if snap['kind']=='quality_approval_decision':
                from tools.quality_approval_decisions import inspect
                return encode(await inspect(snap))
            if snap['kind']=='quality_approval':
                from tools.quality_approval_actions import inspect
                return encode(await inspect(snap))
            if snap['kind'] in ('event_create','event_open','event_reassign'):
                from tools.quality_event_lifecycle import inspect
                return encode(await inspect(snap,json.loads(row[4]) if row[4] else None))
            if snap['kind']=='event_close':
                from tools.quality_event_close_plan import inspect_snapshot
                return encode(await inspect_snapshot(snap))
            if snap['kind']=='event_update':
                from tools.quality_event_update import inspect_snapshot
                return encode(await inspect_snapshot(snap))
            if snap['kind']=='event_triage':
                from tools.quality_event_triage import inspect_snapshot
                return encode(await inspect_snapshot(snap))
            if snap['kind']=='spc_violation':
                from tools.quality_spc_violations import inspect_snapshot
                return encode(await inspect_snapshot(snap))
            if snap['kind']=='spc_annotation':
                from tools.quality_spc_annotations import inspect_snapshot
                return encode(await inspect_snapshot(snap))
            if snap['kind']=='ncr' and snap.get('action')=='quality_close':
                from tools.quality_ncr_closure import inspect_snapshot
                return encode(await inspect_snapshot(snap))
            if snap['kind']=='current_sampling':
                from tools.quality_current_sampling import inspect_snapshot
                return encode(await inspect_snapshot(snap))
            if snap['kind']=='lot_rework':
                from tools.quality_lot_rework import inspect_snapshot
                return encode(await inspect_snapshot(snap))
            if snap['kind']=='quantity_loss':
                from tools.quality_quantity_loss import inspect_snapshot
                return encode(await inspect_snapshot(snap))
            if snap.get('inspection_effects'):
                from tools.quality_inspection_effects import inspect_snapshot
                return encode(await inspect_snapshot(snap))
            if snap['kind']=='lot_maintenance':
                from tools.quality_lots import get_sampling_lot_details
                from tools.quality_lot_maintenance import get_sampling_lot_maintenance_history
                return encode({'current_lot':json.loads(await get_sampling_lot_details('',snap['target_id'])),
                    'history_evidence':json.loads(await get_sampling_lot_maintenance_history('',snap['target_id'])),
                    'verified_success':False,'state_changed':False,'retry_allowed':False,
                    'note':'当前数量或频率相同不证明本次维护成功；原请求候选及历史可另作只读核对。'})
            if snap['kind']=='sampling_reference':
                from tools.quality_sampling_references import get_sampling_reference_definition
                return encode({'current_configuration':json.loads(await get_sampling_reference_definition(
                    snap['reference_kind'],snap['name'],snap.get('target_id'))),
                    'verified_success':False,'state_changed':False,'retry_allowed':False,
                    'note':'当前表与请求一致不证明本次维护成功；仍需最终审计证据人工核实。'})
            if snap['kind'] in ('lot_inspection','lot_completion'):
                from tools.quality_lots import get_sampling_lot_details
                from tools.quality_lot_execution import get_sampling_lot_history
                return encode({'current_lot':json.loads(await get_sampling_lot_details('',snap['target_id'])),
                    'history_evidence':json.loads(await get_sampling_lot_history('',snap['target_id'])),
                    'verified_success':False,'state_changed':False,'retry_allowed':False,
                    'note':'当前批标志、计数或评论不能证明本次批采样/整批执行成功，不解除占用。'})
            if snap['kind'] in ('inspection_definition','sampling_binding'):
                from tools.quality_definitions import lookup,read_state
                if snap['kind']=='inspection_definition':
                    found=await lookup(snap['entity'],snap['name'],snap['revision'])
                    current=await read_state(snap['definition_kind'],snap['name'],snap['revision']) if found else None
                else:current=await reads.ReadContext(1).get('Products',snap['target_id'])
                return encode({'current_configuration':current,'verified_success':False,'state_changed':False,'retry_allowed':False,
                    'note':'当前配置相同不证明本次维护成功；须人工核对最终事务/变更历史证据。'})
            if snap['kind']=='sampling_lot':
                from tools.quality_lots import get_sampling_lot_details
                return encode({'current_lot':json.loads(await get_sampling_lot_details(snap['name'])),
                    'verified_success':False,'state_changed':False,'retry_allowed':False,
                    'note':'同名抽样批或当前数量不是本次创建的最终因果证据。'})
            if snap['kind']=='inspection':
                from tools.quality_inspection import get_container_inspection_tasks,get_container_inspection_history
                return encode({'current_tasks':json.loads(await get_container_inspection_tasks(snap['name'],snap['target_id'])),
                    'history_evidence':json.loads(await get_container_inspection_history(snap['name'],snap['target_id'])),
                    'verified_success':False,'state_changed':False,'retry_allowed':False,
                    'note':'检验状态、累计数或关联标记不证明本次提交成功；须人工最终证据核实。'})
            current=await reads.find_record(snap['kind'],snap['name']);history=None
            if current:
                key='NonconformanceReportId' if snap['kind']=='ncr' else 'CAPAId'
                history=json.loads(await reads.get_quality_history('ncr' if snap['kind']=='ncr' else 'quality_object',snap['name'],current[key],limit=20))
            return encode({'current_record':current,'history_evidence':history,'verified_success':False,'state_changed':False,'retry_allowed':False,
                'note':'证据仅供人工核对；同名/同值不证明本次提交结果。'})
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy) as error:return encode({'verified_success':False,'state_changed':False,'error':str(error)})


@mcp.tool
async def review_quality_transaction(plan_id:str,plan_digest:str,action:Literal['recover','resolve'],evidence_reference:str,
        review_note:str,conclusion:Literal['committed','not_committed','unknown']='unknown')->str:
    """须确认的本地质量事务审计。recover须有尝试记录且原进程锁已释放，仅in_flight转unknown并保留占用；resolve须人工最终事务/审计证据，unknown保存人工结论。当前状态相同或空历史不够。保留原响应/审计，不调用MES、不开原计划重试。"""
    try:
        if action not in ('recover','resolve') or conclusion not in ('committed','not_committed','unknown'):raise ValueError('审计动作/结论无效。')
        if action=='recover' and conclusion!='unknown':raise ValueError('进程恢复不作服务端结论。')
        if not isinstance(evidence_reference,str) or not 1<=len(evidence_reference.strip())<=2000 or not isinstance(review_note,str) or not 10<=len(review_note.strip())<=4000:raise ValueError('须有证据引用及至少10字符核对说明。')
        with attempt_lock(PLAN_DB,plan_id),db_connect() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone();checked(row,plan_digest)
            if row[2]!=('in_flight' if action=='recover' else 'unknown') or not db.execute('SELECT 1 FROM attempts WHERE plan_id=?',(plan_id,)).fetchone():raise ValueError('当前状态/提交尝试记录不允许该审计。')
            state='unknown' if action=='recover' or conclusion=='unknown' else 'human_'+conclusion
            audit={'action':action,'conclusion':conclusion,'evidence_reference':evidence_reference,'review_note':review_note,
                   'previous_state':row[2],'previous_outcome':json.loads(row[4]) if row[4] else None,'created':time.time()}
            result={'state':state,'verified_success':False,'retry_allowed':False,'original_plan_reusable':False,'human_attestation':audit}
            db.execute('INSERT INTO reviews VALUES (?,?,?)',(uuid.uuid4().hex,plan_id,json.dumps(audit,ensure_ascii=False)))
            db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',(state,json.dumps(result,ensure_ascii=False),plan_id))
        return encode({'state_changed':True,'mes_write_performed':False,**result})
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy) as error:return encode({'state_changed':False,'error':str(error)})
