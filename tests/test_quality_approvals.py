import asyncio
import copy
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from tools import mcp,get_tool_func,quality_approvals as a,quality_plans as p
from tests.test_quality_event_triage import env as base_env,EVENT,ORG,OWNER,ROLE,OTHER

SHEET,ENTRY,LIST,DECISION,CAPA=[f'{x:016x}' for x in range(4001,4006)]
def run(**kw):return json.loads(asyncio.run(a.get_quality_approval_context('E','Org',**kw)))

@pytest.fixture
def env(base_env,monkeypatch):
 s=base_env
 sheet=dict.fromkeys(a.SCHEMA['ApprovalSheet'].split(','));sheet.update(ApprovalSheetId=SHEET,ApprovalSheetName='FromCompleted',ParentId=EVENT,
  Status=10,IsFrozen=False,ChangeCount=1,ApprovalRequired=True,ApprovalDecisionListId=LIST,LastApprovalCycleGMT='2026-10-29T01:00:00Z')
 entry=dict.fromkeys(a.SCHEMA['ApprovalSheetEntry'].split(','));entry.update(ApprovalSheetEntryId=ENTRY,ParentId=SHEET,Status=10,SheetLevel=1,
  ApproverId=OWNER,ApproverRoleId=ROLE,AssignedApproverId=OWNER,SubstituteOption=1,EntryRequired=True,IsFrozen=False,ChangeCount=1,DecisionType=0)
 decision=dict(ApprovalDecisionId=DECISION,ApprovalDecisionListId=LIST,ApprovalDecisionName='Accept',DecisionType=1,IncludeComments=True,IsFrozen=False,ChangeCount=1)
 s.update(sheets=[sheet],entries=[entry],decisions=[decision],decision_list=dict(ApprovalDecisionListId=LIST,ApprovalDecisionListName='Standard',IsFrozen=False,ChangeCount=1),
  inquiry=dict(Status=1,Category=1,Organization={'InstanceId':ORG},Owner={'InstanceId':OWNER},Role={'InstanceId':ROLE},ProcessModel=None,
   IsApprovalRequired=True,CompletionApprovalSheet={'InstanceId':SHEET},CompletionApprovalStatus=10,EntryForApproval={'InstanceId':ENTRY}),
  capa=dict(CAPAId=CAPA,CAPAName='E',Status=1,Category=5,OrganizationId=ORG,ChangeCount=1,IsFrozen=False),entry_hook=None)
 original=a.q.sql_rows
 async def sql(query,maximum):
  if 'INFORMATION_SCHEMA.COLUMNS' in query:
   return [dict(TABLE_NAME=t,COLUMN_NAME=c) for t,cols in re.findall(r"TABLE_NAME='(\w+)' AND COLUMN_NAME IN \((.*?)\)",query) for c in re.findall(r"'([^']+)'",cols)]
  if ' FROM SignApprovalHistory WHERE ' in query:return []
  for table,key in [('ApprovalSheet','sheets'),('ApprovalSheetEntry','entries'),('ApprovalDecision','decisions')]:
   if ' FROM '+table+' WHERE ' in query:
    if table=='ApprovalSheetEntry' and s['entry_hook']:s['entry_hook']()
    return copy.deepcopy(s[key])
  if ' FROM ApprovalDecisionList WHERE ' in query:return [copy.deepcopy(s['decision_list'])]
  return await original(query,maximum)
 monkeypatch.setattr(a.q,'sql_rows',AsyncMock(side_effect=sql))
 monkeypatch.setattr(a.q,'object_details',AsyncMock(side_effect=lambda *x:{'QualityObjectInquiryDetail':copy.deepcopy(s['inquiry'])}))
 monkeypatch.setattr(a.q,'find_record',AsyncMock(side_effect=lambda *x:copy.deepcopy(s['capa'])))
 return s


def test_complete_context_is_not_permission_and_never_creates_plan(env):
 out=run();assert out['complete'] and not out['ready_for_execution'] and not out['actor_permission_verified']
 assert out['current_employee_entry_id']==ENTRY and out['sheets'][0]['entries'][0]['routed_candidate']
 assert out['sheets'][0]['allowed_decisions'][0]['IncludeComments']
 assert not p.PLAN_DB.exists() and 'SHOULD_NEVER_BE_STORED' not in json.dumps(out)
 env['client'].request.assert_not_awaited()


def test_capa_direct_sheets_are_supported(env):
 env['sheets'][0]['ParentId']=CAPA;env['inquiry']['Category']=5
 out=run(object_kind='capa');assert out['complete'] and out['target']['Id']==CAPA


def test_signature_requirements_do_not_block_readonly_context(env):
 env['roots']['Organizations'].update(CollectESigForAllQualityTxns=True,QualityESigTxns=[7839,8567])
 out=run();assert out['complete'] and out['signature_policy']['sign_approval_required'] and out['signature_policy']['route_approval_required']
 assert not out['signature_policy']['dynamic_requirements_verified']

@pytest.mark.parametrize('status,label',[(0,'Pending'),(10,'Routed'),(20,'Cancelled'),(30,'Rejected'),(40,'Approved'),(999,'Unknown')])
def test_native_status_labels_do_not_guess_other_values(env,status,label):
 env['sheets'][0]['Status']=status;env['entries'][0]['Status']=status
 out=run();assert out['complete'] and out['sheets'][0]['status_name']==label
 assert out['sheets'][0]['entries'][0]['routed_candidate'] is (status==10)

