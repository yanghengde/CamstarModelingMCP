"""Bounded DPCollection preview. The common submission journal owns execution."""
import json
import time
import uuid

from tools import mcp
from tools.boms import bom_ref,encode
from tools.data_collection import definition,get_container_collection_requirements
from tools.process_context import ReadContext
from tools.container_inquiry import get_container_details
from tools.modeling_details import raw_json,_has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL


async def validate_groups(values,name='',revision='',instance_id=None,use_ror=False):
    from tools.collection_submission import serialize,valid_id
    if not isinstance(values,dict) or not values or len(json.dumps(values,allow_nan=False))>100000:
        raise ValueError('分组值须为非空JSON对象，最大100000字符。')
    data=await definition(ReadContext(4),bom_ref(name,revision,instance_id,use_ror),'point',0,200)
    if not data.get('complete'):raise ValueError('分组定义未完整展开。')
    groups=data.get('raw_items',[])
    if not groups or len(groups)>50 or len({g['Name'] for g in groups})!=len(groups):raise ValueError('组定义为空、重复或超过50组。')
    if set(values)!={g['Name'] for g in groups}:raise ValueError('须明确提供全部已定义分组；不静默跳过未提供组。')
    body=[];checks=[];total=0
    for group in groups:
        gid=group.get('InstanceID');points=group.get('Points');supplied=values[group['Name']]
        if not valid_id(gid) or not isinstance(points,list) or not points or not isinstance(supplied,dict):raise ValueError('组ID/点/输入格式不完整。')
        if len(points)>200 or len({p['Name'] for p in points})!=len(points):raise ValueError('组内点重复或过多。')
        if set(supplied)!={p['Name'] for p in points}:raise ValueError('须明确提供组内全部点。')
        minimum=group.get('MinimumSamples')
        if type(minimum) is not int or minimum<0:raise ValueError('组最少采样数未确认。')
        details=[];counts=[]
        for point in points:
            pid=point.get('InstanceID');samples=supplied[point['Name']];dtype=point.get('DataType')
            count=point.get('MinimumSamples')
            if not valid_id(pid) or dtype not in (1,4,6,7,9) or type(count) is not int or count<0:raise ValueError('分组点ID/类型/最少样本数未确认。')
            if not isinstance(samples,list) or not samples or len(samples)<count or len(samples)>200:raise ValueError('每点样本须为非空列表，满足最少样本数且最多200个。')
            total+=len(samples);counts.append(len(samples))
            if total>1000:raise ValueError('单次最多1000个样本。')
            converted=[{'cellName':str(i+1),'dataValue':serialize({'value':value,'data_type':dtype})} for i,value in enumerate(samples)]
            details.append({'point':{'name':point['Name'],'instanceId':pid},'samples':converted})
            checks.append({'point':group['Name']+'/'+point['Name'],'group_instance_id':gid,'point_instance_id':pid,
                           'data_type':dtype,'value':samples,'minimum_samples':count})
        # Conservative rectangular sampling; the service still owns dynamic validation.
        if len(set(counts))!=1 or min(counts)<minimum:raise ValueError('组内各点须提供相同样本数，且满足组最少采样数。')
        body.append({'group':{'name':group['Name'],'instanceId':gid},'serviceDetails':details})
    return {'definition':{k:data['definition'].get(k) for k in ('InstanceID','Name','Revision')},
            'checks':checks,'values':values,'static_valid':True,'errors':[],
            'quality_verdict':None,'constraints_complete':False},body


@mcp.tool
async def validate_grouped_collection_values(values:dict,name:str='',revision:str='',instance_id:str|None=None,use_ror:bool=False)->str:
    """只读分组采样校验：values={组名:{点名:[样本值]}}，须提供全部组和点，组内等样本数并满足组/点最少采样数。支持整数/小数/字符串/布尔/带时区时间，最多1000样本；不推断质量、不自动覆盖限值。"""
    try:
        validation,_=await validate_groups(values,name,revision,instance_id,use_ror)
        return encode({'executable':False,**validation})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'static_valid':False,'executable':False,'error':str(error)})


