import asyncio
import json
from unittest.mock import AsyncMock
import pytest
from tools import component_issue as ci, get_tool_func
from core.tool_catalog import TOOL_POLICIES


def run(**kw):
    args = dict(container_name='C', product_name='P', product_revision='1', qty=2, issue_control=2, source_name='S')
    args.update(kw)
    return json.loads(asyncio.run(ci.preview_component_issue(**args)))


@pytest.mark.parametrize('kw', [{'qty': 0}, {'qty': -1}, {'qty': float('nan')}, {'qty': float('inf')},
                               {'qty': True}, {'issue_control': 6}, {'source_name': ''},
                               {'product_revision': ''}, {'issue_control': 5}, {'container_instance_id': ''}])
def test_invalid_never_reads(monkeypatch, kw):
    mock = AsyncMock()
    monkeypatch.setattr(ci.ReadContext, 'resolve', mock)
    assert 'error' in run(**kw)
    mock.assert_not_called()


def test_preview_is_never_executable(monkeypatch):
    monkeypatch.setattr(ci.ReadContext, 'resolve', AsyncMock(return_value={'InstanceID': 'p', 'Name': 'P', 'Revision': '1'}))
    monkeypatch.setattr(ci, 'get_container_details', AsyncMock(return_value=json.dumps({'complete': True, 'data': {'Container': {'InstanceId': 'c', 'Name': 'C'}}})))
    data = run()
    assert data['executable'] is False and data['complete'] is False and data['blockers']
    assert data['intent']['quantity_to_issue'] == 2 and 'payload' not in data
    assert run(issue_control=5, source_name='')['intent']['source_kind'] == 'quantity_only'


def test_identity_mismatch(monkeypatch):
    monkeypatch.setattr(ci.ReadContext, 'resolve', AsyncMock(return_value={'InstanceID': 'p'}))
    monkeypatch.setattr(ci, 'get_container_details', AsyncMock(return_value='{"data":{"Container":{"Name":"wrong","InstanceId":"c"}}}'))
    assert 'error' in run()


def test_read_only_registration():
    assert get_tool_func('preview_component_issue')
    assert TOOL_POLICIES['preview_component_issue'].effect == 'read'
