"""Refresh deployed inventory and metadata; never fabricate new acceptance."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.capability_query import needs_capability_refresh, read_capability_directory, capability_context
from core import capability_freshness as freshness
from tools import tool_discovery as discovery


def test_each_new_capability_question_requires_discovery_but_not_old_tool_results():
    tools = [{'function': {'name': 'get_capabilities'}}]
    old = [{'role': 'user', 'content': '你具备哪些能力？'},
           {'role': 'tool', 'name': 'get_capabilities', 'content': 'old inventory'},
           {'role': 'assistant', 'content': 'old answer'}]
    current = old + [{'role': 'user', 'content': '你目前具备哪些能力？'}]
    assert needs_capability_refresh(current)
    assert not needs_capability_refresh(current + [{'role': 'tool', 'name': 'get_capabilities', 'content': '{}'}])
    assert not needs_capability_refresh([{'role': 'user', 'content': '查询工单开工日期'}])
    tool = AsyncMock(return_value='{"source":"live registry"}')
    assert asyncio.run(read_capability_directory(current, tools, lambda _: tool))
    tool.assert_awaited_once_with()
    assert not asyncio.run(read_capability_directory(current, [], lambda _: tool))


def test_prefetch_failure_becomes_explicit_unavailable_evidence():
    tool = AsyncMock(side_effect=ValueError('directory unavailable token=private-value'))
    raw = asyncio.run(read_capability_directory([{'role': 'user', 'content': '你具备哪些能力？'}],
        [{'function': {'name': 'get_capabilities'}}], lambda _: tool))
    assert json.loads(raw)['complete'] is False
    assert 'private-value' not in raw
    assert '不能沿用旧答案' in capability_context(raw)


def test_metadata_changes_are_visible_on_the_next_request(tmp_path, monkeypatch):
    path = tmp_path / 'gaps.json'
    monkeypatch.setattr(discovery, 'CAPABILITY_GAP_FILE', path)
    report = {'scope': 'test scope', 'checked_at': 'old audit date', 'gaps': [{
        'id': 'G00', 'title': 'test', 'groups': ['container'], 'status': 'partial', 'priority': 'P1',
        'current': 'old scope', 'missing': 'not yet implemented', 'contracts': [], 'code': ['tools/test.py']}]}
    path.write_text(json.dumps(report), encoding='utf-8')
    assert discovery.capability_gap_summary()['items'][0]['current'] == 'old scope'
    report['gaps'][0].update(current='new verified scope', status='implemented')
    path.write_text(json.dumps(report), encoding='utf-8')
    updated = discovery.capability_gap_summary()
    assert updated['items'][0]['current'] == 'new verified scope'
    assert updated['items'][0]['status'] == 'implemented'
    assert 'checked_at' not in updated
    changed = discovery.capability_gap_summary(freshness={'changed_source_files': ['tools/test.py']})
    assert changed['items'][0]['status'] == 'validation_pending'
    assert '已修改' in changed['items'][0]['current']
    assert report['gaps'][0]['status'] == 'implemented'  # Presentation never rewrites the source evidence.
    path.write_text('{broken', encoding='utf-8')
    assert discovery.capability_gap_summary()['available'] is False  # No stale fallback.


def test_inventory_is_read_from_running_registry_each_time(monkeypatch):
    tool = SimpleNamespace(name='list_mfgorders', description='live tool', parameters={})
    listing = AsyncMock(side_effect=[[tool], []])
    monkeypatch.setattr(discovery.mcp, 'list_tools', listing)
    first = json.loads(asyncio.run(discovery.get_capabilities()))
    second = json.loads(asyncio.run(discovery.get_capabilities()))
    assert {t['name'] for g in first['groups'] for t in g['tools']} == {'list_mfgorders'}
    assert not any(g['tools'] for g in second['groups'])


def test_source_edits_additions_and_deletions_require_reload(tmp_path, monkeypatch):
    (tmp_path / 'tools').mkdir()
    file = tmp_path / 'tools' / 'test.py'
    file.write_text('original', encoding='utf-8')
    monkeypatch.setattr(freshness, 'ROOT', tmp_path)
    monkeypatch.setattr(freshness, 'LOADED_SOURCES', freshness.source_snapshot())
    assert not freshness.catalog_freshness()['reload_required']
    file.write_text('changed', encoding='utf-8')
    changed = freshness.catalog_freshness()
    assert changed['reload_required'] and changed['changed_source_files'] == ['tools/test.py']
    assert not changed['acceptance_is_automatic']
    file.unlink()
    assert freshness.catalog_freshness()['reload_required']


def test_native_agent_prefetches_actual_directory_with_thinking_compatible_auto_choice(monkeypatch):
    from agent import langgraph_runtime as runtime
    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='', tool_calls=None))])
    async def create(**kwargs):
        assert kwargs['tool_choice'] == 'auto'  # Provider rejects named/required choices in thinking mode.
        return chunks()
    create = AsyncMock(side_effect=create)
    directory = AsyncMock(return_value='{"source":"live registry","live_evidence":"fresh result"}')
    monkeypatch.setattr(runtime, '_client', SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(runtime, '_openai_tools', [{'function': {'name': 'get_capabilities'}}])
    monkeypatch.setattr(runtime, 'get_stream_writer', lambda: lambda event: None)
    monkeypatch.setattr(runtime, 'record_perf', lambda *a, **k: None)
    monkeypatch.setattr(runtime, 'build_runtime_experience_context', lambda *a: '')
    monkeypatch.setattr(runtime, 'get_tool_func', lambda name: directory if name == 'get_capabilities' else None)
    async def run():
        state = {'messages': [{'role': 'user', 'content': '你具备哪些能力？'}]}
        state.update(await runtime._agent_node(state))
        assert 'fresh result' in state['capability_directory']
        state.update(await runtime._agent_node(state))
        return state
    asyncio.run(run())
    directory.assert_awaited_once()
    for request in create.call_args_list:
        messages = request.kwargs['messages']
        assert any('fresh result' in m.get('content', '') for m in messages)
        assert not any(m.get('role') == 'tool' or m.get('tool_calls') for m in messages)


def test_legacy_agent_prefetches_without_forced_tool_choice(monkeypatch):
    from agent import llm_client as client
    from agent.prompts import SYSTEM_PROMPT
    directory = AsyncMock(return_value='{"source":"live registry","live_evidence":"legacy fresh result"}')
    async def create(**kwargs):
        assert kwargs['tool_choice'] == 'auto'
        assert any('legacy fresh result' in m.get('content', '') for m in kwargs['messages'])
        assert not any(m.get('tool_calls') or m.get('role') == 'tool' for m in kwargs['messages'])
        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='当前能力说明。', tool_calls=None))])
        return chunks()
    monkeypatch.setattr(client.oai_client.chat.completions, 'create', create)
    monkeypatch.setattr(client, 'openai_tools', [{'function': {'name': 'get_capabilities'}}])
    monkeypatch.setattr(client, 'get_tool_func', lambda name: directory if name == 'get_capabilities' else None)
    monkeypatch.setattr(client, 'get_user_messages', lambda *a: [{'role': 'system', 'content': SYSTEM_PROMPT}])
    monkeypatch.setattr(client, 'save_user_session', lambda *a: None)
    monkeypatch.setattr(client, 'update_session_title', lambda *a: None)
    monkeypatch.setattr(client, 'record_perf', lambda *a, **k: None)
    monkeypatch.setattr(client, 'build_runtime_experience_context', lambda *a: '')
    monkeypatch.setattr(client, 'record_feedback', lambda *a: None)
    async def run():
        return [event async for event in client.legacy_chat_stream('diagnostic', '你具备哪些能力？', 's')]
    events = asyncio.run(run())
    assert any('当前能力说明' in event for event in events)
    directory.assert_awaited_once()
