"""Fixed Query Inquiry wrappers. No arbitrary CDO or event execution."""
from typing import Literal
import json
import re

from config import CAMSTAR_QUERY_BASE_URL
from core.http_client import request
from tools import mcp
from tools.modeling_details import raw_json, read_envelope


def _container(name: str, instance_id: str | None) -> dict:
    ref = {"name": name.strip()}
    if instance_id:
        ref["instanceId"] = instance_id.strip()
    return ref


async def _inquiry(service: str, body: dict, fields: list[str] | None = None,
                   limit: int = 50) -> str:
    params = {"execute": "true"}
    if service == "ViewContainerStatus":
        params["$expand"] = "ContainerStatusDetails"
        fields = ["ContainerStatusDetails"]
    elif service == "ContainerHistoryInquiry":
        # InquiryDetail is for a selected history row, not the mainline page.
        params["$select"] = "Container,Histories,HistoryDisplayTexts,ResultsetStartRow,ResultsetSizeLimit"
        fields = ["Histories", "HistoryDisplayTexts"]
    elif service == "ContainerInfoInquiry":
        params["$select"] = "Container,ContainerInfo,CurrentSpec,CurrentRouteStep"
        fields = ["ContainerInfo", "CurrentSpec", "CurrentRouteStep"]
    raw = await request("POST", f"/api/{service}", body=body,
                        base_url=CAMSTAR_QUERY_BASE_URL, params=params,
                        response_formatter=raw_json, retry_read_only=True)
    return read_envelope(raw, service, limit=limit, page_fields=fields)


@mcp.tool
async def get_container_details(container_name: str, container_instance_id: str | None = None,
                                view: Literal["info", "status"] = "status") -> str:
    """Read Container through fixed Query inquiries. Use returned TOP-LEVEL
    container_instance_id for subsequent Container references/movement. For status,
    its source is data.ContainerStatusDetails.Container.InstanceId; the adjacent
    data.ContainerStatusDetails.InstanceID is a temporary VIEW ROW, never a Container.
    Supply an actual Container ID for nonunique names. Errors are JSON envelopes,
    not permission to replace an invalid ID or retry writes. No Shopfloor mutation.
    Missing fields remain unknown. This Query server rejects Level in ContainerRef.
    """
    if not container_name.strip() or view not in {"info", "status"}:
        return "Error: container_name is required and view must be info or status."
    service = "ContainerInfoInquiry" if view == "info" else "ViewContainerStatus"
    raw = await _inquiry(service, {"container": _container(container_name, container_instance_id)})
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        return raw_json({'source':service, 'complete':False, 'error':'容器查询失败或返回非JSON；'+str(raw),
                         'requested_container_instance_id':container_instance_id,
                         'note':'InstanceID必须来自真实Container引用，不可使用ContainerStatusDetails视图行ID；仅只读核实，未修改或替换目标。'})
    if not isinstance(result, dict):
        return raw_json({'source':service, 'complete':False, 'error':'容器查询返回非对象响应。'})
    if result.get('error'):
        result.update(complete=False, requested_container_instance_id=container_instance_id)
        return raw_json(result)
    data = result.get('data', {})
    if not isinstance(data, dict):
        return raw_json({'source':service, 'complete':False, 'error':'容器查询data不是对象。'})
    row = data.get('ContainerStatusDetails') if view == 'status' else data
    ref = row.get('Container') if isinstance(row, dict) else None
    cid = (ref.get('InstanceId') or ref.get('InstanceID')) if isinstance(ref, dict) else None
    source = 'data.ContainerStatusDetails.Container' if view == 'status' else 'data.Container'
    valid = (isinstance(ref, dict) and ref.get('Name') == container_name.strip()
             and isinstance(cid, str) and re.fullmatch('[0-9a-fA-F]{16}', cid))
    identity = {'container_instance_id':cid if valid else None,
                'container_ref':ref if valid else None, 'identity_source':source,
                'view_record_instance_id':row.get('InstanceID') if view == 'status' and isinstance(row,dict) else None,
                'identity_note':'视图行InstanceID不是容器ID；过站仅使用container_instance_id或container_ref。'}
    result = {**identity, **result}
    if not valid:
        result.update(complete=False, error='返回中未取得匹配名称的实际Container引用，不使用视图行ID替代。')
    elif container_instance_id and cid != container_instance_id:
        result.update(complete=False, error='返回的实际Container ID与请求ID不符，未替换目标。')
    return raw_json(result)


