# C6：原测试图的Camstar显示配置修正

2026-11-12，Asia/Shanghai。用户明确要求修改Camstar SPC Real Time Monitoring中`CDX-C6-20261112`这一组测试配置，使原生页面可以展示图形。本轮通过实际MCP工具修正显示配置，**原生出图仍未完成**，没有用聊天图片替代此次原生目标。

## 实际修改

目标为`CDX-C6-20261112-CHART`，持久ID `0020048000000002`。执行前备份原图完整GET、持久配置和原四笔历史；不修改原始值或生产关联。

| 原生字段 | 修改前 | 修改后 |
| --- | --- | --- |
| IsNewSPC（Inline SPC） | false | true |
| SPCChartDisplayOption | 3：NoChartNever | 2：RunTimeDisplay |
| RunTimeDisplay | false | true |
| SPCChartSaveOptions | 未显式设置 | 3：Never，仅展示，不写图片文件 |

显示选项值由现场`SPCChartDefs/RequestSelectionValues`返回，完整三项为DisplayOnlyOnException=1、NoChartNever=3、RunTimeDisplay=2，不凭名称猜数字。PUT固定包含原名称、原ChartVariable=TestValue及上述四字段，现场Swagger校验通过。

实际MCP调用为`get_spc_test_chart_display`及`update_spc_test_chart_display(before_digest)`。写前绑定环境、原图、持久版本与原四笔历史摘要，再次复读；SQLite FULL同步记录尝试后仅发一次PUT。响应2xx且无错误字段，保存后核对四个显示字段、原全部引用、其余非审计字段和原始点保持，状态为verified。独立MCP只读复核再次通过。

原值仍为9/10/11/14，原采集点与父链未改。TriggerOCAPs、RequireAnnotation保持false，通知、冻结和资源状态配置仍未设置；未调用SPCTxn、InlineSPCTxn、采集、注释或违例登记。没有修改Portal全局设置或安装组件，也未重启8030。新工具由独立本地MCP客户端加载，长驻8030使用新增入口仍需重新加载注册。

## 尚缺的原生出图配置

对该原图的最新实际读取仍显示：SPCChartTypeId、SPCQueryId、SPCRulesId、SPCConnectionId全部为空，现场SPCConnection集合为空。前次新增的T1建模骨架没有自动挂到本图。显示选项修正不验证图类型参数、数据查询、有效控制限或计算服务。

原生查询、正确图类型及参数、可用数据库连接/计算服务仍需补齐；按需要配置经核实的行业规则。没有真实DSN、连接账号、计算服务地址和对应原生契约时，不填占位连接宣称就绪。本轮`ready_for_native_execution=false`、`native_chart_verified=false`。

门户页面从独立HTTP客户端读取时返回会话未建立，未取得用户已登录会话的服务端错误栈；因此没有把截图中的空引用错误归因到某一具体代码行，也没有声称Generate Chart(s)已成功。本机官方手册2510+的SPC Charts配置要求（纸面3-17起）及实时监控页说明（4-15/4-16）用于核对字段和前置，现场契约仍以实际API为准。

## 证据与回归

- 持久尝试、原配置备份和修改后快照：本地`data/spc_chart_display.sqlite3`。
- MCP修改前、单次保存和修改后独立回执：`tmp/c6-native-chart-repair/mcp-display-before.json`、`mcp-display-update.json`、`mcp-display-after.json`。
- 实际依赖与未就绪结论：同目录`native-readiness-after.json`；显示候选原响应为`display-selection.json`。
- 10项新增专项通过；最终相关回归110通过、1项因既有`phase2-contracts.json`缺失跳过，日志/JUnit为同目录`tests-final.log`和`tests-final.xml`。离线守卫禁用真实HTTPX传输，现场保存和原生出图验收分别记录。

修改工具只针对上述固定测试ID；结果未知不重发，也不能靠换摘要绕过已有尝试。原始C6创建及注释测试日志保留其当时NoChartNever配置，不得当作当前模式说明。统一状态见[C6收尾记录](c6-completion-status.md)。
