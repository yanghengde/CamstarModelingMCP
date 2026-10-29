"""Restricted native annotations on persistent, directly collected numeric data.

No local SPC verdict is promoted to a native violation. All identifiers and
current configuration are read again before the shared one-attempt executor.
"""
import json
import re

from tools import mcp, quality as q, quality_inspection as i, quality_plans as p
from tools.quality_definitions import resources
from tools.quality_lot_execution import runtime_claim
from tools.process_context import ReadContext
from tools.modeling_details import raw_json, _has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SCHEMA = {
    'DataPointHistoryDetail': 'DataPointHistoryDetailId,DataPointHistoryId,DataCollectionDefId,DataPointId,HistoryId,TxnId,ChangeCount,DataName,DataType,DataValue,UOMId',
    'DataPointHistory': 'DataPointHistoryId,HistoryMainlineId,HistoryId,TxnId,ChangeCount,DataCollectionDefId',
    'HistoryMainline': 'HistoryMainlineId,HistoryId,TxnId,ContainerId,ChangeCount,ReversalStatus,TxnDateGMT,TxnType,Comments',
    'SPCChartDef': 'SPCChartDefId,ChangeCount',
    'DataCollectionDef': 'DataCollectionDefId,ChangeCount,SPCChartDefGroupId',
    'AddSPCAnnotationHistory': 'AddSPCAnnotationHistoryId,HistoryMainlineId,HistoryId,TxnId,DataPointId,DataCollectionDefId,SPCChartDefId,Annotation,Exclude,CustomSPCDataId,ExternalDataId',
}
SOURCE_FIELDS = ('DataPointHistoryDetailId,DataPointHistoryId,DataCollectionDefId,DataPointId,HistoryId,TxnId,'
                 'ChangeCount,DataName,DataType,DataValue,UOMId,ParentChangeCount,ParentDefinitionId,'
                 'ParentHistoryId,ParentTxnId,HistoryMainlineId,MainlineHistoryId,MainlineTxnId,ContainerId,'
                 'MainlineChangeCount,ReversalStatus,TxnDateGMT,TxnType')
HISTORY_FIELDS = ('AddSPCAnnotationHistoryId,HistoryMainlineId,HistoryId,TxnId,DataPointId,DataCollectionDefId,'
                  'SPCChartDefId,Annotation,Exclude,CustomSPCDataId,ExternalDataId,MainlineHistoryId,'
                  'MainlineTxnId,ReversalStatus,TxnType,TxnDateGMT,Comments')


def iid(value, label='ID'):
    if not q.valid_id(value): raise ValueError(label+'须为16位十六进制ID。')
    return value.lower()


async def schema_check():
    clauses = ["(TABLE_NAME='"+table+"' AND COLUMN_NAME IN ("+','.join("'"+c+"'" for c in columns.split(','))+'))'
               for table, columns in SCHEMA.items()]
    rows = await q.sql_rows('SELECT TOP 201 TABLE_NAME,COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS '
                            "WHERE TABLE_SCHEMA='dbo' AND ("+' OR '.join(clauses)+') ORDER BY TABLE_NAME,COLUMN_NAME', 201)
    expected = {(table, col) for table, columns in SCHEMA.items() for col in columns.split(',')}
    actual = [(r.get('TABLE_NAME'), r.get('COLUMN_NAME')) for r in rows]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError('当前部署的SPC注释/原始历史字段未完整核实，不能准备写入。')
    return sorted(actual)


