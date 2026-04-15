# Camstar Modeling MCP Agent

> 基于 **FastMCP + FastAPI + LLM** 的 Siemens Opcenter (Camstar) MES 建模 AI 助手。  
> 将 Camstar Modeling REST API 封装为 MCP 工具，通过自然语言对话操控 Spec、Operation、Workflow 等建模实体。

---

## ✨ 功能特性

| 功能 | 说明 |
|---|---|
| **自然语言建模** | 用对话方式创建/查询/更新/删除 Spec、Operation、Workflow |
| **流式实时输出** | 基于 SSE 的逐字流式渲染，消除 LLM 阻塞等待 |
| **安全卡点防护** | 批量操作阈值拦截 + OTP 一次性密码双重验证，防止误操作 |
| **多用户多会话** | 按用户名隔离的持久化历史记忆，支持会话切换 |
| **性能监控** | JSONL 追踪每次 LLM 推理与工具执行耗时，内置可视化 Dashboard |
| **兼容任意 LLM** | 支持 DeepSeek / 通义千问 / 智谱 / Ollama / OpenAI 等兼容 OpenAI 协议的模型 |

---

## 🗂️ 目录结构

```
CamstarModelingMCP/
├── main.py              # 启动入口（uvicorn）
├── config.py            # 统一配置中心（读取 .env）
├── requirements.txt     # Python 依赖
├── .env.example         # 环境变量模板
│
├── core/                # 核心基础设施
│   ├── auth.py          # Bearer Token 生成
│   ├── http_client.py   # 异步 HTTP 请求分发器
│   ├── response.py      # 智能响应裁剪
│   └── perf_logger.py   # 性能日志（JSONL）
│
├── tools/               # MCP 工具层
│   ├── __init__.py      # FastMCP 实例 + 工具注册中心
│   ├── specs.py         # Spec 实体（11 个工具）
│   ├── operations.py    # Operation 实体（9 个工具）
│   ├── workflows.py     # Workflow 实体
│   └── security.py      # OTP 验证守卫
│
├── agent/               # 智能体层
│   ├── llm_client.py    # LLM 调用 + 工具编排 + 安全拦截
│   ├── memory.py        # 多用户多会话记忆管理
│   ├── prompts.py       # System Prompt
│   └── cli.py           # 命令行调试界面
│
├── web/                 # Web API 层
│   ├── app.py           # FastAPI 应用工厂
│   └── routes.py        # 路由定义
│
├── static/              # 前端静态资源
│   ├── index.html       # 聊天主界面
│   └── logs.html        # 性能日志 Dashboard
│
├── data/                # 运行时数据（自动创建）
│   ├── sessions/        # 用户会话 JSON 文件
│   └── logs/            # performance.jsonl
│
├── Swagger/             # Camstar API 原始 Swagger 文档
└── docs/
    └── DESIGN.md        # 完整程序设计文档
```

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 Camstar 地址、账号密码和 LLM API Key
```

关键配置项：

```ini
CAMSTAR_BASE_URL=https://your-camstar-host/Modeling
CAMSTAR_USERNAME=CamstarAdmin
CAMSTAR_PASSWORD=your-password

LLM_API_KEY=sk-your-api-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

### 3. 启动服务

```bash
python main.py
```

服务启动后访问：

- 💬 **聊天界面**：http://127.0.0.1:8000/
- 📊 **性能日志**：http://127.0.0.1:8000/logs

---

## 🛠️ MCP 工具一览

### Spec 实体（`/api/Specs`）

| 工具 | 方法 | 说明 |
|---|---|---|
| `list_specs` | GET | OData 查询 Spec 列表 |
| `get_spec` | GET | 按 key 获取单条 Spec |
| `get_spec_by_odata_key` | GET | OData 键语法获取 |
| `create_spec` | POST | 创建新 Spec |
| `update_spec` | PUT | 全量更新 Spec |
| `update_spec_by_odata_key` | PUT | OData 键全量更新 |
| `patch_spec` | PATCH | 部分字段更新 |
| `delete_spec` | DELETE | 删除 Spec |
| `delete_spec_by_odata_key` | DELETE | OData 键删除 |
| `get_specs_count` | GET | 获取 Spec 总数 |
| `request_selection_values` | POST | 获取 LOV 值 |