@mcp.tool
async def get_container_history(container_name: str, container_instance_id: str | None = None,
                                start_row: int = 1, limit: int = 30,
                                history_source: Literal["auto", "inquiry", "mainline"] = "auto") -> str:
    """Read history newest first; start_row=1-based, limit=1..100. auto tries Inquiry then a fixed read-only HistoryMainline query if empty. mainline is direct-container history, excludes cross-references; use same source for subsequent pages. Never claim a page is full history. No unsupported date filters."""
    if not container_name.strip() or start_row < 1 or not 1 <= limit <= 100 or history_source not in {"auto", "inquiry", "mainline"}:
        return "Error: container_name required, start_row>=1, limit=1..100."
    if history_source == "mainline":
        status = await get_container_details(container_name, container_instance_id)
        try:
            ref = json.loads(status).get("data", {}).get("ContainerStatusDetails", {}).get("Container")
        except (ValueError, AttributeError):
            ref = None
        return await _mainline_history(ref, container_name, container_instance_id, start_row, limit)
    raw = await _inquiry("ContainerHistoryInquiry", {
        "container": _container(container_name, container_instance_id),
        "resultsetStartRow": start_row, "resultsetSizeLimit": limit, "sortAscending": False,
        "showAllTxnTypes": True, "showFullHistory": True,
    }, limit=limit)
    # Always label the server-paged result, even if it contains fewer rows.
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if data.get("error"):
        return raw
    payload = data.get("data", {})
    rows, texts = payload.get("Histories"), payload.get("HistoryDisplayTexts")
    if history_source == "auto" and rows == [] and payload.get("Container"):
        fallback = await _mainline_history(payload["Container"], container_name, container_instance_id, start_row, limit)
        try:
            fallback_data = json.loads(fallback)
        except (ValueError, TypeError):
            fallback_data = {"error": fallback}
        if not fallback_data.get("error"):
            return fallback
        data["fallback_error"] = fallback_data.get("error")
    warnings = data.setdefault("warnings", [])
    if not rows:
        warnings.append("当前页未返回历史记录；不能据此确认该容器从未发生事务。")
    if isinstance(rows, list) and isinstance(texts, list) and len(rows) != len(texts):
        warnings.append("历史引用和显示文本数量不一致，不能按下标可靠对应。")
    # These are server pages; do not expose misleading local-page totals.
    data["pages"] = {"Histories": {"pagination": "server", "start_row": start_row,
        "requested_limit": limit, "returned_count": len(rows) if isinstance(rows, list) else None,
        "server_total_known": False, "has_more": None,
        "next_start_row_to_try": start_row + limit if isinstance(rows, list) and len(rows) == limit else None}}
    data.update({"complete": False, "requested_page": {"start_row": start_row, "limit": limit},
                 "history_note": "仅本页结果；下一页 start_row 加 limit，不能声称这是全部历史。"})
    return raw_json(data)


async def _mainline_history(ref, name, instance_id, start_row, limit):
    """Fixed SQL Server read verified on this deployment; no caller SQL accepted."""
    identifier = (ref or {}).get("InstanceId") if isinstance(ref, dict) else None
    if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-fA-F]{16}", identifier)
            or ref.get("Name") != name.strip() or (instance_id and identifier != instance_id.strip())):
        return raw_json({"complete": False, "error": "无法核对容器名称与 InstanceID，未执行历史兜底查询。"})
    # Only the verified hexadecimal server ID and validated integer limits are interpolated.
    query = ("SELECT HistoryMainlineId, TxnDate, TxnTypeName, ContainerName, SpecId, OperationId, Qty, Comments "
             f"FROM HistoryMainline WHERE ContainerId = '{identifier}' "
             f"ORDER BY TxnDate DESC, HistoryMainlineId DESC OFFSET {start_row - 1} ROWS FETCH NEXT {limit + 1} ROWS ONLY")
    raw = await request("POST", "/api/AdHoc", base_url=CAMSTAR_QUERY_BASE_URL,
                        body={"queryText": query}, response_formatter=raw_json, retry_read_only=True)
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return raw_json({"complete": False, "error": raw})
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("HistoryMainlineId") for row in rows):
        return raw_json({"complete": False, "error": "历史查询未返回有效记录数组。", "response": rows})
    has_more = len(rows) > limit
    return raw_json({"source": "HistoryMainline via Query/AdHoc", "complete": False,
        "history_scope": "direct_container_only", "requested_page": {"start_row": start_row, "limit": limit},
        "data": {"Container": ref, "Histories": rows[:limit]},
        "pages": {"Histories": {"pagination": "server", "start_row": start_row, "requested_limit": limit,
            "returned_count": len(rows[:limit]), "server_total_known": False, "has_more": has_more,
            "next_start_row_to_try": start_row + limit if has_more else None}},
        "warnings": ["固定只读查询返回当前容器的直接历史，不包含父子容器/拆分合并等交叉关联历史。",
                     "按 TxnDate、HistoryMainlineId 倒序分页；并发新增记录时跨页不是一致性快照。"],
        "note": "Inquiry 返回空页时 auto 使用此兜底；后续分页请指定 history_source=mainline，保持相同数据来源。"})


@mcp.tool
async def get_mfgorder_execution(mfgorder_name: str, info_depth: int = 1, offset: int = 0, limit: int = 20) -> str:
    """Read MfgOrderInquiry procedure/task execution status, NOT inferred production counts. info_depth:1 order,2 procedure,3 task list,4 task. Exact named order; no broad name search."""
    if not mfgorder_name.strip() or not 1 <= info_depth <= 4 or offset < 0 or not 1 <= limit <= 100:
        return "Error: order name required; info_depth=1..4, offset>=0, limit=1..100."
    raw = await request("POST", "/api/MfgOrderInquiry", base_url=CAMSTAR_QUERY_BASE_URL,
                        body={"mfgOrder": {"name": mfgorder_name.strip()}, "infoDepth": info_depth},
                        params={"execute": "true", "$expand": "MfgOrderInfos"}, response_formatter=raw_json, retry_read_only=True)
    # Verified server business code; not an absent order or zero production.
    try:
        error = json.loads(raw.split("Response: ", 1)[-1])
        if str(error.get("ErrorCode")) == "16780524":
            return raw_json({"source": "MfgOrderInquiry", "complete": False,
                "status": "procedure_not_configured", "error": error,
                "note": "服务端报告此工单未使用生产前/后流程；不能据此推断工单不存在、没有在制品或生产完成数量为零。"})
    except (ValueError, TypeError, AttributeError):
        pass
    return read_envelope(raw, "MfgOrderInquiry", offset, limit, ["MfgOrderInfos"])
