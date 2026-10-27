"""Real processes/SQLite/OS locks, captured MES replies, and an MCP SDK client."""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

from scripts.component_issue_fault_harness import FaultHarness, ROOT
from tools import component_issue_plans as p, mcp

WORKER = ROOT / 'scripts/component_issue_fault_harness.py'


@pytest.fixture
def harness(tmp_path, monkeypatch):
    value = FaultHarness(tmp_path)
    control(value, execute='ok', receipt='full')
    value.install(monkeypatch)
    yield value
    asyncio.run(value.client.aclose())


def control(harness, **values):
    path = harness.directory / 'control.json'
    temporary = harness.directory / 'next-control.json'
    current = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    temporary.write_text(json.dumps({**current, **values}), encoding='utf-8')
    # Windows readers briefly prevent replacement without FILE_SHARE_DELETE.
    # Retry only this local test-control exchange, never an MES request.
    deadline = time.monotonic() + 1
    while True:
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if os.name != 'nt' or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def run(awaitable):
    return json.loads(asyncio.run(awaitable))


def prepare(harness):
    result = run(p.prepare_component_issue(**harness.snapshot['arguments']))
    assert result.get('state') == 'ready', result
    return result


def args(plan):
    return plan['plan_id'], plan['plan_digest']


def stored(plan):
    return run(p.get_component_issue_plan(plan['plan_id']))


def recover(plan):
    return run(p.recover_component_issue_attempt(*args(plan),
        'offline-integration/child-exit', 'Observed test worker exit; server outcome remains unknown.'))


def events(harness):
    path = harness.directory / 'transport.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def executions(harness):
    return [event for event in events(harness) if event['stage'] == 'execute_accepted']


def start_child(harness, plan):
    return subprocess.Popen([sys.executable, str(WORKER), str(harness.directory), *args(plan)],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})


def finish(child, expected=0):
    try:
        output, error = child.communicate(timeout=40)
    except subprocess.TimeoutExpired:
        child.kill()
        output, error = child.communicate(timeout=10)
        pytest.fail('Test worker did not complete: ' + error[-2000:])
    assert child.returncode == expected, error[-4000:]
    return json.loads(output) if expected == 0 else None


def wait_for_execution(harness, child):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if executions(harness):
            return
        if child.poll() is not None:
            output, error = child.communicate()
            pytest.fail('Worker exited before transport entry: ' + output + error[-4000:])
        time.sleep(0.02)
    pytest.fail('Worker did not reach the isolated execution transport')


def test_live_worker_excludes_recovery_and_another_process_same_container(harness):
    first, second = prepare(harness), prepare(harness)
    control(harness, execute='hold')
    active = start_child(harness, first)
    try:
        wait_for_execution(harness, active)
        assert stored(first)['state'] == 'in_flight'
        assert not recover(first)['state_changed']
        assert not run(p.reconcile_component_issue(*args(first)))['state_changed']
        same = finish(start_child(harness, first))
        other = finish(start_child(harness, second))
        assert same['submitted'] is False and other['submitted'] is False
        assert stored(second)['state'] == 'ready'
        assert len(executions(harness)) == 1
        control(harness, execute='ok')
        assert finish(active)['state'] == 'unknown'
    finally:
        if active.poll() is None:
            active.kill()
            active.communicate(timeout=10)


