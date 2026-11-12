# C6：SPC登记历史核实与本地结案

后续更新：四笔隔离采集到本地判异已完成真实非空验收，新增p/np/c/u计数图；注释准备被实际缺失签名回显拦截。以[本轮最新记录](c6-spc-nonempty-counts.md)为准，下文保留前轮证据。

2026-11-12，Asia/Shanghai。用户要求开始C6，并选择“暂无引擎，先完成不依赖引擎的开发与验证”。本轮交付是受限注释、明确报告的单点违例登记到完整历史核对及确认结案的代码链路。**代码及离线验证完成，真实登记尚未现场验收；C6整体仍未完成。**

## 入口与出口

沿用 `prepare_spc_annotation`、`prepare_spc_violation` 和 `execute_quality_transaction`。明确原始采集历史点、原容器、图以及注释或报告内容；准备和执行前重读，系统确认后只提交一次。结果先进入 `unknown`，失败或超时不重复登记。

新计划保存 `spc_evidence_version=1`。执行尝试后：

1. `find_quality_execution_candidates(plan_id, plan_digest)` 按完整原请求评论在全局主历史分页查找，不按目标点或服务过滤掉冲突标记。候选页不声称目标关联已核实。
2. `reconcile_quality_execution(plan_id, plan_digest)` 自动查唯一候选，也可明确 `candidate_txn_id`。核对完整登记、原点和既有历史；仅返回证据，不改变本地状态或解除占用。
3. 用户要求结案后，经确认调用 `review_quality_transaction(action="resolve_history", conclusion="committed", evidence_reference=原生TxnId, ...)`。再次核对全部证据，在一个本地数据库事务中写审计并置为 `history_committed`。

`in_flight` 先确认原进程停止并执行本地 `recover`，不能跳过恢复直接结案。结案后原计划仍不可重放。没有记录、候选重复、字段不符、当前证据变化或历史读取失败均保留占用；不依据HTTP状态或空历史推断未提交。

旧快照没有新版证据契约，继续 `inspect_quality_transaction` 和人工最终证据审核；不回填版本或借升级重开旧计划。

## 核对范围

所有字段先按当前部署核对，SQL使用固定表列、明确事务ID及有界读取。主历史全局唯一标记、原生服务类型、未冲销状态及本地实际尝试时间窗口必须匹配。

| 登记 | 必须完整核对 |
| --- | --- |
| 注释 | 一个主历史和一个 `AddSPCAnnotationHistory`；真实父键、原历史点、固定定义、图、完整原文、`Exclude=false`；没有外部或自定义来源 |
| 明确报告违例 | 一个主历史、一个 `RecordSPCViolationHistory`、一个 `SPCChartHistoryDetail` 和一个 `SPCViolationHistoryDetail`；全部真实父键及同一事务，原点、容器、图/变量、规则/违例名、原值、绘图值、报告控制限和空副作用引用 |

按事务读取全部五类SPC历史表，出现额外主历史、明细、图、原点或其他登记类型时拒绝用子集结案。当前原点的历史再与上述事务原记录逐字段交叉核对。

原始采集值及完整原父链保持；图、固定定义、SPC组及所选规则版本保持；原有历史逐条存在且内容不变；任何图的有效排除记录都会阻断结案。准备、查询和审核期间均不隐藏冲突、不跟随同名替代对象。读取不提供MES跨请求原子快照，最终本地落库前仍重复核对证据。

注释历史查询改用保留孤立父记录的 `LEFT JOIN`。无父主历史的记录会进入校验并明确失败，不能因内连接隐藏排除或损坏记录。

`history_committed` 仅证明本范围的原生登记和原始历史保持。输出及审计明确保留 `native_engine_result_verified=false`、`native_control_limits_verified=false`、`reported_result_provenance_verified=false`、`release_authorized=false`。登记调用方报告不等于认证报告来源或运行现场引擎。

## 现场只读复核

[本轮证据](quality-fixtures/c6-registration-readonly-20261112.json)记录11次受只读保护的MES请求，均返回200；业务写入和引擎调用均为0。

- SPC图、图类型、规则、查询和连接集合均为空。不能据此推断服务器是否安装了引擎软件，也不能臆造连接地址、编译规则或控制限。
- 注释、违例和内联SPC三份实际接口定义可读，核对了47个登记历史列。接口可见和列存在不等于执行权限或业务效果验收。
- 直接数值历史探测读到3条：2条缺少本入口要求的容器身份，1条 `ActualMinutes` 满足原点身份条件。同范围尚不足独立历史基线及评估段，没有将零散历史拼接或临时编造固定控制限。
- 未修改MES配置、账号权限、业务对象或原始采集值；未重启本机8030加载新代码。

可重复执行只读检查：

```powershell
.\venv\Scripts\python.exe -X utf8 scripts/spc_acceptance_readonly.py --output tmp/c6-readonly.json
```

## 验证及遗留项

相关离线回归817项通过，1项旧CAPA用例因已有归档 `docs/quality-fixtures/capa-preview-echo.json` 缺失而显式排除。首轮选定回归为640项通过及该1项缺失归档失败，最终扩大范围后按上述排除项运行；没有补造旧现场响应，也没有将本轮称为全项目回归通过。

新增C6专项64项，覆盖实际MCP调用、正常历史匹配及确认结案、同标记跨事务、缺失/额外/串错历史、错误字段/父键/时间、旧历史丢失或被改写、有效排除、读取期间变化、旧快照、中断恢复、数据库审计保存失败回滚，以及结案后仍拒绝原计划重放。专项已包含在817项中，不叠加计数。最终回归还覆盖原SPC算法/来源读取、原生检验/关联事件及损失的共用审计入口、能力发现和确认策略；离线HTTPX真实传输尝试为0。

详细结果位于本地 `docs/quality-fixtures/c6-registration-final-test.txt`、`c6-registration-final-junit.xml` 和 `c6-registration-final-network.json`。

后续按以下依赖继续，不将代码验证代替现场通过：

1. **原生引擎接入**：确认实际引擎部署、版本及受控连接；确认测试图、图类型、查询、编译规则及生效控制限来源/版本。
2. **非空样本验收**：准备明确隔离的原始数值序列、单位、合理子组及独立基线，验证原始历史到本地判异。注释及明确报告违例须另外验收一次提交和完整历史结案。
3. **原生计算与处置**：分别核实引擎返回、规则版本、有效控制限和权限/签名；通知、冻结、设备状态及OCAP的自动处置另做明确场景，现有仅登记入口不绕过门禁。
4. **剩余能力**：更多图型、区内规则或派生结果登记、批量/自定义来源、排除与恢复等仍按各自契约开发，未因本轮结案入口放开。

源实现：[历史核对](../tools/quality_spc_evidence.py)、[共用历史入口](../tools/quality_history.py)、[共用最终审计](../tools/quality_transaction_audit.py)。此前能力见[SPC历史与前置配置](quality-spc-history.md)、[本地计算](quality-spc-analysis.md)、[受限注释](quality-spc-annotations.md)和[明确报告违例](quality-spc-violations.md)。
