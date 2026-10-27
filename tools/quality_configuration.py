"""Bounded, read-only sampling configuration diagnostics using existing contracts.

This checks stored definitions. It never predicts frozen MES task requirements,
evaluates switching rules, or certifies a sampling standard or quality result.
"""
import asyncio
import copy

from tools import mcp, quality as q, quality_definitions as d
from tools import quality_inspection as i, quality_sampling_references as s
from tools.process_context import ReadContext

MAX_DEPENDENCIES = 100
MAX_POINT_LINKS = 100
FLAGS = {'submitted': False, 'execution_readiness_verified': False,
         'mes_task_requirements_verified': False, 'standard_compliance_verified': False,
         'quality_verdict': None, 'atomic_snapshot_verified': False}


class Audit:
    def __init__(self):
        self.issues = []
        self.limitations = []
        self.complete = True
        self.states = {}
        self.references = []
        self.failed = set()
        self.ctx = ReadContext(220)
        self.point_links = 0

    def issue(self, code, path, message, incomplete=False):
        self.issues.append({'code': code, 'path': path, 'message': str(message)[:1000]})
        if incomplete:
            self.complete = False

    def limited(self, code, path, message):
        self.limitations.append({'code': code, 'path': path, 'message': message})

    async def load(self, kind, ref):
        named = kind in s.CONFIG
        entity = s.CONFIG[kind][0] if named else d.ENTITIES[kind]
        if not isinstance(ref, dict) or not i.present(ref):
            raise ValueError('配置引用未返回，不能推断对象。')
        # Resolve the original reference again at the end, including UseROR.
        root = await self.ctx.resolve(entity, ref, not named)
        key = root.get('InstanceID')
        if not q.valid_id(key):
            raise ValueError('引用没有有效ID。')
        identity = (kind, key)
        if identity in self.failed:
            raise ValueError('该依赖已读取失败。')
        if identity not in self.states:
            if len(self.states) + len(self.failed) >= MAX_DEPENDENCIES:
                raise ValueError('依赖对象超过100个，不将子集当完整配置。')
            try:
                state = (await s.read_state(kind, root['Name'], key) if named else
                         await d.read_state(kind, root['Name'], root['Revision']))
                if not state or state.get('InstanceID') != key:
                    raise ValueError('完整配置与原引用ID不一致。')
                self.states[identity] = state
            except (ValueError, TypeError, KeyError, AttributeError):
                self.failed.add(identity)
                raise
        self.references.append((entity, copy.deepcopy(ref), not named, key))
        return self.states[identity]

    async def optional_table(self, kind, ref, path):
        try:
            if not i.present(ref):
                return None
            state = await self.load(kind, ref)
            s.validate_table(kind, state[s.CONFIG[kind][3]])
            return state
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            self.issue('table_unverified', path, error, True)
            return None

    async def verify_stable(self):
        # No cached resolution on this pass: detect a changed ROR pointer too.
        fresh = ReadContext(220)
        for entity, ref, revisioned, key in self.references:
            if (await fresh.resolve(entity, ref, revisioned)).get('InstanceID') != key:
                raise ValueError('读取期间引用解析目标发生变化。')
        for (kind, key), before in self.states.items():
            after = (await s.read_state(kind, before['Name'], key) if kind in s.CONFIG else
                     await d.read_state(kind, before['Name'], before['Revision']))
            if before != after:
                raise ValueError('读取期间配置发生变化：'+before['Name'])

    async def check_test(self, ref, path):
        try:
            test = await self.load('sample_test', ref)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            self.issue('test_unverified', path, error, True)
            return None
        if test['Status'] != 1:
            self.issue('test_inactive', path, '检验测试不是Active。')
        try:
            sample_type = i.wire_int(test.get('SampleType'), '采样类型')
        except ValueError as error:
            self.issue('sample_type_unknown', path, error, True)
            sample_type = None
        if sample_type not in (None, 1, 2):
            self.limited('sample_type_unsupported', path, '当前提交工具仅支持计数1或计量2，未覆盖此采样类型。')
        refs = test.get('SampleDataPoints')
        if not isinstance(refs, list) or len(refs) > 20:
            self.issue('points_incomplete', path, '检验点列表缺失或超过20个。', True)
            return {'test': i.revision_ref(test), 'sample_type': sample_type}
        self.point_links += len(refs)
        if self.point_links > MAX_POINT_LINKS:
            raise ValueError('计划检验点关联超过100个，不检查分页子集。')
        if sample_type == 2 and not refs:
            self.issue('measured_points_missing', path, '计量测试没有检验点。')
        names = set(); ids = set(); points = []
        for index, point_ref in enumerate(refs):
            pp = path+f'.SampleDataPoints[{index}]'
            try:
                point = await self.load('sample_data_point', point_ref)
                name = q.text(point['Name']); key = point['InstanceID']
                if name.casefold() in names or key in ids:
                    self.issue('duplicate_point', pp, '检验点名称或ID重复，不能提供唯一逐点值。')
                names.add(name.casefold()); ids.add(key)
                if point['Status'] != 1:
                    self.issue('point_inactive', pp, '检验点不是Active。')
                dtype = i.wire_int(point.get('DataType'), '检验点数据类型')
                if dtype not in (1, 2, 3, 9):
                    self.limited('point_type_unsupported', pp, '当前数值提交工具未支持此检验点类型。')
                else:
                    lo = point.get('LowerLimit'); hi = point.get('UpperLimit')
                    low = None if lo in (None, '') else i.numeric(lo, '下限')
                    high = None if hi in (None, '') else i.numeric(hi, '上限')
                    if low is not None and high is not None and low > high:
                        self.issue('limits_reversed', pp, '检验点下限大于上限。')
                points.append(i.revision_ref(point))
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                self.issue('point_unverified', pp, error, True)
        # These are limitations of the current submission tools, not assertions
        # that an otherwise valid MES configuration is wrong.
        try:
            effects = copy.deepcopy(test)
            effects['ScrapCountedRejectsByReason'] = i.wire_bool(
                test.get('ScrapCountedRejectsByReason'), '拒收报废配置')
            i.reject_effectful_definition({'test': effects})
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            self.limited('submission_effects_unverified', path, str(error))
        return {'test': i.revision_ref(test), 'sample_type': sample_type, 'points': points,
                'is_frozen': test['IsFrozen']}


