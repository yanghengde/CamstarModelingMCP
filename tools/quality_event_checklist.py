"""Bounded original-checklist evidence for Event closure diagnostics."""
import copy
import json

from tools import quality as q, quality_inspection as inspection
from tools.quality_spc_annotations import iid, schema_check
from core.http_client import request
from tools.modeling_details import raw_json
from config import CAMSTAR_SHOPFLOOR_BASE_URL

CHECKLIST_COLUMNS = 'ChecklistId,ParentId,CDOTypeId,ChangeCount,ChecklistName,IsFrozen,ChecklistInstructions'
ENTRY_COLUMNS = ('ChecklistEntryId,ParentId,CDOTypeId,ChangeCount,ChecklistEntryName,IsFrozen,'
                 'ChecklistText,EntryRequired,CommentsEntry,UserComments,ResponseSetId,Sequence')
RESPONSE_COLUMNS = 'ChecklistActualResponseId,ChecklistEntryId,CDOTypeId,ChangeCount,ResponseItemId'
SCHEMA = {'Checklist': CHECKLIST_COLUMNS, 'ChecklistEntry': ENTRY_COLUMNS,
          'ChecklistActualResponse': RESPONSE_COLUMNS}


def record(raw, columns, identity, parent_field, parent_id, cdo_type):
    if not isinstance(raw, dict) or not set(columns.split(',')) <= raw.keys():
        raise ValueError('检查表记录字段不完整。')
    row = {field: copy.deepcopy(raw[field]) for field in columns.split(',')}
    row[identity] = iid(row[identity])
    row[parent_field] = iid(row[parent_field])
    row['CDOTypeId'] = inspection.wire_int(row['CDOTypeId'], '检查表记录类型')
    row['ChangeCount'] = inspection.wire_int(row['ChangeCount'], '检查表记录版本')
    if row[parent_field] != parent_id or row['CDOTypeId'] != cdo_type or row['ChangeCount'] < 0:
        raise ValueError('检查表原始身份、归属、类型或版本不符。')
    return row


async def snapshot(event):
    checklist_id = event['ChecklistId']
    if checklist_id in (None, ''):
        return dict(configured=False, checklist_saved=event['ChecklistSaved'], entries=[], responses=[])
    checklist_id = iid(checklist_id)
    await schema_check(SCHEMA)
    rows = await q.sql_rows('SELECT TOP 2 ' + CHECKLIST_COLUMNS
        + f" FROM Checklist WHERE ChecklistId='{checklist_id}'", 2)
    if len(rows) != 1:
        raise ValueError('原事件检查表缺失或不唯一。')
    checklist = record(rows[0], CHECKLIST_COLUMNS, 'ChecklistId', 'ParentId', event['EventId'], 8121)
    if checklist['ChecklistId'] != checklist_id:
        raise ValueError('原检查表ID不符。')
    checklist['IsFrozen'] = inspection.wire_bool(checklist['IsFrozen'], '检查表冻结状态')
    entries = await q.sql_rows('SELECT TOP 21 ' + ENTRY_COLUMNS
        + f" FROM ChecklistEntry WHERE ParentId='{checklist_id}' ORDER BY Sequence,ChecklistEntryId", 21)
    if len(entries) > 20:
        raise ValueError('检查表超过20项，未读取完整范围。')
    clean_entries = []
    seen = set()
    for raw in entries:
        entry = record(raw, ENTRY_COLUMNS, 'ChecklistEntryId', 'ParentId', checklist_id, 8117)
        key = entry['ChecklistEntryId']
        if key in seen:
            raise ValueError('检查表条目重复。')
        seen.add(key)
        entry['EntryRequired'] = inspection.wire_bool(entry['EntryRequired'], '条目必填')
        entry['IsFrozen'] = inspection.wire_bool(entry['IsFrozen'], '条目冻结状态')
        entry['CommentsEntry'] = inspection.wire_int(entry['CommentsEntry'], '评论规则')
        entry['Sequence'] = inspection.wire_int(entry['Sequence'], '条目顺序')
        if entry['CommentsEntry'] not in (1, 2, 3):
            raise ValueError('检查表评论规则未知。')
        if entry['UserComments'] is not None and not isinstance(entry['UserComments'], str):
            raise ValueError('检查表评论原值类型不符。')
        if entry['ResponseSetId'] not in (None, ''):
            entry['ResponseSetId'] = iid(entry['ResponseSetId'])
        clean_entries.append(entry)
    clean_responses = []
    if seen:
        keys = ','.join(q.literal(key) for key in sorted(seen))
        responses = await q.sql_rows('SELECT TOP 201 ' + RESPONSE_COLUMNS
            + ' FROM ChecklistActualResponse WHERE ChecklistEntryId IN (' + keys
            + ') ORDER BY ChecklistActualResponseId', 201)
        if len(responses) > 200:
            raise ValueError('检查表实际响应超过200条，未读取完整范围。')
        response_ids = set()
        for raw in responses:
            if not isinstance(raw, dict) or raw.get('ChecklistEntryId') not in seen:
                raise ValueError('响应属于其它检查表条目。')
            response = record(raw, RESPONSE_COLUMNS, 'ChecklistActualResponseId', 'ChecklistEntryId',
                              raw['ChecklistEntryId'], 8140)
            if response['ChecklistActualResponseId'] in response_ids:
                raise ValueError('检查表响应重复。')
            response_ids.add(response['ChecklistActualResponseId'])
            if response['ResponseItemId'] not in (None, ''):
                response['ResponseItemId'] = iid(response['ResponseItemId'])
            clean_responses.append(response)
    return dict(configured=True, checklist_saved=event['ChecklistSaved'], checklist=checklist,
                entries=clean_entries, responses=clean_responses)


