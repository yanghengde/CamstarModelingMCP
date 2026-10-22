"""Fixed Query Inquiry wrappers. No arbitrary CDO or event execution."""
from typing import Literal

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
        params.update({"$select": "Container,Histories,HistoryDisplayTexts", "$expand": "InquiryDetail"})
    elif service == "ContainerInfoInquiry":
        params["$select"] = "Container,ContainerInfo,CurrentSpec,CurrentRouteStep"
        fields = ["ContainerInfo", "CurrentSpec", "CurrentRouteStep"]
    raw = await request("POST", f"/api/{service}", body=body,
                        base_url=CAMSTAR_QUERY_BASE_URL, params=params,
                        response_formatter=raw_json)
    return read_envelope(raw, service, limit=limit, page_fields=fields)


@mcp.tool
async def get_container_details(container_name: str, container_instance_id: str | None = None,
                                view: Literal["info", "status"] = "status") -> str:
    """Read Container using fixed Query ViewContainerStatus or ContainerInfoInquiry. Supply container_instance_id for nonunique names. This Query server rejects Level in ContainerRef. Returns server fields; missing fields are unknown, not proof of unset configuration. No Shopfloor mutation."""
    if not container_name.strip() or view not in {"info", "status"}:
        return "Error: container_name is required and view must be info or status."
    return await _inquiry("ContainerInfoInquiry" if view == "info" else "ViewContainerStatus",
                          {"container": _container(container_name, container_instance_id)})


@mcp.tool
async def get_container_history(container_name: str, container_instance_id: str | None = None,
                                start_row: int = 1, limit: int = 30) -> str:
    """Read one page of ContainerHistoryInquiry, newest first. start_row is 1-based, limit=1..100. Uses server resultsetStartRow/resultsetSizeLimit. A page is NOT the full history. No date filters: server Swagger describes startDate as unsupported."""
    if not container_name.strip() or start_row < 1 or not 1 <= limit <= 100:
        return "Error: container_name required, start_row>=1, limit=1..100."
    raw = await _inquiry("ContainerHistoryInquiry", {
        "container": _container(container_name, container_instance_id),
        "resultsetStartRow": start_row, "resultsetSizeLimit": limit, "sortAscending": False,
    })
    # Always label the server-paged result, even if it contains fewer rows.
    import json
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    data.update({"complete": False, "requested_page": {"start_row": start_row, "limit": limit},
                 "history_note": "仅本页结果；下一页 start_row 加 limit，不能声称这是全部历史。"})
    return raw_json(data)


@mcp.tool
async def get_mfgorder_execution(mfgorder_name: str, info_depth: int = 1, offset: int = 0, limit: int = 20) -> str:
    """Read MfgOrderInquiry procedure/task execution status, NOT inferred production counts. info_depth:1 order,2 procedure,3 task list,4 task. Exact named order; no broad name search."""
    if not mfgorder_name.strip() or not 1 <= info_depth <= 4 or offset < 0 or not 1 <= limit <= 100:
        return "Error: order name required; info_depth=1..4, offset>=0, limit=1..100."
    raw = await request("POST", "/api/MfgOrderInquiry", base_url=CAMSTAR_QUERY_BASE_URL,
                        body={"mfgOrder": {"name": mfgorder_name.strip()}, "infoDepth": info_depth},
                        params={"execute": "true", "$expand": "MfgOrderInfos"}, response_formatter=raw_json)
    return read_envelope(raw, "MfgOrderInquiry", offset, limit, ["MfgOrderInfos"])
