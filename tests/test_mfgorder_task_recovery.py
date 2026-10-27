import asyncio
import json
import sqlite3

import pytest

from tests.test_mfgorder_tasks import env
from tests.test_mfgorder_task_preview import preview_env
from tests.test_mfgorder_task_plans import plan_env, prepare, run
from tools import mfgorder_task_recovery as r, mfgorder_task_plans as p, get_tool_func
from core.transfer_attempt_lock import attempt_lock
from core.tool_catalog import TOOL_POLICIES
from agent.safety import plan_preview, evaluate_mutations, MutationCounts


@pytest.fixture
def attempted(plan_env):
    ready = prepare()
    run(p.execute_mfgorder_task_result(ready['plan_id'],ready['plan_digest']))
    return ready


def review(ready, action='resolve', conclusion='committed', **kw):
    args = {'plan_id':ready['plan_id'],'plan_digest':ready['plan_digest'],
            'evidence_reference':'server/final-audit/123','review_note':'Operator verified final transaction audit.'}
    args.update(kw)
    fn = r.resolve_mfgorder_task_outcome if action == 'resolve' else r.recover_mfgorder_task_attempt
    if action == 'resolve': args['conclusion'] = conclusion
    return run(fn(**args))


@pytest.mark.parametrize('conclusion',['committed','not_committed'])
def test_manual_audit_and_old_plan_never_reopens(plan_env, attempted, conclusion):
    out = review(attempted, conclusion=conclusion)
    assert out['state'] == 'manually_confirmed_' + conclusion and out['locks_released']
    assert not out['verified_success'] and not out['original_plan_reusable'] and not out['mes_write_performed']
    assert out['review']['previous_outcome']['submission_attempted']
    assert not review(attempted)['state_changed']
    assert not run(p.execute_mfgorder_task_result(attempted['plan_id'],attempted['plan_digest']))['submitted']
    assert plan_env[3].call_count == 1


@pytest.mark.parametrize('action',['resolve','recover'])
def test_active_lock_rejects(plan_env, attempted, action):
    with p.db_connect() as db:
        if action == 'recover': db.execute("UPDATE plans SET state='in_flight'")
    with attempt_lock(p.PLAN_DB, attempted['plan_id']):
        assert not review(attempted, action)['state_changed']


def test_recovery_keeps_order_occupied_until_separate_resolution(plan_env, attempted):
    with p.db_connect() as db: db.execute("UPDATE plans SET state='in_flight'")
    out = review(attempted, 'recover')
    assert out['state'] == 'unknown' and not out['locks_released']
    assert not review(attempted, 'recover')['state_changed']
    another = prepare()
    assert not run(p.execute_mfgorder_task_result(another['plan_id'],another['plan_digest']))['submitted']
    resolved = review(attempted)
    assert resolved['review']['previous_outcome']['review']['action'] == 'recover'
    with p.db_connect() as db: assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 2
    assert plan_env[3].call_count == 1


@pytest.mark.parametrize('bad',['ready','in_flight','digest','environment','payload','evidence','note','missing_attempt','conclusion'])
def test_invalid_resolution(plan_env, attempted, monkeypatch, bad):
    kw = {}
    with p.db_connect() as db:
        if bad in ('ready','in_flight'): db.execute('UPDATE plans SET state=?',(bad,))
        if bad == 'payload': db.execute("UPDATE plans SET payload='{}'")
        if bad == 'missing_attempt': db.execute('DELETE FROM attempts')
    if bad == 'digest': kw['plan_digest'] = 'wrong'
    if bad == 'environment': monkeypatch.setattr(p,'CAMSTAR_SHOPFLOOR_BASE_URL','other')
    if bad == 'evidence': kw['evidence_reference'] = ''
    if bad == 'note': kw['review_note'] = ' '
    if bad == 'conclusion': kw['conclusion'] = 'maybe'
    assert not review(attempted, **kw)['state_changed']
    with p.db_connect() as db: assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 0


def test_audit_and_state_atomic(plan_env, attempted):
    with p.db_connect() as db:
        db.execute("CREATE TRIGGER reject_review BEFORE UPDATE OF state ON plans BEGIN SELECT RAISE(ABORT,'failure'); END")
    with pytest.raises(sqlite3.DatabaseError): review(attempted)
    with p.db_connect() as db:
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 0
        assert db.execute('SELECT state FROM plans').fetchone()[0] == 'unknown'


def test_confirmation_and_policy(plan_env, attempted):
    for name, action in [('recover_mfgorder_task_attempt','recover'),('resolve_mfgorder_task_outcome','resolve')]:
        with p.db_connect() as db: db.execute('UPDATE plans SET state=?',('in_flight' if action == 'recover' else 'unknown',))
        args = {'plan_id':attempted['plan_id'],'plan_digest':attempted['plan_digest'],
                'conclusion':'not_committed','evidence_reference':'audit/456','review_note':'checked'}
        calls = [{'function':{'name':name,'arguments':json.dumps(args)}}]
        text = plan_preview(calls)
        assert 'mfgOrderTask' in text and 'audit/456' in text
        assert get_tool_func(name) and TOOL_POLICIES[name].always_confirm
        assert evaluate_mutations(MutationCounts(),calls).requires_approval
