"""Business wording for approvals; execution still uses the original bound calls."""
import json
import re


LABELS = {
    'name': '名称', 'revision': '版本', 'description': '说明', 'kind': '业务类型',
    'action': '操作', 'entity': '对象类型', 'container': '容器', 'containerName': '容器',
    'container_names': '容器', 'container_name': '容器', 'new_container_name': '新容器',
    'product': '产品', 'product_name': '产品', 'product_revision': '产品版本',
    'spec': '工艺', 'workflow': '工艺路线', 'operation': '工序', 'resource': '设备',
    'mfgOrder': '工单', 'mfgorder_name': '工单', 'qty': '数量', 'qty2': '第二数量',
    'count': '数量', 'comments': '备注', 'close': '完成后关闭容器',
    'moveAllQty': '移动全部数量', 'thruputAllQty': '上报全部加工数量', 'path': '流转路径',
    'sampleRate': '抽检频率', 'sampleSizeBasedOnStartQty': '按开工数量计算样本量',
    'moveContainerOnCompletion': '检验完成后自动过站', 'samplingPlanDetails': '抽检项目',
    'sampleTest': '检验项目', 'sampleDataPoints': '检验指标', 'sequence': '顺序',
    'inspectAll': '全检', 'inspectAllContQty': '检查容器全部数量',
    'inspectAllQtyOnFailure': '不合格后转全检', 'isFrozen': '冻结',
    'isRevOfRcd': '设为当前使用版本', 'lowerLimit': '下限', 'upperLimit': '上限',
    'defaultValue': '默认值', 'dataType': '数据类型', 'uom': '单位',
    'serviceDetails': '本次明细', 'dataPoints': '采集项目', 'dataValue': '采集值',
    'value': '值', 'pass': '是否通过', 'phase': '阶段', 'task_list': '任务清单',
    'task': '任务', 'taskName': '任务', 'taskList': '任务清单',
    'samplingPlan': '抽检计划', 'sampleSize': '样本数量', 'rejectCount': '不合格数量',
    'lossQuantity': '损失数量', 'loss_quantity': '损失数量', 'loss_reason_name': '损失原因',
    'reasonCode': '原因', 'reason': '原因', 'closeWhenZero': '数量归零时关闭容器',
    'closeWhenEmpty': '移空后关闭容器', 'reduceQty': '扣减来源数量',
    'childContainers': '子容器', 'toContainer': '目标容器', 'toContainerName': '新容器',
    'toContainerDetails': '拆分明细', 'fromContainer': '来源容器',
    'issueDetails': '投料明细', 'issueActuals': '本次投料', 'qtyIssued': '投料数量',
    'quantity_context': '数量核对', 'required_qty': '需求数量', 'remaining_qty': '剩余需求',
    'total_qty_issued': '累计已投数量', 'source_qty': '来源库存',
    'employee': '人员', 'owner': '负责人', 'newOwner': '新负责人', 'dueDate': '到期时间',
    'event': '质量事件', 'qualityObject': '质量对象', 'resolutionCode': '处理结论',
    'evidence_reference': '核实依据', 'review_note': '核实说明', 'conclusion': '核实结论',
    'body': '本次设置', 'request': '本次设置', 'after': '变更后', 'expected': '预计结果',
    'before': '变更前', 'state': '计划状态', 'expired': '计划已过期',
    'business_effects': '预计业务影响', 'current_quantity': '当前数量',
    'projected_remaining_quantity': '预计剩余数量', 'projected_close': '预计关闭容器',
    'samples_this_submission': '本次检验样本数', 'rejects_this_submission': '本次不合格数',
    'counted_groups_this_submission': '本次计数组数', 'deduction_basis': '扣量依据',
    'counted_reject_reason_count': '本组拒收原因数',
    'automatic_move': '自动过站', 'unit': '单位', 'quantity': '数量',
    'allow_full_inspection': '允许按现有规则进行全检',
    'allow_automatic_quantity_loss': '允许检验自动扣量',
    'allow_zero_quantity_loss': '允许检验扣量归零并按原生规则关闭',
    'automatic_quantity_loss': '检验自动扣量', 'full_inspection': '全检规则',
    'reason_breakdown': '按拒收原因扣量明细',
    'stored_inspect_all': '当前任务要求全检', 'failure_rule_enabled': '不合格后要求全检',
    'stored_required_samples': '当前任务要求样本数', 'current_whole_quantity': '容器总数量',
    'projected_quantity_after_loss': '预计扣量后容器数量',
    'conditional_required_samples_after_failure': 'MES判失败时的条件样本要求',
    'required': '需求数量', 'source': '来源', 'target': '目标',
    'net_required_before': '投料前剩余需求', 'quantity_this_request': '本次投料数量',
    'source_available_before': '来源可用数量', 'projected_source_quantity': '预计来源剩余数量',
    'projected_net_required': '预计剩余需求', 'recordAllQty': '处理全部数量',
    'adjustThruput': '调整已加工数量统计', 'disassociateWhenClosed': '关闭时解除装箱关系',
}
INSPECTION_LOSS_BASIS = {
    'counted_sample_size_loss': '本次计数样本合计',
    'measured_sample_size_loss': '本次测量样本数',
    'counted_reject_count_loss': '本次计数拒收合计',
    'counted_reason_loss': '本次检验组拒收原因及拒收数量',
}
VALUES = {
    'create': '创建', 'update': '修改', 'delete': '删除', 'ready': '待执行',
    'inspection_definition': '检验配置', 'sample_data_point': '检验指标',
    'sample_test': '检验项目', 'sampling_plan': '抽检计划', 'quantity_loss': '数量损失',
    'pre_production': '生产前', 'post_production': '生产后',
    'SamplingPlans': '抽检计划', 'SampleTests': '检验项目', 'SampleDataPoints': '检验指标',
}
TITLES = {
    'execute_quality_transaction': '提交质量业务操作',
    'review_quality_transaction': '登记质量事务的本地审计结果',
    'execute_data_collection_change': '维护采集配置',
    'collect_container_data': '提交容器采集数据', 'verify_collection_submission': '核实采集结果',
    'issue_components': '提交物料投料', 'execute_mfgorder_task_result': '提交工单任务结果',
    'execute_container_disassociation': '拆箱', 'execute_container_association': '装箱',
    'execute_container_split': '拆分容器数量', 'execute_container_combination': '合并容器数量',
    'execute_workflow_import_plan': '导入工艺路线', 'container_start': '创建生产容器',
    'container_move_in': '进站', 'container_move_out': '出站', 'container_move': '移动到下一站',
}
OBJECTS = {'spec': '工艺', 'workflow': '工艺路线', 'product': '产品', 'mfgorder': '工单',
           'operation': '工序', 'container_level': '容器层级', 'mfgline': '产线'}
