"""Explicit container-based SamplingCurrentStatus selection and guarded updates."""
import json
from typing import Annotated

from pydantic import Field, StrictBool, StrictInt
from tools import mcp, quality as q, quality_inspection as i, quality_definitions as d
from tools import quality_sampling_references as tables
from tools.process_context import ReadContext
from tools.modeling_details import raw_json
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

COLUMNS = ('SamplingCurrentStatusId,CDOTypeId,ProductId,SamplingLotId,SamplingPlanId,SampleTestId,'
           'SpecId,ResourceId,VendorId,VendorItemId,AQLLevelId,CurrentInspectionLevelId,'
           'SamplePlanInspectionLevelId,CurrentSampleRate,SampleRateCounter,LastUpdateDate,ChangeCount')
OPTIONAL_IDS = ('SamplingLotId', 'SpecId', 'ResourceId', 'VendorId', 'VendorItemId', 'AQLLevelId',
                'CurrentInspectionLevelId', 'SamplePlanInspectionLevelId')


def selection(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 20
            or any(not q.valid_id(value) for value in values)):
        raise ValueError('必须明确选择1..20个16位十六进制当前采样状态ID。')
    # SQL Server char IDs may compare case-insensitively. Normalize hex before
    # uniqueness/ownership checks so case variants cannot duplicate selection.
    values = [value.lower() for value in values]
    if len(set(values)) != len(values):
        raise ValueError('状态ID不能重复。')
    return sorted(values)


async def product_state(name, revision):
    root = await ReadContext(3).resolve('Products', {'Name': q.text(name), 'Revision': q.text(revision, '产品版本')})
    ref = i.revision_ref(root)
    rows = await q.sql_rows(f"SELECT ProductId,ChangeCount FROM Product WHERE ProductId='{ref['instanceId']}'", 1)
    if len(rows) != 1 or rows[0].get('ProductId') != ref['instanceId']:
        raise ValueError('产品变更计数身份不一致。')
    return {'reference': ref, 'status': i.wire_int(root.get('Status'), '产品状态'),
            'change_count': i.wire_int(rows[0].get('ChangeCount'), '产品ChangeCount')}


def normalize(rows, product_id):
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or any(field not in row for field in COLUMNS.split(',')):
            raise ValueError('当前采样状态字段不完整。')
        for field in ('SamplingCurrentStatusId', 'ProductId', 'SamplingPlanId', 'SampleTestId'):
            if not q.valid_id(row[field]): raise ValueError('当前采样状态引用无效：'+field)
        key = row['SamplingCurrentStatusId']
        if key in seen or row['ProductId'] != product_id or i.wire_int(row['CDOTypeId'], '状态类型') != 8426:
            raise ValueError('当前采样状态重复、产品归属或类型不一致。')
        seen.add(key)
        for field in OPTIONAL_IDS:
            if row[field] in (None, ''): row[field] = None
            elif not q.valid_id(row[field]): raise ValueError('状态可选引用无效：'+field)
        for field in ('CDOTypeId', 'CurrentSampleRate', 'SampleRateCounter', 'ChangeCount'):
            row[field] = i.wire_int(row[field], field)
        if row['CurrentSampleRate'] < 1: raise ValueError('当前采样频率非正或未知。')
    return rows


async def selected_rows(product_id, ids):
    if not q.valid_id(product_id): raise ValueError('产品ID无效。')
    ids = selection(ids)
    clause = ','.join("'"+key+"'" for key in ids)
    rows = normalize(await q.sql_rows(f"SELECT TOP 21 {COLUMNS} FROM SamplingCurrentStatus "
        f"WHERE ProductId='{product_id}' AND SamplingCurrentStatusId IN ({clause}) ORDER BY SamplingCurrentStatusId", 21), product_id)
    if len(rows) != len(ids) or {r['SamplingCurrentStatusId'] for r in rows} != set(ids):
        raise ValueError('所选状态不存在、归属其他产品或没有完整返回。')
    return sorted(rows, key=lambda r: r['SamplingCurrentStatusId'])


