"""Record explicitly operator-reported, single original-point SPC excursions.

This is a manual-result intake, not a native-engine verdict or an import of
local WECO calculations. Evidence and limits remain caller-reported facts.
"""
import json
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr
from tools import mcp, quality as q, quality_inspection as i, quality_plans as p
from tools import quality_spc_annotations as a, quality_spc as s
from tools.process_context import ReadContext
from tools.modeling_details import raw_json, _has_next_link
from tools.quality_definitions import resources
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SCHEMA = a.SCHEMA | {
    'A_SPCRules':'SPCRulesId,ChangeCount',
    'SPCRuleViolations':'SPCRulesViolationsId,ParentId,SPCViolationId,IsFrozen,ChangeCount',
    'SPCViolation':'SPCRuleViolationId,SPCRuleViolationName,IsFrozen,ChangeCount',
    'RecordSPCViolationHistory':'RecordSPCViolationSummaryId,HistoryMainlineId,TxnId',
    'SPCChartHistoryDetail':'RecordSPCChartViolationHistDId,RecordSPCViolationSummaryId,SPCChartDefId,SPCChartDefName,TxnId',
    'SPCViolationHistoryDetail':('SPCChartViolationsHistDtlsId,ParentId,HistoryId,TxnId,DataPointId,ContainerId,'
        'ChartVariable,CL,LCL,UCL,CollectedDataValue,DerivedChartDataValue,FailureAction,RuleViolated,ViolationName,'
        'HoldReasonId,ResourceId,ResourceStatusCodeId,CustomSPCDataId'),
}
FIELD_CASE = p.FIELD_CASE | {'cl':'CL','lcl':'LCL','ucl':'UCL','eMailMessage':'EMailMessage','spcChartDefName':'SPCChartDefName'}


class ReportedSPCResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_kind: Literal['operator_reported']
    evidence_reference: Annotated[StrictStr, Field(min_length=1,max_length=512)]
    uom_id: StrictStr
    reported_value: StrictStr
    plotted_value: StrictStr
    lcl: StrictStr
    center: StrictStr
    ucl: StrictStr


def reported(value, source):
    report = ReportedSPCResult.model_validate(value)
    q.text(report.evidence_reference,'明确报告的SPC结果来源',512)
    if a.iid(report.uom_id,'报告单位')!=a.iid(source.get('UOMId'),'原始单位'):
        raise ValueError('报告的数值/控制限单位与原始历史单位不同，不自动换算。')
    low, center, high, measured = map(s.number,(report.lcl,report.center,report.ucl,report.reported_value))
    if s.number(report.plotted_value)!=measured:
        raise ValueError('首版只登记明确报告的绘图值等于原值的单点结果，不计算或回填子组/变换值。')
    if not low < center < high: raise ValueError('报告的控制限须LCL<中心<UCL，不能使用规格限或模型猜定限值。')
    try: actual = Decimal(source['DataValue'])
    except (InvalidOperation,ValueError,TypeError): raise ValueError('原始采集值不能精确解析为数值。') from None
    if not actual.is_finite() or Fraction(actual) != measured: raise ValueError('明确报告的数值与原始持久历史值不同。')
    if low <= measured <= high: raise ValueError('首版仅登记明确报告的单点严格越限；边界、区内运行规则及派生值不支持。')
    limits = {}
    for key,num in [('lcl',low),('cl',center),('ucl',high)]:
        native = float(num)
        if Fraction(Decimal(str(native))) != num: raise ValueError('控制限不能无损通过当前Double字符串往返。')
        limits[key] = native
    return report.model_dump(), limits


