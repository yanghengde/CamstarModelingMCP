# 性能优化进度

更新日期：2026-11-06。

## 第一批：减少重复输入和阻塞读取

- LangGraph 与 legacy 推理路径仅去掉已完整存在于首条 system 消息中的相同规则。用户和工具消息不能触发规则删除；旧检查点缺失规则时仍补充完整规则。未截断历史，也未删除任何审批或业务约束。
- Excel/CSV 导入预览最多读取 201 行，第 201 行用于拒绝超过既有 200 行上限的文件，拒绝前不查询 MES。解析放在线程中执行。
- Excel 结构预览每张表读取最多 201 行，保留前三行样例，使用上下文管理器关闭工作簿。`row_count_is_lower_bound=true` 表示行数是下限，不能当作精确总行数。
- 上传端点使用 FastAPI 同步端点线程池执行复制。聊天状态及事件订阅的入口查询同样移到线程池；SSE 每次轮询通过 `asyncio.to_thread` 在单个连接中读取状态和最多 200 条事件。
- 为会话最近任务查询增加 `(username, session_id, started_at DESC)` 索引，应用初始化时自动建立。

## 本地测量

默认系统提示词下，LangGraph 每轮附加指令减少 **3,711 字符**，系统规则总字符数减少 **12.24%**。这是确定的文本体积变化，不是 token 或真实推理时延降幅。所有原有规则内容保留。

合成 10,000 行、4 列 XLSX 文件，分别执行三次 `pd.read_excel`，本机耗时中位数：

| 读取方式 | 返回行数 | 中位耗时 |
| --- | ---: | ---: |
| 原完整读取 | 10,000 | 1,565.89 ms |
| `nrows=201` | 201 | 351.86 ms |

此样本解析耗时减少约 77.5%，约快 4.45 倍；不代表完整导入流程提速。Excel 引擎仍需打开工作簿、读取元数据/共享字符串，因此行数上限不等于整个附件的硬性内存上限。

## 验证

离线回归命令：

```powershell
python -m pytest -q -p scripts.offline_test_guard tests/test_import_performance.py tests/test_import_plans_progress.py tests/test_import_concurrency.py tests/test_chat_runs.py tests/test_prompts.py tests/test_query_performance.py tests/test_langgraph_checkpoint.py tests/test_langgraph_limits.py tests/test_chat_stream_errors.py tests/test_confirmation_policy.py
```

新增覆盖真实 XLSX/CSV 超限拒绝、样例/行数语义、阻塞解析与日志读取时事件循环仍可响应，以及去重后业务规则完整保留。已有回归覆盖任务幂等、取消、重连、审批和导入执行。

结果：73 项通过（23.12 秒）。pytest 退出清理旧临时目录时出现 Windows 权限错误，测试断言全部通过，进程退出码为 0。

## 后续顺序

1. 增加 run_id 关联的端到端耗时、排队和事件循环延迟基线。
2. 业务规则按领域加载与历史 token 预算；用多轮评测检查计划、对象引用和审批状态不丢失。
3. 全局/每用户有界任务队列；继续处理创建、取消、状态写入和经验库等同步 I/O。当前 SSE 仍采用 250 ms 轮询。
4. 日志索引分页、会话懒加载、上传体积/工作表数量限制和历史数据保留策略。

本批不改变单 worker 部署方式。尚未进行真实模型 A/B 测试或 MES 并发压测；重启服务后代码和查询索引才会生效。
