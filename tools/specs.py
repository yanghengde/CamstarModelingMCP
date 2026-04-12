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


@mcp.tool
async def list_specs() -> str:
    """
    List all Specs.
    GET /api/Specs
    Returns an array of SpecEntity objects with key fields
    (instanceID, name, revision, status, timestamps, etc.).
    Large responses are automatically trimmed.
    """
    return await request("GET", "/api/Specs")


@mcp.tool
async def get_spec(key: str) -> str:
    """
    Get a single Spec by its key (instanceID or name:revision).
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
    name: str,
    revision: str,
    operation_name: str,
    description: Optional[str] = None,
    status: Optional[int] = None,
    body_json: Optional[str] = None,
) -> str:
    """
    Fully update (replace) an existing Spec by key.
    PUT /api/Specs/{key}

    Required fields: name, revision, operation (by name).
    Optional: description, status, or any extra fields via body_json.
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

    if body_json:
        try:
            extra = json.loads(body_json)
            payload.update(extra)
        except json.JSONDecodeError as e:
            return f"❌ Invalid body_json: {e}"

    return await request("PUT", f"/api/Specs/{key}", body=payload)


@mcp.tool
async def update_spec_by_odata_key(
    key: str,
    name: str,
    revision: str,
    operation_name: str,
    description: Optional[str] = None,
    status: Optional[int] = None,
    body_json: Optional[str] = None,
) -> str:
    """
    Fully update (replace) an existing Spec using OData key syntax.
    PUT /api/Specs({key})

    Required fields: name, revision, operation (by name).
    Optional: description, status, or any extra fields via body_json.
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

    if body_json:
        try:
            extra = json.loads(body_json)
            payload.update(extra)
        except json.JSONDecodeError as e:
            return f"❌ Invalid body_json: {e}"

    return await request("PUT", f"/api/Specs({key})", body=payload)


@mcp.tool
async def patch_spec(body_json: str) -> str:
    """
    Partially update (patch) a Spec.
    PATCH /api/Specs

    Provide a JSON string with the fields to update (must include at least
    the identifying fields like name + revision).
    Returns the updated Spec key string on success.
    """
    try:
        payload = json.loads(body_json)
    except json.JSONDecodeError as e:
        return f"❌ Invalid body_json: {e}"

    return await request("PATCH", "/api/Specs", body=payload)


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
