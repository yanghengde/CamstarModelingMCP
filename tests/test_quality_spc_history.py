import asyncio
import copy
import json

import pytest
from fastmcp import Client

from agent.tool_routing import select_tools
from core.tool_catalog import TOOL_POLICIES
from tools import get_tool_func, mcp, process_context, quality_spc as spc, quality_spc_history as history


def key(value):
    return f'{value:016x}'


POINTS = [key(n) for n in range(1, 7)]
UOM = key(100)
RULES = ['beyond_3sigma']
CONTROLS = spc.SPCFixedControls(reference='QC-1', version='A', subgroup_size=1, center='10', sigma='1',
    dispersion_center='1', dispersion_lcl='0', dispersion_ucl='10')


def observation(index):
    return {
        'DataPointHistoryDetailId': POINTS[index], 'DataPointHistoryId': key(30+index),
        'DataCollectionDefId': key(50), 'DataPointId': key(51), 'UOMId': UOM,
        'DataName': 'Diameter', 'DataType': '9', 'DataValue': ['9', '10', '11', '14', '15', '16'][index],
        'HistoryId': key(60+index), 'TxnId': key(70+index), 'ContainerId': key(60+index),
        'ParentDefinitionId': key(50), 'ParentHistoryId': key(60+index), 'ParentTxnId': key(70+index),
        'HistoryMainlineId': key(80+index), 'MainlineHistoryId': key(60+index), 'MainlineTxnId': key(70+index),
        'ChangeCount': '1', 'ParentChangeCount': '1', 'MainlineChangeCount': '1', 'ReversalStatus': '1',
        'TxnDateGMT': f'2026-11-04T08:00:00.000000{index+1}', 'ProductId': key(91),
        'OperationId': key(92), 'SpecId': key(93), 'WorkflowStepId': key(94), 'FactoryId': key(95),
        'ResourceId': '',
    }


def note(point, exclude=False):
    return {'AddSPCAnnotationHistoryId': key(500), 'DataPointId': point, 'HistoryId': point,
            'MainlineHistoryId': point, 'HistoryMainlineId': key(501), 'TxnId': key(502), 'MainlineTxnId': key(502),
            'ReversalStatus': '1', 'TxnType': '8393', 'Exclude': str(exclude).lower()}


@pytest.fixture
def env(monkeypatch):
    state = {'points': [observation(i) for i in range(4)], 'interval': POINTS[:4],
             'annotations': [], 'changes_on_readback': None, 'sql': [], 'model': []}

    async def sql(query, maximum):
        state['sql'].append(query)
        assert query.startswith('SELECT ')
        if 'INFORMATION_SCHEMA' in query:
            assert 'OBJECT_SCHEMA_NAME(OBJECT_ID(TABLE_NAME))' in query
            return [{'TABLE_NAME': table, 'COLUMN_NAME': field}
                    for table, fields in history.SCHEMA.items() for field in fields.split(',')]
        if 'SELECT TOP 1 DataPointHistoryDetailId FROM DataPointHistoryDetail' in query:
            return [{'DataPointHistoryDetailId': POINTS[0]}] if state['points'] else []
        if query == 'SELECT TOP 1 SPCConnectionId FROM A_SPCConnection':
            return []
        if 'FROM AddSPCAnnotationHistory' in query:
            return copy.deepcopy(state['annotations'])
        if 'WHERE d.DataPointHistoryDetailId IN (' in query:
            # Return SQL rows in reverse order; request order is bound to GMT.
            rows = [r for r in reversed(state['points']) if r["DataPointHistoryDetailId"] in query]
            if state['changes_on_readback'] and sum('WHERE d.DataPointHistoryDetailId IN (' in s for s in state['sql']) > 1:
                rows = copy.deepcopy(rows)
                rows[0].update(state['changes_on_readback'])
            return copy.deepcopy(rows)
        assert query.count('(SELECT TxnDateGMT FROM HistoryMainline WHERE HistoryMainlineId=') == 2
        assert 'm.SpecId=' in query and "NULLIF(m.ResourceId,'') IS NULL" in query
        assert 'ReversalStatus=' not in query
        return [{'DataPointHistoryDetailId': point} for point in state['interval']]

    async def get(method, path, **kwargs):
        assert method == 'GET'
        state['model'].append(path)
        if path == '/api/UOMs/'+UOM:
            return json.dumps({'InstanceID': UOM, 'Name': 'mm'})
        assert kwargs['params'] == {'$select': 'InstanceID', '$top': 1}
        if path == '/api/SPCRules':
            return 'invalid response'
        return json.dumps({'value': [{'InstanceID': key(98)}] if path == '/api/SPCChartDefs' else []})

    monkeypatch.setattr(history.q, 'sql_rows', sql)
    monkeypatch.setattr(process_context, 'request', get)
    return state


def analyze(groups=None, **overrides):
    args = dict(chart_type='i_mr', groups=[[key] for key in POINTS[:4]] if groups is None else groups,
                rules=RULES, controls=CONTROLS)
    args.update(overrides)
    return json.loads(asyncio.run(history.analyze_spc_history(**args)))


