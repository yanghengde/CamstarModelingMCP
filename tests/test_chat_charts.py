"""Chart snapshots preserve statistical evidence and cannot block chat history."""
import asyncio
import copy
import json
import time

import pytest

from core import chat_charts as charts
from tools import quality_spc as spc, quality_spc_attributes as attributes


CONTEXT = dict(characteristic='Diameter', unit='mm', process_scope='synthetic line',
               data_reference='test-fixture', order_reference='sequence')


def continuous(kind='i_mr', count=3):
    size = 1 if kind == 'i_mr' else 3
    return spc.analyze(chart_type=kind,
        points=[dict(observation_id=f'P{i}', sequence=i, values=['100000003.000000000001']*size) for i in range(count)],
        context=CONTEXT, rules=['beyond_3sigma'],
        controls=dict(reference='fixed test profile', version='A', subgroup_size=size,
            center='100000000', sigma='1', dispersion_center='1', dispersion_lcl='0', dispersion_ucl='10'))


def counted(kind):
    return attributes.analyze(chart_type=kind,
        count_kind='nonconforming_units' if kind in ('p', 'np') else 'nonconformities',
        points=[dict(observation_id=f'P{i}', sequence=i, count=1, inspected_units='36' if kind in ('np', 'c') else str(4+i*10)) for i in range(3)],
        context=CONTEXT, rule='beyond_3sigma',
        controls=dict(reference='fixed counts', version='A', rate='0.5', inspected_units='36' if kind in ('np', 'c') else None))


@pytest.mark.parametrize('kind', ['i_mr', 'xbar_r', 'p', 'np', 'c', 'u'])
def test_six_templates_retain_backend_values_limits_signals_and_digest(kind):
    data = continuous(kind) if kind in ('i_mr', 'xbar_r') else counted(kind)
    tool = 'analyze_spc_series' if kind in ('i_mr', 'xbar_r') else 'analyze_spc_attributes'
    before = copy.deepcopy(data)
    payload = charts.build(tool, json.dumps(data))
    assert data == before
    assert payload['source_digest'] == data['input_digest']
    assert '本地SPC' in payload['subtitle'] and '放行' in payload['note']
    for original, row in zip(data['points'], payload['panels'][0]['rows']):
        assert row['value'] == original['statistic']
        assert row['id'] == original['observation_id']
        if kind in ('p', 'np', 'c', 'u'):
            assert (row['lcl'], row['ucl'], row['signal']) == (original['lcl'], original['ucl'], original['beyond_3sigma'])
        else:
            assert row['signal']  # >3 with precision greater than JS Number; never recompute client-side.
            assert '100000003.000000000001' in row['detail']


@pytest.mark.parametrize('change', ['digest', 'native', 'write', 'version', 'duplicate', 'nonfinite', 'too_many'])
def test_bad_or_unsupported_evidence_does_not_publish(change, tmp_path, monkeypatch):
    data = continuous()
    if change == 'digest': data['input_snapshot']['changed'] = True
    if change == 'native': data['native_spc_result'] = True
    if change == 'write': data['ready_for_mes_write'] = True
    if change == 'version': data['algorithm_version'] = 'future-unreviewed'
    if change == 'duplicate': data['points'][1]['observation_id'] = data['points'][0]['observation_id']
    if change == 'nonfinite': data['points'][0]['statistic'] = 'NaN'
    if change == 'too_many': data['points'] *= 200
    monkeypatch.setattr(charts, 'ENABLE_CHAT_CHARTS', True)
    monkeypatch.setattr(charts, 'STORE', charts.ChartStore(tmp_path/'charts.db'))
    assert charts.publish('user', 'session', 'call', '画SPC图', 'analyze_spc_series', json.dumps(data)) is None
    assert not (tmp_path/'charts.db').exists()


def test_history_source_requires_complete_verified_window():
    data = continuous()
    assert charts.build('analyze_spc_history', json.dumps(data)) is None
    data.update(complete=True, data_verified_against_mes=True, source_window_verified=True)
    assert 'MES原始历史' in charts.build('analyze_spc_history', json.dumps(data))['subtitle']


