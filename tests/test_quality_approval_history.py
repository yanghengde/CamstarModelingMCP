import asyncio
import copy
import json
import re
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from tools import mcp, get_tool_func, quality_approval_history as h, quality_plans as p
from tests.test_quality_approvals import env as approval_env, base_env, EVENT, SHEET, ENTRY, OWNER, ROLE, OTHER, DECISION, CAPA

CYCLE='2026-10-29T01:00:00.1234567Z'
OLD='2026-10-29T01:00:00.1234566Z'
HISTORY='0000000000005001'
SIGN='0000000000005002'


def run(**kw):return json.loads(asyncio.run(h.get_quality_approval_history('E','Org',SHEET,**kw)))


@pytest.fixture
def env(approval_env,monkeypatch):
    s=approval_env;s['sheets'][0]['LastApprovalCycleGMT']=CYCLE
    s.update(history=[],sign_history=[],history_queries=[],history_hook=None)
    def row(table,key,cycle):
        r=dict.fromkeys(h.SCHEMA[table].split(','))
        r.update({table+'Id':key},ApprovalSheetId=SHEET,QualityObjectId=EVENT,ApprovalSheetEntryId=ENTRY,
                 ApprovalCycleGMT=cycle,ChangeCount=1,TxnId=OTHER,HistoryId=OTHER,HistoryMainlineId=OTHER,
                 Comments='Native comment',GeneralInstructions='Original instructions')
        if table=='ApprovalSheetHistory':r.update(Status=10)
        else:r.update(ApproverId=OWNER,ApproverRoleId=ROLE,ApprovedById=OWNER,ApprovalDecisionId=DECISION,
                      DecisionType=20,SheetLevel=1,LastCompletedOnGMT=CYCLE)
        return r
    s['history']=[row('ApprovalSheetHistory',HISTORY,OLD)]
    s['sign_history']=[row('SignApprovalHistory',SIGN,OLD)]
    s['make_history']=row
    original=h.q.sql_rows
    async def sql(query,maximum):
        for table,storage in [('ApprovalSheetHistory','history'),('SignApprovalHistory','sign_history')]:
            if ' FROM '+table+' WHERE ' in query:
                s['history_queries'].append((query,maximum))
                if s['history_hook']:s['history_hook'](table)
                return copy.deepcopy(s[storage])
        return await original(query,maximum)
    monkeypatch.setattr(h.q,'sql_rows',AsyncMock(side_effect=sql))
    return s


def test_exact_current_old_cycles_and_original_decisions_are_readonly(env):
    out=run();assert out['complete'] and not out['ready_for_execution']
    assert len(out['cycles'])==2 and out['cycles'][0]['is_current_cycle']
    old=out['cycles'][1];assert old['routed_history_present'] and old['decision_history'][0]['DecisionType']==20
    assert old['decision_history'][0]['Comments']=='Native comment'
    assert not out['transaction_attribution_verified'] and not out['retry_allowed']
    assert not p.PLAN_DB.exists();env['client'].request.assert_not_awaited()
    assert len(env['history_queries'])==4
    assert all('TOP ' in sql and 'TxnType <>' not in sql for sql,_ in env['history_queries'])


def test_explicit_cycle_uses_exact_seventh_digit_in_fixed_sql(env):
    out=run(approval_cycle_gmt=OLD);assert out['complete'] and len(out['cycles'])==1
    assert all("CONVERT(datetime2(7),N'2026-10-29T01:00:00.1234566',126)" in sql for sql,_ in env['history_queries'])


@pytest.mark.parametrize('value,expected',[(CYCLE,CYCLE),('2026-10-29 01:00:00.1234567',CYCLE),
    ('2026-10-29T09:00:00.1234567+08:00',CYCLE),('2026-10-29T01:00:00Z','2026-10-29T01:00:00.0000000Z')])
def test_gmt_column_canonicalization_preserves_precision(value,expected):assert h.cycle_key(value)==expected


