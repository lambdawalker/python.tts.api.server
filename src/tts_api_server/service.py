"""Shared application services and a durable, serialized inference worker."""

import copy
import hashlib
import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .adapter import Adapter, ExecutionContext
from .assets import inspect_audio, timestamp, write_asset
from .errors import DomainError, validation_error
from .models import REQUESTS, Voice, VoiceRegistration
from .sessions import Sessions
from .store import Store

log = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled"}


@dataclass
class Settings:
    data_dir: Path = Path(".tts-data")
    default_model: str | None = None
    max_queue: int = 32
    max_upload_bytes: int = 32 * 1024 * 1024
    max_request_bytes: int = 1024 * 1024
    max_audio_duration: float = 600
    asset_ttl: float = 86400
    event_ttl: float = 86400
    event_limit: int = 1000
    idempotency_ttl: float = 86400
    anonymous_sessions: bool = False
    session_ttl: float = 86400
    max_sessions: int = 10000
    tokens: dict[str, str] = field(default_factory=dict, repr=False)
    clock: Callable[[], float] = time.time

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        if self.anonymous_sessions and self.tokens:
            raise ValueError("Anonymous sessions cannot be combined with static tokens")
        for name in (
            "session_ttl",
            "max_sessions",
            "max_queue",
            "max_upload_bytes",
            "max_request_bytes",
            "max_audio_duration",
            "asset_ttl",
            "event_ttl",
            "event_limit",
            "idempotency_ttl",
        ):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if any(not key or not value for key, value in self.tokens.items()):
            raise ValueError("Tokens and caller IDs must not be empty")