def test_history_local_analysis_preserves_exact_source_and_seventh_digit(env):
    env['annotations'] = [note(POINTS[1])]
    out = analyze(controls=CONTROLS.model_dump())
    assert out['data_verified_against_mes'] and out['source_window_verified']
    assert not out['configuration_verified_against_mes'] and not out['native_spc_result']
    assert not out['ready_for_mes_write'] and not out['release_authorized']
    assert out['source_history']['active_annotation_ids'] == [key(500)]
    assert [row['DataPointHistoryDetailId'] for row in out['source_history']['points']] == POINTS[:4]
    assert out['source_history_digest'] == history.digest(out['source_history'])
    assert out['primary_signals'][0]['contributing_ids'] == [POINTS[3]]


def test_independent_persisted_baseline_and_explicit_xbar_groups(env):
    env['points'] = [observation(i) for i in range(6)]
    env['interval'] = POINTS
    out = analyze([POINTS[4:6]], chart_type='xbar_r', baseline_groups=[POINTS[:2], POINTS[2:4]], controls=None)
    assert out['source_history']['baseline_group_count'] == 2
    assert out['control_origin'] == 'separate_baseline_estimate'
    assert out['points'] == [{'observation_id': POINTS[4], 'sequence': 3, 'values': ['15','16'], 'statistic': '15.5'}]
    assert out['primary_signals'][0]['contributing_ids'] == [POINTS[4]]
    assert not out['subgroup_assignment_verified']


@pytest.mark.parametrize(('field','bad'), [
    ('DataCollectionDefId', key(101)), ('ParentTxnId', key(101)), ('ReversalStatus', '2'),
    ('UOMId', key(101)), ('OperationId', key(101)), ('ResourceId', key(101)), ('FactoryId', key(101)),
    ('DataType', '7'), ('DataValue', '1e9'), ('TxnDateGMT', '2026-11-04T08:00:00+08:00'),
])
def test_history_rejects_mixed_scope_or_unusable_original_record(env, field, bad):
    env['points'][2][field] = bad
    assert analyze()['complete'] is False


@pytest.mark.parametrize('failure', ['gap', 'duplicate', 'wrong_order', 'same_time', 'excluded', 'orphan_annotation', 'changed'])
def test_history_refuses_gaps_and_changing_or_excluded_records(env, failure):
    groups = [[value] for value in POINTS[:4]]
    if failure == 'gap':
        groups.pop(1)
    elif failure == 'duplicate':
        groups[1] = groups[0]
    elif failure == 'wrong_order':
        groups.reverse()
    elif failure == 'same_time':
        env['points'][2]['TxnDateGMT'] = env['points'][1]['TxnDateGMT']
    elif failure == 'excluded':
        env['annotations'] = [note(POINTS[1], True)]
    elif failure == 'orphan_annotation':
        env['annotations'] = [note(POINTS[1]) | {'MainlineTxnId': ''}]
    else:
        env['changes_on_readback'] = {'DataValue': '12', 'ChangeCount': '2'}
    assert analyze(groups)['complete'] is False


def test_history_id_validation_before_any_query(env):
    assert analyze([["x' OR 1=1"]])['complete'] is False
    assert analyze([[key(n) for n in range(10*i,10*i+10)] for i in range(11)], chart_type='xbar_r')['complete'] is False
    assert not env['sql']


def test_readiness_keeps_empty_and_unknown_separate(env):
    out = json.loads(asyncio.run(history.get_spc_native_readiness()))
    assert out['checks']['SPCChartDefs']['state'] == 'present'
    assert out['checks']['DataPointHistoryDetail']['state'] == 'present'
    assert set(out['empty']) == {'SPCChartTypes', 'SPCQueries', 'SPCConnection'}
    assert out['unavailable'] == ['SPCRules'] and not out['complete']
    assert not out['ready_for_native_execution'] and not out['native_engine_result_verified']


def test_both_tools_are_read_only_and_available_through_mcp(env):
    async def check():
        async with Client(mcp) as client:
            tools = {tool.name for tool in await client.list_tools()}
            for name in ('analyze_spc_history','get_spc_native_readiness'):
                assert name in tools and TOOL_POLICIES[name].effect == 'read'
                assert get_tool_func(name) is getattr(history, name)
            answer = await client.call_tool('analyze_spc_history', {
                'chart_type': 'i_mr', 'groups': [[value] for value in POINTS[:4]], 'rules': RULES,
                'controls': CONTROLS.model_dump()})
            assert json.loads(answer.content[0].text)['source_window_verified']
    asyncio.run(check())


def test_spc_history_routing_includes_original_source_lookup():
    catalog = [{'function': {'name': name}} for name in TOOL_POLICIES]
    selected = select_tools(catalog, [{'role': 'user', 'content': '根据 MES 历史做 SPC 判异'}])
    names = {tool['function']['name'] for tool in selected}
    assert {'analyze_spc_history', 'get_spc_native_readiness', 'get_container_collection_history'} <= names
    assert 'execute_quality_transaction' not in names