def test_killed_worker_recovers_without_replay_and_late_history_resolves(harness):
    first, other = prepare(harness), prepare(harness)
    control(harness, execute='hold', receipt='late_actual')
    child = start_child(harness, first)
    try:
        wait_for_execution(harness, child)
        child.kill()
        child.communicate(timeout=10)
        assert child.returncode != 0
        assert stored(first)['state'] == 'in_flight'
        with p.db_connect() as db:
            attempt = db.execute('SELECT attempt_id,finished FROM attempts').fetchone()
        assert attempt[0] and attempt[1] is None
        recovery = recover(first)
        assert recovery['state'] == 'unknown' and not recovery['locks_released']
        assert not recovery['mes_write_performed']
        assert not run(p.issue_components(*args(first)))['submitted']
        assert not run(p.issue_components(*args(other)))['submitted']
        incomplete = run(p.reconcile_component_issue(*args(first)))
        assert len(incomplete['evidence']['reads']) == 4
        assert not incomplete['locks_released'] and stored(first)['state'] == 'unknown'
        control(harness, execute='ok', receipt='full')
        verified = run(p.reconcile_component_issue(*args(first)))
        assert verified['verified_success'] and verified['state'] == 'verified_committed'
        assert stored(first)['outcome']['verification']['previous_outcome']['review']['attempt'][1] == attempt[0]
        with p.db_connect() as db:
            assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM receipt_checks').fetchone()[0] == 2
            assert {row[0] for row in db.execute('SELECT kind FROM reviews')} == {'recover', 'verify'}
        assert not finish(start_child(harness, first))['submitted']
        assert len(executions(harness)) == 1
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_crash_after_response_before_final_save_retains_attempt(harness):
    plan = prepare(harness)
    control(harness, execute='crash_after_response')
    finish(start_child(harness, plan), expected=23)
    assert events(harness)[-1]['stage'] == 'response_delivered_before_final_save'
    assert stored(plan)['state'] == 'in_flight' and stored(plan)['outcome'] is None
    assert recover(plan)['state'] == 'unknown'
    assert run(p.reconcile_component_issue(*args(plan)))['verified_success']
    assert not run(p.issue_components(*args(plan)))['submitted']
    assert len(executions(harness)) == 1


@pytest.mark.parametrize('unavailable', ['empty', 'duplicate', 'permission', 'late_actual'])
def test_lost_response_and_inconclusive_receipt_keep_occupancy(harness, unavailable):
    first, second = prepare(harness), prepare(harness)
    control(harness, execute='disconnect', receipt=unavailable)
    result = run(p.issue_components(*args(first)))
    assert result['state'] == 'unknown' and 'Injected response loss' in result['response']
    assert len(executions(harness)) == 1  # The production HTTP dispatcher must not retry POST.
    before = stored(first)['outcome']
    check = run(p.reconcile_component_issue(*args(first)))
    assert not check['verified_success'] and not check['locks_released']
    assert stored(first)['outcome'] == before
    assert not run(p.issue_components(*args(second)))['submitted']
    control(harness, receipt='full')
    assert run(p.reconcile_component_issue(*args(first)))['verified_success']
    assert stored(first)['outcome']['verification']['previous_outcome'] == before
    assert len(executions(harness)) == 1


