import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from tools import mcp, get_tool_func, quality_configuration as a
from tools import quality_definitions as d, quality_sampling_references as s, quality_plans as p
from tools.process_context import ReadContext
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION


def key(n): return f'{n:016x}'
PLAN, TEST, POINT, AQL, LEVEL, DETAIL, AROW, LROW = [key(n) for n in range(1, 9)]
def result(coro): return json.loads(asyncio.run(coro))
def ref(row): return {k: row[k] for k in ('Name', 'Revision', 'InstanceID') if k in row}
def root(name, iid, **kw):
    return dict(Name=name, InstanceID=iid, Revision='A', IsRevOfRcd=True, IsFrozen=False, Status=1, Description='', **kw)


@pytest.fixture
def env(monkeypatch, tmp_path):
    point = root('Length', POINT, DataType=9, LowerLimit='0', UpperLimit='10', DefaultValue=None, ShowLimits=True, UOM=None)
    test = root('Test', TEST, SampleType=2, SampleDataPoints=[ref(point)], Instructions='', AllowMoveOnFailure=False,
                ScrapCountedRejectsByReason=False, AQLRejectReasons=None, DecreaseByRejectCountReason=None,
                DecreaseBySampleSizeReason=None, DefaultFailureMode=None, DefaultPEDescription='', Classification=None, SubClassification=None)
    aql = dict(Name='AQL', InstanceID=AQL, Description='', IsFrozen=False,
               SampleSizeDetails=[dict(InstanceID=AROW, Parent={'InstanceId': AQL}, IsFrozen=False,
                                       SampleSizeCode='A', SampleSize='5', Accepts=0, Rejects=1)])
    level = dict(Name='Level', InstanceID=LEVEL, Description='', IsFrozen=False,
                 LotSizeDetails=[dict(InstanceID=LROW, Parent={'InstanceId': LEVEL}, IsFrozen=False,
                                     SampleSizeCode='A', MinLotQty=1, MaxLotQty=100)])
    detail = dict(InstanceID=DETAIL, SampleTest=ref(test), Sequence=1, IsFrozen=False, InspectAll=False,
                  InspectAllContQty=False, InspectAllQtyOnFailure=False, AQLLevel=None, InspectionLevel=None,
                  SampleRate=0, Spec=None, Resource=None, Vendor=None, VendorItem=None, SwitchingRule=None)
    plan = root('Plan', PLAN, SamplingPlanDetails=[detail], AQLLevel=ref(aql), InspectionLevel=ref(level), SampleRate=2,
                MoveContainerOnCompletion=False, SampleSizeBasedOnStartQty=False)
    state = dict(rows={'SamplingPlans': [plan], 'SampleTests': [test], 'SampleDataPoints': [point],
                       'AQLLevels': [aql], 'InspectionLevels': [level]}, reads=[], hook=None,
                 counters={iid: 1 for iid in (PLAN, TEST, POINT, AQL, LEVEL)})
    async def transport(method, path, body=None, params=None, **kwargs):
        assert method == 'GET' and body is None
        state['reads'].append((path, copy.deepcopy(params)))
        if state['hook']: state['hook'](path, params)
        entity, *parts = path.removeprefix('/api/').split('/')
        rows = state['rows'].get(entity, [])
        if parts:
            matches = [r for r in rows if r['InstanceID'] == parts[0]]
            if len(matches) != 1: return json.dumps({'error': 'not found'})
            return json.dumps(copy.deepcopy(matches[0]))
        expr = params['$filter']
        matches = [r for r in rows if "Name eq '"+r['Name'].replace("'", "''")+"'" in expr]
        if 'Revision eq ' in expr:
            matches = [r for r in matches if "Revision eq '"+r['Revision'].replace("'", "''")+"'" in expr]
        if 'IsRevOfRcd eq true' in expr: matches = [r for r in matches if r['IsRevOfRcd']]
        return json.dumps({'value': copy.deepcopy(matches[:2])})
    # Exercise the real ReadContext reference/identity validation and existing
    # definition/table readers; mock only their shared HTTP and fixed SQL edge.
    from tools import process_context
    transport_mock = AsyncMock(side_effect=transport)
    monkeypatch.setattr(process_context, 'request', transport_mock)
    async def sql(query, maximum):
        for _, table, column, *_ in s.CONFIG.values():
            if ' FROM '+table+' WHERE ' in query:
                iid = query.split("='", 1)[1].split("'", 1)[0]
                return [{column: iid, 'ChangeCount': str(state['counters'][iid])}]
        for table, column in d.TABLES.values():
            if ' FROM '+table+' WHERE ' in query:
                iid = query.split("='", 1)[1].split("'", 1)[0]
                return [{column: iid, 'ChangeCount': str(state['counters'][iid])}]
        raise AssertionError(query)
    monkeypatch.setattr(a.q, 'sql_rows', AsyncMock(side_effect=sql))
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path/'not-created.sqlite3')
    state.update(plan=plan, test=test, point=point, aql=aql, level=level, detail=detail, transport=transport_mock)
    return state


