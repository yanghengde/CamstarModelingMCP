"""Collection definition/point/group and Spec binding plans. No arbitrary JSON writes."""
import asyncio
import copy
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
from tools.modeling_details import _has_next_link
from tools.process_context import ReadContext
from core.http_client import get_client, get_headers
from core.transfer_attempt_lock import attempt_lock, AttemptBusy
from config import CAMSTAR_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL

PLAN_DB = Path(__file__).resolve().parents[1]/'data/collection_modeling_plans.sqlite3'
ENTITIES = {'user': 'UserDataCollectionDefs', 'point': 'DataPointCollectionDefs'}
TYPES = {'integer':1, 'float':2, 'fixed':3, 'string':4, 'object':5, 'timestamp':6, 'boolean':7, 'decimal':9}
ROOT_FIELDS = {'description':'Description','layout':'DataPointLayout','instructions':'Instructions',
               'display_limits':'DisplayLimits','status':'Status'}
POINT_FIELDS = {'required':'IsRequired','row_position':'RowPosition','column_position':'ColumnPosition',
    'decimal_scale':'DecimalScale','rounding_rule':'RoundingRule','lower_limit':'LowerLimit','upper_limit':'UpperLimit',
    'display_limits':'DisplayLimits','limit_override_allowed':'IsLimitOverrideAllowed',
    'boolean_true':'BooleanTrue','boolean_false':'BooleanFalse','attribute_name':'AttributeName',
    'map_to_user_attribute':'MapToUserAttribute','minimum_samples':'MinimumSamples','sequence':'Sequence',
    'inner_lower_limit':'InnerLowerLimit','inner_upper_limit':'InnerUpperLimit',
    'outer_lower_limit':'OuterLowerLimit','outer_upper_limit':'OuterUpperLimit',
    'object_type':'ObjectType','object_selection_type':'ObjectSelValType','display_mode':'DisplayMode',
    'list_field_expression':'ListFieldExpression','query_name':'QueryName','query_type':'QueryType',
    'validate_resource':'ValidateResource'}
GROUP_FIELDS = {'layout':'GroupLayout','minimum_samples':'MinimumSamples','sequence':'Sequence'}
BOOL_FIELDS = {'IsRequired','DisplayLimits','IsLimitOverrideAllowed','MapToUserAttribute','ValidateResource'}
INT_FIELDS = {'RowPosition','ColumnPosition','DecimalScale','RoundingRule','MinimumSamples','Sequence',
              'ObjectType','ObjectSelValType','DisplayMode','QueryType','GroupLayout','DataPointLayout','Status'}
CONFIG_FIELDS = set(ROOT_FIELDS.values()) | set(POINT_FIELDS.values()) | set(GROUP_FIELDS.values()) | {
    'Name','Revision','DataType','UOM','ObjectGroup','Points','Groups','DataPoints','DataCollectionDef','Required','TxnName','TxnToUse','TxnMap','Sequence'}


def _field_schema(field):
    if field in BOOL_FIELDS:return {'type':'boolean'}
    if field in INT_FIELDS:return {'type':'integer','minimum':0,'maximum':2147483647}
    return {'anyOf':[{'type':'string','maxLength':2000},{'type':'null'}]}


_POINT_SCHEMA={'type':'object','additionalProperties':False,'required':['name'],'properties':{
    'op':{'type':'string','enum':['add','update','remove']},
    'name':{'type':'string','minLength':1,'maxLength':128},
    'new_name':{'type':'string','minLength':1,'maxLength':128},
    'data_type':{'anyOf':[{'type':'string','enum':list(TYPES)},{'type':'integer','enum':list(TYPES.values())}]},
    'unit':{'anyOf':[{'type':'string'},{'type':'null'}], 'description':'明确UOM名称；null表示清除，不推断或换算单位。'},
    **{key:_field_schema(field) for key,field in POINT_FIELDS.items()},
}}
for _limit in ('lower_limit','upper_limit','inner_lower_limit','inner_upper_limit','outer_lower_limit','outer_upper_limit'):
    _POINT_SCHEMA['properties'][_limit]['description']='表达式字符串或null；常量也必须写成字符串，例如"95"，不可传数字95。'
_POINT_LIST={'type':'array','maxItems':200,'items':_POINT_SCHEMA}
_GROUP_SCHEMA={'type':'object','additionalProperties':False,'required':['name'],'properties':{
    'op':{'type':'string','enum':['add','update','remove']},'name':{'type':'string','minLength':1,'maxLength':128},
    'new_name':{'type':'string','minLength':1,'maxLength':128},'points':_POINT_LIST,
    **{key:_field_schema(field) for key,field in GROUP_FIELDS.items()},
}}
CHANGE_SCHEMA={'type':'object','additionalProperties':False,'properties':{
    **{key:_field_schema(field) for key,field in ROOT_FIELDS.items()},'points':_POINT_LIST,
    'groups':{'type':'array','maxItems':50,'items':_GROUP_SCHEMA},
}}


