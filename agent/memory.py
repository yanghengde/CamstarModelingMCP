"""
会话记忆管理
==============
按用户名隔离的持久化聊天记忆，存储于 JSON 文件。
"""

import os
import glob
import json
import uuid
import time
import threading
from pathlib import Path

_STORE_LOCK = threading.RLock()

from config import MEMORY_FILE, SESSIONS_DIR
from agent.prompts import SYSTEM_PROMPT

# 内存中的状态字典
user_memories: dict = {}


class LazySession(dict):
    """Metadata-only entry until a caller actually requests its messages."""
    def __init__(self, summary, path):
        super().__init__(summary)
        self.path = path

    def hydrate(self):
        with _STORE_LOCK:
            if not dict.__contains__(self, 'messages'):
                with open(self.path, encoding='utf-8') as stream:
                    full = json.load(stream)
                # Preserve an explicitly changed metadata setting before save.
                full.update(dict(self))
                self.update(full)
        return self

    def __getitem__(self, key):
        if key == 'messages':
            self.hydrate()
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key == 'messages':
            self.hydrate()
        return super().get(key, default)


def _atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
        # Windows readers/scanners can briefly hold the destination open.
        # Retry only this local atomic replacement, never a business operation.
        for attempt, delay in enumerate((.01, .03, .06, 0)):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                if os.name != 'nt' or getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 3:
                    raise
                time.sleep(delay)
    finally:
        temporary.unlink(missing_ok=True)


def _summary(data):
    return {**{k: data[k] for k in ('id', 'title', 'updated_at', 'auto_confirm') if k in data},
            'has_messages': any(m.get('role') in ('user', 'assistant') and bool(m.get('content'))
                                for m in data.get('messages', []))}


def _summary_path(path):
    return Path(path).parent / '.index' / Path(path).name


def _write_summary(path, data):
    stat = os.stat(path)
    _atomic_json(_summary_path(path), {'mtime_ns': stat.st_mtime_ns, 'size': stat.st_size, 'summary': _summary(data)})


def get_user_dir(username: str) -> str:
    return os.path.join(SESSIONS_DIR, username)


def _save_metadata(username: str):
    """保存用户的元数据（如活跃 session 配置）"""
    d = get_user_dir(username)
    os.makedirs(d, exist_ok=True)
    metadata_path = os.path.join(d, "metadata.json")
    data_to_save = {
        "active_session": user_memories[username].get("active_session")
    }
    with _STORE_LOCK:
        _atomic_json(metadata_path, data_to_save)


def save_session(username: str, session_id: str):
    """将指定用户的单个会话数据持久化"""
    if username in user_memories and session_id in user_memories[username].get("sessions", {}):
        d = get_user_dir(username)
        os.makedirs(d, exist_ok=True)
        session_data = user_memories[username]["sessions"][session_id]
        with _STORE_LOCK:
            if isinstance(session_data, LazySession):
                session_data.hydrate()
            session_data["updated_at"] = time.time()
            file_path = os.path.join(d, f"{session_id}.json")
            _atomic_json(file_path, session_data)
            _write_summary(file_path, session_data)


def save_memory():
    """兼容旧版接口的全量保存机制（建议在性能敏感区直接调用 save_session）"""
    for username, udata in user_memories.items():
        _save_metadata(username)
        for sid in udata.get("sessions", {}):
            save_session(username, sid)


def save_user_session(username: str, session_id: str):
    """增量保存单个会话及元数据，极大提升性能"""
    _save_metadata(username)
    save_session(username, session_id)



