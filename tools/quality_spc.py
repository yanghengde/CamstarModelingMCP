"""Pure, versioned SPC analysis; never a native SPC transaction or release decision."""
import re
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from tools import mcp
from tools.boms import encode
from tools.collection_submission import digest

VERSION = 'spc-local-v1'
Rule = Literal['beyond_3sigma', 'two_of_three_2sigma', 'four_of_five_1sigma', 'eight_same_side']
Chart = Literal['i_mr', 'xbar_r']
RULE_WINDOWS = {'beyond_3sigma': (1, 1, 3), 'two_of_three_2sigma': (3, 2, 2),
                'four_of_five_1sigma': (5, 4, 1), 'eight_same_side': (8, 8, 0)}
# Published, rounded NIST constants; retain this precision in the algorithm version.
FACTORS = {2: ('1.880', '0', '3.267'), 3: ('1.023', '0', '2.575'),
           4: ('0.729', '0', '2.282'), 5: ('0.577', '0', '2.115'),
           6: ('0.483', '0', '2.004'), 7: ('0.419', '0.076', '1.924'),
           8: ('0.373', '0.136', '1.864'), 9: ('0.337', '0.184', '1.816'),
           10: ('0.308', '0.223', '1.777')}
SOURCES = ['https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc32.htm',
           'https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc322.htm',
           'https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc321.htm']


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SPCObservation(ClosedModel):
    observation_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    sequence: Annotated[StrictInt, Field(ge=0, le=1000000000)]
    values: Annotated[list[StrictStr], Field(min_length=1, max_length=10)]


class SPCContext(ClosedModel):
    characteristic: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    unit: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    process_scope: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    data_reference: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    order_reference: Annotated[StrictStr, Field(min_length=1, max_length=512)]


class SPCFixedControls(ClosedModel):
    reference: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    version: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    subgroup_size: Annotated[StrictInt, Field(ge=1, le=10)]
    center: StrictStr
    sigma: StrictStr  # Standard deviation of the plotted statistic, not raw X for Xbar.
    dispersion_center: StrictStr
    dispersion_lcl: StrictStr
    dispersion_ucl: StrictStr


def number(value):
    # Exact bounded decimal input avoids binary-float threshold ambiguities.
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r'-?(?:0|[1-9]\d{0,12})(?:\.\d{1,12})?', value):
        raise ValueError('数值须为十进制字符串，最多12位小数，不接受指数、空值、布尔或NaN。')
    result = Decimal(value)
    if abs(result) > Decimal('1000000000000'):
        raise ValueError('数值绝对值不得超过1e12。')
    return Fraction(result)


def plain(value):
    value = Fraction(value)
    rendered = format(Decimal(value.numerator) / Decimal(value.denominator), 'f')
    return '0' if value == 0 else rendered.rstrip('0').rstrip('.') if '.' in rendered else rendered


