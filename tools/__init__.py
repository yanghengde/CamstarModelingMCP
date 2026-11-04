"""
MCP 工具注册中心
==================
所有 MCP 工具模块在此聚合，导出统一的 mcp 实例。
未来新增模块只需在此文件 import 即可。
"""

import logging
from fastmcp import FastMCP
from core.tool_catalog import TOOL_POLICIES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

mcp = FastMCP(
    "CamstarModeling",
    instructions=(
        "MCP Server for Siemens Opcenter (Camstar) Modeling, Shopfloor, and "
        "Query APIs. Provides tools to manage modeling entities, execute "
        "manufacturing Container transactions, and run read-only queries."
    ),
)

# -------------------------------------------------------
# 按模块导入工具 —— 工具通过 @mcp.tool 自动注册
# -------------------------------------------------------
from tools import specs                # Spec 实体
from tools import operations           # Operation 实体
from tools import workflows            # Workflow 实体
from tools import workflow_steps        # Workflow 步骤与路径只读查询
from tools import products             # Product 实体
from tools import mfgorders            # MfgOrder 实体
from tools import container_start       # Shopfloor Container Start 事务
from tools import container_moves       # Shopfloor Move/MoveIn/MoveOut 事务
from tools import container_quality     # Shopfloor ContainerDefect/Rework 事务
from tools import quality, quality_plans, quality_inspection, quality_definitions, quality_lots, quality_lot_execution, quality_history, quality_sampling_references, quality_lot_maintenance, quality_configuration, quality_current_sampling, quality_ncr_escalation, quality_ncr_closure, quality_lot_rework, quality_quantity_loss
from tools import numbering_rules       # NumberingRule 查询
from tools import uoms                  # UOM 计量单位查询与维护
from tools import container_levels      # ContainerLevel 完整建模维护与字段契约/选择值
from tools import queries               # Query API 查询
from tools import mfglines             # MfgLine 实体
from tools import producttypes         # ProductType 实体
from tools import excel_importer       # Excel 导入工具
from tools import modeling_details, container_inquiry, tool_discovery
from tools import container_genealogy, container_trace, process_context, import_plans, order_progress
from tools import modeling_workspace
from tools import mfgorder_tasks
from tools import mfgorder_task_preview
from tools import mfgorder_task_history, mfgorder_task_plans
from tools import mfgorder_task_recovery
from tools import spec_documents
from tools import document_files
from tools import container_transfer_reconciliation
from tools import container_transfer_plans
from tools import container_transfers
from tools import collection_reconciliation
from tools import collection_submission
from tools import collection_recovery
from tools import collection_history
from tools import collection_validation
from tools import collection_groups, collection_group_history, collection_verification
from tools import collection_modeling
from tools import component_issue
from tools import component_issue_plans
from tools import component_issue_bulk
from tools import component_issue_reasons
from tools import production_readiness
from tools import data_collection
from tools import boms
from tools import resources
from tools import production_basics
from tools import system_info          # MCP 服务诊断（无外部副作用）
from tools import quality_spc
from tools import quality_spc_annotations
from tools import quality_spc_violations
from tools import quality_event_triage
from tools import quality_capa_approval_revision
from tools import quality_approval_assignment
from tools import quality_approval_history
from tools import quality_approval_decisions
from tools import quality_approval_delegation
from tools import quality_approval_actions
from tools import quality_approvals
from tools import quality_event_lifecycle
from tools import quality_event_creation
from tools import quality_event_close_plan
from tools import quality_event_closure
from tools import quality_event_update
from tools import quality_event_submission
from tools import quality_event_drafts
from tools import quality_event_data
from tools import quality_event_logs


def get_tool_func(name: str):
    """
    按函数名查找已注册的工具函数，供 Agent 直接调用。
    新增模块时，将对应 module 加入列表即可。
    """
    if name not in TOOL_POLICIES:
        return None
    for module in [quality_event_logs, quality_event_data, quality_event_drafts, quality_event_submission, quality_capa_approval_revision, quality_approval_assignment, quality_approval_history, quality_approval_decisions, quality_approval_delegation, quality_approval_actions, quality_approvals, quality_event_lifecycle, quality_event_creation, quality_event_close_plan, quality_event_closure, quality_event_update, quality_event_triage, quality_spc_violations, quality_spc_annotations, quality_spc, quality, quality_plans, quality_inspection, quality_definitions, quality_lots, quality_lot_execution, quality_history, quality_sampling_references, quality_lot_maintenance, quality_configuration, quality_current_sampling, quality_ncr_escalation, quality_ncr_closure, quality_lot_rework, quality_quantity_loss, collection_groups, collection_group_history, collection_verification, collection_modeling, component_issue_bulk, component_issue_reasons, component_issue_plans, document_files, collection_recovery, spec_documents, mfgorder_task_recovery, mfgorder_task_history, mfgorder_task_plans, mfgorder_task_preview, mfgorder_tasks, container_transfer_reconciliation, container_transfer_plans, container_transfers, collection_reconciliation, collection_submission, collection_history, collection_validation, component_issue, production_readiness, data_collection, boms, resources, production_basics, modeling_workspace, container_genealogy, container_trace,import_plans, order_progress, process_context, modeling_details, container_inquiry, tool_discovery, specs, operations, workflows, workflow_steps, products, mfgorders,
                   container_start, container_moves, container_quality,
                   numbering_rules, uoms,
                   container_levels, queries,
                   mfglines, producttypes, excel_importer, system_info]:
        func = getattr(module, name, None)
        if func is not None:
            return func
    return None
