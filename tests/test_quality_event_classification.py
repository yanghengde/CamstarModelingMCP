"""Existing-draft classification diagnostics and enforced submission boundaries."""
import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from pydantic import ValidationError
from tools import mcp,quality_event_classification as c,quality_event_update as u,quality_event_drafts as d,quality_event_submission as s,quality_plans as p
from tests.test_quality_event_drafts import env as draft_env,submission_env,update_env,triage_env,prepare as save,execute,snap,result
from tests.test_quality_event_submission import final_fixture_review
from tests.test_quality_event_triage import EVENT,ORG,OTHER
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools,ROUTING_INSTRUCTION
from tools.tool_discovery import get_capabilities

CLASS='000000000000b001';SUB='000000000000b002';ALT_CLASS='000000000000c001';ALT_SUB='000000000000c002';MAP='000000000000c003'


@pytest.fixture
def env(draft_env):
    return draft_env


def context(**kw):return result(u.get_quality_event_update_context('E','Org',purpose='classify_pending_draft',**kw))
def submit():return result(s.prepare_quality_event_submission('E','Org','Reviewed original classification'))


def alternate(env):
    env['classification_refs'][ALT_CLASS]=dict(entity='Classifications',InstanceID=ALT_CLASS,Name='Other Class',IsFrozen=False,ChangeCount=1)
    env['classification_refs'][ALT_SUB]=dict(entity='SubClassifications',InstanceID=ALT_SUB,Name='Other Sub',IsFrozen=False,ChangeCount=1)
    env['classification_maps'].append(env['classification_maps'][0]|dict(EventClassificationSpecMapId=MAP,ClassificationId=ALT_CLASS,SubClassificationId=ALT_SUB))


def test_valid_current_and_valid_proposed_pair_are_diagnostics_without_plan_or_reclassification(env):
    alternate(env);before=copy.deepcopy(env['classes'])
    out=context(classification_name='Other Class',subclassification_name='Other Sub')
    assert out['complete'] and out['classification_configuration']['current']['valid'],out
    proposed=out['classification_configuration']['proposal']
    assert proposed['assessment']['valid'] and proposed['references']['ClassificationId']['InstanceID']==ALT_CLASS
    assert not proposed['changes_applied'] and not out['ready_for_execution'] and not out['ready_for_submission']
    assert not out['classification_write_supported'] and not p.PLAN_DB.exists() and env['classes']==before
    assert not env['draft_posts'] and not env['submission_posts']


def test_missing_original_classification_can_only_be_preserved_and_proposal_cannot_repair_it(env):
    env['classes'].update(ClassificationId=None,SubClassificationId=None)
    out=context(classification_name='Class',subclassification_name='Sub')
    assert out['complete'] and not out['classification_configuration']['current']['valid'],out
    assert out['classification_configuration']['proposal']['assessment']['valid']
    assert 'error' in submit() and not env['submission_posts']
    ready=save();assert ready.get('state')=='ready',ready
    assert not {'classification','subClassification'}&ready['request_preview']['body'].keys()
    assert execute(ready)['state']=='unknown' and env['classes']['ClassificationId'] is None
    assert env['event']['Status']==2 and not env['event']['IsSubmitted']


def test_valid_pair_save_review_independent_submit_keeps_original_ids_and_configuration(env):
    alternate(env);original=copy.deepcopy(env['classes']);maps=copy.deepcopy(env['classification_maps'])
    out=context();assert out['classification_configuration']['current']['matching_map_count']==1
    ready=save();assert ready.get('state')=='ready',ready
    assert execute(ready)['state']=='unknown'
    formal=submit();assert formal.get('state')=='ready',formal
    assert 'error' in execute(formal) and not env['submission_posts']
    final_fixture_review(ready)
    assert execute(formal)['state']=='unknown' and len(env['submission_posts'])==1
    assert env['classes']['ClassificationId']==original['ClassificationId'] and env['classes']['SubClassificationId']==original['SubClassificationId']
    assert env['classification_maps']==maps and not {'classification','subClassification'}&snap(formal)['body'].keys()


@pytest.mark.parametrize('mode',['no_mapping','duplicate_pair','frozen_mapping','frozen_class','frozen_sub','wrong_pair'])
def test_invalid_or_out_of_scope_pair_is_displayed_and_formal_submission_is_blocked_before_load(env,mode):
    if mode=='no_mapping':env['classification_maps']=[]
    elif mode=='duplicate_pair':env['classification_maps'].append(env['classification_maps'][0]|dict(EventClassificationSpecMapId=MAP))
    elif mode=='frozen_mapping':env['classification_maps'][0]['IsFrozen']=True
    elif mode=='frozen_class':env['classification_refs'][CLASS]['IsFrozen']=True
    elif mode=='frozen_sub':env['classification_refs'][SUB]['IsFrozen']=True
    else:env['classification_maps'][0]['SubClassificationId']=OTHER
    out=context();assert out['complete'] and not out['classification_configuration']['current']['valid'],out
    count=env['load_count'];reply=submit()
    assert 'error' in reply and env['load_count']==count and not env['submission_posts']
    assert not p.PLAN_DB.exists()


