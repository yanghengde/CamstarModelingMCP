# 草稿分类核验与正式提交保护

2026-10-30。本轮完成范围是既有Pending草稿的分类诊断、拟选组合比较，以及保存/正式提交中的分类保持和配置复核。**修改既有草稿分类、子分类的原生写入尚未实现**；原定修改目标保留在统一清单中，不能用只读核验替代完成。

## 原生发现与写入依赖

标准模型的UpdateEvent（7578）虽然在REST定义中有classification、subClassification引用字段，但原生字段定义为：

| 原生字段 | FieldID | AllowClientToUpdate | 当前值表达式 |
|---|---|---|---|
| Classification | 14001 | false | QualityObject.Classification |
| SubClassification | 14003 | false | QualityObject.SubClassification |

EventDetailToEvent保存映射不包含分类，UpdateEventToUpdateEventHistory也仅映射Submit。已有明细接口UpdateEventDetails及UpdateEventData的Classification仍只读；其SubClassification虽标为可更新，却具有原对象当前值表达式，且UpdateEventDataToEvent实际保存映射不包含分类/子分类。因此不能把一个ref属性、可写标志或成功回显当成持久修改依据。

CreateEventToEvent确实映射分类，但CreateEvent_StartModifyingEntity仅在QualityObject为空、创建新对象时执行该映射。不能据此覆盖既有同名草稿，亦不能自动创建另一条事件替代原草稿。

另对标准MDB全部以Classification/SubClassification为目标的字段映射作了[反向检索](quality-fixtures/event-classification-global-target-maps.json)：写入Event（7489）的分类映射仅CreateEventToEvent；其余为CAPA创建、服务间传递或历史映射。此结果仍不代替实际部署自定义CLF/服务的核实。