def test_final_save_database_failure_can_be_recovered(harness):
    plan = prepare(harness)
    with p.db_connect() as db:
        db.execute("CREATE TRIGGER reject_final BEFORE UPDATE OF state ON plans "
                   "WHEN NEW.state='unknown' BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='disk failure'):
        run(p.issue_components(*args(plan)))
    assert stored(plan)['state'] == 'in_flight'
    with p.db_connect() as db:
        assert db.execute('SELECT finished FROM attempts').fetchone()[0] is None
        db.execute('DROP TRIGGER reject_final')
    assert recover(plan)['state'] == 'unknown'
    assert run(p.reconcile_component_issue(*args(plan)))['verified_success']
    assert len(executions(harness)) == 1


@pytest.mark.parametrize('quantity', [True, False, '2', 0, -1])
def test_mcp_rejects_invalid_quantity_before_any_preview(harness, monkeypatch, quantity):
    preview = AsyncMock(side_effect=p.preview)
    monkeypatch.setattr(p, 'preview', preview)

    async def scenario():
        async with Client(mcp) as client:
            result = await client.call_tool('prepare_component_issue',
                {**harness.snapshot['arguments'], 'qty': quantity}, raise_on_error=False)
            assert result.is_error

    asyncio.run(scenario())
    preview.assert_not_called()
    assert not executions(harness) and not harness.database.exists()


def test_fresh_stdio_mcp_client_plans_executes_queries_and_reconciles(harness):
    async def scenario():
        transport = PythonStdioTransport(WORKER, args=[str(harness.directory), 'stdio'],
                                         cwd=str(ROOT), python_cmd=sys.executable)
        async with Client(transport, timeout=40, init_timeout=40) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {'prepare_component_issue', 'issue_components', 'get_component_issue_plan',
                    'recover_component_issue_attempt', 'reconcile_component_issue'} <= names

            for invalid in (True, '2'):
                denied = await client.call_tool('prepare_component_issue',
                    {**harness.snapshot['arguments'], 'qty': invalid}, raise_on_error=False)
                assert denied.is_error

            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                assert not result.is_error
                return json.loads(result.content[0].text)

            plan = await call('prepare_component_issue', harness.snapshot['arguments'])
            assert plan['state'] == 'ready'
            keys = dict(zip(('plan_id', 'plan_digest'), args(plan)))
            assert (await call('issue_components', keys))['state'] == 'unknown'
            control(harness, receipt='late_actual')
            assert not (await call('reconcile_component_issue', keys))['locks_released']
            control(harness, receipt='full')
            assert (await call('reconcile_component_issue', keys))['verified_success']
            assert (await call('get_component_issue_plan', {'plan_id': plan['plan_id']}))['state'] == 'verified_committed'
            assert not (await call('issue_components', keys))['submitted']

    asyncio.run(scenario())
    assert len(executions(harness)) == 1


def test_authenticated_http_mcp_issue_flow(harness, monkeypatch):
    from fastapi.testclient import TestClient
    import web.app as app_module

    monkeypatch.setattr(app_module, 'ENABLE_MCP_HTTP', True)
    monkeypatch.setattr(app_module, 'MCP_API_KEY', 'offline-issue-client')
    monkeypatch.setattr(app_module, 'MCP_ALLOWED_HOSTS', ['testserver:*'])
    monkeypatch.setattr(app_module, 'MCP_ALLOWED_ORIGINS', [])
    monkeypatch.setattr(app_module, 'AGENT_ENGINE', 'legacy')
    monkeypatch.setattr(app_module, 'register_tools', AsyncMock())
    monkeypatch.setattr(app_module, 'init_memory', lambda: None)
    headers = {'Accept': 'application/json, text/event-stream',
               'Authorization': 'Bearer offline-issue-client'}

    with TestClient(app_module.create_app()) as client:
        initialize = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'offline-t05-acceptance', 'version': '1.0'}}}
        assert client.post('/mcp/', json=initialize).status_code == 401
        assert not harness.database.exists()
        assert client.post('/mcp/', json=initialize, headers=headers).status_code == 200
        request_id = 1

        def call(name, arguments):
            nonlocal request_id
            request_id += 1
            response = client.post('/mcp/', headers=headers, json={
                'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
                'params': {'name': name, 'arguments': arguments}})
            assert response.status_code == 200
            result = response.json()['result']
            assert result['isError'] is False, result
            return json.loads(result['content'][0]['text'])

        plan = call('prepare_component_issue', harness.snapshot['arguments'])
        assert plan['state'] == 'ready'
        keys = dict(zip(('plan_id', 'plan_digest'), args(plan)))
        assert not call('issue_components', {**keys, 'plan_digest': 'incorrect'})['submitted']
        assert not executions(harness)
        assert call('issue_components', keys)['state'] == 'unknown'
        assert call('reconcile_component_issue', keys)['verified_success']
        assert call('get_component_issue_plan', {'plan_id': plan['plan_id']})['state'] == 'verified_committed'
        assert not call('issue_components', keys)['submitted']
    assert len(executions(harness)) == 1
