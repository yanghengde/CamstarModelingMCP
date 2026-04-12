"""
MCP 工具注册中心
==================
所有 MCP 工具模块在此聚合，导出统一的 mcp 实例。
未来新增模块只需在此文件 import 即可。
"""

import logging
from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

mcp = FastMCP(
    "CamstarModeling",
    instructions=(
        "MCP Server for Siemens Opcenter (Camstar) Modeling API. "
        "Provides tools to manage Specs, Operations, Workflows and more."
    ),
)

# -------------------------------------------------------
# 按模块导入工具 —— 工具通过 @mcp.tool 自动注册
# -------------------------------------------------------
from tools import specs                # Spec 实体
from tools import operations           # Operation 实体
# from tools import workflows          # 🆕 未来: Workflow 实体


def get_tool_func(name: str):
    """
    按函数名查找已注册的工具函数，供 Agent 直接调用。
    新增模块时，将对应 module 加入列表即可。
    """
    for module in [specs, operations]:  # 未来在此追加: workflows
        func = getattr(module, name, None)
        if func is not None:
            return func
    return None
