import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from tests.test_collection_submission import setup, preview, run
from tools import collection_submission as cs, collection_recovery as r, collection_history, get_tool_func
from agent.safety import plan_preview, evaluate_mutations, MutationCounts
from core.tool_catalog import TOOL_POLICIES
from core.transfer_attempt_lock import attempt_lock


@pytest.fixture
def env(monkeypatch,tmp_path):
    fixture=setup(monkeypatch,tmp_path)
    monkeypatch.setattr(collection_history,'get_container_collection_history',AsyncMock(return_value='{"rows":[]}'))
    ready=preview()
    return ready,fixture


def review(ready,action='resolve',conclusion='committed',**changes):
    args={'plan_id':ready['plan_id'],'plan_digest':ready['plan_digest'],'evidence_reference':'audit/final/123',
          'review_note':'Operator checked final server audit.'}
    args.update(changes)
    if action=='resolve':return run(r.resolve_collection_submission_outcome(**args,conclusion=conclusion))
    return run(r.recover_collection_submission_attempt(**args))


def submit(ready):return run(cs.collect_container_data(ready['plan_id'],ready['plan_digest']))


@pytest.mark.parametrize('conclusion',['committed','not_committed'])
def test_manual_resolution_and_audit(env,conclusion):
    ready,_=env;submit(ready)
    out=review(ready,conclusion=conclusion)
    assert out['state']=='manually_confirmed_'+conclusion and out['locks_released']
    assert not out['verified_success'] and not out['original_plan_reusable'] and not out['mes_write_performed']
    assert out['review']['previous_outcome']['submission_attempted']
    assert not review(ready)['state_changed'] and not submit(ready)['submitted']
    with cs.db_connect() as db:assert db.execute('SELECT count(*) FROM collection_reviews').fetchone()[0]==1


def test_legacy_unknown_attestation_is_explicit(env):
    ready,_=env
    with cs.db_connect() as db:db.execute("UPDATE plans SET state='unknown',outcome=?",(json.dumps({'response':'legacy'}),))
    out=review(ready)
    assert out['state_changed'] and out['review']['attempt_tracking']=='legacy_unknown_without_tracking'
    assert out['review']['attempt_id'] is None and not out['review']['automatically_verified']


def test_legacy_in_flight_not_recovered(env):
    ready,_=env
    with cs.db_connect() as db:db.execute("UPDATE plans SET state='in_flight'")
    assert not review(ready,'recover')['state_changed']
    assert not review(ready)['state_changed']


def test_recovery_keeps_lock_then_separate_resolution(env):
    ready,_=env;submit(ready)
    with cs.db_connect() as db:
        db.execute("UPDATE plans SET state='in_flight'")
        db.execute('UPDATE collection_attempts SET finished=NULL')
    out=review(ready,'recover')
    assert out['state']=='unknown' and not out['locks_released']
    other=preview();assert not submit(other)['submitted']
    resolved=review(ready)
    assert resolved['review']['previous_outcome']['review']['action']=='recover'
    assert not review(ready,'recover')['state_changed']


@pytest.mark.parametrize('stage',['submit','history'])
def test_cancelled_attempt_persists_unknown(env,monkeypatch,stage):
    ready,fixture=env
    if stage=='submit':
        reply=fixture[2]
        async def request(*a,**kw):
            if kw['params']['execute']=='true':raise asyncio.CancelledError()
            return json.dumps(reply)
        fixture[3].side_effect=request
    else:monkeypatch.setattr(collection_history,'get_container_collection_history',AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):submit(ready)
    out=run(cs.get_collection_submission_plan(ready['plan_id']))
    assert out['state']=='unknown' and out['outcome']['interrupted']
    with cs.db_connect() as db:assert db.execute('SELECT finished FROM collection_attempts').fetchone()[0] is not None
    assert not submit(ready)['submitted']


@pytest.mark.parametrize('action',['recover','resolve'])
def test_active_lock_blocks_management(env,action):
    ready,_=env;submit(ready)
    if action=='recover':
        with cs.db_connect() as db:db.execute("UPDATE plans SET state='in_flight'")
    with attempt_lock(cs.PLAN_DB,ready['plan_id']):assert not review(ready,action)['state_changed']


@pytest.mark.parametrize('bad',['state','digest','payload','environment','evidence','conclusion'])
def test_invalid_review(env,monkeypatch,bad):
    ready,_=env;submit(ready);changes={};conclusion='committed'
    if bad=='state':
        with cs.db_connect() as db:db.execute("UPDATE plans SET state='ready'")
    if bad=='digest':changes['plan_digest']='wrong'
    if bad=='payload':
        with cs.db_connect() as db:db.execute("UPDATE plans SET payload='{}'")
    if bad=='environment':monkeypatch.setattr(cs,'CAMSTAR_SHOPFLOOR_BASE_URL','other')
    if bad=='evidence':changes['evidence_reference']=' '
    if bad=='conclusion':conclusion='unknown'
    assert not review(ready,conclusion=conclusion,**changes)['state_changed']


def test_atomic_review_rollback(env):
    ready,_=env;submit(ready)
    with cs.db_connect() as db:db.execute("CREATE TRIGGER reject_review BEFORE UPDATE OF state ON plans BEGIN SELECT RAISE(ABORT,'failure'); END")
    with pytest.raises(sqlite3.DatabaseError):review(ready)
    with cs.db_connect() as db:
        assert db.execute('SELECT state FROM plans').fetchone()[0]=='unknown'
        assert db.execute('SELECT count(*) FROM collection_reviews').fetchone()[0]==0


@pytest.mark.parametrize('change',['body','arguments','environment'])
def test_stored_plan_integrity_before_submission(env,monkeypatch,change):
    ready,fixture=env;fixture[3].reset_mock()
    if change=='environment':monkeypatch.setattr(cs,'CAMSTAR_SHOPFLOOR_BASE_URL','other')
    else:
        with cs.db_connect() as db:
            data=json.loads(db.execute('SELECT payload FROM plans').fetchone()[0])
            if change=='body':data['snapshot']['body']['skipValidation']=True
            else:data['arguments']['values']['X']=2
            db.execute('UPDATE plans SET payload=?',(json.dumps(data),))
    assert not submit(ready)['submitted']
    assert all(c.kwargs['params']['execute']=='false' for c in fixture[3].call_args_list)


def test_confirmation_and_policy(env):
    ready,_=env;submit(ready)
    for name in ('recover_collection_submission_attempt','resolve_collection_submission_outcome'):
        with cs.db_connect() as db:db.execute('UPDATE plans SET state=?',('in_flight' if name.startswith('recover') else 'unknown',))
        args={k:ready[k] for k in ('plan_id','plan_digest')}
        args.update(conclusion='not_committed',evidence_reference='final/123',review_note='checked')
        calls=[{'function':{'name':name,'arguments':json.dumps(args)}}]
        assert 'dataValue' in plan_preview(calls)
        assert get_tool_func(name) and TOOL_POLICIES[name].always_confirm
        assert evaluate_mutations(MutationCounts(),calls).requires_approval
