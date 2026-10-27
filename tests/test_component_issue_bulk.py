import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from tools import component_issue_bulk as b, get_tool_func, mcp
from core.tool_catalog import TOOL_POLICIES
from tools.tool_discovery import get_capabilities, load_tool_group
from agent.tool_routing import select_tools
from scripts.component_issue_fault_harness import echo

DATA = Path(__file__).resolve().parents[1] / 'docs/t05-fixtures'


@pytest.fixture
def observed():
    snap = json.loads((DATA/'formal-execution-D.json').read_text(encoding='utf-8'))['persisted_plan']['plan']['snapshot']
    target = copy.deepcopy(snap['context'])
    source = copy.deepcopy(target)
    source['Container'] = {'InstanceId': '00041080000000ff', 'Name': 'BULK-SOURCE'}
    source['Product'] = copy.deepcopy(snap['constraints']['Product'])
    source['Qty'] = 3
    detail = copy.deepcopy(snap['constraints']); detail['IssueControl'] = 2
    requirement = {'complete': True, 'status': 'matched', 'data': {
        'ParentContainer': target['Container'], 'ObjectName': 'BULK-SOURCE', 'IssueDetails': detail}}
    balance = {'ContainerId': source['Container']['InstanceId'], 'ContainerName': 'BULK-SOURCE',
               'ProductId': source['Product']['InstanceId'], 'Qty': '3', 'Qty2': '',
               'UOMId': source['UOM']['InstanceId'], 'UOM2Id': '', 'Status': '1', 'ParentContainerId': ''}
    return target, source, requirement, balance, []


def build(data):
    return b.build_preview(*data, 2)


def test_mode2_separates_source_from_nested_mode5_actuals(observed):
    body, context = build(observed)
    assert body['serviceDetails'][0]['issueControl'] == 2
    assert 'fromContainer' not in body['serviceDetails'][0]['actuals'][0]
    assert body['issueActualDetails'][0]['fromContainer']['name'] == 'BULK-SOURCE'
    assert body['preventOverIssue'] is True and context['projected_source_quantity'] == '1'
    assert not any(key in json.dumps(body) for key in ('validSequence', 'manualOverride', 'qtyIssuedBefore'))


@pytest.mark.parametrize('change', ['self', 'mode', 'string_mode', 'product_id', 'revision', 'ror', 'unit',
    'source_short', 'balance_changed', 'balance_product', 'balance_unit', 'balance_id', 'balance_name',
    'balance_status', 'parent', 'children', 'secondary', 'secondary_unit', 'missing_secondary',
    'missing_parent', 'source_nan', 'source_bool', 'already_issued', 'pending', 'return', 'partial',
    'demand_parent', 'wrong_target', 'wrong_match', 'incomplete', 'paging', 'secondary_demand', 'unknown_rule'])
def test_unsafe_or_unknown_source_never_builds_preview(observed, change):
    target, source, requirement, balance, children = observed
    row = requirement['data']['IssueDetails']
    if change == 'self': source['Container'] = target['Container']
    elif change == 'mode': row['IssueControl'] = 5
    elif change == 'string_mode': row['IssueControl'] = '2'
    elif change == 'product_id': source['Product']['InstanceId'] = '0000000000000001'
    elif change == 'revision': source['Product']['Revision'] = '2'
    elif change == 'ror': source['Product']['UseROR'] = True
    elif change == 'unit': source['UOM']['Name'] = 'KG'
    elif change == 'source_short': source['Qty'] = 1; balance['Qty'] = '1'
    elif change == 'balance_changed': balance['Qty'] = '4'
    elif change == 'balance_product': balance['ProductId'] = 'other'
    elif change == 'balance_unit': balance['UOMId'] = 'other'
    elif change == 'balance_id': balance['ContainerId'] = 'other'
    elif change == 'balance_name': balance['ContainerName'] = 'other'
    elif change == 'balance_status': balance['Status'] = True
    elif change == 'parent': balance['ParentContainerId'] = '0000000000000001'
    elif change == 'children': children.append({'ContainerId': '0000000000000001'})
    elif change == 'secondary': balance['Qty2'] = '1'
    elif change == 'secondary_unit': balance['UOM2Id'] = '0000000000000001'
    elif change == 'missing_secondary': balance.pop('Qty2')
    elif change == 'missing_parent': balance.pop('ParentContainerId')
    elif change == 'source_nan': balance['Qty'] = 'NaN'
    elif change == 'source_bool': source['Qty'] = True
    elif change == 'already_issued': row.update(QtyIssued=1, NetQtyRequired=1)
    elif change == 'pending': row['ActualQtyIssued'] = 1
    elif change == 'return': row['RemovalHistoryDetails'] = [{}]
    elif change == 'partial': row.update(QtyRequired=3, NetQtyRequired=3)
    elif change == 'demand_parent': row['BOMLineItem']['Parent']['InstanceId'] = '0000000000000001'
    elif change == 'wrong_target': requirement['data']['ParentContainer'] = source['Container']
    elif change == 'wrong_match': requirement['data']['ObjectName'] = 'OTHER'
    elif change == 'incomplete': requirement['complete'] = False
    elif change == 'paging': requirement['@odata.nextLink'] = 'untrusted'
    elif change == 'secondary_demand': row['NetQty2Required'] = 1
    elif change == 'unknown_rule': row.pop('ValidateQtyDifference')
    with pytest.raises((ValueError, KeyError)):
        build(observed)


