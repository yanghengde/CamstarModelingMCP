"""Resource master data and native activation/setup/throughput/collection plans."""
import asyncio
import copy
import json
import re
import uuid
from decimal import Decimal
from typing import Annotated, Literal
from pydantic import Field

from tools import mcp
from tools import resource_execution as plans
from tools.boms import encode
from tools.bom_modeling import stable, scalar, lookup
from tools.collection_modeling import name, matches
from tools.process_context import ReadContext
from tools.modeling_details import raw_json, _has_next_link
from tools.production_reporting import query_rows, identifier, sql_boolean, sql_number
from core.http_client import request
from config import CAMSTAR_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL

MASTER_FIELDS={'description':'Description','notes':'Notes','vendor_model':'VendorModel',
              'vendor_serial_number':'VendorSerialNumber','nickname':'NickName','fully_qualified_name':'FullyQualifiedName',
              'recipe_pattern':'RecipePattern','clearance_level':'ClearanceLevel'}
MASTER_REFS={'factory':('Factories','Factory'),'resource_type':('ResourceTypes','ResourceType'),
             'resource_family':('ResourceFamilies','ResourceFamily'),'maintenance_class':('MaintenanceClasses','MaintenanceClass'),
             'status_model':('ResourceStatusModels','ResourceStatusModel'),'vendor':('Vendors','Vendor')}
MASTER_LENGTHS={'Description':255,'Notes':2000,'VendorModel':30,'VendorSerialNumber':30,'NickName':100,
                'FullyQualifiedName':500,'RecipePattern':1000}
MASTER_SCHEMA={'type':'object','additionalProperties':False,'properties':{
    **{k:{'anyOf':[{'type':'string','maxLength':MASTER_LENGTHS[v]},{'type':'null'}]} for k,v in MASTER_FIELDS.items() if k!='clearance_level'},
    **{k:{'anyOf':[{'type':'string'},{'type':'null'}]} for k in MASTER_REFS},
    'clearance_level':{'type':'integer','enum':[1,2]}}}


def text(value, label, limit=2000):
    if not isinstance(value,str) or len(value.encode('utf-16-le'))//2>limit or '\x00' in value: raise ValueError(label+'文本无效/过长（上限'+str(limit)+'）。')
    return value


def ref_id(value):
    return identifier((value or {}).get('InstanceId') or (value or {}).get('InstanceID'))


async def read_resource(resource_name, instance_id=None):
    ref={'Name':name(resource_name)} if resource_name else {}
    if instance_id: ref['InstanceID']=identifier(instance_id)
    return await ReadContext(2).resolve('Resources',ref,revisioned=False)


async def operational(resource):
    rid=identifier(resource['InstanceID'])
    rows=await query_rows('SELECT r.ResourceId,r.ResourceName,r.ChangeCount AS ResourceChangeCount,r.IsFrozen,'
        'r.ParentResourceId,r.MaintenanceClassId,r.ResourceStatusModelId,r.ProductionStatusId,'
        'p.ChangeCount AS StatusChangeCount,p.Availability,p.StatusId,p.ReasonId,p.ResourceState,p.SetupId,'
        'p.ProductId,p.Run,p.TotalThruputQty,p.TotalThruputQty2,p.UOMId,p.UOM2Id,p.LastThruputDateGMT,'
        'p.ActiveMfgOrderId,p.MfgOrderId,p.LastRevTxnId,'
        '(SELECT COUNT(*) FROM ResourceDef ch WHERE ch.ParentResourceId=r.ResourceId) AS ChildCount '
        'FROM ResourceDef r LEFT JOIN ProductionStatus p ON r.ProductionStatusId=p.ProductionStatusId '
        f"WHERE r.ResourceId='{rid}'")
    if len(rows)!=1 or rows[0].get('ResourceId')!=rid or rows[0].get('ResourceName')!=resource['Name']:
        raise ValueError('设备持久身份未唯一核实。')
    return rows[0]


async def maintenance_rows(rid, req_id=None):
    rid=identifier(rid)
    rows=await query_rows('SELECT TOP 201 a.AssignedMaintReqId,a.ParentId,a.MaintenanceReqId,a.ActivationDateGMT,'
        'a.ChangeHistoryId,s.MaintenanceStatusId,s.ResourceId,s.StatusId,s.LastRevTxnId '
        'FROM AssignedMaintReq a LEFT JOIN MaintenanceStatus s ON s.AssignedMaintReqId=a.AssignedMaintReqId '
        f"WHERE a.ParentId='{rid}'"+(f" AND a.MaintenanceReqId='{identifier(req_id)}'" if req_id else '')+' ORDER BY a.AssignedMaintReqId')
    if len(rows)>200: raise ValueError('维护要求超过读取上限。')
    return rows


def runtime_allowed(resource, state):
    if resource.get('IsFrozen') is not False or resource.get('InstanceLocked') is True or sql_boolean(state.get('IsFrozen'),'设备冻结'):
        raise ValueError('设备已冻结/锁定或冻结状态未知。')
    if state.get('ParentResourceId') or sql_number(state.get('ChildCount'),'子设备数',integer=True):
        raise ValueError('本入口仅支持独立资源；层级设备的级联影响须另行配置和验收。')
    identifier(state.get('ProductionStatusId'))


