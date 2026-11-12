"""
Camstar Modeling MCP — 统一配置中心
====================================
所有环境变量在此集中读取，其他模块通过 from config import xxx 使用。
"""

import os
from urllib.parse import urlsplit, urlunsplit
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Camstar API 配置
# ---------------------------------------------------------------------------
CAMSTAR_BASE_URL = os.getenv("CAMSTAR_BASE_URL", "http://localhost/Modeling")


def _derive_sibling_url(modeling_url: str, service_name: str) -> str:
    """Derive a sibling Camstar API URL from a Modeling API URL."""
    parsed = urlsplit(modeling_url.rstrip("/"))
    path_parts = parsed.path.rstrip("/").split("/")
    if path_parts[-1].casefold() == "modeling":
        path_parts[-1] = service_name
    else:
        path_parts.append(service_name)
    return urlunsplit((parsed.scheme, parsed.netloc, "/".join(path_parts), "", ""))


def _derive_shopfloor_url(modeling_url: str) -> str:
    """Derive the sibling Shopfloor API URL from a Modeling API URL."""
    return _derive_sibling_url(modeling_url, "Shopfloor")


CAMSTAR_SHOPFLOOR_BASE_URL = os.getenv(
    "CAMSTAR_SHOPFLOOR_BASE_URL",
    _derive_shopfloor_url(CAMSTAR_BASE_URL),
)
CAMSTAR_QUERY_BASE_URL = os.getenv(
    "CAMSTAR_QUERY_BASE_URL",
    _derive_sibling_url(CAMSTAR_BASE_URL, "Query"),
)
CAMSTAR_USERNAME = os.getenv("CAMSTAR_USERNAME", "CamstarAdmin")
CAMSTAR_PASSWORD = os.getenv("CAMSTAR_PASSWORD", "Cam1star")
CAMSTAR_TIMEOUT = int(os.getenv("CAMSTAR_TIMEOUT", "30"))
CAMSTAR_TRUST_ENV = os.getenv("CAMSTAR_TRUST_ENV", "True").lower() in ("true", "1", "yes")

# Maximum response characters before we trim to key fields only
MAX_RESPONSE_LENGTH = int(os.getenv("MAX_RESPONSE_LENGTH", "4000"))

# ---------------------------------------------------------------------------
# LLM 大模型配置（兼容 OpenAI 协议的任意模型）
# ---------------------------------------------------------------------------
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "45"))
MAX_READ_FAILURES = max(1, int(os.getenv("MAX_READ_FAILURES", "3")))
READ_QUERY_CONCURRENCY = max(1, min(4, int(os.getenv('READ_QUERY_CONCURRENCY', '4'))))
AGENT_PROJECT_DISCOVERY = os.getenv('AGENT_PROJECT_DISCOVERY', 'true').lower() in ('true', '1', 'yes')
AGENT_CAPABILITY_FAST_REPLY = os.getenv('AGENT_CAPABILITY_FAST_REPLY', 'true').lower() in ('true', '1', 'yes')

# ---------------------------------------------------------------------------
# Agent 配置
# ---------------------------------------------------------------------------
MAX_TOOL_LOOPS = int(os.getenv("MAX_TOOL_LOOPS", "15"))
# A graph turn traverses multiple nodes (agent, policy, and one node per tool),
# so this must be higher than MAX_TOOL_LOOPS. The agent loop guard remains the
# primary protection against runaway reasoning.
LANGGRAPH_RECURSION_LIMIT = max(
    25,
    int(os.getenv("LANGGRAPH_RECURSION_LIMIT", str(MAX_TOOL_LOOPS * 6 + 10))),
)
MEMORY_FILE = os.path.join("data", "memory.json")
SESSIONS_DIR = os.path.join("data", "sessions")

# Agent execution engine.  Keep "legacy" as a rollback option during rollout.
AGENT_ENGINE = os.getenv("AGENT_ENGINE", "langgraph").strip().lower()
LANGGRAPH_CHECKPOINT_DB = os.getenv(
    "LANGGRAPH_CHECKPOINT_DB",
    os.path.join("data", "langgraph_checkpoints.sqlite"),
)
EXPERIENCE_DB = os.getenv(
    "EXPERIENCE_DB",
    os.path.join("data", "agent_experience.sqlite"),
)
# Bump when MES contracts/business semantics change; old learned rules require review.
MEMORY_CONTRACT_VERSION = os.getenv("MEMORY_CONTRACT_VERSION", "1").strip() or "1"
CHAT_RUN_DB = os.getenv("CHAT_RUN_DB", os.path.join("data", "chat_runs.sqlite"))