def labels(model):
    for key, value in model.model_dump().items():
        if isinstance(value, str) and (not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError(f'{key}须为无首尾空格、无控制字符的非空文本。')


def observations(raw, chart, label, minimum):
    if not isinstance(raw, list) or not minimum <= len(raw) <= 500:
        raise ValueError(f'{label}须有{minimum}..500条有序观测。')
    items = [SPCObservation.model_validate(row) for row in raw]
    sizes = {len(row.values) for row in items}
    if len(sizes) != 1 or (chart == 'i_mr' and sizes != {1}) or (chart == 'xbar_r' and not sizes <= set(FACTORS)):
        raise ValueError('i_mr每条仅1个值；xbar_r为固定2..10个值的合理子组，不自动分组。')
    for row in items:
        labels(row)
    if len({row.observation_id for row in items}) != len(items):
        raise ValueError(f'{label}观测ID重复。')
    if any(b.sequence != a.sequence + 1 for a, b in zip(items, items[1:])):
        raise ValueError(f'{label}顺序必须连续递增；缺口或重排须先按实际过程分段。')
    values = [[number(v) for v in row.values] for row in items]
    means = [sum(v) / len(v) for v in values]
    ranges = ([abs(b[0] - a[0]) for a, b in zip(values, values[1:])] if chart == 'i_mr'
              else [max(v) - min(v) for v in values])
    return items, means, ranges, sizes.pop()


def signals(items, values, center, sigma, rules):
    found = []
    for end in range(len(values)):
        for rule in rules:
            window, count, zone = RULE_WINDOWS[rule]
            if end + 1 < window:
                continue
            start = end + 1 - window
            for side, sign in [('above', 1), ('below', -1)]:
                matching = [k for k in range(start, end + 1) if sign * (values[k] - center) > zone * sigma]
                if len(matching) >= count:
                    found.append({'rule': rule, 'side': side, 'window_start_id': items[start].observation_id,
                                  'window_end_id': items[end].observation_id,
                                  'contributing_ids': [items[k].observation_id for k in matching]})
    return found


def dispersion_signals(items, values, chart, lower, upper):
    result = []
    for k, value in enumerate(values):
        if value < lower or value > upper:
            end = k + 1 if chart == 'i_mr' else k
            result.append({'rule': 'outside_dispersion_limits', 'side': 'above' if value > upper else 'below',
                           'window_end_id': items[end].observation_id,
                           'contributing_ids': [row.observation_id for row in items[k:end + 1]]})
    return result


def analyze(chart_type, points, context, rules, baseline=None, baseline_reference=None, controls=None):
    if chart_type not in ('i_mr', 'xbar_r'):
        raise ValueError('控制图仅支持i_mr/xbar_r。')
    if not isinstance(rules, list) or not rules or len(rules) > 4 or any(r not in RULE_WINDOWS for r in rules) or len(set(rules)) != len(rules):
        raise ValueError('rules须显式选择1..4个不重复的受支持规则。')
    if (baseline is None) == (controls is None):
        raise ValueError('baseline与controls必须且只能提供一项；不使用待评估数据重算控制限。')
    ctx = SPCContext.model_validate(context); labels(ctx)
    with localcontext() as precision:
        precision.prec = 64
        items, values, ranges, size = observations(points, chart_type, 'points', 1)
        baseline_result = None
        if baseline is not None:
            ref = SPCContext.model_validate({**ctx.model_dump(), 'data_reference': baseline_reference})
            labels(ref)
            base_items, base_values, base_ranges, base_size = observations(baseline, chart_type, 'baseline', 2)
            if size != base_size or base_items[-1].sequence + 1 != items[0].sequence:
                raise ValueError('基线与评估段须子组大小相同且顺序相接；分段不连续时需明确固定控制配置。')
            if {r.observation_id for r in items} & {r.observation_id for r in base_items}:
                raise ValueError('基线与评估段观测ID不得重叠。')
            center = sum(base_values) / len(base_values)
            dc = sum(base_ranges) / len(base_ranges)
            if chart_type == 'i_mr':
                sigma, dl, du = dc / Fraction('1.128'), Fraction(0), dc * Fraction('3.267')
                constants = {'d2': '1.128', 'D3': '0', 'D4': '3.267'}
            else:
                a2, d3, d4 = map(Fraction, FACTORS[size])
                sigma, dl, du = a2 * dc / 3, d3 * dc, d4 * dc
                constants = dict(zip(('A2', 'D3', 'D4'), FACTORS[size]))
            if sigma <= 0:
                raise ValueError('基线变差为零，无法建立有效控制限；不输出全通过。')
            baseline_result = {'reference': baseline_reference, 'count': len(base_items), 'constants': constants,
                               'primary_signals': signals(base_items, base_values, center, sigma, rules),
                               'dispersion_signals': dispersion_signals(base_items, base_ranges, chart_type, dl, du),
                               'stability_verified': False}
            baseline_result['has_signals'] = bool(baseline_result['primary_signals'] or baseline_result['dispersion_signals'])
            origin = 'separate_baseline_estimate'
        else:
            if baseline_reference is not None:
                raise ValueError('固定控制配置模式不可另传baseline_reference。')
            cfg = SPCFixedControls.model_validate(controls); labels(cfg)
            if cfg.subgroup_size != size:
                raise ValueError('控制配置子组大小与数据不一致。')
            center, sigma, dc, dl, du = map(number, (cfg.center, cfg.sigma, cfg.dispersion_center, cfg.dispersion_lcl, cfg.dispersion_ucl))
            if sigma <= 0 or not 0 <= dl <= dc <= du or dl == du:
                raise ValueError('sigma须大于零，离散图控制限须0<=LCL<=中心<=UCL且LCL<UCL。')
            origin = 'caller_fixed_controls'
        primary = signals(items, values, center, sigma, rules)
        dispersion = dispersion_signals(items, ranges, chart_type, dl, du)
        request = {'chart_type': chart_type, 'points': [r.model_dump() for r in items], 'context': ctx.model_dump(),
                   'rules': rules, 'baseline': baseline, 'baseline_reference': baseline_reference, 'controls': controls}
        output = {'algorithm_version': VERSION, 'rule_version': 'weco-four-strict-v1', 'chart_type': chart_type,
                  'arithmetic': 'exact_rational_comparisons_decimal_display_64_significant_digits',
                  'input_digest': digest(request), 'input_snapshot': request,
                  'context': ctx.model_dump(), 'control_origin': origin,
                  'control_reference': baseline_reference if baseline is not None else cfg.reference,
                  'control_version': VERSION if baseline is not None else cfg.version,
                  'subgroup_size': size, 'selected_rules': rules, 'baseline': baseline_result,
                  'control_limits': {'primary': {'center': plain(center), 'sigma_of_statistic': plain(sigma),
                                                'lcl': plain(center - 3 * sigma), 'ucl': plain(center + 3 * sigma)},
                                     'dispersion': {'center': plain(dc), 'lcl': plain(dl), 'ucl': plain(du)}},
                  'points': [{'observation_id': row.observation_id, 'sequence': row.sequence, 'values': row.values,
                              'statistic': plain(value)} for row, value in zip(items, values)],
                  'dispersion_points': [{'end_id': items[k + 1 if chart_type == 'i_mr' else k].observation_id,
                                         'value': plain(value)} for k, value in enumerate(ranges)],
                  'primary_signals': primary, 'dispersion_signals': dispersion,
                  'has_signals': bool(primary or dispersion), 'signal_scope': 'points_only',
                  'dispersion_evaluated_points': len(ranges),
                  'evaluation_windows': {r: {'required': RULE_WINDOWS[r][0],
                                            'evaluated': max(0, len(items) - RULE_WINDOWS[r][0] + 1)} for r in rules},
                  'data_verified_against_mes': False, 'configuration_verified_against_mes': False,
                  'native_spc_result': False, 'ready_for_mes_write': False, 'release_authorized': False,
                  'assumptions_verified': False, 'sources': SOURCES,
                  'limitations': ['调用方提供同特性、同单位、同过程的有序数据；本工具未从MES核实来源或独立性/分布假设。',
                                  '基线估计是待审核控制限，基线有信号不剔除、不自动重算；基线/评估段不跨段计算MR或判异窗口。',
                                  'WECO仅用于单值或子组均值；MR/R仅逐点检查其控制限，重叠MR不套用WECO运行规则。',
                                  '等于边界不触发，等于中心线中断同侧连续；无信号不代表过程稳定、产品合格或允许放行。',
                                  '规格限不替代控制限；本地规则不宣称等同现场SPC引擎，不执行违例/注释写入、冻结或资源状态变更。']}
        return output


@mcp.tool
async def analyze_spc_series(
    chart_type: Chart,
    points: Annotated[list[SPCObservation], Field(min_length=1, max_length=500)],
    context: SPCContext,
    rules: Annotated[list[Rule], Field(min_length=1, max_length=4)],
    baseline: Annotated[list[SPCObservation] | None, Field(min_length=2, max_length=500)] = None,
    baseline_reference: str | None = None,
    controls: SPCFixedControls | None = None,
) -> str:
    """本地只读SPC计算与判异：i_mr单值/移动极差，xbar_r固定2..10件合理子组。

    points按sequence连续递增，值为十进制字符串；context明确特性、单位、过程、来源及顺序依据。
    单独baseline+baseline_reference估计控制限，或controls指定有版本的固定中心/sigma/离散图限；二者互斥。
    controls.sigma为绘制统计量的标准差，xbar_r是子组均值标准差，不是单件标准差。
    rules须显式选择；仅WECO四规则，逐窗口报告原ID；不补点/排序/去掉异常，不用规格限代控制限。
    不查询或写入MES，不作为现场引擎结果、合格判定或放行依据；无信号及窗口不足不能证明稳定。
    """
    def obj(value):
        return value.model_dump() if isinstance(value, BaseModel) else value
    try:
        return encode(analyze(chart_type, [obj(r) for r in points], obj(context), rules,
                              [obj(r) for r in baseline] if baseline is not None else None,
                              baseline_reference, obj(controls)))
    except (ValueError, TypeError) as exc:
        return encode({'error': str(exc), 'native_spc_result': False, 'ready_for_mes_write': False, 'release_authorized': False})
