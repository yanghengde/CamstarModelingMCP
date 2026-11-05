import json

from agent.approval_display import business_preview, fields
from agent.safety import approval_prompt, SafetyDecision, MutationCounts


def call(name, args=None):
    return {'function': {'name': name, 'arguments': json.dumps(args or {})}}


def test_sampling_plan_approval_translates_business_options_and_hides_internal_data():
    payload = {'kind': 'inspection_definition', 'action': 'create', 'name': 'IQC_DEMO',
               'request': {'revision': 'A', 'sampleRate': 5, 'inspectAll': False,
                           'moveContainerOnCompletion': False,
                           'samplingPlanDetails': [{'sampleTest': {'name': 'IQC_TEST', 'revision': 'A',
                                                                     'instanceId': 'private-id'},
                                                    'inspectAllQtyOnFailure': True}]},
               'server_context': {'echo_verified': False, 'note': 'PUT technical note'}}
    technical = '1. execute_quality_transaction: {"plan_id":"secret","plan_digest":"hash"}\n质量事务计划：' + json.dumps(payload)
    text = business_preview([call('execute_quality_transaction')], technical)
    for expected in ['创建', 'IQC_DEMO', '版本：A', '每 5 个抽检 1 个', '全检：否',
                     '检验完成后自动过站：否', '不合格后转全检：是', 'IQC_TEST']:
        assert expected in text
    for internal in ['plan_id', 'secret', 'hash', 'sampleRate', 'echo_verified', 'PUT', 'private-id', 'execute_quality']:
        assert internal not in text


def test_absent_settings_are_not_invented_and_unknown_business_values_are_kept():
    text = '\n'.join(fields({'sampleRate': 5, 'siteSpecificLimit': 9}))
    assert '全检' not in text and '自动过站' not in text
    assert 'siteSpecificLimit' in text and '9' in text


def test_long_parameters_keep_late_business_effects_and_full_details():
    from agent.safety import plan_preview
    calls = [call('update_spec', {'body_json': json.dumps({'description': '说明' * 2100,
                                                         'inspectAllQtyOnFailure': True})})]
    details = plan_preview(calls)
    assert 'inspectAllQtyOnFailure' in details
    assert '不合格后转全检：是' in business_preview(calls, details)


def test_partial_update_only_shows_requested_changes():
    text = business_preview([call('update_spec')],
                            '1. update_spec: ' + json.dumps({'key': 'verified-id', 'body_json': '{"description":"新说明"}'}))
    assert '修改工艺' in text and '说明：新说明' in text
    assert 'body_json' not in text and '全检' not in text


def test_inventory_effects_and_missing_plan_are_explicit():
    text = business_preview([call('issue_components')],
                            '确认投料：{"request":{"qty":3}}\n将扣减上述来源数量；不支持退料净额/替代料。结果不明不重试。')
    assert '数量：3' in text and '会扣减' in text and '退料或替代料' in text
    invalid = business_preview([call('execute_quality_transaction')], '质量事务计划无法读取，不应继续。')
    assert '请取消并重新生成计划' in invalid


def test_inspection_automatic_loss_is_not_hidden_with_runtime_metadata():
    technical = json.dumps({'request': {'container': {'name': 'SN1'}}, 'server_context': {
        'counted_groups_this_submission': 3, 'samples_this_submission': 6, 'rejects_this_submission': 3,
        'native_effects': {'mode': 'counted_reject_count_loss', 'allow_automatic_quantity_loss': True, 'unit': {'name': 'PCS'},
                           'automatic_quantity_loss': {'quantity': '3', 'projected_remaining_quantity': '7',
                                                       'native_child_defaults': {'technical_flag': True}}}}})
    text = business_preview([call('execute_quality_transaction')], technical)
    assert '允许检验自动扣量：是' in text and '数量：3' in text and '预计剩余数量：7' in text
    assert '本次计数组数：3' in text and '扣量依据：本次计数拒收合计' in text
    assert '本次检验样本数：6' in text and '本次不合格数：3' in text
    assert 'PCS' in text and 'technical_flag' not in text


def test_combined_inspection_displays_loss_and_conditional_whole_quantity():
    technical = json.dumps({'server_context': {'native_effects': {
        'mode': 'measured_sample_size_loss', 'allow_full_inspection': True,
        'allow_automatic_quantity_loss': True,
        'automatic_quantity_loss': {'quantity': '3', 'projected_remaining_quantity': '7', 'projected_close': False},
        'full_inspection': {'stored_required_samples': 5, 'projected_quantity_after_loss': '7',
                            'conditional_required_samples_after_failure': 7,
                            'native_processing_order': ['inspection_counters', 'quantity_loss', 'failure_full_inspection_rule']}}}})
    text = business_preview([call('execute_quality_transaction')], technical)
    assert '允许按现有规则进行全检：是' in text and '允许检验自动扣量：是' in text
    assert '当前任务要求样本数：5' in text and '预计扣量后容器数量：7' in text
    assert 'MES判失败时的条件样本要求：7' in text
    assert 'inspection_counters' not in text


def test_zero_loss_confirmation_exposes_closure_and_permission():
    technical = json.dumps({'server_context': {'native_effects': {
        'mode': 'counted_sample_size_loss',
        'automatic_quantity_loss': {'quantity': '3', 'projected_remaining_quantity': '0',
                                    'allow_zero_quantity_loss': True, 'projected_close': True}}}})
    text = business_preview([call('execute_quality_transaction')], technical)
    assert '预计剩余数量：0' in text and '预计关闭容器：是' in text
    assert '允许检验扣量归零并按原生规则关闭：是' in text


def test_route_rebuild_keeps_deletions_and_order():
    text = business_preview([call('execute_workflow_import_plan')],
                            '1. execute_workflow_import_plan: {"plan_id":"hidden"}\n路线WF/A：删除原步骤 2 个，新建步骤 3 个。\n删除：旧工序1、旧工序2')
    assert '删除原步骤 2 个' in text and '旧工序1、旧工序2' in text
    assert 'hidden' not in text


def test_generic_prompt_does_not_misreport_tool_counts_as_business_changes():
    text = approval_prompt(SafetyDecision(True, ('批量导入/路线重建（影响数量需核对）',), MutationCounts(updates=4)))
    assert '4 条修改' not in text and '路线重建' not in text
    assert '确认执行' in text


def test_older_pending_approval_is_reformatted_without_changing_stored_plan(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from agent import langgraph_runtime as runtime
    calls = [call('container_move_out')]
    approval = {'message': '旧确认\n待执行工具与参数：\n1. container_move_out: {"container_name":"SN1"}',
                'plan_id': 'original-fingerprint'}
    snapshot = SimpleNamespace(tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value=approval)])],
                               values={'pending_tool_calls': calls}, next=('policy',))
    monkeypatch.setattr(runtime, '_graph', SimpleNamespace(aget_state=AsyncMock(return_value=snapshot)))
    monkeypatch.setattr(runtime, 'user_memories', {'user': {'sessions': {'one': {}}}})
    shown = asyncio.run(runtime.get_workflow_status('user', 'one'))['approval']
    assert '出站' in shown['message'] and 'SN1' in shown['message']
    assert 'container_move_out' not in shown['message']
    assert shown['plan_id'] == approval['plan_id']
    assert 'execution_details' not in approval  # The checkpoint remains untouched.
    assert 'container_move_out' in shown['execution_details']
