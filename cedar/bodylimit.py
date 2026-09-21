"""Request-body ceilings, enforced before a byte is parsed.

Uvicorn accepts a body of any size. Without a ceiling, one request with a
multi-gigabyte body — a JSON document, a multipart upload, a chunked stream
that never ends — is read into memory (or spooled to disk) before the route
gets to say no. This middleware rejects a declared Content-Length above the
limit outright, and counts the bytes of a streamed body as they arrive so an
undeclared one is stopped at the same line.
"""
from __future__ import annotations

import json

from fastapi import HTTPException

from .config import MAX_BODY_BYTES, MAX_UPLOAD_BYTES

# Multipart framing and the filename around a file at MAX_UPLOAD_BYTES.
_MULTIPART_SLACK = 1024 * 1024


def message(limit: int) -> dict:
    return {"detail": f"Request body is too large (limit {limit // (1024 * 1024)} MB)."}


class BodyTooLarge(HTTPException):
    """Raised from inside the route's body read. An HTTPException on purpose:
    FastAPI folds any *other* exception raised while reading a body into a
    generic 400, and this one has to come out as a 413."""

    def __init__(self, limit: int):
        super().__init__(413, message(limit)["detail"], headers={"Connection": "close"})
        self.limit = limit


def limit_for(scope: dict) -> int:
    """The body ceiling for a request: the upload limit for the file route,
    the (much smaller) JSON limit for everything else."""
    if scope.get("method") == "POST" and scope.get("path") == "/api/documents":
        return MAX_UPLOAD_BYTES + _MULTIPART_SLACK
    return MAX_BODY_BYTES


class BodyLimitMiddleware:
    """Pure ASGI, so it sits outside every other layer."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope)

        declared = -1
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
        if declared > limit:
            await _reject(send, limit)
            return

        seen = 0

        async def counted():
            nonlocal seen
            event = await receive()
            if event["type"] == "http.request":
                seen += len(event.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge(limit)  # → 413 via FastAPI's handler
            return event

        await self.app(scope, counted, send)


async def _reject(send, limit: int) -> None:
    body = json.dumps(message(limit)).encode("utf-8")
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})