async def resolve_ref(entity, value, dependencies, revision=''):
    ref={'Name':name(value)}
    if revision: ref['Revision']=name(revision)
    obj=await ReadContext(2).resolve(entity,ref,revisioned=bool(revision))
    if revision and obj.get('Status')!=1: raise ValueError(entity+'引用须为Active。')
    dependencies.append({'entity':entity,'id':obj['InstanceID'],'configuration':stable(obj)})
    result={'name':obj['Name'],'instanceId':identifier(obj['InstanceID'])}
    if revision: result.update(revision=obj['Revision'],useROR=False)
    return result,obj


async def native_preview(service, body, collection_kind=None):
    fields='Resource,Comments'
    params={'execute':'false'}
    if service=='ResourceActivation':
        fields='MaintenanceReq,Comments'; params['$expand']='ServiceDetails'
    elif service in ('ResourceSetup','ResourceSetupTransition'):
        fields+=',Availability,ResourceStatusCode,ResourceStatusReason,Setup,Product,Run,ResourceState'
    elif service=='ResourceThruput': fields+=',Qty,Qty2,UOM,UOM2,Product,Setup,Run'
    elif service=='CollectResourceData':
        fields+=',DataCollectionDef,ParametricDataDefType'
        params['$expand']=('ParametricData($expand=shopfloor.DPCollection/DPCollectionGroups($expand=ServiceDetails($expand=Samples)))'
                           if collection_kind=='point' else 'ParametricData($expand=shopfloor.DataPointSummary/DataPointDetails)')
    params['$select']=fields
    raw=await request('POST','/api/'+service,body=body,params=params,base_url=CAMSTAR_SHOPFLOOR_BASE_URL,response_formatter=raw_json)
    response=json.loads(raw); data=response.get('data',response)
    if response.get('error') or data.get('error') or data.get('ErrorCode') or _has_next_link(response): raise ValueError('原生非执行预览失败/不完整。')
    if service=='ResourceActivation':
        if ref_id(data.get('MaintenanceReq'))!=body['maintenanceReq']['instanceId']: raise ValueError('维护要求预览身份不符。')
        rows=data.get('ServiceDetails')
        if not isinstance(rows,list) or len(rows)!=1 or ref_id(rows[0].get('Resource'))!=body['serviceDetails'][0]['resource']['instanceId']:
            raise ValueError('激活预览未完整回显唯一目标设备。')
        if sql_boolean(rows[0].get('ClassActivated'),'维护类激活回显'): raise ValueError('该维护要求已由维护类激活，不能覆盖其激活关系。')
        if sql_boolean(rows[0].get('Activated'),'激活回显') is not body['serviceDetails'][0]['activated']: raise ValueError('激活意图回显不符。')
    else:
        if ref_id(data.get('Resource'))!=body['resource']['instanceId']: raise ValueError('原生预览设备身份不符。')
        for key in ('availability','qty','qty2','run'):
            if key not in body: continue
            actual=data.get(key[0].upper()+key[1:])
            if key in ('availability','qty','qty2'): actual=sql_number(actual,key,integer=key=='availability')
            if actual!=body[key]: raise ValueError('原生预览字段不符：'+key)
        for key,field in [('setup','Setup'),('product','Product'),('uom','UOM'),('uoM2','UOM2'),('resourceStatusCode','ResourceStatusCode'),('resourceStatusReason','ResourceStatusReason')]:
            if key in body and body[key] and ref_id(data.get(field))!=body[key]['instanceId']: raise ValueError('原生预览引用不符：'+field)
    if service=='CollectResourceData': verify_collection_echo(body,data,{'collection_kind':collection_kind})
    # Store only stable intent, not transient service CDOS / timestamps.
    return stable(data)


async def master(args):
    action=args['action']; n=name(args['resource_name']); text(n,'设备名称',100); changes=args['changes']
    if action not in ('create','update') or not isinstance(changes,dict) or set(changes)-(set(MASTER_FIELDS)|set(MASTER_REFS)):
        raise ValueError('主数据操作/字段无效；运行状态、设置及计数须使用原生事务。')
    before=None; state=None; deps=[]
    if action=='create':
        if await lookup('Resources',n): raise ValueError('设备名称已存在。')
    else:
        before=await read_resource(n,args.get('resource_instance_id')); state=await operational(before)
        if before.get('IsFrozen') is not False or before.get('InstanceLocked') is True: raise ValueError('设备冻结/锁定状态不允许维护。')
        if not changes: raise ValueError('修改内容为空。')
    body={'name':n} if action=='create' else {}
    expected=stable(before) if before else {'Name':n}
    for key,field in MASTER_FIELDS.items():
        if key not in changes: continue
        value=changes[key]
        if key=='clearance_level':
            if type(value) is not int or value not in (1,2): raise ValueError('clearance_level须为1/2。')
        elif value is not None: text(value,key,MASTER_LENGTHS[field])
        body[field[0].lower()+field[1:]]=value; expected[field]=value
    for key,(entity,field) in MASTER_REFS.items():
        if key not in changes: continue
        value=changes[key]
        if key=='status_model' and action!='create': raise ValueError('已有资源的状态模型迁移须单独核实，不在普通字段更新中替换。')
        if value is None: payload=None; expected[field]=None
        else:
            payload,obj=await resolve_ref(entity,value,deps); expected[field]={'Name':obj['Name']}
        body[field[0].lower()+field[1:]]=payload
    rid=before['InstanceID'] if before else None
    return {'kind':'master','action':action,'name':n,'resource_id':rid,'resources':['resource-name:'+n.casefold()]+(['resource:'+rid] if rid else []),
        'before':stable(before),'state_before':state,'expected':expected,'dependencies':deps,'method':'POST' if action=='create' else 'PUT',
        'path':'/api/Resources'+('/'+rid if rid else ''),'body':body}


