"""Request additional model tool definitions; never executes business tools."""
import json
from typing import Literal

from core.tool_catalog import TOOL_POLICIES
from tools import mcp


@mcp.tool
async def load_tool_group(
    group: Literal["quality", "collection", "material", "resource", "workflow", "spec", "operation", "product", "mfgorder", "container", "reference", "query", "excel"],
    include_writes: bool = False,
) -> str:
    """Load a tool group on the NEXT model round when needed tools are missing.

    workflow=routes/steps; spec=specifications; operation=operations;
    product=products; mfgorder=orders; container=execution/status/history;
    collection=definition/point/group maintenance, Spec binding, sampling preview/submission/history/verification; material=BOM/material requirements/version comparison; resource=equipment/groups/spec resource context; reference=levels/numbering/lines/types; query=general Inquiry; excel=import.
    include_writes exposes write definitions only, never authorizes execution.
    """
    names = [name for name, policy in TOOL_POLICIES.items()
             if policy.group == group and policy.default_visible
             and (policy.effect == "read" or include_writes)]
    return json.dumps({"loaded": bool(names), "group": group,
                       "include_writes": include_writes, "tools": names}, ensure_ascii=False)

@mcp.tool
async def get_capabilities(group: str | None = None, tool_name: str | None = None) -> str:
    """查询本系统真实能力目录。回答你能做什么、支持哪些操作、参数和限制时先调用。
    Omit arguments for a grouped overview; use group for tools in that domain,
    or exact tool_name for its actual description and complete input schema.
    Read-only local registry inspection, never executes a business tool.
    """
    registered = {tool.name: tool for tool in await mcp.list_tools()}
    labels = {'quality': '质量、NCR、CAPA、检验与抽样/SPC', 'collection': '数据采集定义、参数维护与采集执行', 'material': 'BOM 与用料', 'resource': '设备资源与工序资源配置', 'workflow': '工艺路线', 'spec': '规格与工序配置', 'operation': '操作定义',
              'product': '产品', 'mfgorder': '工单及现场进度', 'container': '容器与生产事务',
              'reference': '基础资料与原因码', 'query': '查询服务', 'excel': '表格导入', 'system': '系统与能力发现'}
    disabled = {'patch_mfgorder': '当前服务 PATCH 契约未验证，工具已禁用；使用 update_mfgorder。'}
    caveats = {
        'get_quality_event_triage_context': '受限基础Event首次分流只读检查及原生非执行回显，保持责任人/角色/类别，检查组织分流与签名策略；不是审批、签名或实际分流完成。',
        'prepare_quality_event_triage': '已提交Active基础Event首次分流，保留类别/责任人/角色/CAR策略，可明确优先级；已有流程/检查表/审批或签名要求则拒绝。确认单次提交，unknown保留原事件及关联NCR容器占用，实际效果待验收。',
        'get_spc_violation_context': '只读原始直接数值点、完整规则成员及三层违例历史；不运行引擎或核实外部报告。受限图不允许通知/自动处置。',
        'prepare_spc_violation': '登记调用方明确报告的单点越限SPC结果，必须给operator_reported来源、原值及报告控制限；不是工具认证的原生引擎结果。无通知/自动处置，规则归属及完整回显核实后确认单次提交，unknown不重试，现场验收待补。',
        'get_spc_annotation_context': '只读原始数值采集历史点、固定定义/SPC组及所有图的有界注释历史；不是本次提交核实或放行。',
        'prepare_spc_annotation': '原生AddSPCAnnotation受限计划，真实历史点/父链、容器归属、固定定义和完整SPC组须核实；Exclude=false，有效排除/签名要求未知则拒绝；确认单次提交、unknown不重试，现场写入尚待验收。',
        'analyze_spc_series': '本地i_mr/xbar_r计算及显式WECO四规则，独立基线或有版本固定控制配置二选一；保留原ID/顺序/摘要，不核实MES来源，不把规格限当控制限，不写入、不放行。',
        'execute_quality_transaction': 'NCR/CAPA/检验/定义维护/产品关联/抽样批创建确认后单次提交，预览不等于业务批准；结果先unknown，当前只有人工最终证据审计可解占用，现场写入验收待补。',
        'inspect_quality_transaction': '仅当前记录/历史证据，不做成功归因、不解除占用；未验证非空请求到历史链。',
        'prepare_nonconformance_report': 'NCRCreate/Update/Close；更新省略失败码保留现有完整清单，传入则完整替换；关闭需NCR解决代码。',
        'prepare_capa': '固定类别5，submit=false草稿；提交不是批准或关闭，不代填电子签名。',
        'prepare_capa_closure': '仅CAPA的Close处置关闭计划，必须明确组织、质量解决代码及说明；不覆盖审批或签名，现场真实关闭验收待补。',
        'get_quality_closure_options': '实时处置原码候选；AttachToCAPA/CreateCAPA未开放，候选不代表目标许可。',
        'prepare_container_inspection': '单容器计数/测量检验计划，默认拒绝全检/扣量；显式allow_full_inspection只允许已有MES规则，allow_automatic_quantity_loss只允许测量按本次样本数由MES内部扣量，不另发ChangeQty。两者互斥，不扣至零、不支持计数/拒收扣量/事件/批抽样；高级模式核对原任务ID、主单位及配置，unknown需父子最终证据人工核实。',
        'get_inspection_test_definition': '固定版本SampleTest和数值点/单位/限值/原因/副作用配置，不混同普通数据采集。',
        'get_container_inspection_history': '历史累计数和MES状态，只作证据，不把累计量当本次增量，不自动解除unknown。',
        'prepare_quality_ncr_closure': 'NCRCloseQuality受限收尾计划：原NCR仍Open、原关联Event已Closed并有关闭时间/关闭人，明确NCR解决代码；不处理事件审批或关闭，不替代普通NCRClose。统一确认单次执行，unknown需最终证据。',
        'get_ncr_escalation_options': '明确组织的完整分类/子分类映射与签名设置。组合必须唯一；映射ID仅作追溯，接口不支持用它选择。不是NCR升级许可。',
        'prepare_ncr_escalation': '既有Open NCR升级质量事件计划，明确组织/分类/子分类/优先级，核对原报告/容器/完整组织映射及非执行回显。不额外调用关闭服务或绕过签名，统一确认单次执行，unknown不重试。',
        'get_current_sampling_status': '固定产品版本当前采样状态分页，展示真实选择ID、当前频率与独立计数器、当前/手动检验水平和批归属；不推算下个容器必检或不检。',
        'prepare_current_sampling_status_update': '容器采样明确1..20状态记录计数器/命名检验水平或reset_inspection_level恢复计划默认调整计划；命名与复位互斥，不猜默认或切换结果，核对AQL映射及非执行回显；不全选、不改频率、不回填已有任务或处理批采样。统一确认单次提交，unknown需最终证据人工核实。',
        'check_sampling_plan_configuration': '完整有界的计划/测试/数值点及AQL-检验水平代码关联静态检查，报告缺项、未知和当前提交工具限制。重读依赖，不写MES；冻结不等于不可检验，不验证运行时任务或产品绑定，不判合格。',
        'prepare_sampling_lot_update': 'UpdateSamplingLot数量/频率计划，仅当前计数全零且无有效采样历史的Active批，完整核对最多50成员/50任务；保留原调整选项，不改成员。非执行回显不是任务重算或业务验收。',
        'get_sampling_lot_maintenance_history': '历史SamplingLotId关联的维护数量/频率/评论、事务和冲销状态，非增量及最终成功证据。',
        'get_sampling_reference_definition': '命名AQL/检验水平完整表，含冻结、明细ID和变更计数；无版本或Status，不伪造标准表。',
        'prepare_sampling_reference_definition': 'AQL样本量/阈值及检验水平区间的创建、明细增改删计划。字面整数样本量，完整表校验；静态预览，统一确认单次提交，unknown保留占用。',
        'preview_sampling_table_lookup': '批量闭区间→原样样本量代码→AQL行的配置查表预览。不裁剪，不代表MES任务要求、标准符合性或质量结论。',
        'get_inspection_result_history': '按明确汇总历史ID及表结构候选父键展开最多50样本100点，以TxnId/HistoryId限制事务边界；不伪造顺序，父键关联仍待非空现场验收。',
        'find_quality_execution_candidates': '有尝试记录的unknown/in_flight计划，精确评论和历史目标筛选候选；分页不能证明唯一，整批目标关联仍未核实。',
        'reconcile_quality_execution': '明确候选事务，只读比较原测量请求及完整历史无序样本多重集合。计数增量/原因、整批目标及最终因果链尚未核实；匹配不自动成功、不解锁。',
        'prepare_inspection_definition': '检验点/测试/抽样计划的创建、更新及当前ROR升版。Modeling只有静态预览，没有execute=false回显，不是现场维护验收。',
        'prepare_product_sampling_plan': '固定产品版本直接关联固定抽样计划；不回填已有容器任务，不解绑继承配置。',
        'prepare_sampling_lot_create': '创建抽样批计划，明确产品版本、数量与单位；容器清单只读，不自动开工或挂接，不执行整批返工/报废。',
        'get_sampling_lot_details': 'SamplingLot存储于MfgLot，校验类型8587；成员和样本各自分页，不是整批质量判定。',
        'prepare_lot_inspection': 'CollectLotSamplingData，来源成员须已进站且属于该批，使用整批任务剩余数；本次增量，固定任务/版本，随机采样不要求SamplingRequired=true。移站/LIMS false，真实事务待验收。',
        'get_sampling_lot_readiness': '完整有界成员/任务及MES完成、通过标志；前置满足不是权限、签名或业务批准。',
        'prepare_container_quantity_loss': '单个基础Container原生LossDetails部分损失/明确全量报废准备，当前主单位增量；明确归零关闭和生产数量调整选项，原因属当前Operation的LossReasons组。强制4030/-1回显，无抽样批成员/Batch/层级/载具/第二单位；确认单次执行，unknown不重复扣量。',
        'get_sampling_lot_scrap_readiness': '抽样批报废只读阻塞检查，完整成员/任务重读；ScrapReason未实现注释和与LossReason类型冲突未解，执行就绪固定false，不生成结果3计划或循环扣量。',
        'prepare_sampling_lot_rework': '结果2原生整批返工计划，明确批和原因，完整1..20无冻结非层级成员，仅同Workflow恰好一条ReworkPath且无选择器。逐成员Rework只做非执行输入回显；缺失路由不当已验证，正式只提交一次ExecuteSamplingLot；原生会清理ContainerSampleData并标记批完成，实际数量和后续影响待验收。无Spec覆盖，unknown只读原成员证据，不自动成功或解锁。',
        'prepare_sampling_lot_completion': '仅执行MES已确认完成且通过的OK结果，固定码1，核对完整成员集合。不是通用放行；返工2须独立prepare_sampling_lot_rework，报废3未开放，具体后续影响待现场验收。',
        'get_sampling_lot_history': '按历史SamplingLotId只读采样汇总及来源，不证明本次提交或整批完成的最终因果链。',
        'prepare_data_collection_definition': '支持普通/分组定义与参数的增量维护、升版与删除计划；表达式原样保存。实际建模写入尚待集中现场验收。',
        'prepare_data_collection_binding': '仅明确Spec及定义版本的CollectData关联/解绑；不自动配置其他事务。',
        'preview_container_data_collection': 'user为点值映射，point为组/点/样本映射；对象引用提交暂阻断，分组/布尔/时间现场事务验收待补。',
        'verify_collection_submission': '按新计划唯一Comments标记和完整历史链保存本地成功；旧版无标记只支持人工核实，非空现场验收待补。',
        'reconcile_data_collection_change': '须原响应明确成功且目标配置回读一致才保存本地终态；丢失响应保持unknown。',
        'preview_bulk_component_issue': '模式2只读来源校验/派生回显，不扣库存、不执行；正式计划用prepare_bulk_component_issue。',
        'prepare_bulk_component_issue': '模式2首次完整需求的单来源计划，与模式5共用执行/核实/恢复及计划库；执行中/未知同时占用源和目标，不支持部分量/补投/第二数量/退料。',
        'reconcile_component_issue': '只读MES四表唯一历史链核实；匹配才原子保存本地成功终态/审计并解除本计划占用。无证据不推断失败，不重试或重开原计划。',
        'prepare_component_issue': '模式5投料/补投均为本次增量，不超净剩余；须平衡需求/累计量/净需求且无退料，较原需求少的本次数量须明确差异原因。不支持来源扣减/替代料/第二数量。',
        'get_component_issue_difference_reasons': '固定只读上下文选择值；最多100条且要求完整，不自动挑选默认原因。',
        'issue_components': '确认后单次执行模式2/5计划；模式2重读源/目标及余额，同库跨模式参与容器互斥。结果先unknown，完整历史链核实或人工最终证据核实后解除本计划占用，不重放。',
        'recover_component_issue_attempt': '仅同机无活跃执行者in_flight转unknown，保留占用；不证明服务端结束。',
        'resolve_component_issue_outcome': '人工最终事务证据核实，非自动验证；原计划不能重用。',
        'check_mfgorder_readiness': '只读有界配置汇总；可选include_tasks/include_documents。文档必需性仅来自明确documents_required，元数据/待办不证明允许开工。',
        'download_document_file': '通过正式Modeling下载端点验证当前账号访问，10MiB/60秒；不访问Identifier，不执行文件或证明其他账号权限。',
        'resolve_collection_submission_outcome': '仅人工最终事务/审计证据核实，非自动验证；原采集计划不可重用。',
        'recover_collection_submission_attempt': '有进程尝试记录的in_flight转unknown，保留容器占用；旧版无记录in_flight须离线运维。',
        'get_spec_documents': 'Spec文档关联与元数据；documents_required来自调用方明确要求，不推断配置规则。文件访问/下载端点未验证，不返回可执行文档内容。',
        'resolve_mfgorder_task_outcome': '人工最终证据核实，非自动验证；原计划不可重用。',
        'recover_mfgorder_task_attempt': '本地进程互斥恢复in_flight到unknown，保留工单占用；不代表服务端结束。',
        'hold_container': '已封装、测试通过；未完成现场写入验收。',
        'release_container': '仅容器解冻，不是工单释放；未完成现场写入验收。',
        'change_container_quantity': '既有数量变更；损失类型2已阻断直接提交，须prepare_container_quantity_loss及确认的质量计划。其余类型不是报废/报工，真实写入未验收。',
        'get_mfgorder_progress': '分布统计仅覆盖当前页，不等同工单完成数量。',
        'get_mfgorder_execution': '前后置过程任务信息，不等同生产产量。',
        'get_mfgorder_tasks': '只读任务候选与前置阻塞；不提供执行许可或结果提交。非空现场与顺序规则验收待补。',
        'preview_mfgorder_task_result': '仅单次、未开始的通过/失败任务预览；可选execute=false回显不代表业务验证或提交。状态映射、执行及现场验收待补。',
        'get_mfgorder_task_history': '当前执行记录不是请求到事务的审计关联；Modeling任务状态为0..3，不能混同Inquiry的1..4。',
        'execute_mfgorder_task_result': '受限单次确认提交；状态映射与现场执行待验收，结果unknown保留整个工单占用，不自动重试/解锁。',
    }
    result = {'source': 'live MCP registry + explicit tool policies',
              'limitations': ['工具已注册不代表现场事务验收通过；权限、配置和状态由产品校验。',
                              '未提供已验证的独立工单下发、整单报工、生产容器报废能力。',
                              '目录查询不加载业务工具，不授予写入权限。']}
    if group is not None and group not in labels:
        return json.dumps({'error': '未知分组', 'groups': labels}, ensure_ascii=False)
    if tool_name:
        policy = TOOL_POLICIES.get(tool_name)
        tool = registered.get(tool_name)
        if not policy or not tool:
            return json.dumps({'error': '未找到已注册且列入目录的工具'}, ensure_ascii=False)
        result['tool'] = {'name': tool_name, 'group': policy.group, 'effect': policy.effect,
                          'status': 'disabled' if tool_name in disabled else 'registered',
                          'description': tool.description, 'input_schema': tool.parameters,
                          'always_confirm_in_app': policy.always_confirm,
                          'limitations': disabled.get(tool_name) or caveats.get(tool_name)}
    else:
        entries = []
        for name, policy in TOOL_POLICIES.items():
            if name not in registered or not policy.default_visible or (group and policy.group != group):
                continue
            entries.append({'name': name, 'group': policy.group, 'effect': policy.effect,
                            'status': 'disabled' if name in disabled else 'registered',
                            'description': registered[name].description if group else None,
                            'limitations': disabled.get(name) or caveats.get(name)})
        result['groups'] = [{'group': key, 'label': label,
                             'tools': [e if group else {k: v for k, v in e.items() if k != 'description'}
                                       for e in entries if e['group'] == key]}
                            for key, label in labels.items() if not group or key == group]
        result['next'] = '使用 tool_name 查询完整参数；执行前通过 load_tool_group 加载对应定义。'
    return json.dumps(result, ensure_ascii=False)
