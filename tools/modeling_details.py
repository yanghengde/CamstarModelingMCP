"""Bounded, field-preserving read views backed by verified OData navigation."""
import json
from typing import Literal
from urllib.parse import quote

from core.http_client import request
from tools import mcp

VIEWS = {
    "Specs": {"summary": [], "transactions": ["TxnMap"], "labels": ["LabelTxnMap"],
              "signatures": ["ESigTxnMap"], "timers": ["StartTimerTxnMap", "EndTimerTxnMap"]},
    "Products": {"summary": [], "attributes": ["Attributes"]},
    "Operations": {"summary": [], "disallowed_transactions": ["DisallowedTxns"],
                   "scheduling": ["SchedulingDetail"]},
    "MfgOrders": {"summary": [], "materials": ["MaterialList"], "attributes": ["Attributes"],
                  "executed_tasks": ["MfgOrderExecutedTask", "MfgOrderExecutedTaskList"]},
}


def raw_json(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _compact(data):
    """Omit nulls, but retain empty arrays and all actual references/fields."""
    if isinstance(data, dict):
        return {k: _compact(v) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [_compact(v) for v in data]
    return data


def _has_next_link(data):
    if isinstance(data, dict):
        return any("nextlink" in k.lower() or _has_next_link(v) for k, v in data.items())
    if isinstance(data, list):
        return any(_has_next_link(v) for v in data)
    return False


def read_envelope(raw: str, source: str, offset: int = 0, limit: int = 50,
                  page_fields: list[str] | None = None) -> str:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(data, dict):
        return raw_json({"error": "Unexpected non-object response", "source": source})
    if data.get("error") or data.get("ErrorCode"):
        return raw_json({"source": source, "error": data})
    complete = not _has_next_link(data)
    pages = {}
    missing = []
    for key in page_fields or []:
        key = next((k for k in data if k.casefold() == key.casefold()), key)
        values = data.get(key)
        if values is None:
            missing.append(key)
            complete = False
        if isinstance(values, list):
            data[key] = values[offset:offset + limit]
            pages[key] = {"available_count": len(values), "offset": offset,
                          "returned_count": len(data[key]),
                          "next_offset": offset + limit if offset + limit < len(values) else None}
            complete = complete and offset == 0 and len(values) <= limit
    result = {"source": source, "complete": complete, "pages": pages, "missing_fields": missing,
              "data": _compact(data),
              "note": "省略 null 字段；未返回/null 不等于业务上未配置。分页 complete=false 时不得宣称全量。"}
    encoded = raw_json(result)
    if len(encoded) > 80000:
        return raw_json({"source": source, "complete": False,
                         "error": "结果过大，未裁剪字段或伪装成完整结果。请减少 limit 或选择更具体的详情分组。"})
    return encoded


async def _details(entity: str, key: str, view: str, offset: int, limit: int) -> str:
    if not key.strip() or not 1 <= limit <= 200 or offset < 0 or view not in VIEWS[entity]:
        return "Error: provide InstanceID, valid view, offset>=0 and limit=1..200."
    fields = VIEWS[entity][view]
    params = {"$expand": ",".join(fields)} if fields else None
    path = f"/api/{entity}/{quote(key.strip(), safe='')}"
    raw = await request("GET", path, params=params, response_formatter=raw_json)
    return read_envelope(raw, f"{entity}/{key}:{view}", offset, limit, fields)


@mcp.tool
async def get_spec_details(key: str, view: Literal["summary", "transactions", "labels", "signatures", "timers"] = "summary", offset: int = 0, limit: int = 50) -> str:
    """Read Spec refs (Operation, ResourceGroup, Setup, ElectronicProcedure) or configuration navigation. key=InstanceID. Select one view; no field whitelist trimming. Check complete/pages. Null does not prove no configuration."""
    return await _details("Specs", key, view, offset, limit)


@mcp.tool
async def get_product_details(key: str, view: Literal["summary", "attributes"] = "summary", offset: int = 0, limit: int = 50) -> str:
    """Read Product actual Workflow/BOM/BillOfProcess refs or Attributes. key=InstanceID. Follow returned Workflow via list_workflows and list_workflow_steps, never infer it from names."""
    return await _details("Products", key, view, offset, limit)


@mcp.tool
async def get_operation_details(key: str, view: Literal["summary", "disallowed_transactions", "scheduling"] = "summary", offset: int = 0, limit: int = 50) -> str:
    """Read Operation WorkCenter/DispatchRule/reason-group refs, DisallowedTxns or SchedulingDetail. key=InstanceID. Paginated output retains actual fields and reports completeness."""
    return await _details("Operations", key, view, offset, limit)


@mcp.tool
async def get_mfgorder_details(key: str, view: Literal["summary", "materials", "attributes", "executed_tasks"] = "summary", offset: int = 0, limit: int = 50) -> str:
    """Read MfgOrder actual product/quantity/status refs, materials, attributes or executed tasks. key=InstanceID. This is modeling configuration, not proof of completed production quantity."""
    return await _details("MfgOrders", key, view, offset, limit)
