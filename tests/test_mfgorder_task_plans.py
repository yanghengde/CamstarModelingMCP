import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from tests.test_mfgorder_tasks import env, listing, ORDER, PROC, LIST
from tests.test_mfgorder_task_preview import preview_env, TASK
from tools import mfgorder_task_plans as p, get_tool_func
from agent.safety import plan_preview, evaluate_mutations, MutationCounts
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools


@pytest.fixture
def plan_env(preview_env, monkeypatch, tmp_path):
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'plans.sqlite')
    history = {'complete':True,'order':{'Name':'MO','InstanceID':ORDER},'tasks':[],'task_lists':[]}
    history_read = AsyncMock(side_effect=lambda *a, **kw: json.dumps(history))
    monkeypatch.setattr(p,'get_mfgorder_task_history',history_read)
    monkeypatch.setattr(p,'get_mfgorder_execution',AsyncMock(return_value='{"complete":true}'))
    write = AsyncMock(return_value='{"CompletionMsg":"accepted"}')
    monkeypatch.setattr(p,'request',write)
    return preview_env, history, history_read, write


def run(coro): return json.loads(asyncio.run(coro))


def prepare(passed=True):
    return run(p.prepare_mfgorder_task_result('MO','pre_production',PROC,LIST,TASK,passed))


@pytest.mark.parametrize('passed',[False,True])
def test_confirm_once_and_history_is_not_success(plan_env, passed):
    _,_,_,write = plan_env
    ready = prepare(passed)
    assert ready['state'] == 'ready'
    args = {'plan_id':ready['plan_id'],'plan_digest':ready['plan_digest']}
    calls = [{'function':{'name':'execute_mfgorder_task_result','arguments':json.dumps(args)}}]
    text = plan_preview(calls)
    assert 'mfgOrderTask' in text and 'pass' in text and 'unknown' in text and 'pre_production' in text
    assert evaluate_mutations(MutationCounts(), calls).requires_approval
    result = run(p.execute_mfgorder_task_result(**args))
    assert result['state'] == 'unknown' and not result['verified_success'] and not result['retry_allowed']
    assert len(result['evidence']) == 2
    assert write.call_args.args == ('POST','/api/ExecuteMfgOrdertask')
    assert write.call_args.kwargs['params'] == {'execute':'true'}
    assert write.call_args.kwargs['body']['pass'] is passed
    assert not run(p.execute_mfgorder_task_result(**args))['submitted']
    assert write.call_count == 1
    saved = run(p.get_mfgorder_task_plan(ready['plan_id']))
    assert saved['state'] == 'unknown' and saved['outcome']['attempt_id'] == result['attempt_id']


@pytest.mark.parametrize('change',['task','procedure','list','history','echo'])
def test_pre_submit_changes_block(plan_env, change):
    (env, echo), history, _, write = plan_env
    ready = prepare()
    if change == 'task': listing(env)['TaskItemInfos'][0]['LastComment'] = 'changed'
    if change == 'procedure': env[0]['PreProductionProcedureInfo']['State'] = 2
    if change == 'list': listing(env)['State'] = 2
    if change == 'history': history['task_lists'] = [{'new':'record'}]
    if change == 'echo': echo.side_effect = None; echo.return_value = '{}'
    assert not run(p.execute_mfgorder_task_result(ready['plan_id'],ready['plan_digest']))['submitted']
    write.assert_not_called()


@pytest.mark.parametrize('bad',['incomplete','wrong_order','executed','duplicate'])
def test_history_baseline_blocks(plan_env, bad):
    _, history, _, write = plan_env
    if bad == 'incomplete': history['complete'] = False
    if bad == 'wrong_order': history['order']['InstanceID'] = 'other'
    if bad in ('executed','duplicate'):
        row = {'task':{'InstanceId':TASK},'state':'not_started','execution_count':0}
        history['tasks'] = [row]
        if bad == 'executed': row.update(state='complete',execution_count=1)
        else: history['tasks'].append(copy.deepcopy(row))
    assert 'error' in prepare()
    write.assert_not_called()


def test_same_order_unknown_blocks_different_plan(plan_env):
    write = plan_env[3]
    first, second = prepare(), prepare(False)
    run(p.execute_mfgorder_task_result(first['plan_id'],first['plan_digest']))
    assert not run(p.execute_mfgorder_task_result(second['plan_id'],second['plan_digest']))['submitted']
    assert write.call_count == 1


def test_concurrent_same_plan_submits_once(plan_env):
    ready = prepare()
    async def both():
        return await asyncio.gather(*(p.execute_mfgorder_task_result(ready['plan_id'],ready['plan_digest']) for _ in range(2)))
    asyncio.run(both())
    assert plan_env[3].call_count == 1


