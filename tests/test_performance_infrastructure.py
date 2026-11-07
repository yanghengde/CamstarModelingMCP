import asyncio
import copy
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pandas as pd
import pytest
from fastapi import FastAPI

from agent import context_budget, memory, langgraph_runtime as runtime
from agent.tool_routing import routing_instructions, select_tools, ROUTING_INSTRUCTION
from core import perf_logger
from core.run_context import current_run_id
from core.tool_catalog import TOOL_POLICIES
from core.retention import prune_uploads
from web import chat_runs, routes
from web.chat_runs import ChatRuns, QueueFull
from web.upload_limits import UploadLimitMiddleware


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


async def workflow(*_):
    return {'status': 'idle'}


@pytest.fixture(autouse=True)
def isolated_metrics(monkeypatch, tmp_path):
    monkeypatch.setattr(perf_logger, 'PERF_LOG_FILE', str(tmp_path / 'performance.jsonl'))
    monkeypatch.setattr(perf_logger, 'ENABLE_PERFORMANCE_LOG', True)


def test_queue_limits_fairness_idempotency_and_queued_cancel(tmp_path):
    async def run():
        manager = ChatRuns(tmp_path/'runs.sqlite', max_running=2, per_user=1, max_queued=2, queued_per_user=1)
        entered, gates = [], {name: asyncio.Event() for name in ('a1', 'a2', 'b1', 'c1')}
        async def stream(user, message, session):
            entered.append(session)
            await gates[session].wait()
            yield 'data: {"type":"done","reply":"ok"}\n\n'
        async def start(user, session):
            return await manager.astart(user, session, 'read', session, 0, stream, workflow)
        a1 = await start('a', 'a1')
        a2 = await start('a', 'a2')
        with pytest.raises(QueueFull):
            await start('a', 'a3')
        b1 = await start('b', 'b1')
        await until(lambda: set(entered) == {'a1', 'b1'})
        c1 = await start('c', 'c1')
        with pytest.raises(QueueFull):
            await start('d', 'd1')
        assert (await start('a', 'a1'))['id'] == a1['id']
        task = manager.tasks[a2['id']]
        await manager.acancel('a', a2['id'])
        await task
        assert 'a2' not in entered
        assert manager.get('a', a2['id'])['status'] == 'cancelled'
        gates['a1'].set()
        await until(lambda: 'c1' in entered)
        gates['b1'].set(); gates['c1'].set()
        await asyncio.gather(*list(manager.tasks.values()))
        assert not manager._waiting and not manager._running
        await manager.shutdown()
    asyncio.run(run())


def test_queue_timeout_never_calls_stream(tmp_path):
    async def run():
        manager = ChatRuns(tmp_path/'runs.sqlite', max_running=1, queue_timeout=.08)
        entered = []
        async def stream(user, message, session):
            entered.append(session)
            await asyncio.Event().wait()
            yield ''
        first = await manager.astart('a', 's1', 'read', '1', 0, stream, workflow)
        second = await manager.astart('b', 's2', 'read', '2', 0, stream, workflow)
        await manager.tasks[second['id']]
        assert entered == ['s1']
        assert manager.get('b', second['id'])['status'] == 'failed'
        await manager.shutdown()
        assert manager.get('a', first['id'])['status'] == 'interrupted'
    asyncio.run(run())


def test_zero_queue_budget_cannot_be_bypassed_by_per_user_limit(tmp_path):
    async def run():
        manager=ChatRuns(tmp_path/'runs.sqlite',max_running=4,per_user=1,max_queued=0,queued_per_user=8)
        async def stream(*_):
            await asyncio.Event().wait()
            yield ''
        await manager.astart('u','s1','read','1',0,stream,workflow)
        with pytest.raises(QueueFull):
            await manager.astart('u','s2','read','2',0,stream,workflow)
        await manager.shutdown()
    asyncio.run(run())


