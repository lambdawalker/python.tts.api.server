import importlib.util
import io
import threading
import time
import wave

import pytest


def wav():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(24000)
        f.writeframes(b"\0\0" * 240)
    return stream.getvalue()


def wait(service, job, status=None):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = service.get_job("alice", job["id"])
        if value["status"] == status or (
            status is None and value["status"] in {"succeeded", "failed", "cancelled"}
        ):
            return value
        time.sleep(0.005)
    raise AssertionError(value)


def test_service_exists():
    assert importlib.util.find_spec("tts_api_server.service") is not None


def make(tmp_path, **kwargs):
    from tts_api_server.fake import FakeAdapter
    from tts_api_server.service import Service, Settings

    return Service([FakeAdapter()], Settings(data_dir=tmp_path, **kwargs))


def test_lifecycle_idempotency_restart_and_ownership(tmp_path):
    from tts_api_server.errors import DomainError

    service = make(tmp_path)
    service.start()
    try:
        job = service.submit("alice", "tts", {"text": "  Héllo\n"}, "same")
        done = wait(service, job)
        assert done["status"] == "succeeded"
        assert service.submit("alice", "tts", {"text": "  Héllo\n"}, "same")["id"] == job["id"]
        with pytest.raises(DomainError, match="different"):
            service.submit("alice", "tts", {"text": "different"}, "same")
        with pytest.raises(DomainError) as error:
            service.get_job("bob", job["id"])
        assert error.value.detail.code == "job_not_found"
        asset = done["results"][0]["asset_id"]
        assert service.asset("alice", asset)[1].read_bytes()[:4] == b"RIFF"
        with pytest.raises(DomainError):
            service.asset("bob", asset)
        assert len(service.events("alice", job["id"], 0)) >= 3
    finally:
        service.close()
    service = make(tmp_path)
    service.start()
    try:
        assert service.get_job("alice", job["id"])["status"] == "succeeded"
        assert service.submit("alice", "tts", {"text": "  Héllo\n"}, "same")["id"] == job["id"]
    finally:
        service.close()


def test_validation_and_voice_registration(tmp_path):
    from tts_api_server.errors import DomainError

    s = make(tmp_path)
    try:
        a = s.upload("alice", wav())
        v = s.register_voice(
            "alice",
            {"alias": "me", "references": [{"asset_id": a["id"], "transcript": "  exact\n"}]},
        )
        assert v["references"][0]["transcript"] == "  exact\n"
        assert s.validate_speech("alice", {"text": "hi", "voice": {"alias": "me"}})["valid"]
        for payload, code in [
            ({"text": "hi", "foo": 1}, "invalid_request"),
            ({"text": "hi", "language": "xx"}, "unsupported_language"),
            ({"text": "hi", "extensions": {"unknown": {}}}, "unsupported_parameter"),
            ({"text": "hi", "guidance_revision": "old"}, "guidance_revision_mismatch"),
        ]:
            with pytest.raises(DomainError) as e:
                s.validate_speech("alice", payload)
            assert e.value.detail.code == code
        with pytest.raises(DomainError):
            s.register_voice("bob", {"references": [{"asset_id": a["id"]}]})
        s.delete_voice("alice", v["id"])
        assert s.list_voices("alice", None) == []
        assert s.asset("alice", a["id"])[1].exists()
    finally:
        s.close()


def test_cancellation_queue_limit_and_voice_snapshot(tmp_path):
    from tts_api_server.errors import DomainError
    from tts_api_server.fake import FakeAdapter
    from tts_api_server.service import Service, Settings

    entered, release = threading.Event(), threading.Event()
    seen = []

    class Blocking(FakeAdapter):
        def execute(self, operation, profile, request, voice, context):
            seen.append((request, voice))
            entered.set()
            assert release.wait(3)
            return super().execute(operation, profile, request, voice, context)

    s = Service([Blocking()], Settings(data_dir=tmp_path, max_queue=1))
    s.start()
    try:
        a = s.upload("alice", wav())
        v = s.register_voice("alice", {"references": [{"asset_id": a["id"]}]})
        one = s.submit("alice", "tts", {"text": "one", "voice": {"id": v["id"]}}, "one")
        assert entered.wait(2)
        two = s.submit("alice", "tts", {"text": "two"})
        with pytest.raises(DomainError) as e:
            s.submit("alice", "tts", {"text": "three"})
        assert e.value.detail.code == "queue_full"
        assert s.cancel_job("alice", two["id"])["status"] == "cancelled"
        cancelled = s.cancel_job("alice", one["id"])
        assert cancelled["status"] == "running"
        assert cancelled["cancellation_requested"]
        s.delete_voice("alice", v["id"])
        assert (
            s.submit("alice", "tts", {"text": "one", "voice": {"id": v["id"]}}, "one")["id"]
            == one["id"]
        )
        release.set()
        assert wait(s, one)["status"] == "cancelled"
        assert seen[0][1].references[0].asset_id == a["id"]
    finally:
        release.set()
        s.close()
