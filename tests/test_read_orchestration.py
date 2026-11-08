import asyncio
import copy
import json
from pathlib import Path
import time
from unittest.mock import AsyncMock

import pytest

from core import read_catalog as c, read_plan as p
from tools import read_orchestration as t, read_templates as templates

CID, OID, OTHER = [f'{n:016x}' for n in range(1,4)]

def run(coro):
    return asyncio.run(coro)

def result(coro):
    return json.loads(run(coro))

@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(c, 'CACHE_DB', tmp_path / 'metadata.sqlite3')
    monkeypatch.setattr(c, '_locks', {})

def metadata(name='containers'):
    definition=c.DATASETS[name]
    return {'content': {'table':definition['table'],'schema':'dbo','key':definition['key'],
        'columns':{f:{'DATA_TYPE':'nvarchar'} for f in definition['fields']}},
        'cached':True,'schema_digest':'hash'}

def dataset(id='containers', **extra):
    return {'id':id,'op':'dataset','dataset':'containers','fields':['ContainerName','MfgOrderId'],**extra}

def plan(steps, output=None, **extra):
    return {'version':1,'steps':steps,'output':output if output is not None else {'$ref':steps[-1]['id']+'#/data'},**extra}

def mocks(monkeypatch, values=None):
    monkeypatch.setattr(c,'dataset_metadata',AsyncMock(return_value=metadata()))
    query=AsyncMock(return_value=values if values is not None else [])
    monkeypatch.setattr(c,'sql_rows',query)
    return query

@pytest.mark.parametrize('tool',['update_mfgorder','prepare_container_throughput','execute_adhoc_query',
    'execute_query_inquiry','execute_camstar_read_plan','get_camstar_read_metadata','execute_production_station_transition'])
def test_all_steps_validated_before_any_reads_or_forbidden_calls(monkeypatch,tool):
    query=mocks(monkeypatch)
    answer=result(t.execute_camstar_read_plan(plan([dataset(),{'id':'bad','op':'call','tool':tool,'arguments':{}}])))
    assert answer['complete'] is False and answer['stage']=='plan_validation'
    query.assert_not_awaited()

@pytest.mark.parametrize('bad',[
    [{'id':'a','op':'select','source':{'$ref':'missing#/data'},'fields':{'x':'/X'}}],
    [{'id':'a','op':'select','source':{'$ref':'b#/data'},'fields':{'x':'/X'}},
     {'id':'b','op':'select','source':{'$ref':'a#/data'},'fields':{'x':'/X'}}],
    [dataset('a'),dataset('a')],
    [{'id':'a','op':'python','code':'print(1)'}],
    [dataset(extra='unknown')],
])
def test_invalid_graph_never_reaches_mes(monkeypatch,bad):
    query=mocks(monkeypatch)
    assert result(t.execute_camstar_read_plan(plan(bad)))['complete'] is False
    query.assert_not_awaited()

def test_structured_dataset_quotes_values_and_identifiers(monkeypatch):
    query=mocks(monkeypatch,[{'ContainerName':'C','MfgOrderId':OID,'ContainerId':CID}])
    answer=run(p.run(plan([dataset(filters=[{'field':'ContainerName','operator':'eq',
        'value':"C'; DROP TABLE Container;--"}])])))
    assert answer['complete'] is True
    sql=query.await_args.args[0]
    assert "[ContainerName]=N'C''; DROP TABLE Container;--'" in sql
    assert 'OFFSET 0 ROWS FETCH NEXT 101 ROWS ONLY' in sql
    assert answer['output'][0]['ContainerId']==CID
    assert 'data' not in answer['steps']['containers']

@pytest.mark.parametrize('changes',[
    {'fields':['Password']},{'fields':['ContainerName;DROP']},
    {'order_by':'Qty DESC;DROP'}, {'filters':[{'field':'BadField','value':'x'}]},
    {'filters':[{'field':'ContainerName','operator':'in','value':[None]}]},
    {'filters':[{'field':'ContainerName','operator':'is_null','value':'true'}]},
])
def test_unknown_fields_or_invalid_filters_fail_closed(monkeypatch,changes):
    query=mocks(monkeypatch)
    answer=result(t.execute_camstar_read_plan(plan([dataset(**changes)])))
    assert answer['complete'] is False and answer.get('output') is None
    query.assert_not_awaited()

