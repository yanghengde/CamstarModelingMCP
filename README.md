# Camstar Modeling MCP Agent

高级抽样检验组合：[第6部分进度与现场记录](docs/inspection-combinations-completion.md)。计数扣量、分组累计、逐原因损失及归零关闭已验收；独立后续质量事件等待管理员开通 MES 创建服务权限后完成最后一项现场验收。

生产过站与报工：[第1部分完整流程、工具与限制](docs/production-reporting.md)。六项核心功能已完成独立现场闭环验收，包含MES人工分钟落账、两种不良处置和原生整单生产完成；原生MoveOut仍有服务端错误，使用明确分步生产流程，详见[验收记录](docs/production-acceptance.md)。

共享只读查询：[通用编排、元数据缓存与工单日期模板](docs/read-orchestration.md)。复用原MCP工具，支持受控依赖、有限并发、分页和精确关联/汇总；生产写入流程保持独立。

质量开发当前目标与完成标准：[闭环交付总清单](docs/quality-completion-plan.md)。C1代码和离线验证已交付、现场未验收；本轮推进[C2.A角色替代审批](docs/quality-role-approval.md)。下一闭环为安全电子签名输入与提交，旧阶段报告不等于全部质量功能完成。

质量功能入口：[NCR、CAPA、质量对象与抽检/SPC工具及现场验证范围](docs/quality-mcp-tools.md)。
质量分组现有72个工具，覆盖NCR/CAPA、检验定义与抽样表、单容器/批采样、当前采样状态与默认检验水平复位，以及已关闭关联事件后的NCR收尾计划。阶段交付、测试证据、部署步骤和未完成范围见[质量交付说明](docs/quality-delivery.md)；高级功能第一阶段新增[受限批返工](docs/quality-lot-rework.md)，第二阶段新增[数量损失准备与批报废前置检查](docs/quality-quantity-loss.md)，第三阶段扩展[原生全检与测量自动扣量](docs/quality-inspection-effects.md)，第四阶段新增[SPC本地计算与判异](docs/quality-spc-analysis.md)、[原始历史点受限注释](docs/quality-spc-annotations.md)和[明确报告的单点SPC违例登记](docs/quality-spc-violations.md)，第五阶段新增[质量事件首次分流](docs/quality-event-triage.md)与[事件文字资料维护](docs/quality-event-update.md)，后续顺序见[高级功能开发计划](docs/quality-advanced-roadmap.md)；真实MES验收及其余高级功能仍待完成。

多轮调用评测：[38段问法、真实模型结果与已修复/待修复问题](docs/multiturn-evaluation/README.md)。

数据采集工具入口：[完整工具链与参数示例](docs/collection-workflow-tools.md)，覆盖定义/采集点/组维护、工序绑定、预览提交、历史及结果核实。

> 基于 **FastMCP + FastAPI + LangGraph + LLM** 的 Siemens Opcenter (Camstar) MES 建模 AI 助手。
> 将 Camstar Modeling REST API 封装为 MCP 工具，通过自然语言对话操控 Spec、Operation、Workflow 等建模实体。

持续开发入口：[核心周边能力开发任务与进度](docs/core-surrounding-capabilities-roadmap.md)。按 T00–T09 推进设备资源、BOM、采集及开工准备检查，并记录各阶段验收结果。

---

## ✨ 功能特性

| 功能 | 说明 |
|---|---|
| **自然语言建模** | 用对话方式创建/查询/更新/删除 Spec、Operation、Workflow |
| **流式实时输出** | 基于 SSE 的逐字流式渲染，消除 LLM 阻塞等待 |
| **安全卡点防护** | 批量操作阈值拦截 + OTP 一次性密码双重验证，防止误操作 |
| **可恢复工作流** | LangGraph + SQLite 保存执行检查点，确认后从暂停位置继续 |
| **受控经验闭环** | 自动聚合脱敏失败候选，人工审核后才注入后续相关任务 |
| **标准 MCP 接入** | 提供带 Bearer 鉴权的 Streamable HTTP 端点，兼容新旧 MCP 客户端 |
| **多用户多会话** | 按用户名隔离的持久化历史记忆，支持会话切换 |
| **后台执行与进度恢复** | 切换页面、聊天或刷新后任务继续运行，返回后恢复输出；确认和主动停止保留 |
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
│   ├── llm_client.py    # LLM 客户端 + 新旧执行引擎分发
│   ├── langgraph_runtime.py # 可恢复的 Agent 状态图
│   ├── safety.py        # 可测试的工具分类与确认策略
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

后台聊天的执行方式、接口与验证见[后台任务与进度恢复](docs/background-chat-runs.md)。修改后需重启 Web 服务并刷新页面，使用新的前后端代码。

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

