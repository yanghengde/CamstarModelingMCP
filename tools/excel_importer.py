"""
Excel Importer Tools
====================
Provides tools to parse Excel schemas and import Workflows from Excel files.
"""

import asyncio
import os
import json
import pandas as pd
from typing import Any, Dict, List, Optional
from tools import mcp
from config import EXCEL_MAX_SHEETS, UPLOAD_MAX_BYTES
from tools.workflows import create_workflow, list_workflows, rebuild_workflow_route


def get_case_insensitive_key(data: Any, key_name: str) -> Optional[Any]:
    """
    Safely extracts a key from JSON string/dict/list case-insensitively,
    supporting OData 'value' arrays and text warning prefixes.
    """
    if isinstance(data, str):
        try:
            # Strip warning message prefix if present
            if "{" in data:
                data = data[data.find("{"):]
            data = json.loads(data)
        except Exception:
            return None
            
    if isinstance(data, dict):
        for k, v in data.items():
            if k.lower() == key_name.lower():
                return v
        # Also check inside "value" if OData wrapped
        if "value" in data and isinstance(data["value"], list) and len(data["value"]) > 0:
            return get_case_insensitive_key(data["value"][0], key_name)
    elif isinstance(data, list) and len(data) > 0:
        return get_case_insensitive_key(data[0], key_name)
        
    return None


def _read_schema(file_path: str) -> dict:
    # Import plans accept 200 rows. One extra row detects oversized sheets
    # without materializing the entire workbook just to count its rows.
    schema_info = {}
    if os.path.getsize(file_path) > UPLOAD_MAX_BYTES:
        raise ValueError('文件超过大小限制')
    with pd.ExcelFile(file_path) as xl:
        if len(xl.sheet_names) > EXCEL_MAX_SHEETS:
            raise ValueError(f'工作表数量超过上限 {EXCEL_MAX_SHEETS}')
        for sheet_name in xl.sheet_names:
            df = pd.read_excel(xl, sheet_name=sheet_name, nrows=201).fillna("")
            schema_info[sheet_name] = {
                "columns": list(df.columns),
                "row_count_estimate": len(df),
                "row_count_is_lower_bound": len(df) == 201,
                "sample_rows": df.head(3).to_dict(orient="records"),
            }
    return schema_info


@mcp.tool
async def parse_excel_schema(file_path: str) -> str:
    """
    Parse an Excel file's structure (sheets, columns, and sample rows)
    to help mapping columns to MES entity fields.
    Reads at most 201 data rows per sheet; row_count_is_lower_bound=true
    means the sheet has at least row_count_estimate rows, not an exact count.
    
    Required fields:
      - file_path: Absolute or relative path to the Excel file.
    """
    if not os.path.exists(file_path):
        return f"❌ File not found at path: {file_path}"
    
    try:
        schema_info = await asyncio.to_thread(_read_schema, file_path)
            
        return json.dumps({
            "success": True,
            "file_path": file_path,
            "sheets": schema_info
        }, ensure_ascii=False, indent=2)
        
    except Exception as e:
        return f"❌ Failed to parse Excel schema: {str(e)}"


@mcp.tool
async def import_workflows_from_excel(file_path: str, mapping_json: str) -> str:
    """Compatibility entry: prepare an immutable preview; does not write business data.

    Review the returned plan then call execute_workflow_import_plan with its ID
    and digest after confirmation. Invalid rows are never silently skipped.
    """
    from tools.import_plans import preview_workflow_import
    return await preview_workflow_import(file_path, mapping_json)