@mcp.tool
async def get_current_sampling_status(product_name: str, product_revision: str,
        offset: StrictInt = 0, limit: StrictInt = 20) -> str:
    """只读固定产品版本的SamplingCurrentStatus当前页，含真实状态ID、计划/测试/工序/设备/供应商引用、当前频率及独立采样计数器和检验水平。批采样记录标为不适用于本调整工具，空页不等于无需检验。每页<=50，重读本页及产品检测可见变化；不是完整产品清单、历史、样本要求或放行判定。"""
    try:
        q.paging(offset, limit)
        if limit > 50: raise ValueError('每页最多50条状态。')
        product = await product_state(product_name, product_revision); pid = product['reference']['instanceId']
        sql = f"SELECT {COLUMNS} FROM SamplingCurrentStatus WHERE ProductId='{pid}' ORDER BY SamplingCurrentStatusId "+f'OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY'
        rows = normalize(await q.sql_rows(sql, limit+1), pid)
        if normalize(await q.sql_rows(sql, limit+1), pid) != rows or await product_state(product_name, product_revision) != product:
            raise ValueError('读取期间状态页或产品发生变化。')
        more = len(rows) > limit
        return q.encode({'product': product, 'rows': [{**row, 'container_based_candidate': row['SamplingLotId'] is None} for row in rows[:limit]],
                         'page_complete': True, 'complete': False, 'has_more': more, 'next_offset': offset+limit if more else None,
                         'quality_verdict': None, 'execution_readiness_verified': False,
                         'note': '计数器不是采样频率；当前页不是完整清单或事务因果证据，不据此推算下个容器必检或不检。'})
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return q.encode({'complete': False, 'quality_verdict': None, 'error': str(error)})


def echoed_ids(rows):
    if not isinstance(rows, list) or any(not isinstance(r, dict) or not q.valid_id(q.identifier(r)) for r in rows):
        raise ValueError('服务状态引用列表缺失或形状无效。')
    ids = [q.identifier(r) for r in rows]
    if len(ids) != len(set(ids)): raise ValueError('服务状态回显重复。')
    return set(ids)


def validate_transport(snapshot):
    body = snapshot['body']
    fields = {'product', 'samplingCurrentStatus', 'resetInspectionLevel', 'comments'}
    changes = {'newSampleRateCounterValue', 'newInspectionLevel'}
    product = body.get('product'); selected = body.get('samplingCurrentStatus')
    if (snapshot.get('kind') != 'current_sampling' or snapshot.get('action') != 'update'
            or not fields <= set(body) or set(body)-fields-changes
            or type(body.get('resetInspectionLevel')) is not bool
            or (not set(body) & changes and body['resetInspectionLevel'] is not True)
            or (body['resetInspectionLevel'] is True and 'newInspectionLevel' in body)
            or not isinstance(product, dict)
            or set(product) != {'name', 'revision', 'instanceId'} or not q.valid_id(snapshot.get('target_id'))
            or product.get('instanceId') != snapshot['target_id'] or product.get('name') != snapshot.get('name')
            or product.get('revision') != snapshot.get('revision') or not isinstance(selected, list)
            or any(not isinstance(r, dict) or set(r) != {'instanceId'} for r in selected)):
        raise ValueError('当前采样维护服务、产品、选择或允许字段不符。')
    keys = selection([r['instanceId'] for r in selected])
    expected = sorted(r['SamplingCurrentStatusId'] for r in snapshot['before']['statuses'])
    if keys != expected: raise ValueError('执行选择与已核对状态快照不符。')
    if 'newSampleRateCounterValue' in body: i.int32(body['newSampleRateCounterValue'], '执行采样计数器')
    level = body.get('newInspectionLevel')
    if 'newInspectionLevel' in body and (not isinstance(level, dict) or set(level) != {'name', 'instanceId'} or not q.valid_id(level.get('instanceId'))):
        raise ValueError('执行检验水平引用无效。')
    if level is not None: q.text(level.get('name'), '检验水平')


async def inspect_snapshot(snapshot):
    product = await product_state(snapshot['name'], snapshot['revision'])
    if product['reference']['instanceId'] != snapshot['target_id']: raise ValueError('当前产品身份与原计划不同。')
    ids = [r['instanceId'] for r in snapshot['body']['samplingCurrentStatus']]
    rows = await selected_rows(snapshot['target_id'], ids)
    if await selected_rows(snapshot['target_id'], ids) != rows: raise ValueError('证据读取期间状态发生变化。')
    return {'current_product': product, 'current_statuses': rows, 'verified_success': False,
            'state_changed': False, 'retry_allowed': False, 'history_correlation_verified': False,
            'note': '当前计数/水平相同不证明本次调整成功；没有已验证的最终历史链，不自动解锁或重试。'}