# ---------------------------------------------------------------------------
# MCP Streamable HTTP transport
# ---------------------------------------------------------------------------
# The browser's /chat SSE route is separate from the MCP protocol endpoint.
# External MCP access is deliberately opt-in because most tools can mutate MES.
ENABLE_MCP_HTTP = os.getenv("ENABLE_MCP_HTTP", "False").lower() in (
    "true",
    "1",
    "yes",
)
MCP_HTTP_PATH = os.getenv("MCP_HTTP_PATH", "/mcp").strip() or "/mcp"
if not MCP_HTTP_PATH.startswith("/"):
    MCP_HTTP_PATH = f"/{MCP_HTTP_PATH}"
MCP_HTTP_PATH = MCP_HTTP_PATH.rstrip("/") or "/mcp"
MCP_API_KEY = os.getenv("MCP_API_KEY", "").strip()


def _csv_env(name: str, default: str) -> list[str]:
    """Read a comma-separated environment variable into a clean list."""
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


# FastMCP host/origin protection prevents DNS-rebinding attacks. Add the actual
# server address explicitly when exposing the endpoint on a LAN.
MCP_ALLOWED_HOSTS = _csv_env(
    "MCP_ALLOWED_HOSTS",
    "localhost:*,127.0.0.1:*,[::1]:*",
)
MCP_ALLOWED_ORIGINS = _csv_env("MCP_ALLOWED_ORIGINS", "")

# 确认阈值：创建保留任务累计上限，普通修改按本次待执行范围判断；高风险操作另行确认
SAFE_CREATE_THRESHOLD = int(os.getenv("SAFE_CREATE_THRESHOLD", "20"))
SAFE_UPDATE_THRESHOLD = int(os.getenv("SAFE_UPDATE_THRESHOLD", "3"))
SAFE_DELETE_THRESHOLD = int(os.getenv("SAFE_DELETE_THRESHOLD", "0")) # Default 0 means >= 1 triggers it

# ---------------------------------------------------------------------------
# 日志与性能监控开关
# ---------------------------------------------------------------------------
ENABLE_PERFORMANCE_LOG = os.getenv("ENABLE_PERFORMANCE_LOG", "True").lower() in ("true", "1", "yes")

# Single-worker admission and storage budgets. Zero retention disables pruning.
CHAT_MAX_RUNNING = max(1, int(os.getenv("CHAT_MAX_RUNNING", "4")))
CHAT_MAX_RUNNING_PER_USER = max(1, int(os.getenv("CHAT_MAX_RUNNING_PER_USER", "2")))
CHAT_MAX_QUEUED = max(0, int(os.getenv("CHAT_MAX_QUEUED", "32")))
CHAT_MAX_QUEUED_PER_USER = max(0, int(os.getenv("CHAT_MAX_QUEUED_PER_USER", "8")))
CHAT_QUEUE_TIMEOUT = max(1., float(os.getenv("CHAT_QUEUE_TIMEOUT", "120")))
ENABLE_CHAT_CHARTS = os.getenv('ENABLE_CHAT_CHARTS', 'True').lower() in ('true', '1', 'yes')
CHAT_CHART_DB = os.getenv('CHAT_CHART_DB', os.path.join('data', 'chat_charts.sqlite3'))
AGENT_HISTORY_TOKEN_BUDGET = max(0, int(os.getenv("AGENT_HISTORY_TOKEN_BUDGET", "0")))
UPLOAD_MAX_BYTES = max(1, int(os.getenv("UPLOAD_MAX_BYTES", "20971520")))
EXCEL_MAX_SHEETS = max(1, int(os.getenv("EXCEL_MAX_SHEETS", "20")))
PERF_RETENTION_DAYS = max(0, int(os.getenv("PERF_RETENTION_DAYS", "90")))
CHAT_EVENT_RETENTION_DAYS = max(0, int(os.getenv("CHAT_EVENT_RETENTION_DAYS", "30")))
UPLOAD_RETENTION_DAYS = max(0, int(os.getenv("UPLOAD_RETENTION_DAYS", "7")))