def db_connect():
    PLAN_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PLAN_DB)
    db.execute('CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,digest TEXT,payload TEXT,state TEXT,created REAL,outcome TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS attempts(plan_id TEXT PRIMARY KEY,attempt_id TEXT,started REAL,finished REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,plan_id TEXT,record TEXT)')
    return db


def digest(value):
    from tools.collection_submission import digest as fingerprint
    return fingerprint(value)


def name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(c in value for c in '\r\n\x00'):
        raise ValueError('名称/版本须为1..128字符非空字符串。')
    return value.strip()


def field_value(field, value):
    if field in BOOL_FIELDS:
        if type(value) is not bool: raise ValueError(field+'必须是布尔值。')
    elif field in INT_FIELDS:
        if type(value) is not int or not 0 <= value <= 2147483647: raise ValueError(field+'必须是非负32位整数。')
    elif value is not None and (not isinstance(value, str) or len(value) > 2000):
        raise ValueError(field+'须为字符串或明确null；限值表达式不在客户端求值。')
    return value


def config(value):
    """Stable known configuration, not runtime IDs or last-entered samples."""
    if isinstance(value, list): return [config(item) for item in value]
    if not isinstance(value, dict): return value
    if ('Name' in value and any(k in value for k in ('InstanceId','InstanceID'))
            and not any(k in value for k in ('DataType','Points','Groups','DataPoints','Revision','TxnMap'))):
        return {k:value[k] for k in ('Name','Revision') if k in value}
    return {k:config(v) for k,v in value.items() if k in CONFIG_FIELDS}


async def read_definition(kind, definition_name, revision):
    entity = ENTITIES[kind]; ctx = ReadContext(max_requests=4)
    root = await ctx.resolve(entity, {'Name':definition_name,'Revision':revision})
    expanded = await ctx.get(entity, root['InstanceID'], {'$expand':'DataPoints' if kind=='user' else 'Groups($expand=Points)'})
    field = 'DataPoints' if kind=='user' else 'Groups'
    if expanded.get('InstanceID') != root['InstanceID'] or _has_next_link(expanded) or not isinstance(expanded.get(field),list):
        raise ValueError('定义展开不完整或身份不符，不能维护。')
    if len(expanded[field]) > 200: raise ValueError('一次最多维护200项。')
    root[field] = expanded[field]
    if kind=='point' and any(not isinstance(g.get('Points'),list) or len(g['Points'])>200 for g in root[field]):
        raise ValueError('分组点未完整返回。')
    return root


async def lookup(kind, definition_name, revision):
    ctx = ReadContext(max_requests=1)
    escape = lambda s:s.replace("'","''")
    result = await ctx.get(ENTITIES[kind], params={'$filter':f"Name eq '{escape(definition_name)}' and Revision eq '{escape(revision)}'",'$top':2})
    if _has_next_link(result) or not isinstance(result.get('value'),list) or len(result['value'])>1:
        raise ValueError('定义不存在性/唯一性未确认。')
    return result['value']


