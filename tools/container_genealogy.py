"""Bounded multi-hop traversal of explicitly typed container relationships."""
import asyncio
from collections import deque
import json
import re
import time
from tools import mcp
from tools.container_trace import get_container_trace
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_QUERY_BASE_URL


def _id(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{16}", value))


def _edges(section, row):
    pairs = []
    if section == "current_family":
        pairs = [("current_parent", row.get("ParentContainerId"), row.get("ContainerId")),
                 ("split_origin_reference", row.get("SplitFromId"), row.get("ContainerId"))]
    elif section in {"splits", "combines"}:
        pairs = [(section, row.get("FromContainerId"), row.get("ToContainerId"))]
    elif section == "parent_history":
        pairs = [("historical_parent", row.get("ParentContainerId"), row.get("ContainerId"))]
    return [{"type": kind, "from": source, "to": target, "evidence": row}
            for kind, source, target in pairs if _id(source) and _id(target)]


def _cycles(edges):
    adjacency = {}
    for edge in edges:
        adjacency.setdefault(edge["from"], set()).add(edge["to"])
    colors, cycles = {}, []
    def visit(node, trail):
        colors[node] = 1
        for target in sorted(adjacency.get(node, ())):
            if colors.get(target) == 1:
                cycles.append(trail[trail.index(target):] + [target])
            elif not colors.get(target):
                visit(target, trail + [target])
        colors[node] = 2
    for node in sorted(adjacency):
        if not colors.get(node):
            visit(node, [node])
    return cycles


@mcp.tool
async def get_container_genealogy(container_name: str, container_instance_id: str | None = None,
                                   max_depth: int = 2, max_nodes: int = 10, max_pages: int = 2) -> str:
    """Traverse explicit parent/child and split/combine container relationships, retaining rework evidence per node. BFS, deduplication and directed cycle reporting; 90s/200k output budget. Depth=0..3, nodes=1..20, pages=1..3 per section (20 rows/page). Does NOT traverse untyped HistoryCrossRef IDs. Check traversal_complete, boundaries and failures; never claim all historical ancestry."""
    result = {"complete": False, "traversal_complete": True, "nodes": {}, "edges": [], "boundaries": [], "failures": [],
              "note": "仅追踪明确的容器关系；未解码交叉引用不递归。历史关系跨时间合并可能形成合法环，不代表同一时刻关系异常。"}
    if not container_name.strip() or not 0 <= max_depth <= 3 or not 1 <= max_nodes <= 20 or not 1 <= max_pages <= 3:
        return raw_json({**result, "traversal_complete": False, "error": "名称必填，depth=0..3，nodes=1..20，pages=1..3"})
    deadline = time.monotonic() + 90
    queue = deque([(container_name.strip(), container_instance_id, 0)])
    seen, queued, edge_keys = set(), set(), set()

    async def bounded(awaitable):
        return await asyncio.wait_for(awaitable, timeout=max(0.001, deadline - time.monotonic()))

    while queue:
        name, identifier, depth = queue.popleft()
        if identifier in seen:
            continue
        if depth > max_depth or len(seen) >= max_nodes or time.monotonic() >= deadline:
            result["boundaries"].append({"container_id": identifier, "depth": depth, "reason": "depth/nodes/time budget"})
            continue
        try:
            if not name:
                if not _id(identifier):
                    raise ValueError("非法关系 ID")
                raw = await bounded(request("POST", "/api/AdHoc", base_url=CAMSTAR_QUERY_BASE_URL,
                    body={"queryText": f"SELECT ContainerId,ContainerName FROM Container WHERE ContainerId='{identifier}'"},
                    response_formatter=raw_json, retry_read_only=True))
                rows = json.loads(raw)
                if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("ContainerId") != identifier or not rows[0].get("ContainerName"):
                    raise ValueError("关系端点无法唯一解析为容器")
                name = rows[0]["ContainerName"]
            node = json.loads(await bounded(get_container_trace(name, identifier)))
            if node.get("error"):
                raise ValueError(node["error"])
            identifier = node["container"]["InstanceId"]
            if identifier in seen:
                continue
            seen.add(identifier)
            result["nodes"][identifier] = {"depth": depth, "trace": node}
            sections = node["sections"]
            for section, page in sections.items():
                rows = list(page.get("rows", []))
                pages_read = 1
                while page.get("has_more") and pages_read < max_pages:
                    next_page = json.loads(await bounded(get_container_trace(name, identifier, section, page["next_offset"], 20)))
                    page = next_page.get("sections", {}).get(section, {"error": next_page.get("error", "分页结果缺失")})
                    rows.extend(page.get("rows", []))
                    pages_read += 1
                    sections[section] = {**page, "rows": list(rows), "pages_read": pages_read}
                sections[section] = {**page, "rows": rows, "pages_read": pages_read}
                sections[section]["returned_count"] = len(rows)
                if page.get("error") or page.get("has_more") or not page.get("page_complete"):
                    result["boundaries"].append({"container_id": identifier, "section": section, "next_offset": page.get("next_offset"), "error": page.get("error")})
                for row in rows:
                    for edge in _edges(section, row):
                        key = raw_json(edge)
                        if key not in edge_keys:
                            edge_keys.add(key)
                            result["edges"].append(edge)
                        for target in (edge["from"], edge["to"]):
                            if target not in seen and target not in queued:
                                queued.add(target)
                                queue.append((None, target, depth + 1))
            node["requested_sections_complete"] = all(p.get("page_complete") and not p.get("has_more") for p in sections.values())
            if len(raw_json(result)) > 200000:
                result["nodes"].pop(identifier)
                result["edges"] = [e for e in result["edges"] if e["from"] in result["nodes"] and e["to"] in result["nodes"]]
                result["boundaries"].append({"container_id": identifier, "reason": "output budget; last node omitted"})
                break
        except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as exc:
            result["failures"].append({"container_id": identifier, "name": name, "error": str(exc) or "总时间预算已用尽"})
    result["traversal_complete"] = not result["boundaries"] and not result["failures"]
    result["cycles"] = _cycles(result["edges"])
    encoded = raw_json(result)
    if len(encoded) > 200000:
        return raw_json({"complete": False, "traversal_complete": False,
            "error": "结果超出 200k 字符预算，未将裁剪结果伪装为完整族谱；请降低 max_nodes/max_depth/max_pages，或按单容器分段查询。",
            "visited_node_count": len(result["nodes"]), "edge_count": len(result["edges"])})
    return encoded