async def original_point(key):
    key = iid(key, '原始采集历史点')
    columns = ('d.DataPointHistoryDetailId,d.DataPointHistoryId,d.DataCollectionDefId,d.DataPointId,d.HistoryId,d.TxnId,'
               'd.ChangeCount,d.DataName,d.DataType,d.DataValue,d.UOMId,h.ChangeCount AS ParentChangeCount,'
               'h.DataCollectionDefId AS ParentDefinitionId,h.HistoryId AS ParentHistoryId,h.TxnId AS ParentTxnId,'
               'm.HistoryMainlineId,m.HistoryId AS MainlineHistoryId,m.TxnId AS MainlineTxnId,m.ContainerId,'
               'm.ChangeCount AS MainlineChangeCount,m.ReversalStatus,m.TxnDateGMT,m.TxnType')
    rows = await q.sql_rows('SELECT TOP 2 '+columns+' FROM DataPointHistoryDetail d '
                            'INNER JOIN DataPointHistory h ON h.DataPointHistoryId=d.DataPointHistoryId '
                            'INNER JOIN HistoryMainline m ON m.HistoryMainlineId=h.HistoryMainlineId '
                            f"WHERE d.DataPointHistoryDetailId='{key}'", 2)
    if len(rows) != 1 or not set(SOURCE_FIELDS.split(',')) <= rows[0].keys():
        raise ValueError('原始采集点不存在、不唯一或历史链不完整。')
    row = dict(rows[0])
    for field in ('DataPointHistoryDetailId','DataPointHistoryId','DataCollectionDefId','DataPointId','HistoryId','TxnId',
                  'ParentDefinitionId','ParentHistoryId','ParentTxnId','HistoryMainlineId','MainlineHistoryId','MainlineTxnId','ContainerId'):
        row[field] = iid(row[field], field)
    if (row['DataPointHistoryDetailId'] != key or row['DataCollectionDefId'] != row['ParentDefinitionId']
            or len({row['HistoryId'],row['ParentHistoryId'],row['MainlineHistoryId']}) != 1
            or len({row['TxnId'],row['ParentTxnId'],row['MainlineTxnId']}) != 1):
        raise ValueError('原始采集点的父记录、定义或事务组不一致。')
    for field in ('ChangeCount','ParentChangeCount','MainlineChangeCount','ReversalStatus','TxnType','DataType'):
        row[field] = i.wire_int(row[field], field)
    if any(row[f] < 0 for f in ('ChangeCount','ParentChangeCount','MainlineChangeCount')):
        raise ValueError('原始采集历史变更计数无效。')
    if row['ReversalStatus'] != 1 or row['DataType'] not in (1,2,3,9):
        raise ValueError('仅对未冲销的直接数值采集历史准备SPC注释。')
    q.text(row['DataName'], '原始点名')
    if not isinstance(row['DataValue'], str) or not row['DataValue'].strip(): raise ValueError('原始采集值未返回。')
    if row['UOMId'] not in (None,''): row['UOMId'] = iid(row['UOMId'], '原始单位')
    if not isinstance(row['TxnDateGMT'], str) or not row['TxnDateGMT'].strip(): raise ValueError('原始采集时间未知。')
    return row


async def annotation_history(point_id):
    key = iid(point_id)
    # The native GetSPCAnnotations query does NOT filter by chart. Preserve all
    # charts here, so a prior exclusion cannot be hidden by changing chart name.
    columns = ('a.AddSPCAnnotationHistoryId,a.HistoryMainlineId,a.HistoryId,a.TxnId,a.DataPointId,'
               'a.DataCollectionDefId,a.SPCChartDefId,a.Annotation,a.Exclude,a.CustomSPCDataId,a.ExternalDataId,'
               'm.HistoryId AS MainlineHistoryId,m.TxnId AS MainlineTxnId,m.ReversalStatus,m.TxnType,m.TxnDateGMT,m.Comments')
    rows = await q.sql_rows('SELECT TOP 101 '+columns+' FROM AddSPCAnnotationHistory a '
                            'INNER JOIN HistoryMainline m ON m.HistoryMainlineId=a.HistoryMainlineId '
                            f"WHERE a.DataPointId='{key}' AND m.ReversalStatus=1 ORDER BY a.AddSPCAnnotationHistoryId", 101)
    if len(rows) > 100: raise ValueError('原始点注释超过100条，不能用部分历史准备写入。')
    result = []; seen = set()
    for raw in rows:
        if not set(HISTORY_FIELDS.split(',')) <= raw.keys(): raise ValueError('SPC注释历史字段不完整。')
        row = dict(raw)
        for field in ('AddSPCAnnotationHistoryId','HistoryMainlineId','HistoryId','TxnId','DataPointId',
                      'DataCollectionDefId','SPCChartDefId','MainlineHistoryId','MainlineTxnId'):
            row[field] = iid(row[field], field)
        if (row['DataPointId'] != key or row['HistoryId'] != key or row['MainlineHistoryId'] != key
                or row['TxnId'] != row['MainlineTxnId'] or row['AddSPCAnnotationHistoryId'] in seen):
            raise ValueError('SPC注释历史原点、父链、事务组或唯一性不符。')
        row['Exclude'] = i.wire_bool(row['Exclude'], '历史排除标志')
        row['ReversalStatus'] = i.wire_int(row['ReversalStatus'], '历史冲销状态')
        row['TxnType'] = i.wire_int(row['TxnType'], '注释服务类型')
        if row['TxnType'] != 8393 or row['ReversalStatus'] != 1:
            raise ValueError('SPC注释历史服务类型或冲销状态未知。')
        if (not isinstance(row['Annotation'], str) or row['CustomSPCDataId'] not in (None,'')
                or row['ExternalDataId'] not in (None,'')):
            raise ValueError('SPC注释历史包含外部源或注释字段未知。')
        seen.add(row['AddSPCAnnotationHistoryId']); result.append(row)
    return sorted(result, key=lambda r: r['AddSPCAnnotationHistoryId'])