async def point_payload(item, kind, existing=None, position=1):
    allowed = set(POINT_FIELDS)|{'op','name','new_name','data_type','unit'}
    if not isinstance(item,dict) or set(item)-allowed: raise ValueError('采集点包含不支持/只读字段。')
    point_name = name(item.get('new_name',item.get('name')))
    raw_type = item.get('data_type',existing.get('DataType') if existing else None)
    dtype = TYPES.get(raw_type.lower()) if isinstance(raw_type,str) else raw_type
    if type(dtype) is not int or dtype not in (set(TYPES.values()) if kind=='user' else {1,4,6,7,9}):
        raise ValueError('采集点数据类型不支持；分组支持Integer/String/TimeStamp/Boolean/Decimal。')
    if existing and (existing.get('DataType')==5) != (dtype==5):
        raise ValueError('不能原地改变对象点与值点的CDO类型；请新建参数/版本。')
    subtype = 'DataPointCollectionPointChgs' if kind=='point' else 'ObjectDataPointChanges' if dtype==5 else 'ValueDataPointChanges'
    result = {'@odata.type':'#modeling.'+subtype,'name':point_name,'dataType':dtype,
              'rowPosition':existing.get('RowPosition',position) if existing else position,
              'columnPosition':existing.get('ColumnPosition',1) if existing else 1}
    permitted = ((set(POINT_FIELDS)-{'minimum_samples','sequence','inner_lower_limit','inner_upper_limit','outer_lower_limit','outer_upper_limit'}) if kind=='user' else {'required','row_position','column_position','decimal_scale','rounding_rule',
        'minimum_samples','sequence','inner_lower_limit','inner_upper_limit','outer_lower_limit','outer_upper_limit'})
    object_fields = {'object_type','object_selection_type','display_mode','list_field_expression','query_name','query_type','validate_resource'}
    if dtype==5: permitted = {'required','row_position','column_position','decimal_scale','rounding_rule','attribute_name','map_to_user_attribute'} | object_fields
    else: permitted -= object_fields
    for key,value in item.items():
        if key in POINT_FIELDS:
            if key not in permitted: raise ValueError(key+'不属于此采集点类型。')
            field = POINT_FIELDS[key]; result[field[0].lower()+field[1:] if field!='UOM' else 'uom'] = field_value(field,value)
    if dtype==5 and not item.get('object_type',existing.get('ObjectType') if existing else None):
        raise ValueError('对象点必须明确object_type(CDO编号)。')
    for key, entity, field in (('unit','UOMs','uom'),):
        if key in item:
            if key=='unit' and dtype==5 or key=='object_group' and dtype!=5: raise ValueError('单位/对象组不属于此类型。')
            if item[key] is None: result[field] = None
            else:
                obj = await ReadContext(max_requests=2).resolve(entity,{'Name':name(item[key])},revisioned=False)
                result[field] = {'name':obj['Name'],'instanceId':obj['InstanceID']}
    if not existing: result.setdefault('isRequired', False)
    return result


def projected_patch(value):
    result={}
    for k,v in value.items():
        if k.startswith('@'): continue
        field = {'uom':'UOM'}.get(k,k[0].upper()+k[1:])
        if isinstance(v,dict): result[field] = {a[0].upper()+a[1:]:b for a,b in v.items() if a!='instanceId'}
        else: result[field] = v
    return result


async def point_changes(items, kind, before, creating):
    if not isinstance(items,list) or len(items)>200: raise ValueError('points须为0..200个变更的列表。')
    after=copy.deepcopy(before); wrappers=[]; seen=set()
    for item in items:
        if not isinstance(item,dict): raise ValueError('采集点变更须为对象。')
        point_name=name(item.get('name')); key=point_name.casefold()
        if key in seen: raise ValueError('同一采集点不能在一次计划中重复修改。')
        seen.add(key); op=item.get('op','add' if creating else 'update')
        found=[(i,p) for i,p in enumerate(after) if p['Name'].casefold()==key]
        if op=='add':
            if found: raise ValueError('采集点名称已存在：'+point_name)
            value=await point_payload(item,kind,position=len(after)+1)
            wrappers.append({'listItemAction':'add','value':value});after.append(projected_patch(value))
        elif op in ('update','remove'):
            if len(found)!=1 or creating: raise ValueError('待维护采集点不存在/不唯一：'+point_name)
            index,old=found[0]
            if op=='remove':
                if set(item)-{'op','name'}: raise ValueError('remove不接受额外字段。')
                value={};after.pop(index)
            else:
                value=await point_payload(item,kind,old,index+1)
                if any(p['Name'].casefold()==value['name'].casefold() for i,p in enumerate(after) if i!=index): raise ValueError('重命名冲突。')
                after[index].update(projected_patch(value))
            wrappers.append({'key':{'name':old['Name']},'value':value})
        else: raise ValueError('采集点op=add/update/remove。')
    return wrappers,after


