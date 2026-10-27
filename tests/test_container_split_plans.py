import asyncio,copy,json
from unittest.mock import AsyncMock
import pytest
from tools import container_split_plan as s,container_transfer_plans as p

@pytest.fixture
def env(monkeypatch,tmp_path):
 monkeypatch.setattr(p,'PLAN_DB',tmp_path/'plans.sqlite')
 node={'id':'0000000000000001','container':{'Name':'S'},'family_complete':True,'parent_known':True,'parent_id':None,'family':[],'status':{'Status':1,'Qty':10,'Qty2':20}}
 for f in ('UOM','UOM2','Product','ContainerLevel','Spec'):node['status'][f]={'Name':f,'InstanceId':f,'Revision':'1'}
 monkeypatch.setattr(s,'snapshot',AsyncMock(return_value=node))
 async def request(method,path,**kw):
  if path=='/api/AdHoc':return '[]'
  assert kw['params']['execute']=='false'
  b=kw['body'];d=b['toContainerDetails'][0]
  return json.dumps({'Container':{'Name':'S','InstanceId':node['id']},'CloseWhenEmpty':b['closeWhenEmpty'],'ReduceQty':True,'AutoNumber':False,'ToContainerDetails':[{'ToContainerName':d['toContainerName'],'Qty':d['qty'],'Qty2':d['qty2']}]})
 mock=AsyncMock(side_effect=request);monkeypatch.setattr(s,'request',mock)
 write=AsyncMock(return_value='{"ok":true}');monkeypatch.setattr(p,'request',write)
 monkeypatch.setattr(p,'get_container_trace',AsyncMock(return_value='{"sections":{}}'))
 return node,mock,write

def run(c):return json.loads(asyncio.run(c))
def prepare():return run(p.prepare_container_split('S','NEW',2,3))

def test_prepare_execute_once(env):
 _,_,write=env;r=prepare();assert r['quantity_effect']['Qty']['after_if_accepted']=='8'
 assert 'NEW' in p.confirmation_summary(r['plan_id'],r['plan_digest'])
 out=run(p.execute_container_split(r['plan_id'],r['plan_digest']))
 assert out['state']=='unknown' and not out['verified_success']
 assert write.call_args.args==('POST','/api/Split')
 assert not run(p.execute_container_split(r['plan_id'],r['plan_digest']))['submitted'];assert write.call_count==1

@pytest.mark.parametrize('bad',['qty','qty2','parent','unit','closed','children'])
def test_restrictions(env,bad):
 node,_,write=env
 if bad=='qty':node['status']['Qty']=1
 if bad=='qty2':node['status']['Qty2']=None
 if bad=='parent':node['parent_id']='p'
 if bad=='unit':node['status']['UOM2']=None
 if bad=='closed':node['status']['Status']=2
 if bad=='children':node['family']=[{'ParentContainerId':node['id']}]
 assert 'error' in prepare();write.assert_not_called()

def test_name_exists(env):
 _,mock,write=env;mock.side_effect=None;mock.return_value='[{"ContainerName":"NEW"}]'
 assert 'error' in prepare();write.assert_not_called()

def test_bad_echo(env):
 _,mock,write=env;mock.side_effect=['[]','{"Container":{}}']
 assert 'error' in prepare();write.assert_not_called()

def test_source_changes(env):
 node,_,write=env;r=prepare();node['status']['Qty']=9
 assert not run(p.execute_container_split(r['plan_id'],r['plan_digest']))['submitted'];write.assert_not_called()

def test_new_name_lock_other_source(env):
 node,_,write=env;r=prepare();run(p.execute_container_split(r['plan_id'],r['plan_digest']))
 node['id']='0000000000000002'
 other=prepare()
 assert not run(p.execute_container_split(other['plan_id'],other['plan_digest']))['submitted'];assert write.call_count==1