async def preview(body):
    from tools.quality_plans import wire_matches
    params = {'execute': 'false', '$select': 'Product,SamplingCurrentStatus,ServiceDetails,NewSampleRateCounterValue,NewInspectionLevel,ResetInspectionLevel,Comments'}
    raw = await request('POST', '/api/CurrentSamplingStatusUpdate', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                        body=body, params=params, response_formatter=raw_json)
    try: outer = json.loads(raw)
    except (ValueError, TypeError): raise ValueError('当前采样状态非执行预览失败：'+str(raw)[:1500]) from None
    if not isinstance(outer, dict): raise ValueError('服务响应形状无效。')
    data = outer.get('data', outer)
    if (not isinstance(data, dict) or outer.get('error') or outer.get('ErrorCode')
            or data.get('error') or data.get('ErrorCode') or q._has_next_link(data)):
        raise ValueError('服务响应错误或列表未完整返回。')
    if not wire_matches({k: v for k, v in body.items() if k != 'samplingCurrentStatus'}, data):
        raise ValueError('产品、调整值、保留选项或事务评论回显不一致。')
    expected = {r['instanceId'] for r in body['samplingCurrentStatus']}
    if echoed_ids(data.get('SamplingCurrentStatus')) != expected:
        raise ValueError('服务选中状态集合与明确选择不一致，不能扩大到全部产品记录。')
    holding = data.get('ServiceDetails')
    if holding not in (None, []) and echoed_ids(holding) != expected:
        raise ValueError('服务内部处理集合与选中状态集合不一致。')
    for wire, field in (('newSampleRateCounterValue', 'NewSampleRateCounterValue'), ('newInspectionLevel', 'NewInspectionLevel')):
        if wire not in body and data.get(field) not in (None, {}):
            raise ValueError('未请求的调整字段被服务填充默认值：'+field)
    return {'params': params, 'verified_input_echo': body, 'selected_status_ids': sorted(expected),
            'dynamic_validation_verified': False, 'existing_container_tasks_changed': False,
            'future_container_effects_verified': False, 'quality_verdict': None,
            'note': '仅调整明确记录，影响后续符合条件的容器；不改变当前频率、原计划定义或已有容器任务。手动检验水平可能影响后续切换规则评估。'}


async def table_state(kind, key):
    if not q.valid_id(key): raise ValueError('抽样表引用ID无效。')
    root = await ReadContext(1).resolve(tables.CONFIG[kind][0], {'InstanceId': key}, False)
    state = await tables.read_state(kind, root['Name'], key)
    if not state: raise ValueError('抽样表不存在。')
    tables.validate_table(kind, state[tables.CONFIG[kind][3]])
    return state


async def bind_reset_configuration(plan, test_id, dependencies, ctx):
    """Bind all configured table candidates without guessing MES precedence.

    Runtime defaults can also depend on spec/resource/vendor and switching
    context. These are dependency snapshots, never a resolved default level.
    """
    sources = [plan] + [row for row in plan['SamplingPlanDetails']
                        if q.identifier(row.get('SampleTest')) == test_id]
    for row in sources:
        for field, kind in (('InspectionLevel', 'inspection_level'), ('AQLLevel', 'aql_level')):
            ref = row.get(field)
            if not i.present(ref): continue
            root = await ctx.resolve(tables.CONFIG[kind][0], ref, False)
            key = root['InstanceID']; pair = (kind, key)
            if not q.valid_id(key): raise ValueError('复位关联配置ID无效。')
            if pair not in dependencies:
                if len(dependencies) >= 100: raise ValueError('复位关联依赖超过100个，不能用子集准备。')
                state = await tables.read_state(kind, root['Name'], key)
                if not state: raise ValueError('复位关联配置不存在。')
                # Do not evaluate formulas or claim literal lookup rules are
                # the effective MES default. Keep full, bounded raw tables.
                dependencies[pair] = state