async def build_definition(kind, action, definition_name, revision, changes, source_revision=''):
    if kind not in ENTITIES or action not in ('create','update','revise','delete'): raise ValueError('定义类型或操作无效。')
    definition_name,revision=name(definition_name),name(revision)
    if not isinstance(changes,dict) or set(changes)-(set(ROOT_FIELDS)|{'points','groups'}): raise ValueError('changes包含不支持字段。')
    if action=='delete' and changes: raise ValueError('删除定义不接受配置变更。')
    entity=ENTITIES[kind]; before=None; target=None
    if action in ('create','revise'):
        if await lookup(kind,definition_name,revision): raise ValueError('目标定义版本已存在。')
    if action!='create':
        before=await read_definition(kind,definition_name,name(source_revision) if action=='revise' else revision)
        if before.get('IsFrozen') is not False: raise ValueError('定义冻结状态未知或已冻结。')
        if action=='revise' and before.get('IsRevOfRcd') is not True: raise ValueError('PATCH升版只允许明确当前ROR作为源。')
        target=before['InstanceID']
    after=config(before) if before else {'Name':definition_name,'Revision':revision}
    after.update(Name=definition_name,Revision=revision)
    body={} if action in ('update','delete') else {'name':definition_name,'revision':revision}
    for key,value in changes.items():
        if key in ROOT_FIELDS:
            field=ROOT_FIELDS[key]
            if kind=='point' and key in ('layout','display_limits') or kind=='user' and key=='instructions': raise ValueError('根设置与定义类型不匹配。')
            body[field[0].lower()+field[1:]]=field_value(field,value);after[field]=value
    if kind=='user':
        if 'groups' in changes: raise ValueError('用户采集定义不接受groups。')
        if action=='create': body.setdefault('dataPointLayout',1);after.setdefault('DataPointLayout',body['dataPointLayout'])
        if 'points' in changes:
            wrappers,points=await point_changes(changes['points'],kind,after.get('DataPoints',[]),action=='create')
            body['dataPoints' if action=='create' else 'dataPoints-Expanded']=[w['value'] for w in wrappers] if action=='create' else wrappers
            after['DataPoints']=points
    else:
        if 'points' in changes: raise ValueError('分组定义需要groups。')
        if 'groups' in changes:
            if not isinstance(changes['groups'],list) or len(changes['groups'])>50: raise ValueError('groups最多50个。')
            groups=copy.deepcopy(after.get('Groups',[]));wrappers=[];seen=set()
            for item in changes['groups']:
                if not isinstance(item,dict): raise ValueError('分组变更须为对象。')
                if set(item)-({'op','name','new_name','points'}|set(GROUP_FIELDS)): raise ValueError('分组包含未知字段。')
                group_name=name(item.get('name')); key=group_name.casefold()
                if key in seen: raise ValueError('不能重复修改同一组。')
                seen.add(key);op=item.get('op','add' if action=='create' else 'update')
                found=[(i,g) for i,g in enumerate(groups) if g['Name'].casefold()==key]
                if op=='add':
                    if found: raise ValueError('组已存在。')
                    value={'name':group_name,'groupLayout':1,'minimumSamples':1,'sequence':len(groups)+1};old={'Name':group_name,'Points':[]}
                elif op in ('update','remove') and len(found)==1:
                    index,old=found[0];value={'name':name(item.get('new_name',group_name))}
                else: raise ValueError('分组op无效或目标不存在。')
                if op=='remove':
                    if set(item)-{'op','name'}: raise ValueError('remove组不接受额外字段。')
                    groups.pop(index);wrappers.append({'key':{'name':old['Name']},'value':{}});continue
                for k,v in item.items():
                    if k in GROUP_FIELDS:
                        field=GROUP_FIELDS[k];value[field[0].lower()+field[1:]]=field_value(field,v)
                updated=copy.deepcopy(old);updated.update(projected_patch(value))
                if 'points' in item:
                    pts,expected=await point_changes(item['points'],'point',old.get('Points',[]),op=='add')
                    value['points' if op=='add' else 'points-Expanded']=[w['value'] for w in pts] if op=='add' else pts
                    updated['Points']=expected
                if op=='add':groups.append(updated);wrappers.append({'listItemAction':'add','value':value})
                else:groups[index]=updated;wrappers.append({'key':{'name':old['Name']},'value':value})
            if len({g['Name'].casefold() for g in groups})!=len(groups): raise ValueError('分组重命名冲突。')
            body['groups' if action=='create' else 'groups-Expanded']=[w['value'] for w in wrappers] if action=='create' else wrappers
            after['Groups']=groups
    if action=='update' and not body: raise ValueError('没有配置变更。')
    method={'create':'POST','update':'PUT','revise':'PATCH','delete':'DELETE'}[action]
    return {'kind':kind,'action':action,'entity':entity,'name':definition_name,'revision':revision,'source_revision':source_revision,
            'target_id':target,'before':before,'expected':None if action=='delete' else after,
            'method':method,'path':'/api/'+entity+('/'+target if action in ('update','delete') else ''),'body':body}


async def read_binding_spec(spec_name, revision):
    ctx=ReadContext(max_requests=4)
    root=await ctx.resolve('Specs',{'Name':spec_name,'Revision':revision})
    expanded=await ctx.get('Specs',root['InstanceID'],{'$expand':'TxnMap'})
    if (expanded.get('InstanceID')!=root['InstanceID'] or _has_next_link(expanded)
            or not isinstance(expanded.get('TxnMap'),list) or len(expanded['TxnMap'])>200):
        raise ValueError('工序事务映射未完整展开。')
    root['TxnMap']=expanded['TxnMap']
    return root


