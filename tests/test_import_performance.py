"""Bound attachment parsing before validation and keep disk work off the loop."""
import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from tools import excel_importer, import_plans


@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
def test_oversized_import_reads_only_sentinel_rows_and_never_queries_mes(monkeypatch, tmp_path, extension):
    path = tmp_path / ('large.' + extension)
    frame = pd.DataFrame({'W': ['WF'] * 500, 'R': ['A'] * 500,
                          'S': [str(i) for i in range(500)], 'P': ['SPEC'] * 500})
    if extension == 'csv':
        frame.to_csv(path, index=False)
    else:
        frame.to_excel(path, index=False)
    reader_name = 'read_csv' if extension == 'csv' else 'read_excel'
    reader = getattr(pd, reader_name)
    lengths, threads = [], []
    def observed(*args, **kwargs):
        assert kwargs['nrows'] == 201
        result = reader(*args, **kwargs)
        lengths.append(len(result))
        threads.append(threading.get_ident())
        return result
    monkeypatch.setattr(pd, reader_name, observed)
    resolve = AsyncMock(side_effect=AssertionError('oversized input must not query MES'))
    monkeypatch.setattr(import_plans.ReadContext, 'resolve', resolve)
    mapping = json.dumps({'sheet_name': 'Sheet1', 'fields': {
        'workflow_name': 'W', 'workflow_revision': 'R', 'step_name': 'S', 'spec_name': 'P'}})
    result = json.loads(asyncio.run(import_plans.preview_workflow_import(str(path), mapping)))
    assert result['error'] and lengths == [201]
    assert threads[0] != threading.get_ident()
    resolve.assert_not_awaited()


@pytest.mark.parametrize('rows,lower_bound', [(3, False), (200, False), (500, True)])
def test_schema_reports_bounded_count_and_three_samples(tmp_path, rows, lower_bound):
    path = tmp_path / 'sheet.xlsx'
    pd.DataFrame({'name': [f'row-{i}' for i in range(rows)]}).to_excel(path, index=False)
    result = json.loads(asyncio.run(excel_importer.parse_excel_schema(str(path))))
    sheet = result['sheets']['Sheet1']
    assert sheet['row_count_estimate'] == min(rows, 201)
    assert sheet['row_count_is_lower_bound'] is lower_bound
    assert len(sheet['sample_rows']) == 3


def test_slow_schema_parse_does_not_block_event_loop(monkeypatch, tmp_path):
    path = tmp_path / 'placeholder.xlsx'
    path.touch()
    entered, release = threading.Event(), threading.Event()
    def slow_parse(_):
        entered.set()
        assert release.wait(5)
        return {}
    monkeypatch.setattr(excel_importer, '_read_schema', slow_parse)
    async def run():
        task = asyncio.create_task(excel_importer.parse_excel_schema(str(path)))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert not task.done()
        finally:
            release.set()
        assert json.loads(await asyncio.wait_for(task, 3))['success']
    asyncio.run(run())
