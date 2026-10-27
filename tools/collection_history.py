"""Bounded read-only UserDataCollection history; no submission or retry decisions."""
import json
import re

from tools import mcp
from tools.boms import encode
from tools.container_inquiry import get_container_details
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_QUERY_BASE_URL


@mcp.tool
async def get_container_collection_history(container_name: str, container_instance_id: str | None = None,
                                            txn_id: str | None = None, offset: int = 0, limit: int = 20) -> str:
    """只读容器 DataPointHistory/Detail 采集历史，支持已知16位 txn_id 精确筛选。返回原始值、限值和事务标识，不判质量，不证明某次提交结果；非空现场关联尚待验收，不涵盖自定义 ParametricData 或分组采样历史。"""
    out = {'complete': False, 'source': 'Query/AdHoc DataPointHistoryDetail -> DataPointHistory -> HistoryMainline',
           'scope': 'user_data_collection_history', 'submission_outcome': 'unknown',
           'nonempty_linkage_verified': False,
           'note': '仅返回该历史结构中的直接容器记录，不涵盖全部采集类型；空页不能证明提交失败或允许重试。分页不是一致快照。'}
    try:
        if (not container_name.strip() or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError('容器名必填，offset 为非负整数，limit=1..100。')
        for identifier in (container_instance_id, txn_id):
            if identifier is not None and not re.fullmatch(r'[0-9a-fA-F]{16}', identifier):
                raise ValueError('显式容器ID和事务ID必须为16位十六进制字符串。')
        info = json.loads(await get_container_details(container_name.strip(), container_instance_id, view='info'))
        data = info.get('data')
        ref = data.get('Container') if isinstance(data, dict) else None
        identifier = (ref.get('InstanceId') or ref.get('InstanceID')) if isinstance(ref, dict) else None
        if (info.get('error') or not isinstance(identifier, str)
                or not re.fullmatch(r'[0-9a-fA-F]{16}', identifier)
                or ref.get('Name') != container_name.strip()
                or (container_instance_id is not None and identifier != container_instance_id)):
            raise ValueError('容器身份无法核实，未查询历史。')
        query = (
            'SELECT d.DataPointHistoryDetailId,d.DataPointHistoryId,d.DataCollectionDefId,'
            'd.DataPointId,d.TxnId,d.DataName,d.DataType,d.DataValue,d.IntegerValue,'
            'd.FloatValue,d.FixedValue,d.DecimalValue,d.BooleanValue,d.TimestampValue,'
            'd.NDOValueId,d.RDOValueId,d.EnumerationValue,d.UOMId,d.LowerLimit,d.UpperLimit,'
            'd.IsRequired,d.IsLimitViolation,d.IsLimitOverride,d.IsLimitOverrideAllowed,'
            'h.HistoryMainlineId,m.ContainerId,m.TxnDate,m.TxnType '
            'FROM DataPointHistoryDetail d JOIN DataPointHistory h '
            'ON d.DataPointHistoryId=h.DataPointHistoryId '
            'JOIN HistoryMainline m ON h.HistoryMainlineId=m.HistoryMainlineId '
            f"WHERE m.ContainerId='{identifier}' ")
        if txn_id is not None:
            query += f"AND d.TxnId='{txn_id}' "
        query += (f'ORDER BY m.TxnDate DESC,d.DataPointHistoryDetailId '
                  f'OFFSET {offset} ROWS FETCH NEXT {limit + 1} ROWS ONLY')
        raw = await request('POST', '/api/AdHoc', base_url=CAMSTAR_QUERY_BASE_URL,
                            body={'queryText': query}, response_formatter=raw_json, retry_read_only=True)
        rows = json.loads(raw)
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise ValueError('历史查询未返回受限记录列表。')
        keys = []
        for row in rows:
            if (not isinstance(row, dict) or not row.get('DataPointHistoryDetailId')
                    or row.get('ContainerId') != identifier
                    or (txn_id is not None and row.get('TxnId') != txn_id)):
                raise ValueError('历史记录身份或事务筛选不匹配。')
            keys.append(row['DataPointHistoryDetailId'])
        if len(set(keys)) != len(keys):
            raise ValueError('历史关联返回重复明细，不能确认分页。')
        more = len(rows) > limit
        out.update(container=ref, txn_id_filter=txn_id, rows=rows[:limit], offset=offset,
                   has_more=more, next_offset=offset + limit if more else None, page_complete=True)
    except (ValueError, TypeError, AttributeError) as error:
        out['error'] = str(error)
    return encode(out)
