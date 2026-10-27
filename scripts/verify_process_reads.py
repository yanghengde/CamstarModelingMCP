"""Opt-in live, read-only verification. Never creates fixtures in Camstar.

python scripts/verify_process_reads.py --container NAME --order NAME --output FILE
Without --order, inspect at most 100 visible orders for procedure references.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.http_client import close_client
from tools.process_context import ReadContext, get_product_process, get_workflow_structure
from tools.container_inquiry import get_container_history, get_mfgorder_execution


async def verify(args):
    context = ReadContext(300)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "read_only": True}
    try:
        report["workflows"] = []
        data = await context.get("Workflows", params={"$top": 20})
        for row in data.get("value", []):
            result = json.loads(await get_workflow_structure(row["InstanceID"]))
            report["workflows"].append({"name": row["Name"], "revision": row.get("Revision"),
                "complete": result.get("complete"), "error": result.get("error"),
                "step_count": sum(len(n["steps"]) for n in result.get("workflows", {}).values()),
                "warnings": result.get("warnings", [])})
        report["products"] = []
        data = await context.get("Products", params={"$top": 20})
        for row in data.get("value", []):
            result = json.loads(await get_product_process(row["InstanceID"]))
            report["products"].append({"name": row["Name"], "complete": result.get("complete"),
                "error": result.get("error"), "spec_count": len(result.get("specs", {})),
                "operation_count": len(result.get("operations", {})), "warnings": result.get("warnings", [])})
        report["history"] = []
        for name in args.container:
            pages = [json.loads(await get_container_history(name, start_row=start, limit=2)) for start in (1, 3)]
            combined = json.loads(await get_container_history(name, limit=4))
            rows = [p.get("data", {}).get("Histories", []) for p in pages]
            union = combined.get("data", {}).get("Histories", [])
            # Empty results cannot validate pagination continuity.
            success = len(rows[0]) == 2 and bool(rows[1]) and rows[0] + rows[1] == union
            report["history"].append({"container": name, "page_counts": list(map(len, rows)),
                "combined_count": len(union), "nonempty_continuity_verified": success,
                "note": "三次查询非事务快照；并发新增历史可能改变排序。",
                "errors": [p.get("error") for p in pages if p.get("error")]})
        orders = list(args.order)
        if not orders:
            data = await context.get("MfgOrders", params={"$top": 100})
            report["order_sample_scan"] = {"returned_count": len(data.get("value", [])),
                "next_link_present": bool(data.get("@odata.nextLink")), "configured_candidates": []}
            for row in data.get("value", []):
                detail = await context.get("MfgOrders", row["InstanceID"])
                if detail.get("PreProductionProcedure") or detail.get("PostProductionProcedure"):
                    orders.append(row["Name"])
            report["order_sample_scan"]["configured_candidates"] = orders[:]
            if not orders and data.get("value"):
                orders.append(data["value"][0]["Name"])
        report["order_execution"] = []
        for name in orders[:10]:
            raw = await get_mfgorder_execution(name, info_depth=4)
            try:
                result = json.loads(raw)
            except ValueError:
                result = {"error": raw}
            infos = result.get("data", {}).get("MfgOrderInfos", [])
            report["order_execution"].append({"name": name, "status": result.get("status"),
                "nonempty_success_verified": bool(infos) and not result.get("error"),
                "complete": result.get("complete"), "error": result.get("error")})
    finally:
        await close_client()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", action="append", default=[])
    parser.add_argument("--order", action="append", default=[])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    result = asyncio.run(verify(args))
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))