async def transaction(args):
    kind=args['kind']; resource=await read_resource(args['resource_name'],args.get('resource_instance_id')); state=await operational(resource)
    runtime_allowed(resource,state); rid=resource['InstanceID']; deps=[]
    for key in ('setup','product'):
        if bool(args.get(key+'_name')) != bool(args.get(key+'_revision')): raise ValueError(key+'名称与版本须一起明确提供。')
    marker=args['_marker']
    if not re.fullmatch(r'MCP-RES:[0-9a-f]{32}',marker): raise ValueError('事务标记无效。')
    comments=marker+(' '+text(args.get('comments',''),'comments',180) if args.get('comments') else '')
    body={'resource':{'name':resource['Name'],'instanceId':rid},'comments':comments}; expected={}; extra={}
    if kind=='activation':
        if type(args['activated']) is not bool: raise ValueError('activated须为布尔值。')
        entity={'date':'DateReqs','recurring_date':'RecurringDateReqs','throughput':'ThruputReqs'}.get(args['maintenance_req_kind'])
        if not entity: raise ValueError('须明确日期/周期日期/吞吐维护要求类型。')
        req,obj=await resolve_ref(entity,args['maintenance_req_name'],deps,name(args['maintenance_req_revision']))
        rows=await maintenance_rows(rid,obj['InstanceID'])
        if len(rows)>1: raise ValueError('设备维护激活关系不唯一。')
        active=bool(rows)
        if active==args['activated']: raise ValueError('维护要求已经处于目标激活状态，无需重复提交。')
        body={'maintenanceReq':req,'comments':comments,'serviceDetails':[{'resource':body['resource'],'activated':args['activated'],'originalActivated':active}]}
        service='ResourceActivation'; extra.update(maintenance_before=rows,maintenance_req_id=obj['InstanceID']); expected={'activated':args['activated']}
    elif kind=='setup':
        availability=args['availability']
        if type(availability) is not int or availability not in (1,2): raise ValueError('availability须为1=可用/2=不可用。')
        body['availability']=availability; expected['Availability']=availability
        service='ResourceSetupTransition' if args['transition'] else 'ResourceSetup'
        if args['transition'] and not args.get('status_code'): raise ValueError('设置转换须明确目标状态码。')
        for key,entity,field,idfield in [('setup','Setups','SetupId','SetupId'),('product','Products','ProductId','ProductId')]:
            supplied=args.get(key+'_name')
            if supplied:
                payload,obj=await resolve_ref(entity,supplied,deps,name(args.get(key+'_revision')))
            elif state.get(idfield):
                obj=await ReadContext(1).resolve(entity,{'InstanceID':identifier(state[idfield])})
                payload={'name':obj['Name'],'revision':obj['Revision'],'instanceId':obj['InstanceID'],'useROR':False}
                deps.append({'entity':entity,'id':obj['InstanceID'],'configuration':stable(obj)})
            else: payload=None
            if payload: body[key]=payload; expected[field]=payload['instanceId']
        if type(args['transition']) is not bool: raise ValueError('transition须为布尔值。')
        if args.get('run') is not None: body['run']=text(args['run'],'run',30); expected['Run']=body['run']
        elif state.get('Run'): body['run']=state['Run']; expected['Run']=state['Run']
        for key,entity,field in [('status_code','ResourceStatusCodes','StatusId'),('status_reason','ResourceStatusReasons','ReasonId')]:
            if args.get(key):
                payload,obj=await resolve_ref(entity,args[key],deps)
                body['resourceStatusCode' if key=='status_code' else 'resourceStatusReason']=payload; expected[field]=obj['InstanceID']
                if key=='status_code':
                    if obj.get('Availability')!=availability: raise ValueError('目标状态码与明确可用性冲突。')
                    if obj.get('ResourceState') is not None: expected['ResourceState']=obj['ResourceState']
        if state.get('ResourceStatusModelId'):
            model=await ReadContext(1).get('ResourceStatusModels',identifier(state['ResourceStatusModelId']),{'$expand':'Details'})
            if _has_next_link(model) or not isinstance(model.get('Details'),list) or len(model['Details'])>200: raise ValueError('状态模型转换规则不完整。')
            deps.append({'entity':'ResourceStatusModels','id':state['ResourceStatusModelId'],'configuration':stable(model)})
            if args['transition'] and state.get('StatusId'):
                old=await ReadContext(1).get('ResourceStatusCodes',identifier(state['StatusId']))
                if not any((r.get('Status') or {}).get('Name')==old['Name'] and (r.get('ToStatus') or {}).get('Name')==args['status_code'] for r in model['Details']):
                    raise ValueError('模型中不存在明确的当前状态到目标状态转换。')
        elif args['transition']: raise ValueError('设备未配置状态模型，不能以转换入口绕过配置。')
    elif kind=='throughput':
        if sql_number(state.get('Availability'),'设备可用性',integer=True)!=1: raise ValueError('吞吐仅支持明确Up的设备。')
        body['qty']=scalar('QtyRequired',args['quantity']); expected['Qty']=body['qty']
        unit,_=await resolve_ref('UOMs',args['unit_name'],deps); body['uom']=unit; expected['UOMId']=unit['instanceId']
        if args.get('quantity2') is not None:
            body['qty2']=scalar('QtyRequired',args['quantity2']); unit2,_=await resolve_ref('UOMs',name(args.get('unit2_name')),deps)
            body['uoM2']=unit2; expected.update(Qty2=body['qty2'],UOM2Id=unit2['instanceId'])
        elif args.get('unit2_name'): raise ValueError('第二单位须与明确第二数量一起提供。')
        if args.get('product_name'):
            product,_=await resolve_ref('Products',args['product_name'],deps,name(args.get('product_revision'))); body['product']=product
        elif state.get('ProductId'):
            obj=await ReadContext(1).resolve('Products',{'InstanceID':identifier(state['ProductId'])})
            body['product']={'name':obj['Name'],'revision':obj['Revision'],'instanceId':obj['InstanceID'],'useROR':False}
            deps.append({'entity':'Products','id':obj['InstanceID'],'configuration':stable(obj)})
        else: raise ValueError('须明确产品版本，或设备已有可核实当前产品。')
        expected['ProductId']=body['product']['instanceId']
        if state.get('SetupId'):
            obj=await ReadContext(1).resolve('Setups',{'InstanceID':identifier(state['SetupId'])})
            body['setup']={'name':obj['Name'],'revision':obj['Revision'],'instanceId':obj['InstanceID'],'useROR':False}; expected['SetupId']=obj['InstanceID']
        if state.get('Run'): body['run']=state['Run']; expected['Run']=state['Run']
        service='ResourceThruput'
    elif kind=='collection':
        from tools.collection_validation import validate_collection_values
        from tools.collection_submission import serialize
        from tools.collection_groups import validate_groups
        from tools.collection_modeling import read_definition, PLAN_DB as config_db, db_connect as config_connect
        ck=args['collection_kind']
        if ck=='point': raise ValueError('本环境CollectResourceData分组预览成功但提交未保存样本历史；分组资源采集执行已关闭。请使用已验收的user定义，不自动转换或重发原分组请求。')
        if ck=='user':
            validation=json.loads(await validate_collection_values(args['values'],args['definition_name'],args['definition_revision']))
            if validation.get('static_valid') is not True or validation.get('errors'): raise ValueError('设备采集静态校验未通过：'+json.dumps(validation.get('errors')))
            rows=[{'dataPoint':{'name':c['point'],'instanceId':identifier(c['point_instance_id'])},'dataValue':serialize(c)} for c in validation['checks']]
            if len(rows)!=len(args['values']): raise ValueError('采集值包含空值/无法序列化点。')
            parametric={'@odata.type':'#shopfloor.DataPointSummary','dataPointDetails':rows}
        elif ck=='point':
            validation,groups=await validate_groups(args['values'],args['definition_name'],args['definition_revision'])
            parametric={'@odata.type':'#shopfloor.DPCollection','dpCollectionGroups':groups}
        else: raise ValueError('collection_kind须为user/point。')
        definition=await read_definition(ck,name(args['definition_name']),name(args['definition_revision'])); did=identifier(definition['InstanceID'])
        if definition.get('Status')!=1: raise ValueError('采集定义须为Active。')
        if config_db.exists():
            with config_connect() as db:
                for raw, in db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')"):
                    snap=json.loads(raw)['snapshot']
                    if snap.get('environment')==CAMSTAR_BASE_URL.rstrip('/') and snap['entity'] in ('UserDataCollectionDefs','DataPointCollectionDefs') and snap['name']==definition['Name']:
                        raise ValueError('采集定义存在未核实配置维护。')
        deps.append({'entity':'UserDataCollectionDefs' if ck=='user' else 'DataPointCollectionDefs','id':did,'configuration':stable(definition)})
        body.update(dataCollectionDef={'name':definition['Name'],'revision':definition['Revision'],'instanceId':did,'useROR':False},parametricData=parametric)
        extra.update(validation=validation,collection_kind=ck,definition_id=did); service='CollectResourceData'
    else: raise ValueError('资源事务类型无效。')
    if args.get('mfgorder_name'):
        order,obj=await resolve_ref('MfgOrders',args['mfgorder_name'],deps); body['mfgOrder']=order
        if body.get('product'):
            product=await ReadContext(2).resolve('Products',obj.get('Product'))
            if product['InstanceID']!=body['product']['instanceId']: raise ValueError('工单产品版本与明确产品不一致。')
            deps.append({'entity':'Products','id':product['InstanceID'],'configuration':stable(product)})
        expected['MfgOrderId']=obj['InstanceID']
    preview=await native_preview(service,body,extra.get('collection_kind'))
    return {'kind':kind,'service':service,'name':resource['Name'],'resource_id':rid,'resources':['resource:'+rid,'resource-name:'+resource['Name'].casefold()]+(['definition:'+extra['definition_id']] if kind=='collection' else []),
        'before':stable(resource),'state_before':state,'dependencies':deps,'expected':expected,'method':'POST','path':'/api/'+service,
        'body':body,'native_preview':preview,**extra}


