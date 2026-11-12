import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from tools import mcp, get_tool_func
from core.tool_catalog import TOOL_POLICIES
from tools import spc_modeling_test as s

PREFIX = 'CDX-C6-MODEL-TEST'


def run(coro):
    return json.loads(asyncio.run(coro))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(s, 'DB', tmp_path/'plans.db')
    monkeypatch.setattr(s, 'SAFE_CREATE_THRESHOLD', 20)
    monkeypatch.setattr(s, 'contracts', AsyncMock(return_value={'contract': 'current'}))
    monkeypatch.setattr(s, 'ensure_absent', AsyncMock())
    monkeypatch.setattr(s, 'verify', AsyncMock(side_effect=lambda item: {'entity': item['entity'], 'verified': True}))


def plan():
    p = run(s.prepare_spc_modeling_test(PREFIX))
    assert p['executable']
    return {key:p[key] for key in ('plan_id', 'plan_digest')}


@pytest.mark.parametrize('prefix', ['PRODUCTION', 'CDX-C6-MODEL-../X', "CDX-C6-MODEL-X'", 'CDX-C6-MODEL-', 'CDX-C6-MODEL-'+'X'*33])
def test_invalid_prefix_never_reads_or_writes(isolated, prefix):
    assert not run(s.prepare_spc_modeling_test(prefix))['executable']
    s.contracts.assert_not_awaited()


def test_fixed_demo_excludes_connections_and_production_effects():
    items = s.models(PREFIX)
    assert len(items) == 6
    assert 'SPCConnections' not in {i['entity'] for i in items}
    chart = next(i['body'] for i in items if i['entity']=='SPCChartDefs')
    assert chart['spcChartDisplayOption']==3 and chart['spcChartSaveOptions']==3
    assert chart['triggerOCAPs'] is chart['requireAnnotation'] is chart['runTimeDisplay'] is False
    assert not {'spcConnection', 'putContainerOnHold', 'setResourceStatus', 'emailNotification'} & chart.keys()
    query = next(i['body']['queryText'] for i in items if i['entity']=='SPCQueries')
    assert ' FROM ' not in query and all(f'CAST({n} AS' in query for n in (9,10,11,14))


def test_live_database_name_bound_is_checked_before_contract_calls(isolated):
    out=run(s.prepare_spc_modeling_test('CDX-C6-MODEL-20261112-A'))
    assert not out['executable'] and '30' in out['error']
    s.contracts.assert_not_awaited()


def test_attempt_precedes_post_and_unknown_never_replays_even_via_new_plan(isolated, monkeypatch):
    first, second = plan(), plan()
    async def send(method, path, **kwargs):
        assert method=='POST'
        with s.database() as db:
            record = db.execute('SELECT body FROM attempts WHERE plan_id=?', (first['plan_id'],)).fetchone()
        assert json.loads(record[0]) == kwargs['json']
        raise httpx.ReadTimeout('response lost')
    post = AsyncMock(side_effect=send)
    monkeypatch.setattr(s, 'http', post)
    assert run(s.execute_spc_modeling_test(**first))['state']=='unknown'
    assert not run(s.prepare_spc_modeling_test(PREFIX))['executable']
    assert run(s.execute_spc_modeling_test(**first))['replayed'] is False
    assert '跨计划' in run(s.execute_spc_modeling_test(**second))['error']
    assert post.await_count==1


def test_contract_drift_stops_before_first_post(isolated, monkeypatch):
    args=plan()
    s.contracts.return_value={'contract':'changed'}
    post=AsyncMock(); monkeypatch.setattr(s,'http',post)
    result=run(s.execute_spc_modeling_test(**args))
    assert result['state']=='unknown' and not result['objects']
    post.assert_not_awaited()


def test_partial_native_rejection_stops_without_creating_remaining_models(isolated, monkeypatch):
    args=plan()
    replies=[httpx.Response(201,text='created'), httpx.Response(400,text='native rejection')]
    post=AsyncMock(side_effect=replies);monkeypatch.setattr(s,'http',post)
    out=run(s.execute_spc_modeling_test(**args))
    assert out['state']=='unknown' and len(out['objects'])==1 and post.await_count==2
    assert all(call.args[1].startswith('/api/SPC') for call in post.await_args_list)
    with s.database() as db:
        assert db.execute('SELECT status FROM attempts ORDER BY rowid').fetchall()==[(201,),(400,)]


def test_success_rereads_all_six_and_inspection_never_submits(isolated, monkeypatch):
    args=plan()
    post=AsyncMock(return_value=httpx.Response(201,text='created'));monkeypatch.setattr(s,'http',post)
    out=run(s.execute_spc_modeling_test(**args))
    assert out['state']=='verified' and len(out['objects'])==6 and post.await_count==6
    assert s.verify.await_count==12
    assert run(s.inspect_spc_modeling_test(**args))['complete']
    assert post.await_count==6


