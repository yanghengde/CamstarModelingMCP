"""One-hop container genealogy with independently paged evidence sections."""
import json
import re
import asyncio
import time
from tools import mcp
from tools.container_inquiry import get_container_details
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_QUERY_BASE_URL


def _queries(identifier):
    return {
        "current_family": ("SELECT ContainerId,ContainerName,ParentContainerId,SplitFromId FROM Container "
            f"WHERE ContainerId='{identifier}' OR ParentContainerId='{identifier}' OR ContainerId IN "
            f"(SELECT ParentContainerId FROM Container WHERE ContainerId='{identifier}')", "ContainerId"),
        "splits": ("SELECT d.SplitHistoryDetailsId,h.HistoryMainlineId,h.FromContainerId,d.ToContainerId,d.Qty,d.TxnId "
            "FROM SplitHistory h JOIN SplitHistoryDetails d ON h.SplitHistoryId=d.SplitHistoryId "
            f"WHERE h.FromContainerId='{identifier}' OR d.ToContainerId='{identifier}'", "d.SplitHistoryDetailsId"),
        "combines": ("SELECT d.CombineHistoryDetailId,h.HistoryMainlineId,d.FromContainerId,h.ContainerId AS ToContainerId,d.Qty,d.TxnId "
            "FROM CombineHistory h JOIN CombineHistoryDetail d ON h.CombineHistoryId=d.CombineHistoryId "
            f"WHERE d.FromContainerId='{identifier}' OR h.ContainerId='{identifier}'", "d.CombineHistoryDetailId"),
        "parent_history": ("SELECT HistoryMainlineId,TxnDate,TxnTypeName,ContainerId,ParentContainerId,WorkflowStepId,SpecId "
            f"FROM HistoryMainline WHERE (ContainerId='{identifier}' OR ParentContainerId='{identifier}') "
            "AND (ParentContainerId IS NOT NULL AND ParentContainerId <> '')", "TxnDate,HistoryMainlineId"),
        "rework": ("SELECT HistoryMainlineId,TxnDate,TxnTypeName,InRework,ContainerId,WorkflowStepId,SpecId,OperationId "
            f"FROM HistoryMainline WHERE ContainerId='{identifier}' AND (InRework=1 OR TxnTypeName LIKE '%Rework%')", "TxnDate,HistoryMainlineId"),
        "cross_references": ("SELECT HistoryCrossRefId,HistoryId,TrackingId,CrossRefType,StartTxnId,EndTxnId "
            f"FROM HistoryCrossRef WHERE HistoryId='{identifier}' OR TrackingId='{identifier}'", "HistoryCrossRefId"),
    }


@mcp.tool
async def get_container_trace(container_name: str, container_instance_id: str | None = None,
                              section: str = "all", offset: int = 0, limit: int = 20) -> str:
    """Read one-hop parent/child, split, combine and rework genealogy with original evidence IDs. Sections: all/current_family/splits/combines/parent_history/rework/cross_references. Each section paged independently, limit=1..50. Follow related container IDs explicitly; no inferred ancestry or cross-reference type decoding. Unknown/missing sections are not proof of no history."""
    result = {"complete": False, "scope": "one_hop", "sections": {}, "warnings": [
        "当前父子关系与历史关系分别展示；返工记录仅证明对应事务/状态，不推断完整返工闭环。",
        "CrossRefType 保留原码；HistoryId/TrackingId 未必都是容器，不能盲目递归。",
        "各段独立偏移分页；并发新增记录时不是一致性快照。"]}
    if not container_name.strip() or offset < 0 or not 1 <= limit <= 50:
        return raw_json({**result, "error": "容器名必填，offset>=0，limit=1..50"})
    if section not in {"all", *_queries("0").keys()}:
        return raw_json({**result, "error": "不支持的 section"})
    try:
        deadline = time.monotonic() + 60
        status = json.loads(await asyncio.wait_for(get_container_details(container_name, container_instance_id), timeout=15))
        ref = status.get("data", {}).get("ContainerStatusDetails", {}).get("Container") or {}
        identifier = ref.get("InstanceId", "")
        if not re.fullmatch(r"[0-9a-fA-F]{16}", identifier) or ref.get("Name") != container_name.strip() or (container_instance_id and identifier != container_instance_id.strip()):
            raise ValueError("无法核对容器名称与 ID")
        result["container"] = ref
        for key, (query, order) in _queries(identifier).items():
            if section not in {"all", key}:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result["sections"][key] = {"error": "追溯查询总时间预算已用尽", "page_complete": False}
                continue
            try:
                raw = await asyncio.wait_for(request("POST", "/api/AdHoc", base_url=CAMSTAR_QUERY_BASE_URL,
                    body={"queryText": f"{query} ORDER BY {order} OFFSET {offset} ROWS FETCH NEXT {limit + 1} ROWS ONLY"},
                    response_formatter=raw_json, retry_read_only=True), timeout=min(15, remaining))
            except asyncio.TimeoutError:
                raw = "Error: 只读分段查询超时，已保留其他分段结果"
            try:
                rows = json.loads(raw)
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError("非记录数组")
                result["sections"][key] = {"rows": rows[:limit], "page_complete": True,
                    "has_more": len(rows) > limit, "next_offset": offset + limit if len(rows) > limit else None,
                    "offset": offset, "returned_count": len(rows[:limit])}
            except (ValueError, TypeError):
                result["sections"][key] = {"error": raw, "page_complete": False}
        result["requested_sections_complete"] = all(s.get("page_complete") and not s.get("has_more") for s in result["sections"].values()) and offset == 0
    except (ValueError, TypeError, AttributeError, asyncio.TimeoutError) as exc:
        result["error"] = str(exc) or "容器身份查询超时"
    return raw_json(result)
