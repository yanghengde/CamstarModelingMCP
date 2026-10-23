import asyncio,json
from unittest.mock import AsyncMock
import pytest
from tools import container_transfer_plans as p
from core.tool_catalog import TOOL_POLICIES

@pytest.fixture
def env(monkeypatch,tmp_path):
 monkeypatch.setattr(p,'PLAN_DB',tmp_path/'plans.sqlite')
 def node(n):return {'id':n,'container':{'Name':n},'family_complete':True,'parent_known':True,'parent_id':None,'family':[],'status':{'Status':1}}
 data={'parent':node('P'),'relationships':[{'child':node('C'),'parent_before':None}], 'server_echo_verified':True,'relationship_checks_complete':True,'relationship_conflicts':[], 'ancestor_check':{'complete':True,'cycle':False,'nodes':[]},'level_rules':{'complete':True,'checks':[{'child_id':'C','parent_allows_child':'listed','child_allows_parent':'listed'}]},'request_preview':{'body':{'container':{'name':'P','instanceId':'P'},'childContainers':[{'name':'C','instanceId':'C'}]}}}
 preview=AsyncMock(return_value=json.dumps(data));req=AsyncMock(return_value='{"ok":true}')
 monkeypatch.setattr(p,'preview_container_associate',preview);monkeypatch.setattr(p,'request',req)
 monkeypatch.setattr(p,'get_container_trace',AsyncMock(return_value='{"sections":{}}'))
 return data,preview,req

def run(c):return json.loads(asyncio.run(c))
def prepare():return run(p.prepare_container_association('P',['C']))

def test_confirm_execute(env):
 _,_,req=env;r=prepare()
 assert r['capacity_verified'] is False
 assert '容量' in p.confirmation_summary(r['plan_id'],r['plan_digest'])
 assert TOOL_POLICIES['execute_container_association'].always_confirm
 out=run(p.execute_container_association(r['plan_id'],r['plan_digest']))
 assert out['state']=='unknown' and not out['verified_success']
 assert req.call_args.args==('POST','/api/Associate')
 assert set(req.call_args.kwargs['body'])=={'container','childContainers'}
 other=prepare();assert not run(p.execute_container_association(other['plan_id'],other['plan_digest']))['submitted']
 assert req.call_count==1

@pytest.mark.parametrize('bad',['parent','child_tree','level','unknown_level','cycle','status','body'])
def test_blocked(env,bad):
 data,preview,req=env
 if bad=='parent':data['relationships'][0]['parent_before']='other'
 if bad=='child_tree':data['relationships'][0]['child']['family']=[{'ParentContainerId':'C'}]
 if bad=='level':data['level_rules']['checks'][0]['child_allows_parent']='not_listed'
 if bad=='unknown_level':data['level_rules']['complete']=False
 if bad=='cycle':data['ancestor_check']['cycle']=True
 if bad=='status':data['parent']['status']['Status']=2
 if bad=='body':data['request_preview']['body']['skipValidation']=True
 preview.return_value=json.dumps(data)
 assert 'error' in prepare();req.assert_not_called()

def test_changed_rules(env):
 data,preview,req=env;r=prepare()
 data['level_rules']['checks'][0]['child_allows_parent']='not_listed';preview.return_value=json.dumps(data)
 assert not run(p.execute_container_association(r['plan_id'],r['plan_digest']))['submitted'];req.assert_not_called()

def test_wrong_operation(env):
 _,_,req=env;r=prepare()
 assert not run(p.execute_container_disassociation(r['plan_id'],r['plan_digest']))['submitted'];req.assert_not_called()
