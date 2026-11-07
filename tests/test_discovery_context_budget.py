"""Use the real growing registry: a first capability query must reach its answer."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.context_budget import budget_history
from agent import langgraph_runtime as runtime
from tools.tool_discovery import get_capabilities


def test_real_catalog_fits_and_preserves_all_groups_and_gap_constraints():
    catalog = asyncio.run(get_capabilities())
    data = json.loads(catalog)
    message = {'role':'tool','name':'get_capabilities','tool_call_id':'catalog-1','content':catalog}
    messages = [{'role':'user','content':'你具备哪些能力？请根据实际能力目录简要介绍，并说明尚不支持的内容。'},
                {'role':'assistant','content':'先查询实际目录。','tool_calls':[{
                    'id':'catalog-1','function':{'name':'get_capabilities','arguments':'{}'}}]}, message]
    original = copy.deepcopy(messages)
    result, metrics = budget_history(messages, budget=16000)
    assert messages == original  # Persisted response is never rewritten.
    view = json.loads(result[-1]['content'])
    assert metrics['history_estimated_tokens'] < 15000
    assert {g['group'] for g in view['groups']} == {g['group'] for g in data['groups']}
    assert sum(g['tool_count'] for g in view['groups']) == sum(len(g['tools']) for g in data['groups'])
    assert view['limitations'] == data['limitations']
    assert view['capability_gaps']['checked_at'] == data['capability_gaps']['checked_at']
    for actual, source in zip(view['capability_gaps']['items'], data['capability_gaps']['items']):
        for key in ('id','title','status','status_label','current','missing'):
            assert actual[key] == source[key]


def test_tool_detail_and_business_plan_are_never_projected():
    detail = asyncio.run(get_capabilities(tool_name='release_container'))
    messages = [{'role':'tool','name':'get_capabilities','content':detail},
                {'role':'tool','name':'prepare_quality_event_submission',
                 'content':json.dumps({'groups':[{'tools':[]}], 'plan_id':'p', 'plan_digest':'d','state':'unknown'})}]
    assert budget_history(messages, budget=16000)[0] == messages


def test_repeated_directory_retry_fits_without_losing_call_pairs():
    catalog = asyncio.run(get_capabilities())
    messages = [{'role':'user','content':'请介绍能力'}]
    for i in range(2):
        messages.extend([{'role':'assistant','tool_calls':[{'id':f'c{i}', 'function':{
            'name':'get_capabilities','arguments':'{}'}}]},
            {'role':'tool','name':'get_capabilities','tool_call_id':f'c{i}','content':catalog}])
    result, _ = budget_history(messages, budget=16000)
    assert len(result) == len(messages)
    assert result[-1]['tool_call_id'] == 'c1'
    assert json.loads(result[-1]['content'])['directory_duplicate_of_tool_call_id'] == 'c0'


def test_agent_answers_after_real_catalog_instead_of_context_limit(monkeypatch):
    requests = []
    async def create(**kwargs):
        requests.append(kwargs)
        calls = [SimpleNamespace(index=0, id='c', function=SimpleNamespace(
            name='get_capabilities', arguments='{}'))] if len(requests)==1 else None
        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content='' if calls else '当前能力说明：已查询全部业务分组及能力限制。',
                reasoning_content='provider continuation metadata' if calls else None, tool_calls=calls))])
        return chunks()
    monkeypatch.setattr(runtime,'_client',SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(runtime,'get_stream_writer',lambda: lambda _:None)
    monkeypatch.setattr(runtime,'record_perf',lambda *a,**k:None)
    monkeypatch.setattr(runtime,'record_tool_outcome',lambda **k:None)
    monkeypatch.setattr(runtime,'build_runtime_experience_context',lambda *a:'')
    monkeypatch.setattr(runtime,'get_tool_func',lambda name:get_capabilities if name=='get_capabilities' else None)
    async def run():
        state={'messages':[{'role':'user','content':'你具备哪些能力？请根据实际目录简要介绍，并说明尚不支持的内容。'}]}
        state.update(await runtime._agent_node(state))
        state.update(await runtime._execute_tool_node(state))
        assert len(state['messages'][-1]['content'].encode()) > 16000
        state.update(await runtime._agent_node(state))
        assert state['stop_reason']=='complete'
        assert '当前能力说明' in state['final_reply']
    asyncio.run(run())
    assert len(requests)==2
    assistant = next(m for m in requests[1]['messages'] if m.get('tool_calls'))
    assert assistant['reasoning_content']=='provider continuation metadata'


def test_overview_then_workflow_and_followups_keep_budget_and_evidence():
    catalog = asyncio.run(get_capabilities())
    messages = [{'role':'system','content':'全局规则'}, {'role':'user','content':'介绍实际能力和限制'},
                {'role':'assistant','tool_calls':[{'id':'catalog','function':{'name':'get_capabilities','arguments':'{}'}}],
                 'reasoning_content':'old provider metadata '*5000},
                {'role':'tool','name':'get_capabilities','tool_call_id':'catalog','content':catalog},
                {'role':'assistant','content':'当前能力说明，质量、产品、工单、容器、工艺、版本、审批及待核实限制。'*100}]
    original = copy.deepcopy(messages)
    workflow = json.dumps({'value':[{'Name':'WF-A','Revision':'1','InstanceID':'0000000000000701'}]})
    for turn in range(10):
        messages.extend([{'role':'user','content':'查询当前的workflow' if not turn else '继续查看该路线'},
                         {'role':'assistant','tool_calls':[{'id':f'w{turn}','function':{'name':'list_workflows','arguments':'{}'}}]},
                         {'role':'tool','name':'list_workflows','tool_call_id':f'w{turn}','content':workflow},
                         {'role':'assistant','content':'WF-A 版本1，引用实际ID 0000000000000701。'}])
        view, metrics = budget_history(messages, budget=16000)
        assert metrics['history_estimated_tokens'] < 16000
        assert [m for m in view if m.get('name')=='list_workflows'] == [m for m in messages if m.get('name')=='list_workflows']
        assert view[-1] == messages[-1]
        assert 'reasoning_content' not in view[2]
        archived = json.loads(next(m for m in view if m.get('name')=='get_capabilities')['content'])
        assert len(archived['groups']) == len(json.loads(catalog)['groups'])
    assert messages[:len(original)] == original


def test_current_tool_exchange_keeps_provider_metadata():
    messages=[{'role':'user','content':'查询'}, {'role':'assistant','reasoning_content':'continuation metadata',
               'tool_calls':[{'id':'c','function':{'name':'get_capabilities','arguments':'{}'}}]}]
    assert budget_history(messages)[0] == messages


def test_disabled_budget_preserves_large_conversation_and_full_directory(monkeypatch):
    from agent import context_budget
    monkeypatch.setattr(context_budget, 'AGENT_HISTORY_TOKEN_BUDGET', 0)
    catalog = asyncio.run(get_capabilities())
    messages = [{'role':'user','content':'批量操作计划和对象引用不要省略。'*10000},
                {'role':'assistant','tool_calls':[{'id':'c','function':{'name':'get_capabilities','arguments':'{}'}}]},
                {'role':'tool','name':'get_capabilities','tool_call_id':'c','content':catalog},
                {'role':'assistant','content':'完整回答。'*10000},
                {'role':'user','content':'继续查询 workflow'}]
    before = copy.deepcopy(messages)
    result, metrics = budget_history(messages)
    assert result == before == messages
    assert metrics['history_estimated_tokens'] > 16000
    assert metrics['history_budget_enabled'] is False
    assert metrics['history_removed_messages'] == 0
    assert metrics['history_projected_discovery_messages'] == 0
