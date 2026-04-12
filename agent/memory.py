"""
会话记忆管理
==============
按用户名隔离的持久化聊天记忆，存储于 JSON 文件。
"""

import os
import json

from config import MEMORY_FILE
from agent.prompts import SYSTEM_PROMPT

# 内存中的用户记忆字典
user_memories: dict = {}


def load_memory() -> dict:
    """从磁盘加载记忆文件。"""
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_memory():
    """将当前记忆持久化到磁盘。"""
    # 确保 data/ 目录存在
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(user_memories, f, ensure_ascii=False, indent=2)


def _migrate_if_needed(username: str):
    data = user_memories.get(username)
    if isinstance(data, list):
        user_memories[username] = {
            "sessions": {
                "default": {
                    "id": "default",
                    "title": "默认会话",
                    "messages": data
                }
            },
            "active_session": "default"
        }

def get_sessions(username: str) -> list:
    """获取指定用户的所有会话列表"""
    if username not in user_memories:
        return []
    _migrate_if_needed(username)
    sessions = user_memories[username].get("sessions", {})
    return [{"id": k, "title": v.get("title", "会话")} for k, v in sessions.items()]

import uuid

def create_session(username: str) -> str:
    """创建一个新会话"""
    if username not in user_memories:
        user_memories[username] = {"sessions": {}, "active_session": None}
    else:
        _migrate_if_needed(username)
    
    session_id = str(uuid.uuid4())
    user_memories[username]["sessions"][session_id] = {
        "id": session_id,
        "title": f"新对话 {len(user_memories[username]['sessions']) + 1}",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}]
    }
    user_memories[username]["active_session"] = session_id
    save_memory()
    return session_id

def set_active_session(username: str, session_id: str):
    if username in user_memories:
        _migrate_if_needed(username)
        if session_id in user_memories[username]["sessions"]:
            user_memories[username]["active_session"] = session_id
            save_memory()

def get_user_messages(username: str, session_id: str = None) -> list:
    """获取指定用户的消息历史，不存在则初始化。"""
    if username not in user_memories:
        create_session(username)
    else:
        _migrate_if_needed(username)
        
    user_data = user_memories[username]
    if not session_id or session_id not in user_data["sessions"]:
        session_id = user_data["active_session"]
        if not session_id or session_id not in user_data["sessions"]:
             # create one if missing
             session_id = create_session(username)
             
    messages = user_data["sessions"][session_id]["messages"]
    
    # 强制更新老用户的系统提示词，以便新策略立即生效
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = SYSTEM_PROMPT
        
    return messages


def init_memory():
    """启动时从磁盘恢复记忆。"""
    global user_memories
    user_memories = load_memory()
