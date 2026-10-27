import asyncio
import copy
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import component_issue_bulk as b, component_issue_bulk_receipts as r
from tools import component_issue_plans as p, mcp, get_tool_func
from core.tool_catalog import TOOL_POLICIES
from core.transfer_attempt_lock import attempt_lock
from agent.safety import plan_preview, evaluate_mutations, MutationCounts
from scripts.component_issue_fault_harness import echo, SHOPFLOOR, ROOT

DATA = Path(__file__).resolve().parents[1]/'docs/t05-fixtures'


def run(awaitable):
    return json.loads(asyncio.run(awaitable))


@pytest.fixture
def env(tmp_path, monkeypatch):
    captured = json.loads((DATA/'bulk-derived-verified-F.json').read_text(encoding='utf-8'))
    proof = json.loads((DATA/'bulk-derived-receipt-F.json').read_text(encoding='utf-8'))
    rows = {read['table']: json.loads(read['response']) for read in proof['reads']}
    state = {'captured': captured, 'rows': rows, 'marker': None}
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'plans.sqlite3')
    monkeypatch.setattr(p, 'CAMSTAR_SHOPFLOOR_BASE_URL', SHOPFLOOR)
    monkeypatch.setattr(r, 'CAMSTAR_SHOPFLOOR_BASE_URL', SHOPFLOOR)
    async def status(name, *_):
        return json.dumps(captured['source_status'] if name.endswith('-SRC') else captured['target_status'])
    monkeypatch.setattr(b, 'get_container_details', AsyncMock(side_effect=status))
    monkeypatch.setattr(b, 'get_component_requirement', AsyncMock(side_effect=lambda *_: json.dumps(captured['requirement'])))
    async def read(method, path, **kwargs):
        if path == '/api/AdHoc':
            assert kwargs['retry_read_only'] is True
            query = kwargs['body']['queryText']
            return json.dumps([] if 'WHERE ParentContainerId=' in query else [captured['source_balance']])
        assert path == '/api/ComponentIssue' and kwargs['params']['execute'] == 'false'
        result = echo(kwargs['body']); result['ServiceDetails'][0]['Actuals'][0]['CDOTypeId'] = '6540'
        return json.dumps(result)
    monkeypatch.setattr(b, 'request', AsyncMock(side_effect=read))
    async def execute(method, path, **kwargs):
        assert path == '/api/ComponentIssue' and kwargs['params']['execute'] == 'true'
        state['marker'] = kwargs['body']['comments']
        return '{"CompletionMsg":"OFFLINE fixture submitted"}'
    state['post'] = AsyncMock(side_effect=execute); monkeypatch.setattr(p, 'request', state['post'])
    monkeypatch.setattr(p, 'get_component_requirement', AsyncMock(return_value=json.dumps(captured['requirement'])))
    monkeypatch.setattr(p, 'get_container_details', AsyncMock(return_value=json.dumps(captured['source_status'])))
    monkeypatch.setattr(p, 'get_component_issue_history', AsyncMock(return_value='{"rows":[]}'))
    monkeypatch.setattr(p, 'get_container_history', AsyncMock(return_value='{}'))
    async def history(method, path, **kwargs):
        assert method == 'POST' and path == '/api/AdHoc' and kwargs['retry_read_only'] is True
        query = kwargs['body']['queryText']
        table = next(t for t in rows if 'FROM '+t+' WHERE' in query)
        result = copy.deepcopy(rows[table])
        if table == 'HistoryMainline' and isinstance(result, list) and result:
            result[0]['Comments'] = state['marker']
        return json.dumps(result)
    state['history'] = AsyncMock(side_effect=history); monkeypatch.setattr(r, 'request', state['history'])
    return state


def prepare():
    result = run(p.prepare_bulk_component_issue('CDX-T05-20261025-F-DST', 'CDX-T05-20261025-F-SRC',
                                               'CDX-T05-20261025-F-COMP', '1', 2))
    assert result.get('state') == 'ready', result
    return result


def args(plan): return plan['plan_id'], plan['plan_digest']
def execute(plan): return run(p.issue_components(*args(plan)))
def reconcile(plan): return run(p.reconcile_component_issue(*args(plan)))
def stored(plan): return run(p.get_component_issue_plan(plan['plan_id']))


