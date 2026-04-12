"""
Workflow Entity MCP Tools
=========================
Swagger: /api/Workflows
Each API endpoint corresponds to a @mcp.tool.
"""

import json
from typing import Optional

from tools import mcp
from core.http_client import request


@mcp.tool
async def list_workflows() -> str:
    """
    List all Workflows.
    GET /api/Workflows
    Returns an array of WorkflowEntity objects with key fields.
    Large responses are automatically trimmed.
    """
    return await request("GET", "/api/Workflows")


@mcp.tool
async def get_workflow(key: str) -> str:
    """
    Get a single Workflow by its key (instanceID or name:revision).
    GET /api/Workflows/{key}
    Returns the full WorkflowEntity object.
    """
    return await request("GET", f"/api/Workflows/{key}")


@mcp.tool
async def get_workflow_by_odata_key(key: str) -> str:
    """
    Get a single Workflow using OData key syntax.
    GET /api/Workflows({key})
    Example key: 'MyWorkflow:1'
    Returns the full WorkflowEntity object.
    """
    return await request("GET", f"/api/Workflows({key})")


@mcp.tool
async def create_workflow(
    name: str,
    revision: str,
    description: Optional[str] = None,
    status: Optional[int] = None,
    body_json: Optional[str] = None,
) -> str:
    """
    Create a new Workflow.
    POST /api/Workflows

    Required fields:
      - name: Workflow name
      - revision: Workflow revision (e.g., '1', 'A')
    Optional: description, status (1=Active, 2=Inactive), and any additional fields via body_json.
    """
    payload: dict = {
        "name": name,
        "revision": revision,
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

    return await request("POST", "/api/Workflows", body=payload)


@mcp.tool
async def update_workflow(
    key: str,
    name: str,
    revision: str,
    description: Optional[str] = None,
    status: Optional[int] = None,
    body_json: Optional[str] = None,
) -> str:
    """
    Fully update (replace) an existing Workflow by key.
    PUT /api/Workflows/{key}

    Required fields: name, revision.
    Optional: description, status, or any extra fields via body_json.
    """
    payload: dict = {
        "name": name,
        "revision": revision,
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

    return await request("PUT", f"/api/Workflows/{key}", body=payload)


@mcp.tool
async def update_workflow_by_odata_key(
    key: str,
    name: str,
    revision: str,
    description: Optional[str] = None,
    status: Optional[int] = None,
    body_json: Optional[str] = None,
) -> str:
    """
    Fully update (replace) an existing Workflow using OData key syntax.
    PUT /api/Workflows({key})

    Required fields: name, revision.
    Optional: description, status, or any extra fields via body_json.
    """
    payload: dict = {
        "name": name,
        "revision": revision,
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

    return await request("PUT", f"/api/Workflows({key})", body=payload)


@mcp.tool
async def patch_workflow(
    body_json: str,
) -> str:
    """
    Patch an existing Workflow (partial update). Key should be in the URL or payload if Camstar uses it like Specs do.
    PATCH /api/Workflows
    Wait, Camstar patch typically takes no key in URL, but requires OData/InstanceId in body, or similar.
    Provide the partial JSON payload in body_json.
    """
    try:
        payload = json.loads(body_json)
    except json.JSONDecodeError as e:
        return f"❌ Invalid JSON in body_json: {e}"

    return await request("PATCH", "/api/Workflows", body=payload)


@mcp.tool
async def delete_workflow(key: str) -> str:
    """
    Delete a Workflow by key.
    DELETE /api/Workflows/{key}
    """
    return await request("DELETE", f"/api/Workflows/{key}")


@mcp.tool
async def delete_workflow_by_odata_key(key: str) -> str:
    """
    Delete a Workflow using OData key syntax.
    DELETE /api/Workflows({key})
    """
    return await request("DELETE", f"/api/Workflows({key})")


@mcp.tool
async def get_workflows_count() -> str:
    """
    Get the total count of Workflows.
    GET /api/Workflows/$count
    """
    return await request("GET", "/api/Workflows/$count")


@mcp.tool
async def request_workflow_selection_values(
    selection_values_expression: Optional[str] = None,
    body_json: Optional[str] = None,
) -> str:
    """
    Request selection (dropdown / LOV) values for a Workflow entity.
    POST /api/Workflows/RequestSelectionValues

    selection_values_expression: An OData-style expression to filter
        which selection values to return (optional query parameter).
    body_json: A JSON string representing a partial WorkflowEntity whose
        context drives the selection value resolution (optional request body).
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

    return await request("POST", "/api/Workflows/RequestSelectionValues",
                         body=payload, params=params or None)


@mcp.tool
async def add_spec_step_to_workflow(
    workflow_name: str,
    workflow_revision: str,
    step_name: str,
    spec_name: str,
    spec_revision: Optional[str] = None,
    sequence: Optional[int] = None
) -> str:
    """
    Add a step associated with a Spec to an existing Workflow.
    PUT /api/Workflows/{workflow_name}:{workflow_revision}

    Required fields:
      - workflow_name: Name of the target workflow
      - workflow_revision: Revision of the target workflow
      - step_name: Name for the new step in the workflow
      - spec_name: Name of the Spec to associate with this step

    Optional:
      - spec_revision: Specific revision of the Spec. Defaults to Revision of Record if not provided.
      - sequence: Sequence number for the step.
    """
    spec_ref = {"name": spec_name}
    if spec_revision:
        spec_ref["revision"] = spec_revision
    else:
        spec_ref["useROR"] = True

    payload = {
        "name": workflow_name,
        "revision": workflow_revision,
        "steps-Expanded": [
            {
                "listItemAction": "add",
                "value": {
                    "@odata.type": "#modeling.SpecStepChanges",
                    "name": step_name,
                    "spec": spec_ref
                }
            }
        ]
    }
    
    if sequence is not None:
        payload["steps-Expanded"][0]["value"]["sequence"] = sequence

    key = f"{workflow_name}:{workflow_revision}"
    return await request("PUT", f"/api/Workflows/{key}", body=payload)
