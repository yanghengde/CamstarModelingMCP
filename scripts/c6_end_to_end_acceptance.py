"""Actual LLM + actual read-only MES + production web/graph/chart pipeline.

Uses the existing C6 isolated samples, never recreates or submits anything.
Runs a separate loopback candidate with private local state; leaves 8030 alone.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
READ_TOOLS = frozenset({
    'get_capabilities', 'load_tool_group', 'get_mcp_server_status',
    'get_container_details', 'get_container_collection_history',
    'get_spc_parameter_trend', 'analyze_spc_history', 'analyze_spc_series',
    'analyze_spc_attributes', 'get_spc_native_readiness',
})


def model_request_allowed(method, url, base):
    target, origin = urlsplit(str(url)), urlsplit(base)
    return (method == 'POST' and (target.scheme, target.hostname, target.port) ==
            (origin.scheme, origin.hostname, origin.port)
            and target.path == origin.path.rstrip('/')+'/chat/completions'
            and not target.query and not target.fragment)


def questions(container):
    return [
        ('raw_trend', f'查询容器 {container} 的 TestValue 参数原始历史，按GMT先后画原始走势图。'
         '只读展示，不进行SPC判异。这是已有的隔离合成测试样本。', 'get_spc_parameter_trend', 'trend'),
        ('history_spc', '使用刚才同一容器的四条已核实原始历史，9、10、11三条作为预先声明的独立合成测试基线，'
         '14作为评估段。请做本地I-MR控制图，只选择beyond_3sigma规则，并说明信号。'
         '仅做本地分析，不调用原生引擎，不登记或放行。', 'analyze_spc_history', 'line'),
        ('count_spc', '另用以下明确的合成数据画本地p控制图：三个观测COUNT1/COUNT2/COUNT3，sequence为1/2/3，'
         '每组检查36件，不合格品件数依次18、28、8，count_kind=nonconforming_units。'
         '使用固定合成测试不良率rate=0.5，reference=C6 synthetic profile，version=Test-A，'
         'rule=beyond_3sigma。context为characteristic=合成不合格品比例，unit=件，'
         'process_scope=隔离合成软件验收，data_reference=c6-software-acceptance-v1，order_reference=给定顺序。'
         '不是MES实绩，不写入MES，也不作放行判断。', 'analyze_spc_attributes', 'line'),
    ]


def serve(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    state=Path(args.state).resolve();state.mkdir(parents=True,exist_ok=False)
    shutil.copytree(ROOT/'static',state/'static')
    for name,file in {'CHAT_RUN_DB':'runs.sqlite','LANGGRAPH_CHECKPOINT_DB':'graph.sqlite',
                      'EXPERIENCE_DB':'experience.sqlite','CHAT_CHART_DB':'charts.sqlite'}.items():
        os.environ[name]=str(state/file)
    os.environ.update(ENABLE_MCP_HTTP='False',AGENT_ENGINE='langgraph',ENABLE_CHAT_CHARTS='True',
                      UPLOAD_RETENTION_DAYS='0',PERF_RETENTION_DAYS='0',CHAT_EVENT_RETENTION_DAYS='0')
    os.chdir(state)
    import httpx
    import uvicorn
    from fastapi import HTTPException, Request
    from scripts.acceptance_readonly import ReadOnlyGate
    from config import CAMSTAR_BASE_URL,CAMSTAR_QUERY_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL,LLM_BASE_URL
    from core.tool_catalog import TOOL_POLICIES
    from agent import langgraph_runtime as runtime, llm_client
    from tools import get_tool_func
    trace=[];guard_violations=[]
    class Gate(ReadOnlyGate):
        def validate(self,request):
            if model_request_allowed(request.method,request.url,LLM_BASE_URL): return
            return super().validate(request)
    gate=Gate(CAMSTAR_BASE_URL,CAMSTAR_QUERY_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL)
    gate.install()
    def lookup(name):
        actual=get_tool_func(name)
        if name not in READ_TOOLS or TOOL_POLICIES[name].effect!='read':
            async def deny(**kwargs):
                guard_violations.append({'tool':name});raise ValueError('C6验收只允许明确只读入口。')
            return deny
        if actual is None: return None
        async def invoke(**kwargs):
            result=await actual(**kwargs)
            record={'tool':name,'arguments':kwargs,'result':str(result)}
            trace.append(record)
            (state/'tools.json').write_text(json.dumps(trace,ensure_ascii=False,indent=2),encoding='utf8')
            return result
        return invoke
    runtime.get_tool_func=lookup
    llm_client.get_tool_func=lookup
    # Redirect hard-coded local plan stores too, even though all writes are denied.
    for name,module in list(sys.modules.items()):
        if not name.startswith(('tools.','core.')): continue
        for field,value in list(vars(module).items()):
            if isinstance(value,Path) and value.is_absolute() and value.is_relative_to(ROOT/'data'):
                setattr(module,field,state/'data'/value.relative_to(ROOT/'data'))
    from web.app import create_app
    app=create_app()
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,log_level='warning'))
    def authenticate(request):
        if not secrets.compare_digest(request.headers.get('X-C6-Acceptance-Key',''),args.key):
            raise HTTPException(403)
    @app.get('/_acceptance/probe')
    async def probe(request:Request):
        authenticate(request)
        return {'tools':trace,'requests':gate.requests,'violations':guard_violations}
    @app.post('/_acceptance/stop')
    async def stop(request:Request):
        authenticate(request);server.should_exit=True;return {'stopping':True}
    server.run()


def run(args):
    import httpx
    import logging
    from playwright.sync_api import sync_playwright
    from scripts import spc_isolated_acceptance as fixture
    logging.getLogger('httpx').setLevel(logging.WARNING)
    manifest=Path(args.manifest).resolve()
    target=fixture.read(manifest)  # Immutable target and configured environment must match.
    source_ids=fixture.point_ids(manifest)
    output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    state=output/('candidate-'+secrets.token_hex(5))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}';key=secrets.token_hex(32)
    report={'scope':'actual LLM and actual MES reads; local SPC and chart acceptance',
            'native_engine_accepted':False,'native_registration_accepted':False,
            'mes_business_writes_performed':False,'cases':[],'source_point_ids':source_ids,'browser_errors':[]}
    code_files=['agent/langgraph_runtime.py','agent/tool_routing.py','core/chat_charts.py','tools/quality_spc_history.py',
                'tools/quality_spc.py','tools/quality_spc_attributes.py','web/routes.py','static/chat-charts.js',
                'static/chart-spec.js','static/chart-worker.js','static/workspace.js']
    report['source_sha256']={file:hashlib.sha256((ROOT/file).read_bytes()).hexdigest() for file in code_files}
    command=[sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--serve','--state',str(state),'--port',str(port),'--key',key]
    with (output/'candidate.log').open('w',encoding='utf8') as log:
        process=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            with httpx.Client(base_url=base,trust_env=False,timeout=10,headers={'X-C6-Acceptance-Key':key}) as client:
                deadline=time.monotonic()+70
                while True:
                    try:
                        config=client.get('/config').raise_for_status().json();break
                    except httpx.HTTPError:
                        if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Candidate failed to start; see candidate.log')
                        time.sleep(.2)
                username=config['username']
                session=client.post(f'/sessions/{username}/new').raise_for_status().json()['session_id']
                report['session_id']=session
                executable=next(p for p in [Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
                    Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')] if p.exists())
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path=str(executable),headless=True)
                    page=browser.new_page(viewport={'width':1440,'height':1000})
                    page.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
                    page.goto(base+'/?session_id='+session)
                    for case,question,expected,kind in questions(target['container_name']):
                        if args.case and case not in args.case:continue
                        if args.case and case=='history_spc':
                            question=f"目标为原隔离容器{target['container_name']}，按GMT先后的真实原始历史ID为{source_ids}。"+question
                        print('Starting',case,flush=True)
                        page.wait_for_function("!document.getElementById('inputBox').disabled")
                        page.locator('#inputBox').fill(question)
                        with page.expect_response(lambda r:r.url==base+'/api/chat/runs' and r.request.method=='POST') as submitted:
                            page.locator('#sendBtn').click()
                        item=submitted.value.json();deadline=time.monotonic()+args.timeout
                        while True:
                            current=client.get('/api/chat/runs/'+item['id'],params={'username':username}).raise_for_status().json()
                            if current['status'] not in ('queued','running','cancelling'):break
                            if time.monotonic()>deadline:
                                client.post('/api/chat/runs/'+item['id']+'/cancel',params={'username':username})
                                raise TimeoutError('Real chat timed out: '+case)
                            page.wait_for_timeout(500)
                        history=client.get('/history/'+username,params={'session_id':session,'presentation':'true'}).raise_for_status().json()
                        probe=client.get('/_acceptance/probe').raise_for_status().json()
                        entry={'case':case,'run_id':item['id'],'status':current['status'],'charts':history['charts']}
                        report['cases'].append(entry)
                        assert current['status']=='completed',entry
                        used=[row for row in probe['tools'] if row['tool']==expected]
                        assert used,'Expected real tool missing: '+expected
                        result=json.loads(used[-1]['result'])
                        assert not result.get('error') and result.get('complete',True),result
                        chart=history['charts'][-1]
                        payload=client.get('/api/chat/charts/'+chart['id'],params={'username':username,'session_id':session}).raise_for_status().json()
                        assert payload['tool']==expected and payload['panels'][0]['kind']==kind
                        assert result['native_spc_result'] is False and result['release_authorized'] is False
                        if case=='raw_trend':
                            assert [row['id'] for row in payload['panels'][0]['rows']]==source_ids
                            assert [row['value'] for row in payload['panels'][0]['rows']]==['9','10','11','14']
                        if case=='history_spc':
                            assert result['data_verified_against_mes'] and result['has_signals']
                            assert result['primary_signals'][0]['contributing_ids']==[source_ids[-1]]
                        if case=='count_spc':assert [p['beyond_3sigma'] for p in result['points']]==[False,True,True]
                        card=page.locator(f'[data-chart-id="{chart["id"]}"]')
                        card.evaluate("el=>el.scrollIntoView({block:'start'})")
                        page.wait_for_function('(id)=>!!document.querySelector(`[data-chart-id="${id}"] img`)',arg=chart['id'],timeout=30000)
                        entry['payload_sha256']=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                        page.screenshot(path=str(output/(case+'.png')))
                        calls_before=len(probe['tools'])
                        page.reload();page.wait_for_function("!document.getElementById('inputBox').disabled")
                        card=page.locator(f'[data-chart-id="{chart["id"]}"]')
                        card.evaluate("el=>el.scrollIntoView({block:'start'})")
                        page.wait_for_function('(id)=>!!document.querySelector(`[data-chart-id="${id}"] img`)',arg=chart['id'],timeout=30000)
                        assert len(client.get('/_acceptance/probe').json()['tools'])==calls_before,'History replay executed tools again'
                        restored=client.get('/api/chat/charts/'+chart['id'],params={'username':username,'session_id':session}).json()
                        assert restored==payload
                        entry['visible_image_and_snapshot_replay_verified']=True
                        print('Passed',case,flush=True)
                    final=client.get('/_acceptance/probe').raise_for_status().json()
                    assert not final['violations'] and not report['browser_errors'],final['violations']
                    assert not any(r.get('blocked') for r in final['requests']),final['requests']
                    report.update(passed=True,request_count=len(final['requests']),tools=[r['tool'] for r in final['tools']],browser=browser.version)
                    browser.close()
        finally:
            try:
                with httpx.Client(base_url=base,trust_env=False,timeout=3) as shutdown:
                    shutdown.post('/_acceptance/stop',headers={'X-C6-Acceptance-Key':key})
            except httpx.HTTPError:pass
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=10)
            report['source_unchanged']=all(hashlib.sha256((ROOT/file).read_bytes()).hexdigest()==value for file,value in report['source_sha256'].items())
            (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'passed':report.get('passed',False),'cases':len(report['cases']),'report':str(output/'report.json')},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve',action='store_true');parser.add_argument('--state');parser.add_argument('--port',type=int);parser.add_argument('--key')
    parser.add_argument('--manifest',default=str(ROOT/'tmp/c6-acceptance/manifest.json'))
    parser.add_argument('--output',default=str(ROOT/'tmp/c6-completion/end-to-end'))
    parser.add_argument('--timeout',type=int,default=240)
    parser.add_argument('--case',action='append',choices=['raw_trend','history_spc','count_spc'],help='Rerun only a changed case in a fresh isolated candidate.')
    args=parser.parse_args()
    serve(args) if args.serve else run(args)
