# C4隔离现场验收方案

用户批准后，已于 2026-11-04 完成下列隔离验收。共创建并读回 18 项建模、8 个 Container 和 3 个 SamplingLot。开工失败、跳过及未尝试均为 0。对象清单见[c4-isolated-acceptance-plan.json](c4-isolated-acceptance-plan.json)。

| 场景 | MES 实际结果 | 最终事务 |
|---|---|---|
| LR 整批返工 | 批结果 2。R01、R02 到达 Repair，均处于返工，数量各为 1 EACH。两条原成员采样任务已清理。 | `400000000000fe20`，一条批历史、两条成员 Rework 历史及对应 MoveHistory |
| LS 整批报废 | 批结果 3。S01、S02 各扣 1 EACH，均归零关闭。两条原成员采样任务已清理。 | `400000000000fe22`，一条批历史、两条成员 ChangeQty 历史及对应损失明细 |
| D01 双单位部分损失 | 扣 2 EACH、1 UNIT，剩 8 EACH、4 UNIT，容器仍 Active。 | `400000000000fe23` |
| A01 单单位全量损失 | 扣 2 EACH，归零关闭。 | `400000000000fe24` |
| LF 冻结成员失败 | 冻结 F02 后，旧计划在写入前被拦截。原生整批返工返回 `13500440`，明确指出 F02 已冻结。失败前后批头、两名成员、检验任务和成员历史一致，失败请求无最终父历史。F02 已释放。 | 冻结 `400000000000fe25`，释放 `400000000000fe26` |

损失历史确认 SCRAP 原因、扣量方向、AdjustThruput 和 CountsAgainstProduction。LF 覆盖冻结成员导致的原生返工失败，报废分支的独立失败注入不在本次验证矩阵内。正向写入使用正式计划构建器和原生接口，旧计划拦截走统一执行入口。事务归属按持久 ID、关联标记及历史核对。

SamplingPlan 引用了现有 `IQC_DEMO_LVL` 和 `IQC_DEMO_AQL`，两项配置均为只读引用。开工通过 `Details.SamplingLot` 选择明确的批。三个批各完成两次单件拒收，现场任务累计为已检 2、拒收 2、MES 阈值字段为 1。返工测试路径的结束步骤调整为 Inspect，与返回步骤一致。

原始请求、读回、失败证据和最终历史保存在本地 `docs/quality-fixtures/c4-live-acceptance-journal.json`。开工流程依据 [camstar-container-start](../.codex/skills/camstar-container-start/SKILL.md)。

现场兼容性修正后的全项目回归通过 4424 项测试和 2 项子测试。随后含新增路径回归的质量及能力清单测试通过 3011 项，禁用真实 HTTPX 传输期间网络尝试为 0。日志保存在本地 `docs/quality-fixtures/c4-onsite-final-test.txt` 和 `c4-onsite-guarded-test.txt`。

## 新增对象与上下文

统一前缀 `CDX-C4-20261104`，共29个根业务对象，另包含各根对象内的流程步骤、路径及计划明细。

| 类别 | 数量 | 范围 |
|---|---:|---|
| 建模配置 | 18 | 损失组、返工原因/组、两个工序、两个Spec、Workflow、SampleTest、SamplingPlan、三个Product、三个MfgOrder、测试HoldReason/ReleaseReason |
| Container | 8 | 下表列出的明确名称及数量 |
| SamplingLot | 3 | 前缀加LR、LS、LF，各2 EACH，产品为前缀加P，版本1 |

Container Level为BOX，Owner为PRODUCTION，StartReason为NORMAL。后二者在源测试Start上下文中均返回唯一候选。主单位EACH，第二单位UNIT，均已确认持久ID。ProductType为BOX。Factory沿已核实源测试上下文未配置的状态，新增对象上下文须在Start前再次预检。

| Container后缀 | 数量 | Product/MfgOrder后缀 | 用途 |
|---|---|---|---|
| R01、R02 | 各1 EACH | P版本1 / MO | 原生整批返工及清理/去向历史核实 |
| S01、S02 | 各1 EACH | P版本1 / MO | 原生整批报废及归零/关闭/历史核实 |
| F01、F02 | 各1 EACH | P版本1 / MO | 受控负向条件及原子性核实 |
| D01 | 10 EACH、5 UNIT | Q版本1 / QMO | 双单位部分损失 |
| A01 | 2 EACH | T版本1 / TMO | 单单位全量损失及关闭 |

MfgOrder MO数量6，QMO数量10，TMO数量2。Product P绑定隔离SamplingPlan和Workflow，Q绑定隔离Workflow及第二单位UNIT，T绑定隔离Workflow且无抽样计划/第二单位。使用已核实既有LossReason SCRAP，其持久ID为00102c8000000002；新损失组同时用于测试拒收原因组。各对象完整名称均列于JSON清单，现有同名冲突检查为0。

## 实际事务范围与验收

1. 新建上述隔离配置。复读并检查引用、版本和原生字段后，使用明确Container名称、autoNumber=false创建8个独立Container。每次Start前核对MfgOrder/Product/Revision、数量、Level、Owner、StartReason和名称冲突。
2. 通过已核实的原生归集/任务生成流程建立三组各2成员批。组内成员必须与预定集合一致；出现额外批、成员或未知写入立即停止核实。
3. 对LR执行真实原生批返工，核对全部原成员去向、数量、采样任务清理及父子历史。
4. 对LS执行真实原生批报废，核对全部原成员归零/关闭、原生生产数量默认值、任务清理及父子历史。
5. 对LF安排受控负向条件，核对客户端前置拦截及原生失败事务后的成员/批状态；只有真实失败后的完整历史和状态足以支持回滚结论时，才勾选原子性验收。
6. 对D01验证明确主/第二数量增量，对A01验证全量和关闭，核对最终数量、生产数量及历史。
7. unknown结果先查询原计划与原对象，不盲重试，不删除事务库。所有预期结果与异常恢复达到标准后，才将C4标为现场验收完成。

上述实际报废、损失、返工、冻结、释放和新建事务已取得一次明确批准。本方案的 29 个根对象超过创建阈值 20。