async def token(table, column, key, extra=''):
    columns = column+',ChangeCount'+(','+extra if extra else '')
    rows = await q.sql_rows(f"SELECT {columns} FROM {table} WHERE {column}='{iid(key)}'", 1)
    if len(rows) != 1 or iid(rows[0].get(column)) != key: raise ValueError('SPC配置持久身份不符。')
    row = dict(rows[0]); row[column] = key; row['ChangeCount'] = i.wire_int(row.get('ChangeCount'), 'SPC配置变更计数')
    if extra: row[extra] = iid(row.get(extra),extra)
    if row['ChangeCount'] < 0: raise ValueError('SPC配置变更计数无效。')
    return row


async def configuration(point, chart_name, chart_id=None):
    ctx = ReadContext(12)
    ref = {'Name': q.text(chart_name, 'SPC图名称')}
    if chart_id is not None: ref['InstanceID'] = iid(chart_id, 'SPC图ID')
    chart = await ctx.resolve('SPCChartDefs', ref, False)
    chart_key = iid(chart['InstanceID'])
    if i.wire_bool(chart.get('IsFrozen'), 'SPC图冻结状态') is not False or i.wire_bool(chart.get('RecordAgainstCustomSPCDataSrc'), 'SPC数据源') is not False:
        raise ValueError('SPC图已冻结或不是已明确的Camstar原生数据源。')
    chart_token = await token('SPCChartDef','SPCChartDefId',chart_key)
    definition = await ctx.resolve('UserDataCollectionDefs', {'InstanceID': point['DataCollectionDefId']}, True)
    if (i.wire_int(definition.get('Status'), '原始采集定义状态') != 1
            or i.wire_bool(definition.get('IsFrozen'), '原始采集定义冻结状态') is not False):
        raise ValueError('仅支持Active且未冻结的普通数值采集定义，暂不支持分组/自定义源。')
    q.text(definition.get('Name'), '采集定义名'); q.text(definition.get('Revision'), '采集定义版本')
    dc_token = await token('DataCollectionDef','DataCollectionDefId',point['DataCollectionDefId'],'SPCChartDefGroupId')
    group_key = iid(q.identifier(definition.get('SPCChartDefGroup')), '采集定义SPC组')
    if dc_token.get('SPCChartDefGroupId') != group_key: raise ValueError('原始采集定义SPC组与持久记录不一致。')
    group = await ctx.get('SPCChartDefGroups', group_key, {'$expand':'ResolvedEntries'})
    entries = group.get('ResolvedEntries')
    if (q.identifier(group) != group_key or _has_next_link(group) or not isinstance(entries,list) or len(entries) > 100
            or i.wire_bool(group.get('IsFrozen'), 'SPC组冻结状态') is not False):
        raise ValueError('SPC组未完整展开、超过100项或已冻结。')
    normalized = []
    for row in entries:
        normalized.append({'InstanceID': iid(q.identifier(row), 'SPC组成员'), 'Name': q.text(row.get('Name'),'SPC组成员名')})
    if len({r['InstanceID'] for r in normalized}) != len(normalized): raise ValueError('SPC组成员重复。')
    if {'InstanceID':chart_key,'Name':chart['Name']} not in normalized: raise ValueError('所选SPC图不属于原采集定义的完整SPC组。')
    if (await token('SPCChartDef','SPCChartDefId',chart_key) != chart_token
            or await token('DataCollectionDef','DataCollectionDefId',point['DataCollectionDefId'],'SPCChartDefGroupId') != dc_token):
        raise ValueError('SPC配置读取期间发生变化。')
    return {'chart': {'InstanceID':chart_key,'Name':chart['Name'],'ChangeCount':chart_token['ChangeCount'],'content_digest':p.digest(chart)},
            'definition': {'InstanceID':point['DataCollectionDefId'],'Name':definition['Name'],'Revision':definition['Revision'],
                           'ChangeCount':dc_token['ChangeCount'],'content_digest':p.digest(definition)},
            'group': {'InstanceID':group_key,'content_digest':p.digest(group),'entries':sorted(normalized,key=lambda r:r['InstanceID'])}}


