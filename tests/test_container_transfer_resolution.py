import asyncio,json
import pytest
from tools import container_transfer_plans as p,get_tool_func
from core.tool_catalog import TOOL_POLICIES
from agent.safety import plan_preview

@pytest.fixture
def env(monkeypatch,tmp_path):
 monkeypatch.setattr(p,'PLAN_DB',tmp_path/'plans.sqlite')
 snap={'environment':p.CAMSTAR_SHOPFLOOR_BASE_URL.rstrip('/'),'operation':'Associate','body':{'container':{'name':'P'},'childContainers':[{'name':'C'}]}}
 dg=p.digest(snap)
 with p.db_connect() as db:db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',('p',dg,json.dumps({'snapshot':snap}),'unknown',0,json.dumps({'response':'timeout'})))
 return dg

def resolve(dg,**kw):
 args={'plan_id':'p','plan_digest':dg,'conclusion':'committed','evidence_reference':'audit/transaction/123','review_note':'Operator checked service transaction audit.'};args.update(kw)
 return json.loads(asyncio.run(p.resolve_container_transfer_outcome(**args)))

@pytest.mark.parametrize('conclusion',['committed','not_committed'])
def test_audited_terminal(env,conclusion):
 r=resolve(env,conclusion=conclusion)
 assert r['state_changed'] and not r['verified_success'] and not r['original_plan_reusable'] and not r['mes_write_performed']
 with p.db_connect() as db:
  record=json.loads(db.execute('SELECT record FROM resolutions').fetchone()[0])
  assert record['previous_outcome']=={'response':'timeout'}
  assert db.execute('SELECT state FROM plans').fetchone()[0]=='manually_confirmed_'+conclusion
 assert not resolve(env)['state_changed']
 assert not json.loads(asyncio.run(p.execute_container_association('p',env)))['submitted']

@pytest.mark.parametrize('state',['ready','in_flight','manually_confirmed_committed'])
def test_not_unknown(env,state):
 with p.db_connect() as db:db.execute('UPDATE plans SET state=?',(state,))
 assert not resolve(env)['state_changed']

@pytest.mark.parametrize('kw',[{'evidence_reference':''},{'review_note':' '},{'conclusion':'maybe'},{'plan_digest':'wrong'}])
def test_missing_evidence(env,kw):assert not resolve(env,**kw)['state_changed']

def test_environment(env,monkeypatch):
 monkeypatch.setattr(p,'CAMSTAR_SHOPFLOOR_BASE_URL','other')
 assert not resolve(env)['state_changed']

def test_confirmation(env):
 args={'plan_id':'p','plan_digest':env,'conclusion':'not_committed','evidence_reference':'audit/123','review_note':'manual check'}
 text=plan_preview([{'function':{'name':'resolve_container_transfer_outcome','arguments':json.dumps(args)}}])
 assert 'childContainers' in text and 'audit/123' in text and '解除' in text
 assert TOOL_POLICIES['resolve_container_transfer_outcome'].always_confirm
 assert get_tool_func('resolve_container_transfer_outcome')