HIDDEN = {'planid', 'plandigest', 'instanceid', 'cdotypename', 'servercontext', 'transport',
          'environment', 'method', 'scopekey', 'targetid', 'marker'}


def normalized(key):
    return re.sub(r'[_\s]', '', key).casefold()


def fields(value, indent=0):
    """Translate explicit values only; never infer absent booleans or quantities."""
    lines = []
    if isinstance(value, list):
        for i, item in enumerate(value, 1):
            lines.append('  ' * indent + f'第 {i} 项：')
            lines.extend(fields(item, indent + 1))
        return lines
    if not isinstance(value, dict):
        return ['  ' * indent + scalar(value)]
    labels = {normalized(k): v for k, v in LABELS.items()}
    for key, item in value.items():
        folded = normalized(key)
        if folded in HIDDEN or key.startswith('@'):
            continue
        label = labels.get(folded, f'其他设置（{key}）')
        prefix = '  ' * indent + label + '：'
        if isinstance(item, (dict, list)):
            lines.append(prefix + ('无' if not item else ''))
            lines.extend(fields(item, indent + 1))
        else:
            rendered = scalar(item)
            if folded == 'samplerate' and type(item) in (int, float) and item > 0:
                rendered = f'每 {item:g} 个抽检 1 个'
            lines.append(prefix + rendered)
    return lines


def scalar(value):
    if value is True:
        return '是'
    if value is False:
        return '否'
    if value is None:
        return '未提供'
    return VALUES.get(str(value), str(value))


