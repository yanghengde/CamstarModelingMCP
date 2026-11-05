import json

import pytest

from core import perf_logger


def write_log(path, prompt, index, mode='a'):
    with path.open(mode, encoding='utf-8') as stream:
        stream.write(json.dumps({'timestamp': f'2026-11-05 12:00:{index:02d}.000',
            'session_id': 'session', 'username': 'operator', 'duration_ms': 100,
            'action': 'llm', 'details': {'prompt': prompt}}) + '\n')


def test_pages_are_complete_groups_and_snapshot_survives_new_records(tmp_path, monkeypatch):
    path = tmp_path / 'performance.jsonl'
    monkeypatch.setattr(perf_logger, 'PERF_LOG_FILE', str(path))
    for index in range(45):
        write_log(path, f'prompt {index}', index)
    # More than the old 1000-row limit belongs to one complete trace.
    for _ in range(1001):
        write_log(path, 'prompt 44', 44)
    with path.open('a', encoding='utf-8') as stream:
        stream.write('broken json\n')
    first = perf_logger.get_perf_log_groups()
    assert len(first['groups']) == 20 and first['has_more']
    assert first['groups'][0]['prompt'] == 'prompt 44'
    assert len(first['groups'][0]['steps']) == 1002
    assert first['groups'][0]['totalTimeMs'] == 100200
    write_log(path, 'new arriving prompt', 59)
    second = perf_logger.get_perf_log_groups(cursor=first['next_cursor'])
    third = perf_logger.get_perf_log_groups(cursor=second['next_cursor'])
    assert len(second['groups']) == 20 and len(third['groups']) == 5
    assert third['next_cursor'] is None and not third['has_more']
    prompts = [g['prompt'] for page in (first, second, third) for g in page['groups']]
    assert len(set(prompts)) == 45
    assert 'new arriving prompt' not in prompts
    assert perf_logger.get_perf_log_groups()['groups'][0]['prompt'] == 'new arriving prompt'


def test_empty_logs_and_bad_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(perf_logger, 'PERF_LOG_FILE', str(tmp_path / 'missing.jsonl'))
    assert perf_logger.get_perf_log_groups() == {'groups': [], 'has_more': False, 'next_cursor': None}
    for cursor in ('invalid!', 'WzEsIC0xXQ=='):
        with pytest.raises(ValueError, match='分页游标'):
            perf_logger.get_perf_log_groups(cursor=cursor)