async def context(args, original=None):
    q.text(args['container_name'], '容器名称'); q.text(args['spc_chart_name'], 'SPC图名称')
    iid(args['data_point_history_id'], '原始采集历史点')
    for field in ('container_instance_id','spc_chart_instance_id'):
        if args.get(field) is not None: iid(args[field],field)
    await schema_check()
    source = await original_point(args['data_point_history_id'])
    if original is not None and any(source[f] != original[f] for f in (
            'DataPointHistoryDetailId','DataPointHistoryId','DataCollectionDefId','DataPointId','HistoryId',
            'TxnId','HistoryMainlineId','ContainerId')):
        raise ValueError('原始历史点的持久关联已变化，不跟随替代父记录或定义。')
    container, _ = await q.container_identity(args['container_name'], args.get('container_instance_id'))
    if iid(container['instanceId']) != source['ContainerId']: raise ValueError('原始采集历史不属于明确的目标容器。')
    cfg = await configuration(source, args['spc_chart_name'], args.get('spc_chart_instance_id'))
    histories = await annotation_history(source['DataPointHistoryDetailId'])
    if any(r['DataCollectionDefId'] != source['DataCollectionDefId'] for r in histories):
        raise ValueError('原点既有注释引用的采集定义与原始记录不符。')
    return {'source':source,'container':container,'configuration':cfg,'annotation_history':histories}


async def preview(body, before):
    params = {'execute':'false','$select':'DataPoint,SPCChartDef,Annotation,Exclude,Comments,DataCollectionDef,ESigRequirement,AdditionalESigReqs'}
    raw = await request('POST','/api/AddSPCAnnotation',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                        body=body,params=params,response_formatter=raw_json)
    try: outer = json.loads(raw)
    except (ValueError,TypeError): raise ValueError('SPC注释非执行回显失败：'+str(raw)[:1000]) from None
    data = outer.get('data',outer) if isinstance(outer,dict) else None
    if (not isinstance(data,dict) or outer.get('error') or outer.get('ErrorCode') or data.get('error')
            or data.get('ErrorCode') or _has_next_link(data) or not p.wire_matches(body,data)):
        raise ValueError('SPC注释原始点、父引用、图、文本或Exclude=false未完整回显。')
    definition = before['configuration']['definition']
    dref = data.get('DataCollectionDef')
    if (q.identifier(dref) != definition['InstanceID'] or not isinstance(dref,dict)
            or dref.get('Name') != definition['Name'] or dref.get('Revision') != definition['Revision']):
        raise ValueError('MES根据原始点派生的采集定义版本不符。')
    if ('ESigRequirement' not in data or data['ESigRequirement'] is not None
            or 'AdditionalESigReqs' not in data or data['AdditionalESigReqs'] not in (None,[])):
        raise ValueError('SPC注释签名要求非空或未知，本受限入口不覆盖电子签名。')
    return {'params':params,'verified_input_echo':body,'derived_definition':dref,
            'signature_requirement':None,'additional_signature_requirements':[],
            'dynamic_validation_verified':False,'annotation_written_verified':False,
            'note':'仅非执行输入回显，不证明权限、部署扩展或业务提交；不请求排除数据/冻结/通知/违例记录。'}


async def build_annotation(args, marker):
    annotation = args.get('annotation')
    if not isinstance(annotation,str) or not annotation.strip() or len(annotation) > 2000 or '\x00' in annotation:
        raise ValueError('SPC注释须为1..2000字符非空文本。')
    before = await context(args)
    if any(r['Exclude'] and r['ReversalStatus'] == 1 for r in before['annotation_history']):
        raise ValueError('原点任一图存在未冲销的排除记录；不通过Exclude=false覆盖排除或恢复数据。')
    source = before['source']; cfg = before['configuration']
    body = {'dataPoint':{'instanceId':source['DataPointHistoryDetailId'],'parent':{'instanceId':source['DataPointHistoryId']}},
            'spcChartDef':{'name':cfg['chart']['Name'],'instanceId':cfg['chart']['InstanceID']},
            'annotation':annotation,'exclude':False,'comments':marker}
    echo = await preview(body,before)
    if await context(args) != before: raise ValueError('准备期间原始数据、容器归属、SPC配置或注释历史发生变化。')
    return {'kind':'spc_annotation','action':'annotate','service':'AddSPCAnnotation',
            'name':cfg['chart']['Name']+'/'+source['DataPointHistoryDetailId'],'target_id':source['DataPointHistoryDetailId'],
            'scope_key':'spc:point:'+source['DataPointHistoryDetailId'],'before':before,'body':body,'preview':echo,
            'resource_reads':sorted(resources('SPCChartDefs',cfg['chart']) | resources('UserDataCollectionDefs',cfg['definition'])
                                    | {'modeling:SPCChartDefGroups:id:'+cfg['group']['InstanceID']}),
            'resource_writes':[runtime_claim('container',source['ContainerId']),'runtime:spc-point:id:'+source['DataPointHistoryDetailId']],
            'marker':marker,'arguments':args,'environment':p.environment()}