需要运行自动化测试时安装开发依赖：

```bash
pip install -r requirements-dev.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 Camstar 地址、账号密码和 LLM API Key
```

关键配置项：

```ini
CAMSTAR_BASE_URL=https://your-camstar-host/Modeling
CAMSTAR_SHOPFLOOR_BASE_URL=https://your-camstar-host/Shopfloor
CAMSTAR_USERNAME=CamstarAdmin
CAMSTAR_PASSWORD=your-password

LLM_API_KEY=sk-your-api-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

AGENT_ENGINE=langgraph
LANGGRAPH_CHECKPOINT_DB=data/langgraph_checkpoints.sqlite
EXPERIENCE_DB=data/agent_experience.sqlite
MEMORY_CONTRACT_VERSION=1

# 对外提供 MCP Streamable HTTP 时启用；必须设置访问令牌
ENABLE_MCP_HTTP=True
MCP_HTTP_PATH=/mcp
MCP_API_KEY=请替换为高强度随机字符串
# 从局域网访问时加入服务端实际 IP，例如 172.25.23.19:*
MCP_ALLOWED_HOSTS=localhost:*,127.0.0.1:*,[::1]:*,172.25.23.19:*
```

### 3. 启动服务

学习库的审核、过期、撤销、替代、用户纠错捕获与兼容迁移见 [学习与记忆生命周期](docs/learning-and-memory.md)。

能力介绍的即时读取、源码变更检测和生效边界见 [能力目录即时刷新](docs/capability-refresh.md)。

本轮目录直接呈现、历史目录压缩、受控并发读取及实测对比见 [回复性能优化与验证](docs/response-performance-round.md)。

```bash
python main.py
```

服务启动后访问：

- 💬 **聊天界面**：http://127.0.0.1:8030/
- 📊 **性能日志**：http://127.0.0.1:8030/logs
- 🔌 **MCP Streamable HTTP**：http://127.0.0.1:8030/mcp/（启用后）

### 外部 MCP 客户端配置

当前实现使用标准 **Streamable HTTP**，不再新建旧式 `/sse` MCP 传输。浏览器聊天使用的 `/chat` SSE 只是文本流，两者互不影响。

支持自定义 HTTP Header 的 MCP 客户端可使用以下通用配置：

```json
{
  "url": "http://127.0.0.1:8030/mcp/",
  "headers": {
    "Authorization": "Bearer 与 MCP_API_KEY 相同的值"
  }
}
```

局域网客户端还需把服务器 IP 加入 `MCP_ALLOWED_HOSTS`。端点默认关闭；这是因为其中包含创建工单、Start、Move、Rework 等生产写操作，未配置令牌时程序会拒绝启动外部 MCP 服务。

生产环境请在反向代理上启用 HTTPS；Bearer 令牌不应通过明文局域网 HTTP 传输。需要面向多用户或第三方客户端开放时，应进一步换成 OAuth 2.1，而不是共享一个静态令牌。

---

## 🛠️ MCP 工具一览

### Spec 实体（`/api/Specs`）

建模字段更新统一使用实例 ID 和最小 JSON PUT；程序调用方式、同类修复与验收范围见 [建模更新契约](docs/modeling-updates.md)。修改代码后需重启服务，让模型重新加载工具参数。

| 工具 | 方法 | 说明 |
|---|---|---|
| `list_specs` | GET | OData 查询 Spec 列表 |
| `get_spec` | GET | 按 key 获取单条 Spec |
| `get_spec_by_odata_key` | GET | OData 键语法获取 |
| `create_spec` | POST | 创建新 Spec |
| `update_spec` | PUT | 按实例 ID 提交明确字段的最小更新 |
| `update_spec_by_odata_key` | PUT | OData 键最小更新兼容入口 |
| `patch_spec` | — | 已禁用兼容入口，不发送请求 |
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
| `update_operation` | PUT | 按实例 ID 提交明确字段的最小更新 |
| `delete_operation` | DELETE | 删除 Operation |
| `get_operations_count` | GET | 获取总数 |
| *(+ OData 变体及 RequestSelectionValues)* | — | — |

### Shopfloor Container 事务

| 工具 | 方法 | 说明 |
|---|---|---|
| `container_start` | POST | 按工单启动 Container，支持显式序列号或自动编号规则 |
| `container_move` | POST | 通过 MoveStd 将 Container 移动到下一工步 |
| `container_move_in` | POST | 将 Container 移入当前工步/资源开始处理 |
| `container_move_out` | POST | 将 Container 移出当前工步并继续流转 |
| `get_moveout_diagnostics` | 只读查询 | 核对真实容器 ID、状态、层级/工序配置及 ChangeQty 必填明细证据，不执行过站 |
| `container_defect` | POST | 通过 ContainerDefect 记录 Container 缺陷 |
| `rework` | POST | 通过原始 Rework 服务将 Container 转入返工流程 |

