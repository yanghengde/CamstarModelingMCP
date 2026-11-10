"""Performance paths must preserve scope, evidence and mutation boundaries."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent import capability_query as capabilities, context_budget, langgraph_runtime as runtime
from tools.tool_discovery import get_capabilities
from tools import order_progress as progress
from core.inference_metrics import InferenceMetrics

QUESTION = '你具备哪些能力？请根据实际能力目录简要介绍，并说明尚不支持的内容。'


def test_generic_capability_reply_preserves_every_group_and_gap_boundary():
    raw = asyncio.run(get_capabilities())
    data = json.loads(raw)
    reply = capabilities.render_capability_overview(QUESTION, raw)
    assert reply and '<details>' in reply
    for group in data['groups']:
        if group['tools']:
            assert group['label'].replace('&', '&amp;') in reply
    for gap in data['capability_gaps']['items']:
        assert gap['title'] in reply
        assert capabilities.render_capability_overview('请详细介绍质量能力和参数', raw) is None
    assert '真实Batch' in reply and 'unknown' in reply
    assert '注册' not in reply or '验收' in reply
    assert capabilities.render_capability_overview(QUESTION, '{"error":"unavailable"}') is None
    assert capabilities.render_capability_overview('你具备哪些能力？只列设备资源', raw) is None


def test_capability_summary_does_not_wait_for_model_and_keeps_raw_evidence(monkeypatch):
    raw = asyncio.run(get_capabilities())
    model = AsyncMock(side_effect=AssertionError('generic live inventory does not need inference'))
    directory = AsyncMock(return_value=raw)
    monkeypatch.setattr(runtime, '_client', SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=model))))
    monkeypatch.setattr(runtime, '_openai_tools', [{'function': {'name': 'get_capabilities'}}])
    monkeypatch.setattr(runtime, 'get_tool_func', lambda _: directory)
    events, metrics = [], []
    monkeypatch.setattr(runtime, 'get_stream_writer', lambda: events.append)
    monkeypatch.setattr(runtime, 'record_perf', lambda action, *a, **k: metrics.append(action))
    monkeypatch.setattr(runtime, 'build_runtime_experience_context', lambda *a: (_ for _ in ()).throw(AssertionError('not needed')))
    result = asyncio.run(runtime._agent_node({'messages': [{'role': 'user', 'content': QUESTION}]}))
    model.assert_not_awaited()
    directory.assert_awaited_once()
    assert result['stop_reason'] == 'complete' and result['capability_directory'] == raw
    assert result['messages'][-1]['content'] == result['final_reply']
    assert events[-1] == {'type': 'stream_end', 'audience': 'answer'}
    assert metrics == ['Tool_Execute', 'Capability_Overview']


def test_discovery_projection_does_not_require_history_truncation(monkeypatch):
    monkeypatch.setattr(context_budget, 'AGENT_PROJECT_DISCOVERY', True)
    raw = asyncio.run(get_capabilities())
    receipt = {'role': 'tool', 'name': 'execute_quality_transaction', 'tool_call_id': 'write',
               'content': '{"state":"unknown","plan_id":"keep-me","plan_digest":"keep-digest"}'}
    messages = [{'role': 'user', 'content': QUESTION},
        {'role': 'tool', 'name': 'get_capabilities', 'tool_call_id': 'catalog', 'content': raw},
        receipt, {'role': 'assistant', 'content': '计划尚待核实'},
        {'role': 'user', 'content': '继续核实之前的事务，不要重发'}]
    original = copy.deepcopy(messages)
    result, metrics = context_budget.budget_history(messages, budget=0)
    assert messages == original and receipt in result
    assert result[-1] == messages[-1] and metrics['history_removed_messages'] == 0
    assert metrics['history_estimated_tokens'] < metrics['history_original_estimated_tokens']
    projected = json.loads(result[1]['content'])
    assert len(projected['groups']) == len(json.loads(raw)['groups'])
    assert projected['limitations'] == json.loads(raw)['limitations']


def test_token_cache_invalidates_when_message_content_changes():
    context_budget._text_tokens.cache_clear()
    message = {'role': 'user', 'content': '工单A'}
    context_budget.estimate_tokens(message)
    context_budget.estimate_tokens(message)
    assert context_budget._text_tokens.cache_info().hits == 1
    message['content'] = '工单B变更后的全部参数' * 20
    assert context_budget.estimate_tokens(message) > 10
    assert context_budget._text_tokens.cache_info().misses == 2


def test_order_enrichment_is_bounded_ordered_and_checks_identity_before_history(monkeypatch):
    order_id = '0006548000000002'
    rows = [{'ContainerId': f'{i:016x}', 'ContainerName': f'C{i}'} for i in range(6)]
    monkeypatch.setattr(progress, 'READ_QUERY_CONCURRENCY', 3)
    monkeypatch.setattr(progress.ReadContext, 'resolve', AsyncMock(return_value={'Name': 'MO', 'InstanceID': order_id}))
    monkeypatch.setattr(progress, 'request', AsyncMock(return_value=json.dumps(rows)))
    monkeypatch.setattr(progress, '_sort_distribution', AsyncMock())
    active = peak = 0
    status_done, histories = set(), []
    entered = asyncio.Event()
    async def details(name, cid):
        nonlocal active, peak
        active += 1; peak = max(peak, active)
        if active == 3:
            entered.set()
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0)
        active -= 1
        status_done.add(name)
        return json.dumps({'data': {'ContainerStatusDetails': {
            'MfgOrder': {'InstanceId': 'wrong' if name == 'C2' else order_id},
            'StatusName': 'Closed' if name == 'C0' else 'Active', 'Qty': 0 if name == 'C0' else 1}}})
    async def history(name, cid, **kwargs):
        assert name in status_done
        histories.append(name)
        return '{"complete":false,"data":{"Histories":[]}}'
    monkeypatch.setattr(progress, 'get_container_details', details)
    monkeypatch.setattr(progress, 'get_container_history', history)
    result = json.loads(asyncio.run(progress.get_mfgorder_progress('MO', limit=6)))
    assert peak == 3
    assert [r['name'] for r in result['containers']] == [r['ContainerName'] for r in rows]
    assert result['containers'][0]['current']['StatusName'] == 'Closed'
    assert result['containers'][0]['current']['Qty'] == 0
    assert result['containers'][2]['error'] and 'C2' not in histories
    assert result['production_completed_qty'] is None and result['completion_percentage'] is None


def test_inference_metrics_distinguish_thinking_from_visible_response():
    metrics = InferenceMetrics()
    metrics.observe(SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content='internal', content='', tool_calls=None))]))
    assert metrics.result()['first_event_seconds'] is not None
    assert metrics.result()['first_delta_seconds'] is None
    metrics.observe(SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='答复', tool_calls=None))]))
    assert metrics.result()['first_delta_seconds'] is not None
    assert metrics.result()['reasoning_chars'] == 8 and metrics.result()['output_chars'] == 2


def test_local_session_replace_retries_only_temporary_windows_locks(tmp_path, monkeypatch):
    from agent import memory
    if memory.os.name != 'nt':
        pytest.skip('Windows file-sharing semantics')
    destination = tmp_path / 'session.json'
    destination.write_text('{"old":"preserved"}', encoding='utf-8')
    replace = memory.os.replace
    calls, waits = [], []
    def locked_once(src, target):
        calls.append(str(target))
        if len(calls) == 1:
            error = PermissionError('destination temporarily held by reader')
            error.winerror = 5
            raise error
        return replace(src, target)
    monkeypatch.setattr(memory.os, 'replace', locked_once)
    monkeypatch.setattr(memory.time, 'sleep', waits.append)
    memory._atomic_json(destination, {'new': 'complete'})
    assert json.loads(destination.read_text()) == {'new': 'complete'}
    assert len(calls) == 2 and waits == [.01]


def test_permanent_local_replace_failure_keeps_original_file(tmp_path, monkeypatch):
    from agent import memory
    destination = tmp_path / 'session.json'
    destination.write_text('{"old":"preserved"}', encoding='utf-8')
    attempts = []
    def denied(*args):
        attempts.append(args)
        error = PermissionError('permanent denial')
        error.winerror = 5
        raise error
    monkeypatch.setattr(memory.os, 'replace', denied)
    monkeypatch.setattr(memory.time, 'sleep', lambda _: None)
    with pytest.raises(PermissionError):
        memory._atomic_json(destination, {'new': 'not committed'})
    assert json.loads(destination.read_text()) == {'old': 'preserved'}
    assert len(attempts) == (4 if memory.os.name == 'nt' else 1)