async def build_binding(spec_name, spec_revision, kind, definition_name, definition_revision,
                        action='bind', required=True, sequence=1):
    if kind not in ENTITIES or action not in ('bind','unbind'):raise ValueError('关联类型/操作无效。')
    spec_name,spec_revision=name(spec_name),name(spec_revision)
    definition_name,definition_revision=name(definition_name),name(definition_revision)
    field_value('IsRequired',required);field_value('Sequence',sequence)
    definition=await read_definition(kind,definition_name,definition_revision)
    before=await read_binding_spec(spec_name,spec_revision)
    if before.get('IsFrozen') is not False:raise ValueError('工序冻结状态未知或已冻结。')
    rows=copy.deepcopy(before['TxnMap']);indices=[]
    for i,row in enumerate(rows):
        ref=row.get('DataCollectionDef') or {}
        if (row.get('TxnName')=='CollectData' or row.get('TxnToUse')==2200):
            if ref.get('InstanceId',ref.get('InstanceID'))==definition['InstanceID']:
                indices.append(i)
            elif ref.get('Name')==definition_name:
                raise ValueError('同名定义的其他版本已关联；须明确解绑后再关联。')
    if len(indices)>1:raise ValueError('采集事务关联重复，不能选择目标。')
    if action=='unbind' and not indices:raise ValueError('指定版本未关联CollectData。')
    # 2200 is CollectData's own CdoId; inherited ContainerTxn uses 1110.
    value={'dataCollectionDef':{'name':definition_name,'revision':definition_revision,'instanceId':definition['InstanceID']},
           'txnToUse':2200,'required':required,'sequence':sequence}
    expected=config(before)
    if indices:
        index=indices[0]
        wrapper={'index':index,'value':{} if action=='unbind' else value}
        if action=='unbind':rows.pop(index)
        else:
            rows[index].update(projected_patch(value))
    else:
        wrapper={'listItemAction':'add','value':value};rows.append(projected_patch(value))
    expected['TxnMap']=config(rows)
    return {'kind':'binding','action':action,'entity':'Specs','name':spec_name,'revision':spec_revision,
        'target_id':before['InstanceID'],'before':before,'expected':expected,'definition':definition,
        'method':'PUT','path':'/api/Specs/'+before['InstanceID'],'body':{'txnMap-Expanded':[wrapper]}}


@mcp.tool
async def prepare_data_collection_binding(spec_name:str,spec_revision:str,kind:Literal['user','point'],
        definition_name:str,definition_revision:str,action:Literal['bind','unbind']='bind',
        required:bool=True,sequence:int=1)->str:
    """准备明确Spec版本与明确采集定义版本的CollectData事务关联/解绑计划。保留其他TxnMap项，拒绝重复/同名其他版本冲突；required及sequence可配置。只读，后续execute_data_collection_change须确认。"""
    try:
        args=dict(spec_name=spec_name,spec_revision=spec_revision,kind=kind,definition_name=definition_name,
                  definition_revision=definition_revision,action=action,required=required,sequence=sequence)
        snap=await asyncio.wait_for(build_binding(**args),60);snap['environment']=CAMSTAR_BASE_URL.rstrip('/')
        return encode(save_plan(snap,{'builder':'binding','values':args}))
    except (ValueError,TypeError,KeyError,AttributeError,asyncio.TimeoutError) as error:
        return encode({'submitted':False,'error':str(error) or '工序关联预览超时。'})


@mcp.tool
async def list_data_collections(kind: Literal['user','point']='user', name_contains: str='', offset:int=0,limit:int=20)->str:
    """只读列出用户/分组数据采集定义的名称、版本、状态；limit=1..100，多取1条判断后续页，不把当前页当全量。"""
    try:
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100: raise ValueError('分页范围无效。')
        params={'$top':limit+1,'$skip':offset,'$orderby':'Name,Revision','$select':'InstanceID,Name,Revision,Description,IsRevOfRcd,IsFrozen,Status'}
        if name_contains: params['$filter']="contains(Name,'"+name_contains.replace("'","''")+"')"
        data=await ReadContext(max_requests=1).get(ENTITIES[kind],params=params);rows=data.get('value')
        if not isinstance(rows,list): raise ValueError('定义列表响应无效。')
        more=len(rows)>limit or _has_next_link(data)
        return encode({'rows':rows[:limit],'has_more':more,'next_offset':offset+limit if more else None,'complete':offset==0 and not more})
    except (ValueError,TypeError,KeyError) as error:return encode({'complete':False,'error':str(error)})


