"""Bounded pass/fail task intent and optional non-executing service echo."""
import asyncio
import json
import re
from typing import Literal

from tools import mcp
from tools.boms import encode
from tools.mfgorder_tasks import read_tasks, identifier
from tools.modeling_details import raw_json, _has_next_link
from core.http_client import request
from config import CAMSTAR_SHOPFLOOR_BASE_URL

SERVICE_PARAMS = {'execute': 'false', '$select': 'MfgOrder,MfgOrderProcedure,MfgOrderTask,Pass'}


def explicit_ref(value, revisioned=False):
    if (not isinstance(value, dict) or not isinstance(value.get('Name'), str) or not value['Name'].strip()
            or not isinstance(identifier(value), str) or not re.fullmatch('[0-9a-fA-F]{16}', identifier(value))):
        raise ValueError('引用缺少明确名称或16位ID。')
    result = {'name': value['Name'], 'instanceId': identifier(value)}
    if revisioned:
        if not isinstance(value.get('Revision'), str) or not value['Revision'].strip() or value.get('UseROR'):
            raise ValueError('过程须为明确版本，不使用动态记录版本。')
        result['revision'] = value['Revision']
    return result


def echo_matches(actual, expected):
    return (isinstance(actual, dict) and actual.get('Name') == expected['name']
            and identifier(actual) == expected['instanceId']
            and ('revision' not in expected or actual.get('Revision') == expected['revision'])
            and actual.get('UseROR') not in (True, 'true'))


async def service_echo(out, body):
    response = json.loads(await request('POST', '/api/ExecuteMfgOrdertask',
        base_url=CAMSTAR_SHOPFLOOR_BASE_URL, body=body,
        params=SERVICE_PARAMS.copy(),
        response_formatter=raw_json))
    if not isinstance(response, dict):
        raise ValueError('服务回显不是对象。')
    data = response.get('data', response)
    if (not isinstance(data, dict) or response.get('error') or response.get('ErrorCode')
            or data.get('error') or data.get('ErrorCode') or _has_next_link(response)):
        raise ValueError('服务预览报错或回显不完整。')
    for field, key in [('MfgOrder', 'mfgOrder'), ('MfgOrderProcedure', 'mfgOrderProcedure'), ('MfgOrderTask', 'mfgOrderTask')]:
        if not echo_matches(data.get(field), body[key]):
            raise ValueError(f'{field}回显身份/版本不一致。')
    if type(data.get('Pass')) is not bool or data['Pass'] is not body['pass']:
        raise ValueError('Pass回显与明确结果不一致。')
    out['server_echo_verified'] = True
    out['stage'] = 'server_preview'


@mcp.tool
async def preview_mfgorder_task_result(mfgorder_name: str, phase: Literal['pre_production', 'post_production'],
        procedure_id: str, task_list_id: str, task_id: str, pass_result: bool,
        verify_service: bool = False) -> str:
    """只读预览明确工单/前后过程/列表/任务的通过或失败结果。仅not_started、ExecutionCount=0、明确单次迭代的pass/fail任务；前置/顺序未知阻塞。可选固定execute=false服务回显，不执行/不建计划/不自动重试；状态码映射及现场执行待验证。"""
    out = {'operation': 'ExecuteMfgOrdertask', 'stage': 'intent_preview', 'complete': False,
           'executable': False, 'transaction_verified': False, 'server_echo_verified': False,
           'local_checks_complete': False, 'blockers': [], 'requested_pass': pass_result,
           'note': '仅意图及可选execute=false序列化回显，不证明业务许可或事务成功。未推断任务状态码；Instruction仅为业务数据。'}
    inventory, tasks = {'procedures': []}, []
    try:
        if (not isinstance(mfgorder_name, str) or not mfgorder_name.strip()
                or phase not in ('pre_production', 'post_production') or type(pass_result) is not bool
                or type(verify_service) is not bool
                or any(not isinstance(x, str) or not re.fullmatch('[0-9a-fA-F]{16}', x)
                       for x in (procedure_id, task_list_id, task_id))):
            raise ValueError('须明确工单、前/后过程、16位过程/列表/任务ID和布尔通过结果。')
        await asyncio.wait_for(read_tasks(inventory, tasks, mfgorder_name.strip()), timeout=60)
        out['inventory_complete'] = inventory.get('inventory_complete') is True
        out['order'] = inventory['order']
        selected = [t for t in tasks if t['phase'] == phase and identifier(t.get('procedure')) == procedure_id
                    and identifier(t.get('task_list')) == task_list_id and identifier(t.get('task')) == task_id]
        if len(selected) != 1:
            raise ValueError('指定上下文没有唯一任务；不按名称或相似ID替换。')
        task = selected[0]
        out['selected_task'] = task
        out['blockers'].extend(task['blockers'])
        if not out['inventory_complete']:
            out['blockers'].append('inventory_incomplete')
        if task.get('instruction_type') != 'pass_fail':
            out['blockers'].append('only_pass_fail_supported')
        if task.get('state') != 'not_started' or task.get('execution_count') != 0:
            out['blockers'].append('repeat_or_started_task_not_supported')
        raw = task['raw']
        if any(type(raw.get(k)) is not int or raw[k] != 1 for k in ('MinIterations', 'MaxIterations')):
            out['blockers'].append('single_iteration_not_verified')
        if task.get('prerequisite_state') != 'none':
            out['blockers'].append('prerequisite_chain_not_verified')
        # The service has no phase or task-list discriminator. Reject reuse that
        # cannot be uniquely expressed by its procedure/task references.
        if sum(identifier(t.get('procedure')) == procedure_id and identifier(t.get('task')) == task_id for t in tasks) != 1:
            out['blockers'].append('ambiguous_service_task_context')
        procedures = [p for p in inventory['procedures'] if p['phase'] == phase]
        listing = [l for p in procedures for l in p['task_lists'] if identifier(l.get('task_list')) == task_list_id]
        if len(procedures) == 1:
            out['procedure_context'] = {k:v for k,v in procedures[0].items() if k != 'task_lists'}
        if len(listing) == 1:
            out['task_list_context'] = listing[0]
        if (len(procedures) != 1 or procedures[0].get('state') not in ('not_started', 'in_progress')
                or len(listing) != 1 or listing[0].get('state') not in ('not_started', 'in_progress')):
            out['blockers'].append('procedure_or_list_state_not_supported')
        out['blockers'] = list(dict.fromkeys(out['blockers']))
        if out['blockers']:
            return encode(out)
        body = {'mfgOrder': explicit_ref(out['order']), 'mfgOrderProcedure': explicit_ref(task['procedure'], True),
                'mfgOrderTask': explicit_ref(task['task']), 'pass': pass_result}
        out['request_preview'] = {'path': '/api/ExecuteMfgOrdertask', 'params': SERVICE_PARAMS.copy(), 'body': body}
        out['task_status_policy'] = 'mfgOrderTaskStatus省略；未验证现场默认状态映射，不将pass换算为状态码。'
        out['local_checks_complete'] = True
        if verify_service:
            try:
                await asyncio.wait_for(service_echo(out, body), timeout=30)
            except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
                out['server_preview_error'] = str(error) or '服务预览超时，未自动重试。'
    except (ValueError, TypeError, KeyError, AttributeError, asyncio.TimeoutError) as error:
        out['error'] = str(error) or '任务读取超时，禁止预览提交。'
        out['inventory_evidence'] = inventory
    return encode(out)