@pytest.mark.parametrize('bad',['digest','expired','environment','payload','arguments'])
def test_invalid_stored_plan_no_write(plan_env, monkeypatch, bad):
    ready = prepare(); dg = ready['plan_digest']
    if bad == 'digest': dg = 'wrong'
    if bad == 'environment': monkeypatch.setattr(p,'CAMSTAR_SHOPFLOOR_BASE_URL','other')
    with p.db_connect() as db:
        if bad == 'expired': db.execute('UPDATE plans SET created=0')
        if bad in ('payload','arguments'):
            data = json.loads(db.execute('SELECT payload FROM plans').fetchone()[0])
            if bad == 'payload': data['snapshot']['body']['pass'] = False
            else: data['arguments']['pass_result'] = False
            db.execute('UPDATE plans SET payload=?',(json.dumps(data),))
    assert not run(p.execute_mfgorder_task_result(ready['plan_id'],dg))['submitted']
    plan_env[3].assert_not_called()


def test_claim_rechecks_storage_after_async_preview(plan_env, monkeypatch):
    ready = prepare(); original = p.checked_snapshot
    async def changed(args):
        result = await original(args)
        with p.db_connect() as db: db.execute('UPDATE plans SET created=0')
        return result
    monkeypatch.setattr(p,'checked_snapshot',changed)
    assert not run(p.execute_mfgorder_task_result(ready['plan_id'],ready['plan_digest']))['submitted']
    plan_env[3].assert_not_called()


def test_timeout_still_collects_evidence_no_retry(plan_env):
    ready = prepare(); plan_env[3].side_effect = TimeoutError('response lost')
    result = run(p.execute_mfgorder_task_result(ready['plan_id'],ready['plan_digest']))
    assert result['state'] == 'unknown' and 'submission_error' in result
    assert len(result['evidence']) == 2 and plan_env[3].call_count == 1


def test_one_evidence_failure_keeps_other(plan_env):
    ready = prepare()
    history = plan_env[1]
    plan_env[2].side_effect = [json.dumps(history),ValueError('denied')]
    result = run(p.execute_mfgorder_task_result(ready['plan_id'],ready['plan_digest']))
    assert 'error' in result['evidence'][0] and 'result' in result['evidence'][1]
    assert result['state'] == 'unknown'


@pytest.mark.parametrize('stage',['request','evidence'])
def test_cancellation_saves_unknown_and_does_not_retry(plan_env, stage):
    ready = prepare()
    if stage == 'request': plan_env[3].side_effect = asyncio.CancelledError()
    else: plan_env[2].side_effect = [json.dumps(plan_env[1]),asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        run(p.execute_mfgorder_task_result(ready['plan_id'],ready['plan_digest']))
    stored = run(p.get_mfgorder_task_plan(ready['plan_id']))
    assert stored['state'] == 'unknown' and stored['outcome']['interrupted']
    assert not run(p.execute_mfgorder_task_result(ready['plan_id'],ready['plan_digest']))['submitted']
    assert plan_env[3].call_count == 1


def test_extra_request_fields_rejected(plan_env, monkeypatch):
    original = p.preview_mfgorder_task_result
    async def extra(**args):
        result = json.loads(await original(**args))
        result['request_preview']['body']['skipValidation'] = True
        return json.dumps(result)
    monkeypatch.setattr(p,'preview_mfgorder_task_result',extra)
    assert 'error' in prepare()


def test_catalog_and_callable():
    for name in ['get_mfgorder_task_history','prepare_mfgorder_task_result','get_mfgorder_task_plan','execute_mfgorder_task_result']:
        assert get_tool_func(name)
    assert TOOL_POLICIES['execute_mfgorder_task_result'].always_confirm


def test_task_submission_route_keeps_read_queries_readonly():
    tools = [{'function':{'name':n}} for n in TOOL_POLICIES]
    for text, write in [('请提交工单MO的任务结果', True), ('请执行工单MO的任务', True),
                         ('查询工单MO任务执行记录', False), ('查看工单任务提交历史', False)]:
        selected = select_tools(tools, [{'role':'user','content':text}])
        names = {t['function']['name'] for t in selected}
        assert ('execute_mfgorder_task_result' in names) == write
        assert 'get_mfgorder_task_history' in names


def test_other_environment_unknown_does_not_block(plan_env):
    ready = prepare()
    with p.db_connect() as db:
        row = db.execute('SELECT payload FROM plans').fetchone()
        data = json.loads(row[0]); data['snapshot']['environment'] = 'other'
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)',
                   ('other',p.digest(data['snapshot']),json.dumps(data),'unknown',0))
    assert run(p.execute_mfgorder_task_result(ready['plan_id'],ready['plan_digest']))['state'] == 'unknown'
    assert plan_env[3].call_count == 1


def test_failure_flag_contradiction_blocks_plan(plan_env):
    env = plan_env[0][0]
    listing(env)['TaskItemInfos'][0]['StatusTaskFailed'] = True
    assert 'error' in prepare()
    plan_env[3].assert_not_called()


def test_other_plan_cannot_overlap_active_submission(plan_env):
    first, second = prepare(), prepare(False)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def pending(*a, **kw):
            entered.set()
            await release.wait()
            return '{}'
        plan_env[3].side_effect = pending
        task = asyncio.create_task(p.execute_mfgorder_task_result(first['plan_id'],first['plan_digest']))
        await entered.wait()
        try:
            blocked = json.loads(await p.execute_mfgorder_task_result(second['plan_id'],second['plan_digest']))
            assert not blocked['submitted']
        finally:
            release.set()
            await task
    asyncio.run(scenario())
    assert plan_env[3].call_count == 1
