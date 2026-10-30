import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import mcp, quality_approval_assignment as c, quality_plans as p
from tests.test_quality_approval_actions import env as actions_env, approval_env, base_env, EVENT, ORG, OWNER, ROLE, OTHER, SHEET, ENTRY, LIST, DECISION, execute, snap
from tests.test_quality_event_update import schema_fixture

TEMPLATE='0000000000006001'
MAPPING='0000000000006002'
REJECT='0000000000006003'
RESOLUTION='0000000000006004'


def result(x):return json.loads(asyncio.run(x))
def prepare(**kw):return result(c.prepare_quality_approval_assignment('E','Org','Completion','QRC','Reviewed completed corrective work','Submit approval',**kw))


@pytest.fixture
def env(actions_env,monkeypatch):
    s=actions_env;s['sheets']=[];s['inquiry'].update(CompletionApprovalSheet=None,CompletionApprovalStatus=None,EntryForApproval=None)
    s['decisions'][0]['DecisionType']=10
    reject=copy.deepcopy(s['decisions'][0]);reject.update(ApprovalDecisionId=REJECT,ApprovalDecisionName='Reject',DecisionType=20);s['decisions'].append(reject)
    row=dict(ApprovalSheetTemplateId=TEMPLATE,ApprovalSheetTemplateName='Completion',Description='Completion sheet',GeneralInstructions='Review corrective actions',
        ApprovalRequired=True,ApprovalDecisionListId=LIST,FromStage=0,ToStage=0,IsFrozen=False,ChangeCount=1)
    entry=dict.fromkeys(c.SCHEMA['ApprovalSheetEntry'].split(','));entry.update(ApprovalSheetEntryId=ENTRY,ParentId=TEMPLATE,Status=0,SheetLevel=1,
        ApproverId=OWNER,ApproverRoleId=ROLE,AssignedApproverId=OWNER,SubstituteOption=1,EntryRequired=True,IsFrozen=False,ChangeCount=1,
        DecisionType=0,CompleteWithinQty=24,CompleteWithinUOM=1,EditOption=0,SpecialInstructions='Check evidence')
    mapping=dict(ApprovalSheetMapId=MAPPING,ParentId=ORG,ResolutionAction=1,ApprovalRequired=True,ApprovalSheetTemplateId=TEMPLATE,IsFrozen=False,ChangeCount=1)
    s.update(templates=[row],template_entries=[entry],approval_maps=[mapping],assignment_calls=[],assignment_schema_bad=False,
        close_options=[dict(Name='Close',Label='Close',Value=1)],resolution=dict(entity='QualityResolutionCodes',InstanceID=RESOLUTION,Name='QRC',IsFrozen=False,ChangeCount=1))
    original=c.q.sql_rows
    async def sql(query,maximum):
        if ' FROM ApprovalSheetTemplate WHERE ' in query:return copy.deepcopy(s['templates'])
        if ' FROM ApprovalSheetMap WHERE ' in query:return copy.deepcopy(s['approval_maps'])
        if ' FROM ApprovalSheetEntry WHERE ' in query and TEMPLATE in query:return copy.deepcopy(s['template_entries'])
        return await original(query,maximum)
    monkeypatch.setattr(c.q,'sql_rows',AsyncMock(side_effect=sql))
    original_asset=c.t.asset
    async def asset(kind,**kw):
        if kind=='quality_resolution':return copy.deepcopy(s['resolution'])
        return await original_asset(kind,**kw)
    monkeypatch.setattr(c.t,'asset',AsyncMock(side_effect=asset))
    monkeypatch.setattr(c.q,'closure_options',AsyncMock(side_effect=lambda *x:copy.deepcopy(s['close_options'])))
    async def request(method,path,**kw):
        s['assignment_calls'].append((method,path,copy.deepcopy(kw)))
        assert method=='GET' and path=='/swagger/AssignApprovalSheet/swagger.json' # Never a native preview.
        schemas=copy.deepcopy(schema_fixture()['components']['schemas'])
        props={k:{'$ref':'#/components/schemas/'+v} for k,v in dict(qualityObject='NamedObjectRef',organization='NamedObjectRef',
            approvalSheetTemplate='NamedObjectRef',approvalSheetDetails='ApprovalSheetDetails',qualityResolutionCode='NamedObjectRef').items()}
        props.update(resolutionAction={'type':'integer'},closeDescription={'type':'string'},comments={'type':'string'})
        if s['assignment_schema_bad']:props['approvalSheetTemplate']['$ref']='#/components/schemas/RevisionedObjectRef'
        schemas['AssignApprovalSheet']={'properties':props}
        return json.dumps({'components':{'schemas':schemas}})
    monkeypatch.setattr(c,'request',AsyncMock(side_effect=request))
    return s


