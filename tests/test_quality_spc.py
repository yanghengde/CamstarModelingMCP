import asyncio
import copy
import json
from fractions import Fraction

import pytest
from fastmcp import Client

from tools import mcp, get_tool_func, quality_spc as s
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools, ROUTING_INSTRUCTION
from tools.tool_discovery import get_capabilities

RULES = list(s.RULE_WINDOWS)
CONTEXT = dict(characteristic='Diameter', unit='mm', process_scope='Line A / product R1',
               data_reference='export sha256:example', order_reference='measurement order field')


def rows(values, start=0, prefix='P'):
    return [dict(observation_id=f'{prefix}{k}', sequence=start+k,
                 values=[str(v) for v in (value if isinstance(value, list) else [value])]) for k, value in enumerate(values)]


def controls(size=1):
    return dict(reference='approved control document', version='A', subgroup_size=size,
                center='0', sigma='1', dispersion_center='1', dispersion_lcl='0', dispersion_ucl='10')


def analyze(values, **kw):
    args = dict(chart_type='i_mr', points=rows(values), context=CONTEXT, rules=RULES, controls=controls())
    args.update(kw)
    return s.analyze(**args)


@pytest.mark.parametrize('rule,values', [
    ('beyond_3sigma', ['3.000000000001']),
    ('two_of_three_2sigma', ['2.1', '0', '2.2']),
    ('four_of_five_1sigma', ['1.1', '1.2', '0', '1.3', '1.4']),
    ('eight_same_side', ['0.1']*8),
])
@pytest.mark.parametrize('sign', [1, -1])
def test_each_weco_rule_on_both_sides_with_original_evidence(rule, values, sign):
    values = [str(Fraction(v) * sign) if '/' not in str(Fraction(v)*sign) else s.plain(Fraction(v)*sign) for v in values]
    out = analyze(values, rules=[rule])
    found = out['primary_signals']
    assert len(found) == 1 and found[0]['side'] == ('above' if sign == 1 else 'below')
    assert found[0]['window_start_id'] == 'P0' and found[0]['window_end_id'] == f'P{len(values)-1}'
    assert out['has_signals'] and not out['release_authorized'] and not out['ready_for_mes_write']


@pytest.mark.parametrize('rule,values', [
    ('beyond_3sigma', ['3', '-3']),
    ('two_of_three_2sigma', ['2', '2', '2']),
    ('four_of_five_1sigma', ['1']*5),
    ('eight_same_side', ['1']*4 + ['0'] + ['1']*4),
    ('two_of_three_2sigma', ['2.1', '-2.1', '0']),
    ('four_of_five_1sigma', ['1.1', '-1.1', '1.1', '-1.1', '0']),
])
def test_exact_thresholds_center_and_opposite_sides_do_not_trigger(rule, values):
    assert not analyze(values, rules=[rule])['primary_signals']


def test_unfinished_windows_are_explicit_not_passed():
    out = analyze(['1'], rules=['eight_same_side'])
    assert out['evaluation_windows']['eight_same_side'] == {'required': 8, 'evaluated': 0}
    assert out['dispersion_evaluated_points'] == 0 and not out['assumptions_verified']


def test_running_windows_and_contributors_keep_order():
    out = analyze(['2.1', '0', '2.2', '2.3'], rules=['two_of_three_2sigma'])
    assert [(r['window_start_id'], r['window_end_id'], r['contributing_ids']) for r in out['primary_signals']] == [
        ('P0', 'P2', ['P0', 'P2']), ('P1', 'P3', ['P2', 'P3'])]


def test_nist_individuals_example_uses_only_separate_baseline():
    values = ['49.6','47.6','49.9','51.3','47.8','51.2','52.6','52.4','53.6','52.1']
    out = analyze(['100'], points=rows(['100'],10), controls=None, baseline=rows(values,prefix='B'), baseline_reference='NIST ten batches')
    limits = out['control_limits']['primary']
    assert limits['center'] == '50.81'
    assert float(limits['ucl']) == pytest.approx(55.8040898345)
    assert float(limits['lcl']) == pytest.approx(45.8159101655)
    assert out['baseline']['count'] == 10 and out['baseline']['stability_verified'] is False
    assert out['control_origin'] == 'separate_baseline_estimate'
    assert out['primary_signals'][0]['contributing_ids'] == ['P0']
    assert out['dispersion_points'] == []  # No artificial MR across the baseline boundary.


def test_xbar_r_fixed_subgroups_and_limits_match_hand_calculation():
    out = analyze([], chart_type='xbar_r', points=rows([[7,8,9]],2), controls=None,
                  baseline=rows([[1,2,3],[3,4,5]], prefix='B'), baseline_reference='two rational subgroups')
    primary = out['control_limits']['primary']; dispersion = out['control_limits']['dispersion']
    assert primary['center'] == '3' and primary['ucl'] == '5.046' and primary['lcl'] == '0.954'
    assert dispersion == dict(center='2',lcl='0',ucl='5.15')
    assert out['subgroup_size'] == 3 and out['points'][0]['statistic'] == '8'
    assert out['has_signals']


