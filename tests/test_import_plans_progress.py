import asyncio
import json
from unittest.mock import AsyncMock
import pandas as pd
import pytest
from tools import import_plans as p, order_progress as progress


def setup_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(p, 'PLAN_DB', tmp_path / 'plans.db')
    monkeypatch.setattr(p.pd, 'read_excel', lambda *a, **k: pd.DataFrame([{'W':'WF','R':'A','S':'S1','P':'SPEC'}]))
    monkeypatch.setattr(p, '_existing', AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(p.ReadContext, 'resolve', AsyncMock(return_value={'Name':'SPEC','Revision':'A','InstanceID':'spec-id'}))
    return json.dumps({'sheet_name':'Sheet1','fields':{'workflow_name':'W','workflow_revision':'R','step_name':'S','spec_name':'P'}})


def test_preview_freezes_rows_and_has_no_writes(monkeypatch,tmp_path):
    mapping=setup_plan(monkeypatch,tmp_path)
    write=AsyncMock();monkeypatch.setattr(p,'request',write)
    result=json.loads(asyncio.run(p.preview_workflow_import('file',mapping)))
    assert result['executable']
    row=result['plan']['workflows'][0]['after_rows'][0]
    assert row['excel_row']==2 and row['spec_revision']=='A'
    write.assert_not_called()


def test_missing_rows_block_whole_plan(monkeypatch,tmp_path):
    mapping=setup_plan(monkeypatch,tmp_path)
    monkeypatch.setattr(p.pd,'read_excel',lambda *a,**k:pd.DataFrame([{'W':'WF','R':'A','S':'','P':'SPEC'}]))
    result=json.loads(asyncio.run(p.preview_workflow_import('file',mapping)))
    assert not result['executable'] and result['errors'][0]['row']==2


def test_changed_target_blocks_before_write_and_consumes_plan(monkeypatch,tmp_path):
    mapping=setup_plan(monkeypatch,tmp_path)
    plan=json.loads(asyncio.run(p.preview_workflow_import('file',mapping)))
    monkeypatch.setattr(p,'_existing',AsyncMock(return_value=('new-id',{})))
    write=AsyncMock();monkeypatch.setattr(p,'request',write)
    first=json.loads(asyncio.run(p.execute_workflow_import_plan(plan['plan_id'],plan['plan_digest'])))
    second=json.loads(asyncio.run(p.execute_workflow_import_plan(plan['plan_id'],plan['plan_digest'])))
    assert first['status']=='stopped' and second['error']
    write.assert_not_called()


def test_write_error_stops(monkeypatch):
    monkeypatch.setattr(p,'request',AsyncMock(return_value='Error: unavailable'))
    outcomes=[]
    with pytest.raises(ValueError):asyncio.run(p._write('PUT','/api/Workflows/id',{},outcomes))
    assert outcomes[0]['success'] is False


def test_progress_never_infers_completion(monkeypatch):
    identifier='0006548000000002'
    monkeypatch.setattr(progress.ReadContext,'resolve',AsyncMock(return_value={'Name':'MO','InstanceID':identifier,'Qty':100}))
    mock=AsyncMock(return_value=json.dumps([{'ContainerId':'cid','ContainerName':'BOX'}]))
    monkeypatch.setattr(progress,'request',mock)
    monkeypatch.setattr(progress,'get_container_details',AsyncMock(return_value=json.dumps({'data':{'ContainerStatusDetails':{'MfgOrder':{'InstanceId':identifier},'StatusName':'Closed','Qty':1}}})))
    result=json.loads(asyncio.run(progress.get_mfgorder_progress('MO',include_history=False)))
    assert result['returned_container_count']==1
    assert result['production_completed_qty'] is None
    assert result['completion_percentage'] is None
    assert result['containers'][0]['current']['OnHold'] is None
    assert identifier in mock.call_args.kwargs['body']['queryText']


def test_progress_reassignment_and_query_failure(monkeypatch):
    monkeypatch.setattr(progress.ReadContext,'resolve',AsyncMock(return_value={'Name':'MO','InstanceID':'0006548000000002'}))
    monkeypatch.setattr(progress,'request',AsyncMock(return_value='Error: unavailable'))
    result=json.loads(asyncio.run(progress.get_mfgorder_progress('MO')))
    assert result['error'] and not result['complete']

def test_execution_uses_stored_plan_and_is_single_use(monkeypatch,tmp_path):
    from tools import workflow_steps
    mapping=setup_plan(monkeypatch,tmp_path)
    plan=json.loads(asyncio.run(p.preview_workflow_import('file',mapping)))
    monkeypatch.setattr(p.pd,'read_excel',lambda *a,**k: (_ for _ in ()).throw(AssertionError('must not reread file')))
    monkeypatch.setattr(p.ReadContext,'get',AsyncMock(return_value={'value':[{'InstanceID':'new-workflow'}]}))
    write=AsyncMock(return_value='{"ok":true}')
    monkeypatch.setattr(p,'request',write)
    monkeypatch.setattr(workflow_steps,'_read_steps',AsyncMock(return_value={'standard_route_complete':True,'standard_route':['S1'],'steps':[{'name':'S1','spec':{'Name':'SPEC','Revision':'A'}}]}))
    result=json.loads(asyncio.run(p.execute_workflow_import_plan(plan['plan_id'],plan['plan_digest'])))
    assert result['status']=='verified' and write.await_count==2
    again=json.loads(asyncio.run(p.execute_workflow_import_plan(plan['plan_id'],plan['plan_digest'])))
    assert again['error'] and write.await_count==2
    stored=json.loads(asyncio.run(p.get_workflow_import_plan(plan['plan_id'])))
    assert stored['status']=='verified' and len(stored['outcome']['actions'])==2


def test_duplicate_steps_and_wrong_digest_block(monkeypatch,tmp_path):
    mapping=setup_plan(monkeypatch,tmp_path)
    plan=json.loads(asyncio.run(p.preview_workflow_import('file',mapping)))
    write=AsyncMock();monkeypatch.setattr(p,'request',write)
    assert json.loads(asyncio.run(p.execute_workflow_import_plan(plan['plan_id'],'wrong')))['error']
    write.assert_not_called()
    monkeypatch.setattr(p.pd,'read_excel',lambda *a,**k:pd.DataFrame([{'W':'WF','R':'A','S':'S1','P':'SPEC'}]*2))
    assert not json.loads(asyncio.run(p.preview_workflow_import('file',mapping)))['executable']

def test_confirmation_includes_saved_rows(monkeypatch,tmp_path):
    from agent.safety import plan_preview
    mapping=setup_plan(monkeypatch,tmp_path)
    plan=json.loads(asyncio.run(p.preview_workflow_import('file',mapping)))
    text=plan_preview([{'function':{'name':'execute_workflow_import_plan','arguments':json.dumps({'plan_id':plan['plan_id'],'plan_digest':plan['plan_digest']})}}])
    assert 'S1' in text and 'SPEC/A' in text and 'WF/A' in text


def test_cancelled_request_remains_uncertain_in_journal(monkeypatch,tmp_path):
    mapping=setup_plan(monkeypatch,tmp_path)
    plan=json.loads(asyncio.run(p.preview_workflow_import('file',mapping)))
    monkeypatch.setattr(p,'request',AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(p._write('PUT','/api/Workflows/id',{},[],plan['plan_id']))
    stored=json.loads(asyncio.run(p.get_workflow_import_plan(plan['plan_id'])))
    assert stored['outcome']['actions'][0]['success'] is None