def verify_collection_echo(body, data, extra):
    if ref_id(data.get('DataCollectionDef'))!=body['dataCollectionDef']['instanceId']: raise ValueError('采集预览定义身份不符。')
    summary=data.get('ParametricData') or {}; ck=extra['collection_kind']
    expected_type=7273 if ck=='user' else 8373
    if int(data.get('ParametricDataDefType',0))!=expected_type: raise ValueError('采集预览派生类型不符。')
    if ck=='user':
        rows=summary.get('DataPointDetails')
        want={r['dataPoint']['instanceId']:r['dataValue'] for r in body['parametricData']['dataPointDetails']}
        if not isinstance(rows,list) or len(rows)!=len(want): raise ValueError('设备采集点回显不完整。')
        got={ref_id(r.get('DataPoint')):r.get('DataValue') for r in rows}
    else:
        rows=summary.get('DPCollectionGroups')
        if not isinstance(rows,list): raise ValueError('设备分组采集未展开。')
        want={(g['group']['instanceId'],p['point']['instanceId'],s['cellName']):s['dataValue'] for g in body['parametricData']['dpCollectionGroups'] for p in g['serviceDetails'] for s in p['samples']}
        got={}
        for g in rows:
            for p in g.get('ServiceDetails',[]):
                for s in p.get('Samples',[]):
                    key=(ref_id(g.get('Group')),ref_id(p.get('Point')),s.get('CellName'))
                    if key in got: raise ValueError('设备采集回显样本重复。')
                    got[key]=s.get('DataValue')
    if got!=want: raise ValueError('设备采集回显点/组/样本/值不符。')