def seed_peer(snapshot, state='unknown'):
    key = uuid.uuid4().hex; snap = copy.deepcopy(snapshot); snap['marker'] = 'Codex issue '+key
    fingerprint = p.digest(snap)
    with p.db_connect() as db:
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',
                   (key, fingerprint, json.dumps({'plan_id': key, 'marker': snap['marker'], 'snapshot': snap}), state, time.time()))
    return {'plan_id': key, 'plan_digest': fingerprint}


def mode5_plan(env, target_id):
    snap = json.loads((DATA/'formal-execution-D.json').read_text(encoding='utf-8'))['persisted_plan']['plan']['snapshot']
    snap.pop('marker'); snap['environment'] = SHOPFLOOR
    target = {'Name': 'MODE5-TARGET', 'InstanceId': target_id}
    snap['context']['Container'] = target
    ref = {'name': target['Name'], 'instanceId': target_id}
    snap['body']['container'] = ref; snap['body']['serviceDetails'][0]['actuals'][0]['toContainer'] = ref
    snap['arguments'].update(container_name=target['Name'], container_instance_id=target_id)
    env['mode5_snapshot'] = snap
    return seed_peer(snap, 'ready')


def test_shared_plan_confirmation_and_registry(env):
    plan = prepare(); snap = stored(plan)['plan']['snapshot']
    assert snap['issue_control'] == 2 and len(p.participants(snap)) == 2
    assert snap['body']['serviceDetails'][0]['actuals'][0]['@odata.type'] == '#shopfloor.ActualsBulk'
    assert plan['quantity_context']['projected_source_quantity'] == '1'
    summary = p.confirmation_summary(*args(plan))
    assert '来源扣减' in summary and 'F-SRC' in summary and 'F-DST' in summary
    assert 'source_available_before' in summary and 'quantity_this_request' in summary
    rendered = plan_preview([{'function': {'name': 'issue_components', 'arguments': json.dumps(dict(zip(('plan_id','plan_digest'), args(plan))))}}])
    assert '来源扣减' in rendered
    assert get_tool_func('prepare_bulk_component_issue')
    assert TOOL_POLICIES['prepare_bulk_component_issue'].effect == 'read'
    assert TOOL_POLICIES['issue_components'].always_confirm and not plan['submitted']
    env['post'].assert_not_called()


def test_mode2_execution_stays_unknown_then_releases_only_verified_plan(env):
    first, second = prepare(), prepare()
    result = execute(first)
    assert result['state'] == 'unknown' and result['issue_control'] == 2
    assert len(result['occupied_container_ids']) == 2
    assert not execute(first)['submitted'] and not execute(second)['submitted']
    assert env['post'].call_count == 1
    proof = reconcile(first)
    assert proof['verified_success'] and proof['locks_released']
    assert proof['evidence']['source_container_id'] == '00041080000000e7'
    assert stored(first)['outcome']['verification']['previous_outcome'] == result
    assert not execute(first)['submitted']
    assert execute(second)['submission_attempted']  # simulation keeps a stable eligible MES snapshot
    assert env['post'].call_count == 2


@pytest.mark.parametrize('role', ['source', 'target'])
@pytest.mark.parametrize('direction', ['bulk_first', 'quantity_first'])
def test_cross_mode_occupancy_in_both_directions(env, monkeypatch, role, direction):
    bulk = prepare(); snap = stored(bulk)['plan']['snapshot']
    identifier = snap['source_context' if role == 'source' else 'context']['Container']['InstanceId']
    quantity = mode5_plan(env, identifier)
    monkeypatch.setattr(p, 'preview', AsyncMock(return_value=copy.deepcopy(env['mode5_snapshot'])))
    first, other = (bulk, quantity) if direction == 'bulk_first' else (quantity, bulk)
    assert execute(first)['submission_attempted']
    assert not execute(other)['submitted'] and env['post'].call_count == 1


