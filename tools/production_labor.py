"""Persist explicit labor minutes in MES CollectData without guessing TaskTime units."""
import json
import time
from decimal import Decimal

from tools import mcp, production_records as r, production_reporting as p
from tools import collection_submission as c, collection_modeling as cm
from tools.modeling_details import raw_json
from tools.process_context import ReadContext

POINT_TYPES={'LaborKey':4,'EmployeeName':4,'EmployeeId':4,'MfgOrderName':4,
             'ActualMinutes':9,'StartTime':6,'EndTime':6}


def db_connect():
    db=r.connect()
    db.execute('CREATE TABLE IF NOT EXISTS labor_exports(environment TEXT NOT NULL,record_id TEXT NOT NULL,'
        'plan_id TEXT NOT NULL,plan_digest TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(environment,record_id))')
    return db


def labor_record(record_id):
    with r.connect() as db:
        row=db.execute("SELECT entry_key,payload,digest FROM records WHERE id=? AND environment=? AND kind='labor'",
            (record_id,r.environment())).fetchone()
    if not row or p.digest(json.loads(row[1]))!=row[2]:raise ValueError('当前环境的人工工时记录不存在或摘要不符。')
    payload=json.loads(row[1])
    if not payload.get('context'):raise ValueError('MES工时提交须绑定原登记容器和工序。')
    return row[0],payload


def values_for(key, record):
    minutes=Decimal(record['actual_minutes']);number=float(minutes)
    if Decimal(str(number))!=minutes:raise ValueError('分钟数无法通过当前采集契约无损传输。')
    return {'LaborKey':key,'EmployeeName':record['employee']['name'],'EmployeeId':record['employee']['instance_id'],
        'MfgOrderName':record['order']['name'],'ActualMinutes':number,
        'StartTime':record['start_time'],'EndTime':record['end_time']}


def linked_plan(record_id):
    key,record=labor_record(record_id)
    with db_connect() as db:
        link=db.execute('SELECT plan_id,plan_digest FROM labor_exports WHERE environment=? AND record_id=?',
            (r.environment(),record_id)).fetchone()
    if not link:raise ValueError('此人工工时尚未准备MES提交。')
    with c.db_connect() as db:
        row=db.execute('SELECT digest,payload,state,outcome FROM plans WHERE id=?',(link[0],)).fetchone()
    if not row or row[0]!=link[1]:raise ValueError('原采集计划不存在或摘要不符。')
    data=json.loads(row[1]);snap=data['snapshot']
    if (c.digest(snap)!=link[1] or snap['environment']!=p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/')
            or data['arguments'].get('values')!=values_for(key,record)
            or snap['body']['container']['instanceId']!=record['context']['container_id']):
        raise ValueError('原人工工时与不可变MES采集计划不符。')
    points=snap['body']['parametricData']['dataPointDetails']
    expected={n:c.serialize({'value':value,'data_type':POINT_TYPES[n]}) for n,value in values_for(key,record).items()}
    actual={point['dataPoint']['name']:point['dataValue'] for point in points}
    if len(points)!=7 or actual!=expected:raise ValueError('原生人工工时请求点值与声明记录不符。')
    return {'plan_id':link[0],'plan_digest':link[1]},row,record


def export_summary(labor):
    result={'storage':'MES CollectData explicit actual minutes','verified_record_count':0,
        'verified_minutes':'0','local_only_record_count':0,'states':{},'pending_record_ids':[]}
    total=Decimal(0)
    for record in labor:
        with db_connect() as db:
            link=db.execute('SELECT 1 FROM labor_exports WHERE environment=? AND record_id=?',
                (r.environment(),record['record_id'])).fetchone()
        if not link:
            result['local_only_record_count']+=1;continue
        _,row,_=linked_plan(record['record_id']);state=row[2]
        result['states'][state]=result['states'].get(state,0)+1
        if state=='verified_success':
            result['verified_record_count']+=1;total+=Decimal(record['actual_minutes'])
        elif state in ('unknown','in_flight'):
            result['pending_record_ids'].append(record['record_id'])
    result['verified_minutes']=r.decimal_text(total)
    return result


@mcp.tool
async def get_production_labor_submission(record_id: str) -> str:
    """查询人工工时对应的原生CollectData分钟记录提交；本地记录和MES落账状态明确分开。"""
    try:
        args,row,_=linked_plan(record_id)
        return raw_json({'record_id':record_id,**args,'state':row[2],
            'mes_persisted':row[2]=='verified_success','native_storage':'CollectData actual minutes, not TaskTime',
            'outcome':json.loads(row[3]) if row[3] else None,'mes_write_performed':False})
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        return raw_json({'record_id':record_id,'mes_persisted':False,'error':str(exc)})


