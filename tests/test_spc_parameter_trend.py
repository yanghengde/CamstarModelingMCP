"""Raw parameter trends preserve source evidence without inventing SPC controls."""
import asyncio
import copy
import json

import pytest
from fastmcp import Client

from tests.test_quality_spc_history import env, POINTS, note, key, analyze
from tests.test_quality_spc_nonempty import source
from tools import mcp, get_tool_func, quality_spc_history as history
from core import chat_charts as charts
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION


def trend(ids=None, scope_kind='process'):
    return json.loads(asyncio.run(history.get_spc_parameter_trend(POINTS[:4] if ids is None else ids,scope_kind)))


def test_raw_trend_needs_no_baseline_controls_or_native_engine(env):
    out=trend()
    assert out['complete'] and out['data_verified_against_mes'] and out['source_window_verified']
    assert [p['value'] for p in out['points']]==['9','10','11','14']
    assert out['points'][0]['timestamp_gmt'].endswith('0000001')
    assert out['source_history_digest']==history.digest(out['source_history'])
    assert out['control_limits'] is None and not out['analysis_performed']
    assert not out['native_spc_result'] and not out['ready_for_mes_write'] and not out['release_authorized']
    assert all(path=='/api/UOMs/'+key(100) for path in env['model'])
    assert all(query.startswith('SELECT ') for query in env['sql'])
    payload=charts.build('get_spc_parameter_trend',json.dumps(out))
    assert payload['panels'][0]['kind']=='trend'
    assert all(not p['signal'] and 'center' not in p for p in payload['panels'][0]['rows'])


def test_exclusion_records_are_visible_and_do_not_relax_spc_gate(env):
    env['annotations']=[note(POINTS[1],True)]
    out=trend()
    assert len(out['points'])==4
    assert [p['has_active_exclusion_record'] for p in out['points']]==[False,True,False,False]
    assert out['source_history']['active_exclusion_records']==[dict(annotation_id=key(500),point_id=POINTS[1])]
    payload=charts.build('get_spc_parameter_trend',json.dumps(out))
    assert payload['panels'][0]['rows'][1]['excluded']
    assert not analyze()['complete']  # Still forbidden for actual SPC analysis.


def test_prior_nonempty_mes_fixture_replays_to_a_trend_without_recalculation(source, tmp_path):
    ids=[row['DataPointHistoryDetailId'] for row in source['points']]
    out=trend(ids,scope_kind='container')
    assert [p['value'] for p in out['points']]==['9','10','11','14']
    payload=charts.build('get_spc_parameter_trend',json.dumps(out))
    store=charts.ChartStore(tmp_path/'trend.db')
    descriptor=store.save('u','s','trend',payload)
    assert charts.ChartStore(store.path).get('u','s',descriptor['id'])==payload
    assert [p['id'] for p in payload['panels'][0]['rows']]==ids
    assert source['expected_signal_id']==ids[-1] and not payload['panels'][0]['rows'][-1]['signal']


@pytest.mark.parametrize('failure',['duplicate','gap','order','tie','scope','parent','numeric','orphan','changed','bad_exclude'])
def test_invalid_or_changing_source_refuses_plot(env,failure):
    ids=POINTS[:4]
    if failure=='duplicate': ids=[POINTS[0]]*2
    elif failure=='gap': ids=ids[:1]+ids[2:]
    elif failure=='order': ids=ids[::-1]
    elif failure=='tie': env['points'][1]['TxnDateGMT']=env['points'][0]['TxnDateGMT']
    elif failure=='scope': env['points'][1]['ProductId']=key(123)
    elif failure=='parent': env['points'][1]['ParentTxnId']=key(123)
    elif failure=='numeric': env['points'][1]['DataValue']='NaN'
    elif failure=='orphan': env['annotations']=[note(POINTS[0])|{'MainlineTxnId':''}]
    elif failure=='bad_exclude': env['annotations']=[note(POINTS[0])|{'Exclude':None}]
    else: env['changes_on_readback']={'DataValue':'100','ChangeCount':'2'}
    out=trend(ids)
    assert out['complete'] is False and 'points' not in out
    assert charts.build('get_spc_parameter_trend',json.dumps(out)) is None


@pytest.mark.parametrize('ids',[[],["x' OR 1=1"],[key(1)]*101])
def test_invalid_identifiers_rejected_before_io(env,ids):
    assert not trend(ids)['complete']
    assert not env['sql'] and not env['model']


