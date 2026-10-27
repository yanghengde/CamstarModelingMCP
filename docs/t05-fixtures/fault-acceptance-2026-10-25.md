# T05 故障与MCP客户端隔离集成

日期：2026-10-25（Asia/Shanghai）。本轮新增17项测试，完成本机进程与客户端协议层的故障验证。子进程、OS文件锁、SQLite事务及MCP客户端真实运行；MES状态/历史基于D归档样本，预览/完成响应由固定测试传输生成，不代表真实MES响应丢失、服务端并发或故障验收通过。未创建MES对象、提交MES事务、重启运行服务或调用LLM。

## 本轮覆盖

| 用例 | 验证内容 |
|---|---|
| 活跃执行进程 | 执行保持in_flight时，恢复/核实不能抢占；另两个进程分别提交同计划/同容器新计划，均拒绝，执行传输入口只有一次 |
| 进程被强制终止 | 父进程kill真实执行子进程，跳过finally，attempt及in_flight仍持久化；OS释放锁后恢复仅转unknown，原计划及同容器新计划仍禁止执行 |
| 成功响应后崩溃 | 响应已交付而本地结果尚未保存时os._exit(23)；恢复后凭四表证据核实，未重发投料 |
| 响应丢失与历史不完整（4项） | 生产HTTP调度器接收RemoteProtocolError不重试POST；空历史、重复记录、403和实际明细晚到均不解除占用；随后完整历史才能核实，保留原unknown结果 |
| 最终保存失败 | SQLite触发器拒绝最终状态保存，整个事务回滚，attempt.finished仍空；移除测试触发器后恢复/核实，未重复执行。此为SQLite失败注入，不是物理磁盘故障 |
| 严格数量（5项） | MCP拒绝true、false、数字字符串、零、负数，均未进入preview或创建计划库 |
| 独立stdio客户端 | 新Python进程运行已注册MCP服务器，SDK真实初始化、发现工具、拒绝非法数量、准备、单次执行、晚到历史核实、查询终态及拒绝重放 |
| 认证HTTP客户端 | 通过应用实际MCP挂载和Bearer中间件；无凭证401且无计划库，认证后完整准备/执行/核实/回读；错误摘要及已用计划不执行 |
| 部署隔离（2项） | 不同环境不能执行归档实验，也不能自动核实归档计划；不发送预览/核实请求或解除占用 |

## 修复

MCP原qty: float会把数字字符串转换为浮点数，绕过直接函数中的JSON数类型限制。现使用严格、有限且大于0的数值声明；直接调用仍执行原业务检查，不接受非有限值。

首轮全量为932通过、14失败、2项子测试通过，HTTPX真实网络尝试0。14个旧测试失败原因是当前.env环境与归档B/C/D样本环境不同；并非上述故障集成失败。离线fixture现显式绑定样本环境，另加跨部署拒绝测试。生产环境摘要绑定、现场脚本环境检查和历史样本均未放宽或修改；未修改.env。首轮[日志](fault-initial-test-output.txt)、[JUnit](fault-initial-junit.xml)、[网络记录](fault-initial-network.json)原样保留。

受影响的三个旧测试文件修复后，67项专项回归通过。最终全量948项测试、2项子测试通过，失败/错误/跳过均0；父pytest HTTPX真实网络尝试0，git diff --check通过。见[日志](fault-final-test-output.txt)、[JUnit](fault-final-junit.xml)、[网络记录](fault-final-network.json)、[机器汇总](fault-final-summary.json)。

## 运行与边界

```powershell
$env:CORE_TEST_NETWORK_REPORT = "$PWD/docs/t05-fixtures/fault-final-network.json"
$faultTemp = "$PWD/data/fault-final-$([guid]::NewGuid().ToString('N'))"
.\venv\Scripts\python.exe -m pytest tests -p scripts.offline_test_guard -q --basetemp=$faultTemp --junitxml=docs/t05-fixtures/fault-final-junit.xml
```

测试入口为tests/test_component_issue_fault_integration.py，子进程夹具为scripts/component_issue_fault_harness.py；请求固定走HTTPX MockTransport，默认同步/异步HTTPX网络传输均禁止。父pytest的网络报告不统计子进程；子进程自行安装同类传输阻断。它不是OS网络沙箱，也不是现场服务。

直接MCP客户端仍须提供与Agent同等确认流程。此处测试程序按预定隔离场景调用工具，不证明聊天端人工确认/取消交互已验收。当前.env地址已不同于归档样本，后续现场测试必须在当前部署重新核对契约、身份和测试数据，不可重放旧计划。

剩余：真实MES响应丢失/并发/进程故障联合验收、其他现场版本和聊天界面；模式1–4、来源库存、退料净额/替代料/第二数量；T09部署主机上传目录仍未修复。