只读证据：[UpdateEvent及基础模型](quality-fixtures/event-classification-native-models.json)、[字段映射](quality-fixtures/event-classification-native-maps.json)、[创建/验证CLF](quality-fixtures/event-classification-native-flows.json)、[独立明细字段](quality-fixtures/event-classification-alternative-fields.json)、[独立明细流程](quality-fixtures/event-classification-alternative-flows.json)、[独立明细保存](quality-fixtures/event-classification-alternative-modify.json)。来源标准MDB的SHA256为`7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。只读检索未修改模型；这些证据不代表实际部署契约或真实执行验收。

分类实际修改需补充已核实的专用原生服务，或正式部署扩展契约、权限/签名/副作用及历史验收。本轮没有执行开关、用户参数或环境变量可以绕过此限制；不使用通用HTTP、任意PerformEvent、直接改库或CreateEvent覆盖。新建另一条正确分类事件只可作为用户明确授权的独立创建需求。

待接入的原生服务必须提供以下可审阅依据，之后才开发实际写入适配：

1. 实际部署的业务CDO、固定端点、输入类型及CLF/字段保存映射，证明会修改原Event持久分类，而非只改临时明细/回显。既有Event ID、原组织和原版本必须可绑定。
2. 原生当前员工、允许动作、组织/角色、签名与审批要求，以及分类对、EventData、检查表、归属/通知的副作用规则。不能以改分类隐式创建替代事件或覆盖复杂上下文。
3. 明确预览是否会设置内部执行标志，以及实际执行的并发版本条件、失败回滚和一次性提交行为。保持原ID、Pending/未提交状态及未授权字段，未知结果不得重试。
4. 原生最终事务与旧/新分类历史字段、父子事务关联、实际员工和本次标记；现场正向、错误组合、权限/签名失败、并发及超时场景。仅HTTP200或当前分类同值不满足验收。

## 已实现的读取与组合核验

复用原工具，不新增质量工具；当前质量组仍75个工具。

`get_quality_event_update_context(event_name, organization_name, purpose="classify_pending_draft", event_instance_id=可选原ID)`读取原草稿分类、子分类配置、原组织完整映射和组合阻塞。可同时传`classification_name`、`subclassification_name`比较明确拟选组合；两个名称必须同时明确、唯一匹配，不能选首行，也不能单改分类后猜子分类。

默认purpose仍为`update_text`。拟选名称只允许在`classify_pending_draft`只读用途出现，不得混入文字保存、正式提交或审批修订用途。既有`EventDraftChanges`仍拒绝分类参数。无plan_id、无修改或签名收集，`ready_for_execution=false`、`ready_for_submission=false`、`classification_write_supported=false`。拟选组合有效也不会改变原分类或使缺失原分类的草稿可提交。

参考配置采用显式安全字段选择；SQL读取原ID和ChangeCount，再独立复读。组织映射使用标准EventClassificationSpecMap（8001）的真实外键**OrganizationId**，不猜成ParentId。完整读取最多100条；第101条、重复ID、归属不符、未知字段/类型/布尔值/版本、非标准派生类型或不完整schema均拒绝部分核验。

映射快照包括原组织、分类/子分类、Owner、Role、ChecklistTemplate、FailureModeGroup、PEDefault、冻结标志和版本；不修改映射、归属或检查表。只匹配原组织的准确分类/子分类对，重复组合不允许通过指定映射ID绕过。

本受限正式提交入口保守拒绝冻结的所选配置/映射；IsFrozen本身主要说明配置维护限制，不能由此推断所有原生事务均禁止使用。诊断会展示原冻结值及本工具限制，组合有效也不表示原生动态权限或签名已完成。

## 对原业务链的实际影响

1. 草稿保存仍允许分类缺失或组合未通过的草稿修改其它已支持资料；展示分类阻塞原因，分类原值不变。保存不能补分类、使组合生效或自动正式提交。
2. 原生固定Load必须返回只读Classification/SubClassification，并与原SQL持久值一致；原值为空时只能保持空，不接纳派生默认分类。正式提交的非执行回显亦须保持原分类；请求不携带分类写入字段。
3. 正式提交准备强制要求原分类/子分类存在、配置可核实、原组织组合唯一并在受限范围内。失败时不生成提交计划，不进入提交预览，更不会执行业务。
4. 准备及执行前重建完整分类配置/映射摘要；分类或子分类版本、映射版本/默认、冻结、组合、原对象或回显变化使旧计划失效。旧阶段ready计划因快照范围变化须重新准备。
5. 分类配置引用加入共享资源读取占用；原对象unknown、配置相关未核实事务及最终人工审计机制继续生效，不能切换工具绕过。保存结果最终审计后，才独立准备和确认正式提交。

保留[草稿保存](quality-event-draft-editing.md)和[正式提交](quality-event-submission.md)原有单次执行、未知结果、签名/AutoTriage/通知及首次分流边界。Submit=false仍仅固定Load加静态差异，禁止普通POST假预览；组织配置无效时也不能绕过此保护试写分类。

本地占用仅协调本工具的计划。外部客户端在最后重读后的变更仍依赖MES最终校验；尚未核实服务端的原子版本条件，不能声称解决跨客户端并发或保证配置在提交瞬间绝对未变。

## 问法与验收

| 用户问法 | 处理 |
|---|---|
| 查看事件E草稿分类是否匹配组织 | classify_pending_draft读取原组合及阻塞，无计划 |
| 比较改为分类A、子分类B后的组合 | 两个明确名称只读比较，原值不变，不宣称可修改 |
| 请修改草稿分类或补齐子分类 | 说明原生写入尚未核实；保留诊断入口，执行器不自动暴露，不准备替代保存 |
| 核实分类后正式提交原草稿 | 原分类组合必须通过，独立正式提交计划及确认，保持原分类 |
| 分类缺失，先保存说明 | 仅保存明确说明，保留分类缺失及提交阻塞；不能宣称已补齐分类 |

离线验证包含原/拟选有效对、缺失/错误/重复组合、冻结配置、未知schema/字段/类型、错误组织外键、引用或映射漂移、Load/提交回显不一致；还包括保存→unknown阻止提交→隔离最终人工审计→独立正式提交保持原分类的连续模拟。诊断不产生计划，分类修改参数始终拒绝，能力目录和多轮问法如实展示限制。

现场仍未验收：实际部署只读契约、数据权限、分类规则/签名、真实保存/提交效果与原生历史。未真实编辑/提交事件、修改分类/映射、发通知、换账号、部署或重启服务。分类写入目标保留外部接口依赖；下一个独立探索范围为已有事件自定义数据的结构及受限资料维护，不能把分类修改标成完成。

验证记录见[本轮汇总](quality-fixtures/event-classification-summary.json)。全项目3849项及2项子测试通过，定向363项通过，两次HTTPX真实网络尝试均0；计数重叠不相加。实际分类修改仍为未完成接口依赖，现场未验收。
