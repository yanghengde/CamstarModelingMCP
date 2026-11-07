"""Bound multipart bytes before Starlette spools them to disk, including chunked bodies."""
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse
from config import UPLOAD_MAX_BYTES


class UploadLimitMiddleware:
    def __init__(self, app, limit=UPLOAD_MAX_BYTES):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') != '/api/upload':
            return await self.app(scope, receive, send)
        # Allow bounded multipart envelope overhead beyond the file limit.
        maximum = self.limit + 65536
        headers = dict(scope.get('headers', []))
        try:
            declared = int(headers.get(b'content-length', b'0'))
        except ValueError:
            declared = 0
        if declared > maximum:
            return await JSONResponse({'detail': '上传请求超过大小限制。'}, status_code=413)(scope, receive, send)
        total, exceeded = 0, False

        async def bounded_receive():
            nonlocal total, exceeded
            message = await receive()
            total += len(message.get('body', b''))
            if total > maximum:
                exceeded = True
                # The parser closes its temporary files on MultiPartException.
                raise MultiPartException('上传请求超过大小限制。')
            return message

        async def bounded_send(message):
            if exceeded and message['type'] == 'http.response.start':
                message = {**message, 'status': 413}
            await send(message)

        await self.app(scope, bounded_receive, bounded_send)
