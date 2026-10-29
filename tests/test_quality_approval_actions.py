import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from tools import mcp,quality_approval_actions as c,quality_plans as p,quality_event_triage as t
from tests.test_quality_approvals import env as approval_env,base_env,EVENT,ORG,OWNER,ROLE,OTHER,SHEET,ENTRY,LIST,DECISION
from agent.safety import evaluate_mutations,MutationCounts,plan_preview

def result(x):return json.loads(asyncio.run(x))
def prepare(action='route',**kw):return result(c.prepare_quality_approval_action(action,'E','Org',SHEET,'Reviewed approval action',**kw))
def execute(r):return result(p.execute_quality_transaction(r['plan_id'],r['plan_digest']))
def snap(r):return result(p.get_quality_transaction_plan(r['plan_id']))['snapshot']

@pytest.fixture
def env(approval_env,monkeypatch):
 s=approval_env;s['event']['IsSubmitted']=True
 s['sheets'][0].update(Status=0,LastApprovalCycle=None,LastApprovalCycleGMT=None)
 s['entries'][0]['Status']=0;s['inquiry']['CompletionApprovalStatus']=0;s['inquiry']['EntryForApproval']=None
 s.update(policy=dict(smtp_configured=False,entries=[]),hook=None,contract_bad=False,approval_requests=[])
 monkeypatch.setattr(c.creation,'notification_policy',AsyncMock(side_effect=lambda *x:copy.deepcopy(s['policy'])))
 raw=copy.deepcopy(s['event']);raw.update(EventDataId=None)
 monkeypatch.setattr(c.u,'event_record',AsyncMock(side_effect=lambda *x:copy.deepcopy(raw)))
 async def transport(method,path,**kw):
  s['approval_requests'].append((method,path,copy.deepcopy(kw)))
  service=path.split('/')[2] if method=='GET' else path.split('/')[-1]
  if method=='GET':
   # Deliberate test schema from native reference types, not a live capture.
   props={'qualityObject':{'$ref':'#/components/schemas/NamedObjectRef'},'organization':{'$ref':'#/components/schemas/NamedObjectRef'},'approvalSheet':{'$ref':'#/components/schemas/NamedSubentityRef'}}
   if s['contract_bad']:props['approvalSheet']['$ref']='#/components/schemas/NamedObjectRef'
   return json.dumps({'components':{'schemas':{service:{'properties':props}}}})
  assert path in ('/api/RouteApprovalSheet','/api/CancelApprovalSheet') and kw['params']['execute']=='false'
  from tests.test_quality_event_triage import echo
  data=echo(copy.deepcopy(kw['body']));data.update(AvailableActions=257,IgnoreProcessModel=False,ESigRequirement=None,AdditionalESigReqs=[])
  row=s['event'];data['QualityObjectDetail']=dict(Status=row['Status'],Category=row['Category'],Organization={'InstanceId':ORG},Owner={'InstanceId':OWNER},Role={'InstanceId':ROLE},UserIsOwner=True,UserHasRole=True,ProcessModel=None)
  if s['hook']:s['hook'](data)
  return json.dumps({'data':data})
 monkeypatch.setattr(c,'request',AsyncMock(side_effect=transport))
 return s

def routed(s):
 s['sheets'][0].update(Status=10,LastApprovalCycleGMT='2026-10-29T01:00:00Z');s['entries'][0]['Status']=10
 s['inquiry'].update(CompletionApprovalStatus=10,EntryForApproval={'InstanceId':ENTRY})

@pytest.mark.parametrize('action',['route','cancel'])
def test_native_sheet_operation_is_prepared_and_submitted_once(env,action):
 if action=='cancel':routed(env)
 ready=prepare(action);assert ready.get('state')=='ready',ready
 body=ready['request_preview']['body'];assert body['approvalSheet']['instanceId']==SHEET and body['approvalSheet']['parent']['instanceId']==EVENT
 assert not ready['server_context']['approval_action_verified']
 assert execute(ready)['state']=='unknown';env['client'].request.assert_awaited_once()
 expected='RouteApprovalSheet' if action=='route' else 'CancelApprovalSheet'
 assert env['client'].request.await_args.args[1].endswith('/api/'+expected)
 assert 'error' in execute(ready)
 assert all(path not in ('/api/RouteApproval','/api/SignApproval','/api/SignatureApproval') for _,path,_ in env['approval_requests'])

