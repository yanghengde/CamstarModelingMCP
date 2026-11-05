"""Verified TLS for signature-related reads and submissions, no insecure fallback."""
from contextlib import contextmanager
import os
import ssl
from urllib.parse import urlsplit
import httpx
from config import CAMSTAR_BASE_URL,CAMSTAR_QUERY_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL,CAMSTAR_TIMEOUT,CAMSTAR_TRUST_ENV
from core import http_client

_client=None


def tls_context():
    for url in (CAMSTAR_BASE_URL,CAMSTAR_QUERY_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL):
        parts=urlsplit(url)
        if parts.scheme!='https' or not parts.hostname or parts.username or parts.password:
            raise ValueError('电子签名的MES查询、配置及提交通道必须使用HTTPS，不允许凭据URL或不安全回退。')
    try:context=ssl.create_default_context(cafile=os.getenv('CAMSTAR_SIGNATURE_CA_FILE') or None)
    except (OSError,ssl.SSLError):raise ValueError('电子签名的受信任证书配置不可用。') from None
    return context


def get_client():
    global _client
    if _client is None or _client.is_closed:
        context=tls_context()
        try:_client=httpx.AsyncClient(verify=context,timeout=CAMSTAR_TIMEOUT,follow_redirects=False,trust_env=CAMSTAR_TRUST_ENV)
        except Exception:raise ValueError('电子签名安全通道初始化失败，不进行不安全回退。') from None
    return _client


@contextmanager
def verified_channel():
    client=get_client();token=http_client._request_client.set(client)
    try:yield client
    finally:http_client._request_client.reset(token)


async def close_client():
    global _client
    if _client is not None:await _client.aclose()
    _client=None