def test_pagination_is_explicit_and_all_closed_states_are_retained(monkeypatch):
    monkeypatch.setattr(c,'dataset_metadata',AsyncMock(return_value=metadata()))
    a={'ContainerName':'A','MfgOrderId':OID,'ContainerId':CID}
    b={'ContainerName':'B','MfgOrderId':OID,'ContainerId':OTHER}
    query=AsyncMock(side_effect=[[a,b],[b]])
    monkeypatch.setattr(c,'sql_rows',query)
    answer=run(p.run(plan([dataset(limit=1,max_pages=2)])))
    assert answer['complete'] and len(answer['output'])==2
    assert answer['metrics']['dataset_queries']==2
    assert 'Status=' not in query.await_args_list[0].args[0]
    assert 'OFFSET 1 ROWS' in query.await_args_list[1].args[0]

def test_incomplete_page_cannot_be_silently_aggregated(monkeypatch):
    a={'ContainerName':'A','MfgOrderId':OID,'ContainerId':CID}
    b={**a,'ContainerId':OTHER}
    mocks(monkeypatch,[a,b])
    answer=run(p.run(plan([dataset(limit=1),{'id':'counts','op':'group',
        'source':{'$ref':'containers#/data'},'keys':['MfgOrderId'],'metrics':{'n':{'op':'count'}}}])))
    assert not answer['complete']
    assert answer['steps']['containers']['coverage']=='partial'
    assert answer['steps']['counts']['status']=='error'
    assert answer['output'] is None

def test_duplicate_page_ids_do_not_claim_full_coverage(monkeypatch):
    monkeypatch.setattr(c,'dataset_metadata',AsyncMock(return_value=metadata()))
    a={'ContainerName':'A','MfgOrderId':OID,'ContainerId':CID}
    monkeypatch.setattr(c,'sql_rows',AsyncMock(side_effect=[[a,a],[a]]))
    assert not run(p.run(plan([dataset(limit=1,max_pages=2)])))['complete']

def test_dependency_reference_compiles_second_filter_without_model_round(monkeypatch):
    async def meta(name,refresh=False):return metadata(name)
    monkeypatch.setattr(c,'dataset_metadata',meta)
    async def query(sql):
        if '[MfgOrder]' in sql:return [{'MfgOrderName':'MO','MfgOrderId':OID}]
        assert f"[MfgOrderId] IN (N'{OID}')" in sql
        return [{'ContainerName':'C','MfgOrderId':OID,'ContainerId':CID}]
    monkeypatch.setattr(c,'sql_rows',query)
    answer=run(p.run(plan([
        {'id':'orders','op':'dataset','dataset':'orders','fields':['MfgOrderName']},
        dataset(filters=[{'field':'MfgOrderId','operator':'in','value':{'$ref':'orders#/data','column':'MfgOrderId'}}])])))
    assert answer['complete'] and answer['metrics']['model_rounds_inside_executor']==0
    assert answer['metrics']['dataset_queries']==2

def test_failure_skips_dependents_and_does_not_retry(monkeypatch):
    query=mocks(monkeypatch);query.side_effect=ValueError('MES unavailable')
    answer=run(p.run(plan([dataset(),{'id':'after','op':'select','source':{'$ref':'containers#/data'},'fields':{'name':'/ContainerName'}}])))
    assert not answer['complete'] and answer['steps']['after']['status']=='skipped'
    assert query.await_count==1

def test_concurrency_is_bounded_and_independent_reads_overlap(monkeypatch):
    active=peak=0;entered=asyncio.Event()
    monkeypatch.setattr(c,'dataset_metadata',AsyncMock(return_value=metadata()))
    async def query(sql):
        nonlocal active,peak
        active+=1;peak=max(peak,active)
        if active==2:entered.set()
        await asyncio.wait_for(entered.wait(),1)
        await asyncio.sleep(0)
        active-=1
        return []
    monkeypatch.setattr(c,'sql_rows',query)
    answer=run(p.run(plan([dataset('a'),dataset('b'),dataset('d')],concurrency=2)))
    assert answer['complete'] and peak==2

def test_timeout_returns_unknown_not_zero(monkeypatch):
    mocks(monkeypatch)
    async def slow(sql):await asyncio.sleep(5)
    monkeypatch.setattr(c,'sql_rows',slow)
    answer=run(p.run(plan([dataset()],timeout_seconds=1)))
    assert not answer['complete'] and answer['steps']['containers']['status']=='timeout'
    assert answer['output'] is None

