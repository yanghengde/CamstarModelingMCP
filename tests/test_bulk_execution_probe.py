import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from scripts import probe_bulk_issue_execution as p
from tools import component_issue_bulk as b, component_issue_plans as plans

DATA = Path(__file__).resolve().parents[1]/'docs/t05-fixtures'


@pytest.fixture
def sample(monkeypatch):
    fixture = json.loads((DATA/'CDX-T05-20261025-F.json').read_text(encoding='utf-8'))
    preview = json.loads((DATA/'bulk-derived-verified-F.json').read_text(encoding='utf-8'))
    monkeypatch.setattr(p, 'CAMSTAR_BASE_URL', fixture['environment'])
    monkeypatch.setattr(p, 'CAMSTAR_SHOPFLOOR_BASE_URL', fixture['environment'].removesuffix('/Modeling')+'/Shopfloor')
    return fixture, preview


def test_live_derived_type_has_exactly_one_source_bearing_actual(sample):
    body = p.validate(*sample)
    assert 'issueActualDetails' not in body
    assert body['serviceDetails'][0]['@odata.type'] == '#shopfloor.IssueDetailsBulk'
    assert len(body['serviceDetails'][0]['actuals']) == 1
    b.verify_echo(sample[1]['service_echo'], body)


@pytest.mark.parametrize('change', ['base_actual', 'base_detail', 'source', 'target', 'top_level', 'override',
    'product', 'line', 'unit', 'quantity', 'secondary', 'unverified', 'consumed', 'environment', 'fixture'])
def test_probe_rejects_wrong_type_identity_or_override(sample, change):
    fixture, preview = copy.deepcopy(sample)
    body = preview['request_preview']; line = body['serviceDetails'][0]; actual = line['actuals'][0]
    if change == 'base_actual': actual.pop('@odata.type')
    elif change == 'base_detail': line.pop('@odata.type')
    elif change == 'source': actual['fromContainer']['instanceId'] = '0000000000000001'
    elif change == 'target': body['container']['instanceId'] = '0000000000000001'
    elif change == 'top_level': body['issueActualDetails'] = [{}]
    elif change == 'override': actual['manualOverride'] = True
    elif change == 'product': line['product']['revision'] = '2'
    elif change == 'line': line['bomLineItem']['instanceId'] = '0000000000000001'
    elif change == 'unit': line['uom']['name'] = 'KG'
    elif change == 'quantity': actual['qtyIssued'] = 3
    elif change == 'secondary': actual['qty2Issued'] = 1
    elif change == 'unverified': preview['serialization_verified'] = False
    elif change == 'consumed': preview['quantity_context']['available_observed'] = '1'
    elif change == 'environment': fixture['environment'] = 'different'
    elif change == 'fixture': fixture['names']['source'] = 'production'
    with pytest.raises(ValueError): p.validate(fixture, preview)


@pytest.mark.parametrize('change', ['missing_source', 'wrong_source', 'wrong_type'])
def test_derived_echo_never_accepts_quantity_only_actual(sample, change):
    body = sample[1]['request_preview']; reply = copy.deepcopy(sample[1]['service_echo'])
    actual = reply['data']['ServiceDetails'][0]['Actuals'][0]
    if change == 'missing_source': actual.pop('FromContainer')
    elif change == 'wrong_source': actual['FromContainer']['InstanceId'] = '0000000000000001'
    else: actual['CDOTypeId'] = '6570'
    with pytest.raises(ValueError): b.verify_echo(reply, body)


def environment(tmp_path, sample, monkeypatch):
    path = tmp_path/'F.json'; path.write_text(json.dumps(sample[0]), encoding='utf-8')
    monkeypatch.setattr(p, 'PROBE_JOURNAL', path.with_name('F-bulk-execution-probe.json'))
    monkeypatch.setattr(p, 'preview_bulk_component_issue', AsyncMock(return_value=json.dumps(sample[1])))
    monkeypatch.setattr(p, 'get_component_issue_history', AsyncMock(return_value='{"complete":true,"rows":[]}'))
    monkeypatch.setattr(p, 'get_component_requirement', AsyncMock(return_value='{}'))
    monkeypatch.setattr(p, 'get_container_details', AsyncMock(return_value='{}'))
    monkeypatch.setattr(p, 'get_container_history', AsyncMock(return_value='{}'))
    monkeypatch.setattr(plans, 'PLAN_DB', tmp_path/'plans.sqlite3')
    client = type('Client', (), {})()
    client.post = AsyncMock(return_value=httpx.Response(200, json={'value': 'Completed'}))
    monkeypatch.setattr(p, 'get_client', lambda: client)
    monkeypatch.setattr(p, 'get_headers', lambda: {})
    return path, client.post