async def rules_context(before, violation_name=None, expected_rule_id=None, expected_violation_id=None):
    ctx = ReadContext(10); cfg = before['configuration']; chart_ref = cfg['chart']
    chart = await ctx.resolve('SPCChartDefs',{'Name':chart_ref['Name'],'InstanceID':chart_ref['InstanceID']},False)
    if p.digest(chart) != chart_ref['content_digest']: raise ValueError('SPC图与原始上下文不一致。')
    for field in ('EmailNotification','EMailMessage','PutContainerOnHold','SetResourceStatus'):
        if field not in chart or chart[field] not in (None,''): raise ValueError('受限违例登记不支持通知、冻结或资源状态配置；不自动清空配置。')
    if i.wire_bool(chart.get('TriggerOCAPs'),'SPC自动处置') is not False:
        raise ValueError('SPC自动处置开启或未知，不能用仅登记入口代替原生处置。')
    if chart.get('ChartVariable') != before['source']['DataName']:
        raise ValueError('首版要求控制图变量与原始点名完全相同，不推测表达式或派生变量映射。')
    ref = chart.get('SPCRules')
    if not isinstance(ref,dict) or not q.valid_id(q.identifier(ref)): raise ValueError('SPC图没有明确的持久规则引用。')
    if expected_rule_id is not None and a.iid(q.identifier(ref))!=expected_rule_id:
        raise ValueError('原图规则引用已变化，不跟随替代规则。')
    rule = await ctx.resolve('SPCRules',ref,False); key = a.iid(rule['InstanceID'])
    if i.wire_bool(rule.get('IsFrozen'),'SPC规则冻结状态') is not False: raise ValueError('SPC规则已冻结或状态未知。')
    token = await a.token('A_SPCRules','SPCRulesId',key)
    sql = ('SELECT TOP 101 m.SPCRulesViolationsId,m.ParentId,m.SPCViolationId,m.IsFrozen,m.ChangeCount,'
           'c.SPCRuleViolationName AS ViolationName,c.IsFrozen AS ViolationIsFrozen,c.ChangeCount AS ViolationChangeCount '
           'FROM SPCRuleViolations m LEFT JOIN SPCViolation c ON c.SPCRuleViolationId=m.SPCViolationId '
           f"WHERE m.ParentId='{key}' ORDER BY m.SPCRulesViolationsId")
    rows = await q.sql_rows(sql,101)
    if not rows or len(rows)>100: raise ValueError('规则违例配置为空或超过100项，不使用部分配置。')
    members=[]
    for raw in rows:
        row=dict(raw)
        for field in ('SPCRulesViolationsId','ParentId','SPCViolationId'): row[field]=a.iid(row.get(field),field)
        if row['ParentId']!=key: raise ValueError('SPC规则违例配置父引用不符。')
        row['IsFrozen']=i.wire_bool(row.get('IsFrozen'),'规则违例成员冻结状态')
        row['ChangeCount']=i.wire_int(row.get('ChangeCount'),'规则违例成员变更计数')
        row['ViolationName']=q.text(row.get('ViolationName'),'规则清单违例名称',255)
        row['ViolationIsFrozen']=i.wire_bool(row.get('ViolationIsFrozen'),'规则清单违例冻结状态')
        row['ViolationChangeCount']=i.wire_int(row.get('ViolationChangeCount'),'规则清单违例变更计数')
        if row['ChangeCount']<0 or row['ViolationChangeCount']<0: raise ValueError('规则违例成员变更计数无效。')
        members.append(row)
    if len({r['SPCRulesViolationsId'] for r in members})!=len(members) or len({r['SPCViolationId'] for r in members})!=len(members):
        raise ValueError('规则违例配置重复，不猜测参数对应关系。')
    selected=None
    if violation_name is not None:
        vref={'Name':q.text(violation_name,'明确报告的违例名称',255)}
        if expected_violation_id is not None:vref['InstanceID']=expected_violation_id
        obj=await ctx.resolve('SPCViolations',vref,False)
        vid=a.iid(obj['InstanceID']); match=[r for r in members if r['SPCViolationId']==vid]
        if (len(match)!=1 or match[0]['IsFrozen'] or match[0]['ViolationIsFrozen'] or match[0]['ViolationName']!=obj['Name']
                or i.wire_bool(obj.get('IsFrozen'),'违例定义冻结状态') is not False):
            raise ValueError('明确报告的违例不属于当前完整规则清单或已冻结。')
        vt=await a.token('SPCViolation','SPCRuleViolationId',vid)
        if vt['ChangeCount']!=match[0]['ViolationChangeCount']:raise ValueError('规则清单与违例定义版本计数不一致。')
        selected={'InstanceID':vid,'Name':obj['Name'],'ChangeCount':vt['ChangeCount'],'content_digest':p.digest(obj)}
    if await a.token('A_SPCRules','SPCRulesId',key)!=token or await q.sql_rows(sql,101)!=rows:
        raise ValueError('读取SPC规则期间规则或成员发生变化。')
    return {'chart_variable':chart['ChartVariable'],'rule':{'InstanceID':key,'Name':rule['Name'],
            'ChangeCount':token['ChangeCount'],'content_digest':p.digest(rule)},'members':members,'selected_violation':selected,
            'compiled_rule_file_verified':False,'external_engine_result_verified':False,
            'chart_type_semantics_verified':False}


