# C6：通过MCP写入SPC建模测试资料

2026-11-12，Asia/Shanghai。用户在Modeling中看到七类SPC入口并授权写入测试数据。已通过FastMCP客户端调用实际注册工具，创建并核实六类独立测试资料。**这些是建模示例，不是可运行的原生SPC配置或实际违例历史。**

## 页面查找

在对应的Modeling页签刷新实例列表，搜索`CDX-C6-MODEL-T1`：

| 页面 | 测试名称 | 实际持久ID |
| --- | --- | --- |
| SPC Chart Type | `CDX-C6-MODEL-T1-TYPE` | `4880b08000000003` |
| SPC Violation | `CDX-C6-MODEL-T1-VIOLATION` | `0022818000000003` |
| SPC Query | `CDX-C6-MODEL-T1-QUERY` | `4880408000000002` |
| SPC Rules | `CDX-C6-MODEL-T1-RULES` | `4880af8000000002` |
| SPC Chart | `CDX-C6-MODEL-T1-CHART` | `0020048000000003` |
| SPC Chart Group | `CDX-C6-MODEL-T1-GROUP` | `0020068000000003` |

已核实图组包含该图，图引用该类型、查询和规则，规则引用该违例定义。SPC Violation页面中的对象是**违例定义**，不是新发生的生产违例或RecordSPCViolation历史。

查询保存四行合成SQL：SampleNo为1/2/3/4，TestValue为9/10/11/14。SQL只有常量SELECT与UNION ALL，不读写生产表；本轮只保存查询文本，没有将这些常量另行写入采集历史。类型和规则是未配置参数、未编译的示例骨架，不将其称为原生I-MR或三西格玛规则。

图设为NoChartNever=3、图片保存Never=3；TriggerOCAPs、RequireAnnotation、RunTimeDisplay和RecordAgainstCustomSPCDataSrc均为false。没有设置冻结、资源状态、通知或引擎连接，也没有把图组挂到生产数据采集定义。未创建容器、采集新测量或执行原生计算、注释、违例登记和处置。

SPC Connection没有创建：原生接口要求DSN、用户名和密码，而用户确认没有可用引擎/安装资源，当前也没有真实连接资料。本次不填虚构引擎地址或把测试凭据当成可用连接。

## MCP工具与执行记录

新增并注册：

- `prepare_spc_modeling_test(prefix)`：只读核对现场六类Swagger及固定字段、同名冲突、创建阈值和名称长度，生成环境/摘要绑定的一小时计划。所有名称最长30字符。
- `execute_spc_modeling_test(plan_id, plan_digest, continue_unattempted=false)`：用户授权后逐项创建；每笔POST之前提交SQLite尝试记录，保存原响应，随后核实原字段及引用。默认不允许再次执行已尝试计划。
- `inspect_spc_modeling_test(plan_id, plan_digest)`：只读检查，不修改状态、补建、覆盖或解除未知结果。

本轮使用`scripts/spc_modeling_test_acceptance.py`中的`Client(mcp)`进行MCP工具列表发现与协议调用，未直接调用创建函数冒充MCP结果。没有重启8030；新增三个入口须在该服务重新加载注册后才能通过其长驻进程使用，本地MCP客户端已实际加载并执行。此前8030九个C6入口的加载验证仍是前一轮证据。

首次计划使用`CDX-C6-MODEL-20261112-A`。图类型已创建，但违例定义名称超长，服务返回SQL截断错误；只读核实SPCViolation.SPCRuleViolationName实际上限为30，Swagger没有声明maxLength。已补充前检。该首次计划未重放，另一个已创建的测试图类型`CDX-C6-MODEL-20261112-A-TYPE`（`4880b08000000002`）保留，其他五类原名称均未读到对象。

短前缀计划创建到规则、图时分别因扩展投影将标量返回null而停止读回核实；原生保存响应均为2xx。修正为独立读取标量与展开关联；部分继承说明字段额外以固定表、原ID和原名核实存储值，没有为修正显示再次PUT或POST原对象。

显式`continue_unattempted=true`仅允许在**此前每一笔尝试都具有持久2xx无错误响应，并且当前全部请求字段核实通过**之后，继续尚未尝试的后续对象。无响应、非2xx、响应含错误、请求变更、引用或字段不一致均拒绝继续；不重发任何已尝试的POST。跨计划/同前缀也有持久占用，不能靠重建计划绕过。短前缀六类最终状态为verified，每类只有一次POST。

原计划与原响应保存在本地`data/spc_modeling_tests.sqlite3`。MCP准备、执行、只读复核回执及日志在`tmp/c6-modeling-test/`：最终执行回执为`short-manifest.complete-group.json`，独立复核为`short-manifest.inspect.json`。首次失败及两次投影核实中断的原始记录均保留。

本轮共新增七个测试建模根对象（短前缀六个，首次前缀一个），另有一次名称截断拒绝；不把初次尝试描述成六个对象一次无误创建。没有修改已有生产对象或重放此前C6隔离采集。

## 验证口径

新增测试覆盖固定六对象范围、名称限制、计划摘要和环境、注册与写入策略、POST前持久记账、同前缀跨计划防重、部分拒绝停止、无响应/错误禁止继续、只继续未尝试后缀、独立标量与关联投影，以及继承说明的真实身份和值核对。当前22项专项通过，未访问真实网络；现场MCP建模保存与读回单独记录，不用离线替身代替实际保存。

当前模型测试不解除原生引擎、有效控制限、编译规则、签名契约、实际登记与自动处置的剩余工作；C6整体仍未完成。统一状态见[C6收尾记录](c6-completion-status.md)。

最终相关回归342项通过、1项因缺少既有`phase2-contracts.json`归档跳过，包含22项新增专项；不是全项目回归。HTTPX真实传输由离线守卫禁止。证据为本地`tmp/c6-modeling-test/final-tests.xml`及`final-tests.log`；此前直接运行历史分类测试因缺归档出现的错误没有计入通过项。

最终独立MCP只读复核六项全部通过。另调用`get_spc_native_readiness`，确认原图的三条依赖持久ID、规则违例成员及无通知/冻结/资源处置配置；连接集合仍为空，`ready_for_native_execution=false`、`compiled_rules_verified=false`、`native_control_limits_verified=false`。证据为`tmp/c6-modeling-test/readiness-after-modeling.json`。SQLite尝试记录为首次批次2笔（1笔2xx），短前缀批次6笔（全部2xx），后者没有重复POST。
