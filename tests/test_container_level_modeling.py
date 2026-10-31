"""Full ContainerLevel contract coverage; all writes are mocked/offline."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from agent.prompts import CONTAINER_LEVEL_RULE, SYSTEM_PROMPT
from agent.tool_routing import ROUTING_INSTRUCTION, select_tools
from agent.safety import classify_tool
from core.modeling_payload import _schema, build_update_payload
from core.tool_catalog import TOOL_POLICIES
from tools import container_levels as levels, get_tool_func
from tools.tool_discovery import get_capabilities, load_tool_group

CID = '0005288000000003'


def obj():
    return {'InstanceID':CID, 'Name':'BOX', 'IsFrozen':False, 'AllowMove':False,
            'Description':'old', 'Notes':'old', 'IsNameUnique':False, 'ParentLevels':[], 'ChildLevels':[],
            'ContainerNumberingRule':None, 'UIPreference':None, 'FilterTags':None, 'IconId':None}


def test_multiple_configuration_fields_and_reference_lists_verify_by_requested_values(monkeypatch):
    before = obj()
    after = {**before, 'Description':'new', 'Notes':None, 'IsNameUnique':True,
             'ContainerNumberingRule':{'Name':'SEQ','InstanceId':'0000000000000009'},
             'ParentLevels':[{'Name':'PALLET'}], 'ChildLevels':[{'Name':'B'},{'Name':'A'}],
             'UIPreference':{'Name':'UI'}, 'FilterTags':'tag', 'IconId':7}
    request = AsyncMock(side_effect=[json.dumps(before), '{"value":"updated"}', json.dumps(after)])
    monkeypatch.setattr(levels, 'request', request)
    result = json.loads(asyncio.run(levels.update_container_level(CID, expected_name='BOX',
        description='new', is_name_unique=True, container_numbering_rule_name='SEQ',
        parent_level_names=['PALLET'], child_level_names=['A','B'],
        body_json='{"Notes":null,"UIPreference":{"Name":"UI"},"FilterTags":"tag","IconId":7}')))
    assert result['verified'] and result['complete']
    body = request.await_args_list[1].kwargs['body']
    assert body == {'name':'BOX', 'description':'new', 'isNameUnique':True, 'containerNumberingRule':{'name':'SEQ'},
                    'parentLevels':[{'name':'PALLET'}], 'childLevels':[{'name':'A'},{'name':'B'}],
                    'notes':None, 'uiPreference':{'name':'UI'}, 'filterTags':'tag', 'iconId':7}
    assert 'allowMove' not in body
    assert request.await_args_list[0].kwargs['params']['$expand'] == 'ChildLevels,ContainerNumberingRule,ParentLevels,UIPreference'
    assert request.await_args_list[0].kwargs['params']['$select'] == '*'


def test_explicit_rename_verifies_new_name_but_expected_name_checks_old_target(monkeypatch):
    request = AsyncMock(side_effect=[json.dumps(obj()), '✅ PUT succeeded', json.dumps({**obj(),'Name':'NEW'})])
    monkeypatch.setattr(levels,'request',request)
    result=json.loads(asyncio.run(levels.update_container_level(CID, expected_name='BOX', name='NEW')))
    assert result['verified'] and request.await_args_list[1].kwargs['body'] == {'name':'NEW'}


def test_expanded_actions_and_control_arrays_are_sent_but_not_misreported_as_persistent_proof(monkeypatch):
    body={'childLevels-Expanded':[{'listItemAction':'add','value':{'name':'UNIT'}}], 'useROR':[False], 'replaceDetails':False}
    after={**obj(),'ChildLevels':[{'Name':'UNIT'}],'UseROR':[False],'ReplaceDetails':False}
    request=AsyncMock(side_effect=[json.dumps(obj()),'✅ PUT succeeded',json.dumps(after)])
    monkeypatch.setattr(levels,'request',request)
    result=json.loads(asyncio.run(levels.update_container_level(CID,body_json=json.dumps(body))))
    assert request.await_args_list[1].kwargs['body'] == {'name':'BOX',**body}
    assert result['submission_state']=='acknowledged' and not result['verified'] and not result['complete']
    assert all(value is None for value in result['field_checks'].values())


def sample(schema, definitions):
    if '$ref' in schema: schema=definitions[schema['$ref'].rsplit('/',1)[-1]]
    kind=schema['type']
    if kind=='boolean':return False
    if kind=='integer':return 0
    if kind=='number':return 1.5
    if kind=='string':return 'VALUE'
    if kind=='array':return [sample(schema['items'],definitions)]
    props=schema.get('properties',{})
    keys=schema.get('required',[])
    if not keys:keys=['name'] if 'name' in props else ['value'] if 'value' in props else []
    return {key:sample(props[key],definitions) for key in keys if not props[key].get('readOnly')}


def test_every_declared_writable_business_field_is_exposed_and_type_normalized():
    schema,definitions=_schema('ContainerLevels')
    inventory=json.loads(asyncio.run(levels.get_container_level_schema()))['fields']
    tested=set()
    for field, definition in schema['properties'].items():
        value=sample(definition,definitions)
        if definition.get('readOnly') or field=='objectToChange':
            assert not inventory[field]['write_supported']
            with pytest.raises(ValueError):build_update_payload('ContainerLevels',{},json.dumps({field:value}))
        else:
            assert inventory[field]['write_supported']
            assert build_update_payload('ContainerLevels',{},json.dumps({field:value})) == {field:value}
            tested.add(field)
    assert tested == {field for field,value in schema['properties'].items() if not value.get('readOnly') and field!='objectToChange'}
    assert {'allowMove','isNameUnique','attributes','wipMsgConfig','wipMsgDefMgr','uiPreference','childLevels','parentLevels','useROR'} <= tested
    detail=json.loads(asyncio.run(levels.get_container_level_schema('CHILDLEVELS-EXPANDED')))
    assert detail['write_supported'] and {'NamedObjectRefExpandedListItem','NamedObjectRef'} <= set(detail['definitions'])


@pytest.mark.parametrize('body',['{"isFrozen":false}','{"InstanceID":"id"}','{"objectToChange":{"name":"OTHER"}}',
                                 '{"revision":"A"}','{"iconId":2147483648}','{"useROR":["false"]}'])
def test_invalid_full_payloads_never_reach_http(monkeypatch,body):
    request=AsyncMock()
    monkeypatch.setattr(levels,'request',request)
    assert 'error' in json.loads(asyncio.run(levels.update_container_level(CID,body_json=body)))
    assert 'error' in json.loads(asyncio.run(levels.create_container_level('NEW',body_json=body)))
    request.assert_not_awaited()


def test_create_checks_absence_posts_once_then_resolves_new_id_and_reads_back(monkeypatch):
    name="L'BOX"
    created={**obj(),'Name':name,'AllowMove':True,'IsNameUnique':True}
    request=AsyncMock(side_effect=['{"value":[]}', '✅ POST succeeded', json.dumps({'value':[{'Name':name,'InstanceID':CID}]}),json.dumps(created)])
    monkeypatch.setattr(levels,'request',request)
    result=json.loads(asyncio.run(levels.create_container_level(name,True,True)))
    assert result['verified'] and result['instance_id']==CID
    assert request.await_args_list[0].kwargs['params']['$filter']=="Name eq 'L''BOX'"
    assert request.await_args_list[1].args==('POST','/api/ContainerLevels')
    assert request.await_args_list[1].kwargs['body']=={'name':name,'allowMove':True,'isNameUnique':True}
    assert len(request.await_args_list)==4


def test_create_existing_name_never_overwrites(monkeypatch):
    request=AsyncMock(return_value=json.dumps({'value':[obj()]}))
    monkeypatch.setattr(levels,'request',request)
    result=json.loads(asyncio.run(levels.create_container_level('BOX')))
    assert result['submission_state']=='not_submitted' and result['error']
    assert request.await_count==1 and request.await_args.args[0]=='GET'


def test_create_unknown_response_with_object_present_is_not_claimed_as_success(monkeypatch):
    request=AsyncMock(side_effect=['{"value":[]}','❌ Request timed out',json.dumps({'value':[obj()]}),json.dumps(obj())])
    monkeypatch.setattr(levels,'request',request)
    result=json.loads(asyncio.run(levels.create_container_level('BOX')))
    assert result['observed_matches'] and not result['verified'] and result['submission_state']=='unknown'
    assert sum(call.args[0]=='POST' for call in request.await_args_list)==1


@pytest.mark.parametrize('reply,expected',[('✅ DELETE succeeded',True),('❌ Request timed out',False)])
def test_delete_verifies_absence_but_never_retries_unknown_outcome(monkeypatch,reply,expected):
    request=AsyncMock(side_effect=[json.dumps(obj()),reply,'❌ HTTP 404 Error'])
    monkeypatch.setattr(levels,'request',request)
    result=json.loads(asyncio.run(levels.delete_container_level(CID,'BOX')))
    assert result['observed_absent'] and result['verified'] is expected
    assert [call.args[0] for call in request.await_args_list]==['GET','DELETE','GET']
    assert 'body' not in request.await_args_list[1].kwargs


def test_delete_rejected_or_frozen_cannot_cascade_or_claim_absence(monkeypatch):
    request=AsyncMock(side_effect=[json.dumps(obj()),'❌ HTTP 400 Error: referenced',json.dumps(obj())])
    monkeypatch.setattr(levels,'request',request)
    result=json.loads(asyncio.run(levels.delete_container_level(CID)))
    assert not result['verified'] and not result['observed_absent']
    assert result['submission_state']=='rejected' and len(request.await_args_list)==3
    request.reset_mock(side_effect=True)
    request.return_value=json.dumps({**obj(),'IsFrozen':True})
    assert 'error' in json.loads(asyncio.run(levels.delete_container_level(CID)))
    assert request.await_count==1


def test_selection_values_are_read_only_context_and_allow_empty_json(monkeypatch):
    request=AsyncMock(return_value='{"value":[]}')
    monkeypatch.setattr(levels,'request',request)
    asyncio.run(levels.request_container_level_selection_values('ContainerNumberingRule','{"Name":"BOX"}'))
    request.assert_awaited_once_with('POST','/api/ContainerLevels/RequestSelectionValues',body={'name':'BOX'},
        params={'selectionValuesExpression':'ContainerNumberingRule'},response_formatter=levels.raw_json)
    request.reset_mock()
    asyncio.run(levels.request_container_level_selection_values(body_json='{}'))
    assert request.await_args.kwargs['body']=={}


def test_crud_and_schema_are_fully_registered_routed_and_classified():
    writes={'create_container_level':'create','update_container_level':'update','delete_container_level':'delete'}
    reads={'list_container_levels','get_container_level','get_container_levels_count',
           'get_container_level_schema','request_container_level_selection_values'}
    registered={entry['name'] for group in json.loads(asyncio.run(get_capabilities(group='reference')))['groups'] for entry in group['tools']}
    assert set(writes)|reads <= registered
    for name,kind in writes.items():
        assert get_tool_func(name) is getattr(levels,name) and classify_tool(name)==kind
    for name in reads: assert classify_tool(name) is None
    loaded=set(json.loads(asyncio.run(load_tool_group('reference')))['tools'])
    assert reads<=loaded and not set(writes)&loaded
    tools=[{'function':{'name':n}} for n in TOOL_POLICIES]
    for text in ('创建新的容器层级','修改BOX编号规则和父子层级','删除容器层级'):
        assert set(writes)<={t['function']['name'] for t in select_tools(tools,[{'role':'user','content':text}])}
    assert CONTAINER_LEVEL_RULE in SYSTEM_PROMPT and CONTAINER_LEVEL_RULE in ROUTING_INSTRUCTION
