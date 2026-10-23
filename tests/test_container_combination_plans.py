import asyncio,copy,json
from unittest.mock import AsyncMock
import pytest
from tools import container_transfer_plans as p
from core.tool_catalog import TOOL_POLICIES

@pytest.fixture
def env(monkeypatch,tmp_path):
 monkeypatch.setattr(p,'PLAN_DB',tmp_path/'plans.sqlite')
 status={'Status':1,'Qty':10,'Qty2':20}
 for f in ['Product','ContainerLevel','UOM','UOM2','Spec']:status[f]={'Name':f,'InstanceId':f,'Revision':'1'}
 def node(n):return {'id':n,'container':{'Name':n},'family_complete':True,'parent_known':True,'parent_id':None,'family':[],'status':copy.deepcopy(status)}
 body={'container':{'name':'B','instanceId':'B'},'closeWhenEmpty':False,'fromContainerDetails':[{'fromContainer':{'name':'A','instanceId':'A'},'qty':2,'qty2':3,'combineAllQty':False,'closeWhenEmpty':False}]}
 data={'source':node('A'),'target':node('B'),'server_echo_verified':True,'request_preview':{'body':body},'quantity_effect':{},'quantity2_effect':{}}
 preview=AsyncMock(return_value=json.dumps(data));req=AsyncMock(return_value='{"ok":true}')
 monkeypatch.setattr(p,'preview_container_combine',preview);monkeypatch.setattr(p,'request',req)
 monkeypatch.setattr(p,'get_container_trace',AsyncMock(return_value='{"sections":{}}'))
 return data,preview,req

def run(c):return json.loads(asyncio.run(c))
def prepare():return run(p.prepare_container_combination('B','A',2,3))

def test_execute_once(env):
 _,_,req=env;r=prepare()
 assert TOOL_POLICIES['execute_container_combination'].always_confirm
 assert 'Qty2' in p.confirmation_summary(r['plan_id'],r['plan_digest'])
 out=run(p.execute_container_combination(r['plan_id'],r['plan_digest']))
 assert out['state']=='unknown' and len(out['evidence'])==2
 assert req.call_args.args==('POST','/api/Combine')
 assert req.call_args.kwargs['params']=={'execute':'true'}
 assert not run(p.execute_container_combination(r['plan_id'],r['plan_digest']))['submitted']
 assert req.call_count==1

@pytest.mark.parametrize('bad',['qty','qty2','parent','child','unit','revision','status','all','extra'])
def test_restrict_scope(env,bad):
 data,preview,req=env
 if bad=='qty':data['source']['status']['Qty']=1
 if bad=='qty2':data['source']['status']['Qty2']=None
 if bad=='parent':data['source']['parent_id']='P'
 if bad=='child':data['source']['family']=[{'ParentContainerId':'A'}]
 if bad=='unit':data['target']['status']['UOM2']=None
 if bad=='revision':data['target']['status']['Product']['Revision']='2'
 if bad=='status':data['source']['status']['Status']=True
 if bad=='all':data['request_preview']['body']['fromContainerDetails'][0]['combineAllQty']=True
 if bad=='extra':data['request_preview']['body']['extra']='x'
 preview.return_value=json.dumps(data)
 assert 'error' in prepare();req.assert_not_called()

def test_changed_balance_and_operation(env):
 data,preview,req=env;r=prepare()
 assert not run(p.execute_container_disassociation(r['plan_id'],r['plan_digest']))['submitted']
 data['source']['status']['Qty']=9;preview.return_value=json.dumps(data)
 assert not run(p.execute_container_combination(r['plan_id'],r['plan_digest']))['submitted']
 req.assert_not_called()

def test_new_plan_cannot_bypass(env):
 _,_,req=env;r=prepare()
 run(p.execute_container_combination(r['plan_id'],r['plan_digest']))
 other=prepare()
 assert not run(p.execute_container_combination(other['plan_id'],other['plan_digest']))['submitted']
 assert req.call_count==1