@pytest.mark.parametrize('role', ['same_source', 'same_target', 'source_as_target', 'target_as_source'])
@pytest.mark.parametrize('state', ['unknown', 'in_flight'])
def test_mode2_participants_overlap_in_any_role_is_blocked(env, role, state):
    plan = prepare(); snap = stored(plan)['plan']['snapshot']; other = copy.deepcopy(snap)
    unrelated = '00041080000000ff'
    if role == 'same_source': other['body']['container']['instanceId'] = unrelated
    elif role == 'same_target': other['body']['serviceDetails'][0]['actuals'][0]['fromContainer']['instanceId'] = unrelated
    elif role == 'source_as_target':
        other['body']['container']['instanceId'] = snap['source_context']['Container']['InstanceId']
        other['body']['serviceDetails'][0]['actuals'][0]['fromContainer']['instanceId'] = unrelated
    else:
        other['body']['container']['instanceId'] = unrelated
        other['body']['serviceDetails'][0]['actuals'][0]['fromContainer']['instanceId'] = snap['context']['Container']['InstanceId']
    seed_peer(other, state)
    assert not execute(plan)['submitted']; env['post'].assert_not_called()


def test_unrelated_participants_and_other_environment_do_not_block(env):
    plan = prepare(); other = copy.deepcopy(stored(plan)['plan']['snapshot'])
    other['environment'] = 'https://different.invalid/Shopfloor'; seed_peer(other)
    other['environment'] = SHOPFLOOR
    other['body']['container']['instanceId'] = '00041080000000aa'
    other['body']['serviceDetails'][0]['actuals'][0]['fromContainer']['instanceId'] = '00041080000000bb'
    seed_peer(other)
    assert execute(plan)['submission_attempted'] and env['post'].call_count == 1


@pytest.mark.parametrize('change', ['source_qty', 'source_activity', 'target_activity', 'unit', 'demand', 'children'])
def test_changed_source_target_or_demand_refuses_old_snapshot(env, change):
    plan = prepare(); captured = env['captured']
    if change == 'source_qty':
        captured['source_status']['data']['ContainerStatusDetails']['Qty'] = 2.5
        captured['source_balance']['Qty'] = '2.5'
    elif change == 'source_activity': captured['source_status']['data']['ContainerStatusDetails']['LastActivityDate'] = 'changed'
    elif change == 'target_activity': captured['target_status']['data']['ContainerStatusDetails']['LastActivityDate'] = 'changed'
    elif change == 'unit': captured['source_balance']['UOMId'] = '0000000000000001'
    elif change == 'demand': captured['requirement']['data']['IssueDetails']['NetQtyRequired'] = 1
    else: captured['source_balance']['ParentContainerId'] = '0000000000000001'
    assert not execute(plan)['submitted']; env['post'].assert_not_called()


@pytest.mark.parametrize('change', ['mode', 'source', 'arguments', 'extra_actual', 'top_level', 'marker', 'source_context'])
def test_rehashed_tampered_plan_never_bypasses_contract(env, change):
    plan = prepare(); value = stored(plan)['plan']; snap = value['snapshot']
    if change == 'mode': snap['issue_control'] = 5
    elif change == 'source': snap['body']['serviceDetails'][0]['actuals'][0]['fromContainer']['instanceId'] = '0000000000000001'
    elif change == 'arguments': snap['arguments']['source_container_name'] = 'different'
    elif change == 'extra_actual': snap['body']['serviceDetails'][0]['actuals'] *= 2
    elif change == 'top_level': snap['body']['issueActualDetails'] = [{}]
    elif change == 'marker': snap['marker'] = 'different'
    else: snap['source_context']['Status'] = 2
    fingerprint = p.digest(snap)
    with p.db_connect() as db:
        db.execute('UPDATE plans SET digest=?,payload=? WHERE id=?', (fingerprint, json.dumps(value), plan['plan_id']))
    plan['plan_digest'] = fingerprint
    assert not execute(plan)['submitted']; env['post'].assert_not_called()


def test_legacy_mode5_snapshot_keeps_its_digest_and_is_executable(env, monkeypatch):
    plan = mode5_plan(env, '00041080000000aa')
    before = stored(plan); assert 'issue_control' not in before['plan']['snapshot']
    monkeypatch.setattr(p, 'preview', AsyncMock(return_value=copy.deepcopy(env['mode5_snapshot'])))
    assert execute(plan)['issue_control'] == 5
    assert stored(plan)['plan_digest'] == before['plan_digest']


