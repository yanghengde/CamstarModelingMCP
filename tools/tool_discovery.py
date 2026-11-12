"""Request additional model tool definitions; never executes business tools."""
import json
from pathlib import Path
from typing import Literal

from core.tool_catalog import TOOL_POLICIES, DISABLED_MODELING_PATCH_TOOLS
from tools import mcp
from core.capability_freshness import catalog_freshness

CAPABILITY_GAP_FILE = Path(__file__).resolve().parents[1] / 'docs/core-capability-gaps.json'


def capability_gap_summary(group=None, freshness=None):
    """Read the reviewed snapshot, never infer support from a Swagger name."""
    try:
        data = json.loads(CAPABILITY_GAP_FILE.read_text(encoding='utf-8'))
        labels = {'not_integrated': '未接入', 'partial': '部分支持', 'blocked': '执行门禁关闭',
                  'contract_unconfirmed': '业务契约待确认', 'validation_pending': '验收与自动核对待补', 'implemented': '已实现并完成限定范围验收'}
        gaps = [g for g in data['gaps'] if group is None or group in g['groups']]
        changed = set((freshness or {}).get('changed_source_files', []))
        def entry(g):
            item = {**{k: g[k] for k in ('id', 'title', 'groups', 'status', 'priority', 'current', 'missing')},
                    'status_label': labels[g['status']], 'swagger_contracts': g['contracts']}
            if changed.intersection(g['code']):
                item.update(status='validation_pending', status_label='实现变更待加载与复核',
                            current='相关实现已修改，当前进程尚未重新加载；可调用范围以运行工具目录为准。')
            return item
        return {'source': 'docs/core-capability-gaps.json',
                'document': 'docs/core-capability-gaps.md', 'scope': data['scope'],
                'live_health_check': False, 'items': [entry(g) for g in gaps]}
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'note': '能力缺口盘点快照不可用，请核对项目文档；不能推断没有缺口。'}


