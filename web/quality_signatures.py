"""Loopback-only human input; never dispatch credentials through the chat/LLM."""
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import APIRouter,Request
from fastapi.responses import HTMLResponse,JSONResponse
from core.quality_signature_vault import VAULT

router=APIRouter()
COOKIE='quality_signature_browser'
SECURITY={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff',
    'Cross-Origin-Opener-Policy':'same-origin','Cross-Origin-Resource-Policy':'same-origin',
    'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}


def local(request,write=False):
    try:peer=ipaddress.ip_address(request.client.host)
    except (ValueError,AttributeError):raise ValueError('签名页面仅供本机浏览器使用。') from None
    origin=VAULT.origin
    if not origin or not peer.is_loopback or any(k in request.headers for k in ('forwarded','x-forwarded-for','x-forwarded-host','x-forwarded-proto')):
        raise ValueError('签名页面未启用或不是本机直连。')
    expected=urlsplit(origin)
    if request.headers.get('host','').lower()!=expected.netloc.lower() or request.url.scheme!=expected.scheme:
        raise ValueError('签名页面来源不符。')
    if write and request.headers.get('origin')!=origin:raise ValueError('请从本地签名页面操作。')


def reply(data,status=200):return JSONResponse(data,status_code=status,headers=SECURITY)


@router.get('/quality-signature',response_class=HTMLResponse)
async def page(request:Request):
    try:local(request)
    except ValueError as exc:return reply({'error':str(exc)},403)
    return HTMLResponse((Path(__file__).resolve().parents[1]/'static/quality-signature.html').read_text(encoding='utf8'),headers=SECURITY)


@router.post('/api/quality-signature/open')
async def open_input(request:Request):
    try:
        local(request,True)
        data,cookie=VAULT.bootstrap(request.headers.get('x-signature-challenge',''),request.cookies.get(COOKIE))
        result=reply(data);result.set_cookie(COOKIE,cookie,max_age=300,httponly=True,samesite='strict',secure=VAULT.origin.startswith('https:'),path='/api/quality-signature')
        return result
    except ValueError as exc:return reply({'error':str(exc)},403)


@router.post('/api/quality-signature/capture')
async def capture(request:Request):
    # No Pydantic password model: framework validation must not echo secret input.
    body=None;data=None
    try:
        local(request,True)
        if request.headers.get('content-type','').split(';')[0]!='application/json':raise ValueError('签名输入格式不符。')
        body=bytearray()
        async for part in request.stream():
            if len(body)+len(part)>32768:raise ValueError('签名输入超过允许长度。')
            body.extend(part)
        try:data=json.loads(body)
        except (ValueError,UnicodeError):raise ValueError('签名输入格式不符。') from None
        if not isinstance(data,dict) or set(data) not in ({'password','approved'},{'signatures','approved'}):raise ValueError('签名输入字段不符。')
        multiple='signatures' in data
        result=(VAULT.capture_many if multiple else VAULT.capture)(request.headers.get('x-signature-challenge',''),request.cookies.get(COOKIE),
            request.headers.get('x-signature-csrf',''),data['signatures'] if multiple else data['password'],data['approved'])
        return reply(result)
    except ValueError as exc:return reply({'error':str(exc)},400)
    except Exception:return reply({'error':'签名输入未保存，请核实页面状态。'},400)
    finally:
        if data is not None and isinstance(data,dict):data.clear()
        if body is not None:
            for n in range(len(body)):body[n]=0
            body.clear()


@router.post('/api/quality-signature/cancel')
async def cancel(request:Request):
    try:
        local(request,True)
        VAULT.cancel(request.headers.get('x-signature-challenge',''),request.cookies.get(COOKIE),request.headers.get('x-signature-csrf',''))
        return reply({'state':'revoked'})
    except ValueError as exc:return reply({'error':str(exc)},400)
