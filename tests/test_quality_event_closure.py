import asyncio
import json
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from tools import mcp,get_tool_func,quality_event_closure as c,quality_plans as p
from tests.test_quality_event_triage import env as base_env,EVENT,ORG,OWNER,ROLE,PRIORITY,OTHER,NCR,CONT
from agent.tool_routing import select_tools
from core.tool_catalog import TOOL_POLICIES


def run():return json.loads(asyncio.run(c.get_quality_event_closure_context('E','Org')))

@pytest.fixture
def env(base_env,monkeypatch):
    state=base_env;state['event']['TriageComplete']=True
    state['schema']=[dict(TABLE_NAME=t,COLUMN_NAME=k) for t,cols in c.SCHEMA.items() for k in cols.split(',')]
    state['inquiry']=dict(Status=1,Category=1,Organization={'InstanceId':ORG},Owner={'InstanceId':OWNER},Role={'InstanceId':ROLE},ProcessModel=None,
        IsApprovalRequired=False,CompletionApprovalStatus=None,CompletionApprovalSheet=None,EntryForApproval=None)
    state['options']=[dict(Name='Close',Value='1')]
    monkeypatch.setattr(c.q,'object_details',AsyncMock(side_effect=lambda *a: {'QualityObjectInquiryDetail':state['inquiry']}))
    monkeypatch.setattr(c.q,'closure_options',AsyncMock(side_effect=lambda *a:state['options']))
    return state


def test_clear_diagnostic_is_never_execution_ready_or_a_plan(env):
    out=run();assert out['complete'] and not out['ready_for_execution'] and not out['blockers']
    assert out['original_event_id']==EVENT and len(out['unverified_checks'])==6
    assert not p.PLAN_DB.exists();env['client'].request.assert_not_awaited()
    assert 'SHOULD_NEVER_BE_STORED' not in json.dumps(out)

@pytest.mark.parametrize('field,value,code',[
    ('Status',5,'status'),('IsFrozen',True,'frozen'),('IsSubmitted',False,'not_submitted'),
    ('TriageComplete',False,'not_triaged'),('IsCARRequiredToClose',True,'car_review'),
    ('ProcessModelId',OTHER,'process_review'),('ChecklistId',OTHER,'checklist_review'),('CloseDateGMT','2026-10-29','close_information')])
def test_blockers_are_explicit_without_inventing_completion(env,field,value,code):
    env['event'][field]=value
    if field=='Status':env['inquiry']['Status']=value
    out=run();assert out['complete'] and code in {r['code'] for r in out['blockers']}
    assert not out['ready_for_execution']


def test_signature_is_scoped_to_close_and_does_not_bypass_all_quality_signatures(env):
    env['roots']['Organizations']['QualityESigTxns']=[7578]
    assert not run()['blockers']
    env['roots']['Organizations']['QualityESigTxns']=[7712]
    assert 'signature' in {r['code'] for r in run()['blockers']}
    env['roots']['Organizations']['CollectESigForAllQualityTxns']=True
    assert not run()['complete']


def test_approvals_and_missing_close_option_report_together(env):
    env['approvals']=[{'ApprovalSheetId':OTHER}];env['inquiry']['IsApprovalRequired']=True;env['options']=[]
    assert {r['code'] for r in run()['blockers']}=={'approval_review','approval_required','close_option'}

@pytest.mark.parametrize('mode',['missing','wrong_type','schema','mismatch','incomplete','ncr_limit','ncr_identity','duplicate'])
def test_incomplete_or_inconsistent_sources_fail_closed(env,mode):
    if mode=='missing':env['event']=None
    elif mode=='wrong_type':env['event']['CDOTypeId']=7540
    elif mode=='schema':env['schema'].pop()
    elif mode=='mismatch':env['inquiry']['Status']=5
    elif mode=='incomplete':env['inquiry'].pop('CompletionApprovalSheet')
    else:
        row=dict(NonconformanceReportId=NCR,EscalatedEventId=EVENT,ContainerId=CONT,ChangeCount=1)
        env['links']=[row]* (21 if mode=='ncr_limit' else 2 if mode=='duplicate' else 1)
        if mode=='ncr_identity':row['EscalatedEventId']=OTHER
    out=run();assert not out['complete'] and not out['ready_for_execution']
    env['client'].request.assert_not_awaited()


def test_native_rules_are_captured_without_claiming_deployed_behavior():
    from pathlib import Path
    data=json.loads(Path('docs/quality-fixtures/event-close-native-execution.json').read_text(encoding='utf8'))
    close=data['CloseQualityObject_ValidateClose']
    values=[p['value'] for x in close['calls'] for p in x['parameters']]
    assert any('CheckOutstandingLots' in v for v in values) and any('IsCARRequiredToClose' in v for v in values)


def test_registration_closed_schema_and_readonly_routing(env):
    async def check():
        assert get_tool_func('get_quality_event_closure_context') is c.get_quality_event_closure_context
        async with Client(mcp) as client:
            schema=next(t.input_schema for t in await client.list_tools() if t.name=='get_quality_event_closure_context')
            assert schema['additionalProperties'] is False
            out=await client.call_tool('get_quality_event_closure_context',dict(event_name='E',organization_name='Org'))
            assert json.loads(out.content[0].text)['complete']
    asyncio.run(check())
    chosen={x['function']['name'] for x in select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[dict(role='user',content='查看质量事件关闭前置')])}
    assert 'get_quality_event_closure_context' in chosen and 'execute_quality_transaction' not in chosen
