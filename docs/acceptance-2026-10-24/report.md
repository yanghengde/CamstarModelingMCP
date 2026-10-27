# T00–T09 从头测试报告

日期：2026-10-24（Asia/Shanghai）。结论：**T00–T09尚未全部开发完成，也未全部验收通过。** 本轮完成全量自动化回归和受限现场只读检查，没有执行MES业务写入。

## 实际执行结果

- 项目venv全量pytest：**663项测试通过、2项子测试**，失败0项；本次命令退出码0，无跳过。JUnit包含663条主测试记录。离线HTTPX网络尝试为0。
- 新增现场请求保护测试：**14项通过**，覆盖禁止Shopfloor POST/建模修改/事件分发/危险SQL/外部文件访问。
- 实时Swagger：**15/15**与既有脱敏归档一致。
- 现场只读：**18个检查项、76次请求**，Shopfloor POST为0，未触发被禁止的请求。两个Query HTTP400为业务样本限制：工单未配置前/后过程、无匹配投料需求；不能算作这些业务场景验收通过。
- git diff --check通过。没有调用真实LLM执行付费聊天回放，也未进行业务事务或现场恢复/人工结论操作。

## 发现与处理

1. 系统Python首轮回归662通过、1失败：安装aiosqlite 0.22.1，违反项目requirements.txt的<0.22约束，导致checkpoint-sqlite 2.0.11调用不存在的Connection.is_alive。项目venv是0.21.0，同一检查点测试通过。未修改全局依赖或屏蔽测试。
2. 工单进度测试的宽泛resolve mock导致一次未预期的现场GET尝试，被离线拦截器阻止。已限制mock只解析工单，对无Workflow引用明确返回未知；最终全量无网络尝试。生产逻辑未因测试而放宽。
3. 原pytest临时根存在Windows访问权限问题。最终使用新的项目data内basetemp，退出码0，没有删除用户临时目录或降低测试断言。

## 阶段覆盖与限制

下表为最终JUnit中专属测试文件的记录数；共享安全/路由/MCP/持久化等其余测试另计，T00契约使用现场检查。它是测试覆盖索引，不是阶段完成计数。

| 阶段 | 专属自动化测试记录 |
|---|---:|
| T01 | 12 |
| T02 | 11 |
| T03 | 12 |
| T04 | 9 |
| T05 | 19 |
| T06 | 60 |
| T07 | 182 |
| T08 | 141 |
| T09 | 26 |

| 阶段 | 本轮现场观察/实现缺口 | 结论 |
|---|---|---|
| T00 | 15份契约一致；本地目录与注册表一致 | 契约基线通过，不代替业务验收 |
| T01 | 资源返回空列表，Spec ResourceGroup未返回/为空 | 非空资源链未验收 |
| T02 | 标准BOM引用未知，工单物料为空 | 非空物料/差异未验收 |
| T03 | 用户定义SPCTesterPageUDC/1有Thickness点；单位、限值、枚举仍未知；容器查询未给出CurrentSpec | 静态读取可用，完整约束/动态要求未验收 |
| T04 | 工单/产品工艺链读取成功；BOM/资源等未知；本轮上限2工序，另4工序未展开 | 正确保留不完整，不能认定可开工 |
| T05 | 需求返回无匹配；投料历史为空；material组无写工具 | **执行链未实现，开发未完成** |
| T06 | 采集历史为空；单次提交仅模拟；未知锁解除流程和扩展类型有缺口 | 开发/事务验收未完成 |
| T07 | 拆批意图只读返回，未建容器；四类执行仅离线模拟 | 非空兼容/容量/实际事务及精确关联未验收 |
| T08 | 工单未配置前/后过程；当前执行记录为空 | 受限基础闭环可用，默认映射/非空事务等未验收 |
| T09 | Incoming Inspection/1文档集为null，未指定业务必需，未误判缺失；没有文件访问 | **仅元数据，文件访问契约未完成** |

测试内容与后续真实验收步骤见[test-plan.md](test-plan.md)。建议先补T05执行链、T06未知结果恢复和T09文件访问契约，再用专用非空样本进行受确认的现场事务验收；本轮不把已知缺口改成完成标记。

## 重跑与证据

使用项目venv：

```powershell
$env:CORE_TEST_NETWORK_REPORT = "$PWD/docs/acceptance-2026-10-24/offline-final-network.json"
$acceptanceTemp = "$PWD/data/acceptance-temp-$([guid]::NewGuid().ToString('N'))"
.\venv\Scripts\python.exe -m pytest tests -p scripts.offline_test_guard -q --basetemp=$acceptanceTemp --junitxml=docs/acceptance-2026-10-24/offline-final-junit.xml
.\venv\Scripts\python.exe scripts/acceptance_readonly.py --output docs/acceptance-2026-10-24
```

以后全量tests会包含新增的14项保护测试，计数将增加；不要把历史首轮计数当作固定预期。现场脚本只读，不创建缺失样本。

- [最终离线日志](offline-final-output.txt)、[JUnit](offline-final-junit.xml)、[网络检查](offline-final-network.json)
- [保护测试JUnit](readonly-gate-junit.xml)、[现场只读证据](readonly.json)、[现场日志](readonly-output.txt)
- [机器可读汇总](summary.json)