def save_plan(snapshot, arguments):
    key=uuid.uuid4().hex;fingerprint=digest(snapshot);payload={'snapshot':snapshot,'arguments':arguments}
    with db_connect() as db: db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',(key,fingerprint,json.dumps(payload,ensure_ascii=False),'ready',time.time()))
    return {'plan_id':key,'plan_digest':fingerprint,'state':'ready','submitted':False,'confirmation_required':True,
            'request_preview':{k:snapshot[k] for k in ('method','path','body')},'before':snapshot['before'],
            'expected':snapshot['expected'],'expires_in_seconds':3600}


@mcp.tool
async def prepare_data_collection_definition(kind:Literal['user','point'],action:Literal['create','update','revise','delete'],
        name:str,revision:str,changes:Annotated[dict,Field(description='受控的定义/采集点/分组变更。所有限值是表达式字符串或null；常量上限95请传"95"，不要传数字。',json_schema_extra=CHANGE_SCHEMA)],source_revision:str='')->str:
    """准备采集定义/参数维护计划，不写MES。changes根字段description/layout/display_limits/status/instructions；user用points，point用groups[].points；每项op=add/update/remove，name定位，可new_name重命名。点data_type支持integer/float/fixed/string/object/timestamp/boolean/decimal，required、unit、lower_limit/upper_limit等；分组用minimum_samples/inner_*_limit/outer_*_limit。新增用派生CDO，修改用Expanded按键增量，未涉及项保留。revise须source_revision为当前ROR；delete明确版本。执行须确认。"""
    try:
        snapshot=await asyncio.wait_for(build_definition(kind,action,name,revision,changes,source_revision),60)
        snapshot['environment']=CAMSTAR_BASE_URL.rstrip('/')
        arguments={'kind':kind,'action':action,'definition_name':name,'revision':revision,'changes':changes,'source_revision':source_revision}
        return encode(save_plan(snapshot,{'builder':'definition','values':arguments}))
    except (ValueError,TypeError,KeyError,AttributeError,asyncio.TimeoutError) as error:return encode({'submitted':False,'error':str(error) or '定义预览超时。'})


@mcp.tool
async def get_data_collection_change_plan(plan_id:str)->str:
    """读取采集配置维护计划、不可变请求、原结果与核实状态；未知/已用计划永不重放。"""
    with db_connect() as db:row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
    if not row:return encode({'error':'维护计划不存在。'})
    return encode({'plan_id':plan_id,'plan_digest':row[0],'plan':json.loads(row[1]),'state':row[2],
                   'expired':time.time()-row[3]>3600,'outcome':json.loads(row[4]) if row[4] else None})


def checked(row, fingerprint):
    if not row or row[0]!=fingerprint:raise ValueError('维护计划不存在或摘要错误。')
    value=json.loads(row[1]);snap=value['snapshot']
    if digest(snap)!=fingerprint or snap['environment']!=CAMSTAR_BASE_URL.rstrip('/'):raise ValueError('维护计划内容/环境不符。')
    return value


def confirmation_summary(plan_id,plan_digest):
    with db_connect() as db:row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
    snap=checked(row,plan_digest)['snapshot']
    if row[2]!='ready' or time.time()-row[3]>3600:raise ValueError('维护计划已使用或过期。')
    return '确认数据采集配置维护：'+json.dumps({k:snap[k] for k in ('action','entity','name','revision','method','path','body','before','expected')},ensure_ascii=False)+'\n只提交一次；删除定义不可重放。请求不重试，结果不明先核实，不自动解锁。'


async def rebuild(arguments):
    if arguments['builder']=='definition':return await build_definition(**arguments['values'])
    if arguments['builder']=='binding':return await build_binding(**arguments['values'])
    raise ValueError('维护计划构建器无效。')