def test_group_numeric_precision_missing_values_and_utc_ordering():
    s=p.GroupStep.model_validate({'id':'g','op':'group','source':[],'keys':['order'],
        'metrics':{'qty':{'op':'sum','field':'q'},'first':{'op':'min','field':'date','kind':'utc_timestamp'}}})
    values=[{'order':'MO','q':'0.1','date':'2026-11-08T01:00:00.1234567Z'},
            {'order':'MO','q':'0.2','date':'2026-11-08T09:00:00.1234566+08:00'},
            {'order':'MO','q':None,'date':''}]
    row=p.group_rows(s,values)[0]
    assert row['qty']=='0.3' and row['qty_missing_count']==1
    assert row['first']=='2026-11-08T09:00:00.1234566+08:00' and row['first_missing_count']==1


def test_group_preserves_more_than_default_decimal_context_precision():
    step=p.GroupStep(id='g',op='group',source=[],keys=['key'],
        metrics={'q':p.Metric(op='sum',field='qty')})
    row=p.group_rows(step,[{'key':'MO','qty':'123456789012345678901234567890.12345678'},
                           {'key':'MO','qty':'0.00000001'}])[0]
    assert row['q']=='123456789012345678901234567890.12345679'


def test_unzoned_local_dates_cannot_be_silently_assumed_utc():
    with pytest.raises(ValueError,match='时区'):
        p.compare_value('2026-11-08T09:00:00','utc_timestamp','FactoryStartDate')

def test_join_does_not_match_null_keys_or_silently_overwrite():
    s=p.JoinStep(id='j',op='join',left=[],right=[],left_key='id',right_key='id')
    answer=p.join_rows(s,[{'id':None}],[{'id':None,'name':'wrong'}])
    assert answer[0]['related_matched'] is False and 'related_name' not in answer[0]
    with pytest.raises(ValueError):
        p.join_rows(s,[{'id':1,'related_name':'old'}],[{'id':1,'name':'new'}])

def test_tool_foreach_reuses_existing_functions_with_validated_arguments(monkeypatch):
    import tools
    monkeypatch.setattr(p,'tool_schemas',AsyncMock(return_value={'get_container_details':{
        'type':'object','properties':{'container_name':{'type':'string'}},'required':['container_name'],'additionalProperties':False}}))
    calls=[]
    async def lookup(**args):
        calls.append(args);return json.dumps({'complete':True,'name':args['container_name']})
    monkeypatch.setattr(tools,'get_tool_func',lambda n:lookup)
    answer=run(p.run(plan([{'id':'x','op':'call','tool':'get_container_details','foreach':[{'name':'A'},{'name':'B'}],
        'arguments':{'container_name':{'$item':'/name'}}}])))
    assert answer['complete'] and answer['metrics']['tool_calls']==2 and len(calls)==2

def test_metadata_cache_environment_ttl_corruption_and_failed_refresh(monkeypatch):
    loader=AsyncMock(return_value={'column':'one'})
    first=run(c.cached('schema',loader));second=run(c.cached('schema',loader))
    assert not first['cached'] and second['cached'] and loader.await_count==1
    monkeypatch.setattr(c,'CAMSTAR_QUERY_BASE_URL','https://another/Query')
    assert not run(c.cached('schema',loader))['cached'] and loader.await_count==2
    with c.connect() as db:
        db.execute('UPDATE metadata SET fetched=?',(time.time()-c.CACHE_TTL-1,))
    loader.side_effect=ValueError('server unavailable')
    with pytest.raises(ValueError,match='unavailable'):run(c.cached('schema',loader))
    with c.connect() as db:
        db.execute('UPDATE metadata SET fetched=?,digest=?',(time.time(),'broken'))
    with pytest.raises(ValueError,match='摘要'):run(c.cached('schema',loader))

def test_metadata_loader_is_singleflight(monkeypatch):
    n=0
    async def loader():
        nonlocal n
        n+=1;await asyncio.sleep(0);return {'a':1}
    async def together():return await asyncio.gather(c.cached('one',loader),c.cached('one',loader))
    values=run(together())
    assert n==1 and sum(v['cached'] for v in values)==1