def title(name):
    if name in TITLES:
        return TITLES[name]
    for prefix, action in [('create_', '创建'), ('update_', '修改'), ('delete_', '删除')]:
        if name.startswith(prefix):
            return action + OBJECTS.get(name[len(prefix):], '业务配置')
    if name.startswith(('recover_', 'resolve_', 'reconcile_', 'review_')):
        return '核实业务执行结果并登记处理意见'
    return '执行业务操作'


def json_blocks(text):
    decoder = json.JSONDecoder()
    start = 0
    while start < len(text):
        pos = text.find('{', start)
        if pos < 0:
            break
        try:
            value, end = decoder.raw_decode(text[pos:])
        except ValueError:
            start = pos + 1
            continue
        yield value
        start = pos + end


def business_effects(context):
    """Keep consequential projections, while excluding transport/verification flags."""
    if not isinstance(context, dict):
        return {}
    allowed = {'current_quantity', 'loss_quantity', 'projected_remaining_quantity', 'projected_close',
               'unit', 'samples_this_submission', 'rejects_this_submission', 'counted_groups_this_submission',
               'counted_reject_reason_count', 'automatic_move',
               'allow_full_inspection', 'allow_automatic_quantity_loss', 'allow_zero_quantity_loss', 'quantity', 'reason',
               'stored_inspect_all', 'failure_rule_enabled', 'stored_required_samples', 'current_whole_quantity'}
    allowed.update({'projected_quantity_after_loss', 'conditional_required_samples_after_failure', 'reason_breakdown'})
    result = {k: v for k, v in context.items() if k in allowed}
    for key in ('native_effects', 'automatic_quantity_loss', 'full_inspection'):
        selected = business_effects(context.get(key))
        if selected:
            if key == 'native_effects':
                result.update(selected)
            else:
                mode = context.get('mode')
                if key == 'automatic_quantity_loss' and isinstance(mode, str) and mode in INSPECTION_LOSS_BASIS:
                    selected['deduction_basis'] = INSPECTION_LOSS_BASIS[mode]
                result[key] = selected
    return result


def business_preview(calls, technical):
    """Render verified plan summaries produced by the existing preview adapters.

    The raw preview remains available separately for exact before/after review.
    Unknown business fields retain their label rather than disappearing.
    """
    lines = ['请核对本次操作：']
    for i, call in enumerate(calls, 1):
        name = (call.get('function') or {}).get('name', '')
        lines.append(f'{i}. {title(name)}')
    blocks = list(json_blocks(technical))
    for block in blocks:
        # Tool arguments consisting only of plan identity add no business content.
        if isinstance(block, dict) and set(block) <= {'plan_id', 'plan_digest'}:
            continue
        for key in ('body_json',):
            if isinstance(block, dict) and isinstance(block.get(key), str):
                try:
                    block = {**block, 'body': json.loads(block[key])}
                    block.pop(key)
                except ValueError:
                    pass
        # Runtime evidence is deliberately kept in the expandable exact preview.
        if isinstance(block, dict):
            effects = business_effects(block.get('server_context'))
            block = {k: v for k, v in block.items() if normalized(k) not in HIDDEN}
            if effects:
                block['business_effects'] = effects
        lines.extend(fields(block))
    if not blocks or any((c.get('function') or {}).get('name') == 'execute_workflow_import_plan' for c in calls):
        # Import plans already provide a human-readable route and deletion list.
        lines.extend(line for line in technical.splitlines()
                     if line and not re.match(r'\d+\. ', line)
                     and not line.startswith('待执行工具与参数'))
    for phrase, wording in [
        ('不扣来源库存', '库存影响：本次不会扣减来源库存。'),
        ('将扣减上述来源数量', '库存影响：本次会扣减所列来源的库存数量。'),
        ('不自动覆盖越限', '超出允许范围的数据不会自动放行。'),
        ('不支持退料净额/替代料', '本次不包含退料或替代料处理。'),
        ('现场执行', '此操作尚未完成现场执行验证，提交时可能被业务校验拒绝。'),
    ]:
        if phrase in technical:
            lines.append(wording)
    if '无法读取' in technical or '不可执行' in technical or '不应继续' in technical:
        lines.append('计划暂时无法核对，请取消并重新生成计划。')
    lines.append('完整设置与核对说明可在“执行明细”中查看。确认后仅提交本次计划；预计影响以实际执行结果为准，结果不明确时先核实再处理。')
    return '\n'.join(lines)
