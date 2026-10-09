import importlib.util


def test_server_contract_exists():
    assert importlib.util.find_spec("tts_api_server.models") is not None


def test_requests_preserve_content_and_reject_unknown_fields():
    import pytest
    from pydantic import ValidationError

    from tts_api_server.models import SpeechRequest, VoiceSelector

    text = "  [laugh] Héllo!\n"
    request = SpeechRequest(text=text, instructions="  Softly.  ")
    assert request.text == text
    assert request.instructions == "  Softly.  "
    with pytest.raises(ValidationError):
        SpeechRequest(text="ok", unsupported=True)
    with pytest.raises(ValidationError):
        VoiceSelector(id="a", alias="b")


def test_fake_adapter_is_explicit_and_returns_audio():
    from tts_api_server.adapter import ExecutionContext
    from tts_api_server.fake import FakeAdapter

    adapter = FakeAdapter()
    profile = adapter.profiles()[0]
    assert profile.id == "fake-v1"
    assert "test" in profile.description.lower()
    result = adapter.execute(
        "tts",
        profile.id,
        {"text": " hi "},
        None,
        ExecutionContext(lambda *a: None, lambda: False, lambda x: None),
    )
    assert result[0].data[:4] == b"RIFF"
    assert result[0].format == "wav"


def test_profile_can_describe_native_controls_and_feature_combinations(tmp_path):
    from tts_api_server.fake import FakeAdapter
    from tts_api_server.service import Service, Settings

    class Tagged(FakeAdapter):
        def profiles(self):
            p = super().profiles()[0]
            p.inline_tags = {"supported": True, "scope": "span", "catalog_exhaustive": False}
            p.feature_combinations = [{"features": ["tts", "voice_cloning"], "instructions": False}]
            p.inference_mode = "native-tags"
            return [p]

    s = Service([Tagged()], Settings(data_dir=tmp_path))
    try:
        cap = s.capabilities()
        assert cap["controls"]["inline_tags"]["supported"]
        assert cap["feature_combinations"][0]["instructions"] is False
        assert cap["inference_mode"] == "native-tags"
    finally:
        s.close()