def test_dataset_metadata_does_not_guess_missing_fields_or_schema(monkeypatch):
    query=AsyncMock(return_value=[
        {'TABLE_SCHEMA':'dbo','TABLE_NAME':'Container','COLUMN_NAME':'ContainerId','DATA_TYPE':'varchar'},
        {'TABLE_SCHEMA':'dbo','TABLE_NAME':'Container','COLUMN_NAME':'ContainerName','DATA_TYPE':'nvarchar'}])
    monkeypatch.setattr(c,'sql_rows',query)
    meta=run(c.dataset_metadata('containers'))['content']
    assert 'FactoryStartDateGMT' in meta['unavailable_reviewed_fields']
    query.return_value=[{'TABLE_SCHEMA':'other','TABLE_NAME':'Container','COLUMN_NAME':'ContainerId'},
                        {'TABLE_SCHEMA':'dbo','TABLE_NAME':'Container','COLUMN_NAME':'ContainerId'}]
    with pytest.raises(ValueError,match='schema'):run(c.dataset_metadata('containers',True))


def test_read_tool_arguments_with_invalid_literal_type_fail_before_mes(monkeypatch):
    query=mocks(monkeypatch)
    answer=result(t.execute_camstar_read_plan(plan([dataset(),{
        'id':'bad','op':'call','tool':'get_container_details','arguments':{'container_name':42}}])))
    assert not answer['complete'] and answer['stage']=='plan_validation'
    query.assert_not_awaited()


def test_literal_fabricated_business_output_and_sources_are_rejected(monkeypatch):
    query=mocks(monkeypatch)
    assert not result(t.execute_camstar_read_plan(plan([dataset()],output={'result':'all done'})))['complete']
    assert not result(t.execute_camstar_read_plan(plan([dataset(),{'id':'fake','op':'group',
        'source':[{'Qty':100,'Order':'MO'}],'keys':['Order'],'metrics':{'qty':{'op':'sum','field':'Qty'}}}])))['complete']
    query.assert_not_awaited()


def test_missing_data_type_and_invalid_date_do_not_silently_aggregate(monkeypatch):
    _,container,query=template_mock(monkeypatch)
    container['FactoryStartDateGMT']='not-a-date'
    answer=run(templates.order_dates({'order_names':['TMO']}))
    assert not answer['complete'] and answer['rows']==[]