def check(name='Plan', revision='A'): return result(a.check_sampling_plan_configuration(name, revision))
def codes(out): return {r['code'] for r in out.get('issues', [])}
def limited(out): return {r['code'] for r in out.get('submission_limitations', [])}


def test_complete_static_chain_uses_real_readers_and_no_journal(env):
    out = check()
    assert out['complete'] and out['assessment'] == 'no_issues_found' and out['snapshot_rechecked']
    assert len(out['dependencies']) == 5 and len(out['details']) == 1
    assert out['details'][0]['test_definition']['points'][0]['instanceId'] == POINT
    assert out['details'][0]['configured_table_sources'] == dict(AQLLevel='plan_default', InspectionLevel='plan_default')
    assert out['details'][0]['stored_sample_rate'] == 0 and out['details'][0]['plan_default_sample_rate'] == 2
    assert out['quality_verdict'] is None and not out['mes_task_requirements_verified']
    assert not out['execution_readiness_verified'] and not out['atomic_snapshot_verified']
    assert not p.PLAN_DB.exists()
    assert all(c.args[0] == 'GET' for c in env['transport'].await_args_list)


@pytest.mark.parametrize('target', ['plan', 'test', 'point', 'aql', 'level', 'detail'])
def test_frozen_configuration_is_not_misreported_as_unusable(env, target):
    env[target]['IsFrozen'] = True
    if target == 'aql': env['aql']['SampleSizeDetails'][0]['IsFrozen'] = True
    if target == 'level': env['level']['LotSizeDetails'][0]['IsFrozen'] = True
    out = check()
    assert out['complete'] and out['assessment'] == 'no_issues_found'


@pytest.mark.parametrize('target,code', [('plan', 'plan_inactive'), ('test', 'test_inactive'), ('point', 'point_inactive')])
def test_inactive_version_is_reported_without_claiming_quality_verdict(env, target, code):
    env[target]['Status'] = 2
    out = check()
    assert out['complete'] and code in codes(out) and out['assessment'] == 'issues_found'
    assert out['quality_verdict'] is None


@pytest.mark.parametrize('target,field,value', [
    ('point', 'LowerLimit', 'NaN'), ('point', 'UpperLimit', '1+2'), ('point', 'DataType', True),
    ('point', 'Status', None), ('test', 'SampleType', ''), ('detail', 'InspectAll', None),
    ('detail', 'InspectAllContQty', 'unknown'), ('detail', 'InspectAllQtyOnFailure', 0),
])
def test_unknown_malformed_fields_are_not_green(env, target, field, value):
    env[target][field] = value
    out = check()
    assert not out['complete'] and out['assessment'] == 'incomplete'


def test_reversed_limits_are_an_observed_issue(env):
    env['point']['LowerLimit'] = '11'
    assert 'limits_reversed' in codes(check())


@pytest.mark.parametrize('dtype', [1, 2, 3, 9])
def test_numeric_types_and_empty_optional_limits_supported(env, dtype):
    env['point'].update(DataType=dtype, LowerLimit=None, UpperLimit='')
    assert check()['assessment'] == 'no_issues_found'


def test_nonnumeric_point_is_tool_limitation_not_invalid_mes_configuration(env):
    env['point']['DataType'] = 7
    out = check()
    assert out['complete'] and out['assessment'] == 'no_issues_found'
    assert 'point_type_unsupported' in limited(out)


def test_external_sampling_is_tool_limitation(env):
    env['test']['SampleType'] = 3
    assert 'sample_type_unsupported' in limited(check())


@pytest.mark.parametrize('field', ['InspectAll', 'InspectAllContQty', 'InspectAllQtyOnFailure'])
def test_full_inspection_flags_are_limits_not_bad_configuration(env, field):
    env['detail'][field] = True
    out = check()
    assert 'full_inspection_unsupported' in limited(out)
    assert out['assessment'] == 'no_issues_found'


