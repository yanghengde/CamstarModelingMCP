# C4隔离现场验收方案

状态为待批准。名称和源配置已只读核对，本方案尚未创建或修改业务数据。完整机器清单见[c4-isolated-acceptance-plan.json](c4-isolated-acceptance-plan.json)。

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

上述实际报废、损失、返工、冻结/释放和新建事务需要一次明确批准。当前创建阈值为20，本方案29个根对象超过该阈值。
