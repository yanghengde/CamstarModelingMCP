# 产品当前采样状态与调整

2026-10-27新增两个MCP工具，quality分组共44个工具：

| 工具 | 用途 |
|---|---|
| `get_current_sampling_status` | 固定产品版本的真实当前采样状态分页，展示可明确选择的记录ID、计划、测试、工序/设备/供应商引用、当前频率、计数器和检验水平 |
| `prepare_current_sampling_status_update` | 对明确选中的1..20条容器采样状态准备计数器或手动检验水平调整计划 |

复用 `execute_quality_transaction` 确认后单次提交、`get_quality_transaction_plan` 查看原计划、`inspect_quality_transaction` 读取原选择的当前状态，以及 `review_quality_transaction` 人工最终证据审计。

## 参数与流程

先明确产品名称和版本查询：

```json
{
  "product_name": "明确产品名称",
  "product_revision": "明确产品版本",
  "offset": 0,
  "limit": 20
}
```

每页最多50条，按真实 `SamplingCurrentStatusId` 排序，返回 `has_more` 和 `next_offset`。`page_complete=true` 只表示当前页完整，`complete` 始终为false。空页不证明产品无需检验。批采样记录仍可查询，但 `container_based_candidate=false`，不能交给本调整工具。

从查询结果明确选择记录ID，准备修改：

```json
{
  "product_name": "明确产品名称",
  "product_revision": "明确产品版本",
  "status_instance_ids": ["从查询结果取得的16位状态ID"],
  "new_sample_rate_counter": 0,
  "comments": "明确的调整说明"
}
```

`new_sample_rate_counter` 为0..2147483647的明确整数。也可以传 `new_inspection_level_name` 调整手动检验水平，或者同时传两项。至少有一项，全部选择均无实际变化时拒绝。省略的调整字段不发送，并检查非执行回显没有补成默认值。备注最多180个UTF-16单元，事务关联标记由系统添加。

状态ID必须是查询所得真实ID，不能把计划ID、测试ID、容器ID当成状态ID。不能通过名称推测选择，不能传空列表表示全部，不能用多个大小写形式重复选择同一ID。一次全部明确选择只生成一个计划、一个提交请求。

准备成功后展示原状态与请求，用户确认同一 `plan_id` 和 `plan_digest` 后调用统一执行工具。执行前重建计划和摘要，状态或依赖变化时阻止提交；不会静默改用最新计数。

## 计数器、频率和检验水平

`CurrentSampleRate` 是当前采样频率；`SampleRateCounter` 是用于判断采样时机的累计计数器。**调整计数器不等于调整频率**，本工具不改 `CurrentSampleRate` 或原SamplingPlan定义，也不推算下一个容器一定检验或一定免检。

`CurrentInspectionLevelId` 与 `SamplePlanInspectionLevelId` 分开展示。后者用于记录手动检验水平；即使新水平等于当前水平，设置此前不存在的手动水平仍有意义，不能仅凭当前水平相同判无变化。依据EDM，手动水平可能使后续不再评估原切换规则；工具展示此影响，不执行或模拟切换规则。

手动调整检验水平时，完整读取新检验水平及每条状态的当前AQL表，检查父级、明细ID、字面样本量/阈值、闭区间和原样样本量代码映射。最多50行；不解释公式或百分比，不生成抽样标准表，不宣称标准符合性。

## 准备与提交边界

- 产品必须为固定Active版本；状态产品归属和CDO类型8426明确，每条状态必须完整返回，`SamplingLotId` 必须为空。
- 所选状态的计划与测试固定版本必须Active，当前计划仍包含该测试；未改变的Spec/设备/供应商字段仅保留并参与状态快照，不重新定义其业务范围。
- 状态、产品ChangeCount及完整关联配置准备末尾重读；执行前再次重建摘要。重读只能检测可见变化，不保证数据库原子快照，也不锁住其他MES客户端。
- 非执行预览必须回显固定产品、准确的选中状态集合、明确调整值、`ResetInspectionLevel=false` 和完整评论。不能扩大选中集合；服务内部处理集合非空时也必须一致。没有完整选中记录回显就不能生成可执行计划。
- 使用正式Shopfloor `CurrentSamplingStatusUpdate`，不直接更新数据库。实际执行仅一次，不跟随重定向、不自动重试。HTTP 200仍保存为unknown，不能宣称最终成功。
- 未核实事务阻止同产品的其他当前采样调整，也与相关产品绑定、检验定义和抽样基础表维护按读写声明互斥。这只是本MCP质量事务域内防护，不是全MES锁。

本功能只影响**后续符合采样条件的容器**，不回填已在制容器的任务，不修改抽样计划、采样频率或已有批任务。批采样使用独立流程；不能用 `prepare_sampling_lot_update` 代替此工具。本轮不开放ResetInspectionLevel复位模式、全选或自动应用全部记录。

`inspect_quality_transaction` 只读取原计划明确选中的记录，并重读检测可见变化。当前计数或水平相同不是本次事务成功证明，`verified_success/state_changed/retry_allowed/history_correlation_verified` 均为false。当前部署没有发现专用 `CurrentSamplingStatusUpdateHistory` 表，通用主历史与具体状态的因果链未验证，因此 `find_quality_execution_candidates` 与 `reconcile_quality_execution` 暂不支持此类计划。最终证据需人工审计，原计划始终不可重放。

## 接口依据与验证

依据现场Swagger、Shopfloor/Query EDM、数据库列元数据及Siemens Release2510+ Shop Floor手册PDF第365..367页（章节8-9..8-11）。正式字段为 `Product`、`SamplingCurrentStatus`、`NewSampleRateCounterValue`、`NewInspectionLevel` 和 `ResetInspectionLevel`；选择列表为BaseObjectRef，仅使用实例ID。

2026-10-27现场连接恢复，已核对接口和SQL字段。三个未执行预览返回HTTP200，分别验证无目标输入、仅产品输入和省略调整值；**均未选择真实状态记录**。新查询工具对真实产品版本成功返回空状态页，全表TOP1也为空。因此这只能证明契约、查询形状和部分字段回显，不能证明正目标预览、实际计数累计、检验水平切换、后续容器效果或最终业务历史。本轮未向MES写入业务数据，未创建采样状态样本，未重启服务。

全量回归1918项测试、2项子测试通过，其中质量相关712项，父pytest离线HTTPX真实网络尝试0。新增70项离线测试覆盖状态归属、分页、严格计数、选择集合、AQL代码、非执行回显、准备/执行间变化、服务白名单、互斥、MCP协议调用和unknown检查。测试中的准备/执行/检查流程使用模拟业务响应；问法验证是工具可见性检查，不是真实LLM多轮选工具成功率测评。

证据见[本轮摘要](quality-fixtures/current-sampling-summary.json)、[接口契约](quality-fixtures/current-sampling-contracts.json)、[字段](quality-fixtures/current-sampling-columns.json)、[现场只读结果](quality-fixtures/current-sampling-read-validation.json)。
