import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from tools import mcp,quality_approval_decisions as c,quality_plans as p
from tests.test_quality_approval_actions import env as actions_env,approval_env,base_env,EVENT,ORG,OWNER,ROLE,OTHER,SHEET,ENTRY,LIST,DECISION,routed,execute,snap
from agent.safety import evaluate_mutations,MutationCounts,plan_preview

def result(x):return json.loads(asyncio.run(x))
def prepare(action='approve',**kw):return result(c.prepare_quality_approval_decision(action,'E','Org',SHEET,ENTRY,DECISION,'Reviewed decision',**kw))

@pytest.fixture
def env(actions_env,monkeypatch):
 s=actions_env;routed(s);s['decisions'][0]['DecisionType']=10;s.update(decision_hook=None,decision_contract_bad=False,sign_requests=[])
 async def transport(method,path,**kw):
  s['sign_requests'].append((method,path,copy.deepcopy(kw)))
  if method=='GET':
   props={k:{'$ref':'#/components/schemas/'+v} for k,v in dict(qualityObject='NamedObjectRef',organization='NamedObjectRef',approvalSheet='NamedSubentityRef',approvalSheetEntry='SubentityRef',approvalDecision='NamedSubentityRef').items()}
   if s['decision_contract_bad']:props['approvalSheetEntry']['$ref']='#/components/schemas/NamedObjectRef'
   return json.dumps({'components':{'schemas':{'SignApproval':{'properties':props}}}})
  assert path=='/api/SignApproval' and kw['params']['execute']=='false'
  from tests.test_quality_event_triage import echo
  d=echo(copy.deepcopy(kw['body']));d.update(Employee={'InstanceId':OWNER},AvailableActions=513,IgnoreProcessModel=False,ESigRequirement=None,AdditionalESigReqs=[])
  row=s['event'];d['QualityObjectDetail']=dict(Status=row['Status'],Category=row['Category'],Organization={'InstanceId':ORG},EntryForApproval={'InstanceId':ENTRY},ProcessModel=None,UserIsOwner=False)
  if s['decision_hook']:s['decision_hook'](d)
  return json.dumps({'data':d})
 monkeypatch.setattr(c,'request',AsyncMock(side_effect=transport))
 return s

@pytest.mark.parametrize('action,typ',[('approve',10),('reject',20)])
def test_native_current_actor_decision_is_once_only_and_not_signature_override(env,action,typ):
 env['decisions'][0]['DecisionType']=typ
 ready=prepare(action);assert ready.get('state')=='ready',ready
 body=ready['request_preview']['body'];assert body['approvalSheetEntry']['instanceId']==ENTRY and body['approvalDecision']['instanceId']==DECISION
 assert not {'employee','password','approvingFor','approvingForRole','processESignatures'}&body.keys()
 assert ready['server_context']['decision_type']==typ and not ready['server_context']['extra_electronic_signature_performed']
 assert execute(ready)['state']=='unknown';env['client'].request.assert_awaited_once()
 assert env['client'].request.await_args.args[1].endswith('/api/SignApproval')
 assert 'Password' not in env['client'].request.await_args.kwargs['params']['$select']
 assert 'error' in execute(ready)

@pytest.mark.parametrize('field,value',[('Status',40),('DecisionType',False),('SubstituteOption',2),('IsFrozen',True),('DecisionType',10),('ApprovalDecisionId',DECISION),('ApprovedById',OWNER),('LastDecisionId',OTHER),('LastCompletedOnGMT','old'),('SheetLevel',0)])
def test_not_routed_substitution_or_prior_decision_is_blocked(env,field,value):
 env['entries'][0][field]=value
 assert 'error' in prepare();env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('mode',['wrong_type','frozen','missing','bad_list','sheet_frozen','sheet_pending','unknown_cycle','boolean_cycle','current_entry'])
