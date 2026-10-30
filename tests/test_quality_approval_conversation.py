"""Typical multi-turn language selects the quality handlers; it never grants permission."""
import pytest
from agent.tool_routing import select_tools,ROUTING_INSTRUCTION
from core.tool_catalog import TOOL_POLICIES


def chosen(messages):
    return {t['function']['name'] for t in select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],messages)}


@pytest.mark.parametrize('question,expected,write',[
    ('查看质量完成审批模板','get_quality_approval_template',False),
    ('查询审批旧周期及取消记录','get_quality_approval_history',False),
    ('请给质量事件挂接审批模板并路由','prepare_quality_approval_assignment',True),
    ('拒绝后的审批，请修改说明再重提','prepare_quality_approval_action',True),
    ('请重新提交审批','prepare_quality_approval_action',True),
    ('请修改被拒绝CAPA的纠正措施说明','prepare_capa_approval_revision',True),
    ('查看CAPA修改后重新提交审批需要哪些条件','prepare_capa_approval_revision',False),
    ('请按角色替代批准这条审批','prepare_quality_approval_decision',True),
    ('以角色授权批准这条记录','prepare_quality_approval_decision',True),
    ('查询角色替代批准记录，不要执行','get_quality_approval_history',False),
    ('查询NCR更新所需的电子签名要求，不要执行','get_nonconformance_report',False),
    ('查看NCR更新电子签名条件','get_nonconformance_report',False),
    ('请更新NCR备注并按要求多签和协签','prepare_nonconformance_report',True),
    ('请把当前审批条目转派给李工','prepare_quality_approval_delegation',True),
    ('查看该条目转派历史，不要执行','get_quality_approval_context',False),
    ('请批准我受委托的这条审批','prepare_quality_approval_decision',True),
    ('请正式提交质量事件E的草稿','prepare_quality_event_submission',True),
    ('提交事件E','prepare_quality_event_submission',True),
    ('查询事件草稿正式提交条件，不要提交','get_quality_event_update_context',False),
])
def test_approval_language_routes_to_expected_handler(question,expected,write):
    names=chosen([dict(role='user',content=question)])
    assert expected in names and ('execute_quality_transaction' in names) is write


def test_multiturn_context_keeps_quality_group_without_inventing_missing_parameters():
    messages=[dict(role='user',content='查看组织Org的质量事件E审批上下文'),
        dict(role='assistant',content='已读取原对象和原审批表ID。'),dict(role='user',content='现在修改说明后重提')]
    names=chosen(messages)
    assert {'get_quality_approval_history','prepare_quality_event_update','prepare_quality_approval_action','execute_quality_transaction'}<=names
    assert 'previous_cycle_gmt' in ROUTING_INSTRUCTION and '不是单独挂表' in ROUTING_INSTRUCTION
    assert TOOL_POLICIES['execute_quality_transaction'].always_confirm


def test_readonly_repeated_cycle_question_does_not_select_executor():
    names=chosen([dict(role='user',content='查询审批取消后重提的历史，不要执行')])
    assert 'get_quality_approval_history' in names and 'execute_quality_transaction' not in names
