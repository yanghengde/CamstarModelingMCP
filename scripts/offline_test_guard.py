"""Opt-in pytest plugin: disallow HTTPX network transports during regression.

ASGI/TestClient and explicit mock transports remain usable. This is not an OS
network sandbox. Subprocess fault fixtures install their own HTTPX transport
guard and replay captured MES replies; other subprocess tests use local locks.
"""
import json
import os
from pathlib import Path

import httpx

ATTEMPTS = []
ORIGINALS = None


def pytest_configure(config):
    global ORIGINALS
    ATTEMPTS.clear()
    ORIGINALS = (httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request)
    def blocked(self, request):
        ATTEMPTS.append({'method':request.method,'host':request.url.host,
                         'test':os.environ.get('PYTEST_CURRENT_TEST','collection')})
        raise RuntimeError('Offline acceptance: real HTTPX network requests are disabled; use an explicit mock.')
    async def async_blocked(self, request):
        return blocked(self, request)
    httpx.HTTPTransport.handle_request = blocked
    httpx.AsyncHTTPTransport.handle_async_request = async_blocked


def pytest_sessionfinish(session, exitstatus):
    if ATTEMPTS:
        session.exitstatus = 1
    target = os.environ.get('CORE_TEST_NETWORK_REPORT')
    if target:
        Path(target).write_text(json.dumps({'blocked_attempt_count':len(ATTEMPTS),'attempts':ATTEMPTS},indent=2),encoding='utf-8')


def pytest_unconfigure(config):
    if ORIGINALS:
        httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request = ORIGINALS
