import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from tests.test_mfgorder_tasks import env, listing, ORDER, LIST, PROC
from tools import mfgorder_task_preview as p, get_tool_func
from tools.tool_discovery import get_capabilities
from core.tool_catalog import TOOL_POLICIES

TASK = '0000000000000004'


@pytest.fixture
def preview_env(env, monkeypatch):
    listing(env)['TaskItemInfos'][0].update(MinIterations=1, MaxIterations=1)

    async def echo(method, path, **kwargs):
        assert method == 'POST' and path == '/api/ExecuteMfgOrdertask'
        assert kwargs['params'] == p.SERVICE_PARAMS and kwargs['params']['execute'] == 'false'
        body = kwargs['body']
        assert set(body) == {'mfgOrder', 'mfgOrderProcedure', 'mfgOrderTask', 'pass'}
        result = {'Pass': body['pass']}
        for title, key in [('MfgOrder', 'mfgOrder'), ('MfgOrderProcedure', 'mfgOrderProcedure'), ('MfgOrderTask', 'mfgOrderTask')]:
            result[title] = {'Name': body[key]['name'], 'InstanceId': body[key]['instanceId']}
            if 'revision' in body[key]: result[title]['Revision'] = body[key]['revision']
        return json.dumps(result)

    request = AsyncMock(side_effect=echo)
    monkeypatch.setattr(p, 'request', request)
    return env, request


def run(**changes):
    args = {'mfgorder_name': 'MO', 'phase': 'pre_production', 'procedure_id': PROC,
            'task_list_id': LIST, 'task_id': TASK, 'pass_result': True}
    args.update(changes)
    return json.loads(asyncio.run(p.preview_mfgorder_task_result(**args)))


@pytest.mark.parametrize('passed', [False, True])
def test_default_local_preview_no_service(preview_env, passed):
    out = run(pass_result=passed)
    assert out['local_checks_complete'] and out['stage'] == 'intent_preview'
    assert not out['complete'] and not out['executable'] and not out['transaction_verified']
    assert not out['server_echo_verified']
    body = out['request_preview']['body']
    assert body['pass'] is passed
    assert body['mfgOrder']['instanceId'] == ORDER
    assert body['mfgOrderProcedure']['revision'] == 'A'
    assert body['mfgOrderTask']['instanceId'] == TASK
    assert 'mfgOrderTaskStatus' not in body
    preview_env[1].assert_not_called()


@pytest.mark.parametrize('passed', [False, True])
def test_fixed_nonexecuting_service_echo(preview_env, passed):
    out = run(pass_result=passed, verify_service=True)
    assert out['server_echo_verified'] and out['stage'] == 'server_preview'
    assert not out['executable'] and not out['transaction_verified']
    assert preview_env[1].call_count == 1
    assert preview_env[1].call_args.kwargs['params'] == out['request_preview']['params']


@pytest.mark.parametrize('args', [{'phase':'other'}, {'task_id':'bad'}, {'procedure_id':"' OR 1=1"},
                                  {'pass_result':1}, {'pass_result':'false'}, {'verify_service':'true'},
                                  {'mfgorder_name':' '}])
def test_invalid_input_no_requests(preview_env, args):
    env, service = preview_env
    assert 'error' in run(**args)
    service.assert_not_called(); env[2].assert_not_called(); env[3].assert_not_called()


@pytest.mark.parametrize('args', [{'phase':'post_production'}, {'task_list_id':'0000000000000099'},
                                  {'procedure_id':'0000000000000099'}, {'task_id':'0000000000000099'}])
def test_context_must_match_exactly(preview_env, args):
    assert 'error' in run(verify_service=True, **args)
    preview_env[1].assert_not_called()


@pytest.mark.parametrize('change,blocker', [
    ('complete', 'repeat_or_started_task_not_supported'), ('started', 'repeat_or_started_task_not_supported'),
    ('count', 'repeat_or_started_task_not_supported'), ('data', 'only_pass_fail_supported'),
    ('ack', 'only_pass_fail_supported'), ('max', 'single_iteration_not_verified'),
    ('missing_min', 'single_iteration_not_verified'), ('boolean_max', 'single_iteration_not_verified'),
    ('sequence', 'procedure_sequence_not_verified'), ('prerequisite', 'prerequisite_chain_not_verified'),
    ('list_complete', 'procedure_or_list_state_not_supported'), ('paged', 'inventory_incomplete')])
