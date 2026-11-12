# C6：非空采集验收与计数控制图

同日后续：用户授权原测试图切换为Inline SPC和RunTimeDisplay(2)，保存并复核通过；原始采集值保持。该图仍缺类型、查询和连接，原生出图未完成，见[最新显示修正](c6-native-chart-display.md)。下文NoChartNever与未重载等描述保留首次验收时刻，不代表当前显示模式。

2026-11-12，Asia/Shanghai。本轮继续用户已选的“不依赖现场引擎”范围，完成真实隔离采集历史到本地判异的验收，并新增p、np、c、u计数图。**C6整体仍未完成：原生引擎/编译规则/控制限及自动处置未接通；注释正式提交另被实际接口缺失签名回显阻塞。**

## 本轮交付

- `analyze_spc_attributes` 为新增只读MCP工具，支持p/np/c/u，已注册分发、能力发现、按需加载和Agent问法。原有i_mr/xbar_r接口不变。
- `analyze_spc_history` 已使用四笔真实隔离CollectData历史，核对原始点、父事务、固定定义、单位、生产范围、区间完整和未排除状态，读回预先声明的独立测试基线并识别预期信号。
- 修复关联投影兼容：未展开的SPC组引用可能显示null，改由已验证的持久外键定位真实组；非空模型引用仍必须与外键一致。
- 泛型SPC组的继承字段可能返回null。此时核对实际 `NamedObjectGroup` 的ID、名称、CDOTypeId=8198、版本和冻结状态并复读；不将null当作未冻结，不跳过组成员核对。
- 注释准备继续严格要求签名信息。实际响应省略 `ESigRequirement` 和 `AdditionalESigReqs` 时，明确指出缺失字段并停止，不生成可执行计划。

## 计数图使用约定

每个观测明确 `observation_id`、连续递增 `sequence`、非负整数 `count` 和正数字符串 `inspected_units`。必须提供同特性、同单位、同过程、数据来源及顺序依据的context，不自动分组、换算、补点、排序或剔除异常。

| 图型 | 明确计数口径 | 检查单位 | 绘图统计量与方差 |
| --- | --- | --- | --- |
| p | `nonconforming_units`，不合格品件数 | 每点正整数样本量n，允许变化；count≤n | count/n；方差p(1−p)/n |
| np | `nonconforming_units`，不合格品件数 | 全部基线/评估段固定整数n；count≤n | count；方差np(1−p) |
| c | `nonconformities`，缺陷个数 | 全部样本固定检查单位n；缺陷数可超过n | count；均值及方差均为n×rate |
| u | `nonconformities`，缺陷个数 | 每点明确检查单位n，允许变化或正小数 | count/n；方差rate/n |

计数口径不能混用。同一件产品上的多个缺陷不能自动当成多个不合格品件数。c/u的面积、长度或检查单位必须使用同一个context单位，不能拼接不同单位。

控制来源二选一：

1. 独立 `baseline`（至少2点）加 `baseline_reference`，与评估段ID不重叠、序号相接。rate采用基线总计数/总检查单位，p/u不会对各点比例做不加权平均。基线信号单列保留，不删点、不重估控制限。
2. 明确的有版本 `controls={reference,version,rate,...}`。p/np的rate为不合格率，要求0<rate<1；c/u为每检查单位的缺陷率，要求rate>0。np/c另须明确固定 `inspected_units`；p/u按各点检查单位计算，不在controls中另外设置固定单位。

必须显式 `rule="beyond_3sigma"`，当前只逐点判异。以精确有理数比较 `(统计量−中心)² > 9×方差`，等于边界不触发；Decimal平方根仅用于64位有效数字显示。下限截于0，p上限截于1、np上限截于样本量。拒绝全零或全不良等退化基线。

采用对称三西格玛近似限，**不宣称精确二项/泊松概率限**。逐点列出期望计数偏小的观测，独立性、分布、过度离散、合理样本与近似适用性仍需审核。无信号不能证明过程稳定或产品合格。不会把本地计算填成operator_reported报告，不生成原生登记/冻结/放行计划。

