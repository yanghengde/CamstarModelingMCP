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


def get_user_messages(username: str) -> list:
    """获取指定用户的消息历史，不存在则初始化。"""
    if username not in user_memories:
        user_memories[username] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
    else:
        # 强制更新老用户的系统提示词，以便新策略立即生效
        if user_memories[username] and user_memories[username][0].get("role") == "system":
            user_memories[username][0]["content"] = SYSTEM_PROMPT
    return user_memories[username]


def init_memory():
    """启动时从磁盘恢复记忆。"""
    global user_memories
    user_memories = load_memory()