@mcp.tool
async def execute_data_collection_change(plan_id:str,plan_digest:str)->str:
    """确认后单次执行采集定义/参数/组/工序关联维护计划；重读前值、版本、冻结与缺省状态，比对不可变摘要后原子领取。不重试。先unknown，随后reconcile_data_collection_change核实响应及回读；错误/未知不重放。"""
    try:
        from tools.collection_submission import PLAN_DB as submission_db
        with attempt_lock(submission_db,'collection-domain'), attempt_lock(PLAN_DB,plan_id):
            with db_connect() as db:row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
            value=checked(row,plan_digest);snap=value['snapshot']
            if row[2]!='ready' or time.time()-row[3]>3600:raise ValueError('维护计划已用或过期。')
            assert_no_pending_submissions(snap)
            refreshed=await asyncio.wait_for(rebuild(value['arguments']),60);refreshed['environment']=snap['environment']
            if digest(refreshed)!=plan_digest:raise ValueError('定义、参数、关联或引用版本已变化，未写入。')
            with db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
                if current!=row or time.time()-row[3]>3600:raise ValueError('领取前计划变化或过期。')
                for (raw,) in db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')"):
                    other=json.loads(raw)['snapshot']
                    if other['environment']==snap['environment'] and (other['entity'],other['name'])==(snap['entity'],snap['name']):raise ValueError('该配置有未核实维护，不能绕过重复限制。')
                    linked=snap.get('definition') or {}
                    if (other['environment']==snap['environment'] and linked.get('Name')==other['name']
                            and other['entity'] in ENTITIES.values()):raise ValueError('关联定义有未核实维护，不能更新工序映射。')
                    linked=other.get('definition') or {}
                    if (other['environment']==snap['environment'] and linked.get('Name')==snap['name']
                            and snap['entity'] in ENTITIES.values()):raise ValueError('该定义有未核实工序映射维护，不能修改。')
                attempt=uuid.uuid4().hex
                db.execute("UPDATE plans SET state='in_flight' WHERE id=?",(plan_id,))
                db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)',(plan_id,attempt,time.time()))
            outcome={'state':'unknown','submission_attempted':True,'attempt_id':attempt,'retry_allowed':False,'verified_success':False}
            try:
                response=await asyncio.wait_for(get_client().request(snap['method'],CAMSTAR_BASE_URL.rstrip('/')+snap['path'],
                    headers=get_headers(),json=snap['body'] if snap['method']!='DELETE' else None,follow_redirects=False),60)
                outcome.update(http_status=response.status_code,raw_response=response.text)
                try:outcome['response']=response.json()
                except ValueError:pass
            except BaseException as error:
                outcome['error']=repr(error)
                if isinstance(error,asyncio.CancelledError):raise
            finally:
                with db_connect() as db:
                    db.execute("UPDATE plans SET state='unknown',outcome=? WHERE id=? AND state='in_flight'",(json.dumps(outcome,ensure_ascii=False),plan_id))
                    db.execute('UPDATE attempts SET finished=? WHERE plan_id=?',(time.time(),plan_id))
            return encode(outcome)
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy,asyncio.TimeoutError) as error:return encode({'submitted':False,'error':str(error) or '维护复核超时。'})


def matches(expected, actual):
    if isinstance(expected,dict):return isinstance(actual,dict) and all(k in actual and matches(v,actual[k]) for k,v in expected.items())
    if isinstance(expected,list):return isinstance(actual,list) and len(expected)==len(actual) and all(matches(a,b) for a,b in zip(expected,actual))
    return expected==actual and (type(expected) is not bool or type(actual) is bool)


def assert_no_pending_submissions(snap):
    from tools.collection_submission import PLAN_DB as path, db_connect as submission_db
    if not path.exists():return
    with submission_db() as db:
        rows=db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')").fetchall()
    ids={snap.get('target_id'),(snap.get('definition') or {}).get('InstanceID')}-{None}
    for (raw,) in rows:
        pending=json.loads(raw)['snapshot'];body=pending['body'];spec=pending.get('spec') or {}
        if pending.get('environment')!=CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'):continue
        refs={body['dataCollectionDef']['instanceId'],spec.get('InstanceID'),spec.get('InstanceId')}
        if ids & refs:raise ValueError('相关容器采集提交结果未核实，不能修改定义/工序关联。')


def assert_no_pending_changes(runtime_snapshot):
    if not PLAN_DB.exists():return
    spec=runtime_snapshot.get('spec') or {}
    refs={runtime_snapshot['body']['dataCollectionDef']['instanceId'],spec.get('InstanceID'),spec.get('InstanceId')}-{None}
    with db_connect() as db:
        rows=db.execute("SELECT payload FROM plans WHERE state IN ('in_flight','unknown')").fetchall()
    for (raw,) in rows:
        other=json.loads(raw)['snapshot']
        if other.get('environment')!=CAMSTAR_BASE_URL.rstrip('/'):continue
        named=runtime_snapshot['body']['dataCollectionDef']['name']
        if (({other.get('target_id'),(other.get('definition') or {}).get('InstanceID')}-{None}) & refs
                or other['entity'] in ENTITIES.values() and other['name']==named):
            raise ValueError('相关采集定义/工序关联维护结果未核实，不能提交采集。')