@pytest.mark.parametrize('field,value',[
    ('value','15'),('timestamp_gmt','2026-11-12T01:00:00Z'),('observation_id',key(123)),
    ('sequence',True),('has_active_exclusion_record',True)])
def test_chart_adapter_checks_points_against_digest_bound_originals(env,field,value):
    out=trend();out['points'][0][field]=value
    with pytest.raises(ValueError): charts.build('get_spc_parameter_trend',json.dumps(out))


def test_chart_adapter_rejects_digest_context_and_statistical_claims(env):
    original=trend()
    for update in [dict(source_history_digest='0'*64),dict(analysis_performed=True),dict(control_limits={}),
                   dict(native_spc_result=True),dict(complete=False),dict(source_kind='caller_synthetic'),
                   dict(context=dict(characteristic='other',unit='mm'))]:
        out=copy.deepcopy(original);out.update(update)
        try: result=charts.build('get_spc_parameter_trend',json.dumps(out))
        except ValueError: result=None
        assert result is None


def test_tool_registered_closed_schema_and_discovered_readonly(env):
    async def check():
        async with Client(mcp) as client:
            tools={tool.name:tool for tool in await client.list_tools()}
            schema=tools['get_spc_parameter_trend'].input_schema
            assert schema['required']==['history_ids']
            assert schema['properties']['history_ids']['maxItems']==100
            assert schema['additionalProperties'] is False
            out=await client.call_tool('get_spc_parameter_trend',{'history_ids':POINTS[:4],'scope_kind':'process'})
            assert json.loads(out.content[0].text)['complete']
    asyncio.run(check())
    assert TOOL_POLICIES['get_spc_parameter_trend'].effect=='read'
    assert get_tool_func('get_spc_parameter_trend') is history.get_spc_parameter_trend


def test_default_container_scope_refuses_mixing_and_binds_interval(env):
    assert not json.loads(asyncio.run(history.get_spc_parameter_trend(POINTS[:4])))['complete']
    for row in env['points']:
        row['ContainerId']=key(60)
    out=json.loads(asyncio.run(history.get_spc_parameter_trend(POINTS[:4])))
    assert out['complete'] and out['source_history']['scope_kind']=='container'
    assert out['source_history']['scope']['ContainerId']==key(60)
    interval=[s for s in env['sql'] if 'ORDER BY m.TxnDateGMT,d.DataPointHistoryDetailId' in s]
    assert interval and all('AND m.ContainerId='+history.q.literal(key(60)) in s for s in interval)


def test_changing_exclusion_records_refuses_a_mixed_snapshot(env,monkeypatch):
    env['annotations']=[note(POINTS[0])]
    original=history.active_annotations
    calls=0
    async def changing(*args,**kwargs):
        nonlocal calls
        calls+=1
        result=await original(*args,**kwargs)
        if calls>1: result[0]['Exclude']='true'
        return result
    monkeypatch.setattr(history,'active_annotations',changing)
    assert not trend()['complete']


def test_one_observation_is_a_valid_raw_trend_without_a_statistical_claim(env):
    env['points']=env['points'][:1];env['interval']=POINTS[:1]
    out=json.loads(asyncio.run(history.get_spc_parameter_trend(POINTS[:1])))
    assert out['complete'] and len(out['points'])==1 and not out['analysis_performed']
    assert charts.build('get_spc_parameter_trend',json.dumps(out))['panels'][0]['rows'][0]['value']=='9'


def test_trend_question_publishes_scoped_snapshot_with_existing_async_protocol(env,tmp_path,monkeypatch):
    monkeypatch.setattr(charts,'ENABLE_CHAT_CHARTS',True)
    monkeypatch.setattr(charts,'STORE',charts.ChartStore(tmp_path/'trend.db'))
    descriptor=asyncio.run(charts.publish_async('u','s','trend-call','生成参数走势图','get_spc_parameter_trend',json.dumps(trend())))
    assert descriptor and 'panels' not in descriptor
    assert charts.STORE.get('u','s',descriptor['id'])['panels'][0]['kind']=='trend'


@pytest.mark.parametrize('question',['请把采集参数Diameter的历史画成图','测量参数的趋势图','生成参数走势图'])
def test_plain_parameter_chart_request_routes_to_readonly_source_and_trend(question):
    catalog=[{'function':{'name':name}} for name in TOOL_POLICIES]
    names={t['function']['name'] for t in select_tools(catalog,[dict(role='user',content=question)])}
    assert {'get_spc_parameter_trend','get_container_collection_history'}<=names
    assert 'execute_quality_transaction' not in names
    assert 'get_spc_parameter_trend' in ROUTING_INSTRUCTION