def test_expired_or_wrong_digest_never_executes(env):
    plan = prepare()
    assert not run(p.issue_components(plan['plan_id'], 'bad'))['submitted']
    with p.db_connect() as db: db.execute('UPDATE plans SET created=?', (time.time()-3601,))
    assert not execute(plan)['submitted']; env['post'].assert_not_called()


def test_cancel_timeout_and_single_plan_concurrency_keep_both_participants(env):
    plan = prepare()
    async def failed(*args, **kwargs): raise asyncio.CancelledError()
    env['post'].side_effect = failed
    with pytest.raises(asyncio.CancelledError): execute(plan)
    assert stored(plan)['state'] == 'unknown' and stored(plan)['outcome']['interrupted']
    assert not execute(prepare())['submitted']
    env['post'].assert_awaited_once()


def test_concurrent_shared_source_plans_claim_only_once(env):
    first, second = prepare(), prepare()
    async def simultaneous():
        return await asyncio.gather(*(p.issue_components(*args(plan)) for plan in (first, second)))
    results = [json.loads(value) for value in asyncio.run(simultaneous())]
    assert sum(result.get('submission_attempted') is True for result in results) == 1
    assert env['post'].call_count == 1


@pytest.mark.parametrize('table', ['attempts', 'receipt_checks', 'reviews'])
def test_state_and_audit_writes_are_atomic(env, table):
    plan = prepare()
    if table != 'attempts': execute(plan)
    with p.db_connect() as db:
        db.execute(f"CREATE TRIGGER reject_audit BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        (execute if table == 'attempts' else reconcile)(plan)
    assert stored(plan)['state'] == ('ready' if table == 'attempts' else 'unknown')
    with p.db_connect() as db:
        assert db.execute('SELECT count(*) FROM receipt_checks').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 0
    assert env['post'].call_count == (0 if table == 'attempts' else 1)


def test_late_bulk_history_preserves_original_outcome_and_hold(env):
    first, second = prepare(), prepare(); original = execute(first)
    rows = env['rows']['IssueActualsHistory']; env['rows']['IssueActualsHistory'] = []
    check = reconcile(first)
    assert not check['locks_released'] and stored(first)['outcome'] == original
    assert not execute(second)['submitted']
    env['rows']['IssueActualsHistory'] = rows
    assert reconcile(first)['verified_success']
    assert stored(first)['outcome']['verification']['previous_outcome'] == original


def test_recovery_and_manual_resolution_remain_explicit_and_not_automatic(env):
    plan = prepare()
    with p.db_connect() as db:
        db.execute("UPDATE plans SET state='in_flight'")
        db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)', (plan['plan_id'], 'crashed', time.time()))
    review_args = (*args(plan), 'ops/process-exited', 'Checked local worker termination; server may still finish.')
    with attempt_lock(p.PLAN_DB, plan['plan_id']):
        assert not run(p.recover_component_issue_attempt(*review_args))['state_changed']
    recovered = run(p.recover_component_issue_attempt(*review_args))
    assert recovered['state'] == 'unknown' and not recovered['locks_released']
    assert not execute(prepare())['submitted']
    summary = p.review_summary(*args(plan), 'verify', None, None)
    assert 'F-SRC' in summary and 'source_available_before' in summary
    result = run(p.resolve_component_issue_outcome(*args(plan), 'committed', 'server/final-audit', 'Final source/target transaction checked.'))
    assert result['state'] == 'manually_confirmed_committed' and not result['verified_success']
    assert not execute(plan)['submitted']


def test_attempt_changed_during_receipt_cannot_release_participants(env):
    plan = prepare(); execute(plan); original = env['history'].side_effect
    async def change(*args, **kwargs):
        result = await original(*args, **kwargs)
        with p.db_connect() as db: db.execute('UPDATE attempts SET finished=finished+1')
        return result
    env['history'].side_effect = change
    assert not reconcile(plan)['locks_released'] and stored(plan)['state'] == 'unknown'