def test_tampered_payload_rejected_despite_recomputed_digest(isolated, monkeypatch):
    args=plan()
    with s.database() as db:
        payload=json.loads(db.execute('SELECT payload FROM plans WHERE id=?',(args['plan_id'],)).fetchone()[0])
        payload['models'][4]['body']['triggerOCAPs']=True
        args['plan_digest']=s.digest(payload)
        db.execute('UPDATE plans SET digest=?,payload=? WHERE id=?',(args['plan_digest'],json.dumps(payload),args['plan_id']))
    post=AsyncMock();monkeypatch.setattr(s,'http',post)
    assert run(s.execute_spc_modeling_test(**args))['state']=='not_submitted'
    post.assert_not_awaited()


def test_new_tools_have_matching_mcp_registration_and_explicit_write_policy():
    registered={t.name for t in asyncio.run(mcp.list_tools())}
    for name in ('prepare_spc_modeling_test','execute_spc_modeling_test','inspect_spc_modeling_test'):
        assert name in registered and get_tool_func(name) is getattr(s,name)
    assert TOOL_POLICIES['execute_spc_modeling_test'].effect=='create'
    assert TOOL_POLICIES['execute_spc_modeling_test'].always_confirm
    assert TOOL_POLICIES['prepare_spc_modeling_test'].effect==TOOL_POLICIES['inspect_spc_modeling_test'].effect=='read'


def test_reference_and_boolean_matching_is_strict():
    expected={'spcViolation':{'name':'DEMO'}}
    assert s.matches(expected,{'SPCViolation':{'Name':'DEMO','InstanceID':'1'}})
    assert not s.matches(expected,{'SPCViolation':None})
    assert not s.matches(False,0)


@pytest.mark.parametrize('status,reply', [(None,None),(400,'{}'),(200,'{"ErrorCode":"bad"}')])
def test_continuation_rejects_uncertain_or_rejected_prior_post(isolated,monkeypatch,status,reply):
    args=plan()
    with s.database() as db:
        db.execute('INSERT INTO claims VALUES(?,?)',(s.scope(PREFIX),args['plan_id']))
        db.execute("UPDATE plans SET state='unknown' WHERE id=?",(args['plan_id'],))
        first=s.models(PREFIX)[0]
        db.execute('INSERT INTO attempts VALUES(?,?,?,?,?)',(args['plan_id'],first['entity'],json.dumps(first['body']),status,reply))
    post=AsyncMock();monkeypatch.setattr(s,'http',post)
    out=run(s.execute_spc_modeling_test(**args,continue_unattempted=True))
    assert out['state']=='unknown' and '不能继续' in out['error']
    post.assert_not_awaited()


def test_continuation_only_creates_unattempted_suffix_after_prior_ack_and_readback(isolated,monkeypatch):
    args=plan()
    post=AsyncMock(return_value=httpx.Response(200,json={'value':'native added'}));monkeypatch.setattr(s,'http',post)
    s.verify.side_effect=[{'verified':True},ValueError('projection not expanded')]
    assert run(s.execute_spc_modeling_test(**args))['state']=='unknown'
    assert post.await_count==2
    s.verify.side_effect=lambda item:{'entity':item['entity'],'verified':True}
    out=run(s.execute_spc_modeling_test(**args,continue_unattempted=True))
    assert out['state']=='verified' and len(out['objects'])==6
    assert [call.args[1] for call in post.await_args_list]==['/api/'+item['entity'] for item in s.models(PREFIX)]


def test_expansion_null_scalars_do_not_overwrite_independent_plain_get(monkeypatch):
    item=s.models(PREFIX)[4];key='0000000000000001'
    plain={'InstanceID':key,**{k[0].upper()+k[1:]:v for k,v in item['body'].items()}}
    refs={k:v for k,v in plain.items() if isinstance(v,dict)}
    expanded={k:None for k in plain}|{'InstanceID':key,'Name':item['body']['name']}|refs
    monkeypatch.setattr(s,'find',AsyncMock(return_value={'InstanceID':key}))
    get=AsyncMock(side_effect=[plain,expanded]);monkeypatch.setattr(s.ReadContext,'get',get)
    assert asyncio.run(s.verify(item))['verified']
    assert get.await_count==2


@pytest.mark.parametrize('changed',['none','wrong_name','wrong_description'])
def test_missing_inherited_description_requires_exact_persisted_identity_and_value(monkeypatch,changed):
    item=s.models(PREFIX)[3];key='0000000000000001'
    plain={'InstanceID':key,'Name':item['body']['name'],'Description':None,'Violations':item['body']['violations']}
    monkeypatch.setattr(s,'find',AsyncMock(return_value={'InstanceID':key}))
    monkeypatch.setattr(s.ReadContext,'get',AsyncMock(return_value=plain))
    row={'SPCRulesId':key,'SPCRulesName':item['body']['name'],'Description':s.NOTE}
    if changed=='wrong_name':row['SPCRulesName']='OTHER'
    if changed=='wrong_description':row['Description']='OTHER'
    query=AsyncMock(return_value=[row]);monkeypatch.setattr(s.q,'sql_rows',query)
    if changed=='none':assert asyncio.run(s.verify(item))['verified']
    else:
        with pytest.raises(ValueError):asyncio.run(s.verify(item))
    assert query.await_args.args[0].startswith('SELECT TOP 2 ')