@pytest.mark.parametrize('change', ['source', 'target', 'quantity', 'mode', 'unit', 'parent', 'product', 'duplicate', 'missing', 'paging', 'flag'])
def test_serialization_echo_must_preserve_all_source_intent(observed, change):
    body, _ = build(observed); response = echo(body)
    detail, actual = response['ServiceDetails'][0], response['IssueActualDetails'][0]
    if change == 'source': actual['FromContainer']['InstanceId'] = '0000000000000001'
    elif change == 'target': actual['ToContainer']['Name'] = 'other'
    elif change == 'quantity': actual['QtyIssued'] = 3
    elif change == 'mode': detail['IssueControl'] = 5
    elif change == 'unit': detail['UOM']['Name'] = 'KG'
    elif change == 'parent': actual['BOMLineItem']['Parent']['InstanceId'] = '0000000000000001'
    elif change == 'product': actual['Product']['Revision'] = '2'
    elif change == 'duplicate': response['IssueActualDetails'] *= 2
    elif change == 'missing': response.pop('IssueActualDetails')
    elif change == 'paging': response['@odata.nextLink'] = 'untrusted'
    elif change == 'flag': response['PreventOverIssue'] = False
    with pytest.raises((ValueError, KeyError)):
        b.verify_echo(response, body)


def test_recorded_live_mode2_source_and_echo_match_without_execution():
    live = json.loads((DATA/'bulk-live-preview-F.json').read_text(encoding='utf-8'))
    result = live['result']
    body, _ = b.build_preview(result['target_status']['data']['ContainerStatusDetails'],
        result['source_status']['data']['ContainerStatusDetails'], result['requirement'],
        result['source_balance'], result['current_children'], 2)
    assert body == result['request_preview']
    b.verify_echo(result['service_echo'], body)
    assert result['source_checks_passed'] and result['serialization_verified']
    assert not result['executable'] and live['component_issue_execute_true_count'] == 0
    assert live['source_after']['data']['ContainerStatusDetails']['Qty'] == 3
    assert live['requirement_after']['data']['IssueDetails']['QtyIssued'] == 0


def test_recorded_initial_zero_actual_echo_is_rejected():
    initial = json.loads((DATA/'bulk-live-preview-initial-F.json').read_text(encoding='utf-8'))['result']
    with pytest.raises(ValueError, match='ActualQtyIssued'):
        b.verify_echo(initial['service_echo'], initial['request_preview'])


def environment(monkeypatch, observed):
    target, source, requirement, balance, children = observed
    monkeypatch.setattr(b, 'get_container_details', AsyncMock(side_effect=lambda name, *_: json.dumps({
        'complete': True, 'data': {'ContainerStatusDetails': source if name == 'BULK-SOURCE' else target}})))
    monkeypatch.setattr(b, 'get_component_requirement', AsyncMock(return_value=json.dumps(requirement)))
    async def request(method, path, **kwargs):
        assert method == 'POST'
        if path == '/api/AdHoc':
            assert kwargs['retry_read_only'] is True
            query = kwargs['body']['queryText']
            assert query.startswith(('SELECT TOP 2 ', 'SELECT TOP 1 '))
            assert '00041080000000ff' in query and 'BULK-SOURCE' not in query
            return json.dumps(children if 'WHERE ParentContainerId' in query else [balance])
        assert path == '/api/ComponentIssue' and kwargs['params']['execute'] == 'false'
        reply = echo(kwargs['body'])
        reply['ServiceDetails'][0]['Actuals'][0]['CDOTypeId'] = '6540'
        return json.dumps(reply)
    network = AsyncMock(side_effect=request); monkeypatch.setattr(b, 'request', network)
    return {'container_name': target['Container']['Name'], 'source_container_name': 'BULK-SOURCE',
            'product_name': source['Product']['Name'], 'product_revision': '1', 'qty': 2}, network


