# SPC本地计算与判异首版

2026-10-29。新增 `analyze_spc_series`，该阶段quality分组为51个工具；后续新增[原始历史点受限注释](quality-spc-annotations.md)，SPC扩展阶段55个工具；当前61个工具，另有[事件文字资料维护](quality-event-update.md)，新增[基础Event首次分流](quality-event-triage.md)。另有[明确报告的单点SPC违例登记](quality-spc-violations.md)。已接入MCP、Agent分发、能力目录、分组加载和自然语言路由。

这是高级功能第四阶段的计算部分：结果来自调用方明确提供的数据及配置，不是Camstar现场SPC引擎的返回值。本地分析工具不执行违例/注释写入、自动冻结、资源状态变更、通知或质量放行；独立的受限注释计划见上述后续文档。代码交付和离线测试不代表MES业务验收。

## 输入与执行顺序

1. 明确同一质量特性、单位和过程范围；提供数据来源和顺序依据。不要将不同产品/工序/单位的数据拼接。
2. 选择 `chart_type`：`i_mr` 每条观测一个单值，`xbar_r` 每个合理子组固定2..10个原始值。调用方必须定义合理子组，不自动按数量切组。
3. `points` 是待评估段，1..500条；每条包含唯一 `observation_id`、连续递增整数 `sequence` 和十进制字符串 `values`。不排序、不补点、不剔除异常、不自动单位换算。
4. 控制限来源二选一：独立 `baseline` + `baseline_reference`，或明确的有版本 `controls`。不使用待评估段重新估计其控制限，不接受规格限字段。
5. 显式选择 `rules`。查看逐窗口信号、基线信号和未满足最小窗口的规则；保留 `input_snapshot`、`input_digest` 和算法版本供复核。

`context` 必填：`characteristic`、`unit`、`process_scope`、`data_reference`、`order_reference`。这些是调用方声明，工具不从MES验证其真实性、完整性、统计独立性或分布假设。

### 独立基线

`baseline` 有2..500条观测，形状与 `points` 一致、子组大小固定，ID不得与待评估段重复，基线末尾sequence须与评估段相接。不连续的已批准历史基线应转为明确的固定控制配置，不拼接成连续数据。

2条只是计算最低要求，不是过程稳定性或基线样本量充分的标准。估计限始终需要人工审核；零变差拒绝计算，不返回“全通过”。基线按同一规则展示信号，有信号不剔除数据、不重算控制限。`has_signals`只描述 `points`，基线信号另见 `baseline.has_signals`；两段不跨边界计算移动极差或运行规则。

### 有版本的固定配置

`controls` 全部字段必填：

| 字段 | 含义 |
|---|---|
| reference / version | 明确的控制配置来源与版本；不是模型临时猜定的数值 |
| subgroup_size | i_mr为1；xbar_r为2..10，须与每条数据一致 |
| center | 单值图或子组均值图的中心线 |
| sigma | **绘制统计量**的标准差；xbar_r是子组均值的标准差，不能直接填单件标准差 |
| dispersion_center / dispersion_lcl / dispersion_ucl | MR或R图的固定中心和控制限；须0≤LCL≤中心≤UCL，LCL<UCL |

主图使用中心±3sigma；不提供非对称主图控制限、变子组量、加权或自适应控制限。固定配置不自动从平均MR或R反推中心/sigma，也不证明现场配置已被读取、批准或仍然有效。`baseline_reference`不可与固定配置混传。

## 计算及规则版本

算法 `spc-local-v1`，规则 `weco-four-strict-v1`。数值须为十进制字符串，绝对值≤1e12，最多12位小数；拒绝指数、NaN、无穷、空值、布尔、数值类型和超过范围值。

计算和边界比较使用精确有理数；输出的循环小数最多64位有效数字，仅用于展示。输入摘要覆盖原始顺序、ID、数值、上下文、基线/固定配置和规则；这是内容摘要，不是执行授权或MES历史证明。

- i_mr：基线均值作中心，连续两点绝对差的平均值作MR中心；sigma=平均MR/1.128，MR限为0和3.267×平均MR。基线和待评估段分别计算连续差，不虚构跨段MR。
- xbar_r：基线子组均值的平均值作中心，平均子组极差作R中心；均值图半宽=A2×平均R，绘制统计量sigma=半宽/3，R限=D3/D4×平均R。固定2..10件系数使用NIST公布的小数精度；不冒充无限精度统计常数。
- 对主图按选择的规则逐个完整滚动窗口判异；多个重叠窗口都报告，保留窗口起止ID、方向和贡献观测ID。不是只给本次末点的一个合格标志。

| rules中的名称 | 窗口与判定 |
|---|---|
| beyond_3sigma | 1点超出同侧3sigma |
| two_of_three_2sigma | 连续3点中至少2点超出同侧2sigma |
| four_of_five_1sigma | 连续5点中至少4点超出同侧1sigma |
| eight_same_side | 连续8点严格位于中心线同一侧 |