### Operation 实体（`/api/Operations`）

| 工具 | 方法 | 说明 |
|---|---|---|
| `list_operations` | GET | OData 查询 Operation 列表 |
| `get_operation` | GET | 按 key 获取单条 |
| `create_operation` | POST | 创建新 Operation |
| `update_operation` | PUT | 全量更新 |
| `delete_operation` | DELETE | 删除 Operation |
| `get_operations_count` | GET | 获取总数 |
| *(+ OData 变体及 RequestSelectionValues)* | — | — |

---

## 🛡️ 安全机制

### 批量操作阈值拦截

在 `.env` 中可配置触发强制确认的阈值：

```ini
SAFE_CREATE_THRESHOLD=20   # 批量创建 > 20 条时暂停并请求确认
SAFE_UPDATE_THRESHOLD=3    # 批量更新 > 3 条时暂停
SAFE_DELETE_THRESHOLD=0    # 任意删除均需先确认（0 = >=1 拦截）
```

### OTP 单次密码验证

删除等高风险操作额外受到 OTP 守卫保护：LLM 首次尝试删除时系统自动生成一个内部 OTP，要求用户明确回复"确认删除"后，由 LLM 持 OTP 二次发起才能真正执行删除，且 OTP 一次性消耗防止重放攻击。

---

## ⚙️ 完整环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CAMSTAR_BASE_URL` | `http://localhost/Modeling` | Camstar REST API 根地址 |
| `CAMSTAR_USERNAME` | `CamstarAdmin` | 登录用户名 |
| `CAMSTAR_PASSWORD` | `Cam1star` | 登录密码 |
| `CAMSTAR_TIMEOUT` | `30` | HTTP 超时（秒） |
| `MAX_RESPONSE_LENGTH` | `4000` | 响应裁剪阈值（字符） |
| `LLM_API_KEY` | — | LLM API Key |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | LLM 接口地址 |
| `LLM_MODEL` | `deepseek-chat` | 模型名称 |
| `MAX_TOOL_LOOPS` | `15` | 单轮最大工具调用次数 |
| `ENABLE_PERFORMANCE_LOG` | `True` | 是否开启性能日志 |
| `SAFE_CREATE_THRESHOLD` | `20` | 批量创建拦截阈值 |
| `SAFE_UPDATE_THRESHOLD` | `3` | 批量更新拦截阈值 |
| `SAFE_DELETE_THRESHOLD` | `0` | 批量删除拦截阈值 |

**支持的 LLM Provider（替换 `LLM_BASE_URL` 即可，代码零改动）：**

| Provider | LLM_BASE_URL |
|---|---|
| DeepSeek | `https://api.deepseek.com/v1` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` |
| Ollama 本地 | `http://localhost:11434/v1` |
| OpenAI | `https://api.openai.com/v1` |

---

## 📐 架构概览

```
Browser ──HTTP/SSE──▶ FastAPI (web/)
                          │
                      Agent (agent/)
                     llm_client.py
                          │ OpenAI-compatible API
                      LLM Provider
                          │ function_call
                      Tools (tools/)
                     specs / operations / workflows
                          │
                      Core (core/)
                    auth / http_client / response
                          │ REST/HTTPS
                   Camstar Modeling API
```

> 详细设计请参阅 [`docs/DESIGN.md`](docs/DESIGN.md)

---

## 📦 依赖

```
fastmcp>=2.0.0
httpx>=0.27.0
python-dotenv>=1.0.0
openai>=1.0.0
fastapi>=0.100.0
uvicorn>=0.20.0
```