@pytest.mark.parametrize('mode',['too_many','duplicate_id','wrong_org','wrong_type','missing_column','unknown_bool','negative_version','malformed_id','missing_ref','unknown_freeze'])
def test_partial_unknown_or_corrupt_configurations_do_not_produce_ready_plans(env,mode):
    if mode=='too_many':env['classification_maps']*=101
    elif mode=='duplicate_id':env['classification_maps']*=2
    elif mode=='wrong_org':env['classification_maps'][0]['OrganizationId']=OTHER
    elif mode=='wrong_type':env['classification_maps'][0]['CDOTypeId']=8002
    elif mode=='missing_column':env['classification_maps'][0].pop('PEDefault')
    elif mode=='unknown_bool':env['classification_maps'][0]['PEDefault']=None
    elif mode=='negative_version':env['classification_maps'][0]['ChangeCount']=-1
    elif mode=='malformed_id':env['classification_maps'][0]['ClassificationId']='bad'
    elif mode=='missing_ref':env['classification_refs'].pop(CLASS)
    else:env['classification_refs'][CLASS]['IsFrozen']=None
    assert not context()['complete'] and 'error' in submit() and not env['draft_posts'] and not env['submission_posts']


@pytest.mark.parametrize('field',['classification_name','subclassification_name'])
def test_proposal_requires_both_explicit_names_and_never_broadens_other_context_purposes(env,field):
    assert not context(**{field:'Class'})['complete']
    for purpose in ('update_text','edit_pending_draft','submit_pending'):
        out=result(u.get_quality_event_update_context('E','Org',purpose=purpose,**{field:'Class'}))
        assert not out['complete'] and 'classify_pending_draft' in out['error']
    assert not env['draft_posts'] and not env['submission_posts'] and not p.PLAN_DB.exists()


@pytest.mark.parametrize('mode',['missing_name','ambiguous_name','mismatched_pair','frozen_proposal'])
def test_proposed_pair_is_resolved_explicitly_without_choosing_first_or_changing_event(env,mode):
    alternate(env);names=dict(classification_name='Other Class',subclassification_name='Other Sub')
    if mode=='missing_name':names['classification_name']='Not configured'
    elif mode=='ambiguous_name':env['classification_refs'][OTHER]=env['classification_refs'][ALT_CLASS]|dict(InstanceID=OTHER)
    elif mode=='mismatched_pair':names['subclassification_name']='Sub'
    else:env['classification_refs'][ALT_CLASS]['IsFrozen']=True
    out=context(**names)
    if mode in ('missing_name','ambiguous_name'):assert not out['complete']
    else:assert out['complete'] and not out['classification_configuration']['proposal']['assessment']['valid']
    assert env['classes']['ClassificationId']==CLASS and not env['draft_posts'] and not env['submission_posts']


@pytest.mark.parametrize('stage',['load','submit_preview'])
@pytest.mark.parametrize('mode',['wrong_class','wrong_sub','missing_echo'])
def test_native_read_only_classification_echo_must_match_original_sql_not_requested_proposal(env,stage,mode):
    def hook(data):
        if stage=='submit_preview' and data.get('Submit') is not True:return
        if mode=='missing_echo':data.pop('Classification')
        else:data['Classification' if mode=='wrong_class' else 'SubClassification']={'InstanceId':OTHER}
    env['classification_hook']=hook
    assert 'error' in submit() and not env['submission_posts']
    if stage=='load':assert 'error' in save()


@pytest.mark.parametrize('mode',['mapping_version','mapping_defaults','class_version','sub_version','mapping_duplicate','wrong_native_echo'])
@pytest.mark.parametrize('kind',['save','submit'])
def test_ready_plan_rechecks_full_mapping_references_and_native_classification_before_business_write(env,kind,mode):
    ready=save() if kind=='save' else submit();assert ready.get('state')=='ready',ready
    if mode=='mapping_version':env['classification_maps'][0]['ChangeCount']+=1
    elif mode=='mapping_defaults':env['classification_maps'][0]['OwnerId']=OTHER
    elif mode=='class_version':env['classification_refs'][CLASS]['ChangeCount']+=1
    elif mode=='sub_version':env['classification_refs'][SUB]['ChangeCount']+=1
    elif mode=='mapping_duplicate':env['classification_maps'].append(env['classification_maps'][0]|dict(EventClassificationSpecMapId=MAP))
    else:env['classification_hook']=lambda data:data.update(Classification={'InstanceId':OTHER})
    assert 'error' in execute(ready) and not env['submission_posts'] and not env['draft_posts']


def test_mapping_changes_during_prepare_cannot_be_hidden_by_mixed_reads(env,monkeypatch):
    original=c.q.sql_rows;seen=[0]
    async def sql(query,maximum):
        rows=await original(query,maximum)
        if ' FROM EventClassificationSpecMap WHERE ' in query:
            seen[0]+=1
            if seen[0]==1:env['classification_maps'][0]['ChangeCount']+=1
        return rows
    monkeypatch.setattr(c.q,'sql_rows',AsyncMock(side_effect=sql))
    assert 'error' in submit() and not env['submission_posts'] and not p.PLAN_DB.exists()


