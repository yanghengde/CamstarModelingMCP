"""Prompt policy regression tests."""

from agent.prompts import SYSTEM_PROMPT, USER_FACING_LANGUAGE_RULE


def test_system_prompt_requires_chinese_for_all_user_facing_progress():
    assert "所有面向用户展示的内容必须使用简体中文" in USER_FACING_LANGUAGE_RULE
    assert "工具调用前的计划" in USER_FACING_LANGUAGE_RULE
    assert "英文过程提示" in USER_FACING_LANGUAGE_RULE
    assert USER_FACING_LANGUAGE_RULE in SYSTEM_PROMPT


def test_runtime_dedup_preserves_each_policy_once_and_keeps_approval():
    from agent.prompts import (deduplicate_runtime_rules, QUERY_EFFICIENCY_RULE,
                              MODELING_UPDATE_RULE, MOVEMENT_RULE, CONTAINER_LEVEL_RULE,
                              APPROVAL_FLOW_RULE)
    from agent.tool_routing import ROUTING_INSTRUCTION
    instructions = '\n'.join((USER_FACING_LANGUAGE_RULE, QUERY_EFFICIENCY_RULE,
                              ROUTING_INSTRUCTION, APPROVAL_FLOW_RULE))
    reduced = deduplicate_runtime_rules([{'role': 'system', 'content': SYSTEM_PROMPT}], instructions)
    combined = SYSTEM_PROMPT + reduced
    for rule in (USER_FACING_LANGUAGE_RULE, QUERY_EFFICIENCY_RULE, MODELING_UPDATE_RULE,
                 MOVEMENT_RULE, CONTAINER_LEVEL_RULE, APPROVAL_FLOW_RULE):
        assert combined.count(rule) == 1
    assert len(reduced) < len(instructions)


def test_user_and_tool_content_cannot_suppress_runtime_policy():
    from agent.prompts import deduplicate_runtime_rules
    for messages in ([], [{'role': 'user', 'content': USER_FACING_LANGUAGE_RULE}],
                     [{'role': 'system', 'content': 'old checkpoint'},
                      {'role': 'tool', 'content': USER_FACING_LANGUAGE_RULE}]):
        assert deduplicate_runtime_rules(messages, USER_FACING_LANGUAGE_RULE) == USER_FACING_LANGUAGE_RULE