async def build_update(args, marker):
    from tools import quality_plans as plans
    ids = selection(args['status_instance_ids']); counter = args.get('new_sample_rate_counter')
    level_name = args.get('new_inspection_level_name')
    reset = args.get('reset_inspection_level', False)
    if type(reset) is not bool: raise ValueError('reset_inspection_level须为明确布尔值。')
    if reset and level_name is not None: raise ValueError('恢复计划默认检验水平不能同时指定新的命名检验水平。')
    if counter is None and level_name is None and not reset: raise ValueError('必须明确新采样计数器、新检验水平或恢复计划默认。')
    if counter is not None: i.int32(counter, '新采样计数器')
    comment = i.inspection_comment(args.get('comments', ''))
    product = await product_state(args['product_name'], args['product_revision']); ref = product['reference']; pid = ref['instanceId']
    if product['status'] != 1: raise ValueError('产品不是Active。')
    rows = await selected_rows(pid, ids)
    if any(row['SamplingLotId'] is not None for row in rows): raise ValueError('所选含批采样状态，本工具只调整容器采样记录。')
    dependencies = {}; level = None
    if level_name is not None:
        level = await tables.read_state('inspection_level', level_name)
        if not level: raise ValueError('新检验水平不存在。')
        tables.validate_table('inspection_level', level['LotSizeDetails'])
        dependencies[('inspection_level', level['InstanceID'])] = level
    ctx = ReadContext(80)
    for row in rows:
        for kind, entity, column in [('sampling_plan', 'SamplingPlans', 'SamplingPlanId'), ('sample_test', 'SampleTests', 'SampleTestId')]:
            pair = (kind, row[column])
            if pair not in dependencies:
                root = await ctx.resolve(entity, {'InstanceId': row[column]})
                state = await d.read_state(kind, root['Name'], root['Revision'])
                if state['InstanceID'] != row[column] or state['Status'] != 1: raise ValueError('所选计划/测试不是固定Active版本。')
                dependencies[pair] = state
        plan = dependencies[('sampling_plan', row['SamplingPlanId'])]
        if not any(q.identifier(detail.get('SampleTest')) == row['SampleTestId'] for detail in plan['SamplingPlanDetails']):
            raise ValueError('当前计划已不包含所选状态的测试。')
        if reset:
            await bind_reset_configuration(plan, row['SampleTestId'], dependencies, ctx)
        if level is not None:
            pair = ('aql_level', row['AQLLevelId'])
            if pair not in dependencies: dependencies[pair] = await table_state(*pair)
            codes = {r['SampleSizeCode'] for r in dependencies[pair]['SampleSizeDetails']}
            if {r['SampleSizeCode'] for r in level['LotSizeDetails']} - codes:
                raise ValueError('新检验水平包含当前状态AQL表无法原样匹配的样本量代码。')
    if not reset and all((counter is None or counter == row['SampleRateCounter']) and
           (level is None or level['InstanceID'] == row['SamplePlanInspectionLevelId']) for row in rows):
        raise ValueError('全部所选记录均无实际调整。')
    body = {'product': ref, 'samplingCurrentStatus': [{'instanceId': key} for key in ids],
            'resetInspectionLevel': reset, 'comments': marker+(' '+comment if comment else '')}
    if counter is not None: body['newSampleRateCounterValue'] = counter
    if level is not None: body['newInspectionLevel'] = {'name': level['Name'], 'instanceId': level['InstanceID']}
    context = await preview(body)
    if reset:
        context.update(reset_inspection_level_requested=True, resolved_default_level_verified=False,
                       effective_default_level=None, reset_necessity_verified=False,
                       switching_rule_effects_verified=False, standard_compliance_verified=False,
                       note='仅请求MES恢复计划默认；不发送命名水平或未请求的计数器，不修改原计划、不回填已有任务。最终水平、切换及后续频率影响待验收。',
                       reset_semantics='native MES reset to sampling-plan definition; no locally predicted default or switching outcome')
    if await selected_rows(pid, ids) != rows or await product_state(args['product_name'], args['product_revision']) != product:
        raise ValueError('准备期间所选状态或产品发生变化。')
    for (kind, key), before in dependencies.items():
        after = (await tables.read_state(kind, before['Name'], key) if kind in tables.CONFIG else
                 await d.read_state(kind, before['Name'], before['Revision']))
        if after != before: raise ValueError('准备期间关联配置发生变化。')
    reads = set(d.resources('Products', {'Name': ref['name'], 'InstanceID': pid}))
    for (kind, _), state in dependencies.items(): reads.update(d.resources(tables.CONFIG[kind][0] if kind in tables.CONFIG else d.ENTITIES[kind], state))
    return {'kind': 'current_sampling', 'action': 'update', 'service': 'CurrentSamplingStatusUpdate',
            'name': ref['name'], 'revision': ref['revision'], 'target_id': pid, 'scope_key': 'current-sampling-product:'+pid,
            'before': {'product': product, 'statuses': rows}, 'body': body, 'dependencies': list(dependencies.values()),
            'resource_reads': sorted(reads), 'resource_writes': ['runtime:sampling-status:id:'+key for key in ids],
            'preview': context, 'marker': marker, 'arguments': args, 'environment': plans.environment()}


@mcp.tool
async def prepare_current_sampling_status_update(product_name: str, product_revision: str,
        status_instance_ids: Annotated[list[str], Field(min_length=1, max_length=20)],
        new_sample_rate_counter: StrictInt | None = None, new_inspection_level_name: str | None = None,
        comments: str = '', reset_inspection_level: StrictBool = False) -> str:
    """准备CurrentSamplingStatusUpdate，不写MES。先get_current_sampling_status取明确产品版本下1..20个状态ID；只支持SamplingLotId为空的容器采样记录。新计数器为非负整数，不是频率；或指定命名检验水平；或reset_inspection_level=true请求MES恢复计划默认，不能同时指定新命名水平，可同时明确计数器。复位绑定计划及候选表配置，不猜有效默认、切换结果或是否有实际变化。省略未修改字段且回显不得自动填值。仅后续符合条件容器生效，不回填已有任务、不改原计划、不全选、不处理批采样。统一确认单次执行，unknown不重试；inspect只读证据。"""
    from tools import quality_plans as plans
    return await plans.prepare('current_sampling', dict(product_name=product_name, product_revision=product_revision,
        status_instance_ids=status_instance_ids, new_sample_rate_counter=new_sample_rate_counter,
        new_inspection_level_name=new_inspection_level_name, comments=comments, reset_inspection_level=reset_inspection_level))
