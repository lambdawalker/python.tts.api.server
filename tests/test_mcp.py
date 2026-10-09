import importlib.util


def test_mcp_module_exists():
    assert importlib.util.find_spec("tts_api_server.mcp") is not None


def test_mcp_dispatch_parity(tmp_path):
    from tts_api_server.fake import FakeAdapter
    from tts_api_server.mcp import dispatch, tool_definitions
    from tts_api_server.service import Service, Settings

    s = Service([FakeAdapter()], Settings(data_dir=tmp_path))
    s.start()
    try:
        names = {t.name for t in tool_definitions()}
        assert names == {
            "list_models",
            "get_capabilities",
            "get_guidance",
            "list_voices",
            "clone_voice",
            "delete_voice",
            "validate_speech",
            "generate_speech",
            "design_voice",
            "convert_voice",
            "get_job",
            "cancel_job",
        }
        payload = {"text": " hello\n"}
        assert dispatch(s, "alice", "validate_speech", payload) == s.validate_speech(
            "alice", payload
        )
        job = dispatch(s, "alice", "generate_speech", dict(payload, idempotency_key="mcp"))
        assert s.submit("alice", "tts", payload, "mcp")["id"] == job["id"]
    finally:
        s.close()
