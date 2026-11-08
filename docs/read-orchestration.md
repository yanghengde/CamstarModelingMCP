# Camstar 共享只读查询层 v1

在原有MCP上新增4个只读入口，不另建CLI、不改变地址和认证，也不改动已有生产写入流程。

| 入口 | 用途 |
|---|---|
| search_camstar_read_tools | 跨业务组搜索当前注册的只读工具，返回真实参数及能否编排 |
| get_camstar_read_metadata | 查看数据集、API字段、受控计划schema、工具参数和已审核业务口径 |
| execute_camstar_read_plan | 一次执行有依赖关系的只读计划，内部不调用模型 |
| run_camstar_read_template | 执行可复用业务模板；首个模板为order_dates |

原始MCP工具继续可用。新增层仅编排已审核读取；不是任意脚本执行器、任意SQL代理或生产事务批量入口。

外部客户端在MCP initialize中收到统一查询指引：已知工单日期直接使用order_dates；复杂查询使用受控编排；AdHoc作为兜底。日期模板参数采用明确的order_names/timezone schema，客户端无需额外查询参数字典。初始化指引是建议，不能强制WorkBuddy的模型选择；修改后须重启服务并重新连接客户端。

## WorkBuddy直接使用

刷新服务进程及MCP连接后，可输入：

> 使用run_camstar_read_template的order_dates模板，查询CDX-P1-20261108-END2、CDX-P1-20261108-COMPLETE、CDX-P1-20261108-REPORT、CDX-C4-20261104-TMO、CDX-C4-20261104-QMO的开工日期。显示Asia/Shanghai时间，分别保留计划开工、工单原生生产开工和实际首箱开工。

实际工具参数：

~~~json
{
  "template": "order_dates",
  "parameters": {
    "order_names": ["CDX-C4-20261104-TMO"],
    "timezone": "Asia/Shanghai"
  }
}
~~~

order_dates最多20个明确工单。读取两类生产数据：工单日期、全部当前关联容器。只在分页完整后进行分组和关联，覆盖已关闭容器，保留原生GMT和指定IANA时区显示，保留最多7位小数。

输出明确区分：工单不存在、当前无关联容器、有容器但日期缺失、部分日期缺失、查询失败/未完成分页。无当前关联容器不等于历史未开工；FactoryStartDateGMT的最早记录不冒充工单ProductionStartDateGMT。历史改绑、拆合物料需另查历史。

## 通用执行器

计划是受限JSON，不包含Python、shell、原始SQL、URL、凭据或动态执行代码。示例：读取指定工单的所有状态容器，再按工单汇总数量。

~~~json
{
  "version": 1,
  "steps": [
    {
      "id": "containers",
      "op": "dataset",
      "dataset": "containers",
      "fields": ["ContainerId", "ContainerName", "MfgOrderId", "Status", "Qty", "UOMId"],
      "filters": [{"field": "MfgOrderId", "operator": "eq", "value": "000654800000009a"}],
      "limit": 100,
      "max_pages": 5
    },
    {
      "id": "counts",
      "op": "group",
      "source": {"$ref": "containers#/data"},
      "keys": ["MfgOrderId", "UOMId"],
      "metrics": {
        "container_count": {"op": "count"},
        "material_qty": {"op": "sum", "field": "Qty", "kind": "number"}
      }
    }
  ],
  "output": {"$ref": "counts#/data"},
  "concurrency": 3,
  "timeout_seconds": 60
}
~~~

此例仅当前容器余额汇总，不等于完工量或良品量；父子物料可能重复覆盖，执行器不会替调用方推断业务语义。生产报工仍应使用已有专用业务工具。

| op | 能力 | 约束 |
|---|---|---|
| dataset | 审核表的选列、eq/in/is_null筛选、排序、有限分页 | 字段须同时存在于审核目录和现场元数据，值按标量安全引用，持久主键自动保留 |
| call | 调用现有审核只读工具 | 不以read标签自动授权；不接受写入、预览、计划、任意Inquiry事件或递归编排 |
| select | 行投影/重命名 | 字段路径使用JSON Pointer；缺字段报错，不补假值 |
| group | count、min、max、sum | number使用Decimal；UTC时间显式声明；空值计数单列，不把缺失数值补零 |
| join | left/inner等值关联 | null/空键不关联，字段冲突和超过行上限拒绝 |

跨步骤引用：

~~~json
{"$ref": "orders#/data", "column": "MfgOrderId"}
~~~

表示提取orders结果的全部MfgOrderId，用于下一步in筛选。依赖从引用自动建立；不同步骤可以乱序声明，但未知步骤、自引用和循环都会在读取前拒绝。

批量复用原工具：

~~~json
{
  "version": 1,
  "steps": [{
    "id": "status",
    "op": "call",
    "tool": "get_container_details",
    "foreach": [{"name": "CDX-P1-20261108-C01"}, {"name": "CDX-P1-20261108-C04"}],
    "arguments": {"container_name": {"$item": "/name"}, "view": "status"}
  }],
  "output": {"$ref": "status#/data"}
}
~~~

参数按当前注册工具的真实JSON Schema校验；结果引用型参数在依赖解析后再校验。保留原工具内部的身份、分页和业务检查。原工具没有明确complete=true时不擅自标为完整。

