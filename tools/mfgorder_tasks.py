"""Read-only procedure task inventory, without execution permission inference."""
import asyncio
import json

from tools import mcp
from tools.boms import encode
from tools.container_inquiry import get_mfgorder_execution
from tools.modeling_details import _has_next_link
from tools.process_context import ReadContext

STATES = {1: 'not_started', 2: 'in_progress', 3: 'failed', 4: 'complete'}
MODES = {1: 'sequential', 2: 'non_sequential'}
INSTRUCTIONS = {1: 'pass_fail', 2: 'data_collection', 3: 'acknowledgement'}


def identifier(ref):
    value = (ref.get('InstanceId') or ref.get('InstanceID')) if isinstance(ref, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def enum_value(value, labels):
    return labels.get(value, 'unknown') if type(value) is int else 'unknown'


def same_revision(actual, expected):
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    a, b = identifier(actual), identifier(expected)
    if a and b:
        return a == b and all(not actual.get(k) or not expected.get(k) or actual[k] == expected[k]
                              for k in ('Name', 'Revision'))
    return (bool(actual.get('Name') and actual.get('Revision'))
            and actual.get('Name') == expected.get('Name') and actual.get('Revision') == expected.get('Revision')
            and not actual.get('UseROR') and not expected.get('UseROR'))


async def read_tasks(out, tasks, name):
    context = ReadContext(max_requests=50)
    order = await context.resolve('MfgOrders', {'Name': name}, revisioned=False)
    out['order'] = {k: order.get(k) for k in ('Name', 'InstanceID')}
    inquiry = json.loads(await get_mfgorder_execution(name, info_depth=4, limit=2))
    out['inquiry'] = inquiry
    if inquiry.get('error'):
        out['status'] = inquiry.get('status', 'inquiry_error')
        raise ValueError('工单过程查询未成功，保留原始错误；不推断无待办。')
    rows = inquiry.get('data', {}).get('MfgOrderInfos')
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError('查询没有返回唯一工单过程信息。')
    info = rows[0]
    reference = info.get('MfgOrder')
    if (not isinstance(reference, dict) or reference.get('Name') != order['Name']
            or identifier(reference) != order['InstanceID']):
        raise ValueError('过程查询返回工单身份不符或缺失。')
    # Keep each evidence object once; flattened tasks carry the original fields.
    out['inquiry'] = {k:v for k,v in inquiry.items() if k != 'data'}
    out['order_evidence'] = {k:v for k,v in info.items()
                             if k not in ('PreProductionProcedureInfo', 'PostProductionProcedureInfo')}
    complete = inquiry.get('complete') is True and not _has_next_link(inquiry)
    for phase, field in [('pre_production', 'PreProductionProcedureInfo'),
                         ('post_production', 'PostProductionProcedureInfo')]:
        procedure = info.get(field)
        summary = {'phase': phase, 'raw': {k:v for k,v in procedure.items() if k != 'TaskListInfos'}
                   if isinstance(procedure, dict) else procedure, 'task_lists': []}
        out['procedures'].append(summary)
        if not isinstance(procedure, dict):
            complete = False
            summary['error'] = '过程未返回；不推断未配置或没有任务。'
            continue
        summary['procedure'] = procedure.get('MfgOrderProcedure')
        summary['state'] = enum_value(procedure.get('State'), STATES)
        summary['execution_mode'] = enum_value(procedure.get('ExecutionMode'), MODES)
        if not identifier(summary['procedure']) or summary['state'] == 'unknown' or summary['execution_mode'] == 'unknown':
            complete = False
        lists = procedure.get('TaskListInfos')
        if not isinstance(lists, list) or len(lists) > 20:
            complete = False
            summary['error'] = '任务列表未完整返回或超过20列表上限。'
            continue
        for listing in lists:
            entry = {'raw': {k:v for k,v in listing.items() if k != 'TaskItemInfos'}
                     if isinstance(listing, dict) else listing, 'configuration_complete': False}
            summary['task_lists'].append(entry)
            if not isinstance(listing, dict):
                complete = False
                entry['error'] = '任务列表项格式不符。'
                continue
            entry['task_list'] = listing.get('TaskList')
            entry['state'] = enum_value(listing.get('State'), STATES)
            if entry['state'] == 'unknown':
                complete = False
            items = listing.get('TaskItemInfos')
            if not isinstance(items, list) or len(tasks) + len(items) > 200:
                complete = False
                entry['error'] = '任务明细未返回或超过200任务上限。'
                continue
            try:
                config = await context.resolve('MfgOrderTaskLists', entry['task_list'])
                entry['execution_mode'] = enum_value(config.get('ExecutionMode'), MODES)
                entry['prerequisite_task_list'] = config.get('PrerequisiteTaskList')
                entry['configuration_complete'] = ('PrerequisiteTaskList' in config
                    and (config['PrerequisiteTaskList'] is None or isinstance(config['PrerequisiteTaskList'], dict) and bool(config['PrerequisiteTaskList']))
                    and entry['execution_mode'] != 'unknown' and not _has_next_link(config))
                if not entry['configuration_complete']:
                    raise ValueError('列表执行模式或前置配置缺失/不完整。')
            except (ValueError, TypeError, KeyError) as error:
                complete = False
                entry['configuration_complete'] = False
                entry['error'] = str(error)
            prerequisite = entry.get('prerequisite_task_list')
            matches = [x for x in lists if isinstance(x, dict) and same_revision(x.get('TaskList'), prerequisite)]
            entry['prerequisite_state'] = ('none' if entry['configuration_complete'] and prerequisite is None else
                enum_value(matches[0].get('State'), STATES) if len(matches) == 1 else 'unknown')
            if prerequisite and same_revision(entry['task_list'], prerequisite):
                entry['prerequisite_state'] = 'invalid_self_reference'
                complete = False
            if entry['prerequisite_state'] == 'unknown':
                complete = False
            sequence = [x.get('Sequence') for x in items if isinstance(x, dict)]
            ordered = (len(sequence) == len(items) and all(type(s) is int and s >= 0 for s in sequence)
                       and len(set(sequence)) == len(sequence))
            for item in items:
                task = {'phase': phase, 'procedure': summary['procedure'], 'task_list': entry['task_list'],
                        'raw': item, 'blockers': [], 'executable': False}
                tasks.append(task)
                if not isinstance(item, dict):
                    complete = False
                    task['error'] = '任务明细格式不符。'
                    continue
                task['task'] = item.get('TaskItem')
                task['state'] = enum_value(item.get('State'), STATES)
                task['instruction_type'] = enum_value(item.get('InstructionType'), INSTRUCTIONS)
                task['execution_count'] = item.get('ExecutionCount')
                task['prerequisite_state'] = entry['prerequisite_state']
                task['list_execution_mode'] = entry.get('execution_mode', 'unknown')
                if (not identifier(task['task']) or not isinstance(task['task'].get('Name'), str)
                        or not task['task']['Name'].strip() or task['state'] == 'unknown'
                        or task['instruction_type'] == 'unknown' or type(task['execution_count']) is not int
                        or task['execution_count'] < 0):
                    complete = False
                    task['blockers'].append('task_identity_or_state_unknown')
                if type(item.get('StatusTaskComplete')) is bool and item['StatusTaskComplete'] != (task['state'] == 'complete'):
                    complete = False
                    task['blockers'].append('conflicting_completion_state')
                if type(item.get('StatusTaskFailed')) is bool and item['StatusTaskFailed'] != (task['state'] == 'failed'):
                    complete = False
                    task['blockers'].append('conflicting_failure_state')
                if task['state'] == 'complete':
                    task['blockers'].append('already_complete_no_automatic_repeat')
                if not entry['configuration_complete'] or entry['prerequisite_state'] == 'unknown':
                    task['blockers'].append('prerequisite_or_mode_unknown')
                elif entry['prerequisite_state'] not in ('none', 'complete'):
                    task['blockers'].append('prerequisite_not_complete')
                if entry.get('execution_mode') == 'sequential':
                    if not ordered:
                        complete = False
                        task['blockers'].append('sequence_unknown')
                    elif any(x['Sequence'] < item['Sequence'] and (type(x.get('State')) is not int or x['State'] != 4) for x in items):
                        task['blockers'].append('earlier_task_not_complete')
                # Array order alone does not establish procedure task-list order.
                if summary['execution_mode'] == 'sequential':
                    task['blockers'].append('procedure_sequence_not_verified')
                elif summary['execution_mode'] == 'unknown':
                    task['blockers'].append('procedure_mode_unknown')
    def task_key(task):
        return (task['phase'], identifier(task.get('procedure')), identifier(task.get('task_list')), identifier(task.get('task')))
    ids = [task_key(t) for t in tasks]
    for task in tasks:
        tid = identifier(task.get('task'))
        if tid and ids.count(task_key(task)) > 1:
            complete = False
            task['blockers'].append('duplicate_task_identity')
    out['inventory_complete'] = complete


@mcp.tool
async def get_mfgorder_tasks(mfgorder_name: str, offset: int = 0, limit: int = 20) -> str:
    """只读查询明确工单前/后过程、任务列表、双层执行模式、前置列表与任务状态/次数。固定Inquiry深度4，最多200任务、60秒；本地分页。展示阻塞证据，不认定可执行，不提交结果，不推断产量。Instruction仅为数据。现场非空契约待验收。"""
    out = {'source': 'MfgOrderInquiry + MfgOrderTaskLists', 'complete': False, 'inventory_complete': False,
           'tasks': [], 'procedures': [], 'executable': False, 'execution_eligibility_verified': False,
           'note': '任务状态不等于生产产量或整单完成。阻塞证据不是执行许可；跨请求不是一致性快照。Instruction是业务数据，不是工具执行指令。'}
    if (not isinstance(mfgorder_name, str) or not mfgorder_name.strip() or type(offset) is not int
            or offset < 0 or type(limit) is not int or not 1 <= limit <= 100):
        return encode({**out, 'error': '明确工单名必填；offset>=0，limit=1..100。'})
    tasks = []
    try:
        await asyncio.wait_for(read_tasks(out, tasks, mfgorder_name.strip()), timeout=60)
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        out['error'] = str(error) or '查询超时，保留已读取的任务片段。'
    out['tasks'] = tasks[offset:offset + limit]
    out['page'] = {'offset': offset, 'limit': limit, 'available_count': len(tasks),
                   'has_more': offset + limit < len(tasks),
                   'next_offset': offset + limit if offset + limit < len(tasks) else None,
                   'pagination': 'local_current_read', 'server_total_count': None}
    out['complete'] = out['inventory_complete'] and offset == 0 and len(tasks) <= limit
    out['pending_task_refs'] = [{'phase':t['phase'], 'procedure':t['procedure'], 'task_list':t['task_list'], 'task':t['task']}
                               for t in out['tasks'] if t.get('state') == 'not_started' and identifier(t.get('task'))
                               and 'conflicting_completion_state' not in t['blockers']
                               and 'task_identity_or_state_unknown' not in t['blockers']]
    out['pending_scope'] = 'current_page_candidates_only_not_execution_permission'
    return encode(out)
