# EventData收到日期、报告日期维护

2026-10-30。本轮复用get_quality_event_data_context和prepare_quality_event_data_update，增加标准EventData日期维护及保存后只读证据检查。没有新增工具，质量组仍77个。代码和隔离验证不代表实际部署已更新或MES业务已验收。

## 字段含义与原生依据

| changes字段 | 原生扩展资料字段 | 不改写的原Event字段 |
|---|---|---|
| date_received | EventData.DateReceived / DateReceivedGMT，收到日期对 | Event.OccurrenceDate / OccurrenceDateGMT |
| report_date | EventData.ReportDate / ReportDateGMT，扩展报告日期对 | Event.ReportedDate / ReportedDateGMT |

原生UpdateEventData_ModifyDetails先处理根ReportedDate/OccurrenceDate的换算，再将EventDataDetails的28项值直接映射到原EventData。已核实四个扩展日期字段均在保存映射中，未观察到该ModifyDetails路径替它们自动执行Local/GMT换算；不能把根报告时间的换算误用于扩展ReportDate。证据见[日期映射与修改流程](quality-fixtures/event-data-dates-native-evidence.json)。

输入表达一个明确时刻，Local使用请求携带的表示，GMT按该明确偏移量推导。不推断MES服务器、工厂或用户的时区定义，不把输入偏移量或某次读取的偏移量当作服务器时区配置。物理datetime/datetime2列本身不保存时区偏移；实际REST解析、服务器换算和最终持久值仍须现场核实。[Microsoft datetime2说明](https://learn.microsoft.com/en-us/sql/t-sql/data-types/datetime2-transact-sql?view=sql-server-2017)

未找到足以规定两种扩展日期先后关系的本轮证据，因此客户端不臆定“报告必须晚于收到”或反向规则，也不把它们与原Event发生/报告时间混算。这不表示MES动态规则一定不检查日期；动态校验仍未认证。

## 输入与存储精度

changes保持封闭结构，当前允许problem_description、device_location、date_received、report_date，可在同一计划明确修改文字及日期。Python旧EventDataTextChanges名称保留为EventDataChanges别名；MCP与Agent直接JSON均校验同一结构。

- 日期必须完整到秒，并带Z或明确±HH:MM偏移；最多7位小数，按整数100ns tick计算，不使用浮点或截断第7位。
- 偏移限制为±14:00，分钟必须有效；本入口为保证偏移明确，拒绝-00:00，不规范化+01:60等错误写法。[.NET DateTimeOffset偏移范围](https://learn.microsoft.com/en-us/dotnet/api/system.datetimeoffset.-ctor?view=netframework-4.8.1)
- 省略/null均保留原日期对。日期空串、清除、直接GMT输入以及原Event日期别名均不支持，不补午夜、当前时间或默认时区。
- 原日期对两项均空时可明确填入；原Local/GMT缺一项、不一致或Load与SQL原值不符时停止，不跳过保持值检查来修复。
- 只改变格式、小数写法或偏移量而仍是同一时刻时，保持原Local/GMT值，不算日期修改。日期全部无变化则不生成保存计划；若同时有文字变化，只保存有效变化。

日期准备必须读取dbo.EventData四列的实际DATA_TYPE和DATETIME_PRECISION，完整读取四条，不使用默认精度。准备期间复读、执行前重建摘要；类型/精度改变使旧计划失效。MCP的字符串日期格式不等于数据库精度证明。

| 实际列类型 | 本入口日期输入规则 |
|---|---|
| datetime2(p)，p=0..7 | Local和GMT分别按对应列精度验证，必须精确可表示；尾部多出的零可以表示，任何需舍入/截断的非零尾数拒绝 |
| datetime，精度元数据为3 | 本受限入口只接受整秒，Local和GMT均不得早于1753年；不尝试模拟小数舍入 |
| 未支持类型、未知精度、缺列/重复列/错误归属 | 停止日期准备，不改库或转成字符串绕过 |

datetime2的物理范围为0001至9999年，精度0..7；GMT换算跨出范围也拒绝。[Microsoft datetime2说明](https://learn.microsoft.com/en-us/sql/t-sql/data-types/datetime2-transact-sql?view=sql-server-2017) datetime有特定的小数舍入行为且起始年份为1753，因此“仅整秒”是本工具主动收窄的策略，不是声称数据库只能保存整秒。[Microsoft datetime说明](https://learn.microsoft.com/en-us/sql/t-sql/data-types/datetime-transact-sql?view=sql-server-ver17)

## 工具与连续执行

1. 日期条件查询：get_quality_event_data_context(event_name, organization_name, include_date_storage=true)，显示原Event与扩展日期、date_policy和实际存储契约。此选项默认为false，旧文字只读流程不增加日期元数据依赖；只读结果执行就绪恒false。
2. 明确变化：prepare_quality_event_data_update(..., changes={"date_received":"2026-11-01T09:00:00.1234567+08:00"}, comments=明确说明)。日期准备无论此前只读选项如何，都强制验证存储契约。
3. 审阅：展示date_changes内请求表示、实际拟写Local/GMT、是否同一时刻保留、各列类型和精度。static_and_echo_verified=false，实际动态保存及服务器时区未验证。
4. 系统确认同一计划后，execute_quality_transaction单次UpdateEventData；保留原ID、未修改的28项值、原Event日期/归属/分类及禁用清单，不执行提交/关闭/分流。
5. 结果一律先unknown；inspect仅原ID证据，最终人工事务审计后才可下一次保存。400、超时、取消、日志故障和会话变化均不得重试。

继续沿用[扩展资料维护](quality-event-data-maintenance.md)的受限对象、复杂关系阻断、Edit/原责任人/组织角色核验、验证证书HTTPS及同次内存会话绑定。签名要求、任意现场自定义字段、客户/NCR评论及复杂明细仍不开放。PreValidate会强制内部执行，准备仍禁止普通execute=false POST假预览。

未消费的旧ready计划应重新准备；历史unknown计划的只读核实及人工审计仍保留。旧update_data_text动作只可验证旧文字载荷，不能借旧标签执行日期请求。

## 日期证据与原值回读

日期计划的inspect在原EventData关联仍正确且当前范围可读取时，使用已核实的固定Load及同次会话，验证SQL/Load保持值，并独立复读源状态。不会跟随替代数据ID；当前上下文不可用、读取变化或日期不一致时，返回不完整原因，仍保留原SQL和历史证据。

date_evidence比较每组Local/GMT与计划GMT是否为同一时刻，允许原生读取将输入偏移量归一为不同偏移而仍表示同一时刻。同时展示原Event映射值、根事件时间及其它扩展值是否符合计划，避免仅看到扩展日期相符就忽略其它字段差异。差异只作为当前证据，不归因是本次事务或外部修改造成。

日期值相符、其它值相符、HTTP200或出现8752/8764候选历史，均不证明本次最终事务成功。verified_success、causal_success_verified及retry_allowed仍false，不自动review/解锁。当前读到的偏移量不认证MES时区定义；本地锁也不能消除外部客户端在最后重读后的竞态。

## 离线场景与现场待验收

连续模拟覆盖：明确两种日期及不同偏移→成对保存→原Event时间保持→原生读取归一到另一偏移仍为同一时刻→SQL/Load证据→unknown阻止第二计划→隔离最终人工审计→下一次日期维护。另覆盖：

- 7位小数、跨日、年份边界、UTC溢出、±14偏移、未知偏移/错误分钟、原空值和同一时刻无效变更。
- datetime2不同精度、datetime整秒和本地/GMT双重范围检查；未知/缺失/重复元数据、准备及执行前类型精度变化。
- 禁止原始GMT/原Event日期字段覆盖、混用旧动作标签；身份/角色/源版本变化、HTTP400/超时/取消、远端错误不反射会话。
- 后端丢弃偏移量导致不一致、日期相符但根时间或其它字段不同、原数据关联被替换，以及MCP/Agent直接JSON和多轮问法。

所有业务状态、后端时区行为与人工审计均为明确隔离模拟。未真实保存日期或操作MES，未改配置/账号、部署或重启服务。现场仍须验证实际SQL类型精度、REST日期解析、根时间换算、签名/权限及最终历史，不能以本地数学或模拟替代验收。下一独立探索范围为事件处理日志追加与既有历史保护；任意现场自定义字段及分类修改的原生接口依赖仍保留。

验证记录见[本轮汇总](quality-fixtures/event-data-dates-summary.json)。最终代码定向460项通过，随后全项目4040项及2项子测试通过；两次HTTPX真实网络尝试均0，重叠计数不相加。当前为代码及离线验证完成，现场未验收。
