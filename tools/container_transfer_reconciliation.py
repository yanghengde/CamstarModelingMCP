"""Compare current container state with a transfer plan without claiming causality."""
import asyncio
import json
from decimal import InvalidOperation

from tools import mcp
from tools.boms import encode
from tools import container_transfer_plans as plans
from tools.container_transfers import snapshot, quantity


def compare_quantities(check, current, original, changes, sign):
    for field, input_field in [('Qty', 'qty'), ('Qty2', 'qty2')]:
        before = quantity(original, field)
        actual = quantity(current, field)
        change = quantity({'status': {field: changes.get(input_field)}}, field)
        expected = change
        if sign:
            expected = before + sign * change if before is not None and change is not None else None
        unit = 'UOM' if field == 'Qty' else 'UOM2'
        same_unit = original['status'].get(unit) is not None and current['status'].get(unit) == original['status'][unit]
        status = 'unknown' if expected is None or expected < 0 or actual is None or not same_unit else 'match' if actual == expected else 'different'
        check['fields'].append({'field': field, 'expected': str(expected) if expected is not None else None,
                                'actual': str(actual) if actual is not None else None, 'comparison': status})


@mcp.tool
async def reconcile_container_transfer_plan(plan_id: str) -> str:
    """只读重查已尝试的装箱/拆箱/合批/拆批计划，比较父关系或双数量及单位；拆批按明确新名称定位，身份仅为当前发现。匹配不证明本次事务成功，不解除锁、不重试、不改状态；最多60秒。"""
    out={'plan_id':plan_id,'complete':False,'comparison':'unavailable','checks':[],
         'verified_success':False,'state_changed':False,'retry_allowed':False,
         'note':'当前状态不是事务关联证据。并发事务可能产生相同或不同状态，不能据此判定本次提交成功、失败或安全重试。'}
    try:
        with plans.db_connect() as db:
            row=db.execute('SELECT digest,payload,state FROM plans WHERE id=?',(plan_id,)).fetchone()
        if not row:
            raise ValueError('计划不存在。')
        data=json.loads(row[1]);snap=data['snapshot']
        out['plan_state']=row[2]
        if row[2] not in ('unknown','in_flight'):
            raise ValueError('仅核对已尝试或结果未知的计划。')
        if plans.digest(snap)!=row[0] or snap['environment']!=plans.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'):
            raise ValueError('计划摘要或环境不匹配。')
        op=snap['operation']
        if op not in ('Combine','Associate','Disassociate','Split'):
            raise ValueError('不支持此操作的状态核对。')
        out['operation']=op
        if op == 'Split':
            body = snap['body']
            details = body.get('toContainerDetails')
            if (snap['children'] or body.get('reduceQty') is not True or body.get('autoNumber') is not False
                    or not isinstance(details, list) or len(details) != 1
                    or not isinstance(details[0], dict)
                    or not isinstance(details[0].get('toContainerName'), str)
                    or not details[0]['toContainerName'].strip()
                    or details[0]['toContainerName'].casefold() == snap['parent']['container']['Name'].casefold()):
                raise ValueError('仅支持明确单个新名称且扣减来源的拆批计划。')
        async def compare():
            nodes=[snap['parent']]+[r['child'] for r in snap['children']]
            if op == 'Split':
                nodes.append(None)
            for old in nodes:
                new_target = old is None
                name = details[0]['toContainerName'] if new_target else old['container']['Name']
                check={'container_id':None if new_target else old['id'],'container_name':name,'fields':[]}
                out['checks'].append(check)
                try:
                    current=await snapshot(name)
                    if new_target:
                        if current['container']['Name'] != name or not current['id'] or current['id'] == snap['parent']['id']:
                            raise ValueError('新名称未解析为独立容器。')
                        check['container_id'] = current['id']
                        check['identity_basis'] = 'current_name_lookup_only'
                    elif current['id']!=old['id']:
                        raise ValueError('容器名称解析为其他ID。')
                    check['current_status']=current['status']
                    if op == 'Split':
                        compare_quantities(check, current, snap['parent'], details[0], 0 if new_target else -1)
                    elif op=='Combine':
                        detail=snap['body']['fromContainerDetails'][0]
                        sign=1 if old['id']==snap['parent']['id'] else -1
                        compare_quantities(check, current, old, detail, sign)
                    else:
                        expected=(snap['parent']['id'] if op=='Associate' else None) if old['id']!=snap['parent']['id'] else old['parent_id']
                        actual=current['parent_id']
                        status='unknown' if not current['parent_known'] else 'match' if (actual or None)==(expected or None) else 'different'
                        check['fields'].append({'field':'ParentContainerId','expected':expected,'actual':actual,'comparison':status})
                except (ValueError,TypeError,KeyError,AttributeError,InvalidOperation) as error:
                    check['error']=str(error)
        await asyncio.wait_for(compare(),timeout=60)
        fields=[f['comparison'] for c in out['checks'] for f in c['fields']]
        if any('error' in c for c in out['checks']) or 'unknown' in fields or not fields:
            out['comparison']='incomplete'
        else:
            out['comparison']='current_state_matches' if all(f=='match' for f in fields) else 'current_state_differs'
            out['comparison_complete']=True
    except (ValueError,TypeError,KeyError,asyncio.TimeoutError) as error:
        out['error']=str(error) or '核对超时，已保留读取结果。'
    return encode(out)
