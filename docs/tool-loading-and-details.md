# 工具按需加载与关联查询增强

2026-10-22 实施。此次包含 P0 工具分类/加载与 P1 只读关联查询；未新增 Hold、Release、拆分合并等写入事务。

## 新增工具

- `load_tool_group`：请求下一模型轮次加载领域工具。仅改变候选工具集，不执行业务，也不构成授权。
- `get_spec_details`：摘要引用、事务映射、标签、签名、计时器视图。
- `get_product_details`：产品摘要引用、属性。
- `get_operation_details`：工序摘要、禁止事务与调度视图。
- `get_mfgorder_details`：工单摘要、物料、属性、已执行任务。
- `get_container_details`：ContainerInfoInquiry / ViewContainerStatus 固定查询。
- `get_container_history`：ContainerHistoryInquiry 分页查询。
- `get_mfgorder_execution`：MfgOrderInquiry 前后置过程/任务查询，不是生产完成数量查询。

建模详情通过显式白名单选择导航，保留全部非 null 字段及引用，不使用通用 KEY_FIELDS 裁剪。数组视图支持 offset/limit；这是返回结果的本地分页，不能减少服务端已返回的完整数组。服务端 nextLink、缺少所请求字段、超大结果均明确标记不完整。摘要不意味着所有子实体已展开。

## 加载策略

服务端共 115 个工具，LangGraph 和 legacy 每轮选择当前上下文相关工具。参考最近三次用户消息保留追问主题；通过 load_tool_group 的成功结果补充当前轮候选集。跨产品/工艺/工序场景组合多个领域。工具筛选不是权限系统，写入仍走执行策略。

默认隐藏 21 个 OData key 重复入口，以及实时 Swagger 未声明的 patch_mfgorder、patch_mfgline、patch_producttype。直接 MCP 注册仍保留这些兼容入口，因此隐藏不等于禁用或验证通过。

当前实测 schema JSON 字符数（不是 token，也不是收费或时延实测）：

| 场景 | 模型候选数量 | 定义字符数 |
|---|---:|---:|
| 新版全量库 | 115 | 77,737 |
| 查询工艺路线步骤 | 13 | 7,827 |
| 查询产品关联工艺与工序 | 23 | 13,813 |
| 查询 Container 状态 | 5 | 3,660 |

推理日志新增 available_tool_count、selected_tool_count、tool_schema_chars、selected_tools。未增加强制供应商 usage 参数，真实 token 与缓存命中尚未测量。

## 执行分类

core/tool_catalog.py 明确登记每个工具的领域、读写类型、默认可见性及强制确认策略。启动时检查注册工具具有策略和可执行入口。get_tool_func 不再允许调用未登记的内部 helper。

修复 add_spec_step_to_workflow、connect_workflow_steps、import_workflows_from_excel 未归类的问题。Excel 导入与路线重建在聊天 Agent 中要求确认，并说明工具调用次数不等于实际影响数据条数。legacy 使用同一分类/计数策略，确认词改为明确匹配。

这里的确认仍属于聊天 Agent 执行层；外部直接 MCP 客户端沿用原有认证与客户端授权，不应声称已经具备同样的 interrupt 确认流程。以后扩充写入事务时应继续完善直接调用策略及批量预览。

## 实际接口验证与限制

- Spec、Operation、Product、MfgOrder 摘要读取返回 200；Spec TxnMap 展开返回 200。
- ViewContainerStatus 必须 `$expand=ContainerStatusDetails`。读取 SN0000001 成功，返回 BOX、PACK-1000、MO-PACK-012、010-CELL-INSP、实际 Spec/Operation。
- Query 的 ContainerRef 虽在 Swagger 定义 Level，实际服务拒绝 Level 节点。因此新工具使用 name，可附 InstanceID；不会静默删掉用户指定的层级然后查询另一对象。
- ContainerInfoInquiry 对验证样本仅返回 Container 引用，未取得 CurrentSpec 等字段；标记 missing_fields，不宣称完整详情。优先使用 status 视图。
- ContainerHistoryInquiry 请求成功但样本 Histories 为空。保留“仅本页”标记，不能据此证明没有历史；尚未用有历史行的样本验证分页连续性。
- MfgOrderInquiry 对 MO-PACK-011 返回“没有前/后生产过程”的业务错误。原样保留，不解释成工单不存在或没有生产进度；成功任务结果仍需有相应过程的工单验证。
- 未执行任何生产修改事务，也未用真实 LLM 做写入端到端测试。

验证：69 项相关测试、2 项子测试通过，覆盖候选选择、按需扩展、注册白名单、读写分类、批量确认、完整性、分页以及现有 Container/Workflow 回归。

重启服务后加载新目录和工具；现有聊天记录继续可用。
