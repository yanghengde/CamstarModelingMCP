"""
Camstar Modeling MCP — 统一配置中心
====================================
所有环境变量在此集中读取，其他模块通过 from config import xxx 使用。
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Camstar API 配置
# ---------------------------------------------------------------------------
CAMSTAR_BASE_URL = os.getenv("CAMSTAR_BASE_URL", "http://localhost/Modeling")
CAMSTAR_USERNAME = os.getenv("CAMSTAR_USERNAME", "CamstarAdmin")
CAMSTAR_PASSWORD = os.getenv("CAMSTAR_PASSWORD", "Cam1star")
CAMSTAR_TIMEOUT = int(os.getenv("CAMSTAR_TIMEOUT", "30"))

# Maximum response characters before we trim to key fields only
MAX_RESPONSE_LENGTH = int(os.getenv("MAX_RESPONSE_LENGTH", "4000"))

# ---------------------------------------------------------------------------
# LLM 大模型配置（兼容 OpenAI 协议的任意模型）
# ---------------------------------------------------------------------------
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# ---------------------------------------------------------------------------
# Agent 配置
# ---------------------------------------------------------------------------
MAX_TOOL_LOOPS = int(os.getenv("MAX_TOOL_LOOPS", "15"))
MEMORY_FILE = os.path.join("data", "memory.json")
SESSIONS_DIR = os.path.join("data", "sessions")
