# 学习与记忆生命周期

本次扩展复用 EXPERIENCE_DB（SQLite），不增加外部记忆服务，不修改 MES 数据或模型权重。

## 在线行为

- LangGraph 和 legacy 在首次模型推理前，按当前任务或相关工具检索经验。已审核知识按 MES 环境、MEMORY_CONTRACT_VERSION 和用户范围过滤；不再必须先调用过正确工具才能获得提醒。
- 用户说“不对、漏查、答错”等时，自动保存上一条完整回答、对应问题、工具参数/结果的脱敏限长摘录。它只是待核实反馈，不自动成为正确答案，不修改全局知识或生产规则。识别是启发式，可能漏报或误报，管理员可核查后归档。
- 同一用户、同一会话会收到固定提醒：历史助手结论不能作为事实依据，需重新核对证据。反馈原文不注入系统指令，其他用户/会话不接收这条提醒。已核查反馈仍保留纠错提醒，防止错误上文再次影响推理。
- 结构化工具结果按 error/success 字段判断失败，正常结果中的 error_count、含 error 的描述不再自动作为失败；分页未完整不自动等于接口失败。
- 默认最多注入5条相关知识。实时业务结果须重新读取；fact 类型明确标为历史事实，不代表当前状态。

## 知识的状态与淘汰

| 状态 | 作用 |
|---|---|
| candidate | 新知识待审核，不参与回答 |
| active | 已审核且环境、版本、有效期匹配，可用于相关任务 |
| stale | 过期、契约版本变化或依赖失效，必须重新验证 |
| retracted | 原结论错误，撤销，不允许原记录重新激活 |
| superseded | 被新知识替代，保留 replaced_by，不参与回答 |
| rejected | 候选未通过审核，不参与回答 |

每条知识保留证据、来源、类型、任务标签、所属用户、环境摘要、契约版本、验证日期、有效期、依赖与变更快照。空 owner 表示管理员审核的部署共享规则；指定 owner 仅用于该用户。环境使用 Modeling/Query 地址摘要，不保存凭据。

同一概念使用稳定的 knowledge_key（例如 order_dates.actual_start_basis）。同范围同版本已有 active 规则时，新候选不能直接激活，必须显式 supersede；“新近录入”不会自动覆盖旧规则。个体范围同 key 的规则召回时优先于共享范围，不混合注入两个版本。key 需管理员明确填写；没有 key 的自然语言知识不会自动进行语义矛盾识别。

当前任务标签：order_dates、wip、container_start、order_completion。新增领域时扩充 agent/learning.py 的 TOPICS 和对应回归。旧工具经验无标签时仍可按同名工具召回。

默认有效期90天，可审核时指定1..3650天。这是复核周期，不代表知识90天内必然正确。更换 MES 契约/业务口径时增加 MEMORY_CONTRACT_VERSION 并重启；旧版本知识首次维护/读取时转 stale。换环境时旧知识仍留在原环境，不能跨环境召回。

检查在检索与管理操作时执行，不依赖定时任务。重复提交相同内容、范围、任务、依赖的知识会复用原ID，不延长有效期，也不把重复陈述当作独立证据。后台模型反思、自动生成修改、模型训练与自动发布尚未接入。

撤销/替代/过期会让依赖它的 active 知识递归转 stale。依赖必须显式登记，系统不会可靠地从自然语言推断所有依赖。替换可直接选择 candidate：同一次事务中激活新知识并退休旧知识；新知识不能依赖旧知识或其下游，范围必须一致。失败则回滚。旧数据和审计记录不删除。

## 兼容迁移

旧 experience_events / experience_candidates 不删除。旧 approved 记录缺少可信环境和版本信息，首次打开学习库时迁入 stale，必须补当前证据重新审核后才能用于回答。原审批接口要求非空证据，并可通过 --topic 补任务标签。--no-revalidate 保留旧接口兼容，但不跳过新机制的有效期/现场证据要求。

## 管理与验证

以下命令在项目根目录执行。仅本地管理员使用，不暴露给模型作为工具；管理员负责核查证据是否真的证明结论。

```powershell
# 查看知识（同时检查过期/版本变化）
venv/Scripts/python.exe -m agent.learning list --state stale

# 建立候选；输出知识ID
venv/Scripts/python.exe -m agent.learning propose --topic order_dates --key order_dates.actual_start_basis --content "实际首箱开工取全部当前关联容器最早日期，含关闭容器。" --evidence "现场只读证据与对应回归用例"

# 核实证据后激活；以下ID仅为示例
venv/Scripts/python.exe -m agent.learning review 1 --action activate --evidence "已核对原始响应及回归" --ttl-days 90

# 错误认识：撤销。过去正确、现有新版本：替代
venv/Scripts/python.exe -m agent.learning review 1 --action retract --evidence "被原始证据证伪"
venv/Scripts/python.exe -m agent.learning review 2 --action supersede --replacement 3 --evidence "新契约已验证"

# 已过期但仍成立的知识，重新验证后续期
venv/Scripts/python.exe -m agent.learning review 4 --action revalidate --evidence "当前环境再次核验"
venv/Scripts/python.exe -m agent.learning history 4

# 导出反馈作为回归候选，expected_answer 为 null，不把用户投诉当标准答案
venv/Scripts/python.exe -m agent.learning export-feedback --output scratch/learning-feedback.json

# 补充证据、可选关联已激活的知识后，关闭反馈待办
venv/Scripts/python.exe -m agent.learning resolve-feedback 1 --knowledge-id 4 --evidence "现场记录和回归均已核实"
```

记录依赖使用 propose --depends-on 知识ID，可重复传入。原工具报错候选继续使用 `python -m agent.experience list/approve/reject`，approve 增加必填 --evidence、可重复 --topic。

新增 tests/test_learning.py 覆盖首次工具前召回、环境/用户隔离、有效期、迁移、撤销传播、原子替代、重复知识、反馈脱敏、纠错不自授权、完整 LangGraph 会话入口。真实 MES 日期读取仍由 tests/test_read_orchestration.py 的闭容器/分页/时区回归覆盖。反馈导出后仍需人工制定业务断言并加入对应回归；本模块不将导出数量冒充准确率提升。
