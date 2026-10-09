"""HTTP API factory. Real model adapters are injected by engine deployments."""

import asyncio
import hashlib
import json
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
from fastapi import FastAPI, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .adapter import Adapter
from .auth import AccessMiddleware
from .errors import DomainError
from .models import (
    Asset,
    ErrorResponse,
    JobStatus,
    SpeechRequest,
    Voice,
    VoiceConversionRequest,
    VoiceDesignRequest,
    VoiceRegistration,
)
from .service import TERMINAL, Service, Settings


def create_app(adapters: list[Adapter], settings: Settings | None = None, *, enable_mcp=False):
    settings = settings or Settings()
    service = Service(adapters, settings)
    manager = None
    if enable_mcp:
        try:
            from .mcp import create_mcp

            manager = create_mcp(service)
        except ImportError as exc:
            service.close()
            raise RuntimeError("Install tts-api-server[mcp] to enable MCP.") from exc

    @asynccontextmanager
    async def lifespan(app):
        service.start()
        try:
            async with AsyncExitStack() as stack:
                if manager is not None:
                    await stack.enter_async_context(manager.run())
                yield
        finally:
            await anyio.to_thread.run_sync(service.close)

    app = FastAPI(
        title="Shared TTS API",
        version="1.0",
        lifespan=lifespan,
        responses={
            # Keep the contract independent of Python's HTTPStatus phrases,
            # which changed for 413 and 422 in Python 3.13.
            status: {"model": ErrorResponse, "description": description}
            for status, description in {
                400: "Bad Request",
                401: "Unauthorized",
                403: "Forbidden",
                404: "Not Found",
                409: "Conflict",
                410: "Gone",
                413: "Request Entity Too Large",
                422: "Unprocessable Entity",
                429: "Too Many Requests",
                503: "Service Unavailable",
            }.items()
        },
    )
    app.state.service = service
    app.add_middleware(AccessMiddleware, settings=settings)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(exc.payload(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        first = exc.errors()[0]
        code = "malformed_request" if first["type"] == "json_invalid" else "invalid_request"
        field = ".".join(map(str, first["loc"][1:]))
        error = DomainError(code, first["msg"], field or None)
        return JSONResponse(error.payload(), status_code=error.status)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        code = "not_found" if exc.status_code == 404 else "malformed_request"
        return JSONResponse(
            DomainError(code, str(exc.detail)).payload(), status_code=exc.status_code
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse(
            DomainError("internal_error", "The operation could not be completed.").payload(),
            status_code=500,
        )

    def cached(request, data):
        etag = '"' + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest() + '"'
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(data, headers=headers)

    @app.get("/v1/models")
    def models():
        return service.list_models()

    @app.get("/v1/capabilities")
    def capabilities(request: Request, model: str | None = None):
        return cached(request, service.capabilities(model))

    @app.get("/v1/guidance/{feature}")
    def guidance(request: Request, feature: str, model: str | None = None):
        return cached(request, service.guidance(feature, model))

    @app.post("/v1/assets", response_model=Asset, status_code=201)
    async def upload(request: Request, file: UploadFile):
        try:
            data = await file.read(settings.max_upload_bytes + 1)
            return await anyio.to_thread.run_sync(service.upload, request.state.caller, data)
        finally:
            await file.close()

    @app.get("/v1/assets/{identifier}", response_class=FileResponse)
    def download(request: Request, identifier: str):
        metadata, path = service.asset(request.state.caller, identifier)
        return FileResponse(
            path,
            media_type=metadata["mime_type"],
            filename=identifier + "." + metadata["format"],
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/v1/voices")
    def voices(request: Request, model: str | None = None):
        return {"voices": service.list_voices(request.state.caller, model)}

    @app.post("/v1/voices", response_model=Voice, status_code=201)
    def register(request: Request, body: VoiceRegistration):
        return service.register_voice(request.state.caller, body.model_dump(exclude_none=True))

    @app.delete("/v1/voices/{identifier}", status_code=204)
    def delete(request: Request, identifier: str):
        service.delete_voice(request.state.caller, identifier)
        return Response(status_code=204)

    @app.post("/v1/speech/validate")
    def validate(request: Request, body: SpeechRequest):
        return service.validate_speech(request.state.caller, body.model_dump(exclude_none=True))

    @app.post("/v1/speech", response_model=JobStatus, status_code=202)
    def speech(request: Request, body: SpeechRequest):
        return service.submit(
            request.state.caller,
            "tts",
            body.model_dump(exclude_none=True),
            request.headers.get("idempotency-key"),
        )

    @app.post("/v1/voice-designs", response_model=JobStatus, status_code=202)
    def design(request: Request, body: VoiceDesignRequest):
        return service.submit(
            request.state.caller,
            "voice_design",
            body.model_dump(exclude_none=True),
            request.headers.get("idempotency-key"),
        )

    @app.post("/v1/voice-conversions", response_model=JobStatus, status_code=202)
    def convert(request: Request, body: VoiceConversionRequest):
        return service.submit(
            request.state.caller,
            "voice_conversion",
            body.model_dump(exclude_none=True),
            request.headers.get("idempotency-key"),
        )

    @app.get("/v1/jobs/{identifier}", response_model=JobStatus)
    def job(request: Request, identifier: str):
        return service.get_job(request.state.caller, identifier)

    @app.post("/v1/jobs/{identifier}/cancel", response_model=JobStatus)
    def cancel(request: Request, identifier: str):
        return service.cancel_job(request.state.caller, identifier)

    @app.get("/v1/jobs/{identifier}/events", response_class=StreamingResponse)
    async def events(request: Request, identifier: str):
        value = request.headers.get("last-event-id", "0")
        if not value.isascii() or not value.isdigit() or len(value) > 18:
            raise DomainError("invalid_request", "Last-Event-ID must be a nonnegative integer.")
        cursor = int(value)
        owner = request.state.caller
        first = await anyio.to_thread.run_sync(service.events, owner, identifier, cursor)

        async def stream():
            nonlocal cursor
            batch = first
            last_heartbeat = asyncio.get_running_loop().time()
            while True:
                for seq, data in batch:
                    cursor = seq
                    encoded = json.dumps(data, ensure_ascii=False)
                    yield f"id: {seq}\nevent: status\ndata: {encoded}\n\n"
                    if data["status"] in TERMINAL:
                        return
                if await request.is_disconnected():
                    return
                status = await anyio.to_thread.run_sync(service.get_job, owner, identifier)
                # Fetch events before closing: terminal state may have raced the prior batch.
                try:
                    batch = await anyio.to_thread.run_sync(
                        service.events, owner, identifier, cursor
                    )
                except DomainError:
                    return  # Reconnect returns the structured 409; polling remains available.
                if batch:
                    continue
                if status["status"] in TERMINAL or service.stopping:
                    return
                if asyncio.get_running_loop().time() - last_heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = asyncio.get_running_loop().time()
                await asyncio.sleep(0.1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if manager is not None:

        class MCPEndpoint:
            async def __call__(self, scope, receive, send):
                await manager.handle_request(scope, receive, send)

        app.router.routes.append(Route("/mcp", MCPEndpoint(), methods=["GET", "POST", "DELETE"]))
    return app
