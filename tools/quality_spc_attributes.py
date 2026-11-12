"""Local p/np/c/u charts with explicit count semantics and exact signal decisions."""
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StrictStr

from tools import mcp, quality_spc as spc
from tools.boms import encode
from tools.collection_submission import digest


VERSION = 'spc-attributes-v1'
Chart = Literal['p', 'np', 'c', 'u']
CountKind = Literal['nonconforming_units', 'nonconformities']
SOURCES = [
    'https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc332.htm',
    'https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc331.htm',
    'https://www.itl.nist.gov/div898/software/dataplot/refman1/ch2/np_cont.pdf',
    'https://luca-scr.github.io/qcc/reference/stats.u.html',
]


class AttributeObservation(spc.ClosedModel):
    observation_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    sequence: Annotated[StrictInt, Field(ge=0, le=1000000000)]
    count: Annotated[StrictInt, Field(ge=0, le=1000000000)]
    inspected_units: StrictStr


class AttributeControls(spc.ClosedModel):
    reference: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    version: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    rate: StrictStr
    inspected_units: StrictStr | None = None


FLAGS = {'data_verified_against_mes': False, 'configuration_verified_against_mes': False,
         'native_spc_result': False, 'ready_for_mes_write': False, 'release_authorized': False,
         'assumptions_verified': False, 'native_engine_result_verified': False}


def inspect_rows(raw, chart, label, minimum):
    if not isinstance(raw, list) or not minimum <= len(raw) <= 500:
        raise ValueError(label+f'须有{minimum}..500个明确有序样本。')
    rows = [AttributeObservation.model_validate(row) for row in raw]
    for row in rows:
        spc.labels(row)
    if len({row.observation_id for row in rows}) != len(rows):
        raise ValueError(label+'观测ID重复。')
    if any(right.sequence != left.sequence+1 for left, right in zip(rows, rows[1:])):
        raise ValueError(label+'顺序必须连续递增，不补点、排序或拼接缺口。')
    sizes = [spc.number(row.inspected_units) for row in rows]
    if any(size <= 0 for size in sizes):
        raise ValueError('inspected_units须为明确正数，不推断样本量/检查单位或换算单位。')
    if chart in ('p', 'np') and any(size.denominator != 1 or row.count > size for row, size in zip(rows, sizes)):
        raise ValueError('p/np使用不合格品件数，样本量须正整数且count不得超过样本量。')
    return rows, sizes


def decimal(value):
    value = Fraction(value)
    return Decimal(value.numerator)/Decimal(value.denominator)


def display(value):
    if isinstance(value, Fraction):
        value = decimal(value)
    if value == 0:
        return '0'
    rendered = format(value, 'f')
    return rendered.rstrip('0').rstrip('.') if '.' in rendered else rendered


def evaluate(rows, sizes, rate, chart):
    points, signals, approximation = [], [], []
    for row, size in zip(rows, sizes):
        statistic = Fraction(row.count)/size if chart in ('p', 'u') else Fraction(row.count)
        center = rate if chart in ('p', 'u') else rate*size
        variance = ((rate*(1-rate)/size if chart == 'p' else size*rate*(1-rate))
                    if chart in ('p', 'np') else rate/size if chart == 'u' else size*rate)
        if variance <= 0:
            raise ValueError('控制配置或基线变差为零，不能用退化控制限输出全通过。')
        # Square the exact rational distance. Decimal sqrt is DISPLAY ONLY;
        # equality at a 3-sigma boundary never becomes a signal due to rounding.
        difference = statistic-center
        outside = difference*difference > 9*variance
        width = 3*decimal(variance).sqrt()
        lower = max(Decimal(0), decimal(center)-width)
        upper = decimal(center)+width
        if chart == 'p':
            upper = min(Decimal(1), upper)
        elif chart == 'np':
            upper = min(decimal(size), upper)
        point = {**row.model_dump(), 'statistic': display(statistic), 'center': display(center),
                 'sigma_squared': display(variance), 'lcl': display(lower), 'ucl': display(upper),
                 'beyond_3sigma': outside}
        points.append(point)
        if outside:
            signals.append({'rule': 'beyond_3sigma', 'side': 'above' if difference > 0 else 'below',
                            'window_start_id': row.observation_id, 'window_end_id': row.observation_id,
                            'contributing_ids': [row.observation_id]})
        # This diagnostic is not a proof of binomial/Poisson independence,
        # distribution fit or nominal false-alarm probability.
        expected = rate*size
        if expected < 5 or chart in ('p', 'np') and (1-rate)*size < 5:
            approximation.append(row.observation_id)
    return {'points': points, 'signals': signals, 'has_signals': bool(signals),
            'small_expected_counts': approximation}


