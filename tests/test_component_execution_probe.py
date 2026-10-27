import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import httpx
from scripts import probe_component_issue_execution as p

DATA = Path(__file__).resolve().parents[1] / 'docs/t05-fixtures'


@pytest.fixture
def sample(monkeypatch):
    captured = [json.loads((DATA / name).read_text(encoding='utf-8')) for name in (
        'CDX-T05-20261024-B.json', 'status-before-execution-probe.json', 'requirement-tool-validation.json')]
    # Replay the archived environment, independently of the developer's .env.
    monkeypatch.setattr(p, 'CAMSTAR_BASE_URL', captured[0]['environment'])
    return captured


def test_fixed_fixture_and_no_override_fields(sample):
    body = p.validate_fixture(*sample)
    assert body['preventOverIssue'] is True
    assert body['issueActualDetails'][0]['qtyIssued'] == 1
    assert 'validSequence' not in body['serviceDetails'][0]
    assert 'skip' not in json.dumps(body).lower()


@pytest.mark.parametrize('change', ['name', 'environment', 'inactive', 'product', 'already_issued', 'wrong_id'])
def test_fixture_mismatch_never_eligible(sample, change):
    fixture, status, requirement = sample
    row = status['data']['ContainerStatusDetails']
    if change == 'name': fixture['names']['container'] = 'production'
    elif change == 'environment': fixture['environment'] = 'other'
    elif change == 'inactive': row['Status'] = 2
    elif change == 'product': row['ProductName'] = 'production'
    elif change == 'already_issued': requirement['data']['IssueDetails']['QtyIssued'] = 1
    elif change == 'wrong_id': row['Container']['InstanceId'] = '0004108000000001'
    with pytest.raises(ValueError): p.validate_fixture(*sample)


def setup_probe(tmp_path, sample, monkeypatch):
    fixture = tmp_path / 'fixture.json'
    fixture.write_text(json.dumps(sample[0]), encoding='utf-8')
    monkeypatch.setattr(p, 'get_container_details', AsyncMock(return_value=json.dumps(sample[1])))
    monkeypatch.setattr(p, 'get_component_requirement', AsyncMock(return_value=json.dumps(sample[2])))
    monkeypatch.setattr(p, 'get_component_issue_history', AsyncMock(return_value='{"complete":true,"rows":[]}'))
    monkeypatch.setattr(p, 'get_container_history', AsyncMock(return_value='{}'))
    monkeypatch.setattr(p, 'close_client', AsyncMock())
    post = AsyncMock(side_effect=TimeoutError('ambiguous'))
    class Client:
        pass
    client = Client(); client.post = post
    monkeypatch.setattr(p, 'get_client', lambda: client)
    return fixture, post


def test_plan_never_posts_or_reserves(tmp_path, sample, monkeypatch):
    fixture, post = setup_probe(tmp_path, sample, monkeypatch)
    result = asyncio.run(p.probe(fixture, tmp_path / 'plan.json'))
    assert result['state'] == 'prepared' and not result['request_sent']
    assert not fixture.with_name('fixture-execution-probe.json').exists()
    post.assert_not_called()


def test_timeout_persists_unknown_and_replay_cannot_overwrite(tmp_path, sample, monkeypatch):
    fixture, post = setup_probe(tmp_path, sample, monkeypatch)
    result = asyncio.run(p.probe(fixture, tmp_path / 'apply.json', True))
    journal = fixture.with_name('fixture-execution-probe.json')
    prior = journal.read_bytes()
    assert result['state'] == 'unknown' and result['request_sent']
    assert post.call_count == 1
    assert post.call_args.kwargs['params']['execute'] == 'true'
    result = asyncio.run(p.probe(fixture, tmp_path / 'replay.json', True))
    assert not result['request_sent'] and 'already exists' in result['error']
    assert journal.read_bytes() == prior and post.call_count == 1


def test_protected_output_never_overwrites_fixture(tmp_path, sample, monkeypatch):
    fixture, post = setup_probe(tmp_path, sample, monkeypatch)
    before = fixture.read_bytes()
    with pytest.raises(ValueError): asyncio.run(p.probe(fixture, fixture, True))
    assert fixture.read_bytes() == before
    post.assert_not_called()


def test_only_exact_pre_action_uri_rejection_can_be_corrected():
    previous = json.loads((DATA / 'execution-probe-result.json').read_text(encoding='utf-8'))
    assert p.rejected_select_before_action(previous)
    previous['select_correction'] = True
    assert not p.rejected_select_before_action(previous)
    previous.pop('select_correction')
    previous['response']['error']['innererror']['stacktrace'] = 'Execute business service'
    assert not p.rejected_select_before_action(previous)


def test_business_or_ambiguous_failure_cannot_be_corrected(tmp_path, sample, monkeypatch):
    fixture, post = setup_probe(tmp_path, sample, monkeypatch)
    asyncio.run(p.probe(fixture, tmp_path / 'first.json', True))
    before = fixture.with_name('fixture-execution-probe.json').read_bytes()
    result = asyncio.run(p.probe(fixture, tmp_path / 'second.json', True, True))
    assert not result['request_sent'] and 'already exists' in result['error']
    assert fixture.with_name('fixture-execution-probe.json').read_bytes() == before
    assert post.call_count == 1


def test_business_rejection_collects_evidence_without_retry(tmp_path, sample, monkeypatch):
    fixture, post = setup_probe(tmp_path, sample, monkeypatch)
    post.side_effect = None
    post.return_value = httpx.Response(400, json={'ErrorCode': '13500677', 'Message': 'Difference reason required'})
    result = asyncio.run(p.probe(fixture, tmp_path / 'rejected.json', True))
    assert result['http_status'] == 400 and not result['service_accepted']
    assert not result['transaction_verified'] and not result['retry_allowed']
    assert 'requirement_after' in result and post.call_count == 1
    asyncio.run(p.probe(fixture, tmp_path / 'again.json', True, True))
    assert post.call_count == 1
