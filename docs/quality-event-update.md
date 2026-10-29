# 质量事件文字资料维护：受限首版

2026-10-29。新增 `get_quality_event_update_context`、`prepare_quality_event_update`，该阶段quality分组为59个工具；后续新增[关闭前置诊断](quality-event-closure.md)，当前68个工具。在[首次分流](quality-event-triage.md)之后，提供原生 `UpdateEvent` 的简述、说明和发现区域维护。代码、参数、路由和模拟执行已接入；真实MES验收尚未完成。

## 工具及参数

| 工具 | 用途 |
|---|---|
| get_quality_event_update_context | 核对原事件、当前接口契约、原生Edit权限和完整受支持原明细；不保存计划 |
| prepare_quality_event_update | 仅替换用户明确指定的文字字段，准备不可变计划，复用现有确认执行器 |

两个工具都需要明确 `event_name`、`organization_name`；可提供 `event_instance_id` 绑定原对象。准备工具另有：

| 参数 | 行为及上限 |
|---|---|
| brief_description | 简述，必须非空，最多100个UTF-16代码单元 |
| description | 说明，最多4000个UTF-16代码单元；显式空字符串清空 |
| discovery_area | 发现区域，最多30个UTF-16代码单元；显式空字符串清空 |
| comments | 本次事务备注，最多200个UTF-16代码单元；附在本地生成的请求关联标记后 |

前三个字段省略或传null表示保持原值，至少有一项实际变化。禁止NUL字符，不截断文本、不补默认说明。工具不开放报告人、发现人、日期、优先级、类别、归属、流程、签名或任意服务明细参数。

## 问法与执行顺序

1. “查看组织 Org 下事件 E 的文字维护前置条件”：调用 `get_quality_event_update_context`，核对名称、原ID、原资料及保持值。
2. “仅把说明改为已复测，其他保持”：调用 `prepare_quality_event_update(event_name="E", organization_name="Org", description="已复测")`。说明要清空时才明确传 `description=""`。
3. 展示同一计划的原值、变更、保持值和完整请求，通过 `execute_quality_transaction(plan_id, plan_digest)` 的现有系统确认后单次提交。
4. 执行后结果先为unknown，保留原事件和关联NCR容器占用。`inspect_quality_transaction`只读原ID和最多20条质量历史；最终事务证据由 `review_quality_transaction`人工审阅。HTTP200、当前文字相同或空历史均不能证明本次事务成功，不自动重试或清库解锁。

“继续”只沿用最近明确领域，不等于执行授权。“把说明清空”可继承质量事件上下文并显示执行入口；“查看如何清空说明”仍按查询处理。工具选择测试检查可见性和说明，不代表真实模型多轮业务成功率。

## 当前支持范围

仅支持已提交、Active=1、未冻结的基础Event（CDOTypeId=7489），原责任人、角色、优先级和报告人须有有效ID。允许已首次分流事件，但保持原TriageComplete及CAR策略。

原事件不得有流程、原流程模板、检查表、已保存检查表、关闭时间、自定义EventData或直接审批表、法规报告。固定Load返回的批次、交叉引用明细必须明确为空；不使用部分清单。最多核对20条关联NCR并绑定其原事件、容器、变更计数。

组织必须没有UpdateEvent事务7578的签名要求；其他事务的签名清单不被误当成更新签名。原生返回须有Edit动作位2、UserIsOwner=true、UserHasRole=true，审批/额外签名上下文明确为空，IgnoreProcessModel=false。无法核实则拒绝，不将系统确认当作电子签名。

## 保持原明细的实现依据

不能只向UpdateEvent传新的说明：EventDetail的Reporter默认表达式会使用当前User，省略原值可能改变报告人。实现先读取持久文字、原人员/组织引用和本地/GMT时间，再调用原生固定 `Load` 读取EventDetail（7541）。只有与持久记录逐字段一致的已支持业务明细才能进入计划。

保留原报告人、发现人及组织、优先级、发生/报告时间；时间保持原精度，包括7位小数。本地与GMT必须表示同一时刻，不猜现场时区。原值为null时只接受原生明确null，不接受读取时派生的新默认值。丢弃瞬态服务ID、Parent、HistoryDetail和SmartBarcode，不将这些字段回填为业务输入。事件明细若出现尚未支持的扩展字段则拒绝，避免遗漏未知字段。

固定Load通过 `POST /api/UpdateEvent/PerformEvent` 的 `event.eventName="Load"`、`inputData` 信封调用。运行时必须先读取当前Swagger并核实该输入模型，**不为PerformEvent假设execute=false行为**，也不允许调用方指定其他事件。标准CLF中的此Load构建临时明细；部署自定义仍需现场核实。

准备时对 `POST /api/UpdateEvent?execute=false` 核对完整输入回显，随后重新读取源记录和第二次独立Load，必须仍是原值。执行前通过统一执行器再次重建并比较快照；源资料、权限、签名、配置、引用或关联变化均使原计划失效。

`Submit`始终为false。标准FinishModifyEntity仅在Submit=true时调用SubmitEvent；这里的false不表示撤回原已提交事件，也不支持提交草稿。已有流程、审批、检查表和复杂关联在当前入口拒绝，不清空配置以求通过。

## 原生证据及验证边界

依据包括[原生字段](quality-fixtures/event-update-native-fields.json)、[默认值与明细类型](quality-fixtures/event-update-native-defaults.json)、[Load/校验/保存逻辑](quality-fixtures/event-update-native-execution.json)、[关联字段和表列](quality-fixtures/event-update-native-links.json)及[审批/检查表Load分支](quality-fixtures/event-update-native-load-guards.json)。通用Event字段另见[列证据](quality-fixtures/event-triage-native-columns.json)。标准csi MDB源SHA256为 `7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`，不是部署自定义一致性证明。

已有Swagger归档保留UpdateEvent根及EventDetail字段，未保留该服务完整PerformEvent包装模型。离线测试使用归档根和通用框架信封构造明确的模拟响应，不将它冒充现场UpdateEvent契约。运行时必须实际取得当前契约，否则不能准备更新。

[本轮现场只读检查](quality-fixtures/event-update-live-readcheck.json)未取得有效表列和当前Swagger；没有选择真实目标调用Load或提交业务，没有创建MES测试对象、发送通知或重启服务。不能由读取失败推断现场表为空或权限可用。

新增94项测试；全量2865项测试、2项子测试通过，其中质量相关1659项，父pytest进程真实HTTPX网络尝试为0。核对59个质量工具均已注册、可分发，两个新增参数模型拒绝额外字段；8组单轮/追问夹具检查确定性工具可见性。实际质量计划库只读检查仍为2条cancelled，没有新增现场计划或执行记录。

测试结果见[本轮汇总](quality-fixtures/event-update-summary.json)、[全量JUnit](quality-fixtures/event-update-junit.xml)、[新增测试](quality-fixtures/event-update-new-junit.xml)、[离线HTTPX守卫](quality-fixtures/event-update-network.json)及[工具/参数/问法核对](quality-fixtures/event-update-tool-manifest.json)。守卫覆盖父pytest进程的HTTPX传输，故障子进程有自己的守卫，不是操作系统级网络隔离。

真实验收仍需核实部署自定义、当前固定Load信封及非执行回显，选择明确事件审阅计划后核对单次更新、未改报告人/时间/其他明细、最终历史和关联事务。事件创建、更多字段更新、开启/关闭、重新分派、复杂审批与电子签名仍见[后续开发计划](quality-advanced-roadmap.md)，本轮不表示全部质量功能完成。