def test_disconnected_admission_still_launches_once_and_shutdown_waits_for_disk(monkeypatch, tmp_path):
    async def run():
        manager = ChatRuns(tmp_path/'runs.sqlite')
        entered, release = threading.Event(), threading.Event()
        original = manager._admit
        def slow(*args):
            result = original(*args)
            entered.set()
            assert release.wait(5)
            return result
        monkeypatch.setattr(manager, '_admit', slow)
        async def stream(*_):
            await asyncio.Event().wait()
            yield ''
        accept = asyncio.create_task(manager.astart('u','s','read','req',0,stream,workflow))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            accept.cancel()
            stop = asyncio.create_task(manager.shutdown())
            await asyncio.sleep(.02)
            assert not stop.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await accept
        await stop
        # initialize() after shutdown must not resubmit anything.
        assert manager.latest('u','s')['status'] == 'interrupted'
        assert not manager.tasks
    asyncio.run(run())


def test_run_metrics_correlate_threads_and_capture_loop_lag(tmp_path):
    async def run():
        manager = ChatRuns(tmp_path/'runs.sqlite')
        async def stream(*_):
            await asyncio.sleep(.03)
            time.sleep(.16)  # Deliberate event-loop stall for the monitor.
            await asyncio.sleep(.12)
            await asyncio.to_thread(perf_logger.record_perf, 'Tool_Execute', .01, 'u', 's', {'prompt':'same'})
            yield 'data: {"type":"done"}\n\n'
        item = await manager.astart('u','s','same','req',0,stream,workflow)
        await manager.tasks[item['id']]
        assert current_run_id.get() is None
        logs = perf_logger.get_perf_logs()
        assert {r['details']['run_id'] for r in logs} == {item['id']}
        total = next(r for r in logs if r['action']=='Run_Total')
        assert total['details']['event_loop_lag_max_ms'] > 30
        assert total['details']['queue_seconds'] >= 0
        assert total['details']['execution_seconds'] > .1
        assert perf_logger.get_perf_log_groups()['groups'][0]['totalTimeMs'] == total['duration_ms']
        await manager.shutdown()
    asyncio.run(run())


def test_event_commit_wakes_subscriber_without_polling(monkeypatch, tmp_path):
    async def run():
        manager = ChatRuns(tmp_path/'runs.sqlite')
        gate = asyncio.Event()
        async def stream(*_):
            await gate.wait()
            yield 'data: {"type":"done","reply":"saved"}\n\n'
        item = await manager.astart('u','s','read','1',0,stream,workflow)
        task = manager.tasks[item['id']]
        subscription = manager.events('u', item['id'])
        reads = []
        original = manager._event_page
        def count(*args):
            reads.append(1)
            return original(*args)
        monkeypatch.setattr(manager, '_event_page', count)
        async def collect():
            return ''.join([frame async for frame in subscription])
        collecting = asyncio.create_task(collect())
        await asyncio.sleep(.15)
        before = len(reads)
        await asyncio.sleep(.35)
        assert len(reads) == before
        gate.set()
        output = await asyncio.wait_for(collecting, 1)
        assert 'saved' in output
        await task
        assert not manager._notifications
    asyncio.run(run())


