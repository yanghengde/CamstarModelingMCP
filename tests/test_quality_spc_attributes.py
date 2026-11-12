import asyncio
import copy
import json
from fractions import Fraction

import pytest
from fastmcp import Client

from tools import mcp, get_tool_func, quality_spc_attributes as attributes
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools


CONTEXT = dict(characteristic='Explicit counted outcome', unit='board', process_scope='synthetic test line',
               data_reference='synthetic-counts-v1', order_reference='declared acquisition order')


def row(count, size='36', seq=1, prefix='P'):
    return dict(observation_id=prefix+str(seq), sequence=seq, count=count, inspected_units=size)


def controls(rate='0.5', size=None):
    return dict(reference='Synthetic fixed profile', version='A', rate=rate, inspected_units=size)


def analyze(chart='p', points=None, **overrides):
    args = dict(chart_type=chart, count_kind='nonconforming_units' if chart in ('p', 'np') else 'nonconformities',
        points=points or [row(18)], context=CONTEXT, rule='beyond_3sigma',
        controls=controls('0.5', '36' if chart in ('np', 'c') else None))
    args.update(overrides)
    return attributes.analyze(**args)


@pytest.mark.parametrize('chart', ['p', 'np', 'c', 'u'])
def test_hand_calculated_exact_upper_lower_boundaries(chart):
    if chart in ('p', 'np'):
        counts, size, rate = [27, 28, 9, 8], '36', '0.5'
        expected = ('0.25', '0.75') if chart == 'p' else ('9', '27')
    elif chart == 'c':
        counts, size, rate, expected = [28, 29, 4, 3], '1', '16', ('4', '28')
    else:
        counts, size, rate, expected = [28, 29, 4, 3], '4', '4', ('1', '7')
    out = analyze(chart, [row(count, size, i+1) for i, count in enumerate(counts)],
                  controls=controls(rate, size if chart in ('np', 'c') else None))
    assert [p['beyond_3sigma'] for p in out['points']] == [False, True, False, True]
    assert (out['points'][0]['lcl'], out['points'][0]['ucl']) == expected
    assert [s['side'] for s in out['signals']] == ['above', 'below']
    assert [s['window_end_id'] for s in out['signals']] == ['P2', 'P4']
    assert out['input_snapshot']['points'][0]['count'] == counts[0]
    assert not out['native_spc_result'] and not out['ready_for_mes_write'] and not out['release_authorized']


def test_nist_c_chart_example_independent_baseline_keeps_its_original_signal():
    # NIST counts example: 25 wafers, total400, cbar16, LCL4/UCL28.
    counts = [16, 14, 28, 16, 12, 20, 10, 12, 10, 17, 19, 17, 14,
              16, 15, 13, 14, 16, 11, 20, 11, 19, 16, 31, 13]
    out = analyze('c', [row(28, '1', 26)], controls=None,
        baseline=[row(value, '1', i+1, 'B') for i, value in enumerate(counts)], baseline_reference='NIST pmc331 counts example')
    assert out['rate'] == '16' and out['control_origin'] == 'separate_baseline_estimate'
    assert out['points'][0]['lcl'] == '4' and out['points'][0]['ucl'] == '28'
    assert not out['has_signals'] and out['baseline']['has_signals'] and not out['baseline']['stability_verified']
    assert [s['window_end_id'] for s in out['baseline']['signals']] == ['B24']


@pytest.mark.parametrize('chart,base,expected', [
    ('p', [row(1, '10', 1, 'B'), row(18, '90', 2, 'B')], '0.19'),
    ('u', [row(10, '1', 1, 'B'), row(10, '9', 2, 'B')], '2')])
def test_variable_size_baseline_is_total_count_over_total_exposure(chart, base, expected):
    out = analyze(chart, [row(2, '10', 3)], baseline=base, controls=None, baseline_reference='independent weighted baseline')
    assert out['rate'] == expected
    assert out['input_snapshot']['baseline'] == base


def test_variable_p_and_u_sizes_have_per_point_control_limits():
    for chart, rate in [('p', '0.5'), ('u', '4')]:
        out = analyze(chart, [row(1, '4'), row(1, '36', 2)], controls=controls(rate))
        assert out['points'][0]['ucl'] != out['points'][1]['ucl']
        assert out['fixed_inspected_units'] is None


def test_fractional_inspection_exposure_only_for_nonconformities():
    assert analyze('u', [row(10, '2.5')], controls=controls('4'))['points'][0]['statistic'] == '4'
    assert analyze('c', [row(10, '2.5')], controls=controls('4', '2.5'))['points'][0]['statistic'] == '10'
    for chart in ('p', 'np'):
        with pytest.raises(ValueError):
            analyze(chart, [row(1, '2.5')])


def test_physical_limits_clip_and_sparse_data_is_not_silently_certified():
    low = analyze('p', [row(0, '1')], controls=controls('0.1'))
    high = analyze('np', [row(1, '1')], controls=controls('0.9', '1'))
    assert low['points'][0]['lcl'] == '0' and high['points'][0]['ucl'] == '1'
    assert low['small_expected_counts'] == ['P1'] and not low['assumptions_verified']
    count = analyze('c', [row(10, '1')], controls=controls('1', '1'))
    assert count['points'][0]['lcl'] == '0' and count['small_expected_counts'] == ['P1']


@pytest.mark.parametrize('change', ['both', 'neither', 'missing_ref', 'extra_ref', 'overlap', 'gap', 'zero', 'all_bad',
                                   'variable_np', 'variable_c', 'fixed_missing', 'fixed_wrong', 'fixed_fractional_np'])
