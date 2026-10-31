# 后台聊天任务与进度恢复

2026-10-31。制造助手的执行从浏览器 SSE 生命周期移到服务端独立任务。切换操作记录、进入日志页面、新建/切换聊天、刷新或关闭浏览器只断开显示订阅；只要 Web 服务仍在运行，后台任务就继续执行。重新打开原对话时恢复进度、部分回复及最终输出。

## 使用行为

- 提交后显示“后台执行中 · 可切换页面”和已执行时间；最近聊天显示“执行中”“待确认”等状态。当前对话执行中限制重复提交，其他对话仍可新建和独立执行。
- 回到对话时根据服务端记录恢复输出，不重新调用模型或业务工具；重连按事件序号补齐，避免重复片段及跨对话输出。
- 浏览器收到提交响应前断线时，根据请求 ID/原对话恢复已接受任务，不能把断线当作未执行并自动重发。相同请求 ID 的原请求只启动一次；更换参数后重用同一 ID 会被拒绝。
- 原有业务审批保持：待确认时后台运行结束并保留检查点，返回后显示同一确认卡；不会因为刷新或恢复订阅而自动批准。
- 方形停止按钮现在是“停止执行”：向服务端明确取消后台任务。离开页面不触发该接口。已提交的 MES 操作不会被回滚，取消/异常后仍需核实实际结果。

## 实现与接口

`web/chat_runs.py` 负责独立 asyncio worker、SQLite 任务记录、事件日志、请求去重、当前对话互斥和主动取消。原有 `chat_stream`、模型及工具执行逻辑保持在后台 worker 中运行。浏览器订阅关闭不会取消 worker。

任务状态包括 queued/running/cancelling/completed/approval_required/cancelled/failed/interrupted/limited/incomplete。达到执行上限或检查点不完整分别显示，不当作目标已完成。事件日志只保存公共进度及输出，step 仅记录工具名称，不重复存储工具参数。

| 接口 | 用途 |
|---|---|
| `POST /api/chat/runs` | 提交 message、username、session_id、request_id；202 返回任务记录 |
| `GET /api/chat/runs/{id}?username=...` | 读取指定用户的任务状态 |
| `GET /api/chat/runs/{id}/events?username=...&after=N` | SSE 重放并订阅 N 之后的事件，不执行业务 |
| `POST /api/chat/runs/{id}/cancel?username=...` | 主动停止该任务；不撤销已提交操作 |
| `GET /history/{username}?session_id=...` | 返回原历史及最新任务的恢复元数据 |
| `GET /sessions/{username}` | 返回原会话清单及每个会话最新任务状态 |

原 `POST /chat` 保留 SSE 兼容性，也使用同一后台 worker；关闭旧订阅同样不会中断执行。任务读取、订阅及取消均按 username/id 核对，沿用当前应用的用户身份入口，没有额外引入登录体系。

`static/workspace.js` 在切换对话时仅取消显示连接，使用对话/视图版本防止旧流写入新对话；后台任务不依赖浏览器定时器。页面状态、侧栏标记和显式取消按钮与服务端记录同步。

## 启动与边界

使用现有 `main.py` 的单 Web worker 部署。可通过 `CHAT_RUN_DB` 指定任务数据库，默认 `data/chat_runs.sqlite`；应与现有 sessions 和 LangGraph 检查点一并保留。本实现不提供多 worker/多实例的共享任务调度。

Web 服务关闭或进程退出会停止内存中的 worker。下次启动将遗留活动任务标为 interrupted 并保留事件，不自动重放可能已经提交的业务操作。此能力覆盖页面/浏览器切换和网络重连，不代表服务重启后继续生产事务。服务退出时先停止后台 worker，再关闭检查点和 HTTP 连接。

新版本需重启 Web 服务并刷新页面。静态资源版本已更新；没有自动重启用户当前服务或执行真实 MES 业务操作。

## 验证

`tests/test_chat_runs.py` 使用临时数据库验证订阅断开、游标恢复、请求幂等、单会话互斥、不同会话并发、用户范围、显式/立即取消、关机/重启处理、审批和异常状态，以及旧 `/chat` 兼容性。

`scripts/preview_background_chat.py` 为独立本机预览服务器，仅产生模拟回复，不连接 LLM/MES。`scripts/verify_background_chat.cjs` 用 Playwright 在独立端口、临时会话和任务数据库验证完整页面切换、返回、刷新、新建/切换聊天、停止、确认及提交响应丢失后的恢复。测试屏蔽外部页面/CDN请求；确认测试使用模拟审批，不提交业务。

浏览器验收报告位于本地 `docs/background-chat-ui-validation.json`，桌面/手机截图位于 `data/background-chat-ui/`；这些属于被 git 忽略的测试产物。

本轮结果：148 项离线回归通过；浏览器 5 组验收通过，页面脚本错误为 0。覆盖日志页完整导航、返回与刷新、不同对话并行、明确停止、待确认恢复及提交响应丢失。验证仅使用模拟执行，没有调用真实 LLM 或 MES 业务写入。
