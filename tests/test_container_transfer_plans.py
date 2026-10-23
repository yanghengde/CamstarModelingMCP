import asyncio,copy,json
from unittest.mock import AsyncMock
import pytest
from tools import container_transfer_plans as p
from core.tool_catalog import TOOL_POLICIES
from agent.safety import plan_preview

@pytest.fixture
def env(monkeypatch,tmp_path):
 monkeypatch.setattr(p,'PLAN_DB',tmp_path/'plans.sqlite')
 def node(n):return {'id':n,'container':{'Name':n},'family_complete':True,'status':{'Status':1}}
 data={'parent':node('P'),'relationships':[{'child':node('C'),'parent_before':'P'}],'relationship_checks_complete':True,'relationship_conflicts':[], 'server_echo_verified':True,'ancestor_check':{'nodes':[]},'request_preview':{'body':{'container':{'name':'P','instanceId':'P'},'childContainers':[{'name':'C','instanceId':'C'}],'disassociateAll':False}}}
 preview=AsyncMock(return_value=json.dumps(data));req=AsyncMock(return_value='{"ok":true}')
 monkeypatch.setattr(p,'preview_container_disassociate',preview);monkeypatch.setattr(p,'request',req)
 monkeypatch.setattr(p,'get_container_trace',AsyncMock(return_value='{"sections":{}}'))
 return data,preview,req

def run(c):return json.loads(asyncio.run(c))
def prepare():return run(p.prepare_container_disassociation('P',['C']))

def test_confirm_execute_and_no_retry(env):
 data,preview,req=env;r=prepare()
 assert TOOL_POLICIES['execute_container_disassociation'].always_confirm
 text=plan_preview([{'function':{'name':'execute_container_disassociation','arguments':json.dumps({'plan_id':r['plan_id'],'plan_digest':r['plan_digest']})}}]);assert 'childContainers' in text
 out=run(p.execute_container_disassociation(r['plan_id'],r['plan_digest']))
 assert out['state']=='unknown' and not out['verified_success'] and len(out['evidence'])==2
 assert req.call_args.kwargs['params']=={'execute':'true'}
 assert not run(p.execute_container_disassociation(r['plan_id'],r['plan_digest']))['submitted']
 other=prepare();assert not run(p.execute_container_disassociation(other['plan_id'],other['plan_digest']))['submitted']
 assert req.call_count==1

@pytest.mark.parametrize('change',['parent','status','echo','changed'])
def test_recheck_blocks(env,change):
 data,preview,req=env;r=prepare()
 if change=='parent':data['relationships'][0]['parent_before']='other'
 if change=='status':data['parent']['status']['Status']=2
 if change=='echo':data['server_echo_verified']=False
 if change=='changed':data['parent']['status']['Qty']=3
 preview.return_value=json.dumps(data)
 assert not run(p.execute_container_disassociation(r['plan_id'],r['plan_digest']))['submitted'];req.assert_not_called()

def test_expired_and_digest(env):
 _,_,req=env;r=prepare()
 assert not run(p.execute_container_disassociation(r['plan_id'],'bad'))['submitted']
 with p.db_connect() as db:db.execute('UPDATE plans SET created=0')
 assert not run(p.execute_container_disassociation(r['plan_id'],r['plan_digest']))['submitted'];req.assert_not_called()

def test_response_lost(env):
 _,_,req=env;r=prepare();req.side_effect=TimeoutError('lost')
 out=run(p.execute_container_disassociation(r['plan_id'],r['plan_digest']))
 assert out['state']=='unknown'
 assert run(p.get_container_transfer_plan(r['plan_id']))['state']=='unknown'

def test_concurrency(env):
 _,_,req=env;r=prepare()
 async def both():return await asyncio.gather(*(p.execute_container_disassociation(r['plan_id'],r['plan_digest']) for _ in range(2)))
 asyncio.run(both());assert req.call_count==1

@pytest.mark.parametrize('change',['all','extra','wrong_child','bool_status'])
def test_malformed_execution_body(env,change):
 data,preview,req=env
 if change=='all':data['request_preview']['body']['disassociateAll']=True
 if change=='extra':data['request_preview']['body']['extra']='unsafe'
 if change=='wrong_child':data['request_preview']['body']['childContainers'][0]['instanceId']='Other'
 if change=='bool_status':data['parent']['status']['Status']=True
 preview.return_value=json.dumps(data)
 assert 'error' in prepare();req.assert_not_called()

def test_stored_body_integrity(env):
 _,_,req=env;r=prepare()
 with p.db_connect() as db:
  row=db.execute('SELECT payload FROM plans').fetchone()
  data=json.loads(row[0]);data['snapshot']['body']['disassociateAll']=True
  db.execute('UPDATE plans SET payload=?',(json.dumps(data),))
 assert not run(p.execute_container_disassociation(r['plan_id'],r['plan_digest']))['submitted']
 req.assert_not_called()
