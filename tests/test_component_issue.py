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
    monkeypatch.setattr(ci, 'get_component_requirement', AsyncMock(return_value='{"status":"no_matching_requirement"}'))
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


def test_requirement_contract_and_no_match(monkeypatch):
    mock = AsyncMock(return_value='HTTP Error Response: {"ErrorCode":"13501228"}')
    monkeypatch.setattr(ci, 'request', mock)
    result = json.loads(asyncio.run(ci.get_component_requirement('C', 'P', 'c')))
    assert result['status'] == 'no_matching_requirement' and not result['complete']
    assert mock.call_args.kwargs['body'] == {'parentContainer': {'name': 'C', 'instanceId': 'c'}, 'objectName': 'P'}
    assert mock.call_args.kwargs['retry_read_only'] is True


def test_requirement_identity_and_partial(monkeypatch):
    data = {'ParentContainer': {'Name': 'C', 'InstanceId': 'c'}, 'ObjectName': 'P',
            'IssueDetails': {'BOMLineItem': {'Name': 'line'}, 'QtyRequired': 2}}
    mock = AsyncMock(return_value=json.dumps(data))
    monkeypatch.setattr(ci, 'request', mock)
    def read(): return json.loads(asyncio.run(ci.get_component_requirement('C', 'P', 'c')))
    assert read()['status'] == 'matched'
    data['IssueDetails'].pop('BOMLineItem');mock.return_value=json.dumps(data)
    assert read()['status'] == 'requirement_incomplete'
    data['ParentContainer']['Name']='other';mock.return_value=json.dumps(data)
    assert 'error' in read() and not read()['complete']


def test_requirement_error_not_no_match(monkeypatch):
    monkeypatch.setattr(ci, 'request', AsyncMock(return_value='{"ErrorCode":"13369346"}'))
    result=json.loads(asyncio.run(ci.get_component_requirement('C','P')))
    assert result['status']=='query_failed'


def test_history_bounded_fixed_query(monkeypatch):
    identifier='0004108000000052'
    monkeypatch.setattr(ci,'get_container_details',AsyncMock(return_value=json.dumps({'data':{'Container':{'Name':'C','InstanceId':identifier}}})))
    rows=[{'IssueActualsHistoryId':str(i),'ToContainerId':identifier,'Qty':1} for i in range(2)]
    mock=AsyncMock(return_value=json.dumps(rows));monkeypatch.setattr(ci,'request',mock)
    d=json.loads(asyncio.run(ci.get_component_issue_history('C',limit=1)))
    assert d['has_more'] and not d['complete'] and len(d['rows'])==1
    query=mock.call_args.kwargs['body']['queryText']
    assert "WHERE ToContainerId='0004108000000052'" in query and 'FETCH NEXT 2 ROWS' in query
    mock.return_value='[]'
    d=json.loads(asyncio.run(ci.get_component_issue_history('C')))
    assert d['complete'] and d['rows']==[]


def test_history_identity_rejects_injection(monkeypatch):
    monkeypatch.setattr(ci,'get_container_details',AsyncMock(return_value=json.dumps({'data':{'Container':{'Name':'C','InstanceId':"x';DROP"}}})))
    mock=AsyncMock();monkeypatch.setattr(ci,'request',mock)
    assert 'error' in json.loads(asyncio.run(ci.get_component_issue_history('C')))
    mock.assert_not_called()


def test_history_rejects_other_container_rows(monkeypatch):
    monkeypatch.setattr(ci,'get_container_details',AsyncMock(return_value=json.dumps({'data':{'Container':{'Name':'C','InstanceId':'0004108000000052'}}})))
    monkeypatch.setattr(ci,'request',AsyncMock(return_value='[{"IssueActualsHistoryId":"h","ToContainerId":"wrong"}]'))
    assert 'error' in json.loads(asyncio.run(ci.get_component_issue_history('C')))