def analyze(chart_type, count_kind, points, context, rule, baseline=None, baseline_reference=None, controls=None):
    if chart_type not in ('p', 'np', 'c', 'u') or rule != 'beyond_3sigma':
        raise ValueError('计数图仅支持p/np/c/u及显式beyond_3sigma，不套用连续值WECO运行规则。')
    expected_kind = 'nonconforming_units' if chart_type in ('p', 'np') else 'nonconformities'
    if count_kind != expected_kind:
        raise ValueError('p/np统计不合格品件数；c/u统计缺陷个数，不能混用两种计数口径。')
    if (baseline is None) == (controls is None):
        raise ValueError('须且只能提供独立baseline或有版本controls，不用评估段拟合控制限。')
    ctx = spc.SPCContext.model_validate(context)
    spc.labels(ctx)
    rows, sizes = inspect_rows(points, chart_type, 'points', 1)
    base_rows, base_sizes, base_result, fixed_size = [], [], None, None
    if baseline is not None:
        ref = spc.SPCContext.model_validate({**ctx.model_dump(), 'data_reference': baseline_reference})
        spc.labels(ref)
        base_rows, base_sizes = inspect_rows(baseline, chart_type, 'baseline', 2)
        if (base_rows[-1].sequence+1 != rows[0].sequence
                or {row.observation_id for row in base_rows} & {row.observation_id for row in rows}):
            raise ValueError('独立基线与评估段顺序须相接且ID不能重叠。')
        rate = Fraction(sum(row.count for row in base_rows))/sum(base_sizes)
        origin, reference, version = 'separate_baseline_estimate', baseline_reference, VERSION
        if chart_type in ('np', 'c'):
            fixed_size = base_sizes[0]
    else:
        if baseline_reference is not None:
            raise ValueError('固定配置模式不能另传baseline_reference。')
        cfg = AttributeControls.model_validate(controls)
        spc.labels(cfg)
        rate = spc.number(cfg.rate)
        origin, reference, version = 'caller_fixed_controls', cfg.reference, cfg.version
        if chart_type in ('np', 'c'):
            if cfg.inspected_units is None:
                raise ValueError('np/c固定配置须明确固定inspected_units。')
            fixed_size = spc.number(cfg.inspected_units)
            if fixed_size <= 0 or chart_type == 'np' and fixed_size.denominator != 1:
                raise ValueError('固定检查单位须正数，np的固定样本量须整数。')
        elif cfg.inspected_units is not None:
            raise ValueError('p/u按每点明确检查单位计算，不接受固定配置中的inspected_units。')
    if rate <= 0 or chart_type in ('p', 'np') and rate >= 1:
        raise ValueError('p/np基准不合格率须0<rate<1，c/u基准缺陷率须大于零；退化基线不输出全通过。')
    if fixed_size is not None and any(size != fixed_size for size in base_sizes+sizes):
        raise ValueError('np/c要求基线和评估段检查单位固定；可变样本量分别选择p/u。')
    with localcontext() as precision:
        precision.prec = 64
        evaluation = evaluate(rows, sizes, rate, chart_type)
        if baseline is not None:
            base_result = {'reference': reference, 'count': len(base_rows),
                           **evaluate(base_rows, base_sizes, rate, chart_type), 'stability_verified': False}
        request = {'chart_type': chart_type, 'count_kind': count_kind,
                   'points': [row.model_dump() for row in rows], 'context': ctx.model_dump(), 'rule': rule,
                   'baseline': [row.model_dump() for row in base_rows] if baseline is not None else None,
                   'baseline_reference': baseline_reference,
                   'controls': AttributeControls.model_validate(controls).model_dump() if controls is not None else None}
        return {**FLAGS, 'algorithm_version': VERSION, 'rule_version': 'point-three-sigma-strict-v1',
                'chart_type': chart_type, 'count_kind': count_kind, 'context': ctx.model_dump(),
                'input_snapshot': request, 'input_digest': digest(request), 'selected_rule': rule,
                'control_origin': origin, 'control_reference': reference, 'control_version': version,
                'rate': display(rate), 'fixed_inspected_units': display(fixed_size) if fixed_size is not None else None,
                'baseline': base_result, **evaluation, 'signal_scope': 'points_only',
                'evaluated_points': len(rows), 'arithmetic': 'exact_rational_squared_comparisons_decimal_display_64_digits',
                'sources': SOURCES,
                'limitations': ['调用方声明同一过程、单位、合理样本和计数口径；未查询MES或核实独立性、分布和过度离散。',
                    '采用对称三西格玛近似控制限，不是精确二项/泊松概率限；小期望计数逐点提示，其他样本也未认证近似适用性。',
                    '下限截于0；p上限截于1、np截于样本量。边界使用精确平方比较，显示限值的舍入不影响信号。',
                    '基线至少2点只是计算门槛；基线信号保留、不剔除或自动重算，评估段不参与限值拟合。',
                    '仅逐点beyond_3sigma；不生成违例/注释计划，不认证现场引擎，不判断产品合格或允许放行。']}


@mcp.tool
async def analyze_spc_attributes(
    chart_type: Chart,
    count_kind: CountKind,
    points: Annotated[list[AttributeObservation], Field(min_length=1, max_length=500)],
    context: spc.SPCContext,
    rule: Literal['beyond_3sigma'],
    baseline: Annotated[list[AttributeObservation] | None, Field(min_length=2, max_length=500)] = None,
    baseline_reference: str | None = None,
    controls: AttributeControls | None = None,
) -> str:
    """本地只读p/np/c/u计数控制图。count为非负整数，inspected_units为明确正数字符串。
    p/np必须count_kind=nonconforming_units，样本量整数且count不超过样本量；c/u用nonconformities缺陷个数。
    np/c检查单位须固定，p/u可逐点变化；不混合过程或自动推断单位/分组。
    独立baseline+baseline_reference或有版本controls二选一。controls.rate为每单位不合格率/缺陷率，np/c还须固定inspected_units。
    显式rule=beyond_3sigma，仅逐点三西格玛近似；保留原ID/顺序/摘要及基线信号，拒绝退化基线。
    不查询MES、不生成登记计划、不运行引擎、不判合格或放行；不将本结果填成operator_reported外部报告。
    """
    def obj(value):
        return value.model_dump() if hasattr(value, 'model_dump') else value
    try:
        return encode(analyze(chart_type, count_kind, [obj(row) for row in points], obj(context), rule,
                              [obj(row) for row in baseline] if baseline is not None else None,
                              baseline_reference, obj(controls)))
    except (ValueError, TypeError) as exc:
        return encode({'error': str(exc), **FLAGS})