def test_reference_resources_share_unknown_occupancy_and_missing_runtime_schema_is_rejected(env,monkeypatch):
    saved=snap(save());resources,_=p.resource_sets(saved)
    assert 'modeling:Classifications:id:'+CLASS in resources and 'modeling:SubClassifications:id:'+SUB in resources
    other=dict(kind='classification_config',scope_key='other',environment=p.environment(),body={},resource_writes=['modeling:Classifications:id:'+CLASS])
    assert p.conflicts(saved,other)
    original=c.q.sql_rows
    async def sql(query,maximum):
        rows=await original(query,maximum)
        if 'INFORMATION_SCHEMA.COLUMNS' in query:return [r for r in rows if not (r['TABLE_NAME']=='EventClassificationSpecMap' and r['COLUMN_NAME']=='OrganizationId')]
        return rows
    monkeypatch.setattr(c.q,'sql_rows',AsyncMock(side_effect=sql))
    assert not context()['complete'] and 'error' in submit()


def test_standard_evidence_does_not_turn_ref_properties_or_alternative_fields_into_write_support():
    doc=json.loads(Path('docs/quality-fixtures/event-classification-native-models.json').read_text(encoding='utf-8'))
    root=next(r['result'] for r in doc['results'] if r['result']['name']=='UpdateEvent')
    fields={r['name']:r for r in root['fields']}
    for field in ('Classification','SubClassification'):
        assert fields[field]['AllowClientToUpdate'] is False and fields[field]['CurrentValueExpression']=='QualityObject.'+field
    maps=json.loads(Path('docs/quality-fixtures/event-classification-native-maps.json').read_text(encoding='utf-8'))
    for name in ('EventDetailToEvent','UpdateEventDataToEvent'):
        key=next(r['CDOMapID'] for r in maps['maps'] if r['Name']==name)
        assert not any(r['TargetField'] in ('Classification','SubClassification') for r in maps['fields'] if r['CDOMapID']==key)
    assert c.write_support()['supported'] is False
    for name in ('classification_name','subclassification_name'):
        with pytest.raises(ValidationError):d.EventDraftChanges.model_validate({name:'Class'})


@pytest.mark.parametrize('turns,writes',[
    (['查看事件E草稿分类组合'],False),(['请修改事件E草稿分类'],False),(['把事件E草稿子分类改为Sub'],False),
    (['查看事件E草稿资料','请补齐分类和子分类'],False),(['查看事件E草稿分类','核实后正式提交事件E草稿'],True),
    (['请比较事件E草稿分类组合，不要保存'],False)])
def test_classification_questions_never_offer_a_nonexistent_write_adapter(turns,writes):
    tools=[dict(type='function',function=dict(name=n)) for n in TOOL_POLICIES]
    selected={r['function']['name'] for r in select_tools(tools,[dict(role='user',content=t) for t in turns])}
    assert 'get_quality_event_update_context' in selected and ('execute_quality_transaction' in selected) is writes
    assert 'classify_pending_draft' in ROUTING_INSTRUCTION and '分类修改能力尚未实现' in ROUTING_INSTRUCTION


def test_mcp_context_closed_schema_proposal_is_read_only_and_no_classification_write_tool_is_advertised(env):
    async def check():
        async with Client(mcp) as client:
            registered={t.name:t for t in await client.list_tools()}
            schema=registered['get_quality_event_update_context'].input_schema
            assert schema['additionalProperties'] is False and schema['properties']['purpose']['default']=='update_text'
            assert 'classify_pending_draft' in schema['properties']['purpose']['enum']
            assert {'classification_name','subclassification_name'}<=schema['properties'].keys()
            patch=registered['prepare_quality_event_draft_update'].input_schema['properties']['changes']['properties']
            assert not {'classification_name','subclassification_name'}&patch.keys()
            reply=await client.call_tool('get_quality_event_update_context',dict(event_name='E',organization_name='Org',purpose='classify_pending_draft',
                classification_name='Class',subclassification_name='Sub'))
            out=json.loads(reply.content[0].text);assert out['complete'] and not out['classification_write_supported'],out
    asyncio.run(check());assert not p.PLAN_DB.exists() and not env['draft_posts'] and not env['submission_posts']


def test_capabilities_and_inventory_disclose_native_write_dependency_without_new_tool_count():
    capability=result(get_capabilities(tool_name='get_quality_event_update_context'))
    assert 'classify_pending_draft' in json.dumps(capability) and '分类修改尚无已核实原生写入' in json.dumps(capability,ensure_ascii=False)
    assert '标准UpdateEvent这两字段只读' in json.dumps(result(c.q.get_quality_api_inventory()),ensure_ascii=False)
    assert len([x for x in TOOL_POLICIES.values() if x.group=='quality'])==82
