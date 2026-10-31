"""Approval exemptions use immutable local plans; no MES or LLM requests."""
import json
import time

import pytest

from agent.safety import MutationCounts, count_tool_calls, evaluate_mutations
from agent import langgraph_runtime as runtime
from tools import quality_plans as plans


def call(name, args):
    return {'id': 'call', 'function': {'name': name, 'arguments': json.dumps(args)}}


@pytest.fixture
def saved_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(plans, 'PLAN_DB', tmp_path / 'plans.sqlite')

    def save(**changes):
        snapshot = {'kind': 'inspection_definition', 'action': 'create', 'api': 'modeling',
                    'definition_kind': 'sample_data_point', 'entity': 'SampleDataPoints',
                    'method': 'POST', 'path': '/api/SampleDataPoints', 'name': 'IQC_POINT', 'revision': 'A',
                    'before': None, 'target_id': None, 'body': {'name': 'IQC_POINT', 'revision': 'A'},
                    'environment': plans.environment()}
        snapshot.update(changes)
        fingerprint = plans.digest(snapshot)
        key = str(time.time_ns())
        with plans.db_connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',
                       (key, fingerprint, json.dumps(snapshot), 'ready', time.time()))
        return call('execute_quality_transaction', {'plan_id': key, 'plan_digest': fingerprint})
    return save


@pytest.mark.parametrize('kind,entity', [('sample_data_point', 'SampleDataPoints'),
                                       ('sample_test', 'SampleTests'), ('sampling_plan', 'SamplingPlans')])
def test_routine_new_quality_configuration_needs_no_extra_confirmation(saved_plan, kind, entity):
    proposal = saved_plan(definition_kind=kind, entity=entity, path='/api/' + entity)
    assert not evaluate_mutations(MutationCounts(updates=100), [proposal]).requires_approval
    assert count_tool_calls([proposal]) == MutationCounts(creates=1)


@pytest.mark.parametrize('changes', [
    {'action': 'update'}, {'action': 'revise'}, {'kind': 'sampling_binding'},
    {'kind': 'quantity_loss', 'api': 'shopfloor'}, {'before': {'Name': 'already exists'}},
    {'path': '/api/Products'}, {'method': 'PUT'}, {'quality_signature': {'kind': 'signature'}},
    {'environment': {'modeling': 'different deployment'}},
])
def test_changed_scope_or_risk_is_not_exempt(saved_plan, changes):
    assert evaluate_mutations(MutationCounts(), [saved_plan(**changes)]).requires_approval


@pytest.mark.parametrize('state', ['unknown', 'executing', 'used'])
def test_used_or_uncertain_plan_never_gets_exemption(saved_plan, state):
    proposal = saved_plan()
    args = json.loads(proposal['function']['arguments'])
    with plans.db_connect() as db:
        db.execute('UPDATE plans SET state=? WHERE id=?', (state, args['plan_id']))
    assert evaluate_mutations(MutationCounts(), [proposal]).requires_approval


@pytest.mark.parametrize('change', ['digest', 'payload', 'expired', 'missing', 'extra_arguments'])
def test_invalid_plan_or_claimed_exemption_fails_closed(saved_plan, change):
    proposal = saved_plan()
    args = json.loads(proposal['function']['arguments'])
    with plans.db_connect() as db:
        if change == 'payload':
            db.execute("UPDATE plans SET payload='{}' WHERE id=?", (args['plan_id'],))
        if change == 'expired':
            db.execute('UPDATE plans SET created=? WHERE id=?', (time.time() - 3601, args['plan_id']))
        if change == 'missing':
            db.execute('DELETE FROM plans WHERE id=?', (args['plan_id'],))
    if change == 'digest':
        args['plan_digest'] = 'wrong'
    if change == 'extra_arguments':
        args['confirmation_required'] = False
    assert evaluate_mutations(MutationCounts(), [call('execute_quality_transaction', args)]).requires_approval


def test_many_new_definitions_still_require_batch_review(saved_plan):
    from config import SAFE_CREATE_THRESHOLD
    assert evaluate_mutations(MutationCounts(), [saved_plan()] * (SAFE_CREATE_THRESHOLD + 1)).requires_approval


def test_user_can_explicitly_require_review_even_for_routine_creation(saved_plan):
    from agent.safety import requests_execution_review
    for text in ('先让我确认再执行', '执行前给我确认', '等我确认', '确认后再提交'):
        assert requests_execution_review(text)
        assert evaluate_mutations(MutationCounts(), [saved_plan()], require_review=True).requires_approval
    assert not requests_execution_review('创建检验项目 IQC')
    assert not evaluate_mutations(MutationCounts(), [call('get_container_details', {})], require_review=True).requires_approval


def test_sequential_normal_moves_do_not_interrupt_but_sensitive_followup_does(monkeypatch):
    monkeypatch.setattr(runtime, 'interrupt', lambda payload: pytest.fail('unnecessary confirmation'))
    for count in range(12):
        assert runtime._policy_node({'update_count': count, 'pending_tool_calls': [
            call('container_move_in' if count % 2 == 0 else 'container_move_out', {'container_name': 'SN1'})]}) == {}
    observed = []
    monkeypatch.setattr(runtime, 'plan_preview', lambda calls: '库存扣减')
    monkeypatch.setattr(runtime, 'interrupt', lambda payload: observed.append(payload) or {'approved': True})
    result = runtime._policy_node({'update_count': 12, 'pending_tool_calls': [
        call('change_container_quantity', {'container_name': 'SN1', 'qty': 2})]})
    assert len(observed) == 1 and result['approved_plan']
