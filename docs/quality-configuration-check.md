# 检验计划配置检查

2026-10-27新增只读MCP工具 `check_sampling_plan_configuration`。复用已经核对过的Modeling读取和受限ChangeCount查询，不新增MES写入入口，quality分组现为42个工具。

## 调用

```json
{
  "sampling_plan_name": "明确的计划名称",
  "sampling_plan_revision": "明确的版本"
}
```

适合“检查这个版本的检验计划是否配完整”“检验点上下限有没有问题”“AQL表与检验水平代码能否对应”等问法。没有明确计划名称或版本时先补齐，不猜当前版本。需要维护时，依据问题准备现有 `prepare_inspection_definition` 或 `prepare_sampling_reference_definition` 计划，由统一质量执行入口处理。

## 检查内容

- 完整计划明细中的测试引用、测试和检验点的Active状态；计量测试是否缺点、点名称或ID是否重复。
- 当前工具支持的数值点类型及字面上下限，拒绝把公式或未知数据解释成数值；报告上下限反置。
- 计划默认及明细显式关联的AQL、检验水平表，复用完整表父级、明细ID、阈值和闭区间校验；按原样代码检查检验水平与所配置AQL表的映射。即使被明细覆盖，已配置的默认表仍检查。
- 全检、外部采样、非数值点、自动扣量/事件/拒收报废等现有提交工具的限制，独立列入 `submission_limitations`。这些限制不自动等同MES配置错误。
- 依赖末尾重新读取，检查完整配置与ChangeCount；重新解析原始引用，检测UseROR目标变化。

冻结用于限制修改配置，不据此断言不能执行检验。因此本工具不会把冻结的计划、测试、点或表直接判为不可检验。表维护及查表工具仍保留各自现有的冻结限制。

## 结果含义

`complete=true` 仅表示本工具列明的静态检查已完成；`assessment` 为 `no_issues_found` 或 `issues_found`。无法完整读取、缺少已检查字段、断引用、超限或发生可见变化时为 `complete=false`、`assessment=incomplete`。

每条问题包含稳定的 `code`、具体 `path` 和说明。输出保留具体明细ID、实际解析的版本/ID、依赖ChangeCount和原存储采样频率。`configured_table_sources` 区分明细配置与计划默认；**这不是现场当前采样状态或切换规则执行后的有效表证明**，`runtime_table_selection_verified` 始终为false。不推算有效采样频率。

以下标志始终保持：`submitted=false`、`quality_verdict=null`、`execution_readiness_verified=false`、`mes_task_requirements_verified=false`、`standard_compliance_verified=false`、`atomic_snapshot_verified=false`。重读只能检测可见变化，不保证数据库原子快照，也不阻止其他客户端修改。

本工具不验证产品绑定、Spec/设备/供应商引用、拒收原因组、切换规则、权限、签名或已有容器/批任务，不提供指定批量的有效样本要求，不生成检验结果或放行结论。已有任务仍以 `get_container_inspection_tasks` 或批采样查询的MES持久化要求为准。

## 边界和验证

完整计划1..50条测试明细；每个测试最多20个检验点；一次最多100个不同依赖、100个检验点关联，总检查90秒。超限不以子集冒充完整。共用依赖复用读取，但逐条明细保留检查结果。所有业务请求只使用已有只读Modeling接口和固定ChangeCount查询，不创建质量执行计划或事务journal记录。

最终全量回归1848项测试、2项子测试通过，其中质量相关642项；父pytest离线HTTPX真实网络尝试0。首次回归发现已有并发测试的Windows本地文件短暂占用，已对本地测试控制文件交换加入最多1秒等待；没有改变MES请求重试策略。

本轮67项新增测试覆盖真实ReadContext和既有定义/表读取器、MCP参数、连续两次配置检查、异常数据、代码映射及依赖变化；HTTP和SQL边缘使用模拟数据。问法检查验证工具可见性，**没有进行真实LLM多轮选工具成功率评测**。

2026-10-27现场API连接超时。本轮没有取得新的现场契约或业务数据，也没有执行MES写入。`CurrentSamplingStatusUpdate` 已在先前Swagger目录和当地Siemens手册中发现，但本轮无法核对其字段，该轮未开放工具；后续已补齐[当前采样状态工具](quality-current-sampling.md)。手册说明它只影响后续符合条件的容器，不作用于已在制容器或批采样；不能用抽样批维护工具替代。

现场端到端验收、批返工/报废、全检转换和SPC等仍未完成。证据与回归结果见[本轮摘要](quality-fixtures/configuration-audit-summary.json)。
