import asyncio
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tools import component_issue_bulk_receipts as r

DATA = Path(__file__).resolve().parents[1]/'docs/t05-fixtures'


@pytest.fixture
def observed(monkeypatch):
    report = json.loads((DATA/'bulk-derived-execution-F.json').read_text(encoding='utf-8'))
    proof = json.loads((DATA/'bulk-derived-receipt-F.json').read_text(encoding='utf-8'))
    rows = {read['table']: json.loads(read['response']) for read in proof['reads']}
    monkeypatch.setattr(r, 'CAMSTAR_SHOPFLOOR_BASE_URL', report['environment'])
    async def request(method, path, **kwargs):
        assert method == 'POST' and path == '/api/AdHoc' and kwargs['retry_read_only'] is True
        query = kwargs['body']['queryText']
        assert query.startswith('SELECT TOP 2 ') and 'execute' not in kwargs.get('params', {})
        table = next(table for table in rows if 'FROM '+table+' WHERE' in query)
        return json.dumps(rows[table])
    network = AsyncMock(side_effect=request); monkeypatch.setattr(r, 'request', network)
    return report, rows, network


def verify(observed):
    return asyncio.run(r.receipt(observed[0]))


def test_recorded_bulk_source_chain_is_exactly_one_committed_transaction(observed):
    result = verify(observed)
    assert result['verified_success'] and result['complete']
    assert result['transaction_id'] == '400000000000f21b'
    assert result['source_container_id'] == '00041080000000e7'
    assert not result['mes_write_performed'] and not result['original_probe_reusable']
    assert observed[2].call_count == 4
    assert observed[0]['source_after']['data']['ContainerStatusDetails']['Qty'] == 1
    assert observed[0]['requirement_after']['data']['IssueDetails']['NetQtyRequired'] == 0


@pytest.mark.parametrize('table', ['HistoryMainline','IssueHistoryDetail','ComponentIssueHistory','IssueActualsHistory'])
@pytest.mark.parametrize('bad', ['empty','duplicate','permission'])
def test_incomplete_receipt_does_not_prove_failure_or_release(observed, table, bad):
    rows = observed[1]
    rows[table] = [] if bad == 'empty' else rows[table]*2 if bad == 'duplicate' else {'ErrorCode': 'permission'}
    assert not verify(observed)['verified_success']


@pytest.mark.parametrize('table,field,value', [
    ('HistoryMainline','Comments','other'), ('HistoryMainline','SpecId','other'),
    ('HistoryMainline','OperationId','other'), ('HistoryMainline','ContainerId','other'),
    ('IssueHistoryDetail','IssueControl','5'), ('IssueHistoryDetail','ProductMaterialListItemId','other'),
    ('IssueHistoryDetail','UOMId','other'), ('IssueHistoryDetail','ActualQtyIssued','1'),
    ('IssueHistoryDetail','QtyRequired','3'), ('IssueHistoryDetail','IntegrationTxnId','other'),
    ('ComponentIssueHistory','HistoryMainlineId','other'), ('ComponentIssueHistory','HistoryId','other'),
    ('IssueActualsHistory','CDOTypeId','6941'), ('IssueActualsHistory','FromContainerId',''),
    ('IssueActualsHistory','FromContainerId','0000000000000001'), ('IssueActualsHistory','ToContainerId','other'),
    ('IssueActualsHistory','ProductId','other'), ('IssueActualsHistory','Qty','4'),
    ('IssueActualsHistory','Qty2','1'), ('IssueActualsHistory','FromLot','external'),
    ('IssueActualsHistory','FromStockPoint','point'), ('IssueActualsHistory','IssueHistoryDetailId','other'),
])
def test_bulk_subtype_source_quantity_and_all_links_are_bound(observed, table, field, value):
    observed[1][table][0][field] = value
    assert not verify(observed)['verified_success']


@pytest.mark.parametrize('change', ['digest','environment','marker','attempt','mode','source','duplicate'])
def test_bad_probe_never_queries_history(observed, change):
    report = observed[0]
    if change == 'digest': report['body_digest'] = 'bad'
    elif change == 'environment': report['environment'] = 'different'
    elif change == 'marker': report['marker'] = "x';DELETE"
    elif change == 'attempt': report['request_sent'] = False
    else:
        line = report['body']['serviceDetails'][0]
        if change == 'mode': line['issueControl'] = 5
        elif change == 'source': line['actuals'][0]['fromContainer']['instanceId'] = '0000000000000001'
        elif change == 'duplicate': line['actuals'] *= 2
        report['body_digest'] = hashlib.sha256(json.dumps(report['body'],sort_keys=True).encode()).hexdigest()
    assert not verify(observed)['verified_success']
    observed[2].assert_not_called()


def test_late_actual_history_may_later_verify_without_submitting(observed):
    actuals = copy.deepcopy(observed[1]['IssueActualsHistory'])
    observed[1]['IssueActualsHistory'] = []
    assert not verify(observed)['verified_success']
    observed[1]['IssueActualsHistory'] = actuals
    assert verify(observed)['verified_success'] and observed[2].call_count == 8
