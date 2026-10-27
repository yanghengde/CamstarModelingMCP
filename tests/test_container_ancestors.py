import asyncio,json
from unittest.mock import AsyncMock
import pytest
from tools import container_transfers as ct

A='0000000000000001'
B='0000000000000002'
C='0000000000000003'

def row(cid,parent):return {'ContainerId':cid,'ContainerName':cid,'ParentContainerId':parent}

def test_multilevel_chain(monkeypatch):
    req=AsyncMock(side_effect=[json.dumps([row(A,B)]),json.dumps([row(B,C)]),json.dumps([row(C,None)])])
    monkeypatch.setattr(ct,'request',req)
    r=asyncio.run(ct.ancestor_chain(A))
    assert r['complete'] and [n['ContainerId'] for n in r['nodes']]==[A,B,C]
    assert all(c.args==('POST','/api/AdHoc') for c in req.call_args_list)


def test_cycle(monkeypatch):
    req=AsyncMock(side_effect=[json.dumps([row(A,B)]),json.dumps([row(B,A)])]);monkeypatch.setattr(ct,'request',req)
    r=asyncio.run(ct.ancestor_chain(A))
    assert not r['complete'] and r['cycle'] and req.call_count==2


@pytest.mark.parametrize('rows',[[],[row(B,None)],[{'ContainerId':A}],[row(A,None),row(A,None)],{'error':'denied'}])
def test_bad_rows(monkeypatch,rows):
    monkeypatch.setattr(ct,'request',AsyncMock(return_value=json.dumps(rows)))
    assert not asyncio.run(ct.ancestor_chain(A))['complete']


def test_bound_and_injection(monkeypatch):
    req=AsyncMock(return_value=json.dumps([row(A,B)]));monkeypatch.setattr(ct,'request',req)
    r=asyncio.run(ct.ancestor_chain(A,max_nodes=1));assert not r['complete']
    req.reset_mock()
    assert not asyncio.run(ct.ancestor_chain("x' OR 1=1"))['complete'];req.assert_not_called()


def test_deep_ancestor_conflict(monkeypatch):
    def snap(cid,parent):return {'id':cid,'container':{'Name':cid},'parent_id':parent,'parent_known':True}
    monkeypatch.setattr(ct,'snapshot',AsyncMock(side_effect=[snap(A,B),snap(C,None)]))
    monkeypatch.setattr(ct,'ancestor_chain',AsyncMock(return_value={'complete':True,'nodes':[row(A,B),row(B,C),row(C,None)],'cycle':False}))
    r=json.loads(asyncio.run(ct.preview_container_associate(A,[C])))
    assert r['relationship_conflicts']==[C] and not r['executable']


def test_relationship_changes(monkeypatch):
    def snap(cid):return {'id':cid,'container':{'Name':cid},'parent_id':None,'parent_known':True}
    monkeypatch.setattr(ct,'snapshot',AsyncMock(side_effect=[snap(A),snap(C)]))
    monkeypatch.setattr(ct,'ancestor_chain',AsyncMock(return_value={'complete':True,'nodes':[row(A,B),row(B,None)],'cycle':False}))
    r=json.loads(asyncio.run(ct.preview_container_associate(A,[C])))
    assert not r['relationship_checks_complete']