def test_final_save_failure_retains_recoverable_dual_occupancy(env, monkeypatch):
    plan = prepare(); identifier = stored(plan)['plan']['snapshot']['source_context']['Container']['InstanceId']
    quantity = mode5_plan(env, identifier)
    monkeypatch.setattr(p, 'preview', AsyncMock(return_value=copy.deepcopy(env['mode5_snapshot'])))
    with p.db_connect() as db:
        db.execute("CREATE TRIGGER reject_final BEFORE UPDATE OF state ON plans WHEN NEW.state='unknown' "
                   "BEGIN SELECT RAISE(ABORT,'final save failed'); END")
    with pytest.raises(sqlite3.IntegrityError): execute(plan)
    assert stored(plan)['state'] == 'in_flight'
    with p.db_connect() as db:
        assert db.execute('SELECT finished FROM attempts WHERE plan_id=?', (plan['plan_id'],)).fetchone()[0] is None
        db.execute('DROP TRIGGER reject_final')
    assert not execute(quantity)['submitted']
    assert run(p.recover_component_issue_attempt(*args(plan), 'ops/save-failure', 'Worker stopped after final-save failure.'))['state'] == 'unknown'
    assert not execute(quantity)['submitted']
    assert reconcile(plan)['verified_success']
    assert env['post'].call_count == 1


def test_mcp_prepare_execute_confirm_and_verify_in_one_shared_library(env):
    async def scenario():
        async with Client(mcp) as client:
            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                assert not result.is_error
                return json.loads(result.content[0].text)
            plan = await call('prepare_bulk_component_issue', {'container_name': 'CDX-T05-20261025-F-DST',
                'source_container_name': 'CDX-T05-20261025-F-SRC', 'product_name': 'CDX-T05-20261025-F-COMP',
                'product_revision': '1', 'qty': 2})
            keys = dict(zip(('plan_id','plan_digest'), args(plan)))
            calls = [{'function': {'name': 'issue_components', 'arguments': json.dumps(keys)}}]
            assert evaluate_mutations(MutationCounts(), calls).requires_approval
            assert 'F-SRC' in plan_preview(calls)
            assert (await call('issue_components', keys))['state'] == 'unknown'
            assert (await call('reconcile_component_issue', keys))['verified_success']
            assert not (await call('issue_components', keys))['submitted']
    asyncio.run(scenario())
    assert env['post'].call_count == 1


@pytest.mark.parametrize('qty', [True, '2', 0, -1])
def test_mcp_invalid_bulk_quantity_never_reads_or_stores(env, qty):
    async def scenario():
        async with Client(mcp) as client:
            result = await client.call_tool('prepare_bulk_component_issue', {'container_name':'T', 'source_container_name':'S',
                'product_name':'P','product_revision':'1','qty':qty}, raise_on_error=False)
            assert result.is_error
    asyncio.run(scenario())
    b.get_container_details.assert_not_called()
    assert not p.PLAN_DB.exists()