@pytest.mark.parametrize('mode',['sheet_frozen','entry_frozen','role_substitute','unknown_substitute'])
def test_candidate_and_substitute_policy_are_not_actor_permission(env,mode):
 if mode=='sheet_frozen':env['sheets'][0]['IsFrozen']=True
 elif mode=='entry_frozen':env['entries'][0]['IsFrozen']=True
 else:env['entries'][0]['SubstituteOption']=2 if mode=='role_substitute' else 999
 out=run();assert out['complete'] and not out['actor_permission_verified']
 if mode.endswith('frozen'):assert not out['sheets'][0]['entries'][0]['routed_candidate']
 else:assert out['sheets'][0]['entries'][0]['substitute_policy']==('role_substitute' if mode=='role_substitute' else 'unknown')

@pytest.mark.parametrize('mode',['sheet_parent','entry_parent','decision_parent','list_identity','current_entry','current_sheet','inquiry_state','inquiry_org','missing_field','duplicate_sheet','duplicate_entry','duplicate_decision'])
def test_incomplete_or_cross_object_data_cannot_be_called_complete(env,mode):
 if mode=='sheet_parent':env['sheets'][0]['ParentId']=OTHER
 elif mode=='entry_parent':env['entries'][0]['ParentId']=OTHER
 elif mode=='decision_parent':env['decisions'][0]['ApprovalDecisionListId']=OTHER
 elif mode=='list_identity':env['decision_list']['ApprovalDecisionListId']=OTHER
 elif mode=='current_entry':env['inquiry']['EntryForApproval']={'InstanceId':OTHER}
 elif mode=='current_sheet':env['inquiry']['CompletionApprovalSheet']={'InstanceId':OTHER}
 elif mode=='inquiry_state':env['inquiry']['Status']=5
 elif mode=='inquiry_org':env['inquiry']['Organization']={'InstanceId':OTHER}
 elif mode=='missing_field':env['entries'][0].pop('SubstituteOption')
 elif mode=='duplicate_sheet':env['sheets']*=2
 elif mode=='duplicate_entry':env['entries']*=2
 else:env['decisions']*=2
 out=run();assert not out['complete'] and not out['ready_for_execution']

@pytest.mark.parametrize('which,count',[('sheets',21),('entries',101),('decisions',51)])
def test_bounded_reads_refuse_partial_results(env,which,count):
 env[which]*=count
 out=run();assert not out['complete']


def test_changed_cycle_or_entry_is_not_returned_as_stable(env):
 count=[0]
 def mutate():
  count[0]+=1
  if count[0]>=2:env['entries'][0]['ChangeCount']=2
 env['entry_hook']=mutate
 assert not run()['complete']


def test_oversupplied_fields_and_employee_credentials_are_never_emitted(env):
 for key in ('sheets','entries','decisions'):
  env[key][0]['Password']='NEVER_EMIT_EXTRA_SQL_FIELDS'
 env['decision_list']['Password']='NEVER_EMIT_EXTRA_SQL_FIELDS'
 out=run();assert out['complete'] and 'NEVER_EMIT_EXTRA_SQL_FIELDS' not in json.dumps(out)


def test_empty_direct_sheets_are_not_global_no_approval_claim(env):
 env.update(sheets=[],entries=[]);env['inquiry'].update(CompletionApprovalSheet=None,CompletionApprovalStatus=None,EntryForApproval=None)
 out=run();assert out['complete'] and out['scope']=='direct_quality_object_approval_sheets'
 assert out['current_context']['IsApprovalRequired'] and not out['ready_for_execution']


def test_primary_schema_and_native_sign_validation_are_evidence():
 d=json.loads(Path('docs/quality-fixtures/approval-native-details.json').read_text(encoding='utf8'))
 cols={x['query']['owner']:{r['name'] for r in x['result'].get('records',[])} for x in d['metadata']['results'] if x['query']['kind']=='column'}
 policy=json.loads(Path('docs/quality-fixtures/approval-native-policy.json').read_text(encoding='utf8'))
 for x in policy['results']:
  if x['query']['kind']=='column':cols[x['query']['owner']]={r['name'] for r in x['result']['records']}
 for table in ('ApprovalSheet','ApprovalSheetEntry','ApprovalDecision','ApprovalDecisionList'):assert set(a.SCHEMA[table].split(','))<=cols[table]
 values=[p['value'] for c in d['clfs']['SignApproval_Validate']['calls'] for p in c['parameters']]
 assert 'CheckRoleAndOrg' in values
 values=[p['value'] for c in d['clfs']['RouteApproval_PreValidate']['calls'] for p in c['parameters']]
 assert 'ClearApprovals' in values


def test_mcp_schema_closed_and_readonly_registration(env):
 async def check():
  assert get_tool_func('get_quality_approval_context') is a.get_quality_approval_context
  async with Client(mcp) as client:
   schema=next(t.input_schema for t in await client.list_tools() if t.name=='get_quality_approval_context')
   assert schema['additionalProperties'] is False and not {'password','decision','signer','submit'}&schema['properties'].keys()
   reply=await client.call_tool('get_quality_approval_context',dict(name='E',organization_name='Org'))
   assert json.loads(reply.content[0].text)['complete']
 asyncio.run(check());env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('turns',[
 ['查看审批层级'],['谁可以签批'],['查询质量审批允许的决策'],['查看CAPA审批上下文','继续']])
def test_approval_questions_and_followups_are_readonly(turns):
 from core.tool_catalog import TOOL_POLICIES
 from agent.tool_routing import select_tools,ROUTING_INSTRUCTION
 chosen={x['function']['name'] for x in select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[dict(role='user',content=t) for t in turns])}
 assert 'get_quality_approval_context' in chosen and 'execute_quality_transaction' not in chosen
 assert '不使用事件关闭/分流或普通确认替代审批/电子签名' in ROUTING_INSTRUCTION
