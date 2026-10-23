import asyncio
import json
from unittest.mock import AsyncMock
import pytest
from tools import collection_history as ch, get_tool_func

CID = '0004108000000052'
TID = '0000000000000001'


def setup(monkeypatch, rows=None):
    info = AsyncMock(return_value=json.dumps({'data': {'Container': {'Name': 'C', 'InstanceId': CID}}}))
    req = AsyncMock(return_value=json.dumps(rows if rows is not None else []))
    monkeypatch.setattr(ch, 'get_container_details', info)
    monkeypatch.setattr(ch, 'request', req)
    return info, req


def run(**kwargs):
    return json.loads(asyncio.run(ch.get_container_collection_history('C', **kwargs)))


def row(key='1', **kwargs):
    return {'DataPointHistoryDetailId': key, 'ContainerId': CID, 'TxnId': TID, **kwargs}


def test_empty_is_not_failed_submission(monkeypatch):
    _, req = setup(monkeypatch)
    r = run()
    assert r['rows'] == [] and r['page_complete'] and not r['complete']
    assert r['submission_outcome'] == 'unknown'
    assert get_tool_func('get_container_collection_history')
    assert req.call_args.kwargs['retry_read_only'] is True
    assert req.call_args.args == ('POST', '/api/AdHoc')


def test_pagination_and_raw_values(monkeypatch):
    _, req = setup(monkeypatch, [row(DataValue='0', BooleanValue=False), row('2')])
    r = run(limit=1, txn_id=TID)
    assert r['has_more'] and r['next_offset'] == 1
    assert r['rows'][0]['BooleanValue'] is False
    q = req.call_args.kwargs['body']['queryText']
    assert "AND d.TxnId='" + TID in q
    assert 'FETCH NEXT 2 ROWS ONLY' in q


@pytest.mark.parametrize('kwargs', [{'txn_id': "x' OR 1=1"}, {'container_instance_id': ''}, {'limit': True}, {'offset': -1}, {'limit': 101}])
def test_invalid_before_io(monkeypatch, kwargs):
    info, req = setup(monkeypatch)
    assert 'error' in run(**kwargs)
    info.assert_not_called()
    req.assert_not_called()


def test_wrong_identity_no_history(monkeypatch):
    info, req = setup(monkeypatch)
    info.return_value = json.dumps({'data': {'Container': {'Name': 'Other', 'InstanceId': CID}}})
    assert 'error' in run()
    req.assert_not_called()


@pytest.mark.parametrize('rows', [[row(ContainerId='other')], [row(TxnId='other')], [row(), row()], {'error': 'denied'}])
def test_malformed_or_mismatched(monkeypatch, rows):
    setup(monkeypatch, rows)
    r = run(txn_id=TID)
    assert 'error' in r and 'rows' not in r
