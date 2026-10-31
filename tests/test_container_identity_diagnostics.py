"""View row IDs must never become Container references. All traffic is mocked."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from agent.prompts import MOVEMENT_RULE
from core.tool_catalog import TOOL_POLICIES
from tools import container_inquiry as inquiry, container_moves as moves, get_tool_func


CID = '0004108000000109'
VID = '001af500000000a5'
NAME = 'SN_T_0029'


def status(view_id=VID):
    return {'ContainerStatusDetails': {
        'InstanceID': view_id,
        'Container': {'Name': NAME, 'InstanceId': CID, 'CDOTypeName': 'Container'},
        'ContainerLevel': {'Name': 'BOX', 'InstanceId': '0005288000000003'},
        'Operation': {'Name': 'OP-STAMPING', 'InstanceId': '00060e800000001b'},
        'InProcess': True, 'Status': 1, 'Qty': 1,
        'StepName': 'SP-STAMPING-001', 'LastMoveDate': '2026-10-31T09:48:34',
    }}


def test_changing_view_id_keeps_same_actual_container(monkeypatch):
    request = AsyncMock(side_effect=[json.dumps(status()), json.dumps(status('001af500000000a8'))])
    monkeypatch.setattr(inquiry, 'request', request)
    a = json.loads(asyncio.run(inquiry.get_container_details(NAME)))
    b = json.loads(asyncio.run(inquiry.get_container_details(NAME)))
    assert a['complete'] and b['complete']
    assert a['container_instance_id'] == b['container_instance_id'] == CID
    assert a['view_record_instance_id'] != b['view_record_instance_id']
    assert a['identity_source'] == 'data.ContainerStatusDetails.Container'


def test_info_uses_actual_root_container_not_root_instance_id(monkeypatch):
    data = {'InstanceID': VID, 'Container': status()['ContainerStatusDetails']['Container'],
            'ContainerInfo': {}, 'CurrentSpec': {}, 'CurrentRouteStep': {}}
    monkeypatch.setattr(inquiry, 'request', AsyncMock(return_value=json.dumps(data)))
    result = json.loads(asyncio.run(inquiry.get_container_details(NAME, view='info')))
    assert result['container_instance_id'] == CID and result['view_record_instance_id'] is None
    assert result['identity_source'] == 'data.Container'


@pytest.mark.parametrize('change', ['missing_ref', 'different_name', 'malformed_id', 'requested_view_id'])
def test_identity_must_not_fall_back_to_view_row(monkeypatch, change):
    data = status()
    row = data['ContainerStatusDetails']
    requested = None
    if change == 'missing_ref': row.pop('Container')
    if change == 'different_name': row['Container']['Name'] = 'OTHER'
    if change == 'malformed_id': row['Container']['InstanceId'] = 'bad'
    if change == 'requested_view_id': requested = VID
    request = AsyncMock(return_value=json.dumps(data))
    monkeypatch.setattr(inquiry, 'request', request)
    result = json.loads(asyncio.run(inquiry.get_container_details(NAME, requested)))
    assert result['complete'] is False and result['error']
    assert result.get('container_instance_id') != VID
    request.assert_awaited_once()  # No silent name-only retry with a replacement target.


def test_plain_server_error_is_json_and_retains_original_diagnosis(monkeypatch):
    raw = '❌ HTTP 400: ViewContainerAsParent CDO not found'
    request = AsyncMock(return_value=raw)
    monkeypatch.setattr(inquiry, 'request', request)
    result = json.loads(asyncio.run(inquiry.get_container_details(NAME, VID)))
    assert result['complete'] is False and raw in result['error']
    assert result['requested_container_instance_id'] == VID
    post = AsyncMock()
    monkeypatch.setattr(moves, 'request_shopfloor', post)
    result = asyncio.run(moves.container_move_out(NAME, container_instance_id=VID))
    assert 'ViewContainerAsParent CDO not found' in result
    assert 'Expecting value' not in result
    post.assert_not_awaited()
    assert request.await_count == 2  # One query per independent call, no fallback.


def test_real_query_pipeline_uses_nested_actual_id_for_mocked_move(monkeypatch):
    monkeypatch.setattr(inquiry, 'request', AsyncMock(return_value=json.dumps(status())))
    monkeypatch.setattr(moves.ReadContext, 'resolve', AsyncMock(return_value={'AllowMove': True}))
    post = AsyncMock(return_value='updated')
    monkeypatch.setattr(moves, 'request_shopfloor', post)
    assert asyncio.run(moves.container_move_out(NAME)) == 'updated'
    assert post.await_args.kwargs['body']['container'] == {'name': NAME, 'instanceId': CID}


def test_diagnostics_is_read_only_and_does_not_infer_children_or_readiness(monkeypatch):
    query = AsyncMock(return_value=json.dumps(status()))
    monkeypatch.setattr(inquiry, 'request', query)
    resolve = AsyncMock(side_effect=[{'Name': 'BOX', 'AllowMove': True, 'ParentLevels': [], 'ChildLevels': []},
                                    {'Name': 'OP-STAMPING', 'ThruputReportingLevel': {'Name': 'BOX'}}])
    monkeypatch.setattr(moves.ReadContext, 'resolve', resolve)
    post = AsyncMock()
    monkeypatch.setattr(moves, 'request_shopfloor', post)
    result = json.loads(asyncio.run(moves.get_moveout_diagnostics(NAME)))
    assert result['complete'] and result['container_instance_id'] == CID
    assert result['status_view_record_instance_id'] == VID
    assert result['level']['AllowMove'] is True
    for flag in ('writes_executed', 'root_cause_verified', 'ready_for_execution', 'actual_child_containers_verified'):
        assert result[flag] is False
    fields = {f['owner']: f for f in result['native_contract_evidence']['subservice_fields']}
    assert fields['ChangeQty']['LabelName'] == 'Transaction Details'
    assert fields['Thruput']['LabelName'] != 'Transaction Details'
    live = result['native_contract_evidence']['live_subservice_swagger']
    assert 'serviceDetails' in live['ChangeQty']['required']
    assert 'serviceDetails' not in live['Thruput']['required']
    query.assert_awaited_once()
    assert resolve.await_count == 2
    post.assert_not_awaited()


@pytest.mark.parametrize('raw', ['not JSON', '{"complete":false,"error":"unavailable"}', '[]'])
def test_failed_diagnostics_cannot_claim_complete(monkeypatch, raw):
    monkeypatch.setattr(moves, 'get_container_details', AsyncMock(return_value=raw))
    resolve = AsyncMock()
    monkeypatch.setattr(moves.ReadContext, 'resolve', resolve)
    result = json.loads(asyncio.run(moves.get_moveout_diagnostics(NAME)))
    assert not result['complete'] and result['errors']
    resolve.assert_not_awaited()


def test_diagnostics_registered_as_read_and_policy_explains_identity_and_evidence():
    assert TOOL_POLICIES['get_moveout_diagnostics'].effect == 'read'
    assert get_tool_func('get_moveout_diagnostics') is moves.get_moveout_diagnostics
    assert 'ViewContainerAsParent' in MOVEMENT_RULE
    assert 'ChangeQty.ServiceDetails' in MOVEMENT_RULE
    assert '不证明实际容器没有子容器' in MOVEMENT_RULE
