import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from scripts import prepare_bulk_issue_fixture as p


@pytest.fixture
def preflight(monkeypatch):
    class Context:
        def __init__(self, **kwargs): pass
        async def get(self, *args, **kwargs): return {'value': []}
        async def resolve(self, entity, ref, **kwargs):
            values = {'Products': {'Name': 'WM-900A', 'Revision': 'A',
                                  'Workflow': {'Name': 'WF', 'Revision': 'A'}, 'ProductType': {'Name': 'PT'}},
                      'Workflows': {'Name': 'WF', 'Revision': 'A'}, 'ProductTypes': {'Name': 'PT'},
                      'UOMs': {'Name': 'EACH'}, 'ContainerLevels': {'Name': 'BOX'},
                      'OrderStatuses': {'Name': 'Released'},
                      'MfgOrders': {'Name': 'MO', 'Product': {'Name': 'WM-900A'}}}
            return values[entity]
    async def selection(expression, **kwargs):
        field = expression.split('.')[1]
        return json.dumps({'Details': {field: {'RecordCount': 1, 'RowCount': 1, 'SelectionValuesEx': [
            {'Name': field.upper(), 'InstanceId': '0000000000000001'}]}}})
    monkeypatch.setattr(p, 'ReadContext', Context)
    monkeypatch.setattr(p, 'request', AsyncMock(return_value='[]'))
    monkeypatch.setattr(p, 'request_container_start_selection_values', AsyncMock(side_effect=selection))
    for name in ('create_product', 'create_mfgorder', 'container_start'):
        monkeypatch.setattr(p, name, AsyncMock())


def test_plan_counts_every_object_and_never_writes(preflight, tmp_path):
    plan = asyncio.run(p.prepare('CDX-T05-BULK-PLAN', tmp_path))
    assert plan['count'] == 6 and len(set(plan['names'].values())) == 6
    assert plan['source_quantity'] == 3 and plan['destination_quantity'] == 1
    assert plan['material']['issueControl'] == 2 and not plan['component_issue_submitted']
    p.create_product.assert_not_called(); p.create_mfgorder.assert_not_called(); p.container_start.assert_not_called()
    assert not (tmp_path/'CDX-T05-BULK-PLAN.json').exists()


def test_ambiguous_first_write_stops_and_journal_cannot_replay(preflight, tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'SAFE_CREATE_THRESHOLD', 20)
    p.create_product.side_effect = TimeoutError('Unknown creation result')
    with pytest.raises(TimeoutError):
        asyncio.run(p.prepare('CDX-T05-BULK-STOP', tmp_path, True))
    path = tmp_path/'CDX-T05-BULK-STOP.json'
    before = path.read_bytes(); report = json.loads(before)
    assert report['state'] == 'stopped_without_retry' and len(report['attempts']) == 1
    assert report['attempts'][0]['state'] == 'unknown_or_failed'
    p.create_product.assert_awaited_once(); p.create_mfgorder.assert_not_called(); p.container_start.assert_not_called()
    with pytest.raises(ValueError, match='never replay'):
        asyncio.run(p.prepare('CDX-T05-BULK-STOP', tmp_path, True))
    assert path.read_bytes() == before


def test_total_threshold_blocks_before_any_lookup_or_write(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'SAFE_CREATE_THRESHOLD', 5)
    context = AsyncMock(); monkeypatch.setattr(p, 'ReadContext', context)
    with pytest.raises(ValueError, match='Six objects'):
        asyncio.run(p.prepare('CDX-T05-BULK-LIMIT', tmp_path, True))
    context.assert_not_called()


@pytest.mark.parametrize('prefix', ['REAL-PRODUCTION', 'CDX-T05-../../', "CDX-T05-A';DELETE"])
def test_fixture_prefix_cannot_escape_test_namespace(tmp_path, prefix):
    with pytest.raises(ValueError):
        asyncio.run(p.prepare(prefix, tmp_path, True))
