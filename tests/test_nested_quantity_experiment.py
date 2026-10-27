import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import httpx
import pytest
from scripts import verify_nested_quantity_issue as p

DATA = Path(__file__).resolve().parents[1]/'docs/t05-fixtures'


@pytest.fixture
def sample(monkeypatch):
    fixture = json.loads((DATA/'CDX-T05-20261024-C.json').read_text(encoding='utf-8'))
    monkeypatch.setattr(p, 'CAMSTAR_BASE_URL', fixture['environment'])
    status = {'complete': True, 'data': {'ContainerStatusDetails': fixture['attempts'][-1]['verified_object']}}
    echo = json.loads((DATA/'nested-actuals-preview-C-selected.json').read_text(encoding='utf-8'))
    return fixture, status, echo


def setup(tmp_path, sample, monkeypatch):
    fixture, status, echo = sample
    path = tmp_path/'fixture.json'; path.write_text(json.dumps(fixture), encoding='utf-8')
    monkeypatch.setattr(p, 'get_container_details', AsyncMock(return_value=json.dumps(status)))
    monkeypatch.setattr(p, 'get_component_requirement', AsyncMock(return_value=json.dumps(echo['requirement'])))
    monkeypatch.setattr(p, 'get_component_issue_history', AsyncMock(return_value='{"complete":true,"rows":[]}'))
    monkeypatch.setattr(p, 'get_container_history', AsyncMock(return_value='{}'))
    monkeypatch.setattr(p, 'close_client', AsyncMock())
    class Client: pass
    client = Client()
    client.post = AsyncMock(side_effect=[httpx.Response(200, json=echo['response']), TimeoutError('unknown')])
    monkeypatch.setattr(p, 'get_client', lambda: client)
    return path, client.post


def test_plan_only_previews(tmp_path, sample, monkeypatch):
    path, post = setup(tmp_path, sample, monkeypatch)
    result = asyncio.run(p.verify(path, tmp_path/'plan.json'))
    assert result['serialization_verified'] and not result['request_sent']
    assert post.call_count == 1 and post.call_args.kwargs['params']['execute'] == 'false'
    assert not path.with_name('fixture-execution-probe.json').exists()


def test_different_deployment_never_previews_or_executes(tmp_path, sample, monkeypatch):
    path, post = setup(tmp_path, sample, monkeypatch)
    monkeypatch.setattr(p, 'CAMSTAR_BASE_URL', 'https://different.invalid/Modeling')
    result = asyncio.run(p.verify(path, tmp_path/'different.json', True))
    assert not result['request_sent'] and not result['serialization_verified']
    assert 'error' in result
    post.assert_not_called()


def test_timeout_journal_never_replays(tmp_path, sample, monkeypatch):
    path, post = setup(tmp_path, sample, monkeypatch)
    result = asyncio.run(p.verify(path, tmp_path/'first.json', True))
    journal = path.with_name('fixture-execution-probe.json'); before = journal.read_bytes()
    assert result['state'] == 'unknown' and result['request_sent'] and not result['retry_allowed']
    assert post.call_count == 2 and post.call_args.kwargs['params']['execute'] == 'true'
    replay = asyncio.run(p.verify(path, tmp_path/'second.json', True))
    assert not replay['request_sent'] and journal.read_bytes() == before and post.call_count == 2


def test_changed_context_after_preview_does_not_execute(tmp_path, sample, monkeypatch):
    path, post = setup(tmp_path, sample, monkeypatch)
    changed = copy.deepcopy(sample[1]); changed['data']['ContainerStatusDetails']['Qty'] = 2
    monkeypatch.setattr(p, 'get_container_details', AsyncMock(side_effect=[json.dumps(sample[1]), json.dumps(changed)]))
    result = asyncio.run(p.verify(path, tmp_path/'changed.json', True))
    assert not result['request_sent'] and 'error' in result
    assert post.call_count == 1


def test_business_failure_still_keeps_journal(tmp_path, sample, monkeypatch):
    path, post = setup(tmp_path, sample, monkeypatch)
    post.side_effect = [httpx.Response(200, json=sample[2]['response']), httpx.Response(400, json={'ErrorCode':'13500677'})]
    result = asyncio.run(p.verify(path, tmp_path/'failed.json', True))
    assert not result['service_accepted'] and not result['transaction_verified']
    assert result['http_status'] == 400 and path.with_name('fixture-execution-probe.json').exists()
    assert post.call_count == 2
