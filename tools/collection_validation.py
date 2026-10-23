"""Read-only static validation. Never constructs an unverified CollectData payload."""
from decimal import Decimal, InvalidOperation

from tools import mcp
from tools.boms import bom_ref, encode
from tools.data_collection import definition
from tools.process_context import ReadContext


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


@mcp.tool
async def validate_collection_values(values: dict, name: str = '', revision: str = '',
                                     instance_id: str | None = None, use_ror: bool = False) -> str:
    """只读校验 UserDataCollectionDef 的采集值（点名到值的对象）。明确版本/ID/ROR；检查必填、基本类型、常量限值。越限单列，不推断质量或允许提交；不换算单位，不写入，不支持分组采样。"""
    out = {'complete': False, 'executable': False, 'static_valid': None,
           'errors': [], 'warnings': [], 'checks': [], 'quality_verdict': None,
           'submission_contract_verified': False}
    try:
        if not isinstance(values, dict) or not values or len(values) > 200:
            raise ValueError('values 必须是包含 1..200 个采集点的对象。')
        if any(not isinstance(k, str) or not k.strip() for k in values):
            raise ValueError('采集点名称必须是非空字符串。')
        # Bound input and reject non-JSON / non-finite values without echoing them.
        import json
        try:
            serialized = json.dumps(values, allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError('采集值必须是有效 JSON，不能包含 NaN 或无穷大。')
        if len(serialized) > 20000:
            raise ValueError('采集值过长，请缩小范围。')
        data = await definition(ReadContext(max_requests=4),
                                bom_ref(name, revision, instance_id, use_ror), 'user', 0, 200)
        out['definition'] = {k: data['definition'].get(k) for k in ('InstanceID', 'Name', 'Revision')}
        points = data.get('raw_items', [])
        names = [p.get('Name') for p in points]
        if not data.get('complete') or len(names) != len(set(names)):
            raise ValueError('采集定义不完整或点名重复，不能完成校验。')
        out['errors'].extend({'point': k, 'code': 'unknown_point'} for k in values if k not in names)
        for p in points:
            key, kind = p['Name'], p['DataType']
            value = values.get(key)
            missing = key not in values or value is None or value == ''
            if missing:
                if p['IsRequired']:
                    out['errors'].append({'point': key, 'code': 'required'})
                elif key in values:
                    out['warnings'].append({'point': key, 'code': 'empty_optional_value'})
                continue
            check = {'point': key, 'value': value, 'type_valid': None, 'limits': {}}
            out['checks'].append(check)
            if kind in (1, 2, 3, 9):
                number = numeric(value) if not isinstance(value, str) else None
                valid = number is not None and (kind != 1 or number == number.to_integral_value())
            elif kind == 4:
                valid = isinstance(value, str)
            elif kind == 7:
                valid = isinstance(value, bool)
            else:
                out['warnings'].append({'point': key, 'code': 'type_contract_unverified'})
                continue
            check['type_valid'] = valid
            if not valid:
                out['errors'].append({'point': key, 'code': 'invalid_type'})
                continue
            if kind in (1, 2, 3, 9):
                for field, direction in [('LowerLimit', 'lower'), ('UpperLimit', 'upper')]:
                    bound = numeric(p.get(field))
                    state = 'unknown' if bound is None else ('outside' if
                            (number < bound if direction == 'lower' else number > bound) else 'within')
                    check['limits'][direction] = {'raw': p.get(field), 'status': state}
                    if state != 'within':
                        out['warnings'].append({'point': key, 'code': f'{direction}_limit_{state}'})
            check['limit_override_allowed'] = p.get('IsLimitOverrideAllowed')
        out['static_valid'] = False if out['errors'] else (
            None if any(c['type_valid'] is None for c in out['checks']) else True)
        out['warnings'].append({'code': 'dynamic_constraints_unverified',
                                'detail': '枚举、单位、动态必填和越限处理未验证；静态校验通过不代表可提交或质量合格。'})
    except (ValueError, TypeError, KeyError) as error:
        out['errors'].append({'code': 'validation_unavailable', 'detail': str(error)})
    return encode(out)
