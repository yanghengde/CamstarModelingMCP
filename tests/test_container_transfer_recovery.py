import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent.safety import plan_preview, evaluate_mutations, MutationCounts
from core.tool_catalog import TOOL_POLICIES
from core.transfer_attempt_lock import attempt_lock
from tools import container_transfer_plans as p, get_tool_func


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path / 'plans.sqlite')
    snap = {'environment': p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),
            'operation': 'Disassociate', 'parent': {'id': 'P', 'container': {'Name': 'P'}},
            'children': [{'child': {'id': 'C', 'container': {'Name': 'C'}}}],
            'body': {'container': {'name': 'P'}, 'childContainers': [{'name': 'C'}], 'disassociateAll': False}}
    dg = p.digest(snap)
    with p.db_connect() as db:
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',
                   ('plan', dg, json.dumps({'snapshot': snap, 'arguments': {}}), 'ready', time.time(), None))
    monkeypatch.setattr(p, 'preview_container_disassociate', AsyncMock(return_value='{}'))
    monkeypatch.setattr(p, 'checked_snapshot', lambda _: snap)
    request = AsyncMock(return_value='{"ok":true}')
    monkeypatch.setattr(p, 'request', request)
    monkeypatch.setattr(p, 'get_container_trace', AsyncMock(return_value='{}'))
    return dg, request


def run(coro):
    return json.loads(asyncio.run(coro))


def recover(dg, **changes):
    args = {'plan_id': 'plan', 'plan_digest': dg, 'evidence_reference': 'ops/incident/123',
            'review_note': 'Operator checked terminated local executor; server result still unknown.'}
    args.update(changes)
    return run(p.recover_container_transfer_attempt(**args))


def stranded_attempt():
    with p.db_connect() as db:
        db.execute("UPDATE plans SET state='in_flight',outcome=?", (json.dumps({'response': 'partial evidence'}),))
        db.execute('INSERT INTO transfer_attempts VALUES (?,?,?,NULL)', ('plan', 'attempt', time.time()))


def test_recovery_audits_and_keeps_lock(env):
    dg, request = env
    stranded_attempt()
    out = recover(dg)
    assert out['state_changed'] and out['state'] == 'unknown'
    assert not out['locks_released'] and not out['retry_allowed'] and not out['verified_success']
    assert not out['mes_write_performed']
    with p.db_connect() as db:
        record = json.loads(db.execute('SELECT record FROM transfer_recoveries').fetchone()[0])
        assert record['previous_outcome'] == {'response': 'partial evidence'}
        assert record['attempt_id'] == 'attempt'
        assert db.execute('SELECT state FROM plans').fetchone()[0] == 'unknown'
        assert db.execute('SELECT count(*) FROM resolutions').fetchone()[0] == 0
        # A different plan involving the same containers must still be blocked.
        db.execute("INSERT INTO plans SELECT 'other',digest,payload,'ready',created,NULL FROM plans WHERE id='plan'")
    assert not recover(dg)['state_changed']
    assert not run(p.execute_container_disassociation('plan', dg))['submitted']
    assert not run(p.execute_container_disassociation('other', dg))['submitted']
    request.assert_not_called()


def test_active_attempt_cannot_recover(env):
    stranded_attempt()
    with attempt_lock(p.PLAN_DB, 'plan'):
        assert not recover(env[0])['state_changed']
    with p.db_connect() as db:
        assert db.execute('SELECT state FROM plans').fetchone()[0] == 'in_flight'
        assert db.execute('SELECT count(*) FROM transfer_recoveries').fetchone()[0] == 0


def test_recovery_while_submission_pending(env):
    dg, request = env

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pending(*args, **kwargs):
            entered.set()
            await release.wait()
            return '{}'

        request.side_effect = pending
        task = asyncio.create_task(p.execute_container_disassociation('plan', dg))
        await entered.wait()
        try:
            out = json.loads(await p.recover_container_transfer_attempt('plan', dg, 'ops/123', 'check'))
            assert not out['state_changed']
        finally:
            release.set()
            await task

    asyncio.run(scenario())
    assert request.call_count == 1


