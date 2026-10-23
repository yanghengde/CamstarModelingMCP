import asyncio
import json
from unittest.mock import AsyncMock

from tools import boms, process_context, mcp, get_tool_func
from tools.tool_discovery import get_capabilities, load_tool_group
from agent.tool_routing import select_tools
from core.tool_catalog import TOOL_POLICIES


def run(task):
    return json.loads(asyncio.run(task))


def graph(monkeypatch, left=None, right=None):
    data = {
        'BOMs/a': {'InstanceID': 'a', 'Name': 'B', 'Revision': 'A', 'MaterialList': left if left is not None else []},
        'BOMs/b': {'InstanceID': 'b', 'Name': 'B', 'Revision': 'B', 'MaterialList': right if right is not None else []},
        'Products/p': {'InstanceID': 'p', 'Name': 'P', 'Revision': '1', 'BOM': {'InstanceID': 'a'}},
        'MfgOrders/o': {'InstanceID': 'o', 'Name': 'O', 'Product': {'InstanceID': 'p'}, 'MaterialList': []},
    }
    async def request(method, path, params=None, **kwargs):
        assert method == 'GET'
        key = path.removeprefix('/api/')
        if key == 'BOMs':
            id_ = 'a' if "Revision eq 'A'" in params['$filter'] else 'b'
            return json.dumps({'value': [data['BOMs/' + id_]]})
        if key == 'MfgOrders':
            return json.dumps({'value': [data['MfgOrders/o']]})
        return json.dumps(data[key])
    monkeypatch.setattr(process_context, 'request', request)
    return data


def test_details_empty_and_pagination(monkeypatch):
    data = graph(monkeypatch)
    assert run(boms.get_bom_details(instance_id='a'))['complete']
    data['BOMs/a']['MaterialList'] = [{'Name': 'x'}, {'Name': 'y'}]
    result = run(boms.get_bom_details(instance_id='a', limit=1))
    assert not result['complete'] and result['pages']['MaterialList']['next_offset'] == 1
    assert len(result['data']['MaterialList']) == 1


def test_missing_list_and_nextlink(monkeypatch):
    data = graph(monkeypatch)
    data['BOMs/a'].pop('MaterialList')
    assert not run(boms.get_bom_details(instance_id='a'))['complete']
    data['BOMs/a'].update(MaterialList=[], **{'MaterialList@odata.nextLink': 'next'})
    assert not run(boms.get_bom_details(instance_id='a'))['complete']


def test_validation_before_network(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(process_context, 'request', mock)
    for task in [boms.get_bom_details(name='B'), boms.get_bom_details(name='B', revision='A', use_ror=True),
                 boms.get_bom_details(instance_id='a', limit=0),
                 boms.compare_bom_versions('B', '', 'B', 'B')]:
        assert not run(task)['complete']
    mock.assert_not_called()


def test_explicit_ror(monkeypatch):
    obj = {'InstanceID': 'a', 'Name': 'B', 'Revision': 'A', 'IsRevOfRcd': True, 'MaterialList': []}
    mock = AsyncMock(side_effect=[json.dumps({'value': [obj]}), json.dumps(obj), json.dumps(obj)])
    monkeypatch.setattr(process_context, 'request', mock)
    assert run(boms.get_bom_details(name='B', use_ror=True))['complete']
    assert 'IsRevOfRcd eq true' in mock.call_args_list[0].kwargs['params']['$filter']


def test_expand_failure_and_identity_mismatch(monkeypatch):
    obj = {'InstanceID': 'a', 'Name': 'B', 'Revision': 'A'}
    mock = AsyncMock(side_effect=[json.dumps(obj), '{"error":"denied"}'])
    monkeypatch.setattr(process_context, 'request', mock)
    assert not run(boms.get_bom_details(instance_id='a'))['complete']
    mock.side_effect = [json.dumps(obj), json.dumps({**obj, 'InstanceID': 'wrong', 'MaterialList': []})]
    assert not run(boms.get_bom_details(instance_id='a'))['complete']


def test_order_separate_and_product_match(monkeypatch):
    data = graph(monkeypatch, [{'Name': 'x', 'QtyRequired': 1}])
    result = run(boms.get_product_material_requirements(instance_id='p', mfgorder_name='O'))
    assert result['complete']
    assert result['standard_bom']['data']['MaterialList'][0]['QtyRequired'] == 1
    assert result['order_materials']['data']['MaterialList'] == []
    data['Products/q'] = {'InstanceID': 'q', 'Name': 'P', 'Revision': '2'}
    data['MfgOrders/o']['Product'] = {'InstanceID': 'q'}
    result = run(boms.get_product_material_requirements(instance_id='p', mfgorder_name='O'))
    assert not result['complete'] and result['standard_bom']['complete']
    assert 'error' in result['order_materials']


def test_missing_product_bom_preserves_order(monkeypatch):
    data = graph(monkeypatch)
    data['Products/p'].pop('BOM')
    result = run(boms.get_product_material_requirements(instance_id='p', mfgorder_name='O'))
    assert not result['complete'] and result['order_materials']['complete']


def test_compare_order_independent_and_units(monkeypatch):
    graph(monkeypatch, [{'Name': 'x', 'QtyRequired': 1, 'UOM': {'Name': 'kg'}, 'InstanceID': 'old'},
                        {'Name': 'y', 'QtyRequired': 2}],
          [{'Name': 'y', 'QtyRequired': 2},
           {'Name': 'x', 'QtyRequired': 1, 'UOM': {'Name': 'g'}, 'InstanceID': 'new'}])
    result = run(boms.compare_bom_versions('B', 'A', 'B', 'B'))
    assert result['complete'] and not result['added'] and not result['removed']
    assert result['changed'][0]['fields'] == ['UOM']


def test_compare_added_removed_and_duplicate_material(monkeypatch):
    graph(monkeypatch, [{'Name': 'x', 'Product': {'Name': 'same'}}, {'Name': 'y', 'Product': {'Name': 'same'}}],
          [{'Name': 'y', 'Product': {'Name': 'same'}}, {'Name': 'z', 'Product': {'Name': 'same'}}])
    result = run(boms.compare_bom_versions('B', 'A', 'B', 'B'))
    assert result['added'][0]['Name'] == 'z' and result['removed'][0]['Name'] == 'x'


def test_compare_rejects_ambiguous_or_partial(monkeypatch):
    data = graph(monkeypatch, [{'Name': 'x'}, {'Name': 'x'}])
    assert not run(boms.compare_bom_versions('B', 'A', 'B', 'B'))['complete']
    for rows in [[{}], [{'Name': str(i)} for i in range(201)], ['bad']]:
        data['BOMs/a']['MaterialList'] = rows
        result = run(boms.compare_bom_versions('B', 'A', 'B', 'B'))
        assert not result['complete'] and 'added' not in result


def test_registry_loading_and_routing():
    names = {'get_bom_details', 'get_product_material_requirements', 'compare_bom_versions'}
    assert names <= {t.name for t in asyncio.run(mcp.list_tools())}
    assert all(get_tool_func(n) and TOOL_POLICIES[n].effect == 'read' for n in names)
    assert set(run(load_tool_group('material'))['tools']) == names
    assert run(get_capabilities(group='material'))['groups'][0]['group'] == 'material'
    tools = [{'function': {'name': n}} for n in TOOL_POLICIES]
    chosen = select_tools(tools, [{'role': 'user', 'content': '比较 BOM 两个版本用料差异'}])
    assert names <= {t['function']['name'] for t in chosen}
