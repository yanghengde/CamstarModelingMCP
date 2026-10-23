import asyncio
import json
from unittest.mock import AsyncMock
from tools import production_readiness as pr, mcp, get_tool_func
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools


def run(**kwargs):
    return json.loads(asyncio.run(pr.check_mfgorder_readiness('O', **kwargs)))


def setup(monkeypatch, count=1):
    async def resolve(self, entity, ref, **kwargs):
        return {'InstanceID': 'o', 'Name': 'O', 'Product': {'InstanceID': 'p'}} if entity == 'MfgOrders' else {'InstanceID': 'p', 'Name': 'P', 'Revision': '1'}
    monkeypatch.setattr(pr.ReadContext, 'resolve', resolve)
    async def get(self, entity, key, params):
        return {'InstanceID': key, 'TxnMap': []}
    monkeypatch.setattr(pr.ReadContext, 'get', get)
    process = {'complete': True, 'workflows': {'w': {'steps': [{'name': 'one', 'is_first_step': True, 'step_type': 1, 'spec': {'Name': 'S'}, 'paths': []}]}},
               'specs': {str(i): {} for i in range(count)}}
    monkeypatch.setattr(pr, 'get_product_process', AsyncMock(return_value=json.dumps(process)))
    material = {'standard_bom': {'complete': True, 'data': {'MaterialList': [{'Name': 'x'}]}},
                'order_materials': {'complete': True, 'data': {'MaterialList': [{'Name': 'y'}]}}}
    monkeypatch.setattr(pr, 'get_product_material_requirements', AsyncMock(return_value=json.dumps(material)))
    monkeypatch.setattr(pr, 'get_spec_resource_context', AsyncMock(return_value=json.dumps({'complete': True, 'resources': [{'Name': 'R'}]})))


def test_complete_static_still_not_permission(monkeypatch):
    setup(monkeypatch)
    data = run()
    assert data['ready_to_start'] is None and not data['complete']
    assert data['status_counts']['通过'] == 5
    assert data['status_counts']['未知'] == 2
    assert all(c['source'] and c['next_action'] for c in data['checks'])


def test_partial_failure_preserves_other_results(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(pr, 'get_product_material_requirements', AsyncMock(return_value='{"error":"offline"}'))
    data = run()
    assert next(c for c in data['checks'] if c['area'] == '工序资源')['status'] == '通过'
    assert next(c for c in data['checks'] if c['area'] == '标准BOM')['status'] == '未知'


def test_empty_resources_and_bounded_specs(monkeypatch):
    setup(monkeypatch, 3)
    mock = AsyncMock(return_value='{"complete":true,"resources":[]}')
    monkeypatch.setattr(pr, 'get_spec_resource_context', mock)
    data = run(max_specs=1)
    assert data['unexamined_specs'] == ['1', '2'] and mock.await_count == 1
    assert next(c for c in data['checks'] if c['area'] == '工序资源')['status'] == '未知'


def test_resolution_failure_and_invalid_limit(monkeypatch):
    mock = AsyncMock(side_effect=ValueError('ambiguous'))
    monkeypatch.setattr(pr.ReadContext, 'resolve', mock)
    assert run()['checks'][0]['status'] == '未知'
    mock.reset_mock()
    assert 'error' in run(max_specs=11)
    mock.assert_not_called()


def test_repeat_read_is_stable(monkeypatch):
    setup(monkeypatch)
    assert run() == run()


def test_registry_and_route():
    name = 'check_mfgorder_readiness'
    assert name in {t.name for t in asyncio.run(mcp.list_tools())}
    assert get_tool_func(name) and TOOL_POLICIES[name].effect == 'read'
    selected = select_tools([{'function': {'name': n}} for n in TOOL_POLICIES], [{'role': 'user', 'content': '检查工单 O 开工准备'}])
    assert name in {t['function']['name'] for t in selected}


def test_collection_points_deduplicated_and_bounded(monkeypatch):
    rows = [{'TxnName': 'MoveOut', 'DataCollectionDef': {'InstanceID': 'd', 'CDOTypeName': 'UserDataCollectionDef'}}] * 3
    mock = AsyncMock(return_value={'InstanceID': 's', 'TxnMap': rows})
    monkeypatch.setattr(pr.ReadContext, 'get', mock)
    detail = AsyncMock(return_value={'complete': True, 'constraints_complete': False, 'definition': {'Name': 'D'},
                                    'points': [{'name': 'Thickness', 'lower_limit': 0, 'required': False}]})
    monkeypatch.setattr(pr, 'definition', detail)
    data = asyncio.run(pr.collection_summary('s', 'base', 2, 10))
    assert not data['read_complete'] and data['unexamined_mapping_count'] == 1
    assert detail.await_count == 1 and detail.call_args.args[2] == 'user'
    assert data['mappings'][0]['collection']['points'][0]['lower_limit'] == 0


def test_collection_unknown_type_and_partial_failure(monkeypatch):
    rows = [{'DataCollectionDef': {'InstanceID': 'd'}}, {'DataCollectionDef': None}]
    monkeypatch.setattr(pr.ReadContext, 'get', AsyncMock(return_value={'InstanceID': 's', 'TxnMap': rows}))
    detail = AsyncMock(side_effect=[{'complete': False, 'warnings': ['unknown subtype']}, ValueError('missing ref')])
    monkeypatch.setattr(pr, 'definition', detail)
    data = asyncio.run(pr.collection_summary('s', 'base', 5, 20))
    assert not data['read_complete'] and detail.call_args_list[0].args[2] == 'base'
    assert 'error' in data['mappings'][1] and 'collection' in data['mappings'][0]


def test_collection_malformed_mapping(monkeypatch):
    monkeypatch.setattr(pr.ReadContext, 'get', AsyncMock(return_value={'InstanceID': 's', 'TxnMap': [None]}))
    import pytest
    with pytest.raises(ValueError):
        asyncio.run(pr.collection_summary('s', 'base', 5, 20))