async def preview_groups(container_name,values,name,revision,instance_id,use_ror,container_instance_id,save_plan,request_marker=''):
    from tools import collection_submission as shared
    out={'complete':False,'executable':False,'transaction_executed':False,'stage':'grouped_server_preview',
         'server_echo_verified':False,'blockers':[]}
    try:
        validation,groups=await validate_groups(values,name,revision,instance_id,use_ror);out['validation']=validation
        target=validation['definition']
        if not shared.valid_id(target.get('InstanceID')):raise ValueError('定义ID无效。')
        info=json.loads(await get_container_details(container_name,container_instance_id,view='info'))
        ref=info.get('data',{}).get('Container') or {};cid=shared.identifier(ref)
        if (info.get('error') or not shared.valid_id(cid) or ref.get('Name')!=container_name.strip()
                or container_instance_id is not None and cid!=container_instance_id):raise ValueError('容器身份未确认。')
        context=json.loads(await get_container_collection_requirements(container_name,cid,transaction_name='CollectData',definition_kind='point'))
        out['context']=context
        matching=[m for m in context.get('mappings',[]) if m.get('definition',{}).get('definition',{}).get('InstanceID')==target['InstanceID']]
        if (context.get('error') or context.get('static_mappings_complete') is not True or len(matching)!=1
                or matching[0]['definition'].get('complete') is not True):raise ValueError('当前工序CollectData与分组定义映射未完整核实。')
        body={'container':{'name':ref['Name'],'instanceId':cid},
              'dataCollectionDef':{'name':target['Name'],'revision':target['Revision'],'instanceId':target['InstanceID']},
              'parametricData':{'@odata.type':'#shopfloor.DPCollection','dpCollectionGroups':groups}}
        if save_plan and not request_marker:request_marker='MCP-COL:'+uuid.uuid4().hex
        if request_marker:
            import re
            if not re.fullmatch(r'MCP-COL:[0-9a-f]{32}',request_marker):raise ValueError('采集关联标记无效。')
            body['comments']=request_marker
        params={'execute':'false','$select':'Container,DataCollectionDef,ParametricDataDefType',
                '$expand':'ParametricData($expand=shopfloor.DPCollection/DPCollectionGroups($expand=ServiceDetails($expand=Samples)))'}
        out['request_preview']={'method':'POST','path':'/api/CollectData','params':params,'body':body}
        result=json.loads(await request('POST','/api/CollectData',base_url=CAMSTAR_SHOPFLOOR_BASE_URL,body=body,params=params,response_formatter=raw_json))
        data=result.get('data',result)
        if (result.get('error') or data.get('error') or data.get('ErrorCode') or _has_next_link(result)
                or shared.identifier(data.get('Container'))!=cid or data.get('Container',{}).get('Name')!=ref['Name']
                or shared.identifier(data.get('DataCollectionDef'))!=target['InstanceID']
                or data.get('DataCollectionDef',{}).get('Name')!=target['Name']
                or data.get('DataCollectionDef',{}).get('Revision')!=target['Revision']
                or str(data.get('ParametricDataDefType'))!='8373'):raise ValueError('分组预览容器/定义/类型不匹配。')
        summary=data.get('ParametricData') or {};returned=summary.get('DPCollectionGroups')
        if str(summary.get('CDOTypeId'))!='8373' or not isinstance(returned,list) or len(returned)!=len(groups) or _has_next_link(summary):raise ValueError('分组预览展开不完整。')
        wanted={g['group']['instanceId']:g for g in groups};seen=set()
        for group in returned:
            gid=shared.identifier(group.get('Group'));expected=wanted.get(gid)
            if (not expected or gid in seen or group['Group'].get('Name')!=expected['group']['name']
                    or shared.identifier(group['Group'].get('Parent'))!=target['InstanceID'] or _has_next_link(group)):raise ValueError('组身份/父定义未核实。')
            seen.add(gid);points=group.get('ServiceDetails');expected_points={p['point']['instanceId']:p for p in expected['serviceDetails']}
            if not isinstance(points,list) or len(points)!=len(expected_points):raise ValueError('分组点回显不完整。')
            point_seen=set()
            for point in points:
                pid=shared.identifier(point.get('Point'));ep=expected_points.get(pid)
                if (not ep or pid in point_seen or point['Point'].get('Name')!=ep['point']['name']
                        or shared.identifier(point['Point'].get('Parent'))!=gid or _has_next_link(point)):raise ValueError('分组点身份未核实。')
                point_seen.add(pid);samples=point.get('Samples')
                if not isinstance(samples,list) or len(samples)!=len(ep['samples']):raise ValueError('样本回显数量不一致。')
                echo=[{'cellName':s.get('CellName'),'dataValue':s.get('DataValue')} for s in samples]
                if echo!=ep['samples']:raise ValueError('样本标识、顺序或值与请求不同。')
        out.update(server_echo_verified=True,server_points=returned,executable=True,static_mapping_verified=True,
                   note='分组采集通过非执行回显校验；现场提交/分组历史验收待完成，不代表质量合格。')
        if save_plan:
            snap=shared.snapshot(out);key=uuid.uuid4().hex;fingerprint=shared.digest(snap)
            arguments={'container_name':ref['Name'],'container_instance_id':cid,'instance_id':target['InstanceID'],'values':values,'collection_kind':'point'}
            if request_marker:arguments['request_marker']=request_marker
            with shared.db_connect() as db:
                db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',(key,fingerprint,json.dumps({'snapshot':snap,'arguments':arguments},ensure_ascii=False),'ready',time.time()))
            out.update(plan_id=key,plan_digest=fingerprint,expires_in_seconds=3600)
    except (ValueError,TypeError,KeyError,AttributeError) as error:out.update(executable=False,error=str(error))
    return encode(out)