@pytest.mark.parametrize('size', range(2,11))
def test_supported_subgroup_sizes_keep_actual_groups(size):
    out = analyze([], chart_type='xbar_r', points=rows([[1]*size]), controls=controls(size))
    assert out['subgroup_size'] == size and out['points'][0]['values'] == ['1']*size


def test_rational_comparison_does_not_round_borderline_thirds():
    cfg = controls(3)
    out = analyze([], chart_type='xbar_r', points=rows([['3','3','3'],['3','3','3.000000000003']]), controls=cfg,
                  rules=['beyond_3sigma'])
    assert [r['window_end_id'] for r in out['primary_signals']] == ['P1']


def test_mr_uses_absolute_consecutive_differences_and_only_dispersion_rule():
    cfg = controls(); cfg.update(dispersion_ucl='3')
    out = analyze(['0','3','-1'], controls=cfg, rules=['eight_same_side'])
    assert out['dispersion_points'] == [{'end_id':'P1','value':'3'},{'end_id':'P2','value':'4'}]
    assert out['dispersion_signals'] == [dict(rule='outside_dispersion_limits', side='above', window_end_id='P2', contributing_ids=['P1','P2'])]


def test_dispersion_lower_limit_for_r_chart():
    cfg=controls(2);cfg.update(dispersion_center='2',dispersion_lcl='1')
    out=analyze([],chart_type='xbar_r',points=rows([[0,0],[0,1]]),controls=cfg)
    assert [(r['side'],r['window_end_id']) for r in out['dispersion_signals']]==[('below','P0')]


def test_baseline_signals_preserved_and_never_refitted_or_certified():
    baseline=rows(['0']*24+['100'],prefix='B')
    out=analyze([],points=rows(['0'],25),controls=None,baseline=baseline,baseline_reference='unreviewed baseline')
    assert out['control_limits']['primary']['center']=='4'
    assert out['baseline']['primary_signals'] and out['baseline']['dispersion_signals']
    assert not out['baseline']['stability_verified']


def test_input_digest_changes_with_data_order_rule_context_and_control_version():
    original=analyze(['1','2'])['input_digest']
    cfg=controls();cfg['version']='B'
    ctx={**CONTEXT,'unit':'um'}
    candidates=[analyze(['2','1']),analyze(['1','3']),analyze(['1','2'],rules=['beyond_3sigma']),
                analyze(['1','2'],context=ctx),analyze(['1','2'],controls=cfg)]
    assert all(r['input_digest']!=original for r in candidates)
    assert analyze(['1','2'])['input_digest']==original
    out=analyze(['1','2'])
    assert out['input_digest']==s.digest(out['input_snapshot'])


def test_baseline_never_supplies_rule_window_history_to_evaluation():
    out=analyze([],points=rows(['1'],8),controls=None,baseline=rows(['0','1']*4,prefix='B'),
                baseline_reference='separate segment',rules=['eight_same_side'])
    assert out['evaluation_windows']['eight_same_side']['evaluated']==0
    assert out['primary_signals']==[] and len(out['dispersion_points'])==0


@pytest.mark.parametrize('field,value', [('characteristic',''),('unit',' '),('process_scope','A\nB'),
    ('data_reference','ref '),('order_reference',None),('unverified_override','yes')])
def test_context_requires_explicit_clean_scope_and_sources(field,value):
    ctx={**CONTEXT,field:value}
    with pytest.raises(ValueError):analyze(['0'],context=ctx)


def test_tool_reports_validation_error_without_a_result():
    out=json.loads(asyncio.run(s.analyze_spc_series('i_mr',rows(['0']),CONTEXT,RULES)))
    assert 'error' in out and not out['ready_for_mes_write'] and 'has_signals' not in out


def test_inputs_are_not_modified():
    pts=rows(['1','2']);cfg=controls();before=copy.deepcopy((pts,cfg,CONTEXT))
    analyze([],points=pts,controls=cfg)
    assert (pts,cfg,CONTEXT)==before


@pytest.mark.parametrize('value', [True,False,None,1,1.5,'NaN','Infinity','1e2','1,2',' 1','1 ','+1','01','1.0000000000001','1000000000001'])
def test_invalid_measurements_are_not_coerced_or_skipped(value):
    with pytest.raises(ValueError):analyze([],points=[dict(observation_id='P',sequence=0,values=[value])])