def test_order_distribution_must_not_misrepresent_paged_counts_as_production():
    data = dict(order={'Name':'MO-test'}, distribution_scope='current_page_only',
        containers=[{},{}], returned_container_count=2, container_list_complete=False, page={'has_more':True},
        page_step_distribution=[dict(workflow='WF', revision='A', step='S1', container_count=2)])
    payload = charts.build('get_mfgorder_progress', json.dumps(data))
    assert not payload['complete'] and '仅当前页' in payload['subtitle'] and '不等于产量' in payload['subtitle']
    data['page_step_distribution'][0]['container_count'] = 3
    with pytest.raises(ValueError): charts.build('get_mfgorder_progress', json.dumps(data))


def test_store_is_immutable_scoped_and_only_restores_calls_in_history(tmp_path):
    store = charts.ChartStore(tmp_path/'charts.db')
    payload = charts.build('analyze_spc_series', json.dumps(continuous()))
    descriptor = store.save('u', 's', 'c', payload)
    assert store.save('u', 's', 'c', payload) == descriptor
    assert charts.ChartStore(store.path).get('u', 's', descriptor['id']) == payload
    assert store.get('other', 's', descriptor['id']) is None
    assert store.get('u', 'other', descriptor['id']) is None
    assert store.get('u', 's', '../charts.db') is None
    assert store.list('u', 's', []) == []
    assert store.list('u', 's', [dict(role='tool', tool_call_id='c')]) == [descriptor]
    changed = copy.deepcopy(payload); changed['title'] = 'changed'
    with pytest.raises(ValueError): store.save('u', 's', 'c', changed)
    with store.db() as db: db.execute("UPDATE charts SET payload='{}'")
    with pytest.raises(ValueError): store.get('u', 's', descriptor['id'])


@pytest.mark.parametrize('question,expected', [('SPC分析',True),('给我控制图',True),('画工序柱状图',True),('不要出图',False),('只给文字，不需要图表',False),('查询工单',False)])
def test_chart_intent(question, expected):
    assert charts.requested(question) is expected


def test_optional_storage_timeout_keeps_event_loop_responsive(monkeypatch):
    monkeypatch.setattr(charts, 'ENABLE_CHAT_CHARTS', True)
    def slow(*args): time.sleep(.4)
    monkeypatch.setattr(charts, 'publish', slow)
    async def scenario():
        start = time.perf_counter()
        task = asyncio.create_task(charts.publish_async('u','s','c','SPC图','analyze_spc_series','{}'))
        await asyncio.sleep(.03)
        assert time.perf_counter()-start < .15
        assert await task is None
        assert time.perf_counter()-start < .35
    asyncio.run(scenario())


def test_unexpected_chart_failure_cannot_fail_tool_and_cancellation_still_propagates(monkeypatch):
    monkeypatch.setattr(charts,'ENABLE_CHAT_CHARTS',True)
    def broken(*args): raise RuntimeError('optional component failure')
    monkeypatch.setattr(charts,'publish',broken)
    args=('u','s','c','SPC图','analyze_spc_series','{}')
    assert asyncio.run(charts.publish_async(*args)) is None
    def cancelled(*args): raise asyncio.CancelledError()
    monkeypatch.setattr(charts,'publish',cancelled)
    with pytest.raises(asyncio.CancelledError): asyncio.run(charts.publish_async(*args))


