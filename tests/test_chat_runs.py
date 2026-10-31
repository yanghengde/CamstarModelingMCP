"""Background chat execution, reconnect journals and duplicate-write prevention."""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from web.chat_runs import ChatRuns, RunConflict
from web import routes
from agent import memory


async def workflow(*_):
    return {'status': 'idle'}


def event(payload):
    return 'data: ' + json.dumps(payload) + '\n\n'


def test_detaching_subscription_does_not_cancel_execution_and_reconnect_replays_once(tmp_path):
    async def run():
        manager = ChatRuns(tmp_path / 'runs.sqlite')
        gate, started = asyncio.Event(), asyncio.Event()
        calls = []
        async def stream(*args):
            calls.append(args)
            started.set()
            yield event({'type': 'step', 'func': 'read_status', 'args': {'private': 'not-journaled'}})
            yield event({'type': 'stream_start'})
            yield event({'type': 'stream_chunk', 'content': '第一段'})
            await gate.wait()
            yield event({'type': 'stream_chunk', 'content': '第二段'})
            yield event({'type': 'stream_end'})
        item = manager.start('u', 's', 'query', 'request-1', 1, stream, workflow)
        task = manager.tasks[item['id']]
        subscription = manager.events('u', item['id'])
        first = await anext(subscription)
        await subscription.aclose()
        await started.wait()
        assert not task.done()
        assert manager.get('u', item['id'])['status'] == 'running'
        # A lost POST response and exact retry attaches to the same run.
        same = manager.start('u', 's', 'query', 'request-1', 1, stream, workflow)
        assert same['id'] == item['id']
        gate.set()
        await task
        all_frames = [part async for part in manager.events('u', item['id'])]
        cursor = int(first.splitlines()[0].split(':')[1])
        resumed = [part async for part in manager.events('u', item['id'], cursor)]
        assert resumed == all_frames[1:]
        assert len(calls) == 1
        assert '第一段' in ''.join(resumed) and '第二段' in ''.join(resumed)
        assert 'not-journaled' not in ''.join(all_frames)
        assert manager.get('u', item['id'])['status'] == 'completed'
    asyncio.run(run())


def test_same_session_serialized_but_another_session_can_run(tmp_path):
    async def run():
        manager = ChatRuns(tmp_path / 'runs.sqlite')
        gate = asyncio.Event()
        async def stream(*_):
            await gate.wait()
            yield event({'type': 'done', 'reply': 'done'})
        first = manager.start('u', 'one', 'a', 'r1', 1, stream, workflow)
        with pytest.raises(RunConflict):
            manager.start('u', 'one', 'b', 'r2', 1, stream, workflow)
        with pytest.raises(ValueError, match='request_id'):
            manager.start('u', 'two', 'changed', 'r1', 1, stream, workflow)
        second = manager.start('u', 'two', 'b', 'r2', 1, stream, workflow)
        assert first['id'] != second['id']
        assert manager.get('other', first['id']) is None
        gate.set()
        await asyncio.gather(*list(manager.tasks.values()))
    asyncio.run(run())


def test_explicit_cancel_is_different_from_detach_and_does_not_rollback(tmp_path):
    async def run():
        manager = ChatRuns(tmp_path / 'runs.sqlite')
        submitted = asyncio.Event()
        effects = []
        async def stream(*_):
            effects.append('already committed')
            submitted.set()
            yield event({'type': 'step', 'func': 'committed'})
            await asyncio.Event().wait()
        item = manager.start('u', 's', 'a', 'r1', 1, stream, workflow)
        task = manager.tasks[item['id']]
        await submitted.wait()
        assert manager.cancel('other', item['id']) is None
        manager.cancel('u', item['id'])
        await task
        assert effects == ['already committed']
        assert manager.get('u', item['id'])['status'] == 'cancelled'
        replay = ''.join([part async for part in manager.events('u', item['id'])])
        assert '不会撤销' in replay
    asyncio.run(run())


def test_cancel_immediately_after_creation_does_not_leave_queued_run(tmp_path):
    async def run():
        manager = ChatRuns(tmp_path / 'runs.sqlite')
        async def stream(*_):
            await asyncio.Event().wait()
            yield ''
        item = manager.start('u', 's', 'a', 'r1', 1, stream, workflow)
        task = manager.tasks[item['id']]
        manager.cancel('u', item['id'])
        await task
        assert manager.get('u', item['id'])['status'] == 'cancelled'
    asyncio.run(run())