@pytest.mark.parametrize('stage', ['request', 'evidence'])
def test_cancellation_persists_unknown(env, monkeypatch, stage):
    dg, request = env
    if stage == 'request':
        request.side_effect = asyncio.CancelledError()
    else:
        monkeypatch.setattr(p, 'get_container_trace', AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        run(p.execute_container_disassociation('plan', dg))
    with p.db_connect() as db:
        state, payload = db.execute('SELECT state,outcome FROM plans').fetchone()
        assert state == 'unknown'
        outcome = json.loads(payload)
        assert outcome['interrupted'] and not outcome['verified_success']
        assert db.execute('SELECT finished FROM transfer_attempts').fetchone()[0] is not None
    with attempt_lock(p.PLAN_DB, 'plan'):
        pass
    assert not run(p.execute_container_disassociation('plan', dg))['submitted']
    assert request.call_count == 1


@pytest.mark.parametrize('state', ['ready', 'unknown', 'manually_confirmed_committed'])
def test_wrong_state_rejected(env, state):
    stranded_attempt()
    with p.db_connect() as db:
        db.execute('UPDATE plans SET state=?', (state,))
    assert not recover(env[0])['state_changed']


@pytest.mark.parametrize('bad', ['digest', 'environment', 'payload', 'legacy', 'evidence', 'note'])
def test_invalid_recovery(env, monkeypatch, bad):
    stranded_attempt()
    kwargs = {}
    if bad == 'digest': kwargs['plan_digest'] = 'wrong'
    if bad == 'environment': monkeypatch.setattr(p, 'CAMSTAR_SHOPFLOOR_BASE_URL', 'other')
    if bad == 'evidence': kwargs['evidence_reference'] = ' '
    if bad == 'note': kwargs['review_note'] = ''
    with p.db_connect() as db:
        if bad == 'payload': db.execute("UPDATE plans SET payload='{}'")
        if bad == 'legacy': db.execute('DELETE FROM transfer_attempts')
    assert not recover(env[0], **kwargs)['state_changed']
    with p.db_connect() as db:
        assert db.execute('SELECT state FROM plans').fetchone()[0] == 'in_flight'
        assert db.execute('SELECT count(*) FROM transfer_recoveries').fetchone()[0] == 0
    env[1].assert_not_called()


def test_confirmation_and_discovery(env):
    stranded_attempt()
    name = 'recover_container_transfer_attempt'
    args = {'plan_id': 'plan', 'plan_digest': env[0], 'evidence_reference': 'ops/123', 'review_note': 'check'}
    calls = [{'function': {'name': name, 'arguments': json.dumps(args)}}]
    text = plan_preview(calls)
    assert 'childContainers' in text and 'ops/123' in text and 'unknown' in text and '保留' in text
    assert TOOL_POLICIES[name].always_confirm and get_tool_func(name)
    assert evaluate_mutations(MutationCounts(), calls).requires_approval


def test_recovery_audit_and_state_are_atomic(env):
    stranded_attempt()
    with p.db_connect() as db:
        db.execute("""CREATE TRIGGER reject_recovery BEFORE UPDATE OF state ON plans
                      BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
    with pytest.raises(sqlite3.DatabaseError):
        recover(env[0])
    with p.db_connect() as db:
        assert db.execute('SELECT state FROM plans').fetchone()[0] == 'in_flight'
        assert db.execute('SELECT count(*) FROM transfer_recoveries').fetchone()[0] == 0


def test_final_persistence_failure_keeps_recoverable_attempt(env, monkeypatch):
    original = p.db_connect
    calls = 0

    def fail_final_save():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise sqlite3.OperationalError('simulated disk failure')
        return original()

    monkeypatch.setattr(p, 'db_connect', fail_final_save)
    with pytest.raises(sqlite3.OperationalError):
        run(p.execute_container_disassociation('plan', env[0]))
    monkeypatch.setattr(p, 'db_connect', original)
    with p.db_connect() as db:
        assert db.execute('SELECT state FROM plans').fetchone()[0] == 'in_flight'
        assert db.execute('SELECT finished FROM transfer_attempts').fetchone()[0] is None
    assert recover(env[0])['state_changed']
    assert env[1].call_count == 1


def test_recovered_outcome_requires_separate_manual_resolution(env):
    stranded_attempt()
    assert recover(env[0])['state_changed']
    out = run(p.resolve_container_transfer_outcome('plan', env[0], 'committed',
                                                   'server/final-transaction/456', 'Checked final server audit.'))
    assert out['state'] == 'manually_confirmed_committed' and not out['verified_success']
    assert out['manual_resolution']['previous_outcome']['recovery']['attempt_id'] == 'attempt'
    assert not run(p.execute_container_disassociation('plan', env[0]))['submitted']
    env[1].assert_not_called()


def test_os_lock_excludes_other_process(env):
    script = '''
import sys
from core.transfer_attempt_lock import attempt_lock, AttemptBusy
try:
    with attempt_lock(sys.argv[1], 'plan'):
        sys.exit(2)
except AttemptBusy:
    sys.exit(0)
'''
    with attempt_lock(p.PLAN_DB, 'plan'):
        child = subprocess.run([sys.executable, '-c', script, str(p.PLAN_DB)],
                               cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
    assert child.returncode == 0, child.stderr.decode()


def test_process_crash_releases_guard_for_recovery(env):
    script = '''
import os, sqlite3, sys, time
from core.transfer_attempt_lock import attempt_lock
with attempt_lock(sys.argv[1], 'plan'):
    with sqlite3.connect(sys.argv[1]) as db:
        db.execute("UPDATE plans SET state='in_flight'")
        db.execute('INSERT INTO transfer_attempts VALUES (?,?,?,NULL)', ('plan','crashed',time.time()))
    os._exit(23)
'''
    child = subprocess.run([sys.executable, '-c', script, str(p.PLAN_DB)],
                           cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
    assert child.returncode == 23, child.stderr.decode()
    out = recover(env[0])
    assert out['state_changed'] and out['recovery']['attempt_id'] == 'crashed'
    assert not out['locks_released']
    env[1].assert_not_called()
