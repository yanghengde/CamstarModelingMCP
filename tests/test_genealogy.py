import asyncio,json,copy
from unittest.mock import AsyncMock
import pytest
from tools import container_genealogy as g, import_plans
A='0004108000000002';B='0004108000000003'
def node(identifier,rows,more=False):
 return {'container':{'Name':'A' if identifier==A else 'B','InstanceId':identifier},'sections':{'splits':{'rows':rows,'page_complete':True,'has_more':more,'next_offset':20 if more else None}}}

def install(monkeypatch,cycle=True):
 async def trace(name,identifier=None,*args):
  identifier=identifier or A
  rows=[{'FromContainerId':A,'ToContainerId':B,'SplitHistoryDetailsId':'forward'}] if identifier==A else ([{'FromContainerId':B,'ToContainerId':A,'SplitHistoryDetailsId':'back'}] if cycle else [])
  return json.dumps(node(identifier,rows))
 mock=AsyncMock(side_effect=trace);monkeypatch.setattr(g,'get_container_trace',mock)
 monkeypatch.setattr(g,'request',AsyncMock(return_value=json.dumps([{'ContainerId':B,'ContainerName':'B'}])))
 return mock

def test_cycle_and_dedup(monkeypatch):
 mock=install(monkeypatch)
 result=json.loads(asyncio.run(g.get_container_genealogy('A')))
 assert len(result['nodes'])==2 and mock.await_count==2
 assert result['cycles'] and result['traversal_complete'] and not result['complete']

def test_depth_boundary_is_explicit(monkeypatch):
 install(monkeypatch)
 result=json.loads(asyncio.run(g.get_container_genealogy('A',max_depth=0)))
 assert len(result['nodes'])==1 and not result['traversal_complete']
 assert result['boundaries'][0]['container_id']==B

def test_unknown_crossref_is_never_traversed(monkeypatch):
 data=node(A,[]);data['sections']={'cross_references':{'rows':[{'HistoryId':B,'TrackingId':B}], 'page_complete':True,'has_more':False}}
 monkeypatch.setattr(g,'get_container_trace',AsyncMock(return_value=json.dumps(data)))
 request=AsyncMock();monkeypatch.setattr(g,'request',request)
 result=json.loads(asyncio.run(g.get_container_genealogy('A')))
 assert not result['edges'] and len(result['nodes'])==1
 request.assert_not_called()

def test_page_budget_reports_continuation(monkeypatch):
 monkeypatch.setattr(g,'get_container_trace',AsyncMock(return_value=json.dumps(node(A,[],True))))
 result=json.loads(asyncio.run(g.get_container_genealogy('A',max_pages=1)))
 assert result['boundaries'][0]['next_offset']==20
 assert not result['traversal_complete']

def test_second_page_exhaustion(monkeypatch):
 mock=AsyncMock(side_effect=[json.dumps(node(A,[],True)),json.dumps(node(A,[]))])
 monkeypatch.setattr(g,'get_container_trace',mock)
 result=json.loads(asyncio.run(g.get_container_genealogy('A',max_pages=2)))
 assert result['traversal_complete'] and mock.call_args.args[3]==20
 assert result['nodes'][A]['trace']['sections']['splits']['pages_read']==2

def test_write_timeout_is_uncertain(monkeypatch):
 monkeypatch.setattr(import_plans,'request',AsyncMock(return_value='❌ Request timed out after 30s'))
 outcomes=[]
 with pytest.raises(ValueError):asyncio.run(import_plans._write('PUT','/api/test',{},outcomes))
 assert outcomes[0]['success'] is None

def test_invalid_limits_do_not_read(monkeypatch):
 mock=AsyncMock();monkeypatch.setattr(g,'get_container_trace',mock)
 assert json.loads(asyncio.run(g.get_container_genealogy('A',max_nodes=21)))['error']
 mock.assert_not_called()