@pytest.mark.parametrize('field,value', [('DecreaseBySampleSizeReason', {'Name': 'SampleUse'}),
    ('DecreaseByRejectCountReason', {'Name': 'RejectUse'}), ('DefaultFailureMode', {'Name': 'Failure'}),
    ('DefaultPEDescription', 'record event'), ('ScrapCountedRejectsByReason', True)])
def test_effectful_test_does_not_pass_for_supported_submission(env, field, value):
    env['test'][field] = value
    out = check()
    assert 'submission_effects_unverified' in limited(out)


@pytest.mark.parametrize('measured', [True, False])
def test_only_measured_tests_require_points(env, measured):
    env['test'].update(SampleType=2 if measured else 1, SampleDataPoints=[])
    out = check()
    assert ('measured_points_missing' in codes(out)) is measured


def test_duplicate_points_preserve_evidence_and_report_issue(env):
    env['test']['SampleDataPoints'].append(ref(env['point']))
    out = check()
    assert 'duplicate_point' in codes(out)
    assert len(out['details'][0]['test_definition']['points']) == 2


@pytest.mark.parametrize('target,field,value', [
    ('detail', 'SampleTest', None), ('detail', 'SampleTest', {'Name': 'Missing', 'Revision': 'A'}),
    ('detail', 'SampleTest', {'Name': 'Test', 'Revision': 'A', 'InstanceId': POINT}),
    ('detail', 'AQLLevel', {'Name': 'Missing'}), ('detail', 'InspectionLevel', 'invalid'),
    ('plan', 'AQLLevel', {'Name': 'AQL', 'InstanceId': LEVEL}),
])
def test_broken_references_are_incomplete_without_default_fallback(env, target, field, value):
    env[target][field] = value
    assert check()['assessment'] == 'incomplete'


def test_missing_point_reference_is_incomplete(env):
    env['test']['SampleDataPoints'] = [{'Name': 'Missing', 'Revision': 'A'}]
    assert 'point_unverified' in codes(check())


def test_mismatched_aql_codes_are_exact_case_sensitive_matches(env):
    env['aql']['SampleSizeDetails'][0]['SampleSizeCode'] = 'a'
    out = check()
    assert out['complete'] and 'aql_codes_unmapped' in codes(out)
    assert out['details'][0]['unmapped_inspection_codes'] == ['A']


def test_detail_tables_override_defaults_but_both_are_checked(env):
    other = copy.deepcopy(env['aql']); other.update(Name='DetailAQL', InstanceID=key(10))
    other['SampleSizeDetails'][0].update(InstanceID=key(11), Parent={'InstanceId': key(10)})
    env['rows']['AQLLevels'].append(other); env['counters'][key(10)] = 1
    env['detail']['AQLLevel'] = ref(other)
    out = check()
    assert out['details'][0]['configured_table_sources']['AQLLevel'] == 'detail'
    assert out['details'][0]['configured_tables']['AQLLevel']['instanceId'] == key(10)
    env['aql']['SampleSizeDetails'][0]['SampleSize'] = '5%'
    assert check()['assessment'] == 'incomplete'


@pytest.mark.parametrize('mode', ['empty_aql', 'formula', 'thresholds', 'range_overlap', 'parent', 'duplicate'])
def test_table_problems_use_existing_verified_table_validators(env, mode):
    if mode == 'empty_aql': env['aql']['SampleSizeDetails'] = []
    if mode == 'formula': env['aql']['SampleSizeDetails'][0]['SampleSize'] = 'sqrt(n)'
    if mode == 'thresholds': env['aql']['SampleSizeDetails'][0]['Rejects'] = 0
    if mode == 'range_overlap':
        env['level']['LotSizeDetails'].append({**env['level']['LotSizeDetails'][0], 'InstanceID': key(9)})
    if mode == 'parent': env['level']['LotSizeDetails'][0]['Parent'] = {'InstanceId': AQL}
    if mode == 'duplicate': env['aql']['SampleSizeDetails'].append(copy.deepcopy(env['aql']['SampleSizeDetails'][0]))
    assert check()['assessment'] == 'incomplete'


def test_missing_optional_pair_does_not_invent_tables(env):
    env['plan'].update(AQLLevel=None, InspectionLevel=None)
    env['detail']['InspectAll'] = True
    out = check()
    assert out['complete'] and out['assessment'] == 'no_issues_found'
    assert 'table_pair_unresolved' in limited(out)
    assert 'configured_tables' not in out['details'][0]


