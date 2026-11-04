import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import mcp, quality_event_checklist as checklist, quality_event_closure as closure
from tests.test_quality_event_closure import env as closure_env, base_env
from tests.test_quality_event_triage import EVENT, OTHER

CHECKLIST = '000000000000fd01'
ENTRY = '000000000000fd02'
RESPONSE = '000000000000fd03'


def read(**kwargs):
    return json.loads(asyncio.run(closure.get_quality_event_closure_context(
        'E', 'Org', include_checklist_details=True, **kwargs)))


@pytest.fixture
def env(closure_env, monkeypatch):
    state = closure_env
    state['event']['ChecklistId'] = CHECKLIST
    state['checklist'] = dict(ChecklistId=CHECKLIST, ParentId=EVENT, CDOTypeId=8121,
        ChangeCount=1, ChecklistName='Review', IsFrozen=False, ChecklistInstructions='Review all entries')
    state['entries'] = [dict(ChecklistEntryId=ENTRY, ParentId=CHECKLIST, CDOTypeId=8117,
        ChangeCount=1, ChecklistEntryName='Inspection', IsFrozen=False, ChecklistText='Confirm inspection',
        EntryRequired=True, CommentsEntry=3, UserComments=None, ResponseSetId=None, Sequence=1)]
    state['responses'] = []
    state['checklist_hook'] = None
    original_sql = checklist.q.sql_rows

    async def sql(query, maximum):
        if 'INFORMATION_SCHEMA.COLUMNS' in query and "TABLE_NAME='Checklist'" in query:
            return [dict(TABLE_NAME=table, COLUMN_NAME=column)
                    for table, columns in checklist.SCHEMA.items() for column in columns.split(',')]
        for table, name in [('Checklist', 'checklist'), ('ChecklistEntry', 'entries'),
                            ('ChecklistActualResponse', 'responses')]:
            if ' FROM ' + table + ' WHERE ' in query:
                if state['checklist_hook']:
                    state['checklist_hook'](table)
                return copy.deepcopy([state[name]] if name == 'checklist' else state[name])
        return await original_sql(query, maximum)

    monkeypatch.setattr(checklist.q, 'sql_rows', AsyncMock(side_effect=sql))
    state['contract'] = {'components': {'schemas': {'UpdateEventChecklist': {'properties': {
        'qualityObject': {'$ref': '#/components/schemas/NamedObjectRef'}}}}}}
    monkeypatch.setattr(checklist, 'request', AsyncMock(side_effect=lambda *a, **k: json.dumps(state['contract'])))
    return state


def test_required_response_and_comment_gaps_and_missing_save_contract(env):
    out = read()
    assert out['complete']
    detail = out['checklist_context']
    assert detail['assessment']['missing_response_entry_ids'] == [ENTRY]
    assert detail['assessment']['missing_comment_entry_ids'] == [ENTRY]
    assert detail['save_contract']['reason'] == 'event_wrapper_execute_checklist_not_exposed'
    assert not out['ready_for_execution']
    assert not detail['execution_ready']
    env['client'].request.assert_not_awaited()


def test_saved_flag_does_not_replace_actual_required_answers(env):
    env['event']['ChecklistSaved'] = True
    detail = read()['checklist_context']['assessment']
    assert detail['checklist_saved'] and not detail['required_entries_satisfied']


def test_native_null_response_item_still_counts_as_actual_response(env):
    env['responses'] = [dict(ChecklistActualResponseId=RESPONSE, ChecklistEntryId=ENTRY,
        CDOTypeId=8140, ChangeCount=1, ResponseItemId=None)]
    env['entries'][0]['UserComments'] = 'Reviewed'
    detail = read()['checklist_context']
    assert detail['assessment']['required_entries_satisfied']
    assert not detail['final_transaction_verified']
    assert not detail['execution_ready']


@pytest.mark.parametrize('mode', ['checklist_parent', 'entry_parent', 'entry_type', 'duplicate_entry',
    'entry_limit', 'response_limit', 'response_parent', 'duplicate_response', 'unknown_comment_rule', 'missing_field', 'changing'])
def test_incomplete_or_replaced_original_evidence_is_rejected(env, mode):
    if mode == 'checklist_parent':env['checklist']['ParentId'] = OTHER
    elif mode == 'entry_parent':env['entries'][0]['ParentId'] = OTHER
    elif mode == 'entry_type':env['entries'][0]['CDOTypeId'] = 1
    elif mode == 'duplicate_entry':env['entries'] *= 2
    elif mode == 'entry_limit':env['entries'] *= 21
    elif mode in ('response_limit', 'response_parent', 'duplicate_response'):
        response = dict(ChecklistActualResponseId=RESPONSE, ChecklistEntryId=OTHER if mode == 'response_parent' else ENTRY,
            CDOTypeId=8140, ChangeCount=1, ResponseItemId=None)
        env['responses'] = [response] * (201 if mode == 'response_limit' else 2 if mode == 'duplicate_response' else 1)
    elif mode == 'unknown_comment_rule':env['entries'][0]['CommentsEntry'] = 99
    elif mode == 'missing_field':env['entries'][0].pop('EntryRequired')
    elif mode == 'changing':
        def hook(table):
            if table == 'Checklist':env['checklist']['ChangeCount'] += 1
        env['checklist_hook'] = hook
    out = read()
    assert not out['complete'] and not out['ready_for_execution']
    env['client'].request.assert_not_awaited()


def test_contract_field_exposure_alone_keeps_adapter_pending(env):
    env['contract']['components']['schemas']['UpdateEventChecklist']['properties']['executeChecklist'] = {
        '$ref': '#/components/schemas/ExecuteChecklist'}
    detail = read()['checklist_context']['save_contract']
    assert detail['nested_execution_exposed'] and not detail['execution_ready']


def test_contract_read_failure_preserves_checklist_gaps(env):
    checklist.request.side_effect = ValueError('Unavailable')
    detail = read()['checklist_context']
    assert not detail['save_contract']['checked']
    assert detail['assessment']['missing_response_entry_ids'] == [ENTRY]


def test_default_diagnostic_keeps_previous_dependencies(env):
    out = json.loads(asyncio.run(closure.get_quality_event_closure_context('E', 'Org')))
    assert out['complete'] and out['checklist_context'] is None
    checklist.request.assert_not_awaited()


def test_no_checklist_avoids_native_contract_request(env):
    env['event']['ChecklistId'] = None
    env['event']['ChecklistSaved'] = True
    detail = read()['checklist_context']
    assert detail['assessment']['inconsistent_saved_flag']
    checklist.request.assert_not_awaited()


def test_mcp_optional_checklist_diagnostic(env):
    async def flow():
        async with Client(mcp) as client:
            reply = await client.call_tool('get_quality_event_closure_context', dict(
                event_name='E', organization_name='Org', include_checklist_details=True))
            assert json.loads(reply.content[0].text)['checklist_context']['snapshot']['checklist']['ChecklistId'] == CHECKLIST
    asyncio.run(flow())