def validate_transport(snap):
    body = snap['body']; before = snap['before']; src = before['source']; chart = before['configuration']['chart']
    expected = {'dataPoint':{'instanceId':src['DataPointHistoryDetailId'],'parent':{'instanceId':src['DataPointHistoryId']}},
                'spcChartDef':{'name':chart['Name'],'instanceId':chart['InstanceID']},'annotation':snap['arguments']['annotation'],
                'exclude':False,'comments':snap['marker']}
    if (snap.get('kind') != 'spc_annotation' or snap.get('action') != 'annotate' or snap.get('service') != 'AddSPCAnnotation'
            or not isinstance(snap.get('marker'),str) or not re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',snap['marker'])
            or not q.valid_id(snap.get('target_id')) or snap['target_id'] != src['DataPointHistoryDetailId']
            or type(body.get('exclude')) is not bool or body != expected):
        raise ValueError('SPC注释服务、原始点、图、文本或允许字段不符。')
    if not isinstance(body['annotation'],str) or not body['annotation'].strip() or len(body['annotation']) > 2000 or '\x00' in body['annotation']:
        raise ValueError('SPC注释文本无效。')


async def inspect_snapshot(snap):
    args = dict(snap['arguments'])
    args.update(data_point_history_id=snap['target_id'],container_instance_id=snap['before']['source']['ContainerId'],
                spc_chart_instance_id=snap['before']['configuration']['chart']['InstanceID'])
    current = await context(args, original=snap['before']['source'])
    return {'original_point_id':snap['target_id'],'current_context':current,'original_context_unchanged':current==snap['before'],
            'verified_success':False,'state_changed':False,'retry_allowed':False,'request_correlation_verified':False,
            'note':'当前注释/相同文本/HTTP200不证明来自本次提交；不跟随替代点、不解锁、不重试。需最终事务证据人工review。'}


@mcp.tool
async def get_spc_annotation_context(container_name: str, data_point_history_id: str, spc_chart_name: str,
        container_instance_id: str | None = None, spc_chart_instance_id: str | None = None) -> str:
    """只读核对原始数值采集历史点的容器、父历史/事务链、固定采集定义版本、完整SPC组成员及该点所有图的最多100条注释历史。
    data_point_history_id须为真实DataPointHistoryDetail ID，不是定义点ID、本地观测编号或外部ID。
    不计算SPC、不写入、不判断本次提交成功；字段缺失、冲销、分页或关联不符时失败，不取第一条猜测。
    """
    try:
        args = dict(container_name=container_name,data_point_history_id=iid(data_point_history_id),spc_chart_name=spc_chart_name,
                    container_instance_id=container_instance_id,spc_chart_instance_id=spc_chart_instance_id)
        first = await context(args)
        if await context(args) != first: raise ValueError('SPC注释上下文读取期间发生变化。')
        return q.encode({'complete':True,**first,'verified_success':False,'release_authorized':False})
    except (ValueError,TypeError,KeyError,AttributeError) as exc: return q.encode({'complete':False,'error':str(exc)})


@mcp.tool
async def prepare_spc_annotation(container_name: str, data_point_history_id: str, spc_chart_name: str, annotation: str,
        container_instance_id: str | None = None, spc_chart_instance_id: str | None = None) -> str:
    """准备原生AddSPCAnnotation注释计划，不写MES。只支持原容器直接数值采集的持久历史点、Active普通定义及完整SPC组内的非自定义图。
    明确原始DataPointHistoryDetail ID、图和1..2000字符注释；Exclude固定false，任何图已有有效排除记录则拒绝，不做排除/恢复。
    不把本地SPC信号写成MES违例，不调用内联SPC/邮件/冻结/资源状态。非执行预览必须回显原点/父引用和派生定义版本；签名要求未知或非空则拒绝。
    execute_quality_transaction确认后仅一次提交；unknown用inspect_quality_transaction看原点历史，最终证据人工review，不重试。
    """
    return await p.prepare('spc_annotation',dict(container_name=container_name,data_point_history_id=data_point_history_id,
        spc_chart_name=spc_chart_name,annotation=annotation,container_instance_id=container_instance_id,
        spc_chart_instance_id=spc_chart_instance_id))