def assessment(state):
    if not state['configured']:
        return dict(configured=False, required_entries_satisfied=None,
                    inconsistent_saved_flag=bool(state['checklist_saved']), execution_ready=False)
    answered = {row['ChecklistEntryId'] for row in state['responses']}
    missing_responses = [row['ChecklistEntryId'] for row in state['entries']
                         if row['EntryRequired'] and row['ChecklistEntryId'] not in answered]
    # Standard ChecklistCommentCtrlEnum.CommentsRequired=3. Native validation
    # checks empty/null, so preserve whitespace semantics in this evidence view.
    missing_comments = [row['ChecklistEntryId'] for row in state['entries']
                        if row['CommentsEntry'] == 3 and not row['UserComments']]
    return dict(configured=True, required_entries_satisfied=not missing_responses and not missing_comments,
                missing_response_entry_ids=missing_responses, missing_comment_entry_ids=missing_comments,
                checklist_frozen=state['checklist']['IsFrozen'],
                frozen_entry_ids=[row['ChecklistEntryId'] for row in state['entries'] if row['IsFrozen']],
                checklist_saved=state['checklist_saved'], execution_ready=False,
                native_validation_verified=False)


async def save_contract():
    try:
        raw = await request('GET', '/swagger/UpdateEventChecklist/swagger.json',
                            base_url=CAMSTAR_SHOPFLOOR_BASE_URL, response_formatter=raw_json)
        schemas = json.loads(raw)['components']['schemas']
        root = schemas['UpdateEventChecklist']['properties']
        nested = root.get('executeChecklist')
        if nested is None:
            return dict(checked=True, nested_execution_exposed=False, execution_ready=False,
                        reason='event_wrapper_execute_checklist_not_exposed')
        if nested.get('$ref', '').split('/')[-1] != 'ExecuteChecklist':
            raise ValueError('未知检查表嵌套类型。')
        return dict(checked=True, nested_execution_exposed=True, execution_ready=False,
                    reason='save_adapter_and_deployed_behavior_pending')
    except (ValueError, TypeError, KeyError, AttributeError):
        return dict(checked=False, nested_execution_exposed=None, execution_ready=False,
                    reason='deployed_contract_unavailable')


async def context(event):
    before = await snapshot(event)
    contract = await save_contract() if before['configured'] else None
    if await snapshot(event) != before:
        raise ValueError('读取期间检查表内容、配置或响应变化。')
    return dict(snapshot=before, assessment=assessment(before), save_contract=contract,
                execution_ready=False, final_transaction_verified=False)
