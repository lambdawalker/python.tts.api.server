import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_service import make, wait, wav

from tts_api_server.errors import DomainError
from tts_api_server.fake import FakeAdapter
from tts_api_server.service import Service, Settings


def test_restart_fails_interrupted_jobs_without_rerunning(tmp_path):
    script = """
import os, threading, json
from pathlib import Path
from tts_api_server.fake import FakeAdapter
from tts_api_server.service import Service, Settings
class Block(FakeAdapter):
    def execute(self, *args):
        threading.Event().wait(60)
s = Service([Block()], Settings(data_dir=Path(os.environ['STATE_DIR'])))
s.start()
a = s.submit('alice', 'tts', {'text': 'first'}, 'first')
b = s.submit('alice', 'tts', {'text': 'second'}, 'second')
Path(os.environ['STATE_DIR'], 'jobs.json').write_text(json.dumps([a['id'], b['id']]))
os._exit(0)
"""
    env = dict(os.environ, STATE_DIR=str(tmp_path), PYTHONPATH=str(Path("src").resolve()))
    subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=10)
    s = make(tmp_path)
    s.start()
    try:
        for identifier in json.loads((tmp_path / "jobs.json").read_text()):
            job = s.get_job("alice", identifier)
            assert job["status"] == "failed"
            assert job["error"]["code"] == "server_restarted"
            assert job["results"] == []
    finally:
        s.close()


def test_expiration_event_cursor_and_single_process_lock(tmp_path):
    clock = [1000.0]
    s = make(tmp_path, clock=lambda: clock[0], asset_ttl=10, event_ttl=10)
    s.start()
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            make(tmp_path)
        job = s.submit("alice", "tts", {"text": "hello"}, "persist")
        done = wait(s, job)
        events = s.events("alice", job["id"])
        assert s.events("alice", job["id"], events[-2][0])[-1][1]["status"] == "succeeded"
        clock[0] += 11
        with pytest.raises(DomainError) as e:
            s.asset("alice", done["results"][0]["asset_id"])
        assert e.value.detail.code == "asset_expired"
        with pytest.raises(DomainError) as e:
            s.events("alice", job["id"], 0)
        assert e.value.detail.code == "event_history_expired"
        assert s.get_job("alice", job["id"])["status"] == "succeeded"
        assert s.submit("alice", "tts", {"text": "hello"}, "persist")["id"] == job["id"]
    finally:
        s.close()


def test_exact_content_and_native_rejection_before_queueing(tmp_path):
    seen = []

    class Capture(FakeAdapter):
        def profiles(self):
            p = super().profiles()[0]
            p.instructions = False
            p.reference_transcript_required = True
            p.extension_schemas = {
                "qwen": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"temperature": {"type": "number", "minimum": 0.1}},
                }
            }
            return [p]

        def execute(self, operation, profile, request, voice, context):
            seen.append((request, voice))
            return super().execute(operation, profile, request, voice, context)

    s = Service([Capture()], Settings(data_dir=tmp_path))
    s.start()
    try:
        a = s.upload("alice", wav())
        with pytest.raises(DomainError) as e:
            s.register_voice("alice", {"references": [{"asset_id": a["id"]}]})
        assert e.value.detail.code == "invalid_reference"
        for payload in [{"instructions": ""}, {"extensions": {"qwen": {"typo": 1}}}]:
            with pytest.raises(DomainError) as e:
                s.submit("alice", "tts", dict(text="ok", **payload))
            assert e.value.detail.code == "unsupported_parameter"
        v = s.register_voice(
            "alice", {"references": [{"asset_id": a["id"], "transcript": " Hi \n"}]}
        )
        job = s.submit("alice", "tts", {"text": " [laugh] Héllo\n ", "voice": {"id": v["id"]}})
        assert wait(s, job)["status"] == "succeeded"
        assert seen[0][0]["text"] == " [laugh] Héllo\n "
        assert seen[0][1].references[0].transcript == " Hi \n"
    finally:
        s.close()


def test_worker_survives_native_error(tmp_path):
    class FailOnce(FakeAdapter):
        def execute(self, operation, profile, request, voice, context):
            if request["text"] == "fail":
                raise RuntimeError("private/native/path")
            return super().execute(operation, profile, request, voice, context)

    s = Service([FailOnce()], Settings(data_dir=tmp_path))
    s.start()
    try:
        failed = wait(s, s.submit("alice", "tts", {"text": "fail"}))
        assert failed["error"]["code"] == "inference_failed"
        assert "private" not in json.dumps(failed)
        assert wait(s, s.submit("alice", "tts", {"text": "works"}))["status"] == "succeeded"
    finally:
        s.close()


def test_truncated_audio_is_rejected(tmp_path):
    import io

    import soundfile as sf

    stream = io.BytesIO()
    sf.write(stream, [0.0] * 24000, 24000, format="FLAC")
    s = make(tmp_path)
    try:
        with pytest.raises(DomainError) as error:
            s.upload("alice", stream.getvalue()[:42])
        assert error.value.detail.code == "invalid_reference"
    finally:
        s.close()


def test_worker_survives_broken_native_error_translation(tmp_path):
    class BadTranslator(FakeAdapter):
        def execute(self, operation, profile, request, voice, context):
            if request["text"] == "fail":
                raise RuntimeError("native failure")
            return super().execute(operation, profile, request, voice, context)

        def translate_error(self, error):
            raise ValueError("broken error translator")

    s = Service([BadTranslator()], Settings(data_dir=tmp_path))
    s.start()
    try:
        failed = wait(s, s.submit("alice", "tts", {"text": "fail"}))
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "inference_failed"
        assert wait(s, s.submit("alice", "tts", {"text": "works"}))["status"] == "succeeded"
    finally:
        s.close()


def test_nonfinite_input_and_settings_are_rejected(tmp_path):
    from pydantic import ValidationError

    from tts_api_server.models import SpeechRequest

    with pytest.raises(ValidationError):
        SpeechRequest(text="hi", extensions={"x": {"value": float("nan")}})
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, asset_ttl=float("nan"))


def test_conversion_does_not_require_synthesis_language(tmp_path):
    from tts_api_server.models import Voice

    class Conversion(FakeAdapter):
        def profiles(self):
            p = super().profiles()[0]
            p.features["voice_conversion"] = True
            p.language_optional = False
            p.voices = [Voice(id="preset", model=p.id, kind="preset")]
            return [p]

    s = Service([Conversion()], Settings(data_dir=tmp_path))
    s.start()
    try:
        a = s.upload("alice", wav())
        job = s.submit(
            "alice", "voice_conversion", {"source_asset_id": a["id"], "voice": {"id": "preset"}}
        )
        assert wait(s, job)["status"] == "succeeded"
    finally:
        s.close()
