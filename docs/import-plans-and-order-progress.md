# 批量导入计划与工单现场进度

2026-10-22。本轮新增 preview_workflow_import、execute_workflow_import_plan、get_workflow_import_plan、get_mfgorder_progress；现有 import_workflows_from_excel 改为兼容预览入口，不再直接执行业务写入。

## 导入工作流

1. preview_workflow_import 读取全部映射行，最多 200 行；必填项、重复步骤、序号重复/格式、描述冲突、精确 Spec 引用及已有路线读取失败都会阻止生成可执行计划。不再静默跳过无效行。
2. 预览保存原路线、原步骤配置、Excel 行号、目标步骤/Spec 固定版本和准确写入 payload。无版本的 Spec 在预览时解析记录版本，执行时不再浮动到新的记录版本。现有描述保持不变。
3. 本地 SQLite 保存不可变计划和摘要，有效期一小时。LangGraph 的执行确认展示原步骤删除范围和每行新步骤/Spec；确认绑定计划 ID 与摘要。服务端计划也会校验摘要、时效与领取状态。
4. execute_workflow_import_plan 不重读 Excel。执行前重新核对所有目标路线和 Spec；任一变化都停止。领取一次后不能重放，失败需核对现场后重新预览。
5. 写入逐请求记录持久日志，首个失败立即停止。完成每条路线后读取验证步骤顺序和 Spec 引用。进程中断可能出现未知结果，不自动重试；get_workflow_import_plan 可查看计划和日志。

范围与限制：当前为 Workflow 线性路线重建；会删除旧步骤及其配置。Gates、子 Workflow、条件/返工和非空调度配置被拒绝覆盖。不是通用所有对象的 Excel 导入，也不是数据库事务。预检与实际写入间仍有并发修改窗口；多次请求之间没有自动回滚。外部 MCP 客户端仍负责人工授权，计划 ID/摘要本身不是身份认证或授权凭证。

## 工单现场进度

get_mfgorder_progress 精确解析工单，按工单 ID 读取关联 Container，分页最多 20 个；核验每个容器当前工单引用，再读取 Workflow、Step、Spec、Operation、数量、状态及可选最近三条直接历史。返回本页步骤分布和下一页偏移量。

工单计划数量、容器数量、容器当前数量、生产完成数量严格区分。production_completed_qty 和 completion_percentage 暂为 null：没有可核实的业务完成口径时不会计算。处于末工序、Closed 或生产前后置任务完成都不直接算生产完成。OnHold 等未返回字段表示未知。历史仅直接容器记录，不含拆分合并交叉关系。

查询使用固定 SQL Server Container/HistoryMainline 读取，不接受调用者 SQL。容器关联表结构已在当前环境只读验证；部署权限或表结构不兼容时明确返回错误。

## 验证

- 相关回归 109 项及 2 项子测试通过；随后新增的确认摘要和中断日志测试连同相关测试共 28 项通过。
- 真实 Excel 临时样本对 WF-WM-900A 的预览成功；未执行导入。
- MO-100076435 计划数量 80，首两条容器 BOX-0001 位于包装、BOX-0002 位于冲压，均能读取直接历史；本页仍有后续容器，不据此推断全量进度。
- 证据见 import-progress-live-validation.json。
- 没有执行任何生产写入；真实导入写入链仅用模拟服务验证。重启服务后加载本轮工具。