移动工具现在先核实实际 ID、Active/InProcess 及层级 AllowMove，Level 不进入写入引用。Agent 本轮移动失败后停止同一容器的后续移动尝试，只读查询不因历史写入次数被重复要求确认。详见[移动参数与审批修复](docs/container-movement-recovery.md)。

`get_container_details` 顶层 `container_instance_id`/`container_ref` 明确返回真实 Container 引用；`view_record_instance_id` 仅为临时视图行，不能用于移动。新工具及共享执行规则需重启服务、刷新外部 MCP 工具定义后生效。

### Container 编号与层级维护

| 工具 | 方法 | 说明 |
|---|---|---|
| `list_numbering_rules` / `get_numbering_rule` | GET | 查询并校验自动编号规则 |
| `list_container_levels` / `get_container_level` | GET | 查询 Level 及其默认编号规则 |
| `get_container_levels_count` | GET | 查询层级数量 |
| `get_container_level_schema` | 本地只读 | 查看全部字段及复杂字段的完整类型定义 |
| `create_container_level` | POST | 创建层级，支持完整可写字段 |
| `update_container_level` | PUT | 更新明确字段，支持唯一性、编号、父子层级、属性、WIP/UI 等配置并回读 |
| `delete_container_level` | DELETE | 删除指定层级并核实结果 |
| `request_container_level_selection_values` | POST（读） | 读取原生字段候选值 |

完整字段范围、调用示例与验证边界见[ContainerLevel 完整建模工具](docs/container-level-modeling.md)。新增工具需重启服务并刷新 MCP 工具定义后生效。

### Query API（`/Query/api`）

| 工具 | 方法 | 说明 |
|---|---|---|
| `list_query_services` | GET | 从 Query Swagger 动态列出可用 Inquiry 与查询服务 |
| `get_query_service_schema` | GET | 查询某个 Query CDO 的路径、参数及请求字段 |
| `execute_query_inquiry` | POST | 执行标准 Inquiry CDO，支持 `$select`、`$expand` 与 `execute=true` |
| `execute_query_inquiry_event` | POST | 执行 Inquiry CDO 的自定义事件，并纳入修改安全锁 |
| `request_query_selection_values` | POST | 查询 Inquiry 字段的合法候选值 |
| `execute_advanced_query` | POST | 执行 Designer 中的系统 Advanced Query |
| `execute_user_query` | POST | 执行 Modeling UserQueries 中的 User Query |
| `execute_adhoc_query` | POST | 执行单条只读 SELECT；拒绝写入和多语句查询 |

---

## 🛡️ 安全机制

### 批量操作阈值拦截

在 `.env` 中可配置触发强制确认的阈值：

```ini
SAFE_CREATE_THRESHOLD=20   # 批量创建 > 20 条时暂停并请求确认
SAFE_UPDATE_THRESHOLD=3    # 本次待执行更新 > 3 条时暂停，不累计此前调用触发连续确认
SAFE_DELETE_THRESHOLD=0    # 任意删除均需先确认（0 = >=1 拦截）
```

普通少量修改及进出站不因此前累计修改次数重复要求确认。用户明确要求执行且本地不可变计划有效时，新建检验指标、检验项目和抽检计划免逐项确认；已有质量配置修改/升版、产品关联、检验提交、库存/数量变化、删除及人工处理不明结果仍须确认。创建数量仍保留任务累计上限，防止拆成单条调用绕过批量保护。同一批完整待执行调用合并确认，授权仍绑定原参数；不扩展到尚未生成的后续计划，也不改变失败停止、单次提交及电子签名规则。

LangGraph 会把待执行工具调用保存在 SQLite 检查点中。系统只接受“确认创建”、
“确认修改”、“确认删除”等明确授权；“不是”“不要创建”“取消执行”不会误触发。
确认后会从暂停位置继续，不要求 LLM 重新生成工具参数。紧急回退时可设置
`AGENT_ENGINE=legacy` 并重启服务。

### OTP 单次密码验证

删除等高风险操作额外受到 OTP 守卫保护：LLM 首次尝试删除时系统自动生成一个内部 OTP，要求用户明确回复"确认删除"后，由 LLM 持 OTP 二次发起才能真正执行删除，且 OTP 一次性消耗防止重放攻击。

### 受控 Self-improvement

工具执行失败会以脱敏、去重的方式进入经验候选库，但不会自动改变 Agent 行为。查看状态和候选：

