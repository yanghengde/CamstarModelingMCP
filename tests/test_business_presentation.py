import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent import langgraph_runtime as runtime
from agent.prompts import BUSINESS_PRESENTATION_RULE, SYSTEM_PROMPT
from agent.read_efficiency import previous_failed_read
from core.user_errors import error_event, tool_diagnostic


def test_public_error_is_brief_with_sanitized_evidence():
    result = error_event('明细暂时无法读取。', RuntimeError('HTTP 500 token=secret password="abc" Bearer hidden sk-private-key https://u:pass@host/path'))
    assert result['message'] == '明细暂时无法读取。'
    assert 'HTTP 500' in result['details']
    assert 'secret' not in result['details'] and 'abc' not in result['details'] and 'hidden' not in result['details']
    assert 'sk-private-key' not in result['details'] and 'u:pass' not in result['details']
    assert tool_diagnostic('{"error":"明细ID重复、字段缺失或父级不一致。"}')
    assert tool_diagnostic('{"complete":true,"data":[1,2,3]}') is None


def test_failed_read_is_only_blocked_for_same_arguments_and_current_request():
    messages = [{'role':'user','content':'查质量配置'},
                {'role':'assistant','tool_calls':[{'id':'1','function':{'name':'get_quality_reference_details',
                    'arguments':'{"kind":"aql_level","name":"IQC_DEMO_AQL"}'}}]},
                {'role':'tool','tool_call_id':'1','name':'get_quality_reference_details',
                 'content':'{"error":"明细ID重复、字段缺失或父级不一致。"}'}]
    args = {'name':'IQC_DEMO_AQL','kind':'aql_level'}
    assert previous_failed_read(messages,'get_quality_reference_details',args)
    assert previous_failed_read(messages,'get_quality_reference_details',{**args,'instance_id':'other'}) is None
    assert previous_failed_read(messages+[{'role':'user','content':'重新核实'}],'get_quality_reference_details',args) is None
    assert previous_failed_read(messages,'update_spec',args) is None


@pytest.mark.parametrize('tool_call', [True, False])
def test_stream_classifies_activity_and_injects_business_rule_for_old_sessions(monkeypatch, tool_call):
    calls = [SimpleNamespace(index=0,id='c',function=SimpleNamespace(name='get_capabilities',arguments='{}'))] if tool_call else None
    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content='正在核实对象。' if tool_call else '已找到该表，明细暂时无法读取。',tool_calls=calls))])
    create=AsyncMock(return_value=chunks())
    monkeypatch.setattr(runtime,'_client',SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    events=[]
    monkeypatch.setattr(runtime,'get_stream_writer',lambda:events.append)
    monkeypatch.setattr(runtime,'build_runtime_experience_context',lambda *a:'')
    monkeypatch.setattr(runtime,'record_perf',lambda *a,**k:None)
    asyncio.run(runtime._agent_node({'messages':[{'role':'system','content':'旧会话规则'},
                                                 {'role':'user','content':'查询IQC_DEMO_AQL'}]}))
    assert any(BUSINESS_PRESENTATION_RULE in m.get('content','') for m in create.call_args.kwargs['messages'])
    assert events[-1] == {'type':'stream_end','audience':'activity' if tool_call else 'answer'}


def test_repeated_failed_read_emits_details_without_repeating_backend(monkeypatch):
    read=AsyncMock(return_value='{"error":"detail failed"}')
    monkeypatch.setattr(runtime,'get_tool_func',lambda _:read)
    monkeypatch.setattr(runtime,'record_perf',lambda *a,**k:None)
    monkeypatch.setattr(runtime,'record_tool_outcome',lambda **k:None)
    events=[]
    monkeypatch.setattr(runtime,'get_stream_writer',lambda:events.append)
    def call(key):return {'id':key,'function':{'name':'list_workflows','arguments':'{}'}}
    state={'messages':[{'role':'user','content':'查询路线'}, {'role':'assistant','tool_calls':[call('one'),call('two')]}],
           'pending_tool_calls':[call('one'),call('two')]}
    async def run():
        state.update(await runtime._execute_tool_node(state))
        state.update(await runtime._execute_tool_node(state))
    asyncio.run(run())
    assert read.await_count==1
    assert any(e['type']=='tool_diagnostic' and 'detail failed' in e['details'] for e in events)
    assert BUSINESS_PRESENTATION_RULE in SYSTEM_PROMPT
