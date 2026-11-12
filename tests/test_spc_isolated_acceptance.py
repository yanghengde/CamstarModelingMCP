import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from scripts import spc_isolated_acceptance as runner


@pytest.fixture
def manifest(tmp_path):
    prefix = 'CDX-C6-TEST'
    value = dict(prefix=prefix, created=runner.time.time(), environment=runner.plans.environment(),
                 models=runner.models(prefix), root_objects=9, container_name=prefix+'-SN',
                 values=runner.VALUES, baseline_count=3, annotation=runner.NOTE)
    path = tmp_path/'manifest.json'
    path.write_text(json.dumps({'digest': runner.plans.digest(value), 'payload': value}), encoding='utf8')
    return path, value


@pytest.mark.parametrize('field,value', [('root_objects', 10), ('values', [9, 10, 11, 99]),
    ('prefix', 'PRODUCTION'), ('annotation', 'different operation'), ('container_name', 'PRODUCTION-SN'),
    ('baseline_count', 1)])
def test_manifest_rejects_scope_changes_even_with_recomputed_digest(manifest, field, value):
    path, original = manifest
    changed = copy.deepcopy(original); changed[field] = value
    path.write_text(json.dumps({'digest': runner.plans.digest(changed), 'payload': changed}), encoding='utf8')
    with pytest.raises(ValueError): runner.read(path)


def test_attempt_is_durable_before_network_and_native_error_never_replays(manifest, monkeypatch):
    path, _ = manifest
    async def send(*args, **kwargs):
        assert runner.saved(path, 'test-attempt_attempt')['path'] == '/api/Start'
        return type('Response', (), dict(status_code=400, text='native refusal'))()
    client = type('Client', (), {'request': AsyncMock(side_effect=send)})()
    monkeypatch.setattr(runner, 'get_client', lambda: client)
    for _ in range(2):
        with pytest.raises(ValueError):
            asyncio.run(runner.send(path, 'test-attempt', 'POST', '/api/Start', {}, modeling=False))
    client.request.assert_awaited_once()
    assert runner.saved(path, 'test-attempt_response')['http_status'] == 400


def test_actual_persisted_group_blocks_collection_when_wire_reference_is_null(manifest, monkeypatch):
    path, value = manifest
    cid, did, gid = ('0000000000000001', '0000000000000002', '0000000000000003')
    runner.log(path, 'container', {'ContainerId': cid})
    runner.log(path, 'verified-UserDataCollectionDefs', {'InstanceID': did})
    class Context:
        def __init__(self, *args): pass
        async def get(self, entity, key, params):
            assert params == {'$expand': 'SPCChartDefGroup'}
            return {'SPCChartDefGroup': None, 'SPCChartDefEntries': []}
    monkeypatch.setattr(runner, 'ReadContext', Context)
    monkeypatch.setattr(runner.q, 'sql_rows', AsyncMock(return_value=[{'DataCollectionDefId': did, 'SPCChartDefGroupId': gid}]))
    preview = AsyncMock(); monkeypatch.setattr(runner.collection, 'preview_container_data_collection', preview)
    with pytest.raises(ValueError, match='inline engine'):
        asyncio.run(runner.collect(path))
    preview.assert_not_awaited()
    assert len(runner.records(path)) == 2


def test_expired_manifest_does_not_start_fixture_writes(manifest, monkeypatch):
    path, value = manifest
    value['created'] -= 3601
    path.write_text(json.dumps({'digest': runner.plans.digest(value), 'payload': value}), encoding='utf8')
    send = AsyncMock(); monkeypatch.setattr(runner, 'send', send)
    with pytest.raises(ValueError, match='expired'): asyncio.run(runner.apply(path))
    send.assert_not_awaited()


def test_unverified_previous_collection_cannot_advance_to_next_sample(manifest, monkeypatch):
    path, _ = manifest
    runner.log(path, 'container', {'ContainerId': '0000000000000001'})
    runner.log(path, 'verified-UserDataCollectionDefs', {'InstanceID': '0000000000000002'})
    runner.log(path, 'collect-0-plan', {'plan_id': 'attempted'})
    runner.log(path, 'collect-0-verified', {'verified_success': False})
    preview = AsyncMock(); monkeypatch.setattr(runner.collection, 'preview_container_data_collection', preview)
    with pytest.raises(ValueError, match='never advance'): asyncio.run(runner.collect(path))
    preview.assert_not_awaited()


def test_annotation_prepared_once_cannot_be_replayed_as_another_plan(manifest, monkeypatch):
    path, value = manifest
    runner.log(path, 'binding-after', {'InstanceID': '0000000000000001'})
    runner.log(path, 'annotation-plan', {'state': 'ready', 'plan_id': 'existing'})
    prepare = AsyncMock(); monkeypatch.setattr(runner.annotations, 'prepare_spc_annotation', prepare)
    with pytest.raises(ValueError, match='never replay'): asyncio.run(runner.annotate(path))
    prepare.assert_not_awaited()
