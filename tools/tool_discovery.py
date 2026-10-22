"""Request additional model tool definitions; never executes business tools."""
import json
from typing import Literal

from core.tool_catalog import TOOL_POLICIES
from tools import mcp


@mcp.tool
async def load_tool_group(
    group: Literal["workflow", "spec", "operation", "product", "mfgorder", "container", "reference", "query", "excel"],
    include_writes: bool = False,
) -> str:
    """Load a tool group on the NEXT model round when needed tools are missing.

    workflow=routes/steps; spec=specifications; operation=operations;
    product=products; mfgorder=orders; container=execution/status/history;
    reference=levels/numbering/lines/types; query=general Inquiry; excel=import.
    include_writes exposes write definitions only, never authorizes execution.
    """
    names = [name for name, policy in TOOL_POLICIES.items()
             if policy.group == group and policy.default_visible
             and (policy.effect == "read" or include_writes)]
    return json.dumps({"loaded": bool(names), "group": group,
                       "include_writes": include_writes, "tools": names}, ensure_ascii=False)
