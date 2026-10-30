import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from web import quality_signatures as web
from core.quality_signature_vault import SignatureVault

ORIGIN='http://127.0.0.1:8030'


@pytest.fixture
def sample(monkeypatch):
    vault=SignatureVault();vault.enable(ORIGIN)
    token=vault.create('plan','digest',{'对象':'<script>UNTRUSTED</script>','实际员工':'Test signer'}).split('#')[1]
    monkeypatch.setattr(web,'VAULT',vault)
    app=FastAPI();app.include_router(web.router)
    with TestClient(app,base_url=ORIGIN,client=('127.0.0.1',52100)) as client:
        yield vault,token,client
    vault.disable()


def open_input(sample):
    vault,token,client=sample
    result=client.post('/api/quality-signature/open',headers={'Origin':ORIGIN,'X-Signature-Challenge':token})
    assert result.status_code==200,result.text
    return {'Origin':ORIGIN,'X-Signature-Challenge':token,'X-Signature-CSRF':result.json()['csrf']}


def test_capture_stays_outside_chat_and_never_echoes_password(sample):
    vault,token,client=sample;headers=open_input(sample)
    result=client.post('/api/quality-signature/capture',headers=headers,json={'password':'PRIVATE_SIGNATURE_SENTINEL','approved':True})
    assert result.status_code==200 and result.json()['state']=='captured'
    assert 'PRIVATE_SIGNATURE_SENTINEL' not in result.text
    assert result.headers['cache-control']=='no-store'
    with vault.claim('plan','digest') as credential:assert credential.password()=='PRIVATE_SIGNATURE_SENTINEL'


@pytest.mark.parametrize('mode',['cross_origin','no_origin','forwarded','wrong_host','wrong_csrf','unknown_challenge','unexpected_field','missing_confirmation','malformed','too_large'])
def test_invalid_sensitive_requests_return_only_controlled_errors(sample,mode):
    vault,token,client=sample;headers=open_input(sample)
    body={'password':'PRIVATE_SIGNATURE_SENTINEL','approved':True};raw=None
    if mode=='cross_origin':headers['Origin']='https://evil.example'
    elif mode=='no_origin':headers.pop('Origin')
    elif mode=='forwarded':headers['X-Forwarded-For']='127.0.0.1'
    elif mode=='wrong_host':headers['Host']='evil.example'
    elif mode=='wrong_csrf':headers['X-Signature-CSRF']='invalid'
    elif mode=='unknown_challenge':headers['X-Signature-Challenge']='unknown'
    elif mode=='unexpected_field':body['plan_id']='other'
    elif mode=='missing_confirmation':body['approved']=False
    elif mode=='malformed':raw=b'{"password":"PRIVATE_SIGNATURE_SENTINEL"'
    else:raw=b'PRIVATE_SIGNATURE_SENTINEL'*300
    if raw is not None:headers['Content-Type']='application/json';result=client.post('/api/quality-signature/capture',headers=headers,content=raw)
    else:result=client.post('/api/quality-signature/capture',headers=headers,json=body)
    assert result.status_code>=400 and 'PRIVATE_SIGNATURE_SENTINEL' not in result.text
    assert vault.status('plan','digest')['state']=='awaiting_input'


def test_remote_peer_and_frame_embedding_are_blocked(sample):
    vault,token,client=sample
    page=client.get('/quality-signature');assert page.status_code==200
    assert "frame-ancestors 'none'" in page.headers['content-security-policy'] and page.headers['referrer-policy']=='no-referrer'
    assert 'type="password"' in page.text and 'localStorage' not in page.text
    app=FastAPI();app.include_router(web.router)
    with TestClient(app,base_url=ORIGIN,client=('192.0.2.1',50200)) as remote:assert remote.get('/quality-signature').status_code==403


def test_cookie_is_http_only_strict_and_cancel_wipes_input(sample):
    vault,token,client=sample
    response=client.post('/api/quality-signature/open',headers={'Origin':ORIGIN,'X-Signature-Challenge':token})
    cookie=response.headers['set-cookie'];assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    headers={'Origin':ORIGIN,'X-Signature-Challenge':token,'X-Signature-CSRF':response.json()['csrf']}
    client.post('/api/quality-signature/capture',headers=headers,json={'password':'private','approved':True})
    value=vault.entries[token]['secret']
    assert client.post('/api/quality-signature/cancel',headers=headers).json()['state']=='revoked' and not value


def test_public_api_schema_does_not_offer_password_model(sample):
    vault,token,client=sample
    schema=client.get('/openapi.json').json()
    operation=schema['paths']['/api/quality-signature/capture']['post']
    assert 'requestBody' not in operation


def test_private_signature_service_has_no_chat_schema_or_arbitrary_assets(sample):
    from core.quality_signature_ui import input_app
    with TestClient(input_app(),base_url=ORIGIN,client=('127.0.0.1',52101)) as client:
        assert client.get('/quality-signature').status_code==200
        assert client.get('/static/quality-signature.js').status_code==200
        for path in ('/api/chat','/api/agent','/docs','/openapi.json','/static/app.js','/static/index.html'):
            assert client.get(path).status_code==404