async def violation_history(point_id):
    key=a.iid(point_id)
    columns=('v.SPCChartViolationsHistDtlsId,v.ParentId,v.HistoryId,v.TxnId,v.DataPointId,v.ContainerId,v.ChartVariable,'
             'v.CL,v.LCL,v.UCL,v.CollectedDataValue,v.DerivedChartDataValue,v.FailureAction,v.RuleViolated,v.ViolationName,'
             'v.HoldReasonId,v.ResourceId,v.ResourceStatusCodeId,v.CustomSPCDataId,'
             'c.RecordSPCChartViolationHistDId,c.RecordSPCViolationSummaryId,c.SPCChartDefId,c.SPCChartDefName,c.TxnId AS ChartTxnId,'
             'h.HistoryMainlineId,h.TxnId AS SummaryTxnId,m.HistoryId AS MainlineHistoryId,m.TxnId AS MainlineTxnId,'
             'm.TxnType,m.ReversalStatus,m.TxnDateGMT,m.Comments')
    rows=await q.sql_rows('SELECT TOP 101 '+columns+' FROM SPCViolationHistoryDetail v '
        'LEFT JOIN SPCChartHistoryDetail c ON c.RecordSPCChartViolationHistDId=v.ParentId '
        'LEFT JOIN RecordSPCViolationHistory h ON h.RecordSPCViolationSummaryId=c.RecordSPCViolationSummaryId '
        'LEFT JOIN HistoryMainline m ON m.HistoryMainlineId=h.HistoryMainlineId '
        f"WHERE v.DataPointId='{key}' AND (m.ReversalStatus=1 OR m.HistoryMainlineId IS NULL) ORDER BY v.SPCChartViolationsHistDtlsId",101)
    if len(rows)>100: raise ValueError('原始点违例历史超过100项，不能用部分记录核对或准备。')
    result=[];seen=set()
    for raw in rows:
        row=dict(raw)
        for field in ('SPCChartViolationsHistDtlsId','ParentId','HistoryId','TxnId','DataPointId','ContainerId',
                      'RecordSPCChartViolationHistDId','RecordSPCViolationSummaryId','SPCChartDefId','ChartTxnId',
                      'HistoryMainlineId','SummaryTxnId','MainlineHistoryId','MainlineTxnId'):
            row[field]=a.iid(row.get(field),field)
        if (row['DataPointId']!=key or row['ParentId']!=row['RecordSPCChartViolationHistDId']
                or row['HistoryId']!=row['MainlineHistoryId']
                or len({row['TxnId'],row['ChartTxnId'],row['SummaryTxnId'],row['MainlineTxnId']})!=1
                or row['SPCChartViolationsHistDtlsId'] in seen
                or i.wire_int(row.get('TxnType'),'违例历史事务类型')!=8396
                or i.wire_int(row.get('ReversalStatus'),'违例历史冲销状态')!=1):
            raise ValueError('原点违例历史父链、事务组、类型或唯一性不符。')
        for field in ('ChartVariable','CollectedDataValue','DerivedChartDataValue','FailureAction','RuleViolated','ViolationName',
                      'CL','LCL','UCL','SPCChartDefName','TxnDateGMT','Comments','HoldReasonId','ResourceId','ResourceStatusCodeId','CustomSPCDataId'):
            if field not in row: raise ValueError('原点违例历史字段不完整。')
        if row['CustomSPCDataId'] not in (None,''): raise ValueError('原点违例历史混入自定义数据源。')
        seen.add(row['SPCChartViolationsHistDtlsId']);result.append(row)
    return result


