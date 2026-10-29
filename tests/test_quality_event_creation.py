import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from tools import mcp,get_tool_func,quality_event_creation as c,quality_plans as p
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools

ORG,CLASS,SUB,PRIORITY,REPORTER,MAP,SMTP,NOTICE=['%016x'%x for x in range(1,9)]

def run(**kw):
    args=dict(event_name='E',organization_name='Org',classification_name='Class',subclassification_name='Sub',priority_level_name='Normal',
        brief_description='Investigation',reporter_name='Reporter',occurrence_date='2026-10-29T09:00:00.1234567+08:00',reported_date='2026-10-29T01:01:00Z')
    args.update(kw);return json.loads(asyncio.run(c.validate_quality_event_creation(**args)))

@pytest.fixture
def env(monkeypatch,tmp_path):
    def state(name,key):return dict(Name=name,InstanceID=key,IsFrozen=False,ChangeCount=1)
    states=dict(organization=state('Org',ORG),classification=state('Class',CLASS),subclassification=state('Sub',SUB),priority=state('Normal',PRIORITY))
    mapped=dict(InstanceID=MAP,IsFrozen=False,Classification={'InstanceId':CLASS},Subclassification={'InstanceId':SUB},
        Owner=None,Role=None,ChecklistTemplate=None,FailureModeGroup=None)
    states['organization'].update(CollectESigForAllQualityTxns=False,QualityESigTxns=[],EventClassificationSpecMaps=[mapped])
    reporter=state('Reporter',REPORTER)|{'entity':'Employees'}
    notification=dict(InstanceID=ORG,Name='Org',SMTPTransport={'InstanceId':SMTP,'Password':'DO_NOT_STORE'},
        NotificationEvents=[dict(InstanceID=NOTICE,NotificationType=1,AssigneeOnly=True)])
    data=dict(states=states,reporter=reporter,notification=notification,existing=[],reads=[],hook=None)
    monkeypatch.setattr(c,'schema_check',AsyncMock())
    async def named(kind,name,instance_id=None):return copy.deepcopy(states[kind])
    monkeypatch.setattr(c.n,'named_state',AsyncMock(side_effect=named))
    monkeypatch.setattr(c.t,'asset',AsyncMock(side_effect=lambda *a,**k:copy.deepcopy(data['reporter'])))
    async def sql(query,maximum):
        assert query.startswith('SELECT TOP 2 EventId,EventName,IsSubmitted,Status,ChangeCount FROM Event WHERE EventName=') and maximum==2
        return copy.deepcopy(data['existing'])
    monkeypatch.setattr(c.q,'sql_rows',AsyncMock(side_effect=sql))
    class Context:
        def __init__(self,*a):pass
        async def get(self,entity,key,params):
            assert entity=='Organizations' and key==ORG
            assert 'Password' not in params['$select'] and 'SMTPTransport(' not in params['$expand']
            data['reads'].append(copy.deepcopy(params))
            if data['hook']:data['hook']()
            return copy.deepcopy(data['notification'])
    monkeypatch.setattr(c,'ReadContext',Context)
    monkeypatch.setattr(p,'PLAN_DB',tmp_path/'plans.sqlite3')
    # Any accidental call to a native business service is an immediate failure.
    monkeypatch.setattr(c.q,'request',AsyncMock(side_effect=AssertionError('No shopfloor request is permitted')))
    monkeypatch.setattr(p,'request',AsyncMock(side_effect=AssertionError('No preview or submit is permitted')))
    return data


def test_static_success_is_not_a_plan_and_never_calls_creation_or_notifications(env):
    out=run();assert out['complete'] and out['static_valid'] and not out['ready_for_execution']
    assert not out['native_preview_requested'] and not out['created_event_verified'] and 'plan_id' not in out
    assert not p.PLAN_DB.exists() and len(env['reads'])==2
    assert out['notification_policy']['smtp_configured'] and len(out['validation_digest'])==64
    assert 'DO_NOT_STORE' not in json.dumps(out)
    c.q.request.assert_not_awaited();p.request.assert_not_awaited()

@pytest.mark.parametrize('field,value',[('event_name',''),('event_name','x'*31),('event_name','a\nname'),
    ('brief_description',' '),('brief_description','😀'*51),('description','x'*4001),('description','bad\x00text'),
    ('discovery_area','x'*31),('occurrence_date','2026-10-29T09:00:00'),('reported_date','2026-10-29T00:00:00Z'),
    ('reported_date','2026-10-29T01:00:00.1234566Z')])
def test_invalid_input_fails_before_any_read(env,field,value):
    out=run(**{field:value});assert not out['complete'] and not out['ready_for_execution']
    c.schema_check.assert_not_awaited();c.n.named_state.assert_not_awaited()

@pytest.mark.parametrize('submitted',[False,True])
def test_any_existing_name_including_draft_is_not_overwritten(env,submitted):
    env['existing']=[dict(EventId=REPORTER,EventName='E',IsSubmitted=submitted,Status=2,ChangeCount=1)]
    out=run();assert out['complete'] and not out['static_valid'] and out['errors'][0]['code']=='name_occupied'
    assert not out['ready_for_execution']

