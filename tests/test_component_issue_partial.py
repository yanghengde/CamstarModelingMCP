import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tools import component_issue_payload as b, component_issue_reasons as r, component_issue_plans as p
from tools import component_issue_receipts as receipts, get_tool_func
from core.tool_catalog import TOOL_POLICIES

DATA = Path(__file__).resolve().parents[1]/'docs/t05-fixtures'
NAME = 'CDX-T05-20261024-E-PARTIAL'


@pytest.fixture
def observed():
    data = json.loads((DATA/'partial-plan-snapshot-E.json').read_text(encoding='utf-8'))
    data['reasons'] = json.loads((DATA/'partial-reason-selection-E.json').read_text(encoding='utf-8'))['reasons']
    return data


def requirement(observed):
    snap = observed['stored']['plan']['snapshot']
    return {'complete': True, 'status': 'matched', 'data': {
        'ParentContainer': snap['context']['Container'], 'IssueDetails': snap['constraints']}}


def selection_response(observed):
    values = observed['reasons']
    return {'ServiceDetails': [{'IssueDifferenceReason': {
        'RecordCount': len(values), 'RowCount': len(values), 'SelectionValuesEx': values}}]}


def test_partial_payload_reason_and_real_echo(observed):
    snap = observed['stored']['plan']['snapshot']
    body = b.build_quantity_issue(requirement(observed), 1, snap['difference_reason'])
    assert body == snap['body']
    line = body['serviceDetails'][0]
    assert line['qtyRequired'] == 2 and line['actualQtyIssued'] == 1
    assert line['issueDifferenceReason'] == line['actuals'][0]['issueDifferenceReason']
    assert b.verify_quantity_echo(observed['echo'], body)
    assert 'IssueDifferenceReason' in b.preview_params(body)['$expand']


@pytest.mark.parametrize('where', ['requirement', 'actual'])
def test_reason_not_echoed_or_changed_rejected(observed, where):
    echo = observed['echo']; line = echo['data']['ServiceDetails'][0]
    if where == 'requirement': line.pop('IssueDifferenceReason')
    else: line['Actuals'][0]['IssueDifferenceReason']['InstanceId'] = '0000000000000001'
    with pytest.raises(ValueError): b.verify_quantity_echo(echo, observed['stored']['plan']['snapshot']['body'])


def test_full_with_reason_partial_without_reason_and_used_demand_rejected(observed):
    req = requirement(observed); reason = observed['stored']['plan']['snapshot']['difference_reason']
    with pytest.raises(ValueError): b.build_quantity_issue(req, 2, reason)
    with pytest.raises(ValueError): b.build_quantity_issue(req, 1)
    req['data']['IssueDetails']['QtyIssued'] = 1
    with pytest.raises(ValueError): b.build_quantity_issue(req, 1, reason)


def test_selection_does_not_guess_and_uses_fixed_read_only_endpoint(observed, monkeypatch):
    mock = AsyncMock(return_value=json.dumps(selection_response(observed)))
    monkeypatch.setattr(r, 'request', mock)
    values = asyncio.run(r.selections(requirement(observed), 1))
    assert len(values) == 2 and r.choose(values, NAME)['Name'] == NAME
    assert mock.call_args.args == ('POST', '/api/ComponentIssue/RequestSelectionValues')
    assert mock.call_args.kwargs['retry_read_only'] is True
    assert mock.call_args.kwargs['params']['selectionValuesExpression'] == 'ServiceDetails.IssueDifferenceReason'
    assert 'execute' not in mock.call_args.kwargs['params']
    assert get_tool_func('get_component_issue_difference_reasons')
    assert TOOL_POLICIES['get_component_issue_difference_reasons'].effect == 'read'


@pytest.mark.parametrize('change', ['truncated', 'wrong_count', 'missing_frozen', 'duplicate_id', 'wrong_type', 'error'])
def test_incomplete_or_ambiguous_reason_values_rejected(observed, monkeypatch, change):
    response = selection_response(observed); table = response['ServiceDetails'][0]['IssueDifferenceReason']
    if change == 'truncated': table['RecordCount'] += 1
    elif change == 'wrong_count': table['RowCount'] = True
    elif change == 'missing_frozen': table['SelectionValuesEx'][0].pop('IsFrozen')
    elif change == 'duplicate_id': table['SelectionValuesEx'][1]['InstanceId'] = table['SelectionValuesEx'][0]['InstanceId']
    elif change == 'wrong_type': table['SelectionValuesEx'][0]['IsFrozen'] = 0
    else: response['ErrorCode'] = 'permission'
    monkeypatch.setattr(r, 'request', AsyncMock(return_value=json.dumps(response)))
    with pytest.raises(ValueError): asyncio.run(r.selections(requirement(observed), 1))


