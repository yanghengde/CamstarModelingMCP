import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tools import component_issue_payload as b, component_issue_plans as p
from tools import component_issue_reasons as reasons, component_issue_receipts as receipts
from agent.safety import plan_preview
from agent.tool_routing import select_tools
from core.tool_catalog import TOOL_POLICIES

DATA = Path(__file__).resolve().parents[1]/'docs/t05-fixtures'


@pytest.fixture
def observed():
    sample = json.loads((DATA/'topup-plan-snapshot-E.json').read_text(encoding='utf-8'))
    sample['requirement'] = json.loads((DATA/'topup-requirement-before-E.json').read_text(encoding='utf-8'))
    sample['reasons'] = json.loads((DATA/'partial-reason-selection-E.json').read_text(encoding='utf-8'))['reasons']
    return sample


def test_topup_increment_preserves_original_requirement_and_cumulative_baseline(observed):
    snap = observed['stored']['plan']['snapshot']
    body = b.build_quantity_issue(observed['requirement'], 1, snap['difference_reason'])
    assert body == snap['body']
    line = body['serviceDetails'][0]
    assert line['qtyRequired'] == 2 and line['actualQtyIssued'] == line['actuals'][0]['qtyIssued'] == 1
    assert 'qtyIssued' not in line and 'netQtyRequired' not in line
    assert b.verify_quantity_echo(observed['echo'], body)
    context = b.quantity_context(snap)
    assert context['scope'] == 'incremental_issue'
    assert Decimal(context['cumulative_issued_before']) == Decimal(context['quantity_this_request']) == 1
    assert Decimal(context['projected_cumulative_issued']) == 2 and Decimal(context['projected_net_required']) == 0


@pytest.mark.parametrize('qty', [0, -1, True, '1', 1.01, 2, float('nan'), float('inf')])
def test_invalid_or_over_remaining_increment_rejected(observed, qty):
    with pytest.raises(ValueError): b.build_quantity_issue(observed['requirement'], qty, observed['stored']['plan']['snapshot']['difference_reason'])


@pytest.mark.parametrize('field,value', [('NetQtyRequired', 2), ('QtyIssued', 0), ('ActualQtyIssued', 0.5),
    ('QtyIssued', -1), ('QtyIssued', None), ('NetQtyRequired', float('nan')),
    ('RemovalHistoryDetails', None), ('RemovalHistoryDetails', [{'InstanceId': 'return'}])])
def test_inconsistent_pending_or_removal_balance_rejected(observed, field, value):
    observed['requirement']['data']['IssueDetails'][field] = value
    with pytest.raises(ValueError): b.build_quantity_issue(observed['requirement'], 1, observed['stored']['plan']['snapshot']['difference_reason'])


def test_filled_demand_and_missing_difference_reason_rejected(observed):
    with pytest.raises(ValueError): b.build_quantity_issue(observed['requirement'], 1)
    observed['requirement']['data']['IssueDetails'].update(QtyIssued=2, NetQtyRequired=0)
    with pytest.raises(ValueError): b.build_quantity_issue(observed['requirement'], 1, observed['stored']['plan']['snapshot']['difference_reason'])


def test_return_history_rejected_even_when_net_issued_is_zero(observed):
    row = observed['requirement']['data']['IssueDetails']
    row.update(QtyIssued=0, NetQtyRequired=2, RemovalHistoryDetails=[{'InstanceId': 'return'}])
    with pytest.raises(ValueError): b.build_quantity_issue(observed['requirement'], 2)


def test_reason_selection_uses_increment_without_fabricating_history(observed):
    body = b.reason_selection_body(observed['requirement'], 1)
    line = body['serviceDetails'][0]
    assert line['actualQtyIssued'] == 1 and line['qtyRequired'] == 2
    assert 'issueDifferenceReason' not in line and 'qtyIssued' not in line


