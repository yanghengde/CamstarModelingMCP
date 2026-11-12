"""Real-browser chat responsiveness checks; synthetic data, no model/MES calls.

Optional tooling: python -m pip install playwright
Run from repo root: python scripts/check_chat_chart_performance.py
Uses installed Chrome/Edge; writes screenshots/measurements under ignored tmp/.
"""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'tmp'/'chat-chart-performance'


def payload():
    rows = [dict(id=f'P{i}', label=str(i+1), value=str(10+(i%13-6)/2), center='10',
                 lcl='7', ucl='13', signal=True, detail='合成密集标记压力测试，非MES数据') for i in range(500)]
    return dict(version=1, title='合成压力测试（非MES数据） · I-MR', subtitle='500点 × 2面板 · 所有点带标记的渲染压力测试',
        unit='mm',reference='synthetic-only / v1',note='仅验证页面响应；不用于工艺或产品判断。',complete=True,
        source_digest='a'*64, panels=[dict(title='I · 合成曲线',kind='line',rows=rows),dict(title='MR · 合成曲线',kind='line',rows=rows)])


def fixture_app():
    app = FastAPI()
    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    hits=[]; app.state.hits=hits
    @app.get('/')
    def index(): return FileResponse(ROOT/'static'/'index.html')
    @app.get('/config')
    def config(): return dict(username='chart-fixture',chat_charts=True,agent_engine='fixture')
    @app.get('/sessions/{username}')
    def sessions(username):
        return dict(active_session='plain',sessions=[dict(id=s,title=t,has_messages=True,auto_confirm=False,created_at=time.time())
            for s,t in [('plain','普通聊天'),('charts','100张历史图表'),('slow','慢图表加载'),('failure','图表加载失败'),('stream','流式回复')]])
    @app.get('/workflow/{username}/{session_id}')
    def workflow(username,session_id): return dict(status='complete')
    @app.get('/history/{username}')
    def history(username,session_id='plain'):
        n=100 if session_id=='charts' else 1
        messages=[]; descriptors=[]
        for i in range(n):
            messages.extend([dict(role='user',content=f'合成测试问题{i+1}'),
                dict(role='tool',tool_call_id=f'call-{i}',content='not sent to renderer'),
                dict(role='assistant',content=f'文字结果{i+1}：可以继续输入和滚动，图表单独加载。')])
            if session_id not in ('plain','stream'):
                descriptors.append(dict(id=f'{i+1:032x}',title='合成测试图表（非MES数据）',tool_call_id=f'call-{i}'))
        return dict(messages=messages,charts=descriptors,run=run() if session_id=='stream' and state.get('started') else None)
    @app.get('/api/chat/charts/{chart_id}')
    async def chart(chart_id,username,session_id):
        hits.append((session_id,chart_id))
        if session_id=='slow': await asyncio.sleep(3)
        if session_id=='failure': raise HTTPException(503,'injected failure')
        return payload()
    state={'status':'running'}
    def run(): return dict(id='run-fixture',session_id='stream',username='chart-fixture',message='请出图并流式回复',
                          status=state['status'],history_start=3,started_at=time.time(),request_id='fixture')
    @app.post('/api/chat/runs',status_code=202)
    def start(): state.update(status='running',started=True); return run()
    @app.get('/api/chat/runs/{run_id}')
    def get_run(run_id): return run()
    @app.get('/api/chat/runs/{run_id}/events')
    def stream(run_id):
        async def events():
            frames=[dict(type='chart',chart=dict(id='f'*32,title='流式回复图表')),
                    dict(type='stream_start',audience='answer')]
            frames.extend(dict(type='stream_chunk',content=f'第{i+1}段文字；') for i in range(100))
            frames.extend([dict(type='stream_end',audience='answer'),dict(type='run_state',status='completed')])
            for i,frame in enumerate(frames):
                if frame.get('status')=='completed': state['status']='completed'
                yield f'id: {i+1}\ndata: {json.dumps(frame,ensure_ascii=False)}\n\n'
                await asyncio.sleep(.025)
        return StreamingResponse(events(),media_type='text/event-stream')
    return app


