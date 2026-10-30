import ssl
import pytest
from core import signature_channel as channel,http_client


def test_signature_tls_is_verified_and_channel_context_is_scoped(monkeypatch):
    for key in ('CAMSTAR_BASE_URL','CAMSTAR_QUERY_BASE_URL','CAMSTAR_SHOPFLOOR_BASE_URL'):monkeypatch.setattr(channel,key,'https://example.invalid/service')
    context=channel.tls_context();assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
    marker=object();monkeypatch.setattr(channel,'get_client',lambda:marker)
    before=http_client._request_client.get()
    with channel.verified_channel():assert http_client.get_client() is marker
    assert http_client._request_client.get() is before


@pytest.mark.parametrize('url',['http://example.invalid','https://user:pass@example.invalid','file:///etc/file'])
def test_signature_transport_has_no_insecure_fallback(monkeypatch,url):
    monkeypatch.setattr(channel,'CAMSTAR_SHOPFLOOR_BASE_URL',url)
    with pytest.raises(ValueError):channel.tls_context()
