"""Engine plugin interface. Adapters own inference, never routes or job storage."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from .errors import DomainError
from .models import Voice


class Profile(BaseModel):
    id: str
    description: str
    checkpoint: str
    checkpoint_revision: str
    adapter_version: str
    capabilities_revision: str = "1"
    guidance_revision: str = "1"
    availability: Literal["ready", "loadable", "disabled", "unavailable"] = "loadable"
    features: dict[str, bool] = Field(default_factory=lambda: {"tts": True})
    inference_mode: str = "default"
    inline_tags: dict[str, Any] = Field(default_factory=lambda: {"supported": False})
    feature_combinations: list[dict[str, Any]] = Field(default_factory=list)
    instructions: bool = False
    languages: list[str] = Field(default_factory=list)
    language_optional: bool = True
    voice_required: bool = False
    default_voice: str | None = None
    voices: list[Voice] = Field(default_factory=list)
    output_formats: list[str] = Field(default_factory=lambda: ["wav"])
    sample_rates: list[int] = Field(default_factory=lambda: [24000])
    max_text_characters: int = 10000
    max_reference_count: int = 1
    reference_transcript_required: bool = False
    max_reference_duration: float = 60
    extension_schemas: dict[str, dict[str, Any]] = Field(default_factory=dict)
    native_incremental_audio: bool = False
    cancellation: str = "cooperative_after_inference"
    progress: str = "stages"


@dataclass(frozen=True)
class AudioOutput:
    data: bytes
    format: str = "wav"
    provenance: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionContext:
    emit: Callable[[str, float | None], None]
    cancelled: Callable[[], bool]
    asset_path: Callable[[str], Path]


class Adapter(ABC):
    @abstractmethod
    def profiles(self) -> list[Profile]:
        """List metadata without loading weights. IDs must be unique across adapters."""

    @abstractmethod
    def guidance(self, profile: str, feature: str) -> dict[str, Any]:
        """Return summary, input_rules, complete examples and limitations."""

    def validate(self, operation: str, profile: str, request: dict, voice: Voice | None):
        """Check native combinations. Raise DomainError; never mutate inputs."""

    def load(self, profile: str):
        """Called only by the serialized worker before execution."""

    def unload(self, profile: str):
        """Release resources when switching profiles or shutting down."""

    @abstractmethod
    def execute(
        self,
        operation: str,
        profile: str,
        request: dict,
        voice: Voice | None,
        context: ExecutionContext,
    ) -> list[AudioOutput]:
        """Return encoded audio. Inputs are snapshots; paths are trusted asset accessors."""

    def translate_error(self, error: Exception) -> DomainError:
        """Override to classify native failures without leaking secrets or paths."""
        return DomainError("inference_failed", "The model failed to generate audio.")
