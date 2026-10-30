"""Process-local, bounded, single-use human signature capture. No persistence."""
from contextlib import contextmanager
from dataclasses import dataclass,field
import secrets
import threading
import time
from urllib.parse import urlsplit


def matches(expected,supplied):
    return isinstance(supplied,str) and supplied.isascii() and secrets.compare_digest(expected,supplied)


@dataclass(repr=False)
class Credential:
    value:bytearray=field(repr=False)
    expires:float
    clock:object=field(repr=False)

    def valid(self):
        if not self.value or self.clock()>=self.expires:raise ValueError('签名输入已过期或已使用，必须重新准备并输入。')

    def password(self):
        self.valid()
        return self.value.decode('utf-8')

    def close(self):
        for n in range(len(self.value)):self.value[n]=0
        self.value.clear()


class SignatureVault:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.lock=threading.RLock();self.entries={};self.origin=None

    def enable(self,origin):
        parts=urlsplit(origin)
        try:port=parts.port
        except ValueError:raise ValueError('签名输入页面端口无效。') from None
        if (parts.scheme not in ('http','https') or parts.hostname not in ('127.0.0.1','localhost','::1')
            or port==0 or parts.username or parts.password or parts.path not in ('','/') or parts.query or parts.fragment):
            raise ValueError('签名输入页面仅允许明确的本机回环地址。')
        with self.lock:self.origin=origin.rstrip('/')

    def disable(self):
        with self.lock:
            for e in self.entries.values():self._wipe(e)
            self.entries.clear();self.origin=None

    def _wipe(self,e):
        value=e.pop('secret',None)
        if value is not None:
            for n in range(len(value)):value[n]=0
            value.clear()

    def _prune(self):
        for e in self.entries.values():
            if self.clock()>=e['expires'] and e['state'] not in ('consumed','revoked','expired'):
                self._wipe(e);e['state']='expired'

    def ensure_enabled(self):
        if not self.origin:raise ValueError('安全签名页面未在同一服务进程启用；不能在聊天或MCP参数中输入口令。')

    def expire(self):
        with self.lock:self._prune()

    def create(self,plan_id,digest,display):
        with self.lock:
            self.ensure_enabled();self._prune()
            # Remove terminal/expired metadata before accepting more challenges.
            for key in list(self.entries):
                if self.entries[key]['state'] in ('consumed','revoked','expired'):self.entries.pop(key)
            if len(self.entries)>=100:raise ValueError('签名输入请求已达上限，请完成或取消已有请求。')
            token=secrets.token_urlsafe(32)
            self.entries[token]=dict(plan_id=plan_id,digest=digest,display=display,state='awaiting_input',expires=self.clock()+300,
                browser=None,csrf=None,reference=None)
            return self.origin+'/quality-signature#'+token

    def bootstrap(self,token,browser=None):
        with self.lock:
            e=self._entry(token)
            if e['state'] not in ('awaiting_input','captured'):raise ValueError('签名输入请求已失效。')
            if e['browser'] is not None and not matches(e['browser'],browser):raise ValueError('签名页面已绑定其它浏览器。')
            known=browser and any(x['browser']==browser for x in self.entries.values())
            e['browser']=e['browser'] or (browser if known else secrets.token_urlsafe(32));e['csrf']=secrets.token_urlsafe(32)
            return dict(display=e['display'],state=e['state'],csrf=e['csrf'],expires_in=max(0,int(e['expires']-self.clock()))),e['browser']

    def _entry(self,token):
        self._prune();e=self.entries.get(token)
        if e is None:raise ValueError('签名输入请求不存在或进程已重启，请重新准备计划。')
        return e

    def _browser(self,token,browser,csrf):
        e=self._entry(token)
        if (not e['browser'] or not e['csrf'] or not matches(e['browser'],browser)
            or not matches(e['csrf'],csrf)):raise ValueError('签名页面校验失败。')
        return e

    def capture(self,token,browser,csrf,password,approved):
        # No coercion or exception containing the supplied value.
        try:valid=isinstance(password,str) and bool(password) and '\x00' not in password and len(password.encode('utf-16-le'))//2<=128
        except UnicodeError:valid=False
        if not valid or approved is not True:raise ValueError('须核对并确认此计划，且仅在本页输入有效签名口令。')
        with self.lock:
            e=self._browser(token,browser,csrf)
            if e['state']!='awaiting_input':raise ValueError('签名输入已经使用或失效，不能覆盖。')
            e.update(secret=bytearray(password.encode('utf-8')),state='captured',reference=secrets.token_hex(16),
                expires=min(e['expires'],self.clock()+120))
            return dict(state='captured',capture_reference=e['reference'],expires_in=max(0,int(e['expires']-self.clock())),
                note='仅授权已展示计划的一次提交；返回聊天确认该计划即可。口令不会进入聊天。')

    def cancel(self,token,browser,csrf):
        with self.lock:
            e=self._browser(token,browser,csrf)
            if e['state']=='consumed':raise ValueError('签名输入已领取，不能撤销已尝试事务。')
            self._wipe(e);e['state']='revoked'

    def status(self,plan_id,digest):
        with self.lock:
            self._prune()
            e=next((e for e in self.entries.values() if e['plan_id']==plan_id and e['digest']==digest),None)
            return dict(required=True,state=e['state'] if e else 'unavailable',capture_reference=e['reference'] if e else None,
                expires_in=max(0,int(e['expires']-self.clock())) if e else 0)

    @contextmanager
    def claim(self,plan_id,digest):
        with self.lock:
            self._prune();e=next((e for e in self.entries.values() if e['plan_id']==plan_id and e['digest']==digest),None)
            if e is None or e['state']!='captured':raise ValueError('该计划没有可用的一次性签名输入；过期、取消、重启或已使用均须重新准备。')
            value=e.pop('secret');e['state']='consumed'
            credential=Credential(value,e['expires'],self.clock)
        try:yield credential
        finally:credential.close()


VAULT=SignatureVault()
