import os
import json
import time
from datetime import datetime
from config import ENABLE_PERFORMANCE_LOG

LOGS_DIR = os.path.join("data", "logs")
PERF_LOG_FILE = os.path.join(LOGS_DIR, "performance.jsonl")

# 确保目录存在
if ENABLE_PERFORMANCE_LOG:
    os.makedirs(LOGS_DIR, exist_ok=True)

def record_perf(action: str, duration_sec: float, username: str = "Unknown", session_id: str = "Unknown", details: dict = None):
    """
    记录一段程序的耗时到独立的纯文本行文件 (JSONL) 中
    """
    if not ENABLE_PERFORMANCE_LOG:
        return

    now = datetime.now()
    start_dt = datetime.fromtimestamp(now.timestamp() - duration_sec)
    
    end_time_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    start_time_str = start_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    record = {
        "timestamp": end_time_str,  # 保持原用以排序兼容的值
        "start_time": start_time_str,
        "end_time": end_time_str,
        "action": action,
        "duration_ms": round(duration_sec * 1000, 2),
        "username": username,
        "session_id": session_id,
        "details": details or {}
    }
    
    # 追加到 JSONL 文件
    with open(PERF_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def get_perf_logs(limit: int = 500) -> list:
    """获取最后 N 条性能日志"""
    if not os.path.exists(PERF_LOG_FILE):
        return []
    
    logs = []
    with open(PERF_LOG_FILE, "r", encoding="utf-8") as f:
        # 简单读取并反序列化，如果文件特大会慢，但作为开发期调试够用
        lines = f.readlines()
        for line in lines[-limit:]:
            try:
                logs.append(json.loads(line.strip()))
            except:
                continue
    # 逆序返回，最新的在前面
    return list(reversed(logs))