async def context(args, original=None, original_rules=None):
    q.text(args['container_name'],'容器名称');q.text(args['spc_chart_name'],'SPC图名称');a.iid(args['data_point_history_id'])
    for field in ('container_instance_id','spc_chart_instance_id'):
        if args.get(field) is not None:a.iid(args[field])
    if args.get('violation_name') is not None:q.text(args['violation_name'],'明确报告的违例名称',255)
    await a.schema_check(SCHEMA)
    before=await a.context(args,original)
    before['rules']=await rules_context(before,args.get('violation_name'),
        original_rules['rule']['InstanceID'] if original_rules else None,
        original_rules['selected_violation']['InstanceID'] if original_rules else None)
    before['violation_history']=await violation_history(before['source']['DataPointHistoryDetailId'])
    if any(row['ContainerId']!=before['source']['ContainerId'] for row in before['violation_history']):
        raise ValueError('原点违例历史的原容器关联不符。')
    return before


def body_for(before, report, marker):
    source=before['source'];rules=before['rules'];chart=before['configuration']['chart']
    _,limits=reported(report,source)
    violation={'dataPoint':{'instanceId':source['DataPointHistoryDetailId'],'parent':{'instanceId':source['DataPointHistoryId']}},
               'dataPointName':source['DataName'],'container':before['container'],
               'chartVariable':rules['chart_variable'],'collectedDataValue':source['DataValue'],
               'derivedChartDataValue':report['plotted_value'],'failureAction':'Violation','ruleViolated':rules['rule']['Name'],
               'violationName':rules['selected_violation']['Name'],**limits}
    detail={'spcChartDef':{'instanceId':chart['InstanceID'],'name':chart['Name']},'spcChartDefName':chart['Name'],
            'violations':[violation]}
    return {'serviceDetails':[detail],'comments':marker+' operator-reported SPC result'}


def wire_matches(expected, actual, field=''):
    if field in ('cl','lcl','ucl'):
        if type(actual) not in (int,float,str): return False
        try:
            number=Decimal(str(actual));return number.is_finite() and number==Decimal(str(expected))
        except InvalidOperation:return False
    if isinstance(expected,dict):
        if not isinstance(actual,dict):return False
        for k,v in expected.items():
            if k=='instanceId':
                if q.identifier(actual)!=v:return False
            elif not wire_matches(v,actual.get(FIELD_CASE.get(k,k[0].upper()+k[1:])),k) or FIELD_CASE.get(k,k[0].upper()+k[1:]) not in actual:
                return False
        return True
    if isinstance(expected,list):return isinstance(actual,list) and len(expected)==len(actual) and all(wire_matches(x,y,field) for x,y in zip(expected,actual))
    return type(expected) is type(actual) and expected==actual