def test_exact_sheet_list_decision_and_cycle_required(env,mode):
 if mode=='wrong_type':env['decisions'][0]['DecisionType']=20
 elif mode=='frozen':env['decisions'][0]['IsFrozen']=True
 elif mode=='missing':env['decisions']=[]
 elif mode=='bad_list':env['decisions'][0]['ApprovalDecisionListId']=OTHER
 elif mode=='sheet_frozen':env['sheets'][0]['IsFrozen']=True
 elif mode=='sheet_pending':env['sheets'][0]['Status']=0
 elif mode=='unknown_cycle':env['sheets'][0]['LastApprovalCycleGMT']=None
 elif mode=='boolean_cycle':env['sheets'][0]['LastApprovalCycleGMT']=True
 else:env['inquiry']['EntryForApproval']=None
 assert 'error' in prepare()


def test_no_first_decision_guess_when_native_code_is_routing_canceled(env):
 env['decisions'][0]['DecisionType']=30
 assert 'error' in prepare('reject')

@pytest.mark.parametrize('field,value',[('Employee',{'InstanceId':OTHER}),('AvailableActions',1),('IgnoreProcessModel',True),('ESigRequirement',{}),('AdditionalESigReqs',[{}]),('ApprovalDecision',{'InstanceId':OTHER})])
def test_native_actor_permission_signature_and_echo_cannot_be_overridden(env,field,value):
 env['decision_hook']=lambda d:d.update({field:value})
 assert 'error' in prepare()


def test_organization_signapproval_signature_required_is_blocked(env):
 env['roots']['Organizations']['QualityESigTxns']=[7839]
 assert 'error' in prepare();c.request.assert_not_awaited()


def test_missing_user_comments_cannot_be_satisfied_by_generated_marker(env):
 out=result(c.prepare_quality_approval_decision('approve','E','Org',SHEET,ENTRY,DECISION,' '))
 assert 'error' in out;c.request.assert_not_awaited()


def test_parent_owner_is_not_used_as_substitute_for_actual_approver(env):
 env['event']['OwnerId']=OTHER
 ready=prepare();assert ready.get('state')=='ready',ready
 assert ready['server_context']['employee_id']==OWNER


def test_notifications_require_explicit_allow_and_changes_invalidate(env):
 env['policy'].update(smtp_configured=True,entries=[{'notification_type':1}])
 assert 'error' in prepare()
 ready=prepare(allow_notifications=True);assert ready.get('state')=='ready',ready
 env['policy']['entries'].append({'notification_type':2})
 assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_lower_level_unfinished_blocks_even_if_current_entry_routed(env):
 env['entries'][0]['SheetLevel']=2
 lower=copy.deepcopy(env['entries'][0]);lower.update(ApprovalSheetEntryId=OTHER,SheetLevel=1,Status=0)
 env['entries'].append(lower)
 assert 'error' in prepare()

@pytest.mark.parametrize('what',['decision','entry','cycle','actor_role','current_actor'])
def test_ready_snapshot_changes_stop_submit(env,what):
 ready=prepare();assert ready.get('state')=='ready',ready
 if what=='decision':env['decisions'][0]['ChangeCount']=2
 elif what=='entry':env['entries'][0]['ChangeCount']=2
 elif what=='cycle':env['sheets'][0]['LastApprovalCycleGMT']='new'
 elif what=='actor_role':env['counters'][ROLE]=2
 else:env['decision_hook']=lambda d:d.update(Employee={'InstanceId':OTHER})
 assert 'error' in execute(ready);env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('mode',['timeout','cancel','400'])
def test_unknown_outcomes_never_replay(env,mode):
 ready=prepare()
 if mode=='400':env['client'].request.return_value.status_code=400
 else:env['client'].request.side_effect=TimeoutError('uncertain') if mode=='timeout' else asyncio.CancelledError()
 if mode=='cancel':
  with pytest.raises(asyncio.CancelledError):execute(ready)
 else:assert execute(ready)['state']=='unknown'
 assert 'error' in execute(ready);env['client'].request.assert_awaited_once()