def test_baseline_control_contracts_reject_unsafe_input(change):
    args = dict(chart='p', points=[row(1, '10', 3)], controls=None,
                baseline=[row(1, '10', 1, 'B'), row(2, '10', 2, 'B')], baseline_reference='baseline')
    if change == 'both': args['controls'] = controls()
    elif change == 'neither': args['baseline'] = None
    elif change == 'missing_ref': args['baseline_reference'] = None
    elif change == 'extra_ref': args.update(baseline=None, controls=controls())
    elif change == 'overlap': args['points'][0]['observation_id'] = 'B1'
    elif change == 'gap': args['points'][0]['sequence'] = 4
    elif change == 'zero': args['baseline'] = [row(0, '10', i+1, 'B') for i in range(2)]
    elif change == 'all_bad': args['baseline'] = [row(10, '10', i+1, 'B') for i in range(2)]
    elif change in ('variable_np', 'variable_c'):
        args['chart'] = 'np' if change == 'variable_np' else 'c'; args['points'][0]['inspected_units'] = '20'
    elif change.startswith('fixed_'):
        args.update(chart='np', baseline=None, baseline_reference=None,
                    controls=controls('0.1', None if change == 'fixed_missing' else '20' if change == 'fixed_wrong' else '10.5'))
    with pytest.raises(ValueError): analyze(**args)


@pytest.mark.parametrize('field,value', [('count', True), ('count', -1), ('count', 1.1), ('count', '1'),
    ('count', 1000000001), ('inspected_units', '0'), ('inspected_units', '-1'), ('inspected_units', 'NaN'),
    ('inspected_units', '1e3'), ('inspected_units', 10), ('inspected_units', '1000000000001'),
    ('sequence', True), ('observation_id', ' P'), ('observation_id', ''), ('specification_limit', '5')])
def test_strict_point_inputs(field, value):
    point = row(1); point[field] = value
    with pytest.raises(ValueError): analyze(points=[point])


@pytest.mark.parametrize('chart', ['p', 'np'])
def test_defectives_cannot_exceed_samples_and_count_semantics_are_not_interchangeable(chart):
    with pytest.raises(ValueError): analyze(chart, [row(37)])
    with pytest.raises(ValueError): analyze(chart, count_kind='nonconformities')
    with pytest.raises(ValueError): analyze('c', count_kind='nonconforming_units')


@pytest.mark.parametrize('chart,rate', [('p', '0'), ('p', '1'), ('p', '1.1'), ('np', '-0.1'),
                                    ('c', '0'), ('u', '-1'), ('u', 'NaN')])
def test_degenerate_profiles_are_rejected(chart, rate):
    with pytest.raises(ValueError):
        analyze(chart, controls=controls(rate, '36' if chart in ('np', 'c') else None))


def test_input_digest_retains_order_source_and_fixed_configuration_version():
    first = analyze()
    for change in [dict(controls=controls('0.4')), dict(context={**CONTEXT, 'data_reference': 'different source'}),
                   dict(points=[row(19)]), dict(controls={**controls(), 'version': 'B'})]:
        assert analyze(**change)['input_digest'] != first['input_digest']
    assert analyze()['input_digest'] == first['input_digest']
    assert not analyze()['configuration_verified_against_mes']
    with pytest.raises(ValueError): analyze(points=[row(1), row(2)])
    with pytest.raises(ValueError): analyze(rule='eight_same_side')


def test_decisions_match_independent_count_scale_inequality_for_every_small_sample():
    p = Fraction(1, 4)
    for size in [1, 4, 36, 100]:
        data = [row(count, str(size), count+1) for count in range(size+1)]
        out = analyze('p', data, controls=controls('0.25'))
        expected = [(Fraction(count)-size*p)**2 > 9*size*p*(1-p) for count in range(size+1)]
        assert [point['beyond_3sigma'] for point in out['points']] == expected


def test_tool_registration_closed_schema_pure_computation_and_real_mcp_call(monkeypatch, tmp_path):
    import core.http_client as http
    from tools import quality_plans
    def forbidden(*args, **kwargs): raise AssertionError('local counts must not access MES')
    monkeypatch.setattr(http, 'request', forbidden); monkeypatch.setattr(http, 'get_client', forbidden)
    monkeypatch.setattr(quality_plans, 'PLAN_DB', tmp_path/'never.sqlite3')
    assert get_tool_func('analyze_spc_attributes') is attributes.analyze_spc_attributes
    assert TOOL_POLICIES['analyze_spc_attributes'].effect == 'read'
    async def call():
        async with Client(mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            schema = tools['analyze_spc_attributes'].input_schema
            assert schema['additionalProperties'] is False
            assert set(schema['properties']['chart_type']['enum']) == {'p', 'np', 'c', 'u'}
            reply = await client.call_tool('analyze_spc_attributes', dict(chart_type='p', count_kind='nonconforming_units',
                points=[row(28)], context=CONTEXT, rule='beyond_3sigma', controls=controls()))
            return json.loads(reply.content[0].text)
    result = asyncio.run(call())
    assert result['has_signals'] and not result['ready_for_mes_write'] and not quality_plans.PLAN_DB.exists()


@pytest.mark.parametrize('question', ['分析p控制图不良率', 'np控制图判异', 'SPC缺陷数c图', 'u控制图按面积统计'])
def test_counts_routing_exposes_readonly_tool_without_executor(question):
    tools = [{'function': {'name': name}} for name in TOOL_POLICIES]
    chosen = {tool['function']['name'] for tool in select_tools(tools, [dict(role='user', content=question)])}
    assert 'analyze_spc_attributes' in chosen and 'execute_quality_transaction' not in chosen