公式依据：[NIST p图](https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc332.htm)、[NIST缺陷计数图](https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc331.htm)、[NIST np图](https://www.itl.nist.gov/div898/software/dataplot/refman1/ch2/np_cont.pdf)。u图的每点样本量与控制限接口参照[qcc作者文档](https://luca-scr.github.io/qcc/reference/stats.u.html)，率的方差由同一泊松计数除以检查单位推导。本实现独立计算，不依赖qcc安装，也不模拟现场SPC引擎。

## 隔离现场结果

前缀 `CDX-C6-20261112`，共8个新建模型和1个显式Container，9个根对象均读回核实，没有认领已有同名对象。模型为独立Operation、UserDataCollectionDef、Spec、Workflow、Product、MfgOrder、注释测试SPCChartDef和SPCChartDefGroup。只读引用既有BOX/EACH基础配置。

容器 `CDX-C6-20261112-SN`，1 EACH，Level=BOX；工单 `…-ORDER` 数量1，Product=`…-PRODUCT/1`。Start前核对工单固定产品/版本/容量、名称冲突及唯一选择值Owner=PRODUCTION、StartReason=NORMAL，autoNumber=false。创建1、已存在/跳过0、失败0、未尝试0。没有自动MoveIn或MoveOut。

采集期间定义没有SPC图组，四次均通过原生非执行预览、专用单次执行器及完整历史核实，之后才关联注释测试组。图使用已核实的原生NoChartNever=3，无图类型、查询、连接或编译规则，不冒充现场可运行图。关联后禁止验收脚本继续采集，避免调用InlineSPC。

| 原生采集事务 | 原值 | 用途 |
| --- | --- | --- |
| `4000000000010770` | 9 | 独立测试基线第1点 |
| `4000000000010771` | 10 | 独立测试基线第2点 |
| `4000000000010772` | 11 | 独立测试基线第3点 |
| `4000000000010773` | 14 | 评估段第1点 |

实际i_mr本地结果：中心10，基线平均移动极差1，主图控制限约7.34043至12.65957；原始历史点 `001c6b8000000012` 的值14触发上侧三西格玛信号。三个基线点只是已声明的合成测试样本，不是充分或已批准的生产过程基线。

最终只读核对原值仍为9/10/11/14；容器Active、数量1 EACH，未扣量或移站。注释历史为0，没有正式注释或违例提交。四笔采集计划均为 `verified_success`，原计划不可重放。

关联调试中，第一次未展开导航属性导致误判“未保存”，曾多发一次同值的隔离配置更新。随后SQL和显式展开证明持久组引用已存在，原始点未变；该请求与原始日志保留，不能称为零额外维护。修正后的脚本使用显式展开及持久外键复核，没有保留“空投影即无效果”的重试入口。

原始manifest、逐请求持久日志和现场原响应位于本地 `tmp/c6-acceptance/manifest.json`、`manifest.journal.jsonl`。可复用脚本为[spc_isolated_acceptance.py](../scripts/spc_isolated_acceptance.py)，每笔变更提交前落盘且不重放。便携非空回放为[采集历史](../tests/fixtures/spc_nonempty_history.json)和[实际注释预览](../tests/fixtures/spc_annotation_preview.json)，不含环境URL或凭据。

## 注释仍未验收的具体原因

实际 `AddSPCAnnotation?execute=false` 能完整回显原点、父历史、原图、注释、Exclude=false及派生定义版本，但没有返回 `ESigRequirement` 和 `AdditionalESigReqs`。显式 `$select` 及 `$expand` 复核仍然缺失。不能据此确认无签名要求，准备在执行计划生成前停止。

需要服务端为原对象上下文返回可核验的签名要求，或提供经核实的等价原生查询契约，再继续原注释准备、确认单次提交和完整历史结案。没有关闭签名检查、填假空值、直接改库或改走通用事务绕过。现有测试数据继续保留；不要重复Start或重放四笔采集。

## 验证与下一步

本轮相关离线回归896项通过，1项旧CAPA测试仍因本地归档 `capa-preview-echo.json` 缺失而显式排除；没有声称全项目测试全部通过。回归覆盖四种计数图的手算边界、NIST公开c图示例、变样本量加权基线、退化输入、真实MCP调用/封闭schema、只读问法、真实非空历史及签名缺失回放、SPC关联投影和共用审计入口。HTTPX真实传输尝试为0；验收脚本另有11项单独检查通过，验证清单不可变、过期拒绝、请求前落盘、错误不重放、持久图组阻断采集及未知结果不推进。两次运行分开记录，不叠加成同一次回归。现场事务记录独立于离线测试，不相互代替。

新增工具和读取修复仅落在磁盘源码，尚未重启本机8030。后续顺序为：补齐签名返回契约并验收注释/明确报告登记；取得真实引擎及图类型、规则、查询、有效控制限后，再核对原生计算与自动处置。区内/派生结果登记、批量/自定义来源、排除恢复和其他图型仍需各自契约，未自动开放。

相关记录：[C6登记历史](c6-spc-registration.md)、[MES历史与原生前置](quality-spc-history.md)、[质量交付总清单](quality-completion-plan.md)。