```bash
python -m agent.experience list
python -m agent.experience list --status all
```

验证解决方案后，由管理员显式批准或拒绝：

```bash
python -m agent.experience approve 12 --resolution "先通过 Details.Owner SelectionValues 解析 Owner" --evidence "2026-10-08 回归测试通过"
python -m agent.experience reject 13 --evidence "临时网络故障，不是稳定规则"
```

只有批准的经验会在相关工具或任务中注入。Container Start 请求还会直接读取项目 Skill 中的 Start contract 和 known failures，使 Codex 经验与 Web Agent 使用同一事实来源。只读状态接口为 `/api/experience/status` 和 `/api/experience/candidates`。

---

## ⚙️ 完整环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CAMSTAR_BASE_URL` | `http://localhost/Modeling` | Camstar REST API 根地址 |
| `CAMSTAR_SHOPFLOOR_BASE_URL` | 从 Modeling URL 推导 | Camstar Shopfloor REST API 根地址 |
| `CAMSTAR_QUERY_BASE_URL` | 从 Modeling URL 推导 | Camstar Query REST API 根地址 |
| `CAMSTAR_USERNAME` | `CamstarAdmin` | 登录用户名 |
| `CAMSTAR_PASSWORD` | `Cam1star` | 登录密码 |
| `CAMSTAR_TIMEOUT` | `30` | HTTP 超时（秒） |
| `MAX_RESPONSE_LENGTH` | `4000` | 响应裁剪阈值（字符） |
| `LLM_API_KEY` | — | LLM API Key |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | LLM 接口地址 |
| `LLM_MODEL` | `deepseek-chat` | 模型名称 |
| `MAX_TOOL_LOOPS` | `15` | 单轮最大工具调用次数 |
| `LANGGRAPH_RECURSION_LIMIT` | `MAX_TOOL_LOOPS * 6 + 10` | LangGraph 单次请求的节点步数上限 |
| `AGENT_ENGINE` | `langgraph` | 执行引擎；可设为 `legacy` 回退 |
| `LANGGRAPH_CHECKPOINT_DB` | `data/langgraph_checkpoints.sqlite` | 工作流检查点数据库 |
| `EXPERIENCE_DB` | `data/agent_experience.sqlite` | 脱敏经验事件、候选及审核结果数据库 |
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

MCP Client ──Streamable HTTP + Bearer──▶ FastMCP (/mcp/)
                                             │
                                        同一组 Tools
```

> 详细设计请参阅 [`docs/DESIGN.md`](docs/DESIGN.md)

---

## 📦 依赖

```
fastmcp>=4.0.11,<5.0.0
mcp>=2.3.0,<3.0.0
httpx>=0.27.0
python-dotenv>=1.0.0
openai>=1.0.0
fastapi>=0.142.0,<0.143.0
uvicorn>=0.20.0
```

2026-10-29补充：[事件关闭前置诊断](docs/quality-event-closure.md)已接入；仅诊断，不开放事件关闭提交。

2026-10-29新增：[受限基础Event关闭计划](docs/quality-event-close-plan.md)，仅无复杂关联/流程/审批/签名的独立事件；真实关闭待验收。

2026-10-29补充：[质量事件创建资料核验](docs/quality-event-creation.md)已接入；仅核验名称/分类/报告人/时间及通知策略，不调用CreateEvent，创建执行仍待完成。

2026-10-29新增：[事件生命周期计划](docs/quality-event-lifecycle.md)，自动编号实际创建、Closed开启、明确人员/角色重新分派；显式复杂关闭扩展CAR/流程/关联NCR和已批准审批。真实效果待验收，事件数据/批次子事务、待审批/签名仍未支持。

2026-10-29后续扩展：[制造NCR原生联动关闭](docs/quality-event-ncr-linked.md)支持显式双选项、标准类别3事件数据、唯一原NCR及同容器处置完整覆盖；只发父关闭请求，不补发NCRCloseQuality。实际复合事务仍待验收。

2026-10-29新增：[审批上下文只读核对](docs/quality-approvals.md)，支持Event/CAPA直接审批表、条目、原生允许决策及当前入口；不路由、签批、撤回或收集口令。

2026-10-29新增：[审批表路由/取消计划](docs/quality-approval-actions.md)，仅空白Pending首次路由和未签批Routed取消，系统确认后单次执行；实际签批、退回和电子签名仍未开放。

2026-10-29新增：[审批决策计划](docs/quality-approval-decisions.md)，认证原审批人对当前Routed条目批准/拒绝，明确原表/条目/决策，确认单次执行；角色代签、退回和需口令的电子签名仍未开放。
