"""Bound request bodies while streaming, before multipart spooling can fill a disk."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from .config import get_settings


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        upload = scope["path"].rstrip("/") in ("/submit", "/admin/ops/documents", "/api/requests")
        limit = get_settings().max_upload_bytes + 65536 if upload else 1024 * 1024
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Invalid content length"}, status_code=400)(scope, receive, send)
        if declared > limit or declared < 0:
            return await JSONResponse({"detail": "Request body is too large"}, status_code=413)(scope, receive, send)
        total = 0

        async def bounded_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise HTTPException(413, "Request body is too large")
            return message

        return await self.app(scope, bounded_receive, send)
