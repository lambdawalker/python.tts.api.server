"""Strict wire inputs; shared by HTTP, MCP and application services."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="after")
    def finite_json(self):
        try:
            json.dumps(self.model_dump(), allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("Inputs must contain finite JSON values") from exc
        return self


class VoiceSelector(Request):
    id: str | None = Field(default=None, min_length=1)
    alias: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.id is None) == (self.alias is None):
            raise ValueError("Supply exactly one voice id or alias")
        return self


class OutputOptions(Request):
    format: str = "wav"
    sample_rate: int | None = Field(default=None, gt=0)


class SpeechRequest(Request):
    text: str = Field(min_length=1)
    model: str = "default"
    voice: VoiceSelector | None = None
    language: str | None = None
    instructions: str | None = None
    output: OutputOptions = Field(default_factory=OutputOptions)
    guidance_revision: str | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class Reference(Request):
    asset_id: str = Field(min_length=1)
    transcript: str | None = None


class VoiceRegistration(Request):
    references: list[Reference] = Field(min_length=1)
    model: str = "default"
    alias: str | None = Field(default=None, min_length=1)


class VoiceDesignRequest(Request):
    description: str = Field(min_length=1)
    preview_text: str = Field(min_length=1)
    model: str = "default"
    language: str | None = None
    output: OutputOptions = Field(default_factory=OutputOptions)
    extensions: dict[str, Any] = Field(default_factory=dict)


class VoiceConversionRequest(Request):
    source_asset_id: str = Field(min_length=1)
    voice: VoiceSelector
    model: str = "default"
    output: OutputOptions = Field(default_factory=OutputOptions)
    extensions: dict[str, Any] = Field(default_factory=dict)


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None
    retryable: bool = False
    guidance_url: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class Asset(BaseModel):
    id: str
    mime_type: str
    format: str
    sample_rate: int
    channels: int
    duration: float
    size: int
    created_at: str
    expires_at: str


class Voice(BaseModel):
    id: str
    model: str
    alias: str | None = None
    kind: str = "reference"
    references: list[Reference] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class JobResult(BaseModel):
    asset_id: str
    mime_type: str
    sample_rate: int
    channels: int
    duration: float
    size: int
    expires_at: str
    provenance: dict[str, Any] = Field(default_factory=dict)


class JobStatus(BaseModel):
    id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    operation: str
    model: str
    stage: str | None = None
    progress: float | None = None
    cancellation_requested: bool = False
    results: list[JobResult] = Field(default_factory=list)
    error: ErrorDetail | None = None
    status_url: str
    events_url: str
    created_at: str
    updated_at: str
    provenance: dict[str, Any] = Field(default_factory=dict)


REQUESTS = {
    "tts": SpeechRequest,
    "voice_design": VoiceDesignRequest,
    "voice_conversion": VoiceConversionRequest,
}


class SessionRequest(Request):
    """Session creation accepts no caller-selected identity or lifetime."""


class SessionResponse(BaseModel):
    session_id: str
    access_token: str = Field(repr=False)
    token_type: Literal["Bearer"] = "Bearer"
    expires_at: str