def test_reason_must_be_explicit_unique_and_not_frozen(observed):
    values = observed['reasons']
    with pytest.raises(ValueError): r.choose(values, '')
    with pytest.raises(ValueError): r.choose(values, 'missing')
    values[0]['IsFrozen'] = True
    with pytest.raises(ValueError): r.choose(values, NAME)
    values[0]['IsFrozen'] = False
    values[1]['Name'] = NAME
    with pytest.raises(ValueError): r.choose(values, NAME)


def prepare_environment(observed, monkeypatch, tmp_path):
    snap = observed['stored']['plan']['snapshot']
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'plans.sqlite3')
    monkeypatch.setattr(p, 'get_container_details', AsyncMock(return_value=json.dumps({
        'complete': True, 'data': {'ContainerStatusDetails': snap['context']}})))
    monkeypatch.setattr(p, 'get_component_requirement', AsyncMock(return_value=json.dumps(requirement(observed))))
    monkeypatch.setattr(r, 'selections', AsyncMock(return_value=observed['reasons']))
    network = AsyncMock(return_value=json.dumps(observed['echo'])); monkeypatch.setattr(p, 'request', network)
    args = dict(snap['arguments'])
    plan = json.loads(asyncio.run(p.prepare_component_issue(**args)))
    assert plan.get('state') == 'ready', plan
    return plan, network


def test_frozen_reason_before_execution_rejects_plan(observed, monkeypatch, tmp_path):
    plan, network = prepare_environment(observed, monkeypatch, tmp_path)
    observed['reasons'][0]['IsFrozen'] = True
    result = json.loads(asyncio.run(p.issue_components(plan['plan_id'], plan['plan_digest'])))
    assert not result['submitted'] and network.call_count == 1


def test_reason_identity_changes_before_execution_rejects(observed, monkeypatch, tmp_path):
    plan, network = prepare_environment(observed, monkeypatch, tmp_path)
    observed['reasons'][0]['InstanceId'] = '0000000000000001'
    result = json.loads(asyncio.run(p.issue_components(plan['plan_id'], plan['plan_digest'])))
    assert not result['submitted']
    assert all(c.kwargs['params']['execute'] == 'false' for c in network.call_args_list)


def test_reason_tamper_without_changing_snapshot_digest_rejected(observed, monkeypatch, tmp_path):
    plan, network = prepare_environment(observed, monkeypatch, tmp_path)
    with p.db_connect() as db:
        stored = json.loads(db.execute('SELECT payload FROM plans').fetchone()[0])
        stored['snapshot']['difference_reason']['Name'] = 'other reason'
        db.execute('UPDATE plans SET payload=?', (json.dumps(stored),))
    result = json.loads(asyncio.run(p.issue_components(plan['plan_id'], plan['plan_digest'])))
    assert not result['submitted'] and network.call_count == 1


@pytest.mark.parametrize('missing', [None, 'detail', 'actual'])
def test_receipt_binds_partial_amount_and_both_reason_ids(observed, monkeypatch, missing):
    evidence = json.loads((DATA/'issue-receipt-verification-D.json').read_text(encoding='utf-8'))
    tables = {x['table']: json.loads(x['response']) for x in evidence['reads']}
    payload = observed['stored']['plan']; snap = payload['snapshot']; line = snap['body']['serviceDetails'][0]
    target = snap['body']['container']['instanceId']; product = line['product']['instanceId']
    reason_id = line['issueDifferenceReason']['instanceId']
    tables['HistoryMainline'][0].update(Comments=payload['marker'], ContainerId=target)
    tables['IssueHistoryDetail'][0].update(HistoryId=target, ProductMaterialListItemId=line['bomLineItem']['instanceId'],
        ProductId=product, ActualQtyIssued='1', IssueDifferenceReasonId=reason_id)
    tables['ComponentIssueHistory'][0]['HistoryId'] = target
    tables['IssueActualsHistory'][0].update(HistoryId=target, ToContainerId=target, ProductId=product,
        Qty='1', IssueDifferenceReasonId=reason_id)
    if missing == 'detail': tables['IssueHistoryDetail'][0]['IssueDifferenceReasonId'] = ''
    if missing == 'actual': tables['IssueActualsHistory'][0]['IssueDifferenceReasonId'] = 'other'
    async def read(*args, **kw):
        query = kw['body']['queryText']
        table = next(t for t in tables if 'FROM '+t+' WHERE' in query)
        if table in ('IssueHistoryDetail', 'IssueActualsHistory'): assert 'IssueDifferenceReasonId' in query
        return json.dumps(tables[table])
    monkeypatch.setattr(receipts, 'request', AsyncMock(side_effect=read))
    result = asyncio.run(receipts.receipt(payload))
    assert result['verified_success'] == (missing is None)
