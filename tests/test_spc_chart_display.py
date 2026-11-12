import asyncio
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from tools import spc_chart_display as s, mcp, get_tool_func
from core.tool_catalog import TOOL_POLICIES


def run(coro):return json.loads(asyncio.run(coro))


def snapshot(enabled=False):
    return {'environment':'test','chart':{'InstanceID':s.ID,'Name':s.NAME,'ChartVariable':'TestValue',
        'IsNewSPC':enabled,'SPCChartDisplayOption':2 if enabled else 3,'RunTimeDisplay':enabled,
        'SPCChartSaveOptions':3 if enabled else None,'TriggerOCAPs':False,'RequireAnnotation':False},
        'persisted':{'references':{'SPCChartTypeId':None,'SPCConnectionId':None}},
        'points':{key:{'DataValue':str(v)} for key,v in zip(s.POINTS,(9,10,11,14))}}


@pytest.fixture
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(s,'DB',tmp_path/'display.db')
    before=snapshot()
    monkeypatch.setattr(s,'snapshot',AsyncMock(return_value=before))
    monkeypatch.setattr(s,'display_contract',AsyncMock(return_value={'name':s.NAME,'chartVariable':'TestValue',**s.CHANGES}))
    return before


def test_success_is_one_fixed_put_after_durable_attempt_and_preserves_source(isolated,monkeypatch):
    s.snapshot.side_effect=[isolated,isolated,snapshot(True)]
    async def write(method,path,**kwargs):
        assert method=='PUT' and path=='/api/SPCChartDefs/'+s.ID
        assert kwargs['json']=={'name':s.NAME,'chartVariable':'TestValue',**s.CHANGES}
        with s.database() as db:
            row=db.execute('SELECT state,before_digest,body FROM attempts').fetchone()
        assert row[0]=='unknown' and row[1]==s.digest(isolated) and json.loads(row[2])==kwargs['json']
        return httpx.Response(200,json={'value':'updated'})
    call=AsyncMock(side_effect=write);monkeypatch.setattr(s,'http',call)
    out=run(s.update_spc_test_chart_display(s.digest(isolated)))
    assert out['state']=='verified' and out['source_values_unchanged'] and not out['native_chart_verified']
    assert call.await_count==1


@pytest.mark.parametrize('failure',['timeout','native_error','wrong_flags','changed_source','changed_reference','other_field'])
def test_unknown_never_retries_even_with_new_digest(isolated,monkeypatch,failure):
    after=snapshot(True)
    if failure=='wrong_flags':after['chart']['RunTimeDisplay']=False
    if failure=='changed_source':after['points'][s.POINTS[-1]]['DataValue']='99'
    if failure=='changed_reference':after['persisted']['references']['SPCChartTypeId']='OTHER'
    if failure=='other_field':after['chart']['Description']='Changed externally'
    s.snapshot.side_effect=[isolated,isolated,after]
    call=AsyncMock(return_value=httpx.Response(200,json={'ErrorCode':'native error'} if failure=='native_error' else {}))
    if failure=='timeout':call.side_effect=httpx.ReadTimeout('lost response')
    monkeypatch.setattr(s,'http',call)
    assert run(s.update_spc_test_chart_display(s.digest(isolated)))['state']=='unknown'
    out=run(s.update_spc_test_chart_display(s.digest(after)))
    assert out['state']=='not_submitted' and '禁止重发' in out['error']
    assert call.await_count==1


def test_stale_or_changing_source_stops_before_write(isolated,monkeypatch):
    call=AsyncMock();monkeypatch.setattr(s,'http',call)
    assert run(s.update_spc_test_chart_display('a'*64))['state']=='not_submitted'
    changed=copy.deepcopy(isolated);changed['points'][s.POINTS[0]]['DataValue']='99'
    s.snapshot.side_effect=[isolated,changed]
    assert run(s.update_spc_test_chart_display(s.digest(isolated)))['state']=='not_submitted'
    call.assert_not_awaited()


def test_already_configured_does_not_put_or_claim_native_chart(isolated,monkeypatch):
    before=snapshot(True);s.snapshot.return_value=before
    call=AsyncMock();monkeypatch.setattr(s,'http',call)
    assert run(s.update_spc_test_chart_display(s.digest(before)))['state']=='already_configured'
    call.assert_not_awaited()


def test_read_tool_and_registry_do_not_advertise_native_generation(isolated):
    out=run(s.get_spc_test_chart_display())
    assert out['before_digest']==s.digest(isolated) and out['mes_write_performed'] is False
    assert set(s.CHANGES)=={'isNewSPC','spcChartDisplayOption','runTimeDisplay','spcChartSaveOptions'}
    assert s.CHANGES['spcChartDisplayOption']==2
    registered={t.name for t in asyncio.run(mcp.list_tools())}
    for name in ('get_spc_test_chart_display','update_spc_test_chart_display'):
        assert name in registered and get_tool_func(name) is getattr(s,name)
    assert TOOL_POLICIES['update_spc_test_chart_display'].always_confirm