async def build(args):
    return await (master(args) if args['kind']=='master' else transaction(args))


async def prepare(args):
    try:
        if args['kind']!='master': args['_marker']='MCP-RES:'+uuid.uuid4().hex
        snap=await asyncio.wait_for(build(args),90)
        return encode(plans.save(snap,args))
    except (ValueError,TypeError,KeyError,AttributeError,asyncio.TimeoutError) as exc:
        return encode({'submitted':False,'executable':False,'error':str(exc) or '资源准备超时。'})


@mcp.tool
async def prepare_resource_change(action: Literal['create','update'], resource_name: str,
    changes: Annotated[dict, Field(description='设备基本资料与明确引用；可用性须用独立原生设置事务。',json_schema_extra=MASTER_SCHEMA)], resource_instance_id: str | None=None) -> str:
    """只读准备设备主数据新建/修改。支持描述、备注、厂商型号/序列号、别名、连接名称、配方模式、clearance_level及factory/resource_type/resource_family/maintenance_class/status_model/vendor明确名称引用。运行状态、吞吐计数和设置不通过建模直接修改；新建后用get_resource_operational_state核实原生初始状态。不修改冻结/层级/零件或删除设备。"""
    return await prepare(dict(kind='master',action=action,resource_name=resource_name,resource_instance_id=resource_instance_id,changes=changes))


@mcp.tool
async def prepare_resource_activation(resource_name: str, maintenance_req_name: str, maintenance_req_revision: str, activated: bool, maintenance_req_kind: Literal['date','recurring_date','throughput']='date', resource_instance_id: str | None=None, comments: str='') -> str:
    """准备给一个独立设备激活/停用明确版本维护要求的原生ResourceActivation计划。maintenance_req_kind明确date日期/recurring_date周期日期/throughput吞吐要求。不是设备开关机或Up/Down；类激活不在单设备入口覆盖。只读准备，不自动新建维护要求。"""
    return await prepare(dict(kind='activation',**locals()))


@mcp.tool
async def prepare_resource_setup(resource_name: str, availability: Annotated[int,Field(strict=True,ge=1,le=2)], setup_name: str='', setup_revision: str='', product_name: str='', product_revision: str='', run: str | None=None, status_code: str='', status_reason: str='', transition: bool=False, resource_instance_id: str | None=None, comments: str='') -> str:
    """准备设备设置/可用性变更，availability=1Up/2Down；明确Setup/产品版本、运行批号及状态/原因可选，省略时保留已核实当前Setup/产品/Run。transition=true使用ResourceSetupTransition，须设备已配置状态模型且明确目标状态码，不绕过转换规则。"""
    return await prepare(dict(kind='setup',**locals()))


@mcp.tool
async def prepare_resource_throughput(resource_name: str, quantity: Annotated[float,Field(strict=True,gt=0,allow_inf_nan=False)], unit_name: str, quantity2: Annotated[float,Field(strict=True,gt=0,allow_inf_nan=False)] | None=None, unit2_name: str='', product_name: str='', product_revision: str='', mfgorder_name: str='', resource_instance_id: str | None=None, comments: str='') -> str:
    """准备独立设备ResourceThruput本次数量，不代替容器报工/移动或工单完工。明确主单位；副数量与副单位一起提供，不换算。产品须明确版本或已在设备设置中核实。仅Up独立设备，执行后按唯一标记和实际持久吞吐历史核实。"""
    return await prepare(dict(kind='throughput',**locals()))


@mcp.tool
async def prepare_resource_collection(resource_name: str, definition_name: str, definition_revision: str, values: dict, collection_kind: Literal['user','point']='user', resource_instance_id: str | None=None, comments: str='') -> str:
    """准备CollectResourceData设备采集，不需要容器。user值为{点名:值}；明确采集定义版本，校验类型/必填和原生回显。point分组在本环境提交不保存样本历史，执行门禁关闭，不自动转换。只读准备；不推断质量，不覆盖MES限值/签名。"""
    return await prepare(dict(kind='collection',**locals()))