def test_budget_keeps_exact_plan_refs_tool_pairs_and_current_turn():
    plan = {'plan_id': 'p1', 'plan_digest':'digest1','state':'unknown',
            'container': {'InstanceID':'0000000000000123','Name':'SN1','Revision':'A'}}
    messages = [{'role':'system','content':'policy'}, {'role':'user','content':'给SN1准备计划'},
                {'role':'assistant','tool_calls':[{'id':'call1','function':{'name':'prepare','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'call1','content':json.dumps(plan)},
                {'role':'assistant','content':'old narration '*1000},
                {'role':'user','content':'先不要执行'}, {'role':'assistant','content':'待确认'},
                {'role':'user','content':'对象改为SN2，重新准备'}]
    original = copy.deepcopy(messages)
    trimmed, metrics = context_budget.budget_history(messages, budget=1000)
    assert messages == original and metrics['history_removed_messages'] == 1
    assert messages[2] in trimmed and messages[3] in trimmed
    assert trimmed[-1] == messages[-1]
    with pytest.raises(context_budget.ContextBudgetExceeded):
        context_budget.budget_history(messages, budget=100)


def test_budget_stops_graph_before_model_and_preserves_state(monkeypatch):
    monkeypatch.setattr(context_budget, 'AGENT_HISTORY_TOKEN_BUDGET', 512)
    monkeypatch.setattr(runtime, 'build_runtime_experience_context', lambda *a: '')
    monkeypatch.setattr(runtime, 'get_stream_writer', lambda: lambda _: None)
    create = AsyncMock(side_effect=AssertionError('must not call model'))
    from types import SimpleNamespace
    monkeypatch.setattr(runtime, '_client', SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    messages = [{'role':'user','content':'不要执行。' * 200}]
    result = asyncio.run(runtime._agent_node({'messages':messages,'loop_count':0}))
    assert result['stop_reason']=='context_budget'
    assert result['messages'][0] == messages[0]
    create.assert_not_awaited()


def test_budget_retains_old_assistant_object_and_approval_references():
    evidence = {'role':'assistant','content':'对象 SN-ABC 的计划状态为 unknown，请核实后继续。'}
    messages = [{'role':'user','content':'之前的任务'}, evidence,
                {'role':'user','content':'第二轮'}, {'role':'user','content':'继续'}]
    with pytest.raises(context_budget.ContextBudgetExceeded):
        context_budget.budget_history(messages, budget=40)
    result, _ = context_budget.budget_history(messages, budget=1000)
    assert evidence in result


def test_domain_rules_follow_selected_tools_and_elliptical_topic():
    tools = [{'function':{'name':name}} for name in TOOL_POLICIES]
    plain = routing_instructions(select_tools(tools,[{'role':'user','content':'查询工艺路线'}]))
    assert 'prepare_quality_event_data_update' not in plain
    assert len(plain) < len(ROUTING_INSTRUCTION) / 2
    messages = [{'role':'user','content':'查看质量事件扩展资料'}, {'role':'assistant','content':'已有事件'},
                {'role':'user','content':'继续'}]
    assert 'prepare_quality_event_data_update' in routing_instructions(select_tools(tools,messages))


def test_lazy_sessions_do_not_read_payload_on_restart_or_listing(monkeypatch, tmp_path):
    monkeypatch.setattr(memory,'SESSIONS_DIR',str(tmp_path/'sessions'))
    monkeypatch.setattr(memory,'MEMORY_FILE',str(tmp_path/'legacy.json'))
    monkeypatch.setattr(memory,'user_memories',{})
    sid=memory.create_session('u')
    memory.get_user_messages('u',sid).append({'role':'user','content':'object SN1'})
    memory.save_user_session('u',sid)
    memory.init_memory()
    session=memory.user_memories['u']['sessions'][sid]
    assert 'messages' not in session
    assert memory.get_sessions('u')[0]['has_messages']
    assert 'messages' not in session
    session['auto_confirm']=True
    memory.save_session('u',sid)
    memory.init_memory()
    assert memory.session_auto_confirm('u',sid)
    assert memory.get_user_messages('u',sid)[-1]['content']=='object SN1'


def test_same_prompt_different_runs_and_snapshot_new_step(tmp_path):
    def record(run_id, duration):
        token=current_run_id.set(run_id)
        try: perf_logger.record_perf('Tool_Execute',duration,'u','s',{'prompt':'same'})
        finally: current_run_id.reset(token)
    record('one',1); record('two',2)
    first=perf_logger.get_perf_log_groups(limit=1)
    record('one',10)
    second=perf_logger.get_perf_log_groups(limit=1,cursor=first['next_cursor'])
    assert len(first['groups'][0]['steps'])==len(second['groups'][0]['steps'])==1
    assert {first['groups'][0]['runId'],second['groups'][0]['runId']}=={'one','two'}
    assert len(perf_logger.get_perf_log_groups()['groups'])==2


def test_retention_preserves_approval_and_identity_and_expires_log_cursor(tmp_path):
    manager=ChatRuns(tmp_path/'runs.sqlite');manager.initialize()
    with manager.db() as db:
        for name,status in [('done','completed'),('pending','approval_required')]:
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',(name,'u',name,name,'read',0,status,1,2))
            manager._append(db,name,{'type':'done'})
    assert manager.prune_events(1)==1
    assert manager.get('u','done')['id']=='done'
    with manager.db() as db:
        assert db.execute('SELECT run_id FROM events').fetchone()[0]=='pending'
    path=Path(perf_logger.PERF_LOG_FILE)
    records=[{'timestamp':'2000-01-01 00:00:00','duration_ms':1,'action':'llm','details':{'prompt':p}} for p in ('a','b')]
    path.write_text(''.join(json.dumps(r)+'\n' for r in records),encoding='utf-8')
    cursor=perf_logger.get_perf_log_groups(1)['next_cursor']
    assert perf_logger.prune_perf_logs(1)==2
    with pytest.raises(ValueError,match='过期'):
        perf_logger.get_perf_log_groups(cursor=cursor)
    assert not perf_logger.get_perf_logs()


def test_upload_limits_chunked_and_cleanup(monkeypatch,tmp_path):
    monkeypatch.setattr(routes,'UPLOAD_DIR',str(tmp_path/'uploads'))
    monkeypatch.setattr(routes,'UPLOAD_MAX_BYTES',32)
    app=FastAPI();app.include_router(routes.router);app.add_middleware(UploadLimitMiddleware,limit=32)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.post('/api/upload',files={'file':('ok.csv',b'a'*32)})).status_code==200
            assert (await client.post('/api/upload',files={'file':('too.csv',b'a'*33)})).status_code==413
            async def oversized():
                yield b'--x\r\nContent-Disposition: form-data; name="file"; filename="large.csv"\r\n\r\n'
                yield b'a'*70000
                yield b'\r\n--x--\r\n'
            response=await client.post('/api/upload',content=oversized(),headers={'Content-Type':'multipart/form-data; boundary=x'})
            assert response.status_code==413
    asyncio.run(run())
    assert len(list((tmp_path/'uploads').iterdir()))==1


def test_sheet_limit_and_protected_upload_retention(monkeypatch,tmp_path):
    from tools import excel_importer, import_plans
    path=tmp_path/'many.xlsx'
    with pd.ExcelWriter(path) as writer:
        for name in ('one','two'): pd.DataFrame({'x':[1]}).to_excel(writer,sheet_name=name,index=False)
    monkeypatch.setattr(excel_importer,'EXCEL_MAX_SHEETS',1)
    monkeypatch.setattr(import_plans,'EXCEL_MAX_SHEETS',1)
    assert '上限' in asyncio.run(excel_importer.parse_excel_schema(str(path)))
    with pytest.raises(ValueError,match='上限'):
        import_plans._read_import_frame(str(path),{'sheet_name':'one'},0)
    uploads=tmp_path/'uploads';uploads.mkdir()
    protected=uploads/('a'*32+'_pending.csv');expired=uploads/('b'*32+'_old.csv')
    for f in (protected,expired): f.write_text('x');os.utime(f,(1,1))
    manager=ChatRuns(tmp_path/'runs.sqlite');manager.initialize()
    with manager.db() as db:
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',('p','u','s','p',str(protected),0,'approval_required',1,2))
    assert prune_uploads(manager,uploads,1)==1
    assert protected.exists() and not expired.exists()
