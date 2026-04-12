"""
Camstar Modeling Specs MCP Server
=================================
An MCP Server built with FastMCP for the Camstar Modeling SpecEntity API.
Each Swagger endpoint is exposed as an @mcp.tool with automatic Bearer auth
and smart response truncation.
"""

import os
import json
import logging
from typing import Optional

import httpx
from fastmcp import FastMCP

from generate_token import generate_camstar_auth_token

# ---------------------------------------------------------------------------
# Configuration – all values come from environment variables
# ---------------------------------------------------------------------------
BASE_URL = os.getenv("CAMSTAR_BASE_URL", "http://localhost/Modeling")
CAMSTAR_USERNAME = os.getenv("CAMSTAR_USERNAME", "CamstarAdmin")
CAMSTAR_PASSWORD = os.getenv("CAMSTAR_PASSWORD", "Cam1star")
REQUEST_TIMEOUT = int(os.getenv("CAMSTAR_TIMEOUT", "30"))

# Maximum response characters before we trim to key fields only
MAX_RESPONSE_LENGTH = int(os.getenv("MAX_RESPONSE_LENGTH", "4000"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("camstar-mcp")

# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "CamstarModelingSpecs",
    instructions=(
        "MCP Server for Camstar Modeling SpecEntity API. "
        "Provides tools to list, get, create, update, patch, delete Specs, "
        "get Spec count, and request selection values."
    ),
)

# ---------------------------------------------------------------------------
# Shared HTTP helpers
# ---------------------------------------------------------------------------

def _get_headers() -> dict:
    """Build common request headers with Bearer auth."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    
    # Generate token dynamically using the generate_token module
    try:
        token = generate_camstar_auth_token(CAMSTAR_USERNAME, CAMSTAR_PASSWORD)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    except Exception as e:
        logger.error(f"Failed to generate auth token: {e}")
        
    return headers


def _build_url(path: str) -> str:
    """Construct full URL from a relative API path."""
    base = BASE_URL.rstrip("/")
    return f"{base}{path}"


# ---------------------------------------------------------------------------
# Key‑field extraction for overly large responses
# ---------------------------------------------------------------------------

# Fields we consider "key" – covers ID, status, time‑related, and name info (lowercase for case-insensitive match)
KEY_FIELDS = {
    "instanceid",
    "displayname",
    "name",
    "revision",
    "status",
    "description",
    "isfrozen",
    "isrevofrcd",
    "lastchangedate",
    "lastchangedategmt",
    "creationdate",
    "creationdategmt",
    "creationusername",
    "currentstatus",
    "control",
    "eco",
    "operation",
    "useror",
}

def _extract_key_fields(obj):
    """
    Recursively extract only KEY_FIELDS from an object or list.
    Supports case-insensitive key matching and OData 'value' arrays.
    """
    if isinstance(obj, list):
        return [_extract_key_fields(item) for item in obj]

    if isinstance(obj, dict):
        trimmed = {}
        for key, value in obj.items():
            lkey = key.lower()
            if lkey in KEY_FIELDS:
                trimmed[key] = value
            elif lkey == "value" and isinstance(value, list):
                trimmed[key] = [_extract_key_fields(item) for item in value]
                
        # Always keep 'operation' sub-object if present (it's a ref)
        # Handle lowercase or uppercase 'Operation'
        for k, v in obj.items():
            if k.lower() == "operation" and isinstance(v, dict):
                trimmed[k] = v
        return trimmed

    return obj


def _smart_response(data) -> str:
    """
    Return the JSON string of *data*.
    If it exceeds MAX_RESPONSE_LENGTH, re‑serialize with only key fields
    and attach a note that the response was truncated.
    """
    full_text = json.dumps(data, ensure_ascii=False, indent=2)

    if len(full_text) <= MAX_RESPONSE_LENGTH:
        return full_text

    trimmed = _extract_key_fields(data)
    trimmed_text = json.dumps(trimmed, ensure_ascii=False, indent=2)
    return (
        "⚠️ Response was too large and has been trimmed to key fields "
        "(instanceID, name, revision, status, description, timestamps, etc.).\n"
        "Use get_spec(key) for the full object.\n\n"
        + trimmed_text
    )


async def _request(method: str, path: str, body: dict | None = None,
                    params: dict | None = None) -> str:
    """
    Central HTTP request dispatcher.  Returns the response text after
    smart truncation, or an error message.
    """
    url = _build_url(path)
    headers = _get_headers()

    logger.info("%s %s", method.upper(), url)

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, verify=False) as client:
            resp = await client.request(
                method,
                url,
                headers=headers,
                json=body,
                params=params,
            )

        if resp.status_code >= 400:
            return (
                f"❌ HTTP {resp.status_code} Error\n"
                f"URL: {url}\n"
                f"Response: {resp.text[:2000]}"
            )

        # Some endpoints return empty 200
        if not resp.text.strip():
            return f"✅ {method.upper()} succeeded (HTTP {resp.status_code}, empty body)."

        try:
            data = resp.json()
        except Exception:
            return resp.text[:MAX_RESPONSE_LENGTH]

        return _smart_response(data)

    except httpx.TimeoutException:
        return f"❌ Request timed out after {REQUEST_TIMEOUT}s: {method.upper()} {url}"
    except Exception as exc:
        return f"❌ Request failed: {repr(exc)}"


# =========================================================================
# MCP Tools – one per API endpoint
# =========================================================================

@mcp.tool
async def list_specs() -> str:
    """
    List all Specs.
    GET /api/Specs
    Returns an array of SpecEntity objects with key fields
    (instanceID, name, revision, status, timestamps, etc.).
    Large responses are automatically trimmed.
    """
    return await _request("GET", "/api/Specs")


@mcp.tool
async def get_spec(key: str) -> str:
    """
    Get a single Spec by its key (instanceID or name:revision).
    GET /api/Specs/{key}
    Returns the full SpecEntity object.
    """
    return await _request("GET", f"/api/Specs/{key}")


@mcp.tool
async def get_spec_by_odata_key(key: str) -> str:
    """
    Get a single Spec using OData key syntax.
    GET /api/Specs({key})
    Example key: 'MySpec:001'
    Returns the full SpecEntity object.
    """
    return await _request("GET", f"/api/Specs({key})")


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

    return await _request("POST", "/api/Specs", body=payload)


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

    return await _request("PUT", f"/api/Specs/{key}", body=payload)


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

    return await _request("PUT", f"/api/Specs({key})", body=payload)


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

    return await _request("PATCH", "/api/Specs", body=payload)


@mcp.tool
async def delete_spec(key: str) -> str:
    """
    Delete a Spec by key.
    DELETE /api/Specs/{key}
    """
    return await _request("DELETE", f"/api/Specs/{key}")


@mcp.tool
async def delete_spec_by_odata_key(key: str) -> str:
    """
    Delete a Spec using OData key syntax.
    DELETE /api/Specs({key})
    """
    return await _request("DELETE", f"/api/Specs({key})")


@mcp.tool
async def get_specs_count() -> str:
    """
    Get the total count of Specs.
    GET /api/Specs/$count
    Returns the count (or a list, depending on the server implementation).
    """
    return await _request("GET", "/api/Specs/$count")


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

    return await _request("POST", "/api/Specs/RequestSelectionValues",
                          body=payload, params=params or None)


# =========================================================================
# Entrypoint
# =========================================================================

if __name__ == "__main__":
    mcp.run()
