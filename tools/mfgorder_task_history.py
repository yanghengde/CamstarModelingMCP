"""Current executed-task records are evidence, not a request/transaction audit."""
import asyncio
import json

from tools import mcp
from tools.boms import encode
from tools.modeling_details import get_mfgorder_details, _has_next_link
from tools.process_context import ReadContext
from tools.mfgorder_tasks import identifier, enum_value

# Modeling uses a different enumeration from MfgOrderInquiry (1..4).
EXECUTED_STATES = {0: 'not_started', 1: 'in_progress', 2: 'failed', 3: 'complete'}


@mcp.tool
async def get_mfgorder_task_history(mfgorder_name: str) -> str:
    """只读读取明确工单MfgOrderExecutedTask/TaskList当前执行记录，最多200条/60秒。TaskState为0..3，与Inquiry的1..4不同。保留次数、状态引用、时间及原字段；列表状态不套用任务枚举。不是不可变事务审计，不证明请求因果，不推断产量。"""
    out = {'source': 'MfgOrders:MfgOrderExecutedTask/MfgOrderExecutedTaskList', 'complete': False,
           'tasks': [], 'task_lists': [], 'transaction_verified': False,
           'record_scope': 'current_execution_records_not_transaction_audit',
           'note': 'Modeling TaskState=0..3，不能直接与Inquiry State=1..4比较数值。记录未包含已验证的过程/请求事务关联；空列表不证明从未执行。'}
    if not isinstance(mfgorder_name, str) or not mfgorder_name.strip():
        return encode({**out, 'error': '明确工单名必填。'})

    async def read():
        order = await ReadContext().resolve('MfgOrders', {'Name': mfgorder_name.strip()}, revisioned=False)
        out['order'] = {k: order.get(k) for k in ('Name', 'InstanceID')}
        raw = await get_mfgorder_details(order['InstanceID'], view='executed_tasks', limit=200)
        try:
            result = json.loads(raw)
        except (ValueError, TypeError):
            out['source_error'] = raw
            raise ValueError('执行记录响应不是JSON对象。') from None
        if not isinstance(result, dict) or result.get('error'):
            out['source_error'] = result
            raise ValueError('已执行任务读取失败。')
        data = result.get('data', {})
        if data.get('Name') != order['Name'] or identifier(data) != order['InstanceID']:
            raise ValueError('执行记录工单身份不一致。')
        out['pages'] = result.get('pages')
        tasks, lists = data.get('MfgOrderExecutedTask'), data.get('MfgOrderExecutedTaskList')
        if not isinstance(tasks, list) or not isinstance(lists, list) or len(tasks) > 200 or len(lists) > 200:
            raise ValueError('执行记录/列表缺失或超过200条上限。')
        out['task_lists'] = lists
        complete = result.get('complete') is True and not _has_next_link(result)
        for row in tasks:
            item = {'raw': row, 'state': 'unknown'}
            out['tasks'].append(item)
            if not isinstance(row, dict):
                complete = False
                item['error'] = '记录不是对象。'
                continue
            item.update(task=row.get('Task'), state=enum_value(row.get('TaskState'), EXECUTED_STATES),
                        execution_count=row.get('ExecutionCount'), status=row.get('TaskStatus'))
            if (not identifier(item['task']) or item['state'] == 'unknown'
                    or type(item['execution_count']) is not int or item['execution_count'] < 0):
                complete = False
                item['error'] = '任务身份、状态或次数未知。'
        if any(not isinstance(row, dict) or not identifier(row.get('MfgOrderTaskList')) for row in lists):
            complete = False
        out['complete'] = complete

    try:
        await asyncio.wait_for(read(), timeout=60)
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        out['error'] = str(error) or '执行记录读取超时，保留已取得证据。'
    return encode(out)