@mcp.tool
async def load_tool_group(
    group: Literal["quality", "collection", "material", "resource", "workflow", "spec", "operation", "product", "mfgorder", "container", "reference", "query", "excel"],
    include_writes: bool = False,
) -> str:
    """Load a tool group on the NEXT model round when needed tools are missing.

    workflow=routes/steps; spec=specifications; operation=operations;
    product=products; mfgorder=orders; container=execution/status/history;
    collection=definition/point/group maintenance, Spec binding, sampling preview/submission/history/verification; material=BOM/material requirements/version comparison; resource=equipment/groups/spec resource context; reference=UOM units/levels/numbering/lines/types; query=general Inquiry; excel=import.
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
    Reads current registered tools and latest local capability records on every call.
    Do not display audit dates or processing durations in capability introductions.
    Distinguish missing/partial/blocked/unvalidated capabilities in Chinese.
    Read-only local registry and audit snapshot inspection, never executes a business tool.
    """
    registered = {tool.name: tool for tool in await mcp.list_tools()}
    labels = {'quality': '质量、NCR、CAPA、检验与抽样/SPC', 'collection': '数据采集定义、参数维护与采集执行', 'material': 'BOM 与用料', 'resource': '设备资源与工序资源配置', 'workflow': '工艺路线', 'spec': '规格与工序配置', 'operation': '操作定义',
              'product': '产品', 'mfgorder': '工单及现场进度', 'container': '容器与生产事务',
              'reference': '基础资料与原因码', 'query': '查询服务', 'excel': '表格导入', 'system': '系统与能力发现'}
    disabled = DISABLED_MODELING_PATCH_TOOLS
    caveats = {
        'search_camstar_read_tools': '跨组搜索已注册只读工具和真实参数，无MES请求。是否可编排由独立审核白名单决定，不由read标签自动推断。',
        'get_camstar_read_metadata': '现场列/类型与实体Swagger按环境缓存6小时，明确刷新；只缓存元数据，不缓存生产结果。SQL字段与API字段分别处理。',
        'execute_camstar_read_plan': '受控只读DAG：审核工具调用、结构化数据集、投影/关联/精确汇总、有限并发和分页。上游错误/不完整不作完整汇总，不接受SQL/Python/URL/写入。最多12步500行，非事务快照。',
        'run_camstar_read_template': 'order_dates一次查询1..20个工单及全部当前关联容器（含关闭），明确计划/原生生产/实际首箱日期与时区；空日期不等于未开工。首次元数据读取后复用缓存。',
        'list_quality_references': '有界列出质量参考配置。spc_control对应维护服务，返回受限说明和get_spc_native_readiness入口；SPC图使用spc_chart。',
        'get_quality_reference_details': '读取明确质量配置；spc_control维护服务不支持按名称或ID读取持久控制配置，请核对明确SPC图的原生依赖。',
        'get_container_level_schema': '覆盖当前归档契约的全部字段；指定field_name返回精确嵌套/引用schema。只读字段和路径目标选择器不能写。契约声明可写不等于该字段已完成现场持久化验收。',
        'create_container_level': '完整层级创建：常用具名字段及body_json全可写契约。先查同名，不覆盖或补默认配置；POST一次后查唯一ID并回读。未执行真实业务创建验收，不创建容器。',
        'update_container_level': '完整层级更新：名称、AllowMove、唯一性、编号规则、父子层级、属性、WIP/UI配置及契约内其他可写字段，复杂结构先查get_container_level_schema。省略保持、JSON null显式清除；冻结/未知拒绝，已符合目标不写；单次PUT后按请求字段回读。扩展列表/控制字段仅提交并返回证据，field_checks=null不当已验证；未知结果不重试。全局配置，不自动执行或恢复过站；实际保存/复杂明细仍待现场验收。',
        'delete_container_level': '删除整个层级建模对象，非单容器解绑；核实实际ID、原名和冻结状态，应用沿用删除确认策略。不额外调用其他对象的删除接口；服务端可因引用等规则拒绝。DELETE一次后核实404；未知响应后不存在也不当本次已验证，未现场验收。',
        'request_container_level_selection_values': '读取原生字段候选，支持上下文JSON；不保存配置或自动选择值。字段表达式应来自契约，不猜查询路径。',
        'container_move': '基础MoveStd；写入前读取实际容器ID、Active、InProcess和层级AllowMove。Level不入请求体；不能在用户要求MoveIn/MoveOut时自动替代。仅基础字段，本轮失败后Agent禁止再次移动同一容器；非完整路线执行器。',
        'container_move_in': '基础MoveIn；实际ID、Active、InProcess=false和层级AllowMove前检；已进站拒绝。Level仅本地核对，采集/签名不支持，成功后须另行回读；未声称整路线已验收。',
        'container_move_out': '基础MoveOut；实际Container引用ID、Active、InProcess=true和层级AllowMove前检。Transaction Details在本机原生定义匹配ChangeQty.ServiceDetails；用get_moveout_diagnostics查看只读证据，部署一致性/失败栈仍未核实，顶层Details/ServiceDetails无写入契约；禁止造调整明细，当前请求失败后禁止换参数或事务重试；历史失败不作为永久禁令，完整路线仍待现场验收。',
        'get_moveout_diagnostics': '只读容器真实引用/当前状态/AllowMove/吞吐上报配置及原生静态元数据。错误标签候选ChangeQty.ServiceDetails不是已证实运行根因；无Shopfloor调用，不修改配置，不判空层级配置等于实际无子容器，ready_for_execution=null表示未评估，不是业务拒绝或跨请求禁令；根Swagger无直接入口不证明内部不能生成明细。',
        'get_quality_event_data_context': '只读已有8085标准EventData问题描述/设备位置及扩展收到/报告日期，保持原Event/28项映射值和禁用清单；仅已提交Active Category1且无复杂关联。可include_date_storage=true读取四列实际SQL类型/精度；默认不增加日期元数据查询，日期准备会自动强制核对。固定Load、验证证书HTTPS，无普通POST假预览或计划，执行就绪恒false。日期须明确偏移量且不推断MES时区，不同于原Event发生/报告时间。任意现场EventCustomData字段仍需部署契约。',
        'get_quality_event_log_context': '只读基础Active事件完整日志，核对SQL/Load原作者、时间、内容及当前用户；不预执行。',
        'prepare_quality_event_log_append': '完整保留旧清单，追加一条日志。可用comment_type_name选择既有未冻结标准类别、comment_type_instance_id核对名称与ID，类别版本复读保护；省略类别沿用无类别追加。原生当前用户与系统时间，不改删旧项或代签。确认后统一单次执行，unknown保留占用，原ID旧值和历史候选检查后人工审计。',
        'prepare_quality_event_data_update': '封闭changes仅problem_description(255)/device_location(30 UTF-16字符)、date_received收到日期/report_date扩展报告日期。省略/null保持，文字空串清除，日期不支持清除；日期完整含明确偏移量、最多7位小数，成对生成Local/GMT，同一时刻不同写法保留原对。不改Event发生/报告时间或MES时区，不臆定两种扩展日期先后。实际SQL datetime只接受整秒，datetime2必须精确可表示；不舍入/截断，类型/精度变化阻止旧计划。固定Load+静态差异、HTTPS及原责任人角色/会话核实，签名/复杂关联拒绝，确认单次保存，unknown不重试；日期SQL/Load同值亦不归因成功，最终人工审计才继续，现场未验收。',
        'prepare_quality_event_draft_update': '原Pending2未提交/未分流Category1草稿资料保存；明确文字/报告人/发现人及组织/优先级/带时区日期，省略/null保持原值。无流程/检查表/审批/NCR/事件数据关联。仅固定Load+本地静态差异，不调用可能强制执行的Submit=false普通假预览；static_and_echo_verified=false。核对原责任人/角色组织及原生当前员工，同次会话绑定、验证证书HTTPS；确认单次Submit=false保存，字段原生动态校验未证明。unknown最终人工审计后才独立准备正式提交，签名/AutoTriage/通知门禁不变，现场未验收。',
        'prepare_quality_event_submission': '原分类/子分类配置、原组织完整映射及唯一组合必须核实，固定Load及提交回显保持原分类；配置版本/映射变化使旧计划失效，不修改分类。原Pending2未提交/未分流Category1基础Event的正式提交；UpdateEvent Submit=true，完整Load保持原明细，不同时改资料或覆盖身份。原生AutoTriage会执行规则和可能通知；仅空BusinessRules及保留归属/类别/CAR、无流程/检查表的唯一配置，签名要求拒绝。可能通知须明确允许。原Edit2/责任人角色回显，重读后确认单次提交；unknown不重试，人工最终审计后按真实状态衔接分流，真实部署未验收。',
        'prepare_quality_approval_delegation': '当前指定审批人本人对一个Routed Named1条目的一次原生DelegateTask转派；明确原对象/表/条目/周期、新员工/原因/说明，allow_notifications=true。双方原角色组织及同次会话核实，验证证书HTTPS；不覆盖身份、不改决策、不创建日期任务，签名/转委托/批量仍拒绝。单次确认提交，unknown不重试，最终人工审计后由新员工合法会话签批。现场未验收。',
        'get_nonconformance_report': '默认当前NCR及完整失败码。明确include_native_signature_context=true时只读NCRUpdate通用多签/协签要求和契约阻塞，验证证书HTTPS，无口令/计划/实际执行；ready_for_execution恒false。',
        'prepare_nonconformance_report': '创建/更新/关闭计划，默认不采集签名。通用多签/协签首次接入仅Open未升级NCRUpdate，明确require_native_esign=true及native_signature_assignments原明细/员工/可选协签及原因；原ESigRoleGroup授权和完整人数须匹配。还须核实具体ESigPasswordCapture可写口令集合、固定Load事件及原生认证参数映射；当前原生映射门禁未开放，标准保存契约亦缺项，不生成签名执行计划或安全输入链接。不在聊天/工具参数输入口令，不覆盖执行员工；逐人页面确认、整组短期一次性，captured不表示原生签名成功。unknown不重试，实际部署未验收。',
        'prepare_quality_approval_decision': '默认原审批人的SignApproval批准/拒绝计划；原Role2且明确allow_role_substitution=true时核对原生当前员工角色/组织授权及完整快照，同次核验/执行会话仅内存绑定。固定当前周期/表/条目/决策10或20，Approve512及有效Employee须匹配。组织要求本事务单人QualityESigDetail时可明确require_quality_esign=true，仅在返回的独立本机页面输入口令并核对授权；聊天/工具参数不收口令。verified HTTPS无不安全回退；输入一次性、短期、仅内存，captured不表示MES签名成功，失效须重新准备。原生一次转派后可明确allow_delegated_approval及唯一当前周期delegation_history_id，实际新审批员工和原角色组织须核实，可组合单人质量签名。日期任务、转委托及通用多签/协签仍拒绝。通知及层级须审阅，确认单次提交，unknown不重试；Rejected非退回。',
        'prepare_quality_approval_action': '受限表级route/cancel/resubmit计划：首次路由、未签批取消；重提须明确previous_cycle_gmt并逐条核对旧路由/决策/评论持久历史。原Role2须明确allow_role_substitution=true，保持原策略；拒绝委托/签名要求，保留旧历史。取消原生回Pending并写RoutingCanceled，不当作Cancelled20。可能通知须明确允许，确认单次提交，unknown不重试。',
        'prepare_capa_approval_revision': 'CAPA拒绝/取消后受限简述/说明修订：明确原表和previous_cycle_gmt，旧路由/决策/评论历史须核实；固定Load保持原明细，不回写审批配置。原生会刷新报告时间，必须allow_reported_date_refresh=true；原Role2须allow_role_substitution=true且保持策略，权限/签名门禁；确认单次提交，不自动重提。',
        'get_quality_approval_template': '只读有效必审模板、原审批人及允许决策，双重重读；原Role2模板须明确allow_role_substitution=true，不授予签署资格；不等于原对象/组织适用，不挂接/路由。',
        'prepare_quality_approval_assignment': '首次挂接并立即路由FromCompleted完成审批，模板必须匹配原组织唯一Close映射；明确解决代码和处置说明。本地静态计划，无原生预览（可能清表），展示动态权限/签名未核实；确认单次提交，unknown不重试。',
        'get_quality_approval_history': '原对象/审批表的精确GMT周期及路由、签批、取消历史，保留原条目/审批人/决策/评论/事务ID；最多100表历史、200签批历史，超限指定周期，无截断。双重重读，旧历史非本次成功归因，不解锁unknown。',
        'get_quality_approval_context': '可明确include_delegation_history=true及delegation_entry_id读取所选条目当前周期转派历史；仅证据，不授权。Event/CAPA直接审批表、层级、条目、允许决策、当前审批入口及组织签名策略，只读完整重读。Routed或EntryForApproval不等于签批权限，不调用路由/签批/撤回（含预览），不收集口令。',
        'prepare_quality_event_creation': '实际创建的本地静态计划，仅原生自动编号、Submit=false，准备不调用创建服务预览。明确报告人/发现人和时间，直接归属映射/编号配置须核实；可能通知时明确允许。确认单次提交，unknown保留组织级事件占用，响应ID仅候选，现场验收待补。',
        'prepare_quality_event_open': 'Closed基础Event原生开启为Active，无事件数据/流程/检查表/审批/签名或NCR联动；保持历史关闭信息，不手工改状态。确认单次提交，unknown不重试。',
        'prepare_quality_event_reassignment': '明确新人员及角色，核对ChangeOwner动作和原账号，原生最终验证人员/角色组合；有通知配置须明确允许，确认单次提交，unknown不重试。',
        'validate_quality_event_creation': '只读核验创建资料、名称占用、分类组合、默认归属候选及通知/签名策略；不调用CreateEvent，包括execute=false。未提交保存仍可能分派/通知，预校验执行标志待核实。static_valid不是许可，不生成plan_id，执行就绪恒false，不覆盖同名草稿。',
        'prepare_quality_event_closure': '基础Event受限关闭计划，明确组织/质量解决代码/关闭说明；默认仅已提交Active已分流且无复杂上下文；显式allow_complex_context可核实CAR/CAPA、流程快照、关联NCR及已批准审批。显式allow_native_ncr_closure可受限处理类别3标准EventData的唯一原NCR及同容器完整处置覆盖；只发父关闭，不补发子关闭。其它事件数据/检查表/待审批/签名仍拒绝。核对Resolve64、账号及当前契约；预览不是批准，确认单次提交，unknown不重试，真实效果待验收。',
        'get_quality_event_closure_context': '基础Event只读关闭诊断，汇总静态阻塞与原生Close候选，动态权限、CAR关联、批次处置及审批/签名仍未核实；不生成计划、不关闭事件或关联NCR，执行就绪恒false。',
        'get_quality_event_update_context': '默认基础Event文字维护前置；submit_pending只读Pending草稿正式提交前置；edit_pending_draft核对不完整资料保持值/缺项/当前员工；classify_pending_draft核对原分类和原组织完整映射，可同时明确classification_name/subclassification_name只读比较拟选组合。分类修改尚无已核实原生写入，标准UpdateEvent字段只读，不能直接改库或同名CreateEvent覆盖。ready_for_execution及ready_for_submission恒false，无修改计划；组合有效不表示保存、修改或提交完成。',
        'prepare_quality_event_update': '固定Load加本地静态文字差异，禁止Submit=false普通POST假预览（标准会强制内部执行）；static_and_echo_verified=false。仅明确简述/说明/发现区域；省略/null保留，说明/区域可显式空字符串清除。保留原报告人/日期/优先级及完整受支持明细，submit=false；已提交Active基础Event且无流程/检查表/审批/复杂关联/签名要求。确认单次提交，unknown不重试，真实效果待验收。',
        'get_quality_event_triage_context': '受限基础Event首次分流只读检查及原生非执行回显，保持责任人/角色/类别，检查组织分流与签名策略；不是审批、签名或实际分流完成。',
        'prepare_quality_event_triage': '已提交Active基础Event首次分流，保留类别/责任人/角色/CAR策略，可明确优先级；已有流程/检查表/审批或签名要求则拒绝。确认单次提交，unknown保留原事件及关联NCR容器占用，实际效果待验收。',
        'get_spc_violation_context': '只读原始直接数值点、完整规则成员及三层违例历史；不运行引擎或核实外部报告。受限图不允许通知/自动处置。',
        'prepare_spc_violation': '登记调用方明确报告的单点越限SPC结果，必须给operator_reported来源、原值及报告控制限；不是工具认证的原生引擎结果。无通知/自动处置，规则归属及完整回显核实后确认单次提交，unknown不重试。新计划可reconcile_quality_execution核对完整登记，再resolve_history确认重读结案；只认证登记、不认证引擎/报告来源/控制限，现场验收待补。',
        'get_spc_annotation_context': '只读原始数值采集历史点、固定定义/SPC组及所有图的有界注释历史；不是本次提交核实或放行。',
        'prepare_spc_annotation': '原生AddSPCAnnotation受限计划，真实历史点/父链、容器归属、固定定义和完整SPC组须核实；Exclude=false，有效排除/签名要求未知则拒绝；确认单次提交、unknown不重试；新计划可核对唯一完整原生历史、原点和配置保持后resolve_history结案，旧快照人工取证，现场写入尚待验收。',
        'analyze_spc_series': '本地i_mr/xbar_r计算及显式WECO四规则，独立基线或有版本固定控制配置二选一；保留原ID/顺序/摘要，不核实MES来源，不把规格限当控制限，不写入、不放行。',
        'analyze_spc_attributes': '本地p/np/c/u计数图，明确不合格品件数或缺陷个数及每点检查单位；np/c固定单位，p/u允许变化。独立基线或有版本rate二选一，仅显式逐点beyond_3sigma，精确边界判定；不查写MES、不生成登记计划、不认证引擎或放行。',
        'analyze_spc_history': '用MES原始采集历史点ID核对同一工艺、单位、区间完整性和排除状态，最多100点，计算本地i_mr/xbar_r及WECO信号并保留源摘要。需要独立历史基线或有版本控制配置。',
        'get_spc_parameter_trend': '只读同一参数完整区间的原始值走势，最多100个按GMT先后给定的采集历史ID；默认container同一容器，明确process才可跨容器。不要求基线/控制限/引擎；保留原值、GMT和未冲销排除记录，不删点、不判异、不放行。',
        'get_spc_native_readiness': '只读区分SPC图、类型、规则、查询、连接与原始采集历史可见、为空或不可用。可按图名或持久ID核对依赖、动态参数、规则成员及副作用配置，不返回连接凭据或参数值。',
        'execute_quality_transaction': '质量计划单次提交；用户明确执行时，已核实有效计划的新建检验指标/项目/抽检计划免逐项确认。其他质量维护、生产关联、检验提交及处置仍须确认；结果先unknown，最终证据人工审计后才解占用，不重放。',
        'inspect_quality_transaction': '仅当前记录/历史证据，不做成功归因、不解除占用；未验证非空请求到历史链。',
        'prepare_nonconformance_report': 'NCRCreate/Update/Close；更新省略失败码保留现有完整清单，传入则完整替换；关闭需NCR解决代码。',
        'prepare_capa': '固定类别5，submit=false草稿；提交不是批准或关闭，不代填电子签名。',
        'prepare_capa_closure': '仅CAPA的Close处置关闭计划，必须明确组织、质量解决代码及说明；不覆盖审批或签名，现场真实关闭验收待补。',
        'get_quality_closure_options': '实时处置原码候选；AttachToCAPA/CreateCAPA未开放，候选不代表目标许可。',
        'prepare_container_inspection': '单容器计数/测量检验计划，默认拒绝全检/扣量；显式allow_full_inspection只允许已有MES规则，allow_automatic_quantity_loss允许MES按唯一配置扣量。计数支持单组或counted_groups，显式绑定SampleType和本次SamplesTested，不另发ChangeQty。已验收计数按样本数、拒收数扣量及同组多原因扣量、归零关闭；归零须明确allow_zero_quantity_loss。新版本非全检计数扣量计划可reconcile_quality_execution核对唯一父检验、内部扣量、累计数、逐原因增量及原任务，resolve_history重读最终效果结案。原生分组Rejects为运行累计，不相加当本次拒收。样本分区由用户声明，组内采样分配不作历史证明。多组按原因报废、批自动处置和检验内自动事件仍拒绝；已核实失败检验可独立prepare_inspection_quality_event，不重做检验或扣量。旧快照/全检/测量组合保留原核对范围。',
        'prepare_inspection_quality_event': '已按原生历史结案的失败计数扣量检验，独立准备关联质量事件。绑定原计划ID/摘要/事务及原任务，要求MES Fail且本次有不良，明确组织、分类、子分类、优先级、报告人、发现人及带时区报告时间。只读准备，不调用创建预览；一次CreateProductionEvent自动编号，使用Nonconformance类别3编号映射及带双引号字面值前后缀，不改账号主组织，不挂起或打印，不发送通知。来源标记、原生事件/容器关联和最终效果须核对，同来源防重；独立事务失败不重做检验。当前正向验收被MES服务权限拒绝13369354阻止，须管理员开通后继续，不能宣称已创建。',
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
        'get_inspection_result_history': '按汇总历史ID及父键展开最多50样本100点，以TxnId/HistoryId限制事务边界。C4批计数已核对父键，其他检验类型待现场验收。',
        'find_quality_execution_candidates': '有尝试记录的未知质量计划，按精确事务评论列出有界历史候选。数量损失和整批处置使用主历史，分页不证明唯一。',
        'reconcile_quality_execution': '只读核对原计划和持久历史。数量损失、整批处置、批维护及带系统标记的检验可自动定位。新版本非全检计数扣量核对唯一父检验、内部扣量、各原因增量及任务累计；关联事件有独立原生创建/容器关联核对入口，正向验收待服务权限。整批OK只核对父批结果，批计数核对本次总数和逐组拒收原因。新版本SPC注释和明确报告违例核对全局唯一完整评论、尝试时间、全部父子历史、原点及既有历史保持，登记成功不认证引擎/报告/控制限；旧快照与未覆盖组合保留人工取证。',
        'review_quality_transaction': '经确认登记本地审计。recover恢复中断日志，resolve登记人工最终证据。已匹配的损失、报废、返工、新版本计数扣量及关联事件可指定TxnId用resolve_history重读完整最终效果后结案。resolve_rejection仅支持明确生产事件编号/字面值语法/权限拒绝，须完整原响应、配置/原检验不变及无事件/历史的重复核对，不凭HTTP错误或无历史通用解锁。原计划不可重放，不绕过权限。',
        'prepare_inspection_definition': '检验点/测试/抽样计划的创建、更新及当前ROR升版。Modeling只有静态预览，没有execute=false回显，不是现场维护验收。',
        'prepare_product_sampling_plan': '固定产品版本直接关联固定抽样计划；不回填已有容器任务，不解绑继承配置。',
        'prepare_sampling_lot_create': '创建抽样批计划，明确产品版本、数量与单位；容器清单只读，不自动开工或挂接，不执行整批返工/报废。',
        'get_sampling_lot_details': 'SamplingLot存储于MfgLot，校验类型8587；成员和样本各自分页，不是整批质量判定。',
        'prepare_lot_inspection': 'CollectLotSamplingData，来源成员须已进站且属于该批，使用整批任务剩余数；本次增量，固定任务/版本，随机采样不要求SamplingRequired=true。移站/LIMS false，真实事务待验收。',
        'get_sampling_lot_readiness': '完整有界成员/任务及MES完成、通过标志；前置满足不是权限、签名或业务批准。',
        'get_quantity_loss_scope': '只读完整活动层级、载具成员及双向跟踪身份，最多8个相关容器；不创建计划。真实Batch原生禁止ChangeQty。',
        'prepare_related_container_quantity_loss': '父子层级/跟踪载具单次LossDetails计划；叶部分或明确父子树全量，同产品/主单位/步骤且复杂限单单位。关闭保留历史父ID，归零清除共享状态载具绑定，完整投影/占用；六笔现场原生历史及效果已验收，unknown不重放。',
        'prepare_container_quantity_loss': '单个基础Container原生LossDetails部分损失/明确全量报废准备，当前主单位增量；明确归零关闭和生产数量调整选项，原因属当前Operation的LossReasons组。强制4030/-1回显，无抽样批成员/Batch/层级/载具。默认拒绝双单位；明确secondary_loss_quantity可准备双单位部分损失，两组原单位/余额固定且各自损失小于余额，不转换单位、不混用全量模式；确认单次执行，unknown不重复扣量。',
        'get_sampling_lot_scrap_readiness': '完整抽样批只读前置；提供明确LossReason可核对真实ID绑定、成员原因组及原生默认值，preflight_passed表示前置通过。ready_for_execution固定false表示本工具不批准/执行，不表示报废未接入；准备使用prepare_sampling_lot_scrap。限定正向/冻结拒绝已验收，不循环扣量。',
        'prepare_sampling_lot_scrap': '限定范围已完成正负现场验收。完整1..20个失败抽样批基础成员，明确LossReason ID/原因组、全量LossDetails和原生默认。正式仅一次ExecuteSamplingLot结果3，随后reconcile及resolve_history核实完整父子历史、原成员归属/关闭、批级任务保留及成员任务清理；unknown不重放，不依据错误码或无历史自动解锁，不保证所有故障原子性。',
        'prepare_sampling_lot_rework': '结果2原生整批返工计划，明确批和原因，完整1..20无冻结非层级成员，仅同Workflow且无选择器；多路径须明确expected_path_name核对所有成员的首条原生默认路径并完整回显路径/目标/返回步骤。逐成员Rework只做非执行输入回显；缺失路由不当已验证，正式只提交一次ExecuteSamplingLot；计划固定原成员采样任务基线，原生会清理ContainerSampleData并标记批完成；inspect区分旧任务消失/保留/变化和新增任务，核对原成员当前去向与数量，实际最终历史待验收。无Spec覆盖，unknown只读原成员证据，不自动成功或解锁。',
        'prepare_sampling_lot_completion': '仅执行MES已确认完成且通过的OK结果，固定码1，核对完整成员集合。不是通用放行；返工2须独立prepare_sampling_lot_rework，报废3须独立prepare_sampling_lot_scrap，具体后续影响待现场验收。',
        'get_sampling_lot_history': '按历史SamplingLotId只读采样汇总及来源，不证明本次提交或整批完成的最终因果链。',
        'prepare_resource_change': '独立设备主数据创建/修改与明确引用；不通过建模直接修改运行状态/计数。初始状态由MES决定，用operational_state核实。已完成2026-11-09独立现场验收。已有设备状态模型迁移、设备删除、层级和零件维护另行处理。',
        'prepare_resource_activation': '日期/周期日期/吞吐维护要求的单设备启停，不是Up/Down；日期要求启停已现场验收，另两类触发规则未验收。类激活不覆盖。此原生服务不写通用主历史，须原HTTP成功确认与维护关联回读；丢失响应保持unknown。',
        'prepare_resource_setup': '原生ResourceSetup及ResourceSetupTransition，明确可用性/设置/产品版本/Run及状态码。已验收模型初始Down及Down→Up→Down，未修改项保留；独立设备范围，不绕过模型/维护/签名校验。',
        'prepare_resource_throughput': '独立Up设备主副数量/单位的本次吞吐；已按原生持久历史核实。累计字段未持久化单位时不猜单位，不等同容器报工/工单完工或OEE。',
        'prepare_resource_collection': '普通UserDataCollection资源采集已验收Decimal/Boolean/String及全部点历史。分组point预览可回显但本环境提交未保存样本，执行门禁关闭；不自动转换类型或重发，不推断质量。',
        'get_resource_operational_state': '原生ResourceDef/ProductionStatus及维护关联原值；Up不代表可生产，激活不代表维护完成。计数单位空值请核对逐条吞吐历史。',
        'get_resource_throughput_history': '逐次主副数量、原单位ID和事务链，分页不是一致快照；不同单位不合并。',
        'get_resource_collection_history': '设备普通采集直接历史，不含未落账分组样本；不判质量，空页不证明失败或允许重发。',
        'execute_resource_change': '共享不可变计划/状态与定义占用，单次提交；unknown不重放。原生动态校验仍由MES执行。',
        'reconcile_resource_change': '只读核实并保存本地终态；设置/吞吐/采集核对唯一标记、原生历史及明细；激活用成功确认+维护关联回读，不伪称存在主历史。',
        'prepare_bom_change': 'BOM创建/基本信息/明细增删改和重命名/升版；显式复制源配置，新版成为ROR并保留旧版。已通过2026-11-09独立现场验收。每版最多200行；不支持整BOM删除、ResourceBOM、幻影BOM或替代料表维护。源含未支持复制配置时阻断升版，不静默丢失。发料控制建模1..6不等于实际投料模式全部开放。',
        'prepare_product_bom_binding': '固定产品版本关联/换绑/解绑固定BOM版本，保留其他产品配置；不批量变更已有工单或容器物料清单。',
        'execute_bom_change': '确认后重读不可变计划和依赖，只提交一次。unknown占用同名BOM/产品，原计划不可重放。',
        'reconcile_bom_change': '仅原响应明确成功且回读匹配时保存本地核实终态；升版还验证源版本保留。丢失响应仍unknown，不写MES。',
        'review_bom_change': '仅本地人工最终证据审计/崩溃恢复；不重发原请求，不将配置相同视为本次成功。',
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
        'get_mfgorder_production_summary': '全工单当前关联容器、按工序/单位/冲销状态分组原始吞吐及原生CompletedQty/ScrapQty。完工日期与后置过程分开；本应用良品/不良台账与双口径工时分别用get_production_reports/get_production_worktime，不汇总多工序吞吐为产量。',
        'verify_production_transaction': '按唯一原请求评论、事务主历史和汇总/明细核对Thruput、ContainerDefect、MoveIn、MoveStd；成功保存history_committed，空/错/冲销历史不解锁。',
        'prepare_production_station_transition': '明确分步流程：enter=MoveIn，exit/complete=独立MoveStd，先完整吞吐；complete沿真实路径移入IsLastStep完工节点，不改工单状态或绕过校验。',
        'execute_production_station_transition': '单次原生进站/移站，原计划重核并防重放；未知核实历史，绝非失败MoveOut的自动替代。',
        'prepare_production_labor_submission': '绑定已有人工记录及原工序，专用CollectData七点定义保存实际分钟；明确分钟UOM，不猜TaskTime，不自动改生产配置。',
        'execute_production_labor_submission': '原人工分钟计划单次提交MES CollectData，与原采集计划共享不可重放保护。',
        'verify_production_labor_submission': '核对唯一CollectData事务及全部七点，确认MES实际分钟落账；不会重复累计人工工时。',
        'get_production_labor_submission': '查询人工记录与原MES采集计划的绑定和落账状态，明确本地与MES来源。',
        'prepare_production_reject': '明确record登记保留在制或scrap复用原生LossDetails扣量。record须分别提供缺陷次数和不良件数；scrap须明确归零关闭/吞吐调整选项。',
        'execute_production_reject': '原生ContainerDefect登记，保留数量；限定基础容器的非空提交和完整历史归因已验收，提交未知先verify_production_transaction。',
        'record_production_report': '本应用良品/不良台账，须已核实本工序吞吐及原不良处置计划。原生计划不可重复入账，按工序/单位统计，不代表MES质量判定。',
        'record_production_labor': '本应用人工实报分钟，明确员工/工单/带时区区间，重叠记录拒绝；不写MES TaskTime。',
        'get_production_worktime': '人工实际分钟与原生MoveIn/MoveHistory配对历时分别统计，含MES工时落账状态；匹配截秒源时间后以实际事务毫秒计时，不相加。',
        'get_production_reports': '本应用台账按工序/单位分组的良品、登记不良与报废数量，不跨工序累计为工单产量。',
        'get_mfgorder_completion_readiness': '核对MES生产完成日期、后置过程和未完成在制及本应用未核实提交；不提供MES关闭授权。',
        'record_mfgorder_completion': '只保存MES已完成后的本应用整单收尾记录；不关闭MES工单或改写原状态/日期。',
        'get_container_throughput_history': '实际直接容器ThruputHistory主记录，不重复相加明细；分页不保证跨页快照，冲销和重加工须另行核实。',
        'prepare_container_throughput': '基础相同上报层级：Thruput支持部分加工；空计数读原生QtyRemaining，不直接归零；MoveWithThruput原有受限入口未纳入本次路线验收。',
        'execute_container_throughput': '重读计划后单次原生提交，unknown占用原容器；不自动重试，响应和回读仅为核实证据。',
        'get_container_throughput_plan': '只读本地计划、提交响应及历史回读；unknown/in_flight不是可重试结论。',
        'review_container_throughput': '人工最终事务证据审计unknown计划，不释放in_flight，不再执行或重放旧计划。',
        'recover_container_throughput_attempt': '本机执行中断后将in_flight改为unknown，保留容器占用；不证明MES请求结束，最终证据须独立审计。',
        'get_mfgorder_execution': '前后置过程任务信息，不等同生产产量。',
        'get_mfgorder_tasks': '只读任务候选与前置阻塞；不提供执行许可或结果提交。非空现场与顺序规则验收待补。',
        'preview_mfgorder_task_result': '仅单次、未开始的通过/失败任务预览；可选execute=false回显不代表业务验证或提交。状态映射、执行及现场验收待补。',
        'get_mfgorder_task_history': '当前执行记录不是请求到事务的审计关联；Modeling任务状态为0..3，不能混同Inquiry的1..4。',
        'execute_mfgorder_task_result': '受限单次确认提交；状态映射与现场执行待验收，结果unknown保留整个工单占用，不自动重试/解锁。',
    }
    result = {'source': 'live MCP registry + explicit tool policies',
              'presentation': {'title': '当前能力说明',
                               'inventory_basis': '本次读取的工具注册表与显式策略；不是现场健康检查或事务验收。',
                               'display_rule': '能力介绍不显示处理时长、停留时间或历史盘点/核查日期。只说明当前支持范围、限制与待核实项。',
                               'coverage': '概览覆盖全部非空业务分组，按实际工具及限制归纳；重点呈现近期质量、审批签名、SPC、采集、投料及开工准备入口。'},
              'limitations': ['工具已注册不代表现场事务验收通过；权限、配置和状态由产品校验。',
                              '独立工单下发未核实；第1部分生产与整单完工已限定验收。数量损失的基础单/双单位及同产品单单位层级/跟踪载具已限定验收，真实Batch被MES原生禁止。',
                              '确认方式取决于当前对话设置和执行层策略，不能笼统声称所有写入都需逐项确认；缺失选择/参数或用户明确要求先确认时仍等待用户。',
                              '目录查询不加载业务工具，不授予写入权限。']}
    result['freshness'] = catalog_freshness()
    if result['freshness']['reload_required']:
        result['limitations'].append('部署源码有修改尚未加载；需要重启服务后再查询，不能把磁盘上的新代码当作当前已可调用能力。')
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
        result['capability_gaps'] = capability_gap_summary(group, result['freshness'])
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
