import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tools import component_issue as ci


@pytest.fixture
def requirement():
    path = Path(__file__).resolve().parents[1] / 'docs/t05-fixtures/requirement-tool-validation.json'
    return json.loads(path.read_text(encoding='utf-8'))


def assess(requirement, qty=2):
    product = copy.deepcopy(requirement['data']['IssueDetails']['Product'])
    return ci.assess_quantity_requirement(requirement, product, qty)


def statuses(result):
    return {row['check']: row['status'] for row in result['checks']}


def test_real_demand_keeps_unverified_sequence_unknown(requirement):
    result = assess(requirement)
    checks = statuses(result)
    assert checks['product_version'] == checks['unit'] == checks['bom_line'] == 'pass'
    assert checks['remaining_quantity'] == checks['secondary_quantity'] == 'pass'
    assert checks['assembly_sequence'] == 'unknown'
    assert not result['execution_authorized'] and not result['observed_checks_passed']
    assert result['quantity_unit']['Name'] == 'EACH'


@pytest.mark.parametrize('qty', [0, 2.001, 3])
def test_quantity_uses_net_remaining_instead_of_total(requirement, qty):
    assert statuses(assess(requirement, qty))['remaining_quantity'] == 'blocked'
    requirement['data']['IssueDetails']['QtyRequired'] = 99
    assert statuses(assess(requirement, qty))['remaining_quantity'] == 'blocked'


@pytest.mark.parametrize('value', [None, True, '2', float('inf'), float('nan'), -1])
def test_missing_or_invalid_remaining_is_unknown(requirement, value):
    requirement['data']['IssueDetails']['NetQtyRequired'] = value
    assert statuses(assess(requirement))['remaining_quantity'] == 'unknown'


@pytest.mark.parametrize('field,value,check,expected', [
    ('UOM', None, 'unit', 'unknown'),
    ('BOMLineItem', {'Name': 'row'}, 'bom_line', 'unknown'),
    ('IssueControl', True, 'issue_control', 'blocked'),
    ('IssueControl', 2, 'issue_control', 'blocked'),
    ('NetQty2Required', 1, 'secondary_quantity', 'blocked'),
    ('Qty2Required', None, 'secondary_quantity', 'unknown'),
])
def test_unproved_inputs_do_not_pass(requirement, field, value, check, expected):
    requirement['data']['IssueDetails'][field] = value
    assert statuses(assess(requirement))[check] == expected


@pytest.mark.parametrize('field,value', [('Revision', 'other'), ('InstanceId', '0000000000000001'), ('UseROR', True), ('UseROR', 0)])
def test_wrong_product_version_or_ror_blocked(requirement, field, value):
    product = copy.deepcopy(requirement['data']['IssueDetails']['Product'])
    requirement['data']['IssueDetails']['Product'][field] = value
    assert statuses(ci.assess_quantity_requirement(requirement, product, 2))['product_version'] == 'blocked'


def test_partial_demand_cannot_claim_valid_comparison(requirement):
    requirement['complete'] = False
    assert statuses(assess(requirement)) == {'requirement': 'unknown'}


def test_preview_exposes_checks_without_issuing_material(requirement, monkeypatch):
    data = requirement['data']
    product = data['IssueDetails']['Product']
    monkeypatch.setattr(ci.ReadContext, 'resolve', AsyncMock(return_value=product))
    monkeypatch.setattr(ci, 'get_container_details', AsyncMock(return_value=json.dumps({
        'complete': True, 'data': {'Container': data['ParentContainer']}})))
    monkeypatch.setattr(ci, 'get_component_requirement', AsyncMock(return_value=json.dumps(requirement)))
    network = AsyncMock()
    monkeypatch.setattr(ci, 'request', network)
    result = json.loads(asyncio.run(ci.preview_component_issue(
        data['ParentContainer']['Name'], product['Name'], product['Revision'], 3, 5)))
    assert statuses(result['quantity_requirement_checks'])['remaining_quantity'] == 'blocked'
    assert not result['executable'] and 'payload' not in result
    network.assert_not_called()


def test_partial_issue_requires_configured_difference_reason(requirement):
    assert statuses(assess(requirement, 1))['quantity_difference'] == 'blocked'
    assert statuses(assess(requirement, 2))['quantity_difference'] == 'pass'
    requirement['data']['IssueDetails']['ValidateQtyDifference'] = False
    assert statuses(assess(requirement, 1))['quantity_difference'] == 'pass'


@pytest.mark.parametrize('value', [None, 0, 'false'])
def test_missing_or_untyped_difference_rule_is_unknown(requirement, value):
    requirement['data']['IssueDetails']['ValidateQtyDifference'] = value
    assert statuses(assess(requirement))['quantity_difference'] == 'unknown'
