"""Validated audio containers and opaque, atomic file storage."""

import io
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import soundfile as sf

from .errors import DomainError


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def inspect_audio(data: bytes, max_bytes: int, max_duration: float) -> dict:
    if len(data) > max_bytes:
        raise DomainError("upload_too_large", "Audio exceeds the configured byte limit.")
    try:
        info = sf.info(io.BytesIO(data))
    except (RuntimeError, ValueError) as exc:
        raise DomainError(
            "invalid_reference", "Upload must be a valid WAV or FLAC audio file."
        ) from exc
    if info.format not in {"WAV", "FLAC"} or info.frames <= 0 or info.samplerate <= 0:
        raise DomainError(
            "invalid_reference", "Only nonempty WAV and FLAC containers are accepted."
        )
    if info.channels > 8 or info.duration > max_duration:
        raise DomainError("invalid_reference", "Audio exceeds channel or duration limits.")
    # Header metadata is insufficient: reject corrupt/truncated audio before persistence.
    # Bound memory by decoding in small blocks and bound CPU/output by actual frame count.
    decoded = 0
    try:
        with sf.SoundFile(io.BytesIO(data)) as source:
            while True:
                block = source.read(65536, dtype="float32", always_2d=True)
                if len(block) == 0:
                    break
                decoded += len(block)
                if decoded > max_duration * info.samplerate or decoded > info.frames:
                    raise DomainError("invalid_reference", "Decoded audio exceeds declared limits.")
    except (RuntimeError, ValueError) as exc:
        raise DomainError("invalid_reference", "Audio is corrupt or truncated.") from exc
    if decoded != info.frames:
        raise DomainError("invalid_reference", "Audio is corrupt or truncated.")
    return {
        "format": info.format.lower(),
        "mime_type": {"WAV": "audio/wav", "FLAC": "audio/flac"}[info.format],
        "sample_rate": info.samplerate,
        "channels": info.channels,
        "duration": info.duration,
        "size": len(data),
    }


def write_asset(directory: Path, data: bytes, metadata: dict, now: float, ttl: float) -> dict:
    identifier = "asset_" + uuid.uuid4().hex
    path = directory / identifier
    temporary = directory / (identifier + ".tmp")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(metadata, id=identifier, created_at=timestamp(now), expires_at=timestamp(now + ttl))