@pytest.mark.parametrize('mode',['missing','duplicate','frozen','owner_default'])
def test_mapping_is_exact_and_does_not_claim_defaults_were_applied(env,mode):
    org=env['states']['organization'];maps=org['EventClassificationSpecMaps']
    if mode=='missing':org['EventClassificationSpecMaps']=[]
    elif mode=='duplicate':maps.append(copy.deepcopy(maps[0]));maps[1]['InstanceID']=NOTICE
    elif mode=='frozen':maps[0]['IsFrozen']=True
    else:maps[0]['Owner']={'InstanceId':REPORTER,'Name':'Assigned'}
    out=run();assert out['complete']
    if mode=='owner_default':assert out['classification_mapping']['Owner']==REPORTER and not out['ready_for_execution']
    else:assert not out['static_valid']

@pytest.mark.parametrize('kind',['organization','classification','subclassification','priority'])
def test_frozen_references_are_reported(env,kind):
    env['states'][kind]['IsFrozen']=True
    out=run();assert out['complete'] and not out['static_valid'] and 'frozen_'+kind in {x['code'] for x in out['errors']}

@pytest.mark.parametrize('values,expected',[([],False),([7491],True),([7712],False)])
def test_signature_type_is_specific_to_create(env,values,expected):
    env['states']['organization']['QualityESigTxns']=values
    out=run();assert out['complete'] and out['signature_required'] is expected and not out['ready_for_execution']

@pytest.mark.parametrize('values',[None,[7491,7491],[True],[-1]])
def test_unknown_signature_policy_is_not_complete(env,values):
    env['states']['organization']['QualityESigTxns']=values
    assert not run()['complete']

@pytest.mark.parametrize('mode',['missing_field','pagination','too_many','duplicate','smtp_invalid'])
def test_notification_policy_is_bounded_and_safe(env,mode):
    data=env['notification']
    if mode=='missing_field':data.pop('SMTPTransport')
    elif mode=='pagination':data['@odata.nextLink']='next'
    elif mode=='too_many':data['NotificationEvents']*=101
    elif mode=='duplicate':data['NotificationEvents']*=2
    else:data['SMTPTransport']={'InstanceId':'invalid'}
    assert not run()['complete']


def test_no_smtp_or_notifications_is_still_not_execution_permission(env):
    env['notification'].update(SMTPTransport=None,NotificationEvents=[])
    out=run();assert out['complete'] and not out['notification_policy']['smtp_configured'] and not out['ready_for_execution']


def test_changes_during_read_are_rejected(env):
    count=[0]
    def changed():
        count[0]+=1
        if count[0]>=2:env['notification']['NotificationEvents'][0]['AssigneeOnly']=False
    env['hook']=changed
    out=run();assert not out['complete'] and '变化' in out['error']


def test_native_draft_has_assignment_mail_and_execute_flag_evidence():
    data=json.loads(Path('docs/quality-fixtures/event-create-native-execution.json').read_text(encoding='utf8'))
    values=[p['value'] for c in data['CreateEvent_FinishModifyEntity']['calls'] for p in c['parameters']]
    assert 'GetOwnerRole' in values and 'SendEventEMail' in values
    values=[p['value'] for c in data['CreateEvent_PreValidate']['calls'] for p in c['parameters']]
    assert 'Transaction::IsExecute' in values


def test_real_mcp_schema_and_readonly_routing(env):
    async def check():
        assert get_tool_func('validate_quality_event_creation') is c.validate_quality_event_creation
        async with Client(mcp) as client:
            schema=next(t.input_schema for t in await client.list_tools() if t.name=='validate_quality_event_creation')
            assert not schema['additionalProperties'] and not {'submit','owner','ignoreProcessModel','password'}&schema['properties'].keys()
            reply=await client.call_tool('validate_quality_event_creation',dict(event_name='E',organization_name='Org',classification_name='Class',
                subclassification_name='Sub',priority_level_name='Normal',brief_description='Issue',reporter_name='Reporter',
                occurrence_date='2026-10-29T01:00:00Z',reported_date='2026-10-29T02:00:00Z'))
            assert json.loads(reply.content[0].text)['complete']
    asyncio.run(check())
    chosen={x['function']['name'] for x in select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[dict(role='user',content='查看质量事件创建前置条件')])}
    assert 'validate_quality_event_creation' in chosen and 'execute_quality_transaction' not in chosen


@pytest.mark.parametrize('turns,write',[
    (['查看质量事件创建前置条件'],False),(['核验事件创建资料'],False),
    (['检查事件新建参数'],False),(['查看质量事件创建前置条件','继续'],False),
    (['查看质量事件创建前置条件','查看报告人'],False),
    (['请创建质量事件E'],True),(['查看质量事件创建条件，然后提交'],True)])
def test_creation_queries_are_readonly_and_do_not_invent_a_plan(turns,write):
    from agent.tool_routing import ROUTING_INSTRUCTION
    chosen={x['function']['name'] for x in select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[dict(role='user',content=x) for x in turns])}
    assert 'validate_quality_event_creation' in chosen
    assert ('execute_quality_transaction' in chosen) is write
    assert '不得用CAPA、NCR升级或通用请求替代创建事件' in ROUTING_INSTRUCTION


def test_all_quality_signature_setting_is_reported_not_bypassed(env):
    env['states']['organization']['CollectESigForAllQualityTxns']=True
    out=run();assert out['signature_required'] and not out['ready_for_execution']