def test_local_blocker_prevents_service(preview_env, change, blocker):
    env, service = preview_env
    item = listing(env)['TaskItemInfos'][0]
    if change == 'complete': item.update(State=4, StatusTaskComplete=True)
    if change == 'started': item['State'] = 2
    if change == 'count': item['ExecutionCount'] = 1
    if change == 'data': item['InstructionType'] = 2
    if change == 'ack': item['InstructionType'] = 3
    if change == 'max': item['MaxIterations'] = 2
    if change == 'missing_min': del item['MinIterations']
    if change == 'boolean_max': item['MaxIterations'] = True
    if change == 'sequence': env[0]['PreProductionProcedureInfo']['ExecutionMode'] = 1
    if change == 'prerequisite': env[1][LIST]['PrerequisiteTaskList'] = {'Name':'Other','Revision':'A'}
    if change == 'list_complete': listing(env)['State'] = 4
    if change == 'paged': listing(env)['TaskItemInfos@odata.nextLink'] = 'do-not-follow'
    out = run(verify_service=True)
    assert blocker in out['blockers'] and not out['local_checks_complete']
    assert 'request_preview' not in out
    service.assert_not_called()


def test_payload_cannot_disambiguate_reused_procedure_task(preview_env):
    env, service = preview_env
    env[0]['PostProductionProcedureInfo'] = copy.deepcopy(env[0]['PreProductionProcedureInfo'])
    assert 'ambiguous_service_task_context' in run(verify_service=True)['blockers']
    service.assert_not_called()


@pytest.mark.parametrize('change', ['revision', 'ror'])
def test_procedure_reference_must_be_fixed_revision(preview_env, change):
    ref = preview_env[0][0]['PreProductionProcedureInfo']['MfgOrderProcedure']
    if change == 'revision': del ref['Revision']
    if change == 'ror': ref['UseROR'] = True
    out = run(verify_service=True)
    assert 'error' in out and not out['local_checks_complete']
    preview_env[1].assert_not_called()


@pytest.mark.parametrize('field', ['MfgOrder', 'MfgOrderProcedure', 'MfgOrderTask', 'Pass', 'revision',
                                  'missing', 'paging', 'error', 'wrapped_error', 'nonobject', 'string_bool', 'name', 'ror'])
def test_bad_echo_never_verified(preview_env, field):
    _, service = preview_env
    original = service.side_effect

    async def bad(*args, **kwargs):
        result = json.loads(await original(*args, **kwargs))
        if field in ('MfgOrder', 'MfgOrderProcedure', 'MfgOrderTask'): result[field]['InstanceId'] = 'wrong'
        if field == 'Pass': result['Pass'] = False
        if field == 'revision': result['MfgOrderProcedure']['Revision'] = 'B'
        if field == 'missing': del result['MfgOrderTask']
        if field == 'paging': result['@odata.nextLink'] = 'untrusted-url'
        if field == 'error': result['ErrorCode'] = 'failed'
        if field == 'wrapped_error': result = {'error':'failed', 'data':result}
        if field == 'nonobject': result = []
        if field == 'string_bool': result['Pass'] = 'true'
        if field == 'name': result['MfgOrderTask']['Name'] = 'Other'
        if field == 'ror': result['MfgOrderProcedure']['UseROR'] = True
        return json.dumps(result)

    service.side_effect = bad
    out = run(verify_service=True)
    assert not out['server_echo_verified'] and not out['transaction_verified']
    assert 'server_preview_error' in out and service.call_count == 1


def test_service_timeout_no_retry(preview_env):
    preview_env[1].side_effect = asyncio.TimeoutError()
    out = run(verify_service=True)
    assert 'server_preview_error' in out and not out['server_echo_verified']
    assert preview_env[1].call_count == 1


def test_wrapped_success_and_no_retry_flag(preview_env):
    service = preview_env[1]
    original = service.side_effect
    async def wrapped(*args, **kwargs):
        assert not kwargs.get('retry_read_only')
        return json.dumps({'data': json.loads(await original(*args, **kwargs))})
    service.side_effect = wrapped
    assert run(verify_service=True)['server_echo_verified']


def test_inquiry_error_evidence_is_retained(preview_env):
    env, service = preview_env
    env[3].side_effect = None
    env[3].return_value = 'HTTP 400\nResponse: {"ErrorCode":"16780524"}'
    out = run(verify_service=True)
    assert out['inventory_evidence']['inquiry']['status'] == 'procedure_not_configured'
    service.assert_not_called()


def test_discovery_is_read_only():
    name = 'preview_mfgorder_task_result'
    assert get_tool_func(name) and TOOL_POLICIES[name].effect == 'read'
    detail = json.loads(asyncio.run(get_capabilities(tool_name=name)))['tool']
    assert {'phase','task_id','procedure_id','task_list_id','pass_result'} <= set(detail['input_schema']['required'])