def preview(arguments):
    return json.loads(asyncio.run(b.preview_bulk_component_issue(**arguments)))


def test_public_source_check_never_creates_plan_or_executes(observed, monkeypatch):
    arguments, network = environment(monkeypatch, observed)
    result = preview(arguments)
    assert result['source_checks_passed'] and not result['serialization_verified']
    assert not result['executable'] and not result['complete'] and not result['submitted']
    assert 'plan_id' not in result and network.call_count == 2
    result = preview({**arguments, 'verify_serialization': True})
    assert result['serialization_verified'] and result['blockers']
    assert network.call_args.kwargs['params'] == b.DERIVED_PREVIEW_PARAMS
    assert b.get_component_requirement.call_args.args[1] == 'BULK-SOURCE'


@pytest.mark.parametrize('change', ['inactive', 'rework', 'incomplete', 'bad_id', 'explicit_id', 'product', 'self', 'quantity'])
def test_public_rejection_does_not_send_service_preview(observed, monkeypatch, change):
    arguments, network = environment(monkeypatch, observed)
    if change == 'inactive': observed[1]['Status'] = 2
    elif change == 'rework': observed[1].pop('InRework')
    elif change == 'incomplete': observed[2]['complete'] = False; b.get_component_requirement.return_value = json.dumps(observed[2])
    elif change == 'bad_id': arguments['source_container_instance_id'] = "x';DELETE"
    elif change == 'explicit_id': arguments['source_container_instance_id'] = '0000000000000001'
    elif change == 'product': arguments['product_revision'] = '2'
    elif change == 'self': observed[1]['Container'] = observed[0]['Container']
    elif change == 'quantity': arguments['qty'] = True
    result = preview({**arguments, 'verify_serialization': True})
    assert 'error' in result and not result['serialization_verified'] and not result['source_checks_passed']
    assert all(call.args[1] != '/api/ComponentIssue' for call in network.call_args_list)


def test_missing_or_duplicate_balance_and_service_error_keep_preview_incomplete(observed, monkeypatch):
    arguments, network = environment(monkeypatch, observed)
    network.side_effect = None; network.return_value = '[]'
    assert not preview(arguments)['source_checks_passed']
    network.return_value = json.dumps([observed[3]] * 2)
    assert not preview(arguments)['source_checks_passed']
    environment(monkeypatch, observed)
    request = b.request; original = request.side_effect
    async def error(method, path, **kwargs):
        if path == '/api/ComponentIssue': return 'HTTP 403 Response: {"ErrorCode":"permission"}'
        return await original(method, path, **kwargs)
    request.side_effect = error
    result = preview({**arguments, 'verify_serialization': True})
    assert result['source_checks_passed'] and not result['serialization_verified'] and not result['executable']


@pytest.mark.parametrize('invalid', [True, '2', 0, -1])
def test_mcp_quantity_schema_rejects_before_any_read(monkeypatch, observed, invalid):
    arguments, _ = environment(monkeypatch, observed)
    async def run():
        async with Client(mcp) as client:
            result = await client.call_tool('preview_bulk_component_issue', {**arguments, 'qty': invalid}, raise_on_error=False)
            assert result.is_error
    asyncio.run(run())
    b.get_container_details.assert_not_called()


def test_discovery_registration_and_read_only_routing():
    assert get_tool_func('preview_bulk_component_issue')
    assert TOOL_POLICIES['preview_bulk_component_issue'].effect == 'read'
    assert 'preview_bulk_component_issue' in json.loads(asyncio.run(load_tool_group('material')))['tools']
    details = json.loads(asyncio.run(get_capabilities(tool_name='preview_bulk_component_issue')))['tool']
    assert '不扣库存' in details['limitations']
    tools = [{'function': {'name': name}} for name in TOOL_POLICIES]
    selected = select_tools(tools, [{'role': 'user', 'content': '只读检查批量容器投料来源'}])
    assert 'preview_bulk_component_issue' in {t['function']['name'] for t in selected}
