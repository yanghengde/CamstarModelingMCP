import asyncio,copy,json
from unittest.mock import AsyncMock
import pytest
from tools import container_transfer_reconciliation as r, container_transfer_plans as p,get_tool_func

@pytest.fixture
def env(monkeypatch,tmp_path):
 monkeypatch.setattr(p,'PLAN_DB',tmp_path/'plans.sqlite')
 def node(n,parent=None):return {'id':n,'container':{'Name':n},'parent_known':True,'parent_id':parent,'status':{'Qty':10,'Qty2':20,'UOM':{'Name':'EA'},'UOM2':{'Name':'kg'}}}
 snap={'environment':p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),'operation':'Associate','parent':node('P'),'children':[{'child':node('C')}],'body':{'fromContainerDetails':[{'qty':2,'qty2':3}]}}
 def save(op='Associate',state='unknown'):
  snap['operation']=op
  with p.db_connect() as db:db.execute('INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?,?)',('plan',p.digest(snap),json.dumps({'snapshot':snap}),state,0,None))
 save()
 current=[node('P'),node('C','P')]
 mock=AsyncMock(side_effect=current);monkeypatch.setattr(r,'snapshot',mock)
 return snap,save,current,mock

def run():return json.loads(asyncio.run(r.reconcile_container_transfer_plan('plan')))

def test_match_does_not_unlock(env):
 out=run();assert out['comparison']=='current_state_matches'
 assert not out['verified_success'] and not out['retry_allowed']
 with p.db_connect() as db:assert db.execute('SELECT state FROM plans').fetchone()[0]=='unknown'
 assert get_tool_func('reconcile_container_transfer_plan')

@pytest.mark.parametrize('mode',['different','missing','wrong_id','error'])
def test_bad_state(env,mode):
 _,_,current,mock=env
 if mode=='different':current[1]['parent_id']='Other'
 if mode=='missing':current[1]['parent_known']=False
 if mode=='wrong_id':current[1]['id']='Other'
 if mode=='error':mock.side_effect=ValueError('denied')
 out=run();assert out['comparison']==('current_state_differs' if mode=='different' else 'incomplete')
 assert not out['retry_allowed']

def test_combine_both_quantities(env):
 _,save,current,_=env;save('Combine')
 current[0]['status'].update(Qty=12,Qty2=23);current[1]['status'].update(Qty=8,Qty2=17)
 assert run()['comparison']=='current_state_matches'

def test_changed_units_unknown(env):
 _,save,current,_=env;save('Combine');current[0]['status']['UOM']={'Name':'Other'}
 assert run()['comparison']=='incomplete'

def test_ready_no_io(env):
 _,save,_,mock=env;save(state='ready');assert 'error' in run();mock.assert_not_called()

def test_environment_no_io(env,monkeypatch):
 _,_,_,mock=env;monkeypatch.setattr(p,'CAMSTAR_SHOPFLOOR_BASE_URL','different')
 assert 'error' in run();mock.assert_not_called()

def test_disassociate(env):
 _,save,current,_=env;save('Disassociate');current[1]['parent_id']=None
 assert run()['comparison']=='current_state_matches'


@pytest.fixture
def split_env(env):
 snap,save,current,mock=env
 snap['children']=[]
 snap['body']={'autoNumber':False,'reduceQty':True,
               'toContainerDetails':[{'toContainerName':'NEW','qty':2,'qty2':3}]}
 save('Split')
 current[0]['status'].update(Qty=8,Qty2=17)
 current[1]['id']='NEW-ID';current[1]['container']['Name']='NEW'
 current[1]['status'].update(Qty=2,Qty2=3)
 return snap,save,current,mock


def test_split_match_is_only_current_state(split_env):
 _,_,_,mock=split_env
 out=run()
 assert out['comparison']=='current_state_matches' and out['comparison_complete']
 assert [c['container_name'] for c in out['checks']]==['P','NEW']
 assert out['checks'][1]['identity_basis']=='current_name_lookup_only'
 assert not out['verified_success'] and not out['state_changed'] and not out['retry_allowed']
 assert [c.args for c in mock.call_args_list]==[('P',),('NEW',)]
 with p.db_connect() as db:
  assert db.execute('SELECT state,outcome FROM plans').fetchone()==('unknown',None)
  assert db.execute('SELECT count(*) FROM resolutions').fetchone()[0]==0


@pytest.mark.parametrize('index,field,value,expected',[
 (0,'Qty',9,'current_state_differs'),(1,'Qty2',4,'current_state_differs'),
 (0,'Qty2',None,'incomplete'),(1,'Qty',True,'incomplete'),
 (1,'Qty2',float('inf'),'incomplete'),(1,'UOM',{'Name':'Other'},'incomplete'),
 (0,'UOM2',None,'incomplete')])
def test_split_quantity_and_units(split_env,index,field,value,expected):
 split_env[2][index]['status'][field]=value
 assert run()['comparison']==expected


@pytest.mark.parametrize('mode',['source_id','target_alias','target_name','target_missing','source_error'])
def test_split_identity_or_read_failure_preserves_other_result(split_env,mode):
 _,_,current,mock=split_env
 if mode=='source_id':current[0]['id']='OTHER'
 if mode=='target_alias':current[1]['id']='P'
 if mode=='target_name':current[1]['container']['Name']='OTHER'
 if mode=='target_missing':mock.side_effect=[current[0],ValueError('not found')]
 if mode=='source_error':mock.side_effect=[ValueError('denied'),current[1]]
 out=run()
 assert out['comparison']=='incomplete'
 assert len(out['checks'])==2
 assert len([c for c in out['checks'] if c['fields']])==1


@pytest.mark.parametrize('mode',['all_qty','automatic','multiple','same_name','children'])
def test_split_unsupported_plan_no_read(split_env,mode):
 snap,save,_,mock=split_env
 if mode=='all_qty':snap['body']['reduceQty']=False
 if mode=='automatic':snap['body']['autoNumber']=True
 if mode=='multiple':snap['body']['toContainerDetails']*=2
 if mode=='same_name':snap['body']['toContainerDetails'][0]['toContainerName']='p'
 if mode=='children':snap['children']=[{'child':copy.deepcopy(snap['parent'])}]
 save('Split')
 assert 'error' in run()
 mock.assert_not_called()


def test_split_in_flight_stays_locked(split_env):
 _,save,_,_=split_env;save('Split','in_flight')
 assert run()['comparison']=='current_state_matches'
 with p.db_connect() as db:assert db.execute('SELECT state FROM plans').fetchone()[0]=='in_flight'