@mcp.tool
async def get_resource_operational_state(resource_name: str, resource_instance_id: str | None=None) -> str:
    """只读核对设备持久身份、ProductionStatus、主副吞吐累计/单位及维护要求激活关系；保留null原值，不把Up当作全部生产就绪或维护完成。"""
    try:
        resource=await read_resource(resource_name,resource_instance_id); state=await operational(resource)
        return encode({'complete':True,'resource':{'name':resource['Name'],'instance_id':resource['InstanceID']},'state':state,'maintenance':await maintenance_rows(resource['InstanceID']),
                       'throughput_units_known':bool(state.get('UOMId')),
                       'note':'设备可用性、维护要求激活和实际维护完成分别展示；不自动判断可生产。累计计数字段的单位可能为空，请按吞吐历史逐条核对单位，不推断换算。'})
    except (ValueError,TypeError,KeyError) as exc: return encode({'complete':False,'error':str(exc)})


async def history_headers(rid, marker=None, offset=0, limit=20):
    rid=identifier(rid)
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100: raise ValueError('offset>=0，limit=1..100。')
    clause=f"ResourceId='{rid}'"
    if marker is not None:
        if not re.fullmatch(r'MCP-RES:[0-9a-f]{32}(?: .{0,180})?',marker,re.DOTALL): raise ValueError('历史关联标记无效。')
        clause="Comments=N'"+marker.replace("'","''")+"'"
    return await query_rows('SELECT HistoryMainlineId,HistoryId,TxnId,ResourceId,TxnTypeName,TxnServiceName,TxnParentTypeName,'
        'ReversalStatus,Comments,TxnDate,TxnDateGMT FROM HistoryMainline WHERE '+clause+
        f' ORDER BY TxnDateGMT DESC,HistoryMainlineId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY')


@mcp.tool
async def get_resource_history(resource_name: str, resource_instance_id: str | None=None, offset: int=0, limit: int=20) -> str:
    """分页只读设备直接原生事务历史，保留事务ID、类型及本地/GMT原时间。不是派生子设备历史全集；激活事务可能以维护要求为主对象，请用原计划核实。"""
    try:
        resource=await read_resource(resource_name,resource_instance_id); rows=await history_headers(resource['InstanceID'],offset=offset,limit=limit)
        if any(r.get('ResourceId')!=resource['InstanceID'] for r in rows): raise ValueError('设备历史身份不符。')
        return encode({'complete':offset==0 and len(rows)<=limit,'rows':rows[:limit],'has_more':len(rows)>limit,'next_offset':offset+limit if len(rows)>limit else None,'page_complete':True})
    except (ValueError,TypeError,KeyError) as exc: return encode({'complete':False,'error':str(exc)})


def page_args(txn_id,offset,limit):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100: raise ValueError('offset>=0，limit=1..100。')
    return " AND m.TxnId='"+identifier(txn_id)+"'" if txn_id is not None else ''


@mcp.tool
async def get_resource_throughput_history(resource_name: str, resource_instance_id: str | None=None, txn_id: str | None=None, offset: int=0, limit: int=20) -> str:
    """分页读取设备原生吞吐明细，含每次主副数量/单位ID、产品/Setup/Run、事务ID和GMT原时间。不同单位不直接累计；不是容器/工单完成数量。"""
    try:
        clause=page_args(txn_id,offset,limit); resource=await read_resource(resource_name,resource_instance_id); rid=identifier(resource['InstanceID'])
        rows=await query_rows('SELECT t.ResourceThruputHistoryId,t.HistoryMainlineId,t.HistoryId,t.TxnId,t.ResourceId,t.ProductId,t.SetupId,t.Run,'
            't.Qty,t.Qty2,t.UOMId,t.UOM2Id,t.MfgOrderId,m.TxnDate,m.TxnDateGMT,m.Comments,m.ReversalStatus FROM ResourceThruputHistory t '
            'JOIN HistoryMainline m ON t.HistoryMainlineId=m.HistoryMainlineId AND t.TxnId=m.TxnId AND t.HistoryId=m.HistoryId '
            f"WHERE m.ResourceId='{rid}' AND t.ResourceId='{rid}' AND m.TxnTypeName='ResourceThruput'"+clause+
            f' ORDER BY m.TxnDateGMT DESC,t.ResourceThruputHistoryId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY')
        if any(row.get('ResourceId')!=rid or txn_id is not None and row.get('TxnId')!=txn_id for row in rows): raise ValueError('设备吞吐历史身份不符。')
        return encode({'complete':offset==0 and len(rows)<=limit,'page_complete':True,'rows':rows[:limit],
                       'has_more':len(rows)>limit,'next_offset':offset+limit if len(rows)>limit else None,'note':'逐条记录原单位；不换算或直接相加不同单位。'})
    except (ValueError,TypeError,KeyError) as exc: return encode({'complete':False,'error':str(exc)})


