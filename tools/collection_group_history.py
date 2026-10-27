"""DPCollection history joins verified against configured Query column metadata."""
import json

from tools import mcp
from tools.boms import encode
from tools.container_inquiry import get_container_details
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_QUERY_BASE_URL


async def group_rows(container_id,txn_id,offset,limit):
    from tools.collection_submission import valid_id
    if not valid_id(container_id) or txn_id is not None and not valid_id(txn_id):raise ValueError('历史ID无效。')
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=1000:raise ValueError('历史分页无效。')
    query=(
        'SELECT s.DPCollectionSampleHistoryId,s.DPCollectionPointsHistoryId,s.CellName,s.DataValue,'
        's.IntegerValue,s.DecimalValue,s.BooleanValue,s.TimestampValue,s.DataPointCollectionRangeColor,'
        's.TxnId,p.PointId,p.DataType,p.Name AS PointName,p.MinimumSamples AS PointMinimumSamples,'
        'p.InnerLowerLimit,p.InnerUpperLimit,p.OuterLowerLimit,p.OuterUpperLimit,p.UOMId,'
        'g.GroupId,g.Name AS GroupName,g.MinimumSamples AS GroupMinimumSamples,'
        'h.DataPointCollectionId AS DataCollectionDefId,h.HistoryMainlineId,m.ContainerId,m.TxnDate,m.TxnType '
        'FROM DPCollectionSampleHistory s JOIN DPCollectionPointHistory p '
        'ON s.DPCollectionPointsHistoryId=p.DPCollectionPointHistoryId AND s.TxnId=p.TxnId '
        'JOIN DPCollectionGroupHistory g ON p.DPCollectionGroupHistoryId=g.DPCollectionGroupHistoryId AND p.TxnId=g.TxnId '
        'JOIN CollectDataPointsHistory h ON g.CollectDataPointsHistoryId=h.CollectDataPointsHistoryId AND g.TxnId=h.TxnId '
        'JOIN HistoryMainline m ON h.HistoryMainlineId=m.HistoryMainlineId '
        f"WHERE m.ContainerId='{container_id}' ")
    if txn_id is not None:query+=f"AND s.TxnId='{txn_id}' "
    query+=f'ORDER BY m.TxnDate DESC,s.DPCollectionSampleHistoryId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY'
    rows=json.loads(await request('POST','/api/AdHoc',base_url=CAMSTAR_QUERY_BASE_URL,body={'queryText':query},response_formatter=raw_json,retry_read_only=True))
    if not isinstance(rows,list) or len(rows)>limit+1:raise ValueError('分组历史响应无效。')
    ids=[]
    for row in rows:
        if (not isinstance(row,dict) or not row.get('DPCollectionSampleHistoryId') or row.get('ContainerId')!=container_id
                or txn_id is not None and row.get('TxnId')!=txn_id):raise ValueError('分组历史身份不匹配。')
        ids.append(row['DPCollectionSampleHistoryId'])
    if len(ids)!=len(set(ids)):raise ValueError('分组历史关联重复。')
    return rows


@mcp.tool
async def get_grouped_collection_history(container_name:str,container_instance_id:str|None=None,txn_id:str|None=None,offset:int=0,limit:int=20)->str:
    """只读分组采集样本历史，经Sample→Point→Group→CollectDataPointsHistory→HistoryMainline关联，含组/点/样本标识、原值、限值和事务ID。limit=1..100。空页不证明失败，不判质量；非空现场链待验收。"""
    try:
        from tools.collection_submission import identifier,valid_id
        if not isinstance(container_name,str) or not container_name.strip() or type(limit) is not int or not 1<=limit<=100:raise ValueError('容器名必填；limit=1..100。')
        if container_instance_id is not None and not valid_id(container_instance_id) or txn_id is not None and not valid_id(txn_id):raise ValueError('显式ID无效。')
        info=json.loads(await get_container_details(container_name,container_instance_id,view='info'));ref=info.get('data',{}).get('Container') or {}
        cid=identifier(ref)
        if (info.get('error') or not valid_id(cid) or ref.get('Name')!=container_name.strip()
                or container_instance_id is not None and cid!=container_instance_id):raise ValueError('容器身份未确认。')
        rows=await group_rows(cid,txn_id,offset,limit);more=len(rows)>limit
        return encode({'rows':rows[:limit],'page_complete':True,'complete':False,'has_more':more,
            'next_offset':offset+limit if more else None,'scope':'grouped_data_collection_history',
            'nonempty_linkage_verified':False,'submission_outcome':'unknown','quality_verdict':None})
    except (ValueError,TypeError,KeyError,AttributeError) as error:return encode({'complete':False,'error':str(error)})
