"""Refresh deployed inventory and metadata; never fabricate new acceptance."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.capability_query import capability_tool_choice
from core import capability_freshness as freshness
from tools import tool_discovery as discovery


def test_each_new_capability_question_forces_discovery_but_not_each_model_round():
    tools = [{'function': {'name': 'get_capabilities'}}]
    old = [{'role': 'user', 'content': '你具备哪些能力？'},
           {'role': 'tool', 'name': 'get_capabilities', 'content': 'old inventory'},
           {'role': 'assistant', 'content': 'old answer'}]
    current = old + [{'role': 'user', 'content': '你目前具备哪些能力？'}]
    assert capability_tool_choice(current, tools) == {'type': 'function', 'function': {'name': 'get_capabilities'}}
    assert capability_tool_choice(current + [{'role': 'tool', 'name': 'get_capabilities', 'content': '{}'}], tools) == 'auto'
    assert capability_tool_choice([{'role': 'user', 'content': '查询工单开工日期'}], tools) == 'auto'
    assert capability_tool_choice(current, []) == 'auto'


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


def test_native_agent_passes_forced_discovery_to_the_model(monkeypatch):
    from agent import langgraph_runtime as runtime
    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='', tool_calls=None))])
    create = AsyncMock(return_value=chunks())
    monkeypatch.setattr(runtime, '_client', SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(runtime, '_openai_tools', [{'function': {'name': 'get_capabilities'}}])
    monkeypatch.setattr(runtime, 'get_stream_writer', lambda: lambda event: None)
    monkeypatch.setattr(runtime, 'record_perf', lambda *a, **k: None)
    monkeypatch.setattr(runtime, 'build_runtime_experience_context', lambda *a: '')
    asyncio.run(runtime._agent_node({'messages': [{'role': 'user', 'content': '你具备哪些能力？'}]}))
    assert create.call_args.kwargs['tool_choice']['function']['name'] == 'get_capabilities'