@mcp.tool
async def get_resource_collection_history(resource_name: str, resource_instance_id: str | None=None, txn_id: str | None=None, offset: int=0, limit: int=20) -> str:
    """分页读取设备普通UserDataCollection采集点实际历史值、类型、定义/点ID、限值和事务ID。不返回质量结论；空页不证明提交失败，不支持未落账的分组资源样本。"""
    try:
        clause=page_args(txn_id,offset,limit); resource=await read_resource(resource_name,resource_instance_id); rid=identifier(resource['InstanceID'])
        rows=await query_rows('SELECT d.DataPointHistoryDetailId,d.DataPointId,d.DataCollectionDefId,d.DataType,d.DataName,d.DataValue,d.UOMId,'
            'd.LowerLimit,d.UpperLimit,d.IsLimitViolation,d.IsLimitOverride,d.IsRequired,d.TxnId,d.HistoryId,p.HistoryMainlineId,m.ResourceId,m.TxnDate,m.TxnDateGMT,m.ReversalStatus '
            'FROM DataPointHistoryDetail d JOIN DataPointHistory p ON d.DataPointHistoryId=p.DataPointHistoryId AND d.TxnId=p.TxnId AND d.HistoryId=p.HistoryId '
            'JOIN HistoryMainline m ON p.HistoryMainlineId=m.HistoryMainlineId AND p.TxnId=m.TxnId AND p.HistoryId=m.HistoryId '
            f"WHERE m.ResourceId='{rid}' AND m.TxnTypeName='CollectResourceData'"+clause+
            f' ORDER BY m.TxnDateGMT DESC,d.DataPointHistoryDetailId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY')
        if any(row.get('ResourceId')!=rid or txn_id is not None and row.get('TxnId')!=txn_id for row in rows): raise ValueError('设备采集历史身份不符。')
        return encode({'complete':offset==0 and len(rows)<=limit,'page_complete':True,'rows':rows[:limit],'quality_verdict':None,
                       'has_more':len(rows)>limit,'next_offset':offset+limit if len(rows)>limit else None})
    except (ValueError,TypeError,KeyError) as exc: return encode({'complete':False,'error':str(exc)})


async def verify(snap,outcome):
    resource=await read_resource(snap['name'],snap.get('resource_id'))
    if snap['kind']=='master':
        reply=outcome.get('response',{}); ack=200<=outcome.get('http_status',0)<300 and isinstance(reply,dict) and not reply.get('error') and not reply.get('ErrorCode') and isinstance(reply.get('value'),str) and snap['name'] in reply['value']
        match=matches(snap['expected'],stable(resource))
        return {'verified_success':bool(ack and match),'response_acknowledged':bool(ack),'configuration_matches':match,'readback':resource}
    rid=identifier(snap['resource_id']); service=snap['service']; body=snap['body']
    if service=='ResourceActivation':
        # This service records assignments/MaintenanceStatus, not HistoryMainline.
        # A lost response must never be inferred successful from the same state.
        reply=outcome.get('response',{})
        ack=(200<=outcome.get('http_status',0)<300 and isinstance(reply,dict) and not reply.get('error') and not reply.get('ErrorCode')
             and isinstance(reply.get('value'),str) and reply['value'].startswith('Resource Activation completed '))
        assigned=await maintenance_rows(rid,snap['maintenance_req_id'])
        match=len(assigned)<=1 and bool(assigned)==snap['expected']['activated']
        if assigned:
            a=assigned[0]
            match=match and a.get('ParentId')==rid and a.get('ResourceId')==rid and a.get('MaintenanceReqId')==snap['maintenance_req_id']
            identifier(a.get('AssignedMaintReqId'));identifier(a.get('MaintenanceStatusId'));identifier(a.get('LastRevTxnId'))
            if any(old.get('AssignedMaintReqId')==a['AssignedMaintReqId'] for old in snap['maintenance_before']): match=False
        return {'verified_success':bool(ack and match),'response_acknowledged':bool(ack),'assignment_matches':bool(match),
                'verification_basis':'native_success_ack_and_maintenance_assignment_readback','history_mainline_recorded':False,
                'evidence':{'maintenance':assigned,'request_comments':body['comments']}}
    headers=await history_headers(rid,body['comments'],limit=2)
    if len(headers)!=1: raise ValueError('唯一标记未匹配唯一已提交原生主历史，继续unknown。')
    h=headers[0]
    for f in ('HistoryMainlineId','HistoryId','TxnId'): identifier(h.get(f))
    if h.get('Comments')!=body['comments'] or h.get('TxnTypeName')!=service or h.get('TxnServiceName')!=service or h.get('TxnParentTypeName') or sql_number(h.get('ReversalStatus'),'冲销状态',integer=True)!=1:
        raise ValueError('原生历史服务/标记/冲销状态不符。')
    if service!='ResourceActivation' and h.get('ResourceId')!=rid: raise ValueError('原生主历史设备不符。')
    mid=h['HistoryMainlineId']; txn=h['TxnId']; hid=h['HistoryId']; evidence={'header':h}
    if service=='ResourceActivation':
        assigned=await maintenance_rows(rid,snap['maintenance_req_id'])
        if len(assigned)>1 or bool(assigned)!=snap['expected']['activated']: raise ValueError('维护要求启停状态未与请求匹配。')
        evidence['maintenance']=assigned
    elif service in ('ResourceSetup','ResourceSetupTransition'):
        rows=await query_rows('SELECT ResourceStatusHistoryId,HistoryMainlineId,HistoryId,TxnId,Availability,ResourceStatusCodeId,'
            'ResourceStatusReasonCodeId,ResourceState,SetupId,ProductId,Run FROM ResourceStatusHistory '
            f"WHERE HistoryMainlineId='{mid}' AND TxnId='{txn}'")
        if len(rows)!=1: raise ValueError('资源设置历史不唯一。')
        row=rows[0]
        if row.get('HistoryId')!=hid or row.get('TxnId')!=txn or row.get('HistoryMainlineId')!=mid: raise ValueError('设置历史因果链不符。')
        for field,wanted in snap['expected'].items():
            actual=row.get({'StatusId':'ResourceStatusCodeId','ReasonId':'ResourceStatusReasonCodeId'}.get(field,field))
            if field in ('Availability','ResourceState'): actual=sql_number(actual,field,integer=True)
            if actual!=wanted: raise ValueError('设置持久历史不符：'+field)
        current=await operational(resource)
        for field,wanted in snap['expected'].items():
            actual=current.get(field)
            if field in ('Availability','ResourceState'): actual=sql_number(actual,field,integer=True)
            if actual!=wanted: raise ValueError('设备当前设置回读不符：'+field)
        evidence.update(detail=row,current_state=current)
    elif service=='ResourceThruput':
        rows=await query_rows('SELECT ResourceThruputHistoryId,HistoryMainlineId,HistoryId,TxnId,ResourceId,ProductId,SetupId,Run,Qty,Qty2,UOMId,UOM2Id,MfgOrderId '
            f"FROM ResourceThruputHistory WHERE HistoryMainlineId='{mid}' AND TxnId='{txn}'")
        if len(rows)!=1 or rows[0].get('ResourceId')!=rid: raise ValueError('实际设备吞吐历史不唯一/身份不符。')
        row=rows[0]
        if row.get('HistoryId')!=hid or row.get('TxnId')!=txn or row.get('HistoryMainlineId')!=mid: raise ValueError('吞吐历史因果链不符。')
        for field,wanted in snap['expected'].items():
            if field in ('Qty','Qty2'):
                if Decimal(str(sql_number(row.get(field),field)))!=Decimal(str(wanted)): raise ValueError('实际设备吞吐数量不符。')
            elif row.get(field)!=wanted: raise ValueError('实际设备吞吐引用不符：'+field)
        evidence.update(detail=row,current_state=await operational(resource))
    elif service=='CollectResourceData':
        evidence['details']=await verify_collection_history(snap,h)
    return {'verified_success':True,'history_matches':True,'evidence':evidence}


