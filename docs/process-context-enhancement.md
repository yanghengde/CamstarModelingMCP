# 高优先级工艺查询增强与验收

2026-10-22。新增两个只读工具，注册总数从 115 增至 117；仍按领域加载。

## Workflow 高级结构

`get_workflow_structure(workflow_id, max_depth=3)` 读取普通路径、Gates、PathSelectors、ReworkPaths、ReworkPathSelectors、SchedulingDetail。条件表达式和跨 Workflow 返工引用保留原始数据，不模拟表达式执行结果。

实时 Modeling `$metadata` 确认 `SubWorkflowStepChanges.SubWorkflow` 才是子流程引用，修正旧读取器误用 `Workflow` 的问题。仅 StepType=2 递归子流程；按真实引用 ID 或精确名称/版本解析，UseROR=true 时明确查询记录版本，拒绝歧义匹配。

读取时分别获得 Steps 和叶级导航，再核对 Workflow 名称、版本及步骤集合后合并，不使用临时 Changes-CDO ID 关联。子 Workflow 循环检测使用当前祖先链，共享子流程只读一次。普通路径检测包含非默认分支；跨流程返工路径不按同名步骤错误合并。

上限：深度 0..5、20 个 Workflow、每个 200 步、每次组合查询 160 次请求、输出 160k 字符。达到边界或字段缺失时明确返回不完整。跨请求读取不是事务快照，同版本并发修改仍可能影响结果。

## 产品完整工艺链

`get_product_process(product_id, max_depth=3)` 返回 Product → Workflow/子 Workflow → Step → Spec → Operation。分别读取实体详情，避免列表接口的 null 引用导致误判；不从步骤名称猜测 Spec。相同引用在本次调用中缓存；Spec 与 Operation 结果去重存储，步骤保留对应 ID。单条关联失败保留已读取结果及错误，整体 complete=false。

## Container 历史修正

用户提供 BOX-0001，状态读取确认其产品为 WM-900A/A、路线为 WF-WM-900A/A、当前在 OP-PACKING，关联工单 MO-100076435。

实测 ContainerHistoryInquiry 返回 200 但 Histories 为空，固定只读 Query/AdHoc 查询确认 HistoryMainline 中有记录。不能将 Inquiry 空页解释成没有事务。

`get_container_history` 新增 history_source=auto|inquiry|mainline：

- auto 优先 Inquiry；空页且返回有效容器引用时使用固定 HistoryMainline 查询。
- mainline 先通过状态接口核对容器引用，然后读取直接历史，适合后续连续分页。
- inquiry 保留原接口路径，便于诊断和需要交叉关联历史的场景。

兜底不接受用户 SQL，仅使用核对过名称的 16 位十六进制容器 ID 和限制范围的分页参数。固定字段、TxnDate/HistoryMainlineId 双排序，多取一条判断 has_more。不能把这些记录宣称为包含父子关系、拆分合并等交叉关联的完整历史；响应有明确 scope 和 warnings。若部署数据库/权限不支持该查询，保留原 Inquiry 结果和 fallback_error。

BOX-0001 实测返回 6 条直接历史（1 条 Start、5 条 MoveStd）。第 1 页 2 条、第 2 页 2 条与单次 4 条结果完全一致；mainline 显式模式也通过。分页为偏移分页，存在并发插入时不保证跨页快照一致性。

## 工单执行查询

MfgOrderInquiry 是生产前后置流程任务查询，不是工艺路线执行进度。对实时返回错误码 16780524 明确输出 procedure_not_configured，保留原始服务端错误，不解释成工单不存在或产量为零。

检查当前可见 21 个工单的单实体详情，均未返回 PreProductionProcedure/PostProductionProcedure 引用。用户容器对应的 MO-100076435 也由 Inquiry 明确报告未配置。因此真实的非空任务成功样本验收仍待有相应配置的工单；未修改生产配置来制造样本。成功任务数据保留及分页逻辑已用模拟响应覆盖。

## 验证记录

相关回归测试 97 项通过，另有 2 项子测试通过；包含精确版本/记录版本解析、歧义拒绝、子流程循环与共享引用、深度限制、高级导航完整性、分页兜底、ID 校验和错误保留。

- 3 条实际 Workflow：IMR Workflow 3 步、WF-WM-900A 6 步、WF-PACK-01 13 步，高级导航读取成功。当前真实样本没有覆盖非空条件表达式和子流程循环，这些分支由自动化测试覆盖。
- WM-900A 查通 6 个 Spec/Operation；PACK-1000 查通 13 个。MODULE-100、CELL-50 未返回 Workflow 引用，按不完整结果处理。
- `docs/process-read-validation.json`：初次验证及工单样本扫描。
- `docs/process-read-validation-box.json`：用户提供 BOX-0001 后的非空历史分页与工单验证。
- 可重复运行：`venv/Scripts/python.exe scripts/verify_process_reads.py --container BOX-0001 --order MO-100076435 --output docs/process-read-validation-box.json`。脚本仅查询，不创建业务测试数据。

本轮代码变更需要再次重启服务后生效。
