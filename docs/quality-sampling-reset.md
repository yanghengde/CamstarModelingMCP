# 恢复抽样计划默认检验水平

2026-10-28扩展现有 `prepare_current_sampling_status_update`，新增严格布尔参数 `reset_inspection_level`，默认false。没有新增工具，质量分组保持46个工具。

该参数位于既有 `comments` 之后，保留旧位置参数顺序；旧计数器和命名水平请求仍发送原有的 `ResetInspectionLevel=false`。省略新参数的旧计划重建仍保留原请求和预览语义。

## 调用方式

先用 `get_current_sampling_status` 查询明确产品版本，逐一明确1..20个实际状态ID：

```json
{
  "product_name": "明确产品名称",
  "product_revision": "明确产品版本",
  "status_instance_ids": ["查询返回的实际16位状态ID"],
  "reset_inspection_level": true,
  "comments": "请求恢复抽样计划默认检验水平"
}
```

复位可以同时传入明确的 `new_sample_rate_counter`；不传就省略计数器字段，不把它默认为0。**复位与 `new_inspection_level_name` 互斥**，不能既要求恢复计划默认，又指定新的命名水平。字符串 `"true"`、数字1和null均不能作为复位布尔值。

复位单独使用即可，不强制额外修改计数器。即使当前没有手动检验水平关联，仍可准备明确的原生复位命令：切换规则和其他运行时状态可能使当前水平不同于计划默认，工具不会据此猜测“已经默认、无需复位”，也不会声称一定产生了状态变化。

准备后仍由用户确认同一质量事务计划，`execute_quality_transaction` 单次提交。原生请求只设置 `resetInspectionLevel=true`，不在客户端计算一个默认水平再写入 `newInspectionLevel`。

## 配置与状态保护

- 继续核对固定Active产品、完整选择集合、状态类型8426、计划/测试及当前归属。拒绝批采样、全选、不完整记录或不符的非执行回显。
- 绑定原SamplingPlan完整快照，以及计划根和同测试明细中已配置的AQL/检验水平候选表。完整有界读取，检验表每类最多50行，总依赖最多100个，超限拒绝，不用子集准备。
- 候选表只作为变更依赖，**不推断根配置与Spec/设备/供应商明细的生效优先级**。不解释公式，不执行样本量查表或标准判定；公式保留为原始配置，不因能读取就宣称其有效。
- 没有返回可确认的候选引用时，不伪造默认表或默认水平；实际默认如何解析由MES执行原生复位规则，可能在执行时被MES拒绝。
- 准备末尾重读产品、所选状态和关联配置；执行前再次重建摘要。默认表本身或其ChangeCount发生变化，也会阻止旧计划提交。
- 非执行回显必须与原选择、产品、完整评论及复位true一致。不能将缺失/false的复位当成功，也不能替调用者补上未请求的计数器或命名水平。

工具不发送频率修改字段，不修改抽样计划定义，不回填已在制任务。该事务针对后续符合采样条件的容器；最终水平、切换行为和后续频率影响仍待现场验收，不能承诺复位后下一容器一定检验或免检。

## 输出和未知结果

复位计划增加以下预览字段：

| 字段 | 值及含义 |
|---|---|
| `reset_inspection_level_requested` | true，明确请求原生复位 |
| `effective_default_level` | null，没有客户端推算的有效默认水平 |
| `resolved_default_level_verified` | false，默认解析未通过现场验收 |
| `reset_necessity_verified` | false，不据本地快照断言一定需要或一定无需复位 |
| `switching_rule_effects_verified` | false，未运行或验证MES切换规则 |
| `standard_compliance_verified` | false，不作抽样标准判定 |

沿用原事务的单次尝试、unknown保留占用和关联配置互斥。`inspect_quality_transaction` 仍只读原选择的当前状态，即使它看起来与默认相同也不是本次复位成功证据，不自动解锁或重试。最终审计仍需人工证据；没有扩展自动历史因果核对。

## 证据与测试

现场再次核对正式 `CurrentSamplingStatusUpdate.ResetInspectionLevel`：说明为将所选当前状态的检验水平恢复为Sample Plan定义。无目标、`execute=false` 的复位true探测返回HTTP200，并且未补计数器或命名水平。该探测没有选择实际状态记录，不是正目标预览或业务验收。

2026-10-28采样状态表TOP1仍返回空。没有创建测试业务记录，也没有提交MES复位或其他业务写入。

新增37项测试覆盖原生复位载荷、严格类型、互斥、保留计数器、候选配置依赖变化、多记录选择、旧参数兼容和MCP协议下准备/执行/检查流程。业务响应使用模拟数据；问法测试验证工具可见性及恢复意图识别，不是真实LLM多轮成功率测评。

全量2023项测试、2项子测试通过，其中质量相关817项，父pytest离线HTTPX真实网络尝试0。全量结果见[本轮摘要](quality-fixtures/sampling-level-reset-summary.json)，接口依据见[现场契约](quality-fixtures/sampling-level-reset-contracts.json)。
