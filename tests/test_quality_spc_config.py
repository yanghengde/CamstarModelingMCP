import asyncio
import copy
import json
import re

import pytest

from tools import quality_spc_config as config, quality_spc_history as history


def key(number):
    return f'{number:016x}'


CHART, TYPE, RULE, QUERY, CONNECTION, MEMBER, VIOLATION = map(key, range(1, 8))


@pytest.fixture
def env(monkeypatch):
    chart = {'SPCChartDefId': CHART, 'SPCChartDefName': "Line A's diameter", 'ChangeCount': '1',
             'SPCChartTypeId': TYPE, 'SPCRulesId': RULE, 'SPCQueryId': QUERY, 'SPCConnectionId': CONNECTION}
    chart.update({flag: 'false' for flag in config.CHART_FLAGS})
    chart.update({flag: '0' for flag in config.CHART_PRESENCE_FLAGS})
    frozen = {'IsFrozen': 'false', 'ChangeCount': '2'}
    state = {
        'SPCChartDef': [chart], 'A_SPCChart': [frozen | {'SPCChartId': TYPE}],
        'A_SPCRules': [frozen | {'SPCRulesId': RULE}],
        'UserQuery': [frozen | {'UserQueryId': QUERY, 'CDOTypeId': str(config.SPC_QUERY_CDO_TYPE)}],
        'A_SPCConnection': [frozen | {'SPCConnectionId': CONNECTION, 'EndpointConfigured': '1'}],
        'SPCChartParams': [frozen | {'SPCChartParamsId': key(50), 'ParentId': CHART, 'IsDynamic': 'false'}],
        'SPCChartQueryParams': [frozen | {'SPCChartQueryParamsId': key(51), 'ParentId': CHART, 'DynamicConfigured': '0'}],
        'SPCRuleViolations': [frozen | {'SPCRulesViolationsId': MEMBER, 'ParentId': RULE, 'SPCViolationId': VIOLATION,
            'SPCRuleViolationId': VIOLATION, 'ViolationChangeCount': '3', 'ViolationFrozen': 'false'}],
        'DataPointHistoryDetail': [],
    }
    queries = []

    async def sql(query, maximum):
        queries.append(query)
        assert query.startswith('SELECT TOP ')
        assert not re.search(r'\b(?:Username|Password|DSN|QueryText|ParamValue|DefaultValue)\b', query, re.I)
        table = re.search(r'\bFROM (\w+)', query).group(1)
        rows = copy.deepcopy(state[table])
        if maximum == 1:
            field = query.split()[3]
            return [{field: rows[0][field]}] if rows else []
        if state.get('changed') == table and sum(f'FROM {table} ' in sql for sql in queries) == 2:
            rows[0]['ChangeCount'] = '100'
        return rows

    monkeypatch.setattr(config.q, 'sql_rows', sql)
    return state, queries


def readiness(name=None, chart_id=CHART):
    return asyncio.run(config.chart_readiness(name, chart_id))


def test_named_chart_snapshots_fixed_graph_without_connection_values(env):
    state, queries = env
    result = readiness(state['SPCChartDef'][0]['SPCChartDefName'], None)
    assert result['state'] == 'observed' and result['complete'] and not result['blockers']
    assert result['chart']['id'] == CHART
    assert result['dependencies']['query']['change_count'] == 2
    assert result['dependencies']['connection']['endpoint_configured']
    assert result['members']['rule_members'][0]['violation_reference_resolved']
    assert result['configuration_digest'] == config.digest({field: result[field] for field in ('chart','dependencies','members')})
    assert not result['native_engine_result_verified'] and not result['ready_for_native_execution']
    assert sum("SPCChartDefName=N'Line A''s diameter'" in query for query in queries) == 2


@pytest.mark.parametrize(('table','field','value','blocker'), [
    ('SPCChartDef','SPCChartTypeId','','chart_type_not_configured'),
    ('A_SPCConnection','EndpointConfigured','0','connection_endpoint_not_configured'),
    ('A_SPCRules','IsFrozen','true','rules_frozen_or_unknown'),
    ('SPCChartDef','RequireAnnotation','','side_effect_or_annotation_review_required'),
    ('SPCChartDef','HoldConfigured','1','side_effect_or_annotation_review_required'),
    ('SPCChartDef','RecordAgainstCustomSPCDataSrc','true','custom_data_source_or_unknown'),
    ('SPCChartQueryParams','DynamicConfigured','1','dynamic_parameters_require_runtime_resolution'),
    ('SPCChartParams','IsDynamic','','dynamic_parameters_require_runtime_resolution'),
    ('SPCRuleViolations','SPCRuleViolationId','','violation_reference_missing_frozen_or_unknown'),
    ('UserQuery','CDOTypeId','10','query_wrong_type'),
])
def test_graph_surfaces_specific_blockers(env, table, field, value, blocker):
    env[0][table][0][field] = value
    assert blocker in readiness()['blockers']


@pytest.mark.parametrize('table', ['SPCChartDef', 'UserQuery', 'SPCRuleViolations'])
def test_readback_rejects_changed_graph(env, table):
    env[0]['changed'] = table
    with pytest.raises(ValueError, match='发生变化'):
        readiness()


def test_invalid_identity_and_ambiguous_members_fail_closed(env):
    with pytest.raises(ValueError):
        readiness(chart_id="bad' ID")
    assert not env[1]
    with pytest.raises(ValueError):
        readiness('Another chart')
    env[0]['SPCChartParams'] *= 2
    with pytest.raises(ValueError, match='重复'):
        readiness()


def test_missing_reference_is_distinct_from_missing_chart(env):
    state, _ = env
    state['A_SPCConnection'] = []
    assert readiness()['dependencies']['connection'] == {'id': CONNECTION, 'state': 'missing_reference'}
    state['SPCChartDef'] = []
    assert readiness()['state'] == 'not_found'


def test_readiness_tool_exposes_graph_without_opening_execution(env, monkeypatch):
    async def model_presence(entity):
        return {'state': 'empty' if entity == 'SPCQueries' else 'present'}
    monkeypatch.setattr(history,'model_presence',model_presence)
    out = json.loads(asyncio.run(history.get_spc_native_readiness(spc_chart_instance_id=CHART)))
    assert out['complete'] and out['chart_configuration']['state'] == 'observed'
    assert out['checks']['SPCConnection']['state'] == 'present'
    assert not out['ready_for_native_execution'] and not out['release_authorized']