async def verify_collection_history(snap,h):
    from tools.collection_reconciliation import same_value
    mid=identifier(h['HistoryMainlineId']); txn=identifier(h['TxnId']); hid=identifier(h['HistoryId']); did=identifier(snap['definition_id'])
    body=snap['body']; ck=snap['collection_kind']
    if ck=='user':
        rows=await query_rows('SELECT TOP 201 d.DataPointHistoryDetailId,d.DataPointId,d.DataCollectionDefId,d.DataType,d.DataValue,d.TxnId,d.HistoryId,'
            'p.HistoryMainlineId FROM DataPointHistoryDetail d JOIN DataPointHistory p ON d.DataPointHistoryId=p.DataPointHistoryId AND d.TxnId=p.TxnId AND d.HistoryId=p.HistoryId '
            f"WHERE p.HistoryMainlineId='{mid}' AND d.TxnId='{txn}'")
        expected={r['dataPoint']['instanceId']:r['dataValue'] for r in body['parametricData']['dataPointDetails']}
        key=lambda r:r['DataPointId']; types={c['point_instance_id']:c['data_type'] for c in snap['validation']['checks']}
    else:
        rows=await query_rows('SELECT TOP 1001 s.DPCollectionSampleHistoryId,s.CellName,s.DataValue,s.TxnId,s.HistoryId,p.PointId,p.DataType,g.GroupId,'
            'h.DataPointCollectionId AS DataCollectionDefId,h.HistoryMainlineId FROM DPCollectionSampleHistory s '
            'JOIN DPCollectionPointHistory p ON s.DPCollectionPointsHistoryId=p.DPCollectionPointHistoryId AND s.TxnId=p.TxnId '
            'JOIN DPCollectionGroupHistory g ON p.DPCollectionGroupHistoryId=g.DPCollectionGroupHistoryId AND p.TxnId=g.TxnId '
            'JOIN CollectDataPointsHistory h ON g.CollectDataPointsHistoryId=h.CollectDataPointsHistoryId AND g.TxnId=h.TxnId '
            f"WHERE h.HistoryMainlineId='{mid}' AND s.TxnId='{txn}'")
        expected={(g['group']['instanceId'],p['point']['instanceId'],s['cellName']):s['dataValue'] for g in body['parametricData']['dpCollectionGroups'] for p in g['serviceDetails'] for s in p['samples']}
        key=lambda r:(r['GroupId'],r['PointId'],r['CellName']); types={c['point_instance_id']:c['data_type'] for c in snap['validation']['checks']}
    if len(rows)!=len(expected): raise ValueError('资源采集持久点/样本数量不符。')
    got={}
    for row in rows:
        k=key(row); pid=row.get('DataPointId',row.get('PointId')); kind=sql_number(row.get('DataType'),'采集数据类型',integer=True)
        if k in got or k not in expected or row.get('TxnId')!=txn or row.get('HistoryId')!=hid or row.get('HistoryMainlineId')!=mid or row.get('DataCollectionDefId')!=did or kind!=types.get(pid):
            raise ValueError('资源采集历史身份/事务/类型不符。')
        if not same_value(expected[k],row.get('DataValue'),kind): raise ValueError('实际采集值不符。')
        got[k]=row
    return rows