def test_restart_keeps_events_marks_interrupted_and_never_reexecutes(tmp_path):
    manager = ChatRuns(tmp_path / 'runs.sqlite')
    manager.initialize()
    with manager.db() as db:
        db.execute("INSERT INTO runs VALUES('r','u','s','request','write',1,'running',1,NULL)")
        manager._append(db, 'r', {'type': 'stream_chunk', 'content': 'partial'})
    restored = ChatRuns(manager.path)
    restored.initialize()
    assert restored.latest('u', 's')['status'] == 'interrupted'
    assert not restored.tasks
    async def read():
        return ''.join([part async for part in restored.events('u', 'r')])
    replay = asyncio.run(read())
    assert 'partial' in replay and '不会自动重放' in replay


@pytest.mark.parametrize('mode,expected', [('approval', 'approval_required'), ('error', 'failed'), ('exception', 'failed'),
                                         ('limited', 'limited'), ('cancelled', 'cancelled'), ('incomplete', 'incomplete')])
def test_approval_never_autocontinues_and_errors_are_terminal(tmp_path, mode, expected):
    async def run():
        manager = ChatRuns(tmp_path / 'runs.sqlite')
        async def stream(*_):
            if mode == 'exception':
                raise RuntimeError('fixture failure')
            yield event({'type': 'error', 'message': 'failed'} if mode == 'error' else {'type': 'done', 'reply': '请确认'})
        async def state(*_):
            return {'status': expected if mode not in ('error', 'exception') else 'idle'}
        item = manager.start('u', 's', 'a', 'r1', 1, stream, state)
        await manager.tasks[item['id']]
        assert manager.get('u', item['id'])['status'] == expected
        assert not manager.tasks
    asyncio.run(run())


def test_shutdown_stops_worker_before_closing_runtime(tmp_path):
    async def run():
        manager = ChatRuns(tmp_path / 'runs.sqlite')
        async def stream(*_):
            await asyncio.Event().wait()
            yield ''
        item = manager.start('u', 's', 'a', 'r1', 1, stream, workflow)
        await manager.shutdown()
        assert manager.get('u', item['id'])['status'] == 'interrupted'
        assert not manager.tasks
    asyncio.run(run())


def test_api_is_idempotent_scoped_and_history_can_recover_latest_run(monkeypatch, tmp_path):
    async def run():
        manager = ChatRuns(tmp_path / 'runs.sqlite')
        monkeypatch.setattr(routes, 'RUNS', manager)
        monkeypatch.setattr(memory, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
        monkeypatch.setattr(memory, 'user_memories', {})
        monkeypatch.setattr(routes, 'user_memories', memory.user_memories)
        sid = memory.create_session('u')
        gate = asyncio.Event()
        calls = []
        async def stream(*args):
            calls.append(args)
            yield event({'type': 'stream_chunk', 'content': 'partial'})
            await gate.wait()
            yield event({'type': 'done', 'reply': 'done'})
        monkeypatch.setattr(routes, 'chat_stream', stream)
        monkeypatch.setattr(routes, 'workflow_endpoint', workflow)
        app = FastAPI()
        app.include_router(routes.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            body = {'username': 'u', 'session_id': sid, 'message': 'query', 'request_id': 'req'}
            response = await client.post('/api/chat/runs', json=body)
            assert response.status_code == 202
            item = response.json()
            assert (await client.post('/api/chat/runs', json=body)).json()['id'] == item['id']
            assert (await client.post('/api/chat/runs', json={**body, 'request_id': 'req2'})).status_code == 409
            assert (await client.post('/api/chat/runs', json={**body, 'session_id': 'missing'})).status_code == 404
            assert (await client.get(f"/api/chat/runs/{item['id']}?username=other")).status_code == 404
            assert (await client.get(f"/api/chat/runs/{item['id']}/events?username=other")).status_code == 404
            assert (await client.post(f"/api/chat/runs/{item['id']}/cancel?username=other")).status_code == 404
            history = (await client.get('/history/u', params={'session_id': sid})).json()
            assert history['run']['id'] == item['id'] and history['run']['history_start'] == 1
            assert (await client.get('/sessions/u')).json()['sessions'][0]['run']['id'] == item['id']
            task = manager.tasks[item['id']]
            gate.set()
            await task
            replay = await client.get(f"/api/chat/runs/{item['id']}/events?username=u")
            assert 'partial' in replay.text and 'done' in replay.text
            assert len(calls) == 1
            legacy = await client.post('/chat', json={**body, 'request_id': 'legacy', 'message': 'legacy'})
            assert legacy.status_code == 200 and 'event-stream' in legacy.headers['content-type']
            assert 'done' in legacy.text and len(calls) == 2
    asyncio.run(run())