@pytest.mark.parametrize('field,value',[('Status',40),('LastApprovalCycleGMT','old'),('LastApprovalCycle','old'),('IsFrozen',True)])
def test_route_cannot_reset_old_or_frozen_sheet(env,field,value):
 env['sheets'][0][field]=value;assert 'error' in prepare();env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('field,value',[('DecisionType',1),('ApprovalDecisionId',DECISION),('ApprovedById',OWNER),('LastDecisionId',OTHER),('LastCompletedOnGMT','old'),('Status',40),('IsFrozen',True),('ApproverId',None)])
def test_existing_decisions_or_unknown_approvers_are_not_erased(env,field,value):
 env['entries'][0][field]=value;assert 'error' in prepare()


def test_cancel_cannot_undo_signed_decision(env):
 routed(env);env['entries'][0]['DecisionType']=1
 assert 'error' in prepare('cancel')

@pytest.mark.parametrize('action,typ',[('route',7838),('cancel',7837)])
def test_organization_signature_requirements_are_not_bypassed(env,action,typ):
 if action=='cancel':routed(env)
 env['roots']['Organizations']['QualityESigTxns']=[typ]
 assert 'error' in prepare(action)

@pytest.mark.parametrize('field,value',[('IgnoreProcessModel',True),('ESigRequirement',{}),('AdditionalESigReqs',[{}]),('ApprovalSheet',{'InstanceId':OTHER})])
def test_original_sheet_echo_and_signature_context_required(env,field,value):
 env['hook']=lambda d:d.update({field:value});assert 'error' in prepare()

@pytest.mark.parametrize('field,value',[('UserIsOwner',False),('UserHasRole',False),('Owner',{'InstanceId':OTHER}),('Status',8),('ProcessModel',{})])
def test_native_parent_and_actor_context_must_match(env,field,value):
 env['hook']=lambda d:d['QualityObjectDetail'].update({field:value});assert 'error' in prepare()


def test_cancel_requires_native_cancel_bit(env):
 routed(env);env['hook']=lambda d:d.update(AvailableActions=1)
 assert 'error' in prepare('cancel')


def test_notifications_need_explicit_allow_and_are_bound(env):
 env['policy'].update(smtp_configured=True,entries=[{'notification_type':1}])
 assert 'error' in prepare()
 ready=prepare(allow_notifications=True);assert ready.get('state')=='ready',ready
 env['policy']['entries'].append({'notification_type':2})
 assert 'error' in execute(ready);env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('change',['cycle','entry','decision','sheet_state'])
def test_changed_approval_cycle_or_entry_stops_business_write(env,change):
 ready=prepare();assert ready.get('state')=='ready',ready
 if change=='cycle':env['sheets'][0]['LastApprovalCycleGMT']='new'
 elif change=='entry':env['entries'][0]['ChangeCount']=2
 elif change=='decision':env['decisions'][0]['ChangeCount']=2
 else:env['sheets'][0]['Status']=10
 assert 'error' in execute(ready);env['client'].request.assert_not_awaited()

@pytest.mark.parametrize('mode',['timeout','400','cancel'])
def test_unknown_never_replays_or_unblocks_parent(env,mode):
 ready=prepare()
 if mode=='400':env['client'].request.return_value.status_code=400
 else:env['client'].request.side_effect=TimeoutError('unknown') if mode=='timeout' else asyncio.CancelledError()
 if mode=='cancel':
  with pytest.raises(asyncio.CancelledError):execute(ready)
 else:assert execute(ready)['state']=='unknown'
 assert 'error' in execute(ready);env['client'].request.assert_awaited_once()
 saved=snap(ready)
 other=dict(kind='event_close',scope_key='other',target_id=EVENT,environment=p.environment(),body={},before={'event':{'OrganizationId':ORG}},resource_writes=['runtime:quality-event:id:'+EVENT])
 assert p.conflicts(saved,other)

