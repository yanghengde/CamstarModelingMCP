import os
import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, File, Query, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictBool

from config import CAMSTAR_USERNAME, ENABLE_PERFORMANCE_LOG, AGENT_ENGINE, UPLOAD_MAX_BYTES
from core.async_io import durable_io
from agent.memory import get_user_messages, get_sessions, create_session, set_active_session, get_active_session, user_memories
from agent.llm_client import chat_stream
from agent.experience import get_experience_status, list_candidates
from core.perf_logger import get_perf_logs, get_perf_log_groups
from web.chat_runs import RUNS, RunConflict, QueueFull

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    username: str
    session_id: str = None
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class ConfirmationSettings(BaseModel):
    auto_confirm: StrictBool


@router.put('/sessions/{username}/{session_id}/confirmation')
async def confirmation_settings(username: str, session_id: str, req: ConfirmationSettings):
    from agent.memory import save_session
    session = user_memories.get(username, {}).get('sessions', {}).get(session_id)
    if session is None:
        raise HTTPException(404, '对话不存在。')
    session['auto_confirm'] = req.auto_confirm
    await durable_io(save_session, username, session_id)
    return {'auto_confirm': req.auto_confirm}


@router.get("/")
@router.get("/records")
def index():
    """返回主页 HTML。"""
    html_path = os.path.join("static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@router.get("/config")
def config_endpoint():
    """返回前端所需的配置（如当前绑定用户名）。"""
    return {"username": CAMSTAR_USERNAME, "agent_engine": AGENT_ENGINE}


@router.get("/workflow/{username}/{session_id}")
async def workflow_endpoint(username: str, session_id: str):
    """Read checkpoint state without creating a session or executing tools."""
    if AGENT_ENGINE != "langgraph":
        return {"engine": AGENT_ENGINE, "status": "unavailable"}
    from agent.langgraph_runtime import get_workflow_status
    return await get_workflow_status(username, session_id)


@router.get("/sessions/{username}")
def sessions_endpoint(username: str):
    """获取用户所有会话"""
    statuses = RUNS.session_statuses(username)
    return {"sessions": [{**session, "run": statuses.get(session['id'])} for session in get_sessions(username)],
            "active_session": get_active_session(username)}


@router.post("/sessions/{username}/new")
def new_session_endpoint(username: str):
    """创建新会话"""
    session_id = create_session(username)
    return {"session_id": session_id}


@router.get("/history/{username}")
def history_endpoint(username: str, session_id: str = None):
    """获取指定用户的聊天历史。"""
    if session_id:
        set_active_session(username, session_id)
    messages = get_user_messages(username, session_id)
    return {"messages": messages, "run": RUNS.latest(username, session_id or get_active_session(username))}


def _chat_identity(req):
    session_id = req.session_id or get_active_session(req.username)
    if req.session_id and req.session_id not in user_memories.get(req.username, {}).get('sessions', {}):
        raise HTTPException(404, "对话不存在，请重新选择。")
    if not session_id:
        session_id = create_session(req.username)
    if not req.message.strip():
        raise HTTPException(422, "消息不能为空。")
    return session_id, len(get_user_messages(req.username, session_id))


async def start_chat_run(req):
    session_id, history_start = await durable_io(_chat_identity, req)
    try:
        return await RUNS.astart(req.username, session_id, req.message, req.request_id or uuid.uuid4().hex,
                                history_start, chat_stream, workflow_endpoint)
    except QueueFull as exc:
        raise HTTPException(429, str(exc), headers={'Retry-After': '2'}) from exc
    except RunConflict as exc:
        raise HTTPException(409, {"message": str(exc), "run": exc.run}) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/api/chat/runs', status_code=202)
async def create_chat_run(req: ChatRequest):
    """Accept once, run on the server, and return before execution finishes."""
    return await start_chat_run(req)


@router.get('/api/chat/runs/{run_id}')
def get_chat_run(run_id: str, username: str):
    run = RUNS.get(username, run_id)
    if not run:
        raise HTTPException(404, '任务不存在。')
    return run


@router.get('/api/chat/runs/{run_id}/events')
def chat_run_events(run_id: str, username: str, after: int = Query(default=0, ge=0)):
    if not RUNS.get(username, run_id):
        raise HTTPException(404, '任务不存在。')
    return StreamingResponse(RUNS.events(username, run_id, after), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.post('/api/chat/runs/{run_id}/cancel')
async def cancel_chat_run(run_id: str, username: str):
    run = await RUNS.acancel(username, run_id)
    if not run:
        raise HTTPException(404, '任务不存在。')
    return run


@router.post("/chat")
async def chat_endpoint(req: ChatRequest):
    """Backward-compatible SSE subscription to a server-owned background run."""
    run = await start_chat_run(req)
    return StreamingResponse(
        RUNS.events(req.username, run['id']), media_type="text/event-stream",
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@router.get("/logs")
def logs_page():
    """返回性能日志管理页面 HTML。"""
    html_path = os.path.join("static", "logs.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@router.get("/api/logs/data")
def api_logs_data():
    """获取统计与原始日志数据"""
    if not ENABLE_PERFORMANCE_LOG:
        return {"status": "disabled", "logs": []}
        
    logs = get_perf_logs(1000)
    return {"status": "enabled", "logs": logs}


@router.get('/api/logs/groups')
def api_log_groups(limit: int = Query(default=20, ge=1, le=100),
                   cursor: str | None = Query(default=None, max_length=512)):
    if not ENABLE_PERFORMANCE_LOG:
        return {'status': 'disabled', 'groups': [], 'next_cursor': None, 'has_more': False}
    try:
        return {'status': 'enabled', **get_perf_log_groups(limit, cursor)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/experience/status")
def experience_status_endpoint():
    """Return aggregate self-improvement queue status (read-only)."""
    return get_experience_status()


@router.get("/api/experience/candidates")
def experience_candidates_endpoint(
    status: Literal["pending", "approved", "rejected", "all"] = "pending",
    limit: int = Query(default=100, ge=1, le=500),
):
    """List sanitized experience candidates for human review (read-only)."""
    return {"candidates": list_candidates(status=status, limit=limit)}


UPLOAD_DIR = os.path.join("data", "uploads")


@router.post("/api/upload")
def upload_file(file: UploadFile = File(...)):
    """接收并暂存上传的文件，返回文件本地路径"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    
    unique_filename = f"{uuid.uuid4().hex}_{os.path.basename((file.filename or 'upload').replace(chr(92), '/'))}"
    file_path = os.path.join(UPLOAD_DIR, unique_filename)
    
    if file.size is not None and file.size > UPLOAD_MAX_BYTES:
        raise HTTPException(413, '上传文件超过大小限制。')
    try:
        total = 0
        with open(file_path, "wb") as buffer:
            while chunk := file.file.read(min(1024 * 1024, UPLOAD_MAX_BYTES + 1 - total)):
                total += len(chunk)
                if total > UPLOAD_MAX_BYTES:
                    raise HTTPException(413, '上传文件超过大小限制。')
                buffer.write(chunk)
    except BaseException:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise
        
    return {
        "success": True,
        "filename": file.filename,
        "file_path": file_path.replace("\\", "/")
    }
