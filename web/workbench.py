"""Structured UI endpoints: reads and local previews only, never business writes."""
import asyncio
import json
from pathlib import Path
from typing import Literal
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import pandas as pd

from tools.modeling_workspace import check_modeling_integrity, compare_modeling_versions, preview_workflow_copy
from tools.order_progress import get_mfgorder_progress
from tools.container_genealogy import get_container_genealogy
from tools.container_inquiry import get_container_history
from tools.import_plans import preview_workflow_import, get_workflow_import_plan, reconcile_workflow_import_plan

router = APIRouter()
UPLOADS = Path(__file__).resolve().parents[1] / "data" / "workbench_uploads"


class Request(BaseModel):
    action: Literal["inspect", "compare", "copy", "order", "genealogy", "history", "sheets", "preview", "plan", "reconcile"]
    kind: Literal["Workflows", "Products"] = "Workflows"
    name: str = Field(default="", max_length=200)
    revision: str = Field(default="", max_length=100)
    target_name: str = Field(default="", max_length=200)
    target_revision: str = Field(default="", max_length=100)
    offset: int = Field(default=0, ge=0, le=100000)
    depth: int = Field(default=2, ge=0, le=3)
    file_id: str = ""
    sheet: str = ""
    header_row: int = Field(default=0, ge=0, le=30)
    fields: dict[str, str] = Field(default_factory=dict)
    plan_id: str = Field(default="", max_length=100)


def upload_path(file_id):
    import re
    if not re.fullmatch(r"[a-f0-9]{32}\.(xlsx|xls|csv)", file_id):
        raise ValueError("文件标识无效，请重新上传")
    path = UPLOADS / file_id
    if not path.is_file():
        raise ValueError("上传文件不存在")
    return path


def read_sheet(file_id, sheet, header):
    path = upload_path(file_id)
    if path.suffix == ".csv":
        sheets = ["CSV"]
        frame = pd.read_csv(path, header=header, dtype=str, keep_default_na=False, nrows=201)
    else:
        with pd.ExcelFile(path) as book:
            sheets = book.sheet_names
            frame = pd.read_excel(book, sheet_name=sheet or sheets[0], header=header,
                                  dtype=str, keep_default_na=False, nrows=201)
    return {"sheets": sheets, "sheet": sheet or sheets[0], "columns": list(map(str, frame.columns)),
            "sample": frame.head(8).to_dict(orient="records"), "rows_read": len(frame),
            "over_limit": len(frame) > 200, "note": "样本仅展示前 8 行；生成计划时校验全部数据，最多 200 行。"}


@router.get("/modeling", response_class=HTMLResponse)
async def page():
    return (Path(__file__).resolve().parents[1] / "static" / "modeling.html").read_text(encoding="utf-8")


@router.post("/api/workbench/upload")
async def upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xls", ".csv"}:
        raise HTTPException(400, "仅支持 Excel / CSV")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex + suffix
    target = UPLOADS / identifier
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 10 * 1024 * 1024:
                    raise HTTPException(413, "文件不能超过 10 MB")
                output.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return {"file_id": identifier}


@router.post("/api/workbench")
async def workbench(req: Request):
    try:
        if req.action == "sheets":
            return await asyncio.to_thread(read_sheet, req.file_id, req.sheet, req.header_row)
        if req.action == "inspect":
            task = check_modeling_integrity(req.kind, req.name, req.revision)
        elif req.action == "compare":
            task = compare_modeling_versions(req.kind, req.name, req.revision, req.target_name, req.target_revision)
        elif req.action == "copy":
            if req.kind != "Workflows":
                raise ValueError("此操作仅复制线性路线模板，不复制 Product 对象")
            task = preview_workflow_copy(req.name, req.revision, req.target_name, req.target_revision)
        elif req.action == "order":
            task = get_mfgorder_progress(req.name, offset=req.offset, limit=10)
        elif req.action == "genealogy":
            task = get_container_genealogy(req.name, max_depth=req.depth)
        elif req.action == "history":
            task = get_container_history(req.name, start_row=req.offset + 1, limit=20, history_source="mainline")
        elif req.action == "preview":
            task = preview_workflow_import(str(upload_path(req.file_id)), json.dumps({
                "sheet_name": req.sheet, "header_row": req.header_row, "fields": req.fields}))
        elif req.action == "plan":
            task = get_workflow_import_plan(req.plan_id)
        else:
            task = reconcile_workflow_import_plan(req.plan_id)
        return json.loads(await asyncio.wait_for(task, timeout=150))
    except asyncio.TimeoutError:
        raise HTTPException(504, "读取或预览超时，请缩小范围后重试") from None
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from None