def load_memory() -> dict:
    """初始化加载：从分离的 session 文件夹加载，如果包含旧 memory.json 则尝试热迁移组合"""
    mem = {}
    
    # 1. 挂载旧版的全局 memory.json 进行兼容
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                old_mem = json.load(f)
                for uname, udata in old_mem.items():
                    if isinstance(udata, list):
                        mem[uname] = {
                            "active_session": "default",
                            "sessions": {"default": {"id": "default", "title": "默认会话", "messages": udata}}
                        }
                    else:
                        mem[uname] = udata
        except Exception:
            pass

    # 2. 如果存在分离的文件目录，其数据优先级更高以覆盖基座
    if os.path.exists(SESSIONS_DIR):
        for user_folder in os.listdir(SESSIONS_DIR):
            uname = user_folder
            user_path = os.path.join(SESSIONS_DIR, user_folder)
            if not os.path.isdir(user_path):
                continue
                
            if uname not in mem:
                mem[uname] = {"active_session": None, "sessions": {}}
                
            meta_path = os.path.join(user_path, "metadata.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as fm:
                        mem[uname]["active_session"] = json.load(fm).get("active_session")
                except Exception:
                    pass
            
            for s_file in glob.glob(os.path.join(user_path, "*.json")):
                if os.path.basename(s_file) == "metadata.json":
                    continue
                try:
                    stat = os.stat(s_file)
                    cached = None
                    try:
                        with _summary_path(s_file).open(encoding='utf-8') as stream:
                            cached = json.load(stream)
                    except (OSError, ValueError):
                        pass
                    if (isinstance(cached, dict) and isinstance(cached.get('summary'), dict)
                            and cached['summary'].get('id') and 'has_messages' in cached['summary']
                            and cached.get('mtime_ns') == stat.st_mtime_ns and cached.get('size') == stat.st_size):
                        s_data = LazySession(cached['summary'], s_file)
                    else:
                        # One-time metadata migration; never rewrite the history.
                        with open(s_file, encoding='utf-8') as stream:
                            full = json.load(stream)
                        full.setdefault('updated_at', stat.st_mtime)
                        _write_summary(s_file, full)
                        s_data = LazySession(_summary(full), s_file)
                    sid = s_data.get('id')
                    if sid:
                        mem[uname]['sessions'][sid] = s_data
                except Exception:
                    pass

    return mem


def _migrate_if_needed(username: str):
    # 此函数用作旧版老逻辑结构的容错防御
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
        _save_metadata(username)
        save_session(username, "default")


def get_sessions(username: str) -> list:
    """获取指定用户的所有会话列表"""
    if username not in user_memories:
        return []
    _migrate_if_needed(username)
    sessions = user_memories[username].get("sessions", {})
    # 按时间或自然序号直接吐出列表信息
    return sorted(
        [{"id": k, "title": v.get("title", "会话"), "updated_at": v.get("updated_at", 0),
          "auto_confirm": v.get("auto_confirm") is True,
          "has_messages": v['has_messages'] if isinstance(v, LazySession) and 'messages' not in v else any(
              message.get("role") in ("user", "assistant") and bool(message.get("content"))
              for message in v.get("messages", [])
          )}
         for k, v in sessions.items()],
        key=lambda item: (item["updated_at"], item["id"]),
    )


def session_auto_confirm(username: str, session_id: str) -> bool:
    return user_memories.get(username, {}).get("sessions", {}).get(session_id, {}).get("auto_confirm") is True


AUTO_CONFIRM_INSTRUCTION = (
    "用户已开启本次对话自动确认：需求明确且参数齐全时直接调用工具推进操作，"
    "不要重复询问是否执行。仍需完成计划预检、参数校验和结果核验；"
    "遇到必须由用户选择的选项、歧义或缺失信息时询问用户，不得代选或编造。"
    "用户明确要求先确认时遵从该要求；业务签名、身份验证和工具自身校验照常执行。"
)


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
    
    save_session(username, session_id)
    _save_metadata(username)
    return session_id

def update_session_title(username: str, session_id: str, title: str):
    """更新会话的名称并持久化"""
    if username in user_memories:
        if session_id in user_memories[username].get("sessions", {}):
            user_memories[username]["sessions"][session_id]["title"] = title
            save_session(username, session_id)


def set_active_session(username: str, session_id: str):
    if username in user_memories:
        _migrate_if_needed(username)
        if session_id in user_memories[username]["sessions"]:
            user_memories[username]["active_session"] = session_id
            _save_metadata(username)


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
             session_id = create_session(username)
             
    messages = user_data["sessions"][session_id]["messages"]
    
    # 强制更新老用户的系统提示词，以应用最新的Agent定位指令
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = SYSTEM_PROMPT
        
    return messages


def init_memory():
    """系统启动时的统一外层装载点。"""
    loaded = load_memory()
    # Keep references imported by runtimes valid; startup is not a content update.
    user_memories.clear()
    user_memories.update(loaded)


def get_active_session(username: str) -> str | None:
    data = user_memories.get(username, {})
    active = data.get("active_session")
    return active if active in data.get("sessions", {}) else None
