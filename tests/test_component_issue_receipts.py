import asyncio
import copy
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tools import component_issue_receipts as r, component_issue_plans as p, get_tool_func
from core.tool_catalog import TOOL_POLICIES
from core.transfer_attempt_lock import attempt_lock
from agent.safety import plan_preview

DATA = Path(__file__).resolve().parents[1]/'docs/t05-fixtures'


@pytest.fixture
def captured():
    plan = json.loads((DATA/'formal-execution-D.json').read_text(encoding='utf-8'))['persisted_plan']
    evidence = json.loads((DATA/'issue-receipt-verification-D.json').read_text(encoding='utf-8'))
    rows = {item['table']: json.loads(item['response']) for item in evidence['reads']}
    return plan, rows


def mock_reads(monkeypatch, rows):
    async def read(method, path, **kwargs):
        assert method == 'POST' and path == '/api/AdHoc'
        assert kwargs['retry_read_only'] is True
        query = kwargs['body']['queryText']
        assert query.startswith('SELECT TOP 2 ') and '*' not in query and 'execute' not in kwargs.get('params', {})
        table = next(table for table in rows if 'FROM '+table+' WHERE' in query)
        return json.dumps(rows[table])
    mock = AsyncMock(side_effect=read); monkeypatch.setattr(r, 'request', mock)
    return mock


def test_recorded_receipt_chain_and_header_txn_semantics(captured, monkeypatch):
    plan, rows = captured
    rows['ComponentIssueHistory'][0]['TxnId'] = '001928000000000b'  # service ID, intentionally differs
    mock = mock_reads(monkeypatch, rows)
    evidence = asyncio.run(r.receipt(plan['plan']))
    assert evidence['verified_success'] and evidence['complete']
    assert evidence['transaction_id'] == '400000000000f0f7'
    assert evidence['material_line_id'] == '001b7b8000000004'
    assert mock.call_count == 4


@pytest.mark.parametrize('table', ['HistoryMainline', 'IssueHistoryDetail', 'ComponentIssueHistory', 'IssueActualsHistory'])
@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'error'])
def test_missing_ambiguous_or_error_never_proves_failure_or_success(captured, monkeypatch, table, kind):
    plan, rows = captured
    rows[table] = [] if kind == 'missing' else rows[table]*2 if kind == 'duplicate' else {'ErrorCode': 'permission'}
    mock_reads(monkeypatch, rows)
    evidence = asyncio.run(r.receipt(plan['plan']))
    assert not evidence['verified_success'] and not evidence['complete']


@pytest.mark.parametrize('table,field,value', [
    ('HistoryMainline', 'Comments', 'other marker'), ('HistoryMainline', 'ContainerId', '0000000000000001'),
    ('HistoryMainline', 'TxnTypeName', 'Start'), ('HistoryMainline', 'SpecId', 'other'),
    ('HistoryMainline', 'OperationId', 'other'), ('IssueHistoryDetail', 'ProductMaterialListItemId', 'other line'),
    ('IssueHistoryDetail', 'ProductId', 'other'), ('IssueHistoryDetail', 'UOMId', 'other'),
    ('IssueHistoryDetail', 'IntegrationTxnId', 'other'), ('IssueHistoryDetail', 'TxnId', 'other'),
    ('IssueHistoryDetail', 'ActualQtyIssued', '1'), ('IssueHistoryDetail', 'IssueControl', '2'),
    ('IssueHistoryDetail', 'QtyRequired', '9'), ('ComponentIssueHistory', 'HistoryMainlineId', 'other'),
    ('ComponentIssueHistory', 'HistoryId', 'other'), ('ComponentIssueHistory', 'IntegrationTxnId', 'other'),
    ('IssueActualsHistory', 'IssueHistoryDetailId', 'other'), ('IssueActualsHistory', 'ToContainerId', 'other'),
    ('IssueActualsHistory', 'ProductId', 'other'), ('IssueActualsHistory', 'Qty', 'NaN'),
    ('IssueActualsHistory', 'Qty2', '1'), ('IssueActualsHistory', 'FromLot', 'physical lot'),
    ('IssueActualsHistory', 'Qty', True), ('IssueActualsHistory', 'Qty', '1'),
])
def test_every_receipt_link_and_amount_is_bound(captured, monkeypatch, table, field, value):
    plan, rows = captured; rows[table][0][field] = value
    mock_reads(monkeypatch, rows)
    assert not asyncio.run(r.receipt(plan['plan']))['verified_success']


def test_marker_injection_or_changed_actual_intent_never_queries(captured, monkeypatch):
    plan, rows = captured; mock = mock_reads(monkeypatch, rows)
    plan['plan']['marker'] = "x'; DROP TABLE t"
    assert not asyncio.run(r.receipt(plan['plan']))['verified_success']
    mock.assert_not_called()
    plan['plan']['marker'] = 'Codex issue '+plan['plan_id']
    plan['plan']['snapshot']['body']['serviceDetails'][0]['actuals'][0]['qtyIssued'] = 9
    assert not asyncio.run(r.receipt(plan['plan']))['verified_success']
    mock.assert_not_called()