def test_real_bulk_worker_kill_retains_source_occupancy_until_receipt(env, tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'fault-plans.sqlite3')
    first = prepare(); source_id = stored(first)['plan']['snapshot']['source_context']['Container']['InstanceId']
    quantity = mode5_plan(env, source_id)
    monkeypatch.setattr(p, 'preview', AsyncMock(return_value=copy.deepcopy(env['mode5_snapshot'])))
    (tmp_path/'control.json').write_text(json.dumps({'mode':2,'execute':'hold','receipt':'full'}), encoding='utf-8')
    child = subprocess.Popen([sys.executable, str(ROOT/'scripts/component_issue_fault_harness.py'),
        str(tmp_path), *args(first)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING':'utf-8'})
    try:
        deadline = time.monotonic()+40
        log = tmp_path/'transport.jsonl'
        while time.monotonic() < deadline:
            if log.exists() and 'execute_accepted' in log.read_text(encoding='utf-8'): break
            if child.poll() is not None:
                output, error = child.communicate()
                pytest.fail(output+error[-3000:])
            time.sleep(0.02)
        else: pytest.fail('Worker did not reach isolated execution')
        assert stored(first)['state'] == 'in_flight'
        assert not execute(quantity)['submitted']; env['post'].assert_not_called()
        review = (*args(first), 'ops/child-killed', 'Test worker termination observed; no server conclusion.')
        assert not run(p.recover_component_issue_attempt(*review))['state_changed']
        child.kill(); child.communicate(timeout=10)
        assert run(p.recover_component_issue_attempt(*review))['state'] == 'unknown'
        assert not execute(quantity)['submitted']
        env['marker'] = stored(first)['plan']['marker']
        assert reconcile(first)['verified_success']
        assert not execute(first)['submitted']
        assert execute(quantity)['submission_attempted']
        assert sum(json.loads(line)['stage']=='execute_accepted' for line in log.read_text(encoding='utf-8').splitlines()) == 1
        with p.db_connect() as db:
            record = db.execute("SELECT record FROM reviews WHERE plan_id=? AND kind='recover'", (first['plan_id'],)).fetchone()[0]
        assert json.loads(record)['attempt'][3] is None
    finally:
        if child.poll() is None:
            child.kill(); child.communicate(timeout=10)


def test_fresh_stdio_client_uses_shared_bulk_plan_and_receipt(env, tmp_path, monkeypatch):
    from fastmcp.client.transports import PythonStdioTransport
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'fault-plans.sqlite3')
    prepare()  # supplies the child transport's captured source/target snapshot
    (tmp_path/'control.json').write_text(json.dumps({'mode':2,'execute':'ok','receipt':'full'}), encoding='utf-8')
    async def scenario():
        transport = PythonStdioTransport(ROOT/'scripts/component_issue_fault_harness.py',
            args=[str(tmp_path),'stdio'], cwd=str(ROOT), python_cmd=sys.executable)
        async with Client(transport, timeout=40, init_timeout=40) as client:
            assert 'prepare_bulk_component_issue' in {t.name for t in await client.list_tools()}
            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                assert not result.is_error
                return json.loads(result.content[0].text)
            plan = await call('prepare_bulk_component_issue', {'container_name':'CDX-T05-20261025-F-DST',
                'source_container_name':'CDX-T05-20261025-F-SRC','product_name':'CDX-T05-20261025-F-COMP',
                'product_revision':'1','qty':2})
            keys = dict(zip(('plan_id','plan_digest'), args(plan)))
            assert (await call('issue_components', keys))['state'] == 'unknown'
            assert (await call('reconcile_component_issue', keys))['verified_success']
            assert (await call('get_component_issue_plan', {'plan_id':plan['plan_id']}))['state'] == 'verified_committed'
            assert not (await call('issue_components', keys))['submitted']
    asyncio.run(scenario())
    entries = [json.loads(line) for line in (tmp_path/'transport.jsonl').read_text(encoding='utf-8').splitlines()]
    assert sum(entry['stage']=='execute_accepted' for entry in entries) == 1


def test_two_real_processes_sharing_source_can_claim_only_one_plan(env, tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'fault-plans.sqlite3')
    first, second = prepare(), prepare()
    control = tmp_path/'control.json'
    control.write_text(json.dumps({'mode':2,'execute':'hold','receipt':'full'}), encoding='utf-8')
    children = [subprocess.Popen([sys.executable, str(ROOT/'scripts/component_issue_fault_harness.py'),
        str(tmp_path), *args(plan)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING':'utf-8'}) for plan in (first, second)]
    try:
        deadline = time.monotonic()+40
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children): break
            time.sleep(0.02)
        else: pytest.fail('Two workers failed to resolve their shared-source contention')
        loser = next(child for child in children if child.poll() is not None)
        output, error = loser.communicate(timeout=10)
        assert loser.returncode == 0, error[-3000:]
        assert json.loads(output)['submitted'] is False
        with p.db_connect() as db:
            assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM plans WHERE state='in_flight'").fetchone()[0] == 1
        next_control = tmp_path/'next-control.json'
        next_control.write_text(json.dumps({'mode':2,'execute':'ok','receipt':'full'}), encoding='utf-8')
        # A Windows reader can briefly deny FILE_SHARE_DELETE. Retry only
        # this local fixture exchange; the MES attempt remains single-shot.
        replace_deadline = time.monotonic()+1
        while True:
            try:
                os.replace(next_control, control)
                break
            except PermissionError:
                if os.name != 'nt' or time.monotonic() >= replace_deadline:
                    raise
                time.sleep(0.01)
        winner = next(child for child in children if child is not loser)
        output, error = winner.communicate(timeout=15)
        assert winner.returncode == 0, error[-3000:]
        assert json.loads(output)['state'] == 'unknown'
        entries = [json.loads(line) for line in (tmp_path/'transport.jsonl').read_text(encoding='utf-8').splitlines()]
        assert sum(entry['stage']=='execute_accepted' for entry in entries) == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill(); child.communicate(timeout=10)