class Service:
    def __init__(self, adapters: list[Adapter], settings: Settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.registry = {}
        for adapter in adapters:
            for profile in adapter.profiles():
                if profile.id == "default" or profile.id in self.registry:
                    raise ValueError("Profiles must have unique IDs other than default")
                for schema in profile.extension_schemas.values():
                    Draft202012Validator.check_schema(schema)
                self.registry[profile.id] = (adapter, profile.model_copy(deep=True))
        if not self.registry:
            raise ValueError("At least one adapter profile is required")
        self.default = settings.default_model or next(iter(self.registry))
        if self.default not in self.registry:
            raise ValueError("Configured default model is not registered")
        self.store = Store(settings.data_dir)
        self.sessions = Sessions(self.store, self.lock, settings)
        self.directory = settings.data_dir / "assets"
        self.thread = None
        self.stopping = False
        self.closed = False
        self.loaded = None

    def profile(self, model=None):
        identifier = self.default if model in (None, "default") else model
        if identifier not in self.registry:
            raise DomainError("model_not_found", "Unknown model profile.", "model")
        return self.registry[identifier]

    def list_models(self):
        return {
            "api_version": "1.0",
            "default": self.default,
            "models": [
                dict(
                    p.model_dump(),
                    capabilities_url="/v1/capabilities?model=" + quote(p.id, safe=""),
                )
                for _, p in self.registry.values()
            ],
        }

    def capabilities(self, model=None):
        _, p = self.profile(model)
        return {
            "api_version": "1.0",
            "model": p.id,
            "checkpoint": p.checkpoint,
            "checkpoint_revision": p.checkpoint_revision,
            "adapter_version": p.adapter_version,
            "capabilities_revision": p.capabilities_revision,
            "availability": p.availability,
            "features": {
                f: {"supported": p.features.get(f, False)}
                for f in ("tts", "voice_cloning", "voice_design", "voice_conversion")
            },
            "controls": {
                "instructions": {"supported": p.instructions, "scope": "utterance"},
                "inline_tags": p.inline_tags,
            },
            "inference_mode": p.inference_mode,
            "feature_combinations": p.feature_combinations,
            "languages": p.languages,
            "language_optional": p.language_optional,
            "voice_required": p.voice_required,
            "default_voice": p.default_voice,
            "output": {"formats": p.output_formats, "sample_rates": p.sample_rates},
            "references": {
                "max_count": p.max_reference_count,
                "transcript_required": p.reference_transcript_required,
                "max_duration_seconds": p.max_reference_duration,
                "formats": ["wav", "flac"],
            },
            "limits": {
                "text_characters": p.max_text_characters,
                "upload_bytes": self.settings.max_upload_bytes,
                "queue": self.settings.max_queue,
            },
            "native_incremental_audio": p.native_incremental_audio,
            "cancellation": p.cancellation,
            "progress": p.progress,
            "extension_schemas": p.extension_schemas,
            "retention": {
                "assets_seconds": self.settings.asset_ttl,
                "events_seconds": self.settings.event_ttl,
                "events_per_job": self.settings.event_limit,
                "idempotency_seconds": self.settings.idempotency_ttl,
            },
            "guidance": {f: self.guidance_url(p.id, f) for f, yes in p.features.items() if yes},
        }

    @staticmethod
    def guidance_url(model, feature):
        return f"/v1/guidance/{feature}?model={quote(model, safe='')}"

    def guidance(self, feature, model=None):
        adapter, p = self.profile(model)
        if not p.features.get(feature, False):
            raise DomainError("unsupported_feature", "This profile does not support the feature.")
        return dict(
            adapter.guidance(p.id, feature),
            feature=feature,
            model=p.id,
            revision=p.guidance_revision,
            checkpoint_revision=p.checkpoint_revision,
            adapter_version=p.adapter_version,
        )

    def _asset(self, owner, identifier, allow_expired=False):
        data = self.store.get("asset", identifier, owner)
        if data is None:
            raise DomainError("asset_not_found", "Asset not found.")
        if (
            not allow_expired
            and datetime.fromisoformat(data["expires_at"]).timestamp() <= self.settings.clock()
        ):
            raise DomainError("asset_expired", "Asset has expired.")
        path = self.directory / identifier
        if not path.is_file():
            raise DomainError("asset_expired", "Asset bytes are no longer retained.")
        return data, path

    def asset(self, owner, identifier):
        with self.lock:
            return self._asset(owner, identifier)

    def upload(self, owner, data: bytes):
        metadata = inspect_audio(
            data, self.settings.max_upload_bytes, self.settings.max_audio_duration
        )
        with self.lock:
            asset = write_asset(
                self.directory, data, metadata, self.settings.clock(), self.settings.asset_ttl
            )
            self.store.put("asset", asset["id"], owner, asset)
            return asset

    def list_voices(self, owner, model=None):
        _, p = self.profile(model)
        with self.lock:
            return [v.model_dump() for v in p.voices] + [
                v for v in self.store.all("voice", owner) if v["model"] == p.id
            ]

    def _voice(self, owner, p, selector):
        if selector is None:
            selector = {"id": p.default_voice} if p.default_voice else None
        if selector is None:
            if p.voice_required:
                raise DomainError("invalid_request", "A compatible voice is required.", "voice")
            return None
        key = "id" if selector.get("id") else "alias"
        voice = next(
            (v for v in self.list_voices(owner, p.id) if v.get(key) == selector[key]), None
        )
        if voice is None:
            raise DomainError(
                "voice_unavailable", "Voice is not available for this profile.", "voice"
            )
        for ref in voice["references"]:
            self._asset(owner, ref["asset_id"])
        return Voice.model_validate(voice)

    @staticmethod
    def parse(cls, payload):
        try:
            return cls.model_validate(payload)
        except ValidationError as exc:
            raise validation_error(exc) from exc

    def register_voice(self, owner, payload):
        request = self.parse(VoiceRegistration, payload)
        adapter, p = self.profile(request.model)
        if not p.features.get("voice_cloning"):
            raise DomainError(
                "unsupported_feature", "This profile cannot register reference voices."
            )
        with self.lock:
            if len(request.references) > p.max_reference_count:
                raise DomainError(
                    "invalid_reference", "Too many reference recordings.", "references"
                )
            for ref in request.references:
                metadata, _ = self._asset(owner, ref.asset_id)
                if metadata["duration"] > p.max_reference_duration:
                    raise DomainError(
                        "invalid_reference", "Reference exceeds profile duration limit."
                    )
                if p.reference_transcript_required and not ref.transcript:
                    raise DomainError(
                        "invalid_reference",
                        "Reference transcript is required.",
                        "references.transcript",
                    )
            if request.alias and any(
                v.get("alias") == request.alias for v in self.list_voices(owner, p.id)
            ):
                raise DomainError("alias_conflict", "Voice alias already exists for this profile.")
            voice = Voice(
                id="voice_" + uuid.uuid4().hex,
                model=p.id,
                alias=request.alias,
                references=request.references,
                provenance={"checkpoint_revision": p.checkpoint_revision},
            )
            adapter.validate(
                "voice_registration", p.id, request.model_dump(), voice.model_copy(deep=True)
            )
            self.store.put("voice", voice.id, owner, voice.model_dump())
            return voice.model_dump()

    def delete_voice(self, owner, identifier):
        with self.lock:
            if self.store.get("voice", identifier, owner) is None:
                raise DomainError("voice_unavailable", "Registered voice not found.")
            self.store.delete("voice", identifier)

    def _validate(self, owner, operation, request):
        adapter, p = self.profile(request.model)
        if not p.features.get(operation, False):
            raise DomainError("unsupported_feature", "This profile does not support the operation.")
        if p.availability not in {"ready", "loadable"}:
            raise DomainError("model_unavailable", "Model is not available.", "model", True)
        payload = request.model_dump(exclude_none=True)
        text = payload.get("text", payload.get("preview_text", ""))
        if (
            len(text) > p.max_text_characters
            or len(payload.get("description", "")) > p.max_text_characters
        ):
            raise DomainError(
                "invalid_request", "Text exceeds the profile character limit.", "text"
            )
        if "instructions" in payload and not p.instructions:
            raise DomainError(
                "unsupported_parameter",
                "This profile does not support instructions.",
                "instructions",
                guidance_url=self.guidance_url(p.id, operation),
            )
        language = payload.get("language")
        if operation != "voice_conversion" and language is None and not p.language_optional:
            raise DomainError("invalid_request", "Language is required.", "language")
        if language is not None and language not in p.languages:
            raise DomainError("unsupported_language", "Language is not supported.", "language")
        output = request.output
        if output.format not in p.output_formats:
            raise DomainError(
                "unsupported_parameter", "Output format is not supported.", "output.format"
            )
        if output.sample_rate is not None and output.sample_rate not in p.sample_rates:
            raise DomainError(
                "unsupported_parameter", "Sample rate is not supported.", "output.sample_rate"
            )
        if payload.get("guidance_revision", p.guidance_revision) != p.guidance_revision:
            raise DomainError(
                "guidance_revision_mismatch",
                f"Current guidance revision is {p.guidance_revision}.",
                "guidance_revision",
                guidance_url=self.guidance_url(p.id, operation),
            )
        for namespace, value in request.extensions.items():
            schema = p.extension_schemas.get(namespace)
            if schema is None:
                raise DomainError(
                    "unsupported_parameter",
                    "Unknown extension namespace.",
                    "extensions." + namespace,
                )
            errors = list(Draft202012Validator(schema).iter_errors(value))
            if errors:
                raise DomainError(
                    "unsupported_parameter", errors[0].message, "extensions." + namespace
                )
        voice = None if operation == "voice_design" else self._voice(owner, p, payload.get("voice"))
        if operation == "voice_conversion":
            self._asset(owner, payload["source_asset_id"])
        payload["model"] = p.id
        adapter.validate(operation, p.id, copy.deepcopy(payload), copy.deepcopy(voice))
        return adapter, p, payload, voice

    def validate_speech(self, owner, payload):
        request = self.parse(REQUESTS["tts"], payload)
        with self.lock:
            _, p, _, _ = self._validate(owner, "tts", request)
            return {"valid": True, "model": p.id, "guidance_revision": p.guidance_revision}

    @staticmethod
    def public(job):
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def _record(self, job):
        now = self.settings.clock()
        job["updated_at"] = timestamp(now)
        job["_seq"] = job.get("_seq", 0) + 1
        self.store.put("job", job["id"], job["_owner"], job)
        self.store.db.execute(
            "INSERT INTO events VALUES (?,?,?,?)",
            (job["id"], job["_seq"], now, json.dumps(self.public(job))),
        )
        self.store.db.execute(
            "DELETE FROM events WHERE job=? AND (seq<=? OR created<?)",
            (job["id"], job["_seq"] - self.settings.event_limit, now - self.settings.event_ttl),
        )
        self.wake.notify_all()

    def submit(self, owner, operation, payload, idempotency_key=None):
        request = self.parse(REQUESTS[operation], payload)
        canonical = json.dumps(
            request.model_dump(exclude_none=True),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        if idempotency_key is not None and not re.fullmatch(r"[!-~]{1,256}", idempotency_key):
            raise DomainError(
                "invalid_request", "Idempotency key must be 1–256 printable ASCII characters."
            )
        with self.lock, self.store.transaction():
            now = self.settings.clock()
            if self.stopping or self.thread is None or not self.thread.is_alive():
                raise DomainError(
                    "server_unavailable", "Inference worker is not running.", retryable=True
                )
            if idempotency_key:
                row = self.store.db.execute(
                    "SELECT fingerprint,job FROM idempotency "
                    "WHERE owner=? AND operation=? AND key=? AND expires>?",
                    (owner, operation, idempotency_key, now),
                ).fetchone()
                if row:
                    if row[0] != fingerprint:
                        raise DomainError(
                            "idempotency_conflict", "Key was used for a different request."
                        )
                    return self.public(self.store.get("job", row[1], owner))
            _, p, accepted, voice = self._validate(owner, operation, request)
            if (
                sum(j["status"] == "queued" for j in self.store.all("job"))
                >= self.settings.max_queue
            ):
                raise DomainError("queue_full", "Inference queue is full.", retryable=True)
            identifier = "job_" + uuid.uuid4().hex
            job = {
                "id": identifier,
                "status": "queued",
                "operation": operation,
                "model": p.id,
                "stage": "queued",
                "progress": None,
                "cancellation_requested": False,
                "results": [],
                "error": None,
                "status_url": f"/v1/jobs/{identifier}",
                "events_url": f"/v1/jobs/{identifier}/events",
                "created_at": timestamp(now),
                "provenance": {
                    "checkpoint": p.checkpoint,
                    "checkpoint_revision": p.checkpoint_revision,
                    "adapter_version": p.adapter_version,
                    "guidance_revision": p.guidance_revision,
                },
                "_owner": owner,
                "_request": accepted,
                "_voice": voice.model_dump() if voice else None,
            }
            self._record(job)
            if idempotency_key:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO idempotency VALUES (?,?,?,?,?,?)",
                    (
                        owner,
                        operation,
                        idempotency_key,
                        fingerprint,
                        identifier,
                        now + self.settings.idempotency_ttl,
                    ),
                )
            return self.public(job)

    def get_job(self, owner, identifier):
        with self.lock:
            job = self.store.get("job", identifier, owner)
            if job is None:
                raise DomainError("job_not_found", "Job not found.")
            return self.public(job)

    def cancel_job(self, owner, identifier):
        with self.lock, self.store.transaction():
            self.get_job(owner, identifier)
            job = self.store.get("job", identifier, owner)
            if job["status"] in TERMINAL:
                return self.public(job)
            job["cancellation_requested"] = True
            if job["status"] == "queued":
                job.update(status="cancelled", stage="cancelled")
            self._record(job)
            return self.public(job)

    def events(self, owner, identifier, cursor=0):
        with self.lock:
            self.get_job(owner, identifier)
            self.store.db.execute(
                "DELETE FROM events WHERE job=? AND created<?",
                (identifier, self.settings.clock() - self.settings.event_ttl),
            )
            rows = self.store.db.execute(
                "SELECT seq,data FROM events WHERE job=? ORDER BY seq", (identifier,)
            ).fetchall()
            job = self.store.get("job", identifier, owner)
            oldest = rows[0][0] if rows else job["_seq"] + 1
            if cursor < oldest - 1:
                raise DomainError(
                    "event_history_expired", f"Event history expired. Poll /v1/jobs/{identifier}."
                )
            if cursor > job["_seq"]:
                raise DomainError("invalid_request", "Event cursor is ahead of this job.")
            return [(seq, json.loads(data)) for seq, data in rows if seq > cursor]

    def start(self):
        with self.lock:
            if self.thread is not None:
                return
            with self.store.transaction():
                for job in self.store.all("job"):
                    if job["status"] not in TERMINAL:
                        job.update(
                            status="failed",
                            stage="failed",
                            error={
                                "code": "server_restarted",
                                "message": "Server stopped before completion.",
                                "retryable": False,
                            },
                        )
                        self._record(job)
            self.thread = threading.Thread(target=self._worker, name="tts-inference", daemon=True)
            self.thread.start()

    def _worker(self):
        try:
            while True:
                with self.wake:
                    if self.stopping:
                        return
                    jobs = [j for j in self.store.all("job") if j["status"] == "queued"]
                    if not jobs:
                        self._cleanup()
                        self.wake.wait(timeout=1)
                        continue
                    job = jobs[0]
                    with self.store.transaction():
                        job.update(status="running", stage="loading_model")
                        self._record(job)
                self._execute(job)
        finally:
            if self.loaded:
                adapter, profile = self.loaded
                try:
                    adapter.unload(profile)
                except Exception:
                    log.exception("Adapter unload failed")
                self.loaded = None

    def _execute(self, job):
        adapter, p = self.registry[job["model"]]
        owner, identifier = job["_owner"], job["id"]

        def cancelled():
            with self.lock:
                return self.store.get("job", identifier)["cancellation_requested"]

        def emit(stage, progress=None):
            if progress is not None and not 0 <= progress <= 100:
                raise ValueError("Progress must be measured in [0,100] or None")
            with self.lock, self.store.transaction():
                current = self.store.get("job", identifier)
                current.update(stage=stage, progress=progress)
                self._record(current)

        allowed = {r["asset_id"] for r in (job["_voice"] or {}).get("references", [])}
        if "source_asset_id" in job["_request"]:
            allowed.add(job["_request"]["source_asset_id"])

        def asset_path(asset_id):
            if asset_id not in allowed:
                raise DomainError("asset_not_found", "Asset is not part of the accepted job.")
            with self.lock:
                return self._asset(owner, asset_id, allow_expired=True)[1]

        try:
            if self.loaded != (adapter, p.id):
                if self.loaded:
                    self.loaded[0].unload(self.loaded[1])
                    self.loaded = None
                adapter.load(p.id)
                self.loaded = (adapter, p.id)
            outputs = []
            if not cancelled():
                emit("generating")
                outputs = adapter.execute(
                    job["operation"],
                    p.id,
                    copy.deepcopy(job["_request"]),
                    Voice.model_validate(job["_voice"]) if job["_voice"] else None,
                    ExecutionContext(emit, cancelled, asset_path),
                )
            if not cancelled() and not outputs:
                raise DomainError("inference_failed", "Adapter returned no audio.")
            emit("encoding")
            results = []
            with self.lock:
                if not cancelled():
                    for output in outputs:
                        asset = self.upload(owner, output.data)
                        requested = job["_request"]["output"]
                        if (
                            asset["format"] != output.format
                            or asset["format"] != requested["format"]
                        ):
                            raise DomainError(
                                "inference_failed", "Adapter returned the wrong audio format."
                            )
                        rate = requested.get("sample_rate")
                        if asset["sample_rate"] not in p.sample_rates or (
                            rate and rate != asset["sample_rate"]
                        ):
                            raise DomainError(
                                "inference_failed", "Adapter returned the wrong sample rate."
                            )
                        results.append(
                            dict(
                                asset_id=asset["id"],
                                **{
                                    k: v
                                    for k, v in asset.items()
                                    if k not in {"id", "created_at", "format"}
                                },
                                provenance=dict(output.provenance or {}, **job["provenance"]),
                            )
                        )
                with self.store.transaction():
                    current = self.store.get("job", identifier)
                    status = "cancelled" if current["cancellation_requested"] else "succeeded"
                    current.update(status=status, stage=status, results=results, progress=None)
                    self._record(current)
        except Exception as exc:
            log.exception("Job %s failed", identifier)
            try:
                error = exc if isinstance(exc, DomainError) else adapter.translate_error(exc)
                if not isinstance(error, DomainError):
                    raise TypeError("Adapter returned an invalid error")
            except Exception:
                log.exception("Adapter error translation failed for job %s", identifier)
                error = DomainError("inference_failed", "The model failed to generate audio.")
            with self.lock, self.store.transaction():
                current = self.store.get("job", identifier)
                status = "cancelled" if current["cancellation_requested"] else "failed"
                current.update(
                    status=status, stage=status, error=error.detail.model_dump(), progress=None
                )
                self._record(current)

    def _cleanup(self):
        now = self.settings.clock()
        protected = set()
        for job in self.store.all("job"):
            if job["status"] not in TERMINAL:
                protected.update(r["asset_id"] for r in (job["_voice"] or {}).get("references", []))
                if "source_asset_id" in job["_request"]:
                    protected.add(job["_request"]["source_asset_id"])
        for asset in self.store.all("asset"):
            if (
                asset["id"] not in protected
                and datetime.fromisoformat(asset["expires_at"]).timestamp() <= now
            ):
                (self.directory / asset["id"]).unlink(missing_ok=True)
        self.store.db.execute(
            "DELETE FROM events WHERE created<?", (now - self.settings.event_ttl,)
        )
        self.store.db.execute("DELETE FROM idempotency WHERE expires<=?", (now,))
        self.store.db.execute("DELETE FROM sessions WHERE expires<=?", (now,))

    def close(self):
        with self.wake:
            if self.closed:
                return
            self.stopping = True
            self.wake.notify_all()
        if self.thread:
            self.thread.join()  # Never close storage or unload while native inference still runs.
        with self.lock:
            self.store.close()
            self.closed = True
