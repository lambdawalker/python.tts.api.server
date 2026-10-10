"""Transport-neutral domain errors. Never expose native tracebacks to callers."""

from pydantic import ValidationError

from .models import ErrorDetail

STATUS = {
    "malformed_request": 400,
    "authentication_required": 401,
    "forbidden": 403,
    "model_not_found": 404,
    "voice_unavailable": 404,
    "asset_not_found": 404,
    "job_not_found": 404,
    "not_found": 404,
    "guidance_revision_mismatch": 409,
    "idempotency_conflict": 409,
    "event_history_expired": 409,
    "alias_conflict": 409,
    "asset_expired": 410,
    "upload_too_large": 413,
    "queue_full": 429,
    "session_limit_reached": 429,
    "model_unavailable": 503,
    "server_unavailable": 503,
    "internal_error": 500,
}


class DomainError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        field: str | None = None,
        retryable: bool = False,
        guidance_url: str | None = None,
    ):
        self.detail = ErrorDetail(
            code=code, message=message, field=field, retryable=retryable, guidance_url=guidance_url
        )
        self.status = STATUS.get(code, 422)
        super().__init__(message)

    def payload(self):
        return {"error": self.detail.model_dump(exclude_none=True)}


def validation_error(exc: ValidationError) -> DomainError:
    first = exc.errors(include_input=False, include_context=False)[0]
    return DomainError("invalid_request", first["msg"], ".".join(map(str, first["loc"])))