def test_chart_api_owner_session_and_disabled_switch(tmp_path, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from web import routes
    store = charts.ChartStore(tmp_path/'charts.db')
    descriptor = store.save('chart-user','s','c',charts.build('analyze_spc_series',json.dumps(continuous())))
    monkeypatch.setattr(charts, 'STORE', store)
    monkeypatch.setattr(routes, 'CAMSTAR_USERNAME', 'chart-user')
    monkeypatch.setattr(routes, 'user_memories', {'chart-user':{'sessions':{'s':{}}}})
    monkeypatch.setattr(routes, 'ENABLE_CHAT_CHARTS', True)
    app = FastAPI(); app.include_router(routes.router)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            url='/api/chat/charts/'+descriptor['id']
            assert (await client.get(url,params=dict(username='chart-user',session_id='s'))).status_code == 200
            for owner,session in [('wrong','s'),('chart-user','missing')]:
                assert (await client.get(url,params=dict(username=owner,session_id=session))).status_code == 404
            monkeypatch.setattr(routes, 'ENABLE_CHAT_CHARTS', False)
            assert (await client.get(url,params=dict(username='chart-user',session_id='s'))).status_code == 404
    asyncio.run(scenario())


def test_runtime_emits_only_descriptor_and_keeps_tool_evidence(monkeypatch, tmp_path):
    from agent import langgraph_runtime as runtime
    from unittest.mock import AsyncMock
    raw=json.dumps(continuous())
    monkeypatch.setattr(charts, 'STORE', charts.ChartStore(tmp_path/'charts.db'))
    monkeypatch.setattr(charts, 'ENABLE_CHAT_CHARTS', True)
    monkeypatch.setattr(runtime, 'get_tool_func', lambda _: AsyncMock(return_value=raw))
    monkeypatch.setattr(runtime, 'record_perf', lambda *a, **kw: None)
    monkeypatch.setattr(runtime, 'record_tool_outcome', lambda **kw: None)
    events=[]; monkeypatch.setattr(runtime, 'get_stream_writer', lambda: events.append)
    call=dict(id='call-chart',function=dict(name='analyze_spc_series',arguments='{}'))
    state=dict(username='u',session_id='s',request_message='请出SPC图',messages=[],pending_tool_calls=[call])
    result=asyncio.run(runtime._execute_tool_node(state))
    assert result['messages']==[dict(role='tool',tool_call_id='call-chart',name='analyze_spc_series',content=raw)]
    chart=next(e['chart'] for e in events if e['type']=='chart')
    assert 'panels' not in chart and len(json.dumps(chart))<400
    assert charts.STORE.get('u','s',chart['id'])['source_digest']==json.loads(raw)['input_digest']


def test_chart_storage_failure_does_not_hide_text_history(monkeypatch):
    from web import routes
    monkeypatch.setattr(routes,'ENABLE_CHAT_CHARTS',True)
    monkeypatch.setattr(routes,'set_active_session',lambda *a: None)
    monkeypatch.setattr(routes,'get_user_messages',lambda *a:[dict(role='assistant',content='文字保留')])
    monkeypatch.setattr(routes.RUNS,'latest',lambda *a:None)
    def broken(*args): raise OSError('injected optional chart disk failure')
    monkeypatch.setattr(charts.STORE,'list',broken)
    response=asyncio.run(routes.history_endpoint('u','s'))
    assert response['messages'][0]['content']=='文字保留' and response['charts']==[]


def test_browser_history_omits_large_tool_arrays_without_changing_model_memory(monkeypatch):
    from web import routes
    messages=[dict(role='system',content='system rules'),dict(role='user',content='请画图'),
        dict(role='assistant',content='核实中',tool_calls=[dict(id='c',function=dict(name='analyze_spc_series',arguments='large arrays'))]),
        dict(role='tool',tool_call_id='c',content='large tool output'),dict(role='assistant',content='结果')]
    original=copy.deepcopy(messages)
    monkeypatch.setattr(routes,'ENABLE_CHAT_CHARTS',False)
    monkeypatch.setattr(routes,'set_active_session',lambda *a:None)
    monkeypatch.setattr(routes,'get_user_messages',lambda *a:messages)
    monkeypatch.setattr(routes.RUNS,'latest',lambda *a:None)
    projected=asyncio.run(routes.history_endpoint('u','s',True))['messages']
    assert messages==original and len(projected)==len(messages)
    assert projected[3]==dict(role='tool',tool_call_id='c')
    assert projected[2]['tool_calls'][0]['id']=='c' and 'arguments' not in json.dumps(projected)
    assert projected[-1]['content']=='结果' and 'system rules' not in json.dumps(projected)
    assert asyncio.run(routes.history_endpoint('u','s'))['messages']==messages