@pytest.mark.parametrize('mode', ['empty', 'overflow', 'duplicate', 'point_overflow', 'table_overflow'])
def test_unbounded_or_ambiguous_lists_never_return_complete(env, mode):
    if mode == 'empty': env['plan']['SamplingPlanDetails'] = []
    if mode == 'overflow': env['plan']['SamplingPlanDetails'] *= 51
    if mode == 'duplicate': env['plan']['SamplingPlanDetails'] *= 2
    if mode == 'point_overflow': env['test']['SampleDataPoints'] *= 21
    if mode == 'table_overflow': env['level']['LotSizeDetails'] *= 51
    assert not check()['complete']


def test_dependency_cap_fails_without_subset_success(env, monkeypatch):
    monkeypatch.setattr(a, 'MAX_DEPENDENCIES', 3)
    assert not check()['complete']


def test_total_point_link_cap_includes_repeated_test_usage(env, monkeypatch):
    monkeypatch.setattr(a, 'MAX_POINT_LINKS', 1)
    env['plan']['SamplingPlanDetails'].append({**env['detail'], 'InstanceID': key(12)})
    out = check()
    assert not out['complete'] and '100' in out['error']


def test_late_change_to_previously_read_point_is_detected(env):
    n = 0
    def hook(path, params):
        nonlocal n
        if path == '/api/SampleDataPoints/'+POINT:
            n += 1
            if n == 3:
                env['point']['UpperLimit'] = '11'
                env['counters'][POINT] += 1
    env['hook'] = hook
    out = check()
    assert not out['complete'] and not out['snapshot_rechecked'] and '变化' in out['error']


def test_use_ror_pointer_change_is_detected_by_fresh_reference_resolution(env):
    env['detail']['SampleTest'] = {'Name': 'Test', 'UseROR': True}
    n = 0
    def hook(path, params):
        nonlocal n
        if path == '/api/SampleTests' and params and 'IsRevOfRcd' in params.get('$filter', ''):
            n += 1
            if n == 2:
                env['test']['IsRevOfRcd'] = False
                other = copy.deepcopy(env['test']); other.update(InstanceID=key(20), Revision='B', IsRevOfRcd=True)
                env['rows']['SampleTests'].append(other)
    env['hook'] = hook
    assert not check()['complete']


@pytest.mark.parametrize('name,revision', [('', 'A'), ('Plan', ''), ('Plan\n', 'A'), ('Unknown', 'A'), ('Plan', 'B')])
def test_explicit_plan_version_is_required(env, name, revision):
    assert not check(name, revision)['complete']


def test_routing_registry_capabilities_and_real_mcp_multi_turn_reads(env):
    name = 'check_sampling_plan_configuration'
    assert get_tool_func(name) is a.check_sampling_plan_configuration
    assert TOOL_POLICIES[name].effect == 'read' and not TOOL_POLICIES[name].always_confirm
    tools = [{'function': {'name': n}} for n in TOOL_POLICIES]
    for question in ('检查检验计划配置是否完整', 'AQL与检验水平代码是否对应', '看看质量计划的检验点上下限'):
        assert name in {t['function']['name'] for t in select_tools(tools, [{'role': 'user', 'content': question}])}
    assert name in ROUTING_INSTRUCTION
    async def run():
        async with Client(mcp) as client:
            schema = next(t.input_schema for t in await client.list_tools() if t.name == name)
            assert schema['additionalProperties'] is False
            assert set(schema['required']) == {'sampling_plan_name', 'sampling_plan_revision'}
            cap = await client.call_tool('get_capabilities', {'tool_name': name})
            assert name in cap.content[0].text
            first = await client.call_tool(name, {'sampling_plan_name': 'Plan', 'sampling_plan_revision': 'A'})
            assert json.loads(first.content[0].text)['assessment'] == 'no_issues_found'
            env['point']['LowerLimit'] = '11'
            second = await client.call_tool(name, {'sampling_plan_name': 'Plan', 'sampling_plan_revision': 'A'})
            assert 'limits_reversed' in codes(json.loads(second.content[0].text))
            bad = await client.call_tool(name, {'sampling_plan_name': 'Plan', 'sampling_plan_revision': 'A', 'execute': True}, raise_on_error=False)
            assert bad.is_error
    asyncio.run(run())
    assert not p.PLAN_DB.exists()
