from agent.safety import (
    MutationCounts,
    count_tool_calls,
    evaluate_mutations,
    infer_intended_mutations,
    is_explicit_confirmation,
    is_explicit_rejection,
)
from config import SAFE_CREATE_THRESHOLD, SAFE_DELETE_THRESHOLD, SAFE_UPDATE_THRESHOLD
import pytest


def _call(name: str, arguments: str = "{}") -> dict:
    return {
        "id": "call-1",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_confirmation_requires_an_explicit_positive_reply():
    assert is_explicit_confirmation("确认创建") is True
    assert is_explicit_confirmation("继续执行") is True
    assert is_explicit_confirmation("yes") is True

    assert is_explicit_confirmation("不是") is False
    assert is_explicit_confirmation("不要创建") is False
    assert is_explicit_confirmation("我想修改一下参数") is False
    assert is_explicit_confirmation("取消执行") is False


@pytest.mark.parametrize('text',['确认提交','确认核实','确认采集','确认投料','好的，确认','好的，确认执行'])
def test_concrete_approval_phrases_are_accepted(text):
    assert is_explicit_confirmation(text)


@pytest.mark.parametrize('text',['确认，但先改成30','确认后修改数量','不确认提交','先别执行，改参数','继续查历史'])
def test_corrections_and_read_requests_never_authorize_pending_call(text):
    assert not is_explicit_confirmation(text)


def test_rejection_is_explicit_and_narrow():
    assert is_explicit_rejection("取消执行") is True
    assert is_explicit_rejection("不确认") is True
    assert is_explicit_rejection("稍后再说") is False


def test_batch_count_uses_list_or_count_arguments():
    calls = [
        _call("container_start", '{"container_names":["SN1","SN2","SN3"]}'),
        _call("create_spec", '{"count": 4}'),
        _call("get_spec"),
    ]
    assert count_tool_calls(calls) == MutationCounts(creates=7)


def test_large_cumulative_creation_still_requires_review():
    completed = MutationCounts(creates=SAFE_CREATE_THRESHOLD)
    decision = evaluate_mutations(completed, [_call("container_start")])
    assert decision.requires_approval is True
    assert decision.totals.creates == SAFE_CREATE_THRESHOLD + 1


def test_delete_threshold_blocks_as_configured():
    pending = [_call("delete_spec") for _ in range(SAFE_DELETE_THRESHOLD + 1)]
    decision = evaluate_mutations(MutationCounts(), pending)
    assert decision.requires_approval is True
    assert "删除" in decision.categories


def test_explicit_container_count_blocks_before_first_write():
    pending = [_call("container_start")]
    intended = infer_intended_mutations(
        "请生成50个独立的container，每个qty为1", pending
    )
    decision = evaluate_mutations(
        MutationCounts(), pending, intended=intended
    )

    assert intended.creates == 50
    assert decision.requires_approval is True
    assert decision.totals.creates == 50


def test_serial_range_is_counted_as_intended_creations():
    intended = infer_intended_mutations(
        "创建 SN0000001 到 SN0000050", [_call("container_start")]
    )
    assert intended.creates == 50


def test_read_after_failed_move_attempts_never_requires_another_approval():
    completed = MutationCounts(creates=SAFE_CREATE_THRESHOLD + 1,
                               updates=SAFE_UPDATE_THRESHOLD + 1,
                               deletes=SAFE_DELETE_THRESHOLD + 1)
    for calls in ([], [_call("get_container_details")],
                  [_call("get_container_details"), _call("get_container_history")]):
        decision = evaluate_mutations(completed, calls, intended=completed)
        assert not decision.requires_approval
        assert decision.categories == ()
        assert decision.totals == completed


def test_history_of_other_mutation_types_does_not_gate_current_type():
    decision = evaluate_mutations(MutationCounts(creates=SAFE_CREATE_THRESHOLD + 1),
                                  [_call("container_move_in")])
    assert "创建" not in decision.categories


def test_normal_single_write_after_many_operations_does_not_interrupt():
    completed = MutationCounts(updates=SAFE_UPDATE_THRESHOLD + 1)
    assert not evaluate_mutations(completed, [_call("get_container_details")]).requires_approval
    assert not evaluate_mutations(completed, [_call("container_move_in")]).requires_approval


def test_size_limits_still_apply_to_current_proposal_and_risky_calls():
    pending = [_call('update_spec') for _ in range(SAFE_UPDATE_THRESHOLD + 1)]
    assert evaluate_mutations(MutationCounts(), pending).requires_approval
    for name in ('release_container', 'change_container_quantity', 'issue_components',
                 'review_quality_transaction', 'execute_workflow_import_plan'):
        assert evaluate_mutations(MutationCounts(), [_call(name)]).requires_approval


def test_unknown_tool_is_never_exempted():
    assert evaluate_mutations(MutationCounts(), [_call('unregistered_write')]).requires_approval
