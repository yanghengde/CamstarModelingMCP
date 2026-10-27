import asyncio,json
from unittest.mock import AsyncMock
import pytest
from core.import_lock import workflow_locks
from tools import import_plans as p, workflow_steps


def test_overlap_conflicts_and_releases_on_exception(tmp_path):
 a=[{'name':'WF','revision':'A'}];alias=[{'name':'wf','revision':'a'}]
 with pytest.raises(ValueError):
  with workflow_locks(tmp_path,a):
   with pytest.raises(RuntimeError):
    with workflow_locks(tmp_path,alias):pass
   with workflow_locks(tmp_path,[{'name':'OTHER','revision':'A'}]):pass
   raise ValueError('cancel')
 with workflow_locks(tmp_path,alias):pass


def store(monkeypatch,tmp_path):
 monkeypatch.setattr(p,'PLAN_DB',tmp_path/'plans.db')
 plan={'environment_id':p._environment_id(),'workflows':[{'name':'WF','revision':'A','id':None,'before':None,'after_rows':[{'step_name':'S','spec_name':'SPEC','spec_revision':'A'}]}]}
 with p._db() as db:
  db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',('plan',p._digest(plan),json.dumps(plan),'stopped',0,json.dumps({'actions':[]})))
 return plan


def test_reconciliation_reads_only_and_keeps_plan_status(monkeypatch,tmp_path):
 store(monkeypatch,tmp_path)
 monkeypatch.setattr(p.ReadContext,'get',AsyncMock(return_value={'value':[{'InstanceID':'wf'}]}))
 monkeypatch.setattr(workflow_steps,'_read_steps',AsyncMock(return_value={'steps_complete':True,'standard_route_complete':True,'standard_route':['S'],'steps':[{'name':'S','spec':{'Name':'SPEC','Revision':'A'}}]}))
 write=AsyncMock();monkeypatch.setattr(p,'request',write)
 result=json.loads(asyncio.run(p.reconcile_workflow_import_plan('plan')))
 assert result['workflows'][0]['comparison']=='matches_planned_route_and_specs'
 assert result['plan_status']=='stopped'
 with p._db() as db: assert db.execute('SELECT state FROM plans').fetchone()[0]=='stopped'
 write.assert_not_called()


def test_reconciliation_failure_is_unknown(monkeypatch,tmp_path):
 store(monkeypatch,tmp_path)
 monkeypatch.setattr(p.ReadContext,'get',AsyncMock(side_effect=ValueError('offline')))
 result=json.loads(asyncio.run(p.reconcile_workflow_import_plan('plan')))
 assert result['workflows'][0]['comparison']=='unknown'


def test_lock_conflict_does_not_claim_plan(monkeypatch,tmp_path):
 plan=store(monkeypatch,tmp_path)
 with p._db() as db:db.execute("UPDATE plans SET state='ready'")
 with workflow_locks(tmp_path/'import_locks',plan['workflows']):
  result=json.loads(asyncio.run(p.execute_workflow_import_plan('plan',p._digest(plan))))
 assert result['error']
 with p._db() as db:assert db.execute('SELECT state FROM plans').fetchone()[0]=='ready'

def test_environment_switch_blocks_before_any_read_or_write(monkeypatch,tmp_path):
 import time
 plan=store(monkeypatch,tmp_path)
 with p._db() as db:db.execute("UPDATE plans SET state='ready',created=?",(time.time(),))
 monkeypatch.setattr(p,'CAMSTAR_BASE_URL','https://another-server/Modeling')
 read=AsyncMock();write=AsyncMock()
 monkeypatch.setattr(p,'_existing',read);monkeypatch.setattr(p,'request',write)
 result=json.loads(asyncio.run(p.execute_workflow_import_plan('plan',p._digest(plan))))
 assert result['error']
 read.assert_not_called();write.assert_not_called()
 with p._db() as db:assert db.execute('SELECT state FROM plans').fetchone()[0]=='ready'
 assert json.loads(asyncio.run(p.reconcile_workflow_import_plan('plan')))['error']


def test_second_preflight_detects_change(monkeypatch,tmp_path):
 import time
 plan=store(monkeypatch,tmp_path)
 with p._db() as db:db.execute("UPDATE plans SET state='ready',created=?",(time.time(),))
 # Add the required pinned Spec ID to this test plan.
 plan['workflows'][0]['after_rows'][0]['spec_id']='spec-id'
 with p._db() as db:db.execute('UPDATE plans SET digest=?,payload=?',(p._digest(plan),json.dumps(plan)))
 monkeypatch.setattr(p,'_existing',AsyncMock(side_effect=[(None,None),('changed',{})]))
 monkeypatch.setattr(p.ReadContext,'resolve',AsyncMock(return_value={}))
 write=AsyncMock();monkeypatch.setattr(p,'request',write)
 result=json.loads(asyncio.run(p.execute_workflow_import_plan('plan',p._digest(plan))))
 assert result['status']=='stopped' and result['actions']==[]
 write.assert_not_called()


def test_lock_blocks_second_process(tmp_path):
 import subprocess,sys
 script="""
import sys
from pathlib import Path
from core.import_lock import workflow_locks
try:
    with workflow_locks(Path(sys.argv[1]),[{'name':'WF','revision':'A'}]):
        sys.exit(2)
except RuntimeError:
    sys.exit(0)
"""
 with workflow_locks(tmp_path,[{'name':'WF','revision':'A'}]):
  result=subprocess.run([sys.executable,'-c',script,str(tmp_path)],timeout=10,capture_output=True)
 assert result.returncode==0,result.stderr
