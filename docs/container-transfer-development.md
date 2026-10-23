# T07 拆合批与父子关联开发记录

## 当前交付（2026-10-23）

四个独立的只读意图预览已注册到 container 能力组和能力列表，聊天路由支持拆批、合批、装箱、拆箱关键词。

| 工具 | 范围 |
|---|---|
| preview_container_split | 明确来源、新容器名及数量；展示 reduceQty/closeWhenEmpty 效果，不生成系统历史字段 |
| preview_container_combine | 单来源到现有目标的明确数量；比较产品、层级、单位、工序，显示条件余额 |
| preview_container_associate | 明确1..10个子容器，显示当前和预期父关系；已有父关系/直接关系环提示阻塞 |
| preview_container_disassociate | 明确1..10个直属子容器；disassociateAll固定false，不隐式拆全部 |

使用现有 ViewContainerStatus 和 current_family 追溯查询核对身份与当前状态。输出保留原始状态、未知字段和阻塞项，所有工具 complete=false、executable=false，没有 Shopfloor 写入。数量仅为条件计算，不是服务认可的变化；第二数量、单位、深层关系环、容量/层级及事务兼容规则尚待核对。

## 契约发现

[实时元数据](container-transfer-metadata.json) 与归档四份 Swagger 分别确认服务和字段。Split.toContainerDetails 的说明明确为系统生成历史，用户不应设置；Split 顶层也没有普通 Qty 输入。因此当前未将 qty 拼到该字段，也没有用 sampleQty 替代。实际数量拆批应继续核对服务事件/受支持客户端请求方式。

Combine.fromContainerDetails 是必填列表，包含 fromContainer、qty/qty2、combineAllQty、closeWhenEmpty；顶层 closeWhenEmpty 与明细项可能交互，写入前必须验证。Associate/Disassociate 使用 childContainers；父子关联不能等同数量合并/拆分。

## 验证

27项相关测试通过（预览、能力发现与Agent安全）；覆盖非法数量、未知单位、重复/自关联、已有父关系、非完整关系、中文路由等。现场只读验证 SN0000001：Qty=1，示例拆出0.25时条件余额0.75；T07-PREVIEW-ONLY仅为意图名称，没有创建容器。见 [证据](container-transfer-preview-validation.json)。

## 后续开发

1. 验证数量拆批的实际输入路径及新名称存在性；不能仅凭系统历史结构构造请求。
2. 增加完整单位/第二数量、层级/产品/工序/状态兼容性和深层祖先环校验。查询受限必须保留未知。
3. 对已确认契约的操作增加服务不执行预览、不可变单次计划、确认展示及提交前重读。
4. 执行与按事务核实必须区分当前状态和历史证据，覆盖部分失败、超时和重试；不提供自动回滚。
5. 所有真实事务测试按用户要求统一延期，见集中验收清单。T07当前仅完成预览层，不标为执行能力或阶段完成。


## 第二步：可选服务回显预览（2026-10-23）

Combine / Associate / Disassociate 三个现有预览工具新增 verify_service（默认 false）。开启后只调用固定服务路径，强制 execute=false，不调用 PerformEvent，不执行事务，不重试；只发送契约定义的用户输入字段。Split 未开放此参数，继续禁止猜测数量输入。

合批核对目标/来源名称和ID、唯一明细、数量、combineAllQty=false及顶层/明细closeWhenEmpty；父子操作核对全部子容器名称和ID且无重复，拆箱额外要求回显disassociateAll=false。服务错误、漏项、错误身份、数量/标志差异或分页不完整均返回server_echo_verified=false。回显成功只改变stage=server_preview，complete/executable仍为false，保留业务规则阻塞项，不生成执行计划。

37项相关模拟测试通过，覆盖三个固定路径、execute=false、默认不请求服务、错误/重复/不完整回显。本轮未对现场发起预览或写入；三类非空现场回显尚待集中验收。后续仍需深层关系与兼容规则验证后再生成可执行计划。
