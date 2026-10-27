import asyncio
import json
from unittest.mock import AsyncMock

from tools import resources, mcp, get_tool_func
from tools import process_context
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools
from tools.tool_discovery import load_tool_group, get_capabilities


def run(coro):
    return json.loads(asyncio.run(coro))


def test_list_page_and_escaped_name(monkeypatch):
    mock = AsyncMock(return_value=json.dumps({'value': [{'Name': 'a'}, {'Name': 'b'}]}))
    monkeypatch.setattr(process_context, 'request', mock)
    data = run(resources.list_resources("O'Brien", limit=1))
    assert not data['complete'] and data['has_more'] and data['next_offset'] == 1
    assert len(data['resources']) == 1
    assert mock.call_args.kwargs['params']['$filter'] == "Name eq 'O''Brien'"
    assert mock.call_args.args[0] == 'GET'


def test_empty_offset_and_invalid_bounds(monkeypatch):
    mock = AsyncMock(return_value='{"value": []}')
    monkeypatch.setattr(process_context, 'request', mock)
    assert run(resources.list_resources())['complete']
    assert not run(resources.list_resources(offset=20))['complete']
    mock.reset_mock()
    assert not run(resources.list_resources(limit=0))['complete']
    mock.assert_not_called()


def test_errors_and_unknown_fields(monkeypatch):
    mock = AsyncMock(return_value='HTTP failure')
    monkeypatch.setattr(process_context, 'request', mock)
    assert not run(resources.list_resources())['complete']
    mock.return_value = json.dumps({'InstanceID': 'r', 'Name': 'R', 'Availability': 99})
    result = run(resources.get_resource_details(instance_id='r'))
    assert result['availability']['label'] == '未知'
    assert 'CurrentSetup' in result['unknown_fields']
    assert not result['complete']


def test_ambiguous_name_and_id_mismatch(monkeypatch):
    mock = AsyncMock(return_value='{"value": [{"InstanceID":"a"},{"InstanceID":"b"}]}')
    monkeypatch.setattr(process_context, 'request', mock)
    assert not run(resources.get_resource_details(name='duplicate'))['complete']
    mock.return_value = '{"InstanceID":"different"}'
    assert not run(resources.get_resource_details(instance_id='r'))['complete']


def fixture_graph(monkeypatch, cyclic=False, missing=False):
    spec = {'InstanceID': 's', 'ResourceGroup': {'InstanceID': 'g'}}
    group = {'InstanceID': 'g', 'Entries': [{'InstanceID': 'r'}, {'InstanceID': 'r'}],
             'Groups': [{'InstanceID': 'h'}]}
    child = {'InstanceID': 'h', 'Entries': [], 'Groups': [{'InstanceID': 'g'}] if cyclic else []}
    if missing:
        child.pop('Entries')
    data = {'Specs/s': spec, 'ResourceGroups/g': group, 'ResourceGroups/h': child,
            'Resources/r': {'InstanceID': 'r', 'Name': 'R'}}
    async def request(method, path, **kwargs):
        assert method == 'GET'
        return json.dumps(data[path.removeprefix('/api/')])
    monkeypatch.setattr(process_context, 'request', request)
    return data


def test_nested_group_dedup(monkeypatch):
    fixture_graph(monkeypatch)
    data = run(resources.get_spec_resource_context(instance_id='s'))
    assert data['complete'] and len(data['groups']) == 2 and len(data['resources']) == 1


def test_cycles_and_bounds(monkeypatch):
    fixture_graph(monkeypatch, cyclic=True)
    assert run(resources.get_spec_resource_context(instance_id='s'))['cycles'] == [['g', 'h', 'g']]
    assert not run(resources.get_spec_resource_context(instance_id='s', max_depth=1))['complete']
    assert not run(resources.get_spec_resource_context(instance_id='s', max_groups=1))['complete']


def test_missing_and_empty_group(monkeypatch):
    data = fixture_graph(monkeypatch, missing=True)
    assert not run(resources.get_spec_resource_context(instance_id='s'))['complete']
    data['ResourceGroups/g'].update(Entries=[], Groups=[])
    result = run(resources.get_spec_resource_context(instance_id='s'))
    assert result['complete'] and not result['resources']
    data['Specs/s'].pop('ResourceGroup')
    assert not run(resources.get_spec_resource_context(instance_id='s'))['complete']


def test_spec_requires_revision_without_id(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(process_context, 'request', mock)
    assert not run(resources.get_spec_resource_context(name='S'))['complete']
    mock.assert_not_called()


def test_missing_reference_preserves_other_candidates(monkeypatch):
    data = fixture_graph(monkeypatch)
    data['ResourceGroups/g']['Entries'].append({})
    result = run(resources.get_spec_resource_context(instance_id='s'))
    assert not result['complete'] and len(result['resources']) == 1


def test_group_next_page_and_resource_budget(monkeypatch):
    data = fixture_graph(monkeypatch)
    data['ResourceGroups/g']['@odata.nextLink'] = 'next'
    result = run(resources.get_spec_resource_context(instance_id='s'))
    assert not result['complete']
    data['ResourceGroups/g'].pop('@odata.nextLink')
    data['ResourceGroups/g']['Entries'] = [{'InstanceID': 'r'}, {'InstanceID': 'r2'}]
    result = run(resources.get_spec_resource_context(instance_id='s', max_resources=1))
    assert not result['complete'] and len(result['resources']) == 1


def test_partial_maintenance_failure_keeps_resource(monkeypatch):
    mock = AsyncMock(side_effect=[json.dumps({'InstanceID': 'r'}), '{"error":"expand denied"}'])
    monkeypatch.setattr(process_context, 'request', mock)
    result = run(resources.get_resource_details(instance_id='r', include_maintenance=True))
    assert result['data']['InstanceID'] == 'r' and not result['complete']
    assert mock.call_args.kwargs['params'] == {'$expand': 'PMStatus'}


def test_registry_discovery_and_routing():
    names = {'list_resources', 'get_resource_details', 'get_spec_resource_context'}
    registered = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names <= registered
    assert all(get_tool_func(n) and TOOL_POLICIES[n].effect == 'read' for n in names)
    assert set(run(load_tool_group('resource'))['tools']) == names
    assert run(get_capabilities(group='resource'))['groups'][0]['group'] == 'resource'
    tools = [{'function': {'name': n}} for n in TOOL_POLICIES]
    picked = select_tools(tools, [{'role': 'user', 'content': '这个工序配置了哪些设备资源？'}])
    assert names <= {t['function']['name'] for t in picked}
