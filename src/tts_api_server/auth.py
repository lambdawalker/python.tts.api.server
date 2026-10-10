"""One ASGI authorization boundary for HTTP, MCP and asset downloads."""

import hmac
from contextvars import ContextVar

from starlette.responses import JSONResponse

caller: ContextVar[str] = ContextVar("tts_caller", default="local")


class AccessMiddleware:
    def __init__(self, app, settings, sessions):
        self.app, self.settings, self.sessions = app, settings, sessions

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        owner = "local"
        bootstrap = (
            self.settings.anonymous_sessions
            and scope["method"] == "POST"
            and scope["path"] == "/v1/sessions"
        )
        if (self.settings.tokens or self.settings.anonymous_sessions) and not bootstrap:
            authorization = headers.get(b"authorization", b"").decode("latin1")
            supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
            owner = (
                self.sessions.authenticate(supplied)
                if self.settings.anonymous_sessions
                else next(
                    (
                        v
                        for k, v in self.settings.tokens.items()
                        if hmac.compare_digest(k.encode(), supplied.encode())
                    ),
                    None,
                )
            )
            if owner is None:
                response = JSONResponse(
                    {
                        "error": {
                            "code": "authentication_required",
                            "message": "A valid bearer token is required.",
                            "retryable": False,
                        }
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                return await response(scope, receive, send)
        # Read a bounded body before multipart parsing can spool unbounded uploads.
        limit = (
            self.settings.max_upload_bytes + 65536
            if scope["path"] == "/v1/assets"
            else self.settings.max_request_bytes
        )
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                response = JSONResponse(
                    {
                        "error": {
                            "code": "upload_too_large",
                            "message": "Request body exceeds the configured limit.",
                            "retryable": False,
                        }
                    },
                    status_code=413,
                )
                return await response(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        scope.setdefault("state", {})["caller"] = owner
        token = caller.set(owner)
        try:
            await self.app(scope, bounded_receive, send)
        finally:
            caller.reset(token)
