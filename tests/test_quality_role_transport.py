import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from core import http_client as http
from tools import quality_approval_authorization as auth


@pytest.mark.parametrize('status',[200,403])
def test_bound_session_is_copied_without_regenerating_or_logging_credentials(monkeypatch,caplog,status):
    headers={'Authorization':'Bearer FAKE_PRIVATE_BOUND_TRANSPORT','Cookie':'FAKE_PRIVATE_COOKIE'}
    async def request(*args,**kw):
        # Changing the source session after request construction cannot alter its copy.
        headers['Authorization']='Bearer FAKE_DIFFERENT_SESSION';headers['Cookie']='FAKE_DIFFERENT_COOKIE'
        assert kw['headers']['Authorization']=='Bearer FAKE_PRIVATE_BOUND_TRANSPORT'
        assert kw['headers']['Cookie']=='FAKE_PRIVATE_COOKIE'
        return httpx.Response(status,json={'data':'ok' if status==200 else 'denied'})
    client=SimpleNamespace(request=AsyncMock(side_effect=request))
    monkeypatch.setattr(http,'get_client',lambda:client)
    def forbidden():raise AssertionError('Do not replace the already verified session.')
    monkeypatch.setattr(http,'get_headers',forbidden)
    out=asyncio.run(http.request('POST','/api/SignApproval',body={'comments':'test'},base_url='https://example.invalid/Shopfloor',
        bound_headers=headers,response_formatter=json.dumps))
    client.request.assert_awaited_once()
    assert 'FAKE_PRIVATE' not in out and 'FAKE_PRIVATE' not in caplog.text


def test_rotating_cookie_preview_does_not_become_an_authorized_plan(monkeypatch):
    client=SimpleNamespace(request=AsyncMock(return_value=httpx.Response(200,json={'Employee':{'InstanceId':'0000000000000001'}},
        headers={'Set-Cookie':'privateSession=FAKE_NEW_COOKIE; HttpOnly; Secure'})))
    monkeypatch.setattr(http,'get_client',lambda:client)
    with pytest.raises(http.BoundSessionChanged) as exc:
        asyncio.run(http.request('POST','/api/SignApproval',base_url='https://example.invalid/Shopfloor',body={},
            bound_headers={'Authorization':'Bearer FAKE_BOUND_AUTH'},require_stable_session=True,response_formatter=json.dumps))
    assert 'FAKE' not in str(exc.value);client.request.assert_awaited_once()


@pytest.mark.parametrize('initial_cookie',[None,'FAKE_ORIGINAL_COOKIE'])
def test_real_httpx_merge_preserves_pinned_cookie_even_when_shared_jar_changes(monkeypatch,initial_cookie):
    seen=[]
    async def handler(request):
        seen.append(dict(cookie=request.headers.get('Cookie',''),authorization=request.headers.get('Authorization')))
        return httpx.Response(200,json={'ok':True})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(http,'get_client',lambda:client)
            monkeypatch.setattr(http,'get_headers',lambda:{'Authorization':'Bearer FAKE_SESSION_HEADER'})
            monkeypatch.setattr(auth,'CAMSTAR_SHOPFLOOR_BASE_URL','https://example.invalid/Shopfloor')
            if initial_cookie:client.cookies.set('privateSession',initial_cookie,domain='example.invalid',path='/')
            headers=auth.capture_headers()
            client.cookies.set('privateSession','FAKE_CHANGED_COOKIE',domain='example.invalid',path='/')
            await client.post('https://example.invalid/Shopfloor/api/SignApproval',headers=headers,json={})
            assert seen==[dict(cookie='' if initial_cookie is None else 'privateSession='+initial_cookie,authorization='Bearer FAKE_SESSION_HEADER')]
    asyncio.run(check())