async def preview(body):
    params={'execute':'false','$select':'Comments,ServiceDetails,ESigRequirement,AdditionalESigReqs',
            '$expand':'ServiceDetails($select=SPCChartDef,SPCChartDefName,EMailMessage,Recipients,Message,FileName,FileDirectory;'
                      '$expand=Violations($select=DataPoint,DataPointName,Container,ChartVariable,CollectedDataValue,DerivedChartDataValue,'
                      'FailureAction,RuleViolated,ViolationName,HoldReason,Resource,ResourceStatusCode,CL,LCL,UCL))'}
    raw=await request('POST','/api/RecordSPCViolation',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,params=params,response_formatter=raw_json)
    try: outer=json.loads(raw)
    except (ValueError,TypeError):raise ValueError('SPC违例非执行预览失败：'+str(raw)[:1000]) from None
    data=outer.get('data',outer) if isinstance(outer,dict) else None
    if (not isinstance(data,dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error') or data.get('ErrorCode')
            or _has_next_link(data) or not wire_matches(body,data)):
        raise ValueError('SPC违例原点、父链、实际值、报告限值、规则或无通知/处置字段未完整回显。')
    detail=data['ServiceDetails'][0];child=detail['Violations'][0]
    for field in ('EMailMessage','Recipients','Message','FileName','FileDirectory'):
        if field not in detail or detail[field] not in (None,''):
            raise ValueError('SPC违例预览派生了通知/文件内容或默认未知，不能提交。')
    for field in ('HoldReason','Resource','ResourceStatusCode'):
        if field not in child or child[field] is not None:
            raise ValueError('SPC违例预览派生了冻结/资源动作或默认未知，不能提交。')
    # The live EDM declares the two nested collection types, but does not
    # expose CDOTypeId on either type. Reject conflicting optional type echoes.
    for item, type_name, cdo_id in ((detail,'RecordSPCViolationDetail',8400), (child,'SPCViolationDetail',8401)):
        if (item.get('@odata.type') not in (None, '#shopfloor.'+type_name)
                or ('CDOTypeId' in item and i.wire_int(item['CDOTypeId'], '违例明细类型') != cdo_id)):
            raise ValueError('SPC违例明细类型与原生契约不符。')
    if ('ESigRequirement' not in data or data['ESigRequirement'] is not None or 'AdditionalESigReqs' not in data
            or data['AdditionalESigReqs'] not in (None,[])):
        raise ValueError('SPC违例签名要求非空或未知，受限登记不覆盖电子签名。')
    return {'params':params,'verified_input_echo':body,'external_engine_result_verified':False,
            'reported_limits_verified_against_engine':False,'evidence_reference_verified':False,
            'dynamic_validation_verified':False,'violation_written_verified':False,
            'note':'登记调用方明确报告的结果和控制限，不证明外部报告、编译规则或引擎计算真实有效；系统确认不能代替电子签名。'}


async def build_violation(args, marker):
    report=ReportedSPCResult.model_validate(args['reported_result']).model_dump()
    q.text(args['violation_name'],'明确报告的违例名称',255)
    before=await context(args)
    if any(row['Exclude'] for row in before['annotation_history']):raise ValueError('原始点存在有效排除记录，不能登记受限越限违例。')
    if any(row['SPCChartDefId']==before['configuration']['chart']['InstanceID']
            and row['ViolationName']==before['rules']['selected_violation']['Name'] for row in before['violation_history']):
        raise ValueError('该原点/图/违例已有有效记录，不重复登记；请检查原历史。')
    body=body_for(before,report,marker);echo=await preview(body)
    if await context(args)!=before:raise ValueError('准备期间原始点、规则、配置或历史发生变化。')
    echo['reported_result']=report
    source=before['source'];cfg=before['configuration'];rules=before['rules']
    return {'kind':'spc_violation','spc_evidence_version':1,'action':'record_reported','service':'RecordSPCViolation','name':cfg['chart']['Name']+'/'+source['DataPointHistoryDetailId'],
            'target_id':source['DataPointHistoryDetailId'],'scope_key':'spc:point:'+source['DataPointHistoryDetailId'],
            'before':before,'body':body,'preview':echo,'marker':marker,'arguments':args,'environment':p.environment(),
            'resource_reads':sorted(resources('SPCChartDefs',cfg['chart'])|resources('UserDataCollectionDefs',cfg['definition'])
                |resources('SPCRules',rules['rule'])|resources('SPCViolations',rules['selected_violation'])
                |{'modeling:SPCChartDefGroups:id:'+cfg['group']['InstanceID']}),
            'resource_writes':['runtime:spc-point:id:'+source['DataPointHistoryDetailId'],
                               'runtime:container:id:'+source['ContainerId']]}


def validate_transport(snap):
    if (snap.get('kind')!='spc_violation' or snap.get('action')!='record_reported' or snap.get('service')!='RecordSPCViolation'
            or not isinstance(snap.get('marker'),str) or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap['marker'])
            or snap.get('target_id')!=snap['before']['source']['DataPointHistoryDetailId']
            or snap['body']!=body_for(snap['before'],snap['arguments']['reported_result'],snap['marker'])):
        raise ValueError('SPC违例服务、来源、原点、限值、规则或允许字段不符。')
    # Python equates False with 0.0; numeric payloads must keep their native type.
    child=snap['body']['serviceDetails'][0]['violations'][0]
    if any(type(child[k]) is not float for k in ('lcl','cl','ucl')):raise ValueError('SPC违例控制限必须保持已核实的Double类型。')