def test_default_plans_only_and_success_stays_unknown(tmp_path, sample, monkeypatch):
    path, post = environment(tmp_path, sample, monkeypatch)
    plan = asyncio.run(p.probe(path, tmp_path/'plan.json'))
    assert not plan['request_sent']; post.assert_not_called()
    result = asyncio.run(p.probe(path, tmp_path/'execution.json', True))
    assert result['state'] == 'unknown_evidence_collected' and not result['verified_success']
    post.assert_awaited_once()
    assert post.call_args.kwargs['params']['execute'] == 'true'
    assert post.call_args.kwargs['follow_redirects'] is False
    body = post.call_args.kwargs['json']
    assert body['comments'].startswith('Codex bulk ') and 'issueActualDetails' not in body
    journal = path.with_name('F-bulk-execution-probe.json'); previous = journal.read_bytes()
    with pytest.raises(ValueError, match='never replay'):
        asyncio.run(p.probe(path, tmp_path/'retry.json', True))
    assert journal.read_bytes() == previous and post.call_count == 1
    copied = tmp_path/'copied.json'; copied.write_bytes(path.read_bytes())
    with pytest.raises(ValueError, match='never replay'):
        asyncio.run(p.probe(copied, tmp_path/'copy-retry.json', True))
    assert post.call_count == 1


@pytest.mark.parametrize('error', [TimeoutError('uncertain'), asyncio.CancelledError()])
def test_interruption_is_journaled_before_post_and_never_replayed(tmp_path, sample, monkeypatch, error):
    path, post = environment(tmp_path, sample, monkeypatch)
    async def failed(*args, **kwargs):
        journal = json.loads(path.with_name('F-bulk-execution-probe.json').read_text(encoding='utf-8'))
        assert journal['request_sent'] and journal['state'] == 'unknown'
        raise error
    post.side_effect = failed
    with pytest.raises(type(error)):
        asyncio.run(p.probe(path, tmp_path/'execution.json', True))
    saved = json.loads(path.with_name('F-bulk-execution-probe.json').read_text(encoding='utf-8'))
    assert saved['state'] == 'unknown' and saved['error'] and not saved['retry_allowed']
    with pytest.raises(ValueError): asyncio.run(p.probe(path, tmp_path/'retry.json', True))
    assert post.call_count == 1


def test_business_rejection_is_not_retried_or_claimed_success(tmp_path, sample, monkeypatch):
    path, post = environment(tmp_path, sample, monkeypatch)
    post.return_value = httpx.Response(400, json={'ErrorCode': 'hold'})
    result = asyncio.run(p.probe(path, tmp_path/'rejected.json', True))
    assert result['http_status'] == 400 and not result['verified_success']
    assert result['state'] == 'unknown_evidence_collected' and post.call_count == 1


@pytest.mark.parametrize('identity', ['00041080000000e7', '00041080000000e8'])
def test_source_or_target_unresolved_plan_blocks_experiment(tmp_path, sample, monkeypatch, identity):
    path, post = environment(tmp_path, sample, monkeypatch)
    with plans.db_connect() as db:
        payload = {'snapshot': {'environment': p.CAMSTAR_SHOPFLOOR_BASE_URL,
                               'body': {'container': {'instanceId': identity}}}}
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)', ('unresolved', 'digest', json.dumps(payload), 'unknown', 0))
    with pytest.raises(ValueError, match='unresolved'):
        asyncio.run(p.probe(path, tmp_path/'blocked.json', True))
    post.assert_not_called()