async def audit_plan(name, revision):
    audit = Audit()
    plan = await audit.load('sampling_plan', {'Name': q.text(name), 'Revision': q.text(revision, '计划版本')})
    if plan['Status'] != 1:
        audit.issue('plan_inactive', 'plan', '抽样计划不是Active。')
    rows = plan.get('SamplingPlanDetails')
    if not isinstance(rows, list) or not rows or len(rows) > 50:
        raise ValueError('计划须有完整1..50条测试明细。')
    # Inspect defaults even when a detail overrides them. Do not silently skip
    # a broken but currently unused configured reference.
    defaults = {}
    for field, kind in (('AQLLevel', 'aql_level'), ('InspectionLevel', 'inspection_level')):
        defaults[field] = await audit.optional_table(kind, plan.get(field), 'plan.'+field)
    checked = []
    for index, row in enumerate(rows):
        path = f'plan.SamplingPlanDetails[{index}]'
        entry = {'detail_id': row['InstanceID'], 'test_definition': await audit.check_test(row.get('SampleTest'), path+'.SampleTest')}
        pair = {}; sources = {}
        for field, kind in (('AQLLevel', 'aql_level'), ('InspectionLevel', 'inspection_level')):
            try:
                override = i.present(row.get(field))
            except ValueError as error:
                audit.issue('reference_unverified', path+'.'+field, error, True)
                override = True
            pair[field] = (await audit.optional_table(kind, row.get(field), path+'.'+field)) if override else defaults[field]
            sources[field] = 'detail' if override else 'plan_default'
        entry['configured_table_sources'] = sources
        entry['runtime_table_selection_verified'] = False
        level = pair['InspectionLevel']; aql = pair['AQLLevel']
        if level and aql:
            missing = sorted({r['SampleSizeCode'] for r in level['LotSizeDetails']}
                             - {r['SampleSizeCode'] for r in aql['SampleSizeDetails']})
            entry['unmapped_inspection_codes'] = missing
            if missing:
                audit.issue('aql_codes_unmapped', path, '检验水平代码在所配置AQL表中缺少原样匹配：'+', '.join(missing))
            entry['configured_tables'] = {field: {'name': state['Name'], 'instanceId': state['InstanceID']}
                                          for field, state in pair.items()}
        else:
            audit.limited('table_pair_unresolved', path, '未取得完整AQL/检验水平配置对；全检或其他MES规则不在本检查范围。')
        for field in ('InspectAll', 'InspectAllContQty', 'InspectAllQtyOnFailure'):
            try:
                if i.wire_bool(row.get(field), field):
                    audit.limited('full_inspection_unsupported', path+'.'+field, '当前提交工具未覆盖此全检流程。')
            except ValueError as error:
                audit.issue('inspection_flag_unknown', path+'.'+field, error, True)
        # A row's zero/empty frequency can have MES default semantics; expose it
        # without inventing the effective runtime frequency.
        entry['stored_sample_rate'] = row.get('SampleRate')
        entry['plan_default_sample_rate'] = plan.get('SampleRate')
        entry['scope_references'] = {field: row.get(field) for field in ('Spec', 'Resource', 'Vendor', 'VendorItem', 'SwitchingRule')}
        checked.append(entry)
    await audit.verify_stable()
    return {'complete': audit.complete,
            'assessment': 'incomplete' if not audit.complete else 'issues_found' if audit.issues else 'no_issues_found',
            'plan': i.revision_ref(plan), 'plan_is_frozen': plan['IsFrozen'], 'details': checked,
            'issues': audit.issues, 'submission_limitations': audit.limitations,
            'dependencies': [{'kind': kind, **{k: row[k] for k in ('InstanceID', 'Name', 'ChangeCount')},
                              **({'Revision': row['Revision']} if 'Revision' in row else {})}
                             for (kind, _), row in audit.states.items()],
            'snapshot_rechecked': True, **FLAGS,
            'scope': 'stored plan/test/numeric-point and AQL/inspection-table checks only',
            'note': '无问题仅表示上述静态检查未发现问题。冻结表示禁止配置修改，不按冻结判不可检验。'
                    '不验证产品绑定、Spec/设备/供应商引用、切换规则、拒收原因组、频率运行时解析、权限、签名或现有任务；'
                    '未生成样本要求、质量判定或放行结论。'}


@mcp.tool
async def check_sampling_plan_configuration(sampling_plan_name: str, sampling_plan_revision: str) -> str:
    """只读检查明确名称/版本的检验计划：完整1..50条明细、测试/计量点缺项及状态、数值限、AQL/检验水平表和代码关联，列出现有提交工具不支持的全检/副作用等配置。最多100依赖、100点关联，末尾重读检测可见变化；超限、缺字段或断引用返回不完整。冻结仅限制配置修改，不视为禁止检验。仅静态配置诊断，不验证产品绑定/拒收原因组/切换规则或运行时任务，不代替MES样本要求、业务批准或质量判定，不写MES。"""
    try:
        return q.encode(await asyncio.wait_for(audit_plan(sampling_plan_name, sampling_plan_revision), 90))
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        return q.encode({'complete': False, 'assessment': 'incomplete', 'snapshot_rechecked': False,
                         **FLAGS, 'error': str(error) or '配置检查超时，不能将部分结果当完整。'})