等于区域/控制限边界不触发该规则，等于中心线中断同侧连续；上下两侧分别计数。规则无默认选择，名称不能与Nelson等其他规则集混用。MR/R只逐点检查其上下控制限，不将重叠移动极差当独立正态值套用WECO。运行窗口不足显式给出evaluated=0；无信号不证明过程稳定、产品合格或允许放行。

公式与规则依据：[NIST单值图](https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc322.htm)、[NIST均值/极差图及系数表](https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc321.htm)、[NIST WECO规则和控制限/规格限区别](https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc32.htm)。

## 完整调用示例

下面是合成演示值，固定控制配置需由调用方实际确认，不能直接当作生产配置：

```json
{
  "chart_type": "i_mr",
  "points": [
    {"observation_id": "DEMO-101", "sequence": 101, "values": ["10.1"]},
    {"observation_id": "DEMO-102", "sequence": 102, "values": ["10.4"]}
  ],
  "context": {
    "characteristic": "Diameter",
    "unit": "mm",
    "process_scope": "Synthetic example only",
    "data_reference": "demo-data-v1",
    "order_reference": "demo acquisition sequence"
  },
  "rules": ["beyond_3sigma", "eight_same_side"],
  "controls": {
    "reference": "synthetic-control-example",
    "version": "demo-v1",
    "subgroup_size": 1,
    "center": "10",
    "sigma": "0.1",
    "dispersion_center": "0.1128",
    "dispersion_lcl": "0",
    "dispersion_ucl": "0.3685176"
  }
}
```

预期：DEMO-102触发上侧3sigma规则；8点同侧规则没有完整窗口；没有MES写入、冻结或放行授权。独立基线用法和NIST示例结果见 [spc-examples.json](quality-fixtures/spc-examples.json)。

## 原生接口调查与后续写入边界

既有现场Swagger归档确有 [RecordSPCViolation](quality-fixtures/RecordSPCViolation-schema.json) 和 [AddSPCAnnotation](quality-fixtures/AddSPCAnnotation-schema.json)。本轮使用官方Designer元数据组件只读解析标准csi MDB，源SHA256：`7bef30314ab7f05106f0b02b1a1af9075f1bc2f59c9452d2573ff477b9138c0c`。这是标准快照，不证明部署自定义覆盖：

- `RecordSPCViolation`包含 `RecordSPCViolationDetail → Violations → SPCViolationDetail`，不能把本地结果直接塞到根层。原始DataPoint或CustomSPCDataId需要真实关联，原始观测ID不自动等于这些持久ID。
- 违例与注释校验要求 `DataPoint` 或 `CustomSPCDataId`；注释还要求非空annotation。任意填入外部ID并不证明已有记录的来源。
- `RecordSPCViolation_FinishModifyingEntity`在配置满足时发送邮件，不能把“记录违例”当纯数据库插入。
- `InlineSPC_Execute`调用外部SPC引擎、解析原始DataId，记录违例后可能冻结容器、改变资源状态。不能用它代替单纯本地计算或直接做写入探针。

证据：[原生CLF目录](quality-fixtures/spc-native-index.json)、[原生执行/校验表达式](quality-fixtures/spc-native-execution.json)、[本轮现场只读检查](quality-fixtures/spc-live-readcheck.json)。本轮现场只读SPC控制和图定义查询均连接失败（ConnectError），记录数未知，未验证当前配置或空表。既有历史证据不能代作本轮现场联调成功。本轮没有创建配置、发送业务事务、调用通知或重启服务。

后续已为原始直接数值采集点增加独立的受限注释准备与证据读取，实际现场效果尚未验收；明确报告的单点越限结果已新增独立登记计划；自动现场引擎/编译规则认证、区内/派生结果和部署效果仍待开发/验收。当前 `ready_for_mes_write`、`native_spc_result`、`release_authorized`始终false，不产生可执行计划，不能用通用HTTP绕过。

## 验证证据

针对性测试覆盖NIST公开单值示例、可手算均值/R图、支持的子组大小、双侧四规则、精确边界、窗口不足、MR重叠、独立基线、零变差、非法数据/顺序/配置、摘要、MCP真实调用及多轮问法路由。详见 [test_quality_spc.py](../tests/test_quality_spc.py)。

本轮新增97项SPC测试；针对性135项通过，全量2436项及2项子测试单次运行全部通过，其中质量专项1230项通过。测试进程真实HTTPX尝试为0。注册/参数及8组问法的确定性工具选择见 [工具核对清单](quality-fixtures/spc-tool-manifest.json)；不将路由测试称为模型多轮业务验收。

离线测试报告及本轮汇总见 [spc-summary.json](quality-fixtures/spc-summary.json)、[全量JUnit](quality-fixtures/spc-junit.xml)、[网络尝试记录](quality-fixtures/spc-network.json)。离线守卫阻断测试进程的真实HTTPX传输，子进程故障测试有自己的守卫；不是操作系统级网络隔离。
