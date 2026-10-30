"""Lazy, separate-origin loopback input server sharing only the in-process vault."""
import asyncio
from contextlib import contextmanager,suppress
import socket
from pathlib import Path
from fastapi import FastAPI,Request
from fastapi.responses import FileResponse,JSONResponse
import uvicorn
from core.quality_signature_vault import VAULT

_enabled=False
_server=None
_task=None
_socket=None
_lock=None


class LocalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):yield
    def install_signal_handlers(self):pass


def input_app():
    from web.quality_signatures import router,local,SECURITY
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    app.include_router(router)
    @app.get('/static/{name}')
    async def asset(name:str,request:Request):
        try:local(request)
        except ValueError:return JSONResponse({'error':'签名页面来源不符。'},403,headers=SECURITY)
        if name not in ('quality-signature.js','quality-signature.css'):return JSONResponse({'error':'文件不存在。'},404,headers=SECURITY)
        return FileResponse(Path(__file__).resolve().parents[1]/'static'/name,headers=SECURITY)
    return app


def activate():
    global _enabled,_lock
    _enabled=True;_lock=asyncio.Lock()


async def _serve(server,sock):
    try:await server.serve(sockets=[sock])
    except SystemExit:raise ValueError('本地签名服务启动失败。') from None


async def ensure_running():
    global _server,_task,_socket
    if not _enabled:raise ValueError('安全签名页面仅在同一Web服务进程可用，不支持独立stdio或跨进程输入。')
    async with _lock:
        if _task is not None and not _task.done() and _server.started:return
        sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        try:
            sock.bind(('127.0.0.1',0));sock.setblocking(False)
            VAULT.enable('http://127.0.0.1:'+str(sock.getsockname()[1]))
            _server=LocalServer(uvicorn.Config(input_app(),log_level='warning',access_log=False,lifespan='off',proxy_headers=False,ws='none'))
            _socket=sock;_task=asyncio.create_task(_serve(_server,sock))
            for _ in range(250):
                if _server.started:return
                if _task.done():break
                await asyncio.sleep(.02)
            raise ValueError('本地安全签名页面未能启动。')
        except BaseException:
            if _task is not None and not _task.done():
                _task.cancel()
                with suppress(asyncio.CancelledError):await _task
            elif _task is not None:
                with suppress(Exception):_task.result()
            sock.close();VAULT.disable();raise


async def close():
    global _enabled,_server,_task,_socket,_lock
    _enabled=False
    if _server is not None:_server.should_exit=True
    try:
        if _task is not None:
            try:await asyncio.wait_for(_task,3)
            except (asyncio.TimeoutError,asyncio.CancelledError):
                _task.cancel()
                with suppress(asyncio.CancelledError):await _task
            except Exception:pass
    finally:
        if _socket is not None:_socket.close()
        VAULT.disable();_server=_task=_socket=_lock=None