def prepare_environment(observed, monkeypatch, tmp_path):
    snap = observed['stored']['plan']['snapshot']
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'plans.sqlite3')
    status = {'complete': True, 'data': {'ContainerStatusDetails': copy.deepcopy(snap['context'])}}
    status_mock = AsyncMock(return_value=json.dumps(status)); monkeypatch.setattr(p, 'get_container_details', status_mock)
    requirement_mock = AsyncMock(return_value=json.dumps(observed['requirement'])); monkeypatch.setattr(p, 'get_component_requirement', requirement_mock)
    monkeypatch.setattr(reasons, 'selections', AsyncMock(return_value=observed['reasons']))
    async def request(*args, **kw):
        return json.dumps(observed['echo']) if kw['params']['execute'] == 'false' else '{}'
    network = AsyncMock(side_effect=request); monkeypatch.setattr(p, 'request', network)
    monkeypatch.setattr(p, 'get_component_issue_history', AsyncMock(return_value='{}'))
    monkeypatch.setattr(p, 'get_container_history', AsyncMock(return_value='{}'))
    plan = json.loads(asyncio.run(p.prepare_component_issue(**snap['arguments'])))
    assert plan.get('state') == 'ready', plan
    return plan, network, requirement_mock


def test_topup_confirmation_displays_baseline_remaining_and_increment(observed, monkeypatch, tmp_path):
    plan, _, _ = prepare_environment(observed, monkeypatch, tmp_path)
    text = plan_preview([{'function': {'name': 'issue_components', 'arguments': json.dumps({
        'plan_id': plan['plan_id'], 'plan_digest': plan['plan_digest']})}}])
    assert 'cumulative_issued_before' in text and 'net_required_before' in text
    assert 'quantity_this_request' in text and 'actuals' in text


def test_consumption_changes_after_preview_reject_stale_plan(observed, monkeypatch, tmp_path):
    plan, network, req = prepare_environment(observed, monkeypatch, tmp_path)
    observed['requirement']['data']['IssueDetails'].update(QtyIssued=2, NetQtyRequired=0)
    req.return_value = json.dumps(observed['requirement'])
    result = json.loads(asyncio.run(p.issue_components(plan['plan_id'], plan['plan_digest'])))
    assert not result['submitted']
    assert all(c.kwargs['params']['execute'] == 'false' for c in network.call_args_list)


def test_same_target_unknown_topup_blocks_another_plan(observed, monkeypatch, tmp_path):
    first, network, _ = prepare_environment(observed, monkeypatch, tmp_path)
    second = json.loads(asyncio.run(p.prepare_component_issue(**observed['stored']['plan']['snapshot']['arguments'])))
    one = json.loads(asyncio.run(p.issue_components(first['plan_id'], first['plan_digest'])))
    two = json.loads(asyncio.run(p.issue_components(second['plan_id'], second['plan_digest'])))
    assert one['submission_attempted'] and one['state'] == 'unknown'
    assert two['submitted'] is False
    assert sum(c.kwargs['params']['execute'] == 'true' for c in network.call_args_list) == 1


@pytest.mark.parametrize('wrong', [None, 'old_marker', 'detail_cumulative', 'actual_cumulative'])
def test_receipt_matches_new_marker_and_increment_not_cumulative(observed, monkeypatch, wrong):
    old = json.loads((DATA/'partial-formal-reconciliation-E.json').read_text(encoding='utf-8'))
    rows = {r['table']: json.loads(r['response']) for r in old['result']['evidence']['reads']}
    payload = observed['stored']['plan']
    if wrong != 'old_marker': rows['HistoryMainline'][0]['Comments'] = payload['marker']
    if wrong == 'detail_cumulative': rows['IssueHistoryDetail'][0]['ActualQtyIssued'] = '2'
    if wrong == 'actual_cumulative': rows['IssueActualsHistory'][0]['Qty'] = '2'
    async def read(*args, **kw):
        q = kw['body']['queryText']
        table = next(t for t in rows if 'FROM '+t+' WHERE' in q)
        return json.dumps(rows[table])
    monkeypatch.setattr(receipts, 'request', AsyncMock(side_effect=read))
    assert asyncio.run(receipts.receipt(payload))['verified_success'] == (wrong is None)


def test_topup_route_exposes_execution_but_query_stays_read_only():
    all_tools = [{'function': {'name': n}} for n in TOOL_POLICIES]
    write = select_tools(all_tools, [{'role': 'user', 'content': '补投剩余需求1个'}])
    assert 'issue_components' in {x['function']['name'] for x in write}
    read = select_tools(all_tools, [{'role': 'user', 'content': '查询补投历史'}])
    assert 'get_component_issue_history' in {x['function']['name'] for x in read}
    assert all(TOOL_POLICIES[x['function']['name']].effect == 'read' for x in read)
