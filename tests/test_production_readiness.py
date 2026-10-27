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


def test_optional_surrounding_checks_default_off(monkeypatch):
    setup(monkeypatch)
    tasks = AsyncMock(); documents = AsyncMock()
    monkeypatch.setattr(pr, 'get_mfgorder_tasks', tasks)
    monkeypatch.setattr(pr, 'get_spec_documents', documents)
    run()
    tasks.assert_not_called(); documents.assert_not_called()


def test_task_summary_preserves_page_and_identity_limits(monkeypatch):
    setup(monkeypatch)
    tasks = AsyncMock(return_value=json.dumps({'complete': False, 'inventory_complete': True,
        'order': {'InstanceID': 'o'}, 'page': {'has_more': True},
        'pending_task_refs': [{'task': {'Name': 'T'}}], 'pending_scope': 'current_page_candidates_only_not_execution_permission'}))
    monkeypatch.setattr(pr, 'get_mfgorder_tasks', tasks)
    result = run(include_tasks=True)
    check = next(c for c in result['checks'] if c['area'] == '工单任务配置')
    assert check['status'] == '未知' and check['detail']['page']['has_more']
    assert not check['detail']['execution_eligibility_verified']
    assert result['ready_to_start'] is None and not result['complete']
    assert tasks.call_args.kwargs == {'offset': 0, 'limit': 20}
    tasks.return_value = '{"complete":true,"order":{"InstanceID":"other"}}'
    assert next(c for c in run(include_tasks=True)['checks'] if c['area'] == '工单任务配置')['status'] == '未知'


def test_document_requirement_must_be_explicit(monkeypatch):
    setup(monkeypatch)
    async def documents(identifier, documents_required, **kwargs):
        return json.dumps({'complete': True, 'spec': {'InstanceID': identifier},
                           'documents_required': documents_required, 'association_status': 'not_associated',
                           'requirement_status': {None: 'requirement_unknown', True: 'missing_required_association',
                                                  False: 'not_required_by_caller'}[documents_required]})
    document_mock = AsyncMock(side_effect=documents)
    monkeypatch.setattr(pr, 'get_spec_documents', document_mock)
    for requirement, expected in [(None, '未知'), (True, '缺失'), (False, '不适用')]:
        result = run(include_documents=True, documents_required=requirement)
        check = next(c for c in result['checks'] if c['area'] == '工序文档配置')
        assert check['status'] == expected
        assert not result['complete'] and result['ready_to_start'] is None
        assert document_mock.call_args.kwargs['documents_required'] is requirement


def test_partial_document_read_never_claims_required_missing(monkeypatch):
    setup(monkeypatch, 3)
    documents = AsyncMock(return_value=json.dumps({'complete': False, 'documents_required': True,
        'requirement_status': 'missing_required_association', 'error': 'permission denied'}))
    monkeypatch.setattr(pr, 'get_spec_documents', documents)
    result = run(include_documents=True, documents_required=True, max_specs=1)
    assert next(c for c in result['checks'] if c['area'] == '工序文档配置')['status'] == '未知'
    assert documents.await_count == 1 and result['unexamined_specs'] == ['1', '2']


def test_optional_invalid_flags_never_read(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(pr.ReadContext, 'resolve', mock)
    for kw in [{'include_documents': 'true'}, {'include_tasks': 1}, {'documents_required': True},
               {'include_documents': True, 'documents_required': 1}]:
        assert 'error' in run(**kw)
    mock.assert_not_called()


def test_task_failure_does_not_remove_other_static_evidence(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(pr, 'get_mfgorder_tasks', AsyncMock(return_value='{"error":"offline"}'))
    result = run(include_tasks=True)
    assert next(c for c in result['checks'] if c['area'] == '工单任务配置')['status'] == '未知'
    assert next(c for c in result['checks'] if c['area'] == '工序资源')['status'] == '通过'
