import asyncio,json
from unittest.mock import AsyncMock
from types import SimpleNamespace
import httpx
from core import http_client as http
from tools import container_trace as trace


def response(code):return httpx.Response(code,json={'ok':True},request=httpx.Request('GET','https://example.test'))

def test_retry_reads_but_not_writes(monkeypatch):
 monkeypatch.setattr(http,'get_headers',lambda:{})
 for method,flag,count in [('GET',False,2),('POST',False,1),('POST',True,2),('PUT',False,1)]:
  send=AsyncMock(side_effect=[response(503),response(200)])
  monkeypatch.setattr(http,'get_client',lambda:SimpleNamespace(request=send))
  asyncio.run(http.request(method,'/api/test',retry_read_only=flag))
  assert send.await_count==count


def test_timeout_and_business_errors_are_not_replayed(monkeypatch):
 monkeypatch.setattr(http,'get_headers',lambda:{})
 for effect in [httpx.ReadTimeout('timeout'),response(400)]:
  send=AsyncMock(side_effect=effect if isinstance(effect,Exception) else None,return_value=effect)
  monkeypatch.setattr(http,'get_client',lambda:SimpleNamespace(request=send))
  result=asyncio.run(http.request('GET','/api/test'))
  assert result.startswith('❌') and send.await_count==1


def test_trace_preserves_partial_failure_and_pagination(monkeypatch):
 monkeypatch.setattr(trace,'get_container_details',AsyncMock(return_value=json.dumps({'data':{'ContainerStatusDetails':{'Container':{'Name':'BOX','InstanceId':'0004108000000002'}}}})))
 mock=AsyncMock(side_effect=[json.dumps([{'ContainerId':str(i)} for i in range(3)]),'Error: unsupported', '[]','[]','[]','[]'])
 monkeypatch.setattr(trace,'request',mock)
 result=json.loads(asyncio.run(trace.get_container_trace('BOX',limit=2)))
 assert result['sections']['current_family']['has_more']
 assert result['sections']['current_family']['next_offset']==2
 assert result['sections']['splits']['error']
 assert not result['requested_sections_complete'] and not result['complete']
 assert mock.await_count==6
 assert all(c.kwargs['retry_read_only'] for c in mock.call_args_list)


def test_trace_validates_id_and_section_before_queries(monkeypatch):
 mock=AsyncMock();monkeypatch.setattr(trace,'request',mock)
 monkeypatch.setattr(trace,'get_container_details',AsyncMock(return_value=json.dumps({'data':{'ContainerStatusDetails':{'Container':{'Name':'BOX','InstanceId':"bad' OR 1=1"}}}})))
 assert json.loads(asyncio.run(trace.get_container_trace('BOX')))['error']
 assert json.loads(asyncio.run(trace.get_container_trace('BOX',section='arbitrary')))['error']
 mock.assert_not_called()

def test_read_trace_does_not_load_rework_mutations():
 from agent.tool_routing import select_tools
 from core.tool_catalog import TOOL_POLICIES
 catalog=[{'function':{'name':name}} for name in TOOL_POLICIES]
 selected=select_tools(catalog,[{'role':'user','content':'追溯容器 BOX-0001 的父子、拆分合并和返工关联'}])
 names={x['function']['name'] for x in selected}
 assert 'get_container_trace' in names
 assert all(TOOL_POLICIES[n].effect=='read' for n in names)
 assert 'get_workflow_structure' not in names