def test_inprocess_mcp_transport_executes_new_typed_plan_without_restart(monkeypatch):
    from tools import mcp
    from fastapi.testclient import TestClient
    template_mock(monkeypatch)
    app=mcp.http_app(path='/',transport='streamable-http',stateless_http=True,json_response=True,
                     allowed_hosts=['testserver:*'])
    headers={'Accept':'application/json, text/event-stream','Content-Type':'application/json'}
    with TestClient(app) as client:
        init=client.post('/',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize',
            'params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
        assert init.status_code==200
        headers['MCP-Protocol-Version']=init.json()['result']['protocolVersion']
        client.post('/',headers=headers,json={'jsonrpc':'2.0','method':'notifications/initialized'})
        response=client.post('/',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/call',
            'params':{'name':'execute_camstar_read_plan','arguments':{'plan':templates.order_dates_plan(['TMO'])}}})
        assert response.status_code==200 and not response.json()['result'].get('isError')
        value=json.loads(response.json()['result']['content'][0]['text'])
        assert value['complete'] and value['output'][0]['actual_container_count']==1
        listing=client.post('/',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/list','params':{}})
        names={tool['name'] for tool in listing.json()['result']['tools']}
        assert {'execute_camstar_read_plan','run_camstar_read_template'}<=names

def template_mock(monkeypatch):
    async def meta(name,refresh=False):return metadata(name)
    monkeypatch.setattr(c,'dataset_metadata',meta)
    order={'MfgOrderId':OID,'MfgOrderName':'TMO','PlannedStartDateGMT':'',
           'ProductionStartDateGMT':'','ProductionCompleteDateGMT':''}
    container={'ContainerId':CID,'ContainerName':'A01','MfgOrderId':OID,'Status':'2',
               'FactoryStartDateGMT':'2026-11-04T14:50:48.337'}
    async def query(sql):return [copy.deepcopy(order)] if '[MfgOrder]' in sql else [copy.deepcopy(container)]
    mock=AsyncMock(side_effect=query);monkeypatch.setattr(c,'sql_rows',mock)
    return order,container,mock

def test_order_dates_regression_includes_closed_tmo_and_preserves_milliseconds(monkeypatch):
    _,_,query=template_mock(monkeypatch)
    answer=run(templates.order_dates({'order_names':['TMO','MISSING']}))
    assert answer['complete'] and not answer['all_requested_orders_found']
    row=answer['rows'][0]
    assert row['container_count']==1 and row['actual_first_container_start']['display']=='2026-11-04T22:50:48.337+08:00'
    assert row['native_production_start']['display'] is None
    assert answer['rows'][1]['status']=='not_found'
    assert all('Status=' not in call.args[0] for call in query.await_args_list)

def test_empty_container_query_is_distinct_from_empty_factory_date(monkeypatch):
    _,container,query=template_mock(monkeypatch)
    container['FactoryStartDateGMT']=''
    row=run(templates.order_dates({'order_names':['TMO']}))['rows'][0]
    assert row['actual_start_state']=='all_container_dates_missing' and row['container_count']==1
    async def empty(sql):
        if '[MfgOrder]' in sql:return [{'MfgOrderId':OID,'MfgOrderName':'TMO','PlannedStartDateGMT':'',
            'ProductionStartDateGMT':'','ProductionCompleteDateGMT':''}]
        return []
    query.side_effect=empty
    row=run(templates.order_dates({'order_names':['TMO']}))['rows'][0]
    assert row['actual_start_state']=='no_current_linked_containers' and row['container_count']==0

def test_utc_display_preserves_seventh_digit_and_rejects_wrong_gmt_offset():
    from zoneinfo import ZoneInfo
    assert templates.display_gmt('2026-11-04T14:50:48.1234567',ZoneInfo('Asia/Shanghai')).endswith('22:50:48.1234567+08:00')
    with pytest.raises(ValueError):templates.display_gmt('2026-11-04T14:50:48+08:00',ZoneInfo('UTC'))

@pytest.mark.parametrize('params',[{'order_names':[]},{'order_names':['MO','MO']},
    {'order_names':['MO'],'sql':'SELECT 1'},{'order_names':['MO'],'timezone':'bad-zone'}])
def test_bad_template_parameters_do_not_read(monkeypatch,params):
    query=mocks(monkeypatch)
    assert not result(t.run_camstar_read_template('order_dates',params))['complete']
    query.assert_not_awaited()

def test_registry_schema_discovery_and_routing_have_no_external_network(monkeypatch):
    from tools import mcp,get_tool_func
    from core.tool_catalog import TOOL_POLICIES
    from agent.tool_routing import select_tools
    names={x.name for x in run(mcp.list_tools())}
    for name in ('search_camstar_read_tools','get_camstar_read_metadata','execute_camstar_read_plan','run_camstar_read_template'):
        assert name in names and get_tool_func(name) and TOOL_POLICIES[name].effect=='read'
    overview=result(t.get_camstar_read_metadata())
    assert overview['complete'] and 'plan_schema' in overview['metadata']
    search=result(t.search_camstar_read_tools('工单日期'))
    assert search['complete'] and search['tools']
    selected=select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],
        [{'role':'user','content':'这5张工单的开工日期是多少'}])
    assert 'run_camstar_read_template' in {x['function']['name'] for x in selected}


def test_external_client_sees_complete_template_arguments_without_schema_exploration():
    from tools import mcp
    from jsonschema import Draft202012Validator, ValidationError
    schema=next(tool.parameters for tool in run(mcp.list_tools()) if tool.name=='run_camstar_read_template')
    validator=Draft202012Validator(schema)
    validator.validate({'template':'order_dates','parameters':{'order_names':['TMO']}})
    for args in ({'template':'order_dates','parameters':{}},
                 {'template':'order_dates','parameters':{'order_names':['TMO'],'sql':'SELECT 1'}}):
        with pytest.raises(ValidationError):validator.validate(args)


def test_typed_template_arguments_execute_the_same_existing_recipe(monkeypatch):
    template_mock(monkeypatch)
    answer=result(t.run_camstar_read_template('order_dates',t.OrderDatesParameters(order_names=['TMO'])))
    assert answer['complete'] and answer['rows'][0]['container_count']==1