@pytest.fixture
def local(captured, tmp_path, monkeypatch):
    plan, rows = captured
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'plans.sqlite3')
    monkeypatch.setattr(p, 'CAMSTAR_SHOPFLOOR_BASE_URL', plan['plan']['snapshot']['environment'])
    with p.db_connect() as db:
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)', (plan['plan_id'], plan['plan_digest'],
            json.dumps(plan['plan']), 'unknown', time.time(), json.dumps(plan['outcome'])))
        db.execute('INSERT INTO attempts VALUES (?,?,?,?)', (plan['plan_id'], 'attempt', time.time()-1, time.time()))
    mock = mock_reads(monkeypatch, rows)
    return plan, rows, mock


def reconcile(local):
    plan = local[0]
    return json.loads(asyncio.run(p.reconcile_component_issue(plan['plan_id'], plan['plan_digest'])))


def test_success_atomically_audits_and_releases_only_this_plan(local):
    plan, _, mock = local
    # A second unresolved plan must keep its independent state/occupancy.
    with p.db_connect() as db:
        db.execute('INSERT INTO plans SELECT ?,digest,payload,state,created,outcome FROM plans', ('other',))
    result = reconcile(local)
    assert result['state_changed'] and result['verified_success'] and result['locks_released']
    assert result['state'] == 'verified_committed' and not result['retry_allowed'] and not result['mes_write_performed']
    with p.db_connect() as db:
        outcome = json.loads(db.execute('SELECT outcome FROM plans WHERE id=?', (plan['plan_id'],)).fetchone()[0])
        assert outcome['verification']['previous_outcome'] == plan['outcome']
        assert db.execute("SELECT state FROM plans WHERE id='other'").fetchone()[0] == 'unknown'
        assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM receipt_checks').fetchone()[0] == 1
    assert not reconcile(local)['state_changed'] and mock.call_count == 4
    assert not json.loads(asyncio.run(p.issue_components(plan['plan_id'], plan['plan_digest'])))['submitted']


def test_inconclusive_check_keeps_outcome_and_audits(local):
    plan, rows, _ = local; rows['IssueActualsHistory'] = []
    result = reconcile(local)
    assert not result['state_changed'] and not result['locks_released'] and result['state'] == 'unknown'
    with p.db_connect() as db:
        row = db.execute('SELECT state,outcome FROM plans').fetchone()
        assert row[0] == 'unknown' and json.loads(row[1]) == plan['outcome']
        assert db.execute('SELECT COUNT(*) FROM receipt_checks').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0] == 0


def test_different_deployment_cannot_reconcile_archived_plan(local, monkeypatch):
    monkeypatch.setattr(p, 'CAMSTAR_SHOPFLOOR_BASE_URL', 'https://different.invalid/Shopfloor')
    result = reconcile(local)
    assert not result['state_changed'] and not result['locks_released']
    local[2].assert_not_called()
    with p.db_connect() as db:
        assert db.execute('SELECT state FROM plans').fetchone()[0] == 'unknown'
        assert db.execute('SELECT COUNT(*) FROM receipt_checks').fetchone()[0] == 0


@pytest.mark.parametrize('state', ['ready', 'in_flight', 'manually_confirmed_committed'])
def test_wrong_state_or_active_local_attempt_never_reads(local, state):
    with p.db_connect() as db: db.execute('UPDATE plans SET state=?', (state,))
    assert not reconcile(local)['state_changed']; local[2].assert_not_called()


def test_active_process_missing_attempt_and_bad_digest_rejected(local):
    plan = local[0]
    with attempt_lock(p.PLAN_DB, plan['plan_id']): assert not reconcile(local)['state_changed']
    local[2].assert_not_called()
    assert not json.loads(asyncio.run(p.reconcile_component_issue(plan['plan_id'], 'wrong')))['state_changed']
    with p.db_connect() as db: db.execute('DELETE FROM attempts')
    assert not reconcile(local)['state_changed']; local[2].assert_not_called()


@pytest.mark.parametrize('table', ['receipt_checks', 'reviews'])
def test_audit_failure_rolls_back_release(local, table):
    with p.db_connect() as db:
        db.execute(f"CREATE TRIGGER reject_audit BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError): reconcile(local)
    with p.db_connect() as db:
        assert db.execute('SELECT state FROM plans').fetchone()[0] == 'unknown'
        assert db.execute('SELECT COUNT(*) FROM receipt_checks').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0] == 0


def test_plan_changed_while_reading_receipt_is_not_released(local, monkeypatch):
    original = local[2].side_effect
    async def change(*args, **kwargs):
        value = await original(*args, **kwargs)
        with p.db_connect() as db: db.execute('UPDATE plans SET created=created-1')
        return value
    local[2].side_effect = change
    result = reconcile(local)
    assert not result['state_changed']
    with p.db_connect() as db:
        assert db.execute('SELECT state FROM plans').fetchone()[0] == 'unknown'
        assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0] == 0


def test_registration_and_confirmation_include_original_request(local):
    plan = local[0]
    assert get_tool_func('reconcile_component_issue')
    assert TOOL_POLICIES['reconcile_component_issue'].effect == 'update'
    assert TOOL_POLICIES['reconcile_component_issue'].always_confirm
    text = plan_preview([{'function': {'name': 'reconcile_component_issue', 'arguments': json.dumps({
        'plan_id': plan['plan_id'], 'plan_digest': plan['plan_digest']})}}])
    assert plan['plan']['marker'] in text and 'actuals' in text and 'verified_committed' in text
