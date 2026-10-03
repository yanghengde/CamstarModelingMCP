"""
ContainerStart MCP tool
=======================
Swagger: Shopfloor /api/Start

Starts a new Camstar Container for a manufacturing order. For unit-level
tracking, the container name is normally the product serial number.
"""

import json
from typing import Optional

from core.http_client import request_shopfloor
from tools import mcp


_START_KEYS = {
    "comments": "comments",
    "currentstatusdetails": "currentStatusDetails",
    "details": "details",
    "factory": "factory",
}

_START_DETAILS_KEYS = {
    "autonumber": "autoNumber",
    "autonumberrule": "autoNumberRule",
    "containername": "containerName",
    "level": "level",
    "mfgorder": "mfgOrder",
    "owner": "owner",
    "product": "product",
    "qty": "qty",
    "uom": "uom",
}

_REFERENCE_KEYS = {
    "name": "name",
    "revision": "revision",
    "useror": "useROR",
    "instanceid": "instanceId",
    "cdotypename": "cdoTypeName",
}


def _merge_case_insensitive(
    target: dict,
    source: dict,
    known_keys: Optional[dict[str, str]] = None,
) -> None:
    """Recursively merge dictionaries without OData case-only duplicates."""
    canonical_keys = {key.casefold(): key for key in target}
    for key, value in source.items():
        folded_key = key.casefold()
        target_key = canonical_keys.get(
            folded_key,
            (known_keys or {}).get(folded_key, key),
        )
        current_value = target.get(target_key)
        if isinstance(current_value, dict) and isinstance(value, dict):
            child_keys = _REFERENCE_KEYS
            if target_key == "details":
                child_keys = _START_DETAILS_KEYS
            _merge_case_insensitive(current_value, value, child_keys)
        else:
            target[target_key] = value
        canonical_keys[folded_key] = target_key


@mcp.tool
async def container_start(
    mfg_order_name: str,
    product_name: str,
    product_revision: str,
    qty: float,
    level_name: str,
    container_name: Optional[str] = None,
    auto_number_rule_name: Optional[str] = None,
    uom_name: Optional[str] = None,
    workflow_name: Optional[str] = None,
    workflow_revision: Optional[str] = None,
    factory_name: Optional[str] = None,
    owner_name: Optional[str] = None,
    comments: Optional[str] = None,
    body_json: Optional[str] = None,
) -> str:
    """
    Start a Container (serial number) against a manufacturing order.
    POST Shopfloor /api/Start

    Required:
      - mfg_order_name: Manufacturing order name.
      - product_name and product_revision: Revisioned Product reference.
      - qty: Initial quantity in the Container. Use 1 for a unit serial number.
      - level_name: Camstar Container Level, for example "Unit".

    Numbering (choose exactly one):
      - container_name: Explicit Container/serial number.
      - auto_number_rule_name: Camstar numbering rule used to generate the name.

    Before starting, use get_container_level to validate level_name and
    get_numbering_rule to validate auto_number_rule_name when applicable.

    Optional fields include UOM, workflow, factory, owner, comments, and
    body_json. body_json must be a JSON object and is recursively merged into
    the Start payload using case-insensitive keys.
    """
    has_container_name = bool(container_name and container_name.strip())
    has_number_rule = bool(
        auto_number_rule_name and auto_number_rule_name.strip()
    )
    if has_container_name == has_number_rule:
        return (
            "❌ Provide exactly one of container_name or "
            "auto_number_rule_name."
        )
    if qty <= 0:
        return "❌ qty must be greater than 0."

    details: dict = {
        "mfgOrder": {"name": mfg_order_name},
        "product": {
            "name": product_name,
            "revision": product_revision,
        },
        "qty": qty,
        "level": {"name": level_name},
    }
    if has_container_name:
        details["containerName"] = container_name.strip()
        details["autoNumber"] = False
    else:
        details["autoNumber"] = True
        details["autoNumberRule"] = {
            "name": auto_number_rule_name.strip()
        }
    if uom_name is not None:
        details["uom"] = {"name": uom_name}
    if workflow_name is not None:
        if not workflow_revision:
            return (
                "❌ workflow_revision is required when workflow_name is "
                "provided."
            )
        payload_workflow = {
            "workflow": {
                "name": workflow_name,
                "revision": workflow_revision,
            }
        }

    payload: dict = {"details": details}
    if workflow_name is not None:
        payload["currentStatusDetails"] = payload_workflow
    if factory_name is not None:
        payload["factory"] = {"name": factory_name}
    if owner_name is not None:
        details["owner"] = {"name": owner_name}
    if comments is not None:
        payload["comments"] = comments

    if body_json:
        try:
            extra = json.loads(body_json)
        except json.JSONDecodeError as exc:
            return f"❌ Invalid body_json: {exc}"
        if not isinstance(extra, dict):
            return "❌ Invalid body_json: expected a JSON object."
        _merge_case_insensitive(payload, extra, _START_KEYS)

    return await request_shopfloor("POST", "/api/Start", body=payload)