async def inspect_snapshot(snap):
    args=dict(snap['arguments']);args.update(data_point_history_id=snap['target_id'],
        container_instance_id=snap['before']['source']['ContainerId'],spc_chart_instance_id=snap['before']['configuration']['chart']['InstanceID'])
    current=await context(args,original=snap['before']['source'],original_rules=snap['before']['rules'])
    detail = snap['body']['serviceDetails'][0]
    violation = detail['violations'][0]
    expected = {'DataPointId': snap['target_id'], 'ContainerId': violation['container']['instanceId'],
                'SPCChartDefId': detail['spcChartDef']['instanceId'], 'SPCChartDefName': detail['spcChartDefName']}
    expected.update({field[0].upper()+field[1:]: violation[field] for field in
                    ('chartVariable','collectedDataValue','derivedChartDataValue','failureAction','ruleViolated','violationName')})

    def matches(row):
        return (all(row[field] == value for field,value in expected.items())
                and all(wire_matches(violation[field], row[FIELD_CASE[field]], field) for field in ('cl','lcl','ucl'))
                and all(row[field] in ('',None) for field in ('HoldReasonId','ResourceId','ResourceStatusCodeId','CustomSPCDataId')))

    candidate = a.history_candidates(snap['before']['violation_history'], current['violation_history'],
        'SPCChartViolationsHistDtlsId', snap['body']['comments'], matches)
    return {'original_point_id':snap['target_id'],'current_context':current,'verified_success':False,'state_changed':False,
            'history_correlation':candidate,
            'retry_allowed':False,'request_correlation_verified':False,
            'note':'仅原点/原图的当前违例证据；相同文本/数值/HTTP200不证明本次提交成功，不解锁或重试，最终证据人工review。'}


@mcp.tool
async def get_spc_violation_context(container_name:str,data_point_history_id:str,spc_chart_name:str,violation_name:str|None=None,
        container_instance_id:str|None=None,spc_chart_instance_id:str|None=None)->str:
    """只读原始直接数值采集点、SPC图/固定定义/组、当前完整规则成员和最多100条原点违例历史。
    可明确violation_name核对规则归属；不提供时列完整候选ID/名称/冻结状态。仅支持无通知、无自动处置且变量与原点名完全相同的原生图。
    不运行SPC引擎，不核实外部控制限/报告，不放行；读取完整不代表允许提交或本次提交成功。
    """
    try:
        args=dict(container_name=container_name,data_point_history_id=data_point_history_id,spc_chart_name=spc_chart_name,
                  violation_name=violation_name,container_instance_id=container_instance_id,spc_chart_instance_id=spc_chart_instance_id)
        first=await context(args)
        if await context(args)!=first:raise ValueError('SPC违例上下文读取期间发生变化。')
        return q.encode({'complete':True,**first,'native_spc_result_verified':False,'release_authorized':False})
    except (ValueError,TypeError,KeyError,AttributeError) as exc:return q.encode({'complete':False,'error':str(exc)})


@mcp.tool
async def prepare_spc_violation(container_name:str,data_point_history_id:str,spc_chart_name:str,violation_name:str,
        reported_result:ReportedSPCResult,container_instance_id:str|None=None,spc_chart_instance_id:str|None=None)->str:
    """准备原生RecordSPCViolation单点登记计划。reported_result必须明确operator_reported、结果来源、原始值、绘图值、单位ID及同单位报告LCL/中心/UCL。
    这是调用方明确报告结果的登记，不计算或认证现场引擎；不得将本地WECO输出/规格限/模型猜定值当外部SPC报告。
    仅未排除直接数值历史点、配置清单内明确违例、原值严格越限、图变量等于原点名；不支持区内运行规则/派生值/批量/自定义源。
    图有通知、冻结、资源状态、OCAP或未知签名要求则拒绝，不自动关闭配置。完整非执行回显及原关联重读后保存计划。
    execute_quality_transaction系统确认单次提交，unknown不重试；新计划用reconcile_quality_execution核对完整登记，再resolve_history确认重读结案。旧快照人工取证；登记成功不认证报告来源、引擎或控制限。
    """
    report=reported_result.model_dump() if isinstance(reported_result,BaseModel) else reported_result
    return await p.prepare('spc_violation',dict(container_name=container_name,data_point_history_id=data_point_history_id,
        spc_chart_name=spc_chart_name,violation_name=violation_name,reported_result=report,
        container_instance_id=container_instance_id,spc_chart_instance_id=spc_chart_instance_id))