def test_configured_completion_template_assigns_and_routes_without_native_preview(env):
    ready=prepare();assert ready.get('state')=='ready',ready
    before=snap(ready);body=before['body'];preview=ready['server_context']
    assert body['approvalSheetTemplate']['instanceId']==TEMPLATE and body['resolutionAction']==1
    detail=body['approvalSheetDetails'];assert detail['name']=='FromCompleted' and detail['executeAction']==1
    assert detail['approvalEntries'][0]['approvalEntry'] is None # Never reuse template entry identity.
    assert detail['approvalEntries'][0]['approver']['instanceId']==OWNER
    assert not preview['native_preview_requested'] and not preview['actor_permission_verified']
    assert all(method=='GET' for method,_,_ in env['assignment_calls']);env['client'].request.assert_not_awaited()
    assert execute(ready)['state']=='unknown';env['client'].request.assert_awaited_once()
    call=env['client'].request.await_args
    assert call.args[1].endswith('/api/AssignApprovalSheet') and call.kwargs['params']['execute']=='true'
    assert 'Employee' not in call.kwargs['params']['$select'] and 'ApprovalSheetDetails' not in call.kwargs['params']['$select']
    assert 'error' in execute(ready)


@pytest.mark.parametrize('mode',['existing_sheet','not_active','pending','process','event_data','inquiry_sheet'])
def test_first_assignment_never_reuses_or_clears_existing_sheet(env,mode,monkeypatch):
    if mode=='existing_sheet':env['sheets']=[dict.fromkeys(c.a.SCHEMA['ApprovalSheet'].split(','))]
    elif mode=='not_active':env['event']['Status']=8;env['inquiry']['Status']=8
    elif mode=='inquiry_sheet':env['inquiry']['CompletionApprovalSheet']={'InstanceId':SHEET}
    else:
        raw=copy.deepcopy(result_async(c.u.event_record('E',EVENT)))
        raw[{'pending':'IsSubmitted','process':'ProcessModelId','event_data':'EventDataId'}[mode]]=False if mode=='pending' else OTHER
        monkeypatch.setattr(c.u,'event_record',AsyncMock(return_value=raw))
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


def result_async(x):return asyncio.run(x)


@pytest.mark.parametrize('mode',['wrong_template','not_required','frozen','duplicate','wrong_org','no_map'])
def test_actual_organization_mapping_cannot_be_overridden(env,mode):
    if mode=='wrong_template':env['approval_maps'][0]['ApprovalSheetTemplateId']=OTHER
    elif mode=='not_required':env['approval_maps'][0]['ApprovalRequired']=False
    elif mode=='frozen':env['approval_maps'][0]['IsFrozen']=True
    elif mode=='duplicate':env['approval_maps']*=2
    elif mode=='wrong_org':env['approval_maps'][0]['ParentId']=OTHER
    else:env['approval_maps']=[]
    assert 'error' in prepare()