@pytest.mark.parametrize('field,value',[('employee',{'instanceId':OTHER}),('password','SECRET'),('processESignatures',False),('approvalSheetEntry',{'instanceId':OTHER}),('approvalDecision',{'instanceId':OTHER})])
def test_transport_forbids_impersonation_and_target_changes(env,field,value):
 saved=snap(prepare());saved['body'][field]=value
 with pytest.raises(ValueError):p.transport(saved)


def test_inspect_retains_original_entry_without_attribution(env):
 ready=prepare();execute(ready);env['entries'][0].update(Status=40,DecisionType=10,ApprovedById=OWNER,LastDecisionId=OTHER)
 out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
 assert out['original_entry_id']==ENTRY and out['current_original_entry']['Status']==40 and not out['verified_success'] and not out['retry_allowed']


def test_unknown_decision_conflicts_with_cancel_and_close(env):
 ready=prepare();execute(ready);saved=snap(ready)
 for kind in ('quality_approval','event_close','capa'):
  assert p.conflicts(saved,dict(kind=kind,target_id=EVENT,scope_key='other',environment=p.environment(),body={},before={'event':{'OrganizationId':ORG},'context':{'target':{'OrganizationId':ORG}}},resource_writes=['runtime:quality-event:id:'+EVENT]))


def test_runtime_contract_guard(env):
 env['decision_contract_bad']=True
 assert 'error' in prepare()


def test_mcp_closed_schema_confirmation_and_no_credentials(env):
 async def check():
  async with Client(mcp) as client:
   schema=next(t.input_schema for t in await client.list_tools() if t.name=='prepare_quality_approval_decision')
   assert not schema['additionalProperties'] and not {'password','signer','employee','approving_for'}&schema['properties'].keys()
   r=await client.call_tool('prepare_quality_approval_decision',dict(action='approve',name='E',organization_name='Org',approval_sheet_id=SHEET,approval_entry_id=ENTRY,approval_decision_id=DECISION,comments='Reviewed'))
   ready=json.loads(r.content[0].text);assert ready.get('state')=='ready',ready
   calls=[dict(function=dict(name='execute_quality_transaction',arguments=json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})))]
   assert evaluate_mutations(MutationCounts(),calls).requires_approval and 'SignApproval' in plan_preview(calls)
 asyncio.run(check())


def test_native_decision_codes_differ_from_sheet_status_and_rejection_can_wait():
 d=json.loads(Path('docs/quality-fixtures/approval-decision-native.json').read_text(encoding='utf8'))
 enum=next(x['result'] for x in d['metadata']['results'] if x['query']['name']=='DecisionTypeEnum')
 values={f['name']:f['DefaultValue'] for f in enum['fields'] if f.get('DefaultValue')}
 assert values['Approved']=='10' and values['Rejected']=='20' and values['RoutingCanceled']=='30'
 e=json.loads(Path('docs/quality-fixtures/approval-decision-native-effects.json').read_text(encoding='utf8'))
 vals=[p['value'] for c in e['ApprovalSheet_AutoRouteDetails']['calls'] for p in c['parameters']]
 assert 'CLF::NextLevel == 0' in vals and 'Reject' in vals


@pytest.mark.parametrize('text,write',[('请批准这个审批条目',True),('拒绝这条审批',True),('请帮我签批',True),('谁可以签批',False),('查询批准和拒绝的决策码',False)])
def test_decision_questions_select_correct_tools_without_authorizing_by_query(text,write):
 from core.tool_catalog import TOOL_POLICIES
 from agent.tool_routing import select_tools,ROUTING_INSTRUCTION
 chosen={x['function']['name'] for x in select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[dict(role='user',content=text)])}
 assert 'prepare_quality_approval_decision' in chosen and ('execute_quality_transaction' in chosen) is write
 assert 'Rejected不保证表立即拒绝' in ROUTING_INSTRUCTION
