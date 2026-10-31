"""
Spec 实体 MCP 工具
====================
Swagger: /api/Specs
每个 API 端点均对应一个 @mcp.tool。
"""

import json
from typing import Optional

from tools import mcp
from core.http_client import request
from core.modeling_payload import build_update_payload
from core.tool_catalog import DISABLED_MODELING_PATCH_TOOLS


@mcp.tool
async def list_specs(
    filter_expr: Optional[str] = None,
    top: Optional[int] = None,
    skip: Optional[int] = None,
    select: Optional[str] = None,
    expand: Optional[str] = None,
    orderby: Optional[str] = None,
) -> str:
    """
    List Specs with optional OData querying.
    GET /api/Specs
    
    Optional OData Parameters:
      - filter_expr: example "Name eq 'ABC'"
      - top: limit number of results
      - skip: skip number of results
      - select: fields to return
      - expand: navigation properties to expand
      - orderby: order sequence, e.g., "Name desc"
      
    Returns an array of SpecEntity objects. Large responses are automatically trimmed.
    """
    params = {}
    if filter_expr: params["$filter"] = filter_expr
    if top is not None: params["$top"] = top
    if skip is not None: params["$skip"] = skip
    if select: params["$select"] = select
    if expand: params["$expand"] = expand
    if orderby: params["$orderby"] = orderby
    
    return await request("GET", "/api/Specs", params=params or None)


@mcp.tool
async def get_spec(key: str) -> str:
    """
    Get a single Spec using the InstanceID returned by list_specs.
    Prefer InstanceID; name:revision is not supported by every server.
    GET /api/Specs/{key}
    Returns the full SpecEntity object.
    """
    return await request("GET", f"/api/Specs/{key}")


@mcp.tool
async def get_spec_by_odata_key(key: str) -> str:
    """
    Get a single Spec using OData key syntax.
    GET /api/Specs({key})
    Example key: 'MySpec:001'
    Returns the full SpecEntity object.
    """
    return await request("GET", f"/api/Specs({key})")


@mcp.tool
async def create_spec(
    name: str,
    revision: str,
    operation_name: str,
    description: Optional[str] = None,
    status: Optional[int] = None,
    body_json: Optional[str] = None,
) -> str:
    """
    Create a new Spec.
    POST /api/Specs

    Required fields: name, revision, operation (by name).
    Optional: description, status (1=Active, 2=Inactive), and any
    additional fields via body_json (a JSON string merged into the payload).
    """
    payload: dict = {
        "name": name,
        "revision": revision,
        "operation": {"name": operation_name},
    }
    if description is not None:
        payload["description"] = description
    if status is not None:
        payload["status"] = status

    # Merge extra fields from caller
    if body_json:
        try:
            extra = json.loads(body_json)
            payload.update(extra)
        except json.JSONDecodeError as e:
            return f"❌ Invalid body_json: {e}"

    return await request("POST", "/api/Specs", body=payload)


@mcp.tool
async def update_spec(
    key: str,
    name: Optional[str] = None,
    revision: Optional[str] = None,
    operation_name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[int] = None,
    body_json: Optional[str] = None,
) -> str:
    """Update explicit fields of an existing Specs object using JSON PUT.
    Only key is required; prefer the InstanceID returned by a read tool.
    Omitted fields are preserved. Supply only user-requested changes; do not
    echo a GET snapshot, read-only fields, or root UseRor. body_json accepts
    a JSON object with write fields (case normalized to the bundled schema).
    Collection PATCH is not the ordinary update path. Read back the same
    instance with the corresponding get tool and verify the changed fields.
    A timeout/unknown result requires read-only verification, never a retry.
    Example: update_spec(key="<InstanceID>", body_json='{"allowReprocess":true}').
    """
    payload: dict = {
        "name": name,
        "revision": revision,
        "operation": {"name": operation_name} if operation_name is not None else None,
    }
    if description is not None:
        payload["description"] = description
    if status is not None:
        payload["status"] = status

    try:
        payload = build_update_payload("Specs", payload, body_json)
    except ValueError as exc:
        return f"❌ {exc}"

    return await request("PUT", f"/api/Specs/{key}", body=payload)


@mcp.tool
async def update_spec_by_odata_key(
    key: str,
    name: Optional[str] = None,
    revision: Optional[str] = None,
    operation_name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[int] = None,
    body_json: Optional[str] = None,
) -> str:
    """Update explicit fields of an existing Specs object using JSON PUT.
    Only key is required; prefer the InstanceID returned by a read tool.
    Omitted fields are preserved. Supply only user-requested changes; do not
    echo a GET snapshot, read-only fields, or root UseRor. body_json accepts
    a JSON object with write fields (case normalized to the bundled schema).
    Collection PATCH is not the ordinary update path. Read back the same
    instance with the corresponding get tool and verify the changed fields.
    A timeout/unknown result requires read-only verification, never a retry.
    Example: update_spec(key="<InstanceID>", body_json='{"allowReprocess":true}').
    """
    payload: dict = {
        "name": name,
        "revision": revision,
        "operation": {"name": operation_name} if operation_name is not None else None,
    }
    if description is not None:
        payload["description"] = description
    if status is not None:
        payload["status"] = status

    try:
        payload = build_update_payload("Specs", payload, body_json)
    except ValueError as exc:
        return f"❌ {exc}"

    return await request("PUT", f"/api/Specs({key})", body=payload)


@mcp.tool
async def patch_spec(body_json: str) -> str:
    """Disabled compatibility entry: collection PATCH is not ordinary update.
    Use the corresponding update tool with InstanceID and minimal JSON, then
    read back the same instance. This tool never sends an HTTP request.
    """
    return DISABLED_MODELING_PATCH_TOOLS["patch_spec"]


@mcp.tool
async def delete_spec(key: str) -> str:
    """
    Delete a Spec by key.
    DELETE /api/Specs/{key}
    """
    return await request("DELETE", f"/api/Specs/{key}")


@mcp.tool
async def delete_spec_by_odata_key(key: str) -> str:
    """
    Delete a Spec using OData key syntax.
    DELETE /api/Specs({key})
    """
    return await request("DELETE", f"/api/Specs({key})")


@mcp.tool
async def get_specs_count() -> str:
    """
    Get the total count of Specs.
    GET /api/Specs/$count
    Returns the count (or a list, depending on the server implementation).
    """
    return await request("GET", "/api/Specs/$count")


@mcp.tool
async def request_selection_values(
    selection_values_expression: Optional[str] = None,
    body_json: Optional[str] = None,
) -> str:
    """
    Request selection (dropdown / LOV) values for a Spec entity.
    POST /api/Specs/RequestSelectionValues

    selection_values_expression: An OData-style expression to filter
        which selection values to return (optional query parameter).
    body_json: A JSON string representing a partial SpecEntity whose context
        drives the selection value resolution (optional request body).
    """
    params = {}
    if selection_values_expression:
        params["selectionValuesExpression"] = selection_values_expression

    payload = None
    if body_json:
        try:
            payload = json.loads(body_json)
        except json.JSONDecodeError as e:
            return f"❌ Invalid body_json: {e}"

    return await request("POST", "/api/Specs/RequestSelectionValues",
                         body=payload, params=params or None)
