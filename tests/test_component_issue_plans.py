import asyncio
import copy
import json
import time
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tools import component_issue_plans as p, get_tool_func, mcp
from core.tool_catalog import TOOL_POLICIES
from core.transfer_attempt_lock import attempt_lock
from agent.safety import plan_preview
from agent.tool_routing import select_tools

DATA = Path(__file__).resolve().parents[1]/'docs/t05-fixtures'


@pytest.fixture
def env(tmp_path, monkeypatch):
    captured = json.loads((DATA/'nested-actuals-execution-C.json').read_text(encoding='utf-8'))
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'plans.sqlite3')
    monkeypatch.setattr(p, 'get_container_details', AsyncMock(return_value=json.dumps(captured['status_before'])))
    monkeypatch.setattr(p, 'get_component_requirement', AsyncMock(return_value=json.dumps(captured['requirement_before'])))
    monkeypatch.setattr(p, 'get_component_issue_history', AsyncMock(return_value='{"complete":true,"rows":[]}'))
    monkeypatch.setattr(p, 'get_container_history', AsyncMock(return_value='{}'))
    async def request(*args, **kwargs):
        return json.dumps(captured['preview_response'] if kwargs['params']['execute'] == 'false' else captured['response'])
    network = AsyncMock(side_effect=request)
    monkeypatch.setattr(p, 'request', network)
    return captured, network


def prepare():
    return json.loads(asyncio.run(p.prepare_component_issue('CDX-T05-20261024-C-C01', 'CDX-T05-20261024-C-COMP', '1', 2)))


def execute(plan):
    return json.loads(asyncio.run(p.issue_components(plan['plan_id'], plan['plan_digest'])))


def post_count(network):
    return sum(c.kwargs['params']['execute'] == 'true' for c in network.call_args_list)


def test_plan_summary_registration_and_approval(env):
    plan = prepare()
    assert plan['state'] == 'ready' and not plan['submitted']
    summary = p.confirmation_summary(plan['plan_id'], plan['plan_digest'])
    assert 'CDX-T05-20261024-C-COMP' in summary and 'actuals' in summary
    assert 'Codex issue '+plan['plan_id'] in summary
    for name in ['prepare_component_issue', 'get_component_issue_plan', 'issue_components',
                 'recover_component_issue_attempt', 'resolve_component_issue_outcome']:
        assert get_tool_func(name)
        assert name in {t.name for t in asyncio.run(mcp.list_tools())}
    assert TOOL_POLICIES['issue_components'].always_confirm
    rendered = plan_preview([{'function': {'name': 'issue_components', 'arguments': json.dumps({
        'plan_id': plan['plan_id'], 'plan_digest': plan['plan_digest']})}}])
    assert 'actuals' in rendered and 'CDX-T05-20261024-C-COMP' in rendered


def test_execute_once_persists_unknown_and_attempt(env):
    plan = prepare(); result = execute(plan)
    assert result['state'] == 'unknown' and not result['verified_success'] and not result['retry_allowed']
    assert post_count(env[1]) == 1
    with p.db_connect() as db:
        assert db.execute('SELECT finished FROM attempts').fetchone()[0] is not None
    assert execute(plan)['submitted'] is False and post_count(env[1]) == 1


def test_same_container_new_plan_cannot_bypass_unknown(env):
    first = prepare(); second = prepare()
    execute(first)
    assert 'error' in execute(second) and post_count(env[1]) == 1


@pytest.mark.parametrize('change', ['qty', 'spec', 'net', 'mode', 'unit'])
def test_changed_business_snapshot_rejected(env, monkeypatch, change):
    plan = prepare(); captured = copy.deepcopy(env[0])
    if change == 'qty': captured['status_before']['data']['ContainerStatusDetails']['Qty'] = 2
    elif change == 'spec': captured['status_before']['data']['ContainerStatusDetails']['Spec']['Revision'] = 'B'
    else:
        row = captured['requirement_before']['data']['IssueDetails']
        if change == 'net': row['NetQtyRequired'] = 1
        elif change == 'mode': row['IssueControl'] = 2
        elif change == 'unit': row['UOM']['Name'] = 'OTHER'
    monkeypatch.setattr(p, 'get_container_details', AsyncMock(return_value=json.dumps(captured['status_before'])))
    monkeypatch.setattr(p, 'get_component_requirement', AsyncMock(return_value=json.dumps(captured['requirement_before'])))
    assert execute(plan)['submitted'] is False and post_count(env[1]) == 0


def test_ephemeral_inquiry_identity_not_bound(env, monkeypatch):
    plan = prepare(); captured = copy.deepcopy(env[0])
    captured['requirement_before']['data']['IssueDetails']['InstanceID'] = 'different transient id'
    monkeypatch.setattr(p, 'get_component_requirement', AsyncMock(return_value=json.dumps(captured['requirement_before'])))
    assert execute(plan)['submission_attempted'] and post_count(env[1]) == 1


@pytest.mark.parametrize('change', ['arguments', 'environment', 'body', 'marker'])
def test_tampered_storage_cannot_submit(env, change):
    plan = prepare()
    with p.db_connect() as db:
        value = json.loads(db.execute('SELECT payload FROM plans').fetchone()[0])
        if change == 'arguments': value['snapshot']['arguments']['qty'] = 9
        elif change == 'environment': value['snapshot']['environment'] = 'other'
        elif change == 'body': value['snapshot']['body']['serviceDetails'][0]['actualQtyIssued'] = 9
        else: value['marker'] = 'other'
        db.execute('UPDATE plans SET payload=?', (json.dumps(value),))
    assert execute(plan)['submitted'] is False and post_count(env[1]) == 0