@pytest.mark.parametrize('cycle',['x',"2026-10-29';DROP",True,'2026-02-30T00:00:00Z','2026-10-29T01:00:00.12345678Z'])
def test_invalid_cycle_does_not_become_sql(env,cycle):
    assert not run(approval_cycle_gmt=cycle)['complete'];assert not env['history_queries']


@pytest.mark.parametrize('table,storage,count',[('ApprovalSheetHistory','history',101),('SignApprovalHistory','sign_history',201)])
def test_history_overflow_never_returns_partial_evidence(env,table,storage,count):
    env[storage]=[env['make_history'](table,f'{7000+n:016x}',OLD) for n in range(count)]
    out=run();assert not out['complete'] and 'cycles' not in out


@pytest.mark.parametrize('storage,field,value',[
    ('history','QualityObjectId',OTHER),('sign_history','ApprovalSheetId',OTHER),
    ('sign_history','ApprovalSheetEntryId',None),('history','ApprovalCycleGMT','bad'),
    ('sign_history','DecisionType',True),('history','ChangeCount',-1)])
def test_invalid_or_cross_object_history_is_rejected(env,storage,field,value):
    env[storage][0][field]=value;assert not run()['complete']


@pytest.mark.parametrize('storage',['history','sign_history'])
def test_duplicate_or_missing_history_is_not_complete(env,storage):
    env[storage]*=2;assert not run()['complete']
    env[storage]=[env[storage][0]];env[storage][0].pop('TxnId');assert not run()['complete']


def test_selected_cycle_cannot_return_other_cycle_rows(env):
    assert not run(approval_cycle_gmt=CYCLE)['complete']


def test_cancelled_decision_and_removed_entry_are_kept_in_history(env):
    env['sign_history'][0].update(DecisionType=30,ApprovedById=None,ApprovalDecisionId=None,ApprovalSheetEntryId=OTHER)
    out=run();assert out['complete'] and out['cycles'][1]['decision_history'][0]['DecisionType']==30
    assert out['cycles'][1]['decision_history'][0]['ApprovalSheetEntryId']==OTHER


def test_changed_history_during_read_is_rejected(env):
    seen=[]
    def hook(table):
        seen.append(table)
        if len(seen)==3:env['history'][0]['ChangeCount']+=1
    env['history_hook']=hook;assert not run()['complete']


def test_safe_projection_never_exposes_overreturned_secrets(env):
    env['sign_history'][0]['Password']='SECRET_NEVER_RETURN'
    out=run();assert out['complete'] and 'SECRET_NEVER_RETURN' not in json.dumps(out)


def test_readonly_history_accepts_signature_policy_and_capa(env):
    env['sheets'][0]['ParentId']=CAPA;env['inquiry']['Category']=5
    env['roots']['Organizations']['CollectESigForAllQualityTxns']=True
    for storage in ('history','sign_history'):env[storage][0]['QualityObjectId']=CAPA
    out=run(object_kind='capa');assert out['complete'] and out['target']['Id']==CAPA


def test_actual_mcp_schema_and_read_routing(env):
    async def check():
        async with Client(mcp) as client:
            schema=next(t.input_schema for t in await client.list_tools() if t.name=='get_quality_approval_history')
            assert not schema['additionalProperties'] and 'approval_cycle_gmt' in schema['properties']
            reply=await client.call_tool('get_quality_approval_history',dict(name='E',organization_name='Org',approval_sheet_id=SHEET))
            assert json.loads(reply.content[0].text)['complete']
    asyncio.run(check());assert get_tool_func('get_quality_approval_history') is h.get_quality_approval_history
    from core.tool_catalog import TOOL_POLICIES
    from agent.tool_routing import select_tools
    chosen={x['function']['name'] for x in select_tools([{'function':{'name':n}} for n in TOOL_POLICIES],[dict(role='user',content='查看审批旧周期历史')])}
    assert 'get_quality_approval_history' in chosen and 'execute_quality_transaction' not in chosen
