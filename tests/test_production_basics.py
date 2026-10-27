import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from tools import production_basics as p, get_tool_func
from core.tool_catalog import TOOL_POLICIES


@pytest.mark.parametrize('name,endpoint,field', [('hold_container','Hold','holdReason'),('release_container','Release','releaseReason')])
def test_hold_release_contract(monkeypatch,name,endpoint,field):
    call=AsyncMock(return_value='ok'); monkeypatch.setattr(p,'request_shopfloor',call)
    assert asyncio.run(getattr(p,name)('BOX','Reason','id','note'))=='ok'
    call.assert_awaited_once_with('POST','/api/'+endpoint,body={'container':{'name':'BOX','instanceId':'id'},field:{'name':'Reason'},'comments':'note'})
    assert get_tool_func(name) is getattr(p,name)
    assert TOOL_POLICIES[name].always_confirm


@pytest.mark.parametrize('kind,entity', list(p.REASONS.items()))
def test_reason_contract(monkeypatch,kind,entity):
    call=AsyncMock(return_value='ok'); monkeypatch.setattr(p,'request',call)
    asyncio.run(p.list_production_reasons(kind,10,20))
    call.assert_awaited_once_with('GET','/api/'+entity,params={'$top':10,'$skip':20})
    evidence=json.loads(Path('docs/production-api-audit-evidence.json').read_text(encoding='utf-8'))
    contract=next(c for c in evidence['contracts'] if c['name']==entity)
    assert 'get' in contract['paths']['/api/'+entity]


@pytest.mark.parametrize('kind',range(1,7))
def test_quantity_contract(monkeypatch,kind):
    call=AsyncMock(return_value='ok'); monkeypatch.setattr(p,'request_shopfloor',call)
    asyncio.run(p.change_container_quantity('BOX',kind,'reason',2,qty2=3))
    body=call.await_args.kwargs['body']
    assert body=={'container':{'name':'BOX'},'serviceDetails':[{'changeQtyType':kind,'reasonCode':{'name':'reason'},'qty':2,'qty2':3}]}
    assert call.await_args.args==('POST','/api/ChangeQty')
    evidence=json.loads(Path('docs/production-api-audit-evidence.json').read_text(encoding='utf-8'))
    schema=next(c for c in evidence['contracts'] if c['name']=='ChangeQty')['schemas']
    assert set(body)<=set(schema['ChangeQty']['properties'])
    assert set(body['serviceDetails'][0])<=set(schema['ChangeQtyDetails']['properties'])


def test_invalid_inputs_never_write(monkeypatch):
    call=AsyncMock(); monkeypatch.setattr(p,'request_shopfloor',call)
    assert asyncio.run(p.hold_container('','reason')).startswith('Error:')
    assert asyncio.run(p.release_container('BOX',' ')).startswith('Error:')
    for kind,qty in [(7,1),(True,1),(1,float('nan')),(1,float('inf'))]:
        assert asyncio.run(p.change_container_quantity('BOX',kind,'r',qty)).startswith('Error:')
    call.assert_not_awaited()


def test_server_failure_returned_without_retry(monkeypatch):
    call=AsyncMock(return_value='Error: container state rejected'); monkeypatch.setattr(p,'request_shopfloor',call)
    assert asyncio.run(p.hold_container('BOX','r'))=='Error: container state rejected'
    assert call.await_count==1
