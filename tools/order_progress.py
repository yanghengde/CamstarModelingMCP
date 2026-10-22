"""Order-linked WIP evidence, kept separate from procedure-task inquiries."""
import json
import re

from tools import mcp
from tools.process_context import ReadContext
from tools.container_inquiry import get_container_details, get_container_history
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_QUERY_BASE_URL


@mcp.tool
async def get_mfgorder_progress(mfgorder_name: str, offset: int = 0, limit: int = 10, include_history: bool = True) -> str:
    """Read actual order-linked Containers, current workflow/step/status and optional latest 3 direct history rows each. offset>=0, limit=1..20. Closed/last step is NOT proof of completed production. No inferred completion percentage. Fixed SQL Server Container lookup; errors explicitly reported if deployment/schema/permissions do not support it."""
    if not mfgorder_name.strip() or offset < 0 or not 1 <= limit <= 20:
        return raw_json({"error": "工单名必填；offset>=0，limit=1..20"})
    result = {"complete": False, "containers": [], "warnings": [], "production_completed_qty": None,
              "completion_percentage": None,
              "note": "只展示关联容器及现场证据；不把 Closed、末步骤或前后置任务状态等同于生产完成。"}
    try:
        order = await ReadContext().resolve("MfgOrders", {"Name": mfgorder_name.strip()}, revisioned=False)
        identifier = order["InstanceID"]
        if not re.fullmatch(r"[0-9a-fA-F]{16}", identifier):
            raise ValueError("工单 ID 无法校验，未查询容器")
        result["order"] = {k: order.get(k) for k in ("Name", "InstanceID", "Product", "Qty", "Status", "UOM")}
        query = ("SELECT ContainerId, ContainerName FROM Container "
                 f"WHERE MfgOrderId = '{identifier}' ORDER BY ContainerId OFFSET {offset} ROWS FETCH NEXT {limit + 1} ROWS ONLY")
        raw = await request("POST", "/api/AdHoc", base_url=CAMSTAR_QUERY_BASE_URL, body={"queryText": query}, response_formatter=raw_json)
        try:
            rows = json.loads(raw)
        except ValueError:
            raise ValueError(raw) from None
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not r.get("ContainerId") or not r.get("ContainerName") for r in rows):
            raise ValueError("容器关联查询返回格式异常")
        result["page"] = {"offset": offset, "limit": limit, "has_more": len(rows) > limit,
                          "next_offset": offset + limit if len(rows) > limit else None, "total_count": None}
        for row in rows[:limit]:
            item = {"name": row["ContainerName"], "instance_id": row["ContainerId"]}
            result["containers"].append(item)
            try:
                status = json.loads(await get_container_details(item["name"], item["instance_id"]))
                details = status.get("data", {}).get("ContainerStatusDetails")
                if not isinstance(details, dict) or not details:
                    raise ValueError(str(status.get("error") or "容器状态未返回"))
                ref = details.get("MfgOrder") or {}
                if ref.get("InstanceId") != identifier:
                    raise ValueError("状态中的工单引用不一致或未返回；容器可能已改绑")
                item["current"] = {k: details.get(k) for k in ("Product", "Workflow", "Step", "Spec", "Operation", "Qty", "UOM", "StatusName", "InProcess", "InRework", "OnHold", "LastMoveDate")}
                item["state_evidence"] = {
                    "in_process": details.get("InProcess"), "on_hold": details.get("OnHold"),
                    "in_rework": details.get("InRework"), "status_name": details.get("StatusName"),
                    "production_completed": None}
                if include_history:
                    item["recent_history"] = json.loads(await get_container_history(item["name"], item["instance_id"], limit=3, history_source="mainline"))
            except (ValueError, TypeError) as exc:
                item["error"] = str(exc)
        result["returned_container_count"] = len(result["containers"])
        steps = {}
        for item in result["containers"]:
            current = item.get("current", {})
            workflow = current.get("Workflow") or {}
            step = current.get("Step") or {}
            key = (workflow.get("Name"), workflow.get("Revision"), step.get("Name"))
            steps[key] = steps.get(key, 0) + 1
        result["page_step_distribution"] = [{"workflow": k[0], "revision": k[1], "step": k[2], "container_count": count}
                                            for k, count in steps.items()]
        result["container_list_complete"] = offset == 0 and not result["page"]["has_more"]
        if not rows:
            result["warnings"].append("本页无关联容器，不等于工单未开工或已完成。")
        result["warnings"].append("OnHold 等未返回字段为未知；容器数量不能直接等同工单产量。跨请求结果不是事务快照。")
    except (ValueError, KeyError) as exc:
        result["error"] = str(exc)
    return raw_json(result)
