"""Small, bounded HTTP request and validation-error boundaries."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BODY_TOO_LARGE_RESPONSE = {
    "detail": {
        "code": "request_body_too_large",
        "message": "Request body exceeds the allowed size.",
    }
}
VALIDATION_FAILED_RESPONSE = {
    "detail": {
        "code": "request_validation_failed",
        "message": "Request validation failed.",
    }
}


class ApiRequestBodyLimitMiddleware:
    """Buffer API writes up to a small limit before request parsing begins."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._guards(scope):
            await self.app(scope, receive, send)
            return

        declared_size = self._content_length(scope)
        if declared_size is not None and declared_size > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        messages: list[Message] = []
        received_size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break

            received_size += len(message.get("body", b""))
            if received_size > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        next_message = 0

        async def replay_receive() -> Message:
            nonlocal next_message
            if next_message < len(messages):
                message = messages[next_message]
                next_message += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    def _guards(scope: Scope) -> bool:
        if scope["type"] != "http" or scope["method"] not in WRITE_METHODS:
            return False
        path = scope.get("path")
        return isinstance(path, str) and (path == "/api" or path.startswith("/api/"))

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        raw_values = [value for name, value in scope["headers"] if name == b"content-length"]
        if len(raw_values) != 1:
            return None
        try:
            value = int(raw_values[0])
        except ValueError:
            return None
        return value if value >= 0 else None

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(BODY_TOO_LARGE_RESPONSE, status_code=413)
        await response(scope, receive, send)
