import asyncio
from unittest.mock import AsyncMock
import pytest
from tools import container_level_rules as lr

P={'InstanceID':'p','Name':'Box'}
C={'InstanceID':'c','Name':'Unit'}

@pytest.mark.parametrize('rows,expected',[(None,'unknown'),([],'unknown'),([C],'listed'),([{'Name':'Unit'}],'listed'),([{'Name':'Other','InstanceID':'x'}],'not_listed'),([{'Name':'Unit','InstanceID':'wrong'}],'unknown'),([{}],'unknown')])
def test_membership(rows,expected):assert lr.membership(rows,C)==expected


def test_read_cache_and_both_directions(monkeypatch):
    ctx=AsyncMock()
    ctx.resolve.side_effect=[P,C]
    ctx.get.side_effect=[dict(P,ChildLevels=[C],ParentLevels=[]),dict(C,ParentLevels=[P],ChildLevels=[])]
    monkeypatch.setattr(lr,'ReadContext',lambda **kw:ctx)
    parent={'status':{'ContainerLevel':P}}
    child={'id':'child','status':{'ContainerLevel':C}}
    r=asyncio.run(lr.check_level_rules(parent,[child,child]))
    assert r['complete'] and not r['capacity_verified']
    assert ctx.get.call_count==2
    assert all(c['child_allows_parent']=='listed' for c in r['checks'])


def test_bad_expansion(monkeypatch):
    ctx=AsyncMock();ctx.resolve.return_value=P;ctx.get.return_value=dict(P,Name='wrong')
    monkeypatch.setattr(lr,'ReadContext',lambda **kw:ctx)
    r=asyncio.run(lr.check_level_rules({'status':{'ContainerLevel':P}},[]))
    assert not r['complete'] and 'error' in r