@pytest.mark.parametrize('change', ['duplicate','reverse','gap','bool_sequence','mixed_sizes','blank_id','newline_id','extra','empty','too_many'])
def test_invalid_order_identity_shape_fails(change):
    points=rows(['0','1'])
    if change=='duplicate':points[1]['observation_id']='P0'
    elif change=='reverse':points.reverse()
    elif change=='gap':points[1]['sequence']=2
    elif change=='bool_sequence':points[0]['sequence']=True
    elif change=='mixed_sizes':points[0]['values']=['0','1']
    elif change=='blank_id':points[0]['observation_id']=' '
    elif change=='newline_id':points[0]['observation_id']='P\n'
    elif change=='extra':points[0]['timestamp']='guessed'
    elif change=='empty':points=[]
    else:points=rows(['0']*501)
    with pytest.raises(ValueError):analyze([],points=points)


@pytest.mark.parametrize('rules', [[],['unknown'],['beyond_3sigma']*2])
def test_rules_must_be_explicit_and_unique(rules):
    with pytest.raises(ValueError):analyze(['0'],rules=rules)


@pytest.mark.parametrize('change', ['both','neither','missing_reference','overlap','gap','constant','one','size'])
def test_baseline_and_controls_fail_closed(change):
    kw=dict(points=rows(['2'],2),baseline=rows(['0','1'],prefix='B'),baseline_reference='baseline',controls=None)
    if change=='both':kw['controls']=controls()
    elif change=='neither':kw['baseline']=None
    elif change=='missing_reference':kw['baseline_reference']=None
    elif change=='overlap':kw['baseline'][0]['observation_id']='P0'
    elif change=='gap':kw['points'][0]['sequence']=4
    elif change=='constant':kw['baseline']=rows(['1','1'],prefix='B')
    elif change=='one':kw['baseline']=rows(['1'],prefix='B')
    elif change=='size':kw.update(chart_type='xbar_r',baseline=rows([[0,1,2],[1,2,3]],prefix='B'),points=rows([[1,2]],2))
    with pytest.raises(ValueError):analyze([],**kw)


@pytest.mark.parametrize('key,value', [('sigma','0'),('sigma','-1'),('sigma','NaN'),('subgroup_size',True),
    ('subgroup_size',2),('dispersion_lcl','-1'),('dispersion_center','11'),('dispersion_ucl','0'),
    ('version',' '),('reference',''),('specification_limit','3')])
def test_invalid_fixed_control_or_specification_fields_rejected(key,value):
    cfg=controls();cfg[key]=value
    with pytest.raises(ValueError):analyze(['1'],controls=cfg)


def test_no_mes_or_journal_activity_in_tool(monkeypatch,tmp_path):
    from tools import quality_plans as p
    import core.http_client as http
    def forbidden(*a,**k):raise AssertionError('pure computation must not query or write')
    monkeypatch.setattr(http,'request',forbidden);monkeypatch.setattr(http,'get_client',forbidden)
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'never.sqlite3')
    out=json.loads(asyncio.run(s.analyze_spc_series('i_mr',rows(['1']),CONTEXT,RULES,controls=controls())))
    assert not out['native_spc_result'] and not out['data_verified_against_mes']
    assert not p.PLAN_DB.exists()


@pytest.mark.parametrize('question', ['帮我计算SPC','查看控制图判异','分析移动极差','用WECO检查这些数据','Xbar-R控制限',
                                      'i_mr分析'])
def test_readonly_natural_language_routing(question):
    tools=[{'function':{'name':n}} for n in TOOL_POLICIES]
    chosen={t['function']['name'] for t in select_tools(tools,[dict(role='user',content=question)])}
    assert 'analyze_spc_series' in chosen and 'execute_quality_transaction' not in chosen


def test_multiturn_followup_keeps_spc_tool():
    tools=[{'function':{'name':n}} for n in TOOL_POLICIES]
    messages=[dict(role='user',content='分析SPC控制图'),dict(role='assistant',content='请给基线'),dict(role='user',content='继续分析这组')]
    assert 'analyze_spc_series' in {t['function']['name'] for t in select_tools(tools,messages)}
    assert '尚无违例/注释写入工具' in ROUTING_INSTRUCTION


def test_mcp_closed_schemas_real_dispatch_and_nested_validation():
    async def run():
        assert get_tool_func('analyze_spc_series') is s.analyze_spc_series
        policy=TOOL_POLICIES['analyze_spc_series'];assert policy.group=='quality' and policy.effect=='read'
        async with Client(mcp) as client:
            tool=next(t for t in await client.list_tools() if t.name=='analyze_spc_series')
            schema=tool.input_schema
            assert schema['additionalProperties'] is False
            assert all(d.get('additionalProperties') is False for d in schema.get('$defs',{}).values() if d.get('type')=='object')
            args=dict(chart_type='i_mr',points=rows(['3.1']),context=CONTEXT,rules=['beyond_3sigma'],controls=controls())
            reply=await client.call_tool('analyze_spc_series',args)
            assert json.loads(reply.content[0].text)['has_signals']
            args['points'][0]['values']=[True]
            with pytest.raises(Exception):await client.call_tool('analyze_spc_series',args)
        cap=json.loads(await get_capabilities(tool_name='analyze_spc_series'))
        assert 'analyze_spc_series' in json.dumps(cap)
    asyncio.run(run())