def test_expired_and_wrong_digest_do_not_submit(env):
    plan = prepare()
    assert json.loads(asyncio.run(p.issue_components(plan['plan_id'], 'wrong')))['submitted'] is False
    with p.db_connect() as db: db.execute('UPDATE plans SET created=?', (time.time()-3601,))
    assert execute(plan)['submitted'] is False and post_count(env[1]) == 0


def test_cancelled_submission_saved_unknown(env, monkeypatch):
    plan = prepare(); original = env[1].side_effect
    async def request(*args, **kwargs):
        if kwargs['params']['execute'] == 'true': raise asyncio.CancelledError()
        return await original(*args, **kwargs)
    env[1].side_effect = request
    with pytest.raises(asyncio.CancelledError): execute(plan)
    stored = json.loads(asyncio.run(p.get_component_issue_plan(plan['plan_id'])))
    assert stored['state'] == 'unknown' and stored['outcome']['interrupted']


def test_concurrent_same_plan_single_mutation(env):
    plan = prepare()
    async def parallel():
        return await asyncio.gather(*(p.issue_components(plan['plan_id'], plan['plan_digest']) for _ in range(2)))
    asyncio.run(parallel())
    assert post_count(env[1]) == 1


def test_audited_manual_outcome_never_reuses_plan(env):
    plan = prepare(); execute(plan)
    result = p.review(plan['plan_id'], plan['plan_digest'], 'resolve', 'final server audit', 'checked', 'committed')
    result = json.loads(result)
    assert result['locks_released'] and not result['verified_success'] and not result['original_plan_reusable']
    assert execute(plan)['submitted'] is False
    assert not json.loads(p.review(plan['plan_id'], plan['plan_digest'], 'resolve', 'audit', 'again', 'committed'))['state_changed']
    assert execute(prepare())['submission_attempted']


def test_recovery_requires_attempt_and_no_active_local_worker(env):
    plan = prepare()
    with p.db_connect() as db: db.execute("UPDATE plans SET state='in_flight'")
    args = (plan['plan_id'], plan['plan_digest'], 'recover', 'process stopped', 'checked')
    assert not json.loads(p.review(*args))['state_changed']
    with p.db_connect() as db: db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)', (plan['plan_id'], 'attempt', time.time()))
    with attempt_lock(p.PLAN_DB, plan['plan_id']): assert not json.loads(p.review(*args))['state_changed']
    recovered = json.loads(p.review(*args))
    assert recovered['state'] == 'unknown' and not recovered['locks_released']


def test_ready_or_missing_evidence_cannot_unlock(env):
    plan = prepare()
    assert not json.loads(p.review(plan['plan_id'], plan['plan_digest'], 'resolve', 'audit', 'checked', 'committed'))['state_changed']
    execute(plan)
    assert not json.loads(p.review(plan['plan_id'], plan['plan_digest'], 'resolve', '', 'checked', 'committed'))['state_changed']


def test_issue_intent_loads_write_but_history_remains_read_only():
    all_tools = [{'function': {'name': name}} for name in TOOL_POLICIES]
    selected = select_tools(all_tools, [{'role': 'user', 'content': '给容器C投料2个P版本1'}])
    assert 'issue_components' in {t['function']['name'] for t in selected}
    selected = select_tools(all_tools, [{'role': 'user', 'content': '查询容器C投料历史'}])
    assert 'get_component_issue_history' in {t['function']['name'] for t in selected}
    assert all(TOOL_POLICIES[t['function']['name']].effect == 'read' for t in selected)


def test_marker_tamper_with_recomputed_snapshot_still_rejected(env):
    plan = prepare()
    with p.db_connect() as db:
        stored = json.loads(db.execute('SELECT payload FROM plans').fetchone()[0])
        stored['snapshot']['marker'] = 'arbitrary marker'
        fingerprint = p.digest(stored['snapshot'])
        db.execute('UPDATE plans SET payload=?,digest=?', (json.dumps(stored), fingerprint))
    result = json.loads(asyncio.run(p.issue_components(plan['plan_id'], fingerprint)))
    assert result['submitted'] is False and post_count(env[1]) == 0


def test_storage_changed_during_refresh_not_claimed(env):
    plan = prepare(); original = env[1].side_effect
    async def request(*args, **kwargs):
        result = await original(*args, **kwargs)
        if kwargs['params']['execute'] == 'false':
            with p.db_connect() as db: db.execute('UPDATE plans SET created=created-1')
        return result
    env[1].side_effect = request
    assert execute(plan)['submitted'] is False and post_count(env[1]) == 0


def test_review_audit_failure_rolls_back_state(env):
    plan = prepare(); execute(plan)
    with p.db_connect() as db:
        db.execute("CREATE TRIGGER reject_review BEFORE INSERT ON reviews BEGIN SELECT RAISE(ABORT,'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        p.review(plan['plan_id'], plan['plan_digest'], 'resolve', 'server audit', 'checked', 'committed')
    with p.db_connect() as db:
        assert db.execute('SELECT state FROM plans').fetchone()[0] == 'unknown'
        assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0] == 0
