"""Isolated browser-test server. Uses simulated replies; never calls LLM or MES."""
import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PREVIEW = Path(os.environ['CHAT_PREVIEW_ROOT']).resolve()
os.environ['CHAT_RUN_DB'] = str(PREVIEW / 'runs.sqlite')

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from agent import memory
from web import routes
from web.chat_runs import RUNS

memory.SESSIONS_DIR = str(PREVIEW / 'sessions')
memory.MEMORY_FILE = str(PREVIEW / 'unused-legacy.json')
routes.CAMSTAR_USERNAME = 'preview'
memory.user_memories.clear()
pending, calls, ticks = {}, {}, {}


def event(data):
    return 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'


async def simulated_stream(username, message, session_id):
    calls[message] = calls.get(message, 0) + 1
    messages = memory.get_user_messages(username, session_id)
    messages.append({'role': 'user', 'content': message})
    memory.update_session_title(username, session_id, message[:30])
    memory.save_user_session(username, session_id)
    if message == '审批演示':
        pending[session_id] = True
        yield event({'type': 'done', 'reply': '演示操作等待确认。'})
        return
    if message == '确认执行':
        pending[session_id] = False
    yield event({'type': 'step', 'func': 'offline_preview'})
    yield event({'type': 'stream_start'})
    count = 250 if message == '需要停止' else 80 if message == '后台连续执行' else 12
    content = ''
    for i in range(count):
        await asyncio.sleep(.12)
        fragment = f'进度 {i + 1:02d}\n'
        content += fragment
        ticks[session_id] = i + 1
        yield event({'type': 'stream_chunk', 'content': fragment})
    content += '\n后台演示已完成。'
    yield event({'type': 'stream_chunk', 'content': '\n后台演示已完成。'})
    yield event({'type': 'stream_end'})
    messages.append({'role': 'assistant', 'content': content})
    memory.save_user_session(username, session_id)


async def workflow(username, session_id):
    return {'status': 'approval_required', 'approval': {'message': '仅用于离线测试的确认计划。'}} if pending.get(session_id) else {'status': 'complete'}


routes.chat_stream = simulated_stream
routes.workflow_endpoint = workflow


@asynccontextmanager
async def lifespan(app):
    RUNS.initialize()
    yield
    await RUNS.shutdown()


app = FastAPI(lifespan=lifespan)
app.add_api_route('/workflow/{username}/{session_id}', workflow, methods=['GET'])
app.include_router(routes.router)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


@app.get('/test/probe')
async def probe():
    return {'calls': calls, 'ticks': ticks, 'pending': pending, 'business_writes_executed': False}


if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    os.chdir(ROOT)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')
