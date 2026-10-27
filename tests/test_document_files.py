import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from tools import document_files as d, get_tool_func
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools


@pytest.fixture
def env(monkeypatch,tmp_path):
    monkeypatch.setattr(d,'DOWNLOAD_DIR',tmp_path/'downloads')
    monkeypatch.setattr(d,'CAMSTAR_BASE_URL','https://mes.test/Modeling')
    monkeypatch.setattr(d,'get_headers',lambda:{'Authorization':'Bearer test'})
    resolve=AsyncMock(return_value={'Name':'Doc.txt','Revision':'1','InstanceID':'0000000000000001',
                                   'Identifier':'https://external.invalid/untrusted'})
    monkeypatch.setattr(d.ReadContext,'resolve',resolve)
    return resolve,tmp_path


def run():return json.loads(asyncio.run(d.download_document_file('Doc.txt','1')))


def install(monkeypatch,response):
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.headers['Authorization']=='Bearer test'
        assert str(request.url)=='https://mes.test/Modeling/api/Download/Doc.txt/1'
        return response
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(d,'get_client',lambda:client)
    return calls


def test_authenticated_download_and_safe_local_name(env,monkeypatch):
    body=b'Fixture instruction only.'
    calls=install(monkeypatch,httpx.Response(200,content=body,headers={
        'Content-Type':'text/plain','Content-Disposition':'attachment; filename="../../evil.cmd"'}))
    out=run()
    assert out['complete'] and out['access_verified'] and not out['content_executed']
    assert out['sha256']==hashlib.sha256(body).hexdigest()
    path=Path(out['local_path']);assert path.parent==env[1]/'downloads' and path.suffix=='.txt'
    assert path.read_bytes()==body and len(calls)==1
    assert get_tool_func('download_document_file') and TOOL_POLICIES['download_document_file'].effect=='read'


@pytest.mark.parametrize('status',[401,403,404,302])
def test_denial_or_redirect_never_claims_access(env,monkeypatch,status):
    calls=install(monkeypatch,httpx.Response(status,headers={'Location':'https://evil.invalid/file'}))
    out=run()
    assert not out['access_verified'] and not out['complete'] and 'local_path' not in out
    assert len(calls)==1


@pytest.mark.parametrize('mode',['header_size','actual_size','empty','missing_header'])
def test_incomplete_or_oversize_file_removed(env,monkeypatch,mode):
    monkeypatch.setattr(d,'MAX_BYTES',4)
    headers={'Content-Disposition':'attachment; filename=x'}
    body=b'x'
    if mode=='header_size':headers['Content-Length']='10'
    if mode=='actual_size':body=b'12345'
    if mode=='empty':body=b''
    if mode=='missing_header':headers={}
    response=httpx.Response(200,content=body,headers=headers)
    if mode=='actual_size':response.headers.pop('Content-Length',None)
    install(monkeypatch,response)
    out=run()
    assert not out['complete'] and not out['download_endpoint_verified']
    assert not list((env[1]/'downloads').glob('*'))


@pytest.mark.parametrize('name',['..','../secret','x\\secret','x%2Fsecret'])
def test_unsafe_name_rejected_before_any_lookup(env,name):
    out=json.loads(asyncio.run(d.download_document_file(name,'1')))
    assert 'error' in out and not out['complete']
    env[0].assert_not_called()


def test_cancelled_stream_removes_partial_file(env,monkeypatch):
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'partial'
            raise asyncio.CancelledError()
    install(monkeypatch,httpx.Response(200,stream=Interrupted(),headers={'Content-Disposition':'attachment; filename=test.txt'}))
    with pytest.raises(asyncio.CancelledError):run()
    assert not list((env[1]/'downloads').glob('*'))


def test_document_download_query_loads_only_read_tools():
    tools=[{'function':{'name':n}} for n in TOOL_POLICIES]
    selected=select_tools(tools,[{'role':'user','content':'下载这份文档 Doc.txt 版本1'}])
    names={t['function']['name'] for t in selected}
    assert 'download_document_file' in names
    assert all(TOOL_POLICIES[n].effect=='read' for n in names)
