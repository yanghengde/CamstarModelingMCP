"""Request marker and full history verification. Does not send or retry MES writes."""
import json
import re
import time

from tools import mcp
from tools import collection_submission as shared
from tools.boms import encode
from tools.modeling_details import raw_json
from core.http_client import request
from core.transfer_attempt_lock import attempt_lock,AttemptBusy
from config import CAMSTAR_QUERY_BASE_URL


async def verify_history(snap):
    from tools.production_reporting import sql_number
    body=snap['body'];marker=body.get('comments');cid=body['container']['instanceId'];did=body['dataCollectionDef']['instanceId']
    if not isinstance(marker,str) or not re.fullmatch(r'MCP-COL:[0-9a-f]{32}',marker):raise ValueError('旧计划没有唯一提交标记，仅可人工核实。')
    if not shared.valid_id(cid) or not shared.valid_id(did):raise ValueError('快照ID无效。')
    sql=("SELECT TOP 2 HistoryMainlineId,TxnId,ContainerId,TxnType,Comments,SpecId,ReversalStatus FROM HistoryMainline "
         f"WHERE ContainerId='{cid}' AND Comments='{marker}' ORDER BY TxnDate DESC,HistoryMainlineId")
    headers=json.loads(await request('POST','/api/AdHoc',base_url=CAMSTAR_QUERY_BASE_URL,body={'queryText':sql},response_formatter=raw_json,retry_read_only=True))
    if not isinstance(headers,list) or len(headers)!=1:raise ValueError('唯一提交标记未匹配唯一历史主事务。')
    header=headers[0];txn=header.get('TxnId');spec=snap.get('spec') or {}
    if (header.get('ContainerId')!=cid or header.get('Comments')!=marker or sql_number(header.get('TxnType'),'采集历史类型',integer=True)!=2200
            or sql_number(header.get('ReversalStatus'),'采集历史冲销状态',integer=True)!=1
            or not shared.valid_id(txn) or not shared.valid_id(header.get('HistoryMainlineId'))
            or not shared.valid_id(spec.get('InstanceID')) or header.get('SpecId')!=spec['InstanceID']):raise ValueError('历史事务类型/工序/容器/标记未匹配。')
    grouped=body['parametricData'].get('@odata.type')=='#shopfloor.DPCollection'
    if grouped:
        from tools.collection_group_history import group_rows
        rows=await group_rows(cid,txn,0,1000)
        expected={(g['group']['instanceId'],p['point']['instanceId'],s['cellName']):s['dataValue']
                  for g in body['parametricData']['dpCollectionGroups'] for p in g['serviceDetails'] for s in p['samples']}
        observed={}
        for row in rows:
            key=(row.get('GroupId'),row.get('PointId'),row.get('CellName'))
            if key in observed:raise ValueError('分组历史样本标识重复。')
            observed[key]=row.get('DataValue')
    else:
        from tools.collection_history import get_container_collection_history
        rows=[]
        for offset in (0,100,200):
            page=json.loads(await get_container_collection_history(body['container']['name'],cid,txn,offset,100))
            if page.get('error') or page.get('page_complete') is not True:raise ValueError('用户采集历史未完整返回。')
            rows.extend(page['rows'])
            if not page.get('has_more'):break
        if len(rows)>200 or page.get('has_more'):raise ValueError('用户采集历史超过计划上限。')
        expected={p['dataPoint']['instanceId']:p['dataValue'] for p in body['parametricData']['dataPointDetails']}
        observed={}
        for row in rows:
            key=row.get('DataPointId')
            if key in observed:raise ValueError('采集历史点重复。')
            observed[key]=row.get('DataValue')
    types={c['point_instance_id']:c['data_type'] for c in snap['validation']['checks']}
    from tools.collection_reconciliation import same_value
    if not rows or set(observed)!=set(expected):raise ValueError('历史采集点/样本与完整提交计划不一致。')
    for key,wanted in expected.items():
        pid=key[1] if grouped else key
        if not same_value(wanted,observed[key],types[pid]):raise ValueError('采集历史值与提交值不一致。')
    for row in rows:
        pid=row.get('PointId' if grouped else 'DataPointId')
        check=next((c for c in snap['validation']['checks'] if c['point_instance_id']==pid),{})
        unit=check.get('uom') or {}
        expected_unit=unit.get('InstanceId') or unit.get('InstanceID')
        if expected_unit and row.get('UOMId')!=expected_unit:
            raise ValueError('采集历史单位与原计划明确单位不符。')
        if (row.get('DataCollectionDefId')!=did or row.get('TxnId')!=txn or row.get('ContainerId')!=cid
                or row.get('HistoryMainlineId')!=header['HistoryMainlineId'] or sql_number(row.get('TxnType'),'采集明细事务类型',integer=True)!=2200
                or sql_number(row.get('DataType'),'采集明细类型',integer=True)!=types.get(pid)):raise ValueError('采集历史定义、数据类型或事务父链不一致。')
    return {'header':header,'rows':rows,'request_marker':marker,'verified_at':time.time()}


@mcp.tool
async def verify_collection_submission(plan_id:str,plan_digest:str)->str:
    """须确认：不写MES，按新计划唯一Comments标记、容器、工序、CollectData事务类型、定义版本ID及全部点/样本/值核实历史。唯一完整匹配才审计保存本地verified_success并解除本计划占用；空页/同值/旧版无标记继续unknown，原计划不可重放。现场非空提交验收待补。"""
    try:
        with attempt_lock(shared.PLAN_DB,plan_id):
            with shared.db_connect() as db:
                row=db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()
                attempt=db.execute('SELECT * FROM collection_attempts WHERE plan_id=?',(plan_id,)).fetchone()
            if not row or row[0]!=plan_digest or row[2]!='unknown' or not attempt:raise ValueError('仅核实有本地提交尝试的unknown计划。')
            snap=json.loads(row[1])['snapshot']
            if shared.digest(snap)!=plan_digest or snap.get('environment')!=shared.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'):raise ValueError('计划内容或环境已变化。')
            evidence=await verify_history(snap)
            record={'evidence':evidence,'previous_outcome':json.loads(row[4]) if row[4] else None}
            result={'state':'verified_success','verified_success':True,'retry_allowed':False,'verification':record,'original_plan_reusable':False}
            with shared.db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if (db.execute('SELECT digest,payload,state,created,outcome FROM plans WHERE id=?',(plan_id,)).fetchone()!=row
                        or db.execute('SELECT * FROM collection_attempts WHERE plan_id=?',(plan_id,)).fetchone()!=attempt):raise ValueError('核实期间计划/尝试记录变化。')
                db.execute('INSERT INTO collection_reviews VALUES (?,?,?,?)',(plan_id,'automatic_verification',json.dumps(record,ensure_ascii=False),time.time()))
                db.execute('UPDATE plans SET state=?,outcome=? WHERE id=?',('verified_success',json.dumps(result,ensure_ascii=False),plan_id))
            return encode({'state_changed':True,'mes_write_performed':False,**result})
    except (ValueError,TypeError,KeyError,AttributeError,AttemptBusy) as error:return encode({'state_changed':False,'verified_success':False,'error':str(error)})
