"""UOM mutation behavior and model discovery; no live writes."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from tools import uoms, get_tool_func
from tools.tool_discovery import load_tool_group
from agent.tool_routing import GROUP_TERMS
from agent.tool_routing import select_tools
from agent.safety import classify_tool

ID = '0008de8000000002'


def row(**changes):
    return dict(InstanceID=ID, Name='V', Description='Voltage', Notes=None,
                IsFrozen=False, InstanceLocked=False, **changes)


def mock(monkeypatch, responses):
    request = AsyncMock(side_effect=[json.dumps(r) if isinstance(r, dict) else r for r in responses])
    monkeypatch.setattr(uoms, 'request', request)
    return request


def test_create_checks_name_and_verifies_fields(monkeypatch):
    request = mock(monkeypatch, [{'value': []}, '✅ POST succeeded', {'value': [row()]}, row()])
    result = json.loads(asyncio.run(uoms.create_uom('V', description='Voltage')))
    assert result['verified'] and result['instance_id'] == ID
    assert request.await_args_list[1].kwargs['body'] == {'name': 'V', 'description': 'Voltage'}
    assert [c.args[0] for c in request.await_args_list] == ['GET', 'POST', 'GET', 'GET']


@pytest.mark.parametrize('response', [{'value': [row()]}, {'value': [], '@odata.nextLink': 'next'}, 'bad response'])
def test_create_existing_or_uncertain_list_never_writes(monkeypatch, response):
    request = mock(monkeypatch, [response])
    result = json.loads(asyncio.run(uoms.create_uom('V')))
    assert not result['verified'] and result['submission_state'] == 'not_submitted'
    assert request.await_count == 1


def test_timeout_readback_match_does_not_prove_create(monkeypatch):
    request = mock(monkeypatch, [{'value': []}, '❌ Request timed out', {'value': [row()]}, row()])
    result = json.loads(asyncio.run(uoms.create_uom('V')))
    assert result['submission_state'] == 'unknown' and result['observed_matches'] and not result['verified']
    assert sum(c.args[0] == 'POST' for c in request.await_args_list) == 1


def test_minimal_update_and_explicit_clear(monkeypatch):
    after = {**row(), 'Notes': ''}
    request = mock(monkeypatch, [row(), '✅ PUT succeeded', after])
    result = json.loads(asyncio.run(uoms.update_uom(ID, 'V', notes='')))
    assert result['verified']
    assert request.await_args_list[1].kwargs['body'] == {'name': 'V', 'notes': ''}


@pytest.mark.parametrize('changes', [{'IsFrozen': True}, {'InstanceLocked': True}, {'IsFrozen': None}, {'InstanceLocked': None}])
def test_frozen_locked_unknown_refuse_write(monkeypatch, changes):
    request = mock(monkeypatch, [{**row(), **changes}])
    result = json.loads(asyncio.run(uoms.update_uom(ID, 'V', notes='changed')))
    assert not result['verified'] and request.await_count == 1


def test_wrong_name_no_change_and_bad_id(monkeypatch):
    request = mock(monkeypatch, [row(), row()])
    assert 'error' in json.loads(asyncio.run(uoms.update_uom(ID, 'wrong', notes='new')))
    result = json.loads(asyncio.run(uoms.update_uom(ID, 'V', description='Voltage')))
    assert result['submission_state'] == 'not_needed'
    assert 'error' in json.loads(asyncio.run(uoms.update_uom('V', 'V', notes='new')))
    assert request.await_count == 2


def test_rename_checks_collision(monkeypatch):
    request = mock(monkeypatch, [row(), {'value': [{**row(), 'Name': 'mV'}]}])
    result = json.loads(asyncio.run(uoms.update_uom(ID, 'V', name='mV')))
    assert result['submission_state'] == 'not_submitted' and request.await_count == 2


def test_update_mismatch_is_not_success(monkeypatch):
    mock(monkeypatch, [row(), '✅ PUT succeeded', row()])
    result = json.loads(asyncio.run(uoms.update_uom(ID, 'V', description='different')))
    assert not result['verified'] and result['field_checks']['description'] is False


def test_pagination_and_escaped_name(monkeypatch):
    request = mock(monkeypatch, [{'value': [row(), row()]}])
    result = json.loads(asyncio.run(uoms.list_uoms("a'b", limit=1)))
    assert not result['complete'] and result['has_more'] and result['next_offset'] == 1
    assert request.await_args.kwargs['params']['$filter'] == "contains(Name,'a''b')"
    assert len(result['rows']) == 1


def test_discoverable_in_reference_group():
    import re
    names = {'list_uoms', 'get_uom', 'create_uom', 'update_uom'}
    for name in names:
        assert get_tool_func(name) is not None
    loaded = json.loads(asyncio.run(load_tool_group('reference', include_writes=True)))
    assert names <= set(loaded['tools'])
    assert classify_tool('create_uom') == 'create' and classify_tool('update_uom') == 'update'
    assert re.search(GROUP_TERMS['reference'], '为电压定义添加计量单位', re.I)
    all_tools = [{'function': {'name': name}} for name in names]
    selected = select_tools(all_tools, [{'role': 'user', 'content': '创建计量单位 mV'}])
    assert names <= {tool['function']['name'] for tool in selected}
    selected = select_tools(all_tools, [{'role': 'user', 'content': '查询计量单位'}])
    assert {tool['function']['name'] for tool in selected} == {'list_uoms', 'get_uom'}