@mcp.tool
async def prepare_production_labor_submission(record_id: str, definition_name: str,
        definition_revision: str, minutes_uom_name: str) -> str:
    """将已有人工工时准备为MES CollectData分钟记录；不猜TaskTime单位，不执行电子作业任务。
    须先record_production_labor绑定容器，并在原工序/步骤次数提交；原定义明确关联当前Spec的CollectData。
    专用User定义须恰有LaborKey/EmployeeName/EmployeeId/MfgOrderName(String)、ActualMinutes(Decimal，
    单位为调用方明确指定的分钟UOM)、StartTime/EndTime(TimeStamp)七点。每条工时只准备一个不可重放计划。
    已有链接返回原计划；定义与工序配置维护使用现有采集维护工具，不自动修改生产配置。
    """
    try:
        with db_connect() as db:
            existing=db.execute('SELECT 1 FROM labor_exports WHERE environment=? AND record_id=?',
                (r.environment(),record_id)).fetchone()
        if existing:return await get_production_labor_submission(record_id)
        key,record=labor_record(record_id);context=record['context']
        current,_,_=await r.report_context(context['container_name'],context['container_id'])
        if current!=context:raise ValueError('人工工时的原容器/工单/单位/步骤次数或工序已变化；不在另一工序补写。')
        p.ensure_no_unresolved_throughput(context['container_id']);p.ensure_no_unresolved_quality(context['container_id'])
        definition=await cm.read_definition('user',definition_name,definition_revision)
        points=definition['DataPoints'];by_name={v['Name']:v for v in points}
        if len(points)!=7 or set(by_name)!=set(POINT_TYPES) or any(by_name[n].get('DataType')!=t for n,t in POINT_TYPES.items()):
            raise ValueError('人工工时专用采集定义的七个点或数据类型不符。')
        uom=await ReadContext(3).resolve('UOMs',{'Name':r.text(minutes_uom_name,'分钟单位')},revisioned=False)
        unit=by_name['ActualMinutes'].get('UOM') or {}
        if unit.get('Name')!=uom['Name'] or (unit.get('InstanceId') or unit.get('InstanceID')) not in (None,uom['InstanceID']):
            raise ValueError('ActualMinutes的单位与明确分钟UOM不符。')
        preview=json.loads(await c.preview_container_data_collection(context['container_name'],values_for(key,record),
            name=definition_name,revision=definition_revision,container_instance_id=context['container_id']))
        if preview.get('executable') is not True or not preview.get('plan_id'):
            return raw_json({'executable':False,'preview':preview,'mes_write_performed':False})
        with db_connect() as db:
            db.execute('INSERT INTO labor_exports VALUES (?,?,?,?,?)',(r.environment(),record_id,
                preview['plan_id'],preview['plan_digest'],time.time()))
        return raw_json({'record_id':record_id,'plan_id':preview['plan_id'],'plan_digest':preview['plan_digest'],
            'state':'ready','request':preview['request_preview'],'unit':'minutes',
            'execute_tool':'execute_production_labor_submission','mes_write_performed':False})
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        return raw_json({'executable':False,'error':str(exc),'mes_write_performed':False})


@mcp.tool
async def execute_production_labor_submission(record_id: str) -> str:
    """将原人工工时计划单次提交MES CollectData，单位为明确分钟；同原采集计划共享不可重放保护。
    提交未知先verify_production_labor_submission，不重试、不移动工序、不执行任务或猜测TaskTime单位。
    """
    try:
        args,_,record=linked_plan(record_id)
        context=record['context'];current,_,_=await r.report_context(context['container_name'],context['container_id'])
        if current!=context:raise ValueError('工时原工序上下文已变化。')
        p.ensure_no_unresolved_throughput(context['container_id']);p.ensure_no_unresolved_quality(context['container_id'])
        return await c.collect_container_data(**args)
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        return raw_json({'submitted':False,'error':str(exc)})


@mcp.tool
async def verify_production_labor_submission(record_id: str) -> str:
    """只读核对人工工时的原生CollectData唯一事务和全部七点，成功才记MES已落账；不重复累计人时。"""
    try:
        args,row,_=linked_plan(record_id)
        if row[2]=='verified_success':return await get_production_labor_submission(record_id)
        from tools.collection_verification import verify_collection_submission
        result=json.loads(await verify_collection_submission(**args))
        return raw_json({'record_id':record_id,'mes_persisted':result.get('verified_success') is True,**result})
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        return raw_json({'mes_persisted':False,'error':str(exc),'mes_write_performed':False})
