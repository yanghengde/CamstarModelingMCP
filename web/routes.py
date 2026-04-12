"""
API 路由
=========
定义所有 FastAPI 路由端点。
"""

import os
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from config import CAMSTAR_USERNAME
from agent.memory import get_user_messages
from agent.llm_client import chat_stream

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    username: str


@router.get("/")
def index():
    """返回主页 HTML。"""
    html_path = os.path.join("static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@router.get("/config")
def config_endpoint():
    """返回前端所需的配置（如当前绑定用户名）。"""
    return {"username": CAMSTAR_USERNAME}


@router.get("/history/{username}")
def history_endpoint(username: str):
    """获取指定用户的聊天历史。"""
    return {"messages": get_user_messages(username)}


@router.post("/chat")
async def chat_endpoint(req: ChatRequest):
    """流式聊天接口 (SSE)。"""
    return StreamingResponse(
        chat_stream(req.username, req.message),
        media_type="text/event-stream"
    )
