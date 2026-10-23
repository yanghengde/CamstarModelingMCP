import asyncio
import json
from unittest.mock import AsyncMock

from tools import data_collection as dc, process_context, mcp, get_tool_func
from tools.tool_discovery import get_capabilities, load_tool_group
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools


def run(coro):
    return json.loads(asyncio.run(coro))


def point(kind=3):
    return {'Name': 'Thickness', 'DataType': kind, 'IsRequired': False, 'LowerLimit': 0, 'UpperLimit': 10}


def setup(monkeypatch, points=None):
    obj = {'InstanceID': 'd', 'Name': 'D', 'Revision': '1', 'DataPoints': points if points is not None else [point()]}
    mock = AsyncMock(return_value=json.dumps(obj))
    monkeypatch.setattr(process_context, 'request', mock)
    return obj, mock


def test_numeric_constraints_and_false_required(monkeypatch):
    setup(monkeypatch)
    result = run(dc.get_data_collection_details(instance_id='d'))
    assert result['complete'] and not result['constraints_complete']
    p = result['points'][0]
    assert p['lower_limit'] == 0 and p['required'] is False
    assert p['data_type']['label'] == 'Fixed' and 'UOM' in p['unknown_constraints']


def test_text_boolean_and_unknown_types(monkeypatch):
    setup(monkeypatch, [point(4), point(7), point(999)])
    result = run(dc.get_data_collection_details(instance_id='d'))
    assert not result['complete']
    assert [p['data_type']['label'] for p in result['points']] == ['String', 'Boolean', '未知']
    assert result['points'][0]['allowed_values']['status'] == 'unknown'


def test_pagination_and_missing_points(monkeypatch):
    obj, mock = setup(monkeypatch, [point(), point()])
    assert not run(dc.get_data_collection_details(instance_id='d', limit=1))['complete']
    obj.pop('DataPoints')
    mock.return_value = json.dumps(obj)
    assert not run(dc.get_data_collection_details(instance_id='d'))['complete']


def test_empty_is_distinct_from_missing(monkeypatch):
    setup(monkeypatch, [])
    result = run(dc.get_data_collection_details(instance_id='d'))
    assert result['complete'] and result['points'] == []


def test_invalid_and_ambiguous_input(monkeypatch):
    _, mock = setup(monkeypatch)
    for task in [dc.get_data_collection_details(name='D'), dc.get_data_collection_details(instance_id='d', limit=0),
                 dc.get_data_collection_details(name='D', revision='1', use_ror=True)]:
        assert not run(task)['complete']
    mock.assert_not_called()


def test_expansion_identity_and_error(monkeypatch):
    obj, mock = setup(monkeypatch)
    mock.side_effect = [json.dumps(obj), json.dumps({**obj, 'Revision': '2'})]
    assert 'error' in run(dc.get_data_collection_details(instance_id='d'))
    mock.side_effect = [json.dumps(obj), '{"error":"denied"}']
    assert 'error' in run(dc.get_data_collection_details(instance_id='d'))


def test_groups_and_base_do_not_claim_points_complete(monkeypatch):
    obj, mock = setup(monkeypatch)
    obj['Groups'] = []
    mock.return_value = json.dumps(obj)
    assert run(dc.get_data_collection_details(instance_id='d', kind='point'))['complete']
    assert not run(dc.get_data_collection_details(instance_id='d', kind='base'))['complete']


def test_group_points_limits_and_missing(monkeypatch):
    obj, mock = setup(monkeypatch)
    obj['Groups'] = [{'Name': 'G', 'MinimumSamples': 2, 'Points': [{**point(), 'InnerLowerLimit': 'x+1', 'OuterLowerLimit': '0'}]}]
    mock.return_value = json.dumps(obj)
    result = run(dc.get_data_collection_details(instance_id='d', kind='point'))
    assert result['complete'] and result['groups'][0]['points'][0]['warning_limits']['lower'] == 'x+1'
    assert mock.call_args.kwargs['params']['$expand'] == 'Groups($expand=Points)'
    obj['Groups'][0].pop('Points')
    mock.return_value = json.dumps(obj)
    assert not run(dc.get_data_collection_details(instance_id='d', kind='point'))['complete']


def container_setup(monkeypatch, maps=None):
    info = {'complete': True, 'data': {'Container': {'Name': 'C', 'InstanceId': 'c'}, 'CurrentSpec': {'InstanceID': 's'}}}
    inquiry = AsyncMock(return_value=json.dumps(info))
    monkeypatch.setattr(dc, 'get_container_details', inquiry)
    spec = {'InstanceID': 's', 'Name': 'S', 'Revision': '1', 'TxnMap': maps if maps is not None else []}
    definition = {'InstanceID': 'd', 'Name': 'D', 'Revision': '1'}
    async def request(method, path, **kwargs):
        assert method == 'GET'
        return json.dumps(definition if path.endswith('/d') else spec)
    monkeypatch.setattr(process_context, 'request', request)
    return info, inquiry, spec


def test_container_mapping_filter(monkeypatch):
    container_setup(monkeypatch, [{'TxnName': 'MoveOut', 'Required': True, 'DataCollectionDef': {'InstanceID': 'd'}},
                                 {'TxnName': 'MoveIn', 'Required': False}])
    result = run(dc.get_container_collection_requirements('C', 'c', transaction_name='MoveOut'))
    assert len(result['mappings']) == 1 and result['static_mappings_complete']
    assert result['mappings'][0]['definition']['definition']['Name'] == 'D'
    assert not result['complete'] and not result['dynamic_requirements_verified']


def test_container_identity_missing_map_and_empty(monkeypatch):
    info, inquiry, spec = container_setup(monkeypatch)
    assert run(dc.get_container_collection_requirements('C'))['static_mapping_count'] == 0
    spec.pop('TxnMap')
    assert 'error' in run(dc.get_container_collection_requirements('C'))
    info['data']['Container']['Name'] = 'wrong'
    inquiry.return_value = json.dumps(info)
    assert 'error' in run(dc.get_container_collection_requirements('C'))


def test_container_bounds_and_missing_definition(monkeypatch):
    container_setup(monkeypatch, [{'TxnName': 'MoveOut'}, {'TxnName': 'MoveIn'}])
    result = run(dc.get_container_collection_requirements('C', max_definitions=1))
    assert not result['static_mappings_complete'] and len(result['mappings']) == 1
    assert 'error' in result['mappings'][0]


def test_registry_and_loading():
    names = {'get_data_collection_details', 'get_container_collection_requirements', 'validate_collection_values', 'get_container_collection_history', 'preview_container_data_collection', 'get_collection_submission_plan', 'collect_container_data', 'reconcile_collection_submission'}
    assert names <= {t.name for t in asyncio.run(mcp.list_tools())}
    assert all(get_tool_func(n) and (TOOL_POLICIES[n].effect == 'read' or n == 'collect_container_data') for n in names)
    assert set(run(load_tool_group('collection'))['tools']) == names - {'collect_container_data'}
    assert set(run(load_tool_group('collection', include_writes=True))['tools']) == names
    assert run(get_capabilities(group='collection'))['groups'][0]['group'] == 'collection'
    selected = select_tools([{'function': {'name': n}} for n in TOOL_POLICIES], [{'role': 'user', 'content': '批次需要采集什么数据？'}])
    assert names - {'collect_container_data'} <= {t['function']['name'] for t in selected}
    assert 'collect_container_data' not in {t['function']['name'] for t in selected}
