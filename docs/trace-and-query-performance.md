# 关联追溯与查询实测

2026-10-22，新增只读 get_container_trace。

## 关联证据

按当前库 INFORMATION_SCHEMA 核对了 Container、SplitHistory/SplitHistoryDetails、CombineHistory/CombineHistoryDetail、HistoryMainline 和 HistoryCrossRef 字段及关联键。工具分六段返回：current_family、splits、combines、parent_history、rework、cross_references。

父子段展示当前父容器、直接子容器与 SplitFromId；历史父子段保留事务时 ParentContainerId。拆分/合并通过主表和明细表关联，保留来源、目标、数量、历史主线 ID 和事务 ID。返工查询保留 InRework 或返工事务记录。交叉引用保留类型原码及起止事务，不假设 HistoryId/TrackingId 都是容器，也不猜测类型含义。

范围是单跳，不是无限递归全族谱；调用者可使用明确的来源/目标容器 ID 继续查。每个 section 独立分页，默认 20、最多 50 条，next_offset 对应该 section。complete 始终为 false，requested_sections_complete 只表示当前指定范围无更多页且没有失败。并发变更下偏移分页不是事务快照。

当前 BOX-0001 返回当前关系 1 行、交叉引用 1 行；其他段为空。当前库 SplitHistory、CombineHistory、返工历史检查均没有找到样本，因此真实非空拆分/合并/返工验收尚未完成。没有创建业务数据来制造样本。

## 稳定性

HTTP GET 和显式 retry_read_only 查询对连接失败、远端协议错误及 429/502/503/504 最多尝试两次；总预算为 CAMSTAR_TIMEOUT，短退避也计入预算。400 等业务错误和超时不自动重放。写入和通用 Query 自定义事件默认只发送一次。

追溯身份查询最多 15 秒，每个证据段最多 15 秒，整个追溯请求预算 60 秒。某段失败保留其他段，返回具体 error；不把错误当成空历史。按需加载修复了“查询返工关联”误触发写工具的问题；工具发现仍可按需补充能力，筛选不是授权机制。

## 实测方法与结果

scripts/benchmark_queries.py --llm 运行固定只读服务查询，并比较按需/全量工具下的模型首轮工具选择。模型输出工具调用只记录，不执行。记录真实供应商 usage（含缓存字段），非流式模型调用时间与服务查询时间分别统计。报告 docs/query-benchmark.json。每场景每模式只有一次采样，不能代表长期准确率、端到端回答质量或稳定延迟收益。

| 场景 | 候选工具 | 服务查询秒 | 按需输入 token | 全量输入 token |
|---|---:|---:|---:|---:|
| 容器状态 | 6 | 0.429 | 1640 | 21822 |
| 关联追溯 | 6 | 0.215 | 1647 | 21829 |
| 产品完整工艺链 | 25 | 1.724 | 4239 | 21828 |
| 工单现场进度 | 13 | 0.522 | 2737 | 21824 |

四类请求的范围内检查通过，按需与全量共 8 次首轮工具选择都命中预期工具。这里的 4/4 是状态字段/请求分段/产品关联/工单当前页检查成功，不表示全业务历史完整。全量请求有缓存命中且部分响应更快，不能由输入 token 减少推断实际费用或延迟同比下降。

116 项相关测试、2 项子测试通过。未执行业务写入。重启服务后生效。