@mcp.tool
async def review_data_collection_change(plan_id:str,plan_digest:str,action:Literal['recover','resolve'],
        evidence_reference:str,review_note:str,conclusion:Literal['applied','not_applied','unknown']='unknown')->str:
    """须确认的本地维护审计。recover仅在原进程互斥已释放且存在尝试记录时in_flight转unknown，继续占用；resolve仅人工核对最终MES审计证据后unknown转人工结论。必须证据引用与说明；不写MES，不重放原计划，不把配置相同当本次成功。"""
    try:
        if action not in ('recover','resolve') or conclusion not in ('applied','not_applied','unknown'):raise ValueError('审计操作/结论无效。')
        if not isinstance(evidence_reference,str) or not 1<=len(evidence_reference.strip())<=2000 or not isinstance(review_note,str) or not 10<=len(review_note.strip())<=4000:
            raise ValueError('须提供证据引用及至少10字符人工说明。')
        with attempt_lock(PLAN_DB,plan_id),db_connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
            checked(row,plan_digest)
            if not db.execute('SELECT 1 FROM attempts WHERE plan_id=?',(plan_id,)).fetchone():raise ValueError('无提交尝试记录，不能审计解锁。')
            if row[2]!=('in_flight' if action=='recover' else 'unknown'):raise ValueError('计划状态不允许此审计。')
            state='unknown' if action=='recover' or conclusion=='unknown' else 'human_'+conclusion
            record={'action':action,'conclusion':conclusion,'evidence_reference':evidence_reference,'review_note':review_note,
                    'previous_outcome':json.loads(row[4]) if row[4] else None,'created':time.time()}
            db.execute('INSERT INTO reviews VALUES (?,?,?)',(uuid.uuid4().hex,plan_id,json.dumps(record,ensure_ascii=False)))
            db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',(state,json.dumps({'state':state,'human_review':record,'verified_success':False,'retry_allowed':False},ensure_ascii=False),plan_id))
        return encode({'state':state,'mes_write_performed':False,'verified_success':False,'original_plan_reusable':False})
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy) as error:return encode({'state_changed':False,'error':str(error)})


@mcp.tool
async def reconcile_data_collection_change(plan_id:str,plan_digest:str)->str:
    """只读核实采集配置维护：原HTTP成功且含明确对象确认、目标请求字段和已知未修改配置回读一致才保存verified_applied。响应丢失仅配置相同不证明本次因果，继续unknown；不重试原计划，不修改MES。"""
    try:
        with attempt_lock(PLAN_DB,plan_id):
            with db_connect() as db:
                row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone();value=checked(row,plan_digest)
                attempt=db.execute('SELECT * FROM attempts WHERE plan_id=?',(plan_id,)).fetchone()
            if row[2]!='unknown' or not attempt:raise ValueError('只核实有提交记录的unknown维护计划。')
            snap=value['snapshot'];outcome=json.loads(row[4]) if row[4] else {}
            if snap['action']=='delete':actual=None if not await lookup(snap['kind'],snap['name'],snap['revision']) else 'still_exists'
            elif snap['kind']=='binding':actual=await read_binding_spec(snap['name'],snap['revision'])
            else:actual=await read_definition(snap['kind'],snap['name'],snap['revision'])
            match=actual is None if snap['action']=='delete' else matches(snap['expected'],config(actual))
            reply=outcome.get('response',{});ack=(200<=outcome.get('http_status',0)<300 and isinstance(reply,dict)
                and not reply.get('error') and not reply.get('ErrorCode') and isinstance(reply.get('value'),str)
                and snap['name'].casefold() in reply['value'].casefold())
            verified=bool(match and ack)
            record={'kind':'automatic_check','configuration_matches':match,'response_acknowledged':ack,'verified_success':verified,
                    'previous_outcome':outcome,'readback':actual,'created':time.time()}
            with db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()!=row or db.execute('SELECT * FROM attempts WHERE plan_id=?',(plan_id,)).fetchone()!=attempt:
                    raise ValueError('核实期间计划或尝试记录变化。')
                db.execute('INSERT INTO reviews VALUES (?,?,?)',(uuid.uuid4().hex,plan_id,json.dumps(record,ensure_ascii=False)))
                if verified:db.execute("UPDATE plans SET state='verified_applied',outcome=? WHERE id=?",(json.dumps({'state':'verified_applied','verified_success':True,'verification':record,'retry_allowed':False},ensure_ascii=False),plan_id))
            return encode({'state_changed':verified,'state':'verified_applied' if verified else 'unknown','verified_success':verified,
                'configuration_matches':match,'response_acknowledged':ack,'mes_write_performed':False,'original_plan_reusable':False})
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy) as error:return encode({'state_changed':False,'verified_success':False,'error':str(error)})
