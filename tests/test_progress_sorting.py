import asyncio
from unittest.mock import AsyncMock

from tools import order_progress as progress


def test_distribution_uses_route_not_sequence_and_keeps_unknown_last(monkeypatch):
    resolve = AsyncMock(return_value={'InstanceID': 'wf'})
    monkeypatch.setattr(progress.ReadContext, 'resolve', resolve)
    read = AsyncMock(return_value={
        'workflow': {'name': 'WF', 'revision': 'A'},
        'standard_route_complete': True, 'standard_route': ['start', 'end'],
        'steps': [{'name': 'start', 'sequence': 9}, {'name': 'end', 'sequence': 1}],
        'warnings': [],
    })
    monkeypatch.setattr(progress, '_read_steps', read)
    rows = [{'workflow': 'WF', 'revision': 'A', 'step': s, 'container_count': 1}
            for s in ['end', 'branch', 'start']]
    warnings = []
    asyncio.run(progress._sort_distribution(rows, warnings))
    assert [r['step'] for r in rows] == ['start', 'end', 'branch']
    assert [r['route_position'] for r in rows] == [1, 2, None]
    assert rows[0]['sequence'] == 9
    assert warnings
    assert read.await_count == 1


def test_failed_route_preserves_distribution(monkeypatch):
    monkeypatch.setattr(progress.ReadContext, 'resolve', AsyncMock(side_effect=ValueError('unavailable')))
    rows = [{'workflow': 'WF', 'revision': 'A', 'step': 'end', 'container_count': 3}]
    warnings = []
    asyncio.run(progress._sort_distribution(rows, warnings))
    assert rows[0]['container_count'] == 3
    assert rows[0]['route_position'] is None
    assert rows[0]['sort_source'] == 'unknown'
    assert warnings
