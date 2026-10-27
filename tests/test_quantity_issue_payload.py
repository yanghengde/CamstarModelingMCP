import copy
import json
from pathlib import Path
import pytest
from tools import component_issue_payload as p

DATA = Path(__file__).resolve().parents[1]/'docs/t05-fixtures'


@pytest.fixture
def observed():
    return json.loads((DATA/'nested-actuals-preview-C-selected.json').read_text(encoding='utf-8'))


def test_nested_full_quantity_uses_actuals_not_cumulative(observed):
    body = p.build_quantity_issue(observed['requirement'], 2)
    row = body['serviceDetails'][0]
    assert row['actualQtyIssued'] == row['actuals'][0]['qtyIssued'] == 2
    assert 'qtyIssued' not in row and 'issueActualDetails' not in body
    assert 'validSequence' not in row and 'validateQtyDifference' not in row
    assert p.verify_quantity_echo(observed['response'], body)


@pytest.mark.parametrize('value', [True, 0, -1, 1, 3, float('nan'), float('inf'), '2'])
def test_partial_invalid_or_over_issue_not_supported(observed, value):
    with pytest.raises(ValueError): p.build_quantity_issue(observed['requirement'], value)


@pytest.mark.parametrize('field,value', [('QtyIssued', 1), ('ActualQtyIssued', 1), ('NetQty2Required', 1),
    ('Qty2Required', None), ('IssueControl', True), ('IssueControl', 2), ('ValidateQtyDifference', None)])
def test_unproved_requirement_rejected(observed, field, value):
    observed['requirement']['data']['IssueDetails'][field] = value
    with pytest.raises(ValueError): p.build_quantity_issue(observed['requirement'], 2)


@pytest.mark.parametrize('change', ['actual_qty', 'summary_qty', 'product_revision', 'line_owner', 'ror', 'flag', 'paging'])
def test_echo_must_preserve_all_nested_intent(observed, change):
    response = copy.deepcopy(observed['response']); row = response['data']['ServiceDetails'][0]
    if change == 'actual_qty': row['Actuals'][0]['QtyIssued'] = '1'
    elif change == 'summary_qty': row['ActualQtyIssued'] = '1'
    elif change == 'product_revision': row['Actuals'][0]['Product']['Revision'] = '2'
    elif change == 'line_owner': row['BOMLineItem']['Parent']['InstanceId'] = '0000000000000001'
    elif change == 'ror': row['Product']['UseRor'] = 'true'
    elif change == 'flag': response['data']['PreventOverIssue'] = 1
    elif change == 'paging': response['@odata.nextLink'] = 'next'
    with pytest.raises(ValueError): p.verify_quantity_echo(response, observed['body'])
