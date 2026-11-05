import ssl
from types import SimpleNamespace
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


@pytest.mark.parametrize('trust_env',[True,False])
def test_camstar_proxy_option_applies_to_both_channels_without_changing_signature_tls(monkeypatch,trust_env):
    calls=[]
    def client(**options):
        calls.append(options)
        return SimpleNamespace(is_closed=False)
    context=ssl.create_default_context()
    monkeypatch.setattr(http_client,'_client',None)
    monkeypatch.setattr(channel,'_client',None)
    monkeypatch.setattr(http_client,'CAMSTAR_TRUST_ENV',trust_env)
    monkeypatch.setattr(channel,'CAMSTAR_TRUST_ENV',trust_env)
    monkeypatch.setattr(channel,'tls_context',lambda:context)
    monkeypatch.setattr(http_client.httpx,'AsyncClient',client)
    assert http_client.get_client() is http_client.get_client()
    assert channel.get_client() is channel.get_client()
    assert len(calls)==2 and all(call['trust_env'] is trust_env for call in calls)
    assert calls[1]['verify'] is context and context.verify_mode==ssl.CERT_REQUIRED
    assert calls[1]['follow_redirects'] is False
