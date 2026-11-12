"""Verify loaded C6 tools on the local app using a read-only inventory chat.

Does not execute MES tools; restores the previously active local conversation.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid
from urllib.parse import urlsplit
import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

REQUIRED={'get_spc_parameter_trend','analyze_spc_series','analyze_spc_history','analyze_spc_attributes',
          'get_spc_native_readiness','get_spc_annotation_context','prepare_spc_annotation',
          'get_spc_violation_context','prepare_spc_violation'}


def checkpoint_inventory(username,session,path):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    path=Path(path)
    if not path.is_absolute():path=ROOT/path
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT type,checkpoint FROM checkpoints WHERE thread_id=? ORDER BY checkpoint_id DESC LIMIT 1',
                       (username+':'+session,)).fetchone()
    if not row:raise ValueError('The local running app checkpoint was not found.')
    snapshot=JsonPlusSerializer().loads_typed((row[0],row[1]))
    return json.loads(snapshot['channel_values']['capability_directory'])


def verify(base,output,existing_session=None):
    url=urlsplit(base)
    if url.hostname not in ('127.0.0.1','localhost') or url.scheme!='http' or url.path not in ('','/'):
        raise ValueError('This verifier is only for the explicitly local application.')
    report={'scope':'loaded application and actual local capability inventory, not native SPC acceptance'}
    with httpx.Client(base_url=base,trust_env=False,timeout=20) as client:
        config=client.get('/config').raise_for_status().json()
        assert config['chat_charts'] is True and config['agent_engine']=='langgraph'
        assert '/api/chat/charts/{chart_id}' in client.get('/openapi.json').raise_for_status().json()['paths']
        username=config['username']
        prior=client.get('/sessions/'+username).raise_for_status().json().get('active_session')
        try:
            session=existing_session or client.post('/sessions/'+username+'/new').raise_for_status().json()['session_id']
            if existing_session:
                run=client.get('/history/'+username,params={'session_id':session,'presentation':'true'}).raise_for_status().json()['run']
            else:
                run=client.post('/api/chat/runs',json={'username':username,'session_id':session,
                    'message':'你具备哪些能力？请根据实际能力目录简要介绍，并说明尚不支持的内容。',
                    'request_id':'c6-release-'+uuid.uuid4().hex}).raise_for_status().json()
            deadline=time.monotonic()+90
            while True:
                current=client.get('/api/chat/runs/'+run['id'],params={'username':username}).raise_for_status().json()
                if current['status'] not in ('queued','running','cancelling'):break
                if time.monotonic()>deadline:raise TimeoutError('Local inventory chat timed out.')
                time.sleep(.25)
            assert current['status']=='completed',current['status']
            messages=client.get('/history/'+username,params={'session_id':session}).raise_for_status().json()['messages']
            tools=[m for m in messages if m.get('role')=='tool']
            assert all(m.get('name')=='get_capabilities' for m in tools)
            if tools:inventory=json.loads(tools[-1]['content'])
            else:
                # The production fast overview deliberately stores its real
                # directory in the graph checkpoint, without synthetic tool messages.
                from config import LANGGRAPH_CHECKPOINT_DB
                inventory=checkpoint_inventory(username,session,LANGGRAPH_CHECKPOINT_DB)
                report['inventory_source']='actual_running_process_checkpoint'
            names={tool['name'] for group in inventory['groups'] for tool in group['tools']}
            assert REQUIRED<=names
            assert inventory['freshness']['reload_required'] is False
            report.update(passed=True,session_id=session,run_id=run['id'],tool_count=len(names),
                          c6_tools=sorted(REQUIRED),freshness=inventory['freshness'],
                          chart_endpoint_loaded=True,agent_engine=config['agent_engine'],mes_business_write_performed=False)
        finally:
            active=client.get('/sessions/'+username).raise_for_status().json().get('active_session')
            if prior and active==session:
                client.get('/history/'+username,params={'session_id':prior,'presentation':'true'}).raise_for_status()
                report['previous_active_session_restored']=True
            elif active!=session:
                report['concurrent_user_selection_preserved']=True
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:report.get(k) for k in ('passed','tool_count','chart_endpoint_loaded','previous_active_session_restored')},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',default='http://127.0.0.1:8030')
    parser.add_argument('--output',type=Path,default=Path('tmp/c6-completion/deployment-verification.json'))
    parser.add_argument('--session-id',help='Reuse this verifier\'s existing read-only inventory session.')
    args=parser.parse_args();verify(args.base,args.output,args.session_id)