## 范围与限额

v1数据集：orders/MfgOrder、containers/Container、current_status/CurrentStatus、history/HistoryMainline。

复用工具覆盖容器状态/历史/吞吐、工单/产品/Spec/Operation详情、原生生产汇总、工时与报工台账、产品工艺/工作流步骤及设备查询。以overview返回的审核清单为准；不等于所有Camstar表或工具都已接入。

- 最多12步，4并发，30次既有工具调用，foreach最多20项。
- 每页最多200行、最多5页，每个结构化数据集/本地变换最多500行；原工具沿用自身分页和总输出字符限制。超过上限明确报错。
- 默认60秒、最大120秒总时限；超时后保留unknown，不自动重试。
- 输入计划最多40000字符，输出最多120000字符。
- 默认只返回指定output与步骤证据摘要；include_intermediate=true可保留中间data，仍受总输出上限限制。
- 上游失败时后续步骤跳过；上游不完整时禁止投影、关联和汇总为完整结果。原始分页片段仍明确为partial。
- 独立只读步骤有限并发；依赖步骤顺序执行。多次读取不是MES事务快照。
- 模型仍需要制定/选择计划；model_rounds_inside_executor=0不是整个WorkBuddy问答没有模型耗时。

## 共享元数据

只缓存现场元数据，生产记录每次重新读取。

- 数据集列/类型来自固定INFORMATION_SCHEMA查询；Modeling实体契约来自现场Swagger。
- 缓存按Modeling/Query环境及配置用户名隔离，保存来源、摘要、读取时间，TTL为6小时。
- 审核数据集定义改变自动使用新缓存键；refresh=true明确刷新。
- 过期加载失败不会静默使用旧副本；缓存摘要损坏会拒绝继续。
- SQL表字段、Modeling API字段、Query视图字段是不同命名空间，不将“某接口不存在”推广为“所有表不存在”。
- 元数据不包含口令、Token或认证Header；不缓存业务查询结果。
- 同一进程相同契约的并发首次请求合并为一次读取。

SQLite文件：data/read_metadata.sqlite3（本机数据，不纳入Git）。

## 扩展方式

1. 新增数据集：在core/read_catalog.py定义审核表、主键及允许字段；现场验证后补测试。
2. 复用新工具：审查其真实读取路径和副作用，再加入READ_TOOLS。不将所有标为read的工具自动加入。
3. 新业务模板：组合dataset/call/select/group/join，补业务口径与展示转换。模板通过同一执行器，不复制HTTP/认证/分页代码。

元数据能确认字段存在和类型，不能自动决定业务意义。新写入流程继续由现有专用事务承载，不纳入本版本。

## 验证与部署

离线回归使用scripts/offline_test_guard.py阻断真实HTTPX网络。包含TMO已关闭容器开工时间、SQL值转义、白名单、循环/缺失引用、完整性传播、分页/重复ID、有限并发、超时、数值精度、时区与MCP协议。

只读现场脚本：

~~~powershell
venv/Scripts/python.exe scripts/validate_read_orchestration.py
~~~

脚本读取真实元数据、重复查询五张工单，并检查现有8030的工具清单；不会启动、停止、重启服务或执行生产写入。若现有进程仍为307工具，它会返回requires_user_process_reload=true；新版本完整注册表为311工具。

在VS Code重启当前服务进程后，WorkBuddy刷新/重连camstar即可看到四个新工具。验证脚本不抢占端口。

### 2026-11-08验收

- 相关回归606项通过；最终新增时区/高精度检查及MCP协议等60项定向检查通过。两轮离线HTTPX真实网络尝试均为0。
- 四个数据集的现场列/类型和Products现场Swagger可读；缺失的审核候选字段明确列出，不猜测可用。
- 五工单模板实际读取成功，TMO已关闭A01开工时间返回2026-11-04T22:50:48.337+08:00，保留毫秒。
- 元数据显式预热后最后两次完整模板读取约1239毫秒、98毫秒；仍执行两条生产数据查询，没有缓存生产结果。该耗时不包含WorkBuddy模型推理/排队/客户端往返，不能据此承诺整体倍数。
- 真实8030连接、initialize和tools/list正常，但仍为旧进程307工具。未停止或重启该进程；新版本311工具已通过进程内MCP协议验证，部署需要在VS Code重启原进程并刷新客户端。
- 本轮没有生产数据写入。

### 外部客户端往返检查

2026-11-08分析用户分享的在制明细请求（标题为开工日期，实际消息为MO-100076435在制品详细说明）：17次工具调用，其中14次AdHoc，3次HTTP 400、2次结果裁剪；首末工具发起时间相隔75.483秒。没有使用新增编排入口，客户端已经处于快速模式。

这说明工具已加载与模型实际选择是两个问题，不能把服务端约百毫秒的模板耗时当成完整问答耗时，也不能把这75秒精确归因给模型，分享记录缺少全部接口结束时间。

据此补齐外部MCP初始化指引和模板参数schema；62项定向回归通过，离线HTTPX真实网络尝试0。没有隐藏通用SQL工具、强制客户端路由、修改客户端模型配置或省略身份/完整性校验。
