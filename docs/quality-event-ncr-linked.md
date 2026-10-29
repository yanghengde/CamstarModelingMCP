# 事件关闭与制造NCR原生联动

2026-10-29扩展现有 `prepare_quality_event_closure`，质量工具数仍为65，不新增执行入口。本轮实现标准制造NCR事件数据下的父/子关闭准备与证据核对，真实复合事务和最终历史尚待MES验收。

## 参数与执行顺序

必须同时明确 `allow_complex_context=true` 与 `allow_native_ncr_closure=true`，其余参数仍是原事件、组织、质量解决代码、非空关闭说明和可选原ID/备注。任一选项省略/false时，原有事件数据阻断保持不变。不能用该入口修改事件数据、批次、处置量或NCR关联。

准备读取父事件及全部受限关联，核实原生父关闭execute=false回显并重读。展示原NCR、原容器、批次/处置覆盖、同名父子解决代码和未核实项，通过现有execute_quality_transaction确认后，**只发送一次CloseQualityObject**。原生内部调用NCRCloseQuality，工具不额外发送第二次子关闭，不发ChangeQty，不自动补处置量、报废、解冻或放行。

## 开放条件

- 父事件仍须基础Event7489、Active、已提交/分流、未冻结，并满足现有CAR、流程、已批准审批和无签名要求的约束。仅支持Nonconformance类别3。
- EventData必须标准类型8085，父EventId及持久ID与原事件一致，无EventCustomData。NonconformanceNumber须为明确无额外空白的原NCR名称，不按近似名寻找替代对象。
- 必须唯一关联原NCR，NCR保持Open、未记录关闭时间、失败码完整，原事件/容器/变更计数均与关联一致。原容器须满足既有Active容器检查，不跨容器联动。
- 原生CloseMfgNCR会按父质量解决代码名称解析NCR解决代码；因此两类代码必须都存在、名称完全一致、未冻结且版本可核实。只找到父质量代码不等于子代码可用。
- 组织关闭事务7712及子事务8081均不得要求当前工具未采集的电子签名。原生子服务虽设置ProcessESignatures=false，本工具不据此绕过组织子签名策略。

## 批次与处置

EventData.EventLots完整查询最多20条，必须都是原NCR同一容器的容器批。每批完整处置关联及子表最多50条，父级、FieldId=21122、处置ID、明细ID和顺序必须有效，关联集合与子表精确相符，无重复或孤立记录。

每批数量和每条处置数量须为有限正十进制数，不超过1e12、最多28位小数；批处置合计必须恰好等于原批数量。使用十进制计算，无浮点容差，不把欠处置、超额或空清单当作已完成。此核对只证明持久记录的量覆盖，**不证明处置动作已经执行、流程完成或容器可放行**。MES仍执行原生最终验证，可能拒绝未完成流程或处置状态；不修改这些状态以求通过。

准备后的重读及执行前重建同时绑定事件数据、原NCR及失败码、容器、批次、全部处置和两类代码/签名策略。任何相关变化均使原计划失效。unknown保留原事件和容器依赖占用，与独立NCR关闭互斥。

## 未知结果检查

inspect_quality_transaction固定原事件ID，并额外读取原NCR ID和原容器。原NCR后来改名、改关联或关闭时，仍只给原记录证据，不追随新关联。verified_child_success、verified_success和retry_allowed均不据当前状态自动变true。父/子历史因果链、是否共同提交或回滚，以及最终状态需人工审计；尚未认证原生复合事务原子性。

## 原生证据与限制

[事件数据及列](quality-fixtures/event-ncr-linked-native-index.json)、[批次/处置关联](quality-fixtures/event-ncr-linked-native-relations.json)、[NCR子服务类型](quality-fixtures/event-ncr-linked-native-child.json)提供标准字段依据。原生父关闭的CloseMfgNCR及ValidateDispositions见[关闭逻辑](quality-fixtures/event-close-native-execution.json)和[执行分支](quality-fixtures/event-close-plan-native.json)。标准MDB SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`，不证明当前部署自定义一致。

测试与交付见[汇总](quality-fixtures/event-ncr-linked-summary.json)、[针对性JUnit](quality-fixtures/event-ncr-linked-focused-junit.xml)、[完整JUnit](quality-fixtures/event-ncr-linked-junit.xml)、[离线HTTPX守卫](quality-fixtures/event-ncr-linked-network.json)。模拟接口及临时计划库不等于现场验收，守卫不是操作系统级网络隔离。

本轮没有真实父关闭、子关闭、扣量、通知或服务重启。跨容器/非容器批、自定义事件数据、检查表执行、未完成审批和电子签名仍未开放；实际父子结果、数量/质量控制副作用及历史链仍待现场验收。

完整性补充：查询必须明确返回EventData所选全部字段，尤其EventCustomDataId缺失不能当作null；读取不完整时拒绝准备，不进行父或子提交。

最终新增46项测试；全量3129项测试、2项子测试通过，其中质量相关1923项，父pytest真实HTTPX网络尝试0。[工具参数核对](quality-fixtures/event-ncr-linked-manifest.json)确认65个质量工具均已注册并可分发，新增选项默认false，参数模型封闭。较早针对性回归297项通过，随后补充的数量精度和缺字段边界均由最终全量覆盖。

[本轮现场元数据只读检查](quality-fixtures/event-ncr-linked-live-readcheck.json)未返回可用JSON；未选择真实事件预览或提交业务，不据此推断数据为空或权限可用。