@pytest.mark.parametrize('field,value',[('approvalSheet',{'name':'Other','instanceId':OTHER}),('ignoreProcessModel',True),('password','SECRET'),('qualityObject',{'instanceId':OTHER})])
def test_transport_forbids_arbitrary_body_overrides(env,field,value):
 saved=snap(prepare());saved['body'][field]=value
 with pytest.raises(ValueError):p.transport(saved)


def test_inspection_is_original_sheet_evidence_not_pending_equals_success(env):
 ready=prepare();execute(ready);env['sheets'][0]['Status']=10
 out=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
 assert out['original_sheet_id']==SHEET and not out['verified_success'] and not out['retry_allowed']


def test_runtime_contract_is_not_guessed(env):
 env['contract_bad']=True;assert 'error' in prepare()


def test_actual_mcp_closed_schema_and_confirmation(env):
 async def check():
  async with Client(mcp) as client:
   schema=next(t.input_schema for t in await client.list_tools() if t.name=='prepare_quality_approval_action')
   assert not schema['additionalProperties'] and not {'password','signature','decision'}&schema['properties'].keys()
   reply=await client.call_tool('prepare_quality_approval_action',dict(action='route',name='E',organization_name='Org',approval_sheet_id=SHEET,comments='Reviewed'))
   ready=json.loads(reply.content[0].text);assert ready.get('state')=='ready',ready
   calls=[dict(function=dict(name='execute_quality_transaction',arguments=json.dumps({k:ready[k] for k in ('plan_id','plan_digest')})))]
   assert evaluate_mutations(MutationCounts(),calls).requires_approval and 'RouteApprovalSheet' in plan_preview(calls)
 asyncio.run(check())


def test_native_cancel_result_is_pending_not_cancelled20():
 d=json.loads(Path('docs/quality-fixtures/approval-sheet-actions-native-flow.json').read_text(encoding='utf8'))
 values=[p['value'] for c in d['ApprovalSheet_Cancel']['calls'] for p in c['parameters']]
 assert 'Transaction::__Const.ApprovalStatus.Pending' in values and 'Transaction::__Const.DecisionType.RoutingCanceled' in values


@pytest.mark.parametrize('text,write',[('查看路由审批条件',False),('请路由审批表',True),('请取消审批',True),('查询审批表允许的决策',False)])
def test_approval_action_routing_does_not_mean_permission(text,write):
 from core.tool_catalog import TOOL_POLICIES
 from agent.tool_routing import select_tools,ROUTING_INSTRUCTION
 chosen={x['function']['name'] for x in select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[dict(role='user',content=text)])}
 assert 'prepare_quality_approval_action' in chosen and ('execute_quality_transaction' in chosen) is write
 assert '不是拒绝/签批/退回决策入口' in ROUTING_INSTRUCTION


def test_cancel_bit_does_not_enable_same_sheet_after_unknown(env):
 routed(env);first=prepare('cancel');second=prepare('cancel');assert execute(first)['state']=='unknown'
 assert 'error' in execute(second);env['client'].request.assert_awaited_once()


def test_existing_approver_ids_and_membership_are_not_claimed_verified(env):
 ready=prepare();assert not ready['dynamic_validation_verified']
 assert ready['server_context']['native_actor_context']['UserIsOwner'] is True
 env['client'].request.return_value.status_code=400
 out=execute(ready);assert out['state']=='unknown' and not out['verified_success']



def test_preview_never_persists_extra_fields_on_employee_reference(env):
 env['hook']=lambda d:d['QualityObjectDetail']['Owner'].update(Password='NEVER_STORE_REFERENCE_SECRET')
 ready=prepare();assert ready.get('state')=='ready',ready
 assert 'NEVER_STORE_REFERENCE_SECRET' not in json.dumps(ready) and 'NEVER_STORE_REFERENCE_SECRET' not in json.dumps(snap(ready))
