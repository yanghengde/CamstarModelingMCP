"""AllowMove maintenance contract. All PUTs use mocks; no real MES writes."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.prompts import MOVEMENT_RULE
from agent.tool_routing import ROUTING_INSTRUCTION, select_tools
from agent.safety import classify_tool
from core import http_client
from core.tool_catalog import TOOL_POLICIES
from tools import container_levels as levels, get_tool_func
from tools.tool_discovery import get_capabilities, load_tool_group


CID = '0005288000000003'


def snapshot(allow_move=False):
    return {'@odata.context': 'metadata', 'InstanceID': CID, 'Name': 'BOX',
            'AllowMove': allow_move, 'IsFrozen': False, 'Description': 'Default Box Container Level',
            'Notes': None, 'IsNameUnique': False, 'ContainerNumberingRule': None,
            'ParentLevels': [], 'ChildLevels': []}


def invoke(**args):
    return json.loads(asyncio.run(levels.update_container_level(**{'key': CID, 'allow_move': True, **args})))


@pytest.mark.parametrize('target', [True, False])
def test_single_update_keeps_original_identity_and_only_changes_allowmove(monkeypatch, target):
    before, after = snapshot(not target), snapshot(target)
    mock = AsyncMock(side_effect=[json.dumps(before), '✅ PUT succeeded (HTTP 204, empty body).', json.dumps(after)])
    monkeypatch.setattr(levels, 'request', mock)
    result = invoke(allow_move=target, expected_name='BOX')
    assert result['verified'] and result['complete'] and result['observed_matches']
    assert result['submission_state'] == 'acknowledged'
    assert result['before'] == {'AllowMove': not target} and result['after'] == {'AllowMove': target}
    assert result['other_changed_fields'] == []
    assert [c.args[:2] for c in mock.await_args_list] == [
        ('GET', '/api/ContainerLevels/' + CID), ('PUT', '/api/ContainerLevels/' + CID), ('GET', '/api/ContainerLevels/' + CID)]
    assert mock.await_args_list[1].kwargs['body'] == {'name': 'BOX', 'allowMove': target}
    assert 'body' not in mock.await_args_list[0].kwargs


def test_already_correct_requires_only_read_even_when_frozen(monkeypatch):
    before = snapshot(True)
    before['IsFrozen'] = True
    mock = AsyncMock(return_value=json.dumps(before))
    monkeypatch.setattr(levels, 'request', mock)
    result = invoke()
    assert result['submission_state'] == 'not_needed' and result['verified']
    assert mock.await_count == 1 and mock.await_args.args[0] == 'GET'


@pytest.mark.parametrize('args', [
    {'key': 'BOX'}, {'key': '../BOX'}, {'key': ''}, {'key': 'not-a-valid-id00'},
    {'allow_move': 'true'}, {'allow_move': 'false'}, {'allow_move': 1}, {'allow_move': None},
    {'expected_name': ''}, {'expected_name': 'BOX\n'},
])
def test_invalid_arguments_never_send_http(monkeypatch, args):
    mock = AsyncMock()
    monkeypatch.setattr(levels, 'request', mock)
    result = invoke(**args)
    assert result['error'] and result['submission_state'] == 'not_submitted'
    mock.assert_not_awaited()


@pytest.mark.parametrize('problem', ['wrong_id', 'missing_allowmove', 'invalid_allowmove', 'frozen', 'unknown_frozen', 'incomplete', 'wrong_name'])
def test_identity_freeze_and_response_failures_block_put(monkeypatch, problem):
    before = snapshot()
    args = {}
    if problem == 'wrong_id': before['InstanceID'] = '0005288000000006'
    if problem == 'missing_allowmove': before.pop('AllowMove')
    if problem == 'invalid_allowmove': before['AllowMove'] = 'false'
    if problem == 'frozen': before['IsFrozen'] = True
    if problem == 'unknown_frozen': before.pop('IsFrozen')
    if problem == 'incomplete': before['@odata.nextLink'] = '/more'
    if problem == 'wrong_name': args['expected_name'] = 'CARRIER'
    mock = AsyncMock(return_value=json.dumps(before))
    monkeypatch.setattr(levels, 'request', mock)
    result = invoke(**args)
    assert result['error'] and not result['verified'] and result['submission_state'] == 'not_submitted'
    assert mock.await_count == 1 and mock.await_args.args[0] == 'GET'


@pytest.mark.parametrize('reply,expected', [
    ('❌ Request timed out', 'unknown'), ('❌ HTTP 503 Error', 'unknown'),
    ('❌ HTTP 408 Error', 'unknown'), ('❌ HTTP 400 Error', 'rejected'),
    ('{"ErrorCode":"13500597","ErrorMessage":"requires input"}', 'unknown'),
])
def test_unknown_or_rejected_write_is_not_retried_even_when_readback_matches(monkeypatch, reply, expected):
    mock = AsyncMock(side_effect=[json.dumps(snapshot()), reply, json.dumps(snapshot(True))])
    monkeypatch.setattr(levels, 'request', mock)
    result = invoke()
    assert result['observed_matches'] and not result['verified'] and not result['complete']
    assert result['submission_state'] == expected and result['error']
    assert sum(c.args[0] == 'PUT' for c in mock.await_args_list) == 1


@pytest.mark.parametrize('after', ['❌ Request timed out', '{', json.dumps(snapshot()), json.dumps({**snapshot(True), 'Name': 'OTHER'})])
def test_success_response_alone_does_not_count_as_verified(monkeypatch, after):
    mock = AsyncMock(side_effect=[json.dumps(snapshot()), '{"value":"updated"}', after])
    monkeypatch.setattr(levels, 'request', mock)
    result = invoke()
    assert not result['verified'] and result['error'] and not result['complete']
    assert result['submission_state'] == 'acknowledged'
    assert sum(c.args[0] == 'PUT' for c in mock.await_args_list) == 1


def test_other_returned_fields_changed_are_reported_without_rollback(monkeypatch):
    after = snapshot(True)
    after['Description'] = 'concurrent update'
    mock = AsyncMock(side_effect=[json.dumps(snapshot()), '{"value":"updated"}', json.dumps(after)])
    monkeypatch.setattr(levels, 'request', mock)
    result = invoke()
    assert result['verified'] and not result['complete']
    assert result['other_changed_fields'] == ['Description'] and result['warnings']
    assert mock.await_count == 3


def test_http_dispatch_timeout_after_commit_remains_unknown(monkeypatch):
    async def run():
        state = snapshot()
        calls = []
        def server(req):
            calls.append(req.method)
            assert req.url.path.endswith('/api/ContainerLevels/' + CID)
            if req.method == 'PUT':
                assert json.loads(req.content) == {'name': 'BOX', 'allowMove': True}
                state['AllowMove'] = True
                raise httpx.ReadTimeout('response lost after submission', request=req)
            return httpx.Response(200, json=copy.deepcopy(state))
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            monkeypatch.setattr(http_client, '_client', client)
            monkeypatch.setattr(http_client, 'get_headers', lambda: {'Content-Type': 'application/json'})
            result = json.loads(await levels.update_container_level(CID, True, 'BOX'))
        assert calls == ['GET', 'PUT', 'GET']
        assert result['submission_state'] == 'unknown' and result['observed_matches'] and not result['verified']
    asyncio.run(run())


def test_registration_schema_policy_and_discovery_expose_actual_write_tool():
    assert get_tool_func('update_container_level') is levels.update_container_level
    assert classify_tool('update_container_level') == 'update'
    detail = json.loads(asyncio.run(get_capabilities(tool_name='update_container_level')))['tool']
    assert detail['status'] == 'registered' and detail['group'] == 'reference'
    assert set(detail['input_schema']['required']) == {'key'}
    assert {'type':'boolean'} in detail['input_schema']['properties']['allow_move']['anyOf']
    assert 'update_container_level' in json.loads(asyncio.run(load_tool_group('reference', include_writes=True)))['tools']
    assert 'update_container_level' not in json.loads(asyncio.run(load_tool_group('reference')))['tools']
    assert 'ContainerLevel' in MOVEMENT_RULE and MOVEMENT_RULE in ROUTING_INSTRUCTION


def test_direct_field_followup_routes_to_reference_writes_but_reads_do_not():
    tools = [{'function': {'name': name}} for name in TOOL_POLICIES]
    for messages in ([{'role': 'user', 'content': '把BOX的AllowMove从false改为true'}],
                     [{'role': 'user', 'content': '查看BOX层级'}, {'role': 'user', 'content': 'AllowMove=false改为true'}]):
        names = {t['function']['name'] for t in select_tools(tools, messages)}
        assert {'update_container_level', 'get_container_level'} <= names
    selected = select_tools(tools, [{'role': 'user', 'content': '查看BOX的AllowMove'}])
    assert 'update_container_level' not in {t['function']['name'] for t in selected}