@pytest.mark.parametrize('mode',['frozen','not_required','wrong_name','duplicate','missing_approve','no_entries','too_many_entries','wrong_parent','substitute','delegation','plm','signed','duration','actor','comments'])
def test_template_must_be_complete_valid_and_named_approver_only(env,mode):
    if mode=='frozen':env['templates'][0]['IsFrozen']=True
    elif mode=='not_required':env['templates'][0]['ApprovalRequired']=False
    elif mode=='wrong_name':env['templates'][0]['ApprovalSheetTemplateName']='Other'
    elif mode=='duplicate':env['templates']*=2
    elif mode=='missing_approve':env['decisions'][0]['DecisionType']=30
    elif mode=='no_entries':env['template_entries']=[]
    elif mode=='too_many_entries':env['template_entries']*=101
    else:
        field,value={'wrong_parent':('ParentId',OTHER),'substitute':('SubstituteOption',2),'delegation':('DelegationTaskId',OTHER),
            'plm':('PLMApprovalURL','https://example.test/plm'),'signed':('LastDecisionId',OTHER),'duration':('CompleteWithinUOM',True),
            'actor':('ApproverId',None),'comments':('Comments','Do not clear')}[mode]
        env['template_entries'][0][field]=value
    assert 'error' in prepare();env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('change',['template','entry','decision','mapping','policy','resolution'])
def test_late_configuration_change_stops_actual_assignment(env,change):
    ready=prepare();assert ready.get('state')=='ready',ready
    if change=='template':env['templates'][0]['ChangeCount']+=1
    elif change=='entry':env['template_entries'][0]['ChangeCount']+=1
    elif change=='decision':env['decisions'][0]['ChangeCount']+=1
    elif change=='mapping':env['approval_maps'][0]['ChangeCount']+=1
    elif change=='policy':env['policy']['smtp_configured']=True
    else:env['resolution']['ChangeCount']+=1
    assert 'error' in execute(ready);env['client'].request.assert_not_awaited()


def test_signature_notification_and_contract_gates(env):
    env['roots']['Organizations']['QualityESigTxns']=[7852];assert 'error' in prepare()
    env['roots']['Organizations']['QualityESigTxns']=[];env['assignment_schema_bad']=True;assert 'error' in prepare()
    env['assignment_schema_bad']=False;env['policy'].update(smtp_configured=True,entries=[{'notification_type':1}])
    assert 'error' in prepare();assert prepare(allow_notifications=True).get('state')=='ready'


@pytest.mark.parametrize('field,value',[('approvalSheet',{'instanceId':SHEET}),('ignoreProcessModel',True),('employee',{'instanceId':OTHER}),('password','SECRET')])
def test_assignment_body_cannot_accept_overrides(env,field,value):
    saved=snap(prepare());saved['body'][field]=value
    with pytest.raises(ValueError):p.transport(saved)


def test_unknown_assignment_blocks_other_plans_for_original_object(env):
    first=prepare();second=prepare();execute(first);assert 'error' in execute(second)
    saved=snap(first)
    assert p.conflicts(saved,dict(kind='event_close',scope_key='other',environment=p.environment(),target_id=EVENT,body={},before={'event':{'OrganizationId':ORG}},resource_writes=['runtime:quality-event:id:'+EVENT]))
    out=result(p.inspect_quality_transaction(first['plan_id'],first['plan_digest']))
    assert out['original_object_id']==EVENT and not out['verified_success'] and not out['retry_allowed']


def test_actual_mcp_template_and_prepare_schemas_are_closed(env):
    async def check():
        async with Client(mcp) as client:
            for tool in await client.list_tools():
                if tool.name in ('get_quality_approval_template','prepare_quality_approval_assignment'):
                    assert not tool.input_schema['additionalProperties']
                    assert not {'password','employee','signature','approval_entries'}&tool.input_schema['properties'].keys()
            reply=await client.call_tool('get_quality_approval_template',dict(name='Completion'))
            assert json.loads(reply.content[0].text)['complete']
    asyncio.run(check());assert not p.PLAN_DB.exists()


def test_first_capa_completion_assignment_uses_original_capa_id(env):
    from tests.test_quality_approvals import CAPA
    env['capa'].update(IsSubmitted=True,ProcessModelId=None);env['inquiry']['Category']=5
    ready=prepare(object_kind='capa');assert ready.get('state')=='ready',ready
    assert ready['request_preview']['body']['qualityObject']['instanceId']==CAPA