MEASURE = """() => {
    window.chartProbe={longTasks:[],longFrames:[],frames:[],inputs:[],on:true};
    new PerformanceObserver(list=>{if(chartProbe.on) for(const e of list.getEntries())chartProbe.longTasks.push(e.duration);}).observe({type:'longtask'});
    if(PerformanceObserver.supportedEntryTypes.includes('long-animation-frame'))new PerformanceObserver(list=>{if(chartProbe.on)for(const e of list.getEntries())chartProbe.longFrames.push(e.toJSON());}).observe({type:'long-animation-frame'});
    let last=performance.now();function tick(now){if(chartProbe.on){chartProbe.frames.push(now-last);last=now;requestAnimationFrame(tick);}}requestAnimationFrame(tick);
    document.getElementById('inputBox').addEventListener('input',event=>{const start=event.timeStamp;requestAnimationFrame(()=>{if(chartProbe.on)chartProbe.inputs.push(performance.now()-start);});});
}"""


def percentile(values,p):
    return round(sorted(values)[min(len(values)-1,int(len(values)*p))],2) if values else 0


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--cpu',type=int,default=1); args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    executable=next((p for p in [Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')] if p.exists()),None)
    app=fixture_app()
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
    def serve_fixture():
        # This HTTP-only fixture needs no subprocess transport. Avoid Windows
        # Proactor pipe-reset noise when intentionally aborting slow requests;
        # the browser tooling keeps its own normal event-loop policy.
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(server.serve(sockets=[sock]))
    thread=threading.Thread(target=serve_fixture,daemon=True);thread.start()
    deadline=time.time()+10
    while not server.started:
        if time.time()>deadline: raise RuntimeError('fixture server failed to start')
        time.sleep(.01)
    summary={}; errors=[]
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=str(executable) if executable else None,headless=True)
            page=browser.new_page(viewport=dict(width=1440,height=1000),device_scale_factor=1)
            page.on('pageerror',lambda error: errors.append(str(error)))
            requests=[];page.on('request',lambda r:requests.append(r.url))
            cdp=page.context.new_cdp_session(page);cdp.send('Emulation.setCPUThrottlingRate',{'rate':args.cpu})
            base=f'http://127.0.0.1:{port}'
            def open_session(session):
                page.goto(base+'/?session_id='+session)
                page.wait_for_function("!document.getElementById('inputBox').disabled")
            open_session('plain')
            assert not any('echarts' in url or 'chart-worker.js' in url or '/api/chat/charts/' in url for url in requests)
            summary['plain_chat_loads_echarts']=False
            page.evaluate(MEASURE)
            page.locator('#inputBox').press_sequentially('plain chat typing '*5,delay=12)
            baseline=page.evaluate('chartProbe.on=false; chartProbe')
            summary['baseline_input_p95_ms']=percentile(baseline['inputs'],.95)
            open_session('charts')
            page.evaluate(MEASURE)
            page.wait_for_function("document.querySelector('.chart-body img')",timeout=30000,polling=100)
            assert page.evaluate('typeof window.echarts')=='undefined'
            assert page.locator('.chat-chart').count()==100
            page.evaluate("document.getElementById('inputBox').focus()")
            page.keyboard.type('chat typing while charts render '*4,delay=12)
            # Cache native targets once; repeated automation locator traversal
            # must not be mistaken for application work in the CPU stress probe.
            scroll_metrics=page.evaluate("""async () => {
                const targets=[...document.querySelectorAll('.chat-chart')], results=[];
                for(let i=0;i<targets.length;i+=5){targets[i].scrollIntoView({block:'center'});await new Promise(r=>setTimeout(r,100));results.push(ChatCharts.metrics());}
                return results;
            }""")
            assert all(m['mounted']<=2 and m['retainedPayloads']<=2 for m in scroll_metrics),scroll_metrics
            page.wait_for_function("document.querySelector('.chart-body img')",timeout=30000,polling=100)
            measurements=page.evaluate('chartProbe.on=false; chartProbe')
            summary.update(cpu_throttle=args.cpu,history_cards=100,points_per_panel=500,panels_per_card=2,
                input_p95_ms=percentile(measurements['inputs'],.95),frame_p95_ms=percentile(measurements['frames'],.95),
                long_tasks_ms=[round(n,2) for n in measurements['longTasks']],chart_metrics=page.evaluate('ChatCharts.metrics()'))
            summary['long_frame_details']=sorted(measurements['longFrames'],key=lambda e:e['duration'],reverse=True)[:3]
            # Inspect a complete graphic and original-value pagination.
            page.locator('.chat-chart').nth(95).evaluate("el=>el.scrollIntoView({block:'start'})")
            card=page.locator('.chat-chart').nth(95)
            page.wait_for_function("document.querySelectorAll('.chat-chart')[95].querySelector('img')!==null")
            page.screenshot(path=str(OUT/f'charts-cpu{args.cpu}.png'))
            card.get_by_text('查看原始数据',exact=True).click()
            assert card.locator('tbody tr').count()==30
            card.get_by_text('下一页',exact=True).click()
            assert card.locator('tbody tr').count()==30
            card.locator('select').select_option('50')
            page.wait_for_function("!document.querySelectorAll('.chat-chart')[95].querySelector('.chart-actions button:last-child').disabled")
            with page.expect_download() as download:
                card.get_by_text('导出SVG',exact=True).click()
            download.value.save_as(str(OUT/f'export-cpu{args.cpu}.svg'))
            summary['table_pagination_and_export']=True
            # A slow snapshot must leave typing available; late results must not cross sessions.
            open_session('slow'); page.wait_for_timeout(150)
            started=time.perf_counter();page.locator('#inputBox').fill('慢图表期间仍可输入')
            summary['slow_chart_fill_ms']=round((time.perf_counter()-started)*1000,2)
            page.get_by_role('button',name='普通聊天',exact=False).first.click()
            page.wait_for_function("!document.getElementById('inputBox').disabled")
            page.wait_for_timeout(3100)
            assert page.locator('.chat-chart').count()==0
            assert page.evaluate('ChatCharts.metrics().mounted')==0
            summary['session_switch_discards_late_chart']=True
            open_session('failure')
            page.wait_for_function("document.querySelector('.chart-status').textContent.includes('暂时无法加载')")
            page.locator('#inputBox').fill('失败时仍可输入')
            summary['chart_failure_keeps_composer']=True
            open_session('slow');page.wait_for_timeout(150)
            page.evaluate('ChatCharts.streaming(true); ChatCharts.streaming(false)')
            page.wait_for_function('ChatCharts.metrics().mounted>0',timeout=30000)
            summary['burst_pause_resume_restarts_cancelled_fetch']=True
            open_session('stream');page.locator('#inputBox').fill('请出图并流式回复');page.locator('#sendBtn').click()
            page.wait_for_function("document.getElementById('msgs').textContent.includes('第10段文字')")
            assert page.evaluate('ChatCharts.metrics().mounted')==0
            page.wait_for_function("document.getElementById('msgs').textContent.includes('第100段文字')")
            page.wait_for_function("!document.getElementById('inputBox').disabled")
            page.locator('.chat-chart').evaluate("el=>el.scrollIntoView({block:'start'})")
            page.wait_for_function('ChatCharts.metrics().mounted>0',timeout=30000)
            summary['stream_text_before_chart_mount']=True
            # Simulate an unavailable Worker bundle, independently of HTTP data failures.
            page.route('**/chart-worker.js?*',lambda route:route.abort())
            open_session('charts')
            page.wait_for_function("document.getElementById('msgs').textContent.includes('图表加载失败')")
            page.locator('#inputBox').fill('绘图线程失败也可以输入')
            summary['worker_failure_keeps_composer']=True
            summary['browser']=browser.version;summary['page_errors']=errors
            assert not errors,errors
            assert summary['input_p95_ms']<50,summary
            assert summary['frame_p95_ms']<=50,summary
            assert max(summary['long_tasks_ms'],default=0)<100,summary
            browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=5)
        (OUT/f'results-cpu{args.cpu}.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(summary,ensure_ascii=True,indent=2))


if __name__=='__main__': main()
