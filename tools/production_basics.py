"""Verified production primitives. Contracts: production-api-audit-evidence.json."""
import math
from typing import Literal
from tools import mcp
from core.http_client import request, request_shopfloor

REASONS = {
    'hold': 'HoldReasons', 'release': 'ReleaseReasons', 'scrap': 'ScrapReasons',
    'quantity_adjust': 'QtyAdjustReasons', 'rework': 'ReworkReasons',
    'container_defect': 'ContainerDefectReasons',
}


@mcp.tool
async def list_production_reasons(kind: Literal['hold', 'release', 'scrap', 'quantity_adjust', 'rework', 'container_defect'],
                                  top: int = 30, skip: int = 0, filter_expr: str | None = None) -> str:
    """Read verified Modeling reason entities. Pagination is not a full list.
    ScrapReasons existence does not establish a Container scrap transaction.
    """
    if kind not in REASONS or not 1 <= top <= 100 or skip < 0:
        return 'Error: invalid reason kind or pagination (top=1..100, skip>=0).'
    params = {'$top': top, '$skip': skip}
    if filter_expr:
        params['$filter'] = filter_expr
    return await request('GET', '/api/' + REASONS[kind], params=params)


def _container(name, instance_id):
    if not name.strip():
        raise ValueError('container_name must not be empty')
    ref = {'name': name.strip()}
    if instance_id is not None:
        if not instance_id.strip():
            raise ValueError('container_instance_id must not be empty')
        ref['instanceId'] = instance_id.strip()
    return ref


async def _hold_release(service, field, container_name, reason_name, instance_id, comments):
    try:
        ref = _container(container_name, instance_id)
        if not reason_name.strip():
            raise ValueError('reason_name must not be empty')
    except ValueError as exc:
        return f'Error: {exc}'
    body = {'container': ref, field: {'name': reason_name.strip()}}
    if comments is not None:
        body['comments'] = comments
    return await request_shopfloor('POST', '/api/' + service, body=body)


@mcp.tool
async def hold_container(container_name: str, reason_name: str, container_instance_id: str | None = None,
                         comments: str | None = None) -> str:
    """POST Shopfloor /api/Hold: place a Container on hold. Select a real HoldReason.
    Provide instance ID for ambiguous names. Server validates state/permissions.
    Reason is required by this wrapper; Swagger itself requires container only.
    """
    return await _hold_release('Hold', 'holdReason', container_name, reason_name, container_instance_id, comments)


@mcp.tool
async def release_container(container_name: str, reason_name: str, container_instance_id: str | None = None,
                            comments: str | None = None) -> str:
    """POST Shopfloor /api/Release: release a Container hold, NOT release a manufacturing order.
    Select a real ReleaseReason. Reason is required by this wrapper.
    """
    return await _hold_release('Release', 'releaseReason', container_name, reason_name, container_instance_id, comments)


@mcp.tool
async def change_container_quantity(container_name: str, change_qty_type: Literal[1, 2, 3, 4, 5, 6],
                                    reason_name: str, qty: float, container_instance_id: str | None = None,
                                    qty2: float | None = None, comments: str | None = None) -> str:
    """POST Shopfloor /api/ChangeQty with one ChangeQtyDetails entry.
    Product enum: 1=defect, 2=loss, 3=sell, 4=adjust, 5=bonus, 6=buy.
    qty is the transaction quantity, NOT a target balance. No inferred scrap,
    completion, throughput adjustment or full-container loss. Server validates
    reason compatibility and quantity semantics; do not retry blindly.
    """
    try:
        ref = _container(container_name, container_instance_id)
        if type(change_qty_type) is not int or change_qty_type not in range(1, 7):
            raise ValueError('change_qty_type must be 1..6')
        if not reason_name.strip():
            raise ValueError('reason_name must not be empty')
        if not math.isfinite(qty) or (qty2 is not None and not math.isfinite(qty2)):
            raise ValueError('quantity must be finite')
    except ValueError as exc:
        return f'Error: {exc}'
    detail = {'changeQtyType': change_qty_type, 'reasonCode': {'name': reason_name.strip()}, 'qty': qty}
    if qty2 is not None:
        detail['qty2'] = qty2
    if comments is not None:
        detail['comments'] = comments
    return await request_shopfloor('POST', '/api/ChangeQty', body={'container': ref, 'serviceDetails': [detail]})
